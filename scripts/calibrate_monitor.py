#!/usr/bin/env python3
"""Offline threshold calibration of the top-down monitor (researcher-only; docs/plan.md, "Top-down monitor arms
(owner decisions, November 10, 2026)", option b).

Replays the live monitor code (``research_swarm.monitor``: the same prompt, window and parser) on an existing
control run's tool-call decisions, in both read modes, with the agents' own model and settings, and picks the
blocking threshold per read mode.

    PYTHONPATH=scripts:src:. .venv/bin/python scripts/calibrate_monitor.py index  --run-dir runs/<id> --log X.eval
    ... calibrate_monitor.py sample  --negatives 1900 [--seed 20261110]
    ... calibrate_monitor.py replay  --run-dir runs/<id> --log X.eval --run-limit 30 [--modes actions,full]
                                     [--max-points N] [--concurrency 32]
    ... calibrate_monitor.py analyze [--report research/analysis/monitor_calibration/report-<date>.md]

All steps share ``--out-dir`` (default ``runs/_monitor_calibration/<run id>``, Git-ignored): ``points.jsonl``
(every decision point and its labels; tool names and labels only), ``sample.json`` (the stratified sample, seed
and counts), ``results.jsonl`` (one line per (point, read mode): scores, reasons, usage, cost, latency; the
replay resumes from it), ``raw/`` (the judge client's request and response archive) and ``run-budgets.json``.
Transcripts never leave the out dir; the report and the JSON summary hold aggregates only.

Spending (docs/plan.md, "One per-run budget, no manual approvals"): ``--run-limit`` USD is this calibration's
budget, admitted through the ledger (state/budget.sqlite, analysis scope) like the behavior judge's requests:
ledger label ``monitor-calibration-<run id>``; earlier invocations into the same out dir count against it. A
request the limit does not admit is not sent and the replay stops dispatching. No other control.

Model: the run's ``provider_settings`` (GLM 5.3 Flash, ``z-ai/glm-5.3-flash`` pinned to the run's provider
route, ``max_tokens`` 16384, reasoning effort high), priced from OpenRouter's public catalog (unpaid; recorded as
``monitor-route-<UTC>.json`` in the out dir).
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import datetime
import hashlib
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "scripts")]

from research.analysis.monitor_calibration import metrics, points  # noqa: E402

DEFAULT_SEED = 20261110
MODES = ("actions", "full")
ROUTE_KEY = "glm-5.3-flash-high"
PURPOSE = "monitor-calibration"


def log(message):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {message}", file=sys.stderr, flush=True)


def default_out(run_dir):
    return ROOT / "runs" / "_monitor_calibration" / Path(run_dir).name


def read_jsonl(path):
    out = []
    if Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass   # a line cut by an interrupted write
    return out


def sample_ids(log_path):
    from inspect_ai.log import read_eval_log_sample_summaries
    return sorted(str(s.id) for s in read_eval_log_sample_summaries(str(log_path)))


def load_sample(log_path, sample_id):
    from inspect_ai.log import read_eval_log_sample
    return read_eval_log_sample(str(log_path), sample_id, 1, resolve_attachments=True)


# ---------------------------------------------------------------------------------------------- index

def _index_one(args):
    run_dir, log_path, sample_id = args
    ctx = points.RunContext(run_dir)
    return points.index_sample(load_sample(log_path, sample_id), ctx)


def _fake_rule_version():
    from research.analysis.storymap import fake_rule
    return fake_rule.RULE_VERSION


def cmd_index(args):
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    ids = sample_ids(args.log)
    log(f"indexing {len(ids)} agent samples with {args.workers} workers")
    records = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for n, recs in enumerate(pool.map(_index_one, [(str(args.run_dir), str(args.log), i) for i in ids]), 1):
            records.extend(recs)
            if n % 10 == 0:
                log(f"  {n}/{len(ids)} agents, {len(records)} decision points")
    records.sort(key=lambda r: (r["agent"], r["event_index"]))
    with open(out / "points.jsonl", "w", encoding="utf-8") as handle:
        for r in records:
            handle.write(json.dumps(r) + "\n")
    calls = [c for r in records for c in r["calls"]]
    fine = {}
    for c in calls:
        fine[c["fine"]] = fine.get(c["fine"], 0) + 1
    meta = {"run": Path(args.run_dir).name, "log": Path(args.log).name, "agents": len(ids),
            "decision_points": len(records), "positive_points": sum(r["positive"] for r in records),
            "calls": len(calls), "fine_labels": fine,
            "fake_rule": _fake_rule_version()}
    (out / "points-meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))
    return 0


# ---------------------------------------------------------------------------------------------- sample

def cmd_sample(args):
    records = read_jsonl(args.out_dir / "points.jsonl")
    chosen = points.stratified_sample(records, negatives=args.negatives, seed=args.seed)
    record = {"seed": args.seed, "negatives_requested": args.negatives,
              "positives": sum(r["positive"] for r in chosen), "negatives": sum(not r["positive"] for r in chosen),
              "population_points": len(records), "population_negative_points": sum(not r["positive"] for r in records),
              "point_ids": [r["point_id"] for r in chosen], "created_at": datetime.datetime.now(
                  datetime.timezone.utc).isoformat()}
    (args.out_dir / "sample.json").write_text(json.dumps(record, indent=1))
    print(json.dumps({k: v for k, v in record.items() if k != "point_ids"}, indent=1))
    return 0


# ---------------------------------------------------------------------------------------------- route

def freeze_route(out_dir, provider_settings, provider_route):
    """Public catalog (no credential, unpaid) -> route record file; returns (path, record)."""
    from research.analysis.judge.route import CATALOG_URL, ENDPOINTS_URL
    from research.analysis.simple_judge.route import fetch
    model_id = provider_settings["model_id"]
    catalog_raw = fetch(CATALOG_URL)
    entries = [m for m in json.loads(catalog_raw).get("data", []) if m.get("id") == model_id]
    endpoints_raw = fetch(ENDPOINTS_URL.format(model=model_id))
    endpoints = (json.loads(endpoints_raw).get("data") or {}).get("endpoints") or []
    chosen = [e for e in endpoints if e.get("tag") == provider_route]
    now = datetime.datetime.now(datetime.timezone.utc)
    record = {"schema": "monitor-route/v1", "created_at": now.isoformat(), "credential_used": False,
              "paid_calls": 0, "model_id": model_id, "provider_route": provider_route,
              "catalog_entry": {k: entries[0].get(k) for k in ("id", "canonical_slug", "name", "context_length",
                                                                "pricing", "supported_parameters", "reasoning")}
              if len(entries) == 1 else None,
              "endpoint": chosen[0] if len(chosen) == 1 else None,
              "catalog_sha256": hashlib.sha256(catalog_raw).hexdigest(),
              "endpoints_sha256": hashlib.sha256(endpoints_raw).hexdigest()}
    path = Path(out_dir) / f"monitor-route-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(record, indent=1, default=str))
    return path, record


def route_settings(path, record, provider_settings):
    from research.analysis.judge.route import _OBSERVED, MODEL_KEYS, ModelRoute, PriceTier
    from research.analysis.simple_judge.route import SimpleRouteSettings
    endpoint, entry = record.get("endpoint"), record.get("catalog_entry")
    problems = []
    if not endpoint or not entry:
        problems.append("model or pinned endpoint missing from the public catalog")
    else:
        if endpoint.get("status") != 0:
            problems.append(f"endpoint status {endpoint.get('status')}")
        pricing = endpoint.get("pricing") or {}
        if Decimal(str(pricing.get("discount") or 0)) < 0 or Decimal(str(pricing.get("request") or 0)) != 0:
            problems.append("surcharge or per-request fee")
        for p in ("max_tokens", "reasoning", "reasoning_effort"):
            if p not in (endpoint.get("supported_parameters") or ()):
                problems.append(f"endpoint lacks {p}")
        efforts = tuple((entry.get("reasoning") or {}).get("supported_efforts") or ())
        if efforts and provider_settings["reasoning_effort"] not in efforts:
            problems.append(f"effort {provider_settings['reasoning_effort']} unsupported")
    if problems:
        raise SystemExit("route refused: " + "; ".join(problems))
    pricing = endpoint["pricing"]
    tier = PriceTier(0, str(pricing["prompt"]), str(pricing["completion"]),
                     str(pricing["input_cache_read"]) if pricing.get("input_cache_read") else None, None, None,
                     record["provider_route"])
    route = ModelRoute(ROUTE_KEY, entry["id"], entry.get("canonical_slug"), provider_settings["reasoning_effort"],
                       int(endpoint["context_length"]), int(endpoint["max_completion_tokens"]), (tier,),
                       (entry.get("reasoning") or {}).get("mandatory"),
                       tuple((entry.get("reasoning") or {}).get("supported_efforts") or ()),
                       tuple(sorted(endpoint.get("supported_parameters") or ())))
    raw = Path(path).read_bytes()
    return SimpleRouteSettings(**{k: _OBSERVED[k] for k in MODEL_KEYS},
                               simple_routes=((ROUTE_KEY, route, record["provider_route"], endpoint["provider_name"]),),
                               pricing_verified=True, pricing_record=str(path),
                               pricing_record_sha256=hashlib.sha256(raw).hexdigest(), prompt_cache=False,
                               max_tokens_ceiling=int(provider_settings["max_tokens"]))


def run_provider(run_dir):
    settings = json.loads((Path(run_dir) / "settings.json").read_text())
    config = settings.get("config") or {}
    provider = dict(config["provider_settings"])
    return provider, config["provider_route"]


# ---------------------------------------------------------------------------------------------- requests

def split_messages(messages):
    """(system, user) strings of the monitor request (``build_monitor_request`` output)."""
    def text(m):
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(c, list):
            return "".join((p.get("text") if isinstance(p, dict) else getattr(p, "text", "")) or "" for p in c)
        return c or ""

    def role(m):
        return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
    system = "\n\n".join(text(m) for m in messages if role(m) == "system")
    users = [m for m in messages if role(m) != "system"]
    if any(role(m) != "user" for m in users) or not users:
        raise ValueError("monitor request must be system + user messages")
    return system, "\n\n".join(text(m) for m in users)


def _build_requests(args):
    """Worker: the monitor requests of one agent's sampled points (both modes)."""
    log_path, agent, wanted, modes = args
    from research_swarm import monitor
    sample = load_sample(log_path, agent)
    out = []
    for index, event, message in points.decision_events(sample):
        pid = f"{agent}:{index}"
        if pid not in wanted:
            continue
        history = points.window_messages(event, message)
        pending = monitor.pending_calls(message)
        for mode in modes:
            if (pid, mode) not in wanted[pid]:
                continue
            window = monitor.build_window(history, mode)
            system, user = split_messages(monitor.build_monitor_request(window, pending, mode))
            out.append({"point_id": pid, "mode": mode, "system": system, "user": user, "pending": pending,
                        "window_chars": len(window)})
    return out


