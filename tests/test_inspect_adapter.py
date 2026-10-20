"""Offline checks of the Inspect/Inspect SWE adapter (no network, no credentials).

Skipped unless the pinned Inspect packages are installed (project .venv).
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from decimal import Decimal
from pathlib import Path

try:
    import inspect_ai  # noqa: F401
    HAVE_INSPECT = True
except ImportError:
    HAVE_INSPECT = False

from research_swarm.budget import BudgetLedger
from research_swarm.provider import ProviderSettings
from research_swarm.runtime import StartBarrier

DUMMY_KEY = "unpaid-offline-test-placeholder"  # never a real credential


def _completion(cost=None, byok=None, upstream=None, finish="stop", tool_call=False):
    from openai.types.chat import ChatCompletion
    usage = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
             "completion_tokens_details": {"reasoning_tokens": 150}}
    if cost is not None:
        usage["cost"] = cost
    if byok is not None:
        usage["is_byok"] = byok
        usage["cost_details"] = {"upstream_inference_cost": upstream}
    message = {"role": "assistant", "content": "done"}
    if tool_call:
        message["tool_calls"] = [{"id": "call_1", "type": "function",
                                  "function": {"name": "Bash", "arguments": "{\"command\": \"ls"}}]
    return ChatCompletion.model_validate({
        "id": "gen-offline", "object": "chat.completion", "created": 0,
        "model": "deepseek/deepseek-v4.1-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": usage})


def _openrouter_model(completion):
    from inspect_ai.model import get_model
    from research_swarm.inspect_adapter import capture_openrouter_cost
    model = get_model("openrouter/deepseek/deepseek-v4.1-flash", api_key=DUMMY_KEY, max_retries=0,
                      provider={"only": ["deepseek"], "allow_fallbacks": False}, reasoning_enabled=True)
    sent = []

    async def fake_completion(request, config):  # replaces the HTTP call only
        sent.append(request)
        return completion
    model.api._generate_completion = fake_completion
    return capture_openrouter_cost(model), sent


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class OpenRouterCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_does_not_report_provider_cost_but_capture_does(self):
        from inspect_ai.model import ChatMessageUser, GenerateConfig
        from research_swarm.inspect_adapter import settlement
        model, sent = _openrouter_model(_completion(cost=0.00123))
        output = await model.generate([ChatMessageUser(content="hi")],
                                      config=GenerateConfig(max_retries=0, reasoning_effort="high",
                                                            extra_body=ProviderSettings().request_options()))
        # Inspect's own field is a price-table estimate (None for this model), never the bill.
        self.assertIsNone(output.usage.total_cost)
        self.assertEqual(output.metadata["openrouter_usage"]["cost"], 0.00123)
        self.assertEqual(output.metadata["openrouter_generation_id"], "gen-offline")
        cost, basis = settlement(ProviderSettings(pricing_verified=True), output)
        self.assertEqual((cost, basis), (Decimal("0.00123"), "provider_reported_cost"))
        self.assertEqual(model.api.client.max_retries, 0)  # no SDK-level silent retries
        body = sent[0]["extra_body"]
        self.assertEqual(body["provider"], {"only": ["deepseek"], "allow_fallbacks": False})
        self.assertEqual(body["reasoning"]["effort"], "high")

    async def test_missing_cost_falls_back_to_token_upper_bound(self):
        from inspect_ai.model import ChatMessageUser, GenerateConfig
        from research_swarm.inspect_adapter import settlement
        model, _ = _openrouter_model(_completion())
        output = await model.generate([ChatMessageUser(content="hi")], config=GenerateConfig(max_retries=0))
        cost, basis = settlement(ProviderSettings(pricing_verified=True), output)
        self.assertEqual(basis, "usage_priced_upper_bound")
        self.assertEqual(cost, Decimal(1000) * Decimal("0.0000003") + Decimal(200) * Decimal("0.0000012"))

    def test_byok_adds_upstream_or_is_uncertain(self):
        from research_swarm.inspect_adapter import openrouter_reported_cost
        self.assertEqual(openrouter_reported_cost({"cost": 0.001, "is_byok": True,
                                                   "cost_details": {"upstream_inference_cost": 0.002}}),
                         Decimal("0.003"))
        self.assertIsNone(openrouter_reported_cost({"cost": 0.001, "is_byok": True, "cost_details": {}}))
        self.assertIsNone(openrouter_reported_cost({"cost": True}))
        self.assertIsNone(openrouter_reported_cost(None))


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class BudgetFilterTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, output, ledger=None, stop=None):
        from inspect_ai.model import ChatMessageUser, GenerateConfig
        from research_swarm.inspect_adapter import budget_filter

        class FakeModel:
            async def generate(self, **kwargs):
                self.config = kwargs["config"]
                return output
        folder = tempfile.mkdtemp()
        ledger = ledger or BudgetLedger(Path(folder) / "budget.sqlite", {"validation": 100})
        events = []
        model = FakeModel()
        intercept = budget_filter("agent-001", ledger, ProviderSettings(pricing_verified=True), StartBarrier(1),
                                  lambda *a: events.append(a), scope="validation", stop=stop)
        result = await intercept(model, [ChatMessageUser(content="hello")], [], None, GenerateConfig())
        return result, model, ledger, events

    async def test_frozen_settings_and_settlement(self):
        from inspect_ai.model import ModelOutput, ModelUsage
        output = ModelOutput.from_content(model="m", content="ok")
        output.usage = ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7, total_cost=99.0)
        output.metadata = {"openrouter_usage": {"cost": 0.00001}}
        _, model, ledger, events = await self._run(output)
        self.assertEqual(model.config.max_tokens, 16384)
        self.assertEqual(model.config.max_retries, 0)
        self.assertEqual(model.config.reasoning_history, "all")
        # `n` is not sent: require_parameters would make the route refuse it.
        self.assertIsNone(model.config.num_choices)
        self.assertFalse(model.config.extra_body["provider"]["allow_fallbacks"])
        snapshot = ledger.snapshot()["validation"]
        self.assertEqual(snapshot["unresolved_requests"], 0)
        # Inspect's 99.0 estimate is ignored; the provider-reported cost settles.
        self.assertEqual(Decimal(snapshot["committed_usd"]), Decimal("0.00001"))
        self.assertEqual([e[0] for e in events], ["inference_admitted", "inference_finished"])

    async def test_admission_records_request_bytes_and_replayed_reasoning(self):
        from inspect_ai.model import (ChatMessageAssistant, ChatMessageUser, ContentReasoning, ContentText,
                                      GenerateConfig, ModelOutput)
        from research_swarm.inspect_adapter import budget_filter, replayed_reasoning_blocks
        messages = [ChatMessageUser(content="Prove \u2200 n : \u2115, n = n"),
                    ChatMessageAssistant(content=[ContentReasoning(reasoning="think"), ContentText(text="ok")]),
                    ChatMessageUser(content="continue")]
        self.assertEqual(replayed_reasoning_blocks(messages), 1)
        self.assertEqual(replayed_reasoning_blocks(messages[:1]), 0)

        class FakeModel:
            async def generate(self, **kwargs):
                return ModelOutput.from_content(model="m", content="ok")
        events = []
        ledger = BudgetLedger(Path(tempfile.mkdtemp()) / "b.sqlite", {"validation": 100})
        intercept = budget_filter("agent-001", ledger, ProviderSettings(pricing_verified=True), StartBarrier(1),
                                  lambda *a: events.append(a), scope="validation")
        await intercept(FakeModel(), messages, [], None, GenerateConfig())
        admitted = [e[2] for e in events if e[0] == "inference_admitted"][0]
        request = json.dumps({"messages": [m.model_dump(mode="json") for m in messages], "tools": []},
                             ensure_ascii=False)
        self.assertEqual(admitted["request_bytes"], len(request.encode("utf-8")))
        self.assertGreater(admitted["request_bytes"], len(request))  # UTF-8 bytes, not characters
        self.assertEqual(admitted["replayed_reasoning_blocks"], 1)
        self.assertEqual(admitted["replayed_signed_tool_call_turns"], 0)
        self.assertNotIn("replayed_signed_reasoning_formats", admitted)

    def test_replayed_signed_reasoning_counts_signed_tool_call_turns_only(self):
        """Gemini thought signatures (signed OpenRouter reasoning_details) next to tool calls; the
        compatibility check's signed_reasoning_tool_call_replay reads this from inference_admitted."""
        from inspect_ai.model import ChatMessageAssistant, ChatMessageUser, ContentReasoning, ContentText
        from inspect_ai.tool import ToolCall
        from research_swarm.inspect_adapter import replayed_signed_reasoning

        def details(*entries):
            return ContentReasoning(reasoning="x", signature="reasoning-details://" + json.dumps(list(entries)))
        encrypted = {"type": "reasoning.encrypted", "data": "sig", "id": "t1", "format": "google-gemini-v1"}
        signed_text = {"type": "reasoning.text", "signature": "sig", "format": "anthropic-claude-v1"}
        unsigned = {"type": "reasoning.text", "text": "plain", "format": "unknown"}
        call = [ToolCall(id="t1", function="Bash", arguments={})]
        messages = [ChatMessageUser(content="go"),
                    ChatMessageAssistant(content=[details(unsigned, encrypted), ContentText(text="")], tool_calls=call),
                    ChatMessageAssistant(content=[details(signed_text)], tool_calls=call),
                    ChatMessageAssistant(content=[details(encrypted)]),  # no tool calls
                    ChatMessageAssistant(content=[details(unsigned)], tool_calls=call),  # DeepSeek/GLM style
                    ChatMessageAssistant(content=[ContentReasoning(reasoning="x", signature="reasoning-details://{")],
                                         tool_calls=call),  # malformed: ignored
                    ChatMessageAssistant(content=[ContentReasoning(reasoning="native", signature="anthropic-sig")],
                                         tool_calls=call)]
        self.assertEqual(replayed_signed_reasoning(messages),
                         {"replayed_signed_tool_call_turns": 2,
                          "replayed_signed_reasoning_formats": ["anthropic-claude-v1", "google-gemini-v1"]})
        self.assertEqual(replayed_signed_reasoning(messages[:1] + messages[3:]), {"replayed_signed_tool_call_turns": 0})

    async def test_truncated_tool_calls_never_reach_scaffold(self):
        from inspect_ai.model import ModelOutput
        output = ModelOutput.for_tool_call(model="m", tool_name="Bash", tool_arguments={"command": "ls"})
        output.choices[0].stop_reason = "max_tokens"
        result, _, _, events = await self._run(output)
        self.assertIsNone(result.choices[0].message.tool_calls)
        self.assertEqual(events[-1][0], "truncated_response")

    async def test_budget_denial_ends_sample(self):
        from inspect_ai.model import ModelOutput
        from inspect_ai.util import LimitExceededError
        folder = tempfile.mkdtemp()
        ledger = BudgetLedger(Path(folder) / "b.sqlite", {"validation": 0.001})
        with self.assertRaises(LimitExceededError):
            await self._run(ModelOutput.from_content(model="m", content="x"), ledger=ledger)
        self.assertEqual(ledger.snapshot()["validation"]["committed_usd"], "0")

    async def test_stop_signal_refuses_new_requests(self):
        from inspect_ai.model import ModelOutput
        from inspect_ai.util import LimitExceededError
        stop = asyncio.Event()
        stop.set()
        with self.assertRaises(LimitExceededError):
            await self._run(ModelOutput.from_content(model="m", content="x"), stop=stop)

    async def test_stop_during_reservation_releases_without_dispatch(self):
        from inspect_ai.model import ChatMessageUser, GenerateConfig
        from inspect_ai.util import LimitExceededError
        from research_swarm.inspect_adapter import budget_filter
        entered, release = threading.Event(), threading.Event()
        stop, events = asyncio.Event(), []
        with tempfile.TemporaryDirectory() as folder:
            ledger = BudgetLedger(Path(folder) / "budget.sqlite", {"validation": 100})
            reserve = ledger.reserve

            def blocked_reserve(*args):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release reservation")
                return reserve(*args)

            class Model:
                async def generate(self, **kwargs):
                    raise AssertionError("stopped request must never enter generation")

            ledger.reserve = blocked_reserve
            intercept = budget_filter("agent-001", ledger, ProviderSettings(pricing_verified=True), StartBarrier(1),
                                      lambda *a: events.append(a), scope="validation", stop=stop)
            task = asyncio.create_task(intercept(Model(), [ChatMessageUser(content="hi")], [], None, GenerateConfig()))
            try:
                async def wait_entered():
                    while not entered.is_set():
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait_entered(), 5)
                stop.set()
                release.set()
                with self.assertRaises(LimitExceededError):
                    await task
            finally:
                release.set()
            self.assertEqual(intercept.state["last_denial"], "run_stopped")
            self.assertEqual((intercept.state["requests"], intercept.state["in_flight"]), (0, 0))
            snapshot = ledger.snapshot()["validation"]
            self.assertEqual(snapshot["unresolved_requests"], 0)
            self.assertEqual(Decimal(snapshot["held_usd"]), 0)
            self.assertNotIn("inference_admitted", [event[0] for event in events])

    async def test_cancel_during_reservation_without_stop_releases_without_dispatch(self):
        import anyio
        from inspect_ai.model import ChatMessageUser, GenerateConfig
        from research_swarm.inspect_adapter import budget_filter
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            ledger = BudgetLedger(Path(folder) / "budget.sqlite", {"validation": 100})
            reserve = ledger.reserve

            def blocked_reserve(*args):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release reservation")
                return reserve(*args)

            model = unittest.mock.Mock()
            ledger.reserve = blocked_reserve
            intercept = budget_filter("agent-001", ledger, ProviderSettings(pricing_verified=True), StartBarrier(1),
                                      lambda *a: None, scope="validation")
            async def request():
                await intercept(model, [ChatMessageUser(content="hi")], [], None, GenerateConfig())

            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(request)
                    with anyio.fail_after(5):
                        while not entered.is_set():
                            await anyio.sleep(0)
                    group.cancel_scope.cancel()
                    release.set()
            finally:
                release.set()
            model.generate.assert_not_called()
            self.assertEqual(intercept.state["last_denial"], "cancelled_before_dispatch")
            snapshot = ledger.snapshot()["validation"]
            self.assertEqual((snapshot["unresolved_requests"], Decimal(snapshot["held_usd"])), (0, 0))

    def test_output_kind(self):
        from inspect_ai.model import ContentReasoning, ContentText, ModelOutput
        from research_swarm.inspect_adapter import output_kind
        self.assertEqual(output_kind(ModelOutput.from_content(model="m", content="done")), "text")
        self.assertEqual(output_kind(ModelOutput.from_content(model="m", content="  ")), "empty")
        self.assertEqual(output_kind(ModelOutput.for_tool_call(model="m", tool_name="Bash",
                                                               tool_arguments={"command": "ls"})), "tool_calls")
        reasoning = ModelOutput.from_content(model="m", content="")
        reasoning.message.content = [ContentReasoning(reasoning="hmm"), ContentText(text=" ")]
        self.assertEqual(output_kind(reasoning), "reasoning_only")
        reasoning.message.content = []
        self.assertEqual(output_kind(reasoning), "empty")
        self.assertEqual(output_kind(None), "empty")

    async def test_state_records_last_outcome_and_output_kind(self):
        """RT-3: the filter state tells the runtime what the session's last request was."""
        from inspect_ai.model import ChatMessageUser, ContentReasoning, GenerateConfig, ModelOutput
        from inspect_ai.util import LimitExceededError
        from research_swarm.inspect_adapter import budget_filter
        output = ModelOutput.from_content(model="m", content="")
        output.message.content = [ContentReasoning(reasoning="only thinking")]

        class FakeModel:
            async def generate(self, **kwargs):
                return output
        ledger = BudgetLedger(Path(tempfile.mkdtemp()) / "b.sqlite", {"validation": 100})
        intercept = budget_filter("agent-001", ledger, ProviderSettings(pricing_verified=True), StartBarrier(1),
                                  lambda *a: None, scope="validation")
        await intercept(FakeModel(), [ChatMessageUser(content="hi")], [], None, GenerateConfig())
        self.assertEqual((intercept.state["last_request_outcome"], intercept.state["last_output_kind"],
                          intercept.state["last_denial"], intercept.state["in_flight"]),
                         ("finished", "reasoning_only", None, 0))
        poor = BudgetLedger(Path(tempfile.mkdtemp()) / "b.sqlite", {"validation": 0.000001})
        denied = budget_filter("agent-001", poor, ProviderSettings(pricing_verified=True), StartBarrier(1),
                               lambda *a: None, scope="validation")
        with self.assertRaises(LimitExceededError):
            await denied(FakeModel(), [ChatMessageUser(content="hi")], [], None, GenerateConfig())
        self.assertEqual((denied.state["last_request_outcome"], denied.state["last_denial"]),
                         ("not_admitted", "budget_admission_failed"))

    async def test_in_flight_count_and_stop_during_generation(self):
        """SPEND-5: a request admitted before the stop finishes and settles; the next one is refused."""
        from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelOutput, ModelUsage
        from inspect_ai.util import LimitExceededError
        from research_swarm.inspect_adapter import budget_filter
        stop, release = asyncio.Event(), asyncio.Event()

        class SlowModel:
            async def generate(self, **kwargs):
                await release.wait()
                output = ModelOutput.from_content(model="m", content="ok")
                output.usage = ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7)
                output.metadata = {"openrouter_usage": {"cost": 0.00002}, "openrouter_generation_id": "gen-late"}
                return output
        ledger = BudgetLedger(Path(tempfile.mkdtemp()) / "b.sqlite", {"validation": 100})
        events = []
        intercept = budget_filter("agent-001", ledger, ProviderSettings(pricing_verified=True), StartBarrier(1),
                                  lambda *a: events.append(a), scope="validation", stop=stop)
        call = asyncio.create_task(intercept(SlowModel(), [ChatMessageUser(content="hi")], [], None, GenerateConfig()))
        while intercept.state["in_flight"] == 0:
            await asyncio.sleep(0.01)
        stop.set()
        with self.assertRaises(LimitExceededError):  # no new admission after the stop
            await intercept(SlowModel(), [ChatMessageUser(content="again")], [], None, GenerateConfig())
        self.assertEqual(intercept.state["in_flight"], 1)
        release.set()
        await call
        self.assertEqual(intercept.state["in_flight"], 0)
        finished = [e[2] for e in events if e[0] == "inference_finished"]
        self.assertEqual([(f["basis"], f["provider_generation_id"]) for f in finished],
                         [("provider_reported_cost", "gen-late")])
        self.assertEqual(ledger.snapshot()["validation"]["unresolved_requests"], 0)
        # Chronological: the refusal came first, then the admitted request finished.
        self.assertEqual((intercept.state["last_request_outcome"], intercept.state["requests"]), ("finished", 1))


