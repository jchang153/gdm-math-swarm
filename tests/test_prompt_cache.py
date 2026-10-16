"""Offline regression checks for request-only token-counter normalization."""

import importlib.util
import unittest

from research_swarm.inspect_adapter import disable_inspect_dotenv
from research_swarm.prompt_cache import (
    TOKEN_COUNTER_NORMALIZATION_VERSION, normalize_token_counters,
)

disable_inspect_dotenv()
HAVE_INSPECT = importlib.util.find_spec("inspect_ai") is not None
COUNTER = "<total_tokens>14982386 tokens left</total_tokens>"


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class TokenCounterTests(unittest.TestCase):
    def test_historical_counter_keeps_payload_and_input_history(self):
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser
        leading = ChatMessageSystem(content=COUNTER)
        anchor = ChatMessageUser(content="Solve the task")
        counter = ChatMessageSystem(content=COUNTER, id="counter-id", source="input",
                                    metadata={"nested": {"origin": "scaffold"}})
        messages = [leading, anchor, counter]
        original = [message.model_dump() for message in messages]
        effective, count = normalize_token_counters(messages)
        self.assertEqual(count, 1)
        self.assertIsNot(effective, messages)
        self.assertIs(effective[0], leading)
        self.assertIs(effective[1], anchor)
        self.assertIsInstance(effective[2], ChatMessageUser)
        self.assertNotIsInstance(effective[2], ChatMessageSystem)
        self.assertEqual(effective[2].model_dump(exclude={"role", "tool_call_id"}),
                         counter.model_dump(exclude={"role"}))
        self.assertEqual([message.model_dump() for message in messages], original)
        again, count = normalize_token_counters(effective)
        self.assertIs(again, effective)
        self.assertEqual(count, 0)
        self.assertEqual(TOKEN_COUNTER_NORMALIZATION_VERSION, "counter-user-v1")

    def test_only_exact_standalone_historical_system_text_changes(self):
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser, ChatMessageTool
        from inspect_ai.model import ContentText
        variants = [" " + COUNTER, COUNTER + "\n", "Instructions\n" + COUNTER,
                    COUNTER.replace("14982386", "14,982,386"),
                    COUNTER.replace("14982386", "١٤٩٨٢٣٨٦"),
                    COUNTER.replace("14982386", "-1"),
                    COUNTER.replace("14982386", "1.5")]
        messages = [ChatMessageSystem(content=COUNTER), ChatMessageSystem(content="Instructions"),
                    ChatMessageUser(content=COUNTER)]
        messages.extend(ChatMessageSystem(content=text) for text in variants)
        messages.extend([ChatMessageSystem(content=[ContentText(text=COUNTER)]),
                         ChatMessageTool(content=COUNTER, tool_call_id="t")])
        effective, count = normalize_token_counters(messages)
        self.assertIs(effective, messages)
        self.assertEqual(count, 0)
        self.assertEqual(normalize_token_counters([]), ([], 0))

    def test_reasoning_and_tool_cycle_keep_identity_and_order(self):
        from inspect_ai.model import (ChatMessageSystem, ChatMessageUser,
                                      ChatMessageAssistant, ChatMessageTool, ContentReasoning)
        from inspect_ai.tool import ToolCall
        messages = [ChatMessageSystem(content="Instructions"), ChatMessageUser(content="Task"),
                    ChatMessageAssistant(content=[ContentReasoning(reasoning="work")],
                                         tool_calls=[ToolCall(id="t", function="probe", arguments={})]),
                    ChatMessageTool(content="result", tool_call_id="t", function="probe"),
                    ChatMessageSystem(content=COUNTER), ChatMessageSystem(content="Actual instruction"),
                    ChatMessageSystem(content=COUNTER)]
        effective, count = normalize_token_counters(messages)
        self.assertEqual(count, 2)
        for index in (0, 1, 2, 3, 5):
            self.assertIs(effective[index], messages[index])
        self.assertEqual([m.content for m in effective], [m.content for m in messages])
        self.assertEqual(effective[2].tool_calls[0].id, effective[3].tool_call_id)

    def test_none_id_is_not_replaced_with_generated_identifier(self):
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser
        counter = ChatMessageSystem.model_construct(content=COUNTER, id=None)
        counter.id = None
        effective, count = normalize_token_counters([ChatMessageUser(content="Task"), counter])
        self.assertEqual(count, 1)
        self.assertIsNone(effective[1].id)


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class OpenRouterSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_serializer_changes_only_the_counter_role(self):
        from inspect_ai.model import (get_model, ChatMessageSystem, ChatMessageUser,
                                      ChatMessageAssistant, ChatMessageTool, ContentReasoning)
        from inspect_ai.tool import ToolCall
        # An inert placeholder and the direct serializer method: no HTTP request,
        # credential loader, generated completion or model inference is involved.
        model = get_model("openrouter/deepseek/deepseek-v4.1-flash",
                          api_key="unpaid-offline-placeholder", reasoning_enabled=True)
        messages = [ChatMessageSystem(content="Instructions"), ChatMessageUser(content="Task"),
                    ChatMessageAssistant(content=[ContentReasoning(reasoning="work")],
                                         tool_calls=[ToolCall(id="t", function="probe", arguments={})]),
                    ChatMessageTool(content="result", tool_call_id="t", function="probe"),
                    ChatMessageSystem(content=COUNTER)]
        before = await model.api.messages_to_openai(messages)
        effective, count = normalize_token_counters(messages)
        after = await model.api.messages_to_openai(effective)
        self.assertEqual(count, 1)
        self.assertEqual(before[:-1], after[:-1])
        self.assertEqual(before[-1]["role"], "system")
        self.assertEqual(after[-1]["role"], "user")
        self.assertEqual({k: v for k, v in before[-1].items() if k != "role"},
                         {k: v for k, v in after[-1].items() if k != "role"})
        self.assertEqual(after[2]["reasoning_content"], "work")
        self.assertEqual(after[2]["tool_calls"][0]["id"], after[3]["tool_call_id"])

