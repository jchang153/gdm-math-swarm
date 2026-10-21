"""GPT 5.6 Sol (OpenRouter, openai route) through the pinned Inspect bridge; offline, no credentials.

OpenAI reasoning reaches OpenRouter clients as ``reasoning_details`` in format
``openai-responses-v1``: readable summaries plus one encrypted reasoning item (``rs_...``)
that the Responses API needs back next to the function calls it led to. These tests drive the
real provider conversions with a captured transport (``_generate_completion`` is replaced, so no
request leaves the process) and the production ``budget_filter``.
"""
import copy
import importlib.util
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_swarm import reasoning_bridge as transport


HAVE_INSPECT = importlib.util.find_spec("inspect_ai") is not None
CONFIG = Path(__file__).resolve().parents[1] / "configs" / "gpt-sol.json"
FORMAT = "openai-responses-v1"
# Parameters the pinned openai route lists (OpenRouter catalog, October 21, 2026) plus the
# request envelope; anything else would be refused under require_parameters.
ROUTE_PARAMETERS = {"include_reasoning", "max_tokens", "reasoning", "reasoning_effort", "response_format", "seed",
                    "structured_outputs", "tool_choice", "tools", "verbosity"}
ENVELOPE = {"model", "messages", "extra_headers", "extra_body"}


def gpt_details(*summaries, encrypted="gAAAAB-encrypted-not-a-secret"):
    item = "rs_" + uuid.uuid4().hex
    details = [{"type": "reasoning.summary", "summary": text, "format": FORMAT, "index": i, "id": item}
               for i, text in enumerate(summaries)]
    if encrypted is not None:
        details.append({"type": "reasoning.encrypted", "data": encrypted, "format": FORMAT,
                        "index": len(summaries), "id": item})
    return details


