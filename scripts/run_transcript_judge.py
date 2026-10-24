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
  # Live behavior judge: one screening pass DURING a run (the supervisor starts one every few minutes
  # for configs with auto_behavior_judge true, behavior_judge "full" and live_behavior_judge true;
  # docs/plan.md, "Live behavior judge during the run (October 20, 2026)"). Only final windows are screened (Haiku), into RUN/analysis/judge/live/;
  # the post-run judge adopts each unit whose request is byte-identical (research/analysis/judge/warm.py).
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> --live \
      --run-config CONFIG
  # Switch an existing judge directory of this revision from package mode to candidate judging,
  # reusing its paid prefilter units: the old judge outputs move to analysis/judge/superseded/
  # (never a v2.x directory: v3 refuses those).
  PYTHONPATH=src:. .venv/bin/python scripts/run_transcript_judge.py --run-dir runs/<run> \
      --judge-mode candidates --upgrade-judge-revision

No estimate, no confirmation and no approval flag (owner decisions, October 20, 2026: "Don't
require estimates for anything"; docs/plan.md "One per-run budget, no manual approvals (October 20,
2026)"). The judge's one spending control is the budget of the judged run's judging (the judge
keeps its own fixed per-run limit): --run-limit USD, else the run config's behavior_judge_max_usd
(--run-config CONFIG, else the config the run recorded in RUN/settings.json), else $50. Every
judging invocation of the run (live passes, the early stages, the post-run judge, any resume, a
re-judge into another --output-dir) counts what the earlier ones committed
(client.committed_for_judging: the ledger runs its judge directory's run-budgets.json records, the
rows its request archives name and every judge request labelled with the run), so no invocation
gets a fresh budget. An invocation never sends a request that would push the committed spend
(in-flight holds at their full reservation, settled and uncertain costs) past the budget and stops
cleanly instead (stop reason run_limit; a resume pays nothing twice); the ledger registers the
invocation as one run whose budget is the room left. There is no cumulative analysis cap, no live
share and no provider-key launch gate (a live pass is deferred, automatically, while the key shared
with the running pilot lacks headroom for it). An invocation bills --ledger, else this checkout's
state/budget.sqlite, and reads its judge directory's earlier spend from every ledger it was counted
in (judging_ledgers: the recorded ledger, or complete copies with its ledger_id, e.g. found in the
run directory); it refuses only when some recorded spend is in no available ledger file
(nothing is sent). Every spending judging invocation of one run holds the run's lock (keyed by the
ledger and the run id: <ledger folder>/.judge-run-locks/, whatever --output-dir) and its judge
directory's lock (<judge dir>/.judge.lock). SIGTERM stops a judging invocation cleanly: nothing more
is admitted, the requests in flight settle, the state is recorded and it exits 3 (a resume pays
nothing twice); a second SIGTERM (or SIGINT) interrupts at once. --mock, --dry-run-estimate
(optional, never required) and --rescore-cached never load credentials, never call a real model and
never write the authoritative ledger. A paid run refreshes the judge route pricing record
automatically (scripts/freeze_judge_route.py, unpaid public catalog, retried) when the newest one is
missing, older than 72 h or no longer valid; it sends nothing with a record that is still stale or
invalid after that (a live pass is then deferred to the next pass), and copies the record it uses
into <judge dir>/route-pricing/, so the run's records carry it.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
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
    parser.add_argument("--pilot-ledger", type=Path,
                        help="paid --live pass: the ledger the running pilot reserves against, read for the automatic "
                             "key-headroom deferral (default --ledger; a budget track's pilot has its own ledger)")
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
                             "else $50) is the judging run's budget unless --run-limit is given (any track). Default: "
                             "the config the run recorded in RUN/settings.json")
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
    parser.add_argument("--max-selected-fraction", type=float, default=1.0,
                        help="optional guard: hold an agent before its judge call when the prefilter selected more "
                             "than this share of its item tokens (default 1, off; the per-run judge budget is the "
                             "only spending control)")
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
                        help="the judging run's budget (paid or mock): no request is sent that would push the "
                             "committed spend of this judge directory's judging (earlier invocations included; "
                             "in-flight holds plus settled and uncertain costs) past it. Default: the run config's "
                             "behavior_judge_max_usd, else $50. No estimate is made")
    parser.add_argument("--live", action="store_true",
                        help="one live screening pass while the run is going (Haiku screens only the windows that can "
                             "no longer change, into <judge dir>/live/; the post-run judge adopts byte-identical units); "
                             "paid with --run-config of a config enabling live_behavior_judge, or --mock")
    parser.add_argument("--live-margin-seconds", type=float, default=None,
                        help="live pass: items newer than this many seconds are never final (default 120)")
    parser.add_argument("--prefilter-window-count", choices=("on", "off"),
                        help="show the agent's window count in each Haiku screening request (on: the v3 bytes) or not "
                             "(off: what live screening needs). Default: the judge directory's recorded setting, else "
                             "the one RUN/analysis/judge recorded, else off when the run's own launch config "
                             "(RUN/settings.json) enabled paid live judging, else on; --live always uses off")
    parser.add_argument("--warm-from", type=Path,
                        help="adopt units screened during the run from this live store (default <judge dir>/live when "
                             "it exists)")
    parser.add_argument("--no-warm", action="store_true", help="never adopt units screened during the run")
    parser.add_argument("--lock-wait-seconds", type=float, default=900.0,
                        help="how long a judging invocation waits for the run's judge lock (default 900)")
    parser.add_argument("--estimate-output", type=Path, help="write the full estimate JSON here")
    parser.add_argument("--estimate-fractions", default="0.02,0.05,0.10,0.20")
    return parser.parse_args(argv)


