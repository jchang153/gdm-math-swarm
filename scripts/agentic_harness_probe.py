#!/usr/bin/env python3
"""UNPAID harness probe for the agentic monitor (arm AG; docs/plan.md, "Agentic monitor arm AG (owner decisions,
November 23, 2026)"): does the pinned Claude Code in the agent image, configured exactly as a monitor
(research_swarm.agentic_monitor: replace-mode prompt agentic-review-v1, the ``review`` MCP server with the five
tools, the built-in tool disallow list, MCP_TOOL_TIMEOUT / MAX_MCP_OUTPUT_TOKENS), behave as the arm needs?

No provider is contacted: the model is Inspect's ``mockllm`` and a generate filter answers every request with a
scripted tool call (``INSPECT_DISABLE_MODEL_API`` is set for the evaluation, as in scripts/validate_runtime.py).
The five tools are stand-ins with the runtime's names and schemas (agentic_prompt.TOOL_PARAMETERS); only ``wait``
and ``get_output`` do anything (sleep; return a large text). Needs Docker and the agent image (run it on the VM).

Script (one monitor sample):
1. first request: record the tools Claude Code offers the model (check ``only_monitor_tools``: exactly
   ``mcp__review__<tool>`` for the five tools, no built-in tool; this checks the disallow list itself, without
   the runtime filter that also strips other tools) and the system text (``prompt_replaced``);
2. ``wait`` returning a delivery of exactly ``agentic_monitor.DELIVERY_MAX_CHARS`` characters
   (``delivery_intact``: the next request carries it whole);
3. ``get_output`` returning ``--big-chars`` (default 120,000) characters (``oversized_result``: recorded as
   observed: whole, cut, or replaced by a file reference; informational);
4. ``wait(seconds = --wait-seconds, default the config's max_wait_seconds, 1800)`` (``long_wait_survives``: the
   result, not a timeout error, arrives in the next request after at least that long);
5. a final text reply; then the monitor container's peak memory (cgroup ``memory.peak``, informational, for the
   monitors' memory limit).

Writes ``data/validation/agentic-harness-<UTC>.json`` (checks and counts only, no credentials) and
``runs/_agentic_harness/<UTC>/`` (Inspect log, runtime.jsonl, requests.jsonl). Takes about --wait-seconds plus a
minute or two.

    PYTHONPATH=src:scripts .venv/bin/python scripts/agentic_harness_probe.py \\
        --config configs/dsv41flash100-seeded-agentic-monitor-120m-push.json [--wait-seconds 1800]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
os.environ.setdefault("INSPECT_DISPLAY", "plain")

SCHEMA = "gdm-agentic-harness/1"
MOCK_MODEL = "mockllm/model"
MONITOR = "agmon-001"
WAIT_OK = "AGPROBE-WAIT-OK"
DELIVERY_HEAD, DELIVERY_TAIL = "AGPROBE-DELIVERY-HEAD", "AGPROBE-DELIVERY-TAIL"
BIG_HEAD, BIG_TAIL = "AGPROBE-BIG-HEAD", "AGPROBE-BIG-TAIL"
FILE_HINTS = ("saved to", "written to", "/tmp/", "tool-results", "exceeds maximum allowed tokens", "too large")


# ------------------------------------------------------------------------------------------- pure parts

def filler(total, head, tail):
    """Exactly ``total`` characters: ``head``, deterministic filler, ``tail``."""
    body = total - len(head) - len(tail)
    if body < 0:
        raise ValueError("total too small")
    line = "lorem ipsum dolor sit amet 0123456789\n"
    return head + (line * (body // len(line) + 1))[:body] + tail


def expected_tools(server="review"):
    from research_swarm.agentic_prompt import TOOL_NAMES
    return sorted(f"mcp__{server}__{name}" for name in TOOL_NAMES)


def tool_result_text(message):
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    parts = []
    for part in content or ():
        text = getattr(part, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def summarize_result(text, head, tail, total):
    """How a tool result arrived: its length, whether head/tail markers survived, hints of a file reference."""
    text = text or ""
    lowered = text.lower()
    return {"chars": len(text), "sent_chars": total, "head": head in text, "tail": tail in text,
            "whole": head in text and tail in text and len(text) >= total,
            "file_reference_hint": any(h in lowered for h in FILE_HINTS), "excerpt_end": text[-200:]}


def evaluate(requests, *, wait_seconds, delivery_chars, big_chars, server="review", prompt=None):
    """Checks from the recorded requests ([{"step", "time", "tools", "system", "last_tool", "last_tool_text"}])."""
    requests = [r for r in requests if r["step"] != "side"]
    by_step = {r["step"]: r for r in requests}
    checks = {}
    first = requests[0] if requests else {}
    offered = sorted(first.get("tools") or [])
    checks["only_monitor_tools"] = {"passed": offered == expected_tools(server), "offered": offered,
                                    "extra": sorted(set(offered) - set(expected_tools(server))),
                                    "missing": sorted(set(expected_tools(server)) - set(offered))}
    system = first.get("system") or ""
    checks["prompt_replaced"] = {"passed": bool(prompt) and prompt in system and len(system) - len(prompt) <= 500,
                                 "other_system_chars": (len(system) - len(prompt)) if prompt and prompt in system
                                 else None}
    after_delivery = by_step.get("after_delivery") or {}
    d = summarize_result(after_delivery.get("last_tool_text"), DELIVERY_HEAD, DELIVERY_TAIL, delivery_chars)
    checks["delivery_intact"] = {"passed": d["whole"], **d}
    after_big = by_step.get("after_big") or {}
    b = summarize_result(after_big.get("last_tool_text"), BIG_HEAD, BIG_TAIL, big_chars)
    checks["oversized_result"] = {"passed": None, "informational": True, **b}
    after_wait = by_step.get("after_long_wait") or {}
    before_wait = by_step.get("after_big") or {}
    elapsed = (after_wait.get("time") - before_wait.get("time")) if after_wait and before_wait else None
    text = after_wait.get("last_tool_text") or ""
    checks["long_wait_survives"] = {"passed": WAIT_OK in text and elapsed is not None and elapsed >= wait_seconds,
                                    "wait_seconds": wait_seconds, "elapsed_s": None if elapsed is None
                                    else round(elapsed, 1), "result_excerpt": text[:300]}
    checks["finished"] = {"passed": "final" in by_step}
    return checks


# ------------------------------------------------------------------------------------------- the probe

def stand_in_tools(wait_cap, delivery_chars, big_chars):
    """The five tools with the runtime's names and schemas; ``wait`` sleeps, ``get_output`` returns big text."""
    from inspect_ai.tool import ToolDef, ToolParams
    from research_swarm.agentic_prompt import TOOL_DESCRIPTIONS, TOOL_NAMES, TOOL_PARAMETERS
    calls = {"wait": 0}

    async def run(name, args):
        if name == "wait":
            calls["wait"] += 1
            if calls["wait"] == 1:
                return filler(delivery_chars, DELIVERY_HEAD, DELIVERY_TAIL)
            seconds = min(max(float(args.get("seconds") or 0), 0.0), float(wait_cap))
            await asyncio.sleep(seconds)
            return f"{WAIT_OK} after {seconds:.0f} s\n(no new activity)"
        if name == "get_output":
            return filler(big_chars, BIG_HEAD, BIG_TAIL)
        if name == "list_revertible":
            return "(none)"
        return "ok"

    tools = []
    for name in TOOL_NAMES:
        def bind(name):
            async def execute(**args):
                return await run(name, args)
            return execute
        tools.append(ToolDef(bind(name), name=name, description=TOOL_DESCRIPTIONS[name],
                             parameters=ToolParams.model_validate(TOOL_PARAMETERS[name]), max_output=0).as_tool())
    return tools


