"""Pinned OpenRouter route, frozen prices and conservative reservations for the judge.

Researcher-only (docs/plan.md "Transcript behavior judge specified, October 17, 2026",
rows "Reading strategy", "Route and key", "Refusals" and "Spending", as superseded by
"Superseding decision (October 17, 2026): downsized model allocation" and "Superseding
decision (October 17, 2026, evening): misbehavior-focused judge"; judge rubric
gdm-judge-rubric/2 Section 7). Nothing here may enter experimental agent images,
containers, workspaces or prompts.

Route (verified against OpenRouter's public catalog and documentation on 2026-10-17;
scripts/freeze_judge_route.py re-fetches it and writes
data/validation/judge-route-pricing-<UTC>.json):

* Models (model allocation, judge rubric Section 7): ``anthropic/claude-haiku-5.5``
  (``"haiku"``, effort ``low``) screens every window; ``anthropic/claude-sonnet-5.5``
  (``"sonnet"``, effort ``high``) performs the per-agent judging (with the 5% recall
  sample folded into the same call); ``anthropic/claude-opus-5.5`` (``"opus"``, effort
  ``high``) re-checks every positive finding and writes the run-level summary. Effort is sent as Chat
  Completions ``reasoning: {"effort": ...}``, which OpenRouter forwards as Anthropic's
  ``output_config.effort`` with adaptive thinking (Claude 5.5 migration guide).
  Thinking budgets are not forwarded for 5.5 models, so none is sent. Opus 5.5 and
  Sonnet 5.5 reasoning is mandatory; Haiku 5.5 thinks adaptively at ``low``.
* Provider pin: ``{"order": ["anthropic"], "only": ["anthropic"],
  "allow_fallbacks": false, "require_parameters": true}``. ``require_parameters`` is
  true because the Anthropic endpoint lists every parameter the judge sends
  (``max_tokens``, ``reasoning`` and, only when a caller asks for it,
  ``response_format``): a request the endpoint cannot honor then fails instead of
  being silently degraded (the judge model and its settings never silently change).
  Per-block ``cache_control`` is message content, not one of OpenRouter's request
  parameters, so ``require_parameters`` does not gate it. Sampling parameters
  (``temperature``/``top_p``/``top_k``) are never sent: Claude 5.5 rejects non-default
  values and the endpoint does not list them. The base slug ``anthropic`` matches every
  standard ``anthropic[/region]`` endpoint but never a service-tier endpoint such as
  ``anthropic/fast`` (priority, 2x price), which needs an explicit
  ``service_tier``/tier slug (provider-selection and service-tier docs); the client
  still checks the served tier on every response.
* Prompt caching (prompt-caching docs): Anthropic caching is explicit (the endpoint
  reports ``supports_implicit_caching: false``). A request with ``cache_system`` marks
  its system message, the shared rubric prefix, as one text content block carrying
  ``cache_control: {"type": "ephemeral"}`` (the documented 5-minute breakpoint; the
  1-hour TTL is never requested). Claude 5.5 models cache prompts of at least 512
  tokens. Usage reports ``prompt_tokens_details.cached_tokens`` (read) and
  ``cache_write_tokens`` (written); OpenRouter's reported ``usage.cost`` already
  includes both. ``cache_system`` defaults to on for Sonnet and Opus and off for Haiku
  (``CACHE_SYSTEM_DEFAULTS``); ``JudgeRouteSettings.prompt_cache`` overrides it
  (``None`` = per-model defaults, ``True`` = on for every model, ``False`` = never).
  Reservations never assume a cache discount: every prompt token of a marked request is
  priced at the largest of the prompt, cache-write and cache-read prices.

Reservation (``JudgeRouteSettings.reservation``), mirroring the pilot's
``ProviderSettings.reservation``: a prompt-token upper bound equal to the UTF-8 byte
length of the exact serialized request body plus ``FRAMING_TOKEN_ALLOWANCE`` (the
Claude tokenizer is byte-level BPE, so every ordinary token covers at least one byte;
chat framing, the thinking preamble and any structured-output scaffolding are covered
by the allowance), capped at the context length, priced at the highest price among
the price tiers that bound could reach (Haiku 5.5 bills prompts of at least 100,000
tokens at a higher prompt AND completion price), plus ``max_tokens`` at that
completion price (adaptive thinking and the visible reply share ``max_tokens`` and
thinking bills as output). The byte bound is unverified until the first paid request:
a bill above the reservation settles as ``estimate_violation`` and the shared ledger
blocks the scope (fail closed).
"""
from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