def run_live_mode(run_dir):
    """The live judging mode the run's own launch config recorded (``RUN/settings.json`` "config",
    written when the run started, before any live pass): "paid", "mock" or None."""
    from research_swarm.auto_judge import live_mode
    try:
        config = json.loads((Path(run_dir) / "settings.json").read_text(encoding="utf-8")).get("config")
    except (OSError, ValueError, AttributeError):
        return None
    return live_mode(config) if isinstance(config, dict) else None


def window_count_setting(args, judge_dir=None, recorded=None):
    """``prefilter_window_count`` for this invocation: one rule every invocation of a run uses, so
    live passes and the post-run judge build identical screening requests, decided by recorded
    settings and the run's configuration, never by what the live passes happened to do. --live
    always False; else --prefilter-window-count; else the judge directory's recorded setting (a
    resume); else the setting the run's own judge directory (RUN/analysis/judge) recorded, so a
    re-judge of the saved run into another --output-dir reproduces the original judgment; else
    False when the run's own launch config enabled paid live judging (``run_live_mode``: recorded at
    the run's start, so the early stages and the post-run judge agree whether or not a live pass
    ever got far); else True (the v3 bytes). The free shadow of ``live_behavior_judge: "mock"``
    passes ``off`` explicitly."""
    if getattr(args, "live", False):
        return False
    if getattr(args, "prefilter_window_count", None):
        return args.prefilter_window_count == "on"
    from research.analysis.judge.pipeline import read_json
    run_dir = getattr(args, "run_dir", None)
    folders = [Path(judge_dir)] if judge_dir else []
    if run_dir is not None:
        own = Path(run_dir) / "analysis" / "judge"
        if not folders or own.resolve() != folders[0].resolve():
            folders.append(own)
    for settings in [recorded or {}] + [(read_json(folder / "manifest.json") or {}).get("settings") or {}
                                        for folder in folders]:
        if "prefilter_window_count" in settings:
            return bool(settings["prefilter_window_count"])
        if settings:
            return True   # a recorded binding without the key is the v3 default
    return not (run_dir is not None and run_live_mode(run_dir) == "paid")


def settings_from(args, recorded=None, judge_dir=None):
    """PipelineSettings from the flags. ``recorded`` (a judge directory's manifest settings,
    for --rescore-cached) supplies the judge mode and candidate settings when --judge-mode is
    not given (absent candidate settings mean the v2/v2.1 package mode); ``prefilter_window_count``
    follows ``window_count_setting``."""
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
                            agent_concurrency=args.agent_concurrency,
                            prefilter_window_count=window_count_setting(args, judge_dir, recorded)).validate()


def pricing_record(args):
    if args.pricing_record:
        return args.pricing_record
    found = sorted((ROOT / "data" / "validation").glob(PRICING_GLOB))
    return found[-1] if found else None


class RouteRefused(Exception):
    pass


def pricing_problem(record, *, require_fallback=False):
    """Why ``record`` cannot price a paid judging run now (missing, unreadable, older than 72 h, or
    no longer valid for the pinned route; ``JudgeRouteSettings.from_pricing_record``), or None. With
    ``require_fallback`` (the incident report), a record without a valid non-Anthropic fallback route
    (route.FALLBACK_KEY) is a problem too."""
    from research.analysis.judge.route import JudgeRouteSettings
    if record is None:
        return "no judge route pricing record"
    try:
        settings = JudgeRouteSettings.from_pricing_record(record)
    except (ValueError, OSError, KeyError, TypeError) as error:
        return f"judge route pricing record {Path(record).name}: {error}"
    if require_fallback and not settings.has_fallback:
        return f"judge route pricing record {Path(record).name} has no valid fallback route"
    return None


REFRESH_DELAYS_SECONDS = (10, 30)   # waits before the second and third refresh attempts


def refresh_pricing(args, *, freeze=None, log=None, sleep=None, require_fallback=False):
    """Before a paid run: refresh the judge route pricing record (scripts/freeze_judge_route.py,
    unpaid public catalog, no credential) when the newest one cannot price a paid run now
    (``pricing_problem``: missing, older than the route's 72 h window or no longer valid), trying
    up to three times. An explicit --pricing-record is used as given (``route_settings`` refuses it
    when it is stale or invalid). Never raises; returns the reason it refreshed, or None.
    ``route_settings(paid=True)`` then sends nothing unless the newest record is fresh and valid."""
    import time
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    sleep = sleep or time.sleep
    if args.pricing_record:
        return None
    reason = pricing_problem(pricing_record(args), require_fallback=require_fallback)
    if reason is None:
        return None
    log(f"Refreshing the judge route pricing record ({reason}; unpaid public catalog, no credential).")
    if freeze is None:
        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        from freeze_judge_route import main as freeze
    for attempt, delay in enumerate((0, *REFRESH_DELAYS_SECONDS), start=1):
        if delay:
            sleep(delay)
        try:
            if not freeze([]) and pricing_problem(pricing_record(args), require_fallback=require_fallback) is None:
                return reason
            log(f"Refresh attempt {attempt}: the new judge route pricing record did not validate (see the record).")
        except Exception as error:  # e.g. no network
            log(f"Refresh attempt {attempt} failed ({type(error).__name__}: {error}).")
    return reason


