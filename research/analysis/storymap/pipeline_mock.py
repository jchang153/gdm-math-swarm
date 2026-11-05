"""Story map: deterministic offline model for tests and ``--mock`` (no network, no key, no paid call).

``MockStorymapModel`` is a ``FakeTransport`` responder: it reads the ``STORYMAP UNIT {json}`` header line of the
user message and returns a schema-shaped JSON reply served as the requested model by its pinned provider.
Stage 1 quotes the start of the chunk's first reasoning item (code ``C1``, low certainty); stages 2 and 3 return
"unclear"/"unknown" labels. It never claims cheating; it only exercises the plumbing.

Overseer mode (unit header ``"mode": "overseer"``): replies follow ``storymap/schema_overseer.py`` (its
``EXAMPLE_STAGE1/2/3`` outputs with neutral values adapted to the unit: the quote is a verbatim prefix of the
chunk's first reasoning item, the event id is the unit's, one phase spans the overseer's timeline). It never claims
a detection.
"""
from __future__ import annotations

import copy
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


def _overseer_example(stage):
    """A deep copy of ``schema_overseer.EXAMPLE_STAGE<n>`` (imported lazily: overseer mode only), else {}."""
    try:
        from . import schema_overseer
    except ImportError:
        return {}
    return copy.deepcopy(getattr(schema_overseer, f"EXAMPLE_STAGE{stage}", None) or {})


def overseer_reply(unit, user):
    """The mock reply of an overseer-mode unit: valid under schema_overseer, neutral, never a detection."""
    stage = unit.get("stage")
    out = _overseer_example(stage)
    if stage == 1:
        found = _first_reasoning(user)
        code = next((t.get("code") for t in out.get("thoughts") or [] if isinstance(t, dict) and t.get("code")),
                    "O1")
        state = dict(out.get("state") or {})
        state.update(status="not_looking", knows_trick=False, suspected=[], open_questions=[],
                     summary=f"mock overseer state after chunk {unit.get('chunk')}")
        out.update(thoughts=[] if found is None else [{"code": code, "turn": found[1], "item": found[0], "t": found[2],
                                                       "quote": found[3], "certainty": "low", "note": None}],
                   state=state)
        return out
    if stage == 2:
        out.update(event_id=unit.get("event_id"), registered="unclear", change="none", response="none",
                   recognised_trick=None, reason=None, next_action="mock", quotes=[], certainty="low")
        return out
    if stage == 3:
        first, last = unit.get("first_min") or 0.0, unit.get("last_min") or 0.0
        final = dict(out.get("final") or {})
        final.update(detected_trick=False, first_suspicion_t=None, first_suspected=None, detection_t=None,
                     confirmed_on_real_t=None, enforced=False, communicated=False, coordinated=None,
                     public_posts_specific="none", names_trick_publicly=False, states_consequences=False)
        out.update(agent=unit.get("agent"),
                   phases=[{"from_t": first, "to_t": max(first, last), "status": "not_looking", "label": "mock"}],
                   turning_points=[], story="mock overseer story", final=final, consistency_flags=[])
        return out
    return {}


def reply(user):
    """The mock reply (a dict) for one user message."""
    unit = unit_header(user)
    stage = unit.get("stage")
    if unit.get("mode") == "overseer":
        return overseer_reply(unit, user)
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
