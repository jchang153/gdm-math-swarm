"""Rollback lever of the asynchronous reviewer arms A1 (reason shown) and A2 (silent).

docs/plan.md, "Asynchronous reviewer arms A1–A4 (owner decisions, November 18, 2026)". Given agent X
and a cut tool call C (the earliest misbehaving call the reviewer names), X is set back to just before
C without restarting its Claude Code process:

(a) conversation: a bridge-level history splice. From the rollback on, every request of X that holds
    the assistant turn issuing C has that turn and everything after it up to the rollback moment (the
    "span") removed, so the model never sees it again; Claude Code keeps its own full history and the
    splice is re-applied identically to each request (like the push and block pins of
    inspect_adapter), which keeps the prompt-cache prefix before the cut intact. A1 adds one user
    message with the reviewer's note at the cut (the same object on every request); A2 adds nothing.
    A splice is applied only when the result keeps tool_use/tool_result pairing valid.
(b) files: host-driven snapshots of X's private files taken at the start of every request with
    tools, i.e. after every earlier tool call finished and before any tool call of the coming
    response runs; the response's tool calls are keyed to that snapshot. The scan runs in X's
    container as X's user (the containers drop every capability, so root cannot read X's 0700
    folders) from a script passed on stdin; the store lives on the host (FileStore), so X can
    never see or change it. The rollback restores the snapshot of the request whose response
    issued C.
(c) submissions: X's accepted submissions made at or after that request are reverted (revert by
    problem, through the ``revert`` callable supplied by the reviewer loop).
(d) A1: the note; A2: none.

The rollback itself runs at X's next model request (inside the bridge filter, ``before_request``):
at that moment Claude Code has finished every tool call of its previous response, so no tool of X
runs while files are restored, and the span is exactly the request's messages from the cut turn on.
``apply_rollback`` waits for that request (bounded) and returns the record. Any failure before the
splice is activated leaves the agent as it was (files are put back if a later step fails).

Compaction: a Claude Code compaction request passes through the same filter, so its summary is made
from the spliced view (a trailing compaction instruction is kept out of the span); afterwards the
cut turn is no longer in the history and the splice has nothing left to remove (recorded once as
``rollback_splice_absent``).

Board posts and DMs already sent stay, as do other agents' library entries; the clock is not
rolled back. Researcher-only: nothing here enters an agent workspace.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sqlite3
import time
import uuid

from .rollback_fs import BOOTSTRAP, SCRIPT as FS_SCRIPT

ROLLBACK_VERSION = "rollback-v1"

AGENT_UID = 1000
HOME = "/home/researcher"
ROOTS = ("/workspace", "/tmp", HOME)
# Claude Code's own state is never restored (its running process owns it); its auto-memory is.
SKIP_TOP = {HOME: [".claude", ".agent"], "/tmp": ["claude", ".gdm-resume-"]}
EXTRA_GLOBS = (HOME + "/.claude/projects/*/memory", HOME + "/.agent/projects/*/memory")
SKIP_NAMES = (".lake", "__pycache__", ".cache", "node_modules", ".npm")
MAX_FILE_BYTES = 2 << 20             # larger files are skipped (left alone by a restore)
MAX_SCAN_BYTES = 6 << 20             # new content per scan (Inspect's exec output limit is 10 MiB)
MAX_ENTRIES = 50000

SNAPSHOT_TIMEOUT_SECONDS = 60
RESTORE_TIMEOUT_SECONDS = 180
CLAIM_TIMEOUT_SECONDS = 300           # apply_rollback: wait for the agent's next request
RETENTION_SECONDS = 1800              # snapshots older than this (and unreferenced) are pruned
PRUNE_EVERY = 25                      # snapshots between prunes

# Claude Code's compaction instruction (neutral_env._COMPACTION): a trailing message holding it is
# never part of a span.
COMPACTION_MARKER = "Your task is to create a detailed summary of the conversation so far"


# ---------------------------------------------------------------------------
# Message-level helpers (inspect ChatMessage lists, as the bridge filter sees them)

def _role(message):
    return getattr(message, "role", None)


def _call_ids(message):
    return [c.id for c in (getattr(message, "tool_calls", None) or []) if getattr(c, "id", None)]


def _text(message):
    try:
        return message.text or ""
    except Exception:
        content = getattr(message, "content", "")
        return content if isinstance(content, str) else ""


def signature(message):
    """Structural identity of one message, robust to tool-result content edits (microcompaction)."""
    role = _role(message)
    if role == "tool":
        return ("tool", getattr(message, "tool_call_id", None))
    if role == "assistant":
        ids = tuple(_call_ids(message))
        return ("assistant", ids) if ids else ("assistant", hashlib.sha256(_text(message).encode()).hexdigest())
    return (role, hashlib.sha256(_text(message).encode()).hexdigest())


def cut_index(messages, cut_id):
    """Index of the (last) assistant message issuing tool call ``cut_id``, else None."""
    for index in range(len(messages) - 1, -1, -1):
        if _role(messages[index]) == "assistant" and cut_id in _call_ids(messages[index]):
            return index
    return None


def pairing_problems(messages):
    """Set of tool_use/tool_result pairing faults: ("orphan", id) for a result without a call in the
    preceding assistant turn, ("unanswered", id) for a call with no result before the next assistant
    turn (calls of the final assistant turn are not counted)."""
    problems, open_ids = set(), set()
    for message in messages:
        role = _role(message)
        if role == "assistant":
            problems.update(("unanswered", i) for i in open_ids)
            open_ids = set(_call_ids(message))
        elif role == "tool":
            tid = getattr(message, "tool_call_id", None)
            if tid in open_ids:
                open_ids.discard(tid)
            else:
                problems.add(("orphan", tid))
    return problems


def _is_compaction(message):
    return _role(message) == "user" and COMPACTION_MARKER in _text(message)


@dataclasses.dataclass
class Splice:
    """One rollback's removal: from the assistant turn issuing ``cut_id``, ``length`` messages, the
    last of which has ``end_sig``; ``note_message`` (A1) is inserted in their place."""
    cut_id: str
    length: int
    end_sig: tuple
    span_ids: frozenset
    n_calls: int
    note_message: object = None
    absent_recorded: bool = False


def capture_splice(messages, cut_id, note=None):
    """(Splice, None) for the span from the turn issuing ``cut_id`` to the end of ``messages`` (a
    trailing compaction instruction excluded), or (None, error)."""
    k = cut_index(messages, cut_id)
    if k is None:
        return None, "cut_call_not_in_context"
    end = len(messages)
    if end - 1 > k and _is_compaction(messages[end - 1]):
        end -= 1
    span = messages[k:end]
    span_ids = set()
    n_calls = 0
    for message in span:
        ids = _call_ids(message)
        n_calls += len(ids)
        span_ids.update(ids)
        if _role(message) == "tool":
            span_ids.add(getattr(message, "tool_call_id", None))
    note_message = None
    if note:
        from inspect_ai.model import ChatMessageUser
        note_message = ChatMessageUser(content=note)
    splice = Splice(cut_id=cut_id, length=end - k, end_sig=signature(span[-1]), span_ids=frozenset(span_ids),
                    n_calls=n_calls, note_message=note_message)
    view, status = splice_one(messages, splice)
    if status != "applied":
        return None, f"splice_invalid:{status}"
    return splice, None


def _in_span(message, splice):
    role = _role(message)
    if role == "tool":
        return getattr(message, "tool_call_id", None) in splice.span_ids
    if role == "assistant":
        return any(i in splice.span_ids for i in _call_ids(message))
    return False


def splice_one(messages, splice):
    """(messages, status): status "applied", "absent" (the cut turn is not in this request, e.g. a side
    request or after compaction) or a mismatch reason (messages returned unchanged)."""
    k = cut_index(messages, splice.cut_id)
    if k is None:
        return messages, "absent"
    end = k + splice.length

    def fits(e):
        return e <= len(messages) and signature(messages[e - 1]) == splice.end_sig and (
            e == len(messages) or not _in_span(messages[e], splice))
    if not fits(end):  # messages inserted or dropped inside the span: find its end by structure
        end = next((e for e in range(k + 1, len(messages) + 1) if fits(e)), None)
        if end is None:
            return messages, "end_not_found"
    view = list(messages[:k]) + ([splice.note_message] if splice.note_message is not None else []) \
        + list(messages[end:])
    if not pairing_problems(view) <= pairing_problems(messages):
        return messages, "pairing_invalid"
    return view, "applied"


def apply_splices(messages, splices):
    """(messages, statuses): every splice applied in order (each on the previous one's view)."""
    statuses = []
    for splice in splices:
        messages, status = splice_one(messages, splice)
        statuses.append(status)
    return messages, statuses


# ---------------------------------------------------------------------------
# Conference: submissions to revert

def accepted_submissions_since(database_path, agent_id, since):
    """[{problem_id, submission_id, public_id, submitted_at}] of ``agent_id``'s currently accepted
    submissions made at or after ``since`` (conference clock), read-only."""
    db = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=30)
    try:
        rows = db.execute(
            "SELECT t.task_id, s.submission_id, s.public_id, s.submitted_at FROM tasks t "
            "JOIN submissions s ON s.submission_id = t.accepted_submission "
            "WHERE t.status = 'closed' AND t.credited_agent = ? AND s.agent_id = ? AND s.submitted_at >= ? "
            "ORDER BY s.submitted_at", (agent_id, agent_id, since)).fetchall()
    finally:
        db.close()
    return [{"problem_id": r[0], "submission_id": f"sub-{r[1]:06d}", "public_id": r[2], "submitted_at": r[3]}
            for r in rows]


