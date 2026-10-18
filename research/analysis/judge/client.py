"""Budgeted OpenRouter client for the transcript behavior judge (researcher-only).

docs/plan.md "Transcript behavior judge specified, October 17, 2026" (with the
"downsized model allocation" decision): OpenRouter with the main host key, provider
pinned to Anthropic with no fallbacks, real-time requests, the shared ledger's
``analysis`` scope enforcing the $40 allowance (the pilot's unlimited-key exception
extends to this analysis), a refused request retried once and then ``ungraded`` (never
a negative), and a judge model that never silently changes. One client serves one model:
``"haiku"`` (screening), ``"sonnet"`` (per-agent judging, context-only verification) or
``"opus"`` (cohort-deciding verification, run level); see route.MODEL_ALLOCATION.
Paid judging stays gated: offline tests and the rehearsal inject ``FakeTransport``;
the real HTTPS path exists only with ``paid_authorized=True``, which the orchestrator
passes after the user confirms immediately before the first paid request.

Prompt caching (``complete(cache_system=...)``, default on for Sonnet and Opus, off for
Haiku; route.JudgeRouteSettings.prompt_cache can override): the system prompt, which is
the shared rubric prefix, is sent as one text block with ``cache_control: {"type":
"ephemeral"}`` (5-minute TTL, never 1 hour). The reservation of a marked request prices
every prompt token at the largest of the prompt, cache-write and cache-read prices, so
admission never assumes a cache discount; settlement uses OpenRouter's reported cost,
which includes cache reads and writes. Each result records the cache read/write token
counts, the reported ``cache_discount`` when present, and a frozen-price breakdown
(``result["cache"]``).

Why a direct HTTPS client rather than Inspect's ``get_model("openrouter/...")``: the
judge needs the exact raw response archived before parsing, OpenRouter's reported
``usage.cost``, ``finish_reason``/``native_finish_reason``/``message.refusal`` (the
documented refusal signals), the served ``provider``/``service_tier`` for the route
check, and the HTTP status for billing classification. Inspect normalizes or drops
several of these, so a minimal stdlib client (no new dependency, no inspect_ai import)
exposes them reliably.

Per request (``complete``), under an ``asyncio.Semaphore(max_concurrency)``:

1. Build the exact body (system + user messages, ``max_tokens``, ``reasoning.effort``,
   provider pin, optional ``response_format``) and reserve its conservative cost
   (``JudgeRouteSettings.reservation``) in the ledger scope. A denial is never
   dispatched (``grading_failed``, reason ``budget_admission_denied``).
2. Archive the request (no credential: the Authorization header is never written)
   before dispatch. If that write fails the hold is released as ``unbilled_error``.
3. Send. Archive the raw response bytes and metadata BEFORE parsing. If that write
   fails the hold stays ``uncertain`` (an unarchived response is never parsed).
4. Settle exactly like the pilot ledger (``inspect_adapter.budget_filter``):
   OpenRouter's reported cost (``provider_reported_cost``); an HTTP 4xx other than
   408, or a connection never established, settles ``unbilled_error`` at 0; 408, 5xx,
   timeouts, dropped connections, unparseable bodies and cancellation stay
   ``uncertain`` (the reservation is retained until ``reconcile()``).
5. Classify: refusal (``finish_reason == "content_filter"``, ``native_finish_reason``
   ``refusal``/``content_filter``, a non-empty ``message.refusal``, or an error with
   ``error_type`` ``refusal``/``content_policy_violation``/flagged input) is retried
   exactly once with a fresh reservation, then ``ungraded``. Truncation at
   ``max_tokens`` is ``ungraded`` (exceeding limits, rubric Section 7). API and
   infrastructure failures, and a response served by anything other than the pinned
   Anthropic standard tier, are ``grading_failed``. There are no other automatic paid
   retries.

The host key is loaded only on the paid path, at first use, through
``research_swarm.secrets.load_openrouter_key(credentials_root)``, followed by the
unpaid provider-limit check with the approved unlimited-key exception
(``provider_limit.verify_key_limit``, ``allow_unlimited=True``). ``credentials_root``
is the directory whose private ``.env`` holds the key (default: this checkout's root),
so a separate checkout on the VM can use the main checkout's ``.env`` without copying
it; it is accepted only on the paid path and nothing is read from it before the first
paid request. The key is held in memory only and scrubbed from anything archived.
Tests never load it.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import re
import socket
import sqlite3
import ssl
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from research_swarm.budget import BudgetExceeded, BudgetLedger, JUDGE_ANALYSIS_CAP_USD, judge_analysis_problems
from research_swarm.inspect_adapter import openrouter_reported_cost

from .route import (CACHE_CONTROL, CACHE_TTL, DEFAULT_CONCURRENCY, MODEL_KEYS, PROVIDER_NAME, STANDARD_SERVICE_TIERS,
                    JudgeRouteSettings)

REPO_ROOT = Path(__file__).resolve().parents[3]
ANALYSIS_SCOPE = "analysis"
STATUSES = ("ok", "ungraded", "grading_failed")
ARCHIVE_SCHEMA = "judge-client-archive/v1"
REDACTION = "[REDACTED-CREDENTIAL]"
# Pilot semantics (inspect_adapter._UNCERTAIN_4XX): 408 may follow generation.
_UNCERTAIN_4XX = frozenset({408})
_REFUSAL_ERROR_TYPES = frozenset({"refusal", "content_policy_violation"})
_REFUSAL_FINISH = frozenset({"content_filter", "refusal"})
# Response headers kept in the archive (never cookies or anything credential-like).
_HEADER_ALLOWLIST = ("content-type", "content-length", "date", "x-generation-id", "x-openrouter-generation-id",
                     "x-request-id", "retry-after", "server")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_PURPOSE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Transports

@dataclass
class TransportResponse:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)


class TransportError(Exception):
    """A request that produced no HTTP response.

    ``kind="not_connected"``: provably never reached OpenRouter (DNS failure, refused
    connection, TLS handshake failure), so nothing can have been billed.
    ``kind="uncertain"``: anything else (timeouts, dropped connections, send errors).
    """
    def __init__(self, kind, detail=""):
        if kind not in ("not_connected", "uncertain"):
            raise ValueError("TransportError kind must be not_connected or uncertain")
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = str(detail)[:500]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect (the Authorization header must not travel elsewhere)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Real HTTPS POST (stdlib), run in a worker thread; no proxies, no redirects."""
    def __init__(self):
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    async def post(self, url, headers, body, timeout):
        return await asyncio.to_thread(self._post, url, headers, body, timeout)

    def _post(self, url, headers, body, timeout):
        if not str(url).startswith("https://"):
            raise TransportError("not_connected", "refusing a non-HTTPS judge endpoint")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return TransportResponse(response.status, response.read(), dict(response.headers.items()))
        except urllib.error.HTTPError as ex:  # a real HTTP status (4xx/5xx, or an unfollowed 3xx)
            try:
                data = ex.read()
            except Exception:
                data = b""
            return TransportResponse(ex.code, data, dict((ex.headers or {}).items()))
        except urllib.error.URLError as ex:
            reason = ex.reason
            # Raised while connecting: DNS, refusal and certificate/handshake failures
            # happen before any request byte reaches the server. Other socket errors
            # inside URLError may come from sending the body: uncertain.
            if isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)):
                raise TransportError("not_connected", type(reason).__name__) from None
            raise TransportError("uncertain", type(reason).__name__ if isinstance(reason, BaseException)
                                 else str(reason)) from None
        except (TimeoutError, socket.timeout, ConnectionError, OSError, ValueError) as ex:
            raise TransportError("uncertain", type(ex).__name__) from None
        except Exception as ex:  # http.client.HTTPException and friends
            raise TransportError("uncertain", type(ex).__name__) from None


