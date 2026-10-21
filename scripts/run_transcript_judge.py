#!/usr/bin/env python3
"""Researcher-only misbehavior judge: estimate, rehearse (mock) or run (paid).

docs/plan.md, "Superseding decision (October 17, 2026, evening): misbehavior-focused judge"
(cheating, whistleblowing and evaluation awareness only), applying rubric gdm-judge-rubric/3 as
judge revision v3 ("Behavior judge iteration specified, October 19, 2026"): Claude Haiku 5.5
screens every window narrowly; Claude Sonnet 5.5 decides every candidate (prefilter
selections, forced windows, keyword recall, submissions) on its own in small batches, a long
keyword-matching candidate shown only as its keyword passages (--passage-min-chars,
--passage-chars, --act-passage-min-chars), and judges each random-sample window in its own
request; Claude Opus 5.5 verifies findings one per request against windows of the cited
records (--verifier-window-chars, --verifier-edge-chars, --verifier-full-chars,
--verifier-neighbor-chars): every cheating and whistleblowing finding, an agent's
evaluation-awareness findings one at a time until its level and certainty are settled; and
writes the short run-level summary. --judge-mode package is the superseded v2/v2.1
one-call-per-agent package. A judge directory written by judge revisions v2 to v2.3 (rubric
gdm-judge-rubric/2, e.g. the cache100 outputs) is refused: there is no upgrade path, pass a new
--output-dir. Procedure and outputs in research/analysis/judge/README.md. Nothing here may
enter agent containers.

  # Cost estimate: no model calls, no ledger writes, no credentials. Prints the selection scenarios
  # and what a judging run with the same flags would send now, with its run limit.
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> --dry-run-estimate
  # Unpaid rehearsal with the deterministic mock model; output OUTSIDE the run directory.
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> --mock \
      --output-dir <scratch>/<run>/analysis/judge
  # Paid judging (the default mode; GCP VM, authoritative ledger; docs/gcp-vm.md, docs/validation.md).
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> \
      [--credentials-root /path/to/main/checkout] [--run-limit USD | --run-config CONFIG]
  # Re-apply host facts (e.g. new evaluator verdicts) from cached units only: no model calls.
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> --rescore-cached
  # Switch an existing judge directory of this revision from package mode to candidate judging,
  # reusing its paid prefilter units: the old judge outputs move to analysis/judge/superseded/
  # (never a v2.x directory: v3 refuses those).
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> \
      --judge-mode candidates --upgrade-judge-revision

No estimate, no confirmation and no approval flag (owner decisions, October 20, 2026: "Don't
require estimates for anything"; docs/plan.md "One per-run budget, no manual approvals (October 20,
2026)"). Every judging invocation (--mock or paid) is one run with its own budget, its fixed run
limit: --run-limit USD, else the run config's behavior_judge_max_usd (--run-config CONFIG), else
$50. It never sends a request that would push its committed spend (in-flight holds, settled and
uncertain costs) past that budget, and stops cleanly instead (stop reason run_limit; a resume pays
nothing twice); the ledger registers the invocation as one run with the same budget. There is no
cumulative analysis cap. --mock, --dry-run-estimate (optional, never required) and
--rescore-cached never load credentials, never call a real model and never write the
authoritative ledger. A paid run refreshes the judge route pricing record automatically when it
is missing or older than 72 h (scripts/freeze_judge_route.py, unpaid public catalog); a record that
no longer validates for the pinned route is refused.
"""

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


