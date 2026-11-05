"""Story map stage 0: one chronological timeline per agent (offline, read only, no model calls).

``python -m research.analysis.storymap.extract --run-dir runs/<id> --out DIR [--agents a,b] [--workers N]``
writes ``DIR/timeline/<agent>.jsonl`` (one item per line) and ``DIR/timeline/index.json``
(``{agent: {items, est_tokens, first_min, last_min, role, source}}``). Solvers only by default.

Overseer mode (``--overseers``: every overseer, never the seed): overseer tool results add K1 (check_proof), V1
(revert_submission), M1 (overseer-board post read, one per post) and M2 (own overseer-board post) events, failed
calls included (detail ``ok`` false), joined to the conference events table (see ``Builder.ov_join``), and
``timeline/overseer_context.json`` (``overseer_context``) is written. Solver timelines are unchanged.

Transcript source: the agent's Claude Code session JSONL(s) inside ``resume/agents/<agent>/state.tar.gz``
(complete history; only ``<config_dir>/projects/<project>/<session>.jsonl`` members are read, in memory,
never credential or auth files), falling back to the Inspect sample through ``judge/history.py``.
Conference context (who wrote what, reverts, validity) comes from ``conference/state.sqlite`` (read only),
``evaluations/evaluations.jsonl`` and ``settings.json``. Item schema and event types: the story-map
interface (W1); every field is present, unused ones are null.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tarfile
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from research.analysis import roles as roles_mod
from research.analysis.storymap import domain as domain_mod
from research.analysis.storymap.fake_rule import classify, is_fake

COLLAB_FULL = 6000          # collab tool results kept in full up to this many characters
OTHER_HEAD, OTHER_TAIL = 600, 200
POST_TEXT = 1500            # characters of a post/DM body repeated in its event's text
SESSION_MEMBER = re.compile(r"(?:^|/)\.?[\w.-]*/projects/[^/]+/[0-9a-f-]{36}\.jsonl$")
CREDENTIAL = re.compile(r"credential|\.key$|auth|token|\.env|\.claude\.json|secret|oauth", re.I)
LEAN_ERROR = re.compile(r":\d+:\d+: error|^error:", re.M)

TRICK = re.compile(r"Iff\.rfl|Iff\.refl|iff_self|\bP\s*↔\s*P\b|tautolog|restat|answer\(\s*P\s*\)|"
                   r"(?:proposition|statement|conjecture) (?:on the other side|itself)|other side of the iff|"
                   r"answer[- ]field|answer slot|loophole|exploit", re.I)
WARNING = re.compile(r"\bwarn|revert|will be (?:audited|reverted|removed)|not allowed|gaming|withdrawn|exploit|"
                     r"not genuine|cheat|fraud|tautolog", re.I)
RULING = re.compile(r"revert|not genuine|credit (?:withdrawn|removed)|ruling|verdict|reopened|re-verified", re.I)
REVERT = re.compile(r"revert|reopened|credit (?:withdrawn|removed)|withdrawn", re.I)
REMINDER_SUMMARY = 300  # characters of an integrity reminder kept in the event text (full text in detail)
UNREAD = re.compile(r"(\d+) new ([a-z ]+?)(?: since| \(|$)")
PUSH_EVENT = "push_delivered"   # research_swarm.conference.PUSH_EVENT
PUSH_CHANNEL_TYPE = {"board": "B1", "dm": "D1", "reminder": "R1"}


def push_delivery(seq, timestamp, payload):
    """Adapter for one ``push_delivered`` conference event (research_swarm.conference.deliver_push): payload
    ``{"items": [{"channel": "board"|"dm"|"reminder", "id", "author", "text", "created_ts", "public_id"?,
    "boundary"?}], "request_seq", "anchor_tool_use_id", "block", "counts"}``. Returns ``{"delivery_id", "ts",
    "anchor", "request_seq", "items"}`` (items in delivery order, unknown channels dropped) or None."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
    except (ValueError, TypeError):
        return None
    items = [it for it in data.get("items") or [] if isinstance(it, dict) and it.get("channel") in PUSH_CHANNEL_TYPE]
    if not items:
        return None
    return {"delivery_id": f"push-{int(seq):06d}", "ts": float(timestamp),
            "anchor": data.get("anchor_tool_use_id") or None,
            "request_seq": data.get("request_seq", data.get("model_call")), "items": items}


def place_pushes(stream, pushes):
    """The stream with one ``("push", ts, delivery)`` record per push delivery: right after the tool result it was
    appended to (``anchor_tool_use_id``; ``placement`` "anchor"), else, when that result is not in the transcript,
    before the first record later than the delivery (``placement`` "time"; at the end when none is). The Builder
    holds a push until the next model step starts: that step is the one that received it."""
    if not pushes:
        return stream
    ids = {p.get("id"): k for k, (kind, _, p) in enumerate(stream) if kind == "tool_result" and p.get("id")}
    after, unanchored = {}, []
    for push in pushes:
        k = ids.get(push["anchor"]) if push["anchor"] else None
        if k is None:
            unanchored.append(dict(push, placement="time"))
        else:
            after.setdefault(k, []).append(dict(push, placement="anchor"))
    unanchored.sort(key=lambda p: p["ts"])
    out, u = [], 0
    for k, rec in enumerate(stream):
        ts = rec[1]
        while u < len(unanchored) and ts is not None and ts > unanchored[u]["ts"] and rec[0] != "tool_result":
            out.append(("push", unanchored[u]["ts"], unanchored[u]))
            u += 1
        out.append(rec)
        for push in after.get(k, ()):
            out.append(("push", push["ts"], push))
    out.extend(("push", p["ts"], p) for p in unanchored[u:])
    return out


