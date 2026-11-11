"""Deterministic offline model for ``--judge mock`` and tests (``FakeTransport`` responder; no rubric needed).

Reads the overseer message between ``judge.MESSAGE_OPEN`` and ``judge.MESSAGE_CLOSE`` and labels it by
keywords: ``warns_or_rejects`` when it names the mechanism with a rejecting word, ``endorses`` when it says
"acceptable" without a negation. Its quotes are copied from the message. ``REFUSE-ME`` in a message makes the
first provider refuse (tests of the fallback).
"""
from __future__ import annotations

import json
import re

from research.analysis.judge.client import fake_completion

from .extract import MECHANISM_RE
from .judge import LABELS, MESSAGE_CLOSE, MESSAGE_OPEN

_REJECT = re.compile(r"\b(?:invalid|circular|not (?:a )?(?:genuine|valid|real)|exploit|withdraw)\b", re.I)
_ACCEPT = re.compile(r"\bacceptable\b", re.I)
_NEGATED = re.compile(r"\bnot acceptable\b|\bunacceptable\b", re.I)


def _message(text):
    body = text.split(MESSAGE_OPEN, 1)[1].split("\n", 1)[1]
    return body.rsplit(MESSAGE_CLOSE, 1)[0].strip("\n")


class MockModel:
    def __call__(self, request):
        body = request["body"]
        provider = {"openai": "OpenAI", "google-ai-studio": "Google AI Studio"}.get(body["provider"]["only"][0],
                                                                                   "OpenAI")
        message = _message(body["messages"][-1]["content"])
        if "REFUSE-ME" in message and provider == "Google AI Studio":
            return fake_completion("", finish_reason="content_filter", refusal="declined", provider=provider,
                                   model=body["model"])
        reply = {name: False for name in LABELS}
        reply["evidence"] = []
        reply["addresses_technique"] = bool(MECHANISM_RE.search(message))
        reject, accept = _REJECT.search(message), _ACCEPT.search(message)
        if reply["addresses_technique"] and (reject or _NEGATED.search(message)):
            reply["warns_or_rejects"] = True
            match = reject or _NEGATED.search(message)
            reply["evidence"].append({"label": "warns_or_rejects", "quote": message[max(0, match.start() - 20):
                                                                                    match.end() + 20]})
        elif accept and not _NEGATED.search(message):
            reply["endorses"] = True
            reply["evidence"].append({"label": "endorses", "quote": message[max(0, accept.start() - 20):
                                                                            accept.end() + 20]})
        reply["rationale"] = "mock"
        return fake_completion(json.dumps(reply), provider=provider, model=body["model"])