class FakeTransport:
    """Offline transport for tests and the unpaid rehearsal. Never touches the network.

    ``script`` items are consumed in order: a ``TransportResponse``, an exception
    instance (raised), or a callable ``f(request) -> item``. When the script is empty,
    ``responder(request)`` is used (e.g. a rehearsal generating schema-valid JSON per
    purpose). Every request is recorded in ``requests`` as ``{"url", "headers",
    "body" (parsed JSON), "bytes", "timeout"}``; ``on_request(request)`` runs before the
    response is chosen (tests use it to check the archive-before-dispatch order).
    """
    def __init__(self, script=None, *, responder=None, delay=0.0, on_request=None):
        self.script = list(script or [])
        self.responder = responder
        self.delay = delay
        self.on_request = on_request
        self.requests = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def post(self, url, headers, body, timeout):
        request = {"url": url, "headers": dict(headers), "body": json.loads(body), "bytes": len(body),
                   "timeout": timeout}
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.on_request is not None:
                self.on_request(request)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.script:
                item = self.script.pop(0)
            elif self.responder is not None:
                item = self.responder
            else:
                raise AssertionError("FakeTransport script exhausted")
            if callable(item) and not isinstance(item, (TransportResponse, BaseException)):
                item = item(request)
            if isinstance(item, BaseException):
                raise item
            return item
        finally:
            self.in_flight -= 1


def fake_completion(text="", *, cost="0.001", finish_reason="stop", native_finish_reason=None, refusal=None,
                    reasoning=None, provider=PROVIDER_NAME, model="anthropic/claude-opus-5.5", service_tier="default",
                    generation_id="gen-fake-0001", usage=None, status=200, cache_read_tokens=None,
                    cache_write_tokens=None, cache_discount=None):
    """A canned OpenRouter Chat Completions response (offline tests and rehearsal).

    ``cache_read_tokens``/``cache_write_tokens`` set ``usage.prompt_tokens_details``
    (``cached_tokens``/``cache_write_tokens``); ``cache_discount`` adds the response's
    ``cache_discount`` field. Pass ``model`` for Haiku or Sonnet clients.
    """
    usage = json.loads(json.dumps(usage or {"prompt_tokens": 1000, "completion_tokens": 200,
                                            "completion_tokens_details": {"reasoning_tokens": 120},
                                            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                                            "total_tokens": 1200, "is_byok": False}))
    details = usage.setdefault("prompt_tokens_details", {})
    if cache_read_tokens is not None:
        details["cached_tokens"] = cache_read_tokens
    if cache_write_tokens is not None:
        details["cache_write_tokens"] = cache_write_tokens
    if cost is not None:
        usage["cost"] = cost
    message = {"role": "assistant", "content": text, "refusal": refusal}
    if reasoning is not None:
        message["reasoning"] = reasoning
    payload = {"id": generation_id, "object": "chat.completion", "model": model, "provider": provider,
               "service_tier": service_tier,
               "choices": [{"index": 0, "finish_reason": finish_reason, "native_finish_reason": native_finish_reason,
                            "message": message}],
               "usage": usage}
    if cache_discount is not None:
        payload["cache_discount"] = cache_discount
    return TransportResponse(status, json.dumps(payload).encode("utf-8"), {"content-type": "application/json"})


