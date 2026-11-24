"""Agentic monitor arm AG (docs/plan.md, "Agentic monitor arm AG (owner decisions, November 23, 2026)").

One monitor per solver, external to the swarm and undisclosed: ``agmon-NNN`` watches ``agent-NNN``. Each
monitor is one more Inspect sample (its own container, like every participant) running genuine Claude Code
on the solvers' model, route and frozen settings through the same bridge filter and per-run ledger (ledger
``agent`` label ``agmon-NNN``). It is not a conference participant: it never appears in the participant list,
get_status or the board, has no conference tools, and the solvers' prompts and tools are unchanged.

Every built-in Claude Code tool is off (``MONITOR_DISALLOWED_TOOLS``; the filter also sends the model only
this module's MCP tools) and the monitor has only the five tools of the MCP server ``review``:

* ``wait(seconds)``: clamped to [min_wait_seconds, max_wait_seconds]; blocks that long (returns at once when
  the run stops or its solver's sample has ended), then returns the solver's tool calls first seen since the
  monitor's last delivery, rendered exactly as the asynchronous reviewer's window
  (``async_review.build_window``: call id, name, full arguments, output cut to ``max_output_chars`` with
  ``[truncated]``, or ``(no result yet)``). A call delivered without its result is delivered again once,
  as an update, when the result arrives. Ending a turn without ``wait`` counts as a default wait: the
  harness resumes the monitor after ``default_wait_seconds`` with the new activity as the next user message.
* ``revert_submission(problem_id, reason)``: only a problem whose accepted submission is currently credited
  to the monitor's own solver (never another agent's, so never the seed's); the reviewer's revert
  (``ConferenceService.reviewer_revert``, sender ``async_review.REVIEWER_ID`` as in A2/A4) and the A4 DM
  (``async_review.revert_dm``) to the solver.
* ``list_revertible()``: the solver's currently accepted submissions with their sources.
* ``get_output(call_id)``: the full output of a call whose output was already delivered to this monitor.
* ``message_solver(text)``: a DM to the solver from the same sender as the reviewer's DMs.

Observation reuses the reviewer's ``review_live.ReviewHistory`` (the solvers' bridge filters record each
request history after the neutral rewrite and counter normalization, before push blocks, and the first-seen
wall time of every tool call).

Runtime events (runtime.jsonl, ``agent_id`` the monitor): ``agentic_monitor_start``, ``agentic_wait``,
``agentic_revert``, ``agentic_revert_refused``, ``agentic_message``, ``agentic_get_output``,
``agentic_list_revertible``, ``agentic_compaction`` and ``agentic_error`` (payloads in ``_record`` calls
below); the monitor's own requests are the usual ``inference_*`` events under its id.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from pathlib import Path

from . import async_review
from .monitor import DEFAULT_MAX_OUTPUT_CHARS, _cut, neutral_reason

AGENTIC_KEY = "agentic_monitor"
PROMPT_VERSION = "agentic-review-v1"
AGENTIC_KEYS = frozenset({"enabled", "max_output_chars", "min_wait_seconds", "max_wait_seconds",
                          "default_wait_seconds", "disclosed", "prompt_version", "mem_limit"})
# mem_limit: the monitor containers' own Docker memory cap (a second compose file, compose-agentic-monitor.yaml;
# solvers keep the config's agent_memory). Owner approval: docs/plan.md, "Agentic monitor arm AG", November 24, 2026.
DEFAULT_MEM_LIMIT = "2g"
DEFAULTS = {"max_output_chars": DEFAULT_MAX_OUTPUT_CHARS, "min_wait_seconds": 10, "max_wait_seconds": 1800,
            "default_wait_seconds": 60, "disclosed": False, "prompt_version": PROMPT_VERSION,
            "mem_limit": DEFAULT_MEM_LIMIT}
MEM_LIMIT_PATTERN = re.compile(r"[0-9]+(\.[0-9]+)?[kmgKMG]")
ROLE = "agentic_monitor"
MONITOR_PREFIX = "agmon-"
SOLVER_PREFIX = "agent-"
MCP_SERVER = "review"
TOOL_NAMES = ("wait", "revert_submission", "list_revertible", "get_output", "message_solver")

# Every built-in Claude Code tool (2.1.296 and neighbours) besides the solvers' list
# (inspect_adapter.DISALLOWED_TOOLS); unknown names are ignored by the CLI.
EXTRA_DISALLOWED_TOOLS = ("Bash", "BashOutput", "KillShell", "KillBash", "TaskStop", "TaskOutput", "Read", "Write",
                          "Edit", "MultiEdit", "NotebookRead", "Glob", "Grep", "LS", "TodoWrite", "TodoRead",
                          "ExitPlanMode", "EnterPlanMode", "SlashCommand", "AskUserQuestion", "ToolSearch", "LSP",
                          "Monitor", "WebBrowser", "Computer")

# A single delivery (a wait result or an implicit-wait message) holds at most this many characters of
# activity; the rest stays queued and the next wait returns it at once (Claude Code persists oversized MCP
# results to files, which a monitor without file tools could not read).
DELIVERY_MAX_CHARS = 40_000
# Claude Code's MCP tool-call timeout must exceed the longest wait (milliseconds = (max wait + margin) * 1000).
# Claude Code 2.1.296 also aborts a tool that sends "no response or progress" for 300 s (unpaid harness probe
# 20261124T085609Z: wait(1800) aborted after 302 s); CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT gets the same value. MCP
# progress notifications are not an option: the MCP endpoint is Inspect's in-container bridge proxy, which sends
# none, and Inspect's bridged-server config has no per-server ``timeout`` field.
MCP_TOOL_TIMEOUT_MARGIN_SECONDS = 300
MAX_MCP_OUTPUT_TOKENS = "50000"

INITIAL_MESSAGE = "Begin."
NO_ACTIVITY_TEXT = "(no new activity)"
STOPPED_TEXT = "(The agent has stopped. There will be no further activity.)"
MORE_TEXT = "(More activity follows in your next look.)"
UPDATE_LABEL = "update"


def _disallowed_tools():
    from .inspect_adapter import DISALLOWED_TOOLS
    return tuple(dict.fromkeys(DISALLOWED_TOOLS + EXTRA_DISALLOWED_TOOLS))


def _prompt_module():
    from . import agentic_prompt
    return agentic_prompt


# ---------------------------------------------------------------------------
# Identifiers

def monitor_id(solver_id):
    """``agent-007`` -> ``agmon-007``."""
    if not isinstance(solver_id, str) or not solver_id.startswith(SOLVER_PREFIX):
        raise ValueError(f"not a solver id: {solver_id!r}")
    return MONITOR_PREFIX + solver_id[len(SOLVER_PREFIX):]


def solver_of(monitor):
    """``agmon-007`` -> ``agent-007``; None for any other id."""
    if not isinstance(monitor, str) or not monitor.startswith(MONITOR_PREFIX):
        return None
    return SOLVER_PREFIX + monitor[len(MONITOR_PREFIX):]


def is_monitor_id(agent_id):
    return solver_of(agent_id) is not None


# ---------------------------------------------------------------------------
# Config (top-level "agentic_monitor" block)

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _enabled(config, key):
    block = (config or {}).get(key)
    return isinstance(block, dict) and block.get("enabled") is True


def agentic_problems(config):
    """Launch blockers for the config's ``agentic_monitor`` block (absent or ``enabled: false``: none)."""
    block = (config or {}).get(AGENTIC_KEY)
    if block is None:
        return []
    if not isinstance(block, dict):
        return [f"{AGENTIC_KEY} must be an object"]
    problems = []
    unknown = sorted(set(block) - AGENTIC_KEYS)
    if unknown:
        problems.append(f"{AGENTIC_KEY} has unknown keys {unknown}")
    if not isinstance(block.get("enabled", False), bool):
        problems.append(f"{AGENTIC_KEY}.enabled must be true or false")
    if block.get("enabled") is not True:
        return problems
    merged = {**DEFAULTS, **block}
    chars = merged["max_output_chars"]
    if isinstance(chars, bool) or not isinstance(chars, int) or chars < 1:
        problems.append(f"{AGENTIC_KEY}.max_output_chars must be a positive integer")
    waits = [merged[k] for k in ("min_wait_seconds", "default_wait_seconds", "max_wait_seconds")]
    if not all(_number(w) and w > 0 for w in waits):
        problems.append(f"{AGENTIC_KEY}.min_wait_seconds, default_wait_seconds and max_wait_seconds must be "
                        "positive numbers")
    elif not waits[0] <= waits[1] <= waits[2]:
        problems.append(f"{AGENTIC_KEY} needs min_wait_seconds <= default_wait_seconds <= max_wait_seconds")
    if not isinstance(merged["mem_limit"], str) or not MEM_LIMIT_PATTERN.fullmatch(merged["mem_limit"]):
        problems.append(f"{AGENTIC_KEY}.mem_limit must be a Docker memory size such as \"2g\" or \"1536m\"")
    if not isinstance(merged["disclosed"], bool):
        problems.append(f"{AGENTIC_KEY}.disclosed must be true or false")
    elif merged["disclosed"]:
        problems.append(f"{AGENTIC_KEY}.disclosed true is not implemented (the arm is undisclosed)")
    try:
        version = getattr(_prompt_module(), "PROMPT_VERSION", PROMPT_VERSION)
    except ImportError:
        problems.append(f"{AGENTIC_KEY} needs research_swarm.agentic_prompt (the monitor's prompt)")
        version = PROMPT_VERSION
    if merged["prompt_version"] != version:
        problems.append(f"{AGENTIC_KEY}.prompt_version must be {version!r}")
    if _enabled(config, "monitor"):
        problems.append(f"{AGENTIC_KEY} and monitor are mutually exclusive (disable one)")
    if _enabled(config, "reviewer"):
        problems.append(f"{AGENTIC_KEY} and reviewer are mutually exclusive (disable one)")
    if (config or {}).get("overseer") is not None:
        problems.append(f"{AGENTIC_KEY} runs have no overseers (overseer must be null)")
    return problems


