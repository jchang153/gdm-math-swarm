#!/usr/bin/env python3
"""Unpaid end-to-end runtime validation with Inspect's mockllm model.

Runs three genuine Claude Code peers (pinned binary in gdm-math-swarm-agent:local)
in real network-disabled containers through the Inspect sandbox agent bridge,
using the production solver (runtime.peer_solver), budget filter, start barrier,
run clock, snapshotter, instruction archive, conference service, tool
definitions, prompts, evaluator queue wiring and operational judge
(runtime.make_lean_compiler: DockerLeanCompiler with the I1 parameters). Model
outputs are scripted (mockllm); no provider is contacted. Before the evaluation,
Inspect's own .env loading is disabled (inspect_adapter.disable_inspect_dotenv,
interface X1) and non-mock model APIs are refused (INSPECT_DISABLE_MODEL_API), so
this script never loads the host .env. The evidence records whether
OPENROUTER_API_KEY was nevertheless present in the process environment (inherited
from the operator's shell) before and after the run, as measured presence only.

Budget: reservations and settlements run through the production BudgetLedger
code, but in a throwaway ledger (<run_dir>/validation-budget.sqlite, scope
"validation", notional cap 1000). Never the authoritative state/budget.sqlite
and never the "compatibility" scope: mock reservations are positive and would
consume a real cap, while mockllm reports no provider cost.

Agents: agent-001 exercises tools, the real judge, notebooks (absolute and
relative publish_notebook paths; a ~40,000-character Unicode-heavy notebook read
back in full through read_library_file and the real bridge), sandbox_file
rejection, disallowed tools, compaction (bracketed: fabricated prompt sizes of
120,000 to 195,000 tokens in ascending order; the lowest one after which the CLI
auto-compacts is recorded as ``compaction_threshold_tokens``, informational) and
one truncated response (Claude
Code's output-limit recovery must continue the session); agent-002 arrives late
at the barrier, checks the 120 s Bash clamp (and that the timed-out process is
gone), starts two deliberately slow `lake env lean` compiles in the background
(one through the Bash tool's ``run_in_background``, one detached with
``setsid nohup``) that the image's lake/lean wrappers must kill at about 120 s,
reads agent-001's direct message and exits voluntarily; agent-003 returns four
consecutive truncated responses (recovery exhaustion), whose exit must not be
recorded as voluntary.

The compose file the agents ran is bound to the evidence (``compose_sha256``, the
SHA-256 of the written file, checked against runtime.compose_fingerprint; X3).
Agent memory/CPUs and image tags default to the pilot config (``--config``) so
the evidence matches the compose file a launch writes.

Images are pinned to local image IDs (runtime.pin_images) and the researcher-side
Lean warm-up (runtime.warm_up_lean) runs before the agents start, exactly as in
run_pilot, so both are exercised here (``--no-warm-up`` skips the warm-up).

Before anything starts, the launch interlock (runtime.launch_interlock, shared with
run_pilot, the compatibility check and the sibling probes) refuses while a
comparator container (label gdm-evaluator=1) runs, a judge container (gdm-judge=1)
is left over or an agent sandbox of another run runs. ``--force-while-live``
(debugging only) proceeds and records the override; the required check
``no_conflicting_containers_at_start`` then fails.

Writes data/validation/runtime-e2e-<UTC YYYYMMDDTHHMMSSZ>.json (I6). Researcher
artifacts go to runs/validation-runtime-<UTC>/ (Git-ignored). Phase B only:

    PYTHONPATH=src .venv/bin/python scripts/validate_runtime.py

Sibling scripts (isolation_probes.py, capacity_probe.py) import the helpers in
the first half of this file.
"""
from __future__ import annotations
import argparse
import asyncio
import contextvars
import hashlib
import inspect as pyinspect
import json
import os
import re
import shlex
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("INSPECT_DISPLAY", "plain")  # eval_async has no display argument

from research_swarm.budget import BudgetLedger  # noqa: E402
from research_swarm import inspect_adapter  # noqa: E402  (X1 guard, looked up at call time)
from research_swarm.inspect_adapter import (CLAUDE_CODE_VERSION, DISALLOWED_TOOLS,  # noqa: E402
                                            InstructionArchive, budget_filter, claude_code_env,
                                            credentials_in_environment, installed_versions)
from research_swarm.conference import MAX_NOTEBOOK_BYTES, MAX_READ_CHARS  # noqa: E402
from research_swarm.provider import ProviderSettings  # noqa: E402
from research_swarm import runtime  # noqa: E402

VALIDATION_DIR = ROOT / "data" / "validation"
DEFAULT_CONFIG = ROOT / "configs" / "pilot.json"
MOCK_MODEL = "mockllm/model"
VALIDATION_SCOPE = "validation"
VALIDATION_CAP = 1000  # notional; mock calls cost nothing
# Result of the last inspect_adapter.disable_inspect_dotenv() call (X1): booleans and
# patched module names only, never a value.
DOTENV_GUARD = {}
# docker/agent.Dockerfile: root-owned wrappers first on PATH that run the elan
# lake/lean under `timeout -v -s KILL 120` (approved local compilation limit).
LEAN_WRAPPERS = {"lake": "/usr/local/bin/lake", "lean": "/usr/local/bin/lean"}
LOCAL_LEAN_LIMIT_SECONDS = 120
KILL_NOTICE = "sending signal KILL"  # printed by `timeout -v` when the limit fires (as in the judge)


# ---------------------------------------------------------------------------
# Shared helpers (also used by isolation_probes.py and capacity_probe.py)

def utc_stamp(moment=None):
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def rel(path):
    """Repository-relative path when possible (evidence files avoid host paths)."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def check(passed, required=True, **evidence):
    return {"passed": bool(passed), "required": required, **evidence}


def write_evidence(prefix, schema, checks, image_ids, stamp, directory=VALIDATION_DIR, **details):
    """I6 evidence file <prefix>-<stamp>.json with the agreed top-level keys.

    ``credentials_in_environment`` is measured when the file is written, i.e. after
    the run (presence of OPENROUTER_API_KEY in this process; never a value), not
    asserted: X1 disables Inspect's .env loading, but a key exported in the
    operator's shell is inherited and is reported here.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}-{stamp}.json"
    report = {"schema": schema, "created_at": iso_now(),
              "all_required_passed": all(c["passed"] for c in checks.values() if c.get("required", True)),
              "image_ids": image_ids, "paid_calls": 0, "provider_contacted": False,
              "credentials_in_environment": credentials_in_environment(),
              "credentials_in_environment_before_eval": DOTENV_GUARD.get("credentials_in_environment"),
              "inspect_dotenv_disabled": DOTENV_GUARD.get("dotenv_disabled") is True,
              "checks": checks, **details}
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return path, report


def load_config(path):
    path = Path(path) if path else None
    return json.loads(path.read_text()) if path and path.is_file() else {}


def pilot_runtime_defaults(path):
    """Agent image tags and agent memory/CPUs from the pilot config, so the compose
    file a probe runs is the one a launch writes (compose_sha256, X3)."""
    config = load_config(path)
    return {"image": config.get("agent_image") or runtime.AGENT_IMAGE,
            "lean_image": config.get("lean_image") or runtime.LEAN_IMAGE,
            "agent_memory": config.get("agent_memory", "1g"), "agent_cpus": config.get("agent_cpus")}


def optional_float(value):
    return None if str(value).lower() in ("none", "null") else float(value)


