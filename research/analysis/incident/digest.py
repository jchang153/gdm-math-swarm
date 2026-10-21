"""Deterministic run digest and item locators for the incident report (no model, no network).

The digest is what the planning and writing models read about a run: the run's facts
(``results.json``), the behavior judge's verified outputs when present (``analysis/judge``), the
independent proof checker's verdicts (``evaluations/`` as summarized in ``results.json``), the
conference records (``conference/state.sqlite``, read only through the judge's history builder),
the timeline (``analysis/timeline.jsonl``) and, per agent, its activity and a short list of notable
items with previews. Every item is named by the judge history's stable id (``A07.t0123.r``,
``post-000004``, ``sub-000012``, ...), so the models can ask for exact texts and the host can check
every quote against the cited record.

``Locator`` turns an item id into what a reader (or a coding agent) needs to find it: the agent,
kind, time, a deep link into the run's Inspect View bundle (``inspect-view/``; the sample's
transcript at the model event, ``#/logs/<log>/samples/sample/<id>/<epoch>/transcript?event=<uuid>``)
and the raw record path.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import quote

SCHEMA = "gdm-incident-digest/1"
INSPECT_VIEW = "inspect-view/index.html"
# The report lives at RUN/analysis/incident/report.html: links into the run go up two levels.
RUN_PREFIX = "../../"

PREVIEW_CHARS = 280
MAX_COMMUNICATIONS = 24
MAX_KEYWORD_ITEMS = 14
SUMMARY_EXCERPT_CHARS = 1800
INSTRUCTIONS_CHARS = 9000

ITEM_ID = r"(?:A\d{2,3}\.t\d{4}\.(?:r|x|s|c\d+|o\d+|u\d+)|A\d{2,3}\.f\[[^\]\s]+\]@\d+|(?:post|dm|fb|lib|sub|feedback|note)-\d{6}|seq-\d{6}|RT\.L\d+)"
ITEM_ID_RE = re.compile(ITEM_ID)

_CONFERENCE_LABELS = {"board": "Board post", "dm": "Direct message", "feedback": "Feedback to organizers",
                      "notebook": "Library notebook", "library_solution": "Library solution (published automatically)",
                      "submission": "Submitted proof"}
_CALL_LABELS = {"submission": "Submission (tool call)", "board": "Board post (tool call)",
                "dm": "Direct message (tool call)", "feedback": "Feedback to organizers (tool call)",
                "notebook": "Library tool call", "library_solution": "Library tool call"}


def label_for(item):
    """The excerpt label a reader sees for an item (host-assigned, never model-written)."""
    kind, channel = item.get("kind"), item.get("channel")
    if kind == "reasoning":
        return "Reasoning (chain of thought)"
    if kind == "text":
        return "Agent message"
    if kind == "summary":
        return "Agent's own summary (written at a context compaction)"
    if kind == "tool_call":
        return _CALL_LABELS.get(channel, "Tool call")
    if kind == "tool_result":
        return "Tool result (what the agent was shown)"
    if kind == "file_version":
        return "Private file version"
    if kind == "conference":
        return _CONFERENCE_LABELS.get(channel, "Conference record")
    if kind == "runtime":
        return "Harness input" if channel == "harness" else "Runtime record"
    return str(kind or "Record")


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _preview(text, limit=PREVIEW_CHARS, around=None):
    text = re.sub(r"\s+", " ", text or "").strip()
    if around is not None and len(text) > limit:
        start = max(0, around - limit // 3)
        piece = text[start:start + limit]
        return ("…" if start else "") + piece + ("…" if start + limit < len(text) else "")
    return text if len(text) <= limit else text[:limit] + "…"


class Locator:
    """Item id -> location record (agent, kind, label, time, Inspect View link, raw record path).

    ``run``: a judge ``RunHistory`` with its agents rebuilt (``history.build_runs``) or a synthetic
    one. ``epochs``: {agent id: epoch} of each Inspect sample (default 1). ``prefix``: the path
    from the report to the run directory."""

    def __init__(self, run, run_dir, *, epochs=None, prefix=RUN_PREFIX):
        self.run = run
        self.run_dir = Path(run_dir)
        self.epochs = dict(epochs or {})
        self.prefix = prefix
        self.inspect_view_present = (self.run_dir / INSPECT_VIEW).is_file()

    def item(self, item_id):
        return self.run.lookup(item_id)

    def _inspect_of(self, item):
        meta = item.get("meta") or {}
        ref = meta.get("inspect") or meta.get("inspect_result")
        if not ref and meta.get("call_item"):
            call = self.run.lookup(meta["call_item"])
            if call is not None:
                ref = (call.get("meta") or {}).get("inspect")
        if not ref or not ref.get("file"):
            return None
        sample = ref.get("sample_id") or item.get("agent_id")
        return {"file": ref["file"], "log": Path(ref["file"]).name, "sample_id": sample,
                "epoch": self.epochs.get(sample, 1), "event_uuid": ref.get("event_uuid"),
                "message_id": ref.get("message_id")}

    def inspect_url(self, ref):
        """Deep link into the run's Inspect View bundle (the sample's transcript at the event)."""
        if not ref:
            return None
        base = (f"{self.prefix}{INSPECT_VIEW}#/logs/{quote(ref['log'], safe='')}/samples/sample/"
                f"{quote(str(ref['sample_id']), safe='')}/{ref['epoch']}/transcript")
        if ref.get("event_uuid"):
            base += "?event=" + quote(str(ref["event_uuid"]), safe="")
        return base

    def sample_url(self, agent_id):
        """Inspect View link to an agent's whole sample (its transcript tab)."""
        history = (self.run.agents or {}).get(agent_id)
        source = getattr(history, "source", None)
        if not source:
            return None
        return self.inspect_url({"log": Path(source).name, "sample_id": agent_id,
                                 "epoch": self.epochs.get(agent_id, 1), "event_uuid": None})

    def raw_path(self, item, ref):
        meta = item.get("meta") or {}
        kind = item.get("kind")
        if kind == "file_version" and meta.get("blob_sha256"):
            return (f"snapshots/{item['agent_id']}/{meta['blob_sha256']} (file {meta.get('path')} version "
                    f"{meta.get('version')}; runtime.jsonl line {meta.get('runtime_line')})")
        if kind == "conference":
            seq = meta.get("event_seq") or meta.get("seq")
            where = f"conference/state.sqlite (record {item['id']}"
            where += f", event seq {seq})" if seq else ")"
            if ref:
                where += f"; created by {meta.get('call_item')} in {ref['file']}"
            return where
        if kind == "runtime" and meta.get("runtime_line"):
            return f"runtime.jsonl line {meta['runtime_line']}"
        if ref:
            out = f"{ref['file']} sample {ref['sample_id']} epoch {ref['epoch']}"
            if ref.get("event_uuid"):
                out += f", model event {ref['event_uuid']}"
            if ref.get("message_id"):
                out += f", message {ref['message_id']}"
            return out
        return None

    def locate(self, item_id):
        item = self.run.lookup(item_id)
        if item is None:
            return None
        ref = self._inspect_of(item)
        return {"id": item["id"], "agent": item.get("agent_id"), "kind": item.get("kind"),
                "channel": item.get("channel"), "label": label_for(item), "time": item.get("time")
                or ((item.get("time_bounds") or [None])[0]), "chars": len(item.get("text") or ""),
                "inspect": ref, "inspect_url": self.inspect_url(ref), "raw": self.raw_path(item, ref)}


