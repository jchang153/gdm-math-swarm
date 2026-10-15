"""Offline tests of scripts/compat_check.py (no Docker, no network, no credential).

The paid run itself is replaced by fakes; these tests cover the refusal paths, the
abort guard around the production budget filter, and the evidence/check computation
of the shared interface read by the launch gate ``compatibility``.
"""
import asyncio
import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import compat_check as cc  # noqa: E402
from research_swarm import runtime  # noqa: E402
from research_swarm.budget import BudgetLedger  # noqa: E402
from research_swarm.provider import frozen_fields, settings_from_config  # noqa: E402

try:
    import inspect_ai  # noqa: F401
    HAVE_INSPECT = True
except ImportError:
    HAVE_INSPECT = False

CONFIG = json.loads((ROOT / "configs" / "pilot.json").read_text())
SETTINGS = settings_from_config(CONFIG, pricing_verified=True)
PROVIDER_NAME = {"deepseek": "DeepSeek", "fireworks": "Fireworks"}.get(
    SETTINGS.provider_route, SETTINGS.provider_route)
OTHER_PROVIDER_NAME = "DeepSeek" if SETTINGS.provider_route != "deepseek" else "Fireworks"
IMAGES = {"agent": {"tag": "gdm-math-swarm-agent:local", "id": "sha256:" + "a" * 64},
          "lean": {"tag": "gdm-math-swarm-lean:local", "id": "sha256:" + "b" * 64}}


def finished(request_id, *, basis="provider_reported_cost", completion=300, reasoning=200, name=PROVIDER_NAME,
             model=SETTINGS.model_id, error=None, agent="agent-001", stop_reason="tool_calls"):
    return {"kind": "inference_finished", "agent_id": agent, "payload": {
        "request_id": request_id, "settled_usd": "0.001", "basis": basis,
        "usage": {"output_tokens": completion, "reasoning_tokens": reasoning},
        "provider_usage": {"completion_tokens": completion, "completion_tokens_details": {"reasoning_tokens": reasoning},
                           "cost": 0.001},
        "provider_generation_id": "gen-" + request_id, "provider_name": name, "provider_model": model,
        "stop_reason": stop_reason, "error": error}}


def admitted(request_id, replayed=0, agent="agent-001"):
    return {"kind": "inference_admitted", "agent_id": agent,
            "payload": {"request_id": request_id, "reserved_usd": "0.05", "request_bytes": 1000,
                        "replayed_reasoning_blocks": replayed}}


def probe_events(request_id="p1", cap=cc.DEFAULT_CAP_PROBE_MAX_TOKENS, **changes):
    """The reasoning-cap probe's runtime events: truncated at the cap with reasoning reported."""
    options = dict(completion=cap, reasoning=cap - 24, stop_reason="max_tokens")
    options.update(changes)
    return [{"kind": "reasoning_cap_probe_started", "agent_id": cc.CAP_PROBE_AGENT, "payload": {"max_tokens": cap}},
            admitted(request_id, agent=cc.CAP_PROBE_AGENT),
            finished(request_id, agent=cc.CAP_PROBE_AGENT, **options)]


def passing_inputs():
    events = probe_events() + [admitted("r1"), finished("r1"), admitted("r2", replayed=1), finished("r2")]
    conference_events = [{"seq": 1, "kind": "board_viewed", "agent_id": "agent-001", "tool": "get_bulletin_board",
                          "payload": {}}]
    activity = {"tool_calls": [{"id": "c1", "function": "Bash", "parse_error": None},
                               {"id": "c2", "function": "mcp__conference__get_bulletin_board", "parse_error": None}],
                "tool_results": {"c1": {"function": "Bash", "text": "Erdos6.lean\n", "error": None},
                                 "c2": {"function": "mcp__conference__get_bulletin_board",
                                        "text": json.dumps({"ok": True, "posts": []}), "error": None}},
                "model_errors": [], "compaction_events": 0, "sample_error": None, "sample_limit": None}
    return {"agent_id": "agent-001", "events": events, "conference_events": conference_events, "activity": activity,
            "settings": SETTINGS, "abort_reason": "max_requests", "violation_ids": []}


def failed(checks):
    return sorted(k for k, v in checks.items() if v["required"] and not v["passed"])


