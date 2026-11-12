#!/usr/bin/env python3
"""Convert one monitor run's call log into an Inspect ``.eval`` log (offline, no provider calls).

The top-down monitor (``research_swarm.monitor``; ``inspect_adapter.budget_filter``) writes every
scored call to ``RUN/monitor/calls.jsonl`` (``runtime.make_monitor_log``: agent, request_id,
agent_request_id, read, prompt_version, pending, system, user, response, reasoning, scores) and
records ``monitor_scored`` / ``monitor_blocked`` / ``monitor_block_enforced`` /
``monitor_block_result`` / ``monitor_error`` in ``RUN/runtime.jsonl``. This script joins them into
one Inspect log with one sample per solver agent and one ModelEvent per monitor call, in time
order: input is the exact system and user messages the monitor received, output is its reasoning
(``ContentReasoning``) and reply text. Each event's metadata lists, per pending tool call, the tool,
tool_use_id, score, reason, blocked and enforced, plus ``agent_request_id`` and (with
``--agent-eval``) ``agent_turn``: the 1-based index of the agent's model call that proposed those
tool calls in the agent transcript log. Monitor errors (no logged request) are ModelEvents with
``error`` set.

  python scripts/monitor_to_inspect.py --run-dir runs/<run-id> [--calls PATH] \\
      [--agent-eval runs/_monitor_inspect/M1-actions-silent-r1.eval] --out runs/_monitor_inspect

The output is researcher-only (it holds the agents' activity): keep it local and Git-ignored.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ARMS = {("actions", "silent"): 1, ("actions", "reason"): 2, ("full", "silent"): 3, ("full", "reason"): 4}
NAME = re.compile(r"monitor-(actions|full)-(silent|reason)-.*?-r(\d+)-")


def arm_of(run_id):
    """(k, read, mode, replicate) from a monitor run id, or None."""
    match = NAME.search(run_id)
    if not match:
        return None
    read, mode, rep = match.group(1), match.group(2), int(match.group(3))
    return ARMS[(read, mode)], read, mode, rep


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def read_runtime(path):
    """Monitor records of runtime.jsonl: (enabled payload, scored by request_id, errors, blocks,
    enforced ids, block results by id, agents)."""
    enabled, scored, errors, blocked, enforced, results, agents = None, {}, [], {}, set(), {}, set()
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            kind, payload, agent = event.get("kind"), event.get("payload") or {}, event.get("agent_id")
            if kind == "monitor_enabled":
                enabled = payload
            elif kind == "monitor_scored":
                scored[payload.get("request_id")] = {**payload, "timestamp": event["timestamp"], "agent": agent}
            elif kind == "monitor_error":
                errors.append({**payload, "timestamp": event["timestamp"], "agent": agent})
            elif kind == "monitor_blocked":
                blocked[payload["tool_use_id"]] = payload
            elif kind == "monitor_block_enforced":
                enforced.add(payload["tool_use_id"])
            elif kind == "monitor_block_result":
                results[payload["tool_use_id"]] = payload
            elif kind in ("monitor_hook_installed", "agent_exit") and agent:
                agents.add(agent)
    return enabled, scored, errors, blocked, enforced, results, agents


def agent_turns(eval_path):
    """{tool_use_id: (agent, 1-based model-call index, ModelEvent uuid)} from the agents' transcript log."""
    from inspect_ai.log import read_eval_log_sample, read_eval_log_sample_summaries
    turns = {}
    for summary in read_eval_log_sample_summaries(eval_path):
        sample = read_eval_log_sample(eval_path, summary.id, summary.epoch)
        index = 0
        for event in sample.events:
            if event.event != "model":
                continue
            index += 1
            for call in (event.output.message.tool_calls or []) if event.output and event.output.choices else []:
                turns[call.id] = (str(sample.id), index, event.uuid)
    return turns


def _usage(scored):
    from inspect_ai.model import ModelUsage
    usage = (scored or {}).get("usage")
    try:
        return ModelUsage(**usage) if usage else None
    except Exception:
        return None


