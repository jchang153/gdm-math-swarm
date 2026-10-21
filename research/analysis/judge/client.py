"""Budgeted OpenRouter client for the transcript behavior judge (researcher-only).

docs/plan.md "Transcript behavior judge specified, October 17, 2026" (with the
"downsized model allocation" decision): OpenRouter with the main host key, provider
pinned to Anthropic with no fallbacks, real-time requests, the shared ledger's
``analysis`` scope enforcing the analysis allowance (``budget.JUDGE_ANALYSIS_CAP_USD``, $200
after the five audited raises $5 -> $40 -> $50 -> $100 -> $110 -> $200; the pilot's
unlimited-key exception extends to this analysis), a refused request retried once and then
``ungraded`` (never a negative), and a judge model that never silently changes. One client
serves one model:
``"haiku"`` (screening), ``"sonnet"`` (per-agent judging) or ``"opus"`` (verification, one
finding per request, and the run level); see route.MODEL_ALLOCATION. Judge revision v3
(docs/plan.md, "Behavior judge iteration specified, October 19, 2026", items 6, 7 and 10): a
multi-item request is never resent after a refusal (``refusal_retry=False``), the provider's
refusal message is recorded, an HTTP 429 is resent with growing delays, and a ``RunLimit``
holds one judging invocation to its own limit beside the ledger's cap.
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
   dispatched (``grading_failed``, reason ``budget_admission_denied``). With a
   ``RunLimit`` (judge revision v3, item 7: the invocation's own limit, 2x its central
   estimate unless overridden), the same reservation must first fit under that limit
   together with the invocation's committed spend (its in-flight holds, settled costs and
   uncertain holds); otherwise nothing is reserved or sent (``grading_failed``, reason
   ``run_limit``). The limit follows every settlement.
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
   exactly once with a fresh reservation, then ``ungraded`` (``refused_twice``). A caller
   sending several items in one request passes ``refusal_retry=False`` (judge revision v3,
   docs/plan.md, "Behavior judge iteration specified, October 19, 2026", item 6): the
   identical request is then never resent, the result is ``ungraded`` with reason
   ``refused_once`` and the caller splits the items into single-item requests. The
   provider's refusal message (``message.refusal``, or the error message of an error-shaped
   refusal) is kept per refused attempt in ``refusal_details``; the request text is never
   altered to get past a filter. Truncation at ``max_tokens`` is ``ungraded`` with reason
   ``max_tokens`` and the truncated text (the pipeline treats it as a malformed output:
   one repair). An HTTP 429 (rate limited: rejected before any processing, so unbilled)
   settles ``unbilled_error`` at 0 and is resent after a growing delay (``Retry-After``
   when the response gives one, else ``RATE_LIMIT_BASE_DELAY_S`` doubling, capped at
   ``RATE_LIMIT_MAX_DELAY_S``), each time with a fresh reservation, for at most
   ``RATE_LIMIT_ATTEMPTS`` requests in all; then ``grading_failed`` (``http_429``). API
   and infrastructure failures, and a response served by anything other than the pinned
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
import email.utils
import hashlib
import json
import random
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
# HTTP 429 (rate limited; never processed, so unbilled): resent with growing delays (judge
# revision v3, item 10). At most RATE_LIMIT_ATTEMPTS requests are sent while the answer is 429;
# the delay is the response's Retry-After when given, else the base doubling per 429, never
# above the cap, plus up to RATE_LIMIT_JITTER of itself so concurrent requests spread out.
RATE_LIMITED = 429
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_BASE_DELAY_S = 2.0
RATE_LIMIT_MAX_DELAY_S = 300.0
RATE_LIMIT_JITTER = 0.25
REFUSAL_MESSAGE_CHARS = 1000
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
    """Real HTTPS POST (stdlib), run in a worker thread; no proxies, no redirects.

    Each transport owns a bounded thread pool of ``max_workers`` threads (the client's
    ``max_concurrency``: a request admitted by the client's semaphore always has a thread, never
    queued behind other clients' requests in asyncio's shared default executor, which holds at most
    min(32, CPUs + 4) threads) and one verified TLS context shared by its connections (created
    once, not per request)."""
    def __init__(self, max_workers=8):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        self.max_workers = max_workers
        self._context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                   urllib.request.HTTPSHandler(context=self._context), _NoRedirect())
        self._executor = None
        self._executor_lock = threading.Lock()

    def _pool(self):
        with self._executor_lock:
            if self._executor is None:
                from concurrent.futures import ThreadPoolExecutor
                self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="judge-http")
            return self._executor

    def close(self):
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)

    async def post(self, url, headers, body, timeout):
        return await asyncio.get_running_loop().run_in_executor(self._pool(), self._post, url, headers, body, timeout)

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
    return {"status": None, "text": None, "attempts": 0, "refusals": 0, "refusal_details": [], "rate_limited": 0,
            "rate_limit_delays_s": [], "admission_waits": 0, "usage": {}, "cost_usd": None,
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


def retry_after_seconds(value, now=None):
    """Seconds asked for by an HTTP ``Retry-After`` header (delta-seconds or an HTTP date),
    else None (absent, unparseable or negative)."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        seconds = max(0.0, (moment - (now or datetime.datetime.now(datetime.timezone.utc))).total_seconds())
    if seconds != seconds or seconds < 0 or seconds == float("inf"):  # NaN, negative or infinite
        return None
    return seconds


