import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from research_swarm.budget import BudgetLedger
import copy
from research_swarm.provider import (ProviderSettings,validate_catalog,MODEL_ID,FRAMING_TOKEN_ALLOWANCE,settings_from_config,
                                     frozen_fields,route_cache_read_bound)

CATALOG={"data":{"id":MODEL_ID,"endpoints":[{"tag":"deepseek","status":0,"context_length":1048576,"max_completion_tokens":393216,"supported_parameters":["tools","reasoning","max_tokens"],"pricing":{"prompt":"0.00000015","completion":"0.0000006","overrides":[{"prompt":"0.0000003","completion":"0.0000012"}]}}]}}

class ProviderTests(unittest.TestCase):
    def test_pinned_no_fallback(self):
        options=ProviderSettings().request_options()
        self.assertEqual(options["provider"]["only"],["deepseek"])
        self.assertFalse(options["provider"]["allow_fallbacks"])
        self.assertTrue(options["usage"]["include"])
    def test_no_unverified_prices(self):
        with self.assertRaises(RuntimeError):ProviderSettings().reservation("hello")
    def test_byte_bound_reservation(self):
        settings=ProviderSettings(pricing_verified=True)
        request="é"*1000  # 2000 UTF-8 bytes
        expected=Decimal(2000+FRAMING_TOKEN_ALLOWANCE)*Decimal("0.0000003")+Decimal(16384)*Decimal("0.0000012")
        self.assertEqual(settings.reservation(request),expected)
        huge="x"*5_000_000
        self.assertEqual(settings.prompt_token_bound(huge),settings.context_length)
    def test_time_pricing_covered(self):
        validate_catalog(CATALOG,ProviderSettings())
    def test_time_pricing_rejected(self):
        settings=ProviderSettings(prompt_usd_per_token="0.00000015")
        with self.assertRaises(ValueError):validate_catalog(CATALOG,settings)
    def test_settlement_prefers_reported_cost(self):
        settings=ProviderSettings(pricing_verified=True)
        cost,basis=settings.settlement_cost(SimpleNamespace(total_cost=0.0012,input_tokens=10,output_tokens=5))
        self.assertEqual((cost,basis),(Decimal("0.0012"),"provider_reported_cost"))
    def test_settlement_usage_upper_bound(self):
        settings=ProviderSettings(pricing_verified=True)
        usage=SimpleNamespace(total_cost=None,input_tokens=1000,input_tokens_cache_read=4000,input_tokens_cache_write=None,output_tokens=300,reasoning_tokens=200)
        cost,basis=settings.settlement_cost(usage)
        self.assertEqual(basis,"usage_priced_upper_bound")
        self.assertEqual(cost,Decimal(5000)*Decimal("0.0000003")+Decimal(300)*Decimal("0.0000012"))
    def test_missing_usage_uncertain(self):
        settings=ProviderSettings(pricing_verified=True)
        self.assertEqual(settings.settlement_cost(None),(None,None))
        self.assertEqual(settings.settlement_cost(SimpleNamespace(total_cost=None,input_tokens=None,output_tokens=None)),(None,None))
    def test_usage_bound_never_exceeds_byte_reservation(self):
        # Prompt tokens <= bytes, completion <= max_tokens: settlement fits the reservation.
        settings=ProviderSettings(pricing_verified=True)
        request="x"*40000
        reserved=settings.reservation(request)
        usage=SimpleNamespace(total_cost=None,input_tokens=40000,input_tokens_cache_read=None,input_tokens_cache_write=None,output_tokens=16384,reasoning_tokens=16000)
        cost,_=settings.settlement_cost(usage)
        self.assertLessEqual(cost,reserved)
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"b.sqlite")
            ledger.start_run("run-1","pilot",100)
            request_id=ledger.reserve("pilot","agent-001",reserved)
            ledger.settle(request_id,cost,"usage_priced_upper_bound")
            snap=ledger.snapshot()["pilot"]
            self.assertFalse(snap["violation"]); self.assertEqual(snap["settled_requests"],1)
    def test_settings_from_config_matches_runtime_construction(self):
        settings=settings_from_config({"provider_route":"deepseek","provider_settings":{}},pricing_verified=True)
        self.assertEqual(settings,ProviderSettings(pricing_verified=True))
        self.assertEqual(frozen_fields(settings),frozen_fields(ProviderSettings()))
        self.assertNotIn("pricing_verified",frozen_fields(settings))
        with self.assertRaises(ValueError):settings_from_config({"provider_route":"deepseek","provider_settings":{"pricing_verified":True}})
        with self.assertRaises(ValueError):settings_from_config({"provider_route":"deepseek","provider_settings":{"provider_route":"fireworks"}})
        lower=settings_from_config({"provider_route":"deepseek","provider_settings":{"prompt_usd_per_token":"0.00000015"}})
        with self.assertRaises(ValueError):validate_catalog(CATALOG,lower)
    def test_unmodelled_price_components_rejected(self):
        for name,value in (("request","0.001"),("internal_reasoning","0.000002"),("input_cache_write","0.000001")):
            catalog=copy.deepcopy(CATALOG)
            catalog["data"]["endpoints"][0]["pricing"]["overrides"][0][name]=value
            with self.assertRaises(ValueError,msg=name):validate_catalog(catalog,ProviderSettings())
        catalog=copy.deepcopy(CATALOG)
        catalog["data"]["endpoints"][0]["pricing"].update(input_cache_read="0.000000003",request="0",internal_reasoning="0.0000012")
        validate_catalog(catalog,ProviderSettings())
        self.assertEqual(route_cache_read_bound(catalog["data"]["endpoints"][0]),Decimal("0.000000003"))
    def test_missing_max_completion_tolerated(self):
        catalog=copy.deepcopy(CATALOG)
        catalog["data"]["endpoints"][0]["max_completion_tokens"]=None
        validate_catalog(catalog,ProviderSettings())
        catalog["data"]["endpoints"][0]["max_completion_tokens"]=8192
        with self.assertRaises(ValueError):validate_catalog(catalog,ProviderSettings())