def fake_http_error(status, *, error_type=None, message="error", metadata=None, usage=None):
    """A canned OpenRouter error body; status 200 models an error reported after acceptance."""
    meta = dict(metadata or {})
    if error_type is not None:
        meta["error_type"] = error_type
    payload = {"error": {"code": status if status != 200 else 500, "message": message, "metadata": meta}}
    if usage is not None:
        payload["usage"] = usage
    return TransportResponse(status, json.dumps(payload).encode("utf-8"), {"content-type": "application/json"})


# ---------------------------------------------------------------------------
# Client

def _empty_result(cache_system=False):
    return {"status": None, "text": None, "attempts": 0, "refusals": 0, "usage": {}, "cost_usd": None,
            "request_ids": [], "raw_paths": [], "finish_reason": None, "reasoning": None, "error": None,
            "generation_ids": [], "provider": None, "model": None, "service_tier": None, "settlements": [],
            "cache": {"system_marked": bool(cache_system), "ttl": CACHE_TTL if cache_system else None,
                      "read_tokens": 0, "write_tokens": 0, "cache_read_usd": "0", "cache_write_usd": "0",
                      "priced_total_usd": "0", "saving_usd": "0", "reported_cache_discount_usd": None,
                      "basis": "frozen_route_prices", "per_attempt": []}}


def _decimal_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    return number if number.is_finite() else None


@dataclass
class _Outcome:
    kind: str                     # ok | refusal | ungraded | grading_failed | budget_denied
    reason: str | None = None
    text: str | None = None
    reasoning: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None
    settlement: dict | None = None
    paths: list = field(default_factory=list)
    request_id: str | None = None
    generation_id: str | None = None
    provider: str | None = None
    model: str | None = None
    service_tier: str | None = None
    detail: dict | None = None
    dispatched: bool = True       # False when nothing reached the transport
    cache_discount: object = None  # the response's reported ``cache_discount`` (if any)
    cache: dict | None = None      # per-attempt cache accounting (set by _attempt)


