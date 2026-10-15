#!/usr/bin/env python3
"""Pilot launcher. By default it runs the unpaid preflight only and spends nothing.

A PAID launch (20 Claude Code peers on OpenRouter, shared cumulative pilot ledger) requires
all of the following, in addition to every launch gate passing (including the
compatibility evidence from the separate $2 live compatibility check; that check
spends its own ledger scope, never the pilot's):

1. ``paid_launch_enabled: true`` in the config, set by the user;
2. ``--launch-paid-pilot`` on the command line;
3. typing the displayed remaining pilot allowance at the confirmation prompt. ``--yes``
   skips the prompt and may be used only when the user approved this exact launch.

``runtime.run_pilot`` then re-runs its own fail-closed preflight, loads the host
credential (host process only; never containers) and, before any paid request,
re-verifies the provider-side limit of the key it actually loaded
(``provider_limit.verify_key_limit``: an unpaid GET /api/v1/key; the limit must be
set, must not reset, and its ``limit_remaining`` must not exceed the remaining pilot
allowance). The ``provider-limit`` evidence alone is not bound to that key, because an
exported ``OPENROUTER_API_KEY`` wins over the host .env. A refused key ends the
launch with exit code 2 before any paid call. Then the run starts.

    .venv/bin/python scripts/run_pilot.py                      # preflight only
    .venv/bin/python scripts/run_pilot.py --launch-paid-pilot  # paid, after approval
"""
from __future__ import annotations
import argparse
import asyncio
import datetime
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from preflight import collect, money, render, render_spending, spending_checkpoint  # noqa: E402
from research_swarm.budget import DEFAULT_CAPS, DEFAULT_LEDGER_PATH  # noqa: E402


def log_locations(run_dir, repo_root):
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
        "budget_final": str(run_dir / "budget-final.json"),
        "shared_ledger": str(DEFAULT_LEDGER_PATH(repo_root)),
        "post_run_evaluation": f".venv/bin/python scripts/evaluate_run.py --run-dir {run_dir}",
    }


def confirm(remaining, ask=input):
    """True only if the operator types the remaining pilot allowance exactly."""
    answer = ask(f"Remaining pilot allowance is {money(remaining)} (cap ${DEFAULT_CAPS['pilot']}, shared by all 20 "
                 f"agents; the ${DEFAULT_CAPS['compatibility']} compatibility and ${DEFAULT_CAPS['analysis']} analysis "
                 "allowances are separate). Type that amount in USD to start the PAID pilot, anything else aborts: ")
    try:
        return Decimal(answer.strip().lstrip("$").replace(",", "")) == Decimal(str(remaining))
    except (InvalidOperation, AttributeError):
        return False


def launch_decision(config, report, args):
    """(allowed, reason) for a paid launch; pure so it can be tested offline."""
    if not args.launch_paid_pilot:
        return False, "preflight only (pass --launch-paid-pilot for a paid launch)"
    if config.get("paid_launch_enabled") is not True:
        return False, "config paid_launch_enabled is not true"
    if not report["ready"]:
        return False, "launch gates are blocked"
    return True, "all launch conditions met"


def main(argv=None, ask=input, run=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "pilot.json")
    parser.add_argument("--launch-paid-pilot", action="store_true", help="start the paid pilot if every gate passes")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation (user-approved launch only)")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    report = collect(config, ROOT)
    print(render(report))
    allowed, reason = launch_decision(config, report, args)
    if not allowed:
        print(f"Not launching: {reason}. No paid call was made.")
        if args.launch_paid_pilot:
            return 2
        return 0 if report["ready"] else 1
    remaining = report["spending"]["scopes"]["pilot"]["remaining_usd"]
    if not args.yes:
        if not sys.stdin.isatty() and ask is input:
            print("Not launching: confirmation needs an interactive terminal (or --yes after user approval).")
            return 2
        if not confirm(remaining, ask):
            print("Not launching: confirmation did not match. No paid call was made.")
            return 2
    config = dict(config)
    if not config.get("run_dir"):  # explicit, so the log locations below are exact
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        config["run_dir"] = str(ROOT / "runs" / f"pilot-{stamp}")
    locations = log_locations(config["run_dir"], ROOT)
    print("Starting paid pilot. Logs:\n" + json.dumps(locations, indent=2))
    if run is None:
        from research_swarm.runtime import run_pilot
        run = run_pilot
    from research_swarm.provider_limit import ProviderLimitError
    try:
        asyncio.run(run(config, ROOT))
    except ProviderLimitError as ex:  # raised by the live key check, before any paid request
        print(f"Not launching: the provider-side limit of the loaded key was refused ({ex}). No paid call was made; "
              "set a non-resetting key limit no larger than the remaining pilot allowance (configs/README.md).")
        return 2
    finally:
        print("Run ended. Logs:\n" + json.dumps(locations, indent=2))
        print("\n".join(render_spending(spending_checkpoint(ROOT))))  # post-run spending checkpoint
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
