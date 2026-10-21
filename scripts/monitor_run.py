#!/usr/bin/env python3
"""Read-only live progress view of a run, for use on the VM that runs it.

    python scripts/monitor_run.py --run-dir runs/RUN_ID [--phase-dir runs/phase-b/RUN_ID] [--ledger LEDGER]

Prints elapsed time against the run's ``max_run_seconds``, this run's budget, its settled and
committed spending and their rate, the run's remaining budget and the time it lasts at that rate,
requests, prompt-cache fraction, agent restarts and exits, and conference submissions and solved
tasks.

Nothing is written. The ledger and ``conference/state.sqlite`` are opened read-only. The ledger is
``--ledger`` or the path recorded in the supervisor's ``live-checkpoint.json``. A run with its own
budget (docs/plan.md "One per-run budget, no manual approvals (October 20, 2026)") is read from the
ledger's ``runs`` table (run id = the run directory's name), else from the checkpoint's
``run_budget`` or the run's ``run-budget.json``. For older runs (cumulative caps), this run's
spending is the ledger now minus the ledger at launch, read from the phase directory's
``budget-before-launch.sqlite`` backup; when that backup is absent the baseline is reported as
unknown and only the checkpoint's ``new_settled_usd`` is shown.
"""
import argparse
import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from research_swarm.budget import read_run, read_snapshot  # noqa: E402


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _records(path):
    records = []
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue  # a line still being written
    except OSError:
        pass
    return records


def _money(value):
    return None if value is None else Decimal(str(value))


def _conference(path):
    if not Path(path).exists():
        return None
    db = sqlite3.connect(f"file:{urllib.parse.quote(str(Path(path).resolve()))}?mode=ro", uri=True, timeout=30)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def count(table):
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in tables else None

        out = {"tasks": count("tasks"), "posts": count("posts"), "messages": count("messages"),
               "library": count("library"), "submissions": {}, "solved": []}
        if "submissions" in tables:
            out["submissions"] = dict(db.execute("SELECT outcome, COUNT(*) FROM submissions GROUP BY outcome"))
        if "tasks" in tables:
            out["solved"] = [list(row) for row in db.execute(
                "SELECT task_id, credited_agent FROM tasks WHERE status='closed' ORDER BY accepted_at")]
        return out
    finally:
        db.close()


def summarize(run_dir, phase_dir=None, ledger=None, scope="pilot", now=None):
    """Return the progress summary as a dict (amounts as Decimal strings)."""
    run_dir = Path(run_dir)
    phase_dir = Path(phase_dir) if phase_dir else run_dir.parent / "phase-b" / run_dir.name
    settings = _json(run_dir / "settings.json") or {}
    launch = _json(phase_dir / "launch-config.json") or {}
    checkpoint = _json(phase_dir / "live-checkpoint.json") or {}
    records = _records(run_dir / "runtime.jsonl")
    kinds = {}
    for record in records:
        kinds[record.get("kind")] = kinds.get(record.get("kind"), 0) + 1
    start = next((r["timestamp"] for r in records if r.get("kind") == "common_start"), None)
    stop = next((r["timestamp"] for r in records if r.get("kind") == "run_stop"), None)
    end = stop if stop is not None else (time.time() if now is None else now)
    elapsed = None if start is None else max(end - start, 0.0)
    max_seconds = (settings.get("config") or {}).get("max_run_seconds", launch.get("max_run_seconds"))

    ledger_path = ledger or checkpoint.get("ledger")
    exists = bool(ledger_path) and Path(ledger_path).exists()
    live_run = read_run(ledger_path, run_dir.name) if exists else None
    final_run = _json(run_dir / "run-budget.json")
    run = live_run or checkpoint.get("run_budget") or final_run
    rate_settled = None
    if run:  # a run with its own budget
        source = "ledger" if live_run else ("checkpoint" if checkpoint.get("run_budget") else "run-budget.json")
        spend = {"scope": run.get("scope", scope), "source": source, "ledger": str(ledger_path) if live_run else None,
                 "run_id": run.get("run_id"), "budget_usd": run.get("budget_usd"),
                 "run_committed_usd": run.get("committed_usd"), "run_settled_usd": run.get("settled_usd"),
                 "remaining_usd": run.get("remaining_usd"), "unresolved_requests": run.get("unresolved_requests"),
                 "estimate_violations": run.get("estimate_violations")}
        rate_settled = _money(run.get("settled_usd"))
    else:  # older runs: cumulative ledger caps
        live = read_snapshot(ledger_path) if exists else None
        current = (live or checkpoint.get("budget") or {}).get(scope)
        source = "ledger" if live else ("checkpoint" if current else None)
        base_snapshot = read_snapshot(phase_dir / "budget-before-launch.sqlite")
        base = (base_snapshot or {}).get(scope)
        spend = {"scope": scope, "source": source, "ledger": str(ledger_path) if live else None,
                 "baseline": "budget-before-launch.sqlite" if base else None}
        if current:
            cap, committed = _money(current.get("cap_usd", current.get("legacy_cap_usd"))), _money(current["committed_usd"])
            spend.update(committed_total_usd=str(committed))
            if cap is not None:
                spend.update(cap_usd=str(cap), remaining_usd=str(cap - committed))
            if base:
                spend["baseline_committed_usd"] = base["committed_usd"]
                spend["run_committed_usd"] = str(committed - _money(base["committed_usd"]))
                if cap is not None:
                    spend["run_allowance_usd"] = str(cap - _money(base["committed_usd"]))
                rate_settled = _money(current["settled_usd"]) - _money(base["settled_usd"])
    if rate_settled is None and checkpoint.get("new_settled_usd") is not None:
        rate_settled = _money(checkpoint["new_settled_usd"])
    if rate_settled is not None:
        spend["run_settled_usd"] = str(rate_settled)
    if rate_settled is not None and elapsed:
        per_hour = rate_settled / Decimal(str(elapsed)) * 3600
        spend["settled_usd_per_hour"] = f"{per_hour:.2f}"
        if spend.get("remaining_usd") is not None and per_hour > 0:
            spend["eta_hours"] = round(float(_money(spend["remaining_usd"]) / per_hour), 2)

    restarts = {}
    for record in records:
        if record.get("kind") == "agent_restart":
            payload = record.get("payload") or {}
            restarts.setdefault(record.get("agent_id"), []).append(
                f"{payload.get('ended')}({payload.get('requests_in_session')})")
    cache = (checkpoint.get("cache") or {}).get("fraction")
    return {
        "run": run_dir.name, "checkpoint_utc": checkpoint.get("utc"),
        "started": start is not None, "stopped": stop is not None,
        "elapsed_seconds": None if elapsed is None else round(elapsed), "max_run_seconds": max_seconds,
        "spend": spend,
        "requests": {"admitted": kinds.get("inference_admitted", 0), "finished": kinds.get("inference_finished", 0),
                     "uncertain": kinds.get("inference_uncertain", 0)},
        "cache_fraction": cache,
        "restarts": kinds.get("agent_restart", 0), "exits": kinds.get("agent_exit", 0),
        "restarts_by_agent": {agent: ends[-8:] for agent, ends in sorted(restarts.items())},
        "conference": _conference(run_dir / "conference" / "state.sqlite"),
    }


