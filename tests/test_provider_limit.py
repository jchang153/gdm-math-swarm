"""Offline tests for the live provider-side key-limit check (SPEND-6, interface X2).

Every HTTP call goes to a fake ``http_get`` (or a patched ``urlopen``); the key is a
fake string and the host credential loader is patched. No network, no .env.
"""
import datetime
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

from research_swarm import provider_limit
from research_swarm.provider_limit import KEY_URL, ProviderLimitError, verify_key_limit

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.datetime(2026, 10, 14, 12, 0, tzinfo=datetime.timezone.utc)
FAKE_KEY = "test-openrouter-key-not-real-0123456789"
GOOD = {"label": "sk-or-v1-abc...xyz", "limit": 20, "limit_remaining": 20, "limit_reset": None, "usage": 0,
        "is_free_tier": False, "include_byok_in_limit": True, "expires_at": None}


def no_network(*args, **kwargs):
    raise AssertionError("network access")


class FakeGet:
    def __init__(self, data=None, body=None, error=None):
        self.body = body if body is not None else json.dumps({"data": data}).encode()
        self.error, self.calls = error, []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        if self.error:
            raise self.error
        return self.body


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"pl_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@mock.patch("urllib.request.urlopen", no_network)
class VerifyKeyLimitTests(unittest.TestCase):
    def verify(self, data, cap="20", **kwargs):
        get = FakeGet(data, **kwargs)
        return verify_key_limit(FAKE_KEY, Decimal(cap), http_get=get, now=NOW), get

    def test_passing_key_returns_a_key_free_report(self):
        report, get = self.verify(GOOD)
        self.assertEqual(get.calls, [(KEY_URL, {"Authorization": "Bearer " + FAKE_KEY})])
        self.assertTrue(report["all_required_passed"])
        self.assertEqual((report["limit"], report["limit_remaining"], report["limit_reset"], report["max_remaining_usd"]),
                         (20, 20, None, "20"))
        self.assertEqual(report["paid_calls"], 0)
        text = json.dumps(report)
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn("label", report)
        self.assertNotIn("sk-", text)
        self.assertNotIn("Authorization", text)

    def test_refusals(self):
        cases = {
            "limit_set": dict(GOOD, limit=None, limit_remaining=None),
            "limit_does_not_reset": dict(GOOD, limit_reset="monthly"),
            "remaining_within_allowance": dict(GOOD, limit=25, limit_remaining=20.5),
            "key_not_expired": dict(GOOD, expires_at="2026-10-01T00:00:00Z"),
        }
        for check, data in cases.items():
            with self.subTest(check), self.assertRaises(ProviderLimitError) as caught:
                self.verify(data)
            self.assertIn(check, str(caught.exception))
            self.assertFalse(caught.exception.report["checks"][check]["passed"])
            self.assertNotIn(FAKE_KEY, str(caught.exception) + json.dumps(caught.exception.report))
        with self.assertRaises(ProviderLimitError):  # limit set but no remaining figure: fail closed
            self.verify(dict(GOOD, limit_remaining=None))

    def test_remaining_compared_with_the_scope_allowance_and_tolerance(self):
        self.assertTrue(self.verify(dict(GOOD, limit=2, limit_remaining=2), cap="2")[0]["all_required_passed"])
        self.assertTrue(self.verify(dict(GOOD, limit_remaining="2.000000001"), cap="2")[0]["all_required_passed"])
        with self.assertRaises(ProviderLimitError):
            self.verify(dict(GOOD, limit_remaining="2.000000002"), cap="2")
        with self.assertRaises(ProviderLimitError):  # a $20 pilot key is refused for the $2 compatibility check
            self.verify(GOOD, cap="2")
        report, _ = self.verify(dict(GOOD, limit=22, limit_remaining=19.5, usage=2.5), cap="19.75")
        self.assertEqual(len(report["warnings"]), 2)  # prior usage and a limit above the allowance

    def test_query_failures_fail_closed_without_the_key(self):
        error = OSError(f"connection reset while sending Bearer {FAKE_KEY}")
        with self.assertRaises(ProviderLimitError) as caught:
            self.verify(None, error=error)
        self.assertIn("could not query", str(caught.exception))
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        for body in (b"not json", json.dumps({"error": "x"}).encode(), json.dumps([1]).encode()):
            with self.subTest(body=body), self.assertRaises(ProviderLimitError):
                self.verify(None, body=body)
        get = FakeGet(GOOD)
        for key in ("", "   ", None):
            with self.assertRaises(ProviderLimitError):
                verify_key_limit(key, Decimal("20"), http_get=get, now=NOW)
        self.assertEqual(get.calls, [])

    def test_credential_like_response_is_not_recorded(self):
        with self.assertRaises(ProviderLimitError) as caught:
            self.verify(dict(GOOD, expires_at="sk-or-v1-0123456789abcdef"))
        self.assertIn("credential-like", str(caught.exception))
        self.assertIsNone(caught.exception.report)

    def test_default_get_uses_urllib_with_the_header(self):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization"), timeout))
            return Response(json.dumps({"data": GOOD}).encode())
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            report = verify_key_limit(FAKE_KEY, Decimal("20"), now=NOW)
        self.assertTrue(report["all_required_passed"])
        self.assertEqual(seen, [(KEY_URL, "Bearer " + FAKE_KEY, provider_limit.TIMEOUT_SECONDS)])


@mock.patch("urllib.request.urlopen", no_network)
class CheckProviderLimitScriptTests(unittest.TestCase):
    def run_script(self, data, *argv):
        module = load_script("check_provider_limit")
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("research_swarm.secrets.load_openrouter_key", return_value=FAKE_KEY), \
                redirect_stdout(io.StringIO()) as out:
            code = module.main(["--i-have-user-approval", "--output-dir", folder, *argv], http_get=FakeGet(data))
            files = list(Path(folder).glob("provider-limit-*.json"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text()
        self.assertNotIn(FAKE_KEY, text + out.getvalue())
        self.assertNotIn("sk-", text)
        return code, json.loads(text)

    def test_writes_key_free_evidence_through_the_shared_assessment(self):
        code, record = self.run_script(GOOD)
        self.assertEqual(code, 0)
        self.assertTrue(record["all_required_passed"])
        self.assertEqual((record["credential_used"], record["credential_recorded"], record["paid_calls"]),
                         (True, False, 0))
        self.assertEqual(record["endpoint"], KEY_URL)
        self.assertNotIn("label", record)
        code, record = self.run_script(GOOD, "--max-limit-usd", "2")  # a compatibility key must be at most $2
        self.assertEqual(code, 1)
        self.assertFalse(record["checks"]["remaining_within_allowance"]["passed"])
        code, record = self.run_script(dict(GOOD, limit_reset="daily"))
        self.assertEqual(code, 1)
        self.assertFalse(record["all_required_passed"])

    def test_launch_gate_accepts_the_written_evidence_shape(self):
        from research_swarm import launch_gates
        module = load_script("check_provider_limit")
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("research_swarm.secrets.load_openrouter_key", return_value=FAKE_KEY), \
                redirect_stdout(io.StringIO()):
            module.main(["--i-have-user-approval", "--output-dir", folder], http_get=FakeGet(GOOD))
            gates = launch_gates._Gates(ROOT, {}, folder, datetime.datetime.now(datetime.timezone.utc),
                                        lambda tag: None)
            gates.provider_limit("20")
        self.assertTrue(gates.results["provider_limit"]["passed"], gates.results["provider_limit"])


if __name__ == "__main__":
    unittest.main()
