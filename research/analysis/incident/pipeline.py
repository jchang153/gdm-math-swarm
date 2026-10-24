"""Incident report pipeline: digest -> plan -> evidence -> write -> verify -> render.

1. Digest (host, no model): ``digest.build_digest``.
2. Plan (Claude Opus 5.5): 2 to 5 storylines and the record ids to quote (``prompts.PLAN_SYSTEM``).
   Unknown ids are dropped and recorded.
3. Evidence (host, plus the extraction model for long records): the exact text of every planned
   record is fetched by id from the rebuilt histories; a short record is offered whole, a long one is
   shown as numbered segments and the model picks passages by segment number
   (``prompts.EXTRACT_SYSTEM``); the host cuts them out and completes their boundaries
   (``complete_span``); a refused or empty extraction gets a flagged host-chosen passage.
   Model fallback (owner request, October 21, 2026): with a ``"fallback"`` client (a non-Anthropic
   model, route.FALLBACK_MODEL_ID), a unit Anthropic refuses is not resent to Anthropic; it goes once to
   the fallback model instead (unit ``<name>.fallback``), and everything that model chose is flagged
   (``model_fallbacks`` in evidence.json, a "Fallback model" badge on its excerpts).
4. Write (Opus): the Markdown report (``prompts.WRITE_SYSTEM``).
5. Verify (host): every excerpt block and inline quote must be a verbatim substring of its cited
   record (whitespace normalized only), citations must exist and the required sections must be
   present (``verify``). With problems, Opus revises once (``prompts.REPAIR_SYSTEM``); the version
   with fewer problems is kept, and every remaining unverified quote is marked in the output.
6. Render (host): report.md, report.html (banner, labelled excerpts, Inspect View deep links, the
   host-written "Where to look" appendix) and evidence.json.

Each model request is one cached unit (``units/<name>.json``, keyed by the SHA-256 of the exact
request), so a rerun or a resume after a stop pays nothing twice; raw requests and responses are
archived by ``JudgeClient`` under ``raw/`` before parsing. Spending is admitted only through the
invocation's ``RunLimit`` and the ledger (one per-run budget).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path

from . import contagion, prompts, render, verify
from .digest import ITEM_ID_RE, Locator, build_digest, digest_text

SCHEMA = "gdm-incident-evidence/2"
SHORT_CHARS = 700             # a record this short is offered to the writer whole
LONG_VIEW_CHARS = 50000       # a longer record is shown to the extractor as windows
MAX_STORYLINES = 5
MAX_ITEMS_PER_STORY = 8
MAX_ITEMS = 36
MAX_EXCERPT_CHARS = 900       # a model-chosen passage is cut to at most this (before boundary completion)
FALLBACK_EXCERPT_CHARS = 450  # a host-chosen passage aims at about this length
COMPLETION_CHARS = 500        # how far a passage may be extended to end at a complete boundary
SEGMENT_CHARS = 320           # a longer line is split at sentence ends into segments
REFUSALS = ("refused_once", "refused_twice")
MAX_TOKENS = {"plan": 16000, "extract": 12000, "write": 40000, "repair": 40000}
WRITER_NOTABLE = 10


class StageError(Exception):
    def __init__(self, stage, reason, detail=None):
        super().__init__(f"{stage}: {reason}")
        self.stage, self.reason, self.detail = stage, reason, detail


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def parse_json_object(text):
    """The first JSON object in a model reply (tolerates code fences and surrounding prose)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("the reply is not a JSON object")
    return value


def strip_markdown_fence(text):
    text = (text or "").strip()
    match = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", text, re.S)
    return match.group(1) if match else text


