"""Full per-agent history reconstruction for the transcript behavior judge (researcher-only).

Implements I-HISTORY (docs/plan.md, "Transcript behavior judge specified, October 17,
2026"; research/analysis/judge_rubric.md Sections 3.5, 3.6 and 4). Offline only: reads a
saved run directory and never calls a model, the network or Docker. Nothing here may
enter an agent image, container, workspace, prompt or the conference library.

Sources (all read-only), for ``runs/<run-id>/``:

* ``inspect/*.eval``: one sample per agent. ``sample.messages`` holds only the segment
  after the last compaction, so each model turn is taken from its ``ModelEvent.output``
  (returned reasoning is complete only there; replayed inputs drop some of it). Tool
  results are deduplicated by ``tool_call_id`` across every model input, plus the final
  turn's results from ``sample.messages``. A model turn whose input carries Claude Code's
  compaction request is a compaction summary turn (``_segment_structure``): written by
  the agent's own model, it is derived self-report (``meta.derived``). Segments start
  where a request's input restarts shorter than the previous one.
* ``conference/state.sqlite`` (events table == ``events.jsonl``): posts, DMs with
  delivery, feedback, library revisions, submissions, the event log (``seq``) and the
  public-to-internal id crosswalk. Agents saw opaque public ids (``dm-<hex>``,
  ``sub-<hex>``) and library paths; items cite internal ids.
* ``runtime.jsonl``: personas, barrier, exits, run stop and ``file_snapshot`` records;
  ``snapshots/<agent>/<blob_sha256>`` holds content-addressed blobs (``mode: append``
  blobs are deltas after ``offset`` bytes of the previous full content).
* ``evaluations/evaluations.jsonl`` and ``statement_defects.json``: host-only context.

Item, history and exposure shapes are the shared interface (I-ITEM, I-HISTORY); see
``make_item`` and ``AgentHistory``. Times are UTC ISO-8601; conference ``seq`` gives the
authoritative cross-agent order of conference events. Capture times of private files
are observation times, not edit times.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import pathname2url

SCHEMA = "gdm-judge-history/1"
REPO = Path(__file__).resolve().parents[3]
DEFECTS_PATH = Path(__file__).with_name("statement_defects.json")

CONFERENCE_PREFIX = "mcp__conference__"
# The neutral collaborative environment (research_swarm.prompts.NEUTRAL_VARIANTS) exposes the
# same tools as mcp__collab__<name>, with get_conference_status renamed get_status
# (research_swarm.tools.TOOL_ALIASES); conference events keep the internal names.
CONFERENCE_PREFIXES = (CONFERENCE_PREFIX, "mcp__collab__")
TOOL_ALIASES = {"get_status": "get_conference_status"}
# Claude Code's own session transcript duplicates the Inspect history (rubric 3.5); the
# neutral environment moves its state directory to ~/.agent.
SESSION_TRANSCRIPT = re.compile(r"^/home/[^/]+/\.(?:claude|agent)/projects/.*\.jsonl$")
# Per-turn token counters (counter-user-v1 context, docs/prompt-caching.md): dropped from
# harness inputs, which are otherwise deduplicated by text.
COUNTER = re.compile(r"<total_tokens>\s*\d+\s+tokens left\s*</total_tokens>")
BILLING_HEADER = re.compile(r"^x-anthropic-billing-header:[^\n]*\n?")
CONTINUATION_MARK = "This session is being continued from a previous conversation"
# Claude Code's compaction request, appended to the input of the summary request.
COMPACTION_REQUEST = "CRITICAL: Respond with TEXT ONLY"
# Tolerance (seconds) around a call's execution window when joining conference events.
JOIN_SLACK = 5.0

# Rubric 3.5 channel of each conference tool (calls and results); other tools are
# ``tool_action``. Library reads of a solution are re-labelled ``library_solution``.
CONFERENCE_CHANNELS = {
    "post_intent": "board", "get_bulletin_board": "board",
    "send_direct_message": "dm", "get_messages": "dm",
    "submit_feedback": "feedback", "submit_solution": "submission",
    "publish_notebook": "notebook", "list_library": "notebook", "read_library_file": "notebook",
}
# Conference event kind that records each tool's action (research_swarm.conference).
PRIMARY_EVENT = {
    "post_intent": "board_post", "get_bulletin_board": "board_read",
    "send_direct_message": "dm_sent", "get_messages": "dm_retrieved",
    "submit_feedback": "feedback_submitted", "list_problems": "problems_listed",
    "get_problem": "problem_viewed", "submit_solution": "submission_received",
    "list_library": "library_listed", "read_library_file": "library_read",
    "publish_notebook": "notebook_published", "get_conference_status": "status_viewed",
}
ERROR_EVENTS = ("tool_error", "internal_error")
EVENT_CHANNELS = {
    "board_post": "board", "board_read": "board", "dm_sent": "dm", "dm_retrieved": "dm",
    "feedback_submitted": "feedback", "notebook_published": "notebook", "library_read": "notebook",
    "library_listed": "notebook", "library_published": "library_solution",
    "submission_received": "submission", "submission_resolved": "submission",
    "submission_judged": "submission", "task_closed": "submission",
}
PUBLIC_ID = re.compile(r"^(dm|sub)-[0-9a-f]{16}$")
AGENT_ID = re.compile(r"^agent-(\d+)$")


# ---------------------------------------------------------------------------
# Small helpers


def agent_code(agent_id):
    """``agent-007`` -> ``A07`` (rubric Section 4); at least two digits."""
    match = AGENT_ID.fullmatch(str(agent_id))
    if not match:
        raise ValueError(f"Unexpected agent identity: {agent_id!r}")
    return "A%02d" % int(match.group(1))


def estimate_tokens(text):
    """Token estimate used for windows and budgets: characters / 4, rounded up.

    A deliberate, tokenizer-free heuristic (DeepSeek and Claude tokenizers differ, and
    mathematical Unicode tokenizes worse than English prose); the survey's run totals
    used the same rule. Budget reservations must not rely on it alone.
    """
    return (len(text or "") + 3) // 4


def _epoch(value):
    """datetime / number / ISO string -> float seconds since the epoch, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _epoch(moment)
    return None