PLAN_SECTION = "Transcript behavior judge specified, October 17, 2026"
ALLOCATION_DECISION = "Superseding decision (October 17, 2026, evening): misbehavior-focused judge"
MODEL_IDS = {"haiku": "anthropic/claude-haiku-5.5", "sonnet": "anthropic/claude-sonnet-5.5",
             "opus": "anthropic/claude-opus-5.5"}
MODEL_KEYS = tuple(MODEL_IDS)
# User decisions ("Reading strategy", the downsized allocation and the misbehavior-focused
# judge): Haiku low effort screens; Sonnet high effort judges; Opus high effort re-checks
# every positive finding and writes the run-level summary.
EFFORTS = {"haiku": "low", "sonnet": "high", "opus": "high"}
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Judge rubric Section 7 (documentation for callers; pipeline.STAGE_MODELS routes each unit).
MODEL_ALLOCATION = {"prefilter": ("haiku",), "judge": ("sonnet",), "verifier": ("opus",),
                    "runlevel": ("opus",)}
# Default request concurrency per model (configurable per client).
DEFAULT_CONCURRENCY = {"haiku": 24, "sonnet": 8, "opus": 6}
# Whether a request marks its system prompt (the shared rubric prefix) for caching
# when the caller does not say (JudgeClient.complete(cache_system=None)).
CACHE_SYSTEM_DEFAULTS = {"haiku": False, "sonnet": True, "opus": True}
CACHE_CONTROL = {"type": "ephemeral"}  # the documented 5-minute TTL; never "ttl": "1h"
CACHE_TTL = "5m"
MIN_CACHEABLE_PROMPT_TOKENS = 512  # Claude Haiku/Sonnet/Opus 5.5 (prompt-caching docs)
PROVIDER_TAG = "anthropic"
PROVIDER_NAME = "Anthropic"
# Response ``service_tier`` values that mean the standard tier (or an unreported one).
STANDARD_SERVICE_TIERS = (None, "", "default", "standard")

CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
CATALOG_URL = "https://openrouter.ai/api/v1/models"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"

# Parameters every judge request sends; require_parameters=true makes the router
# refuse an endpoint that does not list them.
REQUIRED_PARAMETERS = ("max_tokens", "reasoning")
# Listed by the endpoint when it honors an effort setting (checked, never sent itself).
EFFORT_PARAMETER = "reasoning_effort"
# Sent only when a caller passes ``response_format`` (structured outputs).
STRUCTURED_OUTPUT_PARAMETERS = ("response_format", "structured_outputs")

FRAMING_TOKEN_ALLOWANCE = 8192
# Per-request output ceiling (thinking + reply). Well below the route's 128,000
# maximum so one reservation stays small relative to the $40 allowance.
MAX_TOKENS_CEILING = 64000
# Advisory per-purpose defaults for callers (the client takes max_tokens per call).
DEFAULT_MAX_TOKENS = {"prefilter": 16000, "judge": 48000, "verifier": 24000, "runlevel": 48000}
# Non-streaming responses at high effort can take many minutes; a timeout leaves the
# reservation held as uncertain (never a silent release).
REQUEST_TIMEOUT_SECONDS = 1800.0

PRICING_PREFIX = "judge-route-pricing"
PRICING_SCHEMA = f"{PRICING_PREFIX}/v1"
DEFAULT_MAX_AGE_HOURS = 72  # same freshness window as the pilot's route-pricing gate


@dataclass(frozen=True)
class PriceTier:
    """One OpenRouter price entry (USD per token). ``min_prompt_tokens`` is 0 for the
    base entry and the override threshold otherwise (prices apply to the WHOLE
    request once its prompt reaches the threshold)."""
    min_prompt_tokens: int
    prompt: str
    completion: str
    cache_read: str | None = None
    cache_write: str | None = None
    cache_write_1h: str | None = None
    endpoint_tag: str = PROVIDER_TAG