def rate_limit_delay(rate_limited, retry_after=None, jitter=0.0):
    """Delay before resending a request that got its ``rate_limited``-th HTTP 429: the
    response's Retry-After when given, else ``RATE_LIMIT_BASE_DELAY_S`` doubling per 429;
    never above ``RATE_LIMIT_MAX_DELAY_S``; ``jitter`` in [0, 1) adds up to
    ``RATE_LIMIT_JITTER`` of the delay."""
    base = retry_after if retry_after is not None else RATE_LIMIT_BASE_DELAY_S * 2 ** max(0, rate_limited - 1)
    base = min(float(base), RATE_LIMIT_MAX_DELAY_S)
    return min(base * (1 + RATE_LIMIT_JITTER * max(0.0, min(float(jitter), 1.0))), RATE_LIMIT_MAX_DELAY_S)


RUN_LIMIT_REASON = "run_limit"
RUN_LIMIT_SCHEMA = "gdm-judge-run-limit/1"
LABEL_PREFIX = "judge-"
LABEL_LENGTH = 200


def request_label(model_key, purpose, unit_id, run_id=None):
    """The ledger label of one judge request (the ``calls.agent`` column):
    ``judge-<model>:<run id>:<purpose>:<unit id>``, or ``judge-<model>:<purpose>:<unit id>``
    without a run id (requests made before the per-run cap existed). The run id comes before
    anything long, so truncation to ``LABEL_LENGTH`` characters never cuts it."""
    parts = [f"{LABEL_PREFIX}{model_key}"] + ([str(run_id)] if run_id else []) + [str(purpose), str(unit_id)]
    return ":".join(parts)[:LABEL_LENGTH]