class BuildChecksTests(unittest.TestCase):
    def checks(self, **changes):
        inputs = copy.deepcopy(passing_inputs())
        inputs["settings"] = SETTINGS
        inputs.update(changes)
        return cc.build_checks(**inputs)

    def test_passing_run(self):
        checks = self.checks()
        self.assertEqual(failed(checks), [])
        self.assertEqual(set(cc.REQUIRED_CHECKS) | set(cc.OPTIONAL_CHECKS), set(checks))
        for name in cc.REQUIRED_CHECKS:
            self.assertTrue(checks[name]["required"], name)
            self.assertIsInstance(checks[name]["detail"], str)
        for name in cc.OPTIONAL_CHECKS:
            self.assertFalse(checks[name]["required"])

    def test_tool_call_failures(self):
        inputs = passing_inputs()
        no_bash = copy.deepcopy(inputs["activity"])
        no_bash["tool_calls"] = no_bash["tool_calls"][1:]
        self.assertEqual(failed(self.checks(activity=no_bash)), ["tool_call_parsed"])
        rejected = copy.deepcopy(inputs["activity"])
        rejected["tool_results"]["c1"]["text"] = "<tool_use_error>InputValidationError: bad</tool_use_error>"
        self.assertEqual(failed(self.checks(activity=rejected)), ["tool_call_parsed"])
        parse = copy.deepcopy(inputs["activity"])
        parse["tool_calls"].append({"id": "c3", "function": "Bash", "parse_error": "Expecting value"})
        self.assertEqual(failed(self.checks(activity=parse)), ["tool_call_parsed"])

    def test_conference_call_must_be_identity_bound(self):
        others = [{"seq": 1, "kind": "board_viewed", "agent_id": "agent-002", "tool": "get_bulletin_board"}]
        self.assertEqual(failed(self.checks(conference_events=others)), ["conference_tool_call"])
        not_ok = copy.deepcopy(passing_inputs()["activity"])
        not_ok["tool_results"]["c2"]["text"] = json.dumps({"ok": False, "error": "x"})
        self.assertEqual(failed(self.checks(activity=not_ok)), ["conference_tool_call"])

    def test_reasoning_replay(self):
        events = probe_events() + [admitted("r1"), finished("r1"), admitted("r2"), finished("r2")]
        self.assertEqual(failed(self.checks(events=events)), ["reasoning_replay_accepted"])
        # replayed request never finished
        rejected = probe_events() + [admitted("r1"), finished("r1"), admitted("r2", replayed=2)]
        self.assertIn("reasoning_replay_accepted", failed(self.checks(events=rejected)))

    def test_cost_tokens_route_and_violation(self):
        probe = probe_events()
        events = probe + [admitted("r1"), finished("r1", basis="usage_priced_upper_bound"), admitted("r2", 1),
                          finished("r2")]
        self.assertEqual(failed(self.checks(events=events)), ["provider_cost_reported"])
        limit = SETTINGS.max_tokens
        events = probe + [admitted("r1"), finished("r1", completion=limit + 1, reasoning=10), admitted("r2", 1),
                          finished("r2")]
        self.assertEqual(failed(self.checks(events=events)), ["completion_within_max_tokens"])
        # Reasoning reported separately from (above) completion counts in addition.
        events = probe + [admitted("r1"), finished("r1", completion=100, reasoning=limit), admitted("r2", 1),
                          finished("r2")]
        self.assertEqual(failed(self.checks(events=events)), ["completion_within_max_tokens"])
        events = probe + [admitted("r1"), finished("r1", name=OTHER_PROVIDER_NAME), admitted("r2", 1), finished("r2")]
        self.assertEqual(failed(self.checks(events=events)), ["route_pinned"])
        events = probe + [admitted("r1"), finished("r1", model="deepseek/deepseek-v3"), admitted("r2", 1),
                          finished("r2")]
        self.assertEqual(failed(self.checks(events=events)), ["route_pinned"])
        events = probe + [admitted("r1"), finished("r1", model="deepseek/deepseek-v4.1-flash-20260901"),
                          admitted("r2", 1), finished("r2")]
        self.assertEqual(failed(self.checks(events=events)), [])
        self.assertEqual(failed(self.checks(violation_ids=["r1"])), ["no_estimate_violation"])
        self.assertEqual(failed(self.checks(abort_reason="estimate_violation")), ["no_estimate_violation"])

    def test_reasoning_cap_probe(self):
        """SPEND-4: the cap probe must show truncation at max_tokens with reasoning counted and billed within it."""
        agent = passing_inputs()["events"][3:]
        cap = cc.DEFAULT_CAP_PROBE_MAX_TOKENS
        cases = {
            "missing": [],
            "not truncated (inconclusive)": probe_events(stop_reason="stop", completion=400, reasoning=300),
            "generated above the cap": probe_events(completion=cap + 1, reasoning=cap - 1),
            "reasoning reported separately and above the cap": probe_events(completion=100, reasoning=cap),
            "no reasoning tokens reported": probe_events(reasoning=0),
            "no provider-reported cost": probe_events(basis="usage_priced_upper_bound"),
            "served by another provider": probe_events(name=OTHER_PROVIDER_NAME),
            "provider error output": probe_events(error="upstream"),
            "request raised": probe_events()[:2] + [{"kind": "inference_uncertain", "agent_id": cc.CAP_PROBE_AGENT,
                                                    "payload": {"request_id": "p1", "reason": "connection_lost"}}],
        }
        for name, probe in cases.items():
            checks = self.checks(events=probe + agent)
            self.assertEqual(failed(checks), ["reasoning_cap_enforced"], name)
        # Billed above the probe's reservation (estimate_violation of the probe request).
        self.assertIn("reasoning_cap_enforced", failed(self.checks(violation_ids=["p1"])))
        passed = self.checks()["reasoning_cap_enforced"]
        self.assertTrue(passed["passed"] and passed["required"])
        self.assertIn(f"probe max_tokens {cap}", passed["detail"])
        self.assertIn("max_tokens", passed["detail"])
        # The probe's events never count as the agent's own requests.
        only_probe = self.checks(events=probe_events())
        self.assertFalse(only_probe["provider_cost_reported"]["passed"])
        self.assertTrue(only_probe["reasoning_cap_enforced"]["passed"])

    def test_sample_errors_and_failure_aborts(self):
        errored = copy.deepcopy(passing_inputs()["activity"])
        errored["sample_error"] = "RuntimeError: boom"
        self.assertEqual(failed(self.checks(activity=errored)), ["no_sample_error"])
        self.assertEqual(failed(self.checks(abort_reason="provider_4xx")), ["no_sample_error"])
        checks = self.checks(abort_reason="provider_rejection")
        self.assertEqual(failed(checks), ["no_sample_error"])
        evidence = cc.build_evidence(checks=checks, images=IMAGES, settings=SETTINGS, spent_usd=0, request_ids=[])
        self.assertFalse(evidence["all_required_passed"])
        for normal in (None, "max_requests", "max_minutes"):
            self.assertEqual(failed(self.checks(abort_reason=normal)), [])

    def test_no_inference_fails_closed(self):
        checks = self.checks(events=[])
        for name in ("provider_cost_reported", "completion_within_max_tokens", "route_pinned",
                     "reasoning_replay_accepted"):
            self.assertFalse(checks[name]["passed"], name)

    def test_recovered_transient_model_error_can_pass_but_terminal_unknown_errors_cannot(self):
        inputs = passing_inputs()
        message = "Error code: 429 - {'error': 'rate limit'}"
        activity = copy.deepcopy(inputs["activity"])
        activity["model_errors"] = [message]
        transient = {"kind": "compatibility_transient_error", "agent_id": "agent-001",
                     "payload": {"error": message, "status": 429, "reason": "provider_rejected_before_generation"}}
        # Old successful turns are insufficient: this error must recover later.
        events = inputs["events"] + [transient]
        self.assertEqual(failed(self.checks(events=events, activity=activity)), ["no_sample_error"])
        events += [admitted("retry"), finished("retry")]
        checks = self.checks(events=events, activity=activity)
        self.assertEqual(failed(checks), [])
        self.assertTrue(cc.build_evidence(checks=checks, images=IMAGES, settings=SETTINGS,
                                          spent_usd=0, request_ids=[])["all_required_passed"])
        for name, changed_events, changed_activity in (
                ("missing transient record", inputs["events"] + [finished("retry")], activity),
                ("other agent recovery", inputs["events"] + [transient, finished("retry", agent="agent-002")], activity),
                ("error output", inputs["events"] + [transient, finished("retry", error="bad")], activity),
                ("unpriced output", inputs["events"] + [transient, finished("retry", basis="unbilled_error")], activity),
                ("extra error occurrence", events, dict(activity, model_errors=[message, message])),
                ("unknown error", events, dict(activity, model_errors=[message, "unexpected error"])),
                ("terminal sample failure", events, dict(activity, sample_error="retries exhausted"))):
            with self.subTest(name=name):
                self.assertIn("no_sample_error", failed(self.checks(events=changed_events, activity=changed_activity)))

    def test_optional_observations(self):
        events = passing_inputs()["events"] + [
            {"kind": "truncated_response", "agent_id": "agent-001", "payload": {"stop_reason": "max_tokens"}},
            {"kind": "compaction_request", "agent_id": "agent-001", "payload": {"count": 1}}]
        checks = self.checks(events=events)
        self.assertTrue(checks["truncation_observed"]["passed"])
        self.assertTrue(checks["compaction_observed"]["passed"])
        self.assertEqual(failed(checks), [])