@dataclass(frozen=True)
class ModelRoute:
    key: str
    model_id: str
    canonical_slug: str | None
    effort: str
    context_length: int
    max_completion_tokens: int
    tiers: tuple
    reasoning_mandatory: bool | None = None
    supported_efforts: tuple = ()
    supported_parameters: tuple = ()

    @property
    def structured_outputs(self):
        return set(STRUCTURED_OUTPUT_PARAMETERS) <= set(self.supported_parameters)

    @property
    def supports_prompt_cache(self):
        """Every price tier lists cache read and (5-minute) cache write prices."""
        return bool(self.tiers) and all(t.cache_read is not None and t.cache_write is not None for t in self.tiers)


# Catalog values observed 2026-10-17 (public /api/v1/models/<id>/endpoints, tag
# "anthropic"). They document the route and back offline tests; paid admission
# requires settings loaded from a fresh freeze record (pricing_verified=True).
_OBSERVED_PARAMETERS = tuple(sorted(("reasoning", "include_reasoning", "max_tokens", "stop", "tool_choice", "tools",
                                      "structured_outputs", "response_format", "verbosity", "reasoning_effort")))
_ALL_EFFORTS = ("max", "xhigh", "high", "medium", "low")
_OBSERVED = {
    "haiku": ModelRoute("haiku", MODEL_IDS["haiku"], "anthropic/claude-haiku-5.5-20261007", EFFORTS["haiku"], 1000000,
                        128000, (PriceTier(0, "0.0000001", "0.0000005", "0.00000001", "0.000000125", "0.0000002"),
                                 PriceTier(100000, "0.0000005", "0.0000025", "0.00000005", "0.000000625", "0.000001")),
                        False, (*_ALL_EFFORTS, "none"), _OBSERVED_PARAMETERS),
    "sonnet": ModelRoute("sonnet", MODEL_IDS["sonnet"], "anthropic/claude-sonnet-5.5-20260928", EFFORTS["sonnet"],
                         1000000, 128000, (PriceTier(0, "0.000002", "0.00001", "0.0000001", "0.0000025", "0.000004"),),
                         True, _ALL_EFFORTS, _OBSERVED_PARAMETERS),
    "opus": ModelRoute("opus", MODEL_IDS["opus"], "anthropic/claude-opus-5.5-20260921", EFFORTS["opus"], 1000000,
                       128000, (PriceTier(0, "0.000004", "0.00002", "0.0000002", "0.000005", "0.000008"),),
                       True, _ALL_EFFORTS, _OBSERVED_PARAMETERS),
}


def _marks_cache(serialized_body):
    data = serialized_body if isinstance(serialized_body, (bytes, bytearray)) else str(serialized_body).encode("utf-8")
    return b'"cache_control"' in data