def route_settings(args, *, paid):
    """Frozen route settings. A paid run uses the newest record (after ``refresh_pricing``) only
    when it is fresh (at most 72 h old) and valid for the pinned route; otherwise ``RouteRefused``
    and nothing is sent. Offline modes accept a stale record (prices only) and fall back to the
    observed defaults only when no record exists. A record that does not validate (for example one
    predating the Sonnet route) is refused."""
    from research.analysis.judge.route import JudgeRouteSettings
    record = pricing_record(args)
    try:
        if paid:
            problem = pricing_problem(record)
            if problem is not None:
                raise RouteRefused(f"{problem} (the automatic unpaid refresh, scripts/freeze_judge_route.py, did not "
                                   "produce a fresh valid record; the next invocation tries again); nothing was sent")
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


class LimitRefused(ValueError):
    pass


def fixed_run_limit(override, judge_config=None, *, prior_usd=None):
    """The judging run's budget without any estimate: ``--run-limit``, else the run config's
    ``behavior_judge_max_usd`` (``judge_config``: ``auto_judge.judge_settings`` of --run-config, or
    ``recorded_budget_config`` of RUN/settings.json), else DEFAULT_RUN_LIMIT_USD ($50). No
    cumulative cap clamps it.

    The budget holds for the judged run's judging (docs/plan.md "One per-run budget, no manual
    approvals (October 20, 2026)": the judge keeps its own fixed per-run limit), whichever way it
    was given and whatever ``--output-dir``: ``prior_usd`` (what the run's earlier judging
    invocations committed, ``client.committed_for_judging``) always counts against it, so a resume
    or a re-judge never gets a fresh budget. The returned ``limit_usd`` is prior + room,
    with room = the budget minus prior (``RunLimit(prior_usd=...)`` starts at prior, and its ledger
    run's budget is the room). Raises LimitRefused when no room is left."""
    if override is not None:
        limit, source, basis = Decimal(str(override)), "override", "override"
    elif judge_config is not None and judge_config.get("budget_source") not in (None, "default"):
        limit, source, basis = Decimal(str(judge_config["max_usd"])), "run_config", judge_config["budget_source"]
    else:
        limit, source, basis = DEFAULT_RUN_LIMIT_USD, "default", "default"
    prior = Decimal(str(prior_usd or 0))
    room = limit - prior
    if prior:
        basis = basis + "_minus_prior"
    if room <= 0:
        raise LimitRefused(f"no room under the run's judging budget ({basis}: budget ${limit}, already committed by "
                           f"this run's judging ${prior}); nothing was sent. Pass a larger --run-limit to give this "
                           "run's judging a larger budget")
    return {"limit_usd": str(prior + room), "source": source, "basis": basis, "prior_usd": str(prior),
            "room_usd": str(room)}


def recorded_budget_config(run_dir):
    """The judge budget the judged run's own launch config sets (``RUN/settings.json`` "config",
    written when the run started): ``{"max_usd", "budget_source", "track", "config_path"}`` when it
    names ``behavior_judge_max_usd``, else None. Raises ValueError for an invalid amount."""
    from research_swarm.auto_judge import MAX_USD_KEY, config_track, judge_budget
    path = Path(run_dir) / "settings.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8")).get("config")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(config, dict) or config.get(MAX_USD_KEY) is None:
        return None
    amount, _ = judge_budget(config)
    return {"max_usd": str(amount), "budget_source": f"run settings.json {MAX_USD_KEY}",
            "track": config_track(config) or "baseline", "config_path": str(path)}


def ledger_bindings(judge_dir):
    """``{ledger_id: {"runs": {ledger run id: recorded ledger path}, "paths": [recorded paths]}}``: the
    ledgers the judge directory's earlier paid spend was counted in (``client.run_budget_records``)."""
    from research.analysis.judge.client import run_budget_records
    bound = {}
    for entry in run_budget_records(judge_dir):
        if not entry.get("ledger_id"):
            continue
        item = bound.setdefault(entry["ledger_id"], {"runs": {}, "paths": []})
        if entry.get("ledger_run_id"):
            item["runs"][str(entry["ledger_run_id"])] = entry.get("ledger")
        if entry.get("ledger") and entry["ledger"] not in item["paths"]:
            item["paths"].append(entry["ledger"])
    return bound


def ledger_id_of(path):
    """The ledger's id (opening an existing ledger assigns one to an older file); None when absent.
    Only for the ledger this invocation bills (it opens it for writing); ``read_ledger_id`` reads
    any other ledger."""
    from research_swarm.budget import BudgetLedger, read_ledger_id
    path = Path(path)
    if not path.exists():
        return None
    return read_ledger_id(path) or BudgetLedger(path).ledger_id


def _ledger_runs(path):
    from research_swarm.budget import list_runs
    try:
        return {run["run_id"] for run in list_runs(path)}
    except Exception:   # not a readable ledger
        return set()


def _ledger_copies(folders):
    """Every ``*.sqlite`` under ``folders`` (e.g. the supervisor's ``budget-final.sqlite`` retrieved
    with the run), as candidate copies of a ledger that is no longer at its recorded path."""
    found = []
    for folder in folders:
        folder = Path(folder)
        if folder.is_dir():
            found += sorted(p for p in folder.rglob("*.sqlite") if p.is_file())
    return found


