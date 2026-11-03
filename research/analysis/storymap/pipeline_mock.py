"""Story map: deterministic offline model for tests and ``--mock`` (no network, no key, no paid call).

``MockStorymapModel`` is a ``FakeTransport`` responder: it reads the ``STORYMAP UNIT {json}`` header line of the
user message and returns a schema-shaped JSON reply served as the requested model by its pinned provider.
Stage 1 quotes the start of the chunk's first reasoning item (code ``C1``, low certainty); stages 2 and 3 return
"unclear"/"unknown" labels. It never claims cheating; it only exercises the plumbing.
"""
from __future__ import annotations

import json
import re

from research.analysis.judge.client import fake_completion

from .pipeline_models import PROVIDER_NAMES

HEADER = "STORYMAP UNIT "
_ITEM = re.compile(r"^\[i=(\d+) turn=(\S+) t=(\S+) ([^\]]*)\] ?(.*)$")


def unit_header(user):
    for line in str(user).splitlines():
        if line.startswith(HEADER):
            return json.loads(line[len(HEADER):])
    return {}


def _first_reasoning(user):
    for line in str(user).splitlines():
        match = _ITEM.match(line)
        if match and match.group(4) == "reasoning" and match.group(5).strip():
            turn = int(match.group(2)) if match.group(2).isdigit() else 0
            t = float(match.group(3)) if match.group(3) not in ("?",) else 0.0
            return int(match.group(1)), turn, t, match.group(5).strip()[:60]
    return None


def reply(user):
    """The mock reply (a dict) for one user message."""
    unit = unit_header(user)
    stage = unit.get("stage")
    if stage == 1:
        found = _first_reasoning(user)
        thoughts = [] if found is None else [{"code": "A1", "turn": found[1], "item": found[0], "t": found[2],
                                               "quote": found[3], "certainty": "low", "note": None}]
        return {"thoughts": thoughts,
                "state": {"has_trick": False, "stance": "none", "believes_judge": "unknown", "open_questions": [],
                          "summary": f"mock state after chunk {unit.get('chunk')}"}}
    if stage == 2:
        return {"event_id": unit.get("event_id"), "registered": "unclear", "change": "none", "direction": None,
                "reason": None, "self_attributed": False,
                "overseer": {"F1": False, "F2": False, "F3": None, "F4": False, "F5": False}
                if unit.get("overseer_expected") else None, "next_action": "mock", "quotes": [],
                "certainty": "low"}
    if stage == 3:
        last = unit.get("last_min") or 0.0
        return {"agent": unit.get("agent"),
                "phases": [{"from_t": unit.get("first_min") or 0.0, "to_t": last, "stance": "none", "label": "mock"}],
                "turning_points": [], "story": "mock story",
                "final": {"considered": False, "faked": False, "credited": False, "first_fake_t": None,
                          "source": "unknown", "stopped": False, "stop_cause_event_ids": [], "aware_cheating": "unclear"},
                "consistency_flags": []}
    return {}


class MockStorymapModel:
    """``FakeTransport(responder=MockStorymapModel())``; ``replies`` counts replies per stage."""

    def __init__(self, cost="0.001"):
        self.cost = cost
        self.replies = {}

    def __call__(self, request):
        body = request["body"]
        user = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
        data = reply(user)
        stage = unit_header(user).get("stage")
        self.replies[stage] = self.replies.get(stage, 0) + 1
        tag = ((body.get("provider") or {}).get("only") or ["anthropic"])[0]
        return fake_completion(json.dumps(data), cost=self.cost, provider=PROVIDER_NAMES.get(tag, tag),
                               model=body["model"])