def _gate_supports_request_ids():
    from research_swarm import launch_gates
    return hasattr(getattr(launch_gates, "_Gates", None), "compatibility_requests")


class EvidenceTests(unittest.TestCase):
    def test_shared_interface_fields(self):
        checks = cc.build_checks(**passing_inputs())
        record = cc.build_evidence(checks=checks, images=IMAGES, settings=SETTINGS, spent_usd=Decimal("0.0123"),
                                   request_ids=["r1", "r2", "r1"], abort_reason="max_requests")
        for key in ("schema", "created_at", "all_required_passed", "image_ids", "model_id", "provider_route",
                    "settings", "spent_usd", "requests", "request_ids", "checks"):
            self.assertIn(key, record)
        self.assertEqual(record["schema"], "gdm-compatibility/1")
        self.assertRegex(record["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(record["all_required_passed"])
        self.assertTrue(record["checks"]["reasoning_cap_enforced"]["required"])
        self.assertEqual(record["image_ids"], {"agent": IMAGES["agent"]["id"], "lean": IMAGES["lean"]["id"]})
        self.assertEqual((record["model_id"], record["provider_route"]), (SETTINGS.model_id, SETTINGS.provider_route))
        # Equal to what the launch gate rebuilds from the config.
        self.assertEqual(record["settings"], frozen_fields(settings_from_config(CONFIG)))
        # X4: request_ids de-duplicated in admission order; requests is their count.
        self.assertEqual((record["spent_usd"], record["requests"], record["request_ids"]), ("0.0123", 2, ["r1", "r2"]))
        json.dumps(record)  # serializable as written

    def _gate(self, folder, record):
        from research_swarm import launch_gates
        cc.write_evidence(folder / "validation", cc.utc_stamp(), record)
        ids = {CONFIG["agent_image"]: IMAGES["agent"]["id"], CONFIG["lean_image"]: IMAGES["lean"]["id"]}
        report = launch_gates.evaluate(CONFIG, ROOT, validation_dir=folder / "validation",
                                       ledger_path=folder / "budget.sqlite",
                                       legacy_ledger_path=folder / "absent.sqlite", image_id=ids.get)
        return report["evidence"]["compatibility"]

    def test_launch_gate_accepts_the_written_evidence(self):
        # Writer (this script) against the reader (launch_gates "compatibility" gate).
        from research_swarm import launch_gates
        if not hasattr(launch_gates, "COMPATIBILITY_SCHEMA"):
            self.skipTest("launch_gates has no compatibility gate yet")
        self.assertIn("reasoning_cap_enforced", cc.REQUIRED_CHECKS)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            ledger = BudgetLedger(folder / "budget.sqlite")
            request = ledger.reserve("compatibility", "agent-001", Decimal("0.05"))
            ledger.settle(request, Decimal("0.0123"))
            record = cc.build_evidence(checks=cc.build_checks(**passing_inputs()), images=IMAGES, settings=SETTINGS,
                                       spent_usd=Decimal("0.0123"), request_ids=[request])
            gate = self._gate(folder, record)
        self.assertTrue(gate["passed"], gate["reason"])

    def test_reconcile_then_gate(self):
        """SPEND-1: an uncertain hold reconciled after the run (lower actual cost) must not block the gate."""
        if not _gate_supports_request_ids():
            self.skipTest("launch_gates does not read request_ids yet (X4 reader)")
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            path = folder / "budget.sqlite"
            ledger = BudgetLedger(path)
            ledger.reserve("pilot", "agent-001", Decimal("0.5"))  # another scope: never part of this evidence
            settled = ledger.reserve("compatibility", "agent-001", Decimal("0.05"))
            ledger.settle(settled, Decimal("0.0123"))
            held = ledger.reserve("compatibility", "agent-001", Decimal("0.064"))
            ledger.settle(held, None)  # uncertain at run end (e.g. cancelled in flight)
            request_ids = [settled, held]
            rows = cc.request_rows(path, request_ids)
            spent = cc.committed_usd(rows.values())
            self.assertEqual(spent, Decimal("0.0763"))  # uncertain hold counted at its full reservation
            inputs = passing_inputs()
            inputs["activity"]["model_errors"] = ["temporary HTTP 500"]
            inputs["events"] += [
                {"kind": "compatibility_transient_error", "agent_id": "agent-001",
                 "payload": {"error": "temporary HTTP 500", "status": 500}}, finished("recovered")]
            record = cc.build_evidence(checks=cc.build_checks(**inputs), images=IMAGES, settings=SETTINGS,
                                       spent_usd=spent, request_ids=request_ids)
            self.assertTrue(record["all_required_passed"])  # runtime recovered; billing has not
            gate = self._gate(folder, record)
            self.assertFalse(gate["passed"])
            self.assertIn("unresolved", gate["reason"])
            ledger.reconcile(held, Decimal("0.009"), note="OpenRouter activity log, offline test")
            self.assertLess(Decimal(ledger.snapshot()["compatibility"]["committed_usd"]), spent)
            gate = self._gate(folder, record)
        self.assertTrue(gate["passed"], gate["reason"])

    def test_request_rows_cover_every_state_of_this_check_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "budget.sqlite"
            ledger = BudgetLedger(path)
            ours = [ledger.reserve("compatibility", "agent-001", Decimal("0.05")) for _ in range(3)]
            other = ledger.reserve("compatibility", "agent-001", Decimal("0.05"))  # an earlier check's request
            ledger.settle(ours[0], Decimal("0.01"))
            ledger.settle(ours[1], Decimal("0.07"))  # above its reservation: estimate_violation
            rows = cc.request_rows(path, ours)
        self.assertEqual(set(rows), set(ours))
        self.assertNotIn(other, rows)
        self.assertEqual({rows[i]["state"] for i in ours}, {"settled", "estimate_violation", "reserved"})
        self.assertEqual(cc.committed_usd(rows.values()), Decimal("0.13"))

    def test_missing_required_check_fails_and_credentials_are_redacted(self):
        checks = cc.build_checks(**passing_inputs())
        del checks["route_pinned"]
        record = cc.build_evidence(checks=checks, images=IMAGES, settings=SETTINGS, spent_usd=0, request_ids=[],
                                   note="provider said sk-or-v1-abcdef0123456789")
        self.assertFalse(record["all_required_passed"])
        self.assertFalse(record["checks"]["route_pinned"]["passed"])
        with tempfile.TemporaryDirectory() as folder:
            path = cc.write_evidence(folder, "20261013T120000Z", record)
            self.assertEqual(path.name, "compatibility-20261013T120000Z.json")
            text = path.read_text()
        self.assertNotIn("sk-or-v1-abcdef", text)
        self.assertIn("redacted", text)

    def test_sample_activity_from_events_and_messages(self):
        call = types.SimpleNamespace(id="c1", function="Bash", parse_error=None)
        output = types.SimpleNamespace(choices=[object()], message=types.SimpleNamespace(tool_calls=[call]))
        tool_message = types.SimpleNamespace(role="tool", tool_call_id="c1", function="Bash", text="done", error=None)
        events = [types.SimpleNamespace(event="model", input=[], output=output, error=None),
                  types.SimpleNamespace(event="model", input=[tool_message], output=types.SimpleNamespace(choices=[]),
                                        error=None),
                  types.SimpleNamespace(event="compaction")]
        sample = types.SimpleNamespace(events=events, messages=[], error=None, limit=None)
        activity = cc.sample_activity(sample)
        self.assertEqual(activity["tool_calls"], [{"id": "c1", "function": "Bash", "parse_error": None}])
        self.assertEqual(activity["tool_results"]["c1"]["text"], "done")
        self.assertEqual(activity["compaction_events"], 1)
        self.assertIsNone(activity["sample_error"])
        self.assertEqual(cc.sample_activity(None)["sample_error"], "no sample in the Inspect log")


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class AbortGuardTests(unittest.IsolatedAsyncioTestCase):
    def make(self, behaviors, max_requests=12, violation=False):
        from inspect_ai.util import LimitExceededError
        events = []
        record = lambda kind, agent, payload: events.append((kind, payload))  # noqa: E731
        clock = runtime.RunClock(runtime.StartBarrier(1, record))

        async def inner(model, messages, tools, tool_choice, config):
            if clock.stop.is_set():
                raise LimitExceededError("time", value=0, limit=0, message="Conference run has ended.")
            inner.state["requests"] += 1
            return behaviors.pop(0)()
        inner.state = {"requests": 0}
        ledger = types.SimpleNamespace(snapshot=lambda: {"compatibility": {"violation": violation}})
        guard = cc.AbortGuard(inner, clock, record, ledger, max_requests=max_requests)
        return guard, clock, events

    async def call(self, guard):
        from inspect_ai.model import ChatMessageUser
        return await guard(None, [ChatMessageUser(content="hi")], [], None, None)

    def ok(self):
        from inspect_ai.model import ModelOutput
        return lambda: ModelOutput.from_content(model="m", content="ok")

    async def test_stops_after_max_admitted_requests(self):
        from inspect_ai.util import LimitExceededError
        guard, clock, events = self.make([self.ok(), self.ok(), self.ok()], max_requests=2)
        await self.call(guard)
        await self.call(guard)
        self.assertFalse(clock.stop.is_set())
        with self.assertRaises(LimitExceededError):
            await self.call(guard)
        self.assertEqual((guard.reason, clock.reason), ("max_requests", "max_requests"))
        self.assertIs(guard.state, guard.inner.state)
        self.assertIn("compatibility_abort", [k for k, _ in events])

    async def test_first_provider_4xx_aborts(self):
        import httpx
        import openai
        response = httpx.Response(400, request=httpx.Request("POST", "https://openrouter.invalid"),
                                  json={"error": {"message": "bad"}})

        def raise_400():
            raise openai.APIStatusError("bad", response=response, body={"message": "bad"})
        guard, clock, _ = self.make([raise_400])
        with self.assertRaises(openai.APIStatusError):
            await self.call(guard)
        self.assertEqual(guard.reason, "provider_4xx")
        self.assertTrue(clock.stop.is_set())

    async def test_server_error_does_not_abort(self):
        import httpx
        import openai
        response = httpx.Response(502, request=httpx.Request("POST", "https://openrouter.invalid"), json={})

        def raise_502():
            raise openai.APIStatusError("bad gateway", response=response, body={})
        guard, clock, _ = self.make([raise_502])
        with self.assertRaises(openai.APIStatusError):
            await self.call(guard)
        self.assertIsNone(guard.reason)
        self.assertFalse(clock.stop.is_set())

    async def test_transient_errors_preserve_bounded_retry_admission(self):
        """The guard propagates temporary failures without adding retries or stopping the harness."""
        import httpx2
        import openai
        from inspect_ai.util import LimitExceededError

        for status in (408, 429, 500):
            response = httpx2.Response(status, request=httpx2.Request("POST", "https://offline.invalid"), json={})
            error = openai.APIStatusError("temporary", response=response, body={})
            with self.subTest(status=status):
                def fail():
                    raise error
                guard, clock, events = self.make([fail, self.ok(), self.ok()], max_requests=2)
                with self.assertRaises(openai.APIStatusError):
                    await self.call(guard)
                self.assertIsNone(guard.reason)
                self.assertFalse(clock.stop.is_set())
                # The existing caller may retry; the guard itself never did.
                self.assertEqual(guard.state["requests"], 1)
                from inspect_ai._util.error import exception_message
                self.assertEqual(events[0], ("compatibility_transient_error",
                                              {"error": exception_message(error)[:500], "status": status,
                                               "reason": cc.classify_generate_error(error)["reason"]}))
                await self.call(guard)
                with self.assertRaises(LimitExceededError):
                    await self.call(guard)
                self.assertEqual((guard.reason, guard.state["requests"]), ("max_requests", 2))
        error = openai.APIConnectionError(request=httpx2.Request("POST", "https://offline.invalid"))

        def disconnected():
            raise error
        guard, clock, _ = self.make([disconnected, self.ok()])
        with self.assertRaises(openai.APIConnectionError):
            await self.call(guard)
        self.assertFalse(clock.stop.is_set())
        await self.call(guard)
        self.assertIsNone(guard.reason)

    async def test_converted_rejections_abort_but_generated_truncations_do_not(self):
        from inspect_ai.model import ModelOutput, ModelUsage
        for stop_reason in ("model_length", "content_filter"):
            with self.subTest(stop_reason=stop_reason):
                output = ModelOutput.from_content(model="m", content="rejected", stop_reason=stop_reason)
                guard, clock, events = self.make([lambda: output])
                await self.call(guard)
                self.assertEqual(guard.reason, "provider_rejection")
                self.assertTrue(clock.stop.is_set())
                self.assertEqual(events[0][1]["failures"][0]["stop_reason"], stop_reason)
                output.usage = ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15)
                guard, clock, _ = self.make([lambda: output])
                await self.call(guard)
                self.assertIsNone(guard.reason)
                self.assertFalse(clock.stop.is_set())
        output = ModelOutput.from_content(model="m", content="", stop_reason="max_tokens")
        guard, clock, _ = self.make([lambda: output])
        await self.call(guard)
        self.assertIsNone(guard.reason)  # generated cap truncation is not a converted context rejection

    async def test_pinned_sdk_http_context_rejection_stops_preflight(self):
        """Exercise real SDK conversion + production ledger/filter with an in-memory HTTP response."""
        import httpx2
        from inspect_ai.model import ChatMessageUser, GenerateConfig, get_model
        from research_swarm.inspect_adapter import budget_filter, capture_openrouter_cost
        calls, events = [], []

        def rejected(request):
            calls.append(request)
            return httpx2.Response(400, json={"error": {"message": "Maximum context exceeded",
                                                       "type": "invalid_request_error",
                                                       "code": "context_length_exceeded"}})
        with tempfile.TemporaryDirectory() as folder:
            ledger = BudgetLedger(Path(folder) / "budget.sqlite")
            clock = runtime.RunClock(runtime.StartBarrier(1))
            record = lambda *args: events.append(args)  # noqa: E731
            model = capture_openrouter_cost(get_model("openrouter/" + SETTINGS.model_id,
                                                       api_key="unpaid-offline-placeholder", max_retries=0))
            client = httpx2.AsyncClient(transport=httpx2.MockTransport(rejected))
            model.api.client._client = client
            try:
                inner = budget_filter(cc.AGENT_ID, ledger, SETTINGS, clock.barrier, record,
                                      cc.SCOPE, stop=clock.stop)
                guard = cc.AbortGuard(inner, clock, record, ledger)
                output = await guard(model, [ChatMessageUser(content="hello")], [], None, GenerateConfig())
                self.assertEqual((len(calls), output.stop_reason, output.usage), (1, "model_length", None))
                self.assertEqual(guard.reason, "provider_rejection")
                self.assertTrue(clock.stop.is_set())
                from inspect_ai.util import LimitExceededError
                with self.assertRaises(LimitExceededError):
                    await guard(model, [ChatMessageUser(content="retry")], [], None, GenerateConfig())
                self.assertEqual(len(calls), 1)
                snapshot = ledger.snapshot()[cc.SCOPE]
                self.assertEqual((snapshot["unresolved_requests"], Decimal(snapshot["committed_usd"])), (0, 0))
            finally:
                await client.aclose()

    async def test_parse_failure_and_violation_abort(self):
        from inspect_ai.model import ModelOutput

        def malformed():
            output = ModelOutput.for_tool_call(model="m", tool_name="Bash", tool_arguments={"command": "ls"})
            output.choices[0].message.tool_calls[0].parse_error = "Expecting ',' delimiter"
            return output
        guard, clock, _ = self.make([malformed])
        await self.call(guard)
        self.assertEqual(guard.reason, "tool_call_parse_failure")
        guard, clock, _ = self.make([self.ok()], violation=True)
        await self.call(guard)
        self.assertEqual(guard.reason, "estimate_violation")
        self.assertTrue(clock.stop.is_set())


def _gates_all_pass(config):
    return {"evidence": {name: {"passed": True, "reason": "ok"} for name in cc.REQUIRED_GATES}}


class MainRefusalTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.ledger_file = self.folder / "state" / "budget.sqlite"
        self.out = self.folder / "validation"
        patcher = mock.patch("research_swarm.secrets.load_openrouter_key",
                             side_effect=AssertionError("credential must not be read"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runs = []

    async def fake_run(self, config, args, stamp, *, images, ledger, ledger_path):
        self.runs.append(stamp)
        before = ledger.snapshot()["compatibility"]
        ids = []
        for _ in range(3):  # the cap probe, then two agent requests
            request_id = ledger.reserve("compatibility", "agent-001", Decimal("0.05"))
            ledger.settle(request_id, Decimal("0.001"))
            ids.append(request_id)
        inputs = passing_inputs()
        mapping = {"p1": ids[0], "r1": ids[1], "r2": ids[2]}
        for event in inputs["events"]:
            if "request_id" in event["payload"]:
                event["payload"]["request_id"] = mapping[event["payload"]["request_id"]]
        return {"run_dir": self.folder / "runs" / f"compat-{stamp}", "settings": SETTINGS, "persona": "combinatorics",
                "warm_up": {"ok": True}, "ledger_before": before, "ledger_after": ledger.snapshot()["compatibility"],
                "abort_reason": "max_requests", "cancelled": False, "activity": inputs["activity"],
                "runtime_events": inputs["events"], "conference_events": inputs["conference_events"],
                "agent_exit": {"status": "stopped:max_requests"}, "inspect_log": "x.eval",
                "credentials_in_environment": False, "provider_limit": {"limit": 2, "limit_remaining": 2},
                "compose_sha256": runtime.compose_fingerprint(config, images["agent"]["id"])}

    def main(self, *argv, gates=_gates_all_pass, environment=None, ask=None, isatty=lambda: True):
        environment = environment or (lambda config: ([], IMAGES))
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output):
            return cc.main(["--output-dir", str(self.out), "--runs-dir", str(self.folder / "runs"), *argv],
                           ask=ask or (lambda prompt: self.fail("unexpected prompt")), gates=gates,
                           environment=environment, run=self.fake_run, ledger_file=self.ledger_file, isatty=isatty)

    def test_requires_user_approval_flag(self):
        never = lambda config: self.fail("nothing may be evaluated without approval")  # noqa: E731
        self.assertEqual(self.main("--yes", gates=never, environment=never), 2)
        self.assertFalse(self.ledger_file.exists())
        self.assertEqual(self.runs, [])

    def test_refuses_a_different_compatibility_cap(self):
        BudgetLedger(self.ledger_file, {"pilot": 20, "analysis": 5, "compatibility": 0})
        self.assertEqual(self.main("--i-have-user-approval", "--yes"), 2)
        self.assertEqual(self.runs, [])

    def test_refuses_low_remaining_and_violations(self):
        ledger = BudgetLedger(self.ledger_file)
        request = ledger.reserve("compatibility", "agent-001", Decimal("1.6"))
        ledger.settle(request, Decimal("1.6"))
        self.assertEqual(self.main("--i-have-user-approval", "--yes"), 2)  # $0.40 < $0.50
        self.assertIn("below the minimum", self.output.getvalue())
        self.assertIn("remaining $0.4", self.output.getvalue())  # the snapshot is printed before refusing
        self.assertEqual(self.main("--i-have-user-approval", "--yes", "--min-remaining", "0.30"), 0)
        problems = cc.ledger_problems({"compatibility": {"cap_usd": "2", "remaining_usd": "1.9", "violation": True}},
                                      Decimal("0.5"))
        self.assertTrue(any("estimate_violation" in p for p in problems))
        self.assertTrue(cc.ledger_problems({}, Decimal("0.5")))
        self.assertTrue(cc.ledger_problems({"compatibility": {"cap_usd": "2", "remaining_usd": "2",
                                                              "violation": False}}, Decimal("0.5"),
                                           caps={"compatibility": 0}))

    def test_refuses_failed_gates_and_environment(self):
        def gates(config):
            report = _gates_all_pass(config)
            report["evidence"]["route_pricing"] = {"passed": False, "reason": "freeze is 80 h old"}
            del report["evidence"]["runtime-isolation"]
            return report
        self.assertEqual(self.main("--i-have-user-approval", "--yes", gates=gates), 2)
        self.assertEqual(cc.gate_problems(gates(None)), ["route_pricing: freeze is 80 h old",
                                                        "runtime-isolation: launch gate not evaluated"])
        broken = lambda config: (["agent image gdm-math-swarm-agent:local does not resolve"], None)  # noqa: E731
        self.assertEqual(self.main("--i-have-user-approval", "--yes", environment=broken), 2)
        self.assertEqual(self.runs, [])

    def test_environment_check_applies_the_launch_interlock_without_override(self):
        """S1: a running comparator / leftover judge / other agent sandbox refuses the paid check."""
        busy = {"checked_at": "t", "containers": [], "judge_containers": [],
                "blockers": ["1 running comparator container(s) with label gdm-evaluator=1 (gdm-comparator-x); wait",
                             "2 running agent sandbox container(s) from another run (inspect-a, inspect-b); wait"]}
        seen = []
        versions = {"inspect-ai": {"actual": "x", "expected": "x", "matches": True}}
        with mock.patch("research_swarm.inspect_adapter.installed_versions", return_value=versions), \
                mock.patch.object(runtime, "pin_images", return_value=IMAGES):
            problems, images = cc.environment_check(dict(CONFIG, agent_image="custom:agent"),
                                                    interlock=lambda image: seen.append(image) or busy)
            self.assertEqual(problems, busy["blockers"])
            self.assertEqual(seen, ["custom:agent"])
            with mock.patch.object(runtime, "launch_interlock",
                                   lambda image, docker="docker": dict(busy, blockers=[])):
                self.assertEqual(cc.environment_check(CONFIG), ([], IMAGES))  # default: runtime.launch_interlock
        self.assertFalse(hasattr(cc.parse_args(["--i-have-user-approval"]), "force_while_live"))  # no override
        self.assertEqual(self.main("--i-have-user-approval", "--yes",
                                   environment=lambda config: (busy["blockers"], IMAGES)), 2)
        self.assertIn("gdm-evaluator=1", self.output.getvalue())
        self.assertEqual(self.runs, [])

    def test_confirmation(self):
        self.assertEqual(self.main("--i-have-user-approval", ask=lambda prompt: "1.00"), 2)
        self.assertEqual(self.main("--i-have-user-approval", isatty=lambda: False, ask=input), 2)
        self.assertEqual(self.runs, [])
        prompts = []
        self.assertEqual(self.main("--i-have-user-approval", ask=lambda prompt: prompts.append(prompt) or "$2.00"), 0)
        self.assertIn("PAID", prompts[0])
        self.assertEqual(len(self.runs), 1)

    def test_run_failure_returns_error_without_evidence(self):
        async def broken(config, args, stamp, **kwargs):
            raise RuntimeError("Set OPENROUTER_API_KEY in the host environment or private .env")
        self.fake_run = broken
        self.assertEqual(self.main("--i-have-user-approval", "--yes"), 1)
        self.assertIn("aborted before evidence", self.output.getvalue())
        self.assertEqual(list(self.out.glob("compatibility-*.json")) if self.out.exists() else [], [])

    def test_successful_run_writes_evidence(self):
        self.assertEqual(self.main("--i-have-user-approval", "--yes"), 0)
        files = sorted(self.out.glob("compatibility-*.json"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^compatibility-\d{8}T\d{6}Z\.json$")
        record = json.loads(files[0].read_text())
        self.assertTrue(record["all_required_passed"])
        self.assertEqual(Decimal(record["spent_usd"]), Decimal("0.003"))
        self.assertEqual(record["requests"], 3)
        self.assertEqual(len(record["request_ids"]), 3)  # X4: probe + agent requests, ledger ids
        ledger_ids = {r["request_id"] for r in cc.list_requests(self.ledger_file, states=cc.LEDGER_STATES)}
        self.assertEqual(set(record["request_ids"]), ledger_ids)
        self.assertEqual(record["ledger"]["request_states"], {"settled": 3})
        self.assertEqual(record["ledger"]["missing_request_ids"], [])
        self.assertIs(record["credentials_in_environment"], False)  # measured, not a constant
        self.assertEqual(record["provider_limit"], {"limit": 2, "limit_remaining": 2})
        self.assertEqual(record["compose_sha256"], runtime.compose_fingerprint(CONFIG, IMAGES["agent"]["id"]))
        self.assertTrue(record["checks"]["reasoning_cap_enforced"]["passed"])
        self.assertEqual(record["image_ids"]["agent"], IMAGES["agent"]["id"])
        self.assertEqual(record["settings"], frozen_fields(settings_from_config(CONFIG)))
        self.assertEqual(record["abort_reason"], "max_requests")
        self.assertEqual(record["ledger"]["scope"], "compatibility")

    def test_cap_probe_must_be_below_the_frozen_max_tokens(self):
        self.assertEqual(self.main("--i-have-user-approval", "--yes", "--cap-probe-max-tokens",
                                   str(SETTINGS.max_tokens)), 2)
        self.assertIn("--cap-probe-max-tokens", self.output.getvalue())
        self.assertEqual(self.runs, [])


def _priced(output, n, *, output_tokens=120, reasoning=80, cost=0.0004):
    from inspect_ai.model import ModelUsage
    output.usage = ModelUsage(input_tokens=1000, output_tokens=output_tokens, total_tokens=1000 + output_tokens,
                              reasoning_tokens=reasoning)
    output.metadata = {"openrouter_usage": {"cost": cost, "completion_tokens": output_tokens,
                                            "completion_tokens_details": {"reasoning_tokens": reasoning}},
                       "openrouter_generation_id": f"gen-offline-{n}", "openrouter_provider": PROVIDER_NAME,
                       "openrouter_model": SETTINGS.model_id}
    return output


def _probe_output(config, *, truncated=True):
    """The cap probe's answer: cut at config.max_tokens, mostly reasoning (or, untruncated, a short answer)."""
    from inspect_ai.model import ContentReasoning, ContentText, ModelOutput
    output = ModelOutput.from_content(model=SETTINGS.model_id, content="",
                                      stop_reason="max_tokens" if truncated else "stop")
    output.message.content = [ContentReasoning(reasoning="Count lattice points row by row ...")]
    if not truncated:
        output.message.content.append(ContentText(text="31417"))
        return _priced(output, "probe", output_tokens=300, reasoning=250, cost=0.0001)
    return _priced(output, "probe", output_tokens=config.max_tokens, reasoning=config.max_tokens - 30, cost=0.0013)


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class OfflineRehearsalTests(unittest.TestCase):
    """run_check through real Inspect eval, the production solver, budget filter, ledger
    (scope compatibility), conference and evidence code; the sandbox, CLI, judge
    compiler, warm-up and the OpenRouter model are offline stand-ins. No credential."""
    def setUp(self):
        from research_swarm.inspect_adapter import disable_inspect_dotenv
        disable_inspect_dotenv()  # X1: before any Inspect evaluation in this process
        sys.path.insert(0, str(ROOT / "tests"))
        from inspect_ai.util._display import display_type, init_display_type
        previous_display = display_type()
        init_display_type("none")
        self.addCleanup(init_display_type, previous_display)

    def rehearse(self, outputs, *argv, config=CONFIG, cwd=None):
        """run_check with offline stand-ins; ``outputs(input, tools, tool_choice, config, n)`` answers each call."""
        from test_runtime import _fake_claude_code, _fake_sandbox_class
        from inspect_ai.model import get_model
        from inspect_ai.util import SandboxEnvironmentSpec
        from research_swarm import inspect_adapter
        from research_swarm.lean import CompileResult
        sandbox_cls = _fake_sandbox_class()
        served = []

        async def answer(input, tools, tool_choice, generate_config):
            served.append(generate_config.max_tokens)
            return await outputs(input, tools, tool_choice, generate_config, len(served))

        class FakeCompiler:
            image, timeout, memory, cpus, max_concurrent = IMAGES["lean"]["id"], 120, "2g", 1, None

            def compile(self, source):
                return CompileResult("error", 1, "Submission.lean:1:1: error: Provide a complete proof.", "", 0)
        original_build_task = runtime.build_task

        def build_task(**kwargs):
            task = original_build_task(**kwargs)
            task.sandbox = SandboxEnvironmentSpec("gdm_offline_fake")
            return task
        built = []

        def fake_model(settings, repo_root, max_connections, *, max_remaining_usd, record=None):
            # X2: the limit check runs inside the builder, against the remaining compatibility allowance.
            built.append((settings.model_id, max_connections, str(max_remaining_usd)))
            limit = {"limit": 2.0, "limit_remaining": float(max_remaining_usd), "limit_reset": None}
            record("provider_limit_verified", None, limit)
            return get_model("mockllm/model", custom_outputs=answer), limit
        folder = Path(tempfile.mkdtemp()).resolve()
        sandbox_cls.root = str(folder)
        ledger_path = folder / "state" / "budget.sqlite"
        ledger = BudgetLedger(ledger_path)
        args = cc.parse_args(["--i-have-user-approval", "--runs-dir", str(folder / "runs"), *argv])
        warm_ups = []
        previous_cwd = os.getcwd()
        try:
            if cwd is not None:
                os.chdir(cwd)
            with mock.patch.object(inspect_adapter, "build_claude_agent", _fake_claude_code), \
                    mock.patch.object(runtime, "make_lean_compiler", lambda config, image=None: FakeCompiler()), \
                    mock.patch.object(runtime, "warm_up_lean",
                                      lambda image, record, **o: warm_ups.append(image) or {"ok": True}), \
                    mock.patch.object(runtime, "build_task", build_task), \
                    mock.patch.object(runtime, "build_openrouter_model", fake_model), \
                    mock.patch("research_swarm.secrets.load_openrouter_key",
                               side_effect=AssertionError("credential must not be read")):
                result = asyncio_run(cc.run_check(config, args, cc.utc_stamp(), images=IMAGES, ledger=ledger,
                                                  ledger_path=ledger_path))
        finally:
            os.chdir(previous_cwd)
        return result, ledger, ledger_path, served, built, warm_ups

    def test_run_check_with_fakes(self):
        from inspect_ai.model import ContentReasoning, ContentText, ModelOutput

        def bash_turn():
            output = ModelOutput.for_tool_call(model=SETTINGS.model_id, tool_name="Bash", tool_call_id="b1",
                                               tool_arguments={"command": "ls /workspace/problems | head -n 3",
                                                               "description": "List problems"})
            output.message.content = [ContentReasoning(reasoning="Look at the problems first."),
                                      ContentText(text="Listing the problems.")]
            return output
        script = [bash_turn,
                  lambda: ModelOutput.for_tool_call(model=SETTINGS.model_id, tool_name="mcp__conference__get_bulletin_board",
                                                    tool_arguments={}, tool_call_id="c1"),
                  lambda: ModelOutput.for_tool_call(model=SETTINGS.model_id, tool_name="Bash", tool_call_id="b2",
                                                    tool_arguments={"command": "echo third", "description": "Echo"}),
                  lambda: ModelOutput.from_content(model=SETTINGS.model_id, content="never served")]
        agent_calls = []

        async def outputs(input, tools, tool_choice, config, n):
            if config.max_tokens == cc.DEFAULT_CAP_PROBE_MAX_TOKENS:
                self.assertIn("x^2 + y^2", input[-1].text)  # the researcher-side probe, not an agent turn
                return _probe_output(config)
            agent_calls.append(len(input))
            return _priced(script[len(agent_calls) - 1](), n)
        # A fake .env in the cwd must never reach the process environment (X1).
        folder = Path(tempfile.mkdtemp()).resolve()
        (folder / ".env").write_text("GDM_OFFLINE_DOTENV_PROBE=not-a-secret\n")
        self.assertNotIn("GDM_OFFLINE_DOTENV_PROBE", os.environ)
        result, ledger, ledger_path, served, built, warm_ups = self.rehearse(
            outputs, "--max-requests", "3", "--max-minutes", "5", cwd=folder)
        self.assertNotIn("GDM_OFFLINE_DOTENV_PROBE", os.environ)
        self.assertIs(result["credentials_in_environment"], "OPENROUTER_API_KEY" in os.environ)
        checks, spent, request_ids, violations, details = cc.summarize_run(result, SETTINGS, ledger_path)
        self.assertEqual(failed(checks), [], {k: v["detail"] for k, v in checks.items() if not v["passed"]})
        self.assertIn('"ok": true', result["activity"]["tool_results"]["c1"]["text"])  # resolved, not attachment://
        self.assertEqual((built, warm_ups), ([(SETTINGS.model_id, 1, "2")], [IMAGES["lean"]["id"]]))
        # The probe ran first, with the lowered cap; then the agent at the frozen max_tokens.
        self.assertEqual(served, [cc.DEFAULT_CAP_PROBE_MAX_TOKENS] + [SETTINGS.max_tokens] * 3)
        self.assertEqual((result["abort_reason"], len(request_ids), len(agent_calls)), ("max_requests", 4, 3))
        self.assertEqual(spent, Decimal("0.0012") + Decimal("0.0013"))
        self.assertEqual((violations, details["request_states"]), ([], {"settled": 4}))
        self.assertEqual(result["persona"], runtime.assign_personas(20, CONFIG["persona_seed"])["agent-001"])
        compose = (result["run_dir"] / "compose.yaml").read_text()
        self.assertIn(f"image: {IMAGES['agent']['id']}", compose)
        self.assertEqual(result["compose_sha256"], hashlib.sha256(compose.encode("utf-8")).hexdigest())
        settings_json = json.loads((result["run_dir"] / "settings.json").read_text())
        self.assertEqual(settings_json["effective"]["provider_limit"]["limit_remaining"], 2.0)
        self.assertTrue(settings_json["effective"]["inspect_dotenv"]["dotenv_disabled"])
        self.assertEqual(settings_json["effective"]["max_connections"], cc.COMPAT_MAX_CONNECTIONS)  # one agent: 1
        kinds = [e["kind"] for e in result["runtime_events"]]
        self.assertLess(kinds.index("provider_limit_verified"), kinds.index("inference_admitted"))
        self.assertLess(kinds.index("reasoning_cap_probe"), kinds.index("common_start"))
        snapshot = ledger.snapshot()
        self.assertEqual((snapshot["compatibility"]["unresolved_requests"], snapshot["pilot"]["committed_usd"]), (0, "0"))

    def test_failed_cap_probe_skips_the_agent_run(self):
        agent_calls = []

        async def outputs(input, tools, tool_choice, config, n):
            if config.max_tokens == cc.DEFAULT_CAP_PROBE_MAX_TOKENS:
                return _probe_output(config, truncated=False)  # inconclusive: never reached the cap
            agent_calls.append(n)
            raise AssertionError("the agent must not run after a failed probe")
        result, ledger, ledger_path, served, _, _ = self.rehearse(outputs)
        checks, spent, request_ids, _, _ = cc.summarize_run(result, SETTINGS, ledger_path)
        self.assertEqual((served, agent_calls, len(request_ids)), ([cc.DEFAULT_CAP_PROBE_MAX_TOKENS], [], 1))
        self.assertEqual(result["abort_reason"], "reasoning_cap_unverified")
        self.assertFalse(checks["reasoning_cap_enforced"]["passed"])
        self.assertIn("'stop'", checks["reasoning_cap_enforced"]["detail"])
        self.assertFalse(checks["no_sample_error"]["passed"])
        self.assertEqual(spent, Decimal("0.0001"))
        self.assertEqual(ledger.snapshot()["compatibility"]["unresolved_requests"], 0)

    def test_stop_lets_the_in_flight_request_settle(self):
        """SPEND-5: max_minutes lands mid-generation; the request finishes and settles with its reported
        cost and generation id instead of being cancelled into an uncertain hold."""
        from inspect_ai.model import ModelOutput

        async def outputs(input, tools, tool_choice, config, n):
            if config.max_tokens == cc.DEFAULT_CAP_PROBE_MAX_TOKENS:
                return _probe_output(config)
            if n == 2:
                await asyncio.sleep(5.0)  # still generating when the 1.2 s maximum stops the run (1 s polls)
                return _priced(ModelOutput.for_tool_call(model=SETTINGS.model_id, tool_name="Bash", tool_call_id="b1",
                                                         tool_arguments={"command": "echo late",
                                                                         "description": "Echo"}), n)
            raise AssertionError("no request may be admitted after the stop")
        result, ledger, ledger_path, served, _, _ = self.rehearse(outputs, "--max-minutes", "0.02")
        events = result["runtime_events"]
        kinds = [e["kind"] for e in events]
        self.assertEqual(result["abort_reason"], "max_minutes")
        self.assertNotIn("inference_uncertain", kinds)
        finished = [e["payload"] for e in events if e["kind"] == "inference_finished" and e["agent_id"] == cc.AGENT_ID]
        self.assertEqual([(f["basis"], f["provider_generation_id"]) for f in finished],
                         [("provider_reported_cost", "gen-offline-2")])
        grace = next(e["payload"] for e in events if e["kind"] == "agent_in_flight_grace")
        self.assertEqual((grace["in_flight_at_stop"], grace["still_in_flight"]), (1, 0))
        self.assertLess(kinds.index("run_stop"), kinds.index("inference_finished", kinds.index("common_start")))
        self.assertEqual(ledger.snapshot()["compatibility"]["unresolved_requests"], 0)
        self.assertEqual(result["activity"]["model_errors"], [])  # no cancelled ModelEvent
        self.assertEqual(result["agent_exit"]["status"], "stopped:max_run_seconds")
        self.assertEqual(served, [cc.DEFAULT_CAP_PROBE_MAX_TOKENS, SETTINGS.max_tokens])


def asyncio_run(coroutine):
    import asyncio
    return asyncio.run(coroutine)


if __name__ == "__main__":
    unittest.main()