def library_entries_since(database_path, agent_id, since):
    """Library entries ``agent_id`` published at or after ``since`` (kept; listed in the record)."""
    db = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=30)
    try:
        rows = db.execute("SELECT artifact_id, path, kind FROM library WHERE author = ? AND published_at >= ? "
                          "ORDER BY artifact_id", (agent_id, since)).fetchall()
    finally:
        db.close()
    return [{"artifact_id": f"lib-{r[0]:06d}", "path": r[1], "kind": r[2]} for r in rows]


# ---------------------------------------------------------------------------
# Host-side file store (one per agent)

def _same(cur, want):
    if cur is None or cur[0] != want[0] or cur[1] != want[1]:
        return False
    if want[0] == "l":
        return cur[4] == want[4]
    if want[0] == "f":
        return cur[4] == want[4] and cur[5] == want[5]
    return True


class FileStore:
    """Snapshots of one agent's private files, kept on the host (``directory``, researcher-only):
    ``objects/<sha256>``, ``manifests/<sha256>.json`` (deduplicated) and the snapshot ids in memory.

    A manifest maps container paths to ["d", mode, uid, gid], ["l", mode, uid, gid, target],
    ["f", mode, uid, gid, sha256, mtime_ns] or ["s", mode, uid, gid, size] (skipped: left alone)."""

    def __init__(self, directory):
        from pathlib import Path
        self.dir = Path(directory)
        (self.dir / "objects").mkdir(parents=True, exist_ok=True)
        (self.dir / "manifests").mkdir(parents=True, exist_ok=True)
        self.cache = {}   # path -> [size, mtime_ns, inode, sha256] of the last scan
        self.snaps = {}   # snapshot id -> (manifest sha256, wall time)

    def _object(self, sha):
        return self.dir / "objects" / sha

    def ingest(self, snapshot_id, reply):
        """Store a ``scan`` reply as snapshot ``snapshot_id``; returns its manifest."""
        import base64
        import os
        for sha, data in (reply.get("blobs") or {}).items():
            path = self._object(sha)
            if not path.exists():
                raw = base64.b64decode(data)
                if hashlib.sha256(raw).hexdigest() != sha:
                    raise ValueError("object_digest_mismatch")
                tmp = path.with_name(f".{sha}.{uuid.uuid4().hex[:8]}")
                tmp.write_bytes(raw)
                os.replace(tmp, path)
        manifest, cache = {}, {}
        for path, e in (reply.get("entries") or {}).items():
            if e[0] == "f":
                if not self._object(e[4]).exists():
                    raise ValueError("object_missing")
                manifest[path] = e[:6]
                cache[path] = [e[6], e[5], e[7], e[4]]
            else:
                manifest[path] = e
        blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        msha = hashlib.sha256(blob).hexdigest()
        mpath = self.dir / "manifests" / f"{msha}.json"
        if not mpath.exists():
            mpath.write_bytes(blob)
        self.cache = cache
        self.snaps[snapshot_id] = (msha, time.time())
        return manifest

    def manifest(self, snapshot_id):
        msha, _ = self.snaps[snapshot_id]
        return json.loads((self.dir / "manifests" / f"{msha}.json").read_text())

    def restore_plan(self, want, cur):
        """The ``apply`` plan turning state ``cur`` into ``want`` (skipped entries left alone), and the
        number of skipped files the restore leaves as they are."""
        import base64
        delete = [[p, cur[p][0] == "d"] for p in sorted(cur, key=lambda q: (-q.count("/"), q))
                  if p not in want and cur[p][0] != "s"]
        dirs = [[p, want[p][1]] for p in sorted((q for q in want if want[q][0] == "d"),
                                                key=lambda q: (q.count("/"), q)) if not _same(cur.get(p), want[p])]
        files, left = [], 0
        for p in sorted(want):
            w = want[p]
            if w[0] == "s":
                left += 1
            elif w[0] in ("f", "l") and not _same(cur.get(p), w):
                if w[0] == "l":
                    files.append([p, "l", 0, 0, w[4]])
                else:
                    files.append([p, "f", w[1], w[5], base64.b64encode(self._object(w[4]).read_bytes()).decode()])
        return {"delete": delete, "dirs": dirs, "files": files}, left

    def prune(self, keep):
        """Drop snapshots not in ``keep`` and every object or manifest nothing kept references."""
        keep = set(keep)
        self.snaps = {s: v for s, v in self.snaps.items() if s in keep}
        live = {m for m, _ in self.snaps.values()}
        objects = {c[3] for c in self.cache.values()}
        for path in (self.dir / "manifests").glob("*.json"):
            if path.stem not in live:
                path.unlink()
                continue
            objects.update(e[4] for e in json.loads(path.read_text()).values() if e[0] == "f")
        freed = 0
        for path in (self.dir / "objects").iterdir():
            if path.name not in objects and not path.name.startswith("."):
                path.unlink()
                freed += 1
        return freed