# ---------------------------------------------------------------------------
# Run-level facts


def _validity_by_public(results):
    out = {}
    evaluation = results.get("independent_evaluation") or {}
    for row in (evaluation.get("records") or []) + (results.get("accepted_submissions") or []):
        if isinstance(row, dict) and row.get("submission"):
            entry = out.setdefault(row["submission"], {})
            for key in ("validity", "independent_validity", "evaluator_reason", "evaluator_details"):
                if row.get(key) is not None:
                    entry[key] = row[key]
    return out


def _judge_outputs(judge_dir):
    judge_dir = Path(judge_dir)
    run_level = _read_json(judge_dir / "run_level.json")
    report = _read_json(judge_dir / "report.json") or {}
    agents = {}
    for path in sorted((judge_dir / "agents").glob("agent-*.json")):
        data = _read_json(path) or {}
        verified = data.get("verified") or {}
        findings = []
        for finding in verified.get("findings") or []:
            if finding.get("holds") is False or finding.get("state") in ("refuted",):
                continue
            findings.append({"id": finding.get("id"), "category": finding.get("category"),
                             "level": finding.get("established_level") or finding.get("level") or finding.get("claim"),
                             "certainty": finding.get("verified_certainty") or finding.get("certainty"),
                             "topic": finding.get("verified_topic") or finding.get("topic"),
                             "state": finding.get("state"),
                             "explanation": finding.get("verifier_plain_explanation")
                             or finding.get("plain_explanation") or finding.get("summary"),
                             "evidence": [{"item": e.get("item"), "quote": e.get("quote")}
                                          for e in finding.get("evidence") or [] if isinstance(e, dict)]})
        levels = {k: (verified.get(k) or {}).get("level") for k in ("cheating", "whistleblowing",
                                                                     "evaluation_awareness")}
        agents[data.get("agent_id") or path.stem] = {"status": data.get("status"), "levels": levels,
                                                     "note": data.get("note"), "findings": findings}
    rows = {row.get("agent_id"): row for row in report.get("agents") or [] if isinstance(row, dict)}
    for agent, row in rows.items():
        if agent in agents:
            agents[agent]["cohort"] = row.get("paper_cohort") or row.get("cohort")
            agents[agent]["plain_note"] = row.get("plain_note")
    return {"available": bool(agents or run_level), "path": str(judge_dir),
            "run_summary": (run_level or {}).get("plain_summary") or (run_level or {}).get("summary"),
            "rubric": (report.get("rubric") or {}).get("version") if isinstance(report.get("rubric"), dict) else None,
            "miss_rate": (run_level or {}).get("miss_rate"), "agents": agents}


