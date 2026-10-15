"""Genuine Inspect SWE integration. Optional Inspect imports happen only at use.

Verified against the installed sources of inspect-ai 0.3.279, inspect-swe 0.2.71
and the pinned Claude Code 2.1.296 binary (static inspection, October 13, 2026);
see docs/runtime.md. Runtime evidence is the mock end-to-end record
data/validation/runtime-e2e-<UTC>.json (scripts/validate_runtime.py), pending its
Phase B run. Findings that shape this module:

* ``claude_code(system_prompt=...)`` APPENDS to Claude Code's built-in prompt
  (``--append-system-prompt``); ``replace_system_prompt=...`` replaces it
  (``--system-prompt``). The two cannot be combined.
* ``effort`` sets ``GenerateConfig.reasoning_effort`` on the served model; the
  bridge drops client generation parameters (``forward_generation_config=False``),
  so the filter below is what freezes request settings.
* A filter that calls ``model.generate`` itself is a supported path; the bridge
  registers tool-execution grants from the returned output, so stripping tool
  calls here means a truncated call can never execute (bridged tools default to
  ``require_proposal=True``).
* The bridge maps ``stop_reason`` ``max_tokens``/``model_length`` to Anthropic
  ``max_tokens``. Claude Code 2.1.296 then turns it into a ``max_output_tokens``
  API-error message and recovers up to 3 consecutive times per turn by injecting
  a meta user message ("Output token limit hit. Resume directly ..."). A 4th
  consecutive truncation ends the turn, i.e. the ``-p`` session. The filter keeps
  ``stop_reason`` intact (so that recovery path runs) and records consecutive
  truncations so the runtime can tell a truncation-ended session from a
  voluntary exit.
* The in-sandbox proxy answers Anthropic streaming requests with ``message_start``
  immediately and ``ping`` events every 5 s while the host generates, so Claude
  Code's stream first-byte/idle watchdogs (180-300 s) do not fire during long
  generations or while the first request waits at the start barrier.
* Inspect's OpenRouter provider does NOT read OpenRouter's reported ``usage.cost``.
  ``ModelUsage.total_cost`` is computed from Inspect's own model-info price table
  (absent for deepseek/deepseek-v4.1-flash, so it is ``None``). It must never be
  treated as provider billing. ``capture_openrouter_cost`` copies the raw
  reported usage (and the generation id) into ``output.metadata``; settlement
  uses only that.
* A ``LimitExceededError`` raised during a bridged generation ends the sample.
  Other exceptions are forwarded to the CLI as provider errors with their HTTP
  status, which Claude Code retries (408/409/429/5xx/connection; at most
  ``CLAUDE_CODE_MAX_RETRIES``), each retry re-entering this filter.
* ``eval_async`` (and ``eval_set``, ``inspect view`` and the Inspect CLI) call
  Inspect's ``init_dotenv``, which walks up from the cwd to the nearest ``.env``
  and loads ALL of it into ``os.environ`` (inherited by every subprocess).
  ``disable_inspect_dotenv`` must run before any Inspect evaluation in this
  project; only ``secrets.load_openrouter_key`` may read the host ``.env``.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import functools
import hashlib
import json
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

INSPECT_AI_VERSION = "0.3.279"
INSPECT_SWE_VERSION = "0.2.71"
# The bridge needs the Anthropic SDK's request schemas even for non-Anthropic
# models; the OpenRouter provider uses the OpenAI SDK (smoke-tested pins).
ANTHROPIC_SDK_VERSION = "1.13.0"
OPENAI_SDK_VERSION = "3.29.0"
CLAUDE_CODE_VERSION = "2.1.296"

# Built-in Claude Code tools removed from the model's tool list. "Task" was the
# nested-agent tool name before it was renamed "Agent"; both are listed.
DISALLOWED_TOOLS = ("Agent", "Task", "WebSearch", "WebFetch")

BASH_TIMEOUT_MS = 120_000

# Claude Code's own API retries (2.1.296: default 10, clamped at 15). Every retry
# re-enters the budget filter with a fresh reservation; rejected-before-generation
# requests settle at 0, in-flight failures stay uncertain. Four retries ride out
# short 429/5xx bursts (exponential CLI backoff) while bounding uncertain holds.
CLAUDE_CODE_MAX_RETRIES = 4

SYSTEM_PROMPT_MODES = ("append", "replace")

# Environment for the Claude Code process (applied after Inspect SWE defaults).
# CLAUDE_CODE_DISABLE_AUTO_MEMORY is added per run by ``claude_code_env``.
CLAUDE_CODE_ENV = {
    # Approved 120 s limit for local commands, including `lake env lean`.
    "BASH_DEFAULT_TIMEOUT_MS": str(BASH_TIMEOUT_MS),
    "BASH_MAX_TIMEOUT_MS": str(BASH_TIMEOUT_MS),
    # No update checks, telemetry, error reporting, feedback or flavour calls.
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_BUG_COMMAND": "1",
    "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
    "CLAUDE_CODE_MAX_RETRIES": str(CLAUDE_CODE_MAX_RETRIES),
    # The first request blocks at the common start barrier (startup watchdog
    # 600 s). A long client timeout avoids a CLI timeout+retry duplicating a
    # request that is still waiting at the barrier.
    "API_TIMEOUT_MS": "1800000",
}


def claude_code_env(auto_memory=True, extra=None):
    """Effective Claude Code environment.

    Inspect SWE disables Claude Code auto-memory ("1"); ``auto_memory=True`` sets
    "0", keeping Claude Code's normal behavior (approved, docs/plan.md "Effective
    agent system prompt").
    """
    env = dict(CLAUDE_CODE_ENV)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "0" if auto_memory else "1"
    env.update(extra or {})
    return env


# ---------------------------------------------------------------------------
# Inspect's .env loading (interface X1)

# Every Inspect module that binds ``init_dotenv`` by name (verified in the pinned
# inspect-ai 0.3.279 sources: _eval/context.py init_runtime_context, called by
# eval/eval_async; _eval/evalset.py eval_set; _view/view.py; _cli/main.py). The
# loader primitives below are what each of them reaches, so patching those covers
# callers imported later too.
_DOTENV_MODULE = "inspect_ai._util.dotenv"
_DOTENV_CALLERS = ("inspect_ai._eval.context", "inspect_ai._eval.evalset")
_DOTENV_LOCK = threading.Lock()


def _dotenv_disabled_init():
    """Replacement for Inspect's ``init_dotenv``: loads nothing."""
    return None


