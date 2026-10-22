#!/usr/bin/env python3
"""Researcher-only incident report for one run: a readable, model-written account (owner request,
October 21, 2026), written to RUN/analysis/incident/ (report.html, report.md, evidence.json).

  # Paid (the default): Claude Opus 5.5 plans, picks the excerpts of long records and writes.
  PYTHONPATH=src:. .venv/bin/python scripts/run_incident_report.py --run-dir runs/<run> [--run-limit 20]
  # Offline rehearsal with the deterministic mock model; output OUTSIDE the run directory.
  PYTHONPATH=src:. .venv/bin/python scripts/run_incident_report.py --run-dir runs/<run> --mock \
      --output-dir <scratch>/<run>/analysis/incident
  # Host only (no model, no budget): recompute "How the exploit spread" and the table of contents and
  # insert them into the existing report.html, report.md and evidence.json.
  PYTHONPATH=src:. .venv/bin/python scripts/run_incident_report.py --run-dir runs/<run> --rerender-contagion

One spending control (docs/plan.md, "One per-run budget, no manual approvals (October 20, 2026)"):
this run's incident reporting has its own budget, --run-limit USD (default $20). Every invocation
into the same output directory counts what the earlier ones committed (its run-budgets.json ledger
runs, its request archives and every request labelled with this run's incident label), so a rerun
never gets a fresh budget; a request that would push the committed spend past the budget is not
sent and the invocation stops cleanly (exit 3; a rerun reuses every finished unit and pays nothing
twice). No estimate, no confirmation, no approval flag. Requests go through the behavior judge's
JudgeClient (OpenRouter, provider pinned to Anthropic, ledger "analysis" scope, raw archives under
OUTPUT/raw/); the judge route pricing record is refreshed automatically when stale (unpaid public
catalog). Transcripts are sent only to the configured models.

Refusals (owner request, October 21, 2026): Anthropic may refuse a unit outright (HTTP 200,
finish_reason content_filter, no usage), e.g. its block on transcripts of another lab's model. Such
a response is settled at its true cost from OpenRouter's generation record (judge client), and the
unit is never resent to Anthropic: it goes once to the non-Anthropic fallback model
(route.FALLBACK_MODEL_ID, frozen in the same pricing record, which is refreshed automatically when it
lacks one) and is flagged in the report and evidence.json (``model_fallbacks``). Before admitting
anything, an automatic startup pass settles this output directory's leftover uncertain holds whose
archived responses reported no usage (generation lookup through the ledger's reconciliation path;
a failed lookup keeps the hold; audit under the ledger's reconciliations/ folder). The deterministic results report is
never modified. Nothing here may reach an experimental agent.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src", ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from research_swarm.inspect_adapter import disable_inspect_dotenv  # noqa: E402

disable_inspect_dotenv()

DEFAULT_RUN_LIMIT_USD = Decimal("20")
CONCURRENCY = {"opus": 4, "sonnet": 8, "fallback": 4}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-limit", type=Decimal, default=None,
                        help=f"this run's incident-report budget in USD (default {DEFAULT_RUN_LIMIT_USD})")
    parser.add_argument("--output-dir", type=Path, help="default RUN/analysis/incident")
    parser.add_argument("--judge-dir", type=Path, help="behavior judge outputs (default RUN/analysis/judge)")
    parser.add_argument("--mock", action="store_true", help="offline deterministic model (no network, no key)")
    parser.add_argument("--ledger", type=Path, help="ledger to bill (default this checkout's state/budget.sqlite)")
    parser.add_argument("--credentials-root", type=Path, default=ROOT,
                        help="directory whose private .env holds the provider key (paid only; never printed)")
    parser.add_argument("--pricing-record", type=Path, help="judge route pricing record (default: newest)")
    parser.add_argument("--history-workers", type=int, default=None)
    parser.add_argument("--no-repair", action="store_true", help="skip the writer's one revision pass")
    parser.add_argument("--no-model-fallback", action="store_true",
                        help="never send a refused unit to the non-Anthropic fallback model")
    parser.add_argument("--rerender-contagion", action="store_true",
                        help="host only, no model call and no budget: recompute the 'How the exploit spread' section "
                             "and the table of contents and insert them into the existing report.html, report.md "
                             "and evidence.json (idempotent)")
    parser.add_argument("--extract-model", choices=("opus", "sonnet"), default="opus",
                        help="model that picks excerpts of long records by segment number (default opus; "
                             "Sonnet refused Gemini transcripts on gemini10)")
    return parser.parse_args(argv)


def load_histories(run_dir, *, workers=None, log=None):
    """A RunHistory holding every agent (rebuilt in parallel by the judge's history builder) and the
    Inspect epoch of each sample."""
    from research.analysis.judge.history import build_runs, load_run_base, sample_index
    index = sample_index(run_dir)
    agents = sorted(index)
    built = build_runs(run_dir, agents, workers=workers, log=log)
    base = load_run_base(run_dir)
    for agent in agents:
        one = built[agent]
        base.agents[agent] = one.agents[agent]
        for record_id, item in one.conference.items.items():   # own records carry their tool call
            if item.get("agent_id") == agent:
                base.conference.items[record_id] = item
    return base, {agent: index[agent][2] for agent in agents}


def settle_leftover_holds(ledger, out_dir, credentials_root, log):
    """Startup pass: settle this output directory's leftover uncertain holds whose archived responses
    reported no usage (``client.reconcile_archived_holds``: OpenRouter's generation record, the ledger's
    reconciliation path); writes the audit file next to the other reconciliations. Never raises: a
    failure keeps every hold (it then counts at its reservation, as before)."""
    from research.analysis.judge.client import fetch_generation, reconcile_archived_holds
    from reconcile_ledger import write_audit
    from research_swarm.budget import BudgetLedger
    try:
        key = []

        def lookup(generation_id):
            if not key:
                from research_swarm.secrets import load_openrouter_key
                key.append(load_openrouter_key(credentials_root))
            return fetch_generation(generation_id, key[0])

        entries = reconcile_archived_holds(ledger, [out_dir / "raw"], lookup)
        if not entries:
            return []
        audit = write_audit(ledger, BudgetLedger(ledger), entries, mode="incident_startup_generation_lookup",
                            scope="analysis")
        settled = [e for e in entries if e["outcome"] == "reconciled"]
        log(f"settled {len(settled)} of {len(entries)} leftover no-usage hold(s) from OpenRouter generation records "
            f"(total ${sum(float(e['cost_usd']) for e in settled):.4f}); audit {audit}")
        return entries
    except Exception as error:  # never blocks the run: the holds stay counted at their reservations
        log(f"leftover-hold settlement skipped ({type(error).__name__}: {error})")
        return []


def label_run_id(run_dir):
    """The run id in this tool's ledger labels: never the bare run id, so the behavior judge's
    per-run budget does not count incident-report spend (and vice versa)."""
    return f"incident-{Path(run_dir).name}"


def main(argv=None):
    args = parse_args(argv)
    log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
    run_dir = args.run_dir.resolve()
    if not (run_dir / "results.json").is_file():
        log(f"refused: {run_dir} has no results.json (not a completed run)")
        return 2
    out_dir = (args.output_dir or run_dir / "analysis" / "incident").resolve()
    if args.mock and (out_dir == run_dir or run_dir in out_dir.parents):
        log("refused: --mock writes outside the run directory; pass --output-dir <scratch>/<run>/analysis/incident")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = open(out_dir / ".incident.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(f"refused: another incident-report invocation holds {out_dir / '.incident.lock'}; nothing was sent")
        return 2
    with lock:
        if args.rerender_contagion:
            return _rerender(args, run_dir, out_dir, log)
        return _run(args, run_dir, out_dir, log)


def _rerender(args, run_dir, out_dir, log):
    """--rerender-contagion: host-computed section and table of contents only (no model, no ledger)."""
    from research.analysis.incident.contagion import rerender
    record, written = rerender(run_dir, out_dir, judge_dir=args.judge_dir)
    summary = {"status": "rerendered", "source": record["source"], "exploit_users": record["exploit_users_total"],
               "techniques": [{"family": t["family"], **t["counts"]} for t in record["techniques"]],
               "written": written}
    print(json.dumps(summary, indent=1))
    return 0


def _run(args, run_dir, out_dir, log):
    from research.analysis.judge.client import FakeTransport, JudgeClient, RunLimit, committed_for_judging
    from research.analysis.judge.route import FALLBACK_KEY
    from research.analysis.incident.mock import MockModel
    from research.analysis.incident.pipeline import IncidentPipeline, StageError, write_json
    import run_transcript_judge as rtj
    from research_swarm.budget import DEFAULT_LEDGER_PATH

    label = label_run_id(run_dir)
    budget = args.run_limit if args.run_limit is not None else DEFAULT_RUN_LIMIT_USD
    if budget <= 0:
        log("refused: --run-limit must be positive")
        return 2
    if args.mock:
        routes = rtj.route_settings(args, paid=False)
        ledger, scope = (args.ledger or out_dir / "rehearsal-ledger.sqlite").resolve(), "rehearsal"
        if ledger == DEFAULT_LEDGER_PATH(ROOT).resolve() or ledger.name == "budget.sqlite":
            log("refused: --mock never uses the authoritative ledger")
            return 2
        sources = [ledger]
    else:
        ledger, scope = (args.ledger or DEFAULT_LEDGER_PATH(ROOT)).resolve(), "analysis"
        credentials_root = args.credentials_root.resolve()
        if not credentials_root.is_dir():
            log(f"refused: --credentials-root {credentials_root} is not a directory")
            return 2
        rtj.refresh_pricing(args, log=log, require_fallback=not args.no_model_fallback)
        try:
            routes = rtj.route_settings(args, paid=True)
        except rtj.RouteRefused as error:
            log(f"refused: {error}")
            return 2
        sources = rtj.judging_ledgers(ledger, out_dir, search=(run_dir,), log=log)
        settle_leftover_holds(ledger, out_dir, credentials_root, log)
    prior = committed_for_judging(sources, out_dir, scope=scope, run_id=label)
    room = budget - Decimal(prior["committed_usd"])
    if room <= 0:
        log(f"refused: no room under this run's incident-report budget (${budget}; already committed "
            f"${prior['committed_usd']}); nothing was sent. Pass a larger --run-limit to raise the budget")
        return 2
    run_limit = RunLimit(str(budget), source="override" if args.run_limit is not None else "default",
                         basis="run_limit" if args.run_limit is not None else "default", prior_usd=prior["committed_usd"],
                         prior=prior, reservation_factor=prior["reservation_factor"])
    log(f"incident report: budget ${budget} (already committed ${prior['committed_usd']}); ledger {ledger} "
        f"run {run_limit.run_id} (room ${run_limit.room})")
    log("rebuilding agent histories (offline)...")
    run, epochs = load_histories(run_dir, workers=args.history_workers, log=log)
    roles = {"opus": "opus", "extract": args.extract_model}   # role -> judge route key
    models = {"plan_write": routes.route("opus").model_id, "extract": routes.route(args.extract_model).model_id}
    if routes.has_fallback and not args.no_model_fallback:
        roles["fallback"] = FALLBACK_KEY
        models["fallback"] = routes.route(FALLBACK_KEY).model_id
    elif not args.no_model_fallback:
        log("note: the judge route pricing record has no valid fallback route; a refused unit gets host-chosen "
            "excerpts only")
    if args.mock:
        transport = FakeTransport(responder=MockModel())
        made = {key: JudgeClient(key, ledger, scope=scope, archive_dir=out_dir / "raw", settings=routes,
                                 http=transport, max_concurrency=CONCURRENCY[key], run_limit=run_limit,
                                 run_id=label) for key in set(roles.values())}
    else:
        made = {key: JudgeClient(key, ledger, scope=scope, archive_dir=out_dir / "raw", settings=routes, http=None,
                                 max_concurrency=CONCURRENCY[key], paid_authorized=True,
                                 credentials_root=credentials_root, run_limit=run_limit, run_id=label)
                for key in set(roles.values())}
        kept = rtj.keep_pricing_record(out_dir, routes)
        rtj.record_run_budget(out_dir, {"limit_usd": str(budget), "source": run_limit.source,
                                        "prior_usd": prior["committed_usd"], "ledger_run_id": run_limit.run_id,
                                        "label_run_id": label, "pricing_record": None if kept is None else str(kept)},
                              run_dir=run_dir, ledger=ledger)
        for client in made.values():
            client.prepare()   # loads the key in memory only; unpaid provider-limit record
    clients = {role: made[key] for role, key in roles.items()}
    pipeline = IncidentPipeline(run_dir, out_dir, run, clients=clients, mode="mock" if args.mock else "paid",
                                models=models, epochs=epochs, judge_dir=args.judge_dir, repair=not args.no_repair,
                                log=log)
    code = 0
    try:
        summary = asyncio.run(pipeline.run())
    except StageError as error:
        code = 3 if error.reason == "run_limit" else 2
        summary = {"status": "stopped" if code == 3 else "failed", "stage": error.stage, "reason": error.reason,
                   "detail": error.detail, "units": pipeline.units}
    summary["budget"] = run_limit.record()
    write_json(out_dir / "state.json", summary)
    print(json.dumps({k: summary.get(k) for k in ("status", "report", "verification", "storylines", "stage", "reason")}
                     | {"spent_usd": summary["budget"]["settled_usd"], "budget_usd": str(budget)}, indent=1,
                     default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