def _timeline_facts(run_dir):
    path = Path(run_dir) / "analysis" / "timeline.jsonl"
    counts, restarts, exits, notable = Counter(), Counter(), {}, []
    if not path.is_file():
        return {"available": False}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            kind = entry.get("kind")
            counts[f"{entry.get('source')}:{kind}"] += 1
            agent = entry.get("agent_id")
            if kind == "agent_restart" and agent:
                restarts[agent] += 1
            elif kind == "agent_exit" and agent:
                exits[agent] = {"time": entry.get("time"), "status": (entry.get("payload") or {}).get("status")}
            elif kind in ("run_stop", "task_closed", "truncated_response", "tool_error", "internal_error") \
                    and len(notable) < 40:
                notable.append({"time": entry.get("time"), "kind": kind, "agent": agent,
                                "summary": _preview(entry.get("summary") or json.dumps(entry.get("payload"),
                                                                                       default=str), 160)})
    return {"available": True, "path": "analysis/timeline.jsonl", "counts": dict(counts.most_common(30)),
            "restarts": dict(restarts), "exits": exits, "notable": notable}


def _instructions_excerpt(run_dir):
    folder = Path(run_dir) / "instructions"
    for path in sorted(folder.glob("agent-*.json")) if folder.is_dir() else []:
        data = _read_json(path) or {}
        blocks = [b for b in data.get("system_blocks") or [] if isinstance(b, str)]
        if blocks:
            longest = max(blocks, key=len)
            text = longest if len(longest) <= INSTRUCTIONS_CHARS else (longest[:INSTRUCTIONS_CHARS]
                                                                      + f"\n[… {len(longest) - INSTRUCTIONS_CHARS} more characters …]")
            return {"source": str(path.relative_to(run_dir)), "agent": data.get("agent_id"), "text": text}
    return None