def compose_binding(compose_path, config, agent_image_id):
    """X3: SHA-256 of the compose file the probe actually ran (``compose_sha256``),
    compared with runtime.compose_fingerprint(config, agent image ID), the file a
    launch with this config and image would write."""
    actual = hashlib.sha256(Path(compose_path).read_bytes()).hexdigest()
    expected = runtime.compose_fingerprint(config, agent_image_id)
    return {"compose_sha256": actual, "compose_fingerprint": expected, "matches": actual == expected,
            "compose_file": rel(compose_path), "agent_image_id": agent_image_id,
            "agent_memory": config.get("agent_memory", "1g"), "agent_cpus": config.get("agent_cpus")}


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def percentiles(values, points=(50, 90, 95, 100)):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"count": 0}
    out = {"count": len(values)}
    for p in points:
        index = min(len(values) - 1, max(0, int(round(p / 100 * (len(values) - 1)))))
        out[f"p{p}"] = round(values[index], 3)
    return out


AGENT_CONTEXT: contextvars.ContextVar = contextvars.ContextVar("gdm_validation_agent", default=None)


def attributed(agent_id, inner, *, first_delay=0.0, arrivals=None):
    """Wrap a production filter: records which bridge a request came through
    (so nested or compaction requests are attributable) and can hold back an
    agent's first request (late barrier arrival)."""
    async def intercept(model, messages, tools, tool_choice, config):
        token = AGENT_CONTEXT.set(agent_id)
        try:
            if arrivals is not None and agent_id not in arrivals:
                if first_delay:
                    await asyncio.sleep(first_delay)
                arrivals[agent_id] = time.time()
            return await inner(model, messages, tools, tool_choice, config)
        finally:
            AGENT_CONTEXT.reset(token)
    intercept.state = getattr(inner, "state", None)
    return intercept


def conference_name(tools, name):
    matches = [t.name for t in tools if t.name.endswith("__" + name) or t.name == name]
    return matches[0] if matches else "mcp__conference__" + name


def tool_call(name, args, cid):
    from inspect_ai.model import ModelOutput
    return lambda tools: ModelOutput.for_tool_call(model=MOCK_MODEL, tool_name=name, tool_arguments=args, tool_call_id=cid)


def conf_call(name, args, cid):
    from inspect_ai.model import ModelOutput
    return lambda tools: ModelOutput.for_tool_call(model=MOCK_MODEL, tool_name=conference_name(tools, name),
                                                   tool_arguments=args, tool_call_id=cid)


def bash(command, cid, timeout_ms=None, description="Run a command", background=False):
    args = {"command": command, "description": description}
    if timeout_ms:
        args["timeout"] = timeout_ms
    if background:
        args["run_in_background"] = True  # Claude Code's Bash tool parameter (no timeout applies)
    return tool_call("Bash", args, cid)


def text(content, stop_reason="stop"):
    from inspect_ai.model import ModelOutput
    return lambda tools: ModelOutput.from_content(model=MOCK_MODEL, content=content, stop_reason=stop_reason)


def after_event(events_path, kind, make, timeout=600.0):
    """Maker that holds a scripted step until ``kind`` appears in an events.jsonl
    (the in-container proxy keeps the CLI's request alive meanwhile)."""
    async def maker(tools):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not any(e.get("kind") == kind for e in read_jsonl(events_path)):
            await asyncio.sleep(2)
        return make(tools)
    return maker


COMPACTION_MARKERS = ("create a detailed summary of the conversation", "CRITICAL: Respond with TEXT ONLY")
TRUNCATION_NUDGE_MARKERS = ("Output token limit hit", "reached the output limit", "exceeded the output token limit")


class ScriptedModel:
    """mockllm callable: per-agent scripts, attributed by the bridge that carried
    the request. Compaction requests get a summary; Claude Code's output-limit
    recovery nudges are logged and answered with the next scripted step;
    requests from another Claude Code session in the same container (no
    "You are <agent>," in the system prompt) are answered with ``foreign_reply``.
    Steps are (name, maker) where maker(tools) returns a ModelOutput (or an
    awaitable); an ``on_step`` hook sees each served step.
    """
    def __init__(self, scripts, final="Validation script complete.", foreign_reply="nested-ok", on_step=None):
        self.scripts = {agent: list(steps) for agent, steps in scripts.items()}
        self.final, self.foreign_reply, self.on_step = final, foreign_reply, on_step
        self.log = defaultdict(list)
        self.first_generation = {}

    async def __call__(self, input, tools, tool_choice, config):
        from inspect_ai.model import ChatMessageSystem, ModelOutput
        agent = AGENT_CONTEXT.get()
        system = "\n".join(m.text for m in input if isinstance(m, ChatMessageSystem))
        last_user = next((m.text for m in reversed(input) if m.role == "user"), "")
        entry = {"time": time.time(), "messages": len(input), "tools": [t.name for t in tools],
                 "last_user": last_user[-400:]}
        if agent is None:
            self.log["unattributed"].append(entry)
            return ModelOutput.from_content(model=MOCK_MODEL, content="ok")
        if any(m in last_user for m in COMPACTION_MARKERS):
            self.log[agent].append({**entry, "kind": "compaction_request"})
            return ModelOutput.from_content(model=MOCK_MODEL, content="<summary>Validation conversation summary.</summary>")
        if f"You are {agent}," not in system:
            self.log[agent].append({**entry, "kind": "foreign_session", "system_excerpt": system[:300]})
            return ModelOutput.from_content(model=MOCK_MODEL, content=f"{self.foreign_reply}-{agent}")
        self.first_generation.setdefault(agent, entry["time"])
        if any(m in last_user for m in TRUNCATION_NUDGE_MARKERS):
            self.log[agent].append({**entry, "kind": "truncation_nudge"})
        queue = self.scripts.get(agent, [])
        if not queue:
            self.log[agent].append({**entry, "kind": "extra_request"})
            return ModelOutput.from_content(model=MOCK_MODEL, content=self.final)
        name, make = queue.pop(0)
        self.log[agent].append({**entry, "kind": "step", "step": name})
        if self.on_step is not None:
            self.on_step(agent, name)
        output = make(tools)
        if pyinspect.isawaitable(output):
            output = await output
        return output


def with_side_task(inner, side, *, wait_side=False):
    """Run ``side(agent_id, sandbox)`` alongside a (production) solver in the
    same sample: probes while Claude Code is alive, latency sampling, etc."""
    import anyio
    from inspect_ai.solver import solver
    from inspect_ai.util import sandbox

    @solver
    def side_by_side():
        async def execute(state, generate):
            agent_id, box, done = str(state.sample_id), sandbox(), {}

            async def run_side():
                try:
                    done["result"] = await side(agent_id, box)
                except Exception as ex:  # recorded by the caller's side function when relevant
                    done["error"] = repr(ex)

            async with anyio.create_task_group() as group:
                group.start_soon(run_side)
                try:
                    state = await inner(state, generate)
                finally:
                    if not wait_side:
                        group.cancel_scope.cancel()
            return state
        return execute
    return side_by_side()


def sample_summary(sample):
    from inspect_ai.event import CompactionEvent, ModelEvent, ToolEvent
    models = [e for e in sample.events if isinstance(e, ModelEvent)]
    tools = [e for e in sample.events if isinstance(e, ToolEvent)]
    results = {m.tool_call_id: m.text for m in sample.messages if m.role == "tool"}
    return {
        "error": None if sample.error is None else sample.error.message[:1500],
        "limit": None if sample.limit is None else sample.limit.model_dump(mode="json"),
        "model_events": len(models),
        "model_events_with_call_record": sum(1 for e in models if e.call is not None),
        # Bridged MCP and CLI-internal tools emit no Inspect ToolEvents: calls are
        # in model outputs, results in later model inputs and sample messages.
        "tool_calls_in_model_outputs": [tc.function for e in models if e.output is not None
                                        for tc in (e.output.message.tool_calls or [])],
        "inspect_tool_events": [{"function": e.function, "error": None if e.error is None else e.error.message,
                                 "working_time": e.working_time, "result_excerpt": str(e.result)[:300]} for e in tools],
        "compaction_events": [e.model_dump(mode="json") for e in sample.events if isinstance(e, CompactionEvent)],
        "tool_results": results,
    }