def judging_ledgers(billing, judge_dir, *, search=(), log=None):
    """The ledgers that hold a judging invocation's prior spend, read only: ``billing`` (the ledger
    it bills: ``--ledger``, else this checkout's state/budget.sqlite) and, for every ledger the judge
    directory's earlier spend was counted in (``ledger_bindings``), files with that ``ledger_id``
    that together contain every ledger run recorded for it: ``billing`` itself, the recorded paths,
    then copies found under ``search`` (the run directory, retrieved from a stopped VM, carries the
    supervisor's ``budget-final.sqlite``). A stale copy alone (missing a recorded ledger run) is
    never enough. Spend counted in another ledger counts against the budget like the billing
    ledger's (``client.committed_for_judging`` reads them all), so judging continues automatically
    in any ledger. Raises ``RouteRefused`` (nothing is sent) only when some recorded ledger run
    cannot be found in any available file."""
    from research_swarm.budget import read_ledger_id
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    billing = Path(billing).resolve()
    sources = [billing]
    copies = None
    def has_id(path, ledger_id):
        try:
            return path.is_file() and read_ledger_id(path) == ledger_id
        except Exception:
            return False

    for ledger_id, bound in ledger_bindings(judge_dir).items():
        needed, found, chosen = set(), set(), []
        for run_id, recorded in bound["runs"].items():
            home = Path(recorded).resolve() if recorded else None
            if home is not None and has_id(home, ledger_id):
                # The ledger is still where the run was recorded: it is the authority for that run
                # (a ledger run registered there before its record and never used holds nothing).
                if home not in chosen:
                    chosen.append(home)
            else:
                needed.add(run_id)
        candidates = [billing] + [Path(p) for p in bound["paths"]]
        for stage in ("recorded", "copies"):
            if stage == "copies":
                if needed <= found:
                    break
                copies = _ledger_copies(search) if copies is None else copies
                candidates = copies
            for path in candidates:
                path = Path(path).resolve()
                if path in chosen or not has_id(path, ledger_id):
                    continue
                runs = _ledger_runs(path)
                if not (runs & (needed - found)):
                    continue
                chosen.append(path)
                found |= runs
        missing = sorted(needed - found)
        if missing:
            raise RouteRefused(f"{judge_dir} counted earlier judging spend in ledger {ledger_id} "
                               f"({', '.join(bound['paths']) or 'path not recorded'}), and no available copy of it "
                               f"holds its ledger run(s) {', '.join(missing)}, so this run's judging spend so far is "
                               "unknown; nothing was sent. Pass --ledger with that ledger, or put a copy of it (e.g. "
                               "the supervisor's budget-final.sqlite) in the run directory")
        for path in chosen:
            if path not in sources:
                if path != billing:
                    log(f"Counting earlier judging spend recorded in ledger {ledger_id}: {path}")
                sources.append(path)
    return sources


def keep_pricing_record(judge_dir, routes):
    """Copy the pricing record a paid invocation uses into ``<judge dir>/route-pricing/`` (unless
    already there), so the run's records carry the route every reservation was priced with.
    Returns the copy's path, or None."""
    import shutil
    source = routes.pricing_record
    if not source or not Path(source).is_file():
        return None
    target = Path(judge_dir) / "route-pricing" / Path(source).name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return target


def record_run_budget(judge_dir, entry, *, run_dir, ledger):
    """Append this paid invocation's budget, ledger run and ledger (path and ``ledger_id``) to
    ``judge_dir/run-budgets.json`` before its first request (``client.committed_for_judging`` and
    ``ledger_bindings`` read it)."""
    import datetime
    path = Path(judge_dir) / "run-budgets.json"
    try:
        records = json.loads(path.read_text())
    except (OSError, ValueError):
        records = []
    records.append(dict(entry, at=datetime.datetime.now(datetime.timezone.utc).isoformat(), run_dir=str(run_dir),
                        ledger=str(ledger), ledger_id=ledger_id_of(ledger)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2) + "\n")


def refuse(message):
    print(f"REFUSED: {message}", file=sys.stderr)
    return 4


DEFERRED = 5   # a live pass sent nothing and the next pass tries again (no key loaded, or no fresh pricing)


class Termination:
    """SIGTERM while judging (the supervisor stopping a live pass after the pilot exits, or the early
    stages after a failed follow-up or a timeout): close every registered run limit, so nothing more
    is admitted while the requests in flight finish and settle their ledger rows (never left
    ``reserved``); the pipeline then stops cleanly (exit 3) and records its state. Before a run
    limit exists nothing can be in flight, and a second signal means stop now: both raise
    KeyboardInterrupt (``finally`` blocks still release the locks)."""

    def __init__(self):
        self.limits = []
        self.received = None
        self.notify = self._write_stderr

    @staticmethod
    def _write_stderr(text):
        """Unbuffered (``os.write``): a print from a signal handler could interrupt a buffered write
        to the same stream and raise inside the judge."""
        try:
            os.write(2, text.encode("utf-8", "replace"))
        except OSError:
            pass

    def register(self, run_limit):
        if run_limit is not None:
            self.limits.append(run_limit)
            if self.received is not None:
                run_limit.close(self.reason())
        return run_limit

    def reason(self):
        return f"{signal.Signals(self.received).name} received: the judging process was asked to stop"

    def handle(self, signum, frame):  # noqa: ARG002
        if self.received is not None or not self.limits:
            raise KeyboardInterrupt(f"signal {signum}")
        self.received = signum
        for limit in self.limits:
            limit.close(self.reason())
        self.notify(f"STOPPING: {self.reason()}; finishing the requests in flight\n")

    def install(self):
        if threading.current_thread() is not threading.main_thread():
            return None
        return signal.signal(signal.SIGTERM, self.handle)

    @staticmethod
    def restore(previous):
        if previous is not None and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, previous)


