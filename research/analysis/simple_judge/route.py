"""Frozen non-Anthropic routes and the budgeted client of the simplified behavior judge.

Anthropic models refused about 800 judge requests on GLM transcripts, so this judge uses a non-Anthropic model
with a refusal fallback to a second provider (the pattern of the incident report: a refused unit is never
resent to the refusing model; it goes once to the fallback model and is flagged).

* ``CANDIDATES``: model routes compared on a 10-agent sample of glm100-repeat2 (2026-10-24; README.md,
  "Model choice"). ``DEFAULT_MODEL`` and ``DEFAULT_FALLBACK`` record the choice.
* ``freeze``: fetches OpenRouter's PUBLIC catalog (no credential, unpaid) and the endpoints of the chosen
  models, validates the pinned standard endpoint of each (exact model id, provider, available, ``max_tokens``,
  ``reasoning``, ``reasoning_effort`` with the frozen effort, ``response_format``, no per-request fee, no
  surcharge) and writes ``simple-judge-route-<UTC>.json``. A paid run refuses a record older than 72 h and
  refreshes it automatically.
* ``SimpleClient``: the behavior judge's ``JudgeClient`` (ledger reservations, the per-run ``RunLimit``,
  request and response archives before parsing, settlement by OpenRouter's reported cost, generation
  lookup, refusal classification) bound to one of these routes, pinned to its provider with no router
  fallbacks.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from research.analysis.judge.client import JudgeClient
from research.analysis.judge.route import (_OBSERVED, CATALOG_URL, ENDPOINTS_URL, FALLBACK_KEY, MODEL_KEYS, ModelRoute,
                                           PriceTier, JudgeRouteSettings, STANDARD_SERVICE_TIERS, _entries, _price,
                                           matching_endpoints)

RECORD_PREFIX = "simple-judge-route"
RECORD_SCHEMA = f"{RECORD_PREFIX}/v1"
MAX_AGE_HOURS = 72
MAX_TOKENS = 12000          # reasoning + the JSON reply
MAX_TOKENS_CEILING = 32000
CONCURRENCY = 16
USER_AGENT = "gdm-math-swarm-simple-judge-route-freeze"


@dataclass(frozen=True)
class RouteSpec:
    model_id: str
    provider_tag: str
    provider_name: str
    effort: str


CANDIDATES = {
    "gpt-5.6-sol-low": RouteSpec("openai/gpt-5.6-sol", "openai", "OpenAI", "low"),
    "gpt-5.6-terra-medium": RouteSpec("openai/gpt-5.6-terra", "openai", "OpenAI", "medium"),
    "gemini-3.8-flash-medium": RouteSpec("google/gemini-3.8-flash", "google-ai-studio", "Google AI Studio", "medium"),
}
# Chosen 2026-10-24 on the 10-agent repeat2 sample (README.md, "Model choice").
DEFAULT_MODEL = "gemini-3.8-flash-medium"
DEFAULT_FALLBACK = "gpt-5.6-terra-medium"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})   # no Authorization header
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def build_simple_route(key, spec, catalog_entry, endpoints):
    """(ModelRoute | None, problems) for one candidate from its catalog entry and endpoints."""
    problems = []
    catalog_entry = catalog_entry or {}
    if catalog_entry.get("id") != spec.model_id:
        problems.append(f"catalog entry id {catalog_entry.get('id')!r} is not {spec.model_id}")
    standard, _ = matching_endpoints(endpoints, spec.provider_tag)
    primary = [e for e in standard if e.get("tag") == spec.provider_tag]
    if len(primary) != 1:
        return None, problems + [f"expected one standard {spec.provider_tag!r} endpoint, found {len(primary)}"]
    endpoint = primary[0]
    if endpoint.get("provider_name") != spec.provider_name:
        problems.append(f"endpoint provider {endpoint.get('provider_name')!r} is not {spec.provider_name}")
    if endpoint.get("status") != 0:
        problems.append(f"endpoint status {endpoint.get('status')!r} (0 = available)")
    supported = tuple(endpoint.get("supported_parameters") or ())
    for parameter in ("max_tokens", "reasoning", "reasoning_effort", "response_format"):
        if parameter not in supported:
            problems.append(f"endpoint lacks {parameter}")
    efforts = tuple((catalog_entry.get("reasoning") or {}).get("supported_efforts") or ())
    if spec.effort not in efforts:
        problems.append(f"effort {spec.effort!r} not among {list(efforts)}")
    context, max_out = endpoint.get("context_length"), endpoint.get("max_completion_tokens")
    if not isinstance(context, int) or context <= 0:
        problems.append("endpoint context_length missing")
    if not isinstance(max_out, int) or max_out < MAX_TOKENS_CEILING:
        problems.append(f"endpoint max_completion_tokens {max_out!r} below {MAX_TOKENS_CEILING}")
    tiers = []
    pricing = endpoint.get("pricing") or {}
    if Decimal(str(pricing.get("discount") or 0)) < 0:
        problems.append("negative discount (surcharge)")
    for entry in _entries(pricing):
        if Decimal(str(entry.get("request") or 0)) != 0:
            problems.append("per-request fee")
        prompt = _price(entry.get("prompt"), "prompt", problems, required=True)
        completion = _price(entry.get("completion"), "completion", problems, required=True)
        minimum = entry.get("min_prompt_tokens", 0) if entry is not pricing else 0
        if prompt is None or completion is None or not isinstance(minimum, int):
            continue
        tiers.append(PriceTier(minimum, prompt, completion, _price(entry.get("input_cache_read"), "cache_read", problems),
                               None, None, spec.provider_tag))
    if not any(t.min_prompt_tokens == 0 for t in tiers):
        problems.append("no base price tier")
    if problems:
        return None, problems
    route = ModelRoute(key, spec.model_id, catalog_entry.get("canonical_slug"), spec.effort, context, max_out,
                       tuple(sorted(tiers, key=lambda t: t.min_prompt_tokens)),
                       (catalog_entry.get("reasoning") or {}).get("mandatory"), efforts, tuple(sorted(supported)))
    return route, []


def freeze(output_dir, keys, *, fetcher=fetch, now=None):
    """Write a route record for ``keys`` (unpaid public catalog); returns (path, record)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    catalog_raw = fetcher(CATALOG_URL)
    catalog = json.loads(catalog_raw)
    models = {}
    for key in keys:
        spec = CANDIDATES[key]
        url = ENDPOINTS_URL.format(model=spec.model_id)
        raw = fetcher(url)
        endpoints = ((json.loads(raw).get("data") or {}).get("endpoints")) or []
        entries = [m for m in catalog.get("data", []) if m.get("id") == spec.model_id]
        entry = entries[0] if len(entries) == 1 else None
        catalog_entry = None if entry is None else {k: entry.get(k) for k in (
            "id", "canonical_slug", "name", "context_length", "pricing", "supported_parameters", "reasoning")}
        standard, _ = matching_endpoints(endpoints, spec.provider_tag)
        route, problems = build_simple_route(key, spec, catalog_entry, standard)
        models[key] = {"spec": spec.__dict__, "catalog_entry": catalog_entry, "endpoints": standard,
                       "endpoints_sha256": hashlib.sha256(raw).hexdigest(), "problems": problems,
                       "valid": not problems,
                       "price_tiers": None if route is None else [t.__dict__ for t in route.tiers]}
    record = {"schema": RECORD_SCHEMA, "created_at": now.isoformat(), "credential_used": False, "paid_calls": 0,
              "catalog_sha256": hashlib.sha256(catalog_raw).hexdigest(), "models": models,
              "valid": all(m["valid"] for m in models.values())}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{RECORD_PREFIX}-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return path, record