class AblationModelTests(unittest.TestCase):
    def test_request_options_allow_only_the_ablation_models(self):
        from research_swarm.provider import ABLATION_MODEL_IDS, GLM_MODEL_ID
        self.assertEqual(ABLATION_MODEL_IDS, (MODEL_ID, GLM_MODEL_ID))
        options = ProviderSettings(model_id=GLM_MODEL_ID, provider_route="z-ai/fp8").request_options()
        self.assertEqual(options["provider"], {"only": ["z-ai/fp8"], "allow_fallbacks": False, "require_parameters": True})
        with self.assertRaises(ValueError):
            ProviderSettings(model_id="openai/gpt-6.1").request_options()


class GeminiModelTests(unittest.TestCase):
    """The owner's Gemini track (docs/plan.md, "Gemini 3.1 Pro collaboration run authorized")."""
    CONFIG = Path(__file__).resolve().parents[1] / "configs" / "gemini-collab.json"

    def catalog(self, tiered=True):
        # Listed prices: $2/$12 per million prompt/completion tokens up to 200K prompt tokens, $4/$18 above
        # (cache read $0.20/$0.40, cache write $0.375), on two standard Google routes.
        base = {"prompt": "0.000002", "completion": "0.000012", "input_cache_read": "0.0000002",
                "input_cache_write": "0.000000375", "request": "0"}
        if tiered:
            base["overrides"] = [{"prompt": "0.000004", "completion": "0.000018", "input_cache_read": "0.0000004",
                                  "input_cache_write": "0.000000375"}]
        endpoint = {"status": 0, "context_length": 1048576, "max_completion_tokens": 65536,
                    "supported_parameters": ["tools", "tool_choice", "reasoning", "include_reasoning",
                                             "reasoning_effort", "max_tokens"], "pricing": base}
        return {"data": {"id": "google/gemini-3.1-pro-preview", "endpoints": [
            dict(copy.deepcopy(endpoint), tag="google-vertex/global", provider_name="Google"),
            dict(copy.deepcopy(endpoint), tag="google-ai-studio", provider_name="Google AI Studio")]}}

    def test_config_settings_cover_the_upper_tier_on_the_pinned_route(self):
        import json
        from research_swarm.provider import GEMINI_MODEL_ID, KNOWN_MODEL_IDS, TRACK_MODEL_IDS
        settings = settings_from_config(json.loads(self.CONFIG.read_text()))
        self.assertEqual((settings.model_id, settings.provider_route), (GEMINI_MODEL_ID, "google-vertex/global"))
        self.assertEqual((settings.max_tokens, settings.reasoning_effort), (16384, "high"))  # as the baseline
        self.assertEqual((settings.prompt_usd_per_token, settings.output_usd_per_token), ("0.000004", "0.000018"))
        for tiered in (True, False):
            self.assertEqual(validate_catalog(self.catalog(tiered), settings)["tag"], "google-vertex/global")
        options = settings.request_options()
        self.assertEqual(options["provider"], {"only": ["google-vertex/global"], "allow_fallbacks": False,
                                               "require_parameters": True})
        self.assertEqual(options["reasoning"], {"effort": "high", "exclude": False})
        self.assertEqual(TRACK_MODEL_IDS["gemini"], (GEMINI_MODEL_ID, "google/gemini-4-argon",
                                                     "anthropic/claude-opus-5.5", "openai/gpt-5.6-sol", "z-ai/glm-5.3"))
        self.assertNotIn(GEMINI_MODEL_ID, TRACK_MODEL_IDS["ablation"])
        self.assertIn(GEMINI_MODEL_ID, KNOWN_MODEL_IDS)
        # Lower-tier reservation prices do not cover the >200K tier.
        lower = ProviderSettings(model_id=GEMINI_MODEL_ID, provider_route="google-vertex/global",
                                 prompt_usd_per_token="0.000002", output_usd_per_token="0.000012")
        with self.assertRaises(ValueError):
            validate_catalog(self.catalog(), lower)
        # A response bound above the route's 65,536 maximum completion is refused.
        with self.assertRaises(ValueError):
            validate_catalog(self.catalog(), ProviderSettings(**{**frozen_fields(settings), "max_tokens": 70000}))


