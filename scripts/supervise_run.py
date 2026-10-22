#!/usr/bin/env python3
"""Unattended supervisor for one paid run on the experiment VM.

Generalizes the per-run supervisors used for the October 15-16 runs (kept, unchanged, in the
ignored runs/phase-b/<run>/ folders). In a tmux session on the VM it:

1. refuses to start twice for the same phase directory (``supervisor.started`` survives, so a
   second supervisor for the same phase never launches);
2. backs up the budget ledger the run reserves against (always the config's ledger,
   ``budget.ledger_path_for``: a budget track's own ledger such as state/gemini-budget.sqlite,
   otherwise state/budget.sqlite; a --ledger naming any other file is refused), prints the
   run's budget on its own console (and the behavior
   judge's, when enabled; also ``launch-budget.json``), then launches ``scripts/run_pilot.py
   --config CONFIG --launch-paid-pilot`` with a minimal environment (the provider key is never
   passed on; the runtime loads it from the operator's private .env). The run's budget is CONFIG's
   ``run_budget_usd`` (else $100), the one spending control (docs/plan.md, "One per-run budget,
   no manual approvals (October 20, 2026)");
3. writes ``live-checkpoint.json`` every 30 s while the run is going (event counts, prompt-cache
   share, the run's budget and committed spend, ledger history, agent exits, run stop);
4. afterwards backs up the ledger and, unless --no-postrun, runs the follow-ups in order:
   reconcile_ledger.py (settles uncertain holds from OpenRouter's unpaid generation records; a
   failure is recorded and never stops the others), evaluate_run.py --dry-run, evaluate_run.py,
   timeline.py (evaluate_run.py is deferred, follow-ups stop with exit 4, while any comparator
   container, label gdm-evaluator=1, runs: its start-up cleanup removes every comparator
   container, including another checkout's; rerun it later); then, when CONFIG has
   ``auto_behavior_judge`` true, the PAID behavior judge
   (scripts/run_transcript_judge.py --run-config CONFIG: its budget is CONFIG's
   ``behavior_judge_max_usd``, else $50, for the run's judging into RUN/analysis/judge, live passes
   and early stages included; this checkout's state/budget.sqlite and .env; it refreshes its own
   route pricing record when needed and keeps a copy in RUN/analysis/judge/route-pricing/, also
   packaged as judge-route-pricing/ in completed-phase.tgz). Its outputs land in
   RUN/analysis/judge/ (packaged with the run); its outcome is recorded in behavior-judge.json and
   never changes the worker exit or stops the packaging; then, for the same configs, the PAID
   incident report (scripts/run_incident_report.py, docs/plan.md "Incident report per run (October
   21, 2026)": a model-written, verified account of the run into RUN/analysis/incident/, packaged
   with the run; its own budget, CONFIG's ``incident_report_max_usd``, else $20; this checkout's
   state/budget.sqlite and .env). Its outcome is recorded in incident-report.json; a failure, stop
   or skip never changes the worker exit or the packaging;
   With ``auto_behavior_judge`` true the judge also runs DURING the run (docs/plan.md, "Live behavior
   judge during the run (October 20, 2026)"; ``live_behavior_judge``, default true): while the pilot
   runs, one live screening pass (``run_transcript_judge.py --live``) every
   ``behavior_judge_live_interval_seconds`` (default 300) after the common start, at most one at a
   time, with a small Haiku concurrency (the pilot's provider key is shared), at nice 19,
   ``oom_score_adj`` 1000 (an agent or Inspect is never the out-of-memory victim), idle I/O class
   (honoured only by I/O schedulers such as BFQ) and a systemd memory scope only where its
   ``MemoryMax`` is verified to apply, never after the run stop; live passes spend from the judge's
   one budget (no separate share, no provider-key gate); a refused or budget-stopped pass ends the
   live passes for the run, a deferred one (exit 5: no fresh pricing record or no key just then)
   does not. When the pilot exits, the judge's model stages (``--stage
   verify``) start at once, at the same low priority with few history workers, in parallel with the
   follow-ups (no model request reads the evaluator's verdicts), and the post-run judge above
   finishes it as a resume (host facts, run level, report). If a follow-up fails, the early stages
   are stopped (SIGTERM: nothing more is admitted, requests in flight settle; their units stay
   cached for a later resume) and the post-run judge is skipped as before. ``live_behavior_judge:
   "mock"`` instead runs free mock live passes and, in the same order as the paid flow, mock early
   stages beside the follow-ups and a mock resume after them, into PHASE/live-judge-shadow/, beside
   the unchanged paid post-run judge (a shadow check before the first paid live run); ``false``
   keeps the post-run judge alone. A judge process still running when the supervisor finishes (for
   example after an exception) is stopped the same way before the final ledger backup;
5. packages the run and phase records with SHA-256 checksums and waits up to --retrieval-wait
   seconds for a ``retrieved.marker`` in the phase directory;
6. shuts the VM down only when it is idle (no Docker containers, no other tmux session, no
   unexpected process of this user and no process of another login user, e.g. a collaborator's
   tmux or SSH session, checked six times); otherwise it leaves the VM running and records why.
   --no-shutdown skips this step.

The operator uses their own provider key in their private .env (docs/plan.md, "Collaborator
budgets and keys"). Example (from the operator's checkout on the VM):

  tmux new -d -s gdm-RUN_ID "exec .venv/bin/python scripts/supervise_run.py \\
      --config runs/phase-b/RUN_ID/launch-config.json --phase-dir runs/phase-b/RUN_ID \\
      --tmux-session gdm-RUN_ID > runs/phase-b/RUN_ID/supervisor-console.log 2>&1"

(no ``| tee``: a leftover tee process blocks the idle shutdown below.)

For a budget-track config (e.g. configs/gemini-collab.json, ``budget_track: "gemini"``) the
ledger is that track's ledger (state/gemini-budget.sqlite), so backups, checkpoints,
"new_settled_usd" and the reconcile dry run (whose default --expect-model is then the track's
model) use it; the baseline ledger is never read or backed up then.

CONFIG must name the run directory explicitly (``run_dir``). Researcher-only: never copy this
into agent workspaces.
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
FOLLOW_UPS = ("reconcile", "evaluate-dry", "evaluate", "timeline")
NONBLOCKING_FOLLOW_UPS = ("reconcile",)  # bookkeeping: a failure is recorded, the others still run
JUDGE_STEPS = ("behavior-judge", "incident-report")  # after FOLLOW_UPS, only with auto_behavior_judge true
INCIDENT_OUTCOMES = {0: "complete", 2: "failed_or_refused", 3: "stopped"}
JUDGE_OUTCOMES = {0: "complete", 2: "errors_or_held_agents", 3: "stopped", 4: "refused"}
EVALUATOR_LABEL = "gdm-evaluator=1"  # evaluator.CONTAINER_LABEL (comparator containers)
# Login users have UIDs from 1000 on (65534 is "nobody"); a process of another such user means
# someone else is working on the VM.
FIRST_LOGIN_UID, NOBODY_UID = 1000, 65534
# Live behavior judge (docs/plan.md, "Live behavior judge during the run (October 20, 2026)").
LIVE_FILES = ("live-judge.log", "live-judge.json", "behavior-judge-early.log", "behavior-judge-early.exit",
              "behavior-judge-shadow.log", "behavior-judge-shadow.exit", "behavior-judge-shadow-resume.log",
              "behavior-judge-shadow-resume.exit")
LIVE_STOP_WAIT_SECONDS = 120     # an in-flight live pass may finish after the pilot exits, then SIGTERM
# After SIGTERM a judge admits nothing more and lets its in-flight requests settle (never leaving a
# ledger row reserved), then exits 3; only a judge still running after this long is killed.
JUDGE_DRAIN_SECONDS = 900
LIVE_MAX_FAILURES = 3            # failed (crashed) live passes in a row before live passes stop
LIVE_DEFERRED = 5                # run_transcript_judge.py DEFERRED: nothing sent, try again next pass
LIVE_MEMORY_MAX = "8G"           # systemd scope memory ceiling of one live pass (only where verified)
LIVE_HAIKU_CONCURRENCY = 8       # live passes share the pilot's provider key: keep their burst small
EARLY_HISTORY_WORKERS = 2        # the early stages run beside evaluate's Lean comparators
EARLY_JUDGE_TIMEOUT_SECONDS = 4 * 3600
SHADOW_DIR = "live-judge-shadow"


def _memory_bytes(text):
    """``8G`` (systemd's 1024-based suffixes) as bytes, or None."""
    text = str(text).strip().upper()
    scale = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}.get(text[-1:], 1)
    number = text[:-1] if text[-1:] in "KMGT" else text
    try:
        return int(float(number) * scale)
    except ValueError:
        return None


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
        self.session = args.tmux_session
        self.args = args
        config = json.loads(self.config_path.read_text())
        if not config.get("run_dir"):
            raise SystemExit("CONFIG must set run_dir explicitly (the supervisor packages that directory)")
        # Always the ledger run_pilot reserves against; --ledger may only restate it.
        self.ledger = self.config_ledger(config)
        if args.ledger is not None and Path(args.ledger).resolve() != self.ledger:
            raise SystemExit(f"--ledger {args.ledger} is not the ledger this config's run reserves against "
                             f"({self.ledger}, budget.ledger_path_for); omit --ledger")
        run_dir = Path(config["run_dir"])
        self.run = (run_dir if run_dir.is_absolute() else self.root / run_dir).resolve()
        self.env = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}  # never the provider key
        self.env["PYTHONPATH"] = "src"
        self.settled_at_launch = None
        # The behavior judge always bills the baseline ledger (docs/plan.md, "Automatic post-run
        # behavior judge for the owner's runs"; its own per-run budget), even when the run reserves
        # against a budget track's own ledger (self.ledger); a live pass reads the pilot's run from
        # self.ledger (--pilot-ledger) for its key-headroom deferral.
        self.judge_ledger = self.root / "state" / "budget.sqlite"
        self.judge_record = None
        self.incident_record = None
        self.live = None          # live behavior judge state (live_setup)
        self.early = None         # the judge's model stages started when the pilot exits
        self.last_events = []
        self._scope_ok = None
        self.scope_detail = None
        self.shadow_record = None

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
        """This run's budget and committed spend from the ledger (run id = the run directory's name, or
        its newest relaunch id ``<name>-r<N>``, ``budget.latest_run_id``)."""
        sys.path.insert(0, str(self.root / "src"))
        from research_swarm.budget import latest_run_id, read_run
        return read_run(self.ledger, latest_run_id(self.ledger, self.run.name) or self.run.name)

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
        commands = {"reconcile": ["scripts/reconcile_ledger.py", "--run-dir", run, "--ledger", str(self.ledger),
                                  "--credentials-root", str(self.root)],
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
            if code and name not in NONBLOCKING_FOLLOW_UPS:
                return code
        return 0

    # -- behavior judge: live passes, early stages and the post-run judge -----------
    # docs/plan.md, "Live behavior judge during the run (October 20, 2026)". Every judge invocation
    # refreshes its own route pricing record when needed (run_transcript_judge.refresh_pricing).
    def judge_command(self, *extra):
        return ["scripts/run_transcript_judge.py", "--run-dir", str(self.run), *extra]

    def paid_judge_args(self):
        return ["--run-config", str(self.config_path), "--ledger", str(self.judge_ledger),
                "--pilot-ledger", str(self.ledger), "--credentials-root", str(self.root)]

    def shadow_judge_dir(self):
        return self.phase / SHADOW_DIR / self.run.name / "analysis" / "judge"

    def live_setup(self):
        """Decide from CONFIG whether the judge runs during the run (``live_behavior_judge``)."""
        self.live = None
        try:
            config = json.loads(self.config_path.read_text())
            sys.path.insert(0, str(ROOT / "src"))
            from research_swarm.auto_judge import enabled, judge_config_problems, live_settings
            if not enabled(config) or self.args.no_postrun or judge_config_problems(config):
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
                     "ticks": [], "stopped": None, "next_at": None,
                     "failures": 0, "deferred": 0, "proc": None, "started_utc": now()}
        try:
            self.live["isolation"] = self.isolation_record()
        except Exception as error:   # never stops the run
            self.live["isolation"] = {"error": type(error).__name__}
        self.live_record()
        return self.live

    def live_record(self):
        if self.live is None:
            return
        data = {k: v for k, v in self.live.items() if k not in ("proc", "handle")}
        (self.phase / "live-judge.json").write_text(json.dumps(data, indent=2, default=str))

    def scope_ok(self):
        """Whether a ``systemd-run --user --scope`` with ``MemoryMax`` really limits memory here
        (checked once): the scope must start and the cgroup it puts the process in must report that
        ``memory.max`` (cgroup v2 with the memory controller delegated to the user); otherwise no
        scope is used and ``scope_detail`` says why."""
        if self._scope_ok is None:
            self._scope_ok, self.scope_detail = False, "systemd-run not found"
            limit = _memory_bytes(self.args.live_memory_max)
            if shutil.which("systemd-run") and limit:
                probe = ('p=$(sed -n "s/^0:://p" /proc/self/cgroup); cat "/sys/fs/cgroup${p}/memory.max" '
                         "2>/dev/null || echo missing")
                try:
                    done = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "-p",
                                           f"MemoryMax={self.args.live_memory_max}", "/bin/sh", "-c", probe],
                                          env=self.env, capture_output=True, text=True, timeout=15)
                    seen = (done.stdout or "").strip().splitlines()[-1:] or [""]
                    self._scope_ok = done.returncode == 0 and seen[0] == str(limit)
                    self.scope_detail = ("verified" if self._scope_ok else
                                         f"memory.max not applied (exit {done.returncode}, saw {seen[0]!r})")
                except (OSError, subprocess.SubprocessError) as error:
                    self.scope_detail = f"systemd-run failed ({type(error).__name__})"
        return self._scope_ok

    def low_priority_prefix(self, *, memory_scope):
        """Command prefix of a judge process running beside the pilot or the evaluator: a verified
        systemd memory scope (``memory_scope``: live passes) and the idle I/O class, which only I/O
        schedulers such as BFQ honour (``_live_preexec`` adds nice 19 and ``oom_score_adj`` 1000)."""
        prefix = []
        if memory_scope and self.scope_ok():
            prefix += ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={self.args.live_memory_max}"]
        if shutil.which("ionice"):
            prefix += ["ionice", "-c3"]
        return prefix

    def live_prefix(self):
        """Low priority and, where verified, a memory ceiling for one live pass: an experimental agent
        or Inspect must never be slowed much or picked by the out-of-memory killer because of the judge."""
        return self.low_priority_prefix(memory_scope=True)

    def isolation_record(self):
        return {"nice": 19, "oom_score_adj": 1000,
                "ionice": "idle class requested" if shutil.which("ionice") else "ionice not found",
                "ionice_note": "honoured only by I/O schedulers such as BFQ (not mq-deadline or none)",
                "live_memory_max": self.args.live_memory_max if self.scope_ok() else None,
                "memory_scope": self.scope_detail, "live_haiku_concurrency": LIVE_HAIKU_CONCURRENCY,
                "early_history_workers": EARLY_HISTORY_WORKERS}

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
        concurrency = ("--haiku-concurrency", str(LIVE_HAIKU_CONCURRENCY))   # runtime only, never binding
        if self.live["mode"] == "mock":
            return self.judge_command("--live", "--mock", "--output-dir", str(self.shadow_judge_dir()), *concurrency)
        return self.judge_command("--live", *self.paid_judge_args(), *concurrency)

    def shadow_args(self):
        """The free mock shadow's judge directory and request format (the live passes' bytes)."""
        return ["--mock", "--output-dir", str(self.shadow_judge_dir()), "--prefilter-window-count", "off"]

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
        elif code == LIVE_DEFERRED:   # nothing was sent; the next pass tries again
            tick["deferred"] = True
            live["deferred"] = live.get("deferred", 0) + 1
            live["failures"] = 0
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

    @staticmethod
    def drain(proc):
        """Stop a judge process cleanly: SIGTERM (it admits nothing more and its in-flight requests
        settle, so no ledger row is left reserved), then SIGKILL only after ``JUDGE_DRAIN_SECONDS``.
        Returns ``(exit code, killed)``."""
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            return proc.wait(timeout=JUDGE_DRAIN_SECONDS), False
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait(), True

    def live_stop(self):
        """After the pilot exits: let an in-flight pass finish briefly, then stop it cleanly (``drain``)."""
        live = self.live
        if not live or live.get("proc") is None:
            return
        proc = live["proc"]
        killed = False
        try:
            code, terminated = proc.wait(timeout=LIVE_STOP_WAIT_SECONDS), False
        except subprocess.TimeoutExpired:
            (code, killed), terminated = self.drain(proc), True
        if killed and live["ticks"]:
            live["ticks"][-1]["killed"] = True
        self.live_finished(code, terminated=terminated)

    def background(self, name, arguments, *, low_priority=False):
        """A follow-up process running beside the others (its log and exit like ``step``). A judge
        process (``low_priority``) runs at nice 19, ``oom_score_adj`` 1000 and the idle I/O class."""
        handle = (self.phase / (name + ".log")).open("x")
        prefix = self.low_priority_prefix(memory_scope=False) if low_priority else []
        proc = subprocess.Popen(prefix + [str(self.root / ".venv/bin/python"), "-u", *arguments], cwd=self.root,
                                env=self.env, stdout=handle, stderr=subprocess.STDOUT,
                                preexec_fn=_live_preexec if low_priority else None)
        return {"name": name, "proc": proc, "handle": handle, "started_utc": now(), "low_priority": low_priority}

    def early_judge_start(self, worker_code):
        """When the pilot exits: the judge's model stages at once (``--stage verify``), paid (paid live
        mode) or the free mock shadow's (mock live mode), beside the unpaid follow-ups, at low priority
        with few history workers (evaluate's Lean comparators run at the same time)."""
        live = self.live
        if not live or worker_code != 0 or self.args.no_postrun or not (self.run / "settings.json").exists():
            return None
        early_args = ("--stage", "verify", "--history-workers", str(EARLY_HISTORY_WORKERS))
        try:
            if live["mode"] == "mock":
                self.early = self.background("behavior-judge-shadow", self.judge_command(
                    *self.shadow_args(), *early_args), low_priority=True)
                return self.early
            self.early = self.background("behavior-judge-early", self.judge_command(
                *self.paid_judge_args(), *early_args), low_priority=True)
        except Exception as error:   # the post-run judge still covers everything
            self.early = {"name": "behavior-judge-early", "error": f"{type(error).__name__}: {error}"}
        return self.early

    def early_judge_stop(self, reason):
        """Stop the early stages cleanly (``drain``) because the post-run judge will not resume them
        (a follow-up failed, or the supervisor itself failed): their units stay cached, so a later
        manual resume pays nothing twice."""
        early = self.early
        if not early or early.get("proc") is None:
            return early
        if early["proc"].poll() is None:
            early["stopped"] = reason
            code, killed = self.drain(early["proc"])
            if killed:
                early["killed"] = True
        return self.early_judge_wait()

    def early_judge_wait(self):
        early = self.early
        if not early or early.get("proc") is None:
            return early
        proc = early["proc"]
        try:
            code = proc.wait(timeout=EARLY_JUDGE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            early["timed_out"] = True
            code, killed = self.drain(proc)
            if killed:
                early["killed"] = True
        early["handle"].close()
        (self.phase / (early["name"] + ".exit")).write_text(f"{code}\n")
        early.update(exit=code, outcome=JUDGE_OUTCOMES.get(code, "failed"), finished_utc=now())
        for key in ("proc", "handle"):
            early.pop(key, None)
        return early

    def reap_judges(self):
        """Never leave a judge process running unsupervised (for example after an exception in the
        follow-ups): stop a live pass and the early stages cleanly and record their exits. Never
        raises."""
        for action in (self.live_stop, lambda: self.early_judge_stop("the supervisor finished without resuming "
                                                                     "the judge (see supervisor-status.json)")):
            try:
                action()
            except Exception as error:
                try:
                    (self.phase / "judge-reap-error.json").write_text(json.dumps({"type": type(error).__name__,
                                                                                  "detail": str(error)}))
                except OSError:
                    pass

    def shadow_resume(self):
        """Mock live mode, after the follow-ups and the paid post-run judge: the shadow's resume (host
        facts, run level, report from the final records), as the paid flow resumes its early stages."""
        if not self.live or self.live.get("mode") != "mock" or self.args.no_postrun:
            return None
        record = {"utc": now()}
        try:
            code = self.step("behavior-judge-shadow-resume", self.judge_command(*self.shadow_args()))
            record.update(exit=code, outcome=JUDGE_OUTCOMES.get(code, "failed"))
        except Exception as error:   # the free shadow never fails the run
            record.update(outcome="supervisor_error", error_type=type(error).__name__, detail=str(error))
        record["finished_utc"] = now()
        self.shadow_record = record
        return record

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
                code = self.step("behavior-judge", self.judge_command(*self.paid_judge_args()))
                record.update(exit=code, outcome=JUDGE_OUTCOMES.get(code, "failed"))
                if code == 4:
                    refused = [line for line in (self.phase / "behavior-judge.log").read_text().splitlines()
                               if line.startswith("REFUSED:")]
                    record["reason"] = refused[-1] if refused else "refused (see behavior-judge.log)"
        except Exception as error:  # a judge failure never fails the run's packaging
            record.update(outcome="supervisor_error", error_type=type(error).__name__, detail=str(error))
        if worker_code == 0 and not self.args.no_postrun and (self.live or {}).get("mode") == "mock":
            record["shadow_resume"] = self.shadow_resume()   # the free shadow, after the paid judge
        if self.live is not None:
            record["live"] = {k: v for k, v in self.live.items() if k not in ("proc", "ticks")} | {
                "passes": len(self.live.get("ticks") or [])}
        if self.early is not None:
            record["early"] = {k: v for k, v in self.early.items() if k not in ("proc", "handle")}
        record["finished_utc"] = now()
        (self.phase / "behavior-judge.json").write_text(json.dumps(record, indent=2))
        self.status("behavior-judge-" + record["outcome"], worker_exit=worker_code, behavior_judge=record)
        return record

    def incident_report(self, worker_code):
        """The paid incident report (scripts/run_incident_report.py) after the behavior judge, with its own
        budget (CONFIG's ``incident_report_max_usd``, else $20), or a recorded skip. Never raises and never
        changes the worker exit; returns the record (also incident-report.json), or None when the config
        does not enable the automatic judge."""
        record = {"utc": now(), "config": str(self.config_path), "run_dir": str(self.run)}
        try:
            config = json.loads(self.config_path.read_text())
            sys.path.insert(0, str(ROOT / "src"))
            from research_swarm.auto_judge import enabled, incident_budget
            if not enabled(config):
                return None
            amount, source = incident_budget(config)
            record.update(budget_usd=str(amount), budget_source=source)
            if self.args.no_postrun:
                record.update(outcome="skipped", reason="--no-postrun")
            elif worker_code != 0:
                record.update(outcome="skipped", reason=f"the run or its follow-ups did not complete (exit {worker_code})")
            else:
                code = self.step("incident-report", ["scripts/run_incident_report.py", "--run-dir", str(self.run),
                                                     "--run-limit", str(amount), "--ledger", str(self.ledger),
                                                     "--credentials-root", str(self.root)])
                record.update(exit=code, outcome=INCIDENT_OUTCOMES.get(code, "failed"))
                state = self.run / "analysis" / "incident" / "state.json"
                if state.is_file():
                    try:
                        data = json.loads(state.read_text())
                        record["state"] = {k: data.get(k) for k in ("status", "stage", "reason", "verification")}
                        record["spent_usd"] = (data.get("budget") or {}).get("settled_usd")
                    except ValueError:
                        pass
        except Exception as error:  # an incident-report failure never fails the run's packaging
            record.update(outcome="supervisor_error", error_type=type(error).__name__, detail=str(error))
        record["finished_utc"] = now()
        try:
            (self.phase / "incident-report.json").write_text(json.dumps(record, indent=2, default=str))
            self.status("incident-report-" + record.get("outcome", "unknown"), worker_exit=worker_code,
                        incident_report={k: record.get(k) for k in ("outcome", "exit", "reason")})
        except Exception:
            pass
        return record

    def announce_budget(self):
        """Print the run's budget (and the behavior judge's, when enabled) on the supervisor's own
        console before the launch: run_pilot.py prints it too, but only into pilot.log. Recorded in
        ``launch-budget.json``. Never raises (an invalid budget is refused by the launch itself)."""
        lines, record = [], {"utc": now(), "config": str(self.config_path)}
        try:
            sys.path.insert(0, str(self.root / "src"))
            from research_swarm.auto_judge import enabled, incident_budget, judge_budget
            from research_swarm.budget import describe_budget, run_budget
            config = json.loads(self.config_path.read_text())
            try:
                amount, source = run_budget(config)
                lines.append(describe_budget(amount, source))
                record.update(run_budget_usd=str(amount), run_budget_source=source)
            except ValueError as error:
                lines.append(f"Run budget: invalid ({error}); the launch refuses it")
                record["run_budget_error"] = str(error)
            if enabled(config):
                try:
                    amount, source = judge_budget(config)
                    lines.append(describe_budget(amount, source, key="behavior_judge_max_usd")
                                 .replace("Run budget", "Behavior judge budget"))
                    record.update(judge_budget_usd=str(amount), judge_budget_source=source)
                except ValueError as error:
                    lines.append(f"Behavior judge budget: invalid ({error})")
                try:
                    amount, source = incident_budget(config)
                    lines.append(describe_budget(amount, source, key="incident_report_max_usd")
                                 .replace("Run budget", "Incident report budget"))
                    record.update(incident_budget_usd=str(amount), incident_budget_source=source)
                except ValueError as error:
                    lines.append(f"Incident report budget: invalid ({error})")
        except Exception as error:   # never stops the launch
            lines.append(f"Run budget: could not be read ({type(error).__name__}: {error})")
            record["error"] = f"{type(error).__name__}: {error}"
        record["lines"] = lines
        for line in lines:
            print(line, flush=True)
        try:
            (self.phase / "launch-budget.json").write_text(json.dumps(record, indent=2))
        except OSError:
            pass
        return lines

    def work(self):
        code = 1
        try:
            self.announce_budget()
            self.backup("budget-before-launch.sqlite")
            self.settled_at_launch = Decimal((self.snapshot().get("pilot") or {}).get("settled_usd", "0"))
            self.live_setup()
            code = self.step("pilot", ["scripts/run_pilot.py", "--config", str(self.config_path),
                                       "--launch-paid-pilot"], on_wait=self.live_tick)
            self.live_stop()
            self.backup("budget-after-pilot.sqlite")
            self.live_checkpoint()
            self.early_judge_start(code)
            if code == 0 and (self.run / "settings.json").exists() and not self.args.no_postrun:
                code = self.follow_ups()
            if code != 0:   # no resume will finish the early stages: stop them (their units stay cached)
                self.early_judge_stop(f"the run or its follow-ups did not complete (exit {code})")
            self.early_judge_wait()
            self.judge_record = self.behavior_judge(code)
            self.incident_record = self.incident_report(code)
        except Exception as error:
            self.status("worker_error", error_type=type(error).__name__, detail=str(error))
            code = 2
        finally:
            self.reap_judges()   # a live pass or the early stages never outlive the supervisor's records
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
                    "pilot.log", "supervisor-console.log", "worker.exit", "live-checkpoint.json", "launch-budget.json",
                    self.config_path.name, "behavior-judge.json", "incident-report.json", *LIVE_FILES] + [
                        a + b for a in FOLLOW_UPS + JUDGE_STEPS for b in (".log", ".exit")]
        with tarfile.open(self.phase / "completed-phase.tgz", "w:gz") as tar:
            for name in selected:
                path = self.config_path if name == self.config_path.name else self.phase / name
                if path.exists():
                    tar.add(path, arcname=name)
            if (self.phase / SHADOW_DIR).is_dir():  # the free mock shadow of the live judge
                tar.add(self.phase / SHADOW_DIR, arcname=SHADOW_DIR)
            # The judge route pricing records every judge reservation was priced with (each judge
            # invocation keeps a copy of the record it used in RUN/analysis/judge/route-pricing/).
            for record in self.judge_pricing_records():
                tar.add(record, arcname="judge-route-pricing/" + record.name)
        hashes = {name: hashlib.sha256((self.phase / name).read_bytes()).hexdigest()
                  for name in ("completed-run.tgz", "completed-phase.tgz")}
        (self.phase / "completed-checksums.json").write_text(json.dumps(hashes, indent=2))
        self.status("ready_for_retrieval", worker_exit=code, hashes=hashes,
                    behavior_judge=None if self.judge_record is None else
                    {k: self.judge_record.get(k) for k in ("outcome", "exit", "reason")},
                    incident_report=None if self.incident_record is None else
                    {k: self.incident_record.get(k) for k in ("outcome", "exit", "reason")})
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
    def judge_pricing_records(self):
        """The judge route pricing records the run's judging used (``RUN/analysis/judge/route-pricing/``)."""
        folder = self.run / "analysis" / "judge" / "route-pricing"
        return sorted(p for p in folder.glob("*.json") if p.is_file()) if folder.is_dir() else []

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
    parser.add_argument("--config", required=True, type=Path, help="launch config (run_dir set; run_budget_usd, else $100)")
    parser.add_argument("--phase-dir", required=True, type=Path, help="ignored records folder, e.g. runs/phase-b/RUN_ID")
    parser.add_argument("--tmux-session", required=True, help="the tmux session running this supervisor")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository checkout (default: this one)")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="optional restatement of the budget ledger for backups, checkpoints and spending: "
                             "it must be the config's own ledger (budget.ledger_path_for, e.g. "
                             "state/gemini-budget.sqlite for budget_track gemini); any other file is refused")
    parser.add_argument("--retrieval-wait", type=int, default=600,
                        help="seconds to wait for runs/phase-b/.../retrieved.marker before the idle check (default 600)")
    parser.add_argument("--no-postrun", action="store_true", help="skip the reconcile/evaluate/timeline steps and the judge")
    parser.add_argument("--no-shutdown", action="store_true", help="leave the VM running at the end")
    parser.add_argument("--live-memory-max", default=LIVE_MEMORY_MAX,
                        help="memory ceiling of one live behavior-judge pass in its systemd scope (default 8G)")
    parser.add_argument("--poll-seconds", type=float, default=30.0,
                        help="seconds between checkpoints (and live-judge checks) while the pilot runs (default 30)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(Supervisor(parse_args()).main())