@dataclass(frozen=True)
class SimpleRouteSettings(JudgeRouteSettings):
    """Judge route settings holding the simplified judge's own routes (``simple_routes``: ((key, ModelRoute,
    provider tag, provider name), ...)). Requests are pinned to the route's provider, never cached explicitly."""
    simple_routes: tuple = ()

    @classmethod
    def from_record(cls, path, keys, *, now=None, allow_stale=False):
        path = Path(path)
        raw = path.read_bytes()
        record = json.loads(raw)
        if record.get("schema") != RECORD_SCHEMA or record.get("credential_used") is not False:
            raise ValueError(f"{path} is not a {RECORD_SCHEMA} record")
        created = datetime.datetime.fromisoformat(record["created_at"])
        now = now or datetime.datetime.now(datetime.timezone.utc)
        if not allow_stale and now - created > datetime.timedelta(hours=MAX_AGE_HOURS):
            raise ValueError(f"route record {path.name} is older than {MAX_AGE_HOURS} h")
        routes = []
        for key in keys:
            entry = (record.get("models") or {}).get(key)
            if not entry:
                raise ValueError(f"route record {path.name} lacks {key}")
            spec = CANDIDATES[key]
            route, problems = build_simple_route(key, spec, entry.get("catalog_entry"), entry.get("endpoints"))
            if problems:
                raise ValueError(f"route {key} does not validate: " + "; ".join(problems))
            routes.append((key, route, spec.provider_tag, spec.provider_name))
        return cls(**{k: _OBSERVED[k] for k in MODEL_KEYS}, simple_routes=tuple(routes), pricing_verified=True,
                   pricing_record=str(path), pricing_record_sha256=hashlib.sha256(raw).hexdigest(), prompt_cache=False,
                   max_tokens_ceiling=MAX_TOKENS_CEILING)

    @classmethod
    def offline(cls, routes):
        """Unpaid tests: ``routes`` = [(key, ModelRoute, tag, provider name)]."""
        return cls(**{k: _OBSERVED[k] for k in MODEL_KEYS}, simple_routes=tuple(routes), pricing_verified=True,
                   prompt_cache=False, max_tokens_ceiling=MAX_TOKENS_CEILING)

    def _entry(self, key):
        for entry in self.simple_routes:
            if entry[0] == key:
                return entry
        if key == FALLBACK_KEY and self.simple_routes:
            return self.simple_routes[0]
        return None

    def route(self, key):
        entry = self._entry(key)
        return entry[1] if entry else super().route(key)

    def provider_preferences(self, key=None):
        entry = self._entry(key)
        if entry is None:
            return super().provider_preferences(key)
        return {"order": [entry[2]], "only": [entry[2]], "allow_fallbacks": False, "require_parameters": True}

    def provider_name_of(self, key):
        entry = self._entry(key)
        return entry[3] if entry else None

    def cache_system_default(self, key):
        return False


