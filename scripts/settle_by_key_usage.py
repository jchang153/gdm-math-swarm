#!/usr/bin/env python3
"""Settle a run's no-cost holds from the OpenRouter key's own usage counter.

Owner decision (docs/plan.md, "Gemini relaunch: 6 hours, auto-restart", Holds row; chat,
October 20, 2026: "Settle by key usage"). Gemini responses that report no usage (empty
replies) stay ``uncertain`` and are held at their reservation (about $1.8 each), and the
unpaid generation lookup does not find them. By the key's usage counter the 20 such holds
of the two stopped runs cost about $0.30 in total.

Each pass:

* ``U0``: the key's ``usage`` the launch recorded (runtime.jsonl ``provider_limit_verified``);
* ``U1``: the key's ``usage`` now (unpaid GET /api/v1/key, the launch's own check);
* ``S``: the settled cost of this run's requests (ledger ``actual``);
* settles this run's ``uncertain`` holds older than ``--min-age-minutes`` at
  ``X = max(0, U1 - U0 - S) + --margin-usd``, allocated in proportion to their reservations
  and never above one (when ``X`` reaches their total nothing is released).

Conservative: ``U1 - U0 - S`` also contains the cost of younger holds, finished requests not
yet settled and any other use of the key, so the allocation does not understate what the key
was billed, apart from the counter's lag (the margin and the minimum age cover it). Settled
with basis ``manual_reconciliation`` and an audit note per hold; an audit file goes to
``<ledger dir>/reconciliations/key-usage-<UTC>.json``. The credential is loaded only to make
the GET and is never printed or recorded.

It settles without any approval flag (docs/plan.md, "One per-run budget, no manual approvals
(October 20, 2026)"); ``--dry-run`` only prints the plan.

    .venv/bin/python scripts/settle_by_key_usage.py --run-dir runs/RUN --dry-run  # plan only (reads the key)
    .venv/bin/python scripts/settle_by_key_usage.py --run-dir runs/RUN \\
        --loop-minutes 30 --until-exists runs/phase-b/RUN/pilot.exit
"""
from __future__ import annotations
import argparse
import datetime
import json
import sqlite3
import sys
import time
from contextlib import closing
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import MANUAL_BASIS, BudgetLedger, track_ledger_relative  # noqa: E402

NANO = Decimal("0.000000001")


def launch_usage_and_admissions(run_dir):
    """(key usage recorded at launch, {request_id: admitted timestamp}) from runtime.jsonl."""
    usage, admitted = None, {}
    with open(Path(run_dir) / "runtime.jsonl") as handle:
        for line in handle:
            record = json.loads(line)
            if record["kind"] == "provider_limit_verified" and usage is None:
                usage = Decimal(str(record["payload"]["usage"]))
            elif record["kind"] == "inference_admitted":
                admitted[record["payload"]["request_id"]] = record["timestamp"]
    if usage is None:
        raise SystemExit("runtime.jsonl records no provider_limit_verified usage for this run")
    return usage, admitted


def allocate(excess, holds):
    """{request_id: cost}: ``excess`` spread over ``holds`` ({id: reserved}) by reservation,
    rounded up, each at most its reservation; {} when ``excess`` reaches their total."""
    total = sum(holds.values(), Decimal(0))
    if not holds or excess >= total:
        return {}
    return {rid: min(reserved, (excess * reserved / total).quantize(NANO, rounding=ROUND_CEILING))
            for rid, reserved in holds.items()}


def ledger_rows(path, ids):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)) as db:
        rows = db.execute("SELECT id, reserved, actual, state FROM calls WHERE scope='pilot'").fetchall()
    return [(rid, Decimal(reserved) * NANO, None if actual is None else Decimal(actual) * NANO, state)
            for rid, reserved, actual, state in rows if rid in ids]


def key_usage():
    from research_swarm.provider_limit import fetch_key_data
    from research_swarm.secrets import load_openrouter_key
    data = fetch_key_data(load_openrouter_key(ROOT))
    return Decimal(str(data["usage"]))