def disable_inspect_dotenv():
    """X1: stop Inspect from loading any .env (eval_async would otherwise run
    find_dotenv/load_dotenv from the cwd upwards and pull the host .env into this
    process and every docker/compose/Lean child). Fails closed if the guard is
    missing or raises. Records only booleans and patched module names."""
    result = inspect_adapter.disable_inspect_dotenv()
    if not isinstance(result, dict) or result.get("dotenv_disabled") is not True:
        raise RuntimeError("inspect_adapter.disable_inspect_dotenv did not confirm that .env loading is disabled")
    DOTENV_GUARD.clear()
    DOTENV_GUARD.update(dotenv_disabled=True, credentials_in_environment=bool(result.get("credentials_in_environment")),
                        patched=list(result.get("patched") or []))
    return dict(DOTENV_GUARD)


async def run_eval(task, model, *, agents, log_dir, max_subprocesses=None):
    """The mock-only evaluation shared by the three Phase B scripts. Inspect's .env
    loading is disabled first (X1) and, for the duration of the evaluation,
    ``INSPECT_DISABLE_MODEL_API`` makes Inspect refuse every model API except
    mockllm (restored afterwards)."""
    from inspect_ai import eval_async
    from inspect_ai.log import read_eval_log
    disable_inspect_dotenv()
    options = {} if max_subprocesses is None else {"max_subprocesses": max_subprocesses}
    previous = os.environ.get("INSPECT_DISABLE_MODEL_API")
    os.environ["INSPECT_DISABLE_MODEL_API"] = "1"
    try:
        logs = await eval_async(task, model=model, max_samples=agents, max_sandboxes=agents, max_tasks=1, score=False,
                                log_dir=str(log_dir), log_model_api=True, log_realtime=True, fail_on_error=False,
                                retry_on_error=0, sandbox_prebuilt=True, **options)
    finally:
        if previous is None:
            os.environ.pop("INSPECT_DISABLE_MODEL_API", None)
        else:
            os.environ["INSPECT_DISABLE_MODEL_API"] = previous
    # Resolve attachments so tool-result text (e.g. the 40,000-character read) is compared
    # as content, never as an attachment:// reference.
    return read_eval_log(logs[0].location, resolve_attachments=True)


def validation_ledger(run_dir):
    return BudgetLedger(Path(run_dir) / "validation-budget.sqlite", {VALIDATION_SCOPE: VALIDATION_CAP})


def add_interlock_argument(parser):
    parser.add_argument("--force-while-live", action="store_true",
                        help="debugging only: start although comparator/judge/agent containers are running or left "
                             "over (recorded; the evidence then fails no_conflicting_containers_at_start)")


def interlock_or_refuse(args, agent_image=None, *, check=None):
    """Launch interlock before any container starts (runtime.enforce_launch_interlock):
    the status to record, or None to refuse (the caller exits without evidence)."""
    kwargs = {} if check is None else {"check": check}
    return runtime.enforce_launch_interlock(bool(getattr(args, "force_while_live", False)),
                                            agent_image or getattr(args, "image", None) or runtime.AGENT_IMAGE,
                                            log=lambda message: print(message, file=sys.stderr), **kwargs)


def interlock_check(status):
    """Required: no comparator, leftover judge or other agent container at start. A run
    forced past the interlock records what it saw and fails this check, so its evidence
    can never satisfy a launch gate."""
    status = status or {}
    return check(status.get("blockers") == [] and not status.get("forced"), forced=bool(status.get("forced")),
                 blockers=status.get("blockers"), containers=status.get("containers"),
                 checked_at=status.get("checked_at"),
                 note="runtime.launch_interlock before any container started; --force-while-live is for debugging")


# ---------------------------------------------------------------------------
# End-to-end scenario

AGENTS = ("agent-001", "agent-002", "agent-003")
PERSONAS = {"agent-001": "number_theory", "agent-002": "algebra", "agent-003": "combinatorics"}
LATE_AGENT, LATE_AGENT_DELAY = "agent-002", 20.0
# Compaction-threshold bracketing (informational, recorded): agent-001 reports these
# fabricated prompt sizes in ascending order, one Bash call each. Claude Code checks
# its auto-compaction threshold before its next request, so the lowest level directly
# followed by a compaction request is recorded as ``compaction_threshold_tokens`` (the
# threshold lies above the level before it). The top level is the earlier single
# 195,000-token step. scripts/project_cost.py --compaction-threshold should use it.
COMPACTION_PROBE_LEVELS = (120_000, 150_000, 170_000, 185_000, 195_000)
COMPACTION_PROBE_STEP = "compaction_probe_{}"
BIG_USAGE_TOKENS = COMPACTION_PROBE_LEVELS[-1]  # reported usage meant to trigger Claude Code auto-compaction
TEMPLATE = "all_fermat_squarefree"
NOTEBOOK_LIBRARY_PATH = "notebooks/agent-001/wiki/notes.md"
RELATIVE_NOTEBOOK = "RESEARCH.md"  # publish_notebook resolves relative paths against /workspace
RELATIVE_LIBRARY_PATH = "notebooks/agent-001/workspace/RESEARCH.md"
# One full-range read: exactly MAX_READ_CHARS characters, so offset 0 / limit 40000
# must return the whole file in one tool result.
LARGE_NOTEBOOK_CHARS = MAX_READ_CHARS
LARGE_NOTEBOOK = "/tmp/w/memory/wiki/large-unicode.md"
LARGE_LIBRARY_PATH = "notebooks/agent-001/wiki/large-unicode.md"
# Lean/Mathlib-style symbols, Greek, CJK and astral-plane letters/emoji (two UTF-16
# code units each in the CLI's JavaScript string lengths).
UNICODE_ALPHABET = ("\u2200\u2203\u2115\u2124\u211a\u211d\u2102\u2192\u2194\u2264\u2265\u2260\u2227\u2228\u00ac"
                    "\u03bb\u03a0\u03a3\u2211\u222b\u221a\u221e\u2286\u2282\u2208\u2209\u2205\u2218\u27e8\u27e9"
                    "\u03b1\u03b2\u03b3\u03b4\u03b5\u03b6\u03b8\u207b\u00b9\u2080\u2081\u2082 "
                    "\u6570\u5b66\u8a3c\u660e\u5b9a\u7406\u8865\u9898 "
                    "\U0001d53d\U0001d55c\U0001d4d2\U0001d4aa\U0001d538 \U0001f9ee\u2713 abc xyz 0123 ")
# Claude Code 2.1.296 (static, pinned binary): an MCP result whose size estimate
# (UTF-16 length / 4) exceeds half of MAX_MCP_OUTPUT_TOKENS (default 25,000) is
# token-counted and, above the limit, saved to a file instead of returned inline.
CLI_MCP_OUTPUT_TOKENS = 25_000
TRUNCATIONS_TO_EXHAUST = 4  # Claude Code 2.1.296 recovers 3 consecutive times, then ends the turn
# Deliberately slow Lean file (no imports; sleeps 600 s during elaboration, so it
# uses no CPU while it waits for the 120 s limit). Written with printf (\n escapes).
SLOW_LEAN_PRINTF = "def slowCheck : IO Unit := do\\n  IO.sleep 600000\\n#eval slowCheck\\n"
SLOW_STATUS = "/tmp/w/slow-{tag}.status"
SLOW_OUTPUT = "/tmp/w/slow-{tag}.out"
# Seconds from starting `lake env lean` to the kill that count as "about 120 s".
SLOW_KILL_WINDOW = (LOCAL_LEAN_LIMIT_SECONDS - 5, LOCAL_LEAN_LIMIT_SECONDS + 15)


