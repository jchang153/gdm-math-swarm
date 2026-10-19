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