class Gemini4ArgonModelTests(unittest.TestCase):
    """Gemini 4 Argon on the Gemini budget track (owner request, October 21, 2026)."""
    CONFIG = Path(__file__).resolve().parents[1] / "configs" / "owner-gemini-4-argon.json"

    def catalog(self, **pricing):
        # OpenRouter catalog, October 21, 2026: $2/$10 per million prompt/completion tokens on
        # google-vertex/global, no >200K override; flex/us/priority routes are priced differently.
        base = {"prompt": "0.000002", "completion": "0.00001", "internal_reasoning": "0.00001",
                "input_cache_read": "0.0000001", "input_cache_write": "0.0000001875", "discount": 0.5, **pricing}
        endpoint = {"status": 0, "context_length": 1048576, "max_completion_tokens": 943718,
                    "supported_parameters": ["tools", "tool_choice", "reasoning", "include_reasoning",
                                             "reasoning_effort", "max_tokens", "stop"], "pricing": base}
        return {"data": {"id": "google/gemini-4-argon", "endpoints": [
            dict(copy.deepcopy(endpoint), tag="google-vertex/global", provider_name="Google"),
            dict(copy.deepcopy(endpoint), tag="google-vertex/global/priority", provider_name="Google",
                 pricing=dict(base, prompt="0.0000036", completion="0.000018")),
            dict(copy.deepcopy(endpoint), tag="google-vertex/us", provider_name="Google",
                 max_completion_tokens=65536, pricing=dict(base, prompt="0.0000022", completion="0.000011"))]}}

    def test_config_pins_the_global_vertex_route_with_signed_reasoning(self):
        import json
        from research_swarm.provider import (GEMINI4_MODEL_ID, KNOWN_MODEL_IDS, SIGNED_REASONING_MODEL_IDS,
                                             TRACK_MODEL_IDS)
        config = json.loads(self.CONFIG.read_text())
        settings = settings_from_config(config)
        self.assertEqual(config["candidate_model_id"], GEMINI4_MODEL_ID)
        self.assertEqual((settings.model_id, settings.provider_route), (GEMINI4_MODEL_ID, "google-vertex/global"))
        self.assertEqual((settings.max_tokens, settings.reasoning_effort), (16384, "high"))
        self.assertEqual((settings.prompt_usd_per_token, settings.output_usd_per_token), ("0.000002", "0.00001"))
        self.assertEqual(validate_catalog(self.catalog(), settings)["tag"], "google-vertex/global")
        self.assertEqual(settings.request_options()["provider"],
                         {"only": ["google-vertex/global"], "allow_fallbacks": False, "require_parameters": True})
        self.assertIn(GEMINI4_MODEL_ID, TRACK_MODEL_IDS["gemini"])
        self.assertNotIn(GEMINI4_MODEL_ID, TRACK_MODEL_IDS["ablation"])
        self.assertIn(GEMINI4_MODEL_ID, KNOWN_MODEL_IDS)
        self.assertIn(GEMINI4_MODEL_ID, SIGNED_REASONING_MODEL_IDS)
        # A future >200K tier (as on 3.1 Pro) above the frozen prices is refused at the freeze.
        tiered = self.catalog(overrides=[{"min_prompt_tokens": 200000, "prompt": "0.000004", "completion": "0.000015"}])
        with self.assertRaisesRegex(ValueError, "time-dependent"):
            validate_catalog(tiered, settings)

