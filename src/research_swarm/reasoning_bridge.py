"""Lossless OpenRouter reasoning transport through Inspect's Anthropic bridge.

Inspect 0.3.279 renders non-Anthropic reasoning as plain text. Claude Code then
echoes that text rather than a reasoning block. Only the bridge's converters are
wrapped here: genuine Anthropic conversions and provider clients are untouched.
An authenticated, self-contained thinking signature preserves the original block
without a history cache. Its signing key belongs to one AgentBridge instance;
another agent cannot replay it. Omitted/compacted blocks stay omitted.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import functools
import hashlib
import hmac
import importlib.metadata
import json
import secrets
import threading


_PREFIX = "research-swarm-reasoning-v1:"
_KEY_ATTRIBUTE = "_research_swarm_reasoning_transport_key"
_SCOPE = contextvars.ContextVar("research_swarm_reasoning_bridge", default=None)
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
_EXPECTED_INSPECT_VERSION = "0.3.279"


@contextlib.contextmanager
def _bridge_scope(bridge):
    # No await between checking/assigning: concurrent requests on this bridge
    # share its key, while independent bridge objects always receive fresh keys.
    key = getattr(bridge, _KEY_ATTRIBUTE, None)
    if key is None:
        key = secrets.token_bytes(32)
        setattr(bridge, _KEY_ATTRIBUTE, key)
    token = _SCOPE.set(key)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _readable(content):
    return (content.summary or "") if content.redacted else content.reasoning


def _encode(content, key):
    payload = json.dumps(content.model_dump(mode="json"), ensure_ascii=False,
                         separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    digest = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return _PREFIX + digest + ":" + encoded


def _decode(marker, block, key):
    from inspect_ai.agent._bridge._errors import BridgePolicyError
    from inspect_ai.model import ContentReasoning
    try:
        digest, encoded = marker[len(_PREFIX):].split(":", 1)
        expected = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise ValueError("signature mismatch")
        content = ContentReasoning.model_validate(json.loads(base64.b64decode(
            encoded, altchars=b"-_", validate=True)))
        # These markers are only for OpenRouter details, never arbitrary content.
        if not (content.signature or "").startswith("reasoning-details://"):
            raise ValueError("unexpected reasoning source")
        readable = _readable(content)
        if readable:
            if block.get("type") != "thinking" or block.get("thinking") != readable:
                raise ValueError("thinking text modified")
        elif block.get("type") != "redacted_thinking":
            raise ValueError("opaque reasoning type modified")
        return content
    except Exception:
        # Never echo a marker, reasoning, or transport details in an error.
        raise BridgePolicyError("Invalid or foreign swarm reasoning transport") from None


def install_reasoning_bridge():
    """Install once before constructing Claude Code agents; no network or keys.

    Version-guarded because this wraps private APIs in the pinned Inspect SDK.
    The request wrapper scopes the two conversion wrappers to one AgentBridge.
    Calls to the converters outside a bridge request retain their SDK behavior.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        actual = importlib.metadata.version("inspect-ai")
        if actual != _EXPECTED_INSPECT_VERSION:
            raise RuntimeError("Reasoning bridge requires inspect-ai " + _EXPECTED_INSPECT_VERSION)
        if _INSTALLED:
            return
        from inspect_ai.agent._bridge import anthropic_api_impl as impl
        from inspect_ai.model import ContentReasoning, ContentText

        original_request = impl.inspect_anthropic_api_request_impl
        original_output = impl.assistant_message_blocks
        original_input = impl.messages_from_anthropic_input

        @functools.wraps(original_request)
        async def request(json_data, headers, web_search, code_execution, bridge, *, beta=False):
            with _bridge_scope(bridge):
                return await original_request(json_data, headers, web_search, code_execution,
                                              bridge, beta=beta)

        @functools.wraps(original_output)
        async def output(message, *, beta=False):
            key = _SCOPE.get()
            if key is None or not isinstance(message.content, list):
                return await original_output(message, beta=beta)
            content = []
            for part in message.content:
                if isinstance(part, ContentReasoning) and (part.signature or "").startswith(
                        "reasoning-details://"):
                    marker = _encode(part, key)
                    readable = _readable(part)
                    # Use the SDK's native block assembly so tool order and beta
                    # response types keep their existing behavior.
                    part = (ContentReasoning(reasoning=marker, summary=readable, redacted=True)
                            if readable else ContentReasoning(reasoning="", signature=marker,
                                                              redacted=True))
                content.append(part)
            return await original_output(message.model_copy(update={"content": content}), beta=beta)

        @functools.wraps(original_input)
        async def input_messages(input, tools):
            key = _SCOPE.get()
            if key is None:
                return await original_input(input, tools)
            rewritten = []
            restore = {}
            for message in input:
                blocks = message.get("content")
                if message.get("role") != "assistant" or not isinstance(blocks, list):
                    rewritten.append(message)
                    continue
                replacements = []
                for block in blocks:
                    marker = (block.get("signature") if block.get("type") == "thinking"
                              else block.get("data") if block.get("type") == "redacted_thinking"
                              else None)
                    if isinstance(marker, str) and marker.startswith(_PREFIX):
                        restored = _decode(marker, block, key)
                        placeholder = "swarm-reasoning-placeholder:" + secrets.token_hex(32)
                        restore[placeholder] = restored
                        # Do not put transport markers in Inspect's global native
                        # Anthropic thinking cache. This placeholder is call-local.
                        replacements.append({"type": "text", "text": placeholder})
                    else:
                        replacements.append(block)
                rewritten.append({**message, "content": replacements})
            messages = await original_input(rewritten, tools)
            for message in messages:
                if message.role == "assistant" and isinstance(message.content, list):
                    message.content = [restore.get(part.text, part)
                                       if isinstance(part, ContentText) else part
                                       for part in message.content]
            return messages

        impl.inspect_anthropic_api_request_impl = request
        impl.assistant_message_blocks = output
        impl.messages_from_anthropic_input = input_messages
        _INSTALLED = True
