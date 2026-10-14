#!/usr/bin/env python3
"""PAID live compatibility check: one genuine Claude Code agent on the frozen route.

docs/plan.md "Live compatibility check (October 13, 2026)": a separate $2 allowance
(ledger scope ``compatibility`` in the shared state/budget.sqlite), outside the $20
pilot and $5 analysis allowances, for one agent running a few turns on the frozen
OpenRouter route with explicit abort criteria (tool-call parsing, reasoning replay,
provider cost reporting, truncation behavior). The user must confirm immediately
before the paid call. Do not run it during the execution hold (docs/plan.md).

Refusals (nothing is spent and no credential is read):

* without ``--i-have-user-approval``;
* unless the ledger scope ``compatibility`` (budget.DEFAULT_LEDGER_PATH, opened with
  budget.DEFAULT_CAPS) exists with the approved $2 cap, has no unacknowledged
  estimate violation and at least ``--min-remaining`` left;
* unless the launch gates in ``REQUIRED_GATES`` pass (frozen manifest/dataset,
  approved settings, caps, a fresh route-pricing freeze equal to the launch
  settings, credential presence, and the unpaid runtime-e2e and runtime-isolation
  evidence), the pinned Inspect packages are installed, the launch interlock is clear
  (``runtime.launch_interlock``: no leftover judge container, no running comparator
  container, no agent sandbox of another run; no override for this paid check), and
  both image tags resolve to local image IDs;
* unless the operator types the remaining compatibility allowance (``--yes`` skips
  the prompt and may be used only when the user approved this exact run).

What runs is the production path (runtime helpers shared with ``run_pilot``):
pinned image IDs in the compose file and judge, the Lean warm-up before the start
barrier, the real conference service and operational judge in a throwaway
researcher-only run directory ``runs/compat-<UTC>/``, the real pilot prompt for one
persona, ``peer_solver`` and ``budget_filter`` on scope ``compatibility``, and the
OpenRouter model from ``runtime.build_openrouter_model`` (same
``settings_from_config``, ``capture_openrouter_cost`` and ``max_retries=0``). The host
credential is read only inside that builder, via ``secrets.load_openrouter_key``;
Inspect's own .env loading is disabled first (``disable_inspect_dotenv``), and the
evidence records ``credentials_in_environment`` measured after the run. Right after
loading the key the builder verifies, unpaid, that the key has a non-resetting
provider-side limit whose ``limit_remaining`` does not exceed the remaining
compatibility allowance (``provider_limit.verify_key_limit``); otherwise nothing is
spent. A key limited for the $20 pilot therefore needs its limit lowered (or a
separate key) for this check.

Reasoning-cap probe (required check ``reasoning_cap_enforced``): before the agent
runs, one researcher-side request (never shown to any agent) goes through the same
ledger and budget filter with ``max_tokens`` lowered to ``--cap-probe-max-tokens``
(default 1024) and a question that needs long reasoning. It verifies the
assumption behind every reservation (provider.reservation): the route stops at
max_tokens counting reasoning (stop reason ``max_tokens``, i.e. finish_reason
``length``), the provider-reported generated tokens including reasoning are at most
the cap, reasoning tokens are reported (> 0), and the billed cost stays within the
probe's reservation, which prices exactly the cap as output (no estimate_violation).
If the probe does not pass, the agent run is skipped (abort reason
``reasoning_cap_unverified``) and the evidence fails.

Abort criteria: no request is admitted after ``--max-requests`` (default 12)
admitted agent requests; ``--max-minutes`` (default 10) after the first inference;
the first permanent provider HTTP 4xx (excluding transient 408/429), converted
pre-generation rejection, tool-call parse failure or provider output error; any
estimate violation in the compatibility scope. An abort stops admission at once;
a request already admitted may finish (the solver waits up to the config's
``in_flight_grace_seconds``, default 120, for it to settle with its reported cost
before cancelling the agent; one still running then is cancelled and stays an
uncertain hold to reconcile).

Transient HTTP 408/429, server and transport failures propagate to the existing
bounded Claude Code retry mechanism without stopping the check. There is no extra
retry loop here; the request/time/allowance limits still apply. These preflight
criteria do not change the pilot's retry policy. Known transient attempt errors
are allowed in the final evidence only after a later successful provider-priced
inference by that agent; unknown or terminal errors still fail the check, and
unresolved billing holds still block launch until reconciled.

Writes data/validation/compatibility-<UTC YYYYMMDDTHHMMSSZ>.json (schema
``gdm-compatibility/1``), read by the launch gate ``compatibility``. ``request_ids``
lists every ledger request the check admitted (agent and probe); the gate checks the
ledger's current state of exactly those ids, so reconciling a hold afterwards is
supported. ``spent_usd`` is their committed total at run end (uncertain holds at
their full reservation).

    PYTHONPATH=src .venv/bin/python scripts/compat_check.py --i-have-user-approval
"""
from __future__ import annotations
import argparse
import asyncio
import dataclasses
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("INSPECT_DISPLAY", "plain")  # eval_async has no display argument

from research_swarm import runtime  # noqa: E402
from research_swarm.budget import DEFAULT_CAPS, DEFAULT_LEDGER_PATH, BudgetLedger, list_requests  # noqa: E402
from research_swarm.inspect_adapter import _rejected_without_generation, classify_generate_error  # noqa: E402
from research_swarm.provider import frozen_fields, settings_from_config  # noqa: E402

SCHEMA = "gdm-compatibility/1"
PREFIX = "compatibility"
SCOPE = "compatibility"
APPROVED_CAP_USD = Decimal("2")  # docs/plan.md "Live compatibility check (October 13, 2026)"
VALIDATION_DIR = ROOT / "data" / "validation"
DEFAULT_CONFIG = ROOT / "configs" / "pilot.json"
AGENT_ID = "agent-001"
COMPAT_MAX_CONNECTIONS = 1  # one agent, one in-flight request (as in the pilot: max_connections == agent count)
DEFAULT_MAX_REQUESTS = 12
DEFAULT_MAX_MINUTES = 10
DEFAULT_MIN_REMAINING_USD = "0.50"
CONFERENCE_PREFIX = "mcp__conference__"
# Launch gates (research_swarm.launch_gates.evaluate) that must pass before any paid call.
REQUIRED_GATES = ("fixed_task_manifest", "frozen_dataset", "approved_settings", "config_caps", "route_pricing",
                  "credential_presence", "runtime-e2e", "runtime-isolation")