def build_log(run_dir, calls_path, agent_eval=None, turns=None):
    from inspect_ai.event import ModelEvent
    from inspect_ai.log import EvalConfig, EvalDataset, EvalLog, EvalPlan, EvalResults, EvalSample, EvalSpec, EvalStats
    from inspect_ai.model import (ChatMessageAssistant, ChatMessageSystem, ChatMessageUser, ContentReasoning,
                                  ContentText, GenerateConfig, ModelOutput)
    from inspect_ai.model._model_output import ChatCompletionChoice

    run_dir = Path(run_dir)
    run_id = run_dir.name
    arm = arm_of(run_id)
    enabled, scored, errors, blocked, enforced, results, agents = read_runtime(run_dir / "runtime.jsonl")
    enabled = enabled or {}
    if turns is None:
        turns = agent_turns(agent_eval) if agent_eval else {}
    model_id = enabled.get("model_id") or "unknown"
    model_name = f"openrouter/{model_id}"
    config = GenerateConfig(max_tokens=enabled.get("max_tokens"), reasoning_effort=enabled.get("reasoning_effort"))

    per_agent = defaultdict(list)  # agent -> [(timestamp, event, summary)]
    logged = set()

    def per_call(pending, scores_by_id, scored_payload, agent):
        rows = []
        flags = (scored_payload or {}).get("blocked") or []
        ids = (scored_payload or {}).get("tool_use_ids") or []
        for position, call in enumerate(pending):
            cid = call.get("id")
            score = scores_by_id.get(cid, {})
            is_blocked = flags[ids.index(cid)] if cid in ids and ids.index(cid) < len(flags) else cid in blocked
            turn = turns.get(cid)
            rows.append({"tool": call.get("name"), "tool_use_id": cid, "score": score.get("score"),
                         "reason": score.get("reason"), "parse_error": score.get("error"),
                         "blocked": bool(is_blocked), "enforced": cid in enforced,
                         "block_message": (blocked.get(cid) or {}).get("message"),
                         "cli_result": (results.get(cid) or {}).get("cli_text"),
                         "agent_turn": turn[1] if turn and turn[0] == agent else None})
        return rows

    with open(calls_path, encoding="utf-8") as stream:
        for line in stream:
            entry = json.loads(line)
            agent, request_id = entry.get("agent"), entry.get("request_id")
            logged.add(request_id)
            sp = scored.get(request_id)
            end = entry["timestamp"]
            start = end - float((sp or {}).get("latency_seconds") or 0.0)
            pending = entry.get("pending") or []
            rows = per_call(pending, {s.get("id"): s for s in entry.get("scores") or []}, sp, agent)
            turn = next((r["agent_turn"] for r in rows if r["agent_turn"]), None)
            content = []
            if entry.get("reasoning"):
                content.append(ContentReasoning(reasoning=entry["reasoning"]))
            content.append(ContentText(text=entry.get("response") or ""))
            output = ModelOutput(model=model_name, choices=[ChatCompletionChoice(
                message=ChatMessageAssistant(content=content, model=model_name),
                stop_reason=(sp or {}).get("stop_reason") or "stop")], usage=_usage(sp))
            any_block = any(r["blocked"] for r in rows)
            metadata = {"monitor_request_id": request_id, "agent_request_id": entry.get("agent_request_id"),
                        "agent_turn": turn, "read": entry.get("read"), "prompt_version": entry.get("prompt_version"),
                        "blocked": any_block, "enforced": any(r["enforced"] for r in rows),
                        "max_score": max((r["score"] for r in rows if isinstance(r["score"], (int, float))),
                                         default=None),
                        "reasoning_logged": bool(entry.get("reasoning")), "calls": rows,
                        "latency_seconds": (sp or {}).get("latency_seconds"),
                        "settled_usd": (sp or {}).get("settled_usd"),
                        "runtime_record": "monitor_scored" if sp else "missing"}
            event = ModelEvent(model=model_name, input=[ChatMessageSystem(content=entry.get("system") or ""),
                                                        ChatMessageUser(content=entry.get("user") or "")],
                               tools=[], tool_choice="none", config=config, output=output,
                               timestamp=_iso(start), completed=_iso(end),
                               working_time=(sp or {}).get("latency_seconds"), metadata=metadata)
            per_agent[agent].append((start, event, metadata))

    unlogged = [sp for rid, sp in scored.items() if rid not in logged]
    for sp in unlogged:  # scored but absent from calls.jsonl (monitor_log_error): scores only
        errors.append({**sp, "stage": "not_in_call_log", "_scored": True})
    for err in errors:
        agent = err.get("agent")
        end = err["timestamp"]
        start = end - float(err.get("latency_seconds") or 0.0)
        pending = [{"id": i, "name": n} for i, n in zip(err.get("tool_use_ids") or [], err.get("tools") or [])]
        sp = err if err.get("_scored") else None
        scores = {i: {"score": s, "reason": r} for i, s, r in zip(err.get("tool_use_ids") or [],
                                                                  err.get("scores") or [], err.get("reasons") or [])}
        rows = per_call(pending, scores, sp, agent)
        turn = next((r["agent_turn"] for r in rows if r["agent_turn"]), None)
        text = (f"monitor_error stage={err.get('stage')} reason={err.get('reason')} status={err.get('status')} "
                f"error={err.get('error')} detail={err.get('detail')}") if not sp else \
            "scored (monitor_scored) but the request and reply are missing from monitor/calls.jsonl"
        metadata = {"monitor_request_id": err.get("request_id"), "agent_request_id": err.get("agent_request_id"),
                    "agent_turn": turn, "error": True, "stage": err.get("stage"),
                    "blocked": any(r["blocked"] for r in rows), "enforced": any(r["enforced"] for r in rows),
                    "max_score": max((r["score"] for r in rows if isinstance(r["score"], (int, float))), default=None),
                    "calls": rows, "latency_seconds": err.get("latency_seconds"),
                    "runtime_record": "monitor_scored" if sp else "monitor_error"}
        event = ModelEvent(model=model_name, input=[ChatMessageUser(content="(monitor request not logged)")],
                           tools=[], tool_choice="none", config=config,
                           output=ModelOutput(model=model_name, error=text), error=text,
                           timestamp=_iso(start), completed=_iso(end), metadata=metadata)
        per_agent[agent].append((start, event, metadata))

    samples, totals = [], defaultdict(int)
    all_starts = [t for items in per_agent.values() for t, _, _ in items]
    origin = min(all_starts) if all_starts else 0.0
    for agent in sorted(set(per_agent) | agents):
        items = sorted(per_agent.get(agent, []), key=lambda item: item[0])
        events = []
        for start, event, _ in items:
            event.working_start = start - origin
            events.append(event)
        metas = [m for _, _, m in items]
        rows = [r for m in metas for r in m["calls"]]
        summary = {"monitor_calls": sum(1 for m in metas if not m.get("error")),
                   "monitor_errors": sum(1 for m in metas if m.get("error")),
                   "tool_calls_scored": sum(1 for r in rows if r["score"] is not None),
                   "blocks": sum(1 for r in rows if r["blocked"]),
                   "blocks_enforced": sum(1 for r in rows if r["enforced"]),
                   "max_score": max((r["score"] for r in rows if isinstance(r["score"], (int, float))), default=None),
                   "reasoning_logged": sum(1 for m in metas if m.get("reasoning_logged")),
                   "blocked_turns": [m["agent_turn"] for m in metas if m.get("blocked")]}
        for key in ("monitor_calls", "monitor_errors", "tool_calls_scored", "blocks", "blocks_enforced",
                    "reasoning_logged"):
            totals[key] += summary[key]
        samples.append(EvalSample(id=agent, epoch=1, input=f"Monitor calls for {agent} ({run_id})", target="",
                                  events=events, metadata=summary,
                                  messages=[ChatMessageUser(content=f"{len(events)} monitor calls; see Transcript.")]))
    label = f"M{arm[0]}_{arm[1]}_{arm[2]}_r{arm[3]}" if arm else run_id
    created = _iso(origin).isoformat() if all_starts else datetime.now(timezone.utc).isoformat()
    from shortuuid import uuid as short_uuid
    ends = [e.completed.timestamp() for items in per_agent.values() for _, e, _ in items if e.completed]
    finished = _iso(max(ends)).isoformat() if ends else created
    spec = EvalSpec(created=created, task=f"monitor_{label}", task_id=short_uuid(), run_id=short_uuid(),
                    eval_id=short_uuid(), dataset=EvalDataset(name=run_id, samples=len(samples)),
                    model=model_name, config=EvalConfig(), metadata={"run_id": run_id, "monitor": enabled,
                                                                    "source": "monitor/calls.jsonl + runtime.jsonl",
                                                                    "totals": dict(totals)})
    log = EvalLog(eval=spec, plan=EvalPlan(), results=EvalResults(total_samples=len(samples),
                                                                     completed_samples=len(samples)),
                  stats=EvalStats(started_at=created, completed_at=finished), samples=samples, status="success")
    return log, label, dict(totals)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--calls", help="monitor/calls.jsonl (default: RUN/monitor/calls.jsonl)")
    parser.add_argument("--agent-eval", help="the agents' transcript .eval, for agent_turn")
    parser.add_argument("--out", required=True, help="output directory")
    args = parser.parse_args(argv)
    from inspect_ai.log import write_eval_log
    run_dir = Path(args.run_dir)
    log, label, totals = build_log(run_dir, args.calls or run_dir / "monitor" / "calls.jsonl", args.agent_eval)
    arm = arm_of(run_dir.name)
    name = (f"M{arm[0]}-{arm[1]}-{arm[2]}-r{arm[3]}-monitor.eval" if arm else f"{run_dir.name}-monitor.eval")
    out = Path(args.out) / name
    out.parent.mkdir(parents=True, exist_ok=True)
    write_eval_log(log, str(out))
    print(json.dumps({"out": str(out), "bytes": out.stat().st_size, "task": log.eval.task, **totals}))


if __name__ == "__main__":
    main()