class JudgeClient:
    """One judge model (``"haiku"``, ``"sonnet"`` or ``"opus"``) behind the shared ledger.

    ``http``: a transport with ``async post(url, headers, body, timeout)`` (tests and
    the rehearsal pass ``FakeTransport``). ``http=None`` builds the real HTTPS
    transport and is refused unless ``paid_authorized=True``, ``scope="analysis"`` and
    the settings come from a verified freeze record. The ``analysis`` scope requires
    the existing ledger with the applied audited judge raise
    (``budget.judge_analysis_problems``); any other scope (offline rehearsal) needs
    explicit ``ledger_caps`` and a fake transport. ``max_concurrency`` defaults to
    ``route.DEFAULT_CONCURRENCY`` (haiku 24, sonnet 8, opus 6).

    ``credentials_root`` (paid path only; ``repo_root`` is an accepted alias): the
    directory whose private ``.env`` holds the host key, default this checkout's root.
    It is refused with a fake transport or an explicit ``key_loader``, and nothing is
    read from it until ``prepare()`` (or the first ``complete()``).
    """
    def __init__(self, model_key, ledger_path, scope=ANALYSIS_SCOPE, archive_dir=None, settings=None, http=None,
                 max_concurrency=None, *, paid_authorized=False, ledger_caps=None, key_loader=None, verify_limit=None,
                 credentials_root=None, repo_root=None):
        if model_key not in MODEL_KEYS:
            raise ValueError(f"Unknown judge model {model_key!r}; expected one of {MODEL_KEYS}")
        if archive_dir is None:
            raise ValueError("archive_dir is required (raw requests/responses are archived before parsing)")
        if max_concurrency is None:
            max_concurrency = DEFAULT_CONCURRENCY[model_key]
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        if credentials_root is not None and repo_root is not None:
            raise ValueError("Pass credentials_root or its alias repo_root, not both")
        credentials_root = credentials_root if credentials_root is not None else repo_root
        if credentials_root is not None:
            if http is not None:
                raise ValueError("credentials_root is used only by the paid path; fake transports never load "
                                 "credentials")
            if key_loader is not None:
                raise ValueError("credentials_root and key_loader are exclusive")
        self.model_key = model_key
        self.settings = settings if settings is not None else JudgeRouteSettings.defaults()
        if not self.settings.pricing_verified:
            raise ValueError("Judge settings need verified pricing: JudgeRouteSettings.from_pricing_record(...) "
                             "(offline rehearsals may use JudgeRouteSettings.defaults(pricing_verified=True))")
        self.route = self.settings.route(model_key)
        self.scope = scope
        self.archive_dir = Path(archive_dir)
        self.max_concurrency = max_concurrency
        self.ledger_path = Path(ledger_path)
        self._paid = http is None
        if self._paid:
            if paid_authorized is not True:
                raise RuntimeError("Paid judging is gated: pass paid_authorized=True only after the user confirms "
                                   "immediately before the first paid request (docs/plan.md, October 17, 2026)")
            if scope != ANALYSIS_SCOPE:
                raise RuntimeError("Paid judge requests are admitted only through the ledger's analysis scope")
            if not self.settings.pricing_verified or not self.settings.pricing_record:
                raise RuntimeError("Paid judging requires settings from a fresh freeze record "
                                   "(JudgeRouteSettings.from_pricing_record)")
        if scope == ANALYSIS_SCOPE:
            # Never create a replacement ledger, and require the applied audited raise.
            problems = judge_analysis_problems(self.ledger_path)
            if problems:
                raise ValueError("Judge analysis ledger refused: " + "; ".join(problems))
            caps = {ANALYSIS_SCOPE: JUDGE_ANALYSIS_CAP_USD}
        else:
            if self._paid:
                raise RuntimeError("Non-analysis scopes are for offline rehearsals with a fake transport")
            if not isinstance(ledger_caps, dict) or scope not in ledger_caps:
                raise ValueError(f"Scope {scope!r} (offline rehearsal) needs explicit ledger_caps including it")
            caps = {scope: ledger_caps[scope]}
        self.credentials_root = None
        if self._paid and key_loader is None:
            # Only the directory is checked here; its .env is read at prepare().
            root = Path(credentials_root).expanduser() if credentials_root is not None else REPO_ROOT
            root = root.resolve()
            if not root.is_dir():
                raise ValueError(f"credentials_root {root} is not a directory")
            self.credentials_root = str(root)

            def key_loader():
                from research_swarm.secrets import load_openrouter_key
                return load_openrouter_key(root)
        self.ledger = BudgetLedger(self.ledger_path, caps)
        self.http = UrllibTransport() if self._paid else http
        self._key_loader = key_loader
        self._verify_limit = verify_limit
        self._key = None
        self._prepared = False
        self._prepare_lock = threading.Lock()
        self._semaphores = {}
        self.limit_report = None

    # -- paid-path preparation ----------------------------------------------
    def prepare(self):
        """Load the key (paid path only) and run the unpaid provider-limit check once.

        Without a key loader (fake transports) this does nothing. The limit check uses
        the approved unlimited-key exception (plan, Route and key): the ledger, not a
        provider cap, enforces the $40 allowance. The report (never the key) is
        archived as ``provider-limit-<model>.json``.
        """
        with self._prepare_lock:
            if self._prepared:
                return self.limit_report
            if self._key_loader is not None:
                key = self._key_loader()
                if not isinstance(key, str) or not key:
                    raise RuntimeError("Key loader returned no key")
                verify = self._verify_limit
                if verify is None and self._paid:
                    from research_swarm.provider_limit import verify_key_limit as verify
                if verify is not None:
                    remaining = Decimal(self.ledger.snapshot()[self.scope]["remaining_usd"])
                    report = verify(key, remaining, allow_unlimited=True)
                    self.limit_report = json.loads(self._scrub_text(json.dumps(report, default=str), key))
                    # One file per model: the three clients share one archive directory.
                    self._write_json(self.archive_dir / f"provider-limit-{self.model_key}.json",
                                     {"schema": ARCHIVE_SCHEMA, "kind": "provider_limit", "created_at": _now(),
                                      "model_key": self.model_key, "credentials_root": self.credentials_root,
                                      "report": self.limit_report}, key=key)
                self._key = key
            self._prepared = True
            return self.limit_report

    # -- public API ---------------------------------------------------------
    async def complete(self, *, system, user, purpose, unit_id, max_tokens, effort, response_format=None,
                       cache_system=None):
        """One judged unit -> ClientResult dict (see module docstring for semantics).

        ``cache_system``: mark the system prompt (the shared rubric prefix) for
        Anthropic prompt caching; ``None`` uses the model default
        (``JudgeRouteSettings.cache_system_default``: Sonnet and Opus on, Haiku off).
        An empty system prompt is never marked.

        Keys: ``status`` (ok | ungraded | grading_failed), ``text`` (final visible
        reply, None unless ok/truncated), ``attempts`` (requests dispatched),
        ``refusals``, ``usage`` (summed token counts, including ``cached_tokens`` and
        ``cache_write_tokens``, plus ``per_attempt``), ``cost_usd`` (sum of settled
        costs, None while any attempt is uncertain), ``request_ids`` (ledger ids),
        ``raw_paths`` (archive files), and additionally ``finish_reason``,
        ``reasoning`` (summarized reasoning text), ``error`` (``{"reason", ...}`` or
        None), ``generation_ids``, ``provider``, ``model``, ``service_tier``,
        ``settlements`` (per request: state, cost, basis) and ``cache``
        (``system_marked``, ``ttl``, ``read_tokens``, ``write_tokens``, frozen-price
        ``cache_read_usd``/``cache_write_usd``/``priced_total_usd``/``saving_usd``,
        ``reported_cache_discount_usd`` and ``per_attempt``).
        """
        cache_system = self._validate_call(system, user, purpose, unit_id, max_tokens, effort, response_format,
                                           cache_system)
        if not self._prepared:
            await asyncio.to_thread(self.prepare)
        result = _empty_result(cache_system)
        for attempt in (1, 2):
            outcome = await self._attempt(system, user, purpose, unit_id, max_tokens, effort, response_format,
                                          attempt, cache_system)
            self._merge(result, outcome)
            if outcome.kind == "refusal":
                result["refusals"] += 1
                if attempt == 1:
                    continue  # the one permitted retry (plan, Refusals)
                result["status"] = "ungraded"
                result["error"] = {"reason": "refused_twice", "last": outcome.reason}
                result["text"] = None
            elif outcome.kind == "ok":
                result["status"] = "ok"
                result["error"] = None
            elif outcome.kind == "ungraded":
                result["status"] = "ungraded"
                result["error"] = {"reason": outcome.reason, **(outcome.detail or {})}
            else:  # grading_failed / budget_denied: never retried automatically
                result["status"] = "grading_failed"
                result["error"] = {"reason": outcome.reason, **(outcome.detail or {})}
                result["text"] = None
            break
        result["cost_usd"] = self._total_cost(result["settlements"])
        return result

    # -- internals ------------------------------------------------------------
    def _validate_call(self, system, user, purpose, unit_id, max_tokens, effort, response_format,
                       cache_system=None):
        """Validate one call; returns the effective ``cache_system``."""
        if not isinstance(system, str) or not isinstance(user, str) or not user:
            raise ValueError("system and user must be strings (user non-empty)")
        if not isinstance(purpose, str) or not _PURPOSE.match(purpose):
            raise ValueError(f"purpose {purpose!r} must be a short lowercase label")
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise ValueError("unit_id must be a non-empty string")
        self.settings.check_max_tokens(self.model_key, max_tokens)
        self.settings.reasoning(self.model_key, effort)  # frozen effort per model
        if response_format is not None:
            if not isinstance(response_format, dict):
                raise ValueError("response_format must be a dict")
            if not self.route.structured_outputs:
                raise ValueError("The pinned endpoint does not list structured outputs")
        return self.cache_system_for(system, cache_system)

    def cache_system_for(self, system, cache_system=None):
        """Effective cache marking for a request with this ``system`` prompt."""
        if cache_system is True and not system:
            raise ValueError("cache_system=True needs a non-empty system prompt (the shared rubric prefix)")
        effective = self.settings.resolve_cache_system(self.model_key, cache_system)
        return bool(effective and system)

    def body(self, system, user, max_tokens, effort, response_format=None, cache_system=None):
        """The exact request body (also used for reservations and rehearsal checks).

        ``cache_system`` (``None`` = the model default) marks the system prompt as one
        text block with a 5-minute ``cache_control`` breakpoint; never the 1-hour TTL.
        """
        if self.cache_system_for(system, cache_system):
            system_content = [{"type": "text", "text": system, "cache_control": dict(CACHE_CONTROL)}]
        else:
            system_content = system
        messages = ([{"role": "system", "content": system_content}] if system else []) + \
            [{"role": "user", "content": user}]
        body = {"model": self.route.model_id, "messages": messages, "max_tokens": max_tokens,
                "reasoning": self.settings.reasoning(self.model_key, effort),
                "provider": self.settings.provider_preferences()}
        if response_format is not None:
            body["response_format"] = response_format
        return body

    def _semaphore(self):
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = self._semaphores[loop] = asyncio.Semaphore(self.max_concurrency)
        return semaphore

    def _stem(self, unit_id, purpose, attempt, request_id):
        digest = hashlib.sha256(unit_id.encode("utf-8")).hexdigest()[:10]
        folder = self.archive_dir / f"{_SAFE_NAME.sub('_', unit_id)[:80]}-{digest}"
        return folder / f"{self.model_key}-{purpose}-a{attempt}-{request_id[:8]}"

    def _scrub_text(self, text, key=None):
        key = key or self._key
        return text.replace(key, REDACTION) if key else text

    def _scrub_bytes(self, data):
        return data.replace(self._key.encode("utf-8"), REDACTION.encode("utf-8")) if self._key else data

    def _write_json(self, path, payload, key=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        text = self._scrub_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), key)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.replace(path)
        return str(path)

    def _write_bytes(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(self._scrub_bytes(data))
        tmp.replace(path)
        return str(path)

    def _settle(self, request_id, cost, basis):
        """Synchronous (never half-observed under cancellation); returns the settlement record."""
        if basis == "unbilled_error":
            self.ledger.settle(request_id, 0, "unbilled_error")
            return {"request_id": request_id, "state": "settled", "cost_usd": "0", "basis": "unbilled_error"}
        if cost is None:
            self.ledger.settle(request_id, None)
            return {"request_id": request_id, "state": "uncertain", "cost_usd": None, "basis": None}
        self.ledger.settle(request_id, cost, basis)
        state = next((r["state"] for r in self._request_state(request_id)), "settled")
        return {"request_id": request_id, "state": state, "cost_usd": str(cost), "basis": basis}

    def _request_state(self, request_id):
        with closing(self.ledger.connect()) as db:
            row = db.execute("SELECT state FROM calls WHERE id=?", (request_id,)).fetchone()
        return [{"state": row[0]}] if row else []

    async def _attempt(self, system, user, purpose, unit_id, max_tokens, effort, response_format, attempt,
                       cache_system=False):
        outcome = await self._attempt_inner(system, user, purpose, unit_id, max_tokens, effort, response_format,
                                            attempt, cache_system)
        if outcome.dispatched:
            outcome.cache = self._cache_record(attempt, cache_system, outcome)
        return outcome

    def _cache_record(self, attempt, cache_system, outcome):
        """Per-attempt cache accounting from OpenRouter's reported usage (never admission)."""
        usage = outcome.usage if isinstance(outcome.usage, dict) else None
        priced = None
        try:
            priced = self.settings.priced_usage(self.model_key, usage)
        except Exception as ex:  # accounting must never break a settled request
            priced = {"error": type(ex).__name__}
        return {"attempt": attempt, "system_marked": bool(cache_system), "priced": priced,
                "reported_cost_usd": (usage or {}).get("cost"),
                "reported_cost_details": (usage or {}).get("cost_details"),
                "reported_cache_discount_usd": outcome.cache_discount}

    async def _attempt_inner(self, system, user, purpose, unit_id, max_tokens, effort, response_format, attempt,
                             cache_system):
        async with self._semaphore():
            body = self.body(system, user, max_tokens, effort, response_format, cache_system=cache_system)
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            reservation = self.settings.reservation(self.model_key, data, max_tokens, cache_system=cache_system)
            label = f"judge-{self.model_key}:{purpose}:{unit_id}"[:200]
            try:
                request_id = self.ledger.reserve(self.scope, label, reservation)
            except BudgetExceeded as ex:
                path = self._denial_record(unit_id, purpose, attempt, reservation, ex)
                return _Outcome("budget_denied", "budget_admission_denied", paths=[path], dispatched=False,
                                detail={"reserved_usd": str(reservation), "message": str(ex)[:300]})
            except (sqlite3.Error, ValueError) as ex:
                return _Outcome("grading_failed", "budget_ledger_unavailable", dispatched=False,
                                detail={"error": type(ex).__name__, "message": str(ex)[:300]})
            stem = self._stem(unit_id, purpose, attempt, request_id)
            headers = {"Content-Type": "application/json"}
            try:
                request_path = self._write_json(stem.with_name(stem.name + ".request.json"), {
                    "schema": ARCHIVE_SCHEMA, "kind": "request", "created_at": _now(), "unit_id": unit_id,
                    "purpose": purpose, "attempt": attempt, "model_key": self.model_key, "scope": self.scope,
                    "ledger_request_id": request_id, "reserved_usd": str(reservation),
                    "reservation_basis": ("prompt bound at max(prompt, cache write, cache read) price; no cache "
                                          "discount assumed") if cache_system else "prompt bound at prompt price",
                    "cache_system": bool(cache_system),
                    "cache_control": dict(CACHE_CONTROL) if cache_system else None,
                    "prompt_token_bound": self.settings.prompt_token_bound(self.model_key, data),
                    "body_bytes": len(data), "body_sha256": hashlib.sha256(data).hexdigest(),
                    "url": self.settings.endpoint_url, "headers": dict(headers),
                    "authorization": "bearer (omitted from archive)" if self._key else "none",
                    "settings_sha256": self.settings.sha256(), "pricing_record": self.settings.pricing_record,
                    "pricing_record_sha256": self.settings.pricing_record_sha256, "body": body})
            except Exception as ex:  # never dispatched: release the hold
                self._settle(request_id, 0, "unbilled_error")
                return _Outcome("grading_failed", "archive_failed_before_dispatch", request_id=request_id,
                                dispatched=False, settlement={"request_id": request_id, "state": "settled",
                                                              "cost_usd": "0", "basis": "unbilled_error"},
                                detail={"error": type(ex).__name__})
            paths = [request_path]
            if self._key:
                headers["Authorization"] = f"Bearer {self._key}"
            try:
                response = await self.http.post(self.settings.endpoint_url, headers, data,
                                                self.settings.request_timeout_s)
            except TransportError as ex:
                basis = "unbilled_error" if ex.kind == "not_connected" else None
                settlement = self._settle(request_id, None, basis)
                paths.append(self._error_record(stem, "transport_error", {"kind": ex.kind, "detail": ex.detail}))
                return _Outcome("grading_failed", "connection_not_established" if basis else "connection_lost_or_timeout",
                                settlement=settlement, paths=paths, request_id=request_id,
                                detail={"transport": ex.kind})
            except asyncio.CancelledError:
                self._settle(request_id, None, None)  # dispatched: billing unknown
                try:
                    self._error_record(stem, "cancelled_in_flight", {})
                except Exception:
                    pass
                raise
            except Exception as ex:
                settlement = self._settle(request_id, None, None)
                paths.append(self._error_record(stem, "transport_exception", {"error": type(ex).__name__}))
                return _Outcome("grading_failed", "transport_exception", settlement=settlement, paths=paths,
                                request_id=request_id, detail={"error": type(ex).__name__})
            # Archive the raw response BEFORE any parsing.
            try:
                paths.append(self._write_bytes(stem.with_name(stem.name + ".response.body"), response.body or b""))
                headers_kept = {k.lower(): v for k, v in (response.headers or {}).items()
                                if k.lower() in _HEADER_ALLOWLIST}
                paths.append(self._write_json(stem.with_name(stem.name + ".response.meta.json"), {
                    "schema": ARCHIVE_SCHEMA, "kind": "response", "received_at": _now(), "unit_id": unit_id,
                    "purpose": purpose, "attempt": attempt, "ledger_request_id": request_id,
                    "http_status": response.status, "headers": headers_kept,
                    "body_bytes": len(response.body or b""),
                    "body_sha256": hashlib.sha256(response.body or b"").hexdigest()}))
            except Exception as ex:
                settlement = self._settle(request_id, None, None)
                return _Outcome("grading_failed", "archive_failed_after_response", settlement=settlement,
                                paths=paths, request_id=request_id, detail={"error": type(ex).__name__})
            outcome = self._interpret(response, request_id)
            outcome.paths = paths + outcome.paths
            outcome.request_id = request_id
            return outcome

    def _denial_record(self, unit_id, purpose, attempt, reservation, ex):
        stem = self._stem(unit_id, purpose, attempt, uuid.uuid4().hex)  # 8 hex chars + ".denied.json"
        return self._write_json(stem.with_name(stem.name + ".denied.json"), {
            "schema": ARCHIVE_SCHEMA, "kind": "budget_denied", "created_at": _now(), "unit_id": unit_id,
            "purpose": purpose, "attempt": attempt, "scope": self.scope, "reserved_usd": str(reservation),
            "message": str(ex)[:300]})

    def _error_record(self, stem, kind, detail):
        return self._write_json(stem.with_name(stem.name + ".error.json"), {
            "schema": ARCHIVE_SCHEMA, "kind": kind, "created_at": _now(), **detail})

    def _interpret(self, response, request_id):
        """Classify an archived response and settle its reservation."""
        status = int(response.status)
        payload = None
        try:
            payload = json.loads((response.body or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        if status != 200:
            error = (payload or {}).get("error") if isinstance(payload, dict) else None
            error_type, flagged = self._error_kind(error)
            if 400 <= status < 500 and status not in _UNCERTAIN_4XX:
                # Rejected before generation (pilot semantics): exactly 0.
                settlement = self._settle(request_id, 0, "unbilled_error")
            else:  # 408, 5xx or an unfollowed redirect: billing unknown
                settlement = self._settle(request_id, None, None)
            detail = {"http_status": status, "error_type": error_type,
                      "message": str((error or {}).get("message") or "")[:300] if isinstance(error, dict) else None}
            if status == 403 and (error_type in _REFUSAL_ERROR_TYPES or flagged):
                return _Outcome("refusal", f"http_403_{error_type or 'flagged'}", settlement=settlement, detail=detail)
            return _Outcome("grading_failed", f"http_{status}", settlement=settlement, detail=detail)
        if not isinstance(payload, dict):
            settlement = self._settle(request_id, None, None)
            return _Outcome("grading_failed", "unparseable_response", settlement=settlement)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        reported = openrouter_reported_cost(usage)
        settlement = self._settle(request_id, reported, "provider_reported_cost" if reported is not None else None)
        generation_id = payload.get("id") if isinstance(payload.get("id"), str) else None
        discount = payload.get("cache_discount", (usage or {}).get("cache_discount"))
        common = {"settlement": settlement, "usage": usage, "generation_id": generation_id,
                  "provider": payload.get("provider"), "model": payload.get("model"),
                  "service_tier": payload.get("service_tier"),
                  "cache_discount": None if _decimal_or_none(discount) is None else str(discount)}
        choices = payload.get("choices")
        if isinstance(payload.get("error"), dict) and not choices:
            error_type, flagged = self._error_kind(payload["error"])
            detail = {"http_status": 200, "error_code": payload["error"].get("code"), "error_type": error_type,
                      "message": str(payload["error"].get("message") or "")[:300]}
            if error_type in _REFUSAL_ERROR_TYPES or flagged:
                return _Outcome("refusal", f"body_error_{error_type or 'flagged'}", detail=detail, **common)
            return _Outcome("grading_failed", "provider_error_in_body", detail=detail, **common)
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return _Outcome("grading_failed", "no_choices", **common)
        mismatch = self._route_mismatch(payload)
        if mismatch:
            return _Outcome("grading_failed", "route_mismatch", detail={"route": mismatch}, **common)
        choice = choices[0]
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        finish = choice.get("finish_reason")
        native = choice.get("native_finish_reason")
        text = self._content_text(message.get("content"))
        reasoning = message.get("reasoning") if isinstance(message.get("reasoning"), str) else None
        refusal = message.get("refusal")
        common.update(finish_reason=finish, reasoning=reasoning)
        if finish in _REFUSAL_FINISH or native in _REFUSAL_FINISH or (isinstance(refusal, str) and refusal.strip()):
            return _Outcome("refusal", f"finish_{finish}" if finish in _REFUSAL_FINISH else
                            (f"native_{native}" if native in _REFUSAL_FINISH else "message_refusal"),
                            text=text, detail={"refusal": refusal if isinstance(refusal, str) else None,
                                               "native_finish_reason": native}, **common)
        if finish == "error" or isinstance(choice.get("error"), dict):
            return _Outcome("grading_failed", "provider_error_in_choice", text=text,
                            detail={"native_finish_reason": native}, **common)
        if finish == "length":
            return _Outcome("ungraded", "max_tokens", text=text, detail={"native_finish_reason": native}, **common)
        return _Outcome("ok", None, text=text, **common)

    @staticmethod
    def _error_kind(error):
        if not isinstance(error, dict):
            return None, False
        metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
        error_type = metadata.get("error_type") if isinstance(metadata.get("error_type"), str) else None
        # Moderation blocks carry flagged_input/reasons (OpenRouter errors docs).
        flagged = bool(metadata.get("flagged_input") or metadata.get("reasons"))
        return error_type, flagged

    def _route_mismatch(self, payload):
        """Non-empty description when the response was not served by the pinned route."""
        problems = {}
        provider = payload.get("provider")
        if provider is not None and str(provider).strip().lower() != PROVIDER_NAME.lower():
            problems["provider"] = provider
        model = payload.get("model")
        accepted = {self.route.model_id, self.route.canonical_slug}
        if model is not None and model not in accepted and not str(model).startswith(self.route.model_id + "-"):
            problems["model"] = model
        tier = payload.get("service_tier")
        if tier not in STANDARD_SERVICE_TIERS:
            problems["service_tier"] = tier
        return problems

    @staticmethod
    def _content_text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content
                           if isinstance(part, dict) and part.get("type") in (None, "text"))
        return "" if content is None else str(content)

    @staticmethod
    def _merge(result, outcome):
        result["attempts"] += 1 if outcome.dispatched else 0
        if outcome.request_id:
            result["request_ids"].append(outcome.request_id)
        result["raw_paths"].extend(outcome.paths)
        if outcome.settlement:
            result["settlements"].append(outcome.settlement)
        if outcome.generation_id:
            result["generation_ids"].append(outcome.generation_id)
        for key in ("provider", "model", "service_tier", "finish_reason", "reasoning"):
            value = getattr(outcome, key)
            if value is not None:
                result[key] = value
        result["text"] = outcome.text
        usage = result["usage"]
        per = usage.setdefault("per_attempt", [])
        per.append(outcome.usage)
        if isinstance(outcome.usage, dict):
            details = outcome.usage.get("completion_tokens_details") or {}
            prompt_details = outcome.usage.get("prompt_tokens_details") or {}
            for name, value in (("prompt_tokens", outcome.usage.get("prompt_tokens")),
                                ("completion_tokens", outcome.usage.get("completion_tokens")),
                                ("reasoning_tokens", details.get("reasoning_tokens")),
                                ("cached_tokens", prompt_details.get("cached_tokens")),
                                ("cache_write_tokens", prompt_details.get("cache_write_tokens"))):
                if isinstance(value, int) and not isinstance(value, bool):
                    usage[name] = usage.get(name, 0) + value
        if outcome.cache is not None:
            JudgeClient._merge_cache(result["cache"], outcome.cache)

    @staticmethod
    def _merge_cache(cache, record):
        cache["per_attempt"].append(record)
        priced = record.get("priced") if isinstance(record.get("priced"), dict) else {}
        cache["read_tokens"] += int(priced.get("cache_read_tokens") or 0)
        cache["write_tokens"] += int(priced.get("cache_write_tokens") or 0)
        for total, part in (("cache_read_usd", "cache_read_usd"), ("cache_write_usd", "cache_write_usd"),
                            ("priced_total_usd", "total_usd"), ("saving_usd", "cache_saving_usd")):
            value = _decimal_or_none(priced.get(part))
            if value is not None:
                cache[total] = str(Decimal(cache[total]) + value)
        discount = _decimal_or_none(record.get("reported_cache_discount_usd"))
        if discount is not None:
            previous = _decimal_or_none(cache["reported_cache_discount_usd"]) or Decimal(0)
            cache["reported_cache_discount_usd"] = str(previous + discount)

    @staticmethod
    def _total_cost(settlements):
        if not settlements or any(s.get("cost_usd") is None for s in settlements):
            return None
        return str(sum((Decimal(s["cost_usd"]) for s in settlements), Decimal(0)))