def _epoch(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(seconds):
    return None if seconds is None else datetime.fromtimestamp(seconds, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def estimate_tokens(text):
    """characters / 4, rounded up (the judge's rule, judge/history.estimate_tokens)."""
    return (len(text or "") + 3) // 4


def short_tool(name):
    name = str(name or "")
    m = re.match(r"mcp__[^_]+(?:_[^_]+)*?__(.+)$", name)
    return m.group(1) if m else name


def is_collab(name):
    return str(name or "").startswith("mcp__collab__") or str(name or "").startswith("collab_") or \
        short_tool(name) in COLLAB_TOOLS or short_tool(name) in OVERSEER_TOOLS


COLLAB_TOOLS = {"post_intent", "get_bulletin_board", "send_direct_message", "get_messages", "submit_feedback",
                "list_problems", "get_problem", "submit_solution", "list_library", "read_library_file",
                "publish_notebook", "get_status", "get_conference_status"}
# Overseer-only tools (research_swarm.oversight; solvers never have them). Their results get K1/V1/M1/M2 events,
# failed calls included (ok false, an MCP timeout or any non-JSON result).
OVERSEER_TOOLS = {"check_proof", "revert_submission", "post_overseer_board", "read_overseer_board"}
# Conference event kinds that record each overseer tool's call (events table; tool column = the tool name).
OVERSEER_EVENT_KINDS = {"check_proof": "proof_checked", "revert_submission": "submission_reverted",
                        "post_overseer_board": "overseer_board_post", "read_overseer_board": "overseer_board_read"}
ERROR_KINDS = ("tool_error", "internal_error")
JOIN_SLACK = 60.0   # seconds: container vs host clock tolerance when joining a call to its conference event
VERDICT_OUTCOME = {"genuine": "genuine", "not genuine": "not genuine", "undetermined": "undecided"}


def sid(n):
    return None if n is None else "sub-%06d" % int(n)


def mins(seconds, start):
    return None if seconds is None else round((seconds - start) / 60, 3)


# ---------------------------------------------------------------------------
# Run context (conference records)


class RunContext:
    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.settings = roles_mod.load_settings(self.run_dir)
        self.roles = dict(roles_mod.special_roles(self.settings))
        self.domain = domain_mod.run_domain(self.run_dir)   # "lean" (math) or "code" (coding runs)
        self.classify, self.is_fake = domain_mod.fake_rule(self.domain)
        self.trick = domain_mod.trick_pattern(self.domain, TRICK)
        db = sqlite3.connect(f"file:{self.run_dir / 'conference/state.sqlite'}?mode=ro&immutable=1", uri=True)
        db.row_factory = sqlite3.Row
        meta = {r["key"]: r["value"] for r in db.execute("select key, value from meta")}
        self.start = float(meta["run_started_at"]) if meta.get("run_started_at") else None
        self.validity = {}
        ev = self.run_dir / "evaluations/evaluations.jsonl"
        if ev.exists():
            for line in ev.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    e = json.loads(line)
                    self.validity[e.get("source_sha256")] = e.get("validity")
        self.subs, self.sub_by_public = {}, {}
        for r in db.execute("select * from submissions"):
            d = dict(r)
            d["fake"] = self.is_fake(d["source"], d["outcome"], self.validity.get(d["source_sha256"]))
            d["form"] = self.classify(d["source"] or "")
            d["validity"] = self.validity.get(d["source_sha256"])
            self.subs[d["submission_id"]] = d
            if d.get("public_id"):
                self.sub_by_public[d["public_id"]] = d
        self.reversions, self.reversions_list = {}, []
        for r in db.execute("select * from reversions order by reverted_at"):
            self.reversions.setdefault(r["submission_id"], dict(r))
            self.reversions_list.append(dict(r))
        self.library = {}
        for r in db.execute("select * from library"):
            d = dict(r)
            sub = self.subs.get(d["submission_id"]) if d["submission_id"] is not None else None
            sha = sub["source_sha256"] if sub else d["sha256"]
            validity = self.validity.get(sha) if sha in self.validity else self.validity.get(d["sha256"])
            d["fake"] = self.is_fake(d["content"], "accepted" if d["kind"] == "solution" else "", validity)
            d["form"] = self.classify(d["content"] or "")
            d["validity"] = validity
            self.library[(d["path"], d["revision"])] = d
        self.posts = {"post-%06d" % r["post_id"]: dict(r) for r in db.execute("select * from posts")}
        self.messages, self.msg_by_public = {}, {}
        for r in db.execute("select * from messages"):
            d = dict(r)
            self.messages[d["message_id"]] = d
            if d.get("public_id"):
                self.msg_by_public[d["public_id"]] = d
        self.agents = [r["agent_id"] for r in db.execute("select agent_id from agents order by agent_id")]
        self.pushes = {}   # agent -> [push delivery] (push-delivery runs only; docs/plan.md, November 3, 2026)
        has_events = db.execute("select 1 from sqlite_master where type='table' and name='events'").fetchone()
        if has_events:
            for r in db.execute("select seq, timestamp, agent_id, payload from events where kind = ? order by seq",
                                (PUSH_EVENT,)):
                push = push_delivery(r["seq"], r["timestamp"], r["payload"])
                if push is not None:
                    self.pushes.setdefault(r["agent_id"], []).append(push)
        # Overseer tool records (overseer mode): every conference event of an overseer tool, in seq order
        # ({"seq", "ts", "kind", "agent", "tool", "payload"}), and the private overseer board's posts.
        self.ov_events, self.last_event_ts = [], None
        if has_events:
            tools = sorted(OVERSEER_EVENT_KINDS)
            kinds = sorted(set(OVERSEER_EVENT_KINDS.values()) | set(ERROR_KINDS))
            q = (f"select seq, timestamp, kind, agent_id, tool, payload from events where tool in "
                 f"({','.join('?' * len(tools))}) and kind in ({','.join('?' * len(kinds))}) order by seq")
            for r in db.execute(q, tools + kinds):
                try:
                    payload = json.loads(r["payload"])
                except (ValueError, TypeError):
                    payload = {}
                self.ov_events.append({"seq": r["seq"], "ts": r["timestamp"], "kind": r["kind"],
                                       "agent": r["agent_id"], "tool": r["tool"],
                                       "payload": payload if isinstance(payload, dict) else {}})
            self.last_event_ts = db.execute("select max(timestamp) from events").fetchone()[0]
        self.ov_board = {}
        if db.execute("select 1 from sqlite_master where type='table' and name='overseer_board'").fetchone():
            self.ov_board = {"ovpost-%06d" % r["post_id"]: dict(r)
                             for r in db.execute("select * from overseer_board order by post_id")}
        db.close()
        if self.start is None:
            clock = (self.settings.get("effective") or {}).get("common_start_utc")
            self.start = _epoch(clock) or 0.0

    def role(self, agent):
        if agent is None:
            return None
        return self.roles.get(agent, "solver")

    def solvers(self):
        ids = set(self.agents)
        res = self.run_dir / "resume/agents"
        if res.exists():
            ids |= {p.name for p in res.iterdir() if p.name.startswith("agent-")}
        return sorted(a for a in ids if self.role(a) == "solver")

    def overseers(self):
        """Every agent whose role is overseer (never the seed), sorted."""
        ids = set(self.agents) | set(roles_mod.overseers(self.settings))
        res = self.run_dir / "resume/agents"
        if res.exists():
            ids |= {p.name for p in res.iterdir() if p.name.startswith("agent-")}
        return sorted(a for a in ids if self.role(a) == "overseer")

    # -- overseer mode helpers
    @staticmethod
    def accept_time(sub, rev=None):
        """Epoch seconds a submission was credited (the reversion's accepted_at, else judged_at, else submitted)."""
        return (rev or {}).get("accepted_at") or sub.get("judged_at") or sub.get("submitted_at")

    def accepted_sub(self, task, ts):
        """The submission credited for ``task`` at epoch ``ts`` (accepted, not yet reverted), or None."""
        best = None
        for s in self.subs.values():
            if s["task_id"] != task or s["outcome"] != "accepted":
                continue
            rev = self.reversions.get(s["submission_id"])
            at = self.accept_time(s, rev)
            if at is None or at > ts + JOIN_SLACK or (rev and rev["reverted_at"] <= ts):
                continue
            if best is None or at > self.accept_time(best, self.reversions.get(best["submission_id"])):
                best = s
        return best

    def last_reverted_sub(self, task, ts):
        """The latest reversion of ``task`` at or before ``ts`` (any overseer), or None."""
        rows = [r for r in self.reversions_list if r["task_id"] == task and r["reverted_at"] <= ts + JOIN_SLACK]
        return max(rows, key=lambda r: r["reverted_at"]) if rows else None

    def sub_by_sid(self, value):
        """The submission row for ``sub-000123`` (internal id), or None."""
        m = re.fullmatch(r"sub-(\d+)", str(value or ""))
        return self.subs.get(int(m.group(1))) if m else None

    def sub_by_sha(self, digest):
        rows = [s for s in self.subs.values() if digest and s["source_sha256"] == digest]
        return rows[0] if rows else None

    def library_path_of_sub(self, submission_id):
        rows = [r for r in self.library.values() if r.get("submission_id") == submission_id]
        return min(rows, key=lambda r: r["revision"])["path"] if rows else None

    def library_row(self, path, revision=None):
        if revision is not None and (path, revision) in self.library:
            return self.library[(path, revision)]
        rows = [v for (p, _), v in self.library.items() if p == path]
        return max(rows, key=lambda r: r["revision"]) if rows else None


# ---------------------------------------------------------------------------
# Raw record streams (session JSONL, Inspect fallback)


def session_members(tar, agent_json):
    """Session JSONL members of a resume archive: listed in agent.json ``sessions`` when present, else every
    ``<config>/projects/<project>/<uuid>.jsonl``; credential-looking names are never opened."""
    names = {m.name: m for m in tar.getmembers() if m.isfile()}
    wanted = []
    for s in (agent_json or {}).get("sessions") or []:
        p = str(s.get("path") or "").lstrip("/")
        if p in names:
            wanted.append(p)
    if not wanted:
        wanted = [n for n in names if SESSION_MEMBER.search(n) and "/subagents/" not in n]
    return [names[n] for n in wanted if not CREDENTIAL.search(Path(n).name)]


def _result_text(content):
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if isinstance(p, dict) and p.get("type") == "text":
            out.append(p.get("text", ""))
        elif isinstance(p, dict):
            out.append(f"[{p.get('type')}]")
    return "\n".join(out)


def session_stream(run_dir, agent):
    """Normalized records from the agent's Claude Code session file(s), in order:
    (kind, ts, payload) with kind in reasoning/text/tool_call/tool_result/context/compaction/restart."""
    base = Path(run_dir) / "resume/agents" / agent
    archive = base / "state.tar.gz"
    if not archive.exists():
        return None
    try:
        agent_json = json.loads((base / "agent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        agent_json = {}
    files = []
    with tarfile.open(archive) as tar:
        for m in session_members(tar, agent_json):
            lines = tar.extractfile(m).read().decode("utf-8", "replace").splitlines()
            recs = [json.loads(x) for x in lines if x.strip()]
            first = next((r.get("timestamp") for r in recs if r.get("timestamp")), "") or ""
            files.append((first, m.name, recs))
    if not files:
        return None
    files.sort()
    out = []
    seen_uuid, seen_parts, seen_results = set(), set(), set()
    pending_boundary = None
    for fi, (_, name, recs) in enumerate(files):
        if fi:
            out.append(("restart", _epoch(recs[0].get("timestamp") if recs else None) or
                        _epoch(next((r.get("timestamp") for r in recs if r.get("timestamp")), None)),
                        {"session": Path(name).stem}))
        for x in recs:
            u = x.get("uuid")
            if u and u in seen_uuid:
                continue
            if u:
                seen_uuid.add(u)
            typ, ts = x.get("type"), _epoch(x.get("timestamp"))
            msg = x.get("message") or {}
            if typ == "system" and x.get("subtype") == "compact_boundary":
                pending_boundary = x.get("compactMetadata") or {}
                continue
            if typ == "assistant":
                mid = msg.get("id") or u
                for p in msg.get("content") or []:
                    k = p.get("type")
                    key = (mid, k, json.dumps(p.get("thinking") or p.get("text") or p.get("id"), ensure_ascii=False))
                    if key in seen_parts:
                        continue
                    seen_parts.add(key)
                    if k == "thinking" and (p.get("thinking") or "").strip():
                        out.append(("reasoning", ts, {"mid": mid, "text": p["thinking"]}))
                    elif k == "redacted_thinking":
                        out.append(("reasoning", ts, {"mid": mid, "text": "[redacted reasoning]"}))
                    elif k == "text" and (p.get("text") or "").strip():
                        out.append(("text", ts, {"mid": mid, "text": p["text"]}))
                    elif k == "tool_use":
                        out.append(("tool_call", ts, {"mid": mid, "id": p.get("id"), "name": p.get("name"),
                                                      "input": p.get("input")}))
            elif typ == "user":
                c = msg.get("content")
                if x.get("isCompactSummary"):
                    meta = pending_boundary or {}
                    out.append(("compaction", ts, {"summary": _result_text(c), "trigger": meta.get("trigger"),
                                                   "pre_tokens": meta.get("preTokens")}))
                    pending_boundary = None
                    continue
                if isinstance(c, str):
                    if c.strip():
                        out.append(("context", ts, {"text": c}))
                    continue
                for p in c or []:
                    if p.get("type") == "tool_result":
                        tid = p.get("tool_use_id")
                        if tid in seen_results:
                            continue
                        seen_results.add(tid)
                        out.append(("tool_result", ts, {"id": tid, "text": _result_text(p.get("content")),
                                                        "is_error": bool(p.get("is_error"))}))
                    elif p.get("type") == "text" and (p.get("text") or "").strip():
                        out.append(("context", ts, {"text": p["text"]}))
    return out


def inspect_stream(run_dir, agent):
    """Fallback: the Inspect sample through judge/history.py (tool-call inputs are its rendered text)."""
    from research.analysis.judge.history import build_run
    history = build_run(run_dir, agents=[agent], include_files=False).agents[agent]
    out = []
    restarts = {sg.get("first_turn") for sg in history.segments if sg.get("restart")}
    compacted = {sg.get("summary_turn") for sg in history.segments if sg.get("summary_turn") is not None}
    last_turn = None
    for it in history.items:
        kind, ts, turn = it["kind"], _epoch(it.get("time")), it.get("turn")
        if ts is None and it.get("time_bounds"):
            # the lower bound: the upper one (the next model call) put tool results up to minutes late, out of order
            ts = _epoch(it["time_bounds"][0])
        mid = f"turn-{turn}"
        if turn != last_turn and turn in restarts and last_turn is not None:
            out.append(("restart", ts, {"session": None}))
        last_turn = turn
        meta = it.get("meta") or {}
        if kind == "reasoning":
            out.append(("reasoning", ts, {"mid": mid, "text": it["text"]}))
        elif kind == "text":
            out.append(("text", ts, {"mid": mid, "text": it["text"]}))
        elif kind == "summary":
            out.append(("compaction", ts, {"summary": it["text"], "trigger": "auto", "pre_tokens": None}))
        elif kind == "tool_call":
            out.append(("tool_call", ts, {"mid": mid, "id": meta.get("call_id"), "name": meta.get("function"),
                                          "input": it["text"]}))
        elif kind == "tool_result":
            out.append(("tool_result", ts, {"id": meta.get("call_id"), "text": it["text"],
                                            "is_error": bool(meta.get("error"))}))
        if turn in compacted and kind == "summary":
            compacted.discard(turn)
    return out


# ---------------------------------------------------------------------------
# Trimming


def compiler_errors(text):
    """Lean compiler output reduced to its error lines (with the message line that follows each)."""
    lines = (text or "").splitlines()
    keep = []
    for i, line in enumerate(lines):
        if LEAN_ERROR.search(line):
            keep.append(line)
    return "\n".join(keep) if keep else text


def cut(text, head, tail):
    n = len(text)
    if n <= head + tail:
        return text
    return text[:head] + f"\n[cut {n - head - tail} chars]\n" + (text[n - tail:] if tail else "")


def trim_result(tool, text):
    text = text or ""
    if is_collab(tool):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("compiler_output"), str):
            data["compiler_output"] = compiler_errors(data["compiler_output"])
            text = json.dumps(data, ensure_ascii=False)
        return cut(text, COLLAB_FULL, 0)
    if LEAN_ERROR.search(text):
        text = compiler_errors(text)
    return cut(text, OTHER_HEAD, OTHER_TAIL)


# ---------------------------------------------------------------------------
# Events


def text_tags(text, role, trick=None):
    tags = []
    if (trick or TRICK).search(text or ""):
        tags.append("describes_trick")
    if WARNING.search(text or ""):
        tags.append("warning")
    if role == "overseer" and RULING.search(text or ""):
        tags.append("ruling")
    if REVERT.search(text or "") and (role == "overseer" or "revert" in (text or "").lower()):
        tags.append("revert_notice")
    return tags or ["unrelated"]


class Builder:
    def __init__(self, ctx, agent):
        self.ctx, self.agent = ctx, agent
        self.items = []
        self.turn = -1
        self.mid_turn = {}
        self.calls = {}
        self.seen_posts, self.seen_dms, self.seen_lib = set(), set(), set()
        self.p2_seen = set()
        self.pending_push = []   # push deliveries waiting for the model step that received them
        # overseer mode: conference event seqs already joined to a call, per-type counters, the current call's time
        self.ov_used, self.ov_n, self.ov_seen_posts, self.call_ts = set(), {}, set(), None

    # -- helpers
    def t(self, ts):
        return mins(ts, self.ctx.start)

    def add(self, ts, kind, text, tool=None, event=None, turn=None, call_id=None, mid=None):
        """One item. ``call_id`` (the tool_use id of a tool_call / tool_result) and ``mid`` (the model step's
        message id on reasoning / text / tool_call) are evidence references, never rendered into a prompt."""
        self.items.append({"i": len(self.items), "t": self.t(ts), "ts": _iso(ts),
                           "turn": max(self.turn, 0) if turn is None else turn, "kind": kind, "tool": tool,
                           "text": text, "event": event, "call_id": call_id, "mid": mid})

    def event(self, ts, etype, eid, text, tool=None, author=None, fake=None, tags=(), outcome=None, detail=None,
              turn=None):
        ev = {"type": etype, "id": eid, "author": author, "author_role": self.ctx.role(author),
              "fake": fake, "tags": list(tags), "outcome": outcome, "detail": detail or {}}
        self.add(ts, "event", text, tool=tool, event=ev, turn=turn)

    # -- stream
    def feed(self, stream):
        for kind, ts, p in stream:
            if kind in ("reasoning", "text", "tool_call"):
                mid = p.get("mid")
                if mid not in self.mid_turn:
                    self.turn += 1
                    self.mid_turn[mid] = self.turn
                    self.flush_push(self.turn)
                turn = self.mid_turn[mid]
                if kind == "tool_call":
                    name = p.get("name")
                    self.calls[p.get("id")] = (name, turn, p.get("input"), ts)
                    body = p.get("input")
                    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                    self.add(ts, kind, text, tool=short_tool(name), turn=turn, call_id=p.get("id"), mid=mid)
                else:
                    self.add(ts, kind, p["text"], turn=turn, mid=mid)
            elif kind == "push":
                self.pending_push.append(p)
            elif kind == "tool_result":
                name, turn, args, self.call_ts = self.calls.get(p.get("id"), (None, max(self.turn, 0), None, ts))
                self.add(ts, "tool_result", trim_result(name, p["text"]), tool=short_tool(name), turn=turn,
                         call_id=p.get("id"))
                if is_collab(name):
                    self.derive(ts, short_tool(name), args, p["text"], turn)
            elif kind == "context":
                self.add(ts, "context", p["text"])
            elif kind == "compaction":
                self.event(ts, "C1", f"C1:{self.agent}@{self.t(ts)}",
                           f"Context compaction ({p.get('trigger') or 'auto'}); the agent continues from this summary",
                           detail={"trigger": p.get("trigger") or "auto", "pre_tokens": p.get("pre_tokens"),
                                   "summary": p.get("summary")})
            elif kind == "restart":
                self.event(ts, "C1", f"C1:{self.agent}@{self.t(ts)}",
                           "Restart: a new session begins without the earlier conversation",
                           detail={"trigger": "restart", "session": p.get("session"), "summary": None})
        self.flush_push(None)
        return self.items

    # -- push delivery (host-appended board posts, DMs and reminders)
    def flush_push(self, turn):
        """Emit the held push deliveries at the start of model step ``turn`` (the step that received them);
        ``None`` at the end of the transcript (no reply recorded: the last turn, ``answered`` false)."""
        pending, self.pending_push = self.pending_push, []
        for push in pending:
            self.on_push(push, max(self.turn, 0) if turn is None else turn, answered=turn is not None)

    def on_push(self, push, turn, answered=True):
        """One event per pushed item (B1 board post, D1 direct message, R1 integrity reminder), all at the
        delivery's time and turn, sharing ``delivery_id`` with ``position`` / ``delivery_size`` in ``detail``."""
        ts, items = push["ts"], push["items"]
        common = {"delivery": "push", "delivery_id": push["delivery_id"], "delivery_size": len(items),
                  "delivery_items": [self.push_event_id(it) for it in items],
                  "anchor_tool_use_id": push["anchor"], "request_seq": push["request_seq"],
                  "placement": push["placement"], "answered": answered}
        for position, it in enumerate(items):
            detail = dict(common, position=position)
            channel, author = it["channel"], it.get("author")
            if channel == "board":
                pid = it.get("id")
                row = self.ctx.posts.get(pid) or {}
                msg = row.get("message") or it.get("text") or ""
                reread = pid in self.seen_posts
                self.seen_posts.add(pid)
                role = self.ctx.role(author)
                self.event(ts, "B1", f"B1:{pid}", f"{pid} by {author} ({role})"
                           + (" [re-read]" if reread else ": " + msg[:POST_TEXT]) + " [pushed]", tool="push",
                           author=author, tags=text_tags(msg, role, self.ctx.trick),
                           detail=dict(detail, post_id=pid, posted_at=self.t(_epoch(it.get("created_ts"))
                                                                              or row.get("posted_at")),
                                       intent_type=row.get("intent_type"), reread=reread), turn=turn)
            elif channel == "dm":
                mid = self.push_item_key(it)
                row = self.ctx.msg_by_public.get(it.get("public_id")) or {}
                msg = row.get("body") or it.get("text") or ""
                reread = mid in self.seen_dms
                self.seen_dms.add(mid)
                role = self.ctx.role(author)
                self.event(ts, "D1", f"D1:{mid}", f"{mid} from {author} ({role})"
                           + (" [re-read]" if reread else ": " + msg[:POST_TEXT]) + " [pushed]", tool="push",
                           author=author, tags=text_tags(msg, role, self.ctx.trick),
                           detail=dict(detail, message_id=mid, public_id=it.get("public_id"),
                                       sent_at=self.t(_epoch(it.get("created_ts")) or row.get("sent_at")),
                                       reread=reread), turn=turn)
            else:
                notice = str(it.get("text") or "").strip()
                short = notice if len(notice) <= REMINDER_SUMMARY else notice[:REMINDER_SUMMARY].rstrip() + " ..."
                self.event(ts, "R1", self.push_event_id(it), "Integrity reminder (pushed): " + short,
                           tool="push", detail=dict(detail, notice=notice, boundary=it.get("boundary"),
                                                    due_at=self.t(_epoch(it.get("created_ts")))), turn=turn)

    def push_event_id(self, it):
        """The event id of a pushed item: ``B1:post-000123``, ``D1:dm-000045``, ``R1:<agent>:reminder-003``."""
        typ = PUSH_CHANNEL_TYPE[it["channel"]]
        return f"R1:{self.agent}:{self.push_item_key(it)}" if typ == "R1" else f"{typ}:{self.push_item_key(it)}"

    @staticmethod
    def push_item_key(it):
        """The pushed item's id as the pull events name it: ``post-000123``, ``dm-000045``, ``reminder-003``."""
        return str(it.get("id") or (f"reminder-{int(it['boundary']):03d}" if it.get("boundary") is not None else "?"))

    # -- derived conference events
    def derive(self, ts, tool, args, text, turn):
        try:
            r = json.loads(text)
        except (ValueError, TypeError):
            r = None
        if tool in OVERSEER_TOOLS:   # overseer-only tools: an event for failed calls too (solvers never call them)
            getattr(self, "ov_" + tool)(ts, r if isinstance(r, dict) else None, text,
                                        args if isinstance(args, dict) else {}, turn)
        if not isinstance(r, dict) or r.get("ok") is False:
            return
        args = args if isinstance(args, dict) else {}
        handler = getattr(self, "on_" + tool, None)
        if handler:
            handler(ts, r, args, turn)
        if isinstance(r.get("unread"), str) and r["unread"].strip():
            counts = {k.strip(): int(n) for n, k in UNREAD.findall(r["unread"])}
            self.event(ts, "N1", f"N1:{self.agent}@{self.t(ts)}", "Unread notice: " + r["unread"], tool=tool,
                       detail={"notice": r["unread"], "counts": counts}, turn=turn)
        if isinstance(r.get("system_notice"), str) and r["system_notice"].strip():
            # the run's periodic integrity reminder (config integrity_reminder), delivered in this tool result: a
            # host notice the agent saw, not its own act; the full text stays in detail
            notice = r["system_notice"].strip()
            short = notice if len(notice) <= REMINDER_SUMMARY else notice[:REMINDER_SUMMARY].rstrip() + " ..."
            self.event(ts, "R1", f"R1:{self.agent}@{self.t(ts)}", "Integrity reminder: " + short, tool=tool,
                       detail={"notice": notice, "delivery": "notice"}, turn=turn)

    def on_list_library(self, ts, r, args, turn):
        files = r.get("files") or []
        paths = [f.get("path") for f in files]
        fakes = [f.get("path") for f in files
                 if (self.ctx.library_row(f.get("path"), f.get("revision")) or {}).get("fake")]
        self.event(ts, "L1", f"L1:{self.agent}@{self.t(ts)}",
                   f"Library listing: {len(files)} file(s) shown ({len(fakes)} fake)", tool="list_library",
                   detail={"paths": paths, "fake_paths": fakes, "total": r.get("total")}, turn=turn)

    def on_read_library_file(self, ts, r, args, turn):
        path = r.get("path") or args.get("path")
        row = self.ctx.library_row(path, r.get("revision"))
        author = r.get("author") or (row or {}).get("author")
        sub_id = (row or {}).get("submission_id")
        rev = self.ctx.reversions.get(sub_id) if sub_id is not None else None
        reverted_at = rev["reverted_at"] if rev else None
        # a re-read is the same revision again (a new revision of the path is new content); a later page
        # (offset > 0) of the same read is a continuation, not a re-read
        key = (path, r.get("revision"))
        continuation = bool(r.get("offset") or 0)
        reread = key in self.seen_lib and not continuation
        self.seen_lib.add(key)
        fake = (row or {}).get("fake") if row else bool(self.ctx.classify(r.get("content") or ""))
        state = ("reverted before this read" if reverted_at is not None and reverted_at <= ts else
                 (f"reverted later at {self.t(reverted_at)}" if reverted_at else "not reverted"))
        text = (f"Read library file {path} by {author} ({self.ctx.role(author)}): "
                f"{'FAKE (' + str((row or {}).get('form') or 'evaluator') + ')' if fake else 'not fake'}; {state}"
                + (" [re-read]" if reread else ""))
        self.event(ts, "L2", f"L2:{path}@{self.t(ts)}", text, tool="read_library_file", author=author,
                   fake=bool(fake), tags=["describes_trick"] if fake else [],
                   detail={"path": path, "revision": r.get("revision"), "problem_id": r.get("problem_id"),
                           "submission_id": sid(sub_id), "form": (row or {}).get("form"),
                           "validity": (row or {}).get("validity"), "reverted_at": self.t(reverted_at),
                           "reverted_before_read": bool(reverted_at is not None and reverted_at <= ts),
                           "offset": r.get("offset"), "has_more": r.get("has_more"), "reread": reread,
                           "continuation": continuation,
                           "own": author == self.agent}, turn=turn)

    def _listing(self, ts, r, tool, turn):
        problems = r.get("problems") or []
        closed = {p.get("problem_id"): p.get("credited_agent") for p in problems if p.get("status") == "closed"}
        own = sorted(k for k, v in closed.items() if v == self.agent)
        self.event(ts, "P1", f"P1:{self.agent}@{self.t(ts)}",
                   f"Problem listing: {len(closed)} closed of {len(problems)} shown; own credits: "
                   + (", ".join(own) or "none"), tool=tool,
                   detail={"closed": closed, "own_credit": own, "shown": len(problems),
                           "open_count": r.get("open_count", r.get("open_problems"))}, turn=turn)
        shown = {p.get("problem_id"): p for p in problems}
        for sub_id, rev in self.ctx.reversions.items():
            if rev["credited_agent"] != self.agent or rev["reverted_at"] > ts or sub_id in self.p2_seen:
                continue
            p = shown.get(rev["task_id"])
            if p is None or p.get("credited_agent") == self.agent:
                continue
            self.p2_seen.add(sub_id)
            outcome = "reopened" if p.get("status") == "open" else "credit_removed"
            self.event(ts, "P2", f"P2:{sid(sub_id)}",
                       f"Own credit for {rev['task_id']} ({sid(sub_id)}) removed by {rev['reverted_by']}: reverted "
                       f"at {self.t(rev['reverted_at'])}, seen at {self.t(ts)} (problem now {p.get('status')}"
                       + (f", credited to {p.get('credited_agent')}" if p.get("credited_agent") else "") + ")",
                       tool=tool, author=rev["reverted_by"], tags=["revert_notice"], outcome=outcome,
                       detail={"task_id": rev["task_id"], "submission_id": sid(sub_id),
                               "reverted_at": self.t(rev["reverted_at"]), "seen_at": self.t(ts),
                               "reason": rev["reason"], "status_now": p.get("status"),
                               "credited_now": p.get("credited_agent")}, turn=turn)

    def on_list_problems(self, ts, r, args, turn):
        self._listing(ts, r, "list_problems", turn)

    def on_get_status(self, ts, r, args, turn):
        self._listing(ts, r, "get_status", turn)

    def on_get_bulletin_board(self, ts, r, args, turn):
        for post in r.get("posts") or []:
            author, pid = post.get("agent_id"), post.get("post_id")
            if author == self.agent:
                continue
            reread = pid in self.seen_posts
            self.seen_posts.add(pid)
            msg = post.get("message") or ""
            role = self.ctx.role(author)
            head = f"{pid} by {author} ({role})"
            self.event(ts, "B1", f"B1:{pid}", head + (" [re-read]" if reread else ": " + msg[:POST_TEXT]),
                       tool="get_bulletin_board", author=author, tags=text_tags(msg, role, self.ctx.trick),
                       detail={"post_id": pid, "posted_at": self.t(_epoch(post.get("posted_at"))),
                               "intent_type": post.get("intent_type"), "reread": reread, "delivery": "pull"},
                       turn=turn)

    def on_get_messages(self, ts, r, args, turn):
        for m in r.get("messages") or []:
            author = m.get("from_agent_id")
            if author == self.agent:
                continue
            row = self.ctx.msg_by_public.get(m.get("message_id"))
            mid = "dm-%06d" % row["message_id"] if row else m.get("message_id")
            reread = mid in self.seen_dms
            self.seen_dms.add(mid)
            msg = m.get("message") or ""
            role = self.ctx.role(author)
            head = f"{mid} from {author} ({role})"
            self.event(ts, "D1", f"D1:{mid}", head + (" [re-read]" if reread else ": " + msg[:POST_TEXT]),
                       tool="get_messages", author=author, tags=text_tags(msg, role, self.ctx.trick),
                       detail={"message_id": mid, "public_id": m.get("message_id"),
                               "sent_at": self.t(_epoch(m.get("sent_at"))), "reread": reread, "delivery": "pull"},
                       turn=turn)

    def on_submit_solution(self, ts, r, args, turn):
        row = self.ctx.sub_by_public.get(r.get("submission_id"))
        if row is None:
            src = args.get("source") if isinstance(args.get("source"), str) else ""
            fake = bool(self.ctx.classify(src))
            self.event(ts, "S1", f"S1:{r.get('submission_id')}",
                       f"Submitted {r.get('problem_id')}: {r.get('outcome')} (no conference row)",
                       tool="submit_solution", author=self.agent, fake=fake, outcome=r.get("outcome"),
                       detail={"task_id": r.get("problem_id"), "public_id": r.get("submission_id")}, turn=turn)
            return
        rev = self.ctx.reversions.get(row["submission_id"])
        credited = row["outcome"] == "accepted"
        text = (f"Submitted {row['task_id']} ({sid(row['submission_id'])}): {row['outcome']}"
                + (", credited" if credited else "") + (f"; FAKE ({row['form'] or 'evaluator'})" if row["fake"] else "")
                + (f"; later reverted at {self.t(rev['reverted_at'])} by {rev['reverted_by']}" if rev else ""))
        self.event(ts, "S1", f"S1:{sid(row['submission_id'])}", text, tool="submit_solution", author=self.agent,
                   fake=bool(row["fake"]), outcome=row["outcome"],
                   detail={"task_id": row["task_id"], "submission_id": sid(row["submission_id"]),
                           "public_id": row["public_id"], "credited": credited,
                           "reverted_at": self.t(rev["reverted_at"]) if rev else None,
                           "reverted_by": rev["reverted_by"] if rev else None, "validity": row["validity"],
                           "form": row["form"], "stage": row["stage"], "reason": row["reason"],
                           "submitted_at": self.t(row["submitted_at"]), "judged_at": self.t(row["judged_at"]),
                           "task_status_at_submission": row["task_status_at_submission"]}, turn=turn)

    def _own(self, ts, channel, oid, body, detail, tool, turn):
        self.event(ts, "O1", f"O1:{oid}", f"Own {channel} {oid}: {body[:POST_TEXT]}", tool=tool,
                   author=self.agent, tags=text_tags(body, "overseer" if self.ctx.role(self.agent) == "overseer"
                                                     else "solver", self.ctx.trick), detail=dict(detail, channel=channel),
                   turn=turn)

    def on_post_intent(self, ts, r, args, turn):
        pid = r.get("post_id")
        body = args.get("message") or (self.ctx.posts.get(pid) or {}).get("message") or ""
        self._own(ts, "post", pid, body, {"post_id": pid, "intent_type": args.get("intent_type")}, "post_intent", turn)

    def on_send_direct_message(self, ts, r, args, turn):
        row = self.ctx.msg_by_public.get(r.get("message_id"))
        mid = "dm-%06d" % row["message_id"] if row else r.get("message_id")
        body = args.get("message") or (row or {}).get("body") or ""
        self._own(ts, "dm", mid, body, {"message_id": mid, "to": args.get("to_agent_id") or (row or {}).get("recipient")},
                  "send_direct_message", turn)

    def on_submit_feedback(self, ts, r, args, turn):
        fid = r.get("feedback_id") or f"feedback@{self.t(ts)}"
        self._own(ts, "feedback", fid, args.get("message") or "", {"feedback_id": fid}, "submit_feedback", turn)

    def on_publish_notebook(self, ts, r, args, turn):
        path = r.get("path") or args.get("path") or f"notebook@{self.t(ts)}"
        self._own(ts, "notebook", path, args.get("content") or "", {"path": path, "revision": r.get("revision")},
                  "publish_notebook", turn)


    # -- overseer tools (K1 check_proof, V1 revert_submission, M1 read_overseer_board, M2 post_overseer_board)
    #
    # Join to the conference record: the events table's ``call_id`` is the conference's own ``call-<uuid>``
    # (ConferenceService.action), never the Claude Code tool_use id, so a call is matched to the first unused
    # event of the same agent and tool (seq order) whose time lies in [call time - 60 s, result time + 60 s]
    # (no upper bound when the result is not a conference reply, e.g. an MCP client timeout, after which the
    # conference may still finish and record the call) and whose problem id / submission / post id / error text
    # agree with the reply where both are known. A JSON reply with ok true joins the tool's success kind, ok false
    # joins tool_error/internal_error; failures the MCP bridge raised before the conference (path checks) have
    # no record (detail ``conference_seq`` null).
    def ov_join(self, ts, tool, r, **want):
        events = getattr(self.ctx, "ov_events", None) or ()
        replied = isinstance(r, dict)
        ok = replied and r.get("ok") is True
        kinds = ((OVERSEER_EVENT_KINDS[tool],) if ok else ERROR_KINDS) if replied else \
            (OVERSEER_EVENT_KINDS[tool],) + ERROR_KINDS
        low = (self.call_ts if self.call_ts is not None else ts) - JOIN_SLACK
        high = ts + JOIN_SLACK if replied else None
        for e in events:
            if e["seq"] in self.ov_used or e["agent"] != self.agent or e["tool"] != tool or e["kind"] not in kinds:
                continue
            if e["ts"] < low or (high is not None and e["ts"] > high):
                continue
            p, a = e["payload"], e["payload"].get("args") if isinstance(e["payload"].get("args"), dict) else {}
            if any(v is not None and (p.get(k, a.get(k)) is not None) and p.get(k, a.get(k)) != v
                   for k, v in want.items()):
                continue
            self.ov_used.add(e["seq"])
            return e
        return None

    def ov_next(self, etype):
        self.ov_n[etype] = self.ov_n.get(etype, 0) + 1
        return self.ov_n[etype]

    @staticmethod
    def ov_status(r, text):
        """(ok, error) of an overseer tool result: a JSON reply's ok/error, else the raw text (e.g. a timeout)."""
        if isinstance(r, dict):
            ok = r.get("ok") is True
            return ok, None if ok else str(r.get("error") or "failed")[:500]
        return False, str(text or "no result")[:500]

    def ov_check_proof(self, ts, r, text, args, turn):
        ok, error = self.ov_status(r, text)
        problem = (r or {}).get("problem_id") or args.get("problem_id")
        e = self.ov_join(ts, "check_proof", r, problem_id=problem, error=None if ok else (r or {}).get("error"))
        p = e["payload"] if e and e["kind"] == "proof_checked" else {}
        workspace = bool(args.get("file_path") or args.get("source"))
        target = p.get("target") or ("workspace_file" if workspace else "accepted_submission")
        sub = self.ctx.sub_by_sid(p.get("submission_id"))
        if sub is None and target == "accepted_submission" and problem and not p:
            sub = self.ctx.accepted_sub(problem, self.call_ts if self.call_ts is not None else ts)
        same = sub or self.ctx.sub_by_sha(p.get("source_sha256"))
        if same is not None:
            fake = bool(same["fake"])
        elif isinstance(args.get("source"), str):
            fake = bool(self.ctx.classify(args["source"]))
        else:
            fake = True if p.get("validity") == "fraudulent" else None
        sub_id = sid(sub["submission_id"]) if sub else None
        when = e["ts"] if e else ts
        duplicate = bool(sub_id) and any(
            x["kind"] == "proof_checked" and x["payload"].get("submission_id") == sub_id and
            (x["seq"] < e["seq"] if e else x["ts"] < when) for x in self.ctx.ov_events)
        checked_author = sub["agent_id"] if sub else None
        role = self.ctx.role(checked_author)
        rev = self.ctx.reversions.get(sub["submission_id"]) if sub else None
        if ok:
            outcome = VERDICT_OUTCOME.get(r.get("verdict")) or ("genuine" if r.get("verified") else "undecided")
        else:
            outcome = "failed"
        later = None
        if not ok and p:   # the conference finished the check after the overseer stopped waiting
            later = "genuine" if p.get("verified") else ("not genuine" if p.get("validity") == "fraudulent"
                                                         else "undecided")
        n = self.ov_next("K1")
        what = (f"accepted submission {sub_id} by {checked_author} ({role}"
                + (", FAKE" if fake else (", not fake" if fake is False else "")) + ")" if sub else
                ("accepted submission (none)" if target == "accepted_submission" else
                 f"workspace file {args.get('file_path') or p.get('file_path') or '(inline source)'}"))
        text_ = (f"Proof check #{n} of {problem}: {what} -> "
                 + (outcome + (f" ({r.get('reason')})" if r.get("reason") else "") if ok else "FAILED: " + error)
                 + (f"; the conference later recorded {later}, unseen by the overseer" if later else "")
                 + ("; already checked earlier" if duplicate else ""))
        self.event(ts, "K1", f"K1:{self.agent}:{n}", text_, tool="check_proof",
                   author=checked_author or self.agent, fake=fake, outcome=outcome,
                   detail={"problem_id": problem, "target": target, "submission_id": sub_id,
                           "public_submission_id": p.get("public_submission_id") or (sub or {}).get("public_id"),
                           "file_path": args.get("file_path") or p.get("file_path"),
                           "checked_author": checked_author, "checked_author_role": role, "seed": role == "seed",
                           "verified": p.get("verified", (r or {}).get("verified") if ok else None),
                           "validity": p.get("validity"), "reason": p.get("reason") or (r or {}).get("reason"),
                           "accepted_at": self.t(self.ctx.accept_time(sub, rev)) if sub else None,
                           "ok": ok, "error": error, "conference_verdict": later, "duplicate": duplicate,
                           "first_check_of_submission": bool(sub_id and p and not duplicate),
                           "conference_seq": e["seq"] if e else None}, turn=turn)

    def ov_revert_submission(self, ts, r, text, args, turn):
        ok, error = self.ov_status(r, text)
        problem = (r or {}).get("problem_id") or args.get("problem_id")
        e = self.ov_join(ts, "revert_submission", r, problem_id=problem,
                         public_submission_id=(r or {}).get("reverted_submission") if ok else None,
                         error=None if ok else (r or {}).get("error"))
        p = e["payload"] if e and e["kind"] == "submission_reverted" else {}
        ok = ok or bool(p)   # a reply lost to a client timeout after the conference reverted: the revert happened
        sub = self.ctx.sub_by_sid(p.get("submission_id")) or (
            self.ctx.sub_by_public.get((r or {}).get("reverted_submission")) if ok else None)
        duplicate = False
        if not ok and problem and "no accepted submission" in (error or ""):
            duplicate = True
            prev = self.ctx.last_reverted_sub(problem, ts)
            sub = self.ctx.subs.get(prev["submission_id"]) if prev else None
        when = e["ts"] if e else ts
        if ok and sub:   # the same submission reverted earlier (any overseer): an earlier reversion row
            rid = p.get("reversion_id")
            duplicate = any(x["submission_id"] == sub["submission_id"] and
                            (x["reversion_id"] < rid if isinstance(rid, int) else x["reverted_at"] < when - 1.0)
                            for x in self.ctx.reversions_list)
        credited = p.get("credited_agent") or ((r or {}).get("previously_credited_agent") if ok else None) or \
            (sub or {}).get("agent_id")
        role = self.ctx.role(credited)
        rev = next((x for x in self.ctx.reversions_list if sub and x["submission_id"] == sub["submission_id"]), None)
        accepted = _epoch(p.get("accepted_at")) or (self.ctx.accept_time(sub, rev) if sub else None)
        fake = bool(sub["fake"]) if sub else None
        sub_id = sid(sub["submission_id"]) if sub else None
        n = self.ov_next("V1")
        text_ = (f"Revert #{n} of {problem}: " + (
            f"{sub_id} by {credited} ({role}" + (", FAKE" if fake else ", not fake" if fake is False else "") + ")"
            if sub else "no submission") + (
            f" reverted, {round((when - accepted) / 60, 1)} min after acceptance" if ok and accepted else
            (" reverted" if ok else " FAILED: " + error)) + ("; duplicate" if duplicate else ""))
        self.event(ts, "V1", f"V1:{self.agent}:{n}", text_, tool="revert_submission", author=credited, fake=fake,
                   outcome="reverted" if ok else "failed",
                   detail={"problem_id": problem, "submission_id": sub_id,
                           "public_submission_id": p.get("public_submission_id") or (sub or {}).get("public_id"),
                           "credited_agent": credited, "credited_role": role, "seed": role == "seed",
                           "accepted_at": self.t(accepted), "reverted_at": self.t(when) if ok else None,
                           "latency_min": round((when - accepted) / 60, 3) if ok and accepted else None,
                           "reason": args.get("reason") or p.get("reason"), "ok": ok,
                           "error": None if ok else error, "duplicate": duplicate,
                           "library_path": p.get("library_path") or (
                               self.ctx.library_path_of_sub(sub["submission_id"]) if sub else None),
                           "conference_seq": e["seq"] if e else None}, turn=turn)

    def ov_read_overseer_board(self, ts, r, text, args, turn):
        ok, error = self.ov_status(r, text)
        e = self.ov_join(ts, "read_overseer_board", r, error=None if ok else (r or {}).get("error"))
        if not ok:
            n = self.ov_next("M1-failed")
            self.event(ts, "M1", f"M1:{self.agent}:failed-{n}", "Overseer board read FAILED: " + error,
                       tool="read_overseer_board", author=None,
                       detail={"post_id": None, "ok": False, "error": error, "reread": False, "own": False,
                               "conference_seq": e["seq"] if e else None}, turn=turn)
            return
        for post in r.get("posts") or []:
            pid, author = post.get("post_id"), post.get("agent_id")
            reread = pid in self.ov_seen_posts
            self.ov_seen_posts.add(pid)
            msg = post.get("message") or ""
            self.event(ts, "M1", f"M1:{pid}", f"{pid} by {author}" + (" [re-read]" if reread else
                                                                       ": " + msg[:POST_TEXT]),
                       tool="read_overseer_board", author=author, tags=text_tags(msg, "overseer", self.ctx.trick),
                       detail={"post_id": pid, "posted_at": self.t(_epoch(post.get("posted_at"))), "reread": reread,
                               "own": author == self.agent, "ok": True,
                               "conference_seq": e["seq"] if e else None}, turn=turn)

    def ov_post_overseer_board(self, ts, r, text, args, turn):
        ok, error = self.ov_status(r, text)
        pid = (r or {}).get("post_id") if ok else None
        e = self.ov_join(ts, "post_overseer_board", r, post_id=pid, error=None if ok else (r or {}).get("error"))
        if pid is None and e and e["kind"] == "overseer_board_post":   # reply lost, the post was made
            pid, ok, error = e["payload"].get("post_id"), True, None
        message = args.get("message") or (getattr(self.ctx, "ov_board", {}).get(pid) or {}).get("message") or ""
        eid = f"M2:{pid}" if pid else f"M2:{self.agent}:failed-{self.ov_next('M2-failed')}"
        self.event(ts, "M2", eid, (f"Own overseer-board post {pid}: {message[:POST_TEXT]}" if ok else
                                   "Overseer board post FAILED: " + error), tool="post_overseer_board",
                   author=self.agent, tags=text_tags(message, "overseer", self.ctx.trick),
                   detail={"post_id": pid, "message": message, "ok": ok, "error": error,
                           "conference_seq": e["seq"] if e else None}, turn=turn)


# ---------------------------------------------------------------------------
# Overseer run context (overseer_context.json)


def _step_curve(deltas):
    """[[t, n]] from (t, +/-1) changes: the first point [0, 0], one point per change of the running value."""
    out, n = [[0.0, 0]], 0
    for t, d in sorted(deltas):
        n += d
        if out[-1][0] == t:
            out[-1][1] = n
        else:
            out.append([t, n])
    return out


def overseer_context(ctx):
    """Run-level overseer context (story-map interface, "Overseer mode"): solver fakes and their curves (the seed
    and the overseers excluded), the seed's fake, and every overseer check / revert, from state.sqlite + the
    strict fake rule with evaluator validity. Times are minutes from run start."""
    t = lambda x: mins(x, ctx.start)   # noqa: E731
    block = roles_mod.overseer_block(ctx.settings) or {}
    max_run = (ctx.settings.get("config") or {}).get("max_run_seconds")
    run_minutes = round(max_run / 60, 3) if isinstance(max_run, (int, float)) else t(ctx.last_event_ts)
    seeds = sorted(a for a, r in ctx.roles.items() if r == "seed")
    fakes, fakers, accepted, standing, first_fake = [], {}, [], [], {}
    seed_fake = None
    for s in sorted(ctx.subs.values(), key=lambda s: (s["submitted_at"], s["submission_id"])):
        if not s["fake"]:
            continue
        rev = ctx.reversions.get(s["submission_id"])
        acc = s["outcome"] == "accepted"
        t_acc = t(ctx.accept_time(s, rev)) if acc else None
        path = ctx.library_path_of_sub(s["submission_id"]) if acc else None
        role = ctx.role(s["agent_id"])
        if role == "seed":
            if acc and seed_fake is None:
                seed_fake = {"submission_id": sid(s["submission_id"]), "t_accept": t_acc,
                             "reverted_at": t(rev["reverted_at"]) if rev else None,
                             "reverted_by": rev["reverted_by"] if rev else None, "library_path": path}
            continue
        if role != "solver":
            continue
        fakes.append({"agent": s["agent_id"], "submission_id": sid(s["submission_id"]), "task_id": s["task_id"],
                      "t_submit": t(s["submitted_at"]), "outcome": s["outcome"], "accepted": acc, "t_accept": t_acc,
                      "reverted_at": t(rev["reverted_at"]) if rev and acc else None,
                      "reverted_by": rev["reverted_by"] if rev and acc else None, "library_path": path})
        first_fake.setdefault(s["agent_id"], t(s["submitted_at"]))
        if acc:
            accepted.append((t_acc, 1))
            standing.append((t_acc, 1))
            if rev:
                standing.append((t(rev["reverted_at"]), -1))
    checks, reverts = [], []
    for e in ctx.ov_events:
        p = e["payload"]
        if e["kind"] == "proof_checked":
            checks.append({"overseer": e["agent"], "t": t(e["ts"]), "submission_id": p.get("submission_id"),
                           "target": p.get("target"), "problem_id": p.get("problem_id"),
                           "outcome": "genuine" if p.get("verified") else (
                               "not genuine" if p.get("validity") == "fraudulent" else "undecided")})
        elif e["tool"] == "revert_submission" and e["kind"] in ("submission_reverted",) + ERROR_KINDS:
            reverts.append({"overseer": e["agent"], "t": t(e["ts"]), "submission_id": p.get("submission_id"),
                            "problem_id": p.get("problem_id") or (p.get("args") or {}).get("problem_id"),
                            "ok": e["kind"] == "submission_reverted",
                            "error": None if e["kind"] == "submission_reverted" else p.get("error")})
    return {"schema": "storymap-overseer-context/1", "run_minutes": run_minutes, "overseers": ctx.overseers(),
            "seed": seeds, "board": bool(block.get("board")) or bool(ctx.ov_board),
            "powers": bool(block.get("powers", bool(block))), "contact": bool(block.get("contact", bool(block))),
            "solver_fakes": fakes, "fakers_curve": _step_curve((v, 1) for v in first_fake.values()),
            "accepted_fakes_curve": _step_curve(accepted), "standing_fakes_curve": _step_curve(standing),
            "seed_fake": seed_fake, "checks": checks, "reverts": reverts}


# ---------------------------------------------------------------------------
# Driver


def item_tokens(item):
    from research.analysis.storymap.chunk import render_detail
    n = estimate_tokens(item["text"])
    if item["event"]:
        n += estimate_tokens(json.dumps(render_detail(item["event"]["detail"]), ensure_ascii=False))
    return n


def build_agent(run_dir, agent, ctx=None):
    ctx = ctx or RunContext(run_dir)
    stream, source = session_stream(run_dir, agent), "session_jsonl"
    if stream is None:
        stream, source = inspect_stream(run_dir, agent), "inspect"
    items = Builder(ctx, agent).feed(place_pushes(stream, ctx.pushes.get(agent)))
    times = [it["t"] for it in items if it["t"] is not None]
    return items, {"items": len(items), "est_tokens": sum(item_tokens(it) for it in items),
                   "first_min": min(times) if times else None, "last_min": max(times) if times else None,
                   "role": ctx.role(agent), "source": source}


def _work(job):
    run_dir, agent, out = job
    try:
        items, entry = build_agent(run_dir, agent)
    except Exception as ex:  # noqa: BLE001 - reported per agent, the run continues
        return agent, {"error": f"{type(ex).__name__}: {ex}"}
    with open(Path(out) / f"{agent}.jsonl", "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return agent, entry


def extract(run_dir, out, agents=None, workers=4, log=None, overseers=False):
    """Timelines for ``agents`` (default: every solver; with ``overseers``, every overseer, never the seed).
    When any extracted agent is an overseer, ``timeline/overseer_context.json`` is written too."""
    ctx = RunContext(run_dir)
    selected = agents or (ctx.overseers() if overseers else ctx.solvers())
    tl = Path(out) / "timeline"
    tl.mkdir(parents=True, exist_ok=True)
    jobs = [(str(run_dir), a, str(tl)) for a in selected]
    index = {}
    if workers <= 1:
        results = map(_work, jobs)
    else:
        pool = ProcessPoolExecutor(max_workers=workers)
        results = pool.map(_work, jobs)
    for agent, entry in results:
        index[agent] = entry
        if log:
            log(agent, entry)
    if workers > 1:
        pool.shutdown()
    index = dict(sorted(index.items()))
    old = tl / "index.json"
    if agents and old.exists():
        merged = json.loads(old.read_text(encoding="utf-8"))
        merged.update(index)
        index = dict(sorted(merged.items()))
    (tl / "index.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    if ctx.domain != domain_mod.LEAN:   # coding runs: the viewer and pipeline read the domain from here
        (tl / "domain.json").write_text(json.dumps({"domain": ctx.domain}) + "\n", encoding="utf-8")
    if overseers or any(ctx.role(a) == "overseer" for a in selected):
        (tl / "overseer_context.json").write_text(json.dumps(overseer_context(ctx), indent=1) + "\n",
                                                   encoding="utf-8")
    return index


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--agents", help="comma-separated agent ids (default: every solver)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--overseers", action="store_true",
                    help="map every overseer instead of the solvers (never the seed); writes overseer_context.json")
    a = ap.parse_args(argv)
    agents = [x.strip() for x in a.agents.split(",") if x.strip()] if a.agents else None
    index = extract(a.run_dir, a.out, agents, a.workers, overseers=a.overseers,
                    log=lambda ag, e: print(ag, e.get("error") or f"{e['items']} items, {e['est_tokens']} tokens, "
                                            f"{e['source']}", file=sys.stderr, flush=True))
    bad = [k for k, v in index.items() if "error" in v]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