def _dotenv_disabled_find(*args, **kwargs):
    return ""


def _dotenv_disabled_load(*args, **kwargs):
    return False


def _dotenv_disabled_values(*args, **kwargs):
    return {}


def credentials_in_environment():
    """Whether OPENROUTER_API_KEY is set in this process's environment (presence only)."""
    from .secrets import KEY_NAME
    return bool(os.environ.get(KEY_NAME))


def disable_inspect_dotenv():
    """Idempotently stop every Inspect code path from loading a ``.env`` file.

    Inspect's ``init_dotenv`` runs ``find_dotenv(usecwd=True)`` (walking up from
    the cwd) and ``load_dotenv`` on each eval. Here the loader primitives in
    Inspect's dotenv module are replaced (``find_dotenv`` finds nothing,
    ``load_dotenv``/``dotenv_values`` load nothing), so ``init_dotenv`` and
    ``dotenv_environ`` become no-ops wherever they are bound, and ``init_dotenv``
    itself is replaced in its module and in every already-imported Inspect module
    that bound it by name. Raises RuntimeError (fail closed) if the pinned module
    layout is absent. Call before ``eval_async``/``eval`` in every script and test;
    ``runtime.build_task`` and ``runtime.supervise`` also call it.

    Returns ``{"dotenv_disabled": True, "patched": [...],
    "credentials_in_environment": bool}`` (presence of OPENROUTER_API_KEY in
    ``os.environ`` at call time; never a value).
    """
    import importlib
    with _DOTENV_LOCK:
        try:
            module = importlib.import_module(_DOTENV_MODULE)
        except ImportError as ex:
            raise RuntimeError(f"Cannot disable Inspect .env loading: {_DOTENV_MODULE} is unavailable") from ex
        original = getattr(module, "_gdm_original_init_dotenv", None) or getattr(module, "init_dotenv", None)
        if original is None:
            raise RuntimeError(f"Cannot disable Inspect .env loading: {_DOTENV_MODULE}.init_dotenv is absent")
        module._gdm_original_init_dotenv = original
        patched = []
        for name, replacement in (("find_dotenv", _dotenv_disabled_find), ("load_dotenv", _dotenv_disabled_load),
                                  ("dotenv_values", _dotenv_disabled_values), ("init_dotenv", _dotenv_disabled_init)):
            if not hasattr(module, name):
                raise RuntimeError(f"Cannot disable Inspect .env loading: {_DOTENV_MODULE}.{name} is absent")
            setattr(module, name, replacement)
            patched.append(f"{_DOTENV_MODULE}.{name}")
        for caller in _DOTENV_CALLERS:
            try:
                importlib.import_module(caller)
            except ImportError:
                continue
        for name, loaded in list(sys.modules.items()):
            if not (name == "inspect_ai" or name.startswith(("inspect_ai.", "inspect_swe"))) or name == _DOTENV_MODULE:
                continue
            try:
                bound = getattr(loaded, "init_dotenv", None)
            except Exception:  # a lazy module __getattr__; it cannot hold the bound name
                continue
            if bound is original:
                loaded.init_dotenv = _dotenv_disabled_init
            if bound is original or bound is _dotenv_disabled_init:
                patched.append(f"{name}.init_dotenv")
        module._gdm_dotenv_disabled = True
    return {"dotenv_disabled": True, "patched": sorted(set(patched)),
            "credentials_in_environment": credentials_in_environment()}


