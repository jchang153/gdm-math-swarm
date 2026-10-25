"""Offline tests for the informational provider-side key-limit record (interface X2).

Owner decision (docs/plan.md "One per-run budget, no manual approvals (October 20, 2026)"):
the record never refuses a key; the run's budget is the spending control. Every HTTP call goes
to a fake ``http_get`` (or a patched ``urlopen``); the key is a fake string and the host
credential loader is patched. No network, no .env.
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
from research_swarm.provider_limit import KEY_URL, ProviderLimitError, fetch_key_data, key_limit_record

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
class KeyLimitRecordTests(unittest.TestCase):
    def record(self, data, budget="20", **kwargs):
        get = FakeGet(data, **kwargs)
        return key_limit_record(FAKE_KEY, None if budget is None else Decimal(budget), http_get=get, now=NOW), get

    def test_record_is_key_free(self):
        report, get = self.record(GOOD)
        self.assertEqual(get.calls, [(KEY_URL, {"Authorization": "Bearer " + FAKE_KEY})])
        self.assertTrue(report["all_required_passed"])
        self.assertTrue(report["informational"])
        self.assertEqual((report["limit"], report["limit_remaining"], report["limit_reset"], report["run_budget_usd"]),
                         (20, 20, None, "20"))
        self.assertEqual((report["paid_calls"], report["warnings"]), (0, []))
        text = json.dumps(report)
        for forbidden in (FAKE_KEY, "label", "Authorization", "sk-"):
            self.assertNotIn(forbidden, text)

    def test_nothing_is_ever_refused_only_warned(self):
        cases = {
            "limit_set": dict(GOOD, limit=None, limit_remaining=None),
            "limit_does_not_reset": dict(GOOD, limit_reset="monthly"),
            "remaining_within_run_budget": dict(GOOD, limit=250, limit_remaining=200),
            "key_not_expired": dict(GOOD, expires_at="2026-10-01T00:00:00Z"),
        }
        for check, data in cases.items():
            with self.subTest(check):
                report, _ = self.record(data)
                self.assertTrue(report["all_required_passed"])
                self.assertFalse(report["checks"][check]["passed"])
                self.assertTrue(report["warnings"])
        report, _ = self.record(dict(GOOD, limit=250, limit_remaining=200), budget="100")
        self.assertIn("above this run's budget", " ".join(report["warnings"]))
        report, _ = self.record(dict(GOOD, limit_remaining=5, usage=15), budget="100")
        self.assertIn("provider would stop the run first", " ".join(report["warnings"]))
        report, _ = self.record(GOOD, budget=None)
        self.assertNotIn("remaining_within_run_budget", report["checks"])

    def test_query_failures_are_recorded_not_raised(self):
        error = OSError(f"connection reset while sending Bearer {FAKE_KEY}")
        report, _ = self.record(None, error=error)
        self.assertIn("could not query", report["error"])
        self.assertTrue(report["warnings"])
        self.assertNotIn(FAKE_KEY, json.dumps(report))
        for body in (b"not json", json.dumps({"error": "x"}).encode(), json.dumps([1]).encode()):
            with self.subTest(body=body):
                self.assertIn("error", self.record(None, body=body)[0])
        get = FakeGet(GOOD)
        for key in ("", "   ", None):
            self.assertIn("error", key_limit_record(key, Decimal("20"), http_get=get, now=NOW))
            with self.assertRaises(ProviderLimitError):
                fetch_key_data(key, http_get=get)
        self.assertEqual(get.calls, [])

    def test_credential_like_response_is_not_recorded(self):
        report, _ = self.record(dict(GOOD, expires_at="sk-or-v1-0123456789abcdef"))
        self.assertIn("credential-like", report["error"])
        self.assertNotIn("sk-or", json.dumps(report))
        report, _ = self.record(dict(GOOD, usage=FAKE_KEY))
        self.assertNotIn(FAKE_KEY, json.dumps(report))

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
            report = key_limit_record(FAKE_KEY, Decimal("20"), now=NOW)
        self.assertEqual(report["limit"], 20)
        self.assertEqual(seen, [(KEY_URL, "Bearer " + FAKE_KEY, provider_limit.TIMEOUT_SECONDS)])

    def test_write_record_never_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            path = provider_limit.write_record(folder, self.record(GOOD)[0], source="test")
            record = json.loads(Path(path).read_text())
        self.assertEqual((record["source"], record["credential_recorded"]), ("test", False))
        self.assertIsNone(provider_limit.write_record("/dev/null/not-a-dir", {}, source="test"))


@mock.patch("urllib.request.urlopen", no_network)
class CheckProviderLimitScriptTests(unittest.TestCase):
    def run_script(self, data, *argv):
        module = load_script("check_provider_limit")
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("research_swarm.secrets.load_openrouter_key", return_value=FAKE_KEY), \
                redirect_stdout(io.StringIO()) as out:
            code = module.main(["--output-dir", folder, *argv], http_get=FakeGet(data))
            files = list(Path(folder).glob("provider-limit-*.json"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text()
        self.assertNotIn(FAKE_KEY, text + out.getvalue())
        self.assertNotIn("sk-", text)
        return code, json.loads(text)

    def test_writes_key_free_record_without_any_flag(self):
        code, record = self.run_script(GOOD)
        self.assertEqual(code, 0)
        self.assertTrue(record["all_required_passed"])
        self.assertEqual((record["credential_used"], record["credential_recorded"], record["paid_calls"]),
                         (True, False, 0))
        self.assertEqual((record["endpoint"], record["run_budget_usd"]), (KEY_URL, "100"))  # pilot.json budget
        self.assertNotIn("label", record)
        for data in (dict(GOOD, limit=None, limit_remaining=None), dict(GOOD, limit_reset="daily")):
            code, record = self.run_script(data)
            self.assertEqual(code, 0)
            self.assertTrue(record["warnings"])

    def test_compares_with_the_configs_run_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "small.json"
            config.write_text(json.dumps({"run_budget_usd": "5"}))
            code, record = self.run_script(GOOD, "--config", str(config))
        self.assertEqual((code, record["run_budget_usd"]), (0, "5"))
        self.assertFalse(record["checks"]["remaining_within_run_budget"]["passed"])

    def test_launch_gate_reports_the_written_record(self):
        from research_swarm import launch_gates
        module = load_script("check_provider_limit")
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("research_swarm.secrets.load_openrouter_key", return_value=FAKE_KEY), \
                redirect_stdout(io.StringIO()):
            module.main(["--output-dir", folder], http_get=FakeGet(dict(GOOD, limit=None, limit_remaining=None)))
            gates = launch_gates._Gates(ROOT, {}, folder, datetime.datetime.now(datetime.timezone.utc),
                                        lambda tag: None)
            gates.provider_limit()
        result = gates.results["provider_limit"]
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()