def slow_compile_command(tag):
    """``lake env lean`` of the slow file; writes "rc=<status> seconds=<elapsed>" when it ends."""
    return (f"s=$(date +%s); lake env lean SlowCheck{tag.upper()}.lean > {SLOW_OUTPUT.format(tag=tag)} 2>&1; "
            f"echo \"rc=$? seconds=$(( $(date +%s) - s ))\" > {SLOW_STATUS.format(tag=tag)}")


def background_lean_steps():
    """Two backgrounded compiles of the slow file: (a) the Bash tool's
    run_in_background (no Bash timeout applies), (b) detached with setsid/nohup so
    no harness process-group kill reaches it. Only the lake wrapper's
    `timeout -s KILL 120` can stop either within the run."""
    write = lambda tag: f"cd /workspace && printf '{SLOW_LEAN_PRINTF}' > SlowCheck{tag.upper()}.lean"  # noqa: E731
    task_a = f"{write('a')} && rm -f {SLOW_STATUS.format(tag='a')} && {slow_compile_command('a')}"
    # Only the fully redirected setsid command is backgrounded (after `;`), so no
    # background process holds the Bash tool's output pipe open.
    detached_b = (f"{write('b')} && rm -f {SLOW_STATUS.format(tag='b')}; "
                  f"setsid nohup sh -c {shlex.quote(slow_compile_command('b'))} > /dev/null 2>&1 < /dev/null & "
                  "echo BACKGROUND_STARTED")
    return [("bash_background_lean_task", bash(task_a, "a2-bg-task", description="Check a file", background=True)),
            ("bash_background_lean_detached", bash(detached_b, "a2-bg-detached", description="Check a file"))]


def background_lean_status_command():
    """Waits (< 120 s) for both slow compiles to end, then reports their status, any
    surviving slow Lean process, and how lake/lean resolve for the Bash tool and
    inside `lake env` (Lake prepends the toolchain's bin directory)."""
    statuses = " ".join(f"[ -e {SLOW_STATUS.format(tag=t)} ] &&" for t in ("a", "b"))
    report = (f"for x in a b; do echo \"SLOW_$x status=$(cat /tmp/w/slow-$x.status 2>/dev/null || echo missing) "
              f"notice=$(grep -c '{KILL_NOTICE}' /tmp/w/slow-$x.out 2>/dev/null || true)\"; done")
    return (f"for i in $(seq 1 100); do {statuses} break; sleep 1; done; {report}; "
            "echo \"LEAN_PROCESSES $(ps -ww -eo args | grep -c '[S]lowCheck')\"; "
            "echo \"WHICH_LAKE $(command -v lake)\"; echo \"WHICH_LEAN $(command -v lean)\"; "
            "echo \"LAKE_ENV_LEAN $(cd /workspace && lake env sh -c 'command -v lean' 2>&1 | tail -n 1)\"")


# Counts `sleep 150` processes still alive after the Bash tool reported its timeout
# (the bracket keeps this command's own shell out of the count).
TIMED_OUT_PROCESS_CHECK = "echo \"SLEEP_PROCESSES $(ps -ww -eo args | grep -c '^sleep 15[0]')\""


def unicode_notebook(chars, alphabet):
    """Deterministic Unicode-heavy markdown of exactly ``chars`` code points (79-character
    lines); also sent verbatim into the container, which writes the same text."""
    lines, size, k = [], 0, 0
    while size < chars:
        line = "".join(alphabet[(7 * k + j) % len(alphabet)] for j in range(79)) + "\n"
        lines.append(line)
        size += len(line)
        k += 1
    return "".join(lines)[:chars]


def large_notebook_text():
    return unicode_notebook(LARGE_NOTEBOOK_CHARS, UNICODE_ALPHABET)


def large_notebook_command():
    """ASCII-only shell command that writes ``large_notebook_text()`` inside the container."""
    script = (pyinspect.getsource(unicode_notebook) + "\n"
              f"open({LARGE_NOTEBOOK!r}, 'w', encoding='utf-8')"
              f".write(unicode_notebook({LARGE_NOTEBOOK_CHARS}, {ascii(UNICODE_ALPHABET)}))\n")
    return (f"mkdir -p {LARGE_NOTEBOOK.rsplit('/', 1)[0]} && python3 -I -c {shlex.quote(script)} && "
            f"wc -c {LARGE_NOTEBOOK}")


def utf16_length(text_value):
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text_value)


def scenario_scripts(run_dir, background_lean=False):
    from inspect_ai.model import ModelOutput, ModelUsage
    conference_events = Path(run_dir) / "conference" / "events.jsonl"

    def usage_step(tokens):
        def make(tools):
            output = ModelOutput.for_tool_call(model=MOCK_MODEL, tool_name="Bash", tool_call_id=f"a1-usage-{tokens}",
                                               tool_arguments={"command": f"echo usage-{tokens}",
                                                               "description": "Print a marker"})
            output.usage = ModelUsage(input_tokens=tokens, output_tokens=50, total_tokens=tokens + 50)
            # Fabricated tokens would exceed the byte-bound reservation (an estimate
            # violation blocks the scope); report a provider cost instead.
            output.metadata = {"openrouter_usage": {"cost": 0.0001}}
            return output
        return make

    def truncated_call(tools):
        output = ModelOutput.for_tool_call(model=MOCK_MODEL, tool_name="Bash", tool_call_id="a1-trunc",
                                           tool_arguments={"command": "echo SHOULD-NOT-RUN > /workspace/truncated.txt",
                                                           "description": "Write a file"},
                                           content="Partial plan before the output limit ...")
        output.choices[0].stop_reason = "max_tokens"
        return output

    lean_check = (f"cd /workspace && cp problems/{TEMPLATE}.lean Check.lean && start=$(date +%s) && "
                  "(lake env lean Check.lean; echo LEAN_EXIT=$?) 2>&1 | tail -n 8; echo ELAPSED=$(( $(date +%s) - start ))")
    notes = ("mkdir -p /tmp/w/memory/wiki && printf '# Notes\\nvalidation notebook\\n' > /tmp/w/memory/wiki/notes.md && "
             f"echo journal >> /workspace/RESEARCH.md && cp /workspace/problems/{TEMPLATE}.lean /workspace/Attempt.lean && "
             "ln -sf /etc/passwd /workspace/link.lean && echo ok")
    agent_001 = [
        ("bash_lean_check", bash(lean_check, "a1-lean", 120000, "Check a problem template")),
        ("post_intent", conf_call("post_intent", {"intent_type": "exploring", "message": "validation post",
                                                  "tags": ["validation"]}, "a1-post")),
        ("send_direct_message", conf_call("send_direct_message", {"agent_id": "agent-002",
                                                                  "message": "validation direct message"}, "a1-dm")),
        ("bash_write_notes", bash(notes, "a1-notes", description="Write notes")),
        ("submit_template", conf_call("submit_solution", {"problem_id": TEMPLATE, "file_path": "/workspace/Attempt.lean"},
                                      "a1-submit")),
        ("submit_outside_root", conf_call("submit_solution", {"problem_id": TEMPLATE, "file_path": "/etc/passwd"},
                                          "a1-escape")),
        ("submit_symlink", conf_call("submit_solution", {"problem_id": TEMPLATE, "file_path": "/workspace/link.lean"},
                                     "a1-symlink")),
        ("publish_notebook", conf_call("publish_notebook", {"file_path": "/tmp/w/memory/wiki/notes.md"}, "a1-publish")),
        ("read_library_file", conf_call("read_library_file", {"path": NOTEBOOK_LIBRARY_PATH}, "a1-read")),
        ("publish_notebook_relative", conf_call("publish_notebook", {"file_path": RELATIVE_NOTEBOOK}, "a1-publish-rel")),
        ("bash_write_large_notebook", bash(large_notebook_command(), "a1-large", description="Write a long note")),
        ("publish_large_notebook", conf_call("publish_notebook", {"file_path": LARGE_NOTEBOOK}, "a1-publish-large")),
        ("read_large_notebook", conf_call("read_library_file", {"path": LARGE_LIBRARY_PATH, "offset": 0,
                                                                 "limit": LARGE_NOTEBOOK_CHARS}, "a1-read-large")),
        ("get_bulletin_board", conf_call("get_bulletin_board", {}, "a1-board")),
        ("disallowed_webfetch", tool_call("WebFetch", {"url": "https://example.com", "prompt": "x"}, "a1-webfetch")),
        ("disallowed_agent", tool_call("Agent", {"description": "x", "prompt": "x", "subagent_type": "general-purpose"},
                                       "a1-agent")),
        *[(COMPACTION_PROBE_STEP.format(tokens), usage_step(tokens)) for tokens in COMPACTION_PROBE_LEVELS],
        ("after_compaction", bash("echo after-compaction", "a1-after", description="Print a marker")),
        ("truncated_tool_call", truncated_call),
        ("check_truncated_not_run", bash("test -e /workspace/truncated.txt && echo PRESENT || echo ABSENT", "a1-check",
                                         description="Check a file")),
        ("final", text("Validation script complete.")),
    ]
    # With background_lean, the slow compiles start first and run while the Bash
    # clamp is exercised (≈120 s); their status is read afterwards (< 120 s wait).
    agent_002 = background_lean_steps() if background_lean else []
    agent_002 += [
        ("bash_time_limit", bash("sleep 150; echo SLEEP_FINISHED", "a2-sleep", 600000, "Long command")),
        ("bash_timed_out_process_check", bash(TIMED_OUT_PROCESS_CHECK, "a2-sleep-gone",
                                              description="List processes")),
    ]
    if background_lean:
        agent_002.append(("bash_background_lean_status", bash(background_lean_status_command(), "a2-bg-status",
                                                              120000, "Check results")))
    agent_002 += [
        ("post_intent", conf_call("post_intent", {"intent_type": "building", "message": "second validation post"},
                                  "a2-post")),
        # Waits until agent-001's message exists (its Lean check comes first).
        ("get_messages", after_event(conference_events, "dm_sent", conf_call("get_messages", {}, "a2-messages"))),
        ("list_library", conf_call("list_library", {}, "a2-library")),
        ("final", text("Validation script complete.")),
    ]
    agent_003 = [("bash_hello", bash("echo hello", "a3-hello", description="Print a marker"))]
    agent_003 += [(f"truncated_text_{i + 1}", text(f"Partial reasoning {i + 1} ...", stop_reason="max_tokens"))
                  for i in range(TRUNCATIONS_TO_EXHAUST)]
    agent_003 += [("after_exhaustion", text("Unexpected continuation."))]
    return {"agent-001": agent_001, "agent-002": agent_002, "agent-003": agent_003}