def gpt_completion(details, tool_ids, content=""):
    from openai.types.chat import ChatCompletion
    return ChatCompletion.model_validate({
        "id": "gen-offline-" + uuid.uuid4().hex, "object": "chat.completion", "created": 1,
        "model": "openai/gpt-5.6-sol-20260709",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_ids else "stop", "message": {
            "role": "assistant", "content": content, "reasoning_details": details,
            "tool_calls": [{"id": tool_id, "type": "function",
                            "function": {"name": "Bash", "arguments": "{\"command\": \"lake build\"}"}}
                           for tool_id in tool_ids] or None}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
                  "prompt_tokens_details": {"cached_tokens": 1024},
                  "completion_tokens_details": {"reasoning_tokens": 250}, "cost": 0.0123}})


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect SDK not installed")
class GptReasoningBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        transport.install_reasoning_bridge()
        from inspect_ai.agent._bridge import anthropic_api_impl
        from inspect_ai.model import get_model
        self.impl = anthropic_api_impl
        self.bridge = SimpleNamespace()
        # An explicit dummy key: constructing the provider does not load real credentials.
        self.model = get_model("openrouter/openai/gpt-5.6-sol", api_key="unpaid-offline-test-placeholder")
        self.api = self.model.api

    async def blocks(self, message):
        with transport._bridge_scope(self.bridge):
            return await self.impl.assistant_message_blocks(message)

    async def claude_code_next_request(self, blocks, tool_ids):
        """Claude Code's next request: the assistant turn echoed as returned, then its tool results."""
        raw = [{"role": "user", "content": "Begin your conference research."},
               {"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in blocks]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
                                            for tool_id in tool_ids]}]
        with transport._bridge_scope(self.bridge):
            return await self.impl.messages_from_anthropic_input(raw, [])

    async def test_encrypted_reasoning_survives_a_multi_turn_tool_call(self):
        """OpenRouter response -> Inspect -> Anthropic blocks for Claude Code -> Claude Code's next
        request -> Inspect -> OpenRouter request: every openai-responses-v1 detail (summaries and the
        encrypted item with its rs_ id) comes back verbatim on the assistant message with its tool calls."""
        from research_swarm.inspect_adapter import replayed_reasoning_blocks, replayed_signed_reasoning
        cases = {
            "summary and encrypted": (gpt_details("**Plan** Try induction on n."), ["call_a1"], "thinking"),
            "several summaries, parallel calls": (gpt_details("First idea.", "Second idea."),
                                                  ["call_b1", "call_b2"], "thinking"),
            "encrypted only": (gpt_details(), ["call_c1"], "redacted_thinking"),
        }
        for name, (details, tool_ids, block_type) in cases.items():
            with self.subTest(case=name):
                message = self.api.chat_choices_from_completion(gpt_completion(details, tool_ids), [])[0].message
                self.assertEqual([c.id for c in message.tool_calls], tool_ids)
                blocks = await self.blocks(message)
                self.assertEqual([b.type for b in blocks], [block_type, "text", *["tool_use"] * len(tool_ids)])
                self.assertEqual([b.id for b in blocks if b.type == "tool_use"], tool_ids)
                if block_type == "thinking":  # Claude Code shows the last readable summary
                    self.assertEqual(blocks[0].thinking, details[-2]["summary"])
                self.assertNotIn("encrypted-not-a-secret", str(blocks[0]))  # only the authenticated transport
                replayed = await self.claude_code_next_request(blocks, tool_ids)
                self.assertEqual(replayed[1].content[0], message.content[0])
                self.assertEqual(replayed[1].tool_calls, message.tool_calls)
                self.assertEqual(replayed_reasoning_blocks(replayed), 1)
                self.assertEqual(replayed_signed_reasoning(replayed),
                                 {"replayed_signed_tool_call_turns": 1, "replayed_signed_reasoning_formats": [FORMAT]})
                wire = await self.api.messages_to_openai(replayed)
                assistant = wire[1]
                # Nothing is dropped for this format (Inspect's sanitizer filters only unsigned
                # Gemini/Anthropic text) and no DeepSeek-style reasoning_content is added.
                self.assertEqual(assistant["reasoning_details"], details)
                self.assertNotIn("reasoning_content", assistant)
                self.assertEqual([c["id"] for c in assistant["tool_calls"]], tool_ids)
                self.assertEqual([m.get("tool_call_id") for m in wire[2:]], tool_ids)
                self.assertNotIn(transport._PREFIX, str(wire))

    async def test_tampered_or_foreign_gpt_reasoning_fails_closed(self):
        from inspect_ai.agent._bridge._errors import BridgePolicyError
        message = self.api.chat_choices_from_completion(gpt_completion(gpt_details("Plan."), ["call_d1"]), [])[0].message
        raw = [b.model_dump(exclude_none=True) for b in await self.blocks(message)]
        altered = copy.deepcopy(raw)
        altered[0]["thinking"] = "A different plan."
        with transport._bridge_scope(self.bridge):
            with self.assertRaisesRegex(BridgePolicyError, "Invalid or foreign"):
                await self.impl.messages_from_anthropic_input([{"role": "assistant", "content": altered}], [])
        with transport._bridge_scope(SimpleNamespace()):  # another agent's bridge
            with self.assertRaisesRegex(BridgePolicyError, "Invalid or foreign"):
                await self.impl.messages_from_anthropic_input([{"role": "assistant", "content": raw}], [])

    async def test_production_filter_sends_route_parameters_and_a_cache_stable_prefix(self):
        """Two turns through the production budget_filter and the real OpenRouter provider (transport
        captured): the request carries max_tokens (the parameter the openai route lists; Inspect's GPT-5
        rename to max_completion_tokens is undone, research_swarm.openrouter_params), only route-listed
        parameters, the pinned route without fallbacks and high effort; the second request replays the
        first turn byte-identically, so OpenAI's automatic prefix cache can match it."""
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser, GenerateConfig
        from inspect_ai.model._providers.openai_compatible import OpenAICompatibleAPI
        from inspect_ai.tool import ToolInfo, ToolParams
        from research_swarm.budget import BudgetLedger
        from research_swarm.inspect_adapter import budget_filter
        from research_swarm.provider import settings_from_config
        from research_swarm.runtime import StartBarrier

        settings = settings_from_config(json.loads(CONFIG.read_text()), pricing_verified=True)
        details = gpt_details("**Plan** Build first.")
        responses = [gpt_completion(details, ["call_e1"]), gpt_completion(gpt_details("Done."), [], "Proof checks.")]
        sent = []

        async def capture(api_self, request, config):
            sent.append(copy.deepcopy({k: v for k, v in request.items() if k != "extra_headers"}))
            return responses[len(sent) - 1]

        tools = [ToolInfo(name="Bash", description="Run a command", parameters=ToolParams())]
        events = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(OpenAICompatibleAPI, "_generate_completion", capture):
            ledger = BudgetLedger(Path(folder) / "gpt-budget.sqlite")
            ledger.start_run("test-run", "validation", 1)
            intercept = budget_filter("agent-001", ledger, settings, StartBarrier(1),
                                      lambda *args: events.append(args), scope="validation")
            history = [ChatMessageSystem(content="Conference instructions."), ChatMessageUser(content="Begin your conference research.")]
            first = await intercept(self.model, history, tools, "auto", GenerateConfig())
            blocks = await self.blocks(first.message)
            replayed = await self.claude_code_next_request(blocks, ["call_e1"])
            # Claude Code's request: system prompt, then the replayed conversation.
            second = await intercept(self.model, [history[0], *replayed], tools, "auto", GenerateConfig())

        self.assertEqual(second.completion, "Proof checks.")
        for request in sent:
            self.assertEqual(request["model"], "openai/gpt-5.6-sol")
            self.assertEqual(request["max_tokens"], 16384)
            self.assertNotIn("max_completion_tokens", request)
            self.assertLessEqual(set(request) - ENVELOPE, ROUTE_PARAMETERS)
            body = request["extra_body"]
            self.assertEqual(body["provider"], {"only": ["openai"], "allow_fallbacks": False,
                                                "require_parameters": True})
            self.assertEqual(body["reasoning"]["effort"], "high")
            self.assertLessEqual(set(body) - {"provider", "reasoning", "usage"}, set())
        first_messages, second_messages = sent[0]["messages"], sent[1]["messages"]
        self.assertEqual(second_messages[:len(first_messages)], first_messages)  # stable cached prefix
        assistant = second_messages[len(first_messages)]
        self.assertEqual(assistant["reasoning_details"], details)
        self.assertEqual([c["id"] for c in assistant["tool_calls"]], ["call_e1"])
        admitted = [e[2] for e in events if e[0] == "inference_admitted"]
        self.assertEqual(admitted[1]["replayed_signed_tool_call_turns"], 1)
        finished = [e[2] for e in events if e[0] == "inference_finished"]
        # Settled from usage here (runtime.capture_openrouter_cost supplies the reported cost in runs).
        self.assertTrue(all(f["settled_usd"] is not None for f in finished))

    def test_max_tokens_patch_is_idempotent_version_guarded_and_leaves_other_models_alone(self):
        from inspect_ai.model import GenerateConfig, get_model
        from inspect_ai.model._providers.openrouter import OpenRouterAPI
        from research_swarm import openrouter_params
        openrouter_params.install_openrouter_max_tokens()
        wrapped = OpenRouterAPI.completion_params
        openrouter_params.install_openrouter_max_tokens()
        self.assertIs(wrapped, OpenRouterAPI.completion_params)
        config = GenerateConfig(max_tokens=16384, reasoning_effort="high")
        self.assertEqual(self.api.completion_params(config, True)["max_tokens"], 16384)
        gemini = get_model("openrouter/google/gemini-3.1-pro-preview", api_key="unpaid-offline-test-placeholder").api
        self.assertEqual(gemini.completion_params(config, True)["max_tokens"], 16384)
        with patch.object(openrouter_params.importlib.metadata, "version", return_value="0.0.0"):
            with self.assertRaisesRegex(RuntimeError, "requires inspect-ai"):
                openrouter_params.install_openrouter_max_tokens()


if __name__ == "__main__":
    unittest.main()