# Shared interface with launch_gates' ``compatibility`` gate: every name below is a
# required check in the evidence; the optional ones are informational.
REQUIRED_CHECKS = ("tool_call_parsed", "conference_tool_call", "reasoning_replay_accepted", "provider_cost_reported",
                   "completion_within_max_tokens", "reasoning_cap_enforced", "no_estimate_violation", "route_pinned",
                   "no_sample_error")
OPTIONAL_CHECKS = ("truncation_observed", "compaction_observed")
# Abort reasons that mean the check failed (the others end it normally).
FAILURE_ABORTS = ("provider_4xx", "provider_rejection", "tool_call_parse_failure", "provider_output_error", "estimate_violation",
                  "admission_refused", "reasoning_cap_unverified", "monitor_error")
# Every ledger state (research_swarm.budget); request rows are looked up in all of them.
LEDGER_STATES = ("reserved", "uncertain", "settled", "estimate_violation", "violation_acknowledged")
# Reasoning-cap probe: researcher-side only, never part of any agent's context. The
# ledger/runtime agent id keeps its events apart from the agent's.
CAP_PROBE_AGENT = "cap-probe"
DEFAULT_CAP_PROBE_MAX_TOKENS = 1024
CAP_PROBE_PROMPT = ("Reason carefully and at length before you answer, and check every step twice. How many ordered "
                    "pairs of integers (x, y) satisfy x^2 + y^2 <= 10000? Derive the count by two independent methods, "
                    "reconcile any disagreement between them, and only then state the final number.")
# Claude Code's auto-compaction request (validate_runtime.py uses the same markers).
COMPACTION_MARKERS = ("create a detailed summary of the conversation", "CRITICAL: Respond with TEXT ONLY")
# Claude Code tool_result texts that mean the harness rejected a call without running it.
HARNESS_REJECTIONS = ("<tool_use_error>", "No such tool available", "InputValidationError")
_CREDENTIAL_LIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


