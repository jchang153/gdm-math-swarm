#!/usr/bin/env python3
"""Unattended supervisor for one authorized paid run on the experiment VM.

Generalizes the per-run supervisors used for the October 15-16 runs (kept, unchanged, in the
ignored runs/phase-b/<run>/ folders). In a tmux session on the VM it:

1. refuses to start twice for the same phase directory (``supervisor.started`` survives);
2. backs up the budget ledger the run reserves against (always the config's ledger,
   ``budget.ledger_path_for``: a budget track's own ledger such as state/gemini-budget.sqlite,
   otherwise state/budget.sqlite; a --ledger naming any other file is refused), then launches
   ``scripts/run_pilot.py --config CONFIG --launch-paid-pilot --yes`` with a minimal
   environment (the provider key is never passed on; the runtime loads it from the operator's
   private .env);
3. writes ``live-checkpoint.json`` every 30 s while the run is going (event counts, prompt-cache
   share, that ledger's snapshot, pilot-scope spending since launch, agent exits, run stop);
4. afterwards sets ``paid_launch_enabled`` to false in CONFIG (never a second launch by
   accident), backs up the ledger and, unless --no-postrun, runs the unpaid follow-ups in order:
   reconcile_ledger.py --ledger LEDGER (dry run), evaluate_run.py --dry-run, evaluate_run.py,
   timeline.py. evaluate_run.py is deferred (follow-ups stop, exit 4) while any comparator
   container (label gdm-evaluator=1) runs, because its start-up cleanup removes every
   comparator container, including another checkout's; it also refuses on its own while
   another run's agents or judge compilers are live. Rerun it later by hand.
   Then, when CONFIG has ``auto_behavior_judge`` true (the repository owner's standing approval,
   docs/plan.md, "Automatic post-run behavior judge for the owner's runs (October 20, 2026)"),
   the PAID behavior judge (scripts/run_transcript_judge.py --standing-approval-from-config
   CONFIG, per-run cap behavior_judge_max_usd <= $50, the analysis scope of this checkout's
   baseline state/budget.sqlite, also for a gemini-track run, and this checkout's .env), after
   refreshing a judge route pricing record older than 72 h with the unpaid
   scripts/freeze_judge_route.py. It runs only after every follow-up completed (a deferred
   evaluation skips it). Its outputs land in RUN/analysis/judge/ (packaged with the run); its
   outcome is recorded in behavior-judge.json and never changes the worker exit or stops the
   packaging;
5. packages the run and phase records with SHA-256 checksums and waits up to --retrieval-wait
   seconds for a ``retrieved.marker`` in the phase directory;
6. shuts the VM down only when it is idle (no Docker containers, no other tmux session, no
   unexpected process of this user and no process of another login user, e.g. a collaborator's
   tmux or SSH session, checked six times); otherwise it leaves the VM running and records why.
   --no-shutdown skips this step.

Launching a paid run needs the operator's own recorded budget decision (docs/plan.md), the
operator's own provider key in their private .env, and the gates in docs/validation.md; this
script adds no authorization. Example (from the operator's checkout on the VM):

  tmux new -s gdm-RUN_ID
  .venv/bin/python scripts/supervise_run.py --config runs/phase-b/RUN_ID/launch-config.json \\
      --phase-dir runs/phase-b/RUN_ID --tmux-session gdm-RUN_ID 2>&1 | tee runs/phase-b/RUN_ID/supervisor-console.log

For a budget-track config (e.g. configs/gemini-collab.json, ``budget_track: "gemini"``) the
ledger is that track's ledger (state/gemini-budget.sqlite), so backups, checkpoints,
"new_settled_usd" and the reconcile dry run (whose default --expect-model is then the track's
model) use it; the baseline ledger is never read or backed up then.

CONFIG must name the run directory explicitly (``run_dir``) and have ``paid_launch_enabled``
true. Researcher-only: never copy this into agent workspaces.
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
FOLLOW_UPS = ("reconcile-dry", "evaluate-dry", "evaluate", "timeline")
JUDGE_STEPS = ("judge-route-freeze", "behavior-judge")  # after FOLLOW_UPS, only with auto_behavior_judge true
JUDGE_PRICING_GLOB = "judge-route-pricing-*.json"
JUDGE_PRICING_MAX_AGE_HOURS = 72  # research/analysis/judge/route.py DEFAULT_MAX_AGE_HOURS
JUDGE_OUTCOMES = {0: "complete", 2: "errors_or_held_agents", 3: "stopped", 4: "refused"}
EVALUATOR_LABEL = "gdm-evaluator=1"  # evaluator.CONTAINER_LABEL (comparator containers)
# Login users have UIDs from 1000 on (65534 is "nobody"); a process of another such user means
# someone else is working on the VM.
FIRST_LOGIN_UID, NOBODY_UID = 1000, 65534
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
        self.session = args.tmux_session
        self.args = args
        config = json.loads(self.config_path.read_text())
        if not config.get("run_dir"):
            raise SystemExit("CONFIG must set run_dir explicitly (the supervisor packages that directory)")
        if config.get("paid_launch_enabled") is not True:
            raise SystemExit("CONFIG has paid_launch_enabled false; enable it only for the authorized launch")
        # Always the ledger run_pilot reserves against; --ledger may only restate it.
        self.ledger = self.config_ledger(config)
        if args.ledger is not None and Path(args.ledger).resolve() != self.ledger:
            raise SystemExit(f"--ledger {args.ledger} is not the ledger this config's run reserves against "
                             f"({self.ledger}, budget.ledger_path_for); omit --ledger")
        if not self.ledger.is_file():
            raise SystemExit(f"ledger {self.ledger} does not exist; a budget track's ledger is created by its "
                             "compatibility check (scripts/compat_check.py) before any launch")
        run_dir = Path(config["run_dir"])
        self.run = (run_dir if run_dir.is_absolute() else self.root / run_dir).resolve()
        self.env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}  # never the provider key
        self.env["PYTHONPATH"] = "src"
        self.settled_at_launch = None
        # The behavior judge always bills the analysis scope of the baseline ledger (docs/plan.md,
        # "Automatic post-run behavior judge for the owner's runs"), even when the run reserves
        # against a budget track's own ledger (self.ledger).
        self.judge_ledger = self.root / "state" / "budget.sqlite"
        self.judge_record = None

    def config_ledger(self, config):
        """The ledger the launched run reserves against (budget.ledger_path_for in this checkout)."""
        sys.path.insert(0, str(self.root / "src"))
        from research_swarm.budget import ledger_path_for
        try:
            return Path(ledger_path_for(config, self.root)).resolve()
        except ValueError as error:  # invalid budget_track: the run's own gates refuse it too
            raise SystemExit(f"CONFIG: {error}")

    # -- records -------------------------------------------------------------
    def status(self, stage, **fields):
        (self.phase / "supervisor-status.json").write_text(json.dumps({"stage": stage, "utc": now(), **fields},
                                                                      indent=2))

    def backup(self, name):
        with sqlite3.connect("file:" + str(self.ledger) + "?mode=ro", uri=True) as src:
            with sqlite3.connect(str(self.phase / name)) as dst:
                src.backup(dst)

    def snapshot(self):
        sys.path.insert(0, str(self.root / "src"))
        from research_swarm.budget import read_snapshot
        return read_snapshot(self.ledger)

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
        settled = Decimal(budget["pilot"]["settled_usd"])
        data = {"utc": now(), "events": dict(Counter(e.get("kind") for e in events)),
                "cache": {"prompt_tokens": prompt, "cached_tokens": cached,
                          "fraction": cached / prompt if prompt else None},
                "ledger": str(self.ledger), "budget": budget,
                "new_settled_usd": None if self.settled_at_launch is None else str(settled - self.settled_at_launch),
                "agent_exits": [e for e in events if e.get("kind") == "agent_exit"],
                "run_stop": [e for e in events if e.get("kind") == "run_stop"]}
        (self.phase / "live-checkpoint.json").write_text(json.dumps(data, indent=2))

    def disable_launch(self):
        if self.config_path.exists():
            config = json.loads(self.config_path.read_text())
            config["paid_launch_enabled"] = False
            self.config_path.write_text(json.dumps(config, indent=2))

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

    def comparators_running(self):
        """Running comparator containers (any checkout's evaluator), or None if Docker cannot be listed."""
        try:
            result = subprocess.run(["docker", "ps", "--filter", "label=" + EVALUATOR_LABEL, "--format",
                                     "{{.Names}}"], text=True, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.split() if result.returncode == 0 else None

    def follow_ups(self):
        run = str(self.run)
        commands = {"reconcile-dry": ["scripts/reconcile_ledger.py", "--run-dir", run, "--ledger", str(self.ledger)],
                    "evaluate-dry": ["scripts/evaluate_run.py", "--run-dir", run, "--dry-run"],
                    "evaluate": ["scripts/evaluate_run.py", "--run-dir", run],
                    "timeline": ["research/analysis/timeline.py", "--run-dir", run]}
        for name in FOLLOW_UPS:
            if name == "evaluate":
                running = self.comparators_running()
                if running is None or running:
                    # evaluate_run.py removes every comparator container when it starts; never
                    # disturb another evaluation (e.g. a collaborator's checkout). Rerun by hand.
                    (self.phase / "evaluate.deferred").write_text(json.dumps(
                        {"utc": now(), "comparators": running, "reason": "comparator containers running"
                         if running else "could not list comparator containers"}) + "\n")
                    self.status("evaluate_deferred", comparators=running)
                    return 4
            code = self.step(name, commands[name])
            if code:
                return code
        return 0

    # -- automatic behavior judge (owner's standing approval) --------------------
    def judge_pricing(self):
        """(path, age in hours) of the newest judge route pricing record, or (None, None)."""
        found = sorted((self.root / "data" / "validation").glob(JUDGE_PRICING_GLOB))
        if not found:
            return None, None
        try:
            created = json.loads(found[-1].read_text())["created_at"]
            created = datetime.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            age = (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() / 3600
        except (OSError, ValueError, KeyError, TypeError):
            return found[-1], None
        return found[-1], age

    def judge_fresh(self):
        path, age = self.judge_pricing()
        return path is not None and age is not None and 0 <= age <= JUDGE_PRICING_MAX_AGE_HOURS

    def behavior_judge(self, worker_code):
        """The paid behavior judge under the config's standing approval, or a recorded skip. Never
        raises and never changes the worker exit; returns the record (also behavior-judge.json)."""
        record = {"utc": now(), "config": str(self.config_path), "run_dir": str(self.run)}
        try:
            config = json.loads(self.config_path.read_text())
            sys.path.insert(0, str(ROOT / "src"))
            from research_swarm.auto_judge import enabled, standing_approval_problems
            if not enabled(config):
                return None  # not configured: no judge, no record
            problems = standing_approval_problems(config)
            if problems:
                record.update(outcome="skipped", reason="invalid standing approval: " + "; ".join(problems))
            elif self.args.no_postrun:
                record.update(outcome="skipped", reason="--no-postrun")
            elif worker_code != 0:
                record.update(outcome="skipped", reason=f"the run or its follow-ups did not complete (exit {worker_code})")
            else:
                if not self.judge_fresh():
                    record["freeze_exit"] = self.step("judge-route-freeze", ["scripts/freeze_judge_route.py"])
                path, age = self.judge_pricing()
                record["pricing_record"] = None if path is None else str(path)
                record["pricing_age_hours"] = None if age is None else round(age, 3)
                if not self.judge_fresh():
                    record.update(outcome="skipped", reason="no judge route pricing record at most "
                                  f"{JUDGE_PRICING_MAX_AGE_HOURS} h old (freeze_judge_route.py exit "
                                  f"{record.get('freeze_exit')})")
                else:
                    code = self.step("behavior-judge", [
                        "scripts/run_transcript_judge.py", "--run-dir", str(self.run),
                        "--standing-approval-from-config", str(self.config_path), "--ledger", str(self.judge_ledger),
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
            self.settled_at_launch = Decimal(self.snapshot()["pilot"]["settled_usd"])
            code = self.step("pilot", ["scripts/run_pilot.py", "--config", str(self.config_path),
                                       "--launch-paid-pilot", "--yes"])
            self.disable_launch()
            self.backup("budget-after-pilot.sqlite")
            self.live_checkpoint()
            if code == 0 and (self.run / "settings.json").exists() and not self.args.no_postrun:
                code = self.follow_ups()
            self.judge_record = self.behavior_judge(code)
        except Exception as error:
            self.status("worker_error", error_type=type(error).__name__, detail=str(error))
            code = 2
        finally:
            self.disable_launch()
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
            pricing = (self.judge_record or {}).get("pricing_record")
            if pricing and Path(pricing).exists():  # the route record the judge ran with
                tar.add(pricing, arcname="judge-route-pricing/" + Path(pricing).name)
        hashes = {name: hashlib.sha256((self.phase / name).read_bytes()).hexdigest()
                  for name in ("completed-run.tgz", "completed-phase.tgz")}
        (self.phase / "completed-checksums.json").write_text(json.dumps(hashes, indent=2))
        self.status("ready_for_retrieval", worker_exit=code, hashes=hashes,
                    behavior_judge=None if self.judge_record is None else
                    {k: self.judge_record.get(k) for k in ("outcome", "exit", "reason")})
        deadline = time.monotonic() + self.args.retrieval_wait
        while time.monotonic() < deadline and not (self.phase / "retrieved.marker").exists():
            time.sleep(30)

    def other_users(self):
        """Processes of other login users (a collaborator's tmux, SSH session or run), or None if
        they cannot be listed. ``ps -u`` and ``tmux list-sessions`` only see this user's."""
        try:
            result = subprocess.run(["ps", "-e", "-o", "uid=,pid=,comm="], text=True, capture_output=True,
                                    timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        found = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                uid = int(parts[0])
                if uid != os.getuid() and uid >= FIRST_LOGIN_UID and uid != NOBODY_UID:
                    found.append({"uid": uid, "pid": int(parts[1]), "name": parts[2]})
        return found

    def idle_shutdown(self, code):
        """Shut down only an idle VM; never kill processes or remove containers to get there."""
        containers = sessions = processes = None
        others, other_processes, other_users = [], [], None
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
            other_users = self.other_users()
            if (containers.returncode == 0 and not containers.stdout.strip() and sessions.returncode == 0
                    and not others and processes.returncode == 0 and not other_processes
                    and other_users is not None and not other_users):
                self.status("preserved_shutdown_requested", worker_exit=code, run=str(self.run))
                os.sync()
                result = subprocess.run(["sudo", "-n", "shutdown", "-h", "now"])
                if result.returncode:
                    self.status("shutdown_failed", worker_exit=code)
                return result.returncode
            time.sleep(10)
        self.status("preserved_shutdown_blocked_by_other_work", worker_exit=code,
                    containers=containers.stdout.splitlines(), other_sessions=others,
                    other_processes=other_processes, other_users=other_users)
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
    parser.add_argument("--config", required=True, type=Path, help="launch config (run_dir set, paid_launch_enabled true)")
    parser.add_argument("--phase-dir", required=True, type=Path, help="ignored records folder, e.g. runs/phase-b/RUN_ID")
    parser.add_argument("--tmux-session", required=True, help="the tmux session running this supervisor")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository checkout (default: this one)")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="optional restatement of the budget ledger for backups, checkpoints and spending: "
                             "it must be the config's own ledger (budget.ledger_path_for, e.g. "
                             "state/gemini-budget.sqlite for budget_track gemini); any other file is refused")
    parser.add_argument("--retrieval-wait", type=int, default=600,
                        help="seconds to wait for runs/phase-b/.../retrieved.marker before the idle check (default 600)")
    parser.add_argument("--no-postrun", action="store_true", help="skip the unpaid reconcile/evaluate/timeline steps")
    parser.add_argument("--no-shutdown", action="store_true", help="leave the VM running at the end")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(Supervisor(parse_args()).main())