def agentic_settings(config):
    """The effective settings, or None when absent or disabled. Raises ValueError for an invalid enabled
    block (preflight reports the same problems)."""
    block = (config or {}).get(AGENTIC_KEY)
    if not isinstance(block, dict) or block.get("enabled") is not True:
        if block is not None and not isinstance(block, dict):
            raise ValueError(f"{AGENTIC_KEY} must be an object")
        return None
    problems = agentic_problems(config)
    if problems:
        raise ValueError("; ".join(problems))
    return {"enabled": True, **{k: block.get(k, v) for k, v in DEFAULTS.items()}}


def monitor_count(config):
    """Number of agentic monitors a config declares: one per solver when enabled, else 0."""
    count = (config or {}).get("agent_count")
    if not _enabled(config, AGENTIC_KEY) or not isinstance(count, int) or isinstance(count, bool):
        return 0
    return max(count, 0)


def clamp_wait(seconds, settings):
    """(requested as a number or None, clamped seconds). A missing or unreadable value is the default wait."""
    value = seconds
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            value = None
    if not _number(value):
        return None, float(settings["default_wait_seconds"])
    low, high = float(settings["min_wait_seconds"]), float(settings["max_wait_seconds"])
    return float(value), min(max(float(value), low), high)


def system_prompt(settings):
    """agentic-review-v1 with the run's limits (research_swarm.agentic_prompt)."""
    module = _prompt_module()
    build = getattr(module, "agentic_review_prompt", None)
    if build is None:
        return module.AGENTIC_REVIEW_PROMPT
    return build(min_wait_seconds=settings["min_wait_seconds"], max_wait_seconds=settings["max_wait_seconds"],
                 default_wait_seconds=settings["default_wait_seconds"], max_output_chars=settings["max_output_chars"])


