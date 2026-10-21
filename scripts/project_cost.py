#!/usr/bin/env python3
"""PROJECTION (not a measurement) of pilot model spend against the run's budget.

Optional and never required (owner decision, October 20, 2026: no estimates are required for
any run). The budget compared with is ``run_budget_usd`` from ``--config``, else the $100 default
(docs/plan.md "One per-run budget, no manual approvals (October 20, 2026)").

Measured input: request sizes from an unpaid run directory (default: the run named by
the newest data/validation/runtime-e2e-*.json). Each ``inference_admitted`` event in
runtime.jsonl gives the UTF-8 size of the serialized request (payload ``request_bytes``
or, failing that, derived exactly from ``reserved_usd`` and the provider settings).
The first request of each agent is the per-request floor: Claude Code's built-in
system prompt + the appended conference prompt + tool schemas + the initial message.

Everything else is an explicit ASSUMPTION given on the command line: tokens per byte,
turns per agent per minute, output (reasoning + visible) and tool-result tokens per
turn, and the prefix-cache hit rate. Mock (mockllm) runs say nothing about DeepSeek's
real turn rate, reasoning length or caching.

The auto-compaction threshold is read, in this order, from ``--compaction-threshold``,
from ``compaction_threshold_tokens`` recorded in the runtime-e2e evidence
(``--evidence``, else the newest data/validation/runtime-e2e-*.json; the top-level key,
else the same key inside one of its checks), and otherwise falls back to the
ASSUMPTION ``DEFAULT_COMPACTION_THRESHOLD`` (160,000 prompt tokens). The output's
``assumption_sources.compaction_threshold`` names which one was used.

The agent count is ``--agents``, else ``agent_count`` from ``--config`` (default
configs/pilot.json), else ``runtime.APPROVED_AGENT_COUNT`` (``assumption_sources.agents``).

Model per agent: request k has prompt c_k; c_{k+1} = c_k + output + tool_result (the
pinned reasoning_history="all" replays reasoning); above the compaction threshold one
summary request is added and the context restarts at floor + summary. Without caching
every prompt token is billed at the maximum route prompt price; with caching the
previous request's prompt (a stable prefix) is billed at the cache-read price times
the hit rate. Settlement note: if OpenRouter's usage.cost is absent the ledger settles
at the token-priced upper bound, i.e. the no-caching column, even if the bill is lower.

    .venv/bin/python scripts/project_cost.py [--run-dir RUN | --evidence FILE] [--config FILE] [--rates 0.5,1,2]
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import run_budget  # noqa: E402
from research_swarm.launch_gates import newest_evidence  # noqa: E402
from research_swarm.provider import FRAMING_TOKEN_ALLOWANCE, ProviderSettings  # noqa: E402

DEFAULT_CACHE_READ = "0.000000006"  # maximum deepseek-route cache-read price, 2026-10-13 freeze
DEFAULT_CONFIG = ROOT / "configs" / "pilot.json"
# ASSUMPTION used only when neither the command line nor runtime-e2e evidence gives
# the threshold (scripts/validate_runtime.py records compaction_threshold_tokens).
DEFAULT_COMPACTION_THRESHOLD = 160_000
THRESHOLD_KEY = "compaction_threshold_tokens"


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def evidence_compaction_threshold(record):
    """``compaction_threshold_tokens`` from a runtime-e2e record (top level, else inside a check), or None."""
    if not isinstance(record, dict):
        return None
    if _positive_int(record.get(THRESHOLD_KEY)):
        return record[THRESHOLD_KEY]
    checks = record.get("checks") if isinstance(record.get("checks"), dict) else {}
    found = sorted({c[THRESHOLD_KEY] for c in checks.values() if isinstance(c, dict) and _positive_int(c.get(THRESHOLD_KEY))})
    return found[0] if len(found) == 1 else None  # conflicting values: not established


def resolve_compaction_threshold(explicit, evidence_path):
    """(threshold tokens, source): the flag, else the runtime-e2e evidence, else the documented assumption."""
    if explicit is not None:
        return explicit, "command line (--compaction-threshold)"
    if evidence_path is not None:
        value = evidence_compaction_threshold(json.loads(Path(evidence_path).read_text()))
        if value is not None:
            return value, f"runtime-e2e evidence {Path(evidence_path).name} ({THRESHOLD_KEY})"
    return DEFAULT_COMPACTION_THRESHOLD, (f"ASSUMPTION: default {DEFAULT_COMPACTION_THRESHOLD} (no runtime-e2e evidence "
                                          f"records {THRESHOLD_KEY}; verify live)")


def resolve_budget(config_path):
    """(run budget, source): the config's run_budget_usd, else the $100 default."""
    path = Path(config_path) if config_path else None
    config = json.loads(path.read_text()) if path is not None and path.is_file() else {}
    amount, source = run_budget(config)
    return amount, (f"{path.name} {source[len('config '):]}" if source != "default" else "default $100")