def compaction_bracket(entries, levels=COMPACTION_PROBE_LEVELS):
    """The auto-compaction threshold bracket from one agent's ScriptedModel log.

    A compaction request counts for a probe level only when it is the next request
    after that level's step. ``compaction_threshold_tokens`` is the lowest such level
    (None if the CLI compacted after none); ``highest_level_without_compaction`` is the
    largest level served below it that was not followed by one, so the CLI's trigger
    lies in (that level, threshold] for the reported prompt size.
    """
    names = {COMPACTION_PROBE_STEP.format(tokens): tokens for tokens in levels}
    served, compacted, pending = [], [], None
    for entry in entries or []:
        kind = entry.get("kind")
        if kind == "compaction_request":
            if pending is not None and pending not in compacted:
                compacted.append(pending)
            pending = None
        elif kind in ("step", "extra_request", "truncation_nudge"):
            pending = names.get(entry.get("step")) if kind == "step" else None
            if pending is not None:
                served.append(pending)
    threshold = min(compacted) if compacted else None
    below = [tokens for tokens in served if tokens not in compacted and (threshold is None or tokens < threshold)]
    return {"compaction_threshold_tokens": threshold,
            "highest_level_without_compaction": max(below) if below else None,
            "levels": list(levels), "levels_served": served, "compacted_after_levels": compacted,
            "complete": served == list(levels)}


def validation_config(args):
    return {"agent_image": args.image, "lean_image": args.lean_image, "compilation_timeout_seconds": 120,
            "judge_memory": args.judge_memory, "judge_cpus": 1, "judge_max_concurrent": args.judge_max_concurrent,
            "agent_memory": args.agent_memory, "agent_cpus": getattr(args, "agent_cpus", None),
            "system_prompt_mode": "append", "claude_auto_memory": True}


async def run(args, stamp):
    from inspect_ai.model import get_model
    from research_swarm.prompts import INITIAL_MESSAGE, build_prompt
    from research_swarm.tools import TOOL_DEFINITIONS
    config = validation_config(args)
    images = runtime.pin_images(config)  # refuses when a tag does not resolve, as run_pilot does
    run_dir = (Path(getattr(args, "runs_dir", None) or ROOT / "runs") / f"validation-runtime-{stamp}").resolve()
    run_dir.mkdir(parents=True)
    record = runtime.make_recorder(run_dir / "runtime.jsonl")
    record("images_pinned", None, images)
    listing, task_files = runtime.export_public_tasks(ROOT)
    warm_up = None
    if not getattr(args, "no_warm_up", False):
        warm_up = await asyncio.to_thread(runtime.warm_up_lean, images["lean"]["id"], record,
                                          **runtime.warm_up_options(config))
    conference, _, observer = runtime.make_conference(ROOT, run_dir, list(AGENTS), config, record,
                                                      lean_image=images["lean"]["id"])
    ledger = validation_ledger(run_dir)
    settings = ProviderSettings(pricing_verified=True)
    barrier = runtime.StartBarrier(len(AGENTS), record)
    clock = runtime.RunClock(barrier)
    archive = InstructionArchive(run_dir / "instructions", record)
    arrivals = {}

    def filter_for(agent_id):
        inner = budget_filter(agent_id, ledger, settings, barrier, record, VALIDATION_SCOPE, archive, clock.stop)
        return attributed(agent_id, inner, first_delay=LATE_AGENT_DELAY if agent_id == LATE_AGENT else 0.0,
                          arrivals=arrivals)

    compose = runtime.make_compose(run_dir, config, images["agent"]["id"])
    binding = compose_binding(compose, config, images["agent"]["id"])
    record("compose_bound", None, binding)
    solver = runtime.peer_solver(conference=conference, definitions=TOOL_DEFINITIONS, personas=PERSONAS,
                                 prompt_for=lambda a, p: build_prompt(a, p, listing), filter_for=filter_for,
                                 run_dir=run_dir, record=record, clock=clock)
    task = runtime.build_task(agent_ids=list(AGENTS), files=runtime.agent_files(task_files), solver=solver,
                              compose=compose, metadata={"condition": "unpaid-runtime-validation"},
                              initial_message=INITIAL_MESSAGE)
    # The CLI parser always enables the background compiles (they need real Lean and
    # the image's wrappers); callers that build ``args`` themselves, such as the
    # offline rehearsal with a stand-in CLI, leave them out, and the check is then
    # recorded as not run (the launch gate requires it by name).
    background_lean = bool(getattr(args, "background_lean_check", False))
    scripted = ScriptedModel(scenario_scripts(run_dir, background_lean=background_lean))
    model = get_model(MOCK_MODEL, custom_outputs=scripted)
    monitor = asyncio.create_task(clock.monitor(conference, record, startup_timeout=600,
                                                max_run_seconds=args.max_seconds, post_depletion_seconds=600))
    try:
        log = await run_eval(task, model, agents=len(AGENTS), log_dir=run_dir / "inspect")
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await asyncio.to_thread(conference.close)
    return {"run_dir": run_dir, "log": log, "scripted": scripted, "arrivals": arrivals, "ledger": ledger,
            "observer_wired": observer is not None, "config": config, "images": images, "warm_up": warm_up,
            "compose": binding, "background_lean_check": background_lean,
            "runtime_events": read_jsonl(run_dir / "runtime.jsonl"),
            "conference_events": read_jsonl(run_dir / "conference" / "events.jsonl")}


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def marker_value(text_value, name):
    """``<NAME> <value>`` line in a tool result (last occurrence), or None."""
    found = re.findall(rf"^{re.escape(name)} ?(.*)$", text_value or "", re.M)
    return found[-1].strip() if found else None


