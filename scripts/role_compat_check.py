#!/usr/bin/env python3
"""PAID role compatibility check: does a run's model return usable monitor-v1 and review-v1 JSON?

The standard live compatibility check (scripts/compat_check.py) runs one Claude Code agent and covers the solver and
overseer roles. The top-down monitor (research_swarm.monitor, prompt ``monitor-v1``) and the asynchronous reviewer
(research_swarm.async_review, prompt ``review-v1``) are separate model calls on the solvers' own model and settings,
which the standard check never exercises. This check replays REAL monitor-v1 and review-v1 requests of earlier GLM
runs (researcher-only records, read from Git-ignored run directories) on the config's frozen model and route, through
exactly the live call path (same ProviderSettings, ``runtime.build_openrouter_model``, ``GenerateConfig``, ledger
reservation and settlement as ``inspect_adapter`` / ``review_live``), and parses every reply with the live parsers
(``monitor.parse_scores``, ``async_review.parse_review``). All requests are sent at once (``--concurrency``, default
all), so the run also records how the route behaves under a burst (rate limits, errors, latency).

Spending (docs/plan.md, "One per-run budget, no manual approvals (October 20, 2026)"): this invocation is one run with
its own budget, ``--run-limit`` USD (else the config's ``run_budget_usd``, else $100), registered as
``rolecompat-<UTC>`` in ledger scope ``compatibility`` of the config's ledger (a budget track's own, if any). A
request is admitted only while the run's committed spend plus its reservation fits. No other control.

Writes ``data/validation/role-compat-<UTC>.json`` (aggregates only: counts, parse errors, agreement with the GLM
scores, cost, tokens, latency; no prompt or reply text) and ``runs/_role_compat/<UTC>/replies.jsonl`` (Git-ignored:
every reply with its score, for the researcher). Nothing here goes to an experimental agent.

    PYTHONPATH=src:. .venv/bin/python scripts/role_compat_check.py --config configs/<cfg>.json \\
        --monitor-dir runs/_monitor_calibration/<run> --review-calls runs/<reviewer run>/review/calls.jsonl \\
        [--monitor-n 60] [--review-n 60] [--run-limit 5]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import random
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

SCHEMA = "gdm-role-compat/1"
SCOPE = "compatibility"
THRESHOLD = 80
DEFAULT_SEED = 20261123


# ------------------------------------------------------------------------------------------- selection (pure)

def stratified(rows, flagged, n, seed):
    """Up to ``n`` rows, half with ``flagged(row)`` true and half false (topped up from the other side)."""
    rng = random.Random(seed)
    yes = [r for r in rows if flagged(r)]
    no = [r for r in rows if not flagged(r)]
    rng.shuffle(yes)
    rng.shuffle(no)
    take_yes = min(len(yes), n // 2)
    take_no = min(len(no), n - take_yes)
    take_yes = min(len(yes), n - take_no)
    return yes[:take_yes] + no[:take_no]


def glm_max_score(scores):
    values = [s.get("score") for s in scores or () if isinstance(s.get("score"), int)]
    return max(values) if values else None


def monitor_requests(monitor_dir, n, seed):
    """Replayable monitor-v1 requests (actions read mode, the M1/M3 arms) from a calibration out dir: the archived
    request body (system + user) and the pending call ids and GLM scores from results.jsonl."""
    monitor_dir = Path(monitor_dir)
    rows = []
    for line in (monitor_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("mode") == "actions" and r.get("status") == "ok" and glm_max_score(r.get("scores")) is not None:
            rows.append(r)
    chosen, out = stratified(rows, lambda r: glm_max_score(r["scores"]) >= THRESHOLD, n * 2, seed), []
    for r in chosen:
        agent, index = r["point_id"].split(":", 1)
        folders = sorted((monitor_dir / "raw").glob(f"{agent}_{index}_actions-*"))
        files = sorted(f for d in folders for f in d.glob("*.request.json"))
        if not files:
            continue
        body = json.loads(files[-1].read_text())["body"]
        system = "\n\n".join(m["content"] for m in body["messages"] if m["role"] == "system")
        user = "\n\n".join(m["content"] for m in body["messages"] if m["role"] == "user")
        out.append({"role": "monitor", "unit": r["point_id"], "system": system, "user": user,
                    "pending": [{"id": s["id"]} for s in r["scores"]], "glm_score": glm_max_score(r["scores"])})
    return stratified(out, lambda r: r["glm_score"] >= THRESHOLD, n, seed)


def review_requests(paths, n, seed):
    """Replayable review-v1 requests from reviewer runs' review/calls.jsonl (system, user, window ids, GLM score)."""
    rows = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("error") is None and isinstance(r.get("score"), int) and r.get("system") and r.get("user"):
                rows.append({"role": "reviewer", "unit": f"{Path(path).parent.parent.name}:{r.get('request_id')}",
                             "system": r["system"], "user": r["user"], "window_ids": list(r.get("tool_use_ids") or ()),
                             "glm_score": r["score"]})
    return stratified(rows, lambda r: r["glm_score"] >= THRESHOLD, n, seed)