_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    # Conference actions may block on the 120 s judge; do not share the small
    # default asyncio pool with Inspect internals.
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(max_workers=48, thread_name_prefix="conference")
        return _EXECUTOR


# ---------------------------------------------------------------------------
# Sandbox file reads for conference tools (docs/interfaces.md, sandbox_file)

# Runs inside the calling agent's own container as the agent user. Resolves the
# path with realpath, opens it, re-resolves the opened descriptor (so a racing
# symlink swap is detected), and enforces roots, regular-file and size limits.
# Allowed roots are literal paths and are never resolved: the agent owns parts of
# them (e.g. /tmp/w), so a root counts only while realpath(root) == root, i.e. no
# component is a symlink the agent planted. A redirected root is dropped (and
# reported researcher-side as ``invalid_roots``); the descriptor re-check compares
# against the literal roots, so a root swapped after this check cannot match.
_READ_SCRIPT = r"""
import base64, json, os, stat, sys
path, roots, limit, suffixes = sys.argv[1], json.loads(sys.argv[2]), int(sys.argv[3]), json.loads(sys.argv[4])
literal = [os.path.normpath(r) for r in roots if isinstance(r, str) and os.path.isabs(r)]
invalid = [r for r in literal if os.path.realpath(r) != r]
roots = [r for r in literal if r not in invalid]
def inside(p):
    return any(p == r or p.startswith(r.rstrip('/') + '/') for r in roots)
def out(**kw):
    if invalid:
        kw['invalid_roots'] = invalid
    sys.stdout.write(json.dumps(kw)); sys.exit(0)
base = path if os.path.isabs(path) else os.path.join('/workspace', path)
resolved = os.path.realpath(base)
if not inside(resolved):
    out(ok=False, error='path_outside_allowed_roots', resolved=resolved)
try:
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
except OSError as e:
    out(ok=False, error='unreadable', detail=e.strerror, resolved=resolved)
info = os.fstat(fd)
# Linux containers always mount /proc; the fallback only serves host unit tests.
actual = os.path.realpath('/proc/self/fd/%d' % fd) if os.path.isdir('/proc/self/fd') else resolved
if not inside(actual):
    out(ok=False, error='path_outside_allowed_roots', resolved=actual)
if not stat.S_ISREG(info.st_mode):
    out(ok=False, error='not_a_regular_file', resolved=actual)
if suffixes and not actual.lower().endswith(tuple(x.lower() for x in suffixes)):
    out(ok=False, error='file_type_not_allowed', allowed_suffixes=suffixes, resolved=actual)
if info.st_size > limit:
    out(ok=False, error='file_too_large', size=info.st_size, max_bytes=limit, resolved=actual)
data = os.read(fd, limit + 1)
if len(data) > limit:
    out(ok=False, error='file_too_large', max_bytes=limit, resolved=actual)
out(ok=True, resolved=actual, size=len(data), content=base64.b64encode(data).decode())
"""