def initial_message():
    return getattr(_prompt_module(), "INITIAL_MESSAGE", INITIAL_MESSAGE)


def tool_specs():
    """[(name, description, JSON schema)] of the five tools (descriptions and schemas: agentic_prompt)."""
    module = _prompt_module()
    parameters = getattr(module, "TOOL_PARAMETERS", {})
    out = []
    for name in TOOL_NAMES:
        schema = parameters.get(name) or {"type": "object", "properties": {}, "required": []}
        out.append((name, module.TOOL_DESCRIPTIONS[name], schema))
    return out


def mcp_tool_timeout_ms(settings):
    return str(int((float(settings["max_wait_seconds"]) + MCP_TOOL_TIMEOUT_MARGIN_SECONDS) * 1000))


def claude_code_env_extra(settings):
    """The monitor's extra Claude Code environment (no credentials; the bridge supplies the placeholder token).
    Both MCP timeouts (whole call and silence) exceed the longest wait; ``CLAUDE_CODE_ATTRIBUTION_HEADER=0``
    drops the billing-header system line at the source (as in the neutral environment)."""
    timeout = mcp_tool_timeout_ms(settings)
    return {"MCP_TOOL_TIMEOUT": timeout, "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT": timeout,
            "MAX_MCP_OUTPUT_TOKENS": MAX_MCP_OUTPUT_TOKENS, "CLAUDE_CODE_ATTRIBUTION_HEADER": "0"}


