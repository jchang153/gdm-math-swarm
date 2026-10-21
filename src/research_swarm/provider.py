"""Pinned OpenRouter settings and conservative host-side cost bounds."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

MODEL_ID = "deepseek/deepseek-v4.1-flash"
# Models the prompt ablation track may run (docs/plan.md, "Prompt ablation track"). The
# baseline stays pinned to MODEL_ID by the launch gates (approved candidate_model_id).
GLM_MODEL_ID = "z-ai/glm-5.3"
ABLATION_MODEL_IDS = (MODEL_ID, GLM_MODEL_ID)
# The owner's Gemini track (docs/plan.md, "Gemini 3.1 Pro collaboration run authorized, October
# 19, 2026"): the paper's model, allowed on budget track "gemini" and nowhere else.
GEMINI_MODEL_ID = "google/gemini-3.1-pro-preview"
GEMINI_MODEL_IDS = (GEMINI_MODEL_ID,)
# The owner's GPT track (docs/plan.md, "GPT 5.6 Sol track, October 21, 2026"): OpenAI's
# reasoning model via OpenRouter, allowed on budget track "gpt" and nowhere else. Its reasoning
# comes back as OpenRouter reasoning_details in format openai-responses-v1 (summaries plus an
# encrypted reasoning item), which the reasoning bridge carries through Claude Code's replay.
GPT_SOL_MODEL_ID = "openai/gpt-5.6-sol"
GPT_MODEL_IDS = (GPT_SOL_MODEL_ID,)
# Models each budget track (budget.BUDGET_TRACKS) may run; the launch gates check this table.
TRACK_MODEL_IDS = {"ablation": ABLATION_MODEL_IDS, "gemini": GEMINI_MODEL_IDS, "gpt": GPT_MODEL_IDS}
# Every model any track may call (request_options refuses all others).
KNOWN_MODEL_IDS = tuple(dict.fromkeys(m for ids in ((MODEL_ID,), *TRACK_MODEL_IDS.values()) for m in ids))
# Models whose reasoning is signed (OpenRouter reasoning_details formats with a signature or an
# encrypted payload, e.g. Gemini thought signatures) and must be passed back on later turns with
# tool calls. Their live compatibility check must show such a replay accepted
# (``signed_reasoning_tool_call_replay``, scripts/compat_check.py).
SIGNED_REASONING_MODEL_IDS = (GEMINI_MODEL_ID, GPT_SOL_MODEL_ID)

# Chat-template and provider framing allowance added to the byte bound below.
FRAMING_TOKEN_ALLOWANCE = 8192

@dataclass(frozen=True)
class ProviderSettings:
    model_id: str = MODEL_ID
    provider_route: str = "deepseek"
    max_tokens: int = 16384
    context_length: int = 1048576
    # Maximum over the route's base and time-dependent override prices.
    prompt_usd_per_token: str = "0.0000003"
    output_usd_per_token: str = "0.0000012"
    pricing_verified: bool = False
    live_compatibility_verified: bool = False
    reasoning_effort: str = "high"

    def request_options(self):
        if self.model_id not in KNOWN_MODEL_IDS or not self.provider_route:
            raise ValueError("The approved model and explicit provider route are required")
        return {"provider":{"only":[self.provider_route],"allow_fallbacks":False,"require_parameters":True},
                "reasoning":{"effort":self.reasoning_effort,"exclude":False},
                "usage":{"include":True}}

    def prompt_token_bound(self, serialized_request: str) -> int:
        """Upper bound on billed prompt tokens without trusting a tokenizer estimate.

        DeepSeek uses a byte-level BPE vocabulary: every ordinary token covers at
        least one UTF-8 byte, so the UTF-8 length of the serialized request (which
        also includes JSON quoting) bounds its prompt tokens. Special/framing tokens
        are covered by FRAMING_TOKEN_ALLOWANCE. Capped at the route context length.
        The same argument is assumed for the other tracks' models (GLM's BPE vocabulary;
        Gemini's SentencePiece vocabulary with byte fallback, whose pieces also cover at
        least one byte each; GPT's byte-level o200k BPE). Replayed reasoning, including Gemini's encrypted thought
        signatures, is part of the serialized request and so of the bound. An
        unexpected excess shows up as an estimate_violation, which blocks the scope.
        """
        size = len(serialized_request.encode("utf-8")) + FRAMING_TOKEN_ALLOWANCE
        return min(size, self.context_length)

    def reservation(self, serialized_request):
        """Prompt byte bound plus ``max_tokens`` output, both at maximum route prices.

        Assumes the route caps reasoning + visible completion tokens at max_tokens
        (OpenRouter's reasoning normalization). This is unverified until the live
        compatibility check; if the route bills more, settlement records an
        estimate_violation and the ledger scales the run's later reservations up by the
        observed ratio (``budget.BudgetLedger.settle``); the run's budget still bounds spend.
        """
        if not self.pricing_verified:
            raise RuntimeError("Freeze fresh conservative route pricing before paid admission")
        return (Decimal(self.prompt_token_bound(serialized_request))*Decimal(self.prompt_usd_per_token)
                + Decimal(self.max_tokens)*Decimal(self.output_usd_per_token))

    def settlement_cost(self, usage):
        """Return (cost, basis) or (None, None) when billing is uncertain.

        Provider-reported cost is authoritative. Otherwise price provider-reported
        token counts at the frozen maximum route prices. Inspect's OpenAI adapter
        reports ``input_tokens`` net of cached and cache-write tokens
        (inspect_ai/model/_openai.py, pinned 0.3.279), so adding them back gives the
        exact total prompt count; pricing that total at the full prompt price is an
        upper bound because cache reads bill below it (cache-read/write prices are
        checked not to exceed the prompt bound in validate_catalog). Completion uses
        the OpenAI-compatible convention that output tokens include reasoning tokens
        (verified against the pinned Inspect adapter; see docs/runtime.md), taking
        the larger of the two counts. Missing usage → uncertain.
        """
        if usage is None:
            return None, None
        reported = getattr(usage, "total_cost", None)
        if reported is not None:
            return Decimal(str(reported)), "provider_reported_cost"
        prompt = getattr(usage, "input_tokens", None)
        completion = getattr(usage, "output_tokens", None)
        if prompt is None or completion is None:
            return None, None
        prompt += getattr(usage, "input_tokens_cache_read", None) or 0
        prompt += getattr(usage, "input_tokens_cache_write", None) or 0
        completion = max(completion, getattr(usage, "reasoning_tokens", None) or 0)
        cost = Decimal(prompt)*Decimal(self.prompt_usd_per_token) + Decimal(completion)*Decimal(self.output_usd_per_token)
        return cost.quantize(Decimal("1e-9"), rounding=ROUND_CEILING), "usage_priced_upper_bound"

# Fields a run config may override through ``provider_settings``. The two
# verification flags are set only by the launcher after its gates pass.
CONFIG_OVERRIDABLE = frozenset(f for f in ProviderSettings.__dataclass_fields__
                               if f not in ("pricing_verified", "live_compatibility_verified"))

def settings_from_config(config, *, pricing_verified=False, live_compatibility_verified=False):
    """The ProviderSettings a run config describes (shared by runtime.run_pilot,
    scripts/freeze_route.py and research_swarm.launch_gates, so the frozen record,
    the gate and the launched run compare the same object)."""
    overrides = dict(config.get("provider_settings") or {})
    unknown = set(overrides) - CONFIG_OVERRIDABLE
    if unknown:
        raise ValueError(f"Unsupported provider_settings keys: {sorted(unknown)}")
    route = config.get("provider_route")
    if overrides.get("provider_route") not in (None, route):
        raise ValueError("provider_settings.provider_route conflicts with provider_route")
    overrides["provider_route"] = route
    return ProviderSettings(**overrides, pricing_verified=pricing_verified,
                            live_compatibility_verified=live_compatibility_verified)

def frozen_fields(settings):
    """Settings compared against a route-pricing freeze (verification flags excluded)."""
    return {f: getattr(settings, f) for f in sorted(CONFIG_OVERRIDABLE)}

def _price_entries(endpoint):
    return [endpoint["pricing"], *endpoint["pricing"].get("overrides", [])]

def route_price_bounds(endpoint):
    prices = _price_entries(endpoint)
    return (max(Decimal(p["prompt"]) for p in prices), max(Decimal(p["completion"]) for p in prices))

def route_cache_read_bound(endpoint):
    """Maximum cache-read price over base and override pricing (None if unpublished)."""
    values = [Decimal(p["input_cache_read"]) for p in _price_entries(endpoint) if p.get("input_cache_read") is not None]
    return max(values) if values else None

def validate_catalog(catalog, settings):
    data = catalog.get("data",catalog)
    if data.get("id") != settings.model_id:
        raise ValueError("Catalog model mismatch")
    routes = [e for e in data.get("endpoints",[]) if e.get("tag") == settings.provider_route]
    if len(routes) != 1 or routes[0].get("status") != 0:
        raise ValueError("Pinned provider route unavailable")
    endpoint = routes[0]
    if not {"tools","reasoning","max_tokens"}.issubset(endpoint.get("supported_parameters",[])):
        raise ValueError("Route lacks required agent parameters")
    prompt, completion = route_price_bounds(endpoint)
    if Decimal(settings.prompt_usd_per_token) < prompt or Decimal(settings.output_usd_per_token) < completion:
        raise ValueError("Reservation prices do not cover route time-dependent rates")
    # Price components the reservation/upper-bound settlement does not model must
    # be absent, zero, or covered by the prompt/completion bounds.
    for entry in _price_entries(endpoint):
        if Decimal(str(entry.get("request") or 0)) != 0:
            raise ValueError("Route charges a per-request fee the reservation does not cover")
        for name, bound in (("input_cache_read", settings.prompt_usd_per_token),
                            ("input_cache_write", settings.prompt_usd_per_token),
                            ("internal_reasoning", settings.output_usd_per_token)):
            if entry.get(name) is not None and Decimal(str(entry[name])) > Decimal(bound):
                raise ValueError(f"Route {name} price exceeds the reservation bound")
        if Decimal(str(entry.get("discount") or 0)) < 0:
            raise ValueError("Route pricing has a negative discount (surcharge)")
    if settings.context_length != endpoint.get("context_length"):
        raise ValueError("Context bound mismatch")
    if settings.max_tokens > (endpoint.get("max_completion_tokens") or settings.max_tokens):
        raise ValueError("Response bound exceeds route maximum completion")
    return endpoint
