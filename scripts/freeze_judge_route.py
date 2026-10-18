#!/usr/bin/env python3
"""Freeze the transcript judge's OpenRouter route and prices. Unpaid; no credential is read.

Researcher-only (docs/plan.md "Transcript behavior judge specified, October 17, 2026",
as superseded by "downsized model allocation"; rubric Section 11). Fetches the PUBLIC,
unauthenticated catalog (GET /api/v1/models and /api/v1/models/<id>/endpoints for
Claude Haiku 5.5, Claude Sonnet 5.5 and Claude Opus 5.5), validates the pinned
Anthropic endpoint of each with research.analysis.judge.route.build_route (model ids,
availability, required parameters, effort control and the frozen effort, per-request
fees, discounts, internal-reasoning prices, output ceiling, and cache read/write prices
for the models that cache their system prompt by default), and writes the I6 evidence
record data/validation/judge-route-pricing-<UTC>.json. The record embeds each model's
catalog entry and every endpoint the base slug ``anthropic`` could name (standard and
service-tier), so JudgeRouteSettings.from_pricing_record re-validates it offline and
derives prices from it. Prices include each tier (Haiku 5.5 bills prompts of at least
100,000 tokens at higher prompt and completion prices) and the cache read/write prices.

    .venv/bin/python scripts/freeze_judge_route.py [--output-dir data/validation]

Exit 0 when every requirement passed, 1 otherwise (the record is written either way).
"""
import argparse
import datetime
import hashlib
import json
import sys
import urllib.request
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from research.analysis.judge.route import (ALLOCATION_DECISION, CACHE_CONTROL, CACHE_SYSTEM_DEFAULTS,  # noqa: E402
                                           CACHE_TTL, CATALOG_URL, CHAT_COMPLETIONS_URL, DEFAULT_CONCURRENCY,
                                           EFFORT_PARAMETER, EFFORTS, ENDPOINTS_URL, MAX_TOKENS_CEILING,
                                           MIN_CACHEABLE_PROMPT_TOKENS, MODEL_ALLOCATION, MODEL_IDS, PLAN_SECTION,
                                           PRICING_PREFIX, PROVIDER_TAG, JudgeRouteSettings, build_route,
                                           matching_endpoints, summarize_route)
from research_swarm.launch_gates import write_evidence  # noqa: E402