def scripted_filter(steps, log, prompt):
    """A generate filter that never calls a model: records each request and answers the monitor session's
    requests with the next step (other requests of the CLI, e.g. without the monitor prompt, get "ok")."""
    from inspect_ai.model import ChatMessageSystem, ChatMessageTool, ModelOutput

    async def intercept(model, messages, tools, tool_choice, config):
        last_tool = next((m for m in reversed(messages) if isinstance(m, ChatMessageTool)), None)
        system = "\n".join(m.text for m in messages if isinstance(m, ChatMessageSystem))
        if prompt not in system:
            log.append({"step": "side", "time": time.time(), "messages": len(messages),
                        "tools": [t.name for t in tools], "system": system[:300], "last_tool": None,
                        "last_tool_text": None})
            return ModelOutput.from_content(model=MOCK_MODEL, content="ok")
        name, make = steps.pop(0) if steps else ("extra", None)
        log.append({"step": name, "time": time.time(), "messages": len(messages),
                    "tools": [t.name for t in tools], "system": system,
                    "last_tool": None if last_tool is None else last_tool.function,
                    "last_tool_text": None if last_tool is None else tool_result_text(last_tool)})
        if make is None:
            return ModelOutput.from_content(model=MOCK_MODEL, content="Done.")
        return make(tools)
    return intercept