async def read_agent_file(sbox, path, allowed_roots, max_bytes, allowed_suffixes=None):
    """Read ``path`` from the calling agent's own container (never the host).

    Returns ``{"ok": True, "content": str, "resolved": str, "size": int}`` or
    ``{"ok": False, "error": ...}``. The read runs as the container's default
    (agent) user, so it can only return what that agent could read itself.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        return {"ok": False, "error": "invalid_path"}
    roots = [str(r) for r in allowed_roots]
    suffixes = [str(x) for x in (allowed_suffixes or [])]
    result = await sbox.exec(["python3", "-I", "-c", _READ_SCRIPT, path, json.dumps(roots), str(int(max_bytes)),
                              json.dumps(suffixes)], timeout=30)
    if not result.success:
        return {"ok": False, "error": "read_failed"}
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return {"ok": False, "error": "read_failed"}
    if payload.get("ok"):
        try:
            payload["content"] = base64.b64decode(payload["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": "not_utf8_text", "resolved": payload.get("resolved")}
    return payload


def conference_tools(conference, agent_id, definitions, sbox=None, record=None, stop=None):
    """Bridged Inspect tools bound to ``agent_id``; arguments never choose identity.

    ``sbox`` is the calling agent's own sandbox (captured in that sample's
    solver). ``sandbox_file`` definitions read their file argument from it.
    ``stop`` (the run clock's event): once set, no new conference action starts.
    The solver lets an in-flight model request finish after the stop, so the CLI
    may still issue a call in that window; it is refused and recorded.
    """
    from inspect_ai.tool import ToolDef, ToolParams
    record = record or (lambda *args: None)
    result = []
    for definition in definitions:
        spec = definition.get("sandbox_file")

        def bind(name, spec):
            def closed():
                record("conference_action_after_stop", agent_id, {"tool": name})
                return {"ok": False, "error": "conference_closed",
                        "message": "The conference run has ended."}

            def dispatch(args):
                # This runs in the worker, after any executor queue delay. Actions
                # already started may finish; queued actions have not started yet.
                if stop is not None and stop.is_set():
                    return closed()
                return conference.action(agent_id, name, args)

            async def execute(**args):
                args = dict(args)
                if stop is not None and stop.is_set():
                    return json.dumps(closed())
                if spec:
                    target = args.get(spec["arg"])
                    box = sbox
                    if box is None:
                        from inspect_ai.util import sandbox
                        box = sandbox()
                    read = await read_agent_file(box, target, spec["allowed_roots"], spec["max_bytes"],
                                                 spec.get("allowed_suffixes"))
                    if stop is not None and stop.is_set():
                        return json.dumps(closed())
                    if not read.get("ok"):
                        rejection = {"ok": False, "error": read.get("error"), "path": target}
                        for key in ("max_bytes", "size", "allowed_suffixes"):
                            if key in read:
                                rejection[key] = read[key]
                        rejected = {"tool": name, "path": target, "resolved": read.get("resolved"),
                                    "error": read.get("error")}
                        if read.get("invalid_roots"):  # researcher-side only, never returned to the agent
                            rejected["invalid_roots"] = read["invalid_roots"]
                        record("sandbox_file_rejected", agent_id, rejected)
                        return json.dumps(rejection)
                    args[spec["into"]] = read["content"]
                    accepted = {"tool": name, "path": target, "resolved": read["resolved"], "bytes": read["size"],
                                "sha256": hashlib.sha256(read["content"].encode("utf-8")).hexdigest()}
                    if read.get("invalid_roots"):
                        accepted["invalid_roots"] = read["invalid_roots"]
                    record("sandbox_file_read", agent_id, accepted)
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(_executor(), dispatch, args)
                return json.dumps(response, ensure_ascii=False)
            return execute

        schema = copy.deepcopy(definition["inputSchema"])
        for name, prop in schema.get("properties", {}).items():
            prop.setdefault("description", f"The {name.replace('_', ' ')} for this action.")
        result.append(ToolDef(bind(definition["name"], spec), name=definition["name"],
                              description=definition["description"],
                              parameters=ToolParams.model_validate(schema), max_output=0).as_tool())
    return result


# ---------------------------------------------------------------------------
# Provider billing capture

def openrouter_reported_cost(raw_usage):
    """Total USD cost reported by OpenRouter for one response, or None.

    OpenRouter documents that usage (including ``cost``) is always returned;
    ``usage: {include: true}`` is deprecated and has no effect. For BYOK requests
    ``cost`` excludes the upstream charge, which is then added when reported.
    """
    if not isinstance(raw_usage, dict):
        return None
    cost = raw_usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float, str)):
        return None
    try:
        total = Decimal(str(cost))
    except ArithmeticError:
        return None
    if raw_usage.get("is_byok"):
        upstream = (raw_usage.get("cost_details") or {}).get("upstream_inference_cost")
        if isinstance(upstream, bool) or not isinstance(upstream, (int, float, str)):
            return None
        total += Decimal(str(upstream))
    return total if total.is_finite() and total >= 0 else None


def capture_openrouter_cost(model):
    """Wrap ``model.api.generate`` so raw OpenRouter usage reaches the output.

    The OpenAI-compatible base keeps the raw response only on the ModelCall
    (``completion.model_dump()``, which retains OpenRouter's extra usage fields).
    The wrapper copies ``response["usage"]`` into
    ``output.metadata["openrouter_usage"]`` and the generation id / serving
    provider (for post-run reconciliation and route checks). Model copies share
    ``api``.
    """
    api = model.api
    if getattr(api, "_gdm_cost_capture", False):
        return model
    original = api.generate

    async def generate(input, tools, tool_choice, config):
        result = await original(input, tools, tool_choice, config)
        output, call = (result if isinstance(result, tuple) else (result, None))
        raw = getattr(call, "response", None) if call is not None else None
        if hasattr(output, "metadata") and isinstance(raw, dict):
            metadata = dict(output.metadata or {})
            usage = raw.get("usage")
            if isinstance(usage, dict):
                metadata["openrouter_usage"] = usage
            for key, field in (("openrouter_generation_id", "id"), ("openrouter_provider", "provider"),
                               ("openrouter_model", "model")):
                if isinstance(raw.get(field), str):
                    metadata[key] = raw[field]
            output.metadata = metadata
        return result

    api.generate = generate
    api._gdm_cost_capture = True
    return model


def settlement(settings, output):
    """(cost, basis) for ledger settlement; (None, None) means uncertain.

    Inspect's ``usage.total_cost`` is replaced by the provider-reported cost (or
    None) so ``ProviderSettings.settlement_cost`` never mistakes Inspect's
    price-table estimate for a bill.
    """
    if output is None or output.usage is None:
        return None, None
    raw = (output.metadata or {}).get("openrouter_usage")
    reported = openrouter_reported_cost(raw)
    usage = output.usage.model_copy(update={"total_cost": float(reported) if reported is not None else None})
    settle = getattr(settings, "settlement_cost", None)
    if settle is None:
        return (reported, "provider_reported_cost") if reported is not None else (None, None)
    value = settle(usage)
    if isinstance(value, tuple):
        cost, basis = value
    else:
        cost, basis = value, "provider_reported_cost" if reported is not None else "usage_priced_upper_bound"
    if cost is not None and reported is not None and basis == "provider_reported_cost":
        cost = reported  # exact Decimal, not the float round trip
    return cost, basis


# Provider statuses that may follow (partial) generation and therefore stay
# uncertain: 408 request timeout and every 5xx. Other 4xx statuses (OpenRouter:
# 400 bad request, 401 key, 402 credits, 403 moderation, 404, 413, 422, 429
# rate limit) reject the request before generation.
_UNCERTAIN_4XX = frozenset({408})


def _exception_chain(ex):
    """``ex`` plus its causes/contexts and a tenacity RetryError's last attempt."""
    seen, queue = [], [ex]
    while queue:
        item = queue.pop(0)
        if item is None or any(item is s for s in seen):
            continue
        seen.append(item)
        last = getattr(item, "last_attempt", None)
        if last is not None:
            try:
                queue.append(last.exception())
            except Exception:
                pass
        queue.extend([getattr(item, "__cause__", None), getattr(item, "__context__", None)])
    return seen


def _short(value, limit=600):
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return text[:limit]


# Response headers that may carry an OpenRouter generation id on an error response.
_GENERATION_ID_HEADERS = ("x-generation-id", "x-openrouter-generation-id")


def _generation_id(value, depth=0):
    """An OpenRouter generation id inside a (parsed) error body, else None.

    ``generation_id`` keys are taken as given; ``id`` keys only when they look like
    a generation id (``gen-...``), so user or request ids are never mistaken for one.
    """
    if not isinstance(value, dict) or depth > 3:
        return None
    for key in ("generation_id", "id"):
        found = value.get(key)
        if isinstance(found, str) and found and (key == "generation_id" or found.startswith("gen-")):
            return found
    for key in ("error", "metadata"):
        found = _generation_id(value.get(key), depth + 1)
        if found:
            return found
    return None


def _error_generation_id(item):
    """Generation id from an SDK error's body, raw response JSON or headers (or None)."""
    found = _generation_id(getattr(item, "body", None))
    response = getattr(item, "response", None)
    if found or response is None:
        return found
    try:
        found = _generation_id(response.json())
    except Exception:  # unread/streamed/non-JSON response
        found = None
    if found:
        return found
    headers = getattr(response, "headers", None) or {}
    for name in _GENERATION_ID_HEADERS:
        value = headers.get(name)
        if isinstance(value, str) and value:
            return value
    return None


_NEVER_CONNECTED = ("ConnectError", "ConnectTimeout", "PoolTimeout")


def _never_connected_types():
    """Transport errors raised before a connection exists, from every HTTP library
    the pinned OpenAI SDK may raise them from.

    openai 3.29.0 sends through ``httpx2`` (``_base_client`` raises
    ``APIConnectionError``/``APITimeoutError ... from`` an ``httpx2`` error) and also
    catches legacy ``httpx`` errors when that module is loaded; the two libraries'
    classes are unrelated, so both are listed. A library that is not imported can
    have raised nothing.
    """
    found = []
    modules = [sys.modules.get("httpx")]
    try:
        import httpx2
        modules.append(httpx2)
    except ImportError:
        pass
    for module in modules:
        if module is not None:
            found += [getattr(module, name) for name in _NEVER_CONNECTED if isinstance(getattr(module, name, None), type)]
    return tuple(found)


def classify_generate_error(ex):
    """Billing classification of an exception raised by ``model.generate``.

    ``billing`` is ``unbilled_error`` only when the provider demonstrably rejected
    the request before generation (an HTTP 4xx other than 408) or the connection
    was never established (``ConnectError``/``ConnectTimeout``/``PoolTimeout`` from
    ``httpx2`` or ``httpx``); everything else (5xx, read timeouts, dropped
    connections, TLS failures, cancellation, unrecognized errors) is ``uncertain``.
    ``provider_generation_id`` is the OpenRouter generation id when the error
    body/response carries one (scripts/reconcile_ledger.py matches it), else None;
    ``provider_request_id`` is the separate HTTP request id.
    """
    info = {"billing": "uncertain", "error": type(ex).__name__, "status": None,
            "provider_request_id": None, "provider_generation_id": None, "provider_error": None,
            "reason": "unclassified"}
    if not isinstance(ex, Exception):  # cancellation / interpreter exit while in flight
        info["reason"] = "cancelled_in_flight"
        return info
    try:
        import openai
    except ImportError:
        return info
    never_connected = _never_connected_types()
    for item in _exception_chain(ex):
        if isinstance(item, openai.APIStatusError):
            status = item.status_code
            info.update(status=status, provider_request_id=getattr(item, "request_id", None),
                        provider_generation_id=_error_generation_id(item),
                        provider_error=_short(item.body if item.body is not None else str(item)))
            if 400 <= status < 500 and status not in _UNCERTAIN_4XX:
                info.update(billing="unbilled_error", reason="provider_rejected_before_generation")
            else:
                info["reason"] = "provider_error_after_admission"
            return info
        if isinstance(item, openai.APIConnectionError):
            if any(isinstance(c, never_connected) for c in _exception_chain(item)[1:]):
                info.update(billing="unbilled_error", reason="connection_not_established")
            else:
                info["reason"] = "connection_lost_or_timeout"
            return info
    return info


def replayed_reasoning_blocks(messages):
    """Reasoning blocks in earlier assistant turns of a request (replayed history,
    ``reasoning_history="all"``); the live compatibility check needs one accepted."""
    count = 0
    for message in messages:
        content = getattr(message, "content", None)
        if getattr(message, "role", None) == "assistant" and isinstance(content, list):
            count += sum(1 for part in content if getattr(part, "type", None) == "reasoning")
    return count


def _rejected_without_generation(output):
    # Inspect converts some provider 4xx rejections (context overflow, input
    # moderation) into outputs without usage; nothing was generated.
    return output is not None and output.usage is None and output.stop_reason in ("model_length", "content_filter")


def output_kind(output):
    """What the scaffold received in a finished output's first choice.

    ``tool_calls`` (at least one call), ``text`` (visible non-reasoning content),
    ``reasoning_only`` (reasoning blocks and no visible content) or ``empty``.
    Used for exit labelling: a session that ends right after a ``reasoning_only``
    or ``empty`` response did not end with a reply.
    """
    message = output.message if output is not None and output.choices else None
    if message is None:
        return "empty"
    if message.tool_calls:
        return "tool_calls"
    content = message.content
    if isinstance(content, str):
        return "text" if content.strip() else "empty"
    reasoning = False
    for part in content or []:
        kind = getattr(part, "type", None)
        if kind == "reasoning":
            reasoning = True
        elif kind != "text" or (getattr(part, "text", "") or "").strip():
            return "text"
    return "reasoning_only" if reasoning else "empty"


# ---------------------------------------------------------------------------
# Bridge filter: barrier, budget admission, frozen settings, archiving

class InstructionArchive:
    """Researcher-side archive of every distinct system prompt + tool set the CLI sends."""
    def __init__(self, directory, record):
        from pathlib import Path
        self.directory = Path(directory)
        self.record = record
        self.seen = set()
        self.lock = threading.Lock()

    def observe(self, agent_id, messages, tools):
        from inspect_ai.model import ChatMessageSystem
        system = []
        for message in messages:
            if not isinstance(message, ChatMessageSystem):
                break
            system.append(message.text)
        text = "\n\n<<system-block>>\n\n".join(system)
        tool_names = sorted(t.name for t in tools)
        definitions = sorted(({"name": t.name, "description": t.description,
                               "parameters": t.parameters.model_dump(mode="json", exclude_none=True)}
                              for t in tools), key=lambda d: d["name"])
        digest = hashlib.sha256(json.dumps([text, definitions], sort_keys=True).encode("utf-8")).hexdigest()
        with self.lock:
            if (agent_id, digest) in self.seen:
                return
            self.seen.add((agent_id, digest))
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{agent_id}-{digest[:16]}.json"
        path.write_text(json.dumps({"agent_id": agent_id, "system_blocks": system, "tools": tool_names,
                                    "tool_definitions": definitions}, indent=2, ensure_ascii=False))
        self.record("effective_instructions", agent_id,
                    {"sha256": digest, "path": str(path), "system_blocks": len(system), "tools": tool_names})


# Transient SQLite contention while admitting (BudgetLedger already waits up to
# its 30 s busy timeout per attempt): retry briefly, then fail closed.
RESERVE_RETRY_DELAYS = (1.0, 3.0)


def _transient_sqlite(ex):
    text = str(ex).lower()
    return isinstance(ex, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


async def _in_thread(fn, *args, **kwargs):
    """Run a blocking ledger call off the event loop, shielded from cancellation
    so a reservation or settlement is never half-observed."""
    import anyio
    with anyio.CancelScope(shield=True):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


async def _reserve(ledger, scope, agent_id, amount, record):
    import anyio
    for attempt in range(len(RESERVE_RETRY_DELAYS) + 1):
        try:
            return await _in_thread(ledger.reserve, scope, agent_id, amount)
        except Exception as ex:
            if not _transient_sqlite(ex) or attempt == len(RESERVE_RETRY_DELAYS):
                raise
            record("budget_admission_retry", agent_id, {"attempt": attempt + 1, "error": str(ex)[:200]})
            await anyio.sleep(RESERVE_RETRY_DELAYS[attempt])


def _settle_unbilled(ledger, request_id, record, agent_id):
    """Settle at 0 with basis unbilled_error; a ledger without that basis keeps it uncertain."""
    try:
        ledger.settle(request_id, 0, "unbilled_error")
        return "unbilled_error"
    except ValueError:
        ledger.settle(request_id, None)
        record("settlement_fallback", agent_id, {"request_id": request_id, "reason": "unbilled_error_basis_unsupported"})
        return "uncertain"


def budget_filter(agent_id, ledger, settings, barrier, record, scope="pilot", archive=None, stop=None):
    """Bridge GenerateFilter: barrier, admission, frozen settings, settlement.

    The returned coroutine function carries ``.state`` which the solver reads at
    agent exit (``runtime.exit_status``) and while stopping: request counts, last
    stop reason, consecutive truncations, ``in_flight`` (admitted requests whose
    generation has not returned), ``last_request_outcome`` (``finished`` /
    ``error`` / ``not_admitted``), ``last_denial`` (the refusal reason when the
    last request was not admitted: ``run_stopped``, ``cancelled_before_dispatch``,
    ``budget_admission_failed`` or ``budget_ledger_unavailable``), ``last_error`` (billing classification of the
    last generation error) and ``last_output_kind`` (``output_kind`` of the last
    finished output, after truncated tool calls are stripped).
    """
    from inspect_ai.model import Model
    from inspect_ai.util import LimitExceededError
    state = {"requests": 0, "finished": 0, "last_stop_reason": None, "consecutive_truncations": 0,
             "truncated_responses": 0, "unbilled_errors": 0, "uncertain": 0, "in_flight": 0,
             "last_request_outcome": None, "last_denial": None, "last_error": None, "last_output_kind": None}

    def deny(reason, payload=None):
        state["last_request_outcome"], state["last_denial"] = "not_admitted", reason
        record("inference_not_admitted", agent_id, {"reason": reason, **(payload or {})})

    async def intercept(model: Model, messages, tools, tool_choice, config):
        if archive is not None:
            archive.observe(agent_id, messages, tools)
        await barrier.ready(agent_id)
        if stop is not None and stop.is_set():
            deny("run_stopped")
            raise LimitExceededError("time", value=0, limit=0, message="Conference run has ended.")
        request = json.dumps({"messages": [m.model_dump(mode="json") for m in messages],
                              "tools": [t.model_dump(mode="json") for t in tools]}, ensure_ascii=False)
        reservation = settings.reservation(request)
        try:
            request_id = await _reserve(ledger, scope, agent_id, reservation, record)
        except Exception as ex:
            reason = "budget_ledger_unavailable" if _transient_sqlite(ex) else "budget_admission_failed"
            deny(reason, {"error": type(ex).__name__})
            # Ends this sample (the CLI would otherwise retry a forwarded error).
            raise LimitExceededError("cost", value=float(reservation), limit=0,
                                     message="Inference budget cannot admit further requests.") from ex
        # A shielded reservation can complete after the stopping solver has
        # cancelled this scope. Deliver pending cancellation before generation;
        # either cancellation or a stop here is provably before provider dispatch.
        try:
            import anyio
            await anyio.lowlevel.checkpoint()
            if stop is not None and stop.is_set():
                raise LimitExceededError("time", value=0, limit=0, message="Conference run has ended.")
        except BaseException:
            basis = await _in_thread(_settle_unbilled, ledger, request_id, record, agent_id)
            deny("run_stopped" if stop is not None and stop.is_set() else "cancelled_before_dispatch",
                 {"request_id": request_id, "basis": basis, "settled_usd": "0" if basis == "unbilled_error" else None})
            raise
        state["requests"] += 1
        state["in_flight"] += 1
        try:
            return await _generate(model, messages, tools, tool_choice, config, request, request_id, reservation)
        finally:
            state["in_flight"] -= 1

    async def _generate(model, messages, tools, tool_choice, config, request, request_id, reservation):
        record("inference_admitted", agent_id, {"request_id": request_id, "reserved_usd": str(reservation),
                                                "request_bytes": len(request.encode("utf-8")),
                                                "replayed_reasoning_blocks": replayed_reasoning_blocks(messages)})
        # num_choices stays unset: `n` is not in the route's supported parameters
        # and require_parameters would make the provider refuse it.
        frozen = config.model_copy(update={"max_tokens": settings.max_tokens, "max_retries": 0,
                                           "reasoning_effort": settings.reasoning_effort, "reasoning_history": "all",
                                           "cache": False, "fallback_models": None, "num_choices": None,
                                           "extra_body": settings.request_options()})
        try:
            output = await model.generate(input=messages, tools=tools, tool_choice=tool_choice, config=frozen)
        except BaseException as ex:
            info = classify_generate_error(ex)
            if info["billing"] == "unbilled_error":
                # Synchronous: this path may run inside a cancelled scope.
                billing = _settle_unbilled(ledger, request_id, record, agent_id)
            else:
                ledger.settle(request_id, None)
                billing = "uncertain"
            state["unbilled_errors" if billing == "unbilled_error" else "uncertain"] += 1
            state["last_request_outcome"], state["last_denial"] = "error", None
            state["last_error"] = {k: info[k] for k in ("billing", "reason", "status", "error")}
            record("inference_unbilled" if billing == "unbilled_error" else "inference_uncertain", agent_id,
                   {"request_id": request_id, **info})
            raise
        if _rejected_without_generation(output):
            cost, basis = None, await _in_thread(_settle_unbilled, ledger, request_id, record, agent_id)
            state["unbilled_errors" if basis == "unbilled_error" else "uncertain"] += 1
        else:
            cost, basis = settlement(settings, output)
            if cost is None:
                await _in_thread(ledger.settle, request_id, None)
                state["uncertain"] += 1
            else:
                await _in_thread(ledger.settle, request_id, cost, basis)
        metadata = output.metadata or {}
        state["finished"] += 1
        state["last_stop_reason"] = output.stop_reason
        truncated = output.stop_reason in ("max_tokens", "model_length")
        state["consecutive_truncations"] = state["consecutive_truncations"] + 1 if truncated else 0
        record("inference_finished", agent_id, {
            "request_id": request_id, "settled_usd": "0" if basis == "unbilled_error" else (None if cost is None else str(cost)),
            "basis": basis, "usage": output.usage.model_dump(mode="json") if output.usage else None,
            "provider_usage": metadata.get("openrouter_usage"),
            "provider_generation_id": metadata.get("openrouter_generation_id"),
            "provider_name": metadata.get("openrouter_provider"), "provider_model": metadata.get("openrouter_model"),
            "stop_reason": output.stop_reason, "error": output.error})
        if truncated:
            # Never hand partial function arguments to the scaffold for execution.
            # stop_reason stays max_tokens so Claude Code runs its own recovery.
            stripped = sum(len(c.message.tool_calls or []) for c in output.choices)
            for choice in output.choices:
                choice.message.tool_calls = None
            state["truncated_responses"] += 1
            record("truncated_response", agent_id, {"request_id": request_id, "tool_calls_removed": stripped,
                                                    "stop_reason": output.stop_reason,
                                                    "consecutive": state["consecutive_truncations"]})
        state["last_request_outcome"], state["last_denial"], state["last_error"] = "finished", None, None
        state["last_output_kind"] = output_kind(output)
        return output

    intercept.state = state
    return intercept


def build_claude_agent(prompt, tools, filter, env=None, cwd="/workspace", *, system_prompt_mode="append",
                       auto_memory=True):
    """Genuine Claude Code (pinned image binary) with identity-bound bridged tools.

    ``system_prompt_mode``: "append" (approved default: built-in prompt plus the
    conference prompt) or "replace" (conference prompt only).
    """
    if system_prompt_mode not in SYSTEM_PROMPT_MODES:
        raise ValueError(f"system_prompt_mode must be one of {SYSTEM_PROMPT_MODES}")
    from .reasoning_bridge import install_reasoning_bridge
    install_reasoning_bridge()
    from inspect_ai.agent import BridgedToolsSpec
    from inspect_swe import claude_code
    prompt_args = {"system_prompt": prompt} if system_prompt_mode == "append" else {"replace_system_prompt": prompt}
    return claude_code(**prompt_args, version="sandbox", cwd=cwd, effort="high",
                       bridged_tools=[BridgedToolsSpec(name="conference", tools=tools)], filter=filter,
                       disallowed_tools=list(DISALLOWED_TOOLS), permission_mode="bypassPermissions",
                       retry_refusals=0, retry_uncaught_errors=0, env=claude_code_env(auto_memory, env))


def installed_versions():
    from importlib.metadata import version, PackageNotFoundError
    result = {}
    for name, expected in (("inspect-ai", INSPECT_AI_VERSION), ("inspect-swe", INSPECT_SWE_VERSION),
                           ("anthropic", ANTHROPIC_SDK_VERSION), ("openai", OPENAI_SDK_VERSION)):
        try:
            actual = version(name)
        except PackageNotFoundError:
            actual = None
        result[name] = {"actual": actual, "expected": expected, "matches": actual == expected}
    return result