def main(argv=None):
    termination = Termination()
    previous = termination.install()
    try:
        return _main(argv, termination)
    except RouteRefused as error:
        return refuse(str(error))
    finally:
        Termination.restore(previous)


def _main(argv=None, termination=None):
    termination = termination or Termination()
    args = parse_args(argv)
    run_dir = args.run_dir.resolve()
    if not (run_dir / "inspect").is_dir():
        return refuse(f"{run_dir} has no inspect/ directory (not a saved run)")
    disable_inspect_dotenv()  # before anything can import inspect_ai
    from research.analysis.judge import pipeline as pl
    from research_swarm.budget import DEFAULT_LEDGER_PATH, read_snapshot
    if args.stage not in pl.STAGES:
        return refuse(f"--stage must be one of {', '.join(pl.STAGES)}")
    judge_dir = (args.output_dir or run_dir / "analysis" / "judge").resolve()
    recorded = None
    if args.rescore_cached:
        manifest = pl.read_json(judge_dir / "manifest.json")
        recorded = dict((manifest or {}).get("settings") or {}, **({"judge_mode": manifest["judge_mode"]}
                                                                   if (manifest or {}).get("judge_mode") else {}))
    if args.upgrade_judge_revision and (args.dry_run_estimate or args.rescore_cached):
        return refuse("--upgrade-judge-revision applies to a judging run (--mock or a paid run), not to "
                      "--dry-run-estimate or --rescore-cached")
    if args.live and (args.dry_run_estimate or args.rescore_cached or args.upgrade_judge_revision):
        return refuse("--live is a screening pass of its own (--mock or paid); not with --dry-run-estimate, "
                      "--rescore-cached or --upgrade-judge-revision")
    if args.live and args.prefilter_window_count == "on":
        return refuse("--live needs --prefilter-window-count off (the agent's final window count is unknown until "
                      "the run stops)")
    try:
        settings = settings_from(args, recorded, judge_dir)
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
            judge_config = judge_settings(json.loads(config_path.read_text()), config_path, live=args.live)
        except (OSError, ValueError) as error:
            return refuse(f"no valid judge settings in {config_path}: {error}")
    if args.live and not (args.mock or args.dry_run_estimate or args.rescore_cached) and judge_config is None:
        return refuse("a paid --live pass needs --run-config CONFIG of a config with auto_behavior_judge, "
                      "behavior_judge \"full\" and live_behavior_judge true")
    # The judging run's budget source: --run-config, else the judge budget the run's own launch config
    # recorded (RUN/settings.json), else $50 (fixed_run_limit); --run-limit overrides both.
    budget_config = judge_config
    if budget_config is None and not (args.dry_run_estimate or args.rescore_cached):
        try:
            budget_config = recorded_budget_config(run_dir)
        except ValueError as error:
            return refuse(f"the run's recorded launch config ({run_dir / 'settings.json'}): {error}")
    if args.run_limit is not None and (args.dry_run_estimate or args.rescore_cached):
        return refuse("--run-limit applies to a judging run (--mock or a paid run), not to --dry-run-estimate or "
                      "--rescore-cached")
    from research.analysis.judge import estimate as est

    if args.dry_run_estimate:
        routes = route_settings(args, paid=False)
        fractions = tuple(float(x) for x in args.estimate_fractions.split(","))
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

    authoritative = DEFAULT_LEDGER_PATH(ROOT).resolve()
    rescore = False
    if args.rescore_cached:
        manifest = pl.read_json(judge_dir / "manifest.json")
        if manifest is None:
            return refuse(f"no {judge_dir / 'manifest.json'}; --rescore-cached only rescores an existing judge run")
        mode, scope, rescore, all_agents = manifest["mode"], manifest["scope"], True, None
        ledger = judge_dir / "rehearsal-ledger.sqlite" if mode == "rehearsal" else (args.ledger or DEFAULT_LEDGER_PATH(ROOT))
        routes = route_settings(args, paid=False)
        models = manifest["models"]
        credentials_root = None
    elif args.mock:
        if judge_dir == run_dir or run_dir in judge_dir.parents:
            return refuse("--mock writes outside the run directory; pass --output-dir <scratch>/<run_id>/analysis/judge")
        ledger = (args.ledger or judge_dir / "rehearsal-ledger.sqlite").resolve()
        if ledger == authoritative or ledger.name == "budget.sqlite":
            return refuse("--mock never uses the authoritative ledger")
        routes = route_settings(args, paid=False)
        all_agents = None if args.live else sorted(_sample_index(run_dir))
        scope, mode, models, credentials_root = "rehearsal", "rehearsal", models_of(routes), None
    else:
        # The ledger this invocation bills; earlier spend counted in other ledgers is read under the
        # judging locks (``_judging_prior``, ``judging_ledgers``).
        ledger = (args.ledger or DEFAULT_LEDGER_PATH(ROOT)).resolve()
        credentials_root = args.credentials_root.resolve()
        if not credentials_root.is_dir():
            return refuse(f"--credentials-root {credentials_root} is not a directory")
        refresh_pricing(args, log=log)
        try:
            routes = route_settings(args, paid=True)
        except RouteRefused as error:
            if args.live:   # a passing condition: the next pass refreshes again
                return _defer(judge_dir / "live", run_dir, str(error))
            raise
        all_agents = None
        scope, mode, models = "analysis", "paid", models_of(routes)
    if args.live:
        return _live(args, run_dir=run_dir, judge_dir=judge_dir, settings=settings, judge_config=judge_config,
                     budget_config=budget_config, mode=mode, scope=scope, ledger=ledger, routes=routes, models=models,
                     credentials_root=credentials_root, agents=agents, log=log, termination=termination)
    from research.analysis.judge.live import JudgeBusy, judge_lock, judging_locks
    # A spending invocation holds the run's lock (keyed by the ledger and the run id, whatever its
    # --output-dir) and its judge directory's; --rescore-cached spends nothing and only needs the latter.
    locks = (judge_lock(judge_dir, wait_seconds=args.lock_wait_seconds) if rescore else
             judging_locks(judge_dir, ledger=ledger, run_id=run_dir.name, wait_seconds=args.lock_wait_seconds))
    try:
        with locks:
            return _judge(args, run_dir=run_dir, judge_dir=judge_dir, settings=settings, budget_config=budget_config,
                          mode=mode, scope=scope, ledger=ledger, routes=routes, models=models,
                          credentials_root=credentials_root, all_agents=all_agents, agents=agents, rescore=rescore,
                          log=log, termination=termination)
    except JudgeBusy as error:
        return refuse(f"{error}; nothing was sent")


