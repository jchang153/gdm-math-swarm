"""Incident report pipeline: digest -> plan -> evidence -> write -> verify -> render.

1. Digest (host, no model): ``digest.build_digest``.
2. Plan (Claude Opus 5.5): 2 to 5 storylines and the record ids to quote (``prompts.PLAN_SYSTEM``).
   Unknown ids are dropped and recorded.
3. Evidence (host, plus Claude Sonnet 5.5 for long records): the exact text of every planned record
   is fetched by id from the rebuilt histories; a short record is offered whole, a long one goes to
   Sonnet, which copies a few short passages (``prompts.EXTRACT_SYSTEM``); every passage is checked
   verbatim by the host and an unverified one is discarded (recorded).
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

from . import prompts, render, verify
from .digest import ITEM_ID_RE, Locator, build_digest, digest_text

SCHEMA = "gdm-incident-evidence/1"
SHORT_CHARS = 700             # a record this short is offered to the writer whole
LONG_VIEW_CHARS = 50000       # a longer record is shown to the extractor as windows
MAX_STORYLINES = 5
MAX_ITEMS_PER_STORY = 8
MAX_ITEMS = 30
MAX_EXCERPT_CHARS = 900
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


class IncidentPipeline:
    """One incident report for one run. ``clients``: {"opus": JudgeClient, "sonnet": JudgeClient}."""

    def __init__(self, run_dir, out_dir, run, *, clients, mode, models, epochs=None, judge_dir=None, repair=True,
                 log=None):
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
        self.units = []
        self.dropped = []
        self.extract_refusals = []
        self.judge_quotes = {}

    # -- model units -----------------------------------------------------------
    async def call(self, key, stage, unit, system, user, *, refusal_retry=True):
        client = self.clients[key]
        max_tokens = MAX_TOKENS[stage]
        request_sha = hashlib.sha256(json.dumps([client.route.model_id, system, user, max_tokens],
                                                ensure_ascii=False).encode("utf-8")).hexdigest()
        path = self.out_dir / "units" / f"{unit}.json"
        cached = read_json(path)
        if cached and cached.get("request_sha256") == request_sha and cached.get("status") == "ok":
            self.units.append({"unit": unit, "model": cached.get("model"), "cached": True,
                               "cost_usd": cached.get("cost_usd")})
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
                           "status": record["status"]})
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

    def clean_plan(self, plan):
        stories, total = [], 0
        for number, story in enumerate(plan.get("storylines") or [], 1):
            if not isinstance(story, dict) or len(stories) >= MAX_STORYLINES:
                continue
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
            stories.append({"id": f"S{len(stories) + 1}", "planned_id": story.get("id"),
                            "title": str(story.get("title") or f"Storyline {number}"), "why": story.get("why"),
                            "agents": [a for a in story.get("agents") or [] if isinstance(a, str)],
                            "extract": story.get("extract"), "items": items})
        if not stories:
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
                                         "text": long_view(item["text"], RECALL_PATTERN)})
                records.append(record)
            by_id = {r["id"]: r for r in records}
            if long_records:
                await self.extract(story, long_records, by_id)
            for record in records:
                if record["id"] in {r["id"] for r in long_records} and not record["excerpts"]:
                    self.fallback_excerpt(record, RECALL_PATTERN)
                for excerpt in record["excerpts"]:
                    number += 1
                    excerpt["eid"] = f"E{number}"
                    excerpt["verified"] = True
            stories.append(dict(story, records=records))
        return {"storylines": stories}

    async def extract(self, story, long_records, by_id):
        """Sonnet points to passages by anchors; the host cuts them out. A refused multi-record request
        is never resent: each record is then asked for on its own, and a record still refused (or
        without a usable passage) gets a host-chosen excerpt (``fallback_excerpt``)."""
        groups = [long_records]
        try:
            replies = [await self.call("sonnet", "extract", f"extract-{story['id']}", prompts.EXTRACT_SYSTEM,
                                       prompts.extract_user(story, long_records),
                                       refusal_retry=len(long_records) == 1)]
        except StageError as error:
            if error.reason not in ("refused_once", "refused_twice") or len(long_records) == 1:
                if error.reason in ("refused_once", "refused_twice"):
                    self.extract_refusals.append({"storyline": story["id"], "items": [r["id"] for r in long_records]})
                    return
                raise
            self.extract_refusals.append({"storyline": story["id"], "items": [r["id"] for r in long_records],
                                          "split": True})
            replies, groups = [], [[r] for r in long_records]
            for record in long_records:
                try:
                    replies.append(await self.call("sonnet", "extract", f"extract-{story['id']}-{record['id']}",
                                                   prompts.EXTRACT_SYSTEM, prompts.extract_user(story, [record])))
                except StageError as single:
                    if single.reason not in ("refused_once", "refused_twice"):
                        raise
                    self.extract_refusals.append({"storyline": story["id"], "items": [record["id"]]})
        for reply in replies:
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
                span = verify.locate_span(text, str(passage.get("start") or ""), str(passage.get("end") or ""))
                entry = {"note": passage.get("note"), "source": "sonnet", "anchors": [passage.get("start"),
                                                                                       passage.get("end")]}
                if span is None:
                    target["rejected"].append(dict(entry, reason="anchors not found verbatim"))
                    continue
                quote = text[span[0]:span[1]].strip()
                if len(quote) > MAX_EXCERPT_CHARS:
                    target["rejected"].append(dict(entry, reason=f"passage longer than {MAX_EXCERPT_CHARS} characters"))
                    continue
                target["excerpts"].append(dict(entry, quote=quote, offsets=list(span)))

    def fallback_excerpt(self, record, pattern):
        """Host-chosen excerpt of a long record: around a behavior-judge quote of it, else around the first
        keyword-net match, else its opening; cut at sentence boundaries, at most MAX_EXCERPT_CHARS."""
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
        lo = max(0, anchor[0] - 250)
        hi = min(len(text), max(anchor[1] + 250, lo + 400), lo + MAX_EXCERPT_CHARS)
        if lo:
            cut = max(text.rfind(". ", lo - 200, anchor[0]), text.rfind("\n", lo - 200, anchor[0]))
            lo = cut + 1 if cut >= 0 else lo
        end = max(text.rfind(". ", anchor[1], hi), text.rfind("\n", anchor[1], hi))
        hi = end + 1 if end > anchor[1] else hi
        quote = text[lo:hi].strip()
        if quote:
            record["excerpts"].append({"quote": quote, "note": "host-chosen passage (extraction unavailable)",
                                       "source": "host_fallback", "offsets": [lo, hi]})

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
            return f":::excerpt {item_id}\n{quote}\n:::"

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
        digest = build_digest(self.run_dir, self.history, judge_dir=self.judge_dir)
        write_json(self.out_dir / "digest.json", digest)
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
        body = draft.rstrip() + "\n" + render.appendix_markdown(evidence, self.locator, digest)
        checks = verify.verify_markdown(body, self.locator.item)
        markdown = render.banner_markdown(generated) + render.annotate_markdown(body, checks)
        (self.out_dir / "report.md").write_text(markdown, encoding="utf-8")
        page = render.render_html(body, checks, self.locator, digest, generated_at=generated, models=self.models)
        (self.out_dir / "report.html").write_text(page, encoding="utf-8")
        record = {"schema": SCHEMA, "run_id": digest["run_id"], "generated_at": generated, "mode": self.mode,
                  "model_generated": True, "post_hoc": True, "models": self.models, "prompts": prompts.VERSION,
                  "verification": {"rule": "each quote piece is a verbatim substring of the cited record's full text "
                                           "after whitespace normalization only",
                                   "summary": checks["summary"], "quotes": [{k: q[k] for k in q if k != "span"}
                                                                            for q in checks["quotes"]],
                                   "citations": checks["citations"],
                                   "structure_problems": verify.structure_problems(draft), "attempts": attempts},
                  "plan": plan, "plan_dropped_items": self.dropped,
                  "extract_refusals": self.extract_refusals, "evidence": evidence,
                  "locators": {item_id: self.locator.locate(item_id) for item_id in evidence["cited_ids"]},
                  "inspect_view_present": self.locator.inspect_view_present, "units": self.units}
        write_json(self.out_dir / "evidence.json", record)
        return {"status": "complete", "report": str(self.out_dir / "report.html"),
                "verification": checks["summary"], "storylines": len(plan["storylines"]),
                "units": self.units}