@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class BudgetFilterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_and_reservation_use_normalized_history(self):
        import json
        import tempfile
        from decimal import Decimal
        from pathlib import Path
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser, GenerateConfig, ModelOutput, ModelUsage
        from research_swarm.budget import BudgetLedger
        from research_swarm.inspect_adapter import budget_filter
        from research_swarm.provider import ProviderSettings
        from research_swarm.runtime import StartBarrier

        class FakeModel:
            async def generate(self, **kwargs):
                self.request = kwargs
                output = ModelOutput.from_content(model="offline", content="OK")
                output.usage = ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
                output.metadata = {"openrouter_usage": {"cost": 0.000001}}
                return output

        class Archive:
            def observe(self, agent, messages, tools):
                self.messages = messages

        messages = [ChatMessageSystem(content="Preserve instructions"),
                    ChatMessageUser(content="Task"), ChatMessageSystem(content=COUNTER)]
        before = [m.model_dump() for m in messages]
        events = []
        settings = ProviderSettings(pricing_verified=True)
        archive, model = Archive(), FakeModel()
        with tempfile.TemporaryDirectory() as folder:
            ledger = BudgetLedger(Path(folder) / "budget.sqlite", {"validation": 1})
            intercept = budget_filter("agent-001", ledger, settings, StartBarrier(1),
                                      lambda *args: events.append(args), scope="validation", archive=archive)
            await intercept(model, messages, [], None, GenerateConfig())
            sent = model.request["input"]
            self.assertIs(archive.messages, messages)
            self.assertIsInstance(archive.messages[-1], ChatMessageSystem)
            self.assertIsInstance(sent[-1], ChatMessageUser)
            self.assertEqual(sent[-1].content, COUNTER)
            self.assertEqual([m.model_dump() for m in messages], before)
            request = json.dumps({"messages": [m.model_dump(mode="json") for m in sent], "tools": []},
                                 ensure_ascii=False)
            admitted = next(e[2] for e in events if e[0] == "inference_admitted")
            self.assertEqual(admitted["normalized_token_counters"], 1)
            self.assertEqual(admitted["prompt_cache_normalization"], TOKEN_COUNTER_NORMALIZATION_VERSION)
            self.assertEqual(admitted["request_bytes"], len(request.encode("utf-8")))
            self.assertEqual(Decimal(admitted["reserved_usd"]), settings.reservation(request))
            self.assertFalse(model.request["config"].cache)
            self.assertEqual(model.request["config"].reasoning_history, "all")
            self.assertFalse(model.request["config"].extra_body["provider"]["allow_fallbacks"])
            snapshot = ledger.snapshot()["validation"]
            self.assertEqual(Decimal(snapshot["settled_usd"]), Decimal("0.000001"))
            self.assertEqual(snapshot["unresolved_requests"], 0)
