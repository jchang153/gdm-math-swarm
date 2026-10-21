"""Live behavior judge: Haiku screening of final transcript windows while a run is going.

Researcher-only, host-side only (docs/plan.md, "Live behavior judge during the run (October 20,
2026)"; the owner: "Judge during the run, not after"). Nothing here may enter an agent image,
container, workspace, prompt or the conference library; agent containers have no host mounts and
no network, and this module only reads the run's records (every database read-only).

What it does, once per pass (``scripts/run_transcript_judge.py --live``, started every few minutes
by ``scripts/supervise_run.py`` while the pilot runs):

1. reads each agent's Inspect sample as it stands now: from Inspect's realtime sample buffer (the
   SQLite buffer ``log_realtime=True`` keeps beside the ``.eval``; Inspect's own crash-recovery
   reader ``reconstruct_events`` plus ``resolve_events_attachments``), or from the ``.eval`` once
   the sample was flushed there (``read_eval_log_sample``, as the post-run judge reads it); then
   ``runtime.jsonl`` (complete lines only) and the conference database (``mode=ro`` only);
2. rebuilds the agent's history with the unchanged ``history.build_agent`` (Claude Code's own
   session files are never used: ``history.SESSION_TRANSCRIPT``);
3. keeps the longest prefix of items that can no longer change (``final_prefix``, below) and its
   closed windows (``make_windows(prefix)[:-1]``: windows are packed greedily left to right, so
   every window but the open last one of a prefix is identical in any longer history);
4. sends each such window's screening request, built by the post-run judge's own builder
   (``pipeline.prefilter_request``; the ``prefilter_window_count`` setting must be False, so the
   header names the window's id and index but not the agent's final window count), through the
   post-run judge's own unit flow (``Pipeline.unit``: repair, refusal rule, salvage, ledger, run
   limit), and keeps the unit in the warm store ``<judge dir>/live/`` under the hash of its
   request (warm.py). The post-run judge adopts a unit only for a byte-identical request.

Which items are final (``turn_status``, ``horizon``, ``final_prefix``). A model turn's items are
sorted at its completion time; the turn is settled when its event is complete, the next model
event exists and is complete (it decides whether the turn was a compaction summary, ``.s``, and
carries its tool results), and every tool call of the turn has an observed result, unless the
output was discarded (a following request at least as long carried none of its ids) or the agent's
session was restarted after it (``agent_restart``). The horizon is the earliest sort time of any
unsettled turn and at most ``now - margin`` (default 120 s: buffer, snapshot and conference write
lag and twice the conference join slack); the prefix ends before the first item at or after the
horizon, and before a conference tool result whose join window ends within twice
``history.JOIN_SLACK`` of it. A wrong guess only costs money: a window whose request differs
post-run is never adopted (warm.py) and is sent again.

Pass records: ``live/state.json`` (``LIVE_STATE_SCHEMA``: every pass, per agent the horizon, the
prefix, the final windows, what was sent, cached, deferred or failed, the SHA-256 chain of the
final prefix and any prefix violation). An agent whose earlier final prefix or window changed is
marked ``mispredicted``; from then on its windows are sent only after two passes saw the same
request (a cost guard; correctness never depends on it). Agents whose buffer did not change since
a pass limited by an unsettled turn are skipped.
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from . import history as hist
from .history import JOIN_SLACK, _epoch
from .windows import make_windows

LIVE_STATE_SCHEMA = "gdm-judge-live-state/1"
INSPECT_VERSION = "0.3.279"   # the buffer reader uses Inspect internals of exactly this version
DEFAULT_MARGIN_SECONDS = 120.0
DISCARD_LOOKAHEAD = 3
MAX_FAILED_ATTEMPTS = 2        # a window whose live unit failed this often is left to the post-run judge
TURN_KINDS = ("reasoning", "text", "summary", "tool_call", "tool_result")
LOCK_NAME = ".judge.lock"
RUN_LOCKS_DIRNAME = ".judge-run-locks"   # beside the ledger: one lock per (ledger, run id)
STATE_PASSES_KEPT = 200


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Locking


class JudgeBusy(RuntimeError):
    """Another judging process holds the run's lock or the judge directory's lock."""


def run_lock_path(ledger, run_id):
    """The run's judging lock: ``<ledger folder>/.judge-run-locks/<ledger name>-<run id>.lock``, held
    by every spending judging invocation of the run in that ledger whatever its ``--output-dir`` (and
    whichever copy of the run directory it judges), so two of them never read a judging run's
    committed spend (``client.committed_for_judging``) and spend at once. A judge directory's paid
    spend so far is read from every ledger it was counted in (``run_transcript_judge.judging_ledgers``), and its
    ``.judge.lock`` (``judge_lock``) also keeps two processes out of one judge directory."""
    ledger = Path(ledger).resolve()
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{ledger.stem}-{run_id}")
    return ledger.parent / RUN_LOCKS_DIRNAME / f"{name}.lock"


@contextmanager
def judging_locks(judge_dir, *, ledger, run_id, wait_seconds=0.0):
    """Every spending judging invocation of a run (each live pass, the early stages, the post-run
    judge, a manual re-judge into another ``--output-dir``) holds the run's lock (``run_lock_path``)
    for its whole life, then its judge directory's (``judge_lock``), so the judging run's committed
    spend read from the ledger before the first request is never raced. Raises ``JudgeBusy`` when either is not
    free within ``wait_seconds`` (0: at once)."""
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    with _flock(run_lock_path(ledger, run_id), wait_seconds):
        with judge_lock(judge_dir, wait_seconds=max(0.0, deadline - time.monotonic())):
            yield


@contextmanager
def judge_lock(judge_root, *, wait_seconds=0.0):
    """Exclusive lock of one judge directory (``<judge root>/.judge.lock``, ``fcntl.flock``): no two
    processes write one judge directory. ``wait_seconds`` 0 fails at once (``JudgeBusy``)."""
    with _flock(Path(judge_root) / LOCK_NAME, wait_seconds) as path:
        yield path


@contextmanager
def _flock(path, wait_seconds=0.0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JudgeBusy(f"{path} is held by another judging process") from None
                time.sleep(1.0)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "at": _now()}) + "\n")
        handle.flush()
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# ---------------------------------------------------------------------------
# Inspect's realtime buffer


class LiveSourceUnavailable(RuntimeError):
    pass


def inspect_gate():
    """None when the live reader can run, else why not (the live pass then does nothing and the
    post-run judge does all the work): inspect_ai must be exactly ``INSPECT_VERSION`` with its
    buffer and crash-recovery API present."""
    try:
        hist._ensure_inspect()
        import inspect_ai
    except Exception as error:
        return f"inspect_ai unavailable ({type(error).__name__})"
    if getattr(inspect_ai, "__version__", None) != INSPECT_VERSION:
        return f"inspect_ai {getattr(inspect_ai, '__version__', '?')} (the live reader is pinned to {INSPECT_VERSION})"
    try:
        from inspect_ai.log._condense import resolve_events_attachments  # noqa: F401
        from inspect_ai.log._recorders.buffer.database import SampleBufferDatabase  # noqa: F401
        from inspect_ai.log._recover._reconstruct import reconstruct_events  # noqa: F401
    except Exception as error:
        return f"Inspect buffer API unavailable ({type(error).__name__}: {error})"
    return None


class LiveSource:
    """Each agent's sample as it stands now: the realtime buffer (read-only), or the ``.eval``
    once Inspect flushed the completed sample there. ``db_dir`` overrides Inspect's buffer
    directory (tests)."""

    def __init__(self, run_dir, *, db_dir=None):
        self.run_dir = Path(run_dir)
        self.db_dir = Path(db_dir) if db_dir else None
        self.logs = sorted(self.run_dir.glob("inspect/*.eval"))
        self.problems = []
        self._buffers = {}
        self._aliases = []

    def _locations(self, log):
        out = []
        for location in (os.path.abspath(str(log)), str(Path(log).resolve())):
            if location not in out:
                out.append(location)
        return out

    def buffer(self, log):
        if log in self._buffers:
            return self._buffers[log]
        from inspect_ai.log._recorders.buffer.database import SampleBufferDatabase
        found = None
        for location in self._locations(log):
            try:
                found = SampleBufferDatabase(location, create=False, read_only=True, db_dir=self.db_dir)
                break
            except (FileNotFoundError, OSError, sqlite3.Error, ValueError):
                continue
        if found is None:
            found = self._buffer_by_name(log)
        self._buffers[log] = found
        return found

    def _buffer_by_name(self, log):
        """The buffer of ``log`` when the pilot named the log directory by another path (a symlink):
        Inspect keys the buffer directory by a hash of the log directory's path, so find the one
        buffer directory holding ``<log file name>.<pid>.db`` and open it through an alias
        directory whose name is the hash of this path (a directory symlink, so SQLite finds the
        database's -wal and -shm files beside it)."""
        import tempfile
        from inspect_ai._util.file import filesystem
        from inspect_ai.log._recorders.buffer.database import (SampleBufferDatabase, location_dir_and_file,
                                                               resolve_db_dir)
        try:
            folders = {path.parent for path in resolve_db_dir(self.db_dir).glob(f"*/{Path(log).name}.*.db")}
        except OSError:
            return None
        if len(folders) != 1:
            return None
        location = os.path.abspath(str(log))
        hashed, _ = location_dir_and_file(filesystem(location).path_as_uri(location))
        alias = Path(tempfile.mkdtemp(prefix="judge-live-buffer-"))
        self._aliases.append(alias)
        (alias / hashed).symlink_to(next(iter(folders)), target_is_directory=True)
        try:
            return SampleBufferDatabase(location, create=False, read_only=True, db_dir=alias)
        except (FileNotFoundError, OSError, sqlite3.Error, ValueError):
            return None

    def close(self):
        import shutil
        for db in self._buffers.values():
            if db is not None:
                try:
                    db.close()   # closes connections only; never deletes the buffer (that is cleanup())
                except Exception:
                    pass
        self._buffers = {}
        for alias in self._aliases:   # the alias directories hold symlinks only
            shutil.rmtree(alias, ignore_errors=True)
        self._aliases = []

    def index(self):
        """``{agent: {"log", "id", "epoch", "buffered", "flushed"}}`` over every ``.eval``."""
        read_sample, summaries = hist._ensure_inspect()
        found = {}
        for log in self.logs:
            db = self.buffer(log)
            if db is not None:
                try:
                    listed = db.get_samples()
                    for summary in getattr(listed, "samples", None) or []:
                        entry = found.setdefault(str(summary.id), {"log": log, "id": summary.id,
                                                                   "epoch": summary.epoch, "buffered": False,
                                                                   "flushed": False})
                        entry["buffered"] = True
                except Exception as error:
                    self.problems.append({"log": log.name, "error": f"buffer listing: {type(error).__name__}"})
            try:
                for summary in summaries(str(log)):
                    entry = found.setdefault(str(summary.id), {"log": log, "id": summary.id, "epoch": summary.epoch,
                                                               "buffered": False, "flushed": False})
                    entry.update(log=log, id=summary.id, epoch=summary.epoch, flushed=True)
            except Exception as error:   # a log being written: its flushed samples are read next pass
                self.problems.append({"log": log.name, "error": f"eval summaries: {type(error).__name__}"})
        return {agent: entry for agent, entry in sorted(found.items()) if hist.AGENT_ID.fullmatch(agent)}

    def change_marker(self, entry):
        """A value that changes whenever the agent's sample gains or updates an event (rows of the
        buffer's events table, updates included); ``"flushed"`` once the sample is in the .eval."""
        if entry.get("flushed"):
            return "flushed"
        db = self.buffer(entry["log"])
        if db is None:
            return None
        try:
            with sqlite3.connect(Path(db.db_path).as_uri() + "?mode=ro", uri=True, timeout=30) as conn:
                row = conn.execute("SELECT COUNT(*), MAX(id) FROM events WHERE sample_id=? AND sample_epoch=?",
                                   (str(entry["id"]), entry["epoch"])).fetchone()
            return f"{row[0]}:{row[1]}"
        except sqlite3.Error:
            return None

    def read(self, entry):
        """``(sample, complete)``: a flushed sample read like the post-run judge reads it (complete:
        its messages give the last turn's tool results), else the buffered events resolved by
        Inspect's crash-recovery reader with ``messages=[]`` (in progress)."""
        read_sample, _ = hist._ensure_inspect()
        if entry.get("flushed"):
            try:
                return read_sample(str(entry["log"]), id=entry["id"], epoch=entry["epoch"],
                                   resolve_attachments=True), True
            except Exception as error:
                if not entry.get("buffered"):
                    raise LiveSourceUnavailable(f"flushed sample unreadable: {type(error).__name__}") from None
        from inspect_ai.log._condense import resolve_events_attachments
        from inspect_ai.log._recover._reconstruct import reconstruct_events
        db = self.buffer(entry["log"])
        if db is None:
            raise LiveSourceUnavailable("no realtime buffer for " + Path(entry["log"]).name)
        data = db.get_sample_data(entry["id"], entry["epoch"])
        if data is None:
            raise LiveSourceUnavailable("sample no longer in the buffer (flushed; read next pass)")
        events = reconstruct_events(data)
        events = resolve_events_attachments(events, {a.hash: a.content for a in data.attachments}, "full")
        return SimpleNamespace(id=entry["id"], epoch=entry["epoch"], events=events, messages=[], error=None), False


# ---------------------------------------------------------------------------
# Which items are final


def _message(event):
    output = getattr(event, "output", None)
    choices = getattr(output, "choices", None) or []
    return getattr(choices[0], "message", None) if choices else None


def _input_ids(event):
    ids = set()
    for message in getattr(event, "input", None) or []:
        ids.add(getattr(message, "id", None))
        ids.add(getattr(message, "tool_call_id", None))
        for call in getattr(message, "tool_calls", None) or []:
            ids.add(getattr(call, "id", None))
    ids.discard(None)
    return ids


def turn_status(sample, *, complete=False, restarts=()):
    """Per model turn (position among the sample's model events, as ``history`` numbers turns):
    ``{"turn", "key", "settled", "reason"}``. ``key`` is the turn block's sort time (its
    completion, else its start, as ``history.reconstruct_sample`` sorts it). ``restarts``: epoch
    times of this agent's ``agent_restart`` records (a session restarted after a turn never
    delivers that turn's missing tool results)."""
    events = list(getattr(sample, "events", None) or [])
    positions = [i for i, e in enumerate(events) if getattr(e, "event", None) == "model"]
    observed = []          # per turn: tool_call ids carried by its input
    for position in positions:
        observed.append({getattr(m, "tool_call_id", None) for m in getattr(events[position], "input", None) or []
                         if getattr(m, "role", None) == "tool"})
    out = []
    for turn, position in enumerate(positions):
        event = events[position]
        started = _epoch(getattr(event, "timestamp", None))
        completed = _epoch(getattr(event, "completed", None)) or started
        key = completed if completed is not None else float("-inf")
        row = {"turn": turn, "key": key, "settled": True, "reason": None}
        out.append(row)
        if complete:
            continue
        if getattr(event, "pending", False):
            row.update(settled=False, reason="pending")
            continue
        if turn + 1 >= len(positions) or getattr(events[positions[turn + 1]], "pending", False):
            row.update(settled=False, reason="awaiting_next_turn")
            continue
        message = _message(event)
        calls = [getattr(c, "id", None) for c in getattr(message, "tool_calls", None) or []] if message else []
        if not calls:
            continue
        seen = set().union(*observed[turn + 1:]) if turn + 1 < len(observed) else set()
        if all(c in seen for c in calls):
            continue
        if any(when is not None and when > key for when in restarts):
            row["reason"] = "session_restarted"
            continue
        own = set(calls) | {getattr(message, "id", None)}
        length = len(getattr(event, "input", None) or [])
        later = [events[p] for p in positions[turn + 1:turn + 1 + DISCARD_LOOKAHEAD]
                 if not getattr(events[p], "pending", False)]
        if any(len(getattr(e, "input", None) or []) >= length and not (own & _input_ids(e)) for e in later):
            row["reason"] = "discarded"
            continue
        row.update(settled=False, reason="awaiting_tool_results")
    return out


def horizon(turns, now, margin):
    """``(horizon, limited_by)``: the earliest sort time of an unsettled turn, at most
    ``now - margin``."""
    bound, limited = float(now) - float(margin), "margin"
    for row in turns:
        if not row["settled"] and row["key"] < bound:
            bound, limited = row["key"], f"turn {row['turn']}: {row['reason']}"
    return bound, limited


def item_key(item):
    """An item's sort time as ``history.build_agent`` sorted it (a tool result sits in its turn's
    block, at the turn's completion, the lower bound of its time window)."""
    if item.get("kind") == "tool_result":
        bounds = item.get("time_bounds") or [None, None]
        value = _epoch(bounds[0])
    else:
        value = _epoch(item.get("time"))
    return float("-inf") if value is None else value


def final_prefix(items, bound):
    """The longest prefix of ``items`` sorted before ``bound`` whose conference joins are settled
    (a conference tool result's join window must end at least twice ``JOIN_SLACK`` before it)."""
    out = []
    for item in items:
        if item_key(item) >= bound:
            break
        if item.get("kind") == "tool_result" and (item.get("meta") or {}).get("conference") is not None:
            high = _epoch((item.get("time_bounds") or [None, None])[1])
            if high is None or high + 2 * JOIN_SLACK >= bound:
                break
        out.append(item)
    return out


def item_digest(item):
    """What a screening request shows of one item (id, kind, channel, time and text)."""
    from .prompts import _time
    shown = [item.get("id"), item.get("kind"), item.get("channel"), _time(item),
             hashlib.sha256((item.get("text") or "").encode("utf-8")).hexdigest()]
    return hashlib.sha256(json.dumps(shown, ensure_ascii=False).encode("utf-8")).hexdigest()


def prefix_chain(items):
    """SHA-256 chain over ``item_digest`` of each item: entry n hashes the first n items."""
    chain, current = [], hashlib.sha256(b"gdm-judge-live-prefix/1").hexdigest()
    for item in items:
        current = hashlib.sha256((current + item_digest(item)).encode("utf-8")).hexdigest()
        chain.append(current)
    return chain


def final_windows(history, sample, *, complete, now, margin, window_tokens, restarts=()):
    """``(windows, prefix, chain, record)``: the agent's windows that can no longer change, the
    final prefix they cover, its SHA-256 chain (``prefix_chain``) and how they were found (horizon,
    its reason, item and turn counts)."""
    turns = turn_status(sample, complete=complete, restarts=restarts)
    bound, limited = horizon(turns, now, margin)
    items = list(history.items)
    prefix = final_prefix(items, bound)
    windows = make_windows(prefix, window_tokens)[:-1] if prefix else []
    chain = prefix_chain(prefix)
    record = {"complete": complete, "horizon": hist.iso(bound) if bound != float("-inf") else None,
              "limited_by": limited, "items": len(items), "prefix_items": len(prefix),
              "turns": len(turns), "unsettled_turns": sum(1 for t in turns if not t["settled"]),
              "windows_final": len(windows), "chain": {"items": len(prefix), "sha256": chain[-1] if chain else None}}
    return windows, prefix, chain, record


# ---------------------------------------------------------------------------
# One live pass


class LivePass:
    """One pass over every agent: find the final windows (``final_windows``) and screen the new
    ones through ``pipeline`` (a ``Pipeline`` whose ``judge_dir`` is the warm store, mode, clients,
    run limit and settings as the post-run judge; ``prefilter_window_count`` must be False)."""

    def __init__(self, run_dir, pipeline, *, source=None, margin=DEFAULT_MARGIN_SECONDS, now=None, log=None,
                 agents=None, model_key="haiku"):
        if pipeline.settings.prefilter_window_count:
            raise ValueError("live screening needs prefilter_window_count False (the agent's final window count is "
                             "unknown until the run stops)")
        self.run_dir = Path(run_dir).resolve()
        self.pipeline = pipeline
        self.live_dir = Path(pipeline.judge_dir)
        self.source = source or LiveSource(self.run_dir)
        self.margin = float(margin)
        self.clock = now or time.time
        self.log = log or (lambda message: None)
        self.agents = list(agents) if agents else None
        self.model_key = model_key
        from .warm import adoption_code_hash
        self.code_hash = adoption_code_hash()
        self.state = _read(self.live_dir / "state.json") or {"schema": LIVE_STATE_SCHEMA, "agents": {},
                                                              "passes": []}
        self.state.setdefault("agents", {})
        self.state.setdefault("passes", [])

    # -- planning (synchronous: never inside the event loop) -------------------
    def plan(self):
        """``(requests, records)``: every final window still to screen, and per agent what was found."""
        requests, records = [], {}
        index = self.source.index()
        chosen = [a for a in index if self.agents is None or a in self.agents]
        for agent in chosen:
            entry = index[agent]
            memory = self.state["agents"].setdefault(agent, {})
            marker = self.source.change_marker(entry)
            if (marker is not None and marker == memory.get("marker") and memory.get("limited_by") != "margin"
                    and not memory.get("retry_next_pass")):
                records[agent] = {"skipped": "unchanged since the last pass"}
                continue
            now = float(self.clock())
            try:
                sample, complete = self.source.read(entry)
            except LiveSourceUnavailable as error:
                records[agent] = {"skipped": str(error)}
                continue
            except Exception as error:   # e.g. a .eval being rewritten: read next pass
                records[agent] = {"skipped": f"read failed: {type(error).__name__}"}
                continue
            base = hist.load_run_base(self.run_dir, live=True)   # read after the sample (lag-safe)
            restarts = [_epoch(r.get("timestamp")) for r in base.runtime
                        if r.get("kind") == "agent_restart" and r.get("agent_id") == agent]
            log_name = str(Path(entry["log"]).relative_to(self.run_dir)) \
                if Path(entry["log"]).is_relative_to(self.run_dir) else str(entry["log"])
            history = hist.build_agent(sample, base, log_name=log_name)
            windows, prefix, chain, record = final_windows(
                history, sample, complete=complete, now=now, margin=self.margin,
                window_tokens=self.pipeline.settings.window_tokens, restarts=restarts)
            del sample, base
            record["marker"] = marker
            self._check_chain(agent, memory, chain, record)
            by_id = {item["id"]: item for item in prefix}
            previous = dict(memory.get("windows") or {})
            current, deferred, cached = {}, [], 0
            for request in window_requests(agent, history.code, windows, by_id, self.pipeline.settings,
                                           self.live_dir):
                window, digest = request["window"], request["sha"]
                current[window["id"]] = digest
                if previous.get(window["id"]) not in (None, digest):
                    memory["mispredicted"] = True
                    record.setdefault("changed_windows", []).append(window["id"])
                unit = _read(request["path"])
                if isinstance(unit, dict) and unit.get("status") in ("ok", "ungraded"):
                    cached += 1
                    continue
                if memory.get("mispredicted") and previous.get(window["id"]) != digest:
                    deferred.append(window["id"])   # cost guard: sent once two passes agree
                    continue
                requests.append(request)
            memory.update(windows=dict(previous, **current), marker=marker, limited_by=record["limited_by"],
                          complete=complete, retry_next_pass=bool(deferred))
            record.update(cached=cached, deferred=deferred,
                          to_send=sum(1 for r in requests if r["agent"] == agent))
            records[agent] = record
            del history, prefix, by_id
        return requests, records

    def _check_chain(self, agent, memory, chain, record):
        """Prefix violation check: the first n items of this pass's final prefix must hash as the
        n items of the last pass's (a later item sorted before them, or a changed item, breaks it)."""
        earlier = memory.get("chain") or {}
        count, digest = earlier.get("items") or 0, earlier.get("sha256")
        if count and digest:
            if len(chain) < count or chain[count - 1] != digest:
                memory["mispredicted"] = True
                violation = {"at": _now(), "earlier_items": count, "items_now": len(chain)}
                memory.setdefault("violations", []).append(violation)
                record["prefix_violation"] = violation
        memory["chain"] = record["chain"]

    async def send(self, requests):
        return await send_requests(self.pipeline, requests, code_hash=self.code_hash, model_key=self.model_key)

    def run(self):
        """One pass: plan (no event loop), send, record ``live/state.json``. Returns the pass record."""
        import asyncio
        from .pipeline import write_json
        started = _now()
        try:
            requests, records = self.plan()
        finally:
            problems = list(self.source.problems)
            self.source.close()
        outcomes, stopped = asyncio.run(self.send(requests)) if requests else ({}, None)
        for agent, outcome in outcomes.items():
            records.setdefault(agent, {})["outcome"] = outcome
        record = {"started_at": started, "finished_at": _now(), "margin_seconds": self.margin,
                  "agents": records, "requests_planned": len(requests),
                  "windows_final": sum(r.get("windows_final", 0) for r in records.values()),
                  "windows_cached": sum(r.get("cached", 0) for r in records.values()),
                  "windows_deferred": sum(len(r.get("deferred") or []) for r in records.values()),
                  "stopped": stopped, "source_problems": problems, "code_sha256": self.code_hash,
                  "run_limit": self.pipeline.run_limit.record() if self.pipeline.run_limit else None}
        self.state["passes"] = (self.state["passes"] + [record])[-STATE_PASSES_KEPT:]
        self.state.update(schema=LIVE_STATE_SCHEMA, run_id=self.run_dir.name, updated_at=_now())
        write_json(self.live_dir / "state.json", self.state)
        return record


def window_requests(agent_id, code, windows, items_by_id, settings, live_dir):
    """The screening request of each window, built by the post-run judge's own builder
    (``pipeline.prefilter_request``; no window count), with its hash and its live unit path."""
    from .pipeline import prefilter_request, sha256_text
    from .warm import live_unit_path
    out = []
    for window in windows:
        system, user = prefilter_request(agent_id, code, window, items_by_id, settings)
        digest = sha256_text(system + "\0" + user)
        out.append({"agent": agent_id, "window": window, "system": system, "user": user, "sha": digest,
                    "path": live_unit_path(live_dir, agent_id, window["id"], digest)})
    return out


async def send_requests(pipeline, requests, *, code_hash=None, model_key="haiku"):
    """Screen planned windows through the post-run judge's unit flow (``Pipeline.unit``: repair,
    refusal rule, salvage, ledger, run limit) into their content-addressed live unit files. Returns
    ``(per agent {"sent", "ok", "ungraded", "failed", "pending", "skipped_after_failures",
    "refused_left"}, stop reason or None)``. A ``grading_failed`` live unit (never adopted) is set aside as
    ``.failed-<time>`` and tried again, at most ``MAX_FAILED_ATTEMPTS`` times in all; a pending unit
    left by other judge code is set aside as ``.code-changed-<time>``. A unit whose request met a
    refusal is never set aside (a refused request is never resent identically, judge revision v3
    item 6): a pending one is continued (only its one retry is sent) and a failed one is left to
    the post-run judge."""
    import asyncio
    from .pipeline import BudgetStop, _safe, prefilter_unit_options, read_json, write_json
    from .warm import adoption_code_hash, refused_attempts
    code_hash = code_hash or adoption_code_hash()
    outcomes = {}
    if pipeline.run_limit is not None:
        pipeline._attach_run_limit()

    async def one(request):
        path, agent = Path(request["path"]), request["agent"]
        row = outcomes.setdefault(agent, {"sent": 0, "ok": 0, "ungraded": 0, "failed": 0, "pending": 0,
                                          "skipped_after_failures": 0, "refused_left": 0})
        existing = read_json(path) if path.exists() else None
        if isinstance(existing, dict) and existing.get("status") in ("ok", "ungraded"):
            return
        if isinstance(existing, dict) and refused_attempts(existing):
            if existing.get("status") != "pending":
                row["refused_left"] += 1   # never resent; the post-run judge decides from its record
                return
            if (existing.get("live") or {}).get("code_sha256") != code_hash:
                existing.setdefault("live", {})["continued_by_code_sha256"] = code_hash
                write_json(path, existing)
        elif isinstance(existing, dict) and (existing.get("status") == "grading_failed"
                                             or (existing.get("live") or {}).get("code_sha256") != code_hash):
            failures = len(list(path.parent.glob(path.name + ".failed-*")))
            if existing.get("status") == "grading_failed" and failures + 1 >= MAX_FAILED_ATTEMPTS:
                row["skipped_after_failures"] += 1
                return
            suffix = "failed" if existing.get("status") == "grading_failed" else "code-changed"
            path.rename(path.with_name(path.name + f".{suffix}-{_safe(_now())}"))
            existing = None
        if existing is None:
            unit = pipeline.new_unit(model_key, unit_id=request["window"]["id"], purpose="prefilter",
                                     system=request["system"], user=request["user"])
            unit["subjects"] = [request["window"]["id"]]
            unit["live"] = {"code_sha256": code_hash, "screened_at": _now(),
                            "window_items": len(request["window"]["item_ids"])}
            write_json(path, unit)
        row["sent"] += 1
        unit = await pipeline.unit(model_key, path, system=request["system"], user=request["user"],
                                   **prefilter_unit_options(request["window"], pipeline.settings))
        status = unit.get("status")
        row["ok" if status == "ok" else "ungraded" if status == "ungraded" else
            "failed" if status == "grading_failed" else "pending"] += 1

    results = await asyncio.gather(*(one(r) for r in requests), return_exceptions=True)
    stopped = None
    for result in results:
        if isinstance(result, BudgetStop):
            stopped = str(result)
        elif isinstance(result, BaseException):
            raise result
    return outcomes, stopped or (pipeline.stop or {}).get("reason")


def open_live_store(pipeline, route):
    """Write (or check) the live store's manifest: the post-run judge's binding (``check_manifest``
    refuses a store made with other binding settings) plus the route, whose provider pin must stay
    the same for the whole run. Raises ``pipeline.PipelineError``."""
    from .pipeline import PipelineError, read_json, write_json
    from .warm import route_pin
    pipeline.check_manifest()
    path = Path(pipeline.judge_dir) / "manifest.json"
    manifest = read_json(path) or {}
    if manifest.get("route") is None:
        write_json(path, dict(manifest, route=route))
    elif route_pin(manifest["route"]) != route_pin(route):
        raise PipelineError("the provider route changed since the first live pass of this run")
    return path


def record_unavailable(live_dir, reason, *, run_id=None, kind="live_source_unavailable"):
    """Note in live/state.json that a pass did nothing: the live source is unavailable, or (``kind``
    ``"deferred"``) no fresh valid pricing record or no provider key could be had just now."""
    from .pipeline import write_json
    live_dir = Path(live_dir)
    state = _read(live_dir / "state.json") or {"schema": LIVE_STATE_SCHEMA, "agents": {}, "passes": []}
    state.setdefault("passes", []).append({"started_at": _now(), "finished_at": _now(), kind: reason})
    state["passes"] = state["passes"][-STATE_PASSES_KEPT:]
    state.update(schema=LIVE_STATE_SCHEMA, run_id=run_id, updated_at=_now())
    write_json(live_dir / "state.json", state)