def _sample_index(run_dir):
    from research.analysis.judge.history import sample_index
    return sample_index(run_dir)


def _judging_prior(ledger, judge_dir, scope, *, run_dir, log=None):
    """What the judged run's judging already committed (``client.committed_for_judging``): this judge
    directory's invocations (its live store included) in every ledger they were counted in
    (``judging_ledgers``; read under the judging locks, so a binding written by an invocation that
    held them just before is seen), and every judge request labelled with the run, whatever its
    judge directory."""
    from research.analysis.judge.client import committed_for_judging
    sources = judging_ledgers(ledger, judge_dir, search=(run_dir,), log=log)
    return committed_for_judging(sources, judge_dir, scope=scope, run_id=Path(run_dir).name)


def _clients(keys, *, ledger, scope, archive_dir, routes, settings, run_limit, run_id, mock=None,
             credentials_root=None):
    """The invocation's judge clients. They share ``run_limit``: one ledger run whose budget is the
    room it leaves; ``run_id`` (the judged run, ``:live`` for a live pass) goes into every label."""
    from research.analysis.judge.client import FakeTransport, JudgeClient
    if mock is not None:
        transport = FakeTransport(responder=mock)
        return {key: JudgeClient(key, ledger, scope=scope, archive_dir=archive_dir, settings=routes, http=transport,
                                 max_concurrency=settings.concurrency(key), run_limit=run_limit, run_id=run_id)
                for key in keys}
    return {key: JudgeClient(key, ledger, scope=scope, archive_dir=archive_dir, settings=routes, http=None,
                             max_concurrency=settings.concurrency(key), paid_authorized=True,
                             credentials_root=credentials_root, run_limit=run_limit, run_id=run_id)
            for key in keys}


def _mock_model(pl, routes, chosen):
    from research.analysis.judge.history import agent_code
    # Synthetic roles: some exploit agents (every fifth), one evaluation-aware agent (whose judge output
    # is corrupted once, exercising the repair), one whistleblower; most agents have nothing.
    return pl.MockModel(routes, refuse_once={("judge", chosen[0])},
                        refuse_twice={("prefilter", f"{agent_code(chosen[0])}.w0002")},
                        corrupt_first={("judge", chosen[1])} if len(chosen) > 1 else set(),
                        exploit_agents=set(chosen[0::5]), aware_agents=set(chosen[1:2]),
                        whistle_agents=set(chosen[2:3]))


