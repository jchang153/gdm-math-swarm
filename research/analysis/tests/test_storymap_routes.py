"""Story map: model defaults and the frozen non-Anthropic routes (GPT-6.1 Terra). Unpaid: no network."""
from __future__ import annotations

import asyncio
import datetime
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for path in (str(REPO), str(REPO / "src"), str(REPO / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research.analysis.judge.client import FakeTransport, RunLimit, fake_completion  # noqa: E402
from research.analysis.judge.route import CATALOG_URL, ENDPOINTS_URL  # noqa: E402
from research.analysis.storymap import pipeline, pipeline_models as pm  # noqa: E402

NOW = datetime.datetime(2026, 11, 2, 20, 0, tzinfo=datetime.timezone.utc)
EFFORTS = ["max", "xhigh", "high", "medium", "low", "none"]
PARAMETERS = ["reasoning", "include_reasoning", "seed", "max_tokens", "response_format", "structured_outputs",
              "tools", "tool_choice", "verbosity", "reasoning_effort"]
# Public OpenRouter listing of 2026-11-02 (prices in USD per token): base tier and the >= 272k-token override.
PRICES = {
    "openai/gpt-6.1-terra": ("openai/gpt-6.1-terra-20261026",
                             ("0.000001", "0.000005", "0.00000005", "0.00000125"),
                             ("0.000002", "0.0000075", "0.0000001", "0.0000025")),
    "openai/gpt-5.6-terra": ("openai/gpt-5.6-terra-20260709",
                             ("0.000002", "0.000012", "0.0000002", "0.0000025"),
                             ("0.000004", "0.000018", "0.0000004", "0.000005")),
}


def pricing(base, override, scale=1):
    def tier(values):
        prompt, completion, read, write = (v if scale == 1 else format(Decimal(v) * scale, "f") for v in values)
        return {"prompt": prompt, "completion": completion, "input_cache_read": read, "input_cache_write": write}
    return {**tier(base), "web_search": "0.01", "overrides": [{"min_prompt_tokens": 272000, **tier(override)}]}


def endpoint(model_id, tag="openai", service_tier=None, scale=1, provider="OpenAI"):
    slug, base, override = PRICES[model_id]
    return {"name": f"{provider} | {slug}", "model_id": model_id, "context_length": 1050000,
            "pricing": {**pricing(base, override, scale), "discount": 0}, "provider_name": provider, "tag": tag,
            "service_tier": service_tier, "max_completion_tokens": 128000, "max_prompt_tokens": 922000,
            "supported_parameters": PARAMETERS, "status": 0}


def catalog_entry(model_id):
    slug, base, override = PRICES[model_id]
    return {"id": model_id, "canonical_slug": slug, "name": model_id, "context_length": 1050000,
            "pricing": pricing(base, override), "supported_parameters": sorted(PARAMETERS),
            "reasoning": {"mandatory": False, "default_enabled": True, "supported_efforts": EFFORTS,
                          "default_effort": "medium"}}


def public_catalog(model_ids):
    """A fetcher serving the public catalog and endpoint lists (flex/priority tiers and other providers included,
    as OpenRouter lists them); counts its calls. No network."""
    calls = []

    def fetcher(url):
        calls.append(url)
        if url == CATALOG_URL:
            return json.dumps({"data": [catalog_entry(m) for m in model_ids]}).encode()
        for model_id in model_ids:
            if url == ENDPOINTS_URL.format(model=model_id):
                endpoints = [endpoint(model_id, "openai/flex", "flex", Decimal("0.5")), endpoint(model_id),
                             endpoint(model_id, "amazon-bedrock/us-east-1", provider="Amazon Bedrock",
                                      scale=Decimal("1.1")),
                             endpoint(model_id, "openai/fast", "priority", 2)]
                return json.dumps({"data": {"id": model_id, "endpoints": endpoints}}).encode()
        return json.dumps({"data": {"endpoints": []}}).encode()
    return fetcher, calls


class DefaultModelTests(unittest.TestCase):
    def test_stage_defaults(self):
        self.assertEqual(pipeline.DEFAULT_MODELS, {"stage1": "anthropic/claude-haiku-5.5",
                                                   "stage2": "openai/gpt-6.1-terra",
                                                   "stage3": "openai/gpt-6.1-terra"})
        self.assertEqual(pipeline.DEFAULT_CROSSCHECK_MODEL, "google/gemini-3.8-flash")
        for model in (*pipeline.DEFAULT_MODELS.values(), pipeline.DEFAULT_CROSSCHECK_MODEL):
            self.assertEqual(pm.check_model(model), model)

    def test_both_terra_routes_are_known(self):
        # 6.1 is the default; 5.6 stays so the four earlier maps' pipeline.json records still price and resume.
        for model_id in ("openai/gpt-6.1-terra", "openai/gpt-5.6-terra"):
            self.assertIn(model_id, pm.KNOWN_MODELS)
            spec = pm.EXTRA_SPECS[model_id]
            self.assertEqual((spec.model_id, spec.provider_tag, spec.provider_name, spec.effort),
                             (model_id, "openai", "OpenAI", "medium"))
        self.assertEqual(pm.slug("openai/gpt-6.1-terra"), "gpt-6.1-terra")


class TerraRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def freeze(self):
        fetcher, calls = public_catalog(list(PRICES))
        path, record = pm.freeze_extra_routes(self.dir, ["openai/gpt-6.1-terra", "openai/gpt-5.6-terra"],
                                              fetcher=fetcher, now=NOW)
        return path, record, calls

    def test_route_resolves_with_frozen_prices(self):
        path, record, calls = self.freeze()
        self.assertTrue(record["valid"], record)
        self.assertFalse(record["credential_used"])
        self.assertEqual(record["paid_calls"], 0)
        # Only the standard OpenAI endpoint is frozen (no flex/priority tier, no other provider).
        self.assertEqual([e["tag"] for e in record["models"]["openai/gpt-6.1-terra"]["endpoints"]], ["openai"])
        settings = pm.StorymapRouteSettings.from_record(path, ["openai/gpt-6.1-terra"], now=NOW)
        route = settings.route("gpt-6.1-terra")
        self.assertEqual((route.model_id, route.canonical_slug, route.effort, route.context_length,
                          route.max_completion_tokens),
                         ("openai/gpt-6.1-terra", "openai/gpt-6.1-terra-20261026", "medium", 1050000, 128000))
        self.assertEqual([(t.min_prompt_tokens, t.prompt, t.completion, t.cache_read, t.cache_write, t.endpoint_tag)
                          for t in route.tiers],
                         [(0, "0.000001", "0.000005", "0.00000005", None, "openai"),
                          (272000, "0.000002", "0.0000075", "0.0000001", None, "openai")])
        prefs = settings.provider_preferences("gpt-6.1-terra")
        self.assertEqual(prefs["only"], ["openai"])
        self.assertFalse(prefs["allow_fallbacks"])
        self.assertTrue(settings.pricing_verified)
        self.assertFalse(settings.prompt_cache)

    def test_record_prices_stay_frozen(self):
        path, _, _ = self.freeze()
        before = pm.StorymapRouteSettings.from_record(path, ["openai/gpt-6.1-terra"], now=NOW).route("gpt-6.1-terra")
        # A later catalog change cannot touch the record: the route is rebuilt from the record's own copy.
        record = json.loads(path.read_text())
        self.assertEqual(record["models"]["openai/gpt-6.1-terra"]["endpoints"][0]["pricing"]["prompt"], "0.000001")
        after = pm.StorymapRouteSettings.from_record(path, ["openai/gpt-6.1-terra"], now=NOW).route("gpt-6.1-terra")
        self.assertEqual(before.tiers, after.tiers)
        # Tampering with the frozen prices changes the record's SHA-256, which pipeline.json records.
        sha = pm.StorymapRouteSettings.from_record(path, ["openai/gpt-6.1-terra"], now=NOW).pricing_record_sha256
        record["models"]["openai/gpt-6.1-terra"]["endpoints"][0]["pricing"]["prompt"] = "0.0000001"
        path.write_text(json.dumps(record))
        self.assertNotEqual(sha, pm.StorymapRouteSettings.from_record(path, ["openai/gpt-6.1-terra"],
                                                                      now=NOW).pricing_record_sha256)
        # Older than 72 h: refused (a paid run refreshes it).
        with self.assertRaises(ValueError):
            pm.StorymapRouteSettings.from_record(path, ["openai/gpt-6.1-terra"],
                                                 now=NOW + datetime.timedelta(hours=73))

    def test_old_record_without_6_1_is_refreshed_and_5_6_still_resolves(self):
        fetcher, _ = public_catalog(["openai/gpt-5.6-terra"])
        old, _ = pm.freeze_extra_routes(self.dir, ["openai/gpt-5.6-terra"], fetcher=fetcher,
                                        now=datetime.datetime.now(datetime.timezone.utc))
        # The 5.6 route still resolves from a record that predates 6.1.
        self.assertEqual(pm.StorymapRouteSettings.from_record(old, ["openai/gpt-5.6-terra"])
                         .route("gpt-5.6-terra").tiers[0].prompt, "0.000002")
        frozen = []

        def freezer(route_dir, model_ids):
            frozen.append(model_ids)
            fetch_all, _ = public_catalog(list(PRICES))
            return pm.freeze_extra_routes(route_dir, list(PRICES), fetcher=fetch_all,
                                          now=datetime.datetime.now(datetime.timezone.utc)
                                          + datetime.timedelta(seconds=1))
        settings = pm.extra_route_settings(["openai/gpt-6.1-terra"], route_dir=self.dir, freezer=freezer)
        self.assertEqual(len(frozen), 1)
        self.assertIn("openai/gpt-6.1-terra", frozen[0])
        self.assertEqual(settings.route("gpt-6.1-terra").tiers[0].completion, "0.000005")

    def test_client_request_is_pinned_and_settled(self):
        path, _, _ = self.freeze()
        settings = pm.StorymapRouteSettings.from_record(path, ["openai/gpt-6.1-terra"], now=NOW)
        transport = FakeTransport(responder=lambda request: fake_completion(
            '{"ok": true}', provider="OpenAI", model="openai/gpt-6.1-terra-20261026"))
        anthropic, _ = pm.offline_settings([])
        pool = pm.ModelPool(ledger_path=self.dir / "ledger.sqlite", archive_root=self.dir / "raw",
                            anthropic_settings=anthropic, extra_settings=settings, scope="rehearsal",
                            run_limit=RunLimit("10", source="override"), run_id="storymap-test", http=transport)
        described = pool.describe("openai/gpt-6.1-terra")
        self.assertEqual((described["route_key"], described["effort"], described["canonical_slug"]),
                         ("gpt-6.1-terra", "medium", "openai/gpt-6.1-terra-20261026"))
        self.assertEqual(described["provider"]["only"], ["openai"])
        self.assertEqual(described["pricing_record"], str(path))
        client = pool.get("openai/gpt-6.1-terra")
        result = asyncio.run(client.complete(system="s", user="u", purpose="stage2", unit_id="u-1",
                                             max_tokens=16000, effort=client.route.effort,
                                             response_format={"type": "json_object"}))
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(len(transport.requests), 1)
        body = transport.requests[0]["body"]
        self.assertEqual(body["model"], "openai/gpt-6.1-terra")
        self.assertEqual(body["provider"]["only"], ["openai"])
        self.assertFalse(body["provider"]["allow_fallbacks"])


if __name__ == "__main__":
    unittest.main()