def iso(value):
    """UTC ISO-8601 with microseconds and ``Z`` (one fixed format), or None."""
    seconds = _epoch(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_item(item_id, agent_id, kind, channel, text, *, turn=None, time=None, time_bounds=None,
              authored=False, visible=True, meta=None):
    """One I-ITEM dict. ``text`` is always the full text (never truncated)."""
    return {"id": item_id, "agent_id": agent_id, "kind": kind, "channel": channel, "turn": turn,
            "time": iso(time), "time_bounds": None if time_bounds is None else [iso(time_bounds[0]),
                                                                              iso(time_bounds[1])],
            "text": text if isinstance(text, str) else ("" if text is None else str(text)),
            "authored_by_agent": bool(authored), "visible_to_agent": bool(visible), "meta": meta or {}}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _part_text(part):
    """Text of one non-text content part (image, document...): a JSON description."""
    try:
        data = part.model_dump(mode="json", exclude_none=True)
    except AttributeError:
        data = {"type": getattr(part, "type", "unknown")}
    for key in ("image", "data", "document"):  # never inline encoded payloads
        if isinstance(data.get(key), str) and len(data[key]) > 200:
            data[key] = f"[{len(data[key])} characters omitted]"
    return _dump(data)


def message_parts(message):
    """(reasoning parts, text parts, other parts) of an Inspect message (duck-typed)."""
    content = getattr(message, "content", None)
    reasoning, text, other = [], [], []
    if isinstance(content, str):
        text.append(content)
        return reasoning, text, other
    for part in content or []:
        kind = getattr(part, "type", None)
        if kind == "text":
            text.append(getattr(part, "text", "") or "")
        elif kind == "reasoning":
            if getattr(part, "redacted", False):
                reasoning.append("[redacted reasoning: " + str(len(getattr(part, "reasoning", "") or ""))
                                 + " encoded characters]")
            else:
                reasoning.append(getattr(part, "reasoning", "") or "")
        else:
            other.append(_part_text(part))
    return reasoning, text, other


def message_text(message):
    """All visible text of a message (text parts, then other parts), joined by newlines."""
    _, text, other = message_parts(message)
    return "\n".join(text + other)


def render_call(function, arguments):
    """Readable, deterministic rendering of a tool call: the name, then each argument.

    String values are kept raw (no JSON escaping), so quotes from code or messages match
    the cited text; other values are compact JSON.
    """
    lines = [str(function)]
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if isinstance(value, str):
                lines.append(f"{key}:\n{value}" if "\n" in value else f"{key}: {value}")
            else:
                lines.append(f"{key}: {_dump(value)}")
    elif arguments not in (None, {}):
        lines.append(_dump(arguments))
    return "\n".join(lines)


def _short_tool(function):
    function = str(function or "")
    for prefix in CONFERENCE_PREFIXES:
        if function.startswith(prefix):
            name = function[len(prefix):]
            return TOOL_ALIASES.get(name, name)
    return None


def _channel_for(function):
    tool = _short_tool(function)
    return CONFERENCE_CHANNELS.get(tool, "tool_action") if tool else "tool_action"


def _load_json(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# Records


@dataclass
class AgentHistory:
    """One agent's reconstructed history (I-HISTORY).

    ``items``: chronological judge items (own turns and parts, deduplicated tool results,
    compaction summaries flagged ``meta.derived``, private file versions, own conference
    records). ``exposure_ledger``: exactly what other agents' content was returned to
    this agent. ``host_context``: host-only items (``visible_to_agent`` False).
    ``harness_items``: harness-injected inputs the agent saw (initial prompt, system
    reminders, truncation notices, post-compaction continuation and file re-reads);
    kept apart from ``items`` (not agent behavior), used by the transcript export.
    """
    agent_id: str
    code: str
    items: list = field(default_factory=list)
    exposure_ledger: list = field(default_factory=list)
    host_context: list = field(default_factory=list)
    harness_items: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    source: str | None = None
    persona: str | None = None
    exit: dict | None = None
    role: str | None = None           # "overseer" for an overseer run's monitoring agent, else None
    _index: dict = field(default_factory=dict, repr=False)

    def get(self, item_id):
        """Item (own, harness or host-context) by id; fragment suffixes are ignored."""
        if not self._index:
            self._index = {item["id"]: item for item in self.items + self.harness_items + self.host_context}
        return self._index.get(str(item_id).split("#f", 1)[0])


class ConferenceRecords:
    """Read-only conference state with internal ids, crosswalk and event log."""

    def __init__(self):
        self.available = False
        self.posts, self.dms, self.feedback, self.library, self.submissions = {}, {}, {}, {}, {}
        self.tasks = {}
        # Overseer runs: credits the overseer reverted, keyed by the reverted submission's internal id.
        self.reversions = {}
        self.events = []            # dicts ordered by seq
        self.events_by_seq = {}
        self.public_to_internal, self.internal_to_public = {}, {}
        self.library_by_path = {}   # (path, revision) -> lib id
        self.items = {}             # internal id -> I-ITEM of the record (agent_id = author)
        self.problems = []

    @property
    def crosswalk(self):
        return {"public_to_internal": self.public_to_internal, "internal_to_public": self.internal_to_public}

    def event_item(self, seq):
        """I-ITEM for one conference event (``seq-000412``), built on demand."""
        event = self.events_by_seq.get(int(seq))
        if event is None:
            return None
        return make_item("seq-%06d" % event["seq"], event["agent_id"], "conference",
                         EVENT_CHANNELS.get(event["kind"], "host"), _dump(event["payload"]),
                         time=event["timestamp"], authored=event["agent_id"] is not None,
                         meta={"seq": event["seq"], "kind": event["kind"], "tool": event["tool"],
                               "call_id": event["call_id"]})


@dataclass
class RunHistory:
    run_dir: Path
    run_id: str
    agents: dict
    host: dict
    conference: ConferenceRecords
    runtime: list                     # runtime.jsonl records; RT.L<n> is runtime[n - 1]
    problems: list = field(default_factory=list)
    _index: dict = field(default_factory=dict, repr=False)

    @staticmethod
    def agent_code(agent_id):
        return agent_code(agent_id)

    @staticmethod
    def estimate_tokens(text):
        return estimate_tokens(text)

    def lookup(self, item_id):
        """Resolve any identifier of rubric Section 4 to an I-ITEM, or None.

        Covers every agent's items, harness inputs and host context, conference records
        by internal id, ``seq-NNNNNN`` events, ``RT.L<n>`` runtime lines and ``HOST.*``.
        A ``#f<n>`` window fragment suffix resolves to its whole item.
        """
        item_id = str(item_id).split("#f", 1)[0]
        if not self._index:
            index = dict(self.conference.items)
            for item in self.host.get("items", []):
                index[item["id"]] = item
            for history in self.agents.values():
                for item in history.items + history.harness_items + history.host_context:
                    index[item["id"]] = item
            self._index = index
        if item_id in self._index:
            return self._index[item_id]
        match = re.fullmatch(r"seq-0*(\d+)", item_id)
        if match:
            return self.conference.event_item(int(match.group(1)))
        match = re.fullmatch(r"RT\.L0*(\d+)", item_id)
        if match:
            line = int(match.group(1))
            if 1 <= line <= len(self.runtime):
                record = self.runtime[line - 1]
                return make_item(item_id, record.get("agent_id"), "runtime", "host", _dump(record),
                                 time=record.get("timestamp"), visible=False,
                                 meta={"runtime_line": line, "kind": record.get("kind")})
        return None


# ---------------------------------------------------------------------------
# Conference


def _connect_readonly(path, live=False):
    """Read-only connection. When the write-ahead log is absent or empty (a saved run) the
    database is opened ``immutable`` first, so reading never touches its ``-shm`` file;
    otherwise ``mode=ro`` first, so committed WAL content is read. ``live=True`` (a database
    another process is still writing, e.g. the conference of a running experiment read by the
    live behavior judge) opens it ``mode=ro`` only and never ``immutable``: an empty WAL there
    says nothing about the next write, and SQLite assumes an immutable file never changes."""
    wal = Path(str(path) + "-wal")
    settled = not wal.exists() or wal.stat().st_size == 0
    if live:
        order = ("?mode=ro",)
    else:
        order = ("?mode=ro&immutable=1", "?mode=ro") if settled else ("?mode=ro", "?mode=ro&immutable=1")
    for suffix in order:
        try:
            db = sqlite3.connect("file:" + pathname2url(str(Path(path).resolve())) + suffix, uri=True)
            db.execute("SELECT 1 FROM sqlite_master").fetchone()
            db.row_factory = sqlite3.Row
            return db
        except sqlite3.Error:
            continue
    return None


def load_conference(run_dir, *, live=False):
    """Conference records keyed by internal id (never writes the database). ``live=True``: the
    database may still be written (``_connect_readonly(live=True)``)."""
    conf = ConferenceRecords()
    path = Path(run_dir) / "conference" / "state.sqlite"
    db = _connect_readonly(path, live=live) if path.exists() else None
    if db is None:
        conf.problems.append({"file": str(path), "error": "missing_or_unreadable"})
        return conf
    try:
        rows = lambda table: [dict(r) for r in db.execute(f"SELECT * FROM {table}")]  # noqa: E731
        for row in rows("events"):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                payload = {"unparsed": row["payload"]}
            conf.events.append({"seq": row["seq"], "timestamp": row["timestamp"], "kind": row["kind"],
                                "agent_id": row["agent_id"], "tool": row["tool"], "call_id": row["call_id"],
                                "payload": payload})
        conf.events.sort(key=lambda e: e["seq"])
        conf.events_by_seq = {e["seq"]: e for e in conf.events}
        for row in rows("posts"):
            conf.posts["post-%06d" % row["post_id"]] = row
        for row in rows("messages"):
            conf.dms["dm-%06d" % row["message_id"]] = row
        for row in rows("feedback"):
            conf.feedback["fb-%06d" % row["feedback_id"]] = row
        for row in rows("library"):
            conf.library["lib-%06d" % row["artifact_id"]] = row
            conf.library_by_path[(row["path"], row["revision"])] = "lib-%06d" % row["artifact_id"]
        for row in rows("submissions"):
            conf.submissions["sub-%06d" % row["submission_id"]] = row
        for row in rows("tasks"):
            conf.tasks[row["task_id"]] = row
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reversions'").fetchone():
            for row in rows("reversions"):
                conf.reversions["sub-%06d" % row["submission_id"]] = row
        conf.available = True
    except sqlite3.Error as exc:
        conf.problems.append({"file": str(path), "error": "sqlite:" + type(exc).__name__})
    finally:
        db.close()
    for table in (conf.dms, conf.submissions):
        for internal, row in table.items():
            if row.get("public_id"):
                conf.public_to_internal[row["public_id"]] = internal
                conf.internal_to_public[internal] = row["public_id"]
    _record_items(conf)
    return conf


def _record_items(conf):
    """One I-ITEM per conference record, attributed to its author."""
    seq_of = {}
    for event in conf.events:
        payload = event["payload"]
        for key in ("post_id", "message_id", "feedback_id", "artifact_id", "submission_id"):
            value = payload.get(key)
            if isinstance(value, str) and event["kind"] in (
                    "board_post", "dm_sent", "feedback_submitted", "notebook_published", "library_published",
                    "submission_received"):
                seq_of.setdefault(value, event["seq"])
        if event["kind"] == "dm_retrieved":
            for internal in payload.get("first_delivery_ids") or []:
                if internal in conf.dms:
                    conf.dms[internal]["delivered_seq"] = event["seq"]
    for pid, row in conf.posts.items():
        try:
            tags = json.loads(row["tags"])
        except (TypeError, ValueError):
            tags = row["tags"]
        conf.items[pid] = make_item(pid, row["agent_id"], "conference", "board", row["message"],
                                    time=row["posted_at"], authored=True,
                                    meta={"intent_type": row["intent_type"], "tags": tags,
                                          "event_seq": seq_of.get(pid)})
    for mid, row in conf.dms.items():
        conf.items[mid] = make_item(mid, row["sender"], "conference", "dm", row["body"], time=row["sent_at"],
                                    authored=True,
                                    meta={"recipient": row["recipient"], "public_id": row.get("public_id"),
                                          "event_seq": seq_of.get(mid),
                                          # The sender never learns delivery; recipients are exposed
                                          # only through get_messages results (rubric 3.5).
                                          "host_only": {"delivered_at": iso(row.get("delivered_at")),
                                                        "delivered_seq": row.get("delivered_seq")}})
    for fid, row in conf.feedback.items():
        conf.items[fid] = make_item(fid, row["agent_id"], "conference", "feedback", row["message"],
                                    time=row["submitted_at"], authored=True,
                                    meta={"event_seq": seq_of.get(fid)})
    for lid, row in conf.library.items():
        solution = row["kind"] == "solution"
        conf.items[lid] = make_item(
            lid, row["author"], "conference", "library_solution" if solution else "notebook", row["content"],
            time=row["published_at"], authored=True,
            meta={"path": row["path"], "revision": row["revision"], "library_kind": row["kind"],
                  "task_id": row.get("task_id"), "origin_path": row.get("origin_path"), "sha256": row["sha256"],
                  "submission_id": None if row.get("submission_id") is None else "sub-%06d" % row["submission_id"],
                  # Accepted solutions are published automatically, not by the agent's choice.
                  "automatic": solution, "event_seq": seq_of.get(lid),
                  **_withdrawn_meta(conf, row)})
    for sid, row in conf.submissions.items():
        conf.items[sid] = make_item(
            sid, row["agent_id"], "conference", "submission", row["source"], time=row["submitted_at"],
            authored=True,
            meta={"problem_id": row["task_id"], "public_id": row.get("public_id"), "outcome": row["outcome"],
                  "stage": row.get("stage"), "reason": row.get("reason"),
                  "compile_status": row.get("compile_status"), "sorry_count": row.get("sorry_count"),
                  "file_path": row.get("file_path"), "source_sha256": row["source_sha256"],
                  "task_status_at_submission": row.get("task_status_at_submission"),
                  "judged_at": iso(row.get("judged_at")), "event_seq": seq_of.get(sid),
                  **_reverted_meta(conf.reversions.get(sid))})


def _reverted_meta(reversion):
    """Item meta for a credit the overseer reverted ({} otherwise, so other runs' items are unchanged)."""
    if not reversion:
        return {}
    return {"credit_reverted": {"reverted_by": reversion["reverted_by"], "reason": reversion["reason"],
                                "reverted_at": iso(reversion["reverted_at"]),
                                "note": "The overseer reverted this credit: the credit and the library entry were "
                                        "removed and the problem reopened. Nobody was notified automatically."}}


def _withdrawn_meta(conf, row):
    """Item meta for an accepted solution withdrawn from the library by a reversion ({} otherwise)."""
    for reversion in conf.reversions.values():
        if reversion.get("artifact_id") == row["artifact_id"]:
            return {"withdrawn": {"reverted_by": reversion["reverted_by"], "reason": reversion["reason"],
                                  "withdrawn_at": iso(reversion["reverted_at"])}}
    return {}


def crosswalk_for(value, conf):
    """Public id (or ``path@revision``) -> internal id for everything in a tool result."""
    found = {}

    def walk(node):
        if isinstance(node, dict):
            path, revision = node.get("path"), node.get("revision")
            if isinstance(path, str) and isinstance(revision, int):
                internal = conf.library_by_path.get((path, revision))
                if internal:
                    found[f"{path}@{revision}"] = internal
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, str) and PUBLIC_ID.fullmatch(node):
            found[node] = conf.public_to_internal.get(node)
    walk(value)
    return found


PAGING_KEYS = ("total", "returned", "omitted_older", "omitted_newer", "next_since_seq", "truncated",
               "at_or_before_since_seq")
REQUEST_FILTERS = {"get_bulletin_board": ("intent_type", "agent_id", "tag"), "list_library": ("kind", "agent_id")}


def _paging_matches(result, paging):
    """Every paging field recorded by the event equals the returned one."""
    if not isinstance(paging, dict):
        return True
    return all(result.get(k) == paging[k] for k in PAGING_KEYS if k in paging and k in result)


def _request_matches(tool, args, payload):
    """Explicit paging arguments and filters equal the request the event recorded."""
    if not isinstance(args, dict):
        return True
    for key in ("since_seq", "limit"):
        if args.get(key) is not None and key in payload and str(args[key]) != str(payload[key]):
            return False
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    for key in REQUEST_FILTERS.get(tool, ()):
        if args.get(key) is not None and key in filters and str(args[key]) != str(filters[key]):
            return False
    return True


def _event_matches(tool, result, args, event):
    """Content test joining one parsed conference tool result to one event."""
    payload = event["payload"]
    if result.get("ok") is False:
        error = str(result.get("error") or "")
        if event["kind"] == "tool_error":
            return payload.get("error") == error
        return event["kind"] == "internal_error" and error.startswith("Internal conference error")
    if not _request_matches(tool, args, payload):
        return False
    if tool == "post_intent":
        return payload.get("post_id") == result.get("post_id")
    if tool == "get_bulletin_board":
        return (payload.get("post_ids") == [p.get("post_id") for p in result.get("posts") or []]
                and _paging_matches(result, payload.get("paging")))
    if tool == "send_direct_message":
        return payload.get("public_message_id") == result.get("message_id")
    if tool == "get_messages":
        return (payload.get("public_message_ids") == [m.get("message_id") for m in result.get("messages") or []]
                and _paging_matches(result, payload.get("paging")))
    if tool == "submit_feedback":
        return isinstance(args, dict) and payload.get("message") == args.get("message")
    if tool == "list_problems":
        return _paging_matches(result, payload.get("paging"))
    if tool == "get_problem":
        return payload.get("problem_id") == result.get("problem_id")
    if tool == "submit_solution":
        return payload.get("public_submission_id") == result.get("submission_id")
    if tool == "list_library":
        return (payload.get("artifact_ids") == ["lib-%06d" % f.get("seq", -1) for f in result.get("files") or []]
                and _paging_matches(result, payload.get("paging")))
    if tool == "read_library_file":
        return (payload.get("artifact_id") == "lib-%06d" % (result.get("seq") or -1)
                and payload.get("offset") == result.get("offset")
                and payload.get("returned_chars") == result.get("returned_chars"))
    if tool == "publish_notebook":
        return payload.get("path") == result.get("path") and payload.get("revision") == result.get("revision")
    return True  # get_conference_status: no content to compare; time and uniqueness decide


def join_conference_calls(calls, events, slack=JOIN_SLACK):
    """Join an agent's conference tool results to its conference events.

    ``calls``: dicts with ``tool`` (short name), ``args``, ``result`` (parsed JSON or None)
    and the execution window ``lo``/``hi`` (epoch seconds: the issuing turn's completion and
    the first request that carried the result; ``hi`` may be None). ``events``: this
    agent's conference events. Candidates must match tool, window and content (request
    arguments, returned ids and paging). An event serves one call: unique assignment is
    iterated first over events strictly inside each window, then over the window widened
    by ``slack`` seconds. Returns one dict per call: ``status`` is ``joined``,
    ``ambiguous`` (several candidates, none chosen), ``unmatched``, ``no_event_expected``
    (refused before reaching the conference), ``unparsed`` (the returned text is not a
    JSON object) or ``no_result``; ``seq`` and ``seqs`` (every event of that action).
    """
    by_call = {}
    for event in events:
        if event.get("call_id"):
            by_call.setdefault(event["call_id"], []).append(event["seq"])
    exact, loose = [], []
    for call in calls:
        result, tool = call.get("result"), call["tool"]
        if result is None:
            exact.append(None)
            loose.append(None)
            continue
        kinds = ERROR_EVENTS if result.get("ok") is False else (PRIMARY_EVENT.get(tool),)
        lo = call.get("lo") if call.get("lo") is not None else float("-inf")
        hi = call.get("hi") if call.get("hi") is not None else float("inf")
        matching = [e for e in events if e.get("tool") == tool and e["kind"] in kinds
                    and lo - slack <= e["timestamp"] <= hi + slack
                    and _event_matches(tool, result, call.get("args"), e)]
        loose.append(matching)
        exact.append([e for e in matching if lo <= e["timestamp"] <= hi])
    assigned = {}
    for tier in (exact, loose):
        changed = True
        while changed:
            changed = False
            taken = set(assigned.values())
            for index, options in enumerate(tier):
                if options is None or index in assigned:
                    continue
                free = [e for e in options if e["seq"] not in taken]
                if len(free) == 1:
                    assigned[index] = free[0]["seq"]
                    taken.add(free[0]["seq"])
                    changed = True
    joins = []
    for index, (call, options) in enumerate(zip(calls, loose)):
        result = call.get("result")
        if options is None:   # not a JSON object: an Inspect/CLI error or a shortened output
            joins.append({"status": "unparsed" if call.get("result_text") else "no_result", "seq": None,
                          "seqs": [], "candidates": []})
        elif index in assigned:
            seq = assigned[index]
            call_id = next((e["call_id"] for e in options if e["seq"] == seq), None)
            joins.append({"status": "joined", "seq": seq, "seqs": sorted(by_call.get(call_id, [seq])),
                          "candidates": [e["seq"] for e in options]})
        elif not options and result.get("ok") is False:
            # Refused before the conference (run stopped, or the agent's file could not be
            # read for submit_solution/publish_notebook): no conference event exists.
            joins.append({"status": "no_event_expected", "seq": None, "seqs": [], "candidates": []})
        else:
            joins.append({"status": "ambiguous" if options else "unmatched", "seq": None, "seqs": [],
                          "candidates": [e["seq"] for e in options]})
    return joins


# ---------------------------------------------------------------------------
# Inspect samples


def _ensure_inspect():
    """Import inspect_ai's log readers only after disabling every ``.env`` loader."""
    try:
        from research_swarm.inspect_adapter import disable_inspect_dotenv
    except ImportError:
        sys.path.insert(0, str(REPO / "src"))
        from research_swarm.inspect_adapter import disable_inspect_dotenv
    disable_inspect_dotenv()
    from inspect_ai.log import read_eval_log_sample, read_eval_log_sample_summaries
    return read_eval_log_sample, read_eval_log_sample_summaries


def sample_index(run_dir):
    """agent_id -> (log path, sample id, epoch); refuses duplicate agent samples."""
    _, summaries = _ensure_inspect()
    found = {}
    for path in sorted(Path(run_dir).glob("inspect/*.eval")):
        for summary in summaries(str(path)):
            agent = str(summary.id)
            if not AGENT_ID.fullmatch(agent):
                raise ValueError(f"Unexpected sample identity {agent!r} in {path.name}")
            if agent in found:
                raise ValueError(f"Multiple log samples for {agent}; inspect raw logs before reconstructing")
            found[agent] = (path, summary.id, summary.epoch)
    return found


def _harness_kind(text):
    if text.startswith(CONTINUATION_MARK):
        return "compaction_continuation"
    if text.startswith(COMPACTION_REQUEST):
        return "compaction_request"
    if text.startswith("Output token limit hit"):
        return "truncation_notice"
    if text.startswith("[SYSTEM NOTIFICATION"):
        return "task_notification"   # e.g. a background command finished
    if text.startswith("<system-reminder>"):
        return "system_reminder"
    if "Called the Read tool with the following input" in text:
        return "context_reattachment"
    return "input"


def _segment_structure(events, model_positions):
    """(summary turns, segment boundaries) of one sample's model events.

    A segment boundary is a turn whose input is shorter than the previous turn's (the
    conversation restarted from a compaction summary). A summary turn is a model event
    whose input carries Claude Code's compaction request. ``CompactionEvent`` records are
    not used for placement: Inspect sometimes logs one after the first turn of the new
    segment. When a boundary has no marked turn before it, the turn just before it counts
    as the summary turn only if it ended with ``stop`` and made no tool call.
    """
    summary, boundaries, previous = set(), [], None
    for turn, position in enumerate(model_positions):
        inputs = getattr(events[position], "input", None) or []
        if previous is not None and len(inputs) < previous:
            boundaries.append(turn)
        previous = len(inputs)
        if any(getattr(m, "role", None) == "user" and message_text(m).lstrip().startswith(COMPACTION_REQUEST)
               for m in inputs[-6:]):   # the request sits at the end of its own input
            summary.add(turn)
    for index, boundary in enumerate(boundaries):
        start = boundaries[index - 1] if index else 0
        if any(start <= t < boundary for t in summary):
            continue
        output = getattr(events[model_positions[boundary - 1]], "output", None)
        choices = getattr(output, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        if message is not None and getattr(output, "stop_reason", None) == "stop" and not getattr(
                message, "tool_calls", None):
            summary.add(boundary - 1)
    return summary, boundaries


def reconstruct_sample(sample, *, end_time=None, log_name=None):
    """Turn blocks, harness items, conference calls and statistics for one sample.

    ``end_time``: upper time bound for results only observed in ``sample.messages``
    (the agent's exit). ``log_name``: the log path relative to the run directory, used in
    Inspect coordinates (``meta.inspect`` of turn items, ``meta.inspect_result`` of tool
    results, in timeline.py's reference format). Returns a dict consumed by ``build_agent``.
    """
    agent = str(sample.id)
    code = agent_code(agent)
    base_ref = {"file": log_name, "sample_id": agent}
    events = list(getattr(sample, "events", None) or [])
    model_positions = [i for i, e in enumerate(events) if getattr(e, "event", None) == "model"]
    summary_turns, boundaries = _segment_structure(events, model_positions)

    # Tool results: the first observation per tool_call_id, in event order; conflicting
    # copies are recorded (never resolved by choosing the last copy).
    results, harness, seen_messages, harness_text = {}, [], set(), {}
    counters = 0

    def observe_result(message, turn, when, ref):
        call_id = getattr(message, "tool_call_id", None)
        text = message_text(message)
        error = getattr(message, "error", None)
        record = results.get(call_id)
        if record is None:
            results[call_id] = {"message": message, "text": text, "error": error, "turn": turn, "time": when,
                                "copies": 1, "conflicts": 0, "ref": dict(base_ref, **ref)}
        else:
            record["copies"] += 1
            if text != record["text"]:
                record["conflicts"] += 1

    # Calls and message ids the conversation went on to carry: an output none of whose
    # tool calls (or id) reappear was discarded by the harness (a re-sent request).
    replayed_calls, replayed_ids = set(), set()

    def observe_assistant(message):
        replayed_ids.add(getattr(message, "id", None))
        replayed_calls.update(getattr(c, "id", None) for c in getattr(message, "tool_calls", None) or [])

    for turn, position in enumerate(model_positions):
        event = events[position]
        when = _epoch(getattr(event, "timestamp", None))
        for index, message in enumerate(getattr(event, "input", None) or []):
            role = getattr(message, "role", None)
            if role == "tool":
                observe_result(message, turn, when, {"event_uuid": getattr(event, "uuid", None),
                                                     "input_message_index": index,
                                                     "message_id": getattr(message, "id", None)})
                continue
            if role == "assistant":
                observe_assistant(message)
                continue
            if role not in ("user", "system"):
                continue
            message_id = getattr(message, "id", None)
            if message_id is not None and message_id in seen_messages:
                continue
            seen_messages.add(message_id)
            stripped = COUNTER.sub("", message_text(message)).strip()
            if not stripped:
                counters += 1
                continue
            key = BILLING_HEADER.sub("", stripped)  # the header's build hash changes per segment
            if key in harness_text:   # repeated static blocks (system prompt per segment)
                harness_text[key]["meta"]["repeat_turns"].append(turn)
                continue
            number = 1 + sum(1 for h in harness if h["turn"] == turn)
            item = make_item(f"{code}.t{turn:04d}.u{number}", agent, "runtime", "harness", stripped, turn=turn,
                             time=when, visible=True,
                             meta={"role": role, "message_id": message_id, "harness_kind": _harness_kind(stripped),
                                   "derived": stripped.startswith(CONTINUATION_MARK), "repeat_turns": []})
            harness_text[key] = item
            harness.append(item)
    for index, message in enumerate(getattr(sample, "messages", None) or []):
        if getattr(message, "role", None) == "tool" and getattr(message, "tool_call_id", None) not in results:
            observe_result(message, None, None, {"message_id": getattr(message, "id", None),
                                                 "transcript_index": index})
        elif getattr(message, "role", None) == "assistant":
            observe_assistant(message)

    blocks, calls = [], []
    stats = {"turns": len(model_positions), "turns_with_output": 0, "summary_turns": len(summary_turns),
             "compactions": sum(1 for e in events if getattr(e, "event", None) == "compaction"),
             "model_errors": [], "missing_tool_results": [], "conflicting_tool_results": [],
             "unadopted_turns": [], "counter_messages": counters, "input_resets": len(boundaries)}
    starts = [0] + boundaries   # first turn of each segment
    stats["boundaries_without_summary"] = [b for i, b in enumerate(boundaries)
                                           if not any(starts[i] <= t < b for t in summary_turns)]
    segments = []
    for number, first in enumerate(starts if model_positions else []):
        last = starts[number + 1] - 1 if number + 1 < len(starts) else len(model_positions) - 1
        inside = [t for t in summary_turns if first <= t <= last]
        segments.append({"segment": number, "first_turn": first, "last_turn": last,
                         "summary_turn": max(inside) if inside else None})
    issued = set()
    for turn, position in enumerate(model_positions):
        event = events[position]
        segment = sum(1 for b in boundaries if b <= turn)
        started = _epoch(getattr(event, "timestamp", None))
        completed = _epoch(getattr(event, "completed", None)) or started
        output = getattr(event, "output", None)
        choices = getattr(output, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        error = getattr(event, "error", None)
        if error:
            stats["model_errors"].append(turn)
        derived = turn in summary_turns
        call_ids = [getattr(c, "id", None) for c in getattr(message, "tool_calls", None) or []]
        # adopted: True when the conversation carried this output on (its id or any of its calls
        # reappear later), False when it had calls and none reappear (never executed), else None.
        if call_ids and any(c in replayed_calls or c in results for c in call_ids):
            adopted = True
        elif getattr(message, "id", None) in replayed_ids:
            adopted = True
        else:
            adopted = False if call_ids else None
        if adopted is False and message is not None:
            stats["unadopted_turns"].append(turn)
        base_meta = {"segment": segment, "model_event_uuid": getattr(event, "uuid", None),
                     "message_id": getattr(message, "id", None), "stop_reason": getattr(output, "stop_reason", None),
                     "derived": derived, "adopted": adopted,
                     "inspect": dict(base_ref, event_uuid=getattr(event, "uuid", None),
                                     message_id=getattr(message, "id", None))}
        if error:
            base_meta["error"] = str(error)
        block = []
        if message is not None and not getattr(event, "pending", False):
            stats["turns_with_output"] += 1
            reasoning, text, other = message_parts(message)
            prefix = f"{code}.t{turn:04d}"
            bounds = (started, completed)
            if any(r for r in reasoning):
                block.append(make_item(prefix + ".r", agent, "reasoning", "reasoning", "\n\n".join(reasoning),
                                       turn=turn, time=completed, time_bounds=bounds, authored=True,
                                       meta=dict(base_meta, parts=len(reasoning))))
            visible = "\n".join(t for t in text + other if t)
            if derived:
                block.append(make_item(prefix + ".s", agent, "summary", "assistant_text", visible, turn=turn,
                                       time=completed, time_bounds=bounds, authored=True, meta=dict(base_meta)))
            elif visible.strip():
                block.append(make_item(prefix + ".x", agent, "text", "assistant_text", visible, turn=turn,
                                       time=completed, time_bounds=bounds, authored=True, meta=dict(base_meta)))
            for number, call in enumerate(getattr(message, "tool_calls", None) or [], 1):
                function = getattr(call, "function", None)
                arguments = getattr(call, "arguments", None)
                call_id = getattr(call, "id", None)
                issued.add(call_id)
                meta = dict(base_meta, function=function, call_id=call_id, has_result=call_id in results)
                if getattr(call, "parse_error", None):
                    meta["parse_error"] = str(call.parse_error)
                call_item = make_item(f"{prefix}.c{number}", agent, "tool_call", _channel_for(function),
                                      render_call(function, arguments), turn=turn, time=completed,
                                      time_bounds=bounds, authored=True, meta=meta)
                block.append(call_item)
                record = results.get(call_id)
                if record is None:
                    stats["missing_tool_results"].append(call_item["id"])
                    continue
                result_meta = dict(base_meta, function=function, call_id=call_id,
                                   tool_message_id=getattr(record["message"], "id", None),
                                   first_seen_turn=record["turn"], copies=record["copies"],
                                   inspect_result=record["ref"])
                result_meta.pop("message_id", None)
                if record["conflicts"]:
                    result_meta["conflicting_copies"] = record["conflicts"]
                    stats["conflicting_tool_results"].append(call_item["id"])
                if record["error"] is not None:
                    err = record["error"]
                    result_meta["error"] = {"type": getattr(err, "type", None),
                                            "message": getattr(err, "message", str(err))}
                text_out = record["text"]
                if not text_out and record["error"] is not None:
                    text_out = f"[tool error: {result_meta['error']['type']}] {result_meta['error']['message']}"
                hi = record["time"] if record["time"] is not None else end_time
                result_item = make_item(f"{prefix}.o{number}", agent, "tool_result", _channel_for(function),
                                        text_out, turn=turn, time=None, time_bounds=(completed, hi),
                                        authored=False, visible=True, meta=result_meta)
                block.append(result_item)
                tool = _short_tool(function)
                if tool:
                    calls.append({"tool": tool, "args": arguments, "result": _load_json(text_out),
                                  "result_text": text_out,
                                  "lo": completed, "hi": hi, "call_item": call_item, "result_item": result_item})
        blocks.append((completed if completed is not None else float("-inf"), 0, turn, block))
    stats["orphan_tool_results"] = sorted(str(c) for c in results if c not in issued)
    error = getattr(sample, "error", None)
    stats["inspect_error"] = bool(error)
    if error:
        stats["inspect_error_message"] = str(getattr(error, "message", error))
    return {"agent_id": agent, "code": code, "blocks": blocks, "harness": harness, "calls": calls,
            "segments": segments, "stats": stats}


# ---------------------------------------------------------------------------
# Private files


def _snapshot_versions(run_dir, runtime, agent, code, start_time, problems):
    """Private file version items for one agent (rubric 3.5 ``private_file``).

    Excludes the Claude Code session JSONL (it duplicates the transcript), files the host
    provisioned before the common start while still unmodified (task templates, empty
    notes, project files) and verbatim copies of a provisioned task template anywhere.
    Versions are numbered per path by first observation of each full-file sha256
    (excluded versions keep their numbers); a return to an earlier content is recorded
    in that version's ``reobserved_at``. ``append`` blobs are rebuilt onto the previous
    full content and checked against the recorded full-file hash (``meta.integrity``).
    """
    run_dir = Path(run_dir)
    current, versions, provisioned, last_capture, last_item = {}, {}, {}, {}, {}
    template_hashes, items, excluded = set(), [], {"session_transcript": 0, "unmodified_provisioned": 0,
                                                   "repeat_of_earlier_version": 0, "unreadable": 0}
    by_sha = {}
    for line, record in enumerate(runtime, 1):
        if record.get("kind") != "file_snapshot" or record.get("agent_id") != agent:
            continue
        payload = record.get("payload") or {}
        path, digest, blob_digest = payload.get("path"), payload.get("sha256"), payload.get("blob_sha256")
        when = _epoch(record.get("timestamp"))
        if not isinstance(path, str) or SESSION_TRANSCRIPT.match(path):
            excluded["session_transcript"] += 1
            continue
        if not (isinstance(blob_digest, str) and re.fullmatch(r"[0-9a-f]{64}", blob_digest)):
            problems.append({"agent_id": agent, "runtime_line": line, "error": "unsafe_blob_identity"})
            excluded["unreadable"] += 1
            continue
        blob = run_dir / "snapshots" / agent / blob_digest
        try:
            if blob.is_symlink():
                raise OSError("symlinked blob")
            data = blob.read_bytes()
        except OSError:
            problems.append({"agent_id": agent, "runtime_line": line, "error": "missing_blob", "path": path})
            excluded["unreadable"] += 1
            continue
        integrity = "ok" if hashlib.sha256(data).hexdigest() == blob_digest else "blob_hash_mismatch"
        if payload.get("mode") == "append":
            previous = current.get(path)
            offset = payload.get("offset")
            if previous is None or not isinstance(offset, int) or len(previous) < offset:
                problems.append({"agent_id": agent, "runtime_line": line, "error": "append_without_base",
                                 "path": path})
                excluded["unreadable"] += 1
                continue
            data = previous[:offset] + data
        current[path] = data
        if integrity == "ok" and digest and hashlib.sha256(data).hexdigest() != digest:
            integrity = "content_hash_mismatch"
        digest = digest or hashlib.sha256(data).hexdigest()
        known = versions.setdefault(path, {})
        previous_capture = last_capture.get(path)
        last_capture[path] = when
        if digest in known:
            excluded["repeat_of_earlier_version"] += 1
            if known[digest] is not None:
                known[digest]["meta"]["reobserved_at"].append(iso(when))
            continue
        number = len(known) + 1
        early = start_time is not None and when is not None and when < start_time
        if early:   # provisioned by the host before the common start (templates, empty notes)
            provisioned.setdefault(path, set()).add(digest)
            if path.startswith("/workspace/problems/") and path.endswith(".lean"):
                template_hashes.add(digest)
        if digest in provisioned.get(path, ()) or digest in template_hashes:
            known[digest] = None
            excluded["unmodified_provisioned"] += 1
            continue
        text = data.decode("utf-8", errors="replace")
        item = make_item(f"{code}.f[{path}]@{number}", agent, "file_version", "private_file", text,
                         time=when, time_bounds=(previous_capture, when), authored=True,
                         meta={"path": path, "version": number, "sha256": digest, "bytes": len(data),
                               "mode": payload.get("mode"), "runtime_line": line, "blob_sha256": blob_digest,
                               "integrity": integrity, "decoded_with_replacement": "\ufffd" in text,
                               "reobserved_at": [], "previous_version": last_item.get(path),
                               "duplicate_of": by_sha.get(digest)})
        by_sha.setdefault(digest, item["id"])
        last_item[path] = item["id"]
        known[digest] = item
        items.append((when if when is not None else float("-inf"), 2, line, [item]))
    return items, excluded


def file_version_diff(history, item, context=3):
    """Unified diff of a private file version against the previous included version of
    the same path (``meta.previous_version``), or None for a first version. A compact
    view for prompts; the item text stays the full content."""
    import difflib
    previous = history.get(item["meta"].get("previous_version")) if item["meta"].get("previous_version") else None
    if previous is None:
        return None
    return "".join(difflib.unified_diff(previous["text"].splitlines(True), item["text"].splitlines(True),
                                        fromfile=previous["id"], tofile=item["id"], n=context))


# ---------------------------------------------------------------------------
# Host context


def load_runtime(run_dir, problems, *, complete_lines_only=False):
    """``runtime.jsonl`` records in order (``RT.L<n>`` is record n). ``complete_lines_only``
    (a file still being appended to, read by the live behavior judge): a last line without its
    newline is a record still being written and is left out, never parsed or numbered."""
    path = Path(run_dir) / "runtime.jsonl"
    records = []
    if not path.exists():
        problems.append({"file": str(path), "error": "missing"})
        return records
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if complete_lines_only and not line.endswith("\n"):
                break
            try:
                records.append(json.loads(line))
            except ValueError:
                problems.append({"file": "runtime.jsonl", "line": number, "error": "invalid_json"})
                records.append({"kind": "invalid_json", "timestamp": None, "agent_id": None, "payload": {}})
    return records


def load_statement_defects(path=DEFECTS_PATH):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {entry["task_id"]: entry for entry in data.get("tasks", [])}


def load_host(run_dir, runtime, conf, problems):
    """Host-only facts and ``HOST.*`` items (``visible_to_agent`` False)."""
    run_dir = Path(run_dir)
    host = {"run_id": run_dir.name, "evaluations": {}, "statement_defects": load_statement_defects(),
            "exits": {}, "personas": {}, "roles": {}, "overseer": None, "start_barrier_time": None, "stop_time": None, "stop_reason": None,
            "items": []}
    try:
        settings = json.loads((run_dir / "settings.json").read_text(encoding="utf-8"))
        host["personas"] = dict(settings.get("personas") or {})
        effective = settings.get("effective") or {}
        host["roles"] = dict(effective.get("roles") or {})
        host["overseer"] = effective.get("overseer") or (settings.get("config") or {}).get("overseer")
        # Seeded runs: the planted seed participant (role "seed"; research/analysis/roles.py).
        from research.analysis.roles import SEED, seed_agents
        for agent in seed_agents(settings):
            host["roles"][agent] = SEED
    except (OSError, ValueError):
        problems.append({"file": "settings.json", "error": "missing_or_invalid"})
    for line, record in enumerate(runtime, 1):
        kind, agent = record.get("kind"), record.get("agent_id")
        if kind == "agent_configured" and agent and agent not in host["personas"]:
            host["personas"][agent] = (record.get("payload") or {}).get("persona")
        if kind == "agent_configured" and agent and (record.get("payload") or {}).get("role"):
            host["roles"].setdefault(agent, record["payload"]["role"])
        elif kind == "common_start" and host["start_barrier_time"] is None:
            host["start_barrier_time"] = iso(record.get("timestamp"))
            host["start_barrier_line"] = line
        elif kind == "run_stop":
            host["stop_time"] = iso(record.get("timestamp"))
            host["stop_reason"] = (record.get("payload") or {}).get("reason")
            host["stop_line"] = line
        elif kind == "agent_exit" and agent:
            host["exits"][agent] = dict(record.get("payload") or {}, time=iso(record.get("timestamp")),
                                        runtime_line=line)
    evaluations = run_dir / "evaluations" / "evaluations.jsonl"
    if evaluations.exists():
        for number, line in enumerate(evaluations.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
            except ValueError:
                problems.append({"file": "evaluations/evaluations.jsonl", "line": number, "error": "invalid_json"})
                continue
            submission = record.get("submission") or {}
            sid = submission.get("submission_id")
            if not sid:
                continue
            previous = host["evaluations"].get(sid)
            host["evaluations"][sid] = {
                "submission_id": sid, "agent_id": submission.get("agent_id"), "task_id": record.get("task_id"),
                "validity": record.get("validity"), "reason": record.get("reason"),
                "review_flags": record.get("review_flags"), "review_reasons": record.get("review_reasons"),
                "outcome": submission.get("outcome"), "source_sha256": record.get("source_sha256"),
                "finished_at": record.get("finished_at"), "schema": record.get("schema"),
                "evaluations_line": number, "records": (previous or {}).get("records", 0) + 1}
    for sid, record in sorted(host["evaluations"].items()):
        host["items"].append(make_item("HOST.eval." + sid, record["agent_id"], "host", "host", _dump(record),
                                       time=record.get("finished_at"), visible=False,
                                       meta={"submission_id": sid, "validity": record["validity"]}))
    for task, entry in sorted(host["statement_defects"].items()):
        host["items"].append(make_item("HOST.defect." + task, None, "host", "host", _dump(entry), visible=False,
                                       meta={"task_id": task, "known_defect": entry.get("known_defect")}))
    for agent, record in sorted(host["exits"].items()):
        host["items"].append(make_item("HOST.exit." + agent_code(agent), agent, "host", "host", _dump(record),
                                       time=record.get("time"), visible=False,
                                       meta={"runtime_line": record["runtime_line"], "status": record.get("status"),
                                             "rt": "RT.L%05d" % record["runtime_line"]}))
    if host["start_barrier_time"] is None:  # older logs: the conference's start marker
        marker = next((e for e in conf.events if e["kind"] == "run_start_marked"), None)
        if marker:
            host["start_barrier_time"] = iso(marker["payload"].get("run_started_at") or marker["timestamp"])
    return host


# ---------------------------------------------------------------------------
# Assembly


def _exposure_entries(call, join, conf, agent):
    """Exposure-ledger entries for one conference tool result (exactly the text returned)."""
    result, tool = call["result"], call["tool"]
    if not result or result.get("ok") is False:
        return []
    item = call["result_item"]
    event = conf.events_by_seq.get(join["seq"]) if join.get("seq") else None
    when = event["timestamp"] if event else _epoch(item["time_bounds"][1]) if item["time_bounds"] else None
    basis = "conference_event" if event else "result_upper_bound"
    base = {"item_id": item["id"], "time": iso(when), "seq": join.get("seq"), "time_basis": basis,
            "join_status": join["status"], "tool": tool}
    entries = []
    if tool == "get_bulletin_board":
        for post in result.get("posts") or []:
            if post.get("agent_id") == agent:
                continue
            entries.append(dict(base, source_id=post.get("post_id"), author=post.get("agent_id"),
                                text_returned=post.get("message") or "", kind="board_post",
                                truncated=bool(post.get("text_truncated")),
                                full_text_chars=post.get("full_text_chars")))
    elif tool == "get_messages":
        for message in result.get("messages") or []:
            if message.get("from_agent_id") == agent:
                continue
            internal = conf.public_to_internal.get(message.get("message_id"))
            entries.append(dict(base, source_id=internal, author=message.get("from_agent_id"),
                                text_returned=message.get("message") or "", kind="dm",
                                truncated=bool(message.get("text_truncated")), public_id=message.get("message_id"),
                                first_delivery=bool(message.get("new"))))
    elif tool == "read_library_file":
        if result.get("author") != agent:
            offset = result.get("offset") or 0
            entries.append(dict(base, source_id="lib-%06d" % result["seq"] if isinstance(result.get("seq"), int)
                                else None, author=result.get("author"), text_returned=result.get("content") or "",
                                kind="library_read", library_kind=result.get("kind"), path=result.get("path"),
                                revision=result.get("revision"),
                                range=[offset, offset + (result.get("returned_chars") or 0)],
                                total_chars=result.get("total_chars"),
                                truncated=bool(result.get("has_more")) or offset > 0))
    return entries


def build_agent(sample, run, *, include_files=True, log_name=None):
    """AgentHistory for one Inspect sample, joined to run-level records."""
    agent = str(sample.id)
    exit_record = run.host["exits"].get(agent)
    end_time = _epoch(exit_record.get("time")) if exit_record else _epoch(run.host.get("stop_time"))
    rebuilt = reconstruct_sample(sample, end_time=end_time, log_name=log_name)
    code = rebuilt["code"]
    conf = run.conference
    stats = rebuilt["stats"]

    # Conference joins, crosswalk annotations and the exposure ledger.
    agent_events = [e for e in conf.events if e["agent_id"] == agent and e.get("tool")]
    joins = join_conference_calls(rebuilt["calls"], agent_events)
    ledger, join_counts, call_of_record = [], {}, {}
    for call, join in zip(rebuilt["calls"], joins):
        join_counts[join["status"]] = join_counts.get(join["status"], 0) + 1
        result_item, call_item = call["result_item"], call["call_item"]
        annotation = {"status": join["status"], "seq": join["seq"], "seqs": join["seqs"],
                      "candidates": join["candidates"] if join["status"] != "joined" else []}
        result_item["meta"]["conference"] = annotation
        call_item["meta"]["conference"] = annotation
        if join["seq"] is not None:
            result_item["time"] = iso(conf.events_by_seq[join["seq"]]["timestamp"])
        if call["result"] is not None:
            result_item["meta"]["crosswalk"] = crosswalk_for(call["result"], conf)
            if call["tool"] == "read_library_file" and call["result"].get("kind") == "solution":
                result_item["channel"] = call_item["channel"] = "library_solution"
        for seq in join["seqs"]:
            payload = conf.events_by_seq[seq]["payload"]
            for key in ("post_id", "message_id", "feedback_id", "artifact_id", "submission_id"):
                if isinstance(payload.get(key), str) and conf.events_by_seq[seq]["kind"] != "library_read":
                    call_of_record.setdefault(payload[key], call_item["id"])
        if join["seq"] is not None:   # the record this action created (communication or submission)
            primary = conf.events_by_seq[join["seq"]]
            created = {"board_post": "post_id", "dm_sent": "message_id", "feedback_submitted": "feedback_id",
                       "notebook_published": "artifact_id", "submission_received": "submission_id"}.get(primary["kind"])
            if created and isinstance(primary["payload"].get(created), str):
                call_item["meta"]["conference_id"] = result_item["meta"]["conference_id"] = primary["payload"][created]
        ledger.extend(_exposure_entries(call, join, conf, agent))
    ledger.sort(key=lambda e: (_epoch(e["time"]) or float("inf"), e["seq"] or 0, e["item_id"]))

    # The agent's own conference records, attributed to the joined tool call.
    own = []
    for record_id, item in conf.items.items():
        if item["agent_id"] != agent:
            continue
        if record_id in call_of_record:
            item["meta"]["call_item"] = call_of_record[record_id]
            owner = run_item_turn(call_of_record[record_id])
            if owner is not None:
                item["turn"] = owner
        own.append((_epoch(item["time"]) if item["time"] else float("-inf"), 1, record_id, [item]))

    files, excluded = ([], {})
    if include_files:
        files, excluded = _snapshot_versions(run.run_dir, run.runtime, agent, code,
                                             _epoch(run.host.get("start_barrier_time")), run.problems)
    blocks = sorted(rebuilt["blocks"] + own + files, key=lambda b: (b[0], b[1], b[2]))
    items = [item for block in blocks for item in block[3]]

    host_context = [h for h in run.host["items"] if h["id"] == "HOST.exit." + code]
    host_context += [h for h in run.host["items"] if h["id"].startswith("HOST.eval.") and h["agent_id"] == agent]
    host_context += [h for h in run.host["items"] if h["id"].startswith("HOST.defect.")]

    chars, tokens, counts = {}, {}, {}
    for item in items:
        kind = item["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        chars[kind] = chars.get(kind, 0) + len(item["text"])
        tokens[kind] = tokens.get(kind, 0) + estimate_tokens(item["text"])
    stats.update(items=len(items), items_by_kind=counts, chars_by_kind=chars, tokens_by_kind=tokens,
                 tokens_total=sum(tokens.values()), harness_items=len(rebuilt["harness"]),
                 conference_joins=join_counts, exposure_entries=len(ledger),
                 files_excluded=excluded, segments=len(rebuilt["segments"]))
    return AgentHistory(agent_id=agent, code=code, items=items, exposure_ledger=ledger, host_context=host_context,
                        harness_items=rebuilt["harness"], segments=rebuilt["segments"], stats=stats,
                        persona=run.host["personas"].get(agent), exit=exit_record,
                        role=agent_role(run.host, agent))


def agent_role(host, agent):
    """"overseer" for an overseer run's monitoring agent, "seed" for a seeded run's planted participant
    (which has no Inspect sample, so it normally never gets a history); None for every other agent (solvers
    keep the package shape of runs without an overseer)."""
    overseer = (host.get("overseer") or {}).get("agent_id")
    role = (host.get("roles") or {}).get(agent) or ("overseer" if agent == overseer else None)
    return role if role in ("overseer", "seed") else None


def run_item_turn(item_id):
    """Turn number encoded in an ``A07.t0123.*`` id, else None."""
    match = re.match(r"^A\d+\.t(\d+)\.", str(item_id))
    return int(match.group(1)) if match else None


def load_run_base(run_dir, *, live=False):
    """RunHistory of a run directory without any agent (runtime, conference and host records read
    once; ``build_agent`` adds agents). ``live=True`` reads a run that is still going: complete
    runtime lines only and the conference database ``mode=ro`` only (the live behavior judge)."""
    run_dir = Path(run_dir).resolve()
    problems = []
    runtime = load_runtime(run_dir, problems, complete_lines_only=live)
    conference = load_conference(run_dir, live=live)
    problems.extend(conference.problems)
    host = load_host(run_dir, runtime, conference, problems)
    return RunHistory(run_dir=run_dir, run_id=run_dir.name, agents={}, host=host, conference=conference,
                      runtime=runtime, problems=problems)


def build_run(run_dir, *, agents=None, include_files=True):
    """RunHistory for a saved run: every agent (or the named subset), offline.

    Reads one Inspect sample at a time. ``include_files=False`` skips private file
    versions (faster; for diagnostics only).
    """
    run_dir = Path(run_dir).resolve()
    run = load_run_base(run_dir)
    index = sample_index(run_dir)
    selected = sorted(index) if agents is None else [str(a) for a in agents]
    missing = [a for a in selected if a not in index]
    if missing:
        raise ValueError("No Inspect sample for: " + ", ".join(missing))
    read_sample, _ = _ensure_inspect()
    for agent in selected:
        path, sample_id, epoch = index[agent]
        sample = read_sample(str(path), id=sample_id, epoch=epoch, resolve_attachments=True)
        source = str(path.relative_to(run_dir)) if path.is_relative_to(run_dir) else str(path)
        history = build_agent(sample, run, include_files=include_files, log_name=source)
        history.source = source
        run.agents[agent] = history
        del sample
    return run


def _build_one(run_dir, agent_id):
    """Worker of ``build_runs``: one agent's RunHistory (pickled back to the parent)."""
    return agent_id, build_run(run_dir, agents=[agent_id])


HISTORY_WORKER_BYTES = 6 * 1024 ** 3   # peak memory of one worker: ~4.6 GB for a 37 MB Inspect sample
HISTORY_WORKERS_MAX = 16


def default_history_workers(count, memory_bytes=None):
    """Processes for ``build_runs``: one per agent, at most the CPUs, at most HISTORY_WORKERS_MAX
    and at most one per HISTORY_WORKER_BYTES of physical memory (each worker parses and validates
    one whole Inspect sample: about 4.6 GB at peak for a 37 MB sample)."""
    import os
    if memory_bytes is None:
        try:
            memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, ValueError, OSError):
            memory_bytes = HISTORY_WORKER_BYTES
    return max(1, min(int(count), os.cpu_count() or 1, HISTORY_WORKERS_MAX, int(memory_bytes // HISTORY_WORKER_BYTES)))


def build_runs(run_dir, agents, *, workers=None, log=None):
    """``{agent_id: build_run(run_dir, agents=[agent_id])}`` for every agent, rebuilt in parallel
    worker processes (reading and validating an Inspect sample takes tens of seconds per agent and
    is CPU bound). Processes use the ``spawn`` start method: no thread or lock state of the parent
    is inherited, and nothing here starts a thread in the parent. ``workers=1`` (or one agent)
    rebuilds in this process."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed
    log = log or (lambda message: None)
    agents = [str(a) for a in agents]
    workers = default_history_workers(len(agents)) if workers is None else max(1, int(workers))
    if workers == 1 or len(agents) <= 1:
        out = {}
        for agent in agents:
            out[agent] = build_run(run_dir, agents=[agent])
            log(f"history {agent} rebuilt")
        return out
    out = {}
    with ProcessPoolExecutor(max_workers=min(workers, len(agents)),
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(_build_one, str(run_dir), agent) for agent in agents]
        for future in as_completed(futures):
            agent, run = future.result()
            out[agent] = run
            log(f"history {agent} rebuilt")
    return {agent: out[agent] for agent in agents}


class HistoryCache:
    """Each agent's rebuilt RunHistory, built once per invocation (``build_runs``) and shared by the
    invocation estimate and the pipeline (``loader(agent_id)``). ``release(agent_id)`` drops one
    once the pipeline no longer needs it. A cached history is returned without a thread
    (``in_memory``); an agent not prebuilt is rebuilt here on demand."""
    in_memory = True

    def __init__(self, run_dir, runs=None):
        self.run_dir = run_dir
        self.runs = dict(runs or {})

    @classmethod
    def build(cls, run_dir, agents, *, workers=None, log=None):
        return cls(run_dir, build_runs(run_dir, agents, workers=workers, log=log))

    def __call__(self, agent_id):
        run = self.runs.get(agent_id)
        if run is None:
            run = self.runs[agent_id] = build_run(self.run_dir, agents=[agent_id])
        return run

    def release(self, agent_id):
        self.runs.pop(agent_id, None)


def summarize(run, target_tokens=6000):
    """Per-agent statistics table (turns, items, tokens by kind, windows), JSON-ready."""
    from .windows import make_windows
    rows = {}
    for agent, history in sorted(run.agents.items()):
        stats = history.stats
        rows[agent] = {"turns": stats["turns"], "summary_turns": stats["summary_turns"],
                       "compactions": stats["compactions"], "segments": stats["segments"],
                       "items": stats["items"], "items_by_kind": stats["items_by_kind"],
                       "tokens_by_kind": stats["tokens_by_kind"], "tokens_total": stats["tokens_total"],
                       "windows": len(make_windows(history.items, target_tokens)),
                       "exposure_entries": stats["exposure_entries"], "conference_joins": stats["conference_joins"],
                       "missing_tool_results": len(stats["missing_tool_results"]),
                       "unadopted_turns": stats["unadopted_turns"], "files_excluded": stats["files_excluded"]}
    return rows


if __name__ == "__main__":  # researcher diagnostics: python -m research.analysis.judge.history RUN_DIR
    import argparse
    parser = argparse.ArgumentParser(description="Summarize a run's reconstructed agent histories (offline).")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--agent", action="append")
    parser.add_argument("--target-tokens", type=int, default=6000)
    options = parser.parse_args()
    built = build_run(options.run_dir, agents=options.agent)
    print(json.dumps({"schema": SCHEMA, "run_id": built.run_id, "problems": built.problems[:50],
                      "agents": summarize(built, options.target_tokens)}, indent=2, ensure_ascii=False))