USER_AGENT = "gdm-math-swarm-judge-route-freeze"
# Documentation consulted on 2026-10-17 (paraphrased; re-check when the route changes).
DOCS = [
    {"url": "https://openrouter.ai/docs/cookbook/evaluate-and-optimize/model-migrations/claude-5-5.md",
     "facts": ["Chat Completions reasoning.effort maps to Anthropic output_config.effort with adaptive thinking "
               "(Opus 5.5, Sonnet 5.5 and Haiku 5.5)",
               "thinking budgets (reasoning.max_tokens) are not forwarded for Claude 5.5 models",
               "Opus 5.5 and Sonnet 5.5 reasoning is mandatory (effort none / enabled false return 400); "
               "Haiku 5.5 defaults to adaptive thinking at medium and can disable thinking",
               "Sonnet 5.5 with no reasoning setting runs adaptive thinking at high; OpenRouter requests "
               "summarized thinking display",
               "thinking and the reply share max_tokens",
               "non-default temperature/top_p/top_k and assistant prefill are rejected",
               "Haiku 5.5 pricing is tiered by prompt length (over 100K tokens costs more per token)",
               "a declined request returns stop_reason refusal (Sonnet 5.5 adds a stop_details category)",
               "the minimum cacheable prompt for the 5.5 models is 512 tokens"]},
    {"url": "https://openrouter.ai/docs/guides/routing/provider-selection.md",
     "facts": ["order/only/allow_fallbacks/require_parameters provider preferences",
               "a base provider slug matches all of that provider's eligible endpoints (variants, regions)",
               "a base slug does not opt into a service-tier endpoint such as <provider>/fast",
               "require_parameters=true routes only to providers supporting every LLM parameter in the request "
               "(the parameters reference lists no cache_control parameter)"]},
    {"url": "https://openrouter.ai/docs/guides/features/service-tiers.md",
     "facts": ["requests use the standard tier unless another is requested",
               "the response reports the service_tier that served the request; billing follows that endpoint"]},
    {"url": "https://openrouter.ai/docs/api_reference/errors-and-debugging.md",
     "facts": ["a completed model refusal returns message.refusal with finish_reason content_filter",
               "a refusal reported as an error is a 403 with error.metadata.error_type refusal "
               "(or content_policy_violation for filters)",
               "after a provider accepts the request the HTTP status is 200 and later errors arrive in the body"]},
    {"url": "https://openrouter.ai/docs/guides/best-practices/prompt-caching.md",
     "facts": ["Anthropic caching is opt-in via cache_control: top-level automatic caching, or explicit per-block "
               "breakpoints (at most four) on content blocks, including a system message text block",
               "cache_control {type: ephemeral} is the 5-minute TTL; {type: ephemeral, ttl: 1h} is the 1-hour TTL",
               "cache writes bill 1.25x the prompt price (5-minute TTL) or 2x (1-hour TTL); reads 0.1x",
               "minimum cacheable prompt: 512 tokens for Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5",
               "usage.prompt_tokens_details.cached_tokens counts tokens read from the cache and cache_write_tokens "
               "counts tokens written",
               "sticky provider routing is not used when provider.order is set (the pinned order takes priority)"]},
    {"url": "https://openrouter.ai/docs/cookbook/administration/usage-accounting.md",
     "facts": ["every response includes usage with prompt/completion/reasoning tokens, cost and cost_details",
               "prompt_tokens_details.cached_tokens (read) and cache_write_tokens (written, models with explicit "
               "caching and cache-write pricing)",
               "usage.cost is the total charged to the account"]},
    {"url": "https://openrouter.ai/docs/api_reference/parameters.md",
     "facts": ["reasoning and reasoning_effort are listed request parameters; cache_control is not"]},
    {"url": "https://openrouter.ai/docs/guides/best-practices/reasoning-tokens.md",
     "facts": ["reasoning: {effort, exclude}; reasoning tokens count toward max_tokens and bill as output"]},
]


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # no Authorization header
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def model_record(key, catalog, endpoints_raw, endpoints_url):
    model_id = MODEL_IDS[key]
    entries = [m for m in catalog.get("data", []) if m.get("id") == model_id]
    entry = entries[0] if len(entries) == 1 else None
    endpoints_payload = json.loads(endpoints_raw)
    endpoints = (endpoints_payload.get("data") or {}).get("endpoints") or []
    standard, tiered = matching_endpoints(endpoints)
    catalog_entry = None if entry is None else {k: entry.get(k) for k in (
        "id", "canonical_slug", "name", "context_length", "pricing", "top_provider", "supported_parameters",
        "reasoning", "architecture", "expiration_date")}
    route, problems = build_route(key, catalog_entry, standard + tiered)
    if entry is None:
        problems = [f"catalog lists {len(entries)} entries for {model_id}", *problems]
    primary = next((e for e in standard if e.get("tag") == PROVIDER_TAG), {})
    supported = primary.get("supported_parameters") or []
    primary_pricing = primary.get("pricing") or {}
    reasoning = (catalog_entry or {}).get("reasoning") or {}
    record = {
        "model_id": model_id, "effort": EFFORTS[key], "catalog_entry": catalog_entry,
        "endpoints": standard + tiered,
        "endpoints_source": {"url": endpoints_url, "sha256": hashlib.sha256(endpoints_raw).hexdigest(),
                             "bytes": len(endpoints_raw), "all_tags": [e.get("tag") for e in endpoints]},
        "eligible_standard_tags": [e.get("tag") for e in standard],
        "excluded_service_tier_endpoints": [{"tag": e.get("tag"), "service_tier": e.get("service_tier"),
                                             "pricing": e.get("pricing")} for e in tiered],
        "supported": {
            "reasoning": "reasoning" in supported, "reasoning_effort": EFFORT_PARAMETER in supported,
            "reasoning_mandatory": reasoning.get("mandatory"),
            "supported_efforts": reasoning.get("supported_efforts"),
            "default_effort": reasoning.get("default_effort"),
            "frozen_effort_supported": EFFORTS[key] in (reasoning.get("supported_efforts") or []),
            "response_format": "response_format" in supported,
            "structured_outputs": "structured_outputs" in supported,
            "temperature": "temperature" in supported,
            "max_tokens": "max_tokens" in supported,
            "max_completion_tokens": primary.get("max_completion_tokens"),
            "context_length": primary.get("context_length"),
            "prompt_caching": {
                "mechanism": "explicit per-block cache_control on the system message text block (docs)",
                "cache_control": CACHE_CONTROL, "ttl": CACHE_TTL,
                "endpoint_lists_cache_read_price": primary_pricing.get("input_cache_read") is not None,
                "endpoint_lists_cache_write_price": primary_pricing.get("input_cache_write") is not None,
                "supports_implicit_caching": primary.get("supports_implicit_caching"),
                "min_cacheable_prompt_tokens": MIN_CACHEABLE_PROMPT_TOKENS,
                "cache_system_default": CACHE_SYSTEM_DEFAULTS[key],
                "usage_fields": ["prompt_tokens_details.cached_tokens", "prompt_tokens_details.cache_write_tokens"],
            },
            "is_moderated": ((catalog_entry or {}).get("top_provider") or {}).get("is_moderated"),
        },
        "problems": problems,
        "valid": not problems,
    }
    if route is not None:
        record["price_tiers"] = [asdict(t) for t in route.tiers]
        record.update(summarize_route(route))
    return record, route


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "validation")
    args = parser.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc)
    catalog_raw = fetch(CATALOG_URL)
    catalog = json.loads(catalog_raw)
    models, routes = {}, {}
    for key, model_id in MODEL_IDS.items():
        url = ENDPOINTS_URL.format(model=model_id)
        models[key], routes[key] = model_record(key, catalog, fetch(url), url)
    valid = all(m["valid"] for m in models.values())
    record = {
        "kind": "public_judge_route_pricing_freeze", "retrieved_utc": now.isoformat(), "plan_section": PLAN_SECTION,
        "allocation_decision": ALLOCATION_DECISION, "credential_used": False, "paid_calls": 0,
        "provider_tag": PROVIDER_TAG,
        "catalog_source": {"url": CATALOG_URL, "sha256": hashlib.sha256(catalog_raw).hexdigest(),
                           "bytes": len(catalog_raw)},
        "request_endpoint": CHAT_COMPLETIONS_URL, "max_tokens_ceiling": MAX_TOKENS_CEILING,
        "model_allocation": {stage: list(keys) for stage, keys in MODEL_ALLOCATION.items()},
        "default_concurrency": dict(DEFAULT_CONCURRENCY),
        "docs": DOCS, "models": models, "valid": valid,
    }
    if valid:
        settings = JudgeRouteSettings(**routes)
        record["provider_preferences"] = settings.provider_preferences()
        record["reasoning"] = {key: settings.reasoning(key, EFFORTS[key]) for key in MODEL_IDS}
        record["cache_system_defaults"] = {key: settings.cache_system_default(key) for key in MODEL_IDS}
        record["settings"] = settings.describe()
        record["require_parameters_rationale"] = (
            "true: the Anthropic endpoint lists every parameter the judge sends (max_tokens, reasoning and, "
            "when requested, response_format), so the router fails a request it cannot honor instead of "
            "silently dropping a setting; per-block cache_control is message content, not a request "
            "parameter; sampling parameters are never sent")
        record["reservation_rule"] = (
            "prompt bound = UTF-8 bytes of the serialized request + framing allowance (capped at the context), "
            "priced at the highest prompt price among reachable tiers (for a request whose system prompt is "
            "marked for caching, the highest of the prompt, cache-write and cache-read prices), plus max_tokens "
            "at the highest reachable completion price; no cache discount is ever assumed")
    record["all_required_passed"] = valid
    path = write_evidence(args.output_dir, PRICING_PREFIX, record, now)
    print(json.dumps({"path": str(path), "valid": valid,
                      "problems": {k: m["problems"] for k, m in models.items() if m["problems"]}}))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