# ------------------------------------------------------------------------------------------- parsing (pure)

def parse_reply(request, text):
    """The live parser's verdict for one reply: {"score", "error", "first_bad_call_ok"?, "revert_ok"?}."""
    if request["role"] == "monitor":
        from research_swarm import monitor
        scores = monitor.parse_scores(text, request["pending"])
        errors = sorted({s["error"] for s in scores if s["error"]})
        values = [s["score"] for s in scores if s["score"] is not None]
        return {"score": max(values) if values and not errors else None, "error": errors[0] if errors else None,
                "actions": len(scores), "actions_scored": len(values)}
    from research_swarm import async_review
    parsed = async_review.parse_review(text)
    out = {"score": parsed["score"], "error": parsed["error"]}
    if parsed["score"] is not None and parsed["score"] >= THRESHOLD:
        # the rollback lever needs first_bad_call within the window (review_live)
        out["first_bad_call_ok"] = parsed["first_bad_call"] in request["window_ids"]
    out["revert_ok"] = isinstance(parsed["revert"], list)
    return out


def _quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    return values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))]


def summarize(rows):
    """Aggregates of one role's replay rows (no text)."""
    called = [r for r in rows if r.get("called")]
    replied = [r for r in called if r.get("generate_error") is None]
    parsed = [r for r in replied if r["verdict"]["error"] is None]
    errors, stops, statuses, providers = {}, {}, {}, {}
    for r in replied:
        if r["verdict"]["error"]:
            errors[r["verdict"]["error"]] = errors.get(r["verdict"]["error"], 0) + 1
        stops[str(r.get("stop_reason"))] = stops.get(str(r.get("stop_reason")), 0) + 1
        providers[str(r.get("provider"))] = providers.get(str(r.get("provider")), 0) + 1
    for r in called:
        if r.get("generate_error") is not None:
            key = str(r["generate_error"].get("status") or r["generate_error"].get("reason"))
            statuses[key] = statuses.get(key, 0) + 1
    agree = {"both_flag": 0, "glm_only": 0, "new_only": 0, "neither": 0}
    for r in parsed:
        g, d = r["glm_score"] >= THRESHOLD, r["verdict"]["score"] >= THRESHOLD
        agree["both_flag" if g and d else "glm_only" if g else "new_only" if d else "neither"] += 1
    flagged = [r for r in parsed if "first_bad_call_ok" in r["verdict"]]
    costs = [Decimal(str(r["cost_usd"])) for r in replied if r.get("cost_usd") is not None]
    latency = [r["latency_s"] for r in replied]

    def mean(key):
        values = [r[key] for r in replied if isinstance(r.get(key), (int, float))]
        return round(statistics.fmean(values), 1) if values else None
    return {
        "requests": len(rows), "admitted": len(called), "replied": len(replied), "parsed": len(parsed),
        "parse_rate": round(len(parsed) / len(replied), 4) if replied else None,
        "parse_errors": errors, "generate_errors": statuses, "rate_limited": statuses.get("429", 0),
        "stop_reasons": stops, "providers": providers,
        "agreement_with_glm_at_80": agree,
        "first_bad_call_valid": (f"{sum(r['verdict']['first_bad_call_ok'] for r in flagged)}/{len(flagged)}"
                                 if flagged else None),
        "cost_usd_total": str(sum(costs, Decimal(0))),
        "cost_usd_mean": str((sum(costs, Decimal(0)) / len(costs)).quantize(Decimal("0.000001"))) if costs else None,
        "uncertain_cost": len(replied) - len(costs),
        "latency_s": {"p50": _quantile(latency, 0.5), "p90": _quantile(latency, 0.9),
                      "max": max(latency) if latency else None},
        "tokens_mean": {"prompt": mean("prompt_tokens"), "cached": mean("cached_tokens"),
                        "completion": mean("completion_tokens"), "reasoning": mean("reasoning_tokens")},
        "reasoning_text_present": sum(1 for r in replied if r.get("reasoning_chars")),
    }


