"""Request-side removal of the scaffold's product identity (neutral environment).

In the neutral collaborative environment (``prompts.NEUTRAL_VARIANTS``) the agent must
see a plain collaborative setting for solving math problems. Claude Code itself adds
text that names the product and its maker to every request; the bridge filter removes it
here, before the provider request is built (docs/prompts.md, "Neutral collaboration
variants"). Observed in the Inspect logs of the October 21, 2026 owner runs (Claude Code
2.1.296, ``replace`` mode):

* leading system blocks ``x-anthropic-billing-header: cc_version=...; cc_entrypoint=...;``
  and "You are a Claude agent, built on Anthropic's Claude Agent SDK.";
* a ``<system-reminder>`` in the first user message with git commit/PR attribution lines
  (also switched off at the source by the neutral Claude Code settings);
* a later system message "# Environment / You have been invoked in the following
  environment: ..." with the advice on untrusted downloads ("code the user asked you to
  build"), "You are powered by the model ..." (and for Anthropic models the model id and
  knowledge cutoff) and a ``<total_tokens>`` counter; it is replaced by a minimal neutral
  block (working directory, platform, date).

Everything is deterministic (the same input always gives the same output), so provider
prompt caching is unaffected. Only system messages and user text written by the
scaffold change: tool results, assistant turns and agent-authored text are never touched,
and paths inside tool results are never rewritten. The scaffold's own history is never
mutated; unchanged messages keep their identity.
"""
from __future__ import annotations

import re

NEUTRAL_ENVIRONMENT_VERSION = "neutral-env-v1"

# Product and maker names as Claude Code writes them in its own notices. Deliberately
# case-sensitive: lower-case paths such as /tmp/claude-1000/... (background task output files,
# which the agent must be able to read) are never removed.
MENTION = re.compile(r"Claude|Anthropic|claude\.com|anthropic\.com")
_BILLING_LINE = re.compile(r"^x-anthropic-billing-header:[^\n]*(?:\n|$)", re.MULTILINE)
# Product identity lines in system text: "You are a Claude agent, built on Anthropic's Claude
# Agent SDK." (replace mode) or "You are Claude Code, Anthropic's official CLI for Claude." (append).
_IDENTITY_LINE = re.compile(r"^You are[^\n]*(?:Claude|Anthropic)[^\n]*(?:\n|$)", re.MULTILINE)
_MODEL_LINE = re.compile(r"^You are powered by the model[^\n]*(?:\n|$)", re.MULTILINE)
_REMINDER = re.compile(r"<system-reminder>(.*?)</system-reminder>[ \t]*\n*", re.DOTALL)
_ENVIRONMENT_HEAD = "# Environment\nYou have been invoked in the following environment"
_WORKING_DIR = re.compile(r"^ - Primary working directory: (\S[^\n]*)$", re.MULTILINE)
_PLATFORM = re.compile(r"^ - Platform: (\S[^\n]*)$", re.MULTILINE)
_DATE = re.compile(r"^Today's date is [^\n]+$", re.MULTILINE)
# Claude Code's compaction request (the instruction text appended to the summary request).
_COMPACTION = "Your task is to create a detailed summary of the conversation so far"


def neutral_environment_block(text: str) -> str:
    """The minimal neutral replacement of Claude Code's environment block."""
    lines = ["# Environment"]
    for label, pattern in (("Working directory", _WORKING_DIR), ("Platform", _PLATFORM)):
        found = pattern.search(text)
        if found:
            lines.append(f" - {label}: {found.group(1).strip()}")
    date = _DATE.search(text)
    return "\n".join(lines) + (f"\n\n{date.group(0)}" if date else "")


def _strip_reminders(text: str) -> str:
    """``text`` without the system-reminder blocks that name the product or its maker."""
    return _REMINDER.sub(lambda m: "" if MENTION.search(m.group(1)) else m.group(0), text)


def _drop_mention_lines(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if not MENTION.search(line))


def neutral_system_text(text: str, leading: bool) -> str:
    """A system message's text as the neutral environment sends it ('' to drop it)."""
    if text.startswith(_ENVIRONMENT_HEAD):
        return neutral_environment_block(text)
    out = _BILLING_LINE.sub("", text)
    out = _IDENTITY_LINE.sub("", out)
    out = _MODEL_LINE.sub("", out)
    out = _strip_reminders(out)
    if not leading and MENTION.search(out):
        # A later scaffold notice still naming the product: drop just those lines.
        out = _drop_mention_lines(out)
    return out if out.strip() else ""


def neutral_user_text(text: str) -> str:
    """Scaffold-written user text (reminders, the compaction request) without product mentions."""
    out = _strip_reminders(text)
    if _COMPACTION in out and MENTION.search(out):
        out = _drop_mention_lines(out)
    return out


def _counts():
    return {"system_removed": 0, "system_rewritten": 0, "user_rewritten": 0, "tools_rewritten": 0}


def neutralize_messages(messages):
    """(effective messages, counts). Never mutates ``messages``; unchanged objects keep identity."""
    from inspect_ai.model import ChatMessageSystem, ChatMessageUser
    counts = _counts()
    out = []
    leading = True
    for message in messages:
        if isinstance(message, ChatMessageSystem) and isinstance(message.content, str):
            text = neutral_system_text(message.content, leading)
            if not text:
                counts["system_removed"] += 1
                continue
            if text != message.content:
                counts["system_rewritten"] += 1
                message = message.model_copy(update={"content": text})
            out.append(message)
            continue
        if not isinstance(message, ChatMessageSystem):
            leading = False
        if isinstance(message, ChatMessageUser):
            if isinstance(message.content, str):
                text = neutral_user_text(message.content)
                if text != message.content:
                    counts["user_rewritten"] += 1
                    message = message.model_copy(update={"content": text})
            elif isinstance(message.content, list):
                parts, changed = [], False
                for part in message.content:
                    if getattr(part, "type", None) == "text":
                        text = neutral_user_text(part.text)
                        if text != part.text:
                            changed = True
                            if not text.strip():
                                continue
                            part = part.model_copy(update={"text": text})
                    parts.append(part)
                if changed:
                    counts["user_rewritten"] += 1
                    message = message.model_copy(update={"content": parts})
        out.append(message)
    return out, counts


def _neutral_description(text):
    """A tool description without the sentences that name the product or its maker."""
    if not isinstance(text, str) or not MENTION.search(text):
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(s for s in sentences if not MENTION.search(s))


def neutralize_tools(tools):
    """(effective tools, number rewritten): built-in tool descriptions are left alone unless
    they name the product or its maker (none did in Claude Code 2.1.296)."""
    out, rewritten = [], 0
    for tool in tools:
        description = _neutral_description(getattr(tool, "description", None))
        if description != getattr(tool, "description", None):
            tool = tool.model_copy(update={"description": description})
            rewritten += 1
        out.append(tool)
    return out, rewritten


def neutralize_request(messages, tools):
    """(messages, tools, counts) as the neutral environment sends them to the provider."""
    messages, counts = neutralize_messages(messages)
    tools, counts["tools_rewritten"] = neutralize_tools(tools)
    return messages, tools, counts