def one_pass(args, approved, min_age_minutes, usage_reader=key_usage, now=None):
    now = now or time.time()
    u0, admitted = launch_usage_and_admissions(args.run_dir)
    rows = ledger_rows(args.ledger, set(admitted))
    settled = sum((actual for _, _, actual, _ in rows if actual is not None), Decimal(0))
    holds = {rid: reserved for rid, reserved, _, state in rows
             if state == "uncertain" and now - admitted[rid] >= min_age_minutes * 60}
    u1 = usage_reader()
    excess = max(Decimal(0), u1 - u0 - settled) + Decimal(str(args.margin_usd))
    plan = allocate(excess, holds)
    summary = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "run_dir": str(args.run_dir),
               "key_usage_at_launch": str(u0), "key_usage_now": str(u1), "billed_since_launch": str(u1 - u0),
               "settled_this_run": str(settled), "margin_usd": str(args.margin_usd),
               "allocated_usd": str(sum(plan.values(), Decimal(0))), "eligible_holds": len(holds),
               "eligible_reserved_usd": str(sum(holds.values(), Decimal(0))), "min_age_minutes": min_age_minutes,
               "credential_recorded": False, "paid_calls": 0}
    if not plan:
        summary["outcome"] = "nothing released" if holds else "no eligible holds"
        print(json.dumps(summary))
        return summary
    if not approved:
        summary["outcome"] = "dry run"
        print(json.dumps(summary))
        return summary
    ledger = BudgetLedger(args.ledger)
    note = ("key-usage allocation (scripts/settle_by_key_usage.py; owner decision 'Settle by key usage', chat, "
            f"October 20, 2026): key usage {u1} - launch {u0} - settled {settled} + margin {args.margin_usd} = "
            f"{excess} over {len(holds)} uncertain holds by reservation")
    entries = []
    for rid, cost in plan.items():
        try:
            result = ledger.reconcile(rid, str(cost), MANUAL_BASIS, note=note)
            entries.append({"request_id": rid, "reserved_usd": str(holds[rid]), "cost_usd": str(cost),
                            "state": result["state"]})
        except ValueError as ex:  # e.g. settled meanwhile by the run itself
            entries.append({"request_id": rid, "reserved_usd": str(holds[rid]), "skipped": str(ex)})
    summary.update(outcome="settled", entries=entries,
                   released_usd=str(sum((holds[e["request_id"]] - Decimal(e["cost_usd"]) for e in entries
                                         if "cost_usd" in e), Decimal(0))))
    folder = Path(args.ledger).parent / "reconciliations"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    (folder / f"key-usage-{stamp}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "entries"}))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--track", default="gemini")
    parser.add_argument("--ledger", type=Path, default=None, help="default: the track's ledger")
    parser.add_argument("--margin-usd", type=Decimal, default=Decimal("2"))
    parser.add_argument("--min-age-minutes", type=float, default=10)
    parser.add_argument("--loop-minutes", type=float, default=0, help="repeat every N minutes (0: one pass)")
    parser.add_argument("--until-exists", type=Path, default=None,
                        help="with --loop-minutes: stop once this file exists (after one final pass)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only; settle nothing")
    args = parser.parse_args(argv)
    args.ledger = args.ledger or ROOT / track_ledger_relative(args.track)
    while True:
        try:
            one_pass(args, not args.dry_run, args.min_age_minutes)
        except Exception as ex:  # keep looping through transient key-endpoint or lock failures
            print(json.dumps({"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                              "error": f"{type(ex).__name__}: {ex}"[:500]}))
        if not args.loop_minutes:
            return 0
        if args.until_exists is not None and args.until_exists.exists():
            # Final pass once the counter has caught up: every remaining hold is eligible.
            time.sleep(args.min_age_minutes * 60)
            one_pass(args, not args.dry_run, 0)
            return 0
        time.sleep(args.loop_minutes * 60)


if __name__ == "__main__":
    sys.exit(main())