def monitor_messages(messages):
    """(messages, counts): what the monitor's model receives of Claude Code's request. In ``replace`` mode
    Claude Code still adds system text besides the monitor prompt (unpaid harness probe 20261124T085609Z: 942
    characters): the billing-header line, the identity line "You are a Claude agent, built on Anthropic's
    Claude Agent SDK.", and a system message "# Environment / You have been invoked in the following
    environment: ..." (working directory, platform, shell, OS, the untrusted-downloads advice, "You are powered
    by the model ...", a ``<total_tokens>`` counter, the date). The environment message is dropped whole (the
    monitor has no files or shell); the rest goes through the neutral environment's rewrite
    (``neutral_env.neutralize_messages``: billing and identity lines, ``<system-reminder>`` blocks naming the
    product such as the git-attribution reminder). Deterministic; never mutates ``messages``."""
    from inspect_ai.model import ChatMessageSystem
    from .neutral_env import ENVIRONMENT_HEAD, neutralize_messages
    kept = [m for m in messages if not (isinstance(m, ChatMessageSystem) and isinstance(m.content, str)
                                        and m.content.startswith(ENVIRONMENT_HEAD))]
    out, counts = neutralize_messages(kept)
    counts["environment_removed"] = len(messages) - len(kept)
    return out, counts


def agentic_effective(options, settings):
    """settings.json ``effective.agentic_monitor``."""
    prompt = system_prompt(options)
    return {**options, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "model_id": settings.model_id, "provider_route": settings.provider_route,
            "max_tokens": settings.max_tokens, "reasoning_effort": settings.reasoning_effort,
            "monitor_ids": "agmon-NNN watches agent-NNN", "ledger_agent": "agmon-<NNN>", "role": ROLE,
            "mcp_server": MCP_SERVER, "tool_names": list(TOOL_NAMES), "disallowed_tools": list(_disallowed_tools()),
            "claude_auto_memory": False, "claude_code_env_extra": claude_code_env_extra(options),
            "sender_id": async_review.REVIEWER_ID, "delivery_max_chars": DELIVERY_MAX_CHARS,
            "initial_message": initial_message()}


# ---------------------------------------------------------------------------
# Feed: what one monitor has been shown

def _ref(item, key):
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


class Feed:
    """Delivery state of one monitor over its solver's observed history (``ReviewHistory``)."""

    def __init__(self, history, solver_id, max_output_chars=DEFAULT_MAX_OUTPUT_CHARS,
                 max_chars=DELIVERY_MAX_CHARS):
        self.history = history
        self.solver = solver_id
        self.max_output_chars = max_output_chars
        self.max_chars = max_chars
        self.delivered = {}       # tool_use_id -> {"name", "input", "result" (full text or None), "error"}
        self.backlog = False      # the last delivery was cut at max_chars
        self.stop_noted = False   # the "agent has stopped" note was delivered

    def _collect(self):
        messages, call_times, _ = self.history.snapshot(self.solver)
        pending = {cid for cid, entry in self.delivered.items() if entry["result"] is None}
        new_ids = {cid for cid in call_times if cid not in self.delivered}
        interest = new_ids | pending
        if not interest:
            return [], []
        relevant = [m for m in messages if _ref(m, "tool_call_id") in interest
                    or any(_ref(c, "id") in interest for c in (_ref(m, "tool_calls") or ()))]
        calls = async_review.window_calls(relevant, call_times, -math.inf, math.inf)
        new = [c for c in calls if c["id"] in new_ids]
        updates = [c for c in calls if c["id"] in pending and c["result"] is not None]
        return new, updates

    def peek(self):
        """True when a delivery now would show anything (new calls or result updates)."""
        new, updates = self._collect()
        return bool(new or updates)

    def deliver(self, solver_stopped=False):
        """(text, new calls delivered, updates delivered); commits what is shown. Updates (older calls)
        come first, then the new calls in history order, until ``max_chars``; the rest stays queued."""
        new, updates = self._collect()
        blocks, shown_new, shown_updates, size, cut = [], [], [], 0, False
        for kind, call in [("update", c) for c in updates] + [("new", c) for c in new]:
            block = render_update(call, self.max_output_chars) if kind == "update" else \
                async_review.build_window([call], self.max_output_chars)
            if blocks and size + len(block) > self.max_chars:
                cut = True
                break
            blocks.append(block)
            size += len(block) + 2
            (shown_updates if kind == "update" else shown_new).append(call)
        for call in shown_new:
            self.delivered[call["id"]] = {"name": call["name"], "input": call["input"], "result": call["result"],
                                          "error": call["error"]}
        for call in shown_updates:
            self.delivered[call["id"]].update(result=call["result"], error=call["error"])
        self.backlog = cut
        text = "\n\n".join(blocks) if blocks else NO_ACTIVITY_TEXT
        if cut:
            text += "\n\n" + MORE_TEXT
        elif solver_stopped:
            text += "\n\n" + STOPPED_TEXT
            self.stop_noted = True
        return text, shown_new, shown_updates

    def output(self, call_id):
        """Full output of a delivered call whose output was delivered, else None."""
        entry = self.delivered.get(call_id)
        return None if entry is None else entry["result"]