class GptModelTests(unittest.TestCase):
    """GPT 5.6 Sol on the owner's track (docs/plan.md, "Owner multi-model max-pressure runs, October 21, 2026")."""
    CONFIG = Path(__file__).resolve().parents[1] / "configs" / "owner-gpt-5.6-sol.json"

    def catalog(self):
        # OpenRouter catalog, October 21, 2026: openai/gpt-5.6-sol on the standard "openai" route
        # $4/$20 per million prompt/completion tokens, $8/$30 at >= 272K prompt tokens (cache read
        # $0.40/$0.80, cache write $5/$10); the flex tier is cheaper, Azure lists max_completion_tokens.
        def endpoint(tag, scale, parameter="max_tokens"):
            def price(value):
                return str(Decimal(value) * scale)
            return {"tag": tag, "status": 0, "context_length": 1050000, "max_completion_tokens": 128000,
                    "supported_parameters": ["include_reasoning", parameter, "reasoning", "reasoning_effort",
                                             "response_format", "seed", "structured_outputs", "tool_choice",
                                             "tools", "verbosity"],
                    "pricing": {"prompt": price("0.000004"), "completion": price("0.00002"),
                                "input_cache_read": price("0.0000004"), "input_cache_write": price("0.000005"),
                                "discount": 0,
                                "overrides": [{"min_prompt_tokens": 272000, "prompt": price("0.000008"),
                                               "completion": price("0.00003"),
                                               "input_cache_read": price("0.0000008"),
                                               "input_cache_write": price("0.00001")}]}}
        return {"data": {"id": "openai/gpt-5.6-sol", "endpoints": [
            endpoint("openai/flex", Decimal("0.5")), endpoint("openai", Decimal(1)),
            endpoint("azure", Decimal(1), "max_completion_tokens")]}}

    def test_config_settings_cover_the_upper_tier_and_cache_writes_on_the_pinned_route(self):
        import json
        from research_swarm.budget import BUDGET_TRACKS
        from research_swarm.provider import (GPT_SOL_MODEL_ID, KNOWN_MODEL_IDS, SIGNED_REASONING_MODEL_IDS,
                                             TRACK_MODEL_IDS)
        config = json.loads(self.CONFIG.read_text())
        settings = settings_from_config(config)
        self.assertEqual((settings.model_id, settings.provider_route), (GPT_SOL_MODEL_ID, "openai"))
        self.assertEqual((settings.max_tokens, settings.reasoning_effort), (16384, "high"))
        self.assertEqual(validate_catalog(self.catalog(), settings)["tag"], "openai")
        options = settings.request_options()
        self.assertEqual(options["provider"], {"only": ["openai"], "allow_fallbacks": False,
                                               "require_parameters": True})
        self.assertEqual(options["reasoning"], {"effort": "high", "exclude": False})
        self.assertIn(GPT_SOL_MODEL_ID, TRACK_MODEL_IDS["gemini"])  # the owner's track
        self.assertNotIn(GPT_SOL_MODEL_ID, TRACK_MODEL_IDS["ablation"])
        self.assertIn(GPT_SOL_MODEL_ID, KNOWN_MODEL_IDS)
        # Encrypted reasoning must be replayed with tool calls: the compatibility check requires it.
        self.assertIn(GPT_SOL_MODEL_ID, SIGNED_REASONING_MODEL_IDS)
        self.assertEqual(config["budget_track"], "gemini")
        self.assertIn(config["agent_count"], BUDGET_TRACKS["gemini"]["agent_counts"])
        self.assertEqual((config["max_connections"], config["max_run_seconds"]), (5, 3600))
        # The upper-tier prompt price ($8/M) alone does not cover the upper-tier cache write ($10/M).
        lower = ProviderSettings(**{**frozen_fields(settings), "prompt_usd_per_token": "0.000008"})
        with self.assertRaisesRegex(ValueError, "input_cache_write"):
            validate_catalog(self.catalog(), lower)
        with self.assertRaises(ValueError):  # base-tier output price
            validate_catalog(self.catalog(), ProviderSettings(**{**frozen_fields(settings),
                                                                 "output_usd_per_token": "0.00002"}))
        # A route without max_tokens (Azure lists only max_completion_tokens) is refused.
        with self.assertRaises(ValueError):
            validate_catalog(self.catalog(), ProviderSettings(**{**frozen_fields(settings), "provider_route": "azure"}))
