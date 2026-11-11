#!/usr/bin/env python3
"""Track A red-team analysis of one run: facts, overseer-message judge, outcomes (researcher-only).

    PYTHONPATH=scripts:src:. .venv/bin/python scripts/run_redteam_analysis.py --run-dir runs/<id> \\
        [--judge off|mock|paid] [--run-limit 2] [--window-minutes 10] [--overseer agent-102] \\
        [--designated agent-050] [--output-dir DIR] [--model ...] [--fallback ...|none] [--ledger ...]

Writes OUT/facts.json (research/analysis/redteam/extract.py), OUT/labels.jsonl (one row per overseer message;
judge.py) and OUT/outcomes.json (outcomes.py); default OUT is RUN/analysis/redteam. ``--judge off`` (default)
only extracts; outcomes then have no labels. ``--judge mock`` uses a deterministic offline model, a rehearsal
ledger and no key, and must write outside the run. ``--judge paid`` sends one request per overseer message to the
simplified judge's route (default Gemini Flash with a GPT fallback for refusals), admitted through the ledger's
analysis scope under --run-limit USD (default 2; ledger label "redteam-judge-<run id>"); the rubric is
researcher-only (private/research/analysis/redteam/rubric.py) and the run refuses without it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "scripts")]

from research.analysis.simple_judge import route as sroute  # noqa: E402

LABEL_PREFIX = "redteam-judge"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, help="default RUN/analysis/redteam")
    parser.add_argument("--overseer", help="overseer id (default from settings.json, else agent-102)")
    parser.add_argument("--designated", help="designated solver (default the red_team block, else agent-050)")
    parser.add_argument("--judge", choices=("off", "mock", "paid"), default="off")
    parser.add_argument("--run-limit", type=Decimal, default=Decimal("2"), help="paid judge budget in USD")
    parser.add_argument("--window-minutes", type=float, default=10.0)
    parser.add_argument("--model", choices=sorted(sroute.CANDIDATES), default=sroute.DEFAULT_MODEL)
    parser.add_argument("--fallback", default=sroute.DEFAULT_FALLBACK, help="route for refusals, or 'none'")
    parser.add_argument("--ledger", type=Path, help="ledger (default this checkout's state/budget.sqlite)")
    parser.add_argument("--credentials-root", type=Path, default=ROOT)
    parser.add_argument("--route-record", type=Path)
    return parser.parse_args(argv)


def _write_json(path, data):
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def judge_rows(args, facts, out_dir, log):
    from research.analysis.judge.client import FakeTransport, RunLimit, committed_for_judging
    from research.analysis.redteam import judge
    from research.analysis.redteam.mock import MockModel
    from research.analysis.simple_judge.route import SimpleClient
    from research_swarm.budget import DEFAULT_LEDGER_PATH
    import run_simple_judge as rsj
    import run_transcript_judge as rtj

    mock = args.judge == "mock"
    if mock:
        system, version = "MOCK RUBRIC", "mock"
    else:
        from research.analysis.redteam import rubric
        system, version = rubric.SYSTEM, rubric.VERSION   # PrivateMaterialMissing without private/
    fallback_key = None if args.fallback in (None, "", "none") else args.fallback
    keys = [args.model] + ([fallback_key] if fallback_key and fallback_key != args.model else [])
    settings = rsj.route_settings(argparse.Namespace(mock=mock, route_record=args.route_record), keys, log)
    label = f"{LABEL_PREFIX}-{Path(args.run_dir).name}"
    if mock:
        ledger, scope = (args.ledger or out_dir / "rehearsal-ledger.sqlite").resolve(), "rehearsal"
        if ledger.name == "budget.sqlite":
            raise SystemExit("refused: --judge mock never uses the authoritative ledger")
        sources = [ledger]
    else:
        ledger, scope = (args.ledger or DEFAULT_LEDGER_PATH(ROOT)).resolve(), "analysis"
        # Leftover no-usage holds of earlier invocations are settled first (never blocks; AGENTS.md).
        from run_incident_report import settle_leftover_holds
        settle_leftover_holds(ledger, out_dir, args.credentials_root.resolve(), log)
        sources = rtj.judging_ledgers(ledger, out_dir, search=(Path(args.run_dir),), log=log)
    prior = committed_for_judging(sources, out_dir, scope=scope, run_id=label)
    if args.run_limit - Decimal(prior["committed_usd"]) <= 0:
        raise SystemExit(f"refused: no room under the budget (${args.run_limit}; committed ${prior['committed_usd']})")
    run_limit = RunLimit(str(args.run_limit), source="override", basis="run_limit", prior_usd=prior["committed_usd"],
                         prior=prior, reservation_factor=prior["reservation_factor"])
    common = dict(scope=scope, archive_dir=out_dir / "raw", settings=settings, run_limit=run_limit, run_id=label)
    if mock:
        transport = FakeTransport(responder=MockModel())
        make = lambda key: SimpleClient(key, ledger, http=transport, **common)  # noqa: E731
    else:
        rtj.record_run_budget(out_dir, {"limit_usd": str(args.run_limit), "source": "override",
                                        "prior_usd": prior["committed_usd"], "ledger_run_id": run_limit.run_id,
                                        "label_run_id": label, "route_record": settings.pricing_record},
                              run_dir=Path(args.run_dir), ledger=ledger)
        make = lambda key: SimpleClient(key, ledger, http=None, paid_authorized=True,  # noqa: E731
                                        credentials_root=args.credentials_root.resolve(), **common)
    primary = make(args.model)
    fallback = make(fallback_key) if fallback_key else None
    for client in (primary, fallback):
        if client is not None:
            client.prepare()
    log(f"judge ({args.judge}): {len(facts['overseer_messages'])} overseer messages, budget ${args.run_limit}, "
        f"model {settings.route(args.model).model_id}")
    partial = out_dir / "labels.partial.jsonl"   # checkpoint: each row as it finishes
    partial.write_text("", encoding="utf-8")

    def checkpoint(row):
        with open(partial, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    rows = asyncio.run(judge.judge_messages(facts["overseer_messages"], primary, fallback, system=system, log=log,
                                            on_row=checkpoint))
    meta = {"mode": args.judge, "rubric_version": version, "model": settings.route(args.model).model_id,
            "fallback": settings.route(fallback_key).model_id if fallback_key else None,
            "route_record": settings.pricing_record, "budget": run_limit.record()}
    return rows, meta


def main(argv=None):
    args = parse_args(argv)
    log = lambda text: print(text, file=sys.stderr, flush=True)  # noqa: E731
    from research.analysis.redteam import extract, outcomes
    run_dir = args.run_dir.resolve()
    out_dir = (args.output_dir or run_dir / "analysis" / "redteam").resolve()
    if args.judge == "mock" and (out_dir == run_dir or run_dir in out_dir.parents):
        log("refused: --judge mock writes outside the run directory (pass --output-dir)")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    facts = extract.build_facts(run_dir, overseer=args.overseer, designated=args.designated)
    _write_json(out_dir / "facts.json", facts)
    rows, meta = [], {"mode": "off"}
    if args.judge != "off":
        rows, meta = judge_rows(args, facts, out_dir, log)
        with open(out_dir / "labels.jsonl", "w", encoding="utf-8") as handle:
            for row in sorted(rows, key=lambda r: r["t"]):
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    result = outcomes.build_outcomes(facts, rows, window_minutes=args.window_minutes)
    result["judge"] = meta
    _write_json(out_dir / "outcomes.json", result)
    p, s = result["primary"], result["secondary"]
    print(json.dumps({"out_dir": str(out_dir), "judged_completely": result["judged_completely"],
                      "endorsement": p["endorsement"]["any"], "help_messages": p["help_messages"]["any"],
                      "help_submission": p["help_submission"]["attempted"], "silence": s["silence"],
                      "warnings": s["warnings"], "review": len(result["review"])}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