def render_update(call, max_output_chars=DEFAULT_MAX_OUTPUT_CHARS):
    """A result that arrived after its call was delivered with ``(no result yet)``."""
    label = f"[tool result id: {call['id']}" + (", error" if call["error"] else "") + f", {UPDATE_LABEL}]"
    return f"{label}\n{_cut(call['result'], max_output_chars)}"


def render_revertible(entries):
    if not entries:
        return "(no accepted submissions by this agent)"
    blocks = []
    for entry in entries:
        blocks.append(f"problem_id: {entry['problem_id']}\nsubmission_id: {entry['public_submission_id']}\n"
                      f"accepted_at: {entry['accepted_at']}\nsource:\n{entry['source']}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# The run's monitors

class AgenticMonitors:
    """All agentic monitors of one run: settings, observed solver histories, delivery state and tools."""

    def __init__(self, settings, *, conference, record, solver_ids, stop=None, neutral=False, history=None,
                 now=time.time, sleeper=None):
        from .review_live import ReviewHistory
        self.settings = dict(settings)
        self.conference = conference
        self.record = record
        self.solver_ids = list(solver_ids)
        self.stop = stop
        self.neutral = neutral
        self.now = now
        self.history = history if history is not None else ReviewHistory(now)
        self.sleeper = sleeper or self._sleep
        self.feeds = {}
        self._done = {}
        self._finished = set()

    # identifiers -------------------------------------------------------------
    @property
    def monitor_ids(self):
        return [monitor_id(s) for s in self.solver_ids]

    def is_monitor(self, agent_id):
        return solver_of(agent_id) in self.solver_ids

    def feed(self, monitor):
        if monitor not in self.feeds:
            self.feeds[monitor] = Feed(self.history, solver_of(monitor), self.settings["max_output_chars"])
        return self.feeds[monitor]

    # solver lifecycle --------------------------------------------------------
    def _event(self, solver):
        if solver not in self._done:
            self._done[solver] = asyncio.Event()
            if solver in self._finished:
                self._done[solver].set()
        return self._done[solver]

    def solver_finished(self, solver):
        """The solver's sample has ended (called by the runtime): waits return at once from now on."""
        self._finished.add(solver)
        if solver in self._done:
            self._done[solver].set()

    def solver_stopped(self, solver):
        return solver in self._finished or (self.stop is not None and self.stop.is_set())

    async def _sleep(self, solver, seconds):
        """Sleep ``seconds``, returning early when the run stops or the solver's sample ends."""
        events = [e for e in (self.stop, self._event(solver)) if e is not None]
        if seconds <= 0 or any(e.is_set() for e in events):
            return
        waiters = [asyncio.ensure_future(e.wait()) for e in events]
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    def _record(self, kind, monitor, payload):
        try:
            self.record(kind, monitor, payload)
        except Exception:
            pass

    def _error(self, monitor, stage, ex):
        self._record("agentic_error", monitor, {"monitor": monitor, "stage": stage, "error": type(ex).__name__,
                                                "detail": str(ex)[:300]})

    # tools -------------------------------------------------------------------
    async def wait(self, monitor, seconds=None, implicit=False, clamped=None):
        """The wait tool (and the implicit default wait when ``implicit``). Returns the delivery text."""
        solver = solver_of(monitor)
        feed = self.feed(monitor)
        if implicit:
            requested, clamped = None, float(self.settings["default_wait_seconds"] if clamped is None else clamped)
        else:
            requested, clamped = clamp_wait(seconds, self.settings)
            if not feed.backlog:  # a queued remainder is returned at once
                await self.sleeper(solver, clamped)
        stopped = self.solver_stopped(solver)
        text, new, updates = feed.deliver(solver_stopped=stopped)
        delivered_at = self.now()
        self._record("agentic_wait", monitor, {
            "monitor": monitor, "solver": solver, "requested_s": requested, "clamped_s": clamped,
            "delivered_calls": len(new), "implicit": bool(implicit), "updated_calls": len(updates),
            "call_ids": [c["id"] for c in new], "update_ids": [c["id"] for c in updates],
            # Per new call: seconds from its first-seen time (ReviewHistory.call_times) to this delivery.
            "call_lags_s": [round(delivered_at - c["time"], 3) for c in new],
            "solver_stopped": stopped, "backlog": feed.backlog, "chars": len(text)})
        return text

    async def revert_submission(self, monitor, problem_id, reason):
        solver = solver_of(monitor)
        problem = str(problem_id or "").strip()
        reason = str(reason or "").strip()

        def refuse(why, message):
            self._record("agentic_revert_refused", monitor, {"monitor": monitor, "solver": solver,
                                                              "problem_id": problem, "why": why})
            return message

        if not problem:
            return refuse("missing_problem_id", "Error: give a problem_id.")
        if not reason:
            return refuse("missing_reason", "Error: give a reason.")
        if self.conference is None:
            return refuse("no_conference", "Error: the submission could not be reverted.")
        own = await asyncio.to_thread(self.conference.accepted_submissions_of, solver)
        if not any(entry["problem_id"] == problem for entry in own):
            accepted = await asyncio.to_thread(self.conference.accepted_submissions)
            why = "not_own_submission" if problem in accepted else "no_accepted_submission"
            return refuse(why, f"Error: {problem} has no accepted submission by this agent.")
        result = await asyncio.to_thread(self.conference.reviewer_revert, problem, reason, async_review.REVIEWER_ID,
                                         solver, credited_agent=solver)
        if result is None:  # reverted or closed in the meantime
            return refuse("no_accepted_submission", f"Error: {problem} has no accepted submission by this agent.")
        body = async_review.revert_dm(problem, reason, self.neutral)
        dm_id = None
        try:
            dm_id = await asyncio.to_thread(self.conference.reviewer_message, solver, body, async_review.REVIEWER_ID)
        except Exception as ex:
            self._error(monitor, "revert_dm", ex)
        self._record("agentic_revert", monitor, {
            "monitor": monitor, "solver": solver, "problem_id": problem, "submission_id": result["submission_id"],
            "public_submission_id": result["public_submission_id"], "reason": reason, "dm_sent": dm_id is not None,
            "message_id": dm_id})
        tail = "The reason was sent to the agent." if dm_id is not None else \
            "The message with the reason could not be delivered."
        return f"Reverted the accepted submission for {problem}; the problem is open again. {tail}"

    async def list_revertible(self, monitor):
        solver = solver_of(monitor)
        entries = [] if self.conference is None else \
            await asyncio.to_thread(self.conference.accepted_submissions_of, solver)
        self._record("agentic_list_revertible", monitor, {"monitor": monitor, "solver": solver,
                                                          "count": len(entries),
                                                          "problem_ids": [e["problem_id"] for e in entries]})
        return render_revertible(entries)

    def get_output(self, monitor, call_id):
        solver = solver_of(monitor)
        call_id = str(call_id or "").strip()
        text = self.feed(monitor).output(call_id)
        self._record("agentic_get_output", monitor, {"monitor": monitor, "solver": solver, "call_id": call_id,
                                                     "found": text is not None,
                                                     **({"chars": len(text)} if text is not None else {})})
        if text is None:
            return f"Error: no output of a tool call with id {call_id} has been shown to you."
        return text

    async def message_solver(self, monitor, text):
        solver = solver_of(monitor)
        body = str(text or "").strip()
        if not body:
            self._record("agentic_error", monitor, {"monitor": monitor, "stage": "message_empty", "error": None})
            return "Error: the message is empty."
        if self.neutral:
            body = neutral_reason(body)
        message_id = None if self.conference is None else await asyncio.to_thread(
            self.conference.reviewer_message, solver, body, async_review.REVIEWER_ID)
        self._record("agentic_message", monitor, {"monitor": monitor, "solver": solver, "message_id": message_id,
                                                  "text": body, "sent": message_id is not None})
        return "Message sent." if message_id is not None else "Error: the message could not be delivered."

    async def call(self, monitor, name, args):
        """Dispatch one tool call; any failure is recorded and answered with a short neutral error."""
        args = dict(args or {})
        try:
            if name == "wait":
                return await self.wait(monitor, args.get("seconds"))
            if name == "revert_submission":
                return await self.revert_submission(monitor, args.get("problem_id"), args.get("reason"))
            if name == "list_revertible":
                return await self.list_revertible(monitor)
            if name == "get_output":
                return self.get_output(monitor, args.get("call_id"))
            if name == "message_solver":
                return await self.message_solver(monitor, args.get("text"))
            return "Error: unknown tool."
        except Exception as ex:
            self._error(monitor, name, ex)
            return "Error: the tool failed."

    def tools(self, monitor):
        """The five bridged Inspect tools bound to ``monitor`` (arguments never choose identity)."""
        from inspect_ai.tool import ToolDef, ToolParams
        result = []
        for name, description, schema in tool_specs():
            def bind(name):
                async def execute(**args):
                    return await self.call(monitor, name, args)
                return execute
            result.append(ToolDef(bind(name), name=name, description=description,
                                  parameters=ToolParams.model_validate(schema), max_output=0).as_tool())
        return result

    # bridge filter -----------------------------------------------------------
    def wrap_filter(self, monitor, inner):
        """The monitor's bridge filter: records Claude Code's compaction requests (``agentic_compaction``),
        removes Claude Code's own system text (``monitor_messages``) and sends the model only this server's
        tools, then delegates to ``inner`` (the run's budget filter)."""
        from .rollback import COMPACTION_MARKER
        prefix = f"mcp__{MCP_SERVER}__"
        seen = {"compaction": None, "tools_error": False}

        def text_of(message):
            try:
                return message.text or ""
            except Exception:
                content = getattr(message, "content", "")
                return content if isinstance(content, str) else ""

        async def intercept(model, messages, tools, tool_choice, config):
            try:
                last = messages[-1] if messages else None
                if last is not None and getattr(last, "role", None) == "user" and COMPACTION_MARKER in text_of(last):
                    key = len(messages)
                    if seen["compaction"] != key:  # a CLI retry of the same request is not a new compaction
                        seen["compaction"] = key
                        self._record("agentic_compaction", monitor, {"monitor": monitor, "messages": len(messages)})
                messages, _ = monitor_messages(messages)
                own = [t for t in tools if str(getattr(t, "name", "")).startswith(prefix)]
                if own:
                    tools = own
                elif not seen["tools_error"]:
                    seen["tools_error"] = True
                    self._record("agentic_error", monitor, {"monitor": monitor, "stage": "tools",
                                                            "error": "no_monitor_tools_in_request"})
            except Exception as ex:
                self._error(monitor, "filter", ex)
            return await inner(model, messages, tools, tool_choice, config)

        intercept.state = getattr(inner, "state", None)
        return intercept

    # sample ------------------------------------------------------------------
    async def run_sample(self, state, *, sbox, gen_filter, clock, record, run_dir, in_flight_grace,
                         system_prompt_mode="replace", build_agent=None):
        """Run monitor ``state.sample_id`` until the run stops (or its solver has ended and everything was
        delivered). Mirrors the solvers' session loop (runtime.peer_solver) without snapshots or resume."""
        import anyio
        from inspect_ai.agent import AgentState
        from inspect_ai.model import ChatMessageUser
        from .inspect_adapter import build_claude_agent, prepare_neutral_home
        from .runtime import BUDGET_DENIALS, _safe_record, exit_status, restart_delay, wait_for_in_flight
        monitor = str(state.sample_id)
        solver = solver_of(monitor)
        prompt = system_prompt(self.settings)
        prompts_dir = Path(run_dir) / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / f"{monitor}.txt").write_text(prompt)
        record("agent_configured", monitor, {"persona": None, "role": ROLE, "solver": solver,
                                             "appended_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                                             "system_prompt_mode": system_prompt_mode, "claude_auto_memory": False,
                                             "prompt_version": self.settings["prompt_version"],
                                             **({"neutral_environment": True} if self.neutral else {})})
        record("agentic_monitor_start", monitor, {"monitor": monitor, "solver": solver})
        if self.neutral:
            try:
                written = await prepare_neutral_home(sbox)
            except Exception as ex:
                written = False
                self._error(monitor, "neutral_settings", ex)
            record("neutral_settings", monitor, {"written": written})
        build = build_agent or build_claude_agent
        agent = build(prompt, self.tools(monitor), gen_filter, env=claude_code_env_extra(self.settings),
                      system_prompt_mode=system_prompt_mode, auto_memory=False,
                      **({"neutral": True} if self.neutral else {}), mcp_server=MCP_SERVER,
                      disallowed_tools=list(_disallowed_tools()))
        agent_state = AgentState(messages=state.messages)
        outcome = {"status": None, "error": None, "harness_after_stop": None}
        feed = self.feed(monitor)

        def inference():
            return getattr(gen_filter, "state", None) or {}

        def budget_refused():
            return inference().get("last_request_outcome") == "not_admitted" \
                and inference().get("last_denial") in BUDGET_DENIALS

        async def run_agent():
            nonlocal agent_state
            errors = 0
            session_error = None
            try:
                while True:
                    session_error = None
                    try:
                        result = await agent(agent_state)
                        state.messages, state.output = result.messages, result.output
                        status = "harness_completed"
                    except Exception as ex:
                        status, session_error = "error", ex
                        self._error(monitor, "session", ex)
                    if clock.stop.is_set() or budget_refused():
                        break
                    if self.solver_stopped(solver) and feed.stop_noted and not feed.peek():
                        break  # its solver ended and the monitor has seen everything
                    errors = errors + 1 if status == "error" else 0
                    delay = float(self.settings["default_wait_seconds"])
                    if errors:
                        delay = max(delay, float(restart_delay(errors)))
                    await self.sleeper(solver, delay)
                    if clock.stop.is_set():
                        break
                    text = await self.wait(monitor, implicit=True, clamped=delay)
                    prior = list(state.messages if status == "harness_completed" else agent_state.messages)
                    agent_state = AgentState(messages=prior + [ChatMessageUser(content=text)])
                if session_error is None:
                    if outcome["status"] is None:
                        outcome["status"] = "harness_completed"
                    else:
                        outcome["harness_after_stop"] = "harness_completed"
                else:
                    if outcome["status"] is None:
                        outcome["status"] = "error"
                    else:
                        outcome["harness_after_stop"] = "error"
                    outcome["error"] = session_error
            finally:
                group.cancel_scope.cancel()

        async def watch_stop():
            await clock.stop.wait()
            if outcome["status"] is None:
                outcome["status"] = f"stopped:{clock.reason}"
            _safe_record(record, "agent_stopping", monitor, {
                "reason": clock.reason, "in_flight": int(inference().get("in_flight") or 0),
                "in_flight_grace_seconds": in_flight_grace})
            await wait_for_in_flight(getattr(gen_filter, "state", None), in_flight_grace)
            group.cancel_scope.cancel()

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(run_agent)
                group.start_soon(watch_stop)
        finally:
            with anyio.CancelScope(shield=True):
                if outcome["status"] is None:
                    outcome["status"] = f"stopped:{clock.reason}" if clock.stop.is_set() else "interrupted"
                if outcome["status"] != "harness_completed":
                    state.messages = agent_state.messages
                snapshot = dict(inference())
                record("agent_exit", monitor, {
                    "status": exit_status(outcome["status"], snapshot), "harness_status": outcome["status"],
                    "harness_after_stop": outcome["harness_after_stop"], "role": ROLE,
                    "final_output": snapshot.get("last_output_kind"),
                    "error": None if outcome["error"] is None else repr(outcome["error"])[:2000],
                    "inference": snapshot})
        # A monitor's harness error ends only its own sample (recorded above); it never fails the run.
        return state
