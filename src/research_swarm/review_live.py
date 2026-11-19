"""Asynchronous reviewer, live path (docs/plan.md, "Asynchronous reviewer arms A1–A4 (owner decisions,
November 18, 2026)"; prompt and parsing in research_swarm.async_review).

- ``ReviewHistory``: what the bridge filter (``inspect_adapter.budget_filter(review=...)``) observes per
  agent: the latest request history (after the neutral-environment rewrite and token-counter
  normalization, before push blocks are attached, so incoming messages never enter a window), the agent's
  latest response, the model it was generated with, and the wall time each tool_use_id was first seen
  (when the response carrying the call returned; a call first seen in a request history, e.g. after a
  resume, gets that request's time).
- ``LiveReviewer``: one host-side loop per solver (``run``), started in the solver's own task group
  (runtime.peer_solver); the seed and overseers are never reviewed. Ticks are ``interval_minutes`` apart
  from the common start, each agent offset by ``interval * (i + 1) / n`` (i its index among the n
  solvers) so the calls do not burst. Each tick reviews the calls first seen in [tick - interval, tick)
  (``review``): an empty window makes no call; otherwise one call on the agent's own model and frozen
  settings, admitted and settled in the run's ledger with agent label ``reviewer:<agent>``, ModelEvents
  kept out of the agent's transcript, capped at 600 s. Any failure is logged and nothing is done.
  At score >= threshold the lever acts: ``revert`` (A3/A4: ConferenceService.reviewer_revert for each
  eligible problem, async_review.revert_plan; A3 DMs each author) or ``rollback`` (A1/A2: the rollback
  API of research_swarm.rollback, see ``apply_rollback``).

Records: runtime events ``review_admitted`` (request_id, reserved_usd; run accounting), ``review_scored``,
``review_error``, ``review_revert`` {agent, problem_id, submission_id, author, dm_sent}, ``review_rollback``
(the rollback call's result) and ``review_rollback_error``; the full prompt and reply of every call in
runs/<id>/review/calls.jsonl (``log``).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field

from . import async_review

# Upper bound on one reviewer call (as the monitor's); past it the call is cancelled (uncertain hold).
REVIEW_TIMEOUT_SECONDS = 600
CALL_LOG = "review/calls.jsonl"


def _ref(item, key):
    """Attribute of an Inspect object or key of its JSON dump."""
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def reviewer_ledger_agent(agent_id):
    """Ledger ``agent`` label of a reviewer call reviewing ``agent_id`` (separable spend)."""
    return f"reviewer:{agent_id}"


# ---------------------------------------------------------------------------
# Rollback call site (WS2: research_swarm.rollback)

@dataclass
class RollbackContext:
    """What the reviewer hands the rollback implementation.

    ``messages``: the agent's observed history at the review (Inspect messages, the latest response last);
    ``call_times``: {tool_use_id: first-seen wall time}; ``cut_time``: the first-seen time of the cut call;
    ``cut_tool_use_ids``: every call from the cut (inclusive) to the end of ``messages``, in order;
    ``sandbox``: the agent's Inspect sandbox (None in tests); ``history``: the run's ReviewHistory."""
    agent_id: str
    cut_tool_use_id: str
    cut_time: float | None
    cut_tool_use_ids: list
    messages: list
    call_times: dict
    conference: object = None
    sandbox: object = None
    record: object = None
    run_dir: object = None
    history: object = None
    neutral: bool = False
    extra: dict = field(default_factory=dict)


async def apply_rollback(context, agent_id, cut_tool_use_id, note) -> dict:
    """Stub until research_swarm.rollback provides ``apply`` (WS2). Expected contract: set ``agent_id``
    back to before ``cut_tool_use_id`` (conversation cut, files restored, later submissions reverted),
    deliver ``note`` (A1) or nothing (A2, None) and return a JSON-serializable dict for the
    ``review_rollback`` event."""
    raise NotImplementedError("rollback is not implemented yet (research_swarm.rollback.apply)")


def resolve_rollback():
    """``research_swarm.rollback.apply`` when that module exists, else the stub ``apply_rollback``."""
    try:
        from . import rollback as module
    except ImportError:
        return apply_rollback
    return getattr(module, "apply", None) or apply_rollback


# ---------------------------------------------------------------------------
# Observed history

class ReviewHistory:
    """Per-agent observed history (thread-safe; written by the bridge filter, read by the reviewer)."""

    def __init__(self, now=time.time):
        self.now = now
        self.lock = threading.Lock()
        self.messages = {}    # agent -> latest request history (list)
        self.pending = {}     # agent -> latest response message with tool calls (not yet in a request)
        self.models = {}      # agent -> Model of the latest request
        self.call_times = {}  # agent -> {tool_use_id: first-seen wall time}

    def observe_request(self, agent_id, messages, model):
        when = self.now()
        with self.lock:
            times = self.call_times.setdefault(agent_id, {})
            for message in messages:
                for call in getattr(message, "tool_calls", None) or ():
                    times.setdefault(call.id, when)
            self.messages[agent_id] = list(messages)
            self.pending.pop(agent_id, None)
            self.models[agent_id] = model

    def observe_output(self, agent_id, message):
        when = self.now()
        with self.lock:
            times = self.call_times.setdefault(agent_id, {})
            for call in getattr(message, "tool_calls", None) or ():
                times.setdefault(call.id, when)
            self.pending[agent_id] = message

    def snapshot(self, agent_id):
        """(messages with the latest response last, {tool_use_id: time}, model)."""
        with self.lock:
            messages = list(self.messages.get(agent_id, ()))
            if agent_id in self.pending:
                messages.append(self.pending[agent_id])
            return messages, dict(self.call_times.get(agent_id, {})), self.models.get(agent_id)


def make_review_log(path):
    """Append-only JSONL of every reviewer call (researcher-side, private)."""
    from .runtime import make_monitor_log
    return make_monitor_log(path)


# ---------------------------------------------------------------------------
# Live reviewer

class LiveReviewer:
    def __init__(self, settings, *, ledger, provider_settings, record, conference=None, solver_ids=(),
                 barrier=None, stop=None, scope="pilot", neutral=False, log=None, run_dir=None, rollback=None,
                 now=time.time):
        self.settings = dict(settings)
        self.ledger = ledger
        self.provider_settings = provider_settings
        self.record = record
        self.conference = conference
        self.solver_ids = list(solver_ids)
        self.barrier = barrier
        self.stop = stop
        self.scope = scope
        self.neutral = neutral
        self.log = log
        self.run_dir = run_dir
        self.rollback = rollback
        self.now = now
        self.history = ReviewHistory(now)
        self.state = {}

    @property
    def interval_seconds(self):
        return float(self.settings["interval_minutes"]) * 60.0

    def offset_seconds(self, agent_id):
        """Stagger in (0, interval]: agent i of n reviews first at interval * (i + 1) / n after the start."""
        n = max(len(self.solver_ids), 1)
        index = self.solver_ids.index(agent_id) if agent_id in self.solver_ids else 0
        return self.interval_seconds * (index + 1) / n

    def _counters(self, agent_id):
        return self.state.setdefault(agent_id, {"reviews": 0, "empty_windows": 0, "errors": 0, "acted": 0})

    def _stopped(self):
        return self.stop is not None and self.stop.is_set()

    async def _wait(self, seconds):
        """Sleep ``seconds``; return early (True) when the run stops."""
        if seconds <= 0:
            return self._stopped()
        if self.stop is None:
            await asyncio.sleep(seconds)
            return False
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def run(self, agent_id, sandbox=None):
        """Review ``agent_id`` every interval until the run stops (or the caller's scope is cancelled)."""
        if self.barrier is not None:
            await self.barrier.event.wait()
        start = (getattr(self.barrier, "started_wall", None) if self.barrier is not None else None) or self.now()
        interval, offset = self.interval_seconds, self.offset_seconds(agent_id)
        tick_number = 0
        while not self._stopped():
            tick = start + offset + tick_number * interval
            if await self._wait(tick - self.now()):
                break
            try:
                await self.review(agent_id, tick - interval, tick, sandbox=sandbox)
            except Exception as ex:  # never disturbs the agent
                self._counters(agent_id)["errors"] += 1
                self.record("review_error", agent_id, {"stage": "loop", "error": type(ex).__name__,
                                                       "detail": str(ex)[:300]})
            tick_number += 1

    async def review(self, agent_id, start, end, sandbox=None):
        """Review the calls first seen in [start, end). Returns the call-log entry, or None (empty window
        or a failure, which is recorded as ``review_error``)."""
        import anyio
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser, GenerateConfig
        from inspect_ai.model._model import use_model_event_sink
        from .inspect_adapter import (_DroppedModelEvents, _in_thread, _reasoning_text, _rejected_without_generation,
                                      _reserve, _settle_unbilled, _short, classify_generate_error, settlement)
        counters = self._counters(agent_id)
        messages, call_times, model = self.history.snapshot(agent_id)
        in_window = {cid for cid, when in call_times.items() if start <= when < end}
        # Only the window's calls and results are serialized (histories are long; this runs on the event loop).
        relevant = [m for m in messages if _ref(m, "tool_call_id") in in_window
                    or any(_ref(c, "id") in in_window for c in (_ref(m, "tool_calls") or ()))] if in_window else []
        calls = async_review.window_calls(relevant, call_times, start, end)
        if not calls:
            counters["empty_windows"] += 1
            return None
        ids = [c["id"] for c in calls]
        base = {"window_start": start, "window_end": end, "tool_use_ids": ids,
                "prompt_version": async_review.REVIEW_PROMPT_VERSION}
        began = time.monotonic()
        record, ledger, settings, label = self.record, self.ledger, self.provider_settings, reviewer_ledger_agent(agent_id)

        def failed(stage, **payload):
            counters["errors"] += 1
            record("review_error", agent_id, {**base, "stage": stage,
                                              "latency_seconds": round(time.monotonic() - began, 3), **payload})

        if model is None:
            failed("no_model")
            return None
        try:
            window = async_review.build_window(calls, self.settings["max_output_chars"])
            prompt = async_review.build_review_request(window)
        except Exception as ex:
            failed("window", error=type(ex).__name__, detail=_short(str(ex), 300))
            return None
        inputs = [ChatMessageSystem(content=prompt[0]["content"]), ChatMessageUser(content=prompt[1]["content"])]
        serialized = json.dumps({"messages": [m.model_dump(mode="json") for m in inputs], "tools": []},
                                ensure_ascii=False)
        reservation = settings.reservation(serialized)
        try:
            request_id = await _reserve(ledger, self.scope, label, reservation, record)
        except Exception as ex:
            failed("admission", error=type(ex).__name__)
            return None
        # Run accounting (scripts/build_results.py): every reviewer call's ledger request.
        record("review_admitted", agent_id, {"request_id": request_id, "reserved_usd": str(reservation),
                                             "ledger_agent": label})
        if self._stopped():
            await _in_thread(_settle_unbilled, ledger, request_id, record, label)
            failed("run_stopped", request_id=request_id)
            return None
        frozen = GenerateConfig(max_tokens=settings.max_tokens, max_retries=0,
                                reasoning_effort=settings.reasoning_effort, reasoning_history="all", cache=False,
                                extra_body=settings.request_options())
        counters["reviews"] += 1
        try:
            with use_model_event_sink(_DroppedModelEvents()), anyio.fail_after(REVIEW_TIMEOUT_SECONDS):
                result = await model.generate(input=inputs, tools=[], tool_choice=None, config=frozen)
        except Exception as ex:  # includes the timeout; a cancellation (BaseException) settles and propagates
            info = classify_generate_error(ex)
            if info["billing"] == "unbilled_error":
                billing = await _in_thread(_settle_unbilled, ledger, request_id, record, label)
            else:
                await _in_thread(ledger.settle, request_id, None)
                billing = "uncertain"
            failed("timeout" if isinstance(ex, TimeoutError) else "generate", request_id=request_id, billing=billing,
                   **{k: info[k] for k in ("reason", "status", "error", "provider_generation_id")})
            return None
        except BaseException:
            ledger.settle(request_id, None)  # synchronous: this path may run inside a cancelled scope
            raise
        if _rejected_without_generation(result):
            cost, basis = None, await _in_thread(_settle_unbilled, ledger, request_id, record, label)
        else:
            cost, basis = settlement(settings, result)
            if cost is None:
                await _in_thread(ledger.settle, request_id, None)
            else:
                await _in_thread(ledger.settle, request_id, cost, basis)
        latency = time.monotonic() - began
        text = result.completion if result.choices else ""
        parsed = async_review.parse_review(text)
        action = await self._act(agent_id, parsed, calls, messages, call_times, sandbox)
        if action.get("acted"):
            counters["acted"] += 1
        metadata = result.metadata or {}
        usage = {"request_id": request_id,
                 "settled_usd": "0" if basis == "unbilled_error" else (None if cost is None else str(cost)),
                 "basis": basis, "usage": result.usage.model_dump(mode="json") if result.usage else None,
                 "provider_usage": metadata.get("openrouter_usage"),
                 "provider_generation_id": metadata.get("openrouter_generation_id"),
                 "provider_name": metadata.get("openrouter_provider"), "stop_reason": result.stop_reason}
        scored = {**base, "score": parsed["score"], "reason": parsed["reason"],
                  "first_bad_call": parsed["first_bad_call"], "revert": parsed["revert"], "error": parsed["error"],
                  "threshold": self.settings["threshold"], "lever": self.settings["lever"],
                  "show_reason": self.settings["show_reason"], "action": action,
                  "latency_seconds": round(latency, 3), "window_chars": len(window), **usage}
        record("review_scored", agent_id, scored)
        entry = {"agent": agent_id, **scored, "system": prompt[0]["content"], "user": prompt[1]["content"],
                 "response": text, "reasoning": _reasoning_text(result)}
        if self.log is not None:
            try:
                self.log(entry)
            except Exception as ex:
                record("review_log_error", agent_id, {"error": type(ex).__name__})
        return entry

    async def _act(self, agent_id, parsed, calls, messages, call_times, sandbox):
        """The lever at score >= threshold. Returns the action record (``acted`` true when anything was done)."""
        threshold = self.settings["threshold"]
        if parsed["score"] is None or parsed["error"]:
            return {"acted": False, "kind": "none", "why": parsed["error"] or "no_score"}
        if parsed["score"] < threshold:
            return {"acted": False, "kind": "none", "why": "below_threshold"}
        if self.settings["lever"] == "revert":
            return await self._revert(agent_id, parsed, calls)
        return await self._rollback(agent_id, parsed, calls, messages, call_times, sandbox)

    async def _revert(self, agent_id, parsed, calls):
        if self.conference is None:
            return {"acted": False, "kind": "revert", "why": "no_conference"}
        accepted = await asyncio.to_thread(self.conference.accepted_submissions)
        plan = async_review.revert_plan(parsed["revert"], calls, accepted, agent_id)
        reverted = []
        for item in plan:
            if not item["eligible"]:
                continue
            problem = item["problem_id"]
            try:
                result = await asyncio.to_thread(self.conference.reviewer_revert, problem, parsed["reason"],
                                                 async_review.REVIEWER_ID, agent_id)
            except Exception as ex:
                self.record("review_error", agent_id, {"stage": "revert", "problem_id": problem,
                                                       "error": type(ex).__name__})
                continue
            if result is None:  # reverted by someone else in the meantime
                item["why"] = "no_accepted_submission"
                continue
            dm_sent, dm_id = False, None
            if self.settings["show_reason"]:
                body = async_review.revert_dm(problem, parsed["reason"], self.neutral)
                try:
                    dm_id = await asyncio.to_thread(self.conference.reviewer_message, result["author"], body,
                                                    async_review.REVIEWER_ID)
                except Exception as ex:
                    self.record("review_error", agent_id, {"stage": "dm", "problem_id": problem,
                                                           "error": type(ex).__name__})
                dm_sent = dm_id is not None
            event = {"agent": agent_id, "problem_id": problem, "submission_id": result["submission_id"],
                     "public_submission_id": result["public_submission_id"], "author": result["author"],
                     "dm_sent": dm_sent, **({"message_id": dm_id} if dm_id else {})}
            self.record("review_revert", agent_id, event)
            reverted.append(event)
        return {"acted": bool(reverted), "kind": "revert", "plan": plan, "reverted": reverted}

    async def _rollback(self, agent_id, parsed, calls, messages, call_times, sandbox):
        cut = parsed["first_bad_call"]
        if cut is None:
            return {"acted": False, "kind": "rollback", "why": "no_first_bad_call"}
        if cut not in {c["id"] for c in calls}:
            return {"acted": False, "kind": "rollback", "why": "first_bad_call_not_in_window", "cut_tool_use_id": cut}
        cut_ids = async_review.calls_from(messages, cut)
        note = (async_review.rollback_note(len(cut_ids), parsed["reason"], self.neutral)
                if self.settings["show_reason"] else None)
        context = RollbackContext(agent_id=agent_id, cut_tool_use_id=cut, cut_time=call_times.get(cut),
                                  cut_tool_use_ids=cut_ids, messages=messages, call_times=call_times,
                                  conference=self.conference, sandbox=sandbox, record=self.record,
                                  run_dir=self.run_dir, history=self.history, neutral=self.neutral)
        apply = self.rollback or resolve_rollback()
        try:
            result = await apply(context, agent_id, cut, note)
        except Exception as ex:
            payload = {"agent": agent_id, "cut_tool_use_id": cut, "actions": len(cut_ids),
                       "error": type(ex).__name__, "detail": str(ex)[:300]}
            self.record("review_rollback_error", agent_id, payload)
            return {"acted": False, "kind": "rollback", "why": "rollback_error", **payload}
        payload = {"agent": agent_id, "cut_tool_use_id": cut, "actions": len(cut_ids), "note": note,
                   **(result if isinstance(result, dict) else {"result": result})}
        self.record("review_rollback", agent_id, payload)
        return {"acted": payload.get("ok", True) is not False, "kind": "rollback", **payload}


def reviewer_effective(options, settings):
    """settings.json ``effective.reviewer``: the reviewer's settings, prompt version and model."""
    return {**options, "prompt_version": async_review.REVIEW_PROMPT_VERSION, "model_id": settings.model_id,
            "provider_route": settings.provider_route, "max_tokens": settings.max_tokens,
            "reasoning_effort": settings.reasoning_effort, "timeout_seconds": REVIEW_TIMEOUT_SECONDS,
            "ledger_agent": "reviewer:<agent>", "reviewer_id": async_review.REVIEWER_ID,
            "stagger": "interval * (i + 1) / n after the common start", "call_log": CALL_LOG}