@dataclass(frozen=True)
class JudgeRouteSettings:
    haiku: ModelRoute
    sonnet: ModelRoute
    opus: ModelRoute
    provider_tag: str = PROVIDER_TAG
    allow_fallbacks: bool = False
    require_parameters: bool = True
    # None: CACHE_SYSTEM_DEFAULTS per model; True: every model caches its system prompt
    # by default; False: no request is ever marked (cache_system=True is refused).
    prompt_cache: bool | None = None
    framing_token_allowance: int = FRAMING_TOKEN_ALLOWANCE
    max_tokens_ceiling: int = MAX_TOKENS_CEILING
    request_timeout_s: float = REQUEST_TIMEOUT_SECONDS
    endpoint_url: str = CHAT_COMPLETIONS_URL
    pricing_verified: bool = False
    pricing_record: str | None = None
    pricing_record_sha256: str | None = None

    def __post_init__(self):
        if self.prompt_cache not in (None, True, False):
            raise ValueError("prompt_cache must be None (per-model defaults), True or False")

    # -- construction -------------------------------------------------------
    @classmethod
    def defaults(cls, **overrides):
        """Observed 2026-10-17 route, ``pricing_verified=False`` unless overridden
        (offline tests and rehearsals only; paid admission uses a freeze record)."""
        return cls(**{**{key: _OBSERVED[key] for key in MODEL_KEYS}, **overrides})

    @classmethod
    def from_pricing_record(cls, path, *, now=None, max_age_hours=DEFAULT_MAX_AGE_HOURS, allow_stale=False,
                            prompt_cache=None):
        """Settings from a freeze record, re-validated offline; ``pricing_verified=True``.

        Refuses a record that failed, used a credential, is older than
        ``max_age_hours`` (unless ``allow_stale``, for offline rehearsals only), lacks
        any of the three models, or whose embedded catalog entries no longer validate.
        """
        path = Path(path)
        raw = path.read_bytes()
        record = json.loads(raw)
        if record.get("schema") != PRICING_SCHEMA:
            raise ValueError(f"{path} is not a {PRICING_SCHEMA} record")
        if record.get("all_required_passed") is not True or record.get("credential_used") is not False:
            raise ValueError(f"{path} did not pass, or used a credential")
        if record.get("provider_tag") != PROVIDER_TAG:
            raise ValueError("Pricing record provider tag differs from the pinned Anthropic route")
        created = datetime.datetime.fromisoformat(str(record.get("created_at")).replace("Z", "+00:00"))
        now = now or datetime.datetime.now(datetime.timezone.utc)
        if not allow_stale and (now - created) > datetime.timedelta(hours=max_age_hours):
            raise ValueError(f"Pricing record {path.name} is older than {max_age_hours} h; re-run "
                             "scripts/freeze_judge_route.py")
        models = record.get("models") or {}
        missing = [key for key in MODEL_KEYS if key not in models]
        if missing:
            raise ValueError(f"Pricing record {path.name} lacks {missing} (it predates the downsized model "
                             "allocation); re-run scripts/freeze_judge_route.py")
        routes = {}
        for key in MODEL_KEYS:
            entry = models.get(key) or {}
            route, problems = build_route(key, entry.get("catalog_entry"), entry.get("endpoints"))
            if problems:
                raise ValueError(f"Pricing record {key} no longer validates: " + "; ".join(problems))
            routes[key] = route
        return cls(**routes, prompt_cache=prompt_cache, pricing_verified=True, pricing_record=str(path),
                   pricing_record_sha256=hashlib.sha256(raw).hexdigest())

    # -- request pieces -----------------------------------------------------
    def route(self, key) -> ModelRoute:
        if key not in MODEL_KEYS:
            raise ValueError(f"Unknown judge model {key!r}; expected one of {MODEL_KEYS}")
        return getattr(self, key)

    def provider_preferences(self):
        if self.provider_tag != PROVIDER_TAG or self.allow_fallbacks:
            raise ValueError("The judge route is pinned to Anthropic with no fallbacks (plan, Route and key)")
        return {"order": [self.provider_tag], "only": [self.provider_tag], "allow_fallbacks": False,
                "require_parameters": bool(self.require_parameters)}

    def reasoning(self, key, effort):
        route = self.route(key)
        if effort != route.effort:
            raise ValueError(f"{key} effort is frozen at {route.effort!r} (plan, model allocation); got {effort!r}")
        # exclude=false keeps OpenRouter's summarized reasoning text for the archive.
        return {"effort": effort, "exclude": False}

    def cache_system_default(self, key) -> bool:
        """Whether ``key``'s requests mark the system prompt when the caller does not say."""
        route = self.route(key)
        if self.prompt_cache is False:
            return False
        wanted = True if self.prompt_cache is True else CACHE_SYSTEM_DEFAULTS[key]
        return bool(wanted and route.supports_prompt_cache)

    def resolve_cache_system(self, key, requested=None) -> bool:
        """The effective ``cache_system`` for one request (``None`` = the model default)."""
        if requested is None:
            return self.cache_system_default(key)
        if not isinstance(requested, bool):
            raise ValueError("cache_system must be True, False or None")
        if requested:
            if self.prompt_cache is False:
                raise ValueError("Prompt caching is disabled in these judge route settings (prompt_cache=False)")
            if not self.route(key).supports_prompt_cache:
                raise ValueError(f"The pinned {key} endpoint lists no cache read/write prices; caching refused")
        return requested

    # -- cost bounds --------------------------------------------------------
    def prompt_token_bound(self, key, serialized_body) -> int:
        size = len(serialized_body.encode("utf-8") if isinstance(serialized_body, str) else serialized_body)
        return min(size + int(self.framing_token_allowance), int(self.route(key).context_length))

    def applicable_tiers(self, key, prompt_bound):
        tiers = [t for t in self.route(key).tiers if int(t.min_prompt_tokens) <= int(prompt_bound)]
        if not tiers:
            raise ValueError(f"{key} has no base price tier")
        return tiers

    def price_bounds(self, key, prompt_bound, *, cache_system=False):
        """(prompt, completion) USD/token: the maximum over every tier the bound can reach.
        For a request marked for caching every prompt token is bounded by the largest of
        the prompt, cache-write (1.25x) and cache-read prices: no cache discount is assumed."""
        tiers = self.applicable_tiers(key, prompt_bound)
        prompt = []
        for tier in tiers:
            prompt.append(Decimal(tier.prompt))
            if cache_system:
                for value in (tier.cache_write, tier.cache_read):
                    if value is None:
                        raise ValueError(f"{key} tier {tier.min_prompt_tokens} lists no cache prices")
                    prompt.append(Decimal(value))
        return max(prompt), max(Decimal(t.completion) for t in tiers)

    def reservation(self, key, serialized_body, max_tokens, *, cache_system=False) -> Decimal:
        if not self.pricing_verified:
            raise RuntimeError("Freeze fresh judge route pricing (scripts/freeze_judge_route.py) before paid admission")
        max_tokens = self.check_max_tokens(key, max_tokens)
        bound = self.prompt_token_bound(key, serialized_body)
        # A body that carries any cache_control marker is priced as cached regardless of
        # what the caller said (fail safe: only ever raises the reservation).
        prompt, completion = self.price_bounds(key, bound, cache_system=bool(cache_system)
                                               or _marks_cache(serialized_body))
        return Decimal(bound) * prompt + Decimal(max_tokens) * completion

    def check_max_tokens(self, key, max_tokens):
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        limit = min(int(self.max_tokens_ceiling), int(self.route(key).max_completion_tokens))
        if max_tokens > limit:
            raise ValueError(f"max_tokens {max_tokens} exceeds the judge ceiling {limit}")
        return max_tokens

    # -- settled-usage accounting (never used for admission) -----------------
    def billing_prices(self, key, prompt_tokens):
        """USD/token prices of the tier a request with ``prompt_tokens`` bills at (the
        highest threshold reached; the maximum across standard endpoints at it)."""
        tiers = self.applicable_tiers(key, max(int(prompt_tokens or 0), 0))
        top = max(int(t.min_prompt_tokens) for t in tiers)
        chosen = [t for t in tiers if int(t.min_prompt_tokens) == top]

        def highest(name):
            values = [Decimal(getattr(t, name)) for t in chosen if getattr(t, name) is not None]
            return max(values) if values else None
        return {"min_prompt_tokens": top, "prompt": highest("prompt"), "completion": highest("completion"),
                "cache_read": highest("cache_read"), "cache_write": highest("cache_write")}

    def priced_usage(self, key, usage):
        """Cache-aware cost breakdown of one OpenRouter ``usage`` object at the frozen
        prices (USD strings), for the archive and the estimator; settlement always uses
        OpenRouter's reported ``usage.cost``. ``prompt_tokens`` includes cache reads and
        writes (OpenRouter usage accounting), so uncached = prompt - read - write.
        Returns None when the usage has no integer token counts."""
        if not isinstance(usage, dict):
            return None
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (prompt, completion)):
            return None
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}

        def count(name):
            value = details.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
        read, write = count("cached_tokens"), count("cache_write_tokens")
        uncached = max(prompt - read - write, 0)
        prices = self.billing_prices(key, prompt)

        def cost(tokens, price):
            if not tokens:
                return Decimal(0)
            return Decimal(tokens) * (price if price is not None else prices["prompt"])
        parts = {"uncached_prompt_usd": cost(uncached, prices["prompt"]),
                 "cache_read_usd": cost(read, prices["cache_read"]),
                 "cache_write_usd": cost(write, prices["cache_write"]),
                 "completion_usd": cost(completion, prices["completion"])}
        total = sum(parts.values(), Decimal(0))
        uncached_equivalent = Decimal(prompt) * prices["prompt"] + parts["completion_usd"]
        return {"basis": "frozen_route_prices", "tier_min_prompt_tokens": prices["min_prompt_tokens"],
                "prompt_tokens": prompt, "completion_tokens": completion, "cache_read_tokens": read,
                "cache_write_tokens": write, "uncached_prompt_tokens": uncached,
                **{name: str(value) for name, value in parts.items()}, "total_usd": str(total),
                "without_cache_usd": str(uncached_equivalent),
                "cache_saving_usd": str(uncached_equivalent - total)}

    def describe(self):
        return json.loads(json.dumps(asdict(self), default=str))

    def sha256(self):
        return hashlib.sha256(json.dumps(self.describe(), sort_keys=True).encode("utf-8")).hexdigest()

    def with_prompt_cache(self, enabled=True):
        return replace(self, prompt_cache=enabled)


