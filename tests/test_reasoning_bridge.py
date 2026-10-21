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

    async def test_resumed_session_bridge_replays_own_reasoning_within_agent_scope(self):
        """A resumed Claude Code session gets a new AgentBridge (runtime auto_restart); inside one
        agent_transport_scope it shares the agent's key, so the agent's own blocks replay."""
        import anyio
        with transport.agent_transport_scope():
            blocks = await self.blocks(bridge=SimpleNamespace())
            replayed = {}

            async def resumed_bridge_service():  # as run_model_service: a task started inside
                replayed["messages"] = await self.replay(blocks, bridge=SimpleNamespace())
            async with anyio.create_task_group() as group:
                group.start_soon(resumed_bridge_service)
        self.assertEqual(replayed["messages"][0].content, self.message.content)

    async def test_agent_scopes_do_not_share_keys(self):
        from inspect_ai.agent._bridge._errors import BridgePolicyError
        with transport.agent_transport_scope():
            blocks = await self.blocks(bridge=SimpleNamespace())
        with transport.agent_transport_scope():
            with self.assertRaisesRegex(BridgePolicyError, "Invalid or foreign"):
                await self.replay(blocks, bridge=SimpleNamespace())
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

    def gemini_completion(self, details, tool_ids):
        """An OpenRouter chat completion as Gemini 3.1 Pro returns it: thought signatures in
        ``reasoning_details`` (format google-gemini-v1) next to the tool calls."""
        from openai.types.chat import ChatCompletion
        return ChatCompletion.model_validate({
            "id": "gen-offline", "object": "chat.completion", "created": 1, "model": "google/gemini-3.1-pro-preview",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "reasoning_details": details,
                "tool_calls": [{"id": tool_id, "type": "function",
                                "function": {"name": "Bash", "arguments": "{\"command\": \"true\"}"}}
                               for tool_id in tool_ids]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    async def test_gemini_thought_signatures_survive_a_multi_turn_tool_call(self):
        """Gemini reasoning_details (thought signatures) through the whole path: OpenRouter response ->
        Inspect -> Anthropic blocks for Claude Code -> Claude Code's next request (assistant turn echoed,
        tool result added) -> Inspect -> OpenRouter request. The signed details come back on the same
        assistant message as its tool calls, with the tool call ids unchanged."""
        from inspect_ai.model import get_model
        from research_swarm.inspect_adapter import replayed_reasoning_blocks, replayed_signed_reasoning
        api = get_model("openrouter/google/gemini-3.1-pro-preview", api_key="unpaid-offline-test-placeholder").api
        cases = {
            # readable thought plus the encrypted signature bound to the tool call
            "text and encrypted": ([{"type": "reasoning.text", "text": "Try induction on n.",
                                     "format": "google-gemini-v1", "index": 0},
                                    {"type": "reasoning.encrypted", "data": "CiQB-signature-not-a-secret",
                                     "id": "tool_Bash_a1", "format": "google-gemini-v1", "index": 0}],
                                   ["tool_Bash_a1"], "thinking"),
            # a thought carried only as a signature (no readable text), two parallel calls
            "signature only": ([{"type": "reasoning.text", "signature": "EpgB-signature-not-a-secret",
                                 "format": "google-gemini-v1", "index": 0}],
                               ["tool_Bash_b1", "tool_Bash_b2"], "redacted_thinking"),
        }
        for name, (details, tool_ids, block_type) in cases.items():
            with self.subTest(case=name):
                message = api.chat_choices_from_completion(self.gemini_completion(details, tool_ids), [])[0].message
                self.assertEqual([c.id for c in message.tool_calls], tool_ids)
                blocks = await self.blocks(message)
                self.assertEqual([b.type for b in blocks], [block_type, "text", *["tool_use"] * len(tool_ids)])
                self.assertEqual([b.id for b in blocks if b.type == "tool_use"], tool_ids)
                self.assertNotIn("signature-not-a-secret", str(blocks[0]))  # only the authenticated transport
                # Claude Code's next request: the assistant turn as it was returned, then the tool results.
                raw = [{"role": "user", "content": "Begin your conference research."},
                       {"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in blocks]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
                                                    for tool_id in tool_ids]}]
                with transport._bridge_scope(self.bridge):
                    replayed = await self.impl.messages_from_anthropic_input(raw, [])
                # The reasoning block is restored exactly (the SDK renders the empty text as "(no content)").
                self.assertEqual(replayed[1].content[0], message.content[0])
                self.assertEqual(replayed[1].tool_calls, message.tool_calls)
                self.assertEqual(replayed_reasoning_blocks(replayed), 1)
                self.assertEqual(replayed_signed_reasoning(replayed),
                                 {"replayed_signed_tool_call_turns": 1,
                                  "replayed_signed_reasoning_formats": ["google-gemini-v1"]})
                wire = await api.messages_to_openai(replayed)
                assistant = wire[1]
                self.assertEqual([c["id"] for c in assistant["tool_calls"]], tool_ids)
                # Signed entries are replayed verbatim; unsigned Gemini text is dropped by Inspect's sanitizer.
                signed = [d for d in details if d.get("signature") or d.get("type") == "reasoning.encrypted"]
                self.assertEqual(assistant["reasoning_details"], signed)
                self.assertNotIn("reasoning_content", assistant)
                self.assertEqual([m.get("tool_call_id") for m in wire[2:]], tool_ids)
                self.assertNotIn(transport._PREFIX, str(wire))

    async def test_gemini4_argon_thought_signatures_survive_a_multi_turn_tool_call(self):
        """Gemini 4 Argon through the same path. Its reasoning_details shape is not yet observed live;
        Inspect stores the raw details array, so the transport is format-agnostic: the google-gemini-v1
        shape of 3.1 Pro and an unrecognized future format or detail type both replay verbatim."""
        from inspect_ai.model import get_model
        from research_swarm.inspect_adapter import replayed_signed_reasoning
        api = get_model("openrouter/google/gemini-4-argon", api_key="unpaid-offline-test-placeholder").api
        cases = {
            "google-gemini-v1": [{"type": "reasoning.text", "text": "Bound the sum by telescoping.",
                                  "signature": "EpgB-signature-not-a-secret", "format": "google-gemini-v1",
                                  "index": 0},
                                 {"type": "reasoning.encrypted", "data": "CiQB-signature-not-a-secret",
                                  "id": "tool_Bash_d1", "format": "google-gemini-v1", "index": 0}],
            "new format": [{"type": "reasoning.encrypted", "data": "CiQB-signature-not-a-secret",
                            "id": "tool_Bash_d1", "format": "google-gemini-v2", "index": 0}],
            "new detail type": [{"type": "reasoning.thought_signature", "data": "CiQB-signature-not-a-secret",
                                 "format": "google-gemini-v2", "index": 0}],
        }
        for name, details in cases.items():
            with self.subTest(case=name):
                completion = self.gemini_completion(details, ["tool_Bash_d1"])
                completion.model = "google/gemini-4-argon"
                message = api.chat_choices_from_completion(completion, [])[0].message
                blocks = await self.blocks(message)
                # Opaque payloads (including details Inspect cannot parse) are never shown as thinking text.
                self.assertNotIn("signature-not-a-secret", str(blocks))
                self.assertEqual(blocks[0].type, "thinking" if name == "google-gemini-v1" else "redacted_thinking")
                raw = [{"role": "user", "content": "Begin your conference research."},
                       {"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in blocks]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool_Bash_d1",
                                                     "content": "ok"}]}]
                with transport._bridge_scope(self.bridge):
                    replayed = await self.impl.messages_from_anthropic_input(raw, [])
                self.assertEqual(replayed[1].content[0], message.content[0])
                # An unknown detail type is not counted as signed, so the live compatibility check's
                # signed_reasoning_tool_call_replay fails closed until the new shape is reviewed.
                if name != "new detail type":
                    self.assertEqual(replayed_signed_reasoning(replayed)["replayed_signed_tool_call_turns"], 1)
                assistant = (await api.messages_to_openai(replayed))[1]
                self.assertEqual(assistant["reasoning_details"], details)
                self.assertEqual([c["id"] for c in assistant["tool_calls"]], ["tool_Bash_d1"])

    async def test_without_the_transport_gemini_signatures_would_be_lost(self):
        """The pinned SDK alone renders OpenRouter reasoning as text for Claude Code, so the next request
        carries no reasoning_details: the reason research_swarm.reasoning_bridge is installed."""
        from inspect_ai.model import get_model
        api = get_model("openrouter/google/gemini-3.1-pro-preview", api_key="unpaid-offline-test-placeholder").api
        details = [{"type": "reasoning.encrypted", "data": "CiQB-signature-not-a-secret", "id": "tool_Bash_c1",
                    "format": "google-gemini-v1", "index": 0}]
        message = api.chat_choices_from_completion(self.gemini_completion(details, ["tool_Bash_c1"]), [])[0].message
        blocks = await self.impl.assistant_message_blocks(message)  # outside a bridge request scope
        self.assertNotIn("thinking", [b.type for b in blocks])
        raw = [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in blocks]}]
        replayed = await self.impl.messages_from_anthropic_input(raw, [])
        self.assertNotIn("reasoning_details", (await api.messages_to_openai(replayed))[0])

    def test_installation_is_idempotent_and_version_guarded(self):
        wrapped = self.impl.inspect_anthropic_api_request_impl
        transport.install_reasoning_bridge()
        self.assertIs(wrapped, self.impl.inspect_anthropic_api_request_impl)
        with patch.object(transport.importlib.metadata, "version", return_value="0.0.0"):
            with self.assertRaisesRegex(RuntimeError, "requires inspect-ai"):
                transport.install_reasoning_bridge()


if __name__ == "__main__":
    unittest.main()
