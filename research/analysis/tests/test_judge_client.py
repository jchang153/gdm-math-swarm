"""Offline tests for the judge's OpenRouter client, route pricing and ledger integration.

No network, no model call, no credential: every request goes to FakeTransport, and
ledgers are synthetic temporary files reproducing the authoritative lineage (the three
audited pilot raises) plus both audited judge analysis raises ($5 -> $40 -> $50; the
client accepts the ledger only after the second). The three models
(haiku, sonnet, opus), per-request prompt caching of the system prompt, cache usage and
cost recording, and the paid-only credentials_root are covered below; the real key
loader is patched to fail, and fake paths are checked never to open any ``.env``.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from research_swarm import budget  # noqa: E402
from research.analysis.judge import client as judge_client  # noqa: E402
from research.analysis.judge.client import (FakeTransport, JudgeClient, TransportError, fake_completion,  # noqa: E402
                                            fake_http_error)
from research.analysis.judge import route as judge_route  # noqa: E402
from research.analysis.judge.route import (EFFORTS, MODEL_IDS, PRICING_PREFIX, JudgeRouteSettings,  # noqa: E402
                                           build_route)

FAKE_KEY = "test-key-not-a-real-credential-0123456789"
SYSTEM = "You are a careful researcher-side judge."
USER = "Window A07.w0001: the agent wrote a Lean proof."


def make_ledger(folder, *, judge_raises=5):
    """Synthetic ledger with the authoritative lineage and the first ``judge_raises`` audited
    judge raises applied (0: $5, 1: $40, 2: $50, 3: $100, 4: $110, 5: $200, the only state the
    client accepts)."""
    path = Path(folder) / "budget.sqlite"
    ledger = budget.BudgetLedger(path, {"pilot": budget.CACHE_REPEAT_CAP_USD, "analysis": 5, "compatibility": 2})
    ledger.settle(ledger.reserve("pilot", "old-run", 1), ".5")
    with closing(sqlite3.connect(path)) as db, db:
        for previous, new in budget.JUDGE_ANALYSIS_PILOT_CAP_HISTORY:
            db.execute("INSERT INTO cap_changes (scope,previous_cap,new_cap,note) VALUES ('pilot',?,?,?)",
                       (budget.units(previous), budget.units(new), "historical approval fixture"))
    for apply in (budget.raise_judge_analysis_cap, budget.raise_judge_analysis_second_cap,
                  budget.raise_judge_analysis_third_cap, budget.raise_judge_analysis_fourth_cap,
                  budget.raise_judge_analysis_fifth_cap)[:judge_raises]:
        apply(path, "test fixture approval")
    return path


def settings(**overrides):
    return JudgeRouteSettings.defaults(pricing_verified=True, **overrides)


def analysis(path):
    return budget.read_snapshot(path)["analysis"]


def archived_text(folder):
    return "\n".join(p.read_bytes().decode("utf-8", "replace") for p in Path(folder).rglob("*") if p.is_file())


class ClientCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.ledger_path = make_ledger(self.root)
        self.archive = self.root / "archive"
        # Credentials are never loaded by tests: any attempt fails loudly.
        patch = mock.patch("research_swarm.secrets.load_openrouter_key",
                           side_effect=AssertionError("tests must never load the host key"))
        patch.start()
        self.addCleanup(patch.stop)

    def client(self, transport, model_key="opus", **kwargs):
        kwargs.setdefault("settings", settings())
        return JudgeClient(model_key, self.ledger_path, "analysis", self.archive, http=transport, **kwargs)

    def run_one(self, client, model_key="opus", **kwargs):
        call = {"system": SYSTEM, "user": USER, "purpose": "judge", "unit_id": "A07", "max_tokens": 8000,
                "effort": EFFORTS[model_key]}
        call.update(kwargs)
        return asyncio.run(client.complete(**call))


class SuccessAndArchiveTests(ClientCase):
    def test_ok_settles_reported_cost_and_sends_pinned_request(self):
        seen = {}

        def on_request(request):
            # Archive-before-dispatch: the request record exists when the transport is called.
            seen["archived"] = sorted(p.name for p in self.archive.rglob("*.request.json"))
        transport = FakeTransport([fake_completion('{"verdict": 1}', cost="0.0123", reasoning="summary")],
                                  on_request=on_request)
        result = self.run_one(self.client(transport))
        self.assertEqual((result["status"], result["text"], result["attempts"], result["refusals"]),
                         ("ok", '{"verdict": 1}', 1, 0))
        self.assertEqual(result["cost_usd"], "0.0123")
        self.assertEqual(result["reasoning"], "summary")
        self.assertEqual(result["generation_ids"], ["gen-fake-0001"])
        self.assertEqual(len(seen["archived"]), 1)
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["committed_usd"], snap["unresolved_requests"], snap["settled_requests"]),
                         ("0.0123", 0, 1))
        body = transport.requests[0]["body"]
        self.assertEqual(body["model"], MODEL_IDS["opus"])
        self.assertEqual(body["provider"], {"order": ["anthropic"], "only": ["anthropic"], "allow_fallbacks": False,
                                            "require_parameters": True})
        self.assertEqual(body["reasoning"], {"effort": "high", "exclude": False})
        self.assertEqual(body["max_tokens"], 8000)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        for forbidden in ("temperature", "top_p", "top_k", "models", "service_tier", "stream"):
            self.assertNotIn(forbidden, body)
        self.assertNotIn("Authorization", transport.requests[0]["headers"])
        # Raw response bytes archived verbatim, plus request and metadata records.
        names = sorted(Path(p).name.split(".", 1)[1] for p in result["raw_paths"])
        self.assertEqual(names, ["request.json", "response.body", "response.meta.json"])
        request_record = json.loads(next(Path(p) for p in result["raw_paths"] if p.endswith(".request.json"))
                                    .read_text())
        self.assertEqual(request_record["ledger_request_id"], result["request_ids"][0])
        self.assertEqual(request_record["body"], body)
        self.assertEqual(Decimal(request_record["reserved_usd"]),
                         settings().reservation("opus", json.dumps(body, ensure_ascii=False,
                                                                   separators=(",", ":")), 8000))

    def test_haiku_low_effort_and_structured_output(self):
        transport = FakeTransport([fake_completion('{"selections": []}', model=MODEL_IDS["haiku"])])
        schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}
        result = self.run_one(self.client(transport, "haiku"), "haiku", purpose="prefilter", response_format=schema)
        self.assertEqual(result["status"], "ok")
        body = transport.requests[0]["body"]
        self.assertEqual((body["model"], body["reasoning"]["effort"], body["response_format"]),
                         (MODEL_IDS["haiku"], "low", schema))

    def test_archive_precedes_parsing(self):
        raw = b"not json {"
        transport = FakeTransport([judge_client.TransportResponse(200, raw, {"content-type": "text/plain"})])
        client = self.client(transport)
        original = client._interpret

        def interpret(response, request_id):
            bodies = list(self.archive.rglob("*.response.body"))
            self.assertEqual([p.read_bytes() for p in bodies], [raw])  # archived before parsing
            return original(response, request_id)
        client._interpret = interpret
        result = self.run_one(client)
        self.assertEqual((result["status"], result["error"]["reason"]), ("grading_failed", "unparseable_response"))
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["uncertain_requests"], snap["in_flight_requests"]), (1, 0))
        self.assertIsNone(result["cost_usd"])

    def test_response_archive_failure_keeps_hold_and_never_parses(self):
        transport = FakeTransport([fake_completion("{}")])
        client = self.client(transport)
        client._interpret = mock.Mock(side_effect=AssertionError("must not parse"))
        original = client._write_bytes

        def failing(path, data):
            if str(path).endswith(".response.body"):
                raise OSError("disk full")
            return original(path, data)
        client._write_bytes = failing
        result = self.run_one(client)
        self.assertEqual(result["error"]["reason"], "archive_failed_after_response")
        self.assertEqual(analysis(self.ledger_path)["uncertain_requests"], 1)

    def test_request_archive_failure_is_never_dispatched(self):
        transport = FakeTransport([fake_completion("{}")])
        client = self.client(transport)
        with mock.patch.object(client, "_write_json", side_effect=OSError("read-only")):
            result = self.run_one(client)
        self.assertEqual((result["status"], result["error"]["reason"], result["attempts"]),
                         ("grading_failed", "archive_failed_before_dispatch", 0))
        self.assertEqual(transport.requests, [])
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["remaining_usd"], snap["unbilled_error_requests"]), ("200", 1))


class RefusalTests(ClientCase):
    def test_refusal_then_ok_retries_once(self):
        transport = FakeTransport([fake_completion("", finish_reason="content_filter", refusal="I can't", cost=".002"),
                                   fake_completion('{"ok": true}', cost=".003", generation_id="gen-2")])
        result = self.run_one(self.client(transport))
        self.assertEqual((result["status"], result["attempts"], result["refusals"], result["text"]),
                         ("ok", 2, 1, '{"ok": true}'))
        self.assertEqual(result["cost_usd"], "0.005")
        self.assertEqual(len(set(result["request_ids"])), 2)
        self.assertEqual(len(transport.requests), 2)
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["committed_usd"], snap["settled_requests"]), ("0.005", 2))

    def test_double_refusal_is_ungraded_never_negative(self):
        transport = FakeTransport([fake_completion("", native_finish_reason="refusal", finish_reason="stop", cost=".001"),
                                   fake_completion("Sorry", refusal="declined", cost=".001")])
        result = self.run_one(self.client(transport))
        self.assertEqual((result["status"], result["attempts"], result["refusals"], result["text"]),
                         ("ungraded", 2, 2, None))
        self.assertEqual(result["error"]["reason"], "refused_twice")
        self.assertEqual(len(transport.requests), 2)  # exactly one retry

    def test_error_shaped_refusals(self):
        # 403 refusal before acceptance (unbilled), then a refusal error inside a 200 body
        # without a reported cost (uncertain hold).
        transport = FakeTransport([fake_http_error(403, error_type="refusal", message="usage policy"),
                                   fake_http_error(200, error_type="content_policy_violation")])
        result = self.run_one(self.client(transport))
        self.assertEqual((result["status"], result["refusals"]), ("ungraded", 2))
        states = [s["state"] for s in result["settlements"]]
        self.assertEqual(states, ["settled", "uncertain"])
        self.assertEqual(result["settlements"][0]["basis"], "unbilled_error")
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["unbilled_error_requests"], snap["uncertain_requests"]), (1, 1))

    def test_permission_403_is_infrastructure_not_refusal(self):
        transport = FakeTransport([fake_http_error(403, error_type="permission_denied")])
        result = self.run_one(self.client(transport))
        self.assertEqual((result["status"], result["refusals"], result["attempts"]), ("grading_failed", 0, 1))


class FailureTests(ClientCase):
    def test_http_4xx_settles_unbilled_without_retry(self):
        for status in (400, 401, 402, 429):
            with self.subTest(status=status):
                transport = FakeTransport([fake_http_error(status)])
                result = self.run_one(self.client(transport), unit_id=f"u{status}")
                self.assertEqual((result["status"], result["error"]["reason"], result["attempts"]),
                                 ("grading_failed", f"http_{status}", 1))
                self.assertEqual(result["cost_usd"], "0")
                self.assertEqual(len(transport.requests), 1)
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["remaining_usd"], snap["unbilled_error_requests"], snap["unresolved_requests"]),
                         ("200", 4, 0))

    def test_http_5xx_and_408_keep_the_reservation(self):
        for status in (408, 500, 502, 503):
            with self.subTest(status=status):
                transport = FakeTransport([fake_http_error(status)])
                result = self.run_one(self.client(transport), unit_id=f"u{status}")
                self.assertEqual((result["status"], result["error"]["reason"]), ("grading_failed", f"http_{status}"))
                self.assertIsNone(result["cost_usd"])
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["uncertain_requests"], snap["settled_requests"]), (4, 0))
        self.assertEqual(Decimal(snap["held_usd"]) + Decimal(snap["remaining_usd"]), Decimal(200))
        self.assertGreater(Decimal(snap["held_usd"]), 0)

    def test_transport_errors(self):
        transport = FakeTransport([TransportError("not_connected", "gaierror"), TransportError("uncertain", "timeout")])
        client = self.client(transport)
        first = self.run_one(client, unit_id="a")
        second = self.run_one(client, unit_id="b")
        self.assertEqual(first["error"]["reason"], "connection_not_established")
        self.assertEqual(second["error"]["reason"], "connection_lost_or_timeout")
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["unbilled_error_requests"], snap["uncertain_requests"]), (1, 1))

    def test_cancellation_in_flight_keeps_hold_and_propagates(self):
        transport = FakeTransport([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            self.run_one(self.client(transport))
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["uncertain_requests"], snap["in_flight_requests"]), (1, 0))

    def test_budget_denial_is_never_dispatched(self):
        budget.BudgetLedger(self.ledger_path, {"analysis": budget.JUDGE_ANALYSIS_CAP_USD}).reserve(
            "analysis", "other-unit-in-flight", "199.99")
        transport = FakeTransport([fake_completion("{}")])
        result = self.run_one(self.client(transport), max_tokens=64000)
        self.assertEqual((result["status"], result["error"]["reason"], result["attempts"]),
                         ("grading_failed", "budget_admission_denied", 0))
        self.assertEqual(transport.requests, [])
        self.assertEqual(len(list(self.archive.rglob("*.denied.json"))), 1)
        self.assertEqual(analysis(self.ledger_path)["unresolved_requests"], 1)  # only the pre-existing hold

    def test_estimate_violation_blocks_the_scope(self):
        transport = FakeTransport([fake_completion("{}", cost="5")])  # far above the reservation
        result = self.run_one(self.client(transport), max_tokens=1000)
        self.assertEqual(result["settlements"][0]["state"], "estimate_violation")
        self.assertTrue(analysis(self.ledger_path)["violation"])
        second = self.run_one(self.client(FakeTransport([fake_completion("{}")])), unit_id="next")
        self.assertEqual(second["error"]["reason"], "budget_admission_denied")

    def test_route_mismatch_is_grading_failed_but_settled(self):
        for payload in ({"provider": "Amazon Bedrock"}, {"model": "anthropic/claude-sonnet-5.5"},
                        {"service_tier": "priority"}):
            with self.subTest(payload=payload):
                transport = FakeTransport([fake_completion("{}", cost=".01", **payload)])
                result = self.run_one(self.client(transport), unit_id=str(payload))
                self.assertEqual((result["status"], result["error"]["reason"]), ("grading_failed", "route_mismatch"))
                self.assertEqual(result["cost_usd"], "0.01")
        accepted = FakeTransport([fake_completion("{}", model="anthropic/claude-opus-5.5-20260921", service_tier=None)])
        self.assertEqual(self.run_one(self.client(accepted), unit_id="canonical")["status"], "ok")

    def test_truncation_is_ungraded_and_choice_error_failed(self):
        transport = FakeTransport([fake_completion('{"partial', finish_reason="length"),
                                   fake_completion("", finish_reason="error")])
        client = self.client(transport)
        truncated = self.run_one(client, unit_id="t")
        errored = self.run_one(client, unit_id="e")
        self.assertEqual((truncated["status"], truncated["error"]["reason"], truncated["text"]),
                         ("ungraded", "max_tokens", '{"partial'))
        self.assertEqual((errored["status"], errored["error"]["reason"]), ("grading_failed", "provider_error_in_choice"))
        self.assertEqual(len(transport.requests), 2)  # neither retried


class CredentialAndGateTests(ClientCase):
    def test_no_credential_in_archives(self):
        echoed = fake_completion(f"echo {FAKE_KEY}", cost=".001")  # a response that (absurdly) echoes the key
        transport = FakeTransport([echoed, fake_http_error(500, message=f"bad key {FAKE_KEY}")])
        verified = []

        def verify(key, remaining, allow_unlimited=False):
            verified.append((remaining, allow_unlimited))
            return {"limit": None, "unlimited_key_authorized": allow_unlimited, "note": key}
        client = self.client(transport, key_loader=lambda: FAKE_KEY, verify_limit=verify)
        ok = self.run_one(client, unit_id="k1")
        self.run_one(client, unit_id="k2")
        self.assertEqual(verified, [(Decimal("200"), True)])  # unlimited-key exception, ledger remaining
        self.assertEqual(transport.requests[0]["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertNotIn(FAKE_KEY, archived_text(self.archive))
        self.assertIn(judge_client.REDACTION, archived_text(self.archive))
        self.assertIn(FAKE_KEY, ok["text"])  # the in-memory result is the provider's text; only archives scrub
        self.assertTrue((self.archive / "provider-limit-opus.json").exists())

    def test_paid_path_is_gated(self):
        record = sorted((REPO / "data" / "validation").glob(f"{PRICING_PREFIX}-*.json"))[-1]
        verified = JudgeRouteSettings.from_pricing_record(record, allow_stale=True)
        with self.assertRaises(RuntimeError):
            JudgeClient("opus", self.ledger_path, "analysis", self.archive, settings=verified)  # no authorization
        with self.assertRaises(RuntimeError):  # unverified defaults are refused for paid use
            JudgeClient("opus", self.ledger_path, "analysis", self.archive,
                        settings=settings(), paid_authorized=True)
        with self.assertRaises(RuntimeError):  # paid requests only through the analysis scope
            JudgeClient("opus", self.ledger_path, "rehearsal", self.archive, settings=verified,
                        paid_authorized=True, ledger_caps={"rehearsal": 1})
        # Constructing the authorized paid client builds the HTTPS transport but loads no key.
        client = JudgeClient("opus", self.ledger_path, "analysis", self.archive, settings=verified,
                             paid_authorized=True)
        self.assertIsInstance(client.http, judge_client.UrllibTransport)
        self.assertIsNone(client._key)

    def test_analysis_scope_requires_existing_raised_ledger(self):
        folder = self.root / "unraised"
        folder.mkdir()
        unraised = make_ledger(folder, judge_raises=0)
        with self.assertRaises(ValueError):
            JudgeClient("opus", unraised, "analysis", self.archive, settings=settings(), http=FakeTransport())
        absent = self.root / "absent.sqlite"
        with self.assertRaises(ValueError):
            JudgeClient("opus", absent, "analysis", self.archive, settings=settings(), http=FakeTransport())
        self.assertFalse(absent.exists())
        with self.assertRaises(ValueError):  # unverified pricing refused even offline
            JudgeClient("opus", self.ledger_path, "analysis", self.archive,
                        settings=JudgeRouteSettings.defaults(), http=FakeTransport())

    def test_analysis_scope_accepts_only_after_all_raises(self):
        """The $40, $50, $100 and $110 ledgers are refused; the fifth audited raise ($110 -> $200) makes it
        acceptable."""
        folder = self.root / "first-raise-only"
        folder.mkdir()
        path = make_ledger(folder, judge_raises=1)
        ledger = budget.BudgetLedger(path)  # default caps open the $40 ledger unchanged
        ledger.settle(ledger.reserve("analysis", "partial-run", "33"), "32.596031")
        for model_key in ("haiku", "sonnet", "opus"):
            with self.subTest(model_key=model_key), self.assertRaises(ValueError) as caught:
                JudgeClient(model_key, path, "analysis", self.archive, settings=settings(), http=FakeTransport())
            self.assertIn("analysis cap", str(caught.exception))
        self.assertEqual((analysis(path)["cap_usd"], analysis(path)["remaining_usd"]), ("40", "7.403969"))
        budget.raise_judge_analysis_second_cap(path, "test fixture approval")
        with self.assertRaises(ValueError):  # $50: still one audited raise short
            JudgeClient("opus", path, "analysis", self.archive, settings=settings(), http=FakeTransport())
        budget.raise_judge_analysis_third_cap(path, "test fixture approval")
        with self.assertRaises(ValueError):  # $100: still one audited raise short
            JudgeClient("opus", path, "analysis", self.archive, settings=settings(), http=FakeTransport())
        budget.raise_judge_analysis_fourth_cap(path, "test fixture approval")
        with self.assertRaises(ValueError):  # $110: still one audited raise short
            JudgeClient("opus", path, "analysis", self.archive, settings=settings(), http=FakeTransport())
        budget.raise_judge_analysis_fifth_cap(path, "test fixture approval")
        transport = FakeTransport([fake_completion("{}", cost=".01")])
        client = JudgeClient("opus", path, "analysis", self.archive, settings=settings(), http=transport)
        self.assertEqual(self.run_one(client)["status"], "ok")
        snap = analysis(path)
        self.assertEqual((snap["cap_usd"], snap["committed_usd"], snap["remaining_usd"]),
                         ("200", "32.606031", "167.393969"))
        # Tampering with the latest raise's binding is refused again.
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE cap_changes SET note='unbound' WHERE id=(SELECT MAX(id) FROM cap_changes)")
        with self.assertRaises(ValueError):
            JudgeClient("opus", path, "analysis", self.archive, settings=settings(), http=FakeTransport())

    def test_rehearsal_scope_needs_explicit_caps(self):
        path = self.root / "rehearsal.sqlite"
        with self.assertRaises(ValueError):
            JudgeClient("haiku", path, "rehearsal", self.archive, settings=settings(), http=FakeTransport())
        client = JudgeClient("haiku", path, "rehearsal", self.archive, settings=settings(),
                             http=FakeTransport([fake_completion("{}", model=MODEL_IDS["haiku"])]),
                             ledger_caps={"rehearsal": "40"})
        result = self.run_one(client, "haiku", purpose="prefilter")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(budget.read_snapshot(path)["rehearsal"]["settled_requests"], 1)

    def test_call_validation(self):
        client = self.client(FakeTransport())
        for kwargs in ({"effort": "low"}, {"max_tokens": 64001}, {"max_tokens": 0}, {"purpose": "Bad Purpose"},
                       {"unit_id": ""}, {"user": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_one(client, **kwargs)


class ConcurrencyTests(ClientCase):
    def test_semaphore_limits_in_flight_requests(self):
        transport = FakeTransport(responder=lambda request: fake_completion("{}", cost=".001"), delay=0.02)
        client = self.client(transport, max_concurrency=3)

        async def many():
            return await asyncio.gather(*(client.complete(system=SYSTEM, user=USER, purpose="verifier",
                                                          unit_id=f"F{i}", max_tokens=4000, effort="high")
                                          for i in range(10)))
        results = asyncio.run(many())
        self.assertEqual([r["status"] for r in results], ["ok"] * 10)
        self.assertEqual(transport.max_in_flight, 3)
        snap = analysis(self.ledger_path)
        self.assertEqual((snap["settled_requests"], snap["unresolved_requests"], snap["committed_usd"]),
                         (10, 0, "0.01"))


class RouteTests(unittest.TestCase):
    def test_reservation_bounds_and_tiers(self):
        s = settings()
        small, large = b"x" * 1000, b"x" * 100000
        # Haiku: base tier below the 100K threshold, override (prompt AND completion) at or above it.
        self.assertEqual(s.reservation("haiku", small, 16000),
                         Decimal(1000 + 8192) * Decimal("0.0000001") + 16000 * Decimal("0.0000005"))
        self.assertEqual(s.reservation("haiku", large, 16000),
                         Decimal(108192) * Decimal("0.0000005") + 16000 * Decimal("0.0000025"))
        self.assertEqual(s.reservation("opus", large, 48000),
                         Decimal(108192) * Decimal("0.000004") + 48000 * Decimal("0.00002"))
        # Prompt caching: cache writes (1.25x) bound every prompt token; no read discount.
        self.assertEqual(s.reservation("opus", large, 48000, cache_system=True),
                         Decimal(108192) * Decimal("0.000005") + 48000 * Decimal("0.00002"))
        self.assertEqual(s.reservation("sonnet", large, 48000, cache_system=True),
                         Decimal(108192) * Decimal("0.0000025") + 48000 * Decimal("0.00001"))
        self.assertEqual(s.reservation("sonnet", large, 48000),
                         Decimal(108192) * Decimal("0.000002") + 48000 * Decimal("0.00001"))
        # Haiku above 100K: the override tier's cache-write price bounds the prompt.
        self.assertEqual(s.reservation("haiku", large, 16000, cache_system=True),
                         Decimal(108192) * Decimal("0.000000625") + 16000 * Decimal("0.0000025"))
        # A body carrying any cache_control marker is priced as cached even if the caller
        # forgot to say so (fail safe).
        prefix = b'{"cache_control":{"type":"ephemeral"}}'
        marked = prefix + b"x" * (100000 - len(prefix))
        self.assertEqual(s.reservation("opus", marked, 48000), s.reservation("opus", large, 48000, cache_system=True))
        # Bytes bound tokens: multi-byte text only raises the bound.
        self.assertEqual(s.prompt_token_bound("opus", "é" * 10), 20 + 8192)
        self.assertEqual(s.prompt_token_bound("opus", b"x" * 2_000_000), 1_000_000)
        with self.assertRaises(RuntimeError):
            JudgeRouteSettings.defaults().reservation("opus", small, 1000)
        with self.assertRaises(ValueError):
            s.reasoning("opus", "medium")

    def test_prompt_cache_body_marks_system_block_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = make_ledger(folder)
            client = JudgeClient("opus", path, "analysis", Path(folder) / "a", settings=settings(prompt_cache=True),
                                 http=FakeTransport())
            body = client.body("rubric", "window", 1000, "high")
            self.assertEqual(body["messages"][0]["content"],
                             [{"type": "text", "text": "rubric", "cache_control": {"type": "ephemeral"}}])
            self.assertEqual(body["messages"][1], {"role": "user", "content": "window"})

    def test_frozen_record_loads_and_matches_observed_route(self):
        records = sorted((REPO / "data" / "validation").glob(f"{PRICING_PREFIX}-*.json"))
        self.assertTrue(records, "run scripts/freeze_judge_route.py")
        loaded = JudgeRouteSettings.from_pricing_record(records[-1], allow_stale=True)
        self.assertTrue(loaded.pricing_verified)
        observed = JudgeRouteSettings.defaults()
        self.assertEqual((loaded.opus, loaded.haiku), (observed.opus, observed.haiku))
        record = json.loads(records[-1].read_text())
        self.assertEqual((record["credential_used"], record["paid_calls"], record["provider_tag"]),
                         (False, 0, "anthropic"))
        self.assertEqual([e["tag"] for e in record["models"]["opus"]["excluded_service_tier_endpoints"]],
                         ["anthropic/fast"])

    def test_record_freshness_and_integrity(self):
        source = sorted((REPO / "data" / "validation").glob(f"{PRICING_PREFIX}-*.json"))[-1]
        record = json.loads(source.read_text())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "r.json"
            path.write_text(json.dumps(record))
            created = datetime.datetime.fromisoformat(record["created_at"])
            JudgeRouteSettings.from_pricing_record(path, now=created + datetime.timedelta(hours=71))
            with self.assertRaises(ValueError):
                JudgeRouteSettings.from_pricing_record(path, now=created + datetime.timedelta(hours=73))
            for mutate in (lambda r: r.update(all_required_passed=False), lambda r: r.update(credential_used=True),
                           lambda r: r["models"]["opus"]["endpoints"].clear(),
                           lambda r: r["models"]["haiku"]["catalog_entry"].update(id="anthropic/other")):
                broken = json.loads(json.dumps(record))
                mutate(broken)
                path.write_text(json.dumps(broken))
                with self.assertRaises(ValueError):
                    JudgeRouteSettings.from_pricing_record(path, allow_stale=True)

    def test_catalog_validation_rejects_unsafe_pricing(self):
        record = json.loads(sorted((REPO / "data" / "validation").glob(f"{PRICING_PREFIX}-*.json"))[-1].read_text())
        entry = record["models"]["opus"]
        for mutate in (lambda e: e[0]["pricing"].update(request="0.01"),
                       lambda e: e[0]["pricing"].update(discount=-0.1),
                       lambda e: e[0]["pricing"].update(internal_reasoning="1"),
                       lambda e: e[0].update(status=1),
                       lambda e: e[0].update(supported_parameters=["max_tokens"]),
                       lambda e: e[0].update(max_completion_tokens=32000)):
            endpoints = json.loads(json.dumps([e for e in entry["endpoints"] if e["tag"] == "anthropic"]))
            mutate(endpoints)
            route, problems = build_route("opus", entry["catalog_entry"], endpoints)
            self.assertIsNone(route)
            self.assertTrue(problems)
        # A regional standard endpoint at a higher price raises the reservation price.
        endpoints = json.loads(json.dumps(entry["endpoints"]))
        regional = json.loads(json.dumps([e for e in endpoints if e["tag"] == "anthropic"][0]))
        regional.update(tag="anthropic/us")
        regional["pricing"]["prompt"] = "0.0000044"
        route, problems = build_route("opus", entry["catalog_entry"], endpoints + [regional])
        self.assertEqual(problems, [])
        bound = settings(opus=route).price_bounds("opus", 1000)
        self.assertEqual(bound[0], Decimal("0.0000044"))


def newest_record():
    records = sorted((REPO / "data" / "validation").glob(f"{PRICING_PREFIX}-*.json"))
    return records[-1]


class ThreeModelTests(ClientCase):
    def test_models_efforts_and_default_concurrency(self):
        self.assertEqual(judge_route.MODEL_KEYS, ("haiku", "sonnet", "opus"))
        self.assertEqual(MODEL_IDS["sonnet"], "anthropic/claude-sonnet-5.5")
        self.assertEqual(EFFORTS, {"haiku": "low", "sonnet": "high", "opus": "high"})
        self.assertEqual(judge_route.MODEL_ALLOCATION["judge"], ("sonnet",))
        self.assertEqual(judge_route.MODEL_ALLOCATION["verifier"], ("opus",))
        s = settings()
        self.assertEqual(s.reasoning("sonnet", "high"), {"effort": "high", "exclude": False})
        for key, effort in (("sonnet", "low"), ("haiku", "high"), ("opus", "medium")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                s.reasoning(key, effort)
        for key, expected in (("haiku", 24), ("sonnet", 8), ("opus", 6)):
            client = self.client(FakeTransport(), key)
            self.assertEqual(client.max_concurrency, expected)
        with self.assertRaises(ValueError):
            self.client(FakeTransport(), "fable")

    def test_sonnet_judging_request_and_route_check(self):
        transport = FakeTransport([fake_completion('{"findings": []}', model="anthropic/claude-sonnet-5.5-20260928",
                                                   cost=".02"),
                                   fake_completion("{}", model=MODEL_IDS["opus"], cost=".01")])
        client = self.client(transport, "sonnet")
        ok = self.run_one(client, "sonnet")
        self.assertEqual((ok["status"], ok["cost_usd"]), ("ok", "0.02"))
        body = transport.requests[0]["body"]
        self.assertEqual((body["model"], body["reasoning"], body["provider"]["only"]),
                         (MODEL_IDS["sonnet"], {"effort": "high", "exclude": False}, ["anthropic"]))
        wrong = self.run_one(client, "sonnet", unit_id="other")
        self.assertEqual((wrong["status"], wrong["error"]["reason"]), ("grading_failed", "route_mismatch"))


class PromptCacheTests(ClientCase):
    WRITE_USAGE = {"prompt_tokens": 20000, "completion_tokens": 3000,
                   "completion_tokens_details": {"reasoning_tokens": 2000},
                   "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 15000},
                   "total_tokens": 23000}
    READ_USAGE = {"prompt_tokens": 20000, "completion_tokens": 3000,
                  "completion_tokens_details": {"reasoning_tokens": 2000},
                  "prompt_tokens_details": {"cached_tokens": 15000, "cache_write_tokens": 0},
                  "total_tokens": 23000}

    def request_records(self):
        return [json.loads(p.read_text()) for p in sorted(self.archive.rglob("*.request.json"),
                                                         key=lambda p: p.stat().st_mtime_ns)]

    def test_sonnet_and_opus_mark_the_system_prompt_by_default(self):
        for key in ("sonnet", "opus"):
            with self.subTest(key=key):
                transport = FakeTransport([fake_completion("{}", model=MODEL_IDS[key], usage=self.WRITE_USAGE)])
                result = self.run_one(self.client(transport, key), key, unit_id=f"cache-{key}")
                body = transport.requests[0]["body"]
                self.assertEqual(body["messages"][0], {"role": "system", "content": [
                    {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]})
                self.assertEqual(body["messages"][1], {"role": "user", "content": USER})  # never marked
                self.assertNotIn("cache_control", body)  # no top-level automatic caching
                self.assertNotIn('"ttl"', json.dumps(body))  # never the 1-hour TTL
                self.assertEqual(result["cache"]["system_marked"], True)
                self.assertEqual(result["cache"]["ttl"], "5m")

    def test_haiku_default_is_unmarked_and_callers_can_override(self):
        transport = FakeTransport(responder=lambda request: fake_completion("{}", model=request["body"]["model"]))
        haiku = self.client(transport, "haiku")
        self.run_one(haiku, "haiku", purpose="prefilter", unit_id="h1")
        self.assertEqual(transport.requests[-1]["body"]["messages"][0], {"role": "system", "content": SYSTEM})
        result = self.run_one(haiku, "haiku", purpose="prefilter", unit_id="h2", cache_system=True)
        self.assertIn("cache_control", transport.requests[-1]["body"]["messages"][0]["content"][0])
        self.assertTrue(result["cache"]["system_marked"])
        sonnet = self.client(transport, "sonnet")
        result = self.run_one(sonnet, "sonnet", unit_id="s1", cache_system=False)
        self.assertEqual(transport.requests[-1]["body"]["messages"][0], {"role": "system", "content": SYSTEM})
        self.assertEqual((result["cache"]["system_marked"], result["cache"]["ttl"]), (False, None))
        records = self.request_records()
        self.assertEqual([r["cache_system"] for r in records], [False, True, False])

    def test_cache_settings_override_and_validation(self):
        off = settings(prompt_cache=False)
        self.assertEqual({k: off.cache_system_default(k) for k in judge_route.MODEL_KEYS},
                         {"haiku": False, "sonnet": False, "opus": False})
        on = settings(prompt_cache=True)
        self.assertEqual({k: on.cache_system_default(k) for k in judge_route.MODEL_KEYS},
                         {"haiku": True, "sonnet": True, "opus": True})
        default = settings()
        self.assertEqual({k: default.cache_system_default(k) for k in judge_route.MODEL_KEYS},
                         judge_route.CACHE_SYSTEM_DEFAULTS)
        with self.assertRaises(ValueError):
            off.resolve_cache_system("opus", True)
        with self.assertRaises(ValueError):
            default.resolve_cache_system("opus", "yes")
        with self.assertRaises(ValueError):
            settings(prompt_cache="sometimes")
        # A route without cache prices never caches and refuses an explicit request.
        bare = judge_route._OBSERVED["opus"].__class__(**{**judge_route._OBSERVED["opus"].__dict__, "tiers": (
            judge_route.PriceTier(0, "0.000004", "0.00002"),)})
        uncached = settings(opus=bare)
        self.assertFalse(uncached.cache_system_default("opus"))
        with self.assertRaises(ValueError):
            uncached.resolve_cache_system("opus", True)
        client = self.client(FakeTransport(), "opus", settings=off)
        with self.assertRaises(ValueError):
            self.run_one(client, "opus", cache_system=True)
        client = self.client(FakeTransport(), "opus")
        with self.assertRaises(ValueError):  # nothing to mark
            self.run_one(client, "opus", system="", cache_system=True)
        self.assertEqual(client.body("", USER, 1000, "high")["messages"], [{"role": "user", "content": USER}])

    def test_reservation_conservative_and_cache_usage_recorded(self):
        transport = FakeTransport([fake_completion("{}", model=MODEL_IDS["sonnet"], usage=self.WRITE_USAGE,
                                                   cost="0.0975", cache_discount="-0.0075"),
                                   fake_completion("{}", model=MODEL_IDS["sonnet"], usage=self.READ_USAGE,
                                                   cost="0.0415", cache_discount="0.0285",
                                                   generation_id="gen-2")])
        client = self.client(transport, "sonnet")
        first = self.run_one(client, "sonnet", unit_id="A01", max_tokens=48000)
        second = self.run_one(client, "sonnet", unit_id="A02", max_tokens=48000)
        records = self.request_records()
        data = [json.dumps(r["body"], ensure_ascii=False, separators=(",", ":")) for r in records]
        s = settings()
        for record, body in zip(records, data):
            # Reserved at the cache-write price for every prompt token, never a discount,
            # and identical for a request that will read the cache.
            self.assertEqual(Decimal(record["reserved_usd"]), s.reservation("sonnet", body, 48000, cache_system=True))
            self.assertGreater(Decimal(record["reserved_usd"]), s.reservation("sonnet", body.replace(
                ',"cache_control":{"type":"ephemeral"}', ""), 48000))
            self.assertTrue(record["cache_system"])
        self.assertEqual(records[0]["reserved_usd"], records[1]["reserved_usd"])
        # Settlement uses OpenRouter's reported cost; the ledger records exactly that.
        self.assertEqual((first["cost_usd"], second["cost_usd"]), ("0.0975", "0.0415"))
        self.assertEqual(analysis(self.ledger_path)["committed_usd"], "0.139")
        # Usage totals and the cache section (frozen Sonnet prices: $2/M in, $10/M out,
        # $0.10/M read, $2.50/M write).
        self.assertEqual((first["usage"]["cache_write_tokens"], first["usage"]["cached_tokens"]), (15000, 0))
        cache = first["cache"]
        self.assertEqual((cache["write_tokens"], cache["read_tokens"]), (15000, 0))
        self.assertEqual(Decimal(cache["cache_write_usd"]), Decimal("0.0375"))
        self.assertEqual(Decimal(cache["priced_total_usd"]), Decimal("0.0375") + Decimal("0.01") + Decimal("0.03"))
        self.assertEqual(Decimal(cache["saving_usd"]), Decimal("-0.0075"))
        self.assertEqual(cache["reported_cache_discount_usd"], "-0.0075")
        cache = second["cache"]
        self.assertEqual((cache["read_tokens"], cache["write_tokens"]), (15000, 0))
        self.assertEqual(Decimal(cache["cache_read_usd"]), Decimal("0.0015"))
        self.assertEqual(Decimal(cache["priced_total_usd"]), Decimal("0.0015") + Decimal("0.01") + Decimal("0.03"))
        self.assertEqual(Decimal(cache["saving_usd"]), Decimal("0.0285"))
        self.assertEqual(cache["reported_cache_discount_usd"], "0.0285")
        attempt = cache["per_attempt"][0]
        self.assertEqual((attempt["attempt"], attempt["system_marked"], attempt["reported_cost_usd"]),
                         (1, True, "0.0415"))
        self.assertEqual(attempt["priced"]["uncached_prompt_tokens"], 5000)

    def test_refusal_retry_accumulates_cache_records(self):
        transport = FakeTransport([fake_completion("", finish_reason="content_filter", refusal="no",
                                                   model=MODEL_IDS["opus"], usage=self.WRITE_USAGE),
                                   fake_completion("{}", model=MODEL_IDS["opus"], usage=self.READ_USAGE)])
        result = self.run_one(self.client(transport, "opus"), "opus")
        self.assertEqual((result["status"], result["attempts"]), ("ok", 2))
        self.assertEqual((result["cache"]["write_tokens"], result["cache"]["read_tokens"]), (15000, 15000))
        self.assertEqual(len(result["cache"]["per_attempt"]), 2)
        self.assertIsNone(result["cache"]["reported_cache_discount_usd"])

    def test_priced_usage_tiers_and_missing_usage(self):
        s = settings()
        haiku = s.priced_usage("haiku", {"prompt_tokens": 120000, "completion_tokens": 1000,
                                         "prompt_tokens_details": {"cached_tokens": 100000}})
        self.assertEqual(haiku["tier_min_prompt_tokens"], 100000)
        self.assertEqual(Decimal(haiku["cache_read_usd"]), Decimal(100000) * Decimal("0.00000005"))
        self.assertEqual(Decimal(haiku["uncached_prompt_usd"]), Decimal(20000) * Decimal("0.0000005"))
        self.assertEqual(Decimal(haiku["completion_usd"]), Decimal(1000) * Decimal("0.0000025"))
        small = s.priced_usage("haiku", {"prompt_tokens": 9000, "completion_tokens": 100})
        self.assertEqual((small["tier_min_prompt_tokens"], small["cache_read_tokens"]), (0, 0))
        for usage in (None, {}, {"prompt_tokens": "1", "completion_tokens": 2}, {"prompt_tokens": True,
                                                                                  "completion_tokens": 1}):
            with self.subTest(usage=usage):
                self.assertIsNone(s.priced_usage("opus", usage))
        result = self.run_one(self.client(FakeTransport([fake_http_error(500)]), "opus"), "opus")
        self.assertIsNone(result["cache"]["per_attempt"][0]["priced"])


class CredentialsRootTests(ClientCase):
    def verified(self):
        return JudgeRouteSettings.from_pricing_record(newest_record(), allow_stale=True)

    def test_refused_on_fake_and_rehearsal_paths(self):
        folder = self.root / "main-checkout"
        folder.mkdir()
        for kwargs in ({"credentials_root": folder}, {"repo_root": folder},
                       {"credentials_root": folder, "repo_root": folder}):
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(ValueError):
                self.client(FakeTransport(), "sonnet", **kwargs)
        with self.assertRaises(ValueError):
            JudgeClient("haiku", self.root / "r.sqlite", "rehearsal", self.archive, settings=settings(),
                        http=FakeTransport(), ledger_caps={"rehearsal": "1"}, credentials_root=folder)
        with self.assertRaises(ValueError):  # exclusive with an explicit loader, even when paid
            JudgeClient("opus", self.ledger_path, "analysis", self.archive, settings=self.verified(),
                        paid_authorized=True, key_loader=lambda: FAKE_KEY, credentials_root=folder)

    def test_paid_path_reads_only_at_prepare_from_the_given_root(self):
        folder = self.root / "main-checkout"
        folder.mkdir()
        with self.assertRaises(ValueError):
            JudgeClient("opus", self.ledger_path, "analysis", self.archive, settings=self.verified(),
                        paid_authorized=True, credentials_root=self.root / "absent")
        loader = mock.Mock(return_value=FAKE_KEY)
        verified = []

        def verify(key, remaining, allow_unlimited=False):
            verified.append((key == FAKE_KEY, remaining, allow_unlimited))
            return {"limit": None, "unlimited_key_authorized": allow_unlimited}
        with mock.patch("research_swarm.secrets.load_openrouter_key", loader):
            client = JudgeClient("sonnet", self.ledger_path, "analysis", self.archive, settings=self.verified(),
                                 paid_authorized=True, credentials_root=folder, verify_limit=verify)
            self.assertIsInstance(client.http, judge_client.UrllibTransport)
            self.assertEqual(client.credentials_root, str(folder.resolve()))
            loader.assert_not_called()  # nothing is read before the first paid request
            self.assertIsNone(client._key)
            client.http = FakeTransport([fake_completion("{}", model=MODEL_IDS["sonnet"])])  # never the network
            result = self.run_one(client, "sonnet")
        loader.assert_called_once_with(folder.resolve())
        self.assertEqual(verified, [(True, Decimal("200"), True)])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(client.http.requests[0]["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        limit = json.loads((self.archive / "provider-limit-sonnet.json").read_text())
        self.assertEqual((limit["credentials_root"], limit["model_key"]), (str(folder.resolve()), "sonnet"))
        self.assertNotIn(FAKE_KEY, archived_text(self.archive))

    def test_default_root_is_this_checkout(self):
        loader = mock.Mock(return_value=FAKE_KEY)
        with mock.patch("research_swarm.secrets.load_openrouter_key", loader):
            client = JudgeClient("opus", self.ledger_path, "analysis", self.archive, settings=self.verified(),
                                 paid_authorized=True, verify_limit=lambda key, remaining, allow_unlimited: {})
            self.assertEqual(client.credentials_root, str(judge_client.REPO_ROOT.resolve()))
            client.prepare()
        loader.assert_called_once_with(judge_client.REPO_ROOT.resolve())

    def test_fake_paths_never_open_a_dotenv(self):
        import builtins
        import io
        import os
        import pathlib
        opened = []

        def guard(original):
            def wrapper(file, *args, **kwargs):
                name = os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else ""
                name = name.decode() if isinstance(name, bytes) else name
                if os.path.basename(name).startswith(".env"):
                    opened.append(name)
                    raise AssertionError(f"fake path opened {name}")
                return original(file, *args, **kwargs)
            return wrapper

        def path_guard(original):
            def wrapper(self, *args, **kwargs):
                if self.name.startswith(".env"):
                    opened.append(str(self))
                    raise AssertionError(f"fake path opened {self}")
                return original(self, *args, **kwargs)
            return wrapper
        (self.root / ".env").write_text("OPENROUTER_API_KEY=decoy-not-a-credential\n")  # decoy, must stay unread
        patches = [mock.patch.object(builtins, "open", guard(builtins.open)),
                   mock.patch.object(io, "open", guard(io.open)),
                   mock.patch.object(os, "open", guard(os.open)),
                   mock.patch.object(pathlib.Path, "open", path_guard(pathlib.Path.open)),
                   mock.patch.object(pathlib.Path, "read_text", path_guard(pathlib.Path.read_text)),
                   mock.patch.object(pathlib.Path, "read_bytes", path_guard(pathlib.Path.read_bytes))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        transport = FakeTransport(responder=lambda request: fake_completion("{}", model=request["body"]["model"]))
        for key in judge_route.MODEL_KEYS:
            client = self.client(transport, key)
            self.assertIsNone(client._key_loader)
            self.assertIsNone(client.credentials_root)
            self.assertIsNone(client.prepare())
            self.assertEqual(self.run_one(client, key, unit_id=f"fake-{key}")["status"], "ok")
        rehearsal = JudgeClient("sonnet", self.root / "rehearsal.sqlite", "rehearsal", self.archive,
                                settings=settings(), http=transport, ledger_caps={"rehearsal": "5"})
        self.assertEqual(self.run_one(rehearsal, "sonnet", unit_id="rehearsal")["status"], "ok")
        self.assertEqual(opened, [])
        self.assertTrue(all("Authorization" not in r["headers"] for r in transport.requests))


class FreezeRecordTests(unittest.TestCase):
    def test_newest_record_covers_three_models_with_effort_and_caching(self):
        record = json.loads(newest_record().read_text())
        self.assertEqual(set(record["models"]), {"haiku", "sonnet", "opus"})
        self.assertEqual((record["credential_used"], record["paid_calls"], record["all_required_passed"]),
                         (False, 0, True))
        self.assertEqual(record["cache_system_defaults"], {"haiku": False, "sonnet": True, "opus": True})
        for key in ("sonnet", "opus"):
            supported = record["models"][key]["supported"]
            self.assertTrue(supported["reasoning"] and supported["reasoning_effort"])
            self.assertTrue(supported["frozen_effort_supported"])
            self.assertEqual(record["models"][key]["effort"], "high")
            caching = supported["prompt_caching"]
            self.assertTrue(caching["endpoint_lists_cache_read_price"] and caching["endpoint_lists_cache_write_price"])
            self.assertEqual(caching["cache_control"], {"type": "ephemeral"})
            self.assertTrue(record["models"][key]["supports_prompt_cache"])
        self.assertEqual(record["models"]["sonnet"]["catalog_entry"]["canonical_slug"],
                         "anthropic/claude-sonnet-5.5-20260928")
        loaded = JudgeRouteSettings.from_pricing_record(newest_record(), allow_stale=True)
        self.assertEqual(loaded.sonnet, JudgeRouteSettings.defaults().sonnet)
        self.assertIsNone(loaded.prompt_cache)

    def test_two_model_record_is_refused(self):
        old = REPO / "data" / "validation" / "judge-route-pricing-20261018T003538Z.json"
        if not old.exists():
            self.skipTest("historical two-model record absent")
        with self.assertRaises(ValueError) as caught:
            JudgeRouteSettings.from_pricing_record(old, allow_stale=True)
        self.assertIn("lacks", str(caught.exception))

    def test_catalog_validation_requires_effort_control_and_cache_prices(self):
        record = json.loads(newest_record().read_text())
        entry = record["models"]["sonnet"]
        endpoints = [e for e in entry["endpoints"] if e["tag"] == "anthropic"]
        route, problems = build_route("sonnet", entry["catalog_entry"], endpoints)
        self.assertEqual(problems, [])
        self.assertTrue(route.supports_prompt_cache)
        for mutate_endpoint, mutate_catalog in (
                (lambda e: e[0]["pricing"].pop("input_cache_write"), None),
                (lambda e: e[0]["pricing"].pop("input_cache_read"), None),
                (lambda e: e[0]["supported_parameters"].remove("reasoning_effort"), None),
                (None, lambda c: c["reasoning"].update(supported_efforts=["low", "medium"])),
                (None, lambda c: c["reasoning"].pop("supported_efforts"))):
            changed = json.loads(json.dumps(endpoints))
            catalog = json.loads(json.dumps(entry["catalog_entry"]))
            if mutate_endpoint:
                mutate_endpoint(changed)
            if mutate_catalog:
                mutate_catalog(catalog)
            route, problems = build_route("sonnet", catalog, changed)
            self.assertIsNone(route)
            self.assertTrue(problems)
        # Haiku does not cache by default, so missing cache prices only disable caching.
        haiku = record["models"]["haiku"]
        changed = json.loads(json.dumps([e for e in haiku["endpoints"] if e["tag"] == "anthropic"]))
        for entry_pricing in [changed[0]["pricing"], *changed[0]["pricing"]["overrides"]]:
            entry_pricing.pop("input_cache_write")
        route, problems = build_route("haiku", haiku["catalog_entry"], changed)
        self.assertEqual(problems, [])
        self.assertFalse(route.supports_prompt_cache)


if __name__ == "__main__":
    unittest.main()