class _LocalSandbox:
    """Runs the adapter's container-side script with the host interpreter."""
    async def exec(self, cmd, timeout=None, **kwargs):
        cmd = [sys.executable if cmd[0] == "python3" else cmd[0], *cmd[1:]]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        class Result:
            success = proc.returncode == 0
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        return Result()


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class SandboxFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()).resolve()
        self.root = self.folder / "workspace"
        self.root.mkdir()
        (self.root / "Proof.lean").write_text("theorem t : True := trivial\n")
        (self.root / "big.lean").write_text("x" * 100)
        (self.folder / "outside.lean").write_text("secret")
        os.symlink(self.folder / "outside.lean", self.root / "link.lean")
        (self.root / "notes.md").write_text("# notes\n")

    async def read(self, name, suffixes=None, limit=50):
        from research_swarm.inspect_adapter import read_agent_file
        return await read_agent_file(_LocalSandbox(), str(self.root / name) if not name.startswith("/") else name,
                                     [str(self.root)], limit, suffixes)

    async def test_reads_and_rejects(self):
        ok = await self.read("Proof.lean")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["content"], "theorem t : True := trivial\n")
        self.assertEqual((await self.read("link.lean"))["error"], "path_outside_allowed_roots")
        self.assertEqual((await self.read(str(self.folder / "outside.lean")))["error"], "path_outside_allowed_roots")
        self.assertEqual((await self.read("big.lean"))["error"], "file_too_large")
        self.assertEqual((await self.read("missing.lean"))["error"], "unreadable")
        self.assertEqual((await self.read(".", limit=10**6))["error"], "not_a_regular_file")
        self.assertEqual((await self.read("Proof.lean", suffixes=[".md"]))["error"], "file_type_not_allowed")
        self.assertTrue((await self.read("notes.md", suffixes=[".md"]))["ok"])

    async def test_redirected_root_is_not_an_allowed_root(self):
        """ISO-5: an agent-owned root replaced by a symlink (here to '/') no longer counts as a root."""
        from research_swarm.inspect_adapter import read_agent_file
        wiki = self.folder / "wiki"
        os.symlink("/", wiki)
        target = self.folder / "outside.md"
        target.write_text("# not in any allowed root\n")
        through_root = str(wiki) + str(target)  # <wiki>/<absolute path of the file>
        read = await read_agent_file(_LocalSandbox(), through_root, [str(wiki), str(self.root)], 1000, [".md"])
        self.assertEqual(read["error"], "path_outside_allowed_roots")
        self.assertEqual(read["invalid_roots"], [str(wiki)])
        # A file genuinely inside the remaining literal root is still readable.
        ok = await read_agent_file(_LocalSandbox(), str(self.root / "notes.md"), [str(wiki), str(self.root)], 1000,
                                   [".md"])
        self.assertTrue(ok["ok"])
        # A root reached through a symlinked parent directory is rejected the same way.
        alias = self.folder / "alias"
        os.symlink(self.root, alias)
        aliased = await read_agent_file(_LocalSandbox(), str(alias / "notes.md"), [str(alias)], 1000, [".md"])
        self.assertEqual((aliased["error"], aliased["invalid_roots"]), ("path_outside_allowed_roots", [str(alias)]))

    async def test_invalid_roots_stay_researcher_side(self):
        from research_swarm.inspect_adapter import conference_tools
        wiki = self.folder / "wiki"
        os.symlink("/", wiki)

        class Service:
            def action(self, agent_id, tool, args):
                raise AssertionError("a rejected read must not reach the service")
        definitions = [{"name": "publish_notebook", "description": "Publish.",
                        "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string"}},
                                        "required": ["file_path"]},
                        "sandbox_file": {"arg": "file_path", "into": "content", "allowed_roots": [str(wiki)],
                                         "max_bytes": 1000, "allowed_suffixes": [".md"]}}]
        events = []
        tools = conference_tools(Service(), "agent-001", definitions, sbox=_LocalSandbox(),
                                 record=lambda *a: events.append(a))
        reply = json.loads(await tools[0](file_path=str(wiki) + str(self.root / "notes.md")))
        self.assertEqual(reply["error"], "path_outside_allowed_roots")
        self.assertNotIn("invalid_roots", reply)  # never shown to the agent
        self.assertEqual(events[-1][0], "sandbox_file_rejected")
        self.assertEqual(events[-1][2]["invalid_roots"], [str(wiki)])

    async def test_tools_refuse_new_actions_after_the_run_stop(self):
        from research_swarm.inspect_adapter import conference_tools
        calls, events, stop = [], [], asyncio.Event()

        class Service:
            def action(self, agent_id, tool, args):
                calls.append(tool)
                return {"ok": True}
        definitions = [{"name": "post_intent", "description": "Post.",
                        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}}}]
        tools = conference_tools(Service(), "agent-001", definitions, record=lambda *a: events.append(a), stop=stop)
        self.assertTrue(json.loads(await tools[0](message="before"))["ok"])
        stop.set()
        reply = json.loads(await tools[0](message="after"))
        self.assertEqual((reply["ok"], reply["error"]), (False, "conference_closed"))
        self.assertEqual(calls, ["post_intent"])
        self.assertEqual(events[-1][:2], ("conference_action_after_stop", "agent-001"))

    async def test_stop_during_file_read_refuses_dispatch(self):
        from research_swarm.inspect_adapter import conference_tools
        entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        service = unittest.mock.Mock()

        async def blocked_read(*args):
            entered.set()
            await release.wait()
            return {"ok": True, "content": "proof", "resolved": "/workspace/proof.lean", "size": 5}

        definition = {"name": "submit_solution", "description": "Submit.",
                      "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string"}}},
                      "sandbox_file": {"arg": "file_path", "into": "content", "allowed_roots": ["/workspace"],
                                       "max_bytes": 1000}}
        with unittest.mock.patch("research_swarm.inspect_adapter.read_agent_file", blocked_read):
            tool = conference_tools(service, "agent-001", [definition], sbox=object(), stop=stop)[0]
            task = asyncio.create_task(tool(file_path="/workspace/proof.lean"))
            await asyncio.wait_for(entered.wait(), 5)
            stop.set()
            release.set()
            self.assertEqual(json.loads(await task)["error"], "conference_closed")
        service.action.assert_not_called()

    async def test_stop_while_queued_refuses_worker_dispatch(self):
        from concurrent.futures import ThreadPoolExecutor
        from research_swarm.inspect_adapter import conference_tools
        stop, queued = asyncio.Event(), asyncio.Event()
        service = unittest.mock.Mock()
        release = threading.Event()

        class ObservedExecutor(ThreadPoolExecutor):
            def submit(self, fn, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                queued.set()
                return future

        with ObservedExecutor(max_workers=1) as executor:
            # Occupy its only worker; the conference action must wait in the queue.
            ThreadPoolExecutor.submit(executor, release.wait, 5)
            definition = {"name": "post_intent", "description": "Post.",
                          "inputSchema": {"type": "object", "properties": {}}}
            with unittest.mock.patch("research_swarm.inspect_adapter._executor", return_value=executor):
                tool = conference_tools(service, "agent-001", [definition], stop=stop)[0]
                task = asyncio.create_task(tool())
                try:
                    await asyncio.wait_for(queued.wait(), 5)
                    stop.set()
                    release.set()
                    self.assertEqual(json.loads(await task)["error"], "conference_closed")
                finally:
                    release.set()
        service.action.assert_not_called()

    async def test_tools_bind_identity_and_inject_content(self):
        from research_swarm.inspect_adapter import conference_tools
        calls = []

        class Service:
            def action(self, agent_id, tool, args):
                calls.append((agent_id, tool, args))
                return {"ok": True, "agent": agent_id}
        definitions = [
            {"name": "post_intent", "description": "Post.",
             "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
            {"name": "submit_solution", "description": "Submit.",
             "inputSchema": {"type": "object", "properties": {"problem_id": {"type": "string"},
                                                              "file_path": {"type": "string"}},
                             "required": ["problem_id", "file_path"]},
             "sandbox_file": {"arg": "file_path", "into": "source", "allowed_roots": [str(self.root)],
                              "max_bytes": 1000}}]
        events = []
        tools = conference_tools(Service(), "agent-003", definitions, sbox=_LocalSandbox(),
                                 record=lambda *a: events.append(a))
        result = json.loads(await tools[0](message="Working on algebra"))
        self.assertEqual(result["agent"], "agent-003")
        await tools[1](problem_id="p1", file_path=str(self.root / "Proof.lean"))
        self.assertEqual(calls[-1][0:2], ("agent-003", "submit_solution"))
        self.assertEqual(calls[-1][2]["source"], "theorem t : True := trivial\n")
        self.assertEqual(calls[-1][2]["file_path"], str(self.root / "Proof.lean"))
        rejected = json.loads(await tools[1](problem_id="p1", file_path=str(self.root / "link.lean")))
        self.assertFalse(rejected["ok"])
        self.assertEqual(len(calls), 2)  # the rejected read never reached the service
        self.assertEqual(events[-1][0], "sandbox_file_rejected")


_DOTENV_PROBE = r"""
import os, sys
if sys.argv[1] == "disabled":
    from research_swarm.inspect_adapter import disable_inspect_dotenv
    result = disable_inspect_dotenv()
    assert result["dotenv_disabled"] and result["credentials_in_environment"] is False, result
from inspect_ai._eval.context import init_runtime_context
from inspect_ai._eval import evalset
from inspect_ai._util import dotenv
init_runtime_context()  # what eval/eval_async run first
evalset.init_dotenv()   # eval_set
with dotenv.dotenv_environ():
    inside = os.environ.get("GDM_FAKE_DOTENV_PROBE")
print(os.environ.get("GDM_FAKE_DOTENV_PROBE"), inside)
"""


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class InspectDotenvTests(unittest.TestCase):
    """X1 / SPEND-3 / F1: Inspect's .env loading, exercised in a scratch directory with a
    fake value (never the repository .env), in a child process so the control is valid."""
    def probe(self, mode):
        folder = Path(tempfile.mkdtemp()).resolve()
        (folder / ".env").write_text("GDM_FAKE_DOTENV_PROBE=not-a-secret\n")
        env = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_API_KEY", "GDM_FAKE_DOTENV_PROBE")}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        done = subprocess.run([sys.executable, "-c", _DOTENV_PROBE, mode], cwd=folder, env=env, capture_output=True,
                              text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return done.stdout.split()

    def test_inspect_loads_dotenv_unless_disabled(self):
        self.assertEqual(self.probe("control"), ["not-a-secret", "not-a-secret"])  # the hazard is real
        self.assertEqual(self.probe("disabled"), ["None", "None"])

    def test_disable_is_idempotent_and_reports_presence_only(self):
        from research_swarm.inspect_adapter import credentials_in_environment, disable_inspect_dotenv
        first = disable_inspect_dotenv()
        second = disable_inspect_dotenv()
        self.assertTrue(first["dotenv_disabled"] and second["dotenv_disabled"])
        self.assertEqual(first["patched"], second["patched"])
        for name in ("find_dotenv", "load_dotenv", "dotenv_values", "init_dotenv"):
            self.assertIn(f"inspect_ai._util.dotenv.{name}", first["patched"])
        self.assertIn("inspect_ai._eval.context.init_dotenv", first["patched"])
        import inspect_ai._eval.context as context
        self.assertIsNone(context.init_dotenv())
        with unittest.mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": DUMMY_KEY}):
            self.assertIs(credentials_in_environment(), True)
            self.assertIs(disable_inspect_dotenv()["credentials_in_environment"], True)
            self.assertNotIn(DUMMY_KEY, json.dumps(disable_inspect_dotenv()))
        with unittest.mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            self.assertIs(credentials_in_environment(), False)

    def test_missing_module_fails_closed(self):
        from research_swarm.inspect_adapter import disable_inspect_dotenv
        with unittest.mock.patch.dict(sys.modules, {"inspect_ai._util.dotenv": None}):
            with self.assertRaises(RuntimeError):
                disable_inspect_dotenv()


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class ClaudeCodeConfigurationTests(unittest.TestCase):
    def test_agent_configuration(self):
        from research_swarm.inspect_adapter import CLAUDE_CODE_ENV, DISALLOWED_TOOLS, installed_versions
        self.assertTrue(all(v["matches"] for v in installed_versions().values()))
        self.assertEqual(CLAUDE_CODE_ENV["BASH_MAX_TIMEOUT_MS"], "120000")
        self.assertEqual(CLAUDE_CODE_ENV["BASH_DEFAULT_TIMEOUT_MS"], "120000")
        for name in ("Agent", "Task", "WebSearch", "WebFetch", "Workflow", "SendMessage", "Skill"):
            self.assertIn(name, DISALLOWED_TOOLS)
        for name in ("Bash", "Read", "Write", "Edit", "TaskStop"):  # the built-ins agents keep
            self.assertNotIn(name, DISALLOWED_TOOLS)
        self.assertFalse(any("KEY" in k or "TOKEN" in k for k in CLAUDE_CODE_ENV))

    def test_dockerfile_has_no_repository_content(self):
        text = (Path(__file__).resolve().parents[1] / "docker" / "agent.Dockerfile").read_text()
        instructions = [line.split()[0] for line in text.splitlines() if line and not line.startswith(("#", " "))]
        self.assertNotIn("COPY", instructions)
        self.assertNotIn("ADD", instructions)
        self.assertIn("claude-code@${CLAUDE_CODE_VERSION}", text)
        self.assertIn("ARG CLAUDE_CODE_VERSION=2.1.296", text)


def _status_error(status, request_id="req-offline", payload=None, body=None):
    """An APIStatusError on an httpx2 response, the type the pinned openai SDK raises from."""
    import httpx2
    import openai
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://openrouter.invalid/v1/chat/completions"),
                               headers={"x-request-id": request_id},
                               json=payload if payload is not None else {"error": {"message": "offline"}})
    return openai.APIStatusError("offline", response=response,
                                 body=body if body is not None else {"message": "offline", "code": status})


def _sdk_transport_error(cause_cls, message="offline"):
    """Exactly as openai 3.29.0's _base_client does: an httpx2 transport error caught by
    ``timeout_exceptions()`` / ``request_exceptions()`` and re-raised ``from`` it."""
    import httpx2
    import openai
    from openai._httpx2 import request_exceptions, timeout_exceptions
    request = httpx2.Request("POST", "https://openrouter.invalid/api/v1/chat/completions")
    try:
        try:
            raise cause_cls(message, request=request)
        except timeout_exceptions() as err:
            raise openai.APITimeoutError(request=request) from err
        except request_exceptions() as err:
            raise openai.APIConnectionError(request=request) from err
    except Exception as outer:
        return outer


def _connection_error(cause):
    """openai's wrapping of a legacy httpx transport error (raised ... from cause)."""
    import httpx
    import openai
    cls = openai.APITimeoutError if isinstance(cause, httpx.TimeoutException) else openai.APIConnectionError
    try:
        try:
            raise cause
        except Exception as inner:
            raise cls(request=httpx.Request("POST", "https://openrouter.invalid")) from inner
    except Exception as outer:
        return outer


async def _inspect_generate_error(transport_handler):
    """The exception Inspect's OpenRouter model raises when the SDK's own httpx2 client
    (the one Inspect builds) is given an offline MockTransport: the full production
    error path (Inspect -> openai SDK -> httpx2), with no network and a dummy key."""
    import httpx2
    from inspect_ai.model import ChatMessageUser, GenerateConfig, get_model
    model = get_model("openrouter/deepseek/deepseek-v4.1-flash", api_key=DUMMY_KEY, max_retries=0,
                      provider={"only": ["deepseek"], "allow_fallbacks": False}, reasoning_enabled=True)
    model.api.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(transport_handler))
    try:
        await model.generate([ChatMessageUser(content="hi")], config=GenerateConfig(max_retries=0))
    except Exception as ex:
        return ex
    raise AssertionError("generate did not raise")


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class ProviderErrorClassificationTests(unittest.TestCase):
    def classify(self, ex):
        from research_swarm.inspect_adapter import classify_generate_error
        return classify_generate_error(ex)

    def test_rejections_before_generation_are_unbilled(self):
        for status in (400, 401, 402, 403, 429):
            info = self.classify(_status_error(status))
            self.assertEqual((info["billing"], info["status"]), ("unbilled_error", status))
        self.assertEqual(self.classify(_status_error(429))["provider_request_id"], "req-offline")
        import httpx
        self.assertEqual(self.classify(_connection_error(httpx.ConnectError("refused")))["billing"], "unbilled_error")
        self.assertEqual(self.classify(_connection_error(httpx.ConnectTimeout("connect")))["billing"], "unbilled_error")

    def test_sdk_httpx2_never_connected_errors_are_unbilled(self):
        """SPEND-2: the pinned SDK raises from httpx2, whose classes are unrelated to httpx's."""
        import httpx
        import httpx2
        import openai
        self.assertFalse(issubclass(httpx2.ConnectError, httpx.ConnectError))
        for cause, wrapper in ((httpx2.ConnectError, openai.APIConnectionError),
                               (httpx2.ConnectTimeout, openai.APITimeoutError),
                               (httpx2.PoolTimeout, openai.APITimeoutError)):
            ex = _sdk_transport_error(cause)
            self.assertIs(type(ex), wrapper)
            self.assertIsInstance(ex.__cause__, cause)
            info = self.classify(ex)
            self.assertEqual((info["billing"], info["reason"]), ("unbilled_error", "connection_not_established"),
                             cause.__name__)
        for cause in (httpx2.ReadTimeout, httpx2.WriteTimeout, httpx2.RemoteProtocolError, httpx2.ReadError):
            info = self.classify(_sdk_transport_error(cause))
            self.assertEqual((info["billing"], info["reason"]), ("uncertain", "connection_lost_or_timeout"),
                             cause.__name__)

    def test_in_flight_failures_stay_uncertain(self):
        import httpx
        for status in (408, 500, 502, 503):
            self.assertEqual(self.classify(_status_error(status))["billing"], "uncertain")
        self.assertEqual(self.classify(_connection_error(httpx.ReadTimeout("read")))["billing"], "uncertain")
        self.assertEqual(self.classify(_connection_error(httpx.RemoteProtocolError("drop")))["billing"], "uncertain")
        self.assertEqual(self.classify(asyncio.CancelledError())["reason"], "cancelled_in_flight")
        self.assertEqual(self.classify(RuntimeError("other"))["billing"], "uncertain")

    def test_generation_id_from_error_body_or_response(self):
        self.assertIsNone(self.classify(_status_error(400))["provider_generation_id"])
        self.assertIn("provider_generation_id", self.classify(_status_error(400)))
        from_response = _status_error(502, payload={"id": "gen-1760-abc", "error": {"message": "upstream"}})
        info = self.classify(from_response)
        self.assertEqual((info["provider_generation_id"], info["provider_request_id"]), ("gen-1760-abc", "req-offline"))
        from_body = _status_error(400, body={"message": "x", "metadata": {"generation_id": "gen-meta"}})
        self.assertEqual(self.classify(from_body)["provider_generation_id"], "gen-meta")
        # An "id" that is not a generation id is never taken for one.
        self.assertIsNone(self.classify(_status_error(400, payload={"id": "user_123"}))["provider_generation_id"])
        import httpx
        self.assertIsNone(self.classify(_connection_error(httpx.ConnectError("refused")))["provider_generation_id"])

    def test_wrapped_errors_are_unwrapped(self):
        from inspect_ai.model._model import ModelGenerateError
        try:
            try:
                raise _status_error(400)
            except Exception as inner:
                raise ModelGenerateError("wrapped", status_code=400) from inner
        except Exception as wrapped:
            self.assertEqual(self.classify(wrapped)["billing"], "unbilled_error")


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class InspectSdkErrorPathTests(unittest.IsolatedAsyncioTestCase):
    """SPEND-2 through the production error path: Inspect's OpenRouter model, the
    pinned SDK and its httpx2 client, with an offline MockTransport (no network)."""
    async def classify_transport(self, cause):
        from research_swarm.inspect_adapter import classify_generate_error

        def handler(request):
            raise cause("offline", request=request)
        return classify_generate_error(await _inspect_generate_error(handler))

    async def test_never_connected_settles_unbilled(self):
        import httpx2
        for cause in (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            info = await self.classify_transport(cause)
            self.assertEqual((info["billing"], info["reason"]), ("unbilled_error", "connection_not_established"),
                             cause.__name__)

    async def test_lost_connection_stays_uncertain(self):
        import httpx2
        for cause in (httpx2.ReadTimeout, httpx2.RemoteProtocolError):
            info = await self.classify_transport(cause)
            self.assertEqual((info["billing"], info["reason"]), ("uncertain", "connection_lost_or_timeout"),
                             cause.__name__)

    async def test_status_errors_from_the_sdk(self):
        import httpx2
        from research_swarm.inspect_adapter import classify_generate_error
        for status, billing in ((429, "unbilled_error"), (502, "uncertain")):
            ex = await _inspect_generate_error(lambda request, status=status: httpx2.Response(
                status, json={"error": {"message": "offline"}}, headers={"x-request-id": "req-sdk"}))
            info = classify_generate_error(ex)
            self.assertEqual((info["billing"], info["status"], info["provider_request_id"]),
                             (billing, status, "req-sdk"))

    async def test_filter_settles_never_connected_at_zero(self):
        import httpx2
        from inspect_ai.model import ChatMessageUser, GenerateConfig, get_model
        from research_swarm.inspect_adapter import budget_filter
        model = get_model("openrouter/deepseek/deepseek-v4.1-flash", api_key=DUMMY_KEY, max_retries=0,
                          provider={"only": ["deepseek"], "allow_fallbacks": False}, reasoning_enabled=True)

        def refused(request):
            raise httpx2.ConnectError("offline: connection refused", request=request)
        model.api.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(refused))
        ledger = BudgetLedger(Path(tempfile.mkdtemp()) / "b.sqlite", {"validation": 100})
        events = []
        intercept = budget_filter("agent-001", ledger, ProviderSettings(pricing_verified=True), StartBarrier(1),
                                  lambda *a: events.append(a), scope="validation")
        with self.assertRaises(Exception):
            await intercept(model, [ChatMessageUser(content="hi")], [], None, GenerateConfig())
        snapshot = ledger.snapshot()["validation"]
        self.assertEqual((snapshot["unresolved_requests"], snapshot["committed_usd"]), (0, "0"))
        self.assertEqual(events[-1][0], "inference_unbilled")
        self.assertEqual(intercept.state["last_request_outcome"], "error")
        self.assertEqual(intercept.state["in_flight"], 0)


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class BudgetFilterErrorTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, behavior, ledger=None):
        from inspect_ai.model import ChatMessageUser, GenerateConfig
        from research_swarm.inspect_adapter import budget_filter

        class FakeModel:
            async def generate(self, **kwargs):
                return behavior()
        folder = tempfile.mkdtemp()
        ledger = ledger or BudgetLedger(Path(folder) / "budget.sqlite", {"validation": 100})
        events = []
        intercept = budget_filter("agent-001", ledger, ProviderSettings(pricing_verified=True), StartBarrier(1),
                                  lambda *a: events.append(a), scope="validation")
        try:
            result = await intercept(FakeModel(), [ChatMessageUser(content="hello")], [], None, GenerateConfig())
        except Exception as ex:
            result = ex
        return result, ledger, events, intercept.state

    def raising(self, ex):
        def behavior():
            raise ex
        return behavior

    async def test_rate_limit_settles_unbilled(self):
        result, ledger, events, state = await self._run(self.raising(_status_error(429)))
        self.assertIsInstance(result, Exception)
        snapshot = ledger.snapshot()["validation"]
        self.assertEqual((snapshot["unresolved_requests"], snapshot["committed_usd"]), (0, "0"))
        self.assertEqual(events[-1][0], "inference_unbilled")
        self.assertEqual(events[-1][2]["status"], 429)
        self.assertEqual(state["unbilled_errors"], 1)

    async def test_server_error_stays_uncertain(self):
        result, ledger, events, state = await self._run(self.raising(_status_error(502)))
        self.assertEqual(ledger.snapshot()["validation"]["unresolved_requests"], 1)
        self.assertEqual(events[-1][0], "inference_uncertain")
        # Recorded explicitly (null) when the error carries no generation id.
        self.assertIn("provider_generation_id", events[-1][2])
        self.assertIsNone(events[-1][2]["provider_generation_id"])
        payload = {"id": "gen-err-7", "error": {"message": "upstream"}}
        _, _, events, _ = await self._run(self.raising(_status_error(502, payload=payload)))
        self.assertEqual((events[-1][0], events[-1][2]["provider_generation_id"]), ("inference_uncertain", "gen-err-7"))

    async def test_rejected_output_without_usage_is_unbilled(self):
        from inspect_ai.model import ModelOutput
        output = ModelOutput.from_content(model="m", content="context too long", stop_reason="model_length")
        result, ledger, events, state = await self._run(lambda: output)
        self.assertEqual(ledger.snapshot()["validation"]["unresolved_requests"], 0)
        finished = [e for e in events if e[0] == "inference_finished"][0][2]
        self.assertEqual((finished["basis"], finished["settled_usd"]), ("unbilled_error", "0"))

    async def test_generation_id_recorded(self):
        from inspect_ai.model import ModelOutput, ModelUsage
        output = ModelOutput.from_content(model="m", content="ok")
        output.usage = ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7)
        output.metadata = {"openrouter_usage": {"cost": 0.00001}, "openrouter_generation_id": "gen-123",
                           "openrouter_provider": "DeepSeek"}
        _, _, events, _ = await self._run(lambda: output)
        finished = [e for e in events if e[0] == "inference_finished"][0][2]
        self.assertEqual((finished["provider_generation_id"], finished["provider_name"]), ("gen-123", "DeepSeek"))

    async def test_consecutive_truncations_tracked_and_stop_reason_kept(self):
        from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelOutput
        from research_swarm.inspect_adapter import budget_filter
        outputs = []
        for stop in ("max_tokens", "max_tokens", "stop", "max_tokens"):
            output = ModelOutput.for_tool_call(model="m", tool_name="Bash", tool_arguments={"command": "ls"})
            output.choices[0].stop_reason = stop
            outputs.append(output)

        class FakeModel:
            async def generate(self, **kwargs):
                return outputs.pop(0)
        events = []
        intercept = budget_filter("agent-001", BudgetLedger(Path(tempfile.mkdtemp()) / "b.sqlite", {"validation": 100}),
                                  ProviderSettings(pricing_verified=True), StartBarrier(1),
                                  lambda *a: events.append(a), scope="validation")
        seen = []
        for _ in range(4):
            result = await intercept(FakeModel(), [ChatMessageUser(content="hi")], [], None, GenerateConfig())
            seen.append((result.stop_reason, bool(result.message.tool_calls), intercept.state["consecutive_truncations"]))
        self.assertEqual(seen, [("max_tokens", False, 1), ("max_tokens", False, 2), ("stop", True, 0),
                                ("max_tokens", False, 1)])
        self.assertEqual(intercept.state["truncated_responses"], 3)

    async def test_locked_ledger_retries_then_admits(self):
        import sqlite3
        from inspect_ai.model import ModelOutput
        import research_swarm.inspect_adapter as adapter
        real = BudgetLedger(Path(tempfile.mkdtemp()) / "b.sqlite", {"validation": 100})
        failures = [sqlite3.OperationalError("database is locked")]

        class FlakyLedger:
            def reserve(self, *args):
                if failures:
                    raise failures.pop()
                return real.reserve(*args)

            def __getattr__(self, name):
                return getattr(real, name)
        output = ModelOutput.from_content(model="m", content="ok")
        with unittest.mock.patch.object(adapter, "RESERVE_RETRY_DELAYS", (0.0, 0.0)):
            result, _, events, _ = await self._run(lambda: output, ledger=FlakyLedger())
        self.assertIs(result, output)
        self.assertEqual([e[0] for e in events][:2], ["budget_admission_retry", "inference_admitted"])

    async def test_persistently_locked_ledger_fails_closed(self):
        import sqlite3
        from inspect_ai.model import ModelOutput
        from inspect_ai.util import LimitExceededError
        import research_swarm.inspect_adapter as adapter

        class LockedLedger:
            def reserve(self, *args):
                raise sqlite3.OperationalError("database is locked")
        with unittest.mock.patch.object(adapter, "RESERVE_RETRY_DELAYS", (0.0, 0.0)):
            result, _, events, _ = await self._run(lambda: ModelOutput.from_content(model="m", content="x"),
                                                   ledger=LockedLedger())
        self.assertIsInstance(result, LimitExceededError)
        self.assertEqual(events[-1][2]["reason"], "budget_ledger_unavailable")


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class BuildClaudeAgentTests(unittest.TestCase):
    def test_reasoning_bridge_installed_before_agent_construction(self):
        from research_swarm.inspect_adapter import build_claude_agent
        order = []
        with unittest.mock.patch("research_swarm.reasoning_bridge.install_reasoning_bridge",
                                 side_effect=lambda: order.append("bridge")), \
                unittest.mock.patch("inspect_swe.claude_code", side_effect=lambda **kw: order.append("agent")):
            build_claude_agent("PROMPT", [], None)
        self.assertEqual(order, ["bridge", "agent"])

    def build(self, **kwargs):
        from research_swarm.inspect_adapter import build_claude_agent
        captured = {}

        def fake_claude_code(**options):
            captured.update(options)
            return "agent"
        with unittest.mock.patch("inspect_swe.claude_code", fake_claude_code):
            build_claude_agent("PROMPT", [], None, **kwargs)
        return captured

    def test_default_replaces_and_keeps_auto_memory(self):
        options = self.build()
        self.assertEqual(options["replace_system_prompt"], "PROMPT")
        self.assertNotIn("system_prompt", options)
        self.assertEqual(options["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "0")
        self.assertEqual(options["env"]["CLAUDE_CODE_MAX_RETRIES"], "4")
        self.assertEqual(options["version"], "sandbox")

    def test_append_mode_and_memory_off(self):
        options = self.build(system_prompt_mode="append", auto_memory=False)
        self.assertEqual(options["system_prompt"], "PROMPT")
        self.assertNotIn("replace_system_prompt", options)
        self.assertEqual(options["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        with self.assertRaises(ValueError):
            self.build(system_prompt_mode="prepend")

    def test_installed_inspect_swe_parameter_names(self):
        import inspect as pyinspect
        from inspect_swe import claude_code
        parameters = pyinspect.signature(claude_code).parameters
        for name in ("system_prompt", "replace_system_prompt", "filter", "bridged_tools", "disallowed_tools", "env"):
            self.assertIn(name, parameters)


if __name__ == "__main__":
    unittest.main()
