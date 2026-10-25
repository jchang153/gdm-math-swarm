"""Offline tests for provider refusals without usage (owner request, October 21, 2026).

Covered: the judge client settles a 200 refusal that reports no usage from OpenRouter's generation
record (0 when the generation was never recorded, the reported total_cost otherwise, the hold kept
when the lookup fails); the startup pass (``reconcile_archived_holds``) settles leftover uncertain
holds from the archived responses through the ledger's reconciliation path; the incident pipeline
sends a unit Anthropic refuses once to the non-Anthropic fallback model (never back to Anthropic)
and flags it; the fallback route's pin and pricing record. Mock HTTP only: no network, no key.

Run: PYTHONPATH=src:. .venv/bin/python -m unittest research.analysis.tests.test_incident_refusal
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for path in (str(REPO), str(REPO / "src"), str(REPO / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research.analysis.judge import route as judge_route  # noqa: E402
from research.analysis.judge.client import (NOT_RECORDED, FakeTransport, JudgeClient, RunLimit,  # noqa: E402
                                            fake_completion, reconcile_archived_holds, unreported_response)
from research.analysis.judge.route import FALLBACK_KEY, OBSERVED_FALLBACK, JudgeRouteSettings  # noqa: E402
from research.analysis.tests.test_incident_report import LONG, Fixture  # noqa: E402

TOS = ("This request was blocked as it seems to violate Anthropic's Terms of Service restrictions on reverse "
       "engineering or duplicating model outputs.")


def tos_refusal(generation_id="gen-refused-1"):
    """The observed shape: HTTP 200, content_filter / refusal, content null, no usage at all."""
    payload = {"id": generation_id, "object": "chat.completion", "model": "anthropic/claude-opus-5.5",
               "provider": "Anthropic", "service_tier": None,
               "choices": [{"index": 0, "finish_reason": "content_filter", "native_finish_reason": "refusal",
                            "message": {"role": "assistant", "content": None, "refusal": TOS}}]}
    from research.analysis.judge.client import TransportResponse
    return TransportResponse(200, b"\n  \n" + json.dumps(payload).encode(), {"content-type": "application/json"})


async def no_sleep(_delay):
    return None


def ledger_rows(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT id, state, actual, reserved, basis FROM calls ORDER BY rowid").fetchall()


class ClientSettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "ledger.sqlite"
        self.lookups = []

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, script, lookup):
        self.limit = RunLimit("20")
        client = JudgeClient("opus", self.ledger, scope="rehearsal", archive_dir=self.root / "raw",
                             settings=JudgeRouteSettings.defaults(pricing_verified=True),
                             http=FakeTransport(script), run_limit=self.limit, run_id="incident-x",
                             generation_lookup=lookup)
        client.sleep = no_sleep
        return client

    def complete(self, client):
        return asyncio.run(client.complete(system="s", user="u", purpose="incident_extract", unit_id="u1",
                                           max_tokens=1000, effort="high", refusal_retry=False))

    def test_refusal_never_recorded_settles_at_zero(self):
        def lookup(generation_id):
            self.lookups.append(generation_id)
            return NOT_RECORDED
        result = self.complete(self.client([tos_refusal()], lookup))
        self.assertEqual(result["error"]["reason"], "refused_once")
        self.assertEqual(result["cost_usd"], "0")
        self.assertEqual(self.lookups, ["gen-refused-1"] * 3)        # retried while not yet recorded
        (_, state, actual, _, basis), = ledger_rows(self.ledger)
        self.assertEqual((state, actual, basis), ("settled", 0, "unbilled_error"))
        self.assertEqual(self.limit.record()["uncertain_usd"], "0")
        archived = list((self.root / "raw").rglob("*.generation.json"))
        self.assertTrue(archived and json.loads(archived[0].read_text())["not_recorded"])

    def test_no_usage_response_settles_at_reported_total_cost(self):
        reply = fake_completion("{}", cost=None, usage={})
        body = json.loads(reply.body)
        body.pop("usage")
        reply.body = json.dumps(body).encode()
        result = self.complete(self.client([reply], lambda gid: {"model": "anthropic/claude-opus-5.5-20260921",
                                                                 "total_cost": 0.0123}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["cost_usd"], "0.0123")
        self.assertEqual(ledger_rows(self.ledger)[0][1], "settled")

    def test_failed_lookup_keeps_the_hold(self):
        def lookup(generation_id):
            raise OSError("network down")
        result = self.complete(self.client([tos_refusal()], lookup))
        self.assertIsNone(result["cost_usd"])
        self.assertEqual(ledger_rows(self.ledger)[0][1], "uncertain")

    def test_model_mismatch_keeps_the_hold(self):
        result = self.complete(self.client([tos_refusal()], lambda gid: {"model": "other/model", "total_cost": 0}))
        self.assertIsNone(result["cost_usd"])
        self.assertEqual(ledger_rows(self.ledger)[0][1], "uncertain")

    def test_reported_usage_needs_no_lookup(self):
        result = self.complete(self.client([fake_completion("{}", cost="0.004")], lambda gid: self.fail("looked up")))
        self.assertEqual(result["cost_usd"], "0.004")

    def test_unreported_response_shape(self):
        self.assertEqual(unreported_response(tos_refusal().body),
                         {"generation_id": "gen-refused-1", "model": "anthropic/claude-opus-5.5", "refused": True})
        self.assertIsNone(unreported_response(fake_completion("x").body))   # usage.cost reported
        self.assertIsNone(unreported_response(b"not json"))


class StartupPassTests(unittest.TestCase):
    def test_leftover_holds_are_reconciled_from_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.sqlite"
            client = JudgeClient("opus", ledger, scope="analysis", archive_dir=root / "raw",
                                 settings=JudgeRouteSettings.defaults(pricing_verified=True),
                                 http=FakeTransport([tos_refusal("gen-a"), tos_refusal("gen-b"),
                                                     fake_completion("ok", cost="0.01")]),
                                 run_limit=RunLimit("20"), run_id="incident-x")   # no lookup: holds stay
            for unit in ("u1", "u2", "u3"):
                asyncio.run(client.complete(system="s", user="u", purpose="incident_extract", unit_id=unit,
                                            max_tokens=1000, effort="high", refusal_retry=False))
            self.assertEqual([r[1] for r in ledger_rows(ledger)], ["uncertain", "uncertain", "settled"])
            answers = {"gen-a": NOT_RECORDED}

            def lookup(generation_id):
                if generation_id not in answers:
                    raise OSError("lookup failed")
                return answers[generation_id]
            entries = reconcile_archived_holds(ledger, [root / "raw"], lookup)
            self.assertEqual(sorted(e["outcome"] for e in entries), ["lookup_failed: OSError", "reconciled"])
            rows = ledger_rows(ledger)
            self.assertEqual([r[1] for r in rows], ["settled", "uncertain", "settled"])
            self.assertEqual(rows[0][2], 0)
            with closing(sqlite3.connect(ledger)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0], 1)
            # A second pass touches only what is still uncertain.
            answers["gen-b"] = {"model": "anthropic/claude-opus-5.5-20260921", "total_cost": 0.002}
            entries = reconcile_archived_holds(ledger, [root / "raw"], lookup)
            self.assertEqual([e["outcome"] for e in entries], ["reconciled"])
            self.assertEqual([r[1] for r in ledger_rows(ledger)], ["settled"] * 3)


class ModelFallbackTests(unittest.TestCase):
    def test_refused_extraction_goes_to_the_fallback_model_once(self):
        with tempfile.TemporaryDirectory() as root:
            fx = Fixture(root)
            fx.routes = JudgeRouteSettings.defaults(pricing_verified=True, fallback=OBSERVED_FALLBACK)
            sent = {"anthropic": [], "fallback": []}

            def responder(request):
                body = request["body"]
                system = body["messages"][0]["content"]
                system = system if isinstance(system, str) else system[0]["text"]
                extracting = "choose, for the researchers' own report" in system
                if body["model"] == judge_route.FALLBACK_MODEL_ID:
                    sent["fallback"].append(body)
                    reply = fx.model(request)
                    payload = json.loads(reply.body)
                    payload.update(provider="OpenAI", model="openai/gpt-5.6-sol-20260709")
                    reply.body = json.dumps(payload).encode()
                    return reply
                if extracting:
                    sent["anthropic"].append(body)
                    return tos_refusal(f"gen-r{len(sent['anthropic'])}")
                return fx.model(request)

            fx.transport.responder = responder
            pipeline = fx.pipeline()
            limit = pipeline.clients["opus"].run_limit
            pipeline.clients["fallback"] = JudgeClient(FALLBACK_KEY, fx.root / "ledger.sqlite", scope="rehearsal",
                                                       archive_dir=fx.out / "raw", settings=fx.routes,
                                                       http=fx.transport, run_limit=limit,
                                                       run_id="incident-x",
                                                       generation_lookup=lambda gid: NOT_RECORDED)
            pipeline.clients["opus"]._generation_lookup = lambda gid: NOT_RECORDED
            for client in pipeline.clients.values():
                client.sleep = no_sleep
            summary = asyncio.run(pipeline.run())
            self.assertEqual(summary["status"], "complete")
            # Each refused unit went once to Anthropic (no retry, no split) and once to the fallback.
            self.assertEqual(len(sent["anthropic"]), len(sent["fallback"]))
            self.assertTrue(sent["fallback"])
            fallback_body = sent["fallback"][0]
            self.assertEqual(fallback_body["provider"]["only"], ["openai"])
            self.assertFalse(fallback_body["provider"]["allow_fallbacks"])
            self.assertNotIn("cache_control", json.dumps(fallback_body))
            evidence = json.loads((fx.out / "evidence.json").read_text())
            self.assertTrue(evidence["model_fallbacks"])
            self.assertTrue(all(f["status"] == "ok" and f["fallback_model"] == "openai/gpt-5.6-sol"
                                for f in evidence["model_fallbacks"]))
            self.assertFalse(evidence["extract_refusals"])
            record = [r for s in evidence["evidence"]["storylines"] for r in s["records"] if r["id"] == "A02.t0001.r"]
            excerpt = record[0]["excerpts"][0]
            self.assertEqual((excerpt["source"], excerpt.get("model_fallback")), ("model", True))
            self.assertIn(excerpt["quote"], LONG)
            self.assertTrue(evidence["evidence"]["excerpt_sources"]["model_fallback"])
            self.assertIn("Fallback model", (fx.out / "report.html").read_text())
            self.assertIn("answered by the fallback model openai/gpt-5.6-sol", (fx.out / "report.md").read_text())
            # The refusals cost nothing (never recorded) and a rerun sends nothing at all.
            self.assertEqual(limit.record()["uncertain_usd"], "0")
            count = len(fx.transport.requests)
            asyncio.run(pipeline.run())
            self.assertEqual(len(fx.transport.requests), count)


class FallbackRouteTests(unittest.TestCase):
    def test_pricing_record_fallback_is_optional_and_validated(self):
        endpoint = {"tag": "openai", "provider_name": "OpenAI", "status": 0, "service_tier": None,
                    "context_length": 1050000, "max_completion_tokens": 128000,
                    "supported_parameters": ["reasoning", "max_tokens", "reasoning_effort"],
                    "pricing": {"prompt": "0.000004", "completion": "0.00002", "discount": 0}}
        catalog = {"id": judge_route.FALLBACK_MODEL_ID, "reasoning": {"supported_efforts": ["high", "low"]}}
        route, problems = judge_route.build_route(FALLBACK_KEY, catalog, [endpoint])
        self.assertEqual(problems, [])
        self.assertEqual(route.model_id, "openai/gpt-5.6-sol")
        _, problems = judge_route.build_route(FALLBACK_KEY, catalog, [dict(endpoint, provider_name="Azure")])
        self.assertTrue(problems)
        plain = JudgeRouteSettings.defaults(pricing_verified=True)
        self.assertFalse(plain.has_fallback)
        self.assertNotIn("fallback", plain.describe())
        with self.assertRaises(ValueError):
            plain.route(FALLBACK_KEY)
        settings = JudgeRouteSettings.defaults(pricing_verified=True, fallback=route)
        self.assertEqual(settings.provider_preferences(FALLBACK_KEY)["only"], ["openai"])
        self.assertEqual(settings.provider_preferences()["only"], ["anthropic"])
        self.assertFalse(settings.cache_system_default(FALLBACK_KEY))


if __name__ == "__main__":
    unittest.main()
