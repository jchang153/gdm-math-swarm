#!/usr/bin/env python3
"""Pilot launcher. By default it runs the unpaid preflight only and spends nothing.

``--launch-paid-pilot`` starts the PAID run (20 Claude Code peers on OpenRouter; a budget track's
peers and own ledger when the config sets ``budget_track``) when every launch gate passes.
Spending has one control, the run's own budget (owner decisions, docs/plan.md "One per-run
budget, no manual approvals (October 20, 2026)", every track): ``run_budget_usd`` in the config,
else the $100 default; the launch prints the budget it uses, and every request is admitted only
while the run's committed spend plus the request's reservation fits it. There is no typed
confirmation, approval flag or interactive terminal.

Before the gates, a paid launch refreshes the route-pricing freeze automatically when it is
missing, older than ``route_pricing_max_age_hours`` or frozen for other settings
(scripts/freeze_route.py: unpaid public catalog, no credential). ``runtime.run_pilot`` then
re-runs its own fail-closed preflight, registers the run and its budget in the shared ledger,
loads the host credential (host process only; never containers) and records the loaded key's
provider-side limit (unpaid ``GET /api/v1/key``, informational) before any paid request.

    .venv/bin/python scripts/run_pilot.py                      # preflight only
    .venv/bin/python scripts/run_pilot.py --launch-paid-pilot  # paid
"""
from __future__ import annotations
import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from preflight import collect, render, render_spending, spending_checkpoint  # noqa: E402
from research_swarm.budget import DEFAULT_LEDGER_PATH, ledger_path_for  # noqa: E402


def run_ledger(config, repo_root):
    """The ledger the run registers in and reserves against: a budget track's own ledger when the
    config selects one (budget.ledger_path_for), otherwise the shared baseline ledger."""
    try:
        return ledger_path_for(config, repo_root)
    except ValueError:  # invalid budget_track: the launch gates block; show the baseline ledger
        return DEFAULT_LEDGER_PATH(repo_root)


def log_locations(run_dir, repo_root, config=None):
    run_dir = Path(run_dir).resolve()  # as runtime.run_pilot resolves it
    inspect_dir = run_dir / "inspect"
    return {
        "run_dir": str(run_dir),
        # Inspect View loads the nearest .env from its cwd upwards, so browse from
        # outside the repository (research/analysis/README.md).
        "inspect_logs": f"{inspect_dir}  (browse from outside the repository: cd /tmp && "
                        f"{Path(repo_root).resolve() / '.venv' / 'bin' / 'inspect'} view --log-dir {inspect_dir})",
        "runtime_events": str(run_dir / "runtime.jsonl"),
        "conference_events": str(run_dir / "conference" / "events.jsonl"),
        "settings_and_preflight": str(run_dir / "settings.json"),
        "effective_instructions": str(run_dir / "instructions"),
        "run_budget": str(run_dir / "run-budget.json"),
        "budget_final": str(run_dir / "budget-final.json"),
        "shared_ledger": str(run_ledger(config or {}, repo_root)),
        "post_run_evaluation": f".venv/bin/python scripts/evaluate_run.py --run-dir {run_dir}",
    }


def main(argv=None, run=None, refresh=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "pilot.json")
    parser.add_argument("--launch-paid-pilot", action="store_true", help="start the paid pilot if every gate passes")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    if args.launch_paid_pilot:
        if refresh is None:
            from freeze_route import refresh_if_needed as refresh
        refresh(config, args.config)
    report = collect(config, ROOT)
    print(render(report))
    if not args.launch_paid_pilot:
        print("Preflight only (pass --launch-paid-pilot for a paid launch). No paid call was made.")
        return 0 if report["ready"] else 1
    if not report["ready"]:
        print("Not launching: launch gates are blocked. No paid call was made.")
        return 2
    print(report["spending"]["run_budget"]["line"])
    config = dict(config)
    if not config.get("run_dir"):  # explicit, so the log locations below are exact
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        config["run_dir"] = str(ROOT / "runs" / f"pilot-{stamp}")
    locations = log_locations(config["run_dir"], ROOT, config)
    print("Starting paid pilot. Logs:\n" + json.dumps(locations, indent=2))
    if run is None:
        from research_swarm.runtime import run_pilot
        run = run_pilot
    try:
        asyncio.run(run(config, ROOT))
    finally:
        print("Run ended. Logs:\n" + json.dumps(locations, indent=2))
        print("\n".join(render_spending(spending_checkpoint(ROOT, config=config))))  # post-run spending checkpoint
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