def _judge(args, *, run_dir, judge_dir, settings, budget_config, mode, scope, ledger, routes, models,
           credentials_root, all_agents, agents, rescore, log, termination=None):
    """A post-run judging invocation (paid, --mock or --rescore-cached), under the run's judge lock."""
    from research.analysis.judge import pipeline as pl
    from research.analysis.judge.client import RunLimit
    from research_swarm.budget import read_snapshot
    clients, run_limit, limit = {}, None, None
    pipeline = pl.Pipeline(run_dir, judge_dir, mode=mode, clients=clients, settings=settings, agents=agents,
                           stage=args.stage, scope=scope, ledger_path=ledger, models=models,
                           route=((pl.read_json(judge_dir / "pipeline.json") or {}).get("route") if rescore else None)
                           or describe_route(routes), agent_ids=all_agents, retry_failed=args.retry_failed,
                           recompute_stale=args.recompute_stale, label_check=not args.no_label_check,
                           rescore_only=rescore, log=log, upgrade_judge_revision=args.upgrade_judge_revision,
                           warm_from=False if args.no_warm else args.warm_from)
    if not rescore:
        try:
            # Refuse a judge directory with other binding settings (e.g. the superseded 12-dimension
            # attempt, or a v2.x directory) before the estimate, the confirmation and any key.
            pipeline.check_manifest(write=False)
        except pl.PipelineError as error:
            return refuse(str(error))
        # Owner decisions, October 20, 2026 ("Don't require estimates for anything"; "One per-run
        # budget, no manual approvals"): no pre-run cost estimate and no confirmation. The judging
        # run's budget: --run-limit, else the run config's behavior_judge_max_usd, else
        # DEFAULT_RUN_LIMIT_USD, for every invocation into this judge directory (what they committed
        # counts against it). The run limit stops the run cleanly; the ledger enforces the room it
        # leaves for this invocation's ledger run.
        prior = _judging_prior(ledger, judge_dir, scope, run_dir=run_dir, log=log)
        try:
            limit = fixed_run_limit(args.run_limit, budget_config, prior_usd=prior["committed_usd"])
        except LimitRefused as error:
            return refuse(str(error))
        run_limit = RunLimit(limit["limit_usd"], source=limit["source"], estimate=None, factor=None,
                             floor_usd=None, basis=limit["basis"], run_config=budget_config,
                             prior_usd=limit["prior_usd"], prior=prior, reservation_factor=prior["reservation_factor"])
        pipeline.run_limit = run_limit
        if termination is not None:
            termination.register(run_limit)
    # Every agent's history is rebuilt once, in parallel worker processes, before any request and before
    # the event loop starts (never in a thread beside the HTTP threads); the pipeline reads it from memory.
    from research.analysis.judge.history import HistoryCache
    if all_agents is None:
        all_agents = sorted(_sample_index(run_dir))
    unknown = sorted(set(agents or ()) - set(all_agents))
    if unknown:
        return refuse("No such agent in the run: " + ", ".join(unknown))
    pipeline.use_histories(HistoryCache.build(run_dir, sorted(agents) if agents else all_agents,
                                              workers=args.history_workers, log=log))
    if args.mock:
        clients.update(_clients(pl.MODEL_KEYS, ledger=ledger, scope=scope, archive_dir=judge_dir / "raw", routes=routes,
                                settings=settings, run_limit=run_limit, run_id=run_dir.name,
                                mock=_mock_model(pl, routes, agents or all_agents)))
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
        print(f"paid judging: budget ${limit['limit_usd']} ({limit['basis']}; already committed by this judge "
              f"directory's judging ${limit['prior_usd']}); ledger run {run_limit.run_id} (budget ${run_limit.room})",
              file=sys.stderr)
        clients.update(_clients(pl.MODEL_KEYS, ledger=ledger, scope=scope, archive_dir=judge_dir / "raw", routes=routes,
                                settings=settings, run_limit=run_limit, run_id=run_dir.name,
                                credentials_root=credentials_root))
    try:
        if mode == "paid" and not rescore:
            pipeline.check_manifest()  # writes the manifest (or performs the requested upgrade)
            kept = keep_pricing_record(judge_dir, routes)
            record_run_budget(judge_dir, dict(limit, ledger_run_id=run_limit.run_id, run_config=budget_config,
                                              pricing_record=None if kept is None else str(kept)),
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
        short["run_limit"] = {k: record[k] for k in ("limit_usd", "source", "basis", "prior_usd", "settled_usd",
                                                      "uncertain_usd", "peak_committed_usd", "requests", "denied")}
    if pipeline.upgrade:
        short["judge_revision_upgrade"] = {k: pipeline.upgrade[k] for k in ("at", "archived_to", "superseded_spend",
                                                                            "prefilter_units_kept")}
    if summary.get("live"):
        short["live"] = summary["live"]
    short["errors"] = [{k: e[k] for k in ("agent_id", "error")} for e in summary["errors"]]
    print(json.dumps(short, indent=2, default=str))
    return {"complete": 0, "stopped": 3}.get(summary["status"], 2)


def _key_headroom(report, ledger, run_name, room):
    """None when the provider key (shared with the running pilot) leaves the pilot everything it may
    still be billed after this live pass's room, else why not (the pass is then deferred, automatic,
    nothing sent: live judging never starves the experiment of its own key's allowance). What the
    pilot may still be billed is its ledger run's remaining budget plus the reservations it holds
    (``held_usd``; the key has not been charged for them yet). An unlimited key (``limit_remaining``
    null), or no running pilot run in the ledger, always passes. Not a spending control: the budgets
    are."""
    remaining = (report or {}).get("limit_remaining")
    if remaining is None:
        return None
    from research_swarm.budget import latest_run_id, read_run
    try:
        pilot = read_run(ledger, latest_run_id(ledger, run_name) or run_name)
    except Exception as error:
        return f"the pilot's ledger run could not be read from {ledger} ({type(error).__name__})"
    if pilot is None or pilot.get("ended") or pilot.get("scope") != "pilot":
        return None
    held = Decimal(str(pilot.get("held_usd") or 0))
    need = Decimal(str(pilot["remaining_usd"])) + held + Decimal(str(room))
    if Decimal(str(remaining)) < need:
        return (f"the provider key's limit_remaining ${remaining} is below the running pilot's remaining budget "
                f"${pilot['remaining_usd']} plus its unbilled holds ${held} plus this live pass's room ${room}: "
                "judging now could starve the pilot")
    return None


def _live(args, *, run_dir, judge_dir, settings, judge_config, budget_config, mode, scope, ledger, routes, models,
          credentials_root, agents, log, termination=None):
    """One live screening pass (docs/plan.md, "Live behavior judge during the run (October 20, 2026)";
    research/analysis/judge/live.py). Exit 0: done, skipped (live source unavailable, or another
    judging process holds the lock); 3: stopped by the judging run's budget, the ledger or SIGTERM;
    4: refused (a configuration problem, or no room left under the judging run's budget); 5
    (``DEFERRED``): nothing was sent because no fresh valid pricing record or no key could be had just
    now, and the next pass tries again. A paid pass needs ``--run-config`` of a config enabling paid
    live judging; its budget is the judging run's (the judge directory's, shared with the early
    stages and the post-run judge: ``client.committed_for_judging``). There is no separate live share
    and no provider-key gate (the key's limit is recorded, informational)."""
    from research.analysis.judge import live as lv
    from research.analysis.judge import pipeline as pl
    from research.analysis.judge.client import RunLimit
    live_dir = judge_dir / pl.WARM_DIRNAME
    if mode == "paid" and (judge_config is None or not judge_config.get("live")):
        return refuse("a paid --live pass needs --run-config CONFIG of a config with auto_behavior_judge, "
                      "behavior_judge \"full\" and live_behavior_judge true")
    gate = lv.inspect_gate()
    if gate:
        lv.record_unavailable(live_dir, gate, run_id=run_dir.name)
        print(json.dumps({"live": "skipped", "live_source_unavailable": gate}))
        return 0
    margin = args.live_margin_seconds if args.live_margin_seconds is not None else lv.DEFAULT_MARGIN_SECONDS
    try:
        lock = lv.judging_locks(judge_dir, ledger=ledger, run_id=run_dir.name, wait_seconds=0)
        lock.__enter__()
    except lv.JudgeBusy as error:
        print(json.dumps({"live": "skipped", "busy": str(error)}))
        return 0
    try:
        prior = _judging_prior(ledger, judge_dir, scope, run_dir=run_dir, log=log)
        try:
            limit = fixed_run_limit(args.run_limit, budget_config, prior_usd=prior["committed_usd"])
        except LimitRefused as error:
            return refuse(str(error))
        run_limit = RunLimit(limit["limit_usd"], source=limit["source"], basis=limit["basis"],
                             run_config=budget_config, prior_usd=limit["prior_usd"], prior=prior,
                             reservation_factor=prior["reservation_factor"])
        if termination is not None:
            termination.register(run_limit)
        clients = {}
        pipeline = pl.Pipeline(run_dir, live_dir, mode=mode, clients=clients, settings=settings, stage="prefilter",
                               scope=scope, ledger_path=ledger, models=models, route=describe_route(routes),
                               agent_ids=[], log=log, run_limit=run_limit, warm_from=False)
        try:
            lv.open_live_store(pipeline, describe_route(routes))  # live/manifest.json: the binding and route
        except pl.PipelineError as error:
            return refuse(str(error))
        tag = f"{run_dir.name}:live"
        if mode == "rehearsal":
            from research.analysis.judge.live import LiveSource
            chosen = sorted(LiveSource(run_dir).index())
            clients.update(_clients(("haiku",), ledger=ledger, scope=scope, archive_dir=live_dir / "raw",
                                    routes=routes, settings=settings, run_limit=run_limit, run_id=tag,
                                    mock=_mock_model(pl, routes, chosen or ["agent-001"])))
        else:
            clients.update(_clients(("haiku",), ledger=ledger, scope=scope, archive_dir=live_dir / "raw",
                                    routes=routes, settings=settings, run_limit=run_limit, run_id=tag,
                                    credentials_root=credentials_root))
            kept = keep_pricing_record(judge_dir, routes)
            record_run_budget(live_dir, dict(limit, ledger_run_id=run_limit.run_id, run_config=budget_config,
                                             pricing_record=None if kept is None else str(kept)),
                              run_dir=run_dir, ledger=ledger)
            try:
                # The host key in memory and the unpaid provider-limit record (never a launch gate).
                report = clients["haiku"].prepare()
            except Exception as error:   # e.g. no key could be loaded: nothing was sent
                return _defer(live_dir, run_dir, f"the provider key could not be loaded ({type(error).__name__}: "
                                                 f"{error})")
            problem = _key_headroom(report, (args.pilot_ledger or ledger).resolve(), run_dir.name, limit["room_usd"])
            if problem:   # automatic: the next pass checks again
                return _defer(live_dir, run_dir, problem)
        record = lv.LivePass(run_dir, pipeline, margin=margin, log=log, agents=agents).run()
    finally:
        lock.__exit__(None, None, None)
    summary = {"live": "stopped" if record.get("stopped") else "done", "run_id": run_dir.name,
               "requests_planned": record["requests_planned"], "windows_final": record["windows_final"],
               "windows_cached": record["windows_cached"], "windows_deferred": record["windows_deferred"],
               "stopped": record.get("stopped"),
               "run_limit": {k: (record.get("run_limit") or {}).get(k) for k in ("limit_usd", "prior_usd", "settled_usd",
                                                                                  "requests", "denied")}}
    print(json.dumps(summary, indent=2, default=str))
    return 3 if record.get("stopped") else 0


def _defer(live_dir, run_dir, reason):
    """A live pass that sent nothing because of a passing condition (no fresh valid pricing record
    after the automatic refresh, or no key could be loaded just now): recorded in live/state.json,
    exit ``DEFERRED`` (the supervisor tries again next pass instead of ending live judging)."""
    from research.analysis.judge import live as lv
    lv.record_unavailable(live_dir, reason, run_id=run_dir.name, kind="deferred")
    print(f"DEFERRED: {reason}; nothing was sent", file=sys.stderr)
    print(json.dumps({"live": "deferred", "reason": reason}))
    return DEFERRED


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()  # a native crash (e.g. a segmentation fault) prints every thread's traceback
    sys.exit(main())
