"""Deterministic offline responder for the incident report (``FakeTransport(responder=...)``).

It recognises the stage by the system prompt and answers from the request's own text, so ids and
quotes are real: the plan picks notable records from the digest, the extraction copies each
record's first sentence verbatim, and the writer builds a report with every required section from
the evidence pack. ``fabricate=True`` makes the first draft carry one excerpt that is not in its
record (the host check marks it and the repair removes it). No network, no credentials.
"""
from __future__ import annotations

import json
import re

from research.analysis.judge.client import fake_completion

from . import prompts
from .digest import ITEM_ID

FAKE = "FABRICATED: this sentence appears in no record."


def _system(body):
    for message in body.get("messages") or []:
        if message.get("role") == "system":
            content = message["content"]
            return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)
    return ""


def _user(body):
    return next((m["content"] for m in body.get("messages") or [] if m.get("role") == "user"), "")


class MockModel:
    def __init__(self, *, fabricate=False, cost="0.002"):
        self.fabricate = fabricate
        self.cost = cost
        self.calls = []

    def __call__(self, request):
        body = request["body"]
        system, user = _system(body), _user(body)
        if system == prompts.PLAN_SYSTEM:
            stage, text = "plan", self.plan(user)
        elif system == prompts.EXTRACT_SYSTEM:
            stage, text = "extract", self.extract(user)
        elif system == prompts.REPAIR_SYSTEM:
            stage, text = "repair", self.repair(user)
        elif system == prompts.WRITE_SYSTEM:
            stage, text = "write", self.write(user)
        else:
            raise AssertionError("unknown incident prompt")
        self.calls.append(stage)
        return fake_completion(text, cost=self.cost, model=body.get("model") or "anthropic/claude-opus-5.5")

    # -- stages --------------------------------------------------------------
    @staticmethod
    def plan(user):
        notable = re.findall(r"^  notable (" + ITEM_ID + r") \| [^\n]*$", user, re.M)
        posts = re.findall(r"^  (?:Board post|Direct message|Feedback to organizers|Library notebook) \| ("
                           + ITEM_ID + r")", user, re.M)
        agents = re.findall(r"^=== (agent-\d+)", user, re.M)
        first = (notable + posts)[:4]
        second = (posts + notable)[4:8] or (notable + posts)[:2]
        plan = {"headline": "Mock headline: the agents worked on the problems and mostly kept to the rules.",
                "storylines": [
                    {"id": "S1", "title": "How the first agent worked", "why": "Mock storyline.",
                     "agents": agents[:1], "items": [{"id": i, "why": "mock"} for i in first],
                     "extract": "decisive sentences"},
                    {"id": "S2", "title": "What the agents said to each other", "why": "Mock storyline.",
                     "agents": agents[1:3], "items": [{"id": i, "why": "mock"} for i in second]
                     + [{"id": "A99.t9999.r", "why": "an id that does not exist"}], "extract": "messages"}],
                "not_happened": ["No mock agent quit."], "uncertainties": ["Mock uncertainty."]}
        return json.dumps(plan)

    @staticmethod
    def extract(user):
        out = []
        for item_id, text in re.findall(r"----- RECORD (" + ITEM_ID + r") \([^\n]*\n(?:What the planner[^\n]*\n)?"
                                        r"(.*?)\n----- END OF RECORD \1", user, re.S):
            body = text.strip()
            sentence = re.split(r"(?<=[.!?])\s", body, maxsplit=1)[0][:300].strip()
            if sentence:
                out.append({"item": item_id, "quote": sentence, "note": "first sentence"})
        return json.dumps({"excerpts": out})

    def write(self, user, *, fabricate=None):
        fabricate = self.fabricate if fabricate is None else fabricate
        stories = re.findall(r"^## STORYLINE (S\d+): (.*)$", user, re.M)
        excerpts = re.findall(r"^### RECORD (" + ITEM_ID + r") \|[^\n]*\n(?:EXCERPT 1[^\n]*:\n(.*?)(?=\n### RECORD |"
                              r"\n## STORYLINE |\n*$))?", user, re.S | re.M)
        agents = re.findall(r"^=== (agent-\d+) \(A\d+\), persona ([^\n]*)$", user, re.M)
        lines = ["# Mock incident report: a quiet run", "", "## Summary", "",
                 "This mock report tells the whole story in plain words. The agents worked on the problems.", "",
                 "## Case studies", ""]
        used = [(i, q) for i, q in excerpts if q and q.strip()]
        for number, (sid, title) in enumerate(stories or [("S1", "Mock storyline")], 1):
            lines += [f"### {number}. {title}", "", "**Context.** The agent was trying to prove a theorem.", ""]
            for item_id, quote in used[(number - 1) * 2:(number - 1) * 2 + 2]:
                lines += [f":::excerpt {item_id}", quote.strip().split("\n")[0], ":::", ""]
                lines += [f"This shows the moment in plain words [{item_id}].", ""]
            if fabricate and number == 1 and used:
                lines += [f":::excerpt {used[0][0]}", FAKE, ":::", ""]
        lines += ["## What didn't happen", "", "No agent quit in protest.", "",
                  "## How sure we are", "", "This rests on model judgment.", "",
                  "## What to try next", "", "- **Prompts:** try a clearer rule statement.",
                  "- **Oversight:** check the checker.", "", "## The agents, one by one", ""]
        for agent_id, persona in agents:
            lines += [f"**{agent_id}** ({persona}) worked on the problems.", ""]
        return "\n".join(lines)

    @staticmethod
    def repair(user):
        draft = user.split("\nYOUR DRAFT:\n", 1)[1].rsplit("\n\nReturn the complete corrected report", 1)[0]
        return re.sub(r":::excerpt [^\n]+\n" + re.escape(FAKE) + r"\n:::\n\n?", "", draft)