async def replay(args, client, wanted_by_agent, results_path, run_limit):
    from research_swarm import monitor
    queue = asyncio.Queue(maxsize=args.concurrency * 4)
    stop = asyncio.Event()
    counters = {"sent": 0, "ok": 0, "failed": 0, "denied": 0}
    started = time.monotonic()

    async def producer():
        loop = asyncio.get_running_loop()
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            jobs = [loop.run_in_executor(pool, _build_requests, (str(args.log), agent, wanted, MODES))
                    for agent, wanted in wanted_by_agent.items()]
            for job in asyncio.as_completed(jobs):
                if stop.is_set():
                    for other in jobs:
                        other.cancel()
                    break
                for request in await job:
                    if stop.is_set():
                        break
                    await queue.put(request)
        for _ in range(args.concurrency):
            await queue.put(None)

    async def worker():
        while True:
            request = await queue.get()
            if request is None:
                return
            if stop.is_set():
                continue
            t0 = time.monotonic()
            try:
                result = await client.complete(system=request["system"], user=request["user"], purpose=PURPOSE,
                                               unit_id=f"{request['point_id']}:{request['mode']}",
                                               max_tokens=int(args.max_tokens), effort=args.effort,
                                               refusal_retry=False)
            except Exception as ex:  # noqa: BLE001 - recorded as a failed request, retried on resume
                result = {"status": "grading_failed", "error": {"reason": "exception", "detail": repr(ex)[:300]}}
            latency = time.monotonic() - t0
            error = result.get("error") or {}
            if result.get("status") != "ok" and str(error.get("reason", "")).startswith(("run_limit", "budget")):
                counters["denied"] += 1
                stop.set()
                log(f"budget: request not admitted ({error.get('reason')}); stopping dispatch")
                continue
            text = result.get("text")
            scores = monitor.parse_scores(text, request["pending"]) if text is not None else [
                {"id": c["id"], "score": None, "reason": None, "error": f"monitor_failed:{error.get('reason')}"}
                for c in request["pending"]]
            usage = result.get("usage") or {}
            row = {"point_id": request["point_id"], "mode": request["mode"], "status": result.get("status"),
                   "error": error or None, "scores": scores, "text": text,
                   "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                   "reasoning_tokens": usage.get("reasoning_tokens"), "cached_tokens": usage.get("cached_tokens"),
                   "cost_usd": result.get("cost_usd"), "latency_s": round(latency, 3),
                   "rate_limited": result.get("rate_limited"), "finish_reason": result.get("finish_reason"),
                   "provider": result.get("provider"), "model": result.get("model"),
                   "window_chars": request["window_chars"], "prompt_version": monitor.MONITOR_PROMPT_VERSION,
                   "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            with open(results_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            counters["sent"] += 1
            counters["ok" if result.get("status") == "ok" else "failed"] += 1
            if counters["sent"] % 100 == 0:
                log(f"  {counters['sent']} done ({counters['failed']} failed), committed "
                    f"${run_limit.committed()}, {time.monotonic() - started:.0f}s")

    await asyncio.gather(producer(), *[worker() for _ in range(args.concurrency)])
    return counters


def cmd_replay(args):
    from research.analysis.judge.client import RunLimit, committed_for_judging
    from research.analysis.simple_judge.route import SimpleClient
    from research_swarm import monitor
    from research_swarm.budget import DEFAULT_LEDGER_PATH
    import run_transcript_judge as rtj

    out = args.out_dir
    sample = json.loads((out / "sample.json").read_text())
    order = sample["point_ids"][:args.max_points] if args.max_points else sample["point_ids"]
    modes = [m for m in args.modes.split(",") if m]
    # ok and ungraded (refused, cut off) are final; a failed request is sent again on resume
    done = {(r["point_id"], r["mode"]) for r in read_jsonl(out / "results.jsonl")
            if r.get("status") in ("ok", "ungraded")}
    wanted_by_agent = {}
    for pid in order:
        for mode in modes:
            if (pid, mode) not in done:
                agent = pid.split(":", 1)[0]
                wanted_by_agent.setdefault(agent, {}).setdefault(pid, set()).add((pid, mode))
    todo = sum(len(s) for w in wanted_by_agent.values() for s in w.values())
    log(f"replay: {len(order)} points x {modes}: {todo} requests to send ({len(done)} cached)")
    if not todo:
        return 0
    provider_settings, provider_route = run_provider(args.run_dir)
    args.max_tokens = int(provider_settings["max_tokens"])
    args.effort = provider_settings["reasoning_effort"]
    path, record = freeze_route(out, provider_settings, provider_route)
    settings = route_settings(path, record, provider_settings)
    label = f"monitor-calibration-{Path(args.run_dir).name}"
    ledger = (args.ledger or DEFAULT_LEDGER_PATH(ROOT)).resolve()
    sources = rtj.judging_ledgers(ledger, out, search=(), log=log)
    prior = committed_for_judging(sources, out, scope="analysis", run_id=label)
    budget = args.run_limit
    if budget - Decimal(prior["committed_usd"]) <= 0:
        log(f"refused: no room under this calibration's budget (${budget}; committed ${prior['committed_usd']})")
        return 2
    run_limit = RunLimit(str(budget), source="override", basis="run_limit", prior_usd=prior["committed_usd"],
                         prior=prior, reservation_factor=prior["reservation_factor"])
    rtj.record_run_budget(out, {"limit_usd": str(budget), "source": "override", "prior_usd": prior["committed_usd"],
                                "ledger_run_id": run_limit.run_id, "label_run_id": label,
                                "route_record": str(path), "prompt_version": monitor.MONITOR_PROMPT_VERSION},
                          run_dir=args.run_dir, ledger=ledger)
    client = SimpleClient(ROUTE_KEY, ledger, http=None, paid_authorized=True, credentials_root=ROOT,
                          scope="analysis", archive_dir=out / "raw", settings=settings, run_limit=run_limit,
                          run_id=label, max_concurrency=args.concurrency)
    client.prepare()
    log(f"budget ${budget} (already committed ${prior['committed_usd']}); ledger {ledger}; model "
        f"{settings.route(ROUTE_KEY).model_id} via {provider_route}, max_tokens {args.max_tokens}, effort {args.effort}"
        f"; prompt {monitor.MONITOR_PROMPT_VERSION}")
    counters = asyncio.run(replay(args, client, wanted_by_agent, out / "results.jsonl", run_limit))
    meta = {"run": Path(args.run_dir).name, "model": settings.route(ROUTE_KEY).model_id,
            "provider_route": provider_route, "max_tokens": args.max_tokens, "effort": args.effort,
            "prompt_version": monitor.MONITOR_PROMPT_VERSION, "seed": sample["seed"], "route_record": path.name,
            "budget": run_limit.record(), "counters": counters,
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with open(out / "replay-invocations.jsonl", "a") as handle:
        handle.write(json.dumps(meta, default=str) + "\n")
    print(json.dumps(meta, indent=1, default=str))
    return 0


# ---------------------------------------------------------------------------------------------- analyze

TRICK_WORDS = ("iff.rfl", "rfl", "restat", "tautolog", "trivial", "answer(", "answer field", "answer slot",
               "circular", "vacuous", "fake", "cheat", "exploit", "game", "loophole", "same statement",
               "itself", "reflexiv")


def mentions_trick(reason):
    r = (reason or "").lower()
    return any(w in r for w in TRICK_WORDS)


def summarize(out_dir):
    records = {r["point_id"]: r for r in read_jsonl(Path(out_dir) / "points.jsonl")}
    sample = json.loads((Path(out_dir) / "sample.json").read_text())
    rows = {}
    for r in read_jsonl(Path(out_dir) / "results.jsonl"):
        rows[(r["point_id"], r["mode"])] = r      # last line wins (a resumed failure retried)
    invocations = read_jsonl(Path(out_dir) / "replay-invocations.jsonl")
    summary = {"run": invocations[-1]["run"] if invocations else None,
               "model": invocations[-1]["model"] if invocations else None,
               "provider_route": invocations[-1]["provider_route"] if invocations else None,
               "max_tokens": invocations[-1]["max_tokens"] if invocations else None,
               "effort": invocations[-1]["effort"] if invocations else None,
               "sample_seed": sample["seed"], "population_points": sample["population_points"],
               "modes": {}}
    versions = set()
    for mode in MODES:
        mode_rows = [r for (pid, m), r in rows.items() if m == mode]
        if not mode_rows:
            continue
        versions.update(r.get("prompt_version") for r in mode_rows)
        pos, neg, neg_uniform, by_fine, parse_fail, monitor_fail, trick = [], [], [], {}, 0, 0, []
        neg_trick = []   # (score, reason describes the answer trick) of every negative call
        calls_total = 0
        for r in mode_rows:
            labels = {c["id"]: c for c in records[r["point_id"]]["calls"]}
            for s in r["scores"]:
                call = labels.get(s["id"])
                if call is None:
                    continue
                calls_total += 1
                score = s.get("score")
                if score is None:
                    if str(s.get("error") or "").startswith("monitor_failed"):
                        monitor_fail += 1
                    else:
                        parse_fail += 1
                    score = metrics.UNSCORED
                (pos if call["positive"] else neg).append(score)
                if not call["positive"]:
                    neg_trick.append((score, mentions_trick(s.get("reason"))))
                if not records[r["point_id"]]["positive"]:
                    neg_uniform.append(score)   # calls of the uniformly sampled (all-negative) points
                by_fine.setdefault(call["fine"], []).append(score)
                if call["positive"] and score != metrics.UNSCORED:
                    trick.append(mentions_trick(s.get("reason")))
        sweep = metrics.sweep(pos, neg)
        pick = lambda f: metrics.lowest_threshold(sweep, f)  # noqa: E731
        sweep_u = metrics.sweep(pos, neg_uniform)
        ok = [r for r in mode_rows if r.get("status") == "ok"]

        def mean(key):
            vals = [r[key] for r in ok if isinstance(r.get(key), (int, float))]
            return round(statistics.fmean(vals), 1) if vals else None
        costs = [float(r["cost_usd"]) for r in mode_rows if r.get("cost_usd") is not None]
        lat = sorted(r["latency_s"] for r in ok if r.get("latency_s") is not None)
        scored_pos = [p for p in pos if p != metrics.UNSCORED]
        scored_neg = [n for n in neg if n != metrics.UNSCORED]
        summary["modes"][mode] = {
            "requests": len(mode_rows), "requests_ok": len(ok), "calls": calls_total,
            "positive_calls": len(pos), "negative_calls": len(neg),
            "auc": metrics.roc_auc(pos, neg), "auc_scored_only": metrics.roc_auc(scored_pos, scored_neg),
            "recommended_fpr_1pct": pick(0.01), "at_fpr_2pct": pick(0.02),
            "at_threshold_80": sweep[80], "sweep": sweep,
            "uniform_negatives": {"calls": len(neg_uniform), "auc": metrics.roc_auc(pos, neg_uniform),
                                  "lowest_fpr_1pct": metrics.lowest_threshold(sweep_u, 0.01),
                                  "lowest_fpr_2pct": metrics.lowest_threshold(sweep_u, 0.02),
                                  "at_threshold_80": sweep_u[80],
                                  "fpr_at_recommended": sweep_u[pick(0.01)["threshold"]]["fpr"] if pick(0.01) else None},
            "distribution": {"positive": metrics.distribution(pos), "negative": metrics.distribution(neg),
                             **{f"fine:{k}": metrics.distribution(v) for k, v in sorted(by_fine.items())}},
            "negatives_flagged_describing_trick": {
                str(t): {"flagged": sum(1 for sc, _ in neg_trick if sc >= t),
                         "reason_describes_trick": sum(1 for sc, m in neg_trick if sc >= t and m),
                         "fpr_excluding_trick_reasons": (sum(1 for sc, m in neg_trick if sc >= t and not m)
                                                         / len(neg_trick)) if neg_trick else None}
                for t in sorted({row["threshold"] for row in (pick(0.01), pick(0.02), sweep[80]) if row} | {50, 70})},
            "flag_rate_by_label": {
                str(t): {k: round(sum(1 for x in v if x >= t) / len(v), 4) for k, v in sorted(by_fine.items())}
                for t in sorted({row["threshold"] for row in (pick(0.01), pick(0.02), sweep[80]) if row})},
            "parse_failure_rate": parse_fail / calls_total if calls_total else None,
            "monitor_failure_rate": monitor_fail / calls_total if calls_total else None,
            "reason_mentions_trick_rate_on_positives": (sum(trick) / len(trick)) if trick else None,
            "mean_prompt_tokens": mean("prompt_tokens"), "mean_completion_tokens": mean("completion_tokens"),
            "mean_reasoning_tokens": mean("reasoning_tokens"), "mean_cached_tokens": mean("cached_tokens"),
            "mean_window_chars": mean("window_chars"),
            "latency_s": {"mean": round(statistics.fmean(lat), 2) if lat else None,
                          "median": lat[len(lat) // 2] if lat else None,
                          "p90": lat[int(len(lat) * 0.9)] if lat else None},
            "cost_usd_total": round(sum(costs), 4), "cost_usd_per_call": round(statistics.fmean(costs), 6)
            if costs else None,
            "finish_reasons": _count(r.get("finish_reason") for r in mode_rows),
            "statuses": _count(r.get("status") for r in mode_rows)}
    summary["prompt_versions"] = sorted(v for v in versions if v)
    agent_lat = [r["agent_latency_s"] for r in records.values() if isinstance(r.get("agent_latency_s"), (int, float))]
    summary["agent_step_latency_s"] = {"mean": round(statistics.fmean(agent_lat), 2) if agent_lat else None,
                                       "median": sorted(agent_lat)[len(agent_lat) // 2] if agent_lat else None}
    summary["ledger"] = invocations[-1]["budget"] if invocations else None
    return summary


def _count(values):
    out = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def cmd_analyze(args):
    summary = summarize(args.out_dir)
    full = args.out_dir / "summary-full.json"
    full.write_text(json.dumps(summary, indent=1, default=str))
    public = {k: v for k, v in summary.items()}
    ledger = summary.get("ledger") or {}
    # No local paths in the repo copy: the budget as limit, total committed (all invocations) and ledger label.
    public["ledger"] = {"limit_usd": ledger.get("limit_usd"), "committed_usd_all_invocations":
                        ledger.get("committed_usd"), "scope": "analysis",
                        "label_run_id": (ledger.get("prior") or {}).get("judged_run_id")}
    public["modes"] = {m: {k: v for k, v in d.items() if k != "sweep"} | {
        "sweep": [{k: r[k] for k in ("threshold", "fpr", "recall")} for r in d["sweep"]]}
        for m, d in summary["modes"].items()}
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(public, indent=1, default=str) + "\n")
    print(json.dumps({m: {k: d[k] for k in ("requests", "auc", "recommended_fpr_1pct", "at_fpr_2pct",
                                             "at_threshold_80", "parse_failure_rate", "cost_usd_per_call",
                                             "mean_prompt_tokens", "mean_completion_tokens", "latency_s")}
                      for m, d in summary["modes"].items()}, indent=1, default=str))
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("index", "sample", "replay", "analyze"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, default=Path(
            "runs/glmflash100-seeded-control-120m-push-r1-20261104T042405Z"))
        p.add_argument("--out-dir", type=Path)
        if name in ("index", "replay"):
            p.add_argument("--log", type=Path, required=True, help="the run's Inspect .eval log (restored)")
            p.add_argument("--workers", type=int, default=4, help="sample-loading processes (at most 4)")
        if name == "sample":
            p.add_argument("--negatives", type=int, required=True)
            p.add_argument("--seed", type=int, default=DEFAULT_SEED)
        if name == "replay":
            p.add_argument("--run-limit", type=Decimal, required=True, help="this calibration's budget in USD")
            p.add_argument("--modes", default=",".join(MODES))
            p.add_argument("--max-points", type=int, help="replay only the first N sampled points (a pilot)")
            p.add_argument("--concurrency", type=int, default=32)
            p.add_argument("--ledger", type=Path)
        if name == "analyze":
            p.add_argument("--summary", type=Path, help="JSON summary for the repo (aggregates only)")
    args = parser.parse_args(argv)
    args.out_dir = (args.out_dir or default_out(args.run_dir)).resolve()
    if getattr(args, "workers", 1) > 4:
        parser.error("--workers is at most 4 (local heavy processes)")
    return args


def main(argv=None):
    args = parse_args(argv)
    return {"index": cmd_index, "sample": cmd_sample, "replay": cmd_replay, "analyze": cmd_analyze}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