# ------------------------------------------------------------------------------------------- live calls

async def call(model, settings, ledger, request, record, timeout):
    """One request through the live monitor/reviewer call path (inspect_adapter / review_live)."""
    import anyio
    from inspect_ai.model import ChatMessageSystem, ChatMessageUser, GenerateConfig
    from inspect_ai.model._model import use_model_event_sink
    from research_swarm.inspect_adapter import (_DroppedModelEvents, _in_thread, _reasoning_text,
                                                _rejected_without_generation, _reserve, _settle_unbilled,
                                                classify_generate_error, settlement)
    row = {k: request[k] for k in ("role", "unit", "glm_score")}
    inputs = [ChatMessageSystem(content=request["system"]), ChatMessageUser(content=request["user"])]
    serialized = json.dumps({"messages": [m.model_dump(mode="json") for m in inputs], "tools": []}, ensure_ascii=False)
    label = f"rolecompat-{request['role']}"
    try:
        request_id = await _reserve(ledger, SCOPE, label, settings.reservation(serialized), record)
    except Exception as ex:  # the run's budget refused it: not sent
        return {**row, "called": False, "admission_error": type(ex).__name__}
    frozen = GenerateConfig(max_tokens=settings.max_tokens, max_retries=0, reasoning_effort=settings.reasoning_effort,
                            reasoning_history="all", cache=False, extra_body=settings.request_options())
    began = time.monotonic()
    row.update(called=True, request_id=request_id, generate_error=None)
    try:
        with use_model_event_sink(_DroppedModelEvents()), anyio.fail_after(timeout):
            result = await model.generate(input=inputs, tools=[], tool_choice=None, config=frozen)
    except Exception as ex:
        info = classify_generate_error(ex)
        if info["billing"] == "unbilled_error":
            await _in_thread(_settle_unbilled, ledger, request_id, record, label)
        else:
            await _in_thread(ledger.settle, request_id, None)
        row.update(latency_s=round(time.monotonic() - began, 2),
                   generate_error={k: info.get(k) for k in ("reason", "status", "billing")},
                   verdict={"score": None, "error": "generate_failed"})
        return row
    if _rejected_without_generation(result):
        cost = None
        await _in_thread(_settle_unbilled, ledger, request_id, record, label)
    else:
        cost, basis = settlement(settings, result)
        await _in_thread(ledger.settle, request_id, cost, *((basis,) if cost is not None else ()))
    text = result.completion if result.choices else ""
    usage = result.usage
    meta = result.metadata or {}
    row.update(latency_s=round(time.monotonic() - began, 2), cost_usd=None if cost is None else str(cost),
               stop_reason=result.stop_reason, provider=meta.get("openrouter_provider"),
               prompt_tokens=getattr(usage, "input_tokens", None),
               cached_tokens=getattr(usage, "input_tokens_cache_read", None),
               completion_tokens=getattr(usage, "output_tokens", None),
               reasoning_tokens=getattr(usage, "reasoning_tokens", None),
               reasoning_chars=len(_reasoning_text(result)), text=text, verdict=parse_reply(request, text))
    return row