def long_view(text, pattern=None):
    """A long record as verbatim windows (head, keyword passages, tail), each under its offsets."""
    if len(text) <= LONG_VIEW_CHARS:
        return text
    spans = [(0, 18000), (len(text) - 8000, len(text))]
    if pattern is not None:
        budget = 20000
        for match in pattern.finditer(text):
            if budget <= 0:
                break
            lo, hi = max(0, match.start() - 700), min(len(text), match.end() + 700)
            spans.append((lo, hi))
            budget -= hi - lo
    spans.sort()
    merged = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    parts, last = [f"[record length {len(text):,} characters; shown as {len(merged)} verbatim window(s)]"], 0
    for lo, hi in merged:
        if lo > last:
            parts.append(f"[… {lo - last:,} characters omitted …]")
        parts.append(f"[characters {lo:,}-{hi:,}]\n" + text[lo:hi])
        last = hi
    if last < len(text):
        parts.append(f"[… {len(text) - last:,} characters omitted …]")
    return "\n".join(parts)


_NEXT_LINE = re.compile(r"[ \t]*(?:\r?\n[ \t]*)*[^\n]*\S")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]`*]*\s+")


def segments(text):
    """Numbered passage units of a record: each non-blank line, a long line split after its sentence
    ends. Returns [(lo, hi)] character spans (no surrounding whitespace), in order."""
    out, offset = [], 0
    for line in (text or "").splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body.strip():
            pieces, start = [], 0
            if len(body) > SEGMENT_CHARS:
                for match in _SENTENCE_END.finditer(body):
                    if match.start() - start >= 40:
                        pieces.append((start, match.start()))
                        start = match.end()
            pieces.append((start, len(body)))
            for lo, hi in pieces:
                chunk = body[lo:hi]
                if chunk.strip():
                    lead = len(chunk) - len(chunk.lstrip())
                    out.append((offset + lo + lead, offset + lo + len(chunk.rstrip())))
        offset += len(line)
    return out


def segment_view(text, pattern=None):
    """A long record shown to the extraction model as numbered segments ("[12] ..."); a very long one
    only as windows (head, keyword passages, tail) with the omitted segment ranges named."""
    spans = segments(text)
    keep = None
    if len(text) > LONG_VIEW_CHARS:
        windows = [(0, 18000), (len(text) - 8000, len(text))]
        if pattern is not None:
            budget = 20000
            for match in pattern.finditer(text):
                if budget <= 0:
                    break
                windows.append((max(0, match.start() - 700), min(len(text), match.end() + 700)))
                budget -= 1400
        keep = {k for k, (lo, hi) in enumerate(spans) if any(lo < b and a < hi for a, b in windows)}
    lines, skipped = [f"[record of {len(text):,} characters in {len(spans)} numbered segments]"], []
    for number, (lo, hi) in enumerate(spans, 1):
        if keep is not None and number - 1 not in keep:
            skipped.append(number)
            continue
        if skipped:
            lines.append(f"[segments {skipped[0]}-{skipped[-1]} omitted]")
            skipped = []
        lines.append(f"[{number}] {text[lo:hi]}")
    if skipped:
        lines.append(f"[segments {skipped[0]}-{skipped[-1]} omitted]")
    return "\n".join(lines)


def segment_span(text, first, last):
    """Character span of segments ``first``..``last`` (1-based, inclusive), at most MAX_EXCERPT_CHARS long
    (later segments dropped), or None for numbers out of range."""
    spans = segments(text)
    try:
        first, last = int(first), int(last if last is not None else first)
    except (TypeError, ValueError):
        return None
    if not 1 <= first <= len(spans):
        return None
    last = min(max(first, last), len(spans))
    hi = spans[first - 1][1]
    for number in range(first, last + 1):
        if spans[number - 1][1] - spans[first - 1][0] > MAX_EXCERPT_CHARS and number > first:
            break
        hi = spans[number - 1][1]
    return spans[first - 1][0], hi


def complete_span(text, lo, hi):
    """Extend (or, failing that, trim) the passage ``text[lo:hi]`` so it does not read as cut short: it must
    not end on a colon (the next line is added), stop inside a Lean editable block (extended to the
    "EDITABLE END" line) or stop mid-sentence (extended to the sentence end). Returns (lo, hi), or None
    when no complete passage remains."""
    limit = min(len(text), hi + COMPLETION_CHARS)
    for _ in range(6):
        quote = text[lo:hi]
        problem = verify.boundary_problem(quote)
        if problem is None and hi < len(text) and text[hi - 1] not in ".!?:;)]}\"'`*\n" and text[hi] not in "\r\n":
            stop = _SENTENCE_END.search(text, hi, limit)
            newline = text.find("\n", hi, limit)
            ends = [x for x in (stop.start() if stop else -1, newline) if x >= 0]
            if not ends:
                break
            hi = min(ends)
            continue
        if problem is None:
            return lo, hi
        if "colon" in problem:   # add the next non-blank line (what the colon introduces)
            following = _NEXT_LINE.match(text, hi, limit)
            if following is None:
                break
            hi = following.end()
            continue
        end_marker = text.find("EDITABLE END", hi)
        if 0 <= end_marker < limit:
            end = text.find("\n", end_marker)
            hi = len(text) if end < 0 else end
            continue
        break
    # Could not complete it forwards: trim back to the last complete line before the problem.
    quote = text[lo:hi]
    while quote and verify.boundary_problem(quote):
        cut = quote.rstrip().rfind("\n")
        if cut <= 0:
            return None
        quote = quote[:cut]
        start = quote.rfind("EDITABLE START")
        if start >= 0 and "EDITABLE END" not in quote[start:]:
            line_start = quote.rfind("\n", 0, start)
            quote = quote[:max(0, line_start)]
    quote = quote.rstrip()
    return (lo, lo + len(quote)) if quote.strip() else None