def utc_stamp(moment=None):
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def rel(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Refusal checks (unpaid; no credential)

def ledger_problems(snapshot, min_remaining, caps=DEFAULT_CAPS):
    """Reasons to refuse based on the compatibility scope of a ledger snapshot."""
    problems = []
    default = caps.get(SCOPE)
    if default is None or Decimal(str(default)) != APPROVED_CAP_USD:
        problems.append(f"budget.DEFAULT_CAPS[{SCOPE!r}] is {default}, not the approved ${APPROVED_CAP_USD}")
    data = (snapshot or {}).get(SCOPE)
    if data is None:
        problems.append(f"ledger scope {SCOPE!r} does not exist")
        return problems
    if Decimal(data["cap_usd"]) != APPROVED_CAP_USD:
        problems.append(f"ledger scope {SCOPE!r} cap is ${data['cap_usd']}, not the approved ${APPROVED_CAP_USD}")
    if data.get("violation"):
        problems.append(f"ledger scope {SCOPE!r} has an unacknowledged estimate_violation")
    if Decimal(data["remaining_usd"]) < Decimal(str(min_remaining)):
        problems.append(f"remaining compatibility allowance ${data['remaining_usd']} is below the minimum "
                        f"${min_remaining}")
    return problems


def gate_problems(report, required=REQUIRED_GATES):
    evidence = (report or {}).get("evidence") or {}
    problems = []
    for name in required:
        result = evidence.get(name)
        if result is None:
            problems.append(f"{name}: launch gate not evaluated")
        elif not result.get("passed"):
            problems.append(f"{name}: {result.get('reason')}")
    return problems


def evaluate_gates(config):
    from research_swarm import launch_gates
    return launch_gates.evaluate(config, ROOT)


def environment_check(config, interlock=None):
    """(problems, pinned images): pinned packages, the launch interlock, image IDs (read-only Docker).

    The interlock (``runtime.launch_interlock``) refuses while a comparator container
    (label gdm-evaluator=1) runs, a judge container (gdm-judge=1) is left over, or an
    agent sandbox of another run runs, or when Docker cannot be listed. This paid
    check has no override."""
    from research_swarm.inspect_adapter import installed_versions
    problems, images = [], None
    versions = installed_versions()
    if not all(v["matches"] for v in versions.values()):
        problems.append("pinned Inspect runtime packages are not installed: "
                        + ", ".join(f"{k} {v['actual']} (expected {v['expected']})" for k, v in versions.items()
                                    if not v["matches"]))
    status = (interlock or runtime.launch_interlock)(runtime.image_tags(config)["agent"])
    problems += status["blockers"]
    try:
        images = runtime.pin_images(config)
    except runtime.LaunchBlocked as ex:
        problems.append(str(ex))
    return problems, images


def render_snapshot(path, snapshot):
    lines = [f"Compatibility allowance (ledger {rel(path)}, scope {SCOPE!r}, read before any paid call):"]
    data = (snapshot or {}).get(SCOPE)
    if data is None:
        lines.append("  scope not present")
    else:
        lines.append(f"  cap ${data['cap_usd']}  prior known costs ${data['settled_usd']}  "
                     f"unresolved holds ${data['held_usd']} ({data['unresolved_requests']})  "
                     f"remaining ${data['remaining_usd']}" + ("  VIOLATION" if data.get("violation") else ""))
    return "\n".join(lines)


def confirm(remaining, settings, args, ask=input):
    """True only if the operator types the remaining compatibility allowance exactly."""
    answer = ask(f"PAID compatibility check: one reasoning-cap probe request (max_tokens {args.cap_probe_max_tokens}), "
                 f"then one Claude Code agent on {settings.model_id} via route {settings.provider_route!r}, at most "
                 f"{args.max_requests} admitted agent requests and {args.max_minutes} minutes. Remaining compatibility "
                 f"allowance is ${remaining} (cap ${APPROVED_CAP_USD}). Type that amount in USD to start, anything else "
                 "aborts: ")
    try:
        return Decimal(str(answer).strip().lstrip("$").replace(",", "")) == Decimal(str(remaining))
    except (InvalidOperation, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Abort criteria around the production budget filter

def _is_compaction_request(messages):
    last_user = next((getattr(m, "text", "") for m in reversed(messages) if getattr(m, "role", None) == "user"), "")
    return any(marker in (last_user or "") for marker in COMPACTION_MARKERS)


def output_failures(output):
    """Abort reasons carried by a returned output (truncated partial calls are already stripped)."""
    failures = []
    # The pinned Inspect SDK converts context-overflow and input-moderation 4xx
    # responses into normal outputs without usage. Distinguish these refusals
    # from generated truncations (which carry usage) and max_tokens output caps.
    if _rejected_without_generation(output):
        failures.append({"reason": "provider_rejection", "stop_reason": output.stop_reason})
    for choice in getattr(output, "choices", None) or []:
        for call in choice.message.tool_calls or []:
            if call.parse_error:
                failures.append({"reason": "tool_call_parse_failure", "function": call.function,
                                 "parse_error": str(call.parse_error)[:300]})
    if getattr(output, "error", None):
        failures.append({"reason": "provider_output_error", "error": str(output.error)[:300]})
    return failures


class AbortGuard:
    """Wraps the production ``budget_filter`` with this check's abort criteria.

    An abort requests the run-wide stop: the filter then admits nothing further, and
    the solver ends the agent once a request already admitted has settled (bounded
    by ``in_flight_grace_seconds``; see ``runtime.peer_solver``).
    """
    def __init__(self, inner, clock, record, ledger, *, agent_id=AGENT_ID, max_requests=DEFAULT_MAX_REQUESTS,
                 scope=SCOPE):
        self.inner, self.clock, self.record, self.ledger = inner, clock, record, ledger
        self.agent_id, self.max_requests, self.scope = agent_id, int(max_requests), scope
        self.state = getattr(inner, "state", None)  # peer_solver reads filter.state at agent exit
        self.reason = None
        self.compaction_requests = 0

    def abort(self, reason, **detail):
        if self.reason is None:
            self.reason = reason
            self.record("compatibility_abort", self.agent_id, {"reason": reason, **detail})
        self.clock.request_stop(reason, self.record)

    def _violation(self):
        data = self.ledger.snapshot().get(self.scope) or {}
        return bool(data.get("violation"))

    async def __call__(self, model, messages, tools, tool_choice, config):
        admitted = (self.state or {}).get("requests", 0)
        if admitted >= self.max_requests:
            self.abort("max_requests", admitted=admitted)
        if _is_compaction_request(messages):
            self.compaction_requests += 1
            self.record("compaction_request", self.agent_id, {"count": self.compaction_requests})
        try:
            output = await self.inner(model, messages, tools, tool_choice, config)
        except BaseException as ex:
            info = classify_generate_error(ex)
            status = info.get("status")
            if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
                self.abort("provider_4xx", status=status, provider_error=info.get("provider_error"),
                           provider_generation_id=info.get("provider_generation_id"))
            elif status in (408, 429) or (isinstance(status, int) and 500 <= status < 600) or info.get("reason") in (
                    "connection_not_established", "connection_lost_or_timeout"):
                # Inspect preserves each failed attempt as a ModelEvent.error,
                # even if Claude Code subsequently recovers. Record its exact
                # SDK-formatted message so evidence can identify known transient
                # failures without guessing from arbitrary error text.
                from inspect_ai._util.error import exception_message
                self.record("compatibility_transient_error", self.agent_id,
                            {"error": exception_message(ex)[:500], "status": status,
                             "reason": info.get("reason")})
            elif type(ex).__name__ == "LimitExceededError" and not self.clock.stop.is_set():
                self.abort("admission_refused", error=str(ex)[:300])
            raise
        failures = output_failures(output)
        if failures:
            self.abort(failures[0]["reason"], failures=failures)
        if await asyncio.to_thread(self._violation):
            self.abort("estimate_violation")
        return output


# ---------------------------------------------------------------------------
# Evidence (pure functions over runtime.jsonl events, conference events and the sample)

def sample_activity(sample):
    """Tool calls (from model outputs) and tool results (from model inputs and the final
    messages) of one Inspect sample; bridged and built-in tools emit no ToolEvents."""
    activity = {"tool_calls": [], "tool_results": {}, "model_errors": [], "compaction_events": 0,
                "sample_error": None, "sample_limit": None}
    if sample is None:
        activity["sample_error"] = "no sample in the Inspect log"
        return activity

    def take(messages):
        for message in messages or []:
            if getattr(message, "role", None) == "tool" and getattr(message, "tool_call_id", None):
                error = getattr(message, "error", None)
                activity["tool_results"][message.tool_call_id] = {
                    "function": getattr(message, "function", None), "text": (message.text or "")[:4000],
                    "error": None if error is None else str(getattr(error, "message", error))[:500]}

    for event in getattr(sample, "events", None) or []:
        kind = getattr(event, "event", None)
        if kind == "compaction":
            activity["compaction_events"] += 1
        if kind != "model":
            continue
        take(getattr(event, "input", None))
        if getattr(event, "error", None):
            activity["model_errors"].append(str(event.error)[:500])
        output = getattr(event, "output", None)
        message = getattr(output, "message", None) if output is not None and getattr(output, "choices", None) else None
        for call in (getattr(message, "tool_calls", None) or []) if message is not None else []:
            activity["tool_calls"].append({"id": call.id, "function": call.function,
                                           "parse_error": None if call.parse_error is None else str(call.parse_error)})
    take(getattr(sample, "messages", None))
    error = getattr(sample, "error", None)
    activity["sample_error"] = None if error is None else str(getattr(error, "message", error))[:1500]
    limit = getattr(sample, "limit", None)
    activity["sample_limit"] = None if limit is None else getattr(limit, "type", str(limit))
    return activity


def reasoning_tokens(finished):
    raw = finished.get("provider_usage") or {}
    usage = finished.get("usage") or {}
    return int((raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or usage.get("reasoning_tokens")
               or 0)


def completion_tokens(finished):
    """Generated tokens of one inference including reasoning, or None without usage.

    OpenRouter/OpenAI-compatible ``completion_tokens`` include reasoning; if a route
    reported reasoning above completion, the two are treated as separate and added."""
    raw = finished.get("provider_usage") or {}
    usage = finished.get("usage") or {}
    completion = raw.get("completion_tokens", usage.get("output_tokens"))
    if completion is None:
        return None
    reasoning = reasoning_tokens(finished)
    return int(completion) if reasoning <= int(completion) else int(completion) + reasoning


def _norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def route_consistent(finished, settings):
    name, model = finished.get("provider_name"), finished.get("provider_model")
    route = _norm(settings.provider_route.split("/")[0])
    name_ok = bool(name) and _norm(name).startswith(route)
    model_ok = bool(model) and (model == settings.model_id or re.fullmatch(re.escape(settings.model_id) + r"[-:@].+",
                                                                           model) is not None)
    return name_ok and model_ok


def _check(passed, detail, required=True):
    return {"passed": bool(passed), "required": required, "detail": detail}


def unrecovered_model_errors(activity, events, agent_id):
    """Keep unknown/terminal model errors; excuse only recorded transient attempts
    followed by a successful, provider-priced inference by the same agent.

    Matching uses the exact Inspect error text and multiplicity. A retry that
    merely starts, fails, or produces an error/rejection does not establish recovery.
    Billing uncertainty is separate: launch_gates checks the current ledger rows,
    so an otherwise recovered request still blocks launch until reconciled.
    """
    pending, recovered = Counter(), Counter()
    for event in events:
        if event.get("agent_id") != agent_id:
            continue
        payload = event["payload"]
        if event["kind"] == "compatibility_transient_error" and payload.get("error"):
            pending[payload["error"]] += 1
        elif (event["kind"] == "inference_finished" and not payload.get("error")
              and payload.get("basis") == "provider_reported_cost"):
            recovered.update(pending)
            pending.clear()
    errors = []
    for error in activity["model_errors"]:
        if recovered[error] > 0:
            recovered[error] -= 1
        else:
            errors.append(error)
    return errors


def cap_probe_check(events, settings, violation_ids=()):
    """``reasoning_cap_enforced`` from the probe's runtime.jsonl events (agent id CAP_PROBE_AGENT).

    Passes only when the single probe request, sent with max_tokens = the recorded
    cap, finished with stop reason ``max_tokens`` (finish_reason ``length``) on the
    pinned route; the provider-reported generated tokens including reasoning
    (``completion_tokens``) are at most the cap; reasoning tokens were reported
    (> 0, so reasoning demonstrably counted toward the cap); and the provider-reported
    cost settled within the probe's reservation (prompt bound plus exactly the cap at
    the maximum output price), i.e. the request is not an estimate violation.
    """
    probe = [e for e in events if e.get("agent_id") == CAP_PROBE_AGENT]
    started = next((e["payload"] for e in probe if e["kind"] == "reasoning_cap_probe_started"), None)
    cap = (started or {}).get("max_tokens")
    admitted = [e["payload"] for e in probe if e["kind"] == "inference_admitted"]
    finished = [e["payload"] for e in probe if e["kind"] == "inference_finished"]
    failures = [{"kind": e["kind"], **{k: e["payload"].get(k) for k in ("reason", "status", "error")}}
                for e in probe if e["kind"] in ("inference_unbilled", "inference_uncertain", "inference_not_admitted")]
    last = finished[-1] if finished else None
    generated = completion_tokens(last) if last else None
    reasoning = reasoning_tokens(last) if last else 0
    violated = bool(last) and last.get("request_id") in set(violation_ids)
    stop_reason = (last or {}).get("stop_reason")
    passed = (isinstance(cap, int) and cap > 0 and len(admitted) == 1 and len(finished) == 1 and not failures
              and not last.get("error") and stop_reason == "max_tokens" and generated is not None
              and generated <= cap and reasoning > 0 and last.get("basis") == "provider_reported_cost"
              and not violated and route_consistent(last, settings))
    if started is None:
        detail = "the reasoning-cap probe did not run"
    else:
        detail = (f"probe max_tokens {cap}: {len(admitted)} admitted, {len(finished)} finished, "
                  f"{len(failures)} failed {failures[:2]}; stop reason {stop_reason!r} (required 'max_tokens'); "
                  f"provider-reported generated tokens incl. reasoning {generated} (reasoning {reasoning}; required "
                  f"<= {cap} with reasoning > 0); settled {(last or {}).get('settled_usd')} by "
                  f"{(last or {}).get('basis')!r} within the probe reservation: {not violated and bool(last)}; "
                  f"route consistent: {bool(last) and route_consistent(last, settings)}")
    return passed, detail, {"max_tokens": cap, "stop_reason": stop_reason, "generated_tokens": generated,
                            "reasoning_tokens": reasoning, "request_ids": [a.get("request_id") for a in admitted]}


def build_checks(*, agent_id, events, conference_events, activity, settings, abort_reason, violation_ids,
                 scope_violation=False):
    """The shared-interface checks {name: {passed, required, detail}}."""
    kinds = defaultdict(list)
    for event in events:
        if event.get("agent_id") in (agent_id, None):
            kinds[event["kind"]].append(event["payload"])
    admitted, finished = kinds["inference_admitted"], kinds["inference_finished"]
    finished_by_id = {f.get("request_id"): f for f in finished}
    calls, results = activity["tool_calls"], activity["tool_results"]

    def executed(call):
        result = results.get(call["id"])
        return result is not None and not any(r in (result["text"] or "") for r in HARNESS_REJECTIONS)

    parse_failures = [c for c in calls if c["parse_error"]]
    bash = [c for c in calls if c["function"] == "Bash" and not c["parse_error"] and executed(c)]

    def identity_bound(call):
        tool = call["function"][len(CONFERENCE_PREFIX):]
        try:
            reply = json.loads(results[call["id"]]["text"])
        except (KeyError, TypeError, ValueError):
            return False
        return isinstance(reply, dict) and reply.get("ok") is True and any(
            e.get("agent_id") == agent_id and e.get("tool") == tool for e in conference_events)
    conference = [c for c in calls if c["function"].startswith(CONFERENCE_PREFIX) and not c["parse_error"]
                  and executed(c)]
    bound = [c for c in conference if identity_bound(c)]

    replays = [a for a in admitted if (a.get("replayed_reasoning_blocks") or 0) > 0]
    accepted = [a for a in replays if a.get("request_id") in finished_by_id
                and not finished_by_id[a["request_id"]].get("error")
                and finished_by_id[a["request_id"]].get("basis") != "unbilled_error"]
    reasoning_outputs = sum(1 for f in finished if reasoning_tokens(f) > 0)

    bases = Counter(f.get("basis") for f in finished)
    tokens = [completion_tokens(f) for f in finished]
    over = [t for t in tokens if t is None or t > settings.max_tokens]
    off_route = [{"provider_name": f.get("provider_name"), "provider_model": f.get("provider_model")}
                 for f in finished if not route_consistent(f, settings)]
    provider_errors = len(kinds["inference_unbilled"]) + len(kinds["inference_uncertain"])
    truncations = kinds["truncated_response"]
    compaction_requests = len(kinds["compaction_request"])
    probe_passed, probe_detail, _ = cap_probe_check(events, settings, violation_ids)
    model_errors = unrecovered_model_errors(activity, events, agent_id)

    checks = {
        "tool_call_parsed": _check(
            bash and not parse_failures and abort_reason != "tool_call_parse_failure",
            f"{len(bash)} built-in Bash call(s) parsed and executed; {len(parse_failures)} tool call(s) with a parse "
            f"error out of {len(calls)}"),
        "conference_tool_call": _check(
            bool(bound), f"{len(bound)} of {len(conference)} executed {CONFERENCE_PREFIX}* call(s) returned ok and "
                         f"appear in the conference log under {agent_id}"),
        "reasoning_replay_accepted": _check(
            bool(accepted), f"{len(accepted)} of {len(replays)} request(s) replaying earlier reasoning returned "
                            f"without provider error; {reasoning_outputs} response(s) reported reasoning tokens"),
        "provider_cost_reported": _check(
            bool(finished) and set(bases) == {"provider_reported_cost"},
            f"settlement bases of {len(finished)} finished inference(s): {dict(bases)}; "
            f"{provider_errors} unbilled/uncertain provider error(s)"),
        "completion_within_max_tokens": _check(
            bool(finished) and not over,
            f"max generated tokens incl. reasoning {max((t for t in tokens if t is not None), default=None)} "
            f"(limit {settings.max_tokens}); {len(over)} response(s) over the limit or without usage"),
        "reasoning_cap_enforced": _check(probe_passed, probe_detail),
        "no_estimate_violation": _check(
            not violation_ids and not scope_violation and abort_reason != "estimate_violation",
            f"{len(violation_ids)} request(s) of this check in estimate_violation; scope violation flag "
            f"{bool(scope_violation)}"),
        "route_pinned": _check(
            bool(finished) and not off_route,
            f"{len(finished) - len(off_route)} of {len(finished)} finished inference(s) served by route "
            f"{settings.provider_route!r} as {settings.model_id}"
            + (f"; inconsistent: {off_route[:3]}" if off_route else "")),
        "no_sample_error": _check(
            activity["sample_error"] is None and not model_errors
            and abort_reason not in ("provider_4xx", "provider_rejection", "provider_output_error", "admission_refused",
                                     "reasoning_cap_unverified", "monitor_error"),
            f"sample error {activity['sample_error']!r}; {len(model_errors)} unrecovered/unknown out of "
            f"{len(activity['model_errors'])} model event error(s); "
            f"abort reason {abort_reason!r}"),
        "truncation_observed": _check(
            bool(truncations),
            f"{len(truncations)} truncated response(s); stop reasons "
            f"{sorted({t.get('stop_reason') for t in truncations if t.get('stop_reason')})}", required=False),
        "compaction_observed": _check(
            activity["compaction_events"] > 0 or compaction_requests > 0,
            f"{activity['compaction_events']} compaction event(s), {compaction_requests} compaction request(s)",
            required=False),
    }
    return checks


def build_evidence(*, checks, images, settings, spent_usd, request_ids, created_at=None, **details):
    """Evidence record of the shared interface (launch gate ``compatibility``).

    ``request_ids`` (X4): every ledger request id the check admitted (agent and
    probe); ``requests`` is their count.
    """
    checks = dict(checks)
    for name in REQUIRED_CHECKS:  # a missing required check counts as failed
        checks.setdefault(name, _check(False, "check was not computed"))
    for name in REQUIRED_CHECKS:
        checks[name]["required"] = True
    request_ids = list(dict.fromkeys(str(i) for i in request_ids))
    record = {"schema": SCHEMA, "created_at": created_at or iso_now(),
              "all_required_passed": all(c["passed"] for c in checks.values() if c["required"]),
              "image_ids": {"agent": images["agent"]["id"], "lean": images["lean"]["id"]},
              "model_id": settings.model_id, "provider_route": settings.provider_route,
              "settings": frozen_fields(settings), "spent_usd": str(spent_usd), "requests": len(request_ids),
              "request_ids": request_ids, "checks": checks, **details}
    return record


def write_evidence(directory, stamp, record):
    """compatibility-<stamp>.json; credential-like strings are redacted defensively."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{PREFIX}-{stamp}.json"
    text = _CREDENTIAL_LIKE.sub("[redacted-credential-like-string]", json.dumps(record, indent=2, default=str))
    path.write_text(text + "\n")
    return path


def admitted_request_ids(events):
    """Ledger request ids admitted during the check (agent and probe), in admission order."""
    return list(dict.fromkeys(e["payload"]["request_id"] for e in events if e["kind"] == "inference_admitted"))


def request_rows(ledger_path, request_ids, scope=SCOPE):
    """{request_id: ledger row} for ``request_ids`` in every ledger state (read-only)."""
    wanted = set(request_ids)
    return {r["request_id"]: r for r in list_requests(ledger_path, states=LEDGER_STATES, scope=scope)
            if r["request_id"] in wanted}


def committed_usd(rows):
    """Committed total of ledger rows: the actual cost, or the full reservation while unresolved."""
    return sum((Decimal(r["actual_usd"] if r["actual_usd"] is not None else r["reserved_usd"]) for r in rows),
               Decimal(0))


def summarize_run(result, settings, ledger_path):
    """(checks, spent_usd, request_ids, estimate-violation ids, ledger details) from a
    finished run (``run_check`` result). ``spent_usd`` is the committed total of exactly
    the requests this check admitted."""
    events = result["runtime_events"]
    request_ids = admitted_request_ids(events)
    rows = request_rows(ledger_path, request_ids)
    violations = [i for i in request_ids if i in rows
                  and rows[i]["state"] in ("estimate_violation", "violation_acknowledged")]
    before, after = result["ledger_before"], result["ledger_after"]
    spent = committed_usd(rows.values())
    abort_reason = result.get("abort_reason")
    checks = build_checks(agent_id=AGENT_ID, events=events, conference_events=result["conference_events"],
                          activity=result["activity"], settings=settings, abort_reason=abort_reason,
                          violation_ids=violations, scope_violation=bool(after.get("violation")))
    states = Counter(rows[i]["state"] for i in request_ids if i in rows)
    details = {"committed_delta_usd": str(Decimal(after["committed_usd"]) - Decimal(before["committed_usd"])),
               "request_states": dict(states), "missing_request_ids": [i for i in request_ids if i not in rows]}
    return checks, spent, request_ids, violations, details


# ---------------------------------------------------------------------------
# The paid run (production pieces)

def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _read_log(run_dir, logs):
    from inspect_ai.log import read_eval_log
    try:
        # Resolve attachments: logged model inputs store tool results as attachment:// refs.
        if logs:
            return read_eval_log(logs[0].location, resolve_attachments=True)
        files = sorted((Path(run_dir) / "inspect").glob("*.eval"))
        return read_eval_log(str(files[-1]), resolve_attachments=True) if files else None
    except Exception as ex:  # evidence is still written, with the sample marked missing
        print(f"could not read the Inspect log: {type(ex).__name__}: {ex}", file=sys.stderr)
        return None


async def reasoning_cap_probe(model, ledger, settings, record, *, cap=DEFAULT_CAP_PROBE_MAX_TOKENS, scope=SCOPE):
    """One researcher-side request through the production budget filter with
    ``max_tokens=cap`` (reserved and settled in the ledger like any other request).

    Its runtime events carry agent id CAP_PROBE_AGENT; ``cap_probe_check`` evaluates
    them. Returns the exception the request raised, or None.
    """
    from inspect_ai.model import ChatMessageUser, GenerateConfig
    from research_swarm.inspect_adapter import budget_filter
    probe_settings = dataclasses.replace(settings, max_tokens=int(cap))
    record("reasoning_cap_probe_started", CAP_PROBE_AGENT, {"max_tokens": int(cap)})
    # Its own barrier (no records): the probe is not a peer and must not touch the run's start barrier.
    intercept = budget_filter(CAP_PROBE_AGENT, ledger, probe_settings, runtime.StartBarrier(1), record, scope)
    try:
        await intercept(model, [ChatMessageUser(content=CAP_PROBE_PROMPT)], [], None, GenerateConfig())
    except Exception as ex:  # recorded by the filter (inference_unbilled/uncertain/not_admitted)
        return ex
    return None


async def run_check(config, args, stamp, *, images, ledger, ledger_path):
    from inspect_ai import eval_async
    from research_swarm.inspect_adapter import (InstructionArchive, budget_filter, claude_code_env,
                                                credentials_in_environment, disable_inspect_dotenv)
    from research_swarm.prompts import INITIAL_MESSAGE, build_prompt
    from research_swarm.tools import TOOL_DEFINITIONS
    dotenv = disable_inspect_dotenv()  # before anything Inspect runs; the builder below is the only .env reader
    persona = args.persona or runtime.assign_personas(
        runtime.APPROVED_AGENT_COUNT, int(config.get("persona_seed", runtime.DEFAULT_PERSONA_SEED)))[AGENT_ID]
    personas = {AGENT_ID: persona}
    mode = config.get("system_prompt_mode", "append")
    auto_memory = bool(config.get("claude_auto_memory", True))
    in_flight_grace = runtime.in_flight_grace_seconds(config)
    run_dir = (Path(args.runs_dir) / f"compat-{stamp}").resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    record = runtime.make_recorder(run_dir / "runtime.jsonl")
    record("inspect_dotenv_disabled", None, dotenv)
    record("images_pinned", None, images)
    # live_compatibility_verified stays False: this run is what establishes it.
    settings = settings_from_config(config, pricing_verified=True, live_compatibility_verified=False)
    listing, task_files = runtime.export_public_tasks(ROOT)
    warm_up = None
    if not args.no_warm_up:
        warm_up = await asyncio.to_thread(runtime.warm_up_lean, images["lean"]["id"], record,
                                          **runtime.warm_up_options(config))
    before = ledger.snapshot()[SCOPE]
    conference, compiler, observer = runtime.make_conference(ROOT, run_dir, [AGENT_ID], config, record,
                                                             lean_image=images["lean"]["id"])
    guards, logs, cancelled, probe_error, provider_limit = {}, None, False, None, None
    barrier = runtime.StartBarrier(1, record)
    clock = runtime.RunClock(barrier)
    try:
        archive = InstructionArchive(run_dir / "instructions", record)

        def filter_for(agent_id):
            inner = budget_filter(agent_id, ledger, settings, barrier, record, SCOPE, archive, clock.stop)
            guards[agent_id] = AbortGuard(inner, clock, record, ledger, agent_id=agent_id,
                                          max_requests=args.max_requests)
            return guards[agent_id]
        compose = runtime.make_compose(run_dir, config, images["agent"]["id"])
        solver = runtime.peer_solver(conference=conference, definitions=TOOL_DEFINITIONS, personas=personas,
                                     prompt_for=lambda a, p: build_prompt(a, p, listing), filter_for=filter_for,
                                     run_dir=run_dir, record=record, clock=clock, system_prompt_mode=mode,
                                     auto_memory=auto_memory, in_flight_grace=in_flight_grace)
        task = runtime.build_task(agent_ids=[AGENT_ID], files=runtime.agent_files(task_files), solver=solver,
                                  compose=compose, metadata={"condition": "paid-compatibility-check",
                                                             "personas": personas},
                                  initial_message=INITIAL_MESSAGE)
        # The credential is read here (inside the shared builder) and nowhere else; the
        # key's provider-side limit is verified against the remaining compatibility
        # allowance (unpaid) before any paid request.
        # One agent: one in-flight request (the pilot's rule, max_connections == agent count).
        model, provider_limit = runtime.build_openrouter_model(settings, ROOT, COMPAT_MAX_CONNECTIONS,
                                                               max_remaining_usd=before["remaining_usd"],
                                                               record=record)
        (run_dir / "settings.json").write_text(json.dumps({
            "config": config, "model": frozen_fields(settings), "personas": personas, "tasks": listing,
            "effective": {"images": images, "lean_warm_up": warm_up, "budget_scope": SCOPE,
                          "ledger_path": str(ledger_path), "system_prompt_mode": mode, "claude_auto_memory": auto_memory,
                          "claude_code_env": claude_code_env(auto_memory),
                          "compose_sha256": runtime.compose_fingerprint(config, images["agent"]["id"]),
                          "judge": {k: getattr(compiler, k, None) for k in ("image", "timeout", "memory", "cpus",
                                                                            "max_concurrent")},
                          "evaluator_observer": None if observer is None else "queue",
                          "inspect_dotenv": dotenv, "provider_limit": provider_limit,
                          "max_connections": COMPAT_MAX_CONNECTIONS,
                          "limits": {"max_requests": args.max_requests, "max_minutes": args.max_minutes,
                                     "cap_probe_max_tokens": args.cap_probe_max_tokens,
                                     "in_flight_grace_seconds": in_flight_grace}},
            "budget_before": before}, indent=2, default=str))
        probe_error = await reasoning_cap_probe(model, ledger, settings, record, cap=args.cap_probe_max_tokens)
        probe_violations = [r["request_id"] for r in list_requests(ledger_path, states=("estimate_violation",),
                                                                   scope=SCOPE)]
        probe_passed, probe_detail, probe_facts = cap_probe_check(_read_jsonl(run_dir / "runtime.jsonl"), settings,
                                                                  probe_violations)
        record("reasoning_cap_probe", CAP_PROBE_AGENT, {"passed": probe_passed, "detail": probe_detail,
                                                        "error": None if probe_error is None
                                                        else type(probe_error).__name__, **probe_facts})
        if not probe_passed:
            # The reservation's core assumption is unverified: spend nothing on the agent.
            clock.request_stop("reasoning_cap_unverified", record)
            record("compatibility_abort", CAP_PROBE_AGENT, {"reason": "reasoning_cap_unverified",
                                                            "detail": probe_detail})
        else:
            try:
                logs = await runtime.supervise(eval_async(task, model=model,
                                                          **runtime.eval_options(1, run_dir, config)),
                                               clock, conference, record, config,
                                               max_run_seconds=args.max_minutes * 60)
            except asyncio.CancelledError:  # evaluation cancelled after the stop grace; evidence still written
                cancelled = True
    finally:
        await asyncio.to_thread(conference.close)
        after = ledger.snapshot()[SCOPE]
        (run_dir / "budget-final.json").write_text(json.dumps(ledger.snapshot(), indent=2))
        credentials = credentials_in_environment()  # measured after the run (presence only)
    log = _read_log(run_dir, logs)
    sample = log.samples[0] if log is not None and log.samples else None
    guard = guards.get(AGENT_ID)
    abort_reason = (guard.reason if guard is not None else None) or (
        "max_minutes" if clock.reason == "max_run_seconds" else clock.reason)
    events = _read_jsonl(run_dir / "runtime.jsonl")
    return {"run_dir": run_dir, "settings": settings, "persona": persona, "warm_up": warm_up,
            "ledger_before": before, "ledger_after": after, "abort_reason": abort_reason, "cancelled": cancelled,
            "activity": sample_activity(sample), "runtime_events": events,
            "conference_events": _read_jsonl(run_dir / "conference" / "events.jsonl"),
            "agent_exit": next((e["payload"] for e in events if e["kind"] == "agent_exit"), None),
            "inspect_log": None if log is None else Path(log.location).name,
            "credentials_in_environment": credentials, "provider_limit": provider_limit,
            "compose_sha256": runtime.compose_fingerprint(config, images["agent"]["id"]),
            "cap_probe_error": None if probe_error is None else f"{type(probe_error).__name__}: {probe_error}"[:500]}


# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--i-have-user-approval", action="store_true",
                        help="required: the user approved this paid check immediately before it runs")
    parser.add_argument("--yes", action="store_true", help="skip the typed confirmation (user-approved run only)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES)
    parser.add_argument("--min-remaining", default=DEFAULT_MIN_REMAINING_USD,
                        help="refuse unless at least this much compatibility allowance remains (USD)")
    parser.add_argument("--persona", choices=runtime.PERSONA_NAMES,
                        help="default: agent-001's persona in the pilot allocation")
    parser.add_argument("--no-warm-up", action="store_true")
    parser.add_argument("--cap-probe-max-tokens", type=int, default=DEFAULT_CAP_PROBE_MAX_TOKENS,
                        help="max_tokens of the reasoning-cap probe request (must be below the frozen max_tokens)")
    parser.add_argument("--output-dir", default=str(VALIDATION_DIR))
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"), help="researcher-only run artifacts")
    args = parser.parse_args(argv)
    if args.max_requests < 1 or args.max_minutes <= 0:
        parser.error("--max-requests and --max-minutes must be positive")
    if args.cap_probe_max_tokens < 16:
        parser.error("--cap-probe-max-tokens must be at least 16")
    return args


def main(argv=None, *, ask=input, gates=None, environment=None, run=None, ledger_file=None, isatty=None):
    args = parse_args(argv)
    if not args.i_have_user_approval:
        print("Refusing: this check makes PAID OpenRouter calls from the $2 compatibility allowance. Pass "
              "--i-have-user-approval only after the user approved this run. Nothing was spent and no credential "
              "was read.")
        return 2
    try:
        min_remaining = Decimal(str(args.min_remaining))
    except InvalidOperation:
        print(f"Refusing: --min-remaining {args.min_remaining!r} is not a USD amount.")
        return 2
    config = json.loads(Path(args.config).read_text())
    try:
        settings = settings_from_config(config, pricing_verified=True)
    except ValueError as ex:
        print(f"Refusing: provider settings in {args.config} are invalid ({ex}). Nothing was spent.")
        return 2
    path = Path(ledger_file) if ledger_file else DEFAULT_LEDGER_PATH(ROOT)
    problems, ledger, snapshot = [], None, None
    try:
        ledger = BudgetLedger(path)  # creates the scopes with DEFAULT_CAPS; refuses a differing existing cap
        snapshot = ledger.snapshot()
    except ValueError as ex:  # e.g. a ledger created while the compatibility cap was $0
        problems.append(f"ledger {rel(path)}: {ex} (an approved cap change is applied with scripts/preflight.py "
                        "--raise-ledger-cap, never by resetting the ledger)")
    if ledger is not None:
        problems += ledger_problems(snapshot, min_remaining)
    problems += gate_problems((gates or evaluate_gates)(config))
    env_problems, images = (environment or environment_check)(config)
    problems += env_problems
    print(render_snapshot(path, snapshot))
    if args.cap_probe_max_tokens >= settings.max_tokens:
        problems.append(f"--cap-probe-max-tokens {args.cap_probe_max_tokens} must be below the frozen max_tokens "
                        f"{settings.max_tokens} (the probe must truncate below the agent's cap)")
    if problems:
        print("Refusing (nothing was spent and no credential was read):")
        print("\n".join(f"  - {p}" for p in problems))
        return 2
    remaining = snapshot[SCOPE]["remaining_usd"]
    if not args.yes:
        if not (isatty or sys.stdin.isatty)() and ask is input:
            print("Refusing: confirmation needs an interactive terminal (or --yes after user approval).")
            return 2
        if not confirm(remaining, settings, args, ask):
            print("Not running: confirmation did not match. Nothing was spent.")
            return 2
    stamp = utc_stamp()
    started = time.time()
    try:
        result = asyncio.run((run or run_check)(config, args, stamp, images=images, ledger=ledger, ledger_path=path))
    except Exception as ex:  # no evidence without a finished run; show what the scope now holds
        print(f"Compatibility check aborted before evidence could be written: {type(ex).__name__}: {ex}")
        print(render_snapshot(path, ledger.snapshot()))
        return 1
    checks, spent, request_ids, violations, ledger_details = summarize_run(result, result.get("settings") or settings,
                                                                           path)
    credentials = result.get("credentials_in_environment")
    if credentials is None:  # measured after the run (presence only)
        from research_swarm.inspect_adapter import credentials_in_environment
        credentials = credentials_in_environment()
    record = build_evidence(
        checks=checks, images=images, settings=result.get("settings") or settings, spent_usd=spent,
        request_ids=request_ids,
        kind="paid live compatibility check (one genuine Claude Code agent, frozen OpenRouter route)",
        agent_id=AGENT_ID, persona=result.get("persona"), abort_reason=result.get("abort_reason"),
        evaluation_cancelled=bool(result.get("cancelled")), agent_exit=result.get("agent_exit"),
        limits={"max_requests": args.max_requests, "max_minutes": args.max_minutes,
                "min_remaining_usd": str(min_remaining), "cap_probe_max_tokens": args.cap_probe_max_tokens},
        ledger={"path": rel(path), "scope": SCOPE, "before": result["ledger_before"],
                "after": result["ledger_after"], "estimate_violation_requests": violations, **ledger_details},
        run_dir=rel(result["run_dir"]), inspect_log=result.get("inspect_log"), lean_warm_up=result.get("warm_up"),
        tool_calls=[{k: c[k] for k in ("function", "parse_error")} for c in result["activity"]["tool_calls"]],
        duration_seconds=round(time.time() - started, 1), credential_source="host only (secrets.load_openrouter_key)",
        credentials_in_environment=bool(credentials), provider_limit=result.get("provider_limit"),
        compose_sha256=result.get("compose_sha256"), cap_probe_error=result.get("cap_probe_error"))
    out = write_evidence(args.output_dir, stamp, record)
    print(json.dumps({k: (v["passed"], v["required"]) for k, v in record["checks"].items()}, indent=2))
    print(f"spent ${record['spent_usd']} in {record['requests']} admitted request(s); "
          f"abort reason {record['abort_reason']!r}")
    print(f"wrote {rel(out)}; all_required_passed={record['all_required_passed']}")
    return 0 if record["all_required_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