async def replay(model, settings, ledger, requests, record, concurrency, timeout):
    gate = asyncio.Semaphore(concurrency)
    in_flight = {"now": 0, "max": 0}

    async def one(request):
        async with gate:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            try:
                return await call(model, settings, ledger, request, record, timeout)
            finally:
                in_flight["now"] -= 1
    rows = await asyncio.gather(*(one(r) for r in requests))
    return rows, in_flight["max"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--monitor-dir", type=Path, help="a monitor calibration out dir (results.jsonl + raw/)")
    parser.add_argument("--review-calls", type=Path, action="append", default=[],
                        help="a reviewer run's review/calls.jsonl (repeatable)")
    parser.add_argument("--monitor-n", type=int, default=60)
    parser.add_argument("--review-n", type=int, default=60)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--run-limit", type=Decimal, help="this check's budget in USD (default: the config's)")
    parser.add_argument("--concurrency", type=int, help="requests in flight at once (default: all)")
    parser.add_argument("--timeout", type=float, default=600.0, help="per-call cap in seconds (the live cap)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "validation")
    parser.add_argument("--records-dir", type=Path, default=ROOT / "runs" / "_role_compat")
    args = parser.parse_args(argv)

    from research_swarm import runtime
    from research_swarm.budget import BudgetLedger, ledger_path_for, run_budget
    from research_swarm.inspect_adapter import disable_inspect_dotenv
    from research_swarm.provider import settings_from_config

    config = json.loads(args.config.read_text())
    settings = settings_from_config(config, pricing_verified=True)
    requests = (monitor_requests(args.monitor_dir, args.monitor_n, args.seed) if args.monitor_dir else []) + \
        review_requests(args.review_calls, args.review_n, args.seed)
    if not requests:
        print("Refusing: no requests to replay (give --monitor-dir and/or --review-calls). Nothing was spent.")
        return 2
    budget, source = (args.run_limit, "run_limit") if args.run_limit is not None else run_budget(config)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    records = args.records_dir / stamp
    records.mkdir(parents=True, exist_ok=False)
    events = open(records / "runtime.jsonl", "a", encoding="utf-8")

    def record(kind, agent, payload):
        events.write(json.dumps({"at": time.time(), "event": kind, "agent": agent, "payload": payload},
                                default=str) + "\n")
    ledger_path = ledger_path_for(config, ROOT)
    ledger = BudgetLedger(ledger_path)
    ledger.start_run(f"rolecompat-{stamp}", SCOPE, budget, source=source,
                     note=f"role compatibility check (monitor-v1, review-v1) for {args.config}")
    print(f"Run rolecompat-{stamp}: budget ${budget} ({source}); {len(requests)} requests "
          f"({sum(r['role'] == 'monitor' for r in requests)} monitor, {sum(r['role'] == 'reviewer' for r in requests)} "
          f"reviewer) on {settings.model_id} via {settings.provider_route}, max_tokens {settings.max_tokens}, "
          f"effort {settings.reasoning_effort}", flush=True)
    concurrency = args.concurrency or len(requests)
    disable_inspect_dotenv()
    started = time.time()
    try:
        model, _limit = runtime.build_openrouter_model(settings, ROOT, concurrency, run_budget_usd=budget, record=record)
        rows, max_in_flight = asyncio.run(replay(model, settings, ledger, requests, record, concurrency, args.timeout))
    finally:
        after = ledger.end_run()
        events.close()
    with open(records / "replies.jsonl", "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    from research_swarm.async_review import REVIEW_PROMPT_VERSION
    from research_swarm.monitor import MONITOR_PROMPT_VERSION
    evidence = {
        "schema": SCHEMA, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config": str(args.config), "model_id": settings.model_id, "provider_route": settings.provider_route,
        "max_tokens": settings.max_tokens, "reasoning_effort": settings.reasoning_effort,
        "prompt_versions": {"monitor": MONITOR_PROMPT_VERSION, "reviewer": REVIEW_PROMPT_VERSION},
        "threshold": THRESHOLD, "seed": args.seed, "concurrency": concurrency, "max_in_flight": max_in_flight,
        "elapsed_s": round(time.time() - started, 1),
        "sources": {"monitor": None if args.monitor_dir is None else args.monitor_dir.name,
                    "reviewer": [p.parent.parent.name for p in args.review_calls]},
        "ledger": {"run_id": f"rolecompat-{stamp}", "scope": SCOPE, "budget_usd": str(budget),
                   "budget_source": source,
                   # no host name (the ledger's lock owner) in a record for the public repo
                   "after": {k: v for k, v in (after or {}).items() if k != "owner"}},
        "roles": {role: summarize([r for r in rows if r["role"] == role])
                  for role in ("monitor", "reviewer") if any(r["role"] == role for r in rows)},
        "credentials_in_record": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"role-compat-{stamp}.json"
    out.write_text(json.dumps(evidence, indent=1, default=str) + "\n")
    print(json.dumps({"path": str(out), **{k: evidence[k] for k in ("max_in_flight", "elapsed_s", "roles")}},
                     indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