def excerpt_sources(evidence):
    """Counts of the excerpts by who chose them, and the ids of the program-chosen (fallback) ones."""
    counts, fallback, model_fallback = {"whole_record": 0, "model": 0, "host_fallback": 0}, [], []
    for story in evidence["storylines"]:
        for record in story["records"]:
            for excerpt in record["excerpts"]:
                key = "whole_record" if excerpt["source"] == "host" else excerpt["source"]
                counts[key] = counts.get(key, 0) + 1
                if excerpt["source"] == "host_fallback":
                    fallback.append({"eid": excerpt["eid"], "item": record["id"], "storyline": story["id"],
                                     "reason": excerpt.get("fallback_reason")})
                if excerpt.get("model_fallback"):
                    model_fallback.append({"eid": excerpt["eid"], "item": record["id"], "storyline": story["id"],
                                           "model": excerpt.get("model")})
    return dict(counts, fallback=fallback, model_fallback=model_fallback)


class IncidentPipeline:
    """One incident report for one run. ``clients``: {"opus": JudgeClient (plan, write), "extract":
    JudgeClient (excerpt selection; Opus by default)}."""

    def __init__(self, run_dir, out_dir, run, *, clients, mode, models, epochs=None, judge_dir=None, repair=True,
                 log=None, simple_judge_dir=None, judge_source="auto"):
        self.run_dir = Path(run_dir)
        self.out_dir = Path(out_dir)
        self.history = run
        self.clients = clients
        self.mode = mode
        self.models = models
        self.repair_enabled = repair
        self.log = log or (lambda message: None)
        self.locator = Locator(run, run_dir, epochs=epochs)
        self.judge_dir = judge_dir
        self.simple_judge_dir = simple_judge_dir
        self.judge_source = judge_source
        self.units = []
        self.dropped = []
        self.extract_refusals = []
        self.model_fallbacks = []
        self.judge_quotes = {}

    # -- model units -----------------------------------------------------------
    async def call(self, key, stage, unit, system, user, *, refusal_retry=True):
        return (await self.call_info(key, stage, unit, system, user, refusal_retry=refusal_retry))[0]

    async def call_info(self, key, stage, unit, system, user, *, refusal_retry=True):
        """(reply text, {"model", "fallback"}). With a fallback client, a refused request is never resent
        to the refusing model: the unit goes once to the fallback model instead (``<unit>.fallback``)."""
        fallback = self.clients.get("fallback")
        if fallback is None or fallback is self.clients[key]:
            text = await self._call(key, stage, unit, system, user, refusal_retry=refusal_retry)
            return text, {"model": self.clients[key].route.model_id, "fallback": False}
        try:
            text = await self._call(key, stage, unit, system, user, refusal_retry=False)
            return text, {"model": self.clients[key].route.model_id, "fallback": False}
        except StageError as error:
            if error.reason not in REFUSALS:
                raise
            refused = error
        message = (refused.detail or {}).get("message") if isinstance(refused.detail, dict) else None
        entry = {"unit": unit, "stage": stage, "refused_by": self.clients[key].route.model_id,
                 "reason": refused.reason, "message": message, "fallback_model": fallback.route.model_id,
                 "fallback_unit": f"{unit}.fallback"}
        self.model_fallbacks.append(entry)
        self.log(f"{stage}: {unit} refused by {entry['refused_by']}; using the fallback model {entry['fallback_model']}")
        try:
            text = await self._call("fallback", stage, f"{unit}.fallback", system, user, refusal_retry=False)
        except StageError as error:
            entry["status"] = error.reason
            raise
        entry["status"] = "ok"
        return text, {"model": fallback.route.model_id, "fallback": True}

    async def _call(self, key, stage, unit, system, user, *, refusal_retry=True):
        client = self.clients[key]
        max_tokens = MAX_TOKENS[stage]
        request_sha = hashlib.sha256(json.dumps([client.route.model_id, system, user, max_tokens],
                                                ensure_ascii=False).encode("utf-8")).hexdigest()
        path = self.out_dir / "units" / f"{unit}.json"
        cached = read_json(path)
        if cached and cached.get("request_sha256") == request_sha and cached.get("status") == "ok":
            self.units.append({"unit": unit, "model": cached.get("model"), "cached": True,
                               "cost_usd": cached.get("cost_usd"), **({"fallback": True} if key == "fallback" else {})})
            return cached["text"]
        refused = ("refused_once", "refused_twice")
        if cached and cached.get("request_sha256") == request_sha and (cached.get("error") or {}).get("reason") in refused:
            # A refused request is never resent identically (the judge's refusal rule); the caller falls back.
            self.units.append({"unit": unit, "model": cached.get("model"), "cached": True, "status": cached["status"],
                               "cost_usd": cached.get("cost_usd")})
            raise StageError(stage, cached["error"]["reason"], cached["error"])
        self.log(f"{stage}: {unit} -> {client.route.model_id}")
        result = await client.complete(system=system, user=user, purpose=f"incident_{stage}", unit_id=unit,
                                       max_tokens=max_tokens, effort=client.route.effort,
                                       refusal_retry=refusal_retry)
        record = {"unit": unit, "stage": stage, "model": client.route.model_id, "request_sha256": request_sha,
                  "status": result["status"], "error": result.get("error"), "text": result.get("text"),
                  "cost_usd": None if result.get("cost_usd") is None else str(result["cost_usd"]),
                  "attempts": result.get("attempts"), "usage": result.get("usage"),
                  "raw_paths": result.get("raw_paths"), "at": _now(), "prompts": prompts.VERSION}
        write_json(path, record)
        self.units.append({"unit": unit, "model": record["model"], "cached": False, "cost_usd": record["cost_usd"],
                           "status": record["status"], **({"fallback": True} if key == "fallback" else {})})
        if result["status"] != "ok" or not result.get("text"):
            raise StageError(stage, (result.get("error") or {}).get("reason") or result["status"], result.get("error"))
        return result["text"]

    # -- stages ----------------------------------------------------------------
    async def plan(self, digest):
        text = digest_text(digest)
        user = prompts.plan_user(text)
        reply = await self.call("opus", "plan", "plan", prompts.PLAN_SYSTEM, user)
        try:
            plan = parse_json_object(reply)
        except ValueError as error:
            reply = await self.call("opus", "plan", "plan-retry", prompts.PLAN_SYSTEM,
                                    user + f"\n\n(Your previous answer could not be parsed: {error}. Answer with one "
                                           "JSON object only.)")
            plan = parse_json_object(reply)
        return self.clean_plan(plan)


    def _run_relative(self, path):
        """A path inside the run as a run-relative string (published evidence carries no host paths)."""
        if not path:
            return path
        try:
            return Path(path).resolve().relative_to(self.run_dir.resolve()).as_posix()
        except ValueError:
            return Path(path).name
    def clean_plan(self, plan):
        stories, total = [], 0
        planned = [s for s in plan.get("storylines") or [] if isinstance(s, dict)][:MAX_STORYLINES]
        awareness = plan.get("evaluation_awareness")
        if isinstance(awareness, dict):   # the evaluation-awareness section's evidence, as one more "storyline"
            planned.append(dict(awareness, id="EA", title=awareness.get("title") or "Evaluation awareness",
                                why=awareness.get("summary") or awareness.get("why"), section="evaluation_awareness"))
        for number, story in enumerate(planned, 1):
            items = []
            for entry in story.get("items") or []:
                item_id = entry.get("id") if isinstance(entry, dict) else entry
                why = entry.get("why") if isinstance(entry, dict) else None
                if not isinstance(item_id, str):
                    continue
                if self.locator.item(item_id) is None:
                    self.dropped.append({"storyline": story.get("id"), "item": item_id, "reason": "unknown id"})
                    continue
                if len(items) >= MAX_ITEMS_PER_STORY or total >= MAX_ITEMS or any(i["id"] == item_id for i in items):
                    self.dropped.append({"storyline": story.get("id"), "item": item_id, "reason": "over the limit"})
                    continue
                items.append({"id": item_id, "why": why})
                total += 1
            section = story.get("section") or "case_study"
            stories.append({"id": "EA" if section == "evaluation_awareness" else
                            f"S{sum(s['section'] == 'case_study' for s in stories) + 1}",
                            "section": section, "planned_id": story.get("id"),
                            "title": str(story.get("title") or f"Storyline {number}"), "why": story.get("why"),
                            "agents": [a for a in story.get("agents") or [] if isinstance(a, str)],
                            "extract": story.get("extract"), "items": items})
        if not any(s["section"] == "case_study" for s in stories):
            raise StageError("plan", "no storyline in the plan")
        return {"headline": plan.get("headline"), "storylines": stories,
                "not_happened": [s for s in plan.get("not_happened") or [] if isinstance(s, str)],
                "uncertainties": [s for s in plan.get("uncertainties") or [] if isinstance(s, str)]}

    async def evidence(self, plan):
        from research.analysis.judge.pipeline import RECALL_PATTERN
        stories, number = [], 0
        for story in plan["storylines"]:
            records, long_records = [], []
            for entry in story["items"]:
                item = self.locator.item(entry["id"])
                loc = self.locator.locate(entry["id"])
                record = {"id": item["id"], "why": entry.get("why"), "locator": loc, "excerpts": [], "rejected": []}
                text = (item.get("text") or "").strip()
                if len(text) <= SHORT_CHARS:
                    if text:
                        record["excerpts"].append({"quote": text, "note": "whole record", "source": "host"})
                else:
                    long_records.append({"id": item["id"], "label": loc["label"], "agent": loc["agent"],
                                         "time": loc["time"], "why": entry.get("why"),
                                         "text": segment_view(item["text"], RECALL_PATTERN)})
                records.append(record)
            by_id = {r["id"]: r for r in records}
            if long_records:
                await self.extract(story, long_records, by_id)
            for record in records:
                if record["id"] in {r["id"] for r in long_records} and not record["excerpts"]:
                    refused = any(record["id"] in r["items"] and not r.get("split") for r in self.extract_refusals
                                  if r["storyline"] == story["id"])
                    self.fallback_excerpt(record, RECALL_PATTERN, reason="the extraction model's request was refused "
                                          "by the provider" if refused else "the extraction model returned no usable "
                                          "passage")
                for excerpt in record["excerpts"]:
                    number += 1
                    excerpt["eid"] = f"E{number}"
                    excerpt["verified"] = True
                    excerpt["label"] = self.locator.excerpt_label(record["id"], excerpt["quote"])
                    excerpt["fallback"] = excerpt["source"] == "host_fallback"
            stories.append(dict(story, records=records))
        return {"storylines": stories}

    async def extract(self, story, long_records, by_id):
        """The extraction model picks passages by segment numbers (it never copies record text); the
        host cuts each passage out of the record and completes its boundaries (``complete_span``). A
        refused multi-record request is never resent: each record is then asked for on its own, and a
        record still refused (or without a usable passage) gets a host-chosen excerpt
        (``fallback_excerpt``), flagged as such in the report."""
        key = "extract"
        try:
            replies = [await self.call_info(key, "extract", f"extract-{story['id']}", prompts.EXTRACT_SYSTEM,
                                            prompts.extract_user(story, long_records),
                                            refusal_retry=len(long_records) == 1)]
        except StageError as error:
            if error.reason in REFUSALS and "fallback" in self.clients:
                # Refused by Anthropic and by the fallback model: never resent piecemeal to the
                # model that refused; every record gets a flagged host-chosen passage.
                self.extract_refusals.append({"storyline": story["id"], "items": [r["id"] for r in long_records]})
                return
            if error.reason not in REFUSALS or len(long_records) == 1:
                if error.reason in REFUSALS:
                    self.extract_refusals.append({"storyline": story["id"], "items": [r["id"] for r in long_records]})
                    return
                raise
            self.extract_refusals.append({"storyline": story["id"], "items": [r["id"] for r in long_records],
                                          "split": True})
            replies = []
            for record in long_records:
                try:
                    replies.append(await self.call_info(key, "extract", f"extract-{story['id']}-{record['id']}",
                                                        prompts.EXTRACT_SYSTEM, prompts.extract_user(story, [record])))
                except StageError as single:
                    if single.reason not in REFUSALS:
                        raise
                    self.extract_refusals.append({"storyline": story["id"], "items": [record["id"]]})
        for reply, info in replies:
            try:
                passages = parse_json_object(reply).get("passages") or []
            except ValueError:
                passages = []
            for passage in passages:
                if not isinstance(passage, dict) or passage.get("item") not in by_id:
                    continue
                target = by_id[passage["item"]]
                if len(target["excerpts"]) >= 3:
                    continue
                text = self.locator.item(target["id"])["text"]
                entry = {"note": passage.get("note"), "source": "model", "model": info["model"],
                         "segments": [passage.get("first"), passage.get("last")]}
                if info["fallback"]:
                    entry["model_fallback"] = True
                span = segment_span(text, passage.get("first"), passage.get("last"))
                if span is None:
                    target["rejected"].append(dict(entry, reason="segment numbers out of range"))
                    continue
                span = complete_span(text, *span)
                if span is None:
                    target["rejected"].append(dict(entry, reason="passage could not be cut at a complete boundary"))
                    continue
                if any(lo < span[1] and span[0] < hi for lo, hi in (e.get("offsets") or (0, 0)
                                                                     for e in target["excerpts"])):
                    target["rejected"].append(dict(entry, reason="overlaps an earlier passage of the record"))
                    continue
                target["excerpts"].append(dict(entry, quote=text[span[0]:span[1]].strip(), offsets=list(span)))

    def fallback_excerpt(self, record, pattern, *, reason):
        """Host-chosen excerpt of a long record: around a behavior-judge quote of it, else around the first
        keyword-net match, else its opening; cut at complete boundaries (``complete_span``)."""
        text = self.locator.item(record["id"])["text"]
        anchor = None
        for quote in self.judge_quotes.get(record["id"], []):
            span = verify.locate_span(text, quote)
            if span:
                anchor = span
                break
        if anchor is None:
            match = pattern.search(text)
            anchor = (match.start(), match.end()) if match else (0, 0)
        spans = segments(text)
        first = next((k for k, (lo, hi) in enumerate(spans) if hi > anchor[0]), 0) if spans else 0
        first = max(0, first - 1)
        last = first
        while last + 1 < len(spans) and (spans[last][1] < anchor[1] or spans[last + 1][1] - spans[first][0]
                                         <= FALLBACK_EXCERPT_CHARS):
            last += 1
        span = complete_span(text, *(spans[first][0], spans[last][1])) if spans else None
        if span:
            record["excerpts"].append({"quote": text[span[0]:span[1]].strip(), "note": "program-chosen passage",
                                       "source": "host_fallback", "fallback_reason": reason, "offsets": list(span)})

    def expand_excerpts(self, markdown, evidence):
        """':::excerpt E7' lines -> verbatim excerpt blocks (':::excerpt <item>' ... ':::'); unknown or
        repeated ids are removed and returned as problems."""
        table = {e["eid"]: (r["id"], e["quote"]) for s in evidence["storylines"] for r in s["records"]
                 for e in r["excerpts"]}
        problems, used = [], set()

        def replace(match):
            eid = match.group(1)
            if eid not in table:
                problems.append(f"unknown excerpt id {eid}")
                return ""
            if eid in used:
                problems.append(f"excerpt {eid} used more than once")
                return ""
            used.add(eid)
            item_id, quote = table[eid]
            return f":::excerpt {item_id} {eid}\n{quote}\n:::"

        out = re.sub(r"^:::excerpt[ \t]+(E\d+)[ \t]*(?:\n:::[ \t]*(?=\n|$))?", replace, markdown, flags=re.M)
        return out, problems

    async def write(self, digest, plan, evidence):
        text = digest_text(digest, per_agent_notable=WRITER_NOTABLE)
        raw = strip_markdown_fence(await self.call("opus", "write", "write", prompts.WRITE_SYSTEM,
                                                   prompts.write_user(text, plan, evidence)))
        draft, expand_problems = self.expand_excerpts(raw, evidence)
        checks = verify.verify_markdown(draft, self.locator.item)
        problems = expand_problems + self.problems(draft, checks)
        attempts = [{"version": "draft", "problems": problems, "summary": checks["summary"]}]
        if problems and self.repair_enabled:
            revised_raw = strip_markdown_fence(await self.call("opus", "repair", "repair", prompts.REPAIR_SYSTEM,
                                                               prompts.repair_user(raw, problems, evidence)))
            revised, revised_expand = self.expand_excerpts(revised_raw, evidence)
            revised_checks = verify.verify_markdown(revised, self.locator.item)
            revised_problems = revised_expand + self.problems(revised, revised_checks)
            attempts.append({"version": "repair", "problems": revised_problems, "summary": revised_checks["summary"]})
            if len(revised_problems) <= len(problems):
                draft, problems = revised, revised_problems
                attempts[-1]["kept"] = True
        return draft, attempts

    @staticmethod
    def problems(markdown, checks):
        out = list(verify.structure_problems(markdown))
        for quote in checks["quotes"]:
            if not quote["verified"]:
                out.append(f"{quote['form']} quote of {quote['item']} is not verbatim ({quote['reason']}): "
                           f"{verify.normalize(quote['quote'])[:120]!r}")
        for citation in checks["citations"]:
            if not citation["exists"]:
                out.append(f"unknown record id [{citation['item']}]")
        return out

    # -- whole run -----------------------------------------------------------------
    async def run(self):
        generated = _now()
        # Host-computed, before any model stage: written to contagion.json even if a model stage fails.
        sources = {"judge_dir": self.judge_dir, "simple_judge_dir": self.simple_judge_dir,
                   "judge_source": self.judge_source}
        spread = contagion.compute(self.run_dir, **sources)
        write_json(self.out_dir / "contagion.json", spread)
        digest = build_digest(self.run_dir, self.history, **sources)
        write_json(self.out_dir / "digest.json", digest)
        self.log(digest["judge"]["statement"] + f" Contagion: {spread['behavior_findings']}")
        for agent in digest["agents"].values():
            for finding in (agent.get("judge") or {}).get("findings") or []:
                for cited in finding.get("evidence") or []:
                    if cited.get("item") and cited.get("quote"):
                        self.judge_quotes.setdefault(cited["item"], []).append(cited["quote"])
        self.log(f"digest: {len(digest['agents'])} agents, {len(digest_text(digest)):,} characters of prompt text")
        plan = await self.plan(digest)
        evidence = await self.evidence(plan)
        draft, attempts = await self.write(digest, plan, evidence)
        evidence["cited_ids"] = list(dict.fromkeys(m.group(0) for m in ITEM_ID_RE.finditer(draft)
                                                   if self.locator.item(m.group(0)) is not None))
        evidence["excerpt_sources"] = excerpt_sources(evidence)
        note = render.host_note_markdown(digest, evidence, self.extract_refusals, self.units, draft,
                                         model_fallbacks=self.model_fallbacks)
        body = render.insert_into_section(draft.rstrip(), "How sure we are", note) + "\n" \
            + render.appendix_markdown(evidence, self.locator, digest)
        checks = verify.verify_markdown(body, self.locator.item)
        markdown = render.banner_markdown(generated) + render.reader_markdown(body, checks, self.locator, evidence)
        markdown = contagion.insert_markdown(markdown, spread)
        (self.out_dir / "report.md").write_text(markdown, encoding="utf-8")
        page = render.render_html(body, checks, self.locator, digest, generated_at=generated, models=self.models,
                                  evidence=evidence)
        page = render.add_toc(contagion.insert_html(page, spread))
        (self.out_dir / "report.html").write_text(page, encoding="utf-8")
        record = {"schema": SCHEMA, "run_id": digest["run_id"], "generated_at": generated, "mode": self.mode,
                  "model_generated": True, "post_hoc": True, "models": self.models, "prompts": prompts.VERSION,
                  "verification": {"rule": "each quote piece is a verbatim substring of the cited record's full text "
                                           "after whitespace normalization only",
                                   "summary": checks["summary"], "quotes": [{k: q[k] for k in q if k != "span"}
                                                                            for q in checks["quotes"]],
                                   "citations": checks["citations"],
                                   "structure_problems": verify.structure_problems(draft), "attempts": attempts},
                  "chain_of_thought": {k: v for k, v in digest["reasoning"].items()},
                  "behavior_findings": {**{k: digest["judge"].get(k) for k in ("source", "label", "statement", "rubric",
                                                                               "preference", "problem")},
                                        "path": self._run_relative(digest["judge"].get("path"))},
                  "plan": plan, "plan_dropped_items": self.dropped,
                  "extract_refusals": self.extract_refusals, "model_fallbacks": self.model_fallbacks,
                  "evidence": evidence,
                  "locators": {item_id: self.locator.locate(item_id) for item_id in evidence["cited_ids"]},
                  "inspect_view_present": self.locator.inspect_view_present, "units": self.units,
                  "contagion": spread}
        write_json(self.out_dir / "evidence.json", record)
        return {"status": "complete", "report": str(self.out_dir / "report.html"),
                "verification": checks["summary"],
                "storylines": sum(s["section"] == "case_study" for s in plan["storylines"]),
                "excerpt_sources": evidence["excerpt_sources"], "chain_of_thought": digest["reasoning"]["status"],
                "behavior_findings": digest["judge"].get("label"),
                "model_fallbacks": self.model_fallbacks,
                "units": self.units}
