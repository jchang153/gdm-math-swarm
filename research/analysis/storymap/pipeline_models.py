"""Story map: model routes and budgeted clients (researcher-only).

Every request goes through the behaviour judge's ``JudgeClient`` (research/analysis/judge/client.py): the ledger
reservation, the invocation's one ``RunLimit``, request and response archives before parsing, settlement by
OpenRouter's reported cost, refusal classification and the provider pin with no router fallbacks.

* Anthropic models (``anthropic/claude-haiku-5.5``, ``-sonnet-5.5``, ``-opus-5.5``) use the judge's own routes
  and frozen efforts (haiku low, sonnet/opus high) from the judge route pricing record
  (scripts/freeze_judge_route.py, refreshed automatically and unpaid by ``run_transcript_judge.refresh_pricing``),
  with prompt caching on for every model (``JudgeRouteSettings.with_prompt_cache(True)``): the system prompt, the
  fixed story-map prefix, is one text block with a 5-minute ``cache_control`` breakpoint.
* Other models (``EXTRA_SPECS``: the cross-check and stage-1 bake-off models) use the simplified judge's
  ``SimpleClient`` pattern (research/analysis/simple_judge/route.py) with this module's own route record
  ``data/validation/storymap-route-<UTC>.json``, frozen from OpenRouter's PUBLIC catalog (no credential, unpaid)
  and refreshed when older than 72 h. They cache implicitly (OpenAI/Google), so no request is marked.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from research.analysis.judge.client import JudgeClient
from research.analysis.judge.route import (_OBSERVED, CATALOG_URL, ENDPOINTS_URL, MODEL_IDS, MODEL_KEYS,
                                           JudgeRouteSettings, ModelRoute, PriceTier, matching_endpoints)
from research.analysis.simple_judge.route import (MAX_TOKENS_CEILING as EXTRA_MAX_TOKENS_CEILING, RouteSpec,
                                                  SimpleClient, SimpleRouteSettings, build_simple_route, fetch)

ANTHROPIC_KEYS = {model_id: key for key, model_id in MODEL_IDS.items()}   # model id -> judge route key
EXTRA_SPECS = {
    "openai/gpt-5.6-terra": RouteSpec("openai/gpt-5.6-terra", "openai", "OpenAI", "medium"),
    "openai/gpt-5.6-luna": RouteSpec("openai/gpt-5.6-luna", "openai", "OpenAI", "medium"),
    "openai/gpt-5.6-sol": RouteSpec("openai/gpt-5.6-sol", "openai", "OpenAI", "medium"),
    "google/gemini-3.8-flash": RouteSpec("google/gemini-3.8-flash", "google-ai-studio", "Google AI Studio", "medium"),
}
KNOWN_MODELS = tuple(sorted(set(ANTHROPIC_KEYS) | set(EXTRA_SPECS)))
PROVIDER_NAMES = {"anthropic": "Anthropic", **{s.provider_tag: s.provider_name for s in EXTRA_SPECS.values()}}
EXTRA_CONCURRENCY = 16
RECORD_PREFIX = "storymap-route"
RECORD_SCHEMA = f"{RECORD_PREFIX}/v1"
MAX_AGE_HOURS = 72


def slug(model_id):
    """``anthropic/claude-haiku-5.5`` -> ``claude-haiku-5.5`` (folder and route-key names)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(model_id).split("/", 1)[-1])


def check_model(model_id):
    if model_id not in ANTHROPIC_KEYS and model_id not in EXTRA_SPECS:
        raise ValueError(f"unknown story-map model {model_id!r}; expected one of {', '.join(KNOWN_MODELS)}")
    return model_id


# ---------------------------------------------------------------------------
# Non-Anthropic routes

@dataclass(frozen=True)
class StorymapRouteSettings(SimpleRouteSettings):
    """``SimpleRouteSettings`` holding the story map's non-Anthropic routes (keys are model slugs)."""

    @classmethod
    def from_record(cls, path, keys, *, now=None, allow_stale=False):
        """Routes for ``keys`` (model ids) from a story-map route record, re-validated offline."""
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
        for model_id in keys:
            entry = (record.get("models") or {}).get(model_id)
            if not entry:
                raise ValueError(f"route record {path.name} lacks {model_id}")
            spec = EXTRA_SPECS[model_id]
            route, problems = build_simple_route(slug(model_id), spec, entry.get("catalog_entry"),
                                                 entry.get("endpoints"))
            if problems:
                raise ValueError(f"route {model_id} does not validate: " + "; ".join(problems))
            routes.append((slug(model_id), route, spec.provider_tag, spec.provider_name))
        return cls(**{k: _OBSERVED[k] for k in MODEL_KEYS}, simple_routes=tuple(routes), pricing_verified=True,
                   pricing_record=str(path), pricing_record_sha256=hashlib.sha256(raw).hexdigest(), prompt_cache=False,
                   max_tokens_ceiling=EXTRA_MAX_TOKENS_CEILING)


def offline_extra_route(model_id, prompt="0.000002", completion="0.000012"):
    """An unpaid route for tests and --mock (never admitted on the paid path: no route record)."""
    spec = EXTRA_SPECS[model_id]
    route = ModelRoute(slug(model_id), spec.model_id, None, spec.effort, 1000000, 128000,
                       (PriceTier(0, prompt, completion, None, None, None, spec.provider_tag),), None,
                       ("high", "medium", "low"),
                       ("max_tokens", "reasoning", "reasoning_effort", "response_format", "structured_outputs"))
    return (slug(model_id), route, spec.provider_tag, spec.provider_name)