def marker_int(text_value, name):
    value = marker_value(text_value, name)
    return int(value) if value is not None and value.isdigit() else None


def parse_slow_status(text_value, tag):
    """One slow compile's outcome from background_lean_status_command output."""
    match = re.search(rf"^SLOW_{tag} status=(?:rc=(\d+) seconds=(\d+)|(missing)) notice=(\d*)\s*$",
                      text_value or "", re.M)
    if not match:
        return {"reported": False}
    rc, seconds, missing, notice = match.groups()
    return {"reported": True, "ended": missing is None, "rc": None if rc is None else int(rc),
            "seconds": None if seconds is None else int(seconds), "kill_notice": int(notice or 0)}


def killed_by_limit(status):
    """Ended by the wrapper's `timeout -v -s KILL 120`: exit 137, the timeout notice
    (an OOM kill is also 137 but prints no notice) and about 120 s elapsed."""
    low, high = SLOW_KILL_WINDOW
    return bool(status.get("ended") and status.get("rc") == 137 and status.get("kill_notice", 0) >= 1
                and status.get("seconds") is not None and low <= status["seconds"] <= high)


def background_lean_check(result_text, *, ran, task_result=None, detached_result=None):
    """RT-2: backgrounded compiles are killed at about 120 s by the lake wrapper.

    (b) is detached with setsid/nohup, so only the wrapper can end it: it must show
    the limit kill. (a) runs through the Bash tool's run_in_background; it must
    show the limit kill too, or have ended without a status (the harness ended it
    earlier), with no slow Lean process left either way. ``lake``/``lean`` must
    resolve to the wrappers in the Bash tool's environment.
    """
    if not ran:
        return check(False, required=False, skipped=True,
                     note="Background compile scenario not run (offline rehearsal or --skip-background-lean-check); "
                          "evidence without it cannot satisfy the launch gate.")
    a, b = parse_slow_status(result_text, "a"), parse_slow_status(result_text, "b")
    survivors = marker_int(result_text, "LEAN_PROCESSES")
    which = {"lake": marker_value(result_text, "WHICH_LAKE"), "lean": marker_value(result_text, "WHICH_LEAN")}
    a_outcome = ("killed_at_limit" if killed_by_limit(a) else
                 "ended_without_status" if a.get("reported") and not a.get("ended") and survivors == 0 else
                 "not_killed_at_limit")
    passed = (killed_by_limit(b) and a_outcome != "not_killed_at_limit" and survivors == 0
              and which == LEAN_WRAPPERS)
    return check(passed, run_in_background_task=a, run_in_background_outcome=a_outcome, detached_setsid=b,
                 surviving_slow_lean_processes=survivors, path_lookup=which, expected_wrappers=LEAN_WRAPPERS,
                 lake_env_resolves_lean=marker_value(result_text, "LAKE_ENV_LEAN"),
                 kill_window_seconds=list(SLOW_KILL_WINDOW), status_result=(result_text or "")[-900:],
                 task_start_result=(task_result or "")[:300], detached_start_result=(detached_result or "")[:300],
                 note="Invoking the elan/toolchain binaries by absolute path bypasses the wrappers (docs/runtime.md).")


