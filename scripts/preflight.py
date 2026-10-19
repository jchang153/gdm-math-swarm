#!/usr/bin/env python3
"""Unpaid pre-launch report: evidence-based launch gates, runtime/Docker preflight and
the plan's spending-cap checkpoint (prior costs and remaining allowance per scope).

Allowances are separate ledger scopes (docs/plan.md): the cumulative pilot cap, $5 post-run
analysis, and a separate paid $2 compatibility allowance for the one live
compatibility check ("Live compatibility check", October 13, 2026), outside the
pilot cap.

Never loads a credential and never contacts a provider (launch gates read evidence
files, the ledger read-only, and local Docker image IDs). Exits 0 only when every
gate passes. The ``provider_limit`` gate therefore checks evidence only; the paid
launch and the paid compatibility check re-verify the limit of the key they actually
load (``provider_limit.verify_key_limit``) before any paid request.

The one write it can make is an explicit, audited cap raise of an existing ledger
to the currently approved value (e.g. a ledger created while the compatibility cap
was $0). It needs the user's approval and never resets spending:

    .venv/bin/python scripts/preflight.py [--config configs/pilot.json] [--json]
    .venv/bin/python scripts/preflight.py --raise-ledger-cap compatibility \\
        --approval-note "User approved the $2 compatibility allowance in chat on <date>" --i-have-user-approval
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
from research_swarm.budget import (DEFAULT_CAPS, DEFAULT_LEDGER_PATH, caps_for, is_ablation,  # noqa: E402
                                   ledger_path_for, raise_cap, read_snapshot)

# What each allowance is for (docs/plan.md "Approved spending limits" and "Live
# compatibility check"); every scope is outside the others.
SCOPE_NOTES = {
    "pilot": "shared by all pilot agents",
    "analysis": "separate post-run analysis allowance",
    "compatibility": 'separate paid allowance for the live compatibility check, plan row "Live compatibility check"; '
                     "outside the pilot cap",
}
# runtime.preflight blockers that repeat a launch-gate blocker in other words.
DUPLICATE_RUNTIME_BLOCKERS = {"Paid launch is disabled": "paid_launch_enabled:"}


def spending_checkpoint(repo_root, ledger_path=None, caps=None):
    """Prior costs, unresolved holds and remaining allowance per scope (read-only)."""
    path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER_PATH(repo_root)
    caps = caps or DEFAULT_CAPS
    snapshot = read_snapshot(path) or {}
    scopes = {}
    for scope in [*caps, *sorted(set(snapshot) - set(caps))]:
        data = snapshot.get(scope)
        if data is None:  # ledger not created yet (or scope added on first open): nothing spent
            cap = str(caps[scope])
            data = {"cap_usd": cap, "settled_usd": "0", "held_usd": "0", "committed_usd": "0", "remaining_usd": cap,
                    "unresolved_requests": 0, "violation": False}
        scopes[scope] = {k: data[k] for k in ("cap_usd", "settled_usd", "held_usd", "committed_usd", "remaining_usd",
                                              "unresolved_requests", "violation")}
    return {"ledger": str(path), "ledger_exists": bool(snapshot), "scopes": scopes}


def spending_for(config, repo_root, ledger_path=None):
    try:
        return spending_checkpoint(repo_root, ledger_path or ledger_path_for(config, repo_root), caps_for(config))
    except ValueError as ex:  # invalid budget_track: the budget_track gate blocks; report nothing as spent
        return {"ledger": None, "ledger_exists": False, "scopes": {}, "error": str(ex)}


def collect(config, repo_root, ledger_path=None):
    gates = launch_gates.evaluate(config, repo_root, ledger_path=ledger_path)
    try:
        from research_swarm import runtime  # lazy: needs the pinned Inspect packages
        runtime_report = runtime.preflight(config, repo_root)
    except Exception as ex:  # report, never crash the checkpoint
        runtime_report = {"ready": False, "blockers": [f"runtime preflight unavailable: {type(ex).__name__}: {ex}"]}
    # runtime.preflight may already merge launch gates (I3); keep each blocker once.
    runtime_blockers = [b for b in runtime_report.get("blockers", [])
                        if not (b in DUPLICATE_RUNTIME_BLOCKERS
                                and any(g.startswith(DUPLICATE_RUNTIME_BLOCKERS[b]) for g in gates["blockers"]))]
    blockers = list(dict.fromkeys([*gates["blockers"], *runtime_blockers]))
    return {"ready": not blockers, "blockers": blockers, "launch_gates": gates, "runtime_preflight": runtime_report,
            "spending": spending_for(config, repo_root, ledger_path),
            "credentials_read": False, "paid_calls_made": 0}


def money(value):
    return f"${Decimal(str(value)):,.4f}"


def allowance_summary():
    return "Separate allowances (docs/plan.md): " + ", ".join(
        f"{scope} ${DEFAULT_CAPS[scope]} ({SCOPE_NOTES.get(scope, 'scope')})" for scope in DEFAULT_CAPS) + "."


def render_spending(spending):
    lines = ["Spending-cap checkpoint (shared ledger, read-only):",
             f"  ledger {spending['ledger']}" + ("" if spending["ledger_exists"] else " (not created yet)"),
             "  " + allowance_summary()]
    for scope, data in spending["scopes"].items():
        lines.append(f"  {scope:<13} cap {money(data['cap_usd'])}  prior known costs {money(data['settled_usd'])}  "
                     f"unresolved holds {money(data['held_usd'])} ({data['unresolved_requests']})  "
                     f"remaining {money(data['remaining_usd'])}" + ("  VIOLATION" if data["violation"] else ""))
    return lines


def apply_cap_raise(ledger_path, scope, note, approved, out=print, caps=None):
    """Explicit cap raise to the approved value; 0 when applied, 2 when refused or not approved."""
    snapshot = read_snapshot(ledger_path) or {}
    current = snapshot.get(scope)
    approved_cap = (caps or DEFAULT_CAPS).get(scope)
    if current is None or approved_cap is None:
        out(f"Not raised: ledger {ledger_path} has no {scope} scope to raise "
            "(a new ledger starts at the approved caps).")
        return 2
    committed = ", ".join(f"{name} {money(data['committed_usd'])}" for name, data in snapshot.items())
    out(f"Ledger {ledger_path}: {scope} cap {money(current['cap_usd'])} -> approved {money(approved_cap)}; "
        f"recorded spending is kept (committed: {committed}).")
    if not approved or not str(note or "").strip():
        out("Not raised: requires --approval-note TEXT recording the user's approval and --i-have-user-approval.")
        return 2
    try:
        result = raise_cap(ledger_path, scope, approved_cap, note)
    except ValueError as ex:
        out(f"Not raised: {ex}")
        return 2
    out("Raised (audit row in cap_changes): " + json.dumps(result))
    return 0


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
    parser.add_argument("--raise-ledger-cap", metavar="SCOPE", choices=sorted(DEFAULT_CAPS),
                        help="raise SCOPE's cap in the shared ledger to its approved value (audited; spending kept); "
                             "needs --approval-note and --i-have-user-approval")
    parser.add_argument("--approval-note", help="records the user's approval of the cap raise")
    parser.add_argument("--i-have-user-approval", action="store_true",
                        help="the user approved this cap raise in chat")
    args = parser.parse_args(argv)
    if args.approval_note and not args.raise_ledger_cap:
        parser.error("--approval-note is only used with --raise-ledger-cap")
    config = json.loads(args.config.read_text())
    if args.raise_ledger_cap:
        try:
            caps_for(config)
        except ValueError as ex:
            print(f"Not raised: {ex}")
            return 2
        # The ablation track raises its own ledger to its own approved caps (budget.caps_for).
        path = ledger_path_for(config, ROOT) if is_ablation(config) else DEFAULT_LEDGER_PATH(ROOT)
        code = apply_cap_raise(path, args.raise_ledger_cap, args.approval_note,
                               args.i_have_user_approval, caps=caps_for(config))
        if code:
            return code
    report = collect(config, ROOT)
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