def freeze_extra_routes(output_dir, model_ids, *, fetcher=fetch, now=None):
    """Write a story-map route record for ``model_ids`` from the PUBLIC catalog (no credential, unpaid)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    catalog_raw = fetcher(CATALOG_URL)
    catalog = json.loads(catalog_raw)
    models = {}
    for model_id in model_ids:
        spec = EXTRA_SPECS[model_id]
        raw = fetcher(ENDPOINTS_URL.format(model=spec.model_id))
        endpoints = ((json.loads(raw).get("data") or {}).get("endpoints")) or []
        entries = [m for m in catalog.get("data", []) if m.get("id") == spec.model_id]
        entry = entries[0] if len(entries) == 1 else None
        catalog_entry = None if entry is None else {k: entry.get(k) for k in (
            "id", "canonical_slug", "name", "context_length", "pricing", "supported_parameters", "reasoning")}
        standard, _ = matching_endpoints(endpoints, spec.provider_tag)
        route, problems = build_simple_route(slug(model_id), spec, catalog_entry, standard)
        models[model_id] = {"spec": spec.__dict__, "catalog_entry": catalog_entry, "endpoints": standard,
                            "endpoints_sha256": hashlib.sha256(raw).hexdigest(), "problems": problems,
                            "valid": not problems}
    record = {"schema": RECORD_SCHEMA, "created_at": now.isoformat(), "credential_used": False, "paid_calls": 0,
              "catalog_sha256": hashlib.sha256(catalog_raw).hexdigest(), "models": models,
              "valid": all(m["valid"] for m in models.values())}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{RECORD_PREFIX}-{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return path, record


def extra_route_settings(model_ids, *, route_dir, record=None, log=None, freezer=freeze_extra_routes):
    """Paid path: settings for the non-Anthropic ``model_ids`` from ``record``, else the newest usable record in
    ``route_dir``, else a fresh one (unpaid public catalog). None when no extra model is needed."""
    log = log or (lambda message: None)
    model_ids = sorted(set(model_ids))
    if not model_ids:
        return None
    if record:
        return StorymapRouteSettings.from_record(record, model_ids)
    for path in sorted(Path(route_dir).glob(f"{RECORD_PREFIX}-*.json"), reverse=True)[:1]:
        try:
            return StorymapRouteSettings.from_record(path, model_ids)
        except (ValueError, KeyError) as error:
            log(f"route record {path.name} not usable ({error}); refreshing (unpaid public catalog)")
    path, frozen = freezer(route_dir, sorted(EXTRA_SPECS))
    log(f"route record written: {path} (valid: {frozen['valid']})")
    return StorymapRouteSettings.from_record(path, model_ids)


# ---------------------------------------------------------------------------
# Clients

class ModelPool:
    """One budgeted client per model id, built on first use and shared by every stage (one semaphore and one
    ledger run per model; every client shares the invocation's ``RunLimit``).

    ``anthropic_settings``: ``JudgeRouteSettings`` (prompt caching on); ``extra_settings``:
    ``StorymapRouteSettings`` or None; ``common``: keyword arguments of every client (ledger path, scope,
    run limit, ledger label run id, transport or the paid switches); ``archive_root``: raw archives go to
    ``archive_root/<model slug>/``."""

    def __init__(self, *, ledger_path, archive_root, anthropic_settings, extra_settings=None, concurrency=None,
                 **common):
        self.ledger_path = Path(ledger_path)
        self.archive_root = Path(archive_root)
        self.anthropic_settings = anthropic_settings
        self.extra_settings = extra_settings
        self.concurrency = dict(concurrency or {})
        self.common = common
        self.clients = {}

    def get(self, model_id):
        check_model(model_id)
        client = self.clients.get(model_id)
        if client is not None:
            return client
        archive = self.archive_root / slug(model_id)
        limit = self.concurrency.get(model_id)
        if model_id in ANTHROPIC_KEYS:
            client = JudgeClient(ANTHROPIC_KEYS[model_id], self.ledger_path, archive_dir=archive,
                                 settings=self.anthropic_settings, max_concurrency=limit, **self.common)
        else:
            if self.extra_settings is None or self.extra_settings._entry(slug(model_id)) is None:
                raise ValueError(f"no route for {model_id} in the story-map route settings")
            client = SimpleClient(slug(model_id), self.ledger_path, archive_dir=archive, settings=self.extra_settings,
                                  max_concurrency=limit or EXTRA_CONCURRENCY, **self.common)
        self.clients[model_id] = client
        return client

    def prepare(self, model_ids):
        for model_id in dict.fromkeys(model_ids):
            self.get(model_id).prepare()

    def describe(self, model_id):
        """Route facts recorded in pipeline.json for one model."""
        if model_id in ANTHROPIC_KEYS:
            settings, key = self.anthropic_settings, ANTHROPIC_KEYS[model_id]
            route = settings.route(key)
            cache = settings.cache_system_default(key)
        else:
            settings, key = self.extra_settings, slug(model_id)
            route = settings.route(key) if settings is not None and settings._entry(key) else None
            cache = False
        return {"model_id": model_id, "route_key": key, "effort": None if route is None else route.effort,
                "canonical_slug": None if route is None else route.canonical_slug,
                "provider": None if settings is None else settings.provider_preferences(key),
                "system_prompt_cached": cache,
                "pricing_record": None if settings is None else settings.pricing_record,
                "pricing_record_sha256": None if settings is None else settings.pricing_record_sha256,
                "settings_sha256": None if settings is None else settings.sha256()}


def offline_settings(model_ids):
    """(anthropic settings, extra settings) for tests and --mock (observed prices; no pricing record)."""
    anthropic = JudgeRouteSettings.defaults(pricing_verified=True, prompt_cache=True)
    extras = [m for m in dict.fromkeys(model_ids) if m in EXTRA_SPECS]
    extra = StorymapRouteSettings.offline([offline_extra_route(m) for m in extras]) if extras else None
    return anthropic, extra