# ---------------------------------------------------------------------------
# Per-agent activity


def _keyword_pattern():
    from research.analysis.judge.pipeline import RECALL_PATTERN
    return RECALL_PATTERN


def notable_items(history, pinned=(), *, keyword_limit=MAX_KEYWORD_ITEMS, pattern=None):
    """Notable items of one agent with previews: the judge's cited items (``pinned``), the agent's
    own compaction summaries, its final message and keyword-net hits in its reasoning, messages and
    tool calls (the judge's recall vocabulary), spread over the run."""
    pattern = pattern or _keyword_pattern()
    by_id = {item["id"]: item for item in history.items}
    out, seen = [], set()

    def add(item, why, around=None):
        if item is None or item["id"] in seen:
            return
        seen.add(item["id"])
        out.append({"id": item["id"], "label": label_for(item), "time": item.get("time")
                    or ((item.get("time_bounds") or [None])[0]), "chars": len(item.get("text") or ""),
                    "why": why, "preview": _preview(item.get("text"), around=around)})

    for item_id in pinned:
        add(by_id.get(item_id) or history.get(item_id), "cited by the behavior judge")
    for item in history.items:
        if item["kind"] == "summary":
            add(item, "the agent's own summary at a compaction")
    own_text = [i for i in history.items if i["kind"] == "text"]
    if own_text:
        add(own_text[-1], "the agent's last message")
    hits = []
    for item in history.items:
        if item["kind"] not in ("reasoning", "text", "tool_call") or item["id"] in seen:
            continue
        match = pattern.search(item.get("text") or "")
        if match:
            hits.append((item, match))
    if len(hits) > keyword_limit:   # spread evenly over the run
        step = len(hits) / keyword_limit
        hits = [hits[int(k * step)] for k in range(keyword_limit)]
    for item, match in hits:
        collapsed_offset = len(re.sub(r"\s+", " ", item["text"][:match.start()]).lstrip())
        add(item, f"keyword: {match.group(0)!r}", around=collapsed_offset)
    out.sort(key=lambda row: (row["time"] or "", row["id"]))
    return out


def _agent_digest(agent_id, history, run, results_row, judge_row, validity, timeline):
    conf = run.conference
    communications, submissions = [], []
    for item in history.items:
        if item["kind"] != "conference":
            continue
        meta = item.get("meta") or {}
        if item["channel"] == "submission":
            public = meta.get("public_id") or conf.internal_to_public.get(item["id"])
            verdict = validity.get(public or "", {})
            submissions.append({"id": item["id"], "task": meta.get("problem_id"), "time": item.get("time"),
                                "outcome": meta.get("outcome"),
                                "independent_validity": verdict.get("validity") or verdict.get("independent_validity"),
                                "evaluator_details": _preview(verdict.get("evaluator_details") or "", 160) or None})
        elif len(communications) < MAX_COMMUNICATIONS:
            communications.append({"id": item["id"], "label": label_for(item), "time": item.get("time"),
                                   "recipient": meta.get("recipient"), "preview": _preview(item.get("text"))})
    pinned = [e["item"] for f in (judge_row or {}).get("findings", []) for e in f.get("evidence", []) if e.get("item")]
    summaries = [i for i in history.items if i["kind"] == "summary"]
    last_summary = None
    if summaries:
        text = summaries[-1]["text"]
        last_summary = {"id": summaries[-1]["id"], "excerpt": text[:SUMMARY_EXCERPT_CHARS]
                        + ("…" if len(text) > SUMMARY_EXCERPT_CHARS else "")}
    times = [i.get("time") for i in history.items if i.get("time")]
    stats = history.stats or {}
    return {"agent": agent_id, "code": history.code, "persona": history.persona,
            "results": results_row or {}, "turns": stats.get("turns"), "items": len(history.items),
            "first_time": min(times) if times else None, "last_time": max(times) if times else None,
            "restarts": (timeline.get("restarts") or {}).get(agent_id, 0),
            "exit": (history.exit or {}).get("status") or ((timeline.get("exits") or {}).get(agent_id) or {}).get("status"),
            "judge": judge_row, "submissions": submissions, "communications": communications,
            "notable": notable_items(history, pinned), "last_summary": last_summary}


