#!/usr/bin/env python3
"""Unattended supervisor for one paid run on the experiment VM.

Generalizes the per-run supervisors used for the October 15-16 runs (kept, unchanged, in the
ignored runs/phase-b/<run>/ folders). In a tmux session on the VM it:

1. refuses to start twice for the same phase directory (``supervisor.started`` survives, so a
   second supervisor for the same phase never launches);
2. backs up the budget ledger, then launches ``scripts/run_pilot.py --config CONFIG
   --launch-paid-pilot`` with a minimal environment (the provider key is never passed on; the
   runtime loads it from the operator's private .env). The run's budget is CONFIG's
   ``run_budget_usd`` (else $100), the one spending control (docs/plan.md, "One per-run budget,
   no manual approvals (October 20, 2026)");
3. writes ``live-checkpoint.json`` every 30 s while the run is going (event counts, prompt-cache
   share, the run's budget and committed spend, ledger history, agent exits, run stop);
4. afterwards backs up the ledger and, unless --no-postrun, runs the follow-ups in order:
   reconcile_ledger.py (settles uncertain holds from OpenRouter's unpaid generation records; a
   failure is recorded and never stops the others), evaluate_run.py --dry-run, evaluate_run.py,
   timeline.py; then, when CONFIG has ``auto_behavior_judge`` true, the PAID behavior judge
   (scripts/run_transcript_judge.py --run-config CONFIG: its budget is CONFIG's
   ``behavior_judge_max_usd``, else $50; this checkout's state/budget.sqlite and .env; it
   refreshes its own route pricing record when needed). Its outputs land in RUN/analysis/judge/
   (packaged with the run); its outcome is recorded in behavior-judge.json and never changes
   the worker exit or stops the packaging;
5. packages the run and phase records with SHA-256 checksums and waits up to --retrieval-wait
   seconds for a ``retrieved.marker`` in the phase directory;
6. shuts the VM down only when it is idle (no Docker containers, no other tmux session, no
   unexpected process of this user, checked six times); otherwise it leaves the VM running and
   records why. --no-shutdown skips this step.

The operator uses their own provider key in their private .env (docs/plan.md, "Collaborator
budgets and keys"). Example (from the operator's checkout on the VM):

  tmux new -s gdm-RUN_ID
  .venv/bin/python scripts/supervise_run.py --config runs/phase-b/RUN_ID/launch-config.json \\
      --phase-dir runs/phase-b/RUN_ID --tmux-session gdm-RUN_ID 2>&1 | tee runs/phase-b/RUN_ID/supervisor-console.log

CONFIG must name the run directory explicitly (``run_dir``). Researcher-only: never copy this
into agent workspaces.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLLOW_UPS = ("reconcile", "evaluate-dry", "evaluate", "timeline")
NONBLOCKING_FOLLOW_UPS = ("reconcile",)  # bookkeeping: a failure is recorded, the others still run
JUDGE_STEPS = ("behavior-judge",)  # after FOLLOW_UPS, only with auto_behavior_judge true
JUDGE_OUTCOMES = {0: "complete", 2: "errors_or_held_agents", 3: "stopped", 4: "refused"}
IDLE_PROCESSES = {"bash", "sh", "sshd", "sshd-session", "sshd-auth", "tmux: server", "tmux", "systemd", "(sd-pam)",
                  "dbus-daemon", "ps", "sleep"}
ENV_KEYS = ("PATH", "HOME", "USER", "LANG", "TERM", "XDG_RUNTIME_DIR")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Supervisor:
    def __init__(self, args):
        self.root = Path(args.root).resolve()
        self.config_path = Path(args.config).resolve()
        self.phase = Path(args.phase_dir).resolve()
        self.ledger = self.root / "state" / "budget.sqlite"
        self.session = args.tmux_session
        self.args = args
        config = json.loads(self.config_path.read_text())
        if not config.get("run_dir"):
            raise SystemExit("CONFIG must set run_dir explicitly (the supervisor packages that directory)")
        run_dir = Path(config["run_dir"])
        self.run = (run_dir if run_dir.is_absolute() else self.root / run_dir).resolve()
        self.env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}  # never the provider key
        self.env["PYTHONPATH"] = "src"
        self.settled_at_launch = None
        self.judge_record = None

    # -- records -------------------------------------------------------------
    def status(self, stage, **fields):
        (self.phase / "supervisor-status.json").write_text(json.dumps({"stage": stage, "utc": now(), **fields},
                                                                      indent=2))

    def backup(self, name):
        """Copy the ledger into the phase directory (nothing to copy before its first request)."""
        if not self.ledger.exists():
            return False
        with sqlite3.connect("file:" + str(self.ledger) + "?mode=ro", uri=True) as src:
            with sqlite3.connect(str(self.phase / name)) as dst:
                src.backup(dst)

    def snapshot(self):
        sys.path.insert(0, str(self.root / "src"))
        from research_swarm.budget import read_snapshot
        return read_snapshot(self.ledger) or {}

    def run_budget(self):
        """This run's budget and committed spend from the ledger (run id = the run directory's name)."""
        sys.path.insert(0, str(self.root / "src"))
        from research_swarm.budget import read_run
        return read_run(self.ledger, self.run.name)

    def live_checkpoint(self):
        events = []
        path = self.run / "runtime.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        finished = [e for e in events if e.get("kind") == "inference_finished"]
        usage = [(e.get("payload") or {}).get("provider_usage") or {} for e in finished]
        prompt = sum(u.get("prompt_tokens", 0) or 0 for u in usage)
        cached = sum(((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0) for u in usage)
        budget = self.snapshot()
        settled = Decimal((budget.get("pilot") or {}).get("settled_usd", "0"))
        data = {"utc": now(), "events": dict(Counter(e.get("kind") for e in events)),
                "cache": {"prompt_tokens": prompt, "cached_tokens": cached,
                          "fraction": cached / prompt if prompt else None},
                "run_budget": self.run_budget(), "budget": budget, "ledger": str(self.ledger),
                "new_settled_usd": None if self.settled_at_launch is None else str(settled - self.settled_at_launch),
                "agent_exits": [e for e in events if e.get("kind") == "agent_exit"],
                "run_stop": [e for e in events if e.get("kind") == "run_stop"]}
        (self.phase / "live-checkpoint.json").write_text(json.dumps(data, indent=2))

    # -- steps -----------------------------------------------------------------
    def step(self, name, arguments):
        self.status(name)
        with (self.phase / (name + ".log")).open("x") as log:
            process = subprocess.Popen([str(self.root / ".venv/bin/python"), "-u", *arguments], cwd=self.root,
                                       env=self.env, stdout=log, stderr=subprocess.STDOUT)
            while True:
                try:
                    process.wait(timeout=30)
                    break
                except subprocess.TimeoutExpired:
                    if name == "pilot":
                        try:
                            self.live_checkpoint()
                        except Exception as error:  # a checkpoint failure never stops the run
                            (self.phase / "checkpoint-error.json").write_text(
                                json.dumps({"type": type(error).__name__}))
        (self.phase / (name + ".exit")).write_text(f"{process.returncode}\n")
        return process.returncode

    def follow_ups(self):
        run = str(self.run)
        commands = {"reconcile": ["scripts/reconcile_ledger.py", "--run-dir", run, "--ledger", str(self.ledger),
                                  "--credentials-root", str(self.root)],
                    "evaluate-dry": ["scripts/evaluate_run.py", "--run-dir", run, "--dry-run"],
                    "evaluate": ["scripts/evaluate_run.py", "--run-dir", run],
                    "timeline": ["research/analysis/timeline.py", "--run-dir", run]}
        for name in FOLLOW_UPS:
            code = self.step(name, commands[name])
            if code and name not in NONBLOCKING_FOLLOW_UPS:
                return code
        return 0

    # -- automatic behavior judge ---------------------------------------------------
    def behavior_judge(self, worker_code):
        """The paid behavior judge with CONFIG's judge budget, or a recorded skip. Never raises and
        never changes the worker exit; returns the record (also behavior-judge.json)."""
        record = {"utc": now(), "config": str(self.config_path), "run_dir": str(self.run)}
        try:
            config = json.loads(self.config_path.read_text())
            sys.path.insert(0, str(ROOT / "src"))
            from research_swarm.auto_judge import enabled, judge_config_problems
            if not enabled(config):
                return None  # not configured: no judge, no record
            problems = judge_config_problems(config)
            if problems:
                record.update(outcome="skipped", reason="invalid judge settings: " + "; ".join(problems))
            elif self.args.no_postrun:
                record.update(outcome="skipped", reason="--no-postrun")
            elif worker_code != 0:
                record.update(outcome="skipped", reason=f"the run or its follow-ups did not complete (exit {worker_code})")
            else:
                code = self.step("behavior-judge", [
                    "scripts/run_transcript_judge.py", "--run-dir", str(self.run),
                    "--run-config", str(self.config_path), "--ledger", str(self.ledger),
                    "--credentials-root", str(self.root)])
                record.update(exit=code, outcome=JUDGE_OUTCOMES.get(code, "failed"))
                if code == 4:
                    refused = [line for line in (self.phase / "behavior-judge.log").read_text().splitlines()
                               if line.startswith("REFUSED:")]
                    record["reason"] = refused[-1] if refused else "refused (see behavior-judge.log)"
        except Exception as error:  # a judge failure never fails the run's packaging
            record.update(outcome="supervisor_error", error_type=type(error).__name__, detail=str(error))
        record["finished_utc"] = now()
        (self.phase / "behavior-judge.json").write_text(json.dumps(record, indent=2))
        self.status("behavior-judge-" + record["outcome"], worker_exit=worker_code, behavior_judge=record)
        return record

    def work(self):
        code = 1
        try:
            self.backup("budget-before-launch.sqlite")
            self.settled_at_launch = Decimal((self.snapshot().get("pilot") or {}).get("settled_usd", "0"))
            code = self.step("pilot", ["scripts/run_pilot.py", "--config", str(self.config_path),
                                       "--launch-paid-pilot"])
            self.backup("budget-after-pilot.sqlite")
            self.live_checkpoint()
            if code == 0 and (self.run / "settings.json").exists() and not self.args.no_postrun:
                code = self.follow_ups()
            self.judge_record = self.behavior_judge(code)
        except Exception as error:
            self.status("worker_error", error_type=type(error).__name__, detail=str(error))
            code = 2
        finally:
            try:
                self.backup("budget-final.sqlite")
            except Exception as error:
                self.status("backup_failed", error_type=type(error).__name__)
                sys.exit(5)
            (self.phase / "worker.exit").write_text(f"{code}\n")
            os.sync()
        return code

    def package(self, code):
        if not self.run.exists():
            return
        self.status("packaging", worker_exit=code)
        with tarfile.open(self.phase / "completed-run.tgz", "w:gz") as tar:
            tar.add(self.run, arcname=str(self.run.relative_to(self.root)) if self.run.is_relative_to(self.root)
                    else self.run.name)
        selected = ["budget-final.sqlite", "budget-before-launch.sqlite", "budget-after-pilot.sqlite", "pilot.exit",
                    "pilot.log", "supervisor-console.log", "worker.exit", "live-checkpoint.json",
                    self.config_path.name, "behavior-judge.json"] + [
                        a + b for a in FOLLOW_UPS + JUDGE_STEPS for b in (".log", ".exit")]
        with tarfile.open(self.phase / "completed-phase.tgz", "w:gz") as tar:
            for name in selected:
                path = self.config_path if name == self.config_path.name else self.phase / name
                if path.exists():
                    tar.add(path, arcname=name)
        hashes = {name: hashlib.sha256((self.phase / name).read_bytes()).hexdigest()
                  for name in ("completed-run.tgz", "completed-phase.tgz")}
        (self.phase / "completed-checksums.json").write_text(json.dumps(hashes, indent=2))
        self.status("ready_for_retrieval", worker_exit=code, hashes=hashes,
                    behavior_judge=None if self.judge_record is None else
                    {k: self.judge_record.get(k) for k in ("outcome", "exit", "reason")})
        deadline = time.monotonic() + self.args.retrieval_wait
        while time.monotonic() < deadline and not (self.phase / "retrieved.marker").exists():
            time.sleep(30)

    def idle_shutdown(self, code):
        """Shut down only an idle VM; never kill processes or remove containers to get there."""
        containers = sessions = processes = None
        others, other_processes = [], []
        for _ in range(6):
            containers = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], text=True, capture_output=True)
            sessions = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], text=True,
                                      capture_output=True)
            others = [s for s in sessions.stdout.splitlines() if s != self.session]
            processes = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "pid=,comm="], text=True,
                                       capture_output=True)
            other_processes = []
            for line in processes.stdout.splitlines():
                pid, name = line.strip().split(None, 1)
                if int(pid) != os.getpid() and name not in IDLE_PROCESSES:
                    other_processes.append({"pid": int(pid), "name": name})
            if (containers.returncode == 0 and not containers.stdout.strip() and sessions.returncode == 0
                    and not others and processes.returncode == 0 and not other_processes):
                self.status("preserved_shutdown_requested", worker_exit=code, run=str(self.run))
                os.sync()
                result = subprocess.run(["sudo", "-n", "shutdown", "-h", "now"])
                if result.returncode:
                    self.status("shutdown_failed", worker_exit=code)
                return result.returncode
            time.sleep(10)
        self.status("preserved_shutdown_blocked_by_other_work", worker_exit=code,
                    containers=containers.stdout.splitlines(), other_sessions=others,
                    other_processes=other_processes)
        return 3

    def main(self):
        self.phase.mkdir(parents=True, exist_ok=True)
        # The marker deliberately survives: a second supervisor for this phase never launches.
        with (self.phase / "supervisor.started").open("x") as marker:
            marker.write(now())
        code = self.work()
        self.package(code)
        if self.args.no_shutdown:
            self.status("done_vm_left_running", worker_exit=code)
            return code
        return self.idle_shutdown(code)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", required=True, type=Path, help="launch config (run_dir set; run_budget_usd, else $100)")
    parser.add_argument("--phase-dir", required=True, type=Path, help="ignored records folder, e.g. runs/phase-b/RUN_ID")
    parser.add_argument("--tmux-session", required=True, help="the tmux session running this supervisor")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository checkout (default: this one)")
    parser.add_argument("--retrieval-wait", type=int, default=600,
                        help="seconds to wait for runs/phase-b/.../retrieved.marker before the idle check (default 600)")
    parser.add_argument("--no-postrun", action="store_true", help="skip the reconcile/evaluate/timeline steps and the judge")
    parser.add_argument("--no-shutdown", action="store_true", help="leave the VM running at the end")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(Supervisor(parse_args()).main())