class SimpleClient(JudgeClient):
    """``JudgeClient`` bound to one simplified-judge route (``key`` in ``settings.simple_routes``)."""

    def __init__(self, key, ledger_path, *, settings, max_concurrency=CONCURRENCY, **kwargs):
        if settings._entry(key) is None or key == FALLBACK_KEY:
            raise ValueError(f"unknown simplified-judge route {key!r}")
        super().__init__(FALLBACK_KEY, ledger_path, settings=settings, max_concurrency=max_concurrency, **kwargs)
        self.model_key = key
        self.route = settings.route(key)

    def _route_mismatch(self, payload):
        problems = {}
        provider = payload.get("provider")
        expected = self.settings.provider_name_of(self.model_key)
        if provider is not None and str(provider).strip().lower() != str(expected).lower():
            problems["provider"] = provider
        model = payload.get("model")
        accepted = {self.route.model_id, self.route.canonical_slug}
        if model is not None and model not in accepted and not str(model).startswith(self.route.model_id + "-"):
            problems["model"] = model
        if payload.get("service_tier") not in STANDARD_SERVICE_TIERS:
            problems["service_tier"] = payload.get("service_tier")
        return problems


def offline_route(key, prompt="0.000002", completion="0.000012"):
    """A route for unpaid tests and ``--mock`` (never admitted on the paid path: no pricing record)."""
    spec = CANDIDATES[key]
    route = ModelRoute(key, spec.model_id, None, spec.effort, 1000000, 128000, (PriceTier(0, prompt, completion,
                                                                                          None, None, None,
                                                                                          spec.provider_tag),),
                       None, ("high", "medium", "low"),
                       ("max_tokens", "reasoning", "reasoning_effort", "response_format", "structured_outputs"))
    return (key, route, spec.provider_tag, spec.provider_name)