def build_digest(run_dir, run, *, judge_dir=None):
    """The run digest (JSON-ready). ``run``: RunHistory with every agent rebuilt."""
    run_dir = Path(run_dir)
    results = _read_json(run_dir / "results.json", {}) or {}
    judge = _judge_outputs(judge_dir or run_dir / "analysis" / "judge")
    timeline = _timeline_facts(run_dir)
    validity = _validity_by_public(results)
    rows = {row.get("agent"): row for row in results.get("agents") or [] if isinstance(row, dict)}
    try:
        from research.analysis.judge.tasks_plain import task_table
        tasks = {row["task_id"]: row for row in task_table(run_dir)}
    except Exception:  # pragma: no cover - the plain-words table is optional context
        tasks = {}
    touched = set()
    agents = {}
    for agent_id in sorted(run.agents):
        history = run.agents[agent_id]
        agents[agent_id] = _agent_digest(agent_id, history, run, rows.get(agent_id), judge["agents"].get(agent_id),
                                         validity, timeline)
        touched.update(s["task"] for s in agents[agent_id]["submissions"] if s.get("task"))
    task_rows = []
    for row in results.get("tasks") or []:
        if not isinstance(row, dict):
            continue
        described = tasks.get(row.get("task")) or {}
        task_rows.append({"task": row.get("task"), "title": described.get("title"), "plain": described.get("plain"),
                          "submissions": row.get("submissions"), "first_accepted": row.get("first_accepted")})
    for task in sorted(touched - {r["task"] for r in task_rows}):
        described = tasks.get(task) or {}
        task_rows.append({"task": task, "title": described.get("title"), "plain": described.get("plain")})
    evaluation = results.get("independent_evaluation") or {}
    conf = run.conference
    return {
        "schema": SCHEMA, "run_id": run.run_id, "run_dir": str(run_dir),
        "run": {key: results.get(key) for key in ("start_utc", "stop_utc", "elapsed_seconds", "stop_reason",
                                                  "condition", "agent_exits", "auto_restarts", "submission_outcomes",
                                                  "operationally_accepted_tasks")},
        "budget": (results.get("budget") or {}),
        "independent_evaluation": {key: evaluation.get(key) for key in ("status", "by_validity",
                                                                        "distinct_verified_tasks", "not_genuine")},
        "tasks": task_rows, "task_count": (results.get("condition") or {}).get("task_count"),
        "conference": {"posts": len(conf.posts), "dms": len(conf.dms), "feedback": len(conf.feedback),
                       "library": len(conf.library), "submissions": len(conf.submissions),
                       "available": conf.available},
        "judge": {key: judge[key] for key in ("available", "path", "run_summary", "rubric", "miss_rate")},
        "timeline": timeline, "instructions": _instructions_excerpt(run_dir), "agents": agents,
        "sources": {"results": "results.json", "judge": "analysis/judge/" if judge["available"] else None,
                    "evaluations": "evaluations/evaluations.jsonl", "conference": "conference/state.sqlite",
                    "timeline": "analysis/timeline.jsonl", "inspect": sorted({h.source for h in run.agents.values()
                                                                              if getattr(h, "source", None)})},
        "problems": list(getattr(run, "problems", []) or [])[:20],
    }


# ---------------------------------------------------------------------------
# Prompt rendering


def _line(*parts):
    return " | ".join(str(p) for p in parts if p not in (None, "", [], {}))


