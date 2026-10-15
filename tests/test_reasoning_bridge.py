"""Real pinned-SDK conversion regressions; no provider requests or credentials."""
import copy
import importlib.util
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from research_swarm import reasoning_bridge as transport


HAVE_INSPECT = importlib.util.find_spec("inspect_ai") is not None


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect SDK not installed")
class ReasoningBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        transport.install_reasoning_bridge()
        from inspect_ai.agent._bridge import anthropic_api_impl
        from inspect_ai.model import ChatMessageAssistant, ContentText
        from inspect_ai.model._openrouter_reasoning import openrouter_reasoning_details_to_reasoning
        from inspect_ai.tool import ToolCall
        self.impl = anthropic_api_impl
        self.bridge = SimpleNamespace()
        self.details = [{"type": "reasoning.text", "text": "Check the base case, then induction.",
                         "format": "unknown", "index": 0}]
        self.reasoning = openrouter_reasoning_details_to_reasoning(self.details)
        self.message = ChatMessageAssistant(
            content=[self.reasoning, ContentText(text="I will check the proof.")],
            tool_calls=[ToolCall(id="call-" + uuid.uuid4().hex, function="Bash", arguments={"command": "true"})])

    async def blocks(self, message=None, *, bridge=None, beta=False):
        with transport._bridge_scope(bridge or self.bridge):
            return await self.impl.assistant_message_blocks(message or self.message, beta=beta)

    async def replay(self, blocks, *, bridge=None):
        raw = [{"role": "assistant", "content": [b.model_dump(exclude_none=True)
                                                  if hasattr(b, "model_dump") else b for b in blocks]}]
        with transport._bridge_scope(bridge or self.bridge):
            return await self.impl.messages_from_anthropic_input(raw, [])

    async def test_full_openrouter_bridge_round_trip_retains_exact_details_and_tools(self):
        from inspect_ai.model import get_model
        from research_swarm.inspect_adapter import replayed_reasoning_blocks
        blocks = await self.blocks()
        self.assertEqual([b.type for b in blocks], ["thinking", "text", "tool_use"])
        self.assertEqual(blocks[0].thinking, self.reasoning.reasoning)
        replayed = await self.replay(blocks)
        self.assertEqual(replayed[0].content, self.message.content)
        self.assertEqual(replayed[0].tool_calls, self.message.tool_calls)
        self.assertEqual(replayed_reasoning_blocks(replayed), 1)
        # Constructing with an explicit dummy key does not load real credentials.
        api = get_model("openrouter/deepseek/deepseek-v4.1-flash",
                        api_key="unpaid-offline-test-placeholder").api
        wire = (await api.messages_to_openai(replayed))[0]
        original_wire = (await api.messages_to_openai([self.message]))[0]
        self.assertEqual(wire["reasoning_details"], self.details)
        self.assertEqual(wire["reasoning_content"], self.reasoning.reasoning)
        self.assertEqual(wire["content"], original_wire["content"])
        self.assertEqual(wire["tool_calls"], original_wire["tool_calls"])
        self.assertEqual(wire["tool_calls"][0]["id"], self.message.tool_calls[0].id)
        self.assertNotIn(transport._PREFIX, str(wire))

    async def test_beta_endpoint_and_opaque_reasoning_restore_exact_original(self):
        from inspect_ai.model import ChatMessageAssistant
        from inspect_ai.model._openrouter_reasoning import openrouter_reasoning_details_to_reasoning
        original = openrouter_reasoning_details_to_reasoning(
            [{"type": "reasoning.encrypted", "data": "opaque-not-a-secret", "format": "unknown", "index": 0}])
        blocks = await self.blocks(ChatMessageAssistant(content=[original]), beta=True)
        self.assertEqual(blocks[0].type, "redacted_thinking")
        self.assertEqual((await self.replay(blocks))[0].content[0], original)

    async def test_readable_summary_with_opaque_payload_preserves_all_fields(self):
        from inspect_ai.model import ChatMessageAssistant, ContentReasoning
        original = ContentReasoning(reasoning="opaque-data", summary="A readable summary",
                                    redacted=True, signature=self.reasoning.signature,
                                    internal={"original": "metadata"})
        blocks = await self.blocks(ChatMessageAssistant(content=[original]))
        self.assertEqual(blocks[0].thinking, "A readable summary")
        self.assertEqual((await self.replay(blocks))[0].content[0], original)

    async def test_modified_thinking_or_signature_fails_closed(self):
        from inspect_ai.agent._bridge._errors import BridgePolicyError
        raw = [b.model_dump(exclude_none=True) for b in await self.blocks()]
        altered_text = copy.deepcopy(raw)
        altered_text[0]["thinking"] += " modified"
        altered_sig = copy.deepcopy(raw)
        altered_sig[0]["signature"] += "x"
        malformed = copy.deepcopy(raw)
        malformed[0]["signature"] = transport._PREFIX + "invalid"
        for blocks in (altered_text, altered_sig, malformed):
            with self.subTest(block=blocks[0]["signature"][-10:]):
                with self.assertRaisesRegex(BridgePolicyError, "Invalid or foreign"):
                    await self.replay(blocks)

    async def test_cross_agent_marker_cannot_be_replayed(self):
        from inspect_ai.agent._bridge._errors import BridgePolicyError
        blocks = await self.blocks()
        with self.assertRaisesRegex(BridgePolicyError, "Invalid or foreign"):
            await self.replay(blocks, bridge=SimpleNamespace())

    async def test_compaction_omission_does_not_reconstruct_reasoning(self):
        from research_swarm.inspect_adapter import replayed_reasoning_blocks
        blocks = await self.blocks()
        replayed = await self.replay(blocks[1:])
        self.assertEqual(replayed_reasoning_blocks(replayed), 0)
        self.assertEqual([p.type for p in replayed[0].content], ["text"])
        self.assertEqual(replayed[0].tool_calls, self.message.tool_calls)

    async def test_native_anthropic_thinking_uses_unchanged_sdk_semantics(self):
        from inspect_ai.model import ChatMessageAssistant, ContentReasoning
        original = ContentReasoning(reasoning="native-signature", summary="native summary", redacted=True)
        blocks = await self.blocks(ChatMessageAssistant(content=[original]))
        self.assertEqual(blocks[0].signature, "native-signature")
        self.assertEqual((await self.replay(blocks))[0].content[0], original)

    async def test_converter_outside_request_scope_is_unchanged(self):
        blocks = await self.impl.assistant_message_blocks(self.message)
        # SDK's original interoperability behavior: plain <think> text.
        self.assertEqual(blocks[0].type, "text")
        self.assertEqual(blocks[0].text, self.reasoning.text)

    async def test_request_boundary_establishes_scope_and_resets_after_error(self):
        def abort(*args, **kwargs):
            self.assertIsNotNone(transport._SCOPE.get())
            self.assertEqual(transport._SCOPE.get(), getattr(self.bridge, transport._KEY_ATTRIBUTE))
            raise RuntimeError("offline boundary reached")
        with patch.object(self.impl, "resolve_bridge_model", side_effect=abort):
            # Resolution needs bridge routing attributes, but no model or client.
            self.bridge.model_aliases = self.bridge.model_resolver = self.bridge.model = None
            self.bridge.allow_client_model_names = False
            with self.assertRaisesRegex(RuntimeError, "offline boundary"):
                await self.impl.inspect_anthropic_api_request_impl(
                    {"model": "offline"}, None, None, None, self.bridge)
        self.assertIsNone(transport._SCOPE.get())

    def test_installation_is_idempotent_and_version_guarded(self):
        wrapped = self.impl.inspect_anthropic_api_request_impl
        transport.install_reasoning_bridge()
        self.assertIs(wrapped, self.impl.inspect_anthropic_api_request_impl)
        with patch.object(transport.importlib.metadata, "version", return_value="0.0.0"):
            with self.assertRaisesRegex(RuntimeError, "requires inspect-ai"):
                transport.install_reasoning_bridge()


if __name__ == "__main__":
    unittest.main()