def committed_for_run(ledger_path, run_id, scope=ANALYSIS_SCOPE):
    """What the ledger holds against one run's judging, read only (docs/plan.md, "Live behavior
    judge during the run (October 20, 2026)"): the sum over every ``scope`` row whose label names
    ``run_id`` (``request_label``) of its billed amount, or its full reservation while it is
    reserved or uncertain (``COALESCE(actual, reserved)``, exactly what the ledger counts). The
    ledger row is the first durable record of a request (written before its request archive),
    so a hold orphaned by a killed process is counted too. Returns
    ``{"run_id", "committed_usd", "rows", "unresolved_rows"}``; a missing ledger counts 0."""
    from research_swarm.budget import usd
    path = Path(ledger_path)
    if not run_id or not path.exists():
        return {"run_id": run_id, "committed_usd": "0", "rows": 0, "unresolved_rows": 0}
    marker = f":{run_id}:"
    with closing(sqlite3.connect("file:" + str(path.resolve()) + "?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA busy_timeout=30000")
        row = db.execute("SELECT COALESCE(SUM(COALESCE(actual, reserved)), 0), COUNT(*), "
                         "COALESCE(SUM(CASE WHEN state IN ('reserved', 'uncertain') THEN 1 ELSE 0 END), 0) "
                         "FROM calls WHERE scope=? AND substr(agent, 1, ?)=? AND instr(agent, ?) > 0",
                         (scope, len(LABEL_PREFIX), LABEL_PREFIX, marker)).fetchone()
    return {"run_id": run_id, "committed_usd": str(usd(row[0])), "rows": int(row[1]), "unresolved_rows": int(row[2])}


class RunLimit:
    """One judging invocation's own spending limit (judge revision v3, docs/plan.md, "Behavior
    judge iteration specified, October 19, 2026", item 7), shared by the invocation's clients.

    The invocation's committed spend is what its own requests hold or cost: the reservation of
    every request still in flight, the settled cost of every settled request (0 for an unbilled
    one) and the full reservation of every request whose billing is uncertain (as the ledger
    keeps it until ``reconcile()``). ``admit`` refuses a request whose reservation would push the
    committed spend past ``limit_usd``; the client then answers ``grading_failed`` with reason
    ``run_limit`` without touching the ledger or the network, and the pipeline treats it like a
    ledger denial (waits while other requests are in flight, else stops cleanly). The ledger's
    own cap still applies to every admitted request. ``estimate`` (the invocation estimate that
    set the limit), ``source`` (``estimate``: 2x the central estimate, or ``override``:
    ``--run-limit``) and ``basis`` (what set an estimated limit: ``factor``, ``largest_reservation``
    when the largest single reservation needed more room, or ``floor``) are recorded with the
    totals (``record()``)."""

    def __init__(self, limit_usd, *, source="estimate", estimate=None, factor=None, floor_usd=None, basis=None,
                 standing_approval=None, prior_usd=None, prior=None):
        limit = _decimal_or_none(limit_usd)
        if limit is None or limit <= 0:
            raise ValueError("run limit must be a positive amount in USD")
        self.limit = limit
        # A per-run cap (docs/plan.md, "Live behavior judge during the run (October 20, 2026)"): what
        # earlier invocations already committed against the same run (``committed_for_run``) counts
        # against ``limit`` from the start, so a resume or a later stage never gets a fresh cap.
        prior_amount = _decimal_or_none(prior_usd)
        if prior_amount is not None and prior_amount < 0:
            raise ValueError("prior_usd must not be negative")
        self.prior = prior_amount or Decimal(0)
        self.prior_record = prior
        self.source = source
        self.estimate = estimate
        self.factor = factor
        self.floor = _decimal_or_none(floor_usd)
        self.basis = basis
        # Source ``standing_approval`` (docs/plan.md, "Automatic post-run behavior judge for the owner's
        # runs (October 20, 2026)"): the config's approval note, path and per-run cap, recorded as given.
        self.standing_approval = standing_approval
        self.created_at = _now()
        self._lock = threading.Lock()
        self._held = {}                  # token -> (reserved USD, purpose, model key)
        self._uncertain = Decimal(0)
        self._settled = Decimal(0)
        self._next = 0
        self.requests = 0                # admitted requests (each attempt is one request)
        self.denied = 0
        self.first_denial = None
        self.peak_committed = Decimal(0)
        self.by_purpose = {}

    def committed(self):
        with self._lock:
            return self._committed()

    def _committed(self):
        return (sum((held for held, _, _ in self._held.values()), Decimal(0)) + self._uncertain + self._settled
                + self.prior)

    def _row(self, purpose, model):
        return self.by_purpose.setdefault(purpose or "?", {"model_key": model, "requests": 0, "settled_usd": Decimal(0),
                                                           "uncertain_usd": Decimal(0)})

    def admit(self, amount, *, purpose=None, model=None, label=None):
        """A token when ``amount`` (the request's ledger reservation) fits under the limit
        together with the committed spend, else None (the denial is counted)."""
        amount = _decimal_or_none(amount) or Decimal(0)
        with self._lock:
            committed = self._committed()
            if committed + amount > self.limit:
                self.denied += 1
                if self.first_denial is None:
                    self.first_denial = {"at": _now(), "label": label, "reserved_usd": str(amount),
                                         "committed_usd": str(committed), "limit_usd": str(self.limit)}
                return None
            self._next += 1
            token = self._next
            self._held[token] = (amount, purpose, model)
            self.requests += 1
            self._row(purpose, model)["requests"] += 1
            self.peak_committed = max(self.peak_committed, committed + amount)
            return token

    def settle(self, token, cost, *, uncertain=False):
        """Replace the hold of ``token`` by its settled ``cost``; an uncertain request keeps its
        full reservation committed. Unknown tokens are ignored."""
        with self._lock:
            entry = self._held.pop(token, None)
            if entry is None:
                return
            held, purpose, model = entry
            row = self._row(purpose, model)
            if uncertain:
                self._uncertain += held
                row["uncertain_usd"] += held
                return
            cost = _decimal_or_none(cost) or Decimal(0)
            self._settled += cost
            row["settled_usd"] += cost

    def release(self, token):
        """A request that was never dispatched (the ledger refused it or its archive failed):
        nothing was spent."""
        self.settle(token, Decimal(0))

    def denial_detail(self, amount):
        with self._lock:
            return {"run_limit_usd": str(self.limit), "committed_usd": str(self._committed()),
                    "reserved_usd": str(_decimal_or_none(amount) or Decimal(0))}

    def record(self):
        """The limit, how it was set and this invocation's own totals (JSON-ready)."""
        with self._lock:
            held = sum((h for h, _, _ in self._held.values()), Decimal(0))
            return {"schema": RUN_LIMIT_SCHEMA, "limit_usd": str(self.limit), "source": self.source,
                    "basis": self.basis, "factor": None if self.factor is None else str(self.factor),
                    "floor_usd": None if self.floor is None else str(self.floor), "created_at": self.created_at,
                    "estimate": self.estimate, "standing_approval": self.standing_approval,
                    "prior_usd": str(self.prior), "prior": self.prior_record,
                    "committed_usd": str(self._committed()),
                    "peak_committed_usd": str(self.peak_committed), "settled_usd": str(self._settled),
                    "uncertain_usd": str(self._uncertain), "in_flight_usd": str(held), "requests": self.requests,
                    "denied": self.denied, "first_denial": self.first_denial,
                    "by_purpose": {k: dict(v, settled_usd=str(v["settled_usd"]), uncertain_usd=str(v["uncertain_usd"]))
                                   for k, v in sorted(self.by_purpose.items())}}


@dataclass
class _Outcome:
    kind: str                     # ok | refusal | rate_limited | ungraded | grading_failed | budget_denied
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
    the existing ledger with every applied audited judge raise (``budget.judge_analysis_problems``:
    all five, the last $110 -> $200 bound to docs/plan.md, "Behavior judge iteration specified,
    October 19, 2026"), and opens it at ``budget.JUDGE_ANALYSIS_CAP_USD``; any other scope (offline rehearsal) needs
    explicit ``ledger_caps`` and a fake transport. ``max_concurrency`` defaults to
    ``route.DEFAULT_CONCURRENCY`` (haiku 24, sonnet 16, opus 8; judge revision v3, item 10).
    The delay before resending a rate-limited (HTTP 429) request is awaited outside the
    concurrency semaphore through ``self.sleep`` (``asyncio.sleep``; tests replace it).
    ``run_limit`` (a ``RunLimit`` shared by the invocation's three clients; judge revision v3,
    item 7) refuses any request whose reservation would push the invocation's committed spend
    past its limit (reason ``run_limit``); the ledger's cap applies independently.

    ``credentials_root`` (paid path only; ``repo_root`` is an accepted alias): the
    directory whose private ``.env`` holds the host key, default this checkout's root.
    It is refused with a fake transport or an explicit ``key_loader``, and nothing is
    read from it until ``prepare()`` (or the first ``complete()``).
    """
    def __init__(self, model_key, ledger_path, scope=ANALYSIS_SCOPE, archive_dir=None, settings=None, http=None,
                 max_concurrency=None, *, paid_authorized=False, ledger_caps=None, key_loader=None, verify_limit=None,
                 credentials_root=None, repo_root=None, run_limit=None, run_id=None):
        if model_key not in MODEL_KEYS:
            raise ValueError(f"Unknown judge model {model_key!r}; expected one of {MODEL_KEYS}")
        if run_limit is not None and not isinstance(run_limit, RunLimit):
            raise ValueError("run_limit must be a RunLimit (shared by the invocation's clients) or None")
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
            # Never create a replacement ledger, and require both applied audited raises.
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
        self.run_limit = run_limit
        # The judged run's id goes into every ledger label (``request_label``), so the per-run cap
        # can be computed from the ledger alone (``committed_for_run``).
        self.run_id = run_id
        self._limit_tokens = {}             # ledger request id -> RunLimit token
        self.http = UrllibTransport(max_workers=max_concurrency) if self._paid else http
        self._key_loader = key_loader
        self._verify_limit = verify_limit
        self._key = None
        self._prepared = False
        self._prepare_lock = threading.Lock()
        self._semaphores = {}
        self.limit_report = None
        self.sleep = asyncio.sleep          # HTTP 429 back-off (outside the semaphore)
        self.jitter = random.random         # spreads the back-off of concurrent requests

    # -- paid-path preparation ----------------------------------------------
    def prepare(self):
        """Load the key (paid path only) and run the unpaid provider-limit check once.

        Without a key loader (fake transports) this does nothing. The limit check uses
        the approved unlimited-key exception (plan, Route and key): the ledger, not a
        provider cap, enforces the analysis allowance. The report (never the key) is
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
                       cache_system=None, refusal_retry=True, admission_wait=None):
        """One judged unit -> ClientResult dict (see module docstring for semantics).

        ``cache_system``: mark the system prompt (the shared rubric prefix) for
        Anthropic prompt caching; ``None`` uses the model default
        (``JudgeRouteSettings.cache_system_default``: Sonnet and Opus on, Haiku off).
        An empty system prompt is never marked. ``refusal_retry`` (default True): resend a
        refused request once; False for a request holding several items, which is then
        never resent identically (``refused_once``; the caller splits it).
        ``admission_wait`` (optional ``async f(reason) -> bool``; judge revision v3, items 6 and
        7): when the one permitted retry of a refused request is denied admission (by the ledger
        or the run limit), the client awaits it and asks for admission of that same retry again
        while it returns True (the caller waits for its other in-flight requests to settle;
        nothing was sent, so this is not an extra paid attempt); False returns the denial
        (``grading_failed`` with ``refusals`` 1, which the caller records so that only the retry
        is sent later).

        Keys: ``status`` (ok | ungraded | grading_failed), ``text`` (final visible
        reply, None unless ok/truncated), ``attempts`` (requests dispatched),
        ``refusals``, ``refusal_details`` (per refused attempt: ``attempt``, ``signal``
        and the provider's ``message``), ``rate_limited`` (HTTP 429 answers, each settled
        unbilled at 0) and ``rate_limit_delays_s``, ``admission_waits`` (times the refusal retry
        was asked for again after ``admission_wait``), ``usage`` (summed token counts,
        including ``cached_tokens`` and ``cache_write_tokens``, plus ``per_attempt``),
        ``cost_usd`` (sum of settled costs, None while any attempt is uncertain),
        ``request_ids`` (ledger ids), ``raw_paths`` (archive files), and additionally
        ``finish_reason``, ``reasoning`` (summarized reasoning text), ``error``
        (``{"reason", ...}`` or None), ``generation_ids``, ``provider``, ``model``,
        ``service_tier``, ``settlements`` (per request: state, cost, basis) and ``cache``
        (``system_marked``, ``ttl``, ``read_tokens``, ``write_tokens``, frozen-price
        ``cache_read_usd``/``cache_write_usd``/``priced_total_usd``/``saving_usd``,
        ``reported_cache_discount_usd`` and ``per_attempt``).
        """
        cache_system = self._validate_call(system, user, purpose, unit_id, max_tokens, effort, response_format,
                                           cache_system)
        if not isinstance(refusal_retry, bool):
            raise ValueError("refusal_retry must be True or False")
        if not self._prepared:
            await asyncio.to_thread(self.prepare)
        result = _empty_result(cache_system)
        refusals_allowed = 1 if refusal_retry else 0
        attempt = 0
        while True:
            attempt += 1
            outcome = await self._attempt(system, user, purpose, unit_id, max_tokens, effort, response_format,
                                          attempt, cache_system)
            self._merge(result, outcome)
            if outcome.kind == "rate_limited":
                # Rejected before any processing (settled unbilled at 0): wait, then resend
                # with a fresh reservation, at most RATE_LIMIT_ATTEMPTS requests in all.
                result["rate_limited"] += 1
                if result["rate_limited"] < RATE_LIMIT_ATTEMPTS:
                    delay = rate_limit_delay(result["rate_limited"], (outcome.detail or {}).get("retry_after_s"),
                                             self.jitter())
                    result["rate_limit_delays_s"].append(round(delay, 3))
                    await self.sleep(delay)
                    continue
                result["status"] = "grading_failed"
                result["error"] = {"reason": outcome.reason, **(outcome.detail or {}),
                                   "rate_limited": result["rate_limited"]}
                result["text"] = None
            elif outcome.kind == "refusal":
                result["refusals"] += 1
                result["refusal_details"].append(self._refusal_detail(attempt, outcome))
                if result["refusals"] <= refusals_allowed:
                    continue  # the one permitted retry of a single-item request (plan, Refusals)
                result["status"] = "ungraded"
                result["error"] = {"reason": "refused_twice" if result["refusals"] > 1 else "refused_once",
                                   "last": outcome.reason, "message": result["refusal_details"][-1].get("message"),
                                   "retried": result["refusals"] > 1}
                result["text"] = None
            elif outcome.kind == "ok":
                result["status"] = "ok"
                result["error"] = None
            elif outcome.kind == "ungraded":
                result["status"] = "ungraded"
                result["error"] = {"reason": outcome.reason, **(outcome.detail or {})}
            else:  # grading_failed / budget_denied: never retried automatically
                if outcome.kind == "budget_denied" and result["refusals"] and admission_wait is not None \
                        and await admission_wait(outcome.reason):
                    # The one permitted retry of a refused request was not admitted: nothing was
                    # sent, and the caller saw room free up. Ask for admission of that retry again.
                    result["admission_waits"] += 1
                    continue
                result["status"] = "grading_failed"
                result["error"] = {"reason": outcome.reason, **(outcome.detail or {})}
                result["text"] = None
            break
        result["cost_usd"] = self._total_cost(result["settlements"])
        return result

    # -- internals ------------------------------------------------------------
    def _refusal_detail(self, attempt, outcome):
        """What one refused attempt recorded: the refusal signal and the provider's message
        (``message.refusal``, or an error-shaped refusal's message), credential-scrubbed."""
        detail = outcome.detail or {}
        message = detail.get("refusal") or detail.get("message")
        out = {"attempt": attempt, "signal": outcome.reason,
               "message": self._scrub_text(str(message))[:REFUSAL_MESSAGE_CHARS] if message else None}
        for key in ("native_finish_reason", "http_status", "error_type"):
            if detail.get(key) is not None:
                out[key] = detail[key]
        return out

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
        """Synchronous (never half-observed under cancellation); returns the settlement record.
        The invocation's run limit (``RunLimit``) follows the same settlement: the settled cost
        replaces the hold, an uncertain request keeps its reservation committed. If the ledger
        fails to record the settlement (for example a locked database), the ledger still holds
        the reservation, so the run limit keeps it committed as uncertain and the error
        propagates."""
        token = self._limit_tokens.pop(request_id, None)
        try:
            if basis == "unbilled_error":
                self.ledger.settle(request_id, 0, "unbilled_error")
                if token is not None:
                    self.run_limit.settle(token, Decimal(0))
                return {"request_id": request_id, "state": "settled", "cost_usd": "0", "basis": "unbilled_error"}
            if cost is None:
                self.ledger.settle(request_id, None)
                if token is not None:
                    self.run_limit.settle(token, None, uncertain=True)
                return {"request_id": request_id, "state": "uncertain", "cost_usd": None, "basis": None}
            self.ledger.settle(request_id, cost, basis)
            if token is not None:
                self.run_limit.settle(token, cost)
            state = next((r["state"] for r in self._request_state(request_id)), "settled")
            return {"request_id": request_id, "state": state, "cost_usd": str(cost), "basis": basis}
        except BaseException:
            if token is not None:
                self.run_limit.settle(token, None, uncertain=True)  # a no-op once the hold was replaced
            raise

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
            label = request_label(self.model_key, purpose, unit_id, self.run_id)
            token = None
            if self.run_limit is not None:
                # This invocation's own limit (judge revision v3, item 7), checked before the ledger.
                token = self.run_limit.admit(reservation, purpose=purpose, model=self.model_key, label=label)
                if token is None:
                    detail = self.run_limit.denial_detail(reservation)
                    message = (f"run limit: reserving ${reservation} would take this invocation's committed spend "
                               f"(${detail['committed_usd']}) past its run limit ${detail['run_limit_usd']}")
                    path = self._denial_record(unit_id, purpose, attempt, reservation, message, kind="run_limit_denied")
                    return _Outcome("budget_denied", RUN_LIMIT_REASON, paths=[path], dispatched=False,
                                    detail=dict(detail, message=message))
            try:
                request_id = self.ledger.reserve(self.scope, label, reservation)
            except BudgetExceeded as ex:
                if token is not None:
                    self.run_limit.release(token)
                path = self._denial_record(unit_id, purpose, attempt, reservation, ex)
                return _Outcome("budget_denied", "budget_admission_denied", paths=[path], dispatched=False,
                                detail={"reserved_usd": str(reservation), "message": str(ex)[:300]})
            except (sqlite3.Error, ValueError) as ex:
                if token is not None:
                    self.run_limit.release(token)
                return _Outcome("grading_failed", "budget_ledger_unavailable", dispatched=False,
                                detail={"error": type(ex).__name__, "message": str(ex)[:300]})
            if token is not None:
                self._limit_tokens[request_id] = token
            stem = self._stem(unit_id, purpose, attempt, request_id)
            headers = {"Content-Type": "application/json"}
            try:
                request_path = self._write_json(stem.with_name(stem.name + ".request.json"), {
                    "schema": ARCHIVE_SCHEMA, "kind": "request", "created_at": _now(), "unit_id": unit_id,
                    "purpose": purpose, "attempt": attempt, "model_key": self.model_key, "scope": self.scope,
                    "ledger_request_id": request_id, "reserved_usd": str(reservation),
                    "reservation_basis": (("prompt bound at max(prompt, cache write, cache read) price; no cache "
                                           "discount assumed") if cache_system else "prompt bound at prompt price")
                                         + "; output at the model's reservation allowance (at most max_tokens)",
                    "reserved_output_tokens": self.settings.reserved_output_tokens(self.model_key, max_tokens),
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

    def _denial_record(self, unit_id, purpose, attempt, reservation, ex, kind="budget_denied"):
        stem = self._stem(unit_id, purpose, attempt, uuid.uuid4().hex)  # 8 hex chars + ".denied.json"
        return self._write_json(stem.with_name(stem.name + ".denied.json"), {
            "schema": ARCHIVE_SCHEMA, "kind": kind, "created_at": _now(), "unit_id": unit_id,
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
            if status == RATE_LIMITED:
                # Rate limited before any processing: exactly 0, resent after a delay (complete()).
                settlement = self._settle(request_id, 0, "unbilled_error")
                headers = {str(k).lower(): v for k, v in (response.headers or {}).items()}
                return _Outcome("rate_limited", f"http_{status}", settlement=settlement,
                                detail={"http_status": status, "error_type": error_type,
                                        "message": str((error or {}).get("message") or "")[:300]
                                        if isinstance(error, dict) else None,
                                        "retry_after_s": retry_after_seconds(headers.get("retry-after"))})
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
