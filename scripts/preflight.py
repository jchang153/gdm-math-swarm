#!/usr/bin/env python3
"""Unpaid pre-launch report: evidence-based launch gates, runtime/Docker preflight and the
spending checkpoint (the run budget this config sets, and the shared ledger's history).

Spending (owner decisions, docs/plan.md "One per-run budget, no manual approvals (October 20,
2026)"): every paid run has its own budget, specified before it starts: ``run_budget_usd`` in the
config, else $100. A request is admitted only while the run's own committed spend plus the
request's reservation fits that budget. The shared ledger (state/budget.sqlite) records every
request of every run; there is no cumulative cap to raise and no top-up, and holds or estimate
violations of earlier runs never block a new run.

Never loads a credential, never contacts a provider and never writes (launch gates read evidence
files, the ledger read-only, and local Docker image IDs). Exits 0 only when every gate passes.

    .venv/bin/python scripts/preflight.py [--config configs/pilot.json] [--json]
"""
from __future__ import annotations
import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm import launch_gates  # noqa: E402
from research_swarm.budget import DEFAULT_LEDGER_PATH, describe_budget, list_runs, read_snapshot, run_budget  # noqa: E402

RECENT_RUNS = 5


def spending_checkpoint(repo_root, ledger_path=None, config=None):
    """The run budget ``config`` sets and the ledger's history per scope (read-only)."""
    path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER_PATH(repo_root)
    snapshot = read_snapshot(path) or {}
    try:
        amount, source = run_budget(config or {})
        budget = {"run_budget_usd": str(amount), "source": source, "line": describe_budget(amount, source)}
    except ValueError as error:
        budget = {"run_budget_usd": None, "source": None, "error": str(error)}
    scopes = {scope: {k: data[k] for k in ("settled_usd", "held_usd", "committed_usd", "unresolved_requests",
                                           "estimate_violations")}
              for scope, data in snapshot.items()}
    runs = [{k: run[k] for k in ("run_id", "scope", "budget_usd", "committed_usd", "unresolved_requests", "ended")}
            for run in list_runs(path)[-RECENT_RUNS:]]
    return {"ledger": str(path), "ledger_exists": bool(snapshot), "run_budget": budget, "scopes": scopes,
            "recent_runs": runs}


def collect(config, repo_root, ledger_path=None):
    gates = launch_gates.evaluate(config, repo_root, ledger_path=ledger_path)
    try:
        from research_swarm import runtime  # lazy: needs the pinned Inspect packages
        runtime_report = runtime.preflight(config, repo_root)
    except Exception as ex:  # report, never crash the checkpoint
        runtime_report = {"ready": False, "blockers": [f"runtime preflight unavailable: {type(ex).__name__}: {ex}"]}
    # runtime.preflight merges the launch gates (I3); keep each blocker once.
    blockers = list(dict.fromkeys([*gates["blockers"], *runtime_report.get("blockers", [])]))
    return {"ready": not blockers, "blockers": blockers, "launch_gates": gates, "runtime_preflight": runtime_report,
            "spending": spending_checkpoint(repo_root, ledger_path, config), "credentials_read": False,
            "paid_calls_made": 0}


def money(value):
    return f"${Decimal(str(value)):,.4f}"


def render_spending(spending):
    budget = spending["run_budget"]
    lines = ["Spending checkpoint (read-only):",
             "  " + (budget.get("line") or f"Run budget: invalid ({budget.get('error')})"),
             f"  shared ledger {spending['ledger']}" + ("" if spending["ledger_exists"] else " (not created yet)")
             + ": history of every run; holds of earlier runs never block a new run"]
    for scope, data in spending["scopes"].items():
        lines.append(f"  {scope:<13} known costs {money(data['settled_usd'])}  unresolved holds "
                     f"{money(data['held_usd'])} ({data['unresolved_requests']})  committed {money(data['committed_usd'])}"
                     + (f"  estimate violations {data['estimate_violations']}" if data["estimate_violations"] else ""))
    for run in spending.get("recent_runs") or []:
        lines.append(f"  run {run['run_id']} ({run['scope']}): committed {money(run['committed_usd'])} of budget "
                     f"{money(run['budget_usd'])}" + ("" if run["ended"] else " (no recorded end)"))
    return lines


def render(report):
    lines = render_spending(report["spending"])
    lines.append("Launch gates:")
    for name, result in report["launch_gates"]["evidence"].items():
        where = f" [{result['evidence']}]" if result.get("evidence") else ""
        lines.append(f"  {'PASS' if result['passed'] else 'FAIL'} {name}: {result['reason']}{where}")
        for warning in result.get("warnings") or []:
            lines.append(f"       warning: {warning}")
    docker = report["runtime_preflight"].get("docker")
    if docker:
        lines.append(f"Docker: {json.dumps(docker, default=str)}")
    lines.append("READY" if report["ready"] else f"BLOCKED ({len(report['blockers'])}):")
    lines += [f"  - {b}" for b in report["blockers"]]
    lines.append("No credential was read and no paid call was made.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "pilot.json")
    parser.add_argument("--json", action="store_true", help="print the full machine-readable report")
    args = parser.parse_args(argv)
    report = collect(json.loads(args.config.read_text()), ROOT)
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
