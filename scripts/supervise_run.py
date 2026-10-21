#!/usr/bin/env python3
"""Unattended supervisor for one authorized paid run on the experiment VM.

Generalizes the per-run supervisors used for the October 15-16 runs (kept, unchanged, in the
ignored runs/phase-b/<run>/ folders). In a tmux session on the VM it:

1. refuses to start twice for the same phase directory (``supervisor.started`` survives);
2. backs up the budget ledger, then launches ``scripts/run_pilot.py --config CONFIG
   --launch-paid-pilot --yes`` with a minimal environment (the provider key is never passed
   on; the runtime loads it from the operator's private .env);
3. writes ``live-checkpoint.json`` every 30 s while the run is going (event counts, prompt-cache
   share, ledger snapshot, spending since launch, agent exits, run stop);
4. afterwards sets ``paid_launch_enabled`` to false in CONFIG (never a second launch by
   accident), backs up the ledger and, unless --no-postrun, runs the unpaid follow-ups in order:
   reconcile_ledger.py (dry run), evaluate_run.py --dry-run, evaluate_run.py, timeline.py;
   then, when CONFIG has ``auto_behavior_judge`` true (the repository owner's standing approval,
   docs/plan.md, "Automatic post-run behavior judge for the owner's runs (October 20, 2026)"),
   the PAID behavior judge (scripts/run_transcript_judge.py --standing-approval-from-config
   CONFIG, per-run cap behavior_judge_max_usd <= $50, this checkout's state/budget.sqlite and
   .env), after refreshing a judge route pricing record older than 72 h with the unpaid
   scripts/freeze_judge_route.py. Its outputs land in RUN/analysis/judge/ (packaged with the
   run); its outcome is recorded in behavior-judge.json and never changes the worker exit or
   stops the packaging;
   With ``auto_behavior_judge`` true the judge also runs DURING the run (docs/plan.md, "Live behavior
   judge during the run (October 20, 2026)"; ``live_behavior_judge``, default true): while the pilot
   runs, one live screening pass (``run_transcript_judge.py --live``) every
   ``behavior_judge_live_interval_seconds`` (default 300) after the common start, at most one at a
   time, under nice/ionice, ``oom_score_adj`` 1000 and, where available, a systemd memory scope (an
   agent or Inspect is never the out-of-memory victim), never after the run stop; a refused or
   budget-stopped pass ends the live passes for the run. When the pilot exits, the judge's model
   stages (``--stage verify``) start at once, in parallel with the unpaid follow-ups (no model request
   reads the evaluator's verdicts), and the post-run judge above finishes it as a resume (host facts,
   run level, report). ``live_behavior_judge: "mock"`` instead runs free mock live passes and a mock
   post-run pass into PHASE/live-judge-shadow/ beside the unchanged paid post-run judge (a shadow
   check before the first paid live run); ``false`` keeps the post-run judge alone;
5. packages the run and phase records with SHA-256 checksums and waits up to --retrieval-wait
   seconds for a ``retrieved.marker`` in the phase directory;
6. shuts the VM down only when it is idle (no Docker containers, no other tmux session, no
   unexpected process of this user, checked six times); otherwise it leaves the VM running and
   records why. --no-shutdown skips this step.

Launching a paid run needs the operator's own recorded budget decision (docs/plan.md), the
operator's own provider key in their private .env, and the gates in docs/validation.md; this
script adds no authorization. Example (from the operator's checkout on the VM):

  tmux new -s gdm-RUN_ID
  .venv/bin/python scripts/supervise_run.py --config runs/phase-b/RUN_ID/launch-config.json \\
      --phase-dir runs/phase-b/RUN_ID --tmux-session gdm-RUN_ID 2>&1 | tee runs/phase-b/RUN_ID/supervisor-console.log

CONFIG must name the run directory explicitly (``run_dir``) and have ``paid_launch_enabled``
true. Researcher-only: never copy this into agent workspaces.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
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
# Live behavior judge (docs/plan.md, "Live behavior judge during the run (October 20, 2026)").
LIVE_FILES = ("live-judge.log", "live-judge.json", "behavior-judge-early.log", "behavior-judge-early.exit",
              "behavior-judge-shadow.log", "behavior-judge-shadow.exit")
LIVE_STOP_WAIT_SECONDS = 120     # an in-flight live pass may finish after the pilot exits, then SIGTERM
LIVE_KILL_WAIT_SECONDS = 60
LIVE_MAX_FAILURES = 3            # failed (crashed) live passes in a row before live passes stop
LIVE_MEMORY_MAX = "8G"           # default systemd scope memory ceiling of one live pass (when systemd-run works)
EARLY_JUDGE_TIMEOUT_SECONDS = 4 * 3600
SHADOW_DIR = "live-judge-shadow"


def _live_preexec():  # pragma: no cover - runs in the child before exec (Linux)
    """Lowest CPU priority, and the first process the kernel's out-of-memory killer picks."""
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError:
        pass
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
        if config.get("paid_launch_enabled") is not True:
            raise SystemExit("CONFIG has paid_launch_enabled false; enable it only for the authorized launch")
        run_dir = Path(config["run_dir"])
        self.run = (run_dir if run_dir.is_absolute() else self.root / run_dir).resolve()
        self.env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}  # never the provider key
        self.env["PYTHONPATH"] = "src"
        self.settled_at_launch = None
        self.judge_record = None
        self.live = None          # live behavior judge state (live_setup)
        self.early = None         # the judge's model stages started when the pilot exits
        self.last_events = []
        self._scope_ok = None

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
        self.last_events = events
        finished = [e for e in events if e.get("kind") == "inference_finished"]
        usage = [(e.get("payload") or {}).get("provider_usage") or {} for e in finished]
        prompt = sum(u.get("prompt_tokens", 0) or 0 for u in usage)
        cached = sum(((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0) for u in usage)
        budget = self.snapshot()
        settled = Decimal(budget["pilot"]["settled_usd"])
        data = {"utc": now(), "events": dict(Counter(e.get("kind") for e in events)),
                "cache": {"prompt_tokens": prompt, "cached_tokens": cached,
                          "fraction": cached / prompt if prompt else None},
                "budget": budget,
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
    def step(self, name, arguments, on_wait=None):
        self.status(name)
        with (self.phase / (name + ".log")).open("x") as log:
            process = subprocess.Popen([str(self.root / ".venv/bin/python"), "-u", *arguments], cwd=self.root,
                                       env=self.env, stdout=log, stderr=subprocess.STDOUT)
            while True:
                try:
                    process.wait(timeout=self.args.poll_seconds)
                    break
                except subprocess.TimeoutExpired:
                    if name == "pilot":
                        try:
                            self.live_checkpoint()
                        except Exception as error:  # a checkpoint failure never stops the run
                            (self.phase / "checkpoint-error.json").write_text(
                                json.dumps({"type": type(error).__name__}))
                    if on_wait is not None:
                        on_wait()
        (self.phase / (name + ".exit")).write_text(f"{process.returncode}\n")
        return process.returncode

    def follow_ups(self):
        run = str(self.run)
        commands = {"reconcile-dry": ["scripts/reconcile_ledger.py", "--run-dir", run],
                    "evaluate-dry": ["scripts/evaluate_run.py", "--run-dir", run, "--dry-run"],
                    "evaluate": ["scripts/evaluate_run.py", "--run-dir", run],
                    "timeline": ["research/analysis/timeline.py", "--run-dir", run]}
        for name in FOLLOW_UPS:
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

    # -- live behavior judge (docs/plan.md, "Live behavior judge during the run (October 20, 2026)") -----
    def judge_command(self, *extra):
        return ["scripts/run_transcript_judge.py", "--run-dir", str(self.run), *extra]

    def paid_judge_args(self):
        return ["--standing-approval-from-config", str(self.config_path), "--ledger", str(self.ledger),
                "--credentials-root", str(self.root)]

    def shadow_judge_dir(self):
        return self.phase / SHADOW_DIR / self.run.name / "analysis" / "judge"

    def live_setup(self):
        """Decide from CONFIG whether the judge runs during the run (``live_behavior_judge``)."""
        self.live = None
        try:
            config = json.loads(self.config_path.read_text())
            sys.path.insert(0, str(ROOT / "src"))
            from research_swarm.auto_judge import enabled, live_settings, standing_approval_problems
            if not enabled(config) or self.args.no_postrun or standing_approval_problems(config):
                return None
            settings = live_settings(config)
        except Exception as error:   # never stops the run: no live judging, recorded
            self.live = {"mode": None, "error": f"{type(error).__name__}: {error}", "ticks": []}
            self.live_record()
            self.live = None
            return None
        if settings["mode"] is None:
            return None
        self.live = {"mode": settings["mode"], "interval_seconds": settings["interval_seconds"],
                     "max_usd": settings["max_usd"], "ticks": [], "stopped": None, "next_at": None,
                     "failures": 0, "proc": None, "started_utc": now()}
        self.live_record()
        return self.live

    def live_record(self):
        if self.live is None:
            return
        data = {k: v for k, v in self.live.items() if k not in ("proc", "handle")}
        (self.phase / "live-judge.json").write_text(json.dumps(data, indent=2, default=str))

    def scope_ok(self):
        """Whether ``systemd-run --user --scope`` works here (checked once)."""
        if self._scope_ok is None:
            self._scope_ok = False
            if shutil.which("systemd-run"):
                try:
                    self._scope_ok = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "true"],
                                                    env=self.env, capture_output=True, timeout=15).returncode == 0
                except (OSError, subprocess.SubprocessError):
                    self._scope_ok = False
        return self._scope_ok

    def live_prefix(self):
        """Low priority and a memory ceiling for one live pass: an experimental agent or Inspect must
        never be slowed much or picked by the out-of-memory killer because of the judge."""
        prefix = []
        if self.scope_ok():
            prefix += ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={self.args.live_memory_max}"]
        if shutil.which("ionice"):
            prefix += ["ionice", "-c3"]
        return prefix

    def run_stopped(self):
        """Whether runtime.jsonl records the run stop now (read just before a live pass starts)."""
        path = self.run / "runtime.jsonl"
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 262144))
                return b'"run_stop"' in handle.read()
        except OSError:
            return False

    def live_pass_command(self):
        if self.live["mode"] == "mock":
            return self.judge_command("--live", "--mock", "--output-dir", str(self.shadow_judge_dir()))
        return self.judge_command("--live", *self.paid_judge_args())

    def live_tick(self):
        """Called every poll while the pilot runs: finish a done pass, start the next one when due."""
        live = self.live
        if not live or live.get("stopped"):
            return
        try:
            proc = live.get("proc")
            if proc is not None:
                code = proc.poll()
                if code is None:
                    return
                self.live_finished(code)
                if live.get("stopped"):
                    return
            kinds = {e.get("kind") for e in self.last_events}
            if "run_stop" in kinds or "common_start" not in kinds:
                return
            clock = time.time()
            if live["next_at"] is None:
                live["next_at"] = clock + live["interval_seconds"]
            if clock < live["next_at"]:
                return
            if self.run_stopped():
                return
            live["next_at"] = clock + live["interval_seconds"]
            if live["mode"] == "paid" and not self.judge_fresh():
                code = subprocess.run([str(self.root / ".venv/bin/python"), "-u", "scripts/freeze_judge_route.py"],
                                      cwd=self.root, env=self.env, capture_output=True, timeout=300).returncode
                if not self.judge_fresh():
                    live["ticks"].append({"utc": now(), "skipped": f"no fresh judge route pricing record (freeze exit "
                                                                    f"{code})"})
                    self.live_record()
                    return
            with (self.phase / "live-judge.log").open("a") as log:
                log.write(f"\n=== live pass {len(live['ticks']) + 1} at {now()} ===\n")
                log.flush()
                live["proc"] = subprocess.Popen(
                    self.live_prefix() + [str(self.root / ".venv/bin/python"), "-u", *self.live_pass_command()],
                    cwd=self.root, env=self.env, stdout=log, stderr=subprocess.STDOUT, preexec_fn=_live_preexec)
            live["ticks"].append({"started_utc": now(), "pid": live["proc"].pid})
            self.live_record()
        except Exception as error:   # a live-judge problem never stops the run
            live["stopped"] = f"supervisor error {type(error).__name__}: {error}"
            self.live_record()

    def live_finished(self, code, *, terminated=False):
        live = self.live
        tick = live["ticks"][-1] if live["ticks"] else {}
        tick.update(exit=code, finished_utc=now(), **({"terminated": True} if terminated else {}))
        live["proc"] = None
        if terminated:
            pass
        elif code == 3:
            live["stopped"] = "a live pass stopped at the run limit or the ledger (exit 3)"
        elif code == 4:
            live["stopped"] = "a live pass was refused (exit 4; see live-judge.log)"
        elif code != 0:
            live["failures"] += 1
            if live["failures"] >= LIVE_MAX_FAILURES:
                live["stopped"] = f"{LIVE_MAX_FAILURES} failed live passes in a row (last exit {code})"
        else:
            live["failures"] = 0
        self.live_record()

    def live_stop(self):
        """After the pilot exits: let an in-flight pass finish briefly, then SIGTERM (then SIGKILL)."""
        live = self.live
        if not live or live.get("proc") is None:
            return
        proc = live["proc"]
        try:
            code, terminated = proc.wait(timeout=LIVE_STOP_WAIT_SECONDS), False
        except subprocess.TimeoutExpired:
            proc.terminate()
            terminated = True
            try:
                code = proc.wait(timeout=LIVE_KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
        self.live_finished(code, terminated=terminated)

    def background(self, name, arguments):
        """A follow-up process running beside the others (its log and exit like ``step``)."""
        handle = (self.phase / (name + ".log")).open("x")
        proc = subprocess.Popen([str(self.root / ".venv/bin/python"), "-u", *arguments], cwd=self.root, env=self.env,
                                stdout=handle, stderr=subprocess.STDOUT)
        return {"name": name, "proc": proc, "handle": handle, "started_utc": now()}

    def early_judge_start(self, worker_code):
        """When the pilot exits: the judge's model stages at once (``--stage verify``; paid live mode),
        or the free mock shadow post-run pass (mock live mode), beside the unpaid follow-ups."""
        live = self.live
        if not live or worker_code != 0 or self.args.no_postrun or not (self.run / "settings.json").exists():
            return None
        try:
            if live["mode"] == "mock":
                self.early = self.background("behavior-judge-shadow", self.judge_command(
                    "--mock", "--output-dir", str(self.shadow_judge_dir())))
                return self.early
            if not self.judge_fresh():   # the post-run judge refreshes the record and does everything itself
                self.early = {"name": "behavior-judge-early", "skipped": "no fresh judge route pricing record"}
                return None
            self.early = self.background("behavior-judge-early", self.judge_command(
                *self.paid_judge_args(), "--stage", "verify"))
        except Exception as error:   # the post-run judge still covers everything
            self.early = {"name": "behavior-judge-early", "error": f"{type(error).__name__}: {error}"}
        return self.early

    def early_judge_wait(self):
        early = self.early
        if not early or early.get("proc") is None:
            return early
        proc = early["proc"]
        try:
            code = proc.wait(timeout=EARLY_JUDGE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                code = proc.wait(timeout=LIVE_KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            early["timed_out"] = True
        early["handle"].close()
        (self.phase / (early["name"] + ".exit")).write_text(f"{code}\n")
        early.update(exit=code, outcome=JUDGE_OUTCOMES.get(code, "failed"), finished_utc=now())
        for key in ("proc", "handle"):
            early.pop(key, None)
        return early

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
                        "--standing-approval-from-config", str(self.config_path), "--ledger", str(self.ledger),
                        "--credentials-root", str(self.root)])
                    record.update(exit=code, outcome=JUDGE_OUTCOMES.get(code, "failed"))
                    if code == 4:
                        refused = [line for line in (self.phase / "behavior-judge.log").read_text().splitlines()
                                   if line.startswith("REFUSED:")]
                        record["reason"] = refused[-1] if refused else "refused (see behavior-judge.log)"
        except Exception as error:  # a judge failure never fails the run's packaging
            record.update(outcome="supervisor_error", error_type=type(error).__name__, detail=str(error))
        if self.live is not None:
            record["live"] = {k: v for k, v in self.live.items() if k not in ("proc", "ticks")} | {
                "passes": len(self.live.get("ticks") or [])}
        if self.early is not None:
            record["early"] = {k: v for k, v in self.early.items() if k not in ("proc", "handle")}
        record["finished_utc"] = now()
        (self.phase / "behavior-judge.json").write_text(json.dumps(record, indent=2))
        self.status("behavior-judge-" + record["outcome"], worker_exit=worker_code, behavior_judge=record)
        return record

    def work(self):
        code = 1
        try:
            self.backup("budget-before-launch.sqlite")
            self.settled_at_launch = Decimal(self.snapshot()["pilot"]["settled_usd"])
            self.live_setup()
            code = self.step("pilot", ["scripts/run_pilot.py", "--config", str(self.config_path),
                                       "--launch-paid-pilot", "--yes"], on_wait=self.live_tick)
            self.live_stop()
            self.disable_launch()
            self.backup("budget-after-pilot.sqlite")
            self.live_checkpoint()
            self.early_judge_start(code)
            if code == 0 and (self.run / "settings.json").exists() and not self.args.no_postrun:
                code = self.follow_ups()
            self.early_judge_wait()
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
                    self.config_path.name, "behavior-judge.json", *LIVE_FILES] + [
                        a + b for a in FOLLOW_UPS + JUDGE_STEPS for b in (".log", ".exit")]
        with tarfile.open(self.phase / "completed-phase.tgz", "w:gz") as tar:
            for name in selected:
                path = self.config_path if name == self.config_path.name else self.phase / name
                if path.exists():
                    tar.add(path, arcname=name)
            pricing = (self.judge_record or {}).get("pricing_record")
            if pricing and Path(pricing).exists():  # the route record the judge ran with
                tar.add(pricing, arcname="judge-route-pricing/" + Path(pricing).name)
            if (self.phase / SHADOW_DIR).is_dir():  # the free mock shadow of the live judge
                tar.add(self.phase / SHADOW_DIR, arcname=SHADOW_DIR)
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
    parser.add_argument("--config", required=True, type=Path, help="launch config (run_dir set, paid_launch_enabled true)")
    parser.add_argument("--phase-dir", required=True, type=Path, help="ignored records folder, e.g. runs/phase-b/RUN_ID")
    parser.add_argument("--tmux-session", required=True, help="the tmux session running this supervisor")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository checkout (default: this one)")
    parser.add_argument("--retrieval-wait", type=int, default=600,
                        help="seconds to wait for runs/phase-b/.../retrieved.marker before the idle check (default 600)")
    parser.add_argument("--no-postrun", action="store_true", help="skip the unpaid reconcile/evaluate/timeline steps")
    parser.add_argument("--no-shutdown", action="store_true", help="leave the VM running at the end")
    parser.add_argument("--live-memory-max", default=LIVE_MEMORY_MAX,
                        help="memory ceiling of one live behavior-judge pass in its systemd scope (default 8G)")
    parser.add_argument("--poll-seconds", type=float, default=30.0,
                        help="seconds between checkpoints (and live-judge checks) while the pilot runs (default 30)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(Supervisor(parse_args()).main())