def steps_for(wait_seconds, server="review"):
    from inspect_ai.model import ModelOutput

    def call(name, args, cid):
        return lambda tools: ModelOutput.for_tool_call(model=MOCK_MODEL, tool_name=f"mcp__{server}__{name}",
                                                       tool_arguments=args, tool_call_id=cid)
    return [("first", call("wait", {"seconds": 10}, "agprobe-1")),
            ("after_delivery", call("get_output", {"call_id": "agprobe-x"}, "agprobe-2")),
            ("after_big", call("wait", {"seconds": wait_seconds}, "agprobe-3")),
            ("after_long_wait", lambda tools: ModelOutput.from_content(model=MOCK_MODEL, content="Done.")),
            ("final", None)]


async def probe(config, args, run_dir):
    from inspect_ai.agent import AgentState
    from inspect_ai.model import ChatMessageUser, get_model
    from inspect_ai.solver import solver
    from inspect_ai.util import sandbox
    from research_swarm import agentic_monitor as am, runtime
    from research_swarm.inspect_adapter import build_claude_agent
    import validate_runtime as vr

    settings = am.agentic_settings(config)
    if settings is None:
        raise SystemExit("the config has no enabled agentic_monitor block")
    images = runtime.pin_images(config)
    compose = runtime.make_compose(run_dir, {**config, "agent_memory": args.memory or config.get("agent_memory")},
                                   images["agent"]["id"])
    prompt = am.system_prompt(settings)
    log, extra = [], {}
    # The last scripted step ("final") marks that Claude Code sent a request after the final reply's turn; the
    # "after_long_wait" step answers with text, which ends the session.
    steps = steps_for(args.wait_seconds, am.MCP_SERVER)
    gen_filter = scripted_filter(steps, log, prompt)

    @solver
    def monitor_probe():
        async def execute(state, generate):
            box = sandbox()
            version = await box.exec(["bash", "-lc", "claude --version 2>/dev/null || true"], timeout=60)
            extra["claude_code_version"] = (version.stdout or "").strip()[:100]
            agent = build_claude_agent(prompt, stand_in_tools(settings["max_wait_seconds"], am.DELIVERY_MAX_CHARS,
                                                              args.big_chars),
                                       gen_filter, env=am.claude_code_env_extra(settings), auto_memory=False,
                                       mcp_server=am.MCP_SERVER, disallowed_tools=list(am._disallowed_tools()))
            started = time.time()
            try:
                result = await agent(AgentState(messages=[ChatMessageUser(content=am.initial_message())]))
                state.messages = result.messages
                extra["harness_status"] = "completed"
            except Exception as ex:
                extra["harness_status"] = f"error: {type(ex).__name__}: {str(ex)[:300]}"
            extra["session_s"] = round(time.time() - started, 1)
            if not any(r["step"] == "final" for r in log) and extra["harness_status"] == "completed":
                log.append({"step": "final", "time": time.time(), "tools": [], "system": "", "last_tool": None,
                            "last_tool_text": None})
            peak = await box.exec(["bash", "-lc", "cat /sys/fs/cgroup/memory.peak 2>/dev/null || "
                                   "cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null || true"],
                                  timeout=60)
            extra["memory_peak_bytes"] = int(peak.stdout.strip()) if (peak.stdout or "").strip().isdigit() else None
            return state
        return execute

    task = runtime.build_task(agent_ids=[MONITOR], files={}, solver=monitor_probe(), compose=compose,
                              metadata={"condition": "unpaid-agentic-harness-probe"}, initial_message="probe")
    model = get_model(MOCK_MODEL, custom_outputs=[])
    await vr.run_eval(task, model, agents=1, log_dir=run_dir / "inspect")
    return log, extra, prompt, settings, images


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=float, help="the long wait (default: max_wait_seconds)")
    parser.add_argument("--big-chars", type=int, default=120_000)
    parser.add_argument("--memory", help="the probe container's memory limit (default: the config's agent_memory)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "validation")
    parser.add_argument("--records-dir", type=Path, default=ROOT / "runs" / "_agentic_harness")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    from research_swarm import agentic_monitor as am
    settings = am.agentic_settings(config) or {}
    if args.wait_seconds is None:
        args.wait_seconds = float(settings.get("max_wait_seconds", 1800))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (args.records_dir / stamp).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    print(f"Unpaid agentic harness probe {stamp}: long wait {args.wait_seconds:.0f} s, oversized result "
          f"{args.big_chars} chars (mockllm; no provider is contacted)", flush=True)
    started = time.time()
    log, extra, prompt, settings, images = asyncio.run(probe(config, args, run_dir))
    with open(run_dir / "requests.jsonl", "w", encoding="utf-8") as handle:
        for row in log:
            handle.write(json.dumps({k: v for k, v in row.items() if k != "system"}, default=str) + "\n")
    checks = evaluate(log, wait_seconds=args.wait_seconds, delivery_chars=am.DELIVERY_MAX_CHARS,
                      big_chars=args.big_chars, server=am.MCP_SERVER, prompt=prompt)
    required = [k for k, v in checks.items() if not v.get("informational")]
    evidence = {
        "schema": SCHEMA, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config": str(args.config), "kind": "unpaid agentic monitor harness probe (mockllm scripted outputs)",
        "agent_image_id": images["agent"]["id"], "claude_code_version": extra.get("claude_code_version"),
        "harness_status": extra.get("harness_status"), "session_s": extra.get("session_s"),
        "elapsed_s": round(time.time() - started, 1),
        "settings": {"env": am.claude_code_env_extra(settings), "disallowed_tools": list(am._disallowed_tools()),
                     "delivery_max_chars": am.DELIVERY_MAX_CHARS, "memory_limit": args.memory or config.get("agent_memory")},
        "memory_peak_bytes": extra.get("memory_peak_bytes"),
        "checks": checks, "required": required,
        "passed": all(checks[k]["passed"] for k in required),
        "credentials_in_record": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f"agentic-harness-{stamp}.json"
    out.write_text(json.dumps(evidence, indent=1, default=str) + "\n")
    print(json.dumps({"path": str(out), "passed": evidence["passed"],
                      "checks": {k: v["passed"] for k, v in checks.items()},
                      "oversized_result": {k: checks["oversized_result"][k] for k in ("chars", "whole",
                                                                                     "file_reference_hint")},
                      "memory_peak_bytes": evidence["memory_peak_bytes"]}, indent=1))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