# ---------------------------------------------------------------------------
# Catalog validation (shared by scripts/freeze_judge_route.py and the loader)

def _price(value, name, problems, *, required=False):
    if value is None:
        if required:
            problems.append(f"missing {name} price")
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        problems.append(f"unparseable {name} price {value!r}")
        return None
    if not number.is_finite() or number < 0:
        problems.append(f"invalid {name} price {value!r}")
        return None
    return str(value)


def _entries(pricing):
    return [pricing, *(pricing.get("overrides") or [])]


def matching_endpoints(endpoints, tag=PROVIDER_TAG):
    """(standard, tiered) endpoints the base slug ``tag`` could name. Standard ones
    (``tag`` or ``tag/<region>``, no service tier) are eligible for routing; service-
    tier endpoints (e.g. ``anthropic/fast``) need an explicit opt-in the judge never sends."""
    standard, tiered = [], []
    for endpoint in endpoints or []:
        name = str(endpoint.get("tag") or "")
        if name != tag and not name.startswith(tag + "/"):
            continue
        (standard if endpoint.get("service_tier") in STANDARD_SERVICE_TIERS else tiered).append(endpoint)
    return standard, tiered


def build_route(key, catalog_entry, endpoints, *, max_tokens_ceiling=MAX_TOKENS_CEILING):
    """(ModelRoute | None, problems) from a /models entry and the /endpoints list.

    Requires the exact model id, an available standard ``anthropic`` endpoint whose
    provider is Anthropic, every always-sent parameter, effort control (the endpoint
    lists ``reasoning_effort`` and the catalog's supported efforts include the frozen
    one), no per-request fee, no negative discount (surcharge), an internal-reasoning
    price (if any) no higher than the completion price, room for ``max_tokens_ceiling``
    and, for models that cache their system prompt by default (Sonnet, Opus), cache read
    and write prices on every tier. Prices take every standard Anthropic endpoint's tiers.
    """
    problems = []
    model_id = MODEL_IDS.get(key)
    if model_id is None:
        return None, [f"unknown model key {key!r}"]
    catalog_entry = catalog_entry or {}
    if catalog_entry.get("id") != model_id:
        problems.append(f"catalog entry id {catalog_entry.get('id')!r} is not {model_id}")
    standard, _ = matching_endpoints(endpoints)
    primary = [e for e in standard if e.get("tag") == PROVIDER_TAG]
    if len(primary) != 1:
        problems.append(f"expected exactly one standard {PROVIDER_TAG!r} endpoint, found {len(primary)}")
        return None, problems
    endpoint = primary[0]
    if endpoint.get("provider_name") != PROVIDER_NAME:
        problems.append(f"endpoint provider {endpoint.get('provider_name')!r} is not {PROVIDER_NAME}")
    if endpoint.get("status") != 0:
        problems.append(f"Anthropic endpoint status {endpoint.get('status')!r} (0 = available)")
    supported = tuple(endpoint.get("supported_parameters") or ())
    missing = [p for p in REQUIRED_PARAMETERS if p not in supported]
    if missing:
        problems.append(f"Anthropic endpoint lacks required parameters {missing}")
    if EFFORT_PARAMETER not in supported:
        problems.append(f"Anthropic endpoint does not list {EFFORT_PARAMETER!r} (effort control)")
    reasoning = catalog_entry.get("reasoning") or {}
    efforts = tuple(reasoning.get("supported_efforts") or ())
    if EFFORTS[key] not in efforts:
        problems.append(f"effort {EFFORTS[key]!r} not among supported efforts {list(efforts)}")
    context = endpoint.get("context_length")
    max_out = endpoint.get("max_completion_tokens")
    if not isinstance(context, int) or context <= 0:
        problems.append("endpoint context_length missing")
    if not isinstance(max_out, int) or max_out < max_tokens_ceiling:
        problems.append(f"endpoint max_completion_tokens {max_out!r} below the judge ceiling {max_tokens_ceiling}")
    tiers = []
    for item in standard:
        pricing = item.get("pricing") or {}
        if Decimal(str(pricing.get("discount") or 0)) < 0:
            problems.append(f"{item.get('tag')} pricing has a negative discount (surcharge)")
        for entry in _entries(pricing):
            if Decimal(str(entry.get("request") or 0)) != 0:
                problems.append(f"{item.get('tag')} charges a per-request fee the reservation does not cover")
            prompt = _price(entry.get("prompt"), "prompt", problems, required=True)
            completion = _price(entry.get("completion"), "completion", problems, required=True)
            reasoning_price = _price(entry.get("internal_reasoning"), "internal_reasoning", problems)
            if reasoning_price is not None and completion is not None and Decimal(reasoning_price) > Decimal(completion):
                problems.append(f"{item.get('tag')} internal_reasoning price exceeds the completion price")
            minimum = entry.get("min_prompt_tokens", 0) if entry is not pricing else 0
            if not isinstance(minimum, int) or minimum < 0:
                problems.append(f"{item.get('tag')} override has an invalid min_prompt_tokens {minimum!r}")
                continue
            if prompt is None or completion is None:
                continue
            tiers.append(PriceTier(minimum, prompt, completion,
                                   _price(entry.get("input_cache_read"), "input_cache_read", problems),
                                   _price(entry.get("input_cache_write"), "input_cache_write", problems),
                                   _price(entry.get("input_cache_write_1h"), "input_cache_write_1h", problems),
                                   str(item.get("tag"))))
    if not any(t.min_prompt_tokens == 0 for t in tiers):
        problems.append("no base price tier")
    if CACHE_SYSTEM_DEFAULTS[key] and any(t.cache_read is None or t.cache_write is None for t in tiers):
        problems.append(f"{key} caches its system prompt by default but a tier lacks cache read/write prices")
    if problems:
        return None, problems
    route = ModelRoute(key, model_id, catalog_entry.get("canonical_slug"), EFFORTS[key], context, max_out,
                       tuple(sorted(tiers, key=lambda t: (t.min_prompt_tokens, t.endpoint_tag))),
                       reasoning.get("mandatory"), efforts, tuple(sorted(supported)))
    return route, []


def summarize_route(route: ModelRoute):
    """Derived maximum prices for the freeze record (strings, USD per token)."""
    def top(name):
        values = [Decimal(getattr(t, name)) for t in route.tiers if getattr(t, name) is not None]
        return str(max(values)) if values else None
    return {"max_prompt_usd_per_token": top("prompt"), "max_completion_usd_per_token": top("completion"),
            "max_cache_read_usd_per_token": top("cache_read"), "max_cache_write_usd_per_token": top("cache_write"),
            "max_cache_write_1h_usd_per_token": top("cache_write_1h"),
            "tier_thresholds": sorted({t.min_prompt_tokens for t in route.tiers}),
            "supports_prompt_cache": route.supports_prompt_cache,
            "cache_system_default": CACHE_SYSTEM_DEFAULTS[route.key] and route.supports_prompt_cache}