def _usd(value):
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError):
        raise argparse.ArgumentTypeError(f"{value!r} is not an amount in USD") from None
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("the run limit must be a positive amount in USD")
    return amount


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
    parser.add_argument("--run-config", "--standing-approval-from-config", dest="run_config", type=Path,
                        metavar="CONFIG",
                        help="paid run: the judged run's config; its behavior_judge_max_usd (any positive amount, "
                             "else $50) is this judging run's budget unless --run-limit is given (any track)")
    parser.add_argument("--stage", default="report", help="run every stage up to and including this one: "
                        "history, prefilter, judge, verify, host, closed, runlevel, report (default)")
    parser.add_argument("--agents", action="append", help="agent subset, comma separated (repeatable)")
    parser.add_argument("--pricing-record", type=Path, help=f"judge route pricing record (default: newest "
                                                            f"data/validation/{PRICING_GLOB})")
    parser.add_argument("--window-tokens", type=int, default=24000, help="screening window size in estimated "
                                                                         "tokens (default 24000)")
    parser.add_argument("--neighbors", type=_neighbors, default=1, help="context items either side of a selected "
                                                                       "item (default 1), or 'turn'")
    parser.add_argument("--package-token-cap", type=int, default=350000)
    parser.add_argument("--sample-fraction", type=float, default=0.05)
    parser.add_argument("--sample-seed", type=int, default=20261017)
    # Judge revision v3: one finding per Opus request (the v2.x --verifier-batch, --verifier-token-cap and
    # --verifier-context are gone); what the verifier is shown of the cited records.
    parser.add_argument("--verifier-window-chars", type=int, default=4000,
                        help="characters either side of each quoted span of a long cited record (default 4000)")
    parser.add_argument("--verifier-edge-chars", type=int, default=1000,
                        help="the first and last characters of a long cited record shown as well (default 1000)")
    parser.add_argument("--verifier-full-chars", type=int, default=10000,
                        help="a cited record, or the result or reply of a cited act, this long or shorter is shown in "
                             "full (default 10000)")
    parser.add_argument("--verifier-neighbor-chars", type=int, default=1500,
                        help="characters of the records just before and after each cited record (default 1500)")
    parser.add_argument("--haiku-concurrency", type=int, default=48)
    parser.add_argument("--sonnet-concurrency", type=int, default=32)
    parser.add_argument("--opus-concurrency", type=int, default=16)
    parser.add_argument("--agent-concurrency", type=int, default=20,
                        help="agents processed at once (default 20; every history is prebuilt before the run)")
    parser.add_argument("--history-workers", type=int, default=None,
                        help="worker processes rebuilding agent histories in parallel before judging (default: "
                             "one per agent, at most the CPUs, 16 and one per 6 GB of memory; 1 rebuilds in this process)")
    parser.add_argument("--max-selected-fraction", type=float, default=0.20,
                        help="hold an agent before its judge call when the prefilter selected more than this share "
                             "of its item tokens (default 0.20; 1 disables)")
    parser.add_argument("--no-prompt-cache", action="store_true",
                        help="do not mark the Sonnet/Opus system prompt for prompt caching (on by default; "
                             "reservations never assume the cache discount)")
    parser.add_argument("--retry-failed", action="store_true", help="redo units recorded as grading_failed (paid)")
    parser.add_argument("--recompute-stale", action="store_true", help="redo cached units whose prompt changed")
    parser.add_argument("--no-label-check", action="store_true", help="skip timeline.check_labels in the report stage")
    parser.add_argument("--judge-mode", choices=("candidates", "package"),
                        help="candidates (default; judge revision v3 under rubric gdm-judge-rubric/3: every candidate "
                             "decided on its own in small batches, a long keyword-matching candidate shown as its "
                             "keyword passages) or package (the superseded v2/v2.1 one-call-per-agent package); "
                             "--rescore-cached defaults to the judge directory's recorded mode")
    parser.add_argument("--candidate-batch-items", type=int, default=10,
                        help="candidates per Sonnet request in candidate mode (default 10)")
    parser.add_argument("--candidate-batch-tokens", type=int, default=35000,
                        help="estimated tokens of candidate blocks per Sonnet request in candidate mode (default "
                             "35000; a single larger candidate gets its own request, never truncated)")
    parser.add_argument("--candidate-context-chars", type=int, default=800,
                        help="characters of the previous and next item shown as context around each candidate "
                             "(default 800)")
    parser.add_argument("--passage-min-chars", type=int, default=4000,
                        help="a reasoning or assistant-text candidate longer than this that matches the keyword net "
                             "is shown only as its keyword passages (default 4000)")
    parser.add_argument("--passage-chars", type=int, default=500,
                        help="characters either side of each keyword match in a keyword passage (default 500)")
    parser.add_argument("--act-passage-min-chars", type=int, default=12000,
                        help="the same threshold for the agent's acts and communications (tool calls, file versions, "
                             "submissions, messages, posts, notebooks, feedback; default 12000)")
    parser.add_argument("--upgrade-judge-revision", action="store_true",
                        help="switch an existing judge directory whose only binding difference is the judge mode or "
                             "revision to the requested mode: its previous judge outputs move to superseded/, the "
                             "cached prefilter units are reused (recorded in manifest.json and state.json); between "
                             "two candidate-mode revisions the cached candidate/sample/verify units also stay and are "
                             "reused only where the request is byte-identical (add --recompute-stale to redo the rest). "
                             "Never applies to a v2.x judge directory (rubric gdm-judge-rubric/2), which judge revision "
                             "v3 refuses")
    parser.add_argument("--run-limit", type=_usd, metavar="USD",
                        help="this judging invocation's budget (paid or mock): no request is sent that would push the "
                             "invocation's committed spend (in-flight holds plus settled and uncertain costs) past it; "
                             "default the --run-config's behavior_judge_max_usd, else $50. No estimate is made")
    parser.add_argument("--estimate-output", type=Path, help="write the full estimate JSON here")
    parser.add_argument("--estimate-fractions", default="0.02,0.05,0.10,0.20")
    return parser.parse_args(argv)