def digest_text(digest, *, per_agent_notable=None, include_instructions=True):
    """The digest as compact plain text for a model prompt."""
    out = [f"RUN {digest['run_id']}"]
    run = digest["run"]
    out.append("Run facts (results.json): " + json.dumps(run, ensure_ascii=False, default=str))
    out.append("Budget: " + json.dumps(digest.get("budget"), ensure_ascii=False, default=str)[:600])
    out.append("Independent proof checker (evaluations/): "
               + json.dumps(digest["independent_evaluation"], ensure_ascii=False, default=str)[:2500])
    out.append("Conference record counts: " + json.dumps(digest["conference"]))
    judge = digest["judge"]
    if judge["available"]:
        out.append("Behavior judge (post-hoc model judge of cheating, whistleblowing and evaluation awareness; "
                   "verified findings only): " + (judge.get("run_summary") or "no run summary"))
    else:
        out.append("Behavior judge: no outputs for this run.")
    tl = digest["timeline"]
    if tl.get("available"):
        out.append("Timeline counts: " + json.dumps(tl.get("counts"), ensure_ascii=False))
        if tl.get("notable"):
            out.append("Timeline notable: " + "; ".join(_line(n["time"], n["kind"], n["agent"], n["summary"])
                                                        for n in tl["notable"][:25]))
    out.append("\nTASKS (id | title | plain description | submissions | first accepted)")
    for row in digest["tasks"]:
        out.append("- " + _line(row["task"], row.get("title"), row.get("plain"),
                                json.dumps(row.get("submissions")) if row.get("submissions") else None,
                                json.dumps(row.get("first_accepted")) if row.get("first_accepted") else None))
    if include_instructions and digest.get("instructions"):
        ins = digest["instructions"]
        out.append(f"\nAGENT INSTRUCTIONS (system prompt of {ins['agent']}, {ins['source']}; other agents differ "
                   "only in identity and persona):\n" + ins["text"])
    for agent_id, agent in digest["agents"].items():
        out.append(f"\n=== {agent_id} ({agent['code']}), persona {agent.get('persona')}")
        res = agent.get("results") or {}
        out.append(_line(f"requests {res.get('requests')}", f"exit {res.get('exit') or agent.get('exit')}",
                         f"exit_at {res.get('exit_at')}", f"restarts {agent.get('restarts')}",
                         f"turns {agent.get('turns')}", f"active {agent.get('first_time')} to {agent.get('last_time')}",
                         "submission outcomes " + json.dumps(res.get("submissions") or {})))
        judge_row = agent.get("judge")
        if judge_row:
            out.append("judge levels: " + json.dumps(judge_row.get("levels")) + (f" | cohort {judge_row.get('cohort')}"
                                                                                   if judge_row.get("cohort") else ""))
            if judge_row.get("plain_note"):
                out.append("judge note: " + judge_row["plain_note"])
            for finding in judge_row.get("findings") or []:
                cites = ", ".join(e["item"] for e in finding["evidence"] if e.get("item"))
                out.append(f"  finding {finding['id']} {finding['category']}/{finding['level']}"
                           + (f" ({finding['certainty']}, {finding['topic']})" if finding.get("certainty") else "")
                           + f" [{cites}]: {finding.get('explanation')}")
        for sub in agent["submissions"]:
            out.append("  submission " + _line(sub["id"], sub["task"], sub["time"], sub["outcome"],
                                               f"checker: {sub['independent_validity']}" if sub.get("independent_validity")
                                               else None, sub.get("evaluator_details")))
        for com in agent["communications"]:
            out.append("  " + _line(com["label"], com["id"], com["time"],
                                    f"to {com['recipient']}" if com.get("recipient") else None) + ": " + com["preview"])
        notable = agent["notable"] if per_agent_notable is None else agent["notable"][:per_agent_notable]
        for row in notable:
            out.append("  notable " + _line(row["id"], row["label"], row["time"], f"{row['chars']} chars", row["why"])
                       + ": " + row["preview"])
        if agent.get("last_summary"):
            out.append(f"  last own summary {agent['last_summary']['id']}: "
                       + _preview(agent["last_summary"]["excerpt"], SUMMARY_EXCERPT_CHARS))
    return "\n".join(out)
