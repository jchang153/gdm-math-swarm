#!/usr/bin/env python3
"""Researcher-only transcript behavior judge: estimate, rehearse (mock) or run (paid).

docs/plan.md, "Transcript behavior judge specified, October 17, 2026", with the
superseding "no human marks" and "downsized model allocation" decisions: Claude Haiku 5.5
screens, Claude Sonnet 5.5 judges (random-sample windows folded in), Claude Opus 5.5
verifies nuanced findings and writes the run-level analysis, Sonnet verifies context-only
findings. Procedure and outputs in research/analysis/judge/README.md. Nothing here may
enter agent containers.

  # Cost projection: no model calls, no ledger writes, no credentials.
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> --dry-run-estimate
  # Unpaid rehearsal with the deterministic mock model; output OUTSIDE the run directory.
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> --mock \
      --output-dir <scratch>/<run>/analysis/judge
  # Paid judging (GCP VM, authoritative ledger; docs/gcp-vm.md, docs/validation.md).
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> --i-have-user-approval \
      [--credentials-root /path/to/main/checkout]
  # Re-apply host facts (e.g. new evaluator verdicts) from cached units only: no model calls.
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> --rescore-cached

--mock, --dry-run-estimate and --rescore-cached never load credentials, never call a real
model and never write the authoritative ledger (the mock uses its own rehearsal ledger
inside the output directory). A paid run needs --i-have-user-approval, an interactive
terminal, the applied audited analysis raise (budget.judge_analysis_problems), a fresh
judge route pricing record (scripts/freeze_judge_route.py, at most 72 h old) and the
operator typing the analysis scope's remaining allowance exactly as displayed. Only then
does JudgeClient load the host key, in memory, from the private .env under
--credentials-root (default: this checkout). Exit codes: 0 complete, 2 errors, 3 stopped
by the budget ledger, 4 refused before any request.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from research_swarm.inspect_adapter import disable_inspect_dotenv  # noqa: E402

PRICING_GLOB = "judge-route-pricing-*.json"
MODEL_KEYS = ("haiku", "sonnet", "opus")


def _agents(values):
    out = []
    for value in values or []:
        out += [part.strip() for part in value.split(",") if part.strip()]
    return out or None


def _neighbors(value):
    return "turn" if value == "turn" else int(value)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, help="judge directory (default RUN/analysis/judge)")
    parser.add_argument("--ledger", type=Path, help="budget ledger (default state/budget.sqlite; mock: a rehearsal "
                                                     "ledger inside the output directory)")
    parser.add_argument("--credentials-root", type=Path, default=ROOT,
                        help="paid run only: directory whose private .env holds the OpenRouter key (default: this "
                             "checkout); lets a separate checkout use the main checkout's .env without copying it")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="unpaid rehearsal with the deterministic mock model")
    mode.add_argument("--dry-run-estimate", action="store_true", help="windows, selection scenarios and cost "
                                                                      "projection; no model calls")
    mode.add_argument("--rescore-cached", action="store_true",
                      help="recompute host stages from cached model units only (no model calls), e.g. after new "
                           "evaluator verdicts for closed submissions")
    parser.add_argument("--i-have-user-approval", action="store_true",
                        help="required for a paid (non-mock) run; an interactive confirmation follows")
    parser.add_argument("--stage", default="report", help="run every stage up to and including this one: "
                        "history, prefilter, judge, verify, host, closed, runlevel, report (default)")
    parser.add_argument("--agents", action="append", help="agent subset, comma separated (repeatable)")
    parser.add_argument("--pricing-record", type=Path, help=f"judge route pricing record (default: newest "
                                                            f"data/validation/{PRICING_GLOB})")
    parser.add_argument("--window-tokens", type=int, default=24000, help="screening window size in estimated "
                                                                         "tokens (default 24000)")
    parser.add_argument("--neighbors", type=_neighbors, default=2, help="context items either side of a selected "
                                                                       "item (default 2), or 'turn'")
    parser.add_argument("--package-token-cap", type=int, default=350000)
    parser.add_argument("--sample-fraction", type=float, default=0.05)
    parser.add_argument("--sample-seed", type=int, default=20261017)
    parser.add_argument("--verifier-batch", type=int, default=12)
    parser.add_argument("--verifier-context", type=int, default=3, help="items either side of each cited item in "
                                                                        "the verifier's passages (default 3)")
    parser.add_argument("--haiku-concurrency", type=int, default=24)
    parser.add_argument("--sonnet-concurrency", type=int, default=8)
    parser.add_argument("--opus-concurrency", type=int, default=6)
    parser.add_argument("--agent-concurrency", type=int, default=8,
                        help="agents processed at once (each holds its rebuilt history in memory)")
    parser.add_argument("--no-prompt-cache", action="store_true",
                        help="do not mark the Sonnet/Opus system prompt for prompt caching (on by default; "
                             "reservations never assume the cache discount)")
    parser.add_argument("--retry-failed", action="store_true", help="redo units recorded as grading_failed (paid)")
    parser.add_argument("--recompute-stale", action="store_true", help="redo cached units whose prompt changed")
    parser.add_argument("--no-label-check", action="store_true", help="skip timeline.check_labels in the report stage")
    parser.add_argument("--mock-cap", default="40", help="rehearsal ledger cap in USD (mock only)")
    parser.add_argument("--estimate-output", type=Path, help="write the full estimate JSON here")
    parser.add_argument("--estimate-fractions", default="0.05,0.10,0.20,0.30")
    return parser.parse_args(argv)


def settings_from(args):
    from research.analysis.judge.pipeline import PipelineSettings
    return PipelineSettings(window_tokens=args.window_tokens, neighbors=args.neighbors,
                            package_token_cap=args.package_token_cap, sample_fraction=args.sample_fraction,
                            sample_seed=args.sample_seed, verifier_batch=args.verifier_batch,
                            verifier_context=args.verifier_context, prompt_cache=not args.no_prompt_cache,
                            haiku_concurrency=args.haiku_concurrency, sonnet_concurrency=args.sonnet_concurrency,
                            opus_concurrency=args.opus_concurrency,
                            agent_concurrency=args.agent_concurrency).validate()


def pricing_record(args):
    if args.pricing_record:
        return args.pricing_record
    found = sorted((ROOT / "data" / "validation").glob(PRICING_GLOB))
    return found[-1] if found else None


class RouteRefused(Exception):
    pass


def route_settings(args, *, paid):
    """Frozen route settings: a fresh record for a paid run; offline modes accept a stale
    record (prices only) and fall back to the observed defaults only when no record exists.
    A record that does not validate (for example one predating the Sonnet route) is refused."""
    from research.analysis.judge.route import JudgeRouteSettings
    record = pricing_record(args)
    try:
        if paid:
            if record is None:
                raise RouteRefused("no judge route pricing record; run scripts/freeze_judge_route.py first")
            return JudgeRouteSettings.from_pricing_record(record)
        if record is not None:  # frozen prices for the projection/rehearsal; freshness not required offline
            return JudgeRouteSettings.from_pricing_record(record, allow_stale=True)
    except (ValueError, OSError) as error:
        raise RouteRefused(f"judge route pricing record {record}: {error}") from None
    return JudgeRouteSettings.defaults(pricing_verified=True)


def describe_route(settings):
    record = settings.pricing_record
    try:
        record = str(Path(record).resolve().relative_to(ROOT)) if record else None
    except ValueError:
        pass
    return {"pricing_record": record, "pricing_record_sha256": settings.pricing_record_sha256,
            "settings_sha256": settings.sha256(), "provider": settings.provider_preferences()}


def models_of(settings):
    return {key: {"model": settings.route(key).model_id, "effort": settings.route(key).effort,
                  "canonical_slug": settings.route(key).canonical_slug} for key in MODEL_KEYS}


def print_estimate(result, out=sys.stdout):
    projection = result["projection"]
    haiku = projection["haiku"]
    settings = result["settings"]
    print(f"run: {result['run_dir']}", file=out)
    print("allocation: " + "; ".join(f"{row['stage']}={row['model_key']} ({row['effort']})"
                                      for row in result["allocation"]), file=out)
    print(f"agents: {len(result['agents'])}; windows: {projection['windows']} (~{settings['window_tokens']} "
          f"tokens each); item tokens: {sum(a['item_tokens'] for a in result['agents']):,}", file=out)
    print(f"Haiku prefilter: {haiku['requests']} requests ({haiku['requests_at_upper_tier']} at the >= "
          f"{haiku['upper_tier_threshold']}-token tier), input {haiku['input_tokens']:,} tokens, output "
          f"{haiku['output_tokens']:,} tokens (assumed); ${haiku['usd_central']} central, ${haiku['usd_high']} high, "
          f"${haiku['usd_stress']} stress", file=out)
    print(f"neighbors={settings['neighbors']}, package cap {settings['package_token_cap']:,} est. tokens, "
          f"random sample {settings['sample_fraction']:.0%} of unselected windows, verifier context "
          f"{settings['verifier_context']}, prompt cache {'on' if result['runtime']['prompt_cache'] else 'off'}",
          file=out)
    header = (f"{'scenario':>9} {'pkg tokens':>11} {'sample':>6} {'split':>5} {'judge':>6} {'v-opus':>6} "
              f"{'v-son':>6} {'run':>5} {'TOTAL':>7} {'cached':>7} {'high':>7} {'stress':>7} {'fits $40':>8}")
    print(header, file=out)
    for name, row in projection["scenarios"].items():
        central, high, stress = row["central"], row["high"], row["stress"]
        usd = central["usd"]
        print(f"{name:>9} {row['package_user_tokens_total']:>11,} {row['sample_windows']:>6} "
              f"{row['agents_split']:>5} {usd['sonnet_judge']:>6} {usd['opus_verifier']:>6} "
              f"{usd['sonnet_verifier']:>6} {usd['opus_runlevel']:>5} {central['total_usd']:>7} "
              f"{central['total_usd_with_prompt_cache']:>7} {high['total_usd']:>7} {stress['total_usd']:>7} "
              f"{str(central['fits_cap'] and high['fits_cap']):>8}", file=out)
    print("TOTAL = central (characters/4, no cache discount); cached = central with prompt caching of the "
          "Sonnet/Opus system prompts; high = inputs x 4/3; stress = high inputs and outputs x "
          f"{result['assumptions']['output_factor_stress']}. Totals include the Haiku prefilter.", file=out)
    print("largest reservation per request: " + ", ".join(f"{k} ${v}" for k, v in
                                                         projection["largest_reservation_usd"].items())
          + "; at full concurrency: " + ", ".join(f"{k} ${v}" for k, v in
                                                  projection["in_flight_reservation_ceiling_usd"].items()), file=out)


def refuse(message):
    print(f"REFUSED: {message}", file=sys.stderr)
    return 4


def main(argv=None):
    try:
        return _main(argv)
    except RouteRefused as error:
        return refuse(str(error))


def _main(argv=None):
    args = parse_args(argv)
    run_dir = args.run_dir.resolve()
    if not (run_dir / "inspect").is_dir():
        return refuse(f"{run_dir} has no inspect/ directory (not a saved run)")
    disable_inspect_dotenv()  # before anything can import inspect_ai
    from research.analysis.judge import pipeline as pl
    from research_swarm.budget import DEFAULT_LEDGER_PATH, judge_analysis_problems, read_snapshot
    if args.stage not in pl.STAGES:
        return refuse(f"--stage must be one of {', '.join(pl.STAGES)}")
    try:
        settings = settings_from(args)
    except ValueError as error:
        return refuse(str(error))
    agents = _agents(args.agents)
    log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731

    if args.dry_run_estimate:
        routes = route_settings(args, paid=False)
        fractions = tuple(float(x) for x in args.estimate_fractions.split(","))
        judge_dir = (args.output_dir or run_dir / "analysis" / "judge").resolve()
        result = pl.estimate_run(run_dir, settings, routes, agents=agents, fractions=fractions, log=log,
                                 judge_dir=judge_dir if (judge_dir / "manifest.json").exists() else None)
        ledger = args.ledger or DEFAULT_LEDGER_PATH(ROOT)
        snapshot = read_snapshot(ledger) if Path(ledger).exists() else None
        result["ledger_analysis"] = (snapshot or {}).get("analysis")
        if args.estimate_output:
            pl.write_json(args.estimate_output, result)
        print_estimate(result)
        return 0

    judge_dir = (args.output_dir or run_dir / "analysis" / "judge").resolve()
    authoritative = DEFAULT_LEDGER_PATH(ROOT).resolve()
    from research.analysis.judge.client import FakeTransport, JudgeClient
    rescore = False
    if args.rescore_cached:
        manifest = pl.read_json(judge_dir / "manifest.json")
        if manifest is None:
            return refuse(f"no {judge_dir / 'manifest.json'}; --rescore-cached only rescores an existing judge run")
        mode, scope, rescore, clients, all_agents = manifest["mode"], manifest["scope"], True, {}, None
        ledger = judge_dir / "rehearsal-ledger.sqlite" if mode == "rehearsal" else (args.ledger or DEFAULT_LEDGER_PATH(ROOT))
        routes = route_settings(args, paid=False)
        models = manifest["models"]
    elif args.mock:
        if judge_dir == run_dir or run_dir in judge_dir.parents:
            return refuse("--mock writes outside the run directory; pass --output-dir <scratch>/<run_id>/analysis/judge")
        ledger = (args.ledger or judge_dir / "rehearsal-ledger.sqlite").resolve()
        if ledger == authoritative or ledger.name == "budget.sqlite":
            return refuse("--mock never uses the authoritative ledger")
        routes = route_settings(args, paid=False)
        from research.analysis.judge.history import agent_code, sample_index
        all_agents = sorted(sample_index(run_dir))
        chosen = agents or all_agents
        mock = pl.MockModel(routes, refuse_once={("judge", chosen[0])},
                            refuse_twice={("prefilter", f"{agent_code(chosen[0])}.w0002")},
                            corrupt_first={("judge", chosen[1])} if len(chosen) > 1 else set())
        transport = FakeTransport(responder=mock)
        scope, mode, models = "rehearsal", "rehearsal", models_of(routes)
        clients = {key: JudgeClient(key, ledger, scope=scope, archive_dir=judge_dir / "raw", settings=routes,
                                    http=transport, max_concurrency=settings.concurrency(key),
                                    ledger_caps={scope: str(Decimal(args.mock_cap))}) for key in MODEL_KEYS}
    else:
        if not args.i_have_user_approval:
            return refuse("choose --dry-run-estimate, --mock, or a paid run with --i-have-user-approval")
        if not sys.stdin.isatty():
            return refuse("a paid run needs an interactive terminal for the typed confirmation")
        ledger = (args.ledger or DEFAULT_LEDGER_PATH(ROOT)).resolve()
        problems = judge_analysis_problems(ledger)
        if problems:
            return refuse("analysis ledger not ready: " + "; ".join(problems))
        credentials_root = args.credentials_root.resolve()
        if not credentials_root.is_dir():
            return refuse(f"--credentials-root {credentials_root} is not a directory")
        routes = route_settings(args, paid=True)
        snapshot = (read_snapshot(ledger) or {}).get("analysis") or {}
        all_agents = None
        print(json.dumps({"run_dir": str(run_dir), "judge_dir": str(judge_dir), "ledger": str(ledger),
                          "credentials_root": str(credentials_root), "analysis_scope": snapshot,
                          "models": models_of(routes), "route": describe_route(routes),
                          "settings": settings.binding(), "runtime": settings.runtime(), "agents": agents or "all",
                          "stage": args.stage}, indent=2), file=sys.stderr)
        typed = input("Type the analysis scope remaining_usd shown above to start PAID judging: ").strip()
        if not snapshot.get("remaining_usd") or typed != snapshot["remaining_usd"]:
            return refuse("typed allowance does not match the ledger; nothing was sent")
        scope, mode, models = "analysis", "paid", models_of(routes)
        clients = {key: JudgeClient(key, ledger, scope=scope, archive_dir=judge_dir / "raw", settings=routes,
                                    http=None, max_concurrency=settings.concurrency(key), paid_authorized=True,
                                    credentials_root=credentials_root) for key in MODEL_KEYS}
        for client in clients.values():
            client.prepare()  # loads the host key in memory and runs the unpaid provider-limit check
    pipeline = pl.Pipeline(run_dir, judge_dir, mode=mode, clients=clients, settings=settings, agents=agents,
                           stage=args.stage, scope=scope, ledger_path=ledger, models=models,
                           route=((pl.read_json(judge_dir / "pipeline.json") or {}).get("route") if rescore else None)
                           or describe_route(routes), agent_ids=all_agents, retry_failed=args.retry_failed,
                           recompute_stale=args.recompute_stale, label_check=not args.no_label_check,
                           rescore_only=rescore, log=log)
    summary = asyncio.run(pipeline.run())
    short = {k: summary[k] for k in ("run_id", "mode", "stage", "status", "agents", "run_steps", "stop", "spend")}
    short["errors"] = [{k: e[k] for k in ("agent_id", "error")} for e in summary["errors"]]
    print(json.dumps(short, indent=2, default=str))
    return {"complete": 0, "stopped": 3}.get(summary["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