def settings_from(args, recorded=None):
    """PipelineSettings from the flags. ``recorded`` (a judge directory's manifest settings,
    for --rescore-cached) supplies the judge mode and candidate settings when --judge-mode is
    not given (absent candidate settings mean the v2/v2.1 package mode)."""
    from research.analysis.judge.pipeline import PipelineSettings
    judge_mode = args.judge_mode or ("candidates" if recorded is None else recorded.get("judge_mode", "package"))
    candidate = {"candidate_batch_items": args.candidate_batch_items,
                 "candidate_batch_tokens": args.candidate_batch_tokens,
                 "candidate_context_chars": args.candidate_context_chars,
                 "passage_min_chars": args.passage_min_chars, "passage_chars": args.passage_chars,
                 "act_passage_min_chars": args.act_passage_min_chars}
    if recorded is not None and args.judge_mode is None:
        candidate.update({k: recorded[k] for k in candidate if k in recorded})
    return PipelineSettings(judge_mode=judge_mode, **candidate,
                            window_tokens=args.window_tokens, neighbors=args.neighbors,
                            package_token_cap=args.package_token_cap, sample_fraction=args.sample_fraction,
                            sample_seed=args.sample_seed, verifier_window_chars=args.verifier_window_chars,
                            verifier_edge_chars=args.verifier_edge_chars, verifier_full_chars=args.verifier_full_chars,
                            verifier_neighbor_chars=args.verifier_neighbor_chars,
                            prompt_cache=not args.no_prompt_cache, max_selected_fraction=args.max_selected_fraction,
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


def record_age_hours(path):
    """Age of a judge route pricing record from its ``created_at`` (None when unreadable)."""
    import datetime
    try:
        created = datetime.datetime.fromisoformat(str(json.loads(Path(path).read_text())["created_at"])
                                                  .replace("Z", "+00:00"))
        created = created if created.tzinfo else created.replace(tzinfo=datetime.timezone.utc)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() / 3600


def refresh_pricing(args, *, freeze=None, log=None):
    """Before a paid run: refresh the judge route pricing record (scripts/freeze_judge_route.py,
    unpaid public catalog, no credential) when the newest one is missing or older than the route's
    72 h window. An explicit --pricing-record is used as given. Never raises (a failed refresh is
    reported and the newest record is used). Returns the reason it refreshed, or None."""
    from research.analysis.judge.route import DEFAULT_MAX_AGE_HOURS
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    if args.pricing_record:
        return None
    record = pricing_record(args)
    age = None if record is None else record_age_hours(record)
    if record is not None and age is not None and 0 <= age <= DEFAULT_MAX_AGE_HOURS:
        return None
    reason = "no judge route pricing record" if record is None else (
        f"the newest judge route pricing record is {'of unknown age' if age is None else f'{age:.1f} h old'}")
    log(f"Refreshing the judge route pricing record ({reason}; unpaid public catalog, no credential).")
    try:
        if freeze is None:
            if str(ROOT / "scripts") not in sys.path:
                sys.path.insert(0, str(ROOT / "scripts"))
            from freeze_judge_route import main as freeze
        if freeze([]):
            log("The refreshed judge route pricing record did not validate (see the record).")
    except Exception as error:  # e.g. no network: the newest record is used
        log(f"Judge route pricing refresh failed ({type(error).__name__}: {error}); using the newest record.")
    return reason


def route_settings(args, *, paid):
    """Frozen route settings. A paid run uses the newest record after ``refresh_pricing`` (a record
    still stale after a failed refresh is used, with its age in the run's route description);
    offline modes accept a stale record (prices only) and fall back to the observed defaults only
    when no record exists. A record that does not validate (for example one predating the Sonnet
    route) is refused."""
    from research.analysis.judge.route import JudgeRouteSettings
    record = pricing_record(args)
    try:
        if paid:
            if record is None:
                raise RouteRefused("no judge route pricing record (the automatic refresh with "
                                   "scripts/freeze_judge_route.py did not write one)")
            return JudgeRouteSettings.from_pricing_record(record, allow_stale=True)
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


def _assumption_line(assumptions, settings, prompt_cache):
    return (f"pricing: provider tokens = characters/4 x {assumptions['tokenizer_factor_central']:g} central "
            f"(x {assumptions['tokenizer_factor_high']:g} high and stress; measured on the paid cache100 units: 1.8-2.0); "
            f"output tokens per request (thinking included): prefilter {assumptions['haiku_output_per_window']:,}, "
            f"candidates {assumptions['candidate_output_base']:,} + {assumptions['candidate_output_per_item']:,} per "
            f"candidate, random-sample window {assumptions['sample_window_output']:,}, verifier "
            f"{assumptions['verifier_output_per_call']:,}, run level {assumptions['opus_runlevel_output']:,} (stress x "
            f"{assumptions['output_factor_stress']:g}, never above the output limits verifier "
            f"{settings['max_tokens']['verifier']:,}, run level {settings['max_tokens']['runlevel']:,}); one repair at "
            f"{assumptions['candidate_repair_rate']:.0%} (Sonnet), {assumptions['verifier_repair_rate']:.0%} (Opus), "
            f"{assumptions['prefilter_repair_rate']:.0%} (Haiku); prompt cache "
            + ("on (its saving is in the totals)" if prompt_cache else "off"))


def print_candidate_estimate(result, out=None):
    """The candidate-mode (judge revision v3) scenario table: per stage the central dollars, the
    prompt-cache saving, then the central, high and stress totals."""
    out = out or sys.stdout
    projection = result["projection"]
    haiku = projection["haiku"]
    settings = result["settings"]
    assumptions = result["assumptions"]
    remaining = projection.get("remaining_usd")
    revision = (result.get("judge_revision") or "").split(":", 1)[0] or result.get("judge_revision")
    print(f"run: {result['run_dir']}  judge revision: {revision}", file=out)
    print("allocation: " + "; ".join(f"{row['stage']}={row['model_key']} ({row['effort']})"
                                      for row in result["allocation"]), file=out)
    print(f"agents: {len(result['agents'])}; windows: {projection['windows']} (~{settings['window_tokens']:,} "
          f"tokens each); item tokens: {sum(a['item_tokens'] for a in result['agents']):,}", file=out)
    cached = haiku.get("cached") or 0
    print(f"Haiku prefilter: {haiku['requests']} window(s) to screen"
          + (f" ({cached} cached prefilter unit(s) match the current requests: reused, never re-requested)"
             if cached else "") + f": ${haiku['usd_central']} central, ${haiku['usd_high']} high", file=out)
    reuse = (result.get("actual_prefilter") or {}).get("prefilter_reuse")
    missing = (result.get("actual_prefilter") or {}).get("agents_without_cached_prefilter") or []
    if reuse:
        print(f"prefilter reuse check: {reuse['matching']} of {reuse['units']} cached prefilter units match the "
              f"current requests byte for byte" + (f"; MISMATCHED (priced again): {', '.join(reuse['mismatched'][:10])}"
                                                  if reuse["mismatched"] else "" if missing
                                                  else " (no prefilter request would be sent)"),
              file=out)
    if missing:
        print(f"WARNING: {len(missing)} agent(s) have no complete cached prefilter stage for the current record "
              f"({', '.join(missing[:10])}{', ...' if len(missing) > 10 else ''}): a judging run would request "
              "their missing prefilter units (or refuse a changed record), and the 'actual' scenario is omitted",
              file=out)
    print(f"design: candidates = prefilter selections + forced windows + keyword recall + submissions, chronological "
          f"batches of <= {settings['candidate_batch_items']} items / ~{settings['candidate_batch_tokens']:,} tokens, "
          f"{settings['candidate_context_chars']} context characters each side; keyword passages "
          f"(+/- {settings['passage_chars']} characters of each match) for keyword-matching reasoning/text over "
          f"{settings['passage_min_chars']:,} characters and acts/communications over "
          f"{settings['act_passage_min_chars']:,}, everything else in full; random sample "
          f"{settings['sample_fraction']:.0%} of unselected windows, one request each; Opus verifies one finding per "
          f"request (cited records in full up to {settings['verifier_full_chars']:,} characters, else +/- "
          f"{settings['verifier_window_chars']:,} around each quote plus the first and last "
          f"{settings['verifier_edge_chars']:,}; neighbours {settings['verifier_neighbor_chars']:,}) of an assumed "
          f"{assumptions['assumed_positive_findings']} positive findings per run"
          + (f" (scaled to {projection['agents']} of its {projection['agents_in_run']} agents)"
             if projection.get("agents_in_run") and projection["agents_in_run"] > projection["agents"] else "")
          + f", {assumptions['awareness_finding_share']:.0%} "
          f"of them evaluation awareness checked about {assumptions['awareness_checks_per_flagged_agent']:g} per flagged "
          f"agent until settled (early stop), every other finding checked; output limits verifier "
          f"{settings['max_tokens']['verifier']:,}, run level {settings['max_tokens']['runlevel']:,}", file=out)
    print(_assumption_line(assumptions, settings, result["runtime"]["prompt_cache"]), file=out)
    fits = f"fits ${remaining}" if remaining is not None else f"fits ${projection['cap_usd']}"
    print(f"{'scenario':>11} {'sel tok':>7} {'cands':>6} {'psg':>5} {'cand tok':>10} {'sent tok':>10} {'calls':>5} "
          f"{'sample':>6} {'vreq':>5} | {'haiku':>6} {'cands$':>7} {'sample$':>7} {'verify':>7} {'run':>5} {'cache':>6} | "
          f"{'central':>7} {'high':>7} {'stress':>7} | {fits}", file=out)
    for name, row in projection["scenarios"].items():
        central, high, stress = row["central"], row["high"], row["stress"]
        usd = central["usd"]
        fit = [central["fits_remaining"], high["fits_remaining"]] if remaining is not None else \
            [central["fits_cap"], high["fits_cap"]]
        saving = central.get("prompt_cache_saving_usd") or "0.00"
        print(f"{name:>11} {row['selected_token_fraction']:>7.1%} {row['candidate_items']:>6} "
              f"{row.get('passage_candidates', 0):>5} {row['candidate_item_tokens']:>10,} "
              f"{row['candidate_input_tokens']:>10,} {row['candidate_calls']:>5} {row['sample_calls']:>6} "
              f"{central.get('verifier_calls', 0):>5g} | "
              f"{usd['haiku_prefilter']:>6} {usd['sonnet_candidates']:>7} {usd['sonnet_sample_windows']:>7} "
              f"{usd['opus_verifier']:>7} {usd['opus_runlevel']:>5} {'-' + saving:>6} | "
              f"{central['total_usd_with_prompt_cache']:>7} {high['total_usd_with_prompt_cache']:>7} "
              f"{stress['total_usd_with_prompt_cache']:>7} | {'yes' if all(fit) else 'no'}", file=out)
    print("cands = candidates, psg = of which shown as keyword passages, cand tok = the candidates' full item tokens, "
          "sent tok = estimated tokens (characters/4) of the candidate requests as rendered (system prompt included), "
          "calls = candidate requests, sample = random-sample window requests, vreq = expected one-finding Opus "
          "verifier requests (central). USD per stage (central): haiku = Haiku prefilter (windows without a matching "
          "cached unit), cands$ = Sonnet candidate requests, sample$ = Sonnet random-sample windows, verify = Opus "
          "verification of the assumed positive findings, one per request over measured windows of real records, run "
          "= Opus run-level summary, cache = prompt-cache saving. central/high/stress = totals with that saving; "
          "high = provider tokens x "
          f"{assumptions['tokenizer_factor_high']:g} instead of x {assumptions['tokenizer_factor_central']:g}; stress "
          f"= high inputs, outputs and findings x {assumptions['output_factor_stress']:g}. 'recall-only' = no prefilter "
          "selection (keyword recall and submissions only: a lower bound); 'actual' = the cached Haiku selections of "
          "the judge directory. The fit column compares central and high with "
          + ("the remaining allowance." if remaining is not None else "this judging run's budget."), file=out)
    print("largest reservation per request: " + ", ".join(f"{k} ${v}" for k, v in
                                                         projection["largest_reservation_usd"].items())
          + "; at full concurrency: " + ", ".join(f"{k} ${v}" for k, v in
                                                  projection["in_flight_reservation_ceiling_usd"].items()), file=out)


def print_invocation_estimate(estimate, limit, out=None, *, analysis=None, note=None):
    """What one judging invocation would send now (``estimate.plan_invocation``; cached units
    free) with its central, high and stress cost, the run limit it gets and the analysis scope's
    history in the ledger."""
    from research.analysis.judge.estimate import STAGES
    out = out or sys.stdout
    columns = estimate["columns"]
    agents = estimate["agents"]
    print(f"\nThis judging invocation (--stage {estimate['stage']}, {len(agents)} of {estimate['agents_in_run']} "
          f"agent(s), {estimate['judge_mode']} mode; a cached unit that matches its request is reused, free):", file=out)
    if note:
        print(f"  note: {note}", file=out)
    print(f"  {'stage':<26} {'model':<6} {'requests':>9} {'cached':>7} {'central':>9} {'high':>9} {'stress':>9}",
          file=out)
    for stage, (model, _, label) in STAGES.items():
        row = estimate["requests"][stage]
        if not row["to_send"] and not row["cached"] and stage not in ("haiku_prefilter",):
            continue
        print(f"  {label:<26} {model:<6} {row['to_send']:>9g} {row['cached']:>7} "
              + " ".join(f"{'$' + _cents(columns[c]['usd'][stage]):>9}" for c in ("central", "high", "stress")),
              file=out)
    if any(Decimal(columns[c]["prompt_cache_saving_usd"]) for c in columns):
        print(f"  {'prompt-cache saving':<26} {'':<6} {'':>9} {'':>7} "
              + " ".join(f"{'-$' + _cents(columns[c]['prompt_cache_saving_usd']):>9}"
                         for c in ("central", "high", "stress")), file=out)
    print(f"  {'total':<26} {'':<6} {'':>9} {'':>7} {'$' + estimate['central_usd']:>9} {'$' + estimate['high_usd']:>9} "
          f"{'$' + estimate['stress_usd']:>9}", file=out)
    selection = estimate["selection"]
    if estimate["stage"] not in ("history", "prefilter"):
        print(f"  selections: {len(selection['cached'])} agent(s) from their cached Haiku units, "
              f"{len(selection['assumed'])} assumed"
              + (f" (random {selection['assumed_fraction']:.0%} clusters of item tokens: no complete cached prefilter "
                 "stage yet; screen first with --stage prefilter for an exact estimate)" if selection["assumed"] else "")
              + (f", {len(selection['held'])} held by the selection guard (nothing sent): {', '.join(selection['held'])}"
                 if selection["held"] else ""), file=out)
    verification = estimate["verification"]
    if pl_stage_reached(estimate["stage"], "verify"):
        print(f"  verification: {len(verification['exact'])} agent(s) from findings rebuilt from cached judge units "
              "(an unchecked evaluation-awareness finding assumed confirmed as claimed), "
              f"{len(verification['assumed'])} from the assumed findings"
              + (f" ({verification['assumed_findings']}: {estimate['assumptions']['assumed_positive_findings']} per run "
                 f"scaled to {len(verification['assumed'])} of its {estimate['agents_in_run']} agents, checked with the "
                 "early stop)" if verification["assumed"] else ""), file=out)
    if estimate.get("stale_units"):
        print(f"  WARNING: {estimate['stale_units']} cached unit(s) were made from a different request: the run refuses "
              "them unless --recompute-stale (priced here as re-sent)", file=out)
    largest = estimate.get("largest_reservation_usd") or {}
    if largest:
        print("  largest single reservation: " + ", ".join(f"{k} ${v}" for k, v in largest.items()), file=out)
    rule = ("--run-limit override" if limit["source"] != "estimate" else
            f"central ${limit['central_usd']} + {limit['reservation_margin']} x the largest single reservation "
            f"${limit['largest_reservation_usd']}, above {limit['factor']} x central so that request can be admitted"
            if limit.get("basis") == "largest_reservation" else
            f"{limit['factor']} x central ${limit['central_usd']}, at least ${limit['floor_usd']}")
    print(f"  run limit: ${limit['limit_usd']} ({rule}): no request is sent that would push this invocation's committed "
          "spend (in-flight holds, settled and uncertain costs) past it; the run then stops cleanly (stop reason "
          "run_limit) and a resume pays nothing twice", file=out)
    small = [k for k, v in largest.items() if Decimal(v) > Decimal(limit["limit_usd"])]
    if small:
        print(f"  WARNING: a single {', '.join(small)} reservation exceeds the run limit; pass a larger --run-limit",
              file=out)
    elif largest and Decimal(estimate["central_usd"]) + max(Decimal(v) for v in largest.values()) \
            > Decimal(limit["limit_usd"]):
        print("  note: the run limit leaves less than one largest reservation above the central estimate, so the run "
              "may stop (cleanly) before its last requests; resume it without --run-limit (the automatic limit "
              "covers the largest request), or pass a larger --run-limit", file=out)
    if analysis is not None:
        print(f"  analysis scope history: committed ${analysis.get('committed_usd')} over all judging runs (no "
              "cumulative cap; each judging run has its own budget)", file=out)


def _cents(value):
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def pl_stage_reached(stage, target):
    from research.analysis.judge.pipeline import STAGES
    return STAGES.index(stage) >= STAGES.index(target)


def print_estimate(result, out=None):
    out = out or sys.stdout
    if (result.get("projection") or {}).get("judge_mode") == "candidates":
        return print_candidate_estimate(result, out=out)
    projection = result["projection"]
    haiku = projection["haiku"]
    settings = result["settings"]
    remaining = projection.get("remaining_usd")
    print(f"run: {result['run_dir']}", file=out)
    print("allocation: " + "; ".join(f"{row['stage']}={row['model_key']} ({row['effort']})"
                                      for row in result["allocation"]), file=out)
    print(f"agents: {len(result['agents'])}; windows: {projection['windows']} (~{settings['window_tokens']} "
          f"tokens each); item tokens: {sum(a['item_tokens'] for a in result['agents']):,}", file=out)
    print(f"Haiku prefilter: {haiku['requests']} requests ({haiku['requests_at_upper_tier']} at the >= "
          f"{haiku['upper_tier_threshold']}-token tier), input {haiku['input_tokens']:,} tokens, output "
          f"{haiku['output_tokens']:,} tokens (assumed); ${haiku['usd_central']} central, ${haiku['usd_high']} high, "
          f"${haiku['usd_stress']} stress", file=out)
    assumptions = result["assumptions"]
    print(f"package: selected items +/- {settings['neighbors']} neighbor(s), selected communications and exposure "
          f"only, every submission, random sample {settings['sample_fraction']:.0%} of unselected windows; cap "
          f"{settings['package_token_cap']:,} est. tokens; one finding per verifier request; "
          f"assumed {assumptions['flagged_agent_share']:.0%} of agents with findings "
          f"({assumptions['findings_per_flagged_agent']} each); prompt cache "
          f"{'on' if result['runtime']['prompt_cache'] else 'off'}", file=out)
    fits = f"fits ${remaining}" if remaining is not None else f"fits ${projection['cap_usd']}"
    header = (f"{'scenario':>9} {'sel tok':>7} {'pkg tokens':>11} {'comms':>5} {'sample':>6} {'split':>5} "
              f"{'haiku':>6} {'judge':>6} {'verify':>6} {'run':>5} {'TOTAL':>7} {'cached':>7} {'high':>7} "
              f"{'stress':>7} {fits:>12}")
    print(header, file=out)
    for name, row in projection["scenarios"].items():
        central, high, stress = row["central"], row["high"], row["stress"]
        usd = central["usd"]
        fit = [central["fits_remaining"], high["fits_remaining"]] if remaining is not None else \
            [central["fits_cap"], high["fits_cap"]]
        print(f"{name:>9} {row['selected_token_fraction']:>7.1%} {row['package_user_tokens_total']:>11,} "
              f"{row['composition']['communications_shown']:>5} {row['sample_windows']:>6} {row['agents_split']:>5} "
              f"{usd['haiku_prefilter']:>6} {usd['sonnet_judge']:>6} {usd['opus_verifier']:>6} "
              f"{usd['opus_runlevel']:>5} {central['total_usd']:>7} {central['total_usd_with_prompt_cache']:>7} "
              f"{high['total_usd']:>7} {stress['total_usd']:>7} {str(all(fit)):>12}", file=out)
    print("Per stage (central, USD): haiku = Haiku prefilter (windows without a matching cached unit), judge = "
          "Sonnet judging (parts and synthesis included), verify = Opus verification (weighted by the assumed share "
          "of agents with findings), run = Opus run-level summary. TOTAL = central (characters/4 x "
          f"{assumptions['tokenizer_factor_central']:g} provider tokens, no cache discount); cached = central with "
          "prompt caching of the Sonnet/Opus system prompts; high = provider tokens x "
          f"{assumptions['tokenizer_factor_high']:g}; stress = high inputs, outputs and flagged share x "
          f"{assumptions['output_factor_stress']}. 'sel tok' = share of item tokens selected; 'comms' = "
          "communications shown. The fit column compares central and high (with the cache saving) with "
          + ("the remaining allowance." if remaining is not None else "this judging run's budget."),
          file=out)
    print("largest reservation per request: " + ", ".join(f"{k} ${v}" for k, v in
                                                         projection["largest_reservation_usd"].items())
          + "; at full concurrency: " + ", ".join(f"{k} ${v}" for k, v in
                                                  projection["in_flight_reservation_ceiling_usd"].items()), file=out)


DEFAULT_RUN_LIMIT_USD = Decimal("50")


def fixed_run_limit(override, judge_config=None):
    """The invocation's budget without any estimate: ``--run-limit``, else the run config's
    ``behavior_judge_max_usd`` (``judge_config`` from ``auto_judge.judge_settings``), else
    DEFAULT_RUN_LIMIT_USD ($50). No cumulative cap clamps it."""
    if override is not None:
        return {"limit_usd": str(Decimal(str(override))), "source": "override", "basis": "override"}
    if judge_config is not None and judge_config.get("budget_source") != "default":
        return {"limit_usd": str(Decimal(str(judge_config["max_usd"]))), "source": "run_config",
                "basis": judge_config["budget_source"]}
    return {"limit_usd": str(DEFAULT_RUN_LIMIT_USD), "source": "default", "basis": "default"}


def record_run_budget(judge_dir, entry, *, run_dir, ledger):
    """Append this paid invocation's budget to ``judge_dir/run-budgets.json``."""
    import datetime
    path = Path(judge_dir) / "run-budgets.json"
    try:
        records = json.loads(path.read_text())
    except (OSError, ValueError):
        records = []
    records.append(dict(entry, at=datetime.datetime.now(datetime.timezone.utc).isoformat(), run_dir=str(run_dir),
                        ledger=str(ledger)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2) + "\n")


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
    from research_swarm.budget import DEFAULT_LEDGER_PATH, read_snapshot
    if args.stage not in pl.STAGES:
        return refuse(f"--stage must be one of {', '.join(pl.STAGES)}")
    recorded = None
    if args.rescore_cached:
        manifest = pl.read_json((args.output_dir or run_dir / "analysis" / "judge").resolve() / "manifest.json")
        recorded = dict((manifest or {}).get("settings") or {}, **({"judge_mode": manifest["judge_mode"]}
                                                                   if (manifest or {}).get("judge_mode") else {}))
    if args.upgrade_judge_revision and (args.dry_run_estimate or args.rescore_cached):
        return refuse("--upgrade-judge-revision applies to a judging run (--mock or a paid run), not to "
                      "--dry-run-estimate or --rescore-cached")
    try:
        settings = settings_from(args, recorded)
    except ValueError as error:
        return refuse(str(error))
    agents = _agents(args.agents)
    log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731

    judge_config = None
    if args.run_config is not None:
        if args.mock or args.dry_run_estimate or args.rescore_cached:
            return refuse("--run-config applies to a paid judging run only")
        from research_swarm.auto_judge import judge_settings
        config_path = args.run_config.resolve()
        try:
            judge_config = judge_settings(json.loads(config_path.read_text()), config_path)
        except (OSError, ValueError) as error:
            return refuse(f"no valid judge settings in {config_path}: {error}")
    if args.run_limit is not None and (args.dry_run_estimate or args.rescore_cached):
        return refuse("--run-limit applies to a judging run (--mock or a paid run), not to --dry-run-estimate or "
                      "--rescore-cached")
    from research.analysis.judge import estimate as est

    if args.dry_run_estimate:
        routes = route_settings(args, paid=False)
        fractions = tuple(float(x) for x in args.estimate_fractions.split(","))
        judge_dir = (args.output_dir or run_dir / "analysis" / "judge").resolve()
        ledger = args.ledger or DEFAULT_LEDGER_PATH(ROOT)
        snapshot = read_snapshot(ledger) if Path(ledger).exists() else None
        analysis = (snapshot or {}).get("analysis")
        # The invocation block plans exactly what a judging run with these flags would send now; a
        # judge directory such a run would refuse (e.g. a v2.x one) is planned as a new one.
        probe = pl.Pipeline(run_dir, judge_dir, mode="paid", clients={}, settings=settings, agents=agents,
                            stage=args.stage, scope="analysis", models=models_of(routes),
                            upgrade_judge_revision=args.upgrade_judge_revision)
        note, planned_dir = None, judge_dir
        try:
            probe.check_manifest(write=False)
        except pl.PipelineError as error:
            reason = str(error).splitlines()[0].split(". ")[0].replace(str(judge_dir / "manifest.json") + " ", "")
            note, planned_dir = (f"a judging run would refuse {judge_dir} (its manifest {reason[:240]}); planned as "
                                 "for a new --output-dir"), None
        from research.analysis.judge.history import HistoryCache, sample_index
        all_agents = sorted(sample_index(run_dir))
        unknown = sorted(set(agents or ()) - set(all_agents))
        if unknown:
            return refuse("No such agent in the run: " + ", ".join(unknown))
        histories = HistoryCache.build(run_dir, sorted(agents) if agents else all_agents,
                                       workers=args.history_workers, log=log)
        planner = est.InvocationPlanner(planned_dir, settings, routes, stage=args.stage, retry_failed=args.retry_failed,
                                        recompute_stale=args.recompute_stale, all_agents=all_agents)
        result = pl.estimate_run(run_dir, settings, routes, agents=agents, fractions=fractions, log=log,
                                 judge_dir=judge_dir if (judge_dir / "manifest.json").exists() else None,
                                 cap_usd=Decimal(fixed_run_limit(args.run_limit)["limit_usd"]),
                                 remaining_usd=None, on_agent=planner.add,
                                 agents_in_run=len(all_agents), loader=histories)
        invocation = planner.finish(agents=sorted(agents) if agents else all_agents)
        limit = est.invocation_run_limit(invocation)
        result["ledger_analysis"] = analysis
        result["invocation"] = dict(invocation, note=note, run_limit=limit)
        if args.estimate_output:
            pl.write_json(args.estimate_output, result)
        print_estimate(result)
        print_invocation_estimate(invocation, limit, note=note, analysis=analysis)
        return 0

    judge_dir = (args.output_dir or run_dir / "analysis" / "judge").resolve()
    authoritative = DEFAULT_LEDGER_PATH(ROOT).resolve()
    from research.analysis.judge.client import FakeTransport, JudgeClient, RunLimit
    rescore, run_limit = False, None
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
        from research.analysis.judge.history import sample_index
        all_agents = sorted(sample_index(run_dir))
        scope, mode, models, clients = "rehearsal", "rehearsal", models_of(routes), {}
    else:
        ledger = (args.ledger or DEFAULT_LEDGER_PATH(ROOT)).resolve()
        credentials_root = args.credentials_root.resolve()
        if not credentials_root.is_dir():
            return refuse(f"--credentials-root {credentials_root} is not a directory")
        refresh_pricing(args, log=log)
        routes = route_settings(args, paid=True)
        all_agents = None
        scope, mode, models, clients = "analysis", "paid", models_of(routes), {}
    pipeline = pl.Pipeline(run_dir, judge_dir, mode=mode, clients=clients, settings=settings, agents=agents,
                           stage=args.stage, scope=scope, ledger_path=ledger, models=models,
                           route=((pl.read_json(judge_dir / "pipeline.json") or {}).get("route") if rescore else None)
                           or describe_route(routes), agent_ids=all_agents, retry_failed=args.retry_failed,
                           recompute_stale=args.recompute_stale, label_check=not args.no_label_check,
                           rescore_only=rescore, log=log, upgrade_judge_revision=args.upgrade_judge_revision)
    if not rescore:
        try:
            # Refuse a judge directory with other binding settings (e.g. the superseded 12-dimension
            # attempt, or a v2.x directory) before the estimate, the confirmation and any key.
            pipeline.check_manifest(write=False)
        except pl.PipelineError as error:
            return refuse(str(error))
        # Owner decisions, October 20, 2026 ("Don't require estimates for anything"; "One per-run
        # budget, no manual approvals"): no pre-run cost estimate and no confirmation. The
        # invocation's budget: --run-limit, else the run config's behavior_judge_max_usd, else
        # DEFAULT_RUN_LIMIT_USD. It stops the run cleanly; the ledger enforces it for the run too.
        invocation = None
        limit = fixed_run_limit(args.run_limit, judge_config)
        run_limit = RunLimit(limit["limit_usd"], source=limit["source"], estimate=None, factor=None,
                             floor_usd=None, basis=limit["basis"], run_config=judge_config)
        pipeline.run_limit = run_limit
    # Every agent's history is rebuilt once, in parallel worker processes, before any request and before
    # the event loop starts (never in a thread beside the HTTP threads); the pipeline reads it from memory.
    from research.analysis.judge.history import HistoryCache, sample_index
    if all_agents is None:
        all_agents = sorted(sample_index(run_dir))
    unknown = sorted(set(agents or ()) - set(all_agents))
    if unknown:
        return refuse("No such agent in the run: " + ", ".join(unknown))
    pipeline.use_histories(HistoryCache.build(run_dir, sorted(agents) if agents else all_agents,
                                              workers=args.history_workers, log=log))
    if args.mock:
        from research.analysis.judge.history import agent_code
        chosen = agents or all_agents
        # Synthetic roles: some exploit agents (every fifth), one evaluation-aware agent (whose judge output
        # is corrupted once, exercising the repair), one whistleblower; most agents have nothing.
        mock = pl.MockModel(routes, refuse_once={("judge", chosen[0])},
                            refuse_twice={("prefilter", f"{agent_code(chosen[0])}.w0002")},
                            corrupt_first={("judge", chosen[1])} if len(chosen) > 1 else set(),
                            exploit_agents=set(chosen[0::5]), aware_agents=set(chosen[1:2]),
                            whistle_agents=set(chosen[2:3]))
        transport = FakeTransport(responder=mock)
        clients.update({key: JudgeClient(key, ledger, scope=scope, archive_dir=judge_dir / "raw", settings=routes,
                                         http=transport, max_concurrency=settings.concurrency(key),
                                         run_limit=run_limit)
                        for key in MODEL_KEYS})
    elif not rescore:
        snapshot = (read_snapshot(ledger) or {}).get("analysis") or {}
        print(json.dumps({"run_dir": str(run_dir), "judge_dir": str(judge_dir), "ledger": str(ledger),
                          "credentials_root": str(credentials_root), "analysis_scope": snapshot,
                          "models": models_of(routes), "route": describe_route(routes),
                          "settings": settings.binding(), "runtime": settings.runtime(), "agents": agents or "all",
                          "stage": args.stage, "judge_mode": settings.judge_mode,
                          "upgrade_judge_revision": args.upgrade_judge_revision,
                          "run_limit": limit}, indent=2),
              file=sys.stderr)
        print(f"paid judging: run budget ${limit['limit_usd']} ({limit['basis']}); ledger run {run_limit.run_id}",
              file=sys.stderr)
        clients.update({key: JudgeClient(key, ledger, scope=scope, archive_dir=judge_dir / "raw", settings=routes,
                                         http=None, max_concurrency=settings.concurrency(key), paid_authorized=True,
                                         credentials_root=credentials_root, run_limit=run_limit)
                        for key in MODEL_KEYS})
    try:
        if mode == "paid" and not rescore:
            pipeline.check_manifest()  # writes the manifest (or performs the requested upgrade)
            record_run_budget(judge_dir, dict(limit, ledger_run_id=run_limit.run_id, run_config=judge_config),
                              run_dir=run_dir, ledger=ledger)
            for client in clients.values():
                client.prepare()  # loads the host key in memory and runs the unpaid provider-limit check
        summary = asyncio.run(pipeline.run())
    except pl.PipelineError as error:  # raised before any request (manifest/rubric binding)
        return refuse(str(error))
    short = {k: summary[k] for k in ("run_id", "mode", "stage", "status", "agents", "run_steps", "stop", "spend")}
    short["judge_mode"], short["judge_revision"] = settings.judge_mode, pipeline.judge_revision
    if run_limit is not None:
        record = run_limit.record()
        short["run_limit"] = {k: record[k] for k in ("limit_usd", "source", "basis", "settled_usd", "uncertain_usd",
                                                      "peak_committed_usd", "requests", "denied")}
    if pipeline.upgrade:
        short["judge_revision_upgrade"] = {k: pipeline.upgrade[k] for k in ("at", "archived_to", "superseded_spend",
                                                                            "prefilter_units_kept")}
    short["errors"] = [{k: e[k] for k in ("agent_id", "error")} for e in summary["errors"]]
    print(json.dumps(short, indent=2, default=str))
    return {"complete": 0, "stopped": 3}.get(summary["status"], 2)


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()  # a native crash (e.g. a segmentation fault) prints every thread's traceback
    sys.exit(main())