# ---------------------------------------------------------------------------
# Live manager

@dataclasses.dataclass
class _Mark:
    snapshot_id: str | None
    conference_time: float
    wall_time: float


@dataclasses.dataclass
class _Pending:
    cut_id: str
    note: str | None
    future: asyncio.Future
    claimed: bool = False
    requested_at: float = 0.0


class _AgentState:
    def __init__(self, sbox, store):
        self.sbox = sbox
        self.store = store
        self.ready = False
        self.lock = asyncio.Lock()
        self.marks: dict[str, _Mark] = {}
        self.splices: list[Splice] = []
        self.pending: _Pending | None = None
        self.tokens: dict[str, tuple] = {}  # request token -> (task, snapshot_id, conference_time, wall_time)
        self.counter = 0
        self.snapshots = 0
        self.snapshot_errors = 0


def _consume(task):
    if not task.cancelled():
        task.exception()


class RollbackManager:
    """One run's rollback lever (every reviewed agent of an A1/A2 run).

    ``record(kind, agent_id, payload)``: the run's event recorder. ``store_dir``: host folder of the
    snapshot stores; by default a private (0700) temporary folder outside the run directory, removed
    when the process exits (raw agent files never enter a run record or a publication).
    ``conference``: the ConferenceService (its ``database_path`` and clock select the submissions to
    revert). ``revert(agent_id, problem_id, reason) -> dict | None`` (sync, run in a thread): reverts
    one problem's accepted submission without notifying anyone; by default the conference's
    ``reviewer_revert`` (None or ``ok`` False counts as a failure). ``base`` and ``uid`` serve tests
    only (a host folder standing in for "/", the test user's uid).
    """

    def __init__(self, record, store_dir=None, conference=None, revert=None, *, conference_clock=None, base="",
                 uid=AGENT_UID, python="python3", retention_seconds=RETENTION_SECONDS,
                 claim_timeout=CLAIM_TIMEOUT_SECONDS, snapshot_timeout=SNAPSHOT_TIMEOUT_SECONDS,
                 restore_timeout=RESTORE_TIMEOUT_SECONDS, max_file_bytes=MAX_FILE_BYTES):
        from pathlib import Path
        self.record = record
        if store_dir is None:
            import atexit
            import shutil
            import tempfile
            store_dir = tempfile.mkdtemp(prefix="rollback-store-")
            atexit.register(shutil.rmtree, store_dir, True)
        self.store_dir = Path(store_dir)
        self.conference = conference
        if revert is None and hasattr(conference, "reviewer_revert"):
            def revert(agent_id, problem_id, reason):
                return conference.reviewer_revert(problem_id, reason, reviewed_agent=agent_id)
        self.revert = revert
        global _ACTIVE
        _ACTIVE = self
        self.conference_clock = conference_clock or getattr(conference, "_clock", None) or time.time
        self.base = base
        self.uid = uid
        self.python = python
        self.retention_seconds = retention_seconds
        self.claim_timeout = claim_timeout
        self.snapshot_timeout = snapshot_timeout
        self.restore_timeout = restore_timeout
        self.max_file_bytes = max_file_bytes
        self.agents: dict[str, _AgentState] = {}

    # ------------------------------------------------------------- in-container operations
    async def _run(self, st, spec, timeout):
        """Run rollback_fs.SCRIPT in the agent's container as the agent's own user (script on stdin)."""
        spec = {"base": self.base, "uid": self.uid, **spec}
        try:
            result = await st.sbox.exec([self.python, "-I", "-c", BOOTSTRAP],
                                        input=json.dumps({"s": FS_SCRIPT, "p": spec}), timeout=timeout)
        except Exception as ex:
            return {"ok": False, "error": f"exec:{type(ex).__name__}: {str(ex)[:200]}"}
        try:
            out = json.loads(result.stdout or "")
        except ValueError:
            return {"ok": False, "error": f"no_reply:{(result.stderr or '')[:300]}"}
        if not getattr(result, "success", False) and out.get("ok"):
            return {"ok": False, "error": f"exit:{getattr(result, 'returncode', None)}"}
        return out

    async def _scan(self, st, snapshot_id):
        """Scan the container and store snapshot ``snapshot_id``: (manifest, None) or (None, error)."""
        spec = {"op": "scan", "roots": list(ROOTS), "extra_globs": list(EXTRA_GLOBS), "skip_top": SKIP_TOP,
                "skip_names": list(SKIP_NAMES), "max_file": self.max_file_bytes, "max_scan_bytes": MAX_SCAN_BYTES,
                "max_entries": MAX_ENTRIES, "cache": st.store.cache}
        reply = await self._run(st, spec, self.snapshot_timeout)
        if not reply.get("ok"):
            return None, reply.get("error")
        try:
            return await asyncio.to_thread(st.store.ingest, snapshot_id, reply), None
        except Exception as ex:
            return None, f"ingest:{type(ex).__name__}: {str(ex)[:200]}"

    async def _apply(self, st, want, cur):
        plan, left = await asyncio.to_thread(st.store.restore_plan, want, cur)
        reply = await self._run(st, {"op": "apply", "plan": plan}, self.restore_timeout)
        if reply.get("ok"):
            reply["left_large"] = left
        return reply

    def _next_id(self, st, prefix="r"):
        st.counter += 1
        return f"{prefix}{st.counter:06d}"

    async def setup_agent(self, agent_id, sbox):
        """At sample start (before the first launch, or any time before the first request): the
        agent's store and an initial snapshot. Returns {"ok", "error"?}; on failure the agent's
        rollbacks fail cleanly and the agent runs unchanged."""
        try:
            store = await asyncio.to_thread(FileStore, self.store_dir / agent_id)
        except Exception as ex:
            error = f"store:{type(ex).__name__}"
            self.record("rollback_setup_error", agent_id, {"error": error})
            return {"ok": False, "error": error}
        st = self.agents[agent_id] = _AgentState(sbox, store)
        async with st.lock:
            manifest, error = await self._scan(st, "initial")
        if manifest is None:
            self.record("rollback_setup_error", agent_id, {"error": error})
            return {"ok": False, "error": error}
        st.ready = True
        self.record("rollback_setup", agent_id, {"version": ROLLBACK_VERSION, "entries": len(manifest)})
        return {"ok": True}

    async def _snapshot(self, agent_id, st, snapshot_id):
        async with st.lock:
            began = time.monotonic()
            manifest, error = await self._scan(st, snapshot_id)
            st.snapshots += 1
            if manifest is None:
                st.snapshot_errors += 1
                if st.snapshot_errors == 1 or st.snapshot_errors % 20 == 0:
                    self.record("rollback_snapshot_error", agent_id, {
                        "error": error, "errors": st.snapshot_errors, "snapshots": st.snapshots,
                        "seconds": round(time.monotonic() - began, 3)})
                return False
            if st.snapshots % PRUNE_EVERY == 0:
                await self._prune(agent_id, st, snapshot_id)
            return True

    async def _prune(self, agent_id, st, latest):
        horizon = time.time() - self.retention_seconds
        st.marks = {k: m for k, m in st.marks.items() if m.wall_time >= horizon}
        keep = {m.snapshot_id for m in st.marks.values() if m.snapshot_id}
        keep.update(s for s, (_, at) in st.store.snaps.items() if at >= horizon)
        keep.add(latest)
        try:
            await asyncio.to_thread(st.store.prune, keep)
        except Exception as ex:
            self.record("rollback_prune_error", agent_id, {"error": type(ex).__name__})

    # ------------------------------------------------------------- bridge filter hooks
    async def before_request(self, agent_id, messages, tools=None):
        """Called by the bridge filter for every request of ``agent_id``, after the deterministic
        rewrites (neutralize, token counters) and before monitor/push/admission. Executes a pending
        rollback when this request holds its cut, applies every splice, and (requests with tools)
        starts this request's file snapshot. Returns (messages, info); info["token"] goes to
        ``after_response``. Never raises."""
        st = self.agents.get(agent_id)
        info = {"token": None, "splices": []}
        if st is None:
            return messages, info
        try:
            pending = st.pending
            if pending is not None and not pending.claimed and cut_index(messages, pending.cut_id) is not None:
                pending.claimed = True
                outcome = await self._execute(agent_id, st, messages, pending)
                st.pending = None
                if not pending.future.done():
                    pending.future.set_result(outcome)
            if st.splices:
                messages, statuses = apply_splices(messages, st.splices)
                info["splices"] = statuses
                for splice, status in zip(st.splices, statuses):
                    if status == "absent" and tools and not splice.absent_recorded:
                        splice.absent_recorded = True
                        self.record("rollback_splice_absent", agent_id, {"cut_tool_use_id": splice.cut_id})
                    elif status not in ("applied", "absent"):
                        self.record("rollback_splice_error", agent_id, {"cut_tool_use_id": splice.cut_id,
                                                                        "status": status})
            if tools and st.ready:
                snapshot_id = self._next_id(st)
                task = asyncio.ensure_future(self._snapshot(agent_id, st, snapshot_id))
                task.add_done_callback(_consume)
                token = uuid.uuid4().hex
                st.tokens[token] = (task, snapshot_id, self.conference_clock(), time.time())
                info["token"] = token
        except Exception as ex:  # never disturbs the request
            self.record("rollback_filter_error", agent_id, {"error": f"{type(ex).__name__}: {str(ex)[:300]}"})
        return messages, info

    async def after_response(self, agent_id, token, output):
        """Called by the bridge filter after generation, before the response returns to the CLI (so
        no tool of this response runs before its snapshot is complete). Keys the response's tool
        calls to the request's snapshot. Call it in a ``finally`` (``output`` None when generation
        failed or the request was refused): the token is always released. Never raises."""
        st = self.agents.get(agent_id)
        if st is None or token is None:
            return
        entry = st.tokens.pop(token, None)
        if entry is None:
            return
        task, snapshot_id, conference_time, wall_time = entry
        try:
            ok = await asyncio.wait_for(asyncio.shield(task), self.snapshot_timeout + 30)
        except Exception:
            ok = False
        try:
            calls = (output.message.tool_calls or []) if output is not None and output.choices else []
        except Exception:
            calls = []
        for call in calls:
            st.marks[call.id] = _Mark(snapshot_id if ok else None, conference_time, wall_time)

    def release(self, agent_id, token):
        """Forget a request token whose response never came (generation failed or was refused)."""
        st = self.agents.get(agent_id)
        if st is not None and token is not None:
            st.tokens.pop(token, None)

    # ------------------------------------------------------------- rollback
    async def apply(self, context, agent_id, cut_tool_use_id, note):
        """review_live call site (``LiveReviewer(rollback=manager.apply)``; context unused)."""
        return await self.apply_rollback(agent_id, cut_tool_use_id, note)

    async def apply_rollback(self, agent_id, cut_tool_use_id, note=None, timeout=None):
        """Roll ``agent_id`` back to just before tool call ``cut_tool_use_id``; ``note`` (A1) is the
        exact user message shown at the cut, None (A2) shows nothing. Waits for the agent's next model
        request (at most ``timeout``, default CLAIM_TIMEOUT_SECONDS). Returns the record for a
        ``review_rollback`` event; on any failure ``ok`` is False and the agent is unchanged."""
        base = {"agent": agent_id, "cut_tool_use_id": cut_tool_use_id, "version": ROLLBACK_VERSION,
                "note_shown": bool(note), "note": note or None, "n_calls_undone": 0, "n_messages_undone": 0,
                "reverted_submission_ids": [], "files_restored": None}

        def failed(error):
            return {**base, "ok": False, "error": error}
        st = self.agents.get(agent_id)
        if st is None or not st.ready:
            return failed("rollback_not_set_up")
        mark = st.marks.get(cut_tool_use_id)
        if mark is None:
            return failed("cut_call_unknown")
        if mark.snapshot_id is None:
            return failed("no_snapshot_for_cut_call")
        if st.pending is not None:
            return failed("rollback_already_pending")
        pending = _Pending(cut_tool_use_id, note, asyncio.get_running_loop().create_future(),
                           requested_at=time.monotonic())
        st.pending = pending
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout or self.claim_timeout)
        except asyncio.TimeoutError:
            if not pending.claimed:
                st.pending = None
                return failed("no_request_before_timeout")
            return await pending.future  # executing now: its outcome
        except asyncio.CancelledError:
            if not pending.claimed and st.pending is pending:
                st.pending = None
            raise

    async def _execute(self, agent_id, st, messages, pending):
        """The rollback, at a request boundary. Order: plan (no side effects), restore files, revert
        submissions (files put back if none could be reverted), activate the splice."""
        began = time.monotonic()
        base = {"agent": agent_id, "cut_tool_use_id": pending.cut_id, "version": ROLLBACK_VERSION,
                "note_shown": bool(pending.note), "note": pending.note or None, "n_calls_undone": 0,
                "n_messages_undone": 0, "reverted_submission_ids": [], "files_restored": None,
                "waited_seconds": round(began - pending.requested_at, 3)}

        def failed(error, **extra):
            return {**base, **extra, "ok": False, "error": error, "seconds": round(time.monotonic() - began, 3)}

        async def put_back(pre):
            now, error = await self._scan(st, self._next_id(st, "undo"))
            if now is None:
                return False
            return bool((await self._apply(st, pre, now)).get("ok"))
        try:
            mark = st.marks.get(pending.cut_id)
            if mark is None or mark.snapshot_id is None or mark.snapshot_id not in st.store.snaps:
                return failed("no_snapshot_for_cut_call")
            splice, error = capture_splice(messages, pending.cut_id, pending.note)
            if splice is None:
                return failed(error)
            base.update(n_calls_undone=splice.n_calls, n_messages_undone=splice.length,
                        snapshot_id=mark.snapshot_id)
            submissions, library = [], []
            database = getattr(self.conference, "database_path", None)
            if database is not None:
                submissions = await asyncio.to_thread(accepted_submissions_since, database, agent_id,
                                                      mark.conference_time)
                library = await asyncio.to_thread(library_entries_since, database, agent_id, mark.conference_time)
            base["library_entries_kept"] = library
            if submissions and self.revert is None:
                return failed("no_revert_function", submissions_found=submissions)
            async with st.lock:  # waits for a snapshot in progress
                want = await asyncio.to_thread(st.store.manifest, mark.snapshot_id)
                pre, error = await self._scan(st, self._next_id(st, "pre"))
                if pre is None:
                    return failed(f"restore_failed:scan:{error}")
                applied = await self._apply(st, want, pre)
                if not applied.get("ok"):
                    return failed(f"restore_failed:{applied.get('error')}", files_put_back=await put_back(pre))
            base["files_restored"] = {k: applied.get(k) for k in ("written", "deleted", "dirs_created", "links",
                                                                   "left_large", "seconds")}
            reverted, revert_errors = [], []
            reason = f"Rollback of {agent_id} to before tool call {pending.cut_id}"
            for item in submissions:
                try:
                    reply = await asyncio.to_thread(self.revert, agent_id, item["problem_id"], reason)
                    if reply is None or (isinstance(reply, dict) and reply.get("ok") is False):
                        raise RuntimeError(str((reply or {}).get("error") or "nothing_to_revert")[:200])
                    reverted.append(item["submission_id"])
                except Exception as ex:
                    revert_errors.append({"problem_id": item["problem_id"],
                                          "error": f"{type(ex).__name__}: {ex}"[:300]})
            if revert_errors and not reverted:
                async with st.lock:  # nothing reverted: the files go back, the agent stays unchanged
                    restored_back = await put_back(pre)
                return failed("revert_failed", revert_errors=revert_errors, files_put_back=restored_back)
            st.splices.append(splice)
            return {**base, "ok": True, "error": None, "reverted_submission_ids": reverted,
                    "revert_errors": revert_errors, "seconds": round(time.monotonic() - began, 3)}
        except Exception as ex:
            return failed(f"{type(ex).__name__}: {str(ex)[:300]}")


# The manager of the running process (the last one built): review_live.resolve_rollback() finds the
# module-level ``apply`` below when the reviewer is built without an explicit ``rollback``.
_ACTIVE = None


async def apply(context, agent_id, cut_tool_use_id, note) -> dict:
    """review_live call site: roll ``agent_id`` back to before ``cut_tool_use_id`` with the run's
    RollbackManager (``context.extra["rollback_manager"]`` if given, else the active one)."""
    manager = (getattr(context, "extra", None) or {}).get("rollback_manager") or _ACTIVE
    if manager is None:
        return {"ok": False, "error": "rollback_not_set_up", "agent": agent_id, "cut_tool_use_id": cut_tool_use_id}
    return await manager.apply_rollback(agent_id, cut_tool_use_id, note)