def resolve_agents(explicit, config_path):
    """(agent count, source): the flag, else the config's agent_count, else runtime.APPROVED_AGENT_COUNT."""
    if explicit is not None:
        return explicit, "command line (--agents)"
    path = Path(config_path) if config_path else None
    if path is not None and path.is_file():
        value = json.loads(path.read_text()).get("agent_count")
        if _positive_int(value):
            return value, f"{path.name} agent_count"
    from research_swarm import runtime
    return runtime.APPROVED_AGENT_COUNT, "runtime.APPROVED_AGENT_COUNT"


def request_sizes(run_dir, settings):
    """agent -> [request bytes in order] from runtime.jsonl inference_admitted events."""
    sizes = defaultdict(list)
    path = Path(run_dir) / "runtime.jsonl"
    prompt, output = Decimal(settings.prompt_usd_per_token), Decimal(settings.output_usd_per_token)
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("kind") != "inference_admitted":
            continue
        payload = event.get("payload") or {}
        if payload.get("request_bytes") is not None:
            size = int(payload["request_bytes"])
        else:
            bound = (Decimal(payload["reserved_usd"]) - settings.max_tokens * output) / prompt
            if bound >= settings.context_length:
                continue  # capped bound: size unknown
            size = int(bound.to_integral_value()) - FRAMING_TOKEN_ALLOWANCE
        sizes[event.get("agent_id")].append(size)
    return dict(sizes)


