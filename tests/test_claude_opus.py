"""Claude Opus 5.5 through OpenRouter: settings, signed thinking round trip and prompt caching.

Offline only: synthetic OpenRouter payloads in the shapes OpenRouter documents for Anthropic
(reasoning_details format anthropic-claude-v1), a catalog entry recorded from the public
OpenRouter catalog on October 21, 2026, and the pinned Inspect SDK. No provider requests or
credentials ("unpaid-offline-test-placeholder" is a dummy key that is never sent).
"""
import copy
import importlib.util
import json
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from research_swarm import reasoning_bridge as transport
from research_swarm.provider import (CLAUDE_MODEL_ID, GEMINI_MODEL_ID, KNOWN_MODEL_IDS, MODEL_ID,
                                     SIGNED_REASONING_MODEL_IDS, TRACK_MODEL_IDS, ProviderSettings,
                                     frozen_fields, settings_from_config, validate_catalog)

HAVE_INSPECT = importlib.util.find_spec("inspect_ai") is not None
CONFIG = Path(__file__).resolve().parents[1] / "configs" / "owner-claude-opus-5.5.json"
PLACEHOLDER_KEY = "unpaid-offline-test-placeholder"

# GET https://openrouter.ai/api/v1/models/anthropic/claude-opus-5.5/endpoints, October 21, 2026
# (the pinned "anthropic" route; irrelevant fields dropped). "anthropic/fast" is 2x and not pinned.
ANTHROPIC_ENDPOINT = {
    "name": "Anthropic | anthropic/claude-opus-5.5-20260921", "model_id": "anthropic/claude-opus-5.5",
    "context_length": 1000000, "provider_name": "Anthropic", "tag": "anthropic", "status": 0,
    "max_completion_tokens": 128000,
    "pricing": {"prompt": "0.000004", "completion": "0.00002", "web_search": "0.01",
                "input_cache_read": "0.0000002", "input_cache_write": "0.000005",
                "input_cache_write_1h": "0.000008", "discount": 0},
    "supported_parameters": ["reasoning", "include_reasoning", "max_tokens", "stop", "tool_choice", "tools",
                             "structured_outputs", "response_format", "verbosity", "reasoning_effort"]}


def catalog():
    fast = dict(copy.deepcopy(ANTHROPIC_ENDPOINT), tag="anthropic/fast",
                pricing={"prompt": "0.000008", "completion": "0.00004", "input_cache_read": "0.0000004",
                         "input_cache_write": "0.00001", "input_cache_write_1h": "0.000016", "discount": 0})
    return {"data": {"id": CLAUDE_MODEL_ID, "endpoints": [copy.deepcopy(ANTHROPIC_ENDPOINT), fast]}}


class ClaudeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG.read_text())
        self.settings = settings_from_config(self.config)

    def test_config_pins_first_party_route_and_covers_cache_write_price(self):
        s = self.settings
        self.assertEqual((s.model_id, s.provider_route, s.max_tokens, s.reasoning_effort, s.context_length),
                         (CLAUDE_MODEL_ID, "anthropic", 32768, "high", 1000000))
        self.assertEqual(self.config["candidate_model_id"], CLAUDE_MODEL_ID)
        self.assertEqual(validate_catalog(catalog(), s)["tag"], "anthropic")
        # Settlement prices cache writes at the prompt bound: the listed $4/M prompt price alone is
        # below the $5/M 5-minute cache-write price, so the catalog check refuses it.
        lower = ProviderSettings(**{**frozen_fields(s), "prompt_usd_per_token": "0.000004"})
        with self.assertRaisesRegex(ValueError, "input_cache_write"):
            validate_catalog(catalog(), lower)
        with self.assertRaisesRegex(ValueError, "maximum completion"):
            validate_catalog(catalog(), ProviderSettings(**{**frozen_fields(s), "max_tokens": 200000}))

    def test_track_tables(self):
        from research_swarm.budget import BUDGET_TRACKS
        self.assertIn(CLAUDE_MODEL_ID, TRACK_MODEL_IDS["gemini"])  # the owner's track
        self.assertIn(CLAUDE_MODEL_ID, KNOWN_MODEL_IDS)
        self.assertIn(CLAUDE_MODEL_ID, SIGNED_REASONING_MODEL_IDS)
        self.assertNotIn(CLAUDE_MODEL_ID, TRACK_MODEL_IDS["ablation"])
        self.assertIn(5, BUDGET_TRACKS["gemini"]["agent_counts"])
        self.assertEqual(self.config["budget_track"], "gemini")
        self.assertEqual((self.config["agent_count"], self.config["max_connections"]), (5, 5))

    def test_request_options_add_automatic_cache_control_for_anthropic_only(self):
        options = self.settings.request_options()
        self.assertEqual(options["cache_control"], {"type": "ephemeral"})  # 5m: matches Inspect's markers
        self.assertEqual(options["provider"], {"only": ["anthropic"], "allow_fallbacks": False,
                                               "require_parameters": True})
        self.assertEqual(options["reasoning"], {"effort": "high", "exclude": False})
        for other, route in ((MODEL_ID, "deepseek"), (GEMINI_MODEL_ID, "google-vertex/global")):
            self.assertNotIn("cache_control", ProviderSettings(model_id=other, provider_route=route).request_options())

    def test_usage_priced_upper_bound_covers_cache_writes_and_reads(self):
        from inspect_ai.model import ModelUsage
        # input_tokens net of cache reads/writes, as Inspect's OpenRouter provider reports them.
        usage = ModelUsage(input_tokens=1000, output_tokens=2000, total_tokens=0, input_tokens_cache_read=80000,
                           input_tokens_cache_write=5000, reasoning_tokens=1500)
        cost, basis = self.settings.settlement_cost(usage)
        price = ANTHROPIC_ENDPOINT["pricing"]
        actual = (1000 * Decimal(price["prompt"]) + 80000 * Decimal(price["input_cache_read"])
                  + 5000 * Decimal(price["input_cache_write"]) + 2000 * Decimal(price["completion"]))
        self.assertEqual(basis, "usage_priced_upper_bound")
        self.assertGreaterEqual(cost, actual)


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect SDK not installed")
class ClaudeReasoningRoundTripTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        transport.install_reasoning_bridge()
        from inspect_ai.agent._bridge import anthropic_api_impl
        from inspect_ai.model import get_model
        self.impl = anthropic_api_impl
        self.bridge = SimpleNamespace()
        self.api = get_model("openrouter/" + CLAUDE_MODEL_ID, api_key=PLACEHOLDER_KEY).api

    def completion(self, details, tool_ids, content="I will compile the proof."):
        """An OpenRouter chat completion as it returns Anthropic thinking: reasoning_details entries of
        format anthropic-claude-v1 (thinking text + signature; redacted thinking as reasoning.encrypted)."""
        from openai.types.chat import ChatCompletion
        return ChatCompletion.model_validate({
            "id": "gen-offline", "object": "chat.completion", "created": 1, "model": CLAUDE_MODEL_ID,
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": content, "reasoning": "summary", "reasoning_details": details,
                "tool_calls": [{"id": tool_id, "type": "function",
                                "function": {"name": "Bash", "arguments": "{\"command\": \"lake build\"}"}}
                               for tool_id in tool_ids]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    async def test_signed_thinking_survives_a_multi_turn_tool_call(self):
        """OpenRouter response -> Inspect -> Anthropic blocks for Claude Code -> Claude Code's next
        request (assistant turn echoed, tool result added) -> Inspect -> OpenRouter request: every
        signed thinking entry comes back verbatim, in order, on the assistant message with its tool calls."""
        from research_swarm.inspect_adapter import replayed_reasoning_blocks, replayed_signed_reasoning
        fmt = "anthropic-claude-v1"
        cases = {
            "thinking with signature": (
                [{"type": "reasoning.text", "text": "Induct on n; the base case is rfl.",
                  "signature": "ErUBCkYIBx-signature-not-a-secret", "format": fmt, "index": 0}],
                ["toolu_01A"], "thinking"),
            "interleaved thinking blocks and parallel calls": (
                [{"type": "reasoning.text", "text": "First check the statement.",
                  "signature": "EqQBCkYIBx-first-not-a-secret", "format": fmt, "index": 0},
                 {"type": "reasoning.text", "text": "Then run both builds.",
                  "signature": "EqQBCkYIBx-second-not-a-secret", "format": fmt, "index": 1}],
                ["toolu_01B", "toolu_01C"], "thinking"),
            "redacted thinking next to signed thinking": (
                [{"type": "reasoning.encrypted", "data": "EmwKAhgB-redacted-not-a-secret", "format": fmt,
                  "index": 0},
                 {"type": "reasoning.text", "text": "Use Nat.rec.",
                  "signature": "EqQBCkYIBx-third-not-a-secret", "format": fmt, "index": 1}],
                # Inspect's parser keeps a redacted flag with no summary for this order, so Claude Code
                # sees an opaque block; the transport marker still carries every entry.
                ["toolu_01D"], "redacted_thinking"),
            "redacted thinking only": (
                [{"type": "reasoning.encrypted", "data": "EmwKAhgB-only-not-a-secret", "format": fmt, "index": 0}],
                ["toolu_01E"], "redacted_thinking"),
        }
        for name, (details, tool_ids, block_type) in cases.items():
            with self.subTest(case=name):
                message = self.api.chat_choices_from_completion(self.completion(details, tool_ids), [])[0].message
                self.assertEqual([c.id for c in message.tool_calls], tool_ids)
                with transport._bridge_scope(self.bridge):
                    blocks = await self.impl.assistant_message_blocks(message)
                self.assertEqual([b.type for b in blocks], [block_type, "text", *["tool_use"] * len(tool_ids)])
                self.assertEqual([b.id for b in blocks if b.type == "tool_use"], tool_ids)
                self.assertNotIn("not-a-secret", str(blocks))  # only the authenticated transport marker
                raw = [{"role": "user", "content": "Begin your conference research."},
                       {"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in blocks]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
                                                    for tool_id in tool_ids]}]
                with transport._bridge_scope(self.bridge):
                    replayed = await self.impl.messages_from_anthropic_input(raw, [])
                self.assertEqual(replayed[1].content[0], message.content[0])
                self.assertEqual(replayed[1].tool_calls, message.tool_calls)
                self.assertEqual(replayed_reasoning_blocks(replayed), 1)
                self.assertEqual(replayed_signed_reasoning(replayed),
                                 {"replayed_signed_tool_call_turns": 1, "replayed_signed_reasoning_formats": [fmt]})
                wire = await self.api.messages_to_openai(replayed)
                assistant = wire[1]
                # All entries are signed or encrypted, so Inspect's sanitizer keeps every one, in order.
                self.assertEqual(assistant["reasoning_details"], details)
                self.assertNotIn("reasoning_content", assistant)  # DeepSeek-only field
                self.assertEqual([c["id"] for c in assistant["tool_calls"]], tool_ids)
                self.assertEqual([m.get("tool_call_id") for m in wire[2:]], tool_ids)
                self.assertNotIn(transport._PREFIX, str(wire))

    async def test_unsigned_anthropic_text_is_not_replayed(self):
        """A thinking entry without a signature (never valid for Anthropic) is dropped by Inspect's
        sanitizer rather than sent back, where Anthropic would reject it."""
        details = [{"type": "reasoning.text", "text": "Unsigned.", "format": "anthropic-claude-v1", "index": 0}]
        message = self.api.chat_choices_from_completion(self.completion(details, ["toolu_01F"]), [])[0].message
        with transport._bridge_scope(self.bridge):
            blocks = await self.impl.assistant_message_blocks(message)
            replayed = await self.impl.messages_from_anthropic_input(
                [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in blocks]}], [])
        self.assertEqual((await self.api.messages_to_openai(replayed))[0]["reasoning_details"], [])


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect SDK not installed")
class ClaudePromptCachingTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, settings):
        """The OpenRouter request body the frozen bridge config produces (Inspect's own parameter and
        cache-marker code, no client call), for a Claude Code turn: system, user, assistant with a
        tool call, tool result, normalized token counter (counter-user-v1)."""
        from inspect_ai.model import (ChatMessageAssistant, ChatMessageSystem, ChatMessageTool, ChatMessageUser,
                                      ContentText, GenerateConfig, get_model)
        from inspect_ai.model._providers.openrouter import _add_anthropic_cache_markers
        from inspect_ai.tool import ToolCall
        api = get_model("openrouter/" + settings.model_id, api_key=PLACEHOLDER_KEY,
                        provider=settings.request_options()["provider"], reasoning_enabled=True).api
        config = GenerateConfig(max_tokens=settings.max_tokens, reasoning_effort=settings.reasoning_effort,
                                reasoning_history="all", cache=False, extra_body=settings.request_options())
        call_id = "toolu_" + uuid.uuid4().hex
        messages = [ChatMessageSystem(content="Conference instructions."),
                    ChatMessageUser(content=[ContentText(text="Begin.")]),
                    ChatMessageAssistant(content=[ContentText(text="Building.")],
                                         tool_calls=[ToolCall(id=call_id, function="Bash", arguments={})]),
                    ChatMessageTool(content="ok", tool_call_id=call_id),
                    ChatMessageUser(content="<total_tokens>100 tokens left</total_tokens>")]
        request = {"messages": await api.messages_to_openai(messages),
                   "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
                   **api.completion_params(config, True)}
        if api._cache_prompt_enabled(config):
            _add_anthropic_cache_markers(request)
        return request

    async def test_automatic_caching_covers_the_conversation_history(self):
        request = await self.request(settings_from_config(json.loads(CONFIG.read_text())))
        body = request["extra_body"]  # merged into the top level of the JSON body by the OpenAI client
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["provider"]["only"], ["anthropic"])
        self.assertEqual(body["reasoning"]["effort"], "high")
        # Inspect's explicit markers: last system block and last tool only. Its message breakpoint does
        # not land on Claude Code's usual history (string assistant/tool/counter content), which is why
        # the top-level automatic breakpoint is required. Total breakpoints stay within Anthropic's 4.
        marked = [m["role"] for m in request["messages"] if "cache_control" in json.dumps(m)]
        self.assertEqual(marked, ["system"])
        self.assertIn("cache_control", request["tools"][-1])
        self.assertLessEqual(json.dumps(request).count('"cache_control"'), 4)
        self.assertNotIn('"ttl"', json.dumps(request))  # all 5-minute: no TTL-ordering conflict

    async def test_other_models_get_no_anthropic_cache_fields(self):
        request = await self.request(ProviderSettings(model_id=GEMINI_MODEL_ID, provider_route="google-vertex/global"))
        self.assertNotIn("cache_control", json.dumps(request))


if __name__ == "__main__":
    unittest.main()