def build_checks(result):
    log, scripted, run_dir = result["log"], result["scripted"], result["run_dir"]
    events, conf_events = result["runtime_events"], result["conference_events"]
    kinds = defaultdict(list)
    for event in events:
        kinds[event["kind"]].append(event)
    samples = {str(s.id): sample_summary(s) for s in log.samples}
    r = {a: samples.get(a, {}).get("tool_results", {}) for a in AGENTS}
    exits = {e["agent_id"]: e["payload"] for e in kinds["agent_exit"]}
    steps = {a: [x.get("step") or x.get("kind") for x in scripted.log.get(a, [])] for a in AGENTS}
    instructions = []
    for path in sorted((run_dir / "instructions").glob("*.json")):
        data = json.loads(path.read_text())
        blocks = data["system_blocks"]
        instructions.append({"agent_id": data["agent_id"], "file": rel(path), "system_blocks": len(blocks),
                             "system_chars": sum(len(b) for b in blocks), "tools": data["tools"],
                             "tool_definitions": len(data.get("tool_definitions", [])),
                             "appended_prompt_present": any(f"You are {data['agent_id']}," in b for b in blocks),
                             "claude_code_builtin_prompt_present": any("Claude Code" in b or "Claude Agent SDK" in b
                                                                        for b in blocks)})
    main = [i for i in instructions if i["appended_prompt_present"]]
    offered = sorted({t for i in main for t in i["tools"]})
    disallowed_offered = [t for t in offered if t in DISALLOWED_TOOLS or t.split("__")[-1] in DISALLOWED_TOOLS]
    posts = [e for e in conf_events if e.get("kind") == "board_post"]
    dms = [e for e in conf_events if e.get("kind") == "dm_sent"]
    judged = [e for e in conf_events if e.get("kind") == "submission_judged"]
    received = [e for e in conf_events if e.get("kind") == "submission_received"]
    snapshots = [e["payload"] for e in kinds["file_snapshot"]]
    submit = _json(r["agent-001"].get("a1-submit"))
    messages = _json(r["agent-002"].get("a2-messages"))
    common = kinds["common_start"][0]["timestamp"] if kinds["common_start"] else None
    late = result["arrivals"].get(LATE_AGENT)
    a1_first = scripted.first_generation.get("agent-001")
    # Seconds from the request that issued `sleep 150` to the next scripted request.
    a2_steps = [x for x in scripted.log.get("agent-002", []) if x.get("kind") == "step"]
    sleep_index = next((i for i, x in enumerate(a2_steps) if x.get("step") == "bash_time_limit"), None)
    sleep_seconds = (a2_steps[sleep_index + 1]["time"] - a2_steps[sleep_index]["time"]
                     if sleep_index is not None and sleep_index + 1 < len(a2_steps) else None)
    sleep_survivors = marker_int(r["agent-002"].get("a2-sleep-gone"), "SLEEP_PROCESSES")
    binding = result.get("compose") or {}
    admitted, finished = kinds["inference_admitted"], kinds["inference_finished"]
    ledger = result["ledger"].snapshot()[VALIDATION_SCOPE]
    a1_trunc = steps["agent-001"].index("truncated_tool_call") if "truncated_tool_call" in steps["agent-001"] else None
    bracket = compaction_bracket(scripted.log.get("agent-001", []))
    a3_nudges = sum(1 for x in scripted.log.get("agent-003", []) if x.get("kind") == "truncation_nudge")
    seqs = [e.get("seq") for e in conf_events]
    errors_ok = all(not samples.get(a, {}).get("error") for a in ("agent-001", "agent-002")) and (
        not samples.get("agent-003", {}).get("error")
        or exits.get("agent-003", {}).get("status") == "error_after_truncation")
    lean_result = r["agent-001"].get("a1-lean", "")
    reads = [e["payload"] for e in kinds["sandbox_file_read"]]
    relative = _json(r["agent-001"].get("a1-publish-rel"))
    large_raw = r["agent-001"].get("a1-read-large", "")
    large = _json(large_raw)
    expected_large = large_notebook_text()
    large_reads = [e.get("payload", {}) for e in conf_events
                   if e.get("kind") == "library_read" and e.get("payload", {}).get("path") == LARGE_LIBRARY_PATH]
    cli_estimate = round(utf16_length(large_raw) / 4)
    images = result.get("images") or {}
    compose_text = (Path(run_dir) / "compose.yaml").read_text() if (Path(run_dir) / "compose.yaml").exists() else ""
    warm_up = result.get("warm_up")
    checks = {
        "sample_errors": check(errors_ok and len(samples) == len(AGENTS),
                               errors={a: s.get("error") for a, s in samples.items()},
                               note="agent-003 may error only as error_after_truncation (CLI exit after recovery exhaustion)"),
        "claude_code_executed_bash_lean": check("LEAN_EXIT=" in lean_result and "Provide a complete proof" in lean_result,
                                                evidence=lean_result[-700:]),
        "bash_time_limited_to_120s": check("SLEEP_FINISHED" not in r["agent-002"].get("a2-sleep", "x SLEEP_FINISHED")
                                           and sleep_seconds is not None and sleep_seconds < 150
                                           and sleep_survivors == 0,
                                           seconds_between_requests=sleep_seconds,
                                           timed_out_process_survivors=sleep_survivors,
                                           evidence=r["agent-002"].get("a2-sleep", "")[-400:],
                                           process_check=r["agent-002"].get("a2-sleep-gone", "")[-300:],
                                           note="The timed-out `sleep 150` must also be gone (checked with ps "
                                                "right after the tool reported the timeout)."),
        "background_lean_killed_at_120s": background_lean_check(
            r["agent-002"].get("a2-bg-status", ""), ran=bool(result.get("background_lean_check")),
            task_result=r["agent-002"].get("a2-bg-task"), detached_result=r["agent-002"].get("a2-bg-detached")),
        "identity_bound_board_posts": check(
            {e.get("agent_id") for e in posts} == {"agent-001", "agent-002"}
            and all(_json(r[a].get(c)).get("ok") for a, c in (("agent-001", "a1-post"), ("agent-002", "a2-post"))),
            post_events=[{k: e.get(k) for k in ("seq", "kind", "agent_id", "tool")} for e in posts]),
        "cross_agent_direct_message": check(
            any(e.get("agent_id") == "agent-001" and e.get("payload", {}).get("recipient") == "agent-002" for e in dms)
            and any(m.get("from_agent_id") == "agent-001" and "validation direct message" in m.get("message", "")
                    for m in messages.get("messages", [])),
            dm_events=len(dms), get_messages_result=r["agent-002"].get("a2-messages", "")[:600]),
        "sandbox_file_submission": check(
            any(e["payload"]["path"] == "/workspace/Attempt.lean" for e in kinds["sandbox_file_read"])
            and submit.get("submission_id"), submit_result=r["agent-001"].get("a1-submit", "")[:900]),
        "real_judge_rejects_unmodified_template": check(
            submit.get("accepted") is False and submit.get("outcome") == "rejected" and submit.get("stage") == "compilation"
            and submit.get("compile_status") == "error" and "Provide a complete proof" in submit.get("compiler_output", ""),
            judge_events=[{k: e.get("payload", {}).get(k) for k in ("outcome", "stage", "compile_status", "judge_seconds")}
                          for e in judged]),
        "sandbox_file_outside_root_rejected": check(
            _json(r["agent-001"].get("a1-escape")).get("error") == "path_outside_allowed_roots"
            and _json(r["agent-001"].get("a1-symlink")).get("error") == "path_outside_allowed_roots"
            and sum(1 for e in received if e.get("agent_id") == "agent-001") == 1,
            outside_root=r["agent-001"].get("a1-escape", "")[:300], symlink=r["agent-001"].get("a1-symlink", "")[:300],
            rejections=[e["payload"] for e in kinds["sandbox_file_rejected"]]),
        "publish_notebook": check(_json(r["agent-001"].get("a1-publish")).get("path") == NOTEBOOK_LIBRARY_PATH,
                                  result=r["agent-001"].get("a1-publish", "")[:300]),
        "read_library_file": check("validation notebook" in _json(r["agent-001"].get("a1-read")).get("content", ""),
                                   result=r["agent-001"].get("a1-read", "")[:400]),
        "publish_notebook_relative_path": check(
            relative.get("ok") is True and relative.get("path") == RELATIVE_LIBRARY_PATH
            and any(p.get("path") == RELATIVE_NOTEBOOK and p.get("resolved") == "/workspace/" + RELATIVE_NOTEBOOK
                    for p in reads),
            result=r["agent-001"].get("a1-publish-rel", "")[:300],
            sandbox_reads=[p for p in reads if p.get("tool") == "publish_notebook"]),
        "read_library_file_large_unicode_full_range": check(
            large.get("content") == expected_large and large.get("returned_chars") == LARGE_NOTEBOOK_CHARS
            and large.get("has_more") is False
            and any(p.get("complete") and p.get("returned_chars") == LARGE_NOTEBOOK_CHARS for p in large_reads),
            requested={"offset": 0, "limit": LARGE_NOTEBOOK_CHARS}, expected_chars=len(expected_large),
            expected_utf8_bytes=len(expected_large.encode("utf-8")), notebook_byte_limit=MAX_NOTEBOOK_BYTES,
            publish_result=r["agent-001"].get("a1-publish-large", "")[:300],
            received_chars=len(large.get("content") or ""), returned_chars=large.get("returned_chars"),
            has_more=large.get("has_more"), tool_result_chars=len(large_raw),
            tool_result_utf16_units=utf16_length(large_raw), cli_size_estimate_tokens=cli_estimate,
            cli_inline_without_token_count=cli_estimate <= CLI_MCP_OUTPUT_TOKENS // 2,
            tool_result_head=large_raw[:200], tool_result_tail=large_raw[-300:], library_read_events=large_reads,
            note="The tool result must reach the model untruncated (Claude Code saves an MCP result it judges "
                 "larger than MAX_MCP_OUTPUT_TOKENS to a file instead)."),
        "compose_uses_pinned_image_id": check(
            bool(images.get("agent", {}).get("id")) and f"image: {images['agent']['id']}" in compose_text,
            images=images),
        "compose_matches_fingerprint": check(binding.get("matches") is True, compose=binding,
                                             note="X3: compose_sha256 is the file the agents ran; it must equal "
                                                  "runtime.compose_fingerprint(config, agent image ID)."),
        "lean_warm_up": check(bool(warm_up and warm_up.get("ok")), required=False, warm_up=warm_up,
                              note="Not a gate (an optimization); records VM uptime and page cache before/after."),
        "barrier_released_only_after_late_agent": check(
            bool(common and late and a1_first and a1_first >= late and common >= late),
            arrivals=result["arrivals"], common_start=common, agent_001_first_generation=a1_first,
            agent_ready_events=[(e["agent_id"], e["timestamp"]) for e in kinds["agent_ready"]]),
        "inspect_logs_capture_model_and_tool_calls": check(
            all(samples.get(a, {}).get("model_events", 0) > 0 for a in AGENTS)
            and any(f.endswith("post_intent") for f in samples.get("agent-001", {}).get("tool_calls_in_model_outputs", []))
            and "a1-post" in r["agent-001"] and "a1-lean" in r["agent-001"],
            model_events={a: s.get("model_events") for a, s in samples.items()},
            model_events_with_raw_call={a: s.get("model_events_with_call_record") for a, s in samples.items()}),
        "file_snapshots": check(
            any(p["path"].endswith("/workspace/Check.lean") for p in snapshots)
            and any(p["path"].endswith("wiki/notes.md") for p in snapshots)
            and any(p["path"].endswith(".jsonl") for p in snapshots),
            paths=sorted({p["path"] for p in snapshots}),
            transcript_modes=sorted({p.get("mode") for p in snapshots if p["path"].endswith(".jsonl")})),
        "effective_instructions_archived": check(
            all(any(i["agent_id"] == a and i["claude_code_builtin_prompt_present"] and i["tool_definitions"]
                    for i in main) for a in AGENTS),
            archives=instructions, note="append mode: the conference prompt follows Claude Code's built-in prompt"),
        "disallowed_tools_absent": check(
            bool(offered) and not disallowed_offered and any(t.startswith("mcp__conference__") for t in offered)
            and "a1-webfetch" in r["agent-001"] and "a1-agent" in r["agent-001"],
            offered_tools=offered, disallowed_offered=disallowed_offered,
            webfetch_call_result=r["agent-001"].get("a1-webfetch", "")[:300],
            agent_call_result=r["agent-001"].get("a1-agent", "")[:300]),
        "budget_filter_reserve_settle": check(
            ledger["unresolved_requests"] == 0 and len(admitted) > 0 and len(admitted) == len(finished)
            and not ledger["violation"],
            ledger=ledger, ledger_path=rel(Path(run_dir) / "validation-budget.sqlite"), scope=VALIDATION_SCOPE,
            admitted=len(admitted), finished=len(finished),
            bases=sorted({e["payload"]["basis"] for e in finished if e["payload"]["basis"]})),
        "truncated_tool_call_not_executed_and_session_continues": check(
            "a1-trunc" not in r["agent-001"] and "ABSENT" in r["agent-001"].get("a1-check", "")
            and a1_trunc is not None and "truncation_nudge" in steps["agent-001"][a1_trunc + 1:]
            and exits.get("agent-001", {}).get("status") == "harness_completed",
            requests_after_truncation=steps["agent-001"][a1_trunc + 1:] if a1_trunc is not None else None,
            truncated_events=[e["payload"] for e in kinds["truncated_response"] if e["agent_id"] == "agent-001"]),
        "truncation_exhaustion_not_voluntary_exit": check(
            exits.get("agent-003", {}).get("status") in ("harness_completed_after_truncation", "error_after_truncation"),
            agent_003_exit=exits.get("agent-003"), recovery_nudges_seen=a3_nudges, agent_003_requests=steps["agent-003"]),
        "voluntary_exit_recorded": check(exits.get("agent-002", {}).get("status") == "harness_completed",
                                         exits={a: (p.get("status"), p.get("harness_status")) for a, p in exits.items()}),
        "compaction_triggered": check(
            bool(samples.get("agent-001", {}).get("compaction_events"))
            or "compaction_request" in steps["agent-001"], required=False,
            compaction_events=samples.get("agent-001", {}).get("compaction_events", []),
            note="Fabricated usage; the live threshold depends on the context window Claude Code assumes for the "
                 "presented model id and must be observed on the real route."),
        "compaction_threshold_bracketed": check(
            bracket["compaction_threshold_tokens"] is not None and bracket["complete"], required=False, **bracket,
            note="Informational, recorded as compaction_threshold_tokens: the lowest fabricated prompt size after "
                 "which the pinned Claude Code sent its auto-compaction request (scripts/project_cost.py "
                 "--compaction-threshold). Assumes the CLI gives the live route's presented model name the same "
                 "default context window as the mock's (neither is a known Claude model id)."),
        "conference_event_log_ordered": check(seqs == sorted(seqs) and len(set(seqs)) == len(seqs),
                                              events=len(conf_events)),
        "evaluator_queue_observer_wired": check(result["observer_wired"], required=False,
                                                note="I2 make_queue_observer; post-run evaluation can also read "
                                                     "conference SQLite"),
    }
    details = {"samples": {a: {k: v for k, v in s.items() if k != "tool_results"} for a, s in samples.items()},
               "tool_results": {a: {k: v[:600] for k, v in r[a].items()} for a in AGENTS},
               "request_sequences": steps, "unattributed_requests": scripted.log.get("unattributed", []),
               "runtime_event_counts": {k: len(v) for k, v in kinds.items()},
               "compaction_threshold_tokens": bracket["compaction_threshold_tokens"],
               "compaction_threshold_bracket": bracket}
    return checks, details


