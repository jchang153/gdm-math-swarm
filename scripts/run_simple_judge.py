#!/usr/bin/env python3
"""Simplified behavior judge: one model call per agent over a code-built case file (researcher-only).

    PYTHONPATH=scripts:src:. .venv/bin/python scripts/run_simple_judge.py --run-dir runs/<id> --run-limit 10 \\
        [--model gpt-5.6-terra-medium] [--fallback gemini-3.8-flash-medium|none] [--agents agent-001,agent-002] \\
        [--output-dir DIR] [--ledger state/budget.sqlite] [--credentials-root CHECKOUT] [--mock]

Builds every agent's case file (research/analysis/simple_judge/casefile.py; histories from the behavior judge's
history builder, private file versions skipped), sends each once to the model (a refused request goes once to the
fallback model of another provider), checks every quote verbatim and downgrades unsupported labels to "unclear"
(verify.py), and writes RUN/analysis/simple_judge/{agents.jsonl, report.json, report.html}; case files go to
cases.jsonl and raw requests and responses to raw/. When the run has a rubric-3.1 judge (RUN/analysis/judge),
report.json carries the per-agent comparison.

Spending (docs/plan.md, "One per-run budget, no manual approvals"): --run-limit USD (default 10) is this run's
simplified-judge budget, admitted through the ledger like every judge request (RunLimit plus the ledger's analysis
scope; ledger label run id "simple-judge-<run id>", so it never counts against the behavior judge's or the
incident report's budget). Earlier invocations into the same output directory count against it. No other control.
The route record (simple-judge-route-<UTC>.json, unpaid public catalog) is refreshed automatically when missing or
older than 72 h. --mock uses a deterministic offline model, a rehearsal ledger and no key.

Seeded runs (track "seeded"; research/analysis/roles.py): the seeded participant (agent-101, role "seed") is the
planted source, not a model agent. It is never judged (no case file, no call; --agents naming it is refused) and
report.json records it under "seeded" (with the injection minute); a likely source naming it counts as "seeded",
never "independent". Non-solver agents that are judged (every overseer) carry their role in agents.jsonl and on the
page.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "scripts")]

from research.analysis.simple_judge import route as sroute  # noqa: E402

ROUTE_DIR = ROOT / "data" / "validation"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-limit", type=Decimal, default=Decimal("10"), help="this run's budget in USD (default 10)")
    parser.add_argument("--model", choices=sorted(sroute.CANDIDATES), default=sroute.DEFAULT_MODEL)
    parser.add_argument("--fallback", default=sroute.DEFAULT_FALLBACK,
                        help="route for refused requests (another provider), or 'none'")
    parser.add_argument("--agents", help="comma-separated agent ids (default: every agent)")
    parser.add_argument("--output-dir", type=Path, help="default RUN/analysis/simple_judge")
    parser.add_argument("--judge-dir", type=Path, help="rubric-3.1 judge to compare with (default RUN/analysis/judge)")
    parser.add_argument("--cases", type=Path, help="reuse case files from this cases.jsonl instead of rebuilding")
    parser.add_argument("--ledger", type=Path, help="ledger to bill (default this checkout's state/budget.sqlite)")
    parser.add_argument("--credentials-root", type=Path, default=ROOT,
                        help="directory whose private .env holds the provider key (never printed)")
    parser.add_argument("--route-record", type=Path, help="simple-judge route record (default: newest, refreshed)")
    parser.add_argument("--history-workers", type=int, default=3)
    parser.add_argument("--mock", action="store_true", help="offline deterministic model; no network, no key")
    return parser.parse_args(argv)


def route_settings(args, keys, log):
    from research.analysis.simple_judge.route import SimpleRouteSettings, freeze, offline_route
    if args.mock:
        return SimpleRouteSettings.offline([offline_route(key) for key in keys])
    if args.route_record:
        return SimpleRouteSettings.from_record(args.route_record, keys)
    for path in sorted(ROUTE_DIR.glob(f"{sroute.RECORD_PREFIX}-*.json"), reverse=True)[:1]:
        try:
            return SimpleRouteSettings.from_record(path, keys)
        except (ValueError, KeyError) as error:
            log(f"route record {path.name} not usable ({error}); refreshing (unpaid public catalog)")
    path, record = freeze(ROUTE_DIR, sorted(sroute.CANDIDATES))
    log(f"route record written: {path} (valid: {record['valid']})")
    return SimpleRouteSettings.from_record(path, keys)


def seeded_record(run_dir):
    """(seed agents, report.json "seeded" block or None, {agent: non-solver role})."""
    from research.analysis import roles as participant_roles
    from research.analysis.incident.contagion import run_clock
    roles = participant_roles.special_roles(run_dir)
    seeds = {a for a, r in roles.items() if r == participant_roles.SEED}
    info = participant_roles.seeded_info(run_dir)
    if not seeds or info is None:
        return seeds, None, roles
    injected = participant_roles.injection(run_dir)
    record = dict(info, label=participant_roles.SEED_LABEL, judged=False, injected=injected is not None,
                  injected_utc=(injected or {}).get("time"), injected_min=None,
                  note="planted by the researchers; not a model agent, no transcript, never judged")
    if injected and isinstance(injected.get("timestamp"), (int, float)):
        record["injected_min"] = round((injected["timestamp"] - run_clock(run_dir)[0]) / 60, 2)
    return seeds, record, roles


def load_cases(path, agents):
    cases = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if agents is None or case["agent_id"] in agents:
            cases[case["agent_id"]] = case
    return cases


def main(argv=None):
    args = parse_args(argv)
    log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
    run_dir = args.run_dir.resolve()
    out_dir = (args.output_dir or run_dir / "analysis" / "simple_judge").resolve()
    if args.mock and (out_dir == run_dir or run_dir in out_dir.parents):
        log("refused: --mock writes outside the run directory (pass --output-dir)")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / ".simple-judge.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(f"refused: another invocation holds {out_dir / '.simple-judge.lock'}")
            return 2
        return _run(args, run_dir, out_dir, log)


def _run(args, run_dir, out_dir, log):
    from research.analysis.judge.client import FakeTransport, RunLimit, committed_for_judging
    from research.analysis.judge.history import sample_index
    from research.analysis.simple_judge import casefile, compare, pipeline
    from research.analysis.simple_judge.mock import MockModel
    from research.analysis.simple_judge.route import SimpleClient
    from research_swarm.budget import DEFAULT_LEDGER_PATH
    import run_transcript_judge as rtj

    started = time.monotonic()
    fallback_key = None if args.fallback in (None, "", "none") else args.fallback
    if fallback_key and fallback_key not in sroute.CANDIDATES:
        log(f"refused: unknown fallback route {fallback_key!r}")
        return 2
    keys = [args.model] + ([fallback_key] if fallback_key and fallback_key != args.model else [])
    settings = route_settings(args, keys, log)
    label = f"simple-judge-{run_dir.name}"
    if args.mock:
        ledger, scope = (args.ledger or out_dir / "rehearsal-ledger.sqlite").resolve(), "rehearsal"
        if ledger.name == "budget.sqlite":
            log("refused: --mock never uses the authoritative ledger")
            return 2
        sources = [ledger]
    else:
        ledger, scope = (args.ledger or DEFAULT_LEDGER_PATH(ROOT)).resolve(), "analysis"
        sources = rtj.judging_ledgers(ledger, out_dir, search=(run_dir,), log=log)
    prior = committed_for_judging(sources, out_dir, scope=scope, run_id=label)
    budget = args.run_limit
    if budget - Decimal(prior["committed_usd"]) <= 0:
        log(f"refused: no room under this run's simplified-judge budget (${budget}; already committed "
            f"${prior['committed_usd']}); nothing was sent")
        return 2
    run_limit = RunLimit(str(budget), source="override", basis="run_limit", prior_usd=prior["committed_usd"],
                         prior=prior, reservation_factor=prior["reservation_factor"])
    log(f"simple judge: budget ${budget} (already committed ${prior['committed_usd']}); ledger {ledger}; "
        f"model {settings.route(args.model).model_id}" + (f", fallback {settings.route(fallback_key).model_id}"
                                                         if fallback_key else ""))
    seeds, seeded, roles = seeded_record(run_dir)
    agents = sorted(a for a in sample_index(run_dir) if a not in seeds)
    if seeded:
        log(f"seeded run: {', '.join(sorted(seeds))} is the seeded source (planted by researchers); not judged")
    if args.agents:
        wanted = [a.strip() for a in args.agents.split(",") if a.strip()]
        planted = [a for a in wanted if a in seeds]
        if planted:
            log(f"refused: {planted} is the seeded source (planted by researchers), which is never judged")
            return 2
        missing = [a for a in wanted if a not in agents]
        if missing:
            log(f"refused: no Inspect sample for {missing}")
            return 2
        agents = wanted
    build_started = time.monotonic()
    if args.cases:
        cases = load_cases(args.cases, set(agents))
        missing = [a for a in agents if a not in cases]
        if missing:
            cases.update(casefile.build_cases(run_dir, missing, workers=args.history_workers, log=log))
    else:
        cases = casefile.build_cases(run_dir, agents, workers=args.history_workers, log=log)
    build_seconds = time.monotonic() - build_started
    with open(out_dir / "cases.jsonl", "w", encoding="utf-8") as handle:
        for agent in agents:
            handle.write(json.dumps(cases[agent], ensure_ascii=False) + "\n")
    common = dict(scope=scope, archive_dir=out_dir / "raw", settings=settings, run_limit=run_limit, run_id=label)
    if args.mock:
        transport = FakeTransport(responder=MockModel())
        make = lambda key: SimpleClient(key, ledger, http=transport, **common)  # noqa: E731
    else:
        rtj.record_run_budget(out_dir, {"limit_usd": str(budget), "source": "override",
                                        "prior_usd": prior["committed_usd"], "ledger_run_id": run_limit.run_id,
                                        "label_run_id": label, "route_record": settings.pricing_record},
                              run_dir=run_dir, ledger=ledger)
        make = lambda key: SimpleClient(key, ledger, http=None, paid_authorized=True,  # noqa: E731
                                        credentials_root=args.credentials_root.resolve(), **common)
    primary = make(args.model)
    fallback = make(fallback_key) if fallback_key else None
    for client in (primary, fallback):
        if client is not None:
            client.prepare()
    judge_started = time.monotonic()
    rows = asyncio.run(pipeline.judge_agents([cases[a] for a in agents], primary, fallback, log=log))
    judge_seconds = time.monotonic() - judge_started
    old = compare.load_old(args.judge_dir or run_dir / "analysis" / "judge")
    comparison = compare.compare(sorted(rows, key=lambda r: r["agent_id"]), old)
    meta = {"mode": "mock" if args.mock else "paid", "model": settings.route(args.model).model_id,
            "model_key": args.model, "effort": settings.route(args.model).effort,
            "fallback_model": settings.route(fallback_key).model_id if fallback_key else None,
            "route_record": settings.pricing_record, "route_record_sha256": settings.pricing_record_sha256,
            "agents_requested": len(agents), "wall_seconds": round(time.monotonic() - started, 1),
            "case_build_seconds": round(build_seconds, 1), "judging_seconds": round(judge_seconds, 1),
            "budget": run_limit.record()}
    if seeded:
        meta["seeded"] = seeded
    if any(role != "seed" for role in roles.values()):
        meta["roles"] = {a: r for a, r in roles.items() if r != "seed"}
    report = pipeline.write_outputs(out_dir, run_dir.name, rows, meta=meta, comparison=comparison)
    summary = {"status": "done", "out_dir": str(out_dir), "cost_usd": report["cost_usd"],
               "wall_seconds": meta["wall_seconds"], "counts": {k: report["counts"][k] for k in
                                                               ("labelled", "not_labelled", "cheating_level",
                                                                "whistleblower", "evaluation_aware", "fallback_used")}}
    if comparison:
        summary["agreement"] = {k: comparison[k] for k in ("cheating_any", "cheating_level", "whistleblower",
                                                           "evaluation_aware")}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