def render(summary):
    def minutes(seconds):
        return "?" if seconds is None else f"{seconds / 60:.0f}m"

    spend = summary["spend"]
    state = "stopped" if summary["stopped"] else ("running" if summary["started"] else "not started")
    lines = [f"{summary['run']} [{state}] {datetime.datetime.now(datetime.timezone.utc):%H:%M} UTC | elapsed "
             f"{minutes(summary['elapsed_seconds'])} of {minutes(summary['max_run_seconds'])}"]
    if spend.get("run_id"):
        parts = [f"spend[{spend['scope']}] run {spend['run_id']} from {spend['source']}: budget "
                 f"${Decimal(spend['budget_usd']):.2f}"]
        if "settled_usd_per_hour" in spend:
            parts.append(f"run settled ${Decimal(spend['run_settled_usd']):.2f} (${spend['settled_usd_per_hour']}/h)")
        elif spend.get("run_settled_usd") is not None:
            parts.append(f"run settled ${Decimal(spend['run_settled_usd']):.2f}")
        parts.append(f"run committed ${Decimal(spend['run_committed_usd']):.2f}")
        parts.append(f"${Decimal(spend['remaining_usd']):.2f} left")
        if "eta_hours" in spend:
            parts.append(f"~{spend['eta_hours']:.1f}h at this rate")
        lines.append(" | ".join(parts))
    elif spend.get("source"):
        baseline = (f"baseline ${spend['baseline_committed_usd']} at launch" if spend.get("baseline")
                    else "baseline unknown (no budget-before-launch.sqlite)")
        parts = [f"spend[{spend['scope']}] from {spend['source']}: {baseline}"]
        if "run_settled_usd" in spend:
            parts.append(f"run settled ${Decimal(spend['run_settled_usd']):.2f}"
                         + (f" (${spend['settled_usd_per_hour']}/h)" if "settled_usd_per_hour" in spend else ""))
        if "run_committed_usd" in spend:
            parts.append(f"run committed ${Decimal(spend['run_committed_usd']):.2f}")
        allowance = f" of ${Decimal(spend['run_allowance_usd']):.2f}" if "run_allowance_usd" in spend else ""
        if spend.get("remaining_usd") is not None:
            parts.append(f"${Decimal(spend['remaining_usd']):.2f}{allowance} left (cap ${spend['cap_usd']})")
        if "eta_hours" in spend:
            parts.append(f"~{spend['eta_hours']:.1f}h at this rate")
        lines.append(" | ".join(parts))
    else:
        lines.append(f"spend[{spend['scope']}]: no ledger or checkpoint budget available")
    cache = summary["cache_fraction"]
    req = summary["requests"]
    lines.append(f"requests admitted {req['admitted']} finished {req['finished']} uncertain {req['uncertain']} | "
                 f"cache {'n/a' if cache is None else f'{cache:.0%}'} | restarts {summary['restarts']} | "
                 f"exits {summary['exits']}")
    conf = summary["conference"]
    if conf is None:
        lines.append("conference: no state.sqlite")
    else:
        lines.append(f"solved {len(conf['solved'])}/{conf['tasks']} | submissions {conf['submissions']} | "
                     f"posts {conf['posts']} | DMs {conf['messages']} | library {conf['library']}")
        lines.extend(f"  {task}  ({agent})" for task, agent in conf["solved"])
    if summary["restarts_by_agent"]:
        lines.append("restarts (ended(requests in session)), last 8 per agent:")
        lines.extend(f"  {agent}: {' '.join(ends)}" for agent, ends in summary["restarts_by_agent"].items())
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", required=True, help="runs/RUN_ID")
    parser.add_argument("--phase-dir", help="supervisor phase directory (default runs/phase-b/RUN_ID)")
    parser.add_argument("--ledger", help="live ledger to read (default: the checkpoint's recorded ledger)")
    parser.add_argument("--scope", default="pilot")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    if not Path(args.run_dir).is_dir():
        parser.error(f"no run directory {args.run_dir}")
    summary = summarize(args.run_dir, args.phase_dir, args.ledger, args.scope)
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