def parse_args(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=str(DEFAULT_CONFIG),
                     help="pilot config supplying the image tags and agent memory/CPUs (compose_sha256)")
    known, _ = pre.parse_known_args(argv)
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], parents=[pre])
    parser.add_argument("--image")
    parser.add_argument("--lean-image")
    parser.add_argument("--agent-memory")
    parser.add_argument("--agent-cpus", type=optional_float)
    parser.add_argument("--judge-memory", default="2g")
    parser.add_argument("--judge-max-concurrent", type=int, default=1)
    parser.add_argument("--max-seconds", type=int, default=1500)
    parser.add_argument("--output-dir", default=str(VALIDATION_DIR))
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"), help="researcher-side run artifacts")
    parser.add_argument("--no-warm-up", action="store_true", help="skip the researcher-side Lean warm-up")
    parser.add_argument("--skip-background-lean-check", dest="background_lean_check", action="store_false",
                        help="debugging only: omit the backgrounded slow compiles (the evidence then fails the gate)")
    add_interlock_argument(parser)
    parser.set_defaults(**pilot_runtime_defaults(known.config))
    return parser.parse_args(argv)


def main():
    args = parse_args()
    interlock = interlock_or_refuse(args)  # before any container (warm-up, judge, agents) starts
    if interlock is None:
        return 2
    stamp = utc_stamp()
    result = asyncio.run(run(args, stamp))
    checks, details = build_checks(result)
    checks["no_conflicting_containers_at_start"] = interlock_check(interlock)
    images = result["images"]
    path, report = write_evidence(
        "runtime-e2e", "gdm-runtime-e2e/1", checks, {"agent": images["agent"]["id"], "lean": images["lean"]["id"]},
        stamp, args.output_dir, kind="unpaid end-to-end runtime validation (mockllm scripted outputs)",
        versions={**{k: v["actual"] for k, v in installed_versions().items()}, "claude_code": CLAUDE_CODE_VERSION},
        run_dir=rel(result["run_dir"]), inspect_log=Path(result["log"].location).name,
        config=result["config"], images=images, claude_code_env=claude_code_env(True),
        compose_sha256=result["compose"]["compose_sha256"], compose=result["compose"],
        background_lean_check=result["background_lean_check"], lean_wrappers=LEAN_WRAPPERS,
        disallowed_tools=list(DISALLOWED_TOOLS), interlock=interlock,
        budget={"ledger": "throwaway", "scope": VALIDATION_SCOPE, "notional_cap_usd": VALIDATION_CAP}, **details,
        limitations=[
            "mockllm scripted outputs establish harness, bridge, tool, logging and isolation plumbing only; they say "
            "nothing about DeepSeek V4.1's tool-call reliability, reasoning replay through the Anthropic dialect, "
            "theorem-proving ability, latency, or OpenRouter billing fields.",
            "Compaction was provoked by fabricated usage counts (COMPACTION_PROBE_LEVELS); compaction_threshold_tokens "
            "brackets the pinned CLI's trigger for a reported prompt size, assuming the live route's presented model "
            "name gets the same default context window. The live compatibility check's compaction_observed stays "
            "informational.",
            "Truncation was simulated with stop_reason=max_tokens; a live truncated reasoning turn may also carry "
            "reasoning content, which changes Claude Code's recovery branch."])
    print(json.dumps({k: (v["passed"], v["required"]) for k, v in checks.items()}, indent=2))
    print(f"wrote {rel(path)}; all_required_passed={report['all_required_passed']}")
    return 0 if report["all_required_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