def measured_paid_usage(run_dir):
    """Summary of real provider usage, if this run made paid calls (else None)."""
    rows = []
    for line in (Path(run_dir) / "runtime.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        usage = (event.get("payload") or {}).get("provider_usage") if event.get("kind") == "inference_finished" else None
        if isinstance(usage, dict) and usage.get("cost") is not None:
            rows.append(usage)
    if not rows:
        return None
    pick = lambda k: [r.get(k) for r in rows if isinstance(r.get(k), (int, float))]  # noqa: E731
    return {"requests": len(rows), "total_cost_usd": sum(pick("cost")),
            "mean_prompt_tokens": statistics.mean(pick("prompt_tokens") or [0]),
            "mean_completion_tokens": statistics.mean(pick("completion_tokens") or [0])}


def route_prices(validation_dir):
    """Maximum route prices from the newest valid freeze, else the ProviderSettings bounds."""
    path, record = newest_evidence(validation_dir, "route-pricing")
    settings = ProviderSettings()
    if not (record and record.get("valid")):
        record, path = {}, None
    frozen = record.get("settings") or {}
    return (Decimal(record.get("max_prompt_usd_per_token") or frozen.get("prompt_usd_per_token")
                    or settings.prompt_usd_per_token),
            Decimal(record.get("max_completion_usd_per_token") or frozen.get("output_usd_per_token")
                    or settings.output_usd_per_token),
            Decimal(record.get("max_cache_read_usd_per_token") or DEFAULT_CACHE_READ), str(path) if path else None)


def simulate(*, floor, growth, output, threshold, summary, turns, prices, hit_rate):
    """Per-agent cumulative cost after each request: (no_cache[], cache[], compactions)."""
    prompt, completion, cache_read = prices
    context, previous = floor, 0
    no_cache, cached_cost, compactions, cum_n, cum_c = [], [], 0, Decimal(0), Decimal(0)

    def bill(context, previous, out):
        cached = Decimal(min(previous, context)) * Decimal(str(hit_rate))
        return (Decimal(context) * prompt + out * completion,
                cached * cache_read + (Decimal(context) - cached) * prompt + out * completion)

    for _ in range(turns):
        n, c = bill(context, previous, output)
        cum_n, cum_c = cum_n + n, cum_c + c
        no_cache.append(cum_n)
        cached_cost.append(cum_c)
        previous, context = context, context + growth
        if context > threshold:  # auto-compaction: one summary request, then a fresh context
            n, c = bill(context, previous, summary)
            cum_n, cum_c = cum_n + n, cum_c + c
            no_cache[-1], cached_cost[-1] = cum_n, cum_c
            compactions += 1
            context, previous = floor + summary, floor
    return no_cache, cached_cost, compactions


def project(sizes, args, prices):
    firsts = [s[0] for s in sizes.values() if s]
    if not firsts:
        raise SystemExit("No inference_admitted events with usable sizes in the run directory")
    floor = int(max(firsts) / args.bytes_per_token)
    deltas = [b - a for s in sizes.values() for a, b in zip(s, s[1:]) if b > a]
    growth = args.output_tokens + args.tool_result_tokens
    cap = Decimal(str(args.run_budget))
    scenarios = []
    for rate in args.rates:
        turns = max(1, round(rate * args.minutes))
        no_cache, cache, compactions = simulate(floor=floor, growth=growth, output=args.output_tokens,
                                                threshold=args.compaction_threshold, summary=args.summary_tokens,
                                                turns=turns, prices=prices, hit_rate=args.cache_hit_rate)
        row = {"turns_per_agent_per_minute": rate, "turns_per_agent": turns, "compactions_per_agent": compactions}
        for label, series in (("without_caching", no_cache), ("with_caching", cache)):
            total = series[-1] * args.agents
            exhausted = next((i for i, value in enumerate(series) if value * args.agents >= cap), None)
            row[label] = {"projected_total_usd": f"{total:.2f}", "exceeds_run_budget": total > cap,
                          "minutes_until_budget": None if exhausted is None else round((exhausted + 1) / rate, 1)}
        scenarios.append(row)
    settings = ProviderSettings(pricing_verified=True)
    hold = lambda tokens: settings.reservation("x" * int(tokens * args.bytes_per_token))  # noqa: E731
    return {
        "measured": {"agents_with_requests": len(sizes), "requests": sum(len(s) for s in sizes.values()),
                     "first_request_bytes": {"min": min(firsts), "max": max(firsts)},
                     "positive_request_growth_bytes_median": statistics.median(deltas) if deltas else None,
                     "note": "Sizes are serialized Inspect requests (JSON), an over-estimate of prompt text."},
        "derived": {"floor_prompt_tokens": floor, "context_growth_tokens_per_turn": growth},
        "ledger_holds": {"reservation_at_floor_usd": f"{hold(floor):.4f}",
                         "reservation_at_compaction_threshold_usd": f"{hold(args.compaction_threshold):.4f}",
                         "note": f"Up to {args.agents} such holds are in flight at once; admission stops when the "
                                 "run's remaining budget cannot cover the next one."},
        "scenarios": scenarios,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--run-dir", type=Path)
    source.add_argument("--evidence", type=Path, help="runtime-e2e evidence naming a run_dir")
    parser.add_argument("--validation-dir", type=Path, default=ROOT / "data" / "validation")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="run config supplying agent_count and run_budget_usd (default: configs/pilot.json)")
    parser.add_argument("--agents", type=int, default=None, help="default: agent_count from --config")
    parser.add_argument("--minutes", type=float, default=90)
    parser.add_argument("--rates", type=lambda s: [float(x) for x in s.split(",")], default=[0.5, 1.0, 2.0],
                        help="ASSUMPTION: turns (model requests) per agent per minute")
    parser.add_argument("--bytes-per-token", type=float, default=3.5, help="ASSUMPTION (1.0 = strict upper bound)")
    parser.add_argument("--output-tokens", type=int, default=2000, help="ASSUMPTION: reasoning+visible per turn")
    parser.add_argument("--tool-result-tokens", type=int, default=1000, help="ASSUMPTION: tool output per turn")
    parser.add_argument("--compaction-threshold", type=int, default=None,
                        help="prompt tokens at which Claude Code auto-compacts; default: compaction_threshold_tokens "
                             f"from the runtime-e2e evidence, else the ASSUMPTION {DEFAULT_COMPACTION_THRESHOLD}")
    parser.add_argument("--summary-tokens", type=int, default=6000, help="ASSUMPTION: compaction summary size")
    parser.add_argument("--cache-hit-rate", type=float, default=0.9, help="ASSUMPTION: cached share of the prefix")
    parser.add_argument("--output", type=Path, help="also write the JSON projection here (never data/validation)")
    args = parser.parse_args(argv)
    evidence = args.evidence or newest_evidence(args.validation_dir, "runtime-e2e")[0]
    if args.run_dir is None:
        if evidence is None:
            parser.error("no runtime-e2e evidence; pass --run-dir")
        run_dir = json.loads(Path(evidence).read_text())["run_dir"]
        args.run_dir = Path(run_dir) if Path(run_dir).is_absolute() else ROOT / run_dir
    args.agents, agents_source = resolve_agents(args.agents, args.config)
    args.run_budget, budget_source = resolve_budget(args.config)
    args.compaction_threshold, threshold_source = resolve_compaction_threshold(args.compaction_threshold, evidence)
    prompt, completion, cache_read, pricing_path = route_prices(args.validation_dir)
    result = {"kind": "cost_projection", "is_projection": True,
              "warning": "PROJECTION from unpaid request sizes and stated assumptions; not measured spend.",
              "run_dir": str(args.run_dir), "run_budget_usd": str(args.run_budget), "run_budget_source": budget_source,
              "prices_usd_per_token": {"prompt_max": format(prompt, "f"), "completion_max": format(completion, "f"),
                                       "cache_read_max": format(cache_read, "f"),
                                       "source": pricing_path or "ProviderSettings"},
              "assumptions": {k: getattr(args, k) for k in ("agents", "minutes", "rates", "bytes_per_token", "output_tokens",
                                                             "tool_result_tokens", "compaction_threshold",
                                                             "summary_tokens", "cache_hit_rate")},
              "assumption_sources": {"agents": agents_source, "compaction_threshold": threshold_source},
              **project(request_sizes(args.run_dir, ProviderSettings()), args, (prompt, completion, cache_read)),
              "measured_paid_usage": measured_paid_usage(args.run_dir)}
    text = json.dumps(result, indent=2, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    for row in result["scenarios"]:
        print(f"PROJECTION {row['turns_per_agent_per_minute']} turns/agent/min: "
              f"no caching ${row['without_caching']['projected_total_usd']} "
              f"(budget reached at {row['without_caching']['minutes_until_budget']} min), "
              f"with caching ${row['with_caching']['projected_total_usd']} "
              f"(budget reached at {row['with_caching']['minutes_until_budget']} min); run budget ${args.run_budget}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
