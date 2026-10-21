"""Offline tests of the live behavior judge (docs/plan.md, "Live behavior judge during the run (October 20,
2026)"): research/analysis/judge/live.py and warm.py, the per-run cap and the command line.

No model calls, network, Docker or credentials; the authoritative ledger is never opened. Covered:

* windows: the closed windows of any prefix are the final windows (prefix property);
* finality: a synthetic agent (real Inspect events) with parallel tool calls, a discarded re-sent
  output, a compaction, an errored model event, a session that ended and was restarted (its last
  turn becomes a summary turn only once the next session starts), a session killed with a tool call
  unanswered (settled by its ``agent_restart`` record) and file versions, replayed at every second:
  every window declared final has the byte-identical post-run request; with the agent's window
  count in the header (the v3 bytes) they do not (negative control);
* Inspect's realtime buffer: the live reader (real ``SampleBufferDatabase``, pending and updated
  events, attachments) rebuilds the same items as the post-run reader of the ``.eval``;
* equivalence: live passes then the post-run judge give exactly the results, labels, run level and
  Sonnet/Opus requests of a post-run-only judge on the same transcripts, and no unit is paid twice
  (a mispredicted window is paid once more and never adopted);
* adoption guards (binding, route, code, status, replay) and ``live/consistency.json``;
* the per-run cap (ledger labels with the run id, orphaned holds, ``RunLimit`` prior, live sub-cap),
  the judge lock, the live-safe readers and the live config keys;
* a mock end to end: a live pass over a real buffer, then the post-run judge on the final ``.eval``.

Run: PYTHONPATH=src:. .venv/bin/python -m unittest discover -s research/analysis/tests
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import io
import json
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[2]
for path in (str(REPO), str(REPO / "src"), str(TESTS), str(REPO / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research_swarm.inspect_adapter import disable_inspect_dotenv  # noqa: E402

disable_inspect_dotenv()
from research.analysis.judge import history, live  # noqa: E402
from research.analysis.judge import pipeline as pl  # noqa: E402
from research.analysis.judge.client import (JudgeClient, RunLimit, committed_for_run,  # noqa: E402
                                            request_label)
from research.analysis.judge.history import make_item  # noqa: E402
from research.analysis.judge.windows import make_windows  # noqa: E402
from research_swarm import auto_judge  # noqa: E402
from research_swarm.budget import BudgetLedger  # noqa: E402

import test_judge_pipeline as tjp  # noqa: E402

T0 = 1792116900.0
MARGIN = 3.0
WINDOW_TOKENS = 160
OFF = pl.PipelineSettings(window_tokens=WINDOW_TOKENS, prefilter_window_count=False)
ON = pl.PipelineSettings(window_tokens=WINDOW_TOKENS)


def moment(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


def text_of(tag, n):
    return f"{tag}: " + " ".join(f"step {k} of the {tag.lower()} bounds the partial sums" for k in range(n))


# ---------------------------------------------------------------------------
# A synthetic agent with real Inspect events


def scenario(root):
    """(run_dir, final sample, runtime records) of one agent (agent-001). Turns (start, completion):
    t0 (10, 14) one call; t1 (20, 25) two parallel calls; t2 (30, 34); t3 (40, 44) a call whose
    output is discarded (t4 re-sends t3's request); t5 (60, 66) answers Claude Code's compaction
    request; t6 (70, 75) starts the next segment; t7 (80, -) errors without output; t8 (90, 96)
    retries it; t9 (100, 105) ends the session with a stop and no call (a summary turn once the
    session restarts at 110); t10 (120, 126) and t11 (130, 136), whose call is never answered (the
    session is killed and restarts at 145); t12 (150, 156) and t13 (160, 166), whose result is only
    in the final messages. File versions are captured at 12, 33, 57, 85, 128 and 158; the agent
    exits at 170 and the run stops at 175."""
    from inspect_ai.event import ModelEvent
    from inspect_ai.model import (ChatMessageAssistant, ChatMessageSystem, ChatMessageTool, ChatMessageUser,
                                  ContentReasoning, ContentText, GenerateConfig, ModelOutput)
    from inspect_ai.tool import ToolCall

    run = Path(root) / "runs" / "live-20990101T000000Z"
    (run / "inspect").mkdir(parents=True)
    system = ChatMessageSystem(id="s1", content="You are a research agent. Integrity rules apply.")
    users = {name: ChatMessageUser(id=f"u-{name}", content=content) for name, content in (
        ("begin", "Begin."), ("again", "Begin again (restarted)."), ("third", "Begin once more (restarted)."),
        ("compact", history.COMPACTION_REQUEST + " Summarize the conversation."),
        ("cont", history.CONTINUATION_MARK + " ... Summary: the lemma is half done."))}

    def out(n, calls=(), stop_text=None):
        content = [ContentReasoning(reasoning=text_of(f"R{n}", 6 + n % 4)),
                   ContentText(text=stop_text or text_of(f"X{n}", 3 + n % 3))]
        tool_calls = [ToolCall(id=c, function="Bash", arguments={"command": f"lake build {c}"}) for c in calls]
        return ChatMessageAssistant(id=f"o{n}", content=content, tool_calls=tool_calls or None)

    def res(call, long=False):
        body = ("x" * 1500 + "\n") if long else ""
        return ChatMessageTool(id=f"r-{call}", tool_call_id=call, function="Bash",
                               content=body + text_of(f"Output of {call}", 4))

    o = {0: out(0, ["c0"]), 1: out(1, ["c1a", "c1b"]), 2: out(2, ["c2"]), 3: out(3, ["c3"]), 4: out(4, ["c4"]),
         5: out(5, stop_text="<summary>The lemma is half done; c4 compiled.</summary>"), 6: out(6, ["c6"]),
         8: out(8, ["c8"]), 9: out(9, stop_text="Done for now: the lemma compiles."), 10: out(10, ["c10"]),
         11: out(11, ["c11"]), 12: out(12, ["c12"]), 13: out(13, ["c13"])}
    s, b = system, users["begin"]
    base2 = [s, b, o[0], res("c0"), o[1], res("c1a", long=True), res("c1b"), o[2], res("c2")]
    seg2 = [s, users["cont"], o[6], res("c6")]
    inputs = {0: [s, b], 1: [s, b, o[0], res("c0")], 2: [s, b, o[0], res("c0"), o[1], res("c1a", long=True),
                                                          res("c1b")],
              3: base2, 4: base2, 5: base2 + [o[4], res("c4"), users["compact"]], 6: [s, users["cont"]],
              7: seg2, 8: seg2, 9: seg2 + [o[8], res("c8")], 10: [s, users["again"]],
              11: [s, users["again"], o[10], res("c10")], 12: [s, users["third"]],
              13: [s, users["third"], o[12], res("c12")]}
    times = {0: (10, 14), 1: (20, 25), 2: (30, 34), 3: (40, 44), 4: (50, 55), 5: (60, 66), 6: (70, 75),
             7: (80, None), 8: (90, 96), 9: (100, 105), 10: (120, 126), 11: (130, 136), 12: (150, 156),
             13: (160, 166)}
    events = []
    for turn in range(14):
        start, done = times[turn]
        message = o.get(turn)
        stop = "stop" if turn in (5, 9) else "tool_calls"
        # An errored event keeps Inspect's placeholder output (one empty choice), as real ones do.
        output = ModelOutput.from_message(message, stop_reason=stop) if message else ModelOutput.from_content("m", "")
        events.append(ModelEvent(uuid=f"ev-{turn}", model="m", input=inputs[turn], tools=[], tool_choice="auto",
                                 config=GenerateConfig(), output=output, timestamp=moment(T0 + start),
                                 completed=None if done is None else moment(T0 + done),
                                 error="upstream HTTP 429" if turn == 7 else None))
    messages = [s, users["third"], o[12], res("c12"), o[13], res("c13")]
    sample = SimpleNamespace(id="agent-001", epoch=1, events=events, messages=messages, error=None)
    runtime = [{"timestamp": T0 - 5, "kind": "agent_configured", "agent_id": "agent-001", "payload": {"persona": "x"}},
               {"timestamp": T0 + 1, "kind": "common_start", "agent_id": None, "payload": {"agent_count": 1}}]
    for n, when in enumerate((12, 33, 57, 85, 128, 158), 1):
        content = f"# notes v{n}\n" + text_of(f"Notes {n}", 5) + "\n"
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        (run / "snapshots" / "agent-001").mkdir(parents=True, exist_ok=True)
        (run / "snapshots" / "agent-001" / digest).write_bytes(data)
        runtime.append({"timestamp": T0 + when, "kind": "file_snapshot", "agent_id": "agent-001",
                        "payload": {"path": "/workspace/NOTES.md", "sha256": digest, "blob_sha256": digest,
                                    "mode": "full", "bytes": len(data)}})
    runtime += [{"timestamp": T0 + 110, "kind": "agent_restart", "agent_id": "agent-001", "payload": {"restart": 1}},
                {"timestamp": T0 + 145, "kind": "agent_restart", "agent_id": "agent-001", "payload": {"restart": 2}},
                {"timestamp": T0 + 170, "kind": "agent_exit", "agent_id": "agent-001",
                 "payload": {"status": "max_run_seconds"}},
                {"timestamp": T0 + 175, "kind": "run_stop", "agent_id": None, "payload": {"reason": "max_run_seconds"}}]
    runtime.sort(key=lambda r: r["timestamp"])
    (run / "settings.json").write_text(json.dumps({"personas": {"agent-001": "x"}}))
    return run, sample, runtime


def run_at(run_dir, runtime):
    problems = []
    conference = history.ConferenceRecords()
    host = history.load_host(run_dir, runtime, conference, problems)
    return history.RunHistory(run_dir=Path(run_dir), run_id=Path(run_dir).name, agents={}, host=host,
                              conference=conference, runtime=list(runtime), problems=problems)


def pending_copy(event):
    """A model event as Inspect logs it before it completes: its input, a placeholder output, no
    completion time, ``pending`` True."""
    from inspect_ai.model import ModelOutput
    return event.model_copy(update={"pending": True, "output": ModelOutput.from_content("m", ""), "completed": None,
                                    "error": None})


def view_at(sample, runtime, t):
    """The live view at time t: model events started by t (still running ones pending), runtime
    records up to t, no final messages."""
    starts = [history._epoch(e.timestamp) for e in sample.events if getattr(e, "event", None) == "model"]
    events = []
    for event in sample.events:
        start = history._epoch(event.timestamp)
        if start is None or start > t:
            continue
        if getattr(event, "event", None) != "model":
            events.append(event)
            continue
        done = history._epoch(event.completed)
        later = any(start < other <= t for other in starts)
        if (done is not None and done > t) or (done is None and not later):
            event = pending_copy(event)
        events.append(event)
    return (SimpleNamespace(id=sample.id, epoch=1, events=events, messages=[], error=None),
            [r for r in runtime if r["timestamp"] <= t])


def requests_of(agent, code, windows, items, settings, count=None):
    by_id = {item["id"]: item for item in items}
    out = {}
    for window in windows:
        system, user = pl.prefilter_request(agent, code, window, by_id, settings, count)
        out[window["id"]] = pl.sha256_text(system + "\0" + user)
    return out


GDM_LIVE_REPLAY = __import__("os").environ.get("GDM_LIVE_REPLAY")


@unittest.skipUnless(GDM_LIVE_REPLAY, "set GDM_LIVE_REPLAY=runs/<run-id>[,runs/<run-id>] to replay saved runs (minutes)")
class SavedRunReplayTests(unittest.TestCase):
    """Regression replay on saved runs (skipped unless GDM_LIVE_REPLAY names them; October 20, 2026:
    gemini10 0 of 1,168 window checks mismatched, 273 of 283 windows final at the stop; cache100 0 of
    2,746, 799 of 824): the run as the live judge would have seen it 240, 120, 30, 5 and 0 minutes
    before the stop."""

    def test_saved_runs(self):
        for name in GDM_LIVE_REPLAY.split(","):
            run_dir = (REPO / name).resolve() if not Path(name).is_absolute() else Path(name)
            if not (run_dir / "inspect").is_dir():
                self.skipTest(f"{run_dir} is not here")
            total = replay_saved_run(run_dir, (240, 120, 30, 5, 0))
            self.assertEqual(total["mismatch"], 0, total)
            self.assertGreater(total["final_at_stop"], 0.9 * total["windows_total"], total)


def replay_saved_run(run_dir, cuts_minutes, margin=live.DEFAULT_MARGIN_SECONDS, window_tokens=24000):
    """Replay a saved run's live view at each cut (minutes before the stop); returns the totals."""
    settings = pl.PipelineSettings(prefilter_window_count=False)
    problems = []
    runtime = history.load_runtime(run_dir, problems)
    conference = history.load_conference(run_dir)
    host = history.load_host(run_dir, runtime, conference, problems)
    stop = history._epoch(host["stop_time"])
    read_sample, _ = history._ensure_inspect()
    total = {"checks": 0, "mismatch": 0, "final_at_stop": 0, "windows_total": 0}

    def conference_at(t):
        records = history.ConferenceRecords()
        records.available = True
        records.events = [dict(e) for e in conference.events if history._epoch(e["timestamp"]) <= t]
        records.events_by_seq = {e["seq"]: e for e in records.events}
        keep = lambda rows, key: {k: dict(r) for k, r in rows.items()  # noqa: E731
                                  if history._epoch(r[key]) is not None and history._epoch(r[key]) <= t}
        records.posts, records.dms = keep(conference.posts, "posted_at"), keep(conference.dms, "sent_at")
        records.feedback = keep(conference.feedback, "submitted_at")
        records.library = keep(conference.library, "published_at")
        records.submissions, records.tasks = keep(conference.submissions, "submitted_at"), conference.tasks
        for lid, row in records.library.items():
            records.library_by_path[(row["path"], row["revision"])] = lid
        for table in (records.dms, records.submissions):
            for internal, row in table.items():
                if row.get("public_id"):
                    records.public_to_internal[row["public_id"]] = internal
                    records.internal_to_public[internal] = row["public_id"]
        history._record_items(records)
        return records

    for agent, (path, sample_id, epoch) in sorted(history.sample_index(run_dir).items()):
        sample = read_sample(str(path), id=sample_id, epoch=epoch, resolve_attachments=True)
        log_name = str(path.relative_to(run_dir))
        full_run = history.RunHistory(run_dir=run_dir, run_id=run_dir.name, agents={}, host=host,
                                      conference=conference, runtime=runtime, problems=[])
        full = history.build_agent(sample, full_run, log_name=log_name)
        final_windows = make_windows(full.items, window_tokens)
        final = requests_of(agent, full.code, final_windows, full.items, settings)
        total["windows_total"] += len(final_windows)
        for minutes in cuts_minutes:
            t = stop - 60 * minutes
            records = [r for r in runtime if history._epoch(r.get("timestamp")) is not None
                       and history._epoch(r["timestamp"]) <= t]
            conf = conference_at(t)
            run_t = history.RunHistory(run_dir=run_dir, run_id=run_dir.name, agents={},
                                       host=history.load_host(run_dir, records, conf, []), conference=conf,
                                       runtime=records, problems=[])
            view, _ = view_at(sample, [], t)
            built = history.build_agent(view, run_t, log_name=log_name)
            restarts = [history._epoch(r["timestamp"]) for r in records
                        if r.get("kind") == "agent_restart" and r.get("agent_id") == agent]
            windows, prefix, _, _ = live.final_windows(built, view, complete=False, now=t, margin=margin,
                                                       window_tokens=window_tokens, restarts=restarts)
            got = requests_of(agent, built.code, windows, prefix, settings)
            total["checks"] += len(got)
            total["mismatch"] += sum(1 for w, d in got.items() if final.get(w) != d)
            if minutes == 0:
                total["final_at_stop"] += len(got)
        del sample
    return total


class WindowPrefixTests(unittest.TestCase):
    def test_closed_windows_of_any_prefix_are_the_final_windows(self):
        rng = random.Random(20261020)
        for _ in range(60):
            items = [make_item(f"A01.t{n:04d}.r", "agent-001", "reasoning", "reasoning",
                               "y" * rng.choice((40, 300, 640, 1500, 2600, 5200)), turn=n, time=T0 + n)
                     for n in range(rng.randint(1, 30))]
            full = make_windows(items, 300)
            for cut in range(len(items) + 1):
                closed = make_windows(items[:cut], 300)[:-1]
                self.assertEqual(closed, full[:len(closed)])

    def test_window_index_comes_from_the_window_id(self):
        window = {"id": "A07.w0012", "item_ids": ["A07.t0001.r"], "tokens": 1, "fragments": {}}
        self.assertEqual(pl.window_index(window), 12)
        item = make_item("A07.t0001.r", "agent-007", "reasoning", "reasoning", "text", turn=1, time=T0)
        _, user = pl.prefilter_request("agent-007", "A07", window, {item["id"]: item}, OFF, window_count=40)
        self.assertIn("window_index=12", user)
        self.assertNotIn("window_count", user)
        _, user = pl.prefilter_request("agent-007", "A07", window, {item["id"]: item}, ON, window_count=40)
        self.assertIn("window_index=12, window_count=40", user)


class FinalityReplayTests(unittest.TestCase):
    """Replays the scenario at every second: windows declared final are byte-identical post-run."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="judge-live-")
        cls.run_dir, cls.sample, cls.runtime = scenario(cls.tmp)
        final = history.build_agent(copy.deepcopy(cls.sample), run_at(cls.run_dir, cls.runtime))
        cls.final = final
        cls.final_windows = make_windows(final.items, WINDOW_TOKENS)
        cls.final_off = requests_of("agent-001", "A01", cls.final_windows, final.items, OFF)
        cls.final_on = requests_of("agent-001", "A01", cls.final_windows, final.items, ON, len(cls.final_windows))
        cls.replay = {}
        for second in range(0, 181):
            t = T0 + second
            view, runtime = view_at(cls.sample, cls.runtime, t)
            built = history.build_agent(view, run_at(cls.run_dir, runtime))
            restarts = [r["timestamp"] for r in runtime if r["kind"] == "agent_restart"]
            windows, prefix, chain, record = live.final_windows(built, view, complete=False, now=t, margin=MARGIN,
                                                                window_tokens=WINDOW_TOKENS, restarts=restarts)
            cls.replay[second] = {"windows": windows, "prefix": prefix, "record": record, "built": built,
                                  "off": requests_of("agent-001", "A01", windows, prefix, OFF),
                                  "on": requests_of("agent-001", "A01", windows, prefix, ON,
                                                    len(make_windows(built.items, WINDOW_TOKENS)))}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def prefix_ids(self, second):
        return [item["id"] for item in self.replay[second]["prefix"]]

    def test_scenario_has_every_hard_case(self):
        ids = [item["id"] for item in self.final.items]
        self.assertIn("A01.t0005.s", ids)         # the compaction summary
        self.assertIn("A01.t0009.s", ids)         # a session's last turn, a summary once it restarted
        self.assertNotIn("A01.t0003.o1", ids)     # the discarded output's call is never answered
        self.assertNotIn("A01.t0011.o1", ids)     # the killed session's call is never answered
        self.assertIn("A01.t0013.o1", ids)        # the last result, only in the final messages
        self.assertEqual(self.final.stats["model_errors"], [7])
        self.assertGreaterEqual(len(self.final_windows), 10)

    def test_every_final_window_is_the_post_run_request(self):
        checked = 0
        for second, row in self.replay.items():
            for window_id, digest in row["off"].items():
                self.assertEqual(digest, self.final_off[window_id], f"t+{second}: {window_id}")
                checked += 1
        self.assertGreater(checked, 300)
        # At the end only the last turn (whose results come with the final messages) and the open window wait.
        self.assertGreaterEqual(len(self.replay[180]["windows"]), len(self.final_windows) - 2)

    def test_negative_control_with_the_window_count(self):
        mismatched = sum(1 for row in self.replay.values() for w, d in row["on"].items() if self.final_on[w] != d)
        self.assertGreater(mismatched, 50)

    def test_summary_turn_waits_for_the_next_session(self):
        self.assertNotIn("A01.t0009.x", sum((self.prefix_ids(s) for s in self.replay), []))
        self.assertEqual(self.replay[115]["record"]["limited_by"], "turn 9: awaiting_next_turn")
        self.assertIn("A01.t0009.s", self.prefix_ids(140))

    def test_discarded_output_and_errored_turn_do_not_stall(self):
        self.assertIn("A01.t0003.c1", self.prefix_ids(65))
        self.assertIn("A01.t0008.c1", self.prefix_ids(110))

    def test_killed_session_is_settled_by_its_restart_record(self):
        self.assertEqual(self.replay[143]["record"]["limited_by"], "turn 11: awaiting_next_turn")
        self.assertNotIn("A01.t0011.c1", self.prefix_ids(143))
        self.assertIn("A01.t0011.c1", self.prefix_ids(162))
        view, runtime = view_at(self.sample, self.runtime, T0 + 162)
        reasons = {row["turn"]: row["reason"] for row in live.turn_status(view)}
        self.assertEqual((reasons[3], reasons[11], reasons[13]), ("discarded", "awaiting_tool_results", "pending"))
        restarts = [r["timestamp"] for r in runtime if r["kind"] == "agent_restart"]
        settled = {row["turn"]: row for row in live.turn_status(view, restarts=restarts)}
        self.assertEqual((settled[11]["settled"], settled[11]["reason"]), (True, "session_restarted"))
        self.assertTrue(all(row["settled"] for row in live.turn_status(self.sample, complete=True)))

    def test_margin_and_horizon(self):
        for second, row in self.replay.items():
            for item in row["prefix"]:
                self.assertLess(live.item_key(item), T0 + second - MARGIN + 1e-9)
        self.assertEqual(self.replay[0]["windows"], [])

    def test_complete_sample_is_final_up_to_the_margin(self):
        built = history.build_agent(copy.deepcopy(self.sample), run_at(self.run_dir, self.runtime))
        windows, prefix, _, record = live.final_windows(built, self.sample, complete=True, now=T0 + 400,
                                                        margin=MARGIN, window_tokens=WINDOW_TOKENS)
        self.assertEqual((record["limited_by"], len(prefix)), ("margin", len(built.items)))
        self.assertEqual(requests_of("agent-001", "A01", windows, prefix, OFF),
                         {w: self.final_off[w] for w in list(self.final_off)[:-1]})

    def test_prefix_chain_detects_a_changed_item(self):
        prefix = self.replay[100]["prefix"]
        chain = live.prefix_chain(prefix)
        changed = [dict(item) for item in prefix]
        changed[3]["text"] += " (edited)"
        self.assertEqual(live.prefix_chain(prefix[:3]), chain[:3])
        self.assertNotEqual(live.prefix_chain(changed)[3], chain[3])

    def test_conference_result_waits_for_its_join_slack(self):
        result = make_item("A01.t0001.o1", "agent-001", "tool_result", "board", "{}", turn=1,
                           time_bounds=(T0 + 10, T0 + 20), meta={"conference": {"status": "joined"}})
        before = make_item("A01.t0000.r", "agent-001", "reasoning", "reasoning", "r", turn=0, time=T0 + 5)
        self.assertEqual(live.final_prefix([before, result], T0 + 20 + 2 * history.JOIN_SLACK), [before])
        self.assertEqual(live.final_prefix([before, result], T0 + 31), [before, result])


# ---------------------------------------------------------------------------
# Inspect's realtime buffer


def write_buffer(log, db_dir, events, *, sample_id="agent-001"):
    """Log ``events`` into a real realtime buffer for ``log``: a completed model event is logged
    twice (pending, then updated), as Inspect does."""
    from inspect_ai.log import EvalSampleSummary
    from inspect_ai.log._recorders.buffer.database import SampleBufferDatabase
    from inspect_ai.log._recorders.types import SampleEvent
    db = SampleBufferDatabase(str(log), create=True, db_dir=db_dir)
    db.start_sample(EvalSampleSummary(id=sample_id, epoch=1, input="Begin.", target=""))
    for event in events:
        if event.completed is not None:
            db.log_events([SampleEvent(id=sample_id, epoch=1, event=pending_copy(event))])
        db.log_events([SampleEvent(id=sample_id, epoch=1, event=event)])
    return db


def write_eval(log, samples):
    from inspect_ai.log import EvalConfig, EvalDataset, EvalLog, EvalSample, EvalSpec, write_eval_log
    write_eval_log(EvalLog(eval=EvalSpec(created=moment(T0).isoformat(), task="gdm_math_swarm", dataset=EvalDataset(),
                                         model="mockllm/model", config=EvalConfig()),
                           samples=[EvalSample(id=s.id, epoch=1, input="Begin.", target="", messages=s.messages,
                                               events=s.events) for s in samples],
                           status="success" if samples else "started"), str(log))


class InspectBufferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="judge-live-buffer-"))
        self.run_dir, self.sample, self.runtime = scenario(self.tmp)
        self.log = self.run_dir / "inspect" / "2099-01-01T00-00-00_gdm-math-swarm_live.eval"
        self.db_dir = self.tmp / "samplebuffer"
        (self.run_dir / "runtime.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.runtime))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gate_accepts_the_pinned_inspect(self):
        self.assertIsNone(live.inspect_gate())
        with mock.patch("inspect_ai.__version__", "0.3.280"):
            self.assertIn("pinned to 0.3.279", live.inspect_gate())

    def test_buffer_reader_rebuilds_the_items_of_the_eval_reader(self):
        write_eval(self.log, [])               # the header Inspect writes at the start
        db = write_buffer(self.log, self.db_dir, self.sample.events[:12])
        source = live.LiveSource(self.run_dir, db_dir=self.db_dir)
        index = source.index()
        self.assertEqual((index["agent-001"]["buffered"], index["agent-001"]["flushed"]), (True, False))
        marker = source.change_marker(index["agent-001"])
        buffered, complete = source.read(index["agent-001"])
        self.assertFalse(complete)
        self.assertEqual(buffered.messages, [])
        self.assertEqual(len([e for e in buffered.events if e.event == "model"]), 12)
        self.assertTrue(all(not e.pending for e in buffered.events))   # updated rows collapse to the latest
        db.log_events([__import__("inspect_ai.log._recorders.types", fromlist=["SampleEvent"]).SampleEvent(
            id="agent-001", epoch=1, event=self.sample.events[12])])
        self.assertNotEqual(source.change_marker(index["agent-001"]), marker)
        source.close()
        base = history.load_run_base(self.run_dir, live=True)
        from_buffer = history.build_agent(buffered, base, log_name="inspect/" + self.log.name)
        # The .eval reader on the same events (written as the final log would hold them).
        partial = SimpleNamespace(id="agent-001", epoch=1, events=self.sample.events[:12], messages=[], error=None)
        write_eval(self.log, [partial])
        read_sample, _ = history._ensure_inspect()
        from_eval = read_sample(str(self.log), id="agent-001", epoch=1, resolve_attachments=True)
        from_log = history.build_agent(SimpleNamespace(id="agent-001", events=from_eval.events, messages=[],
                                                       error=None), history.load_run_base(self.run_dir, live=True),
                                       log_name="inspect/" + self.log.name)
        shown = lambda items: [(i["id"], i["kind"], i["channel"], i["time"], i["time_bounds"], i["text"])  # noqa: E731
                               for i in items]
        self.assertEqual(shown(from_buffer.items), shown(from_log.items))
        self.assertTrue(any(len(i["text"]) > 1500 for i in from_buffer.items))   # an attachment, resolved
        # Once flushed, the sample is read from the .eval as complete.
        index = live.LiveSource(self.run_dir, db_dir=self.db_dir).index()
        self.assertTrue(index["agent-001"]["flushed"])

    def test_buffer_found_when_the_pilot_named_the_run_by_another_path(self):
        """Inspect keys its buffer by the log directory's path: a pilot that reached the run through a
        symlink is still found (by the log file's unique name, through an alias directory)."""
        alias_root = self.tmp / "alias"
        alias_root.symlink_to(self.run_dir, target_is_directory=True)
        write_eval(self.log, [])
        write_buffer(alias_root / "inspect" / self.log.name, self.db_dir, self.sample.events[:3])
        source = live.LiveSource(self.run_dir, db_dir=self.db_dir)
        entry = source.index()["agent-001"]
        sample, complete = source.read(entry)
        self.assertEqual((complete, len(sample.events)), (False, 3))
        aliases = list(source._aliases)
        source.close()
        self.assertTrue(aliases and not any(a.exists() for a in aliases))

    def test_live_pass_then_post_run_judge_end_to_end_with_the_mock(self):
        """A mock live pass over a real buffer screens the final windows; the post-run judge on the
        final .eval adopts them and sends only the rest, with the result of a post-run-only judge."""
        write_eval(self.log, [])
        write_buffer(self.log, self.db_dir, self.sample.events[:13])
        settings = pl.PipelineSettings(window_tokens=WINDOW_TOKENS, prefilter_window_count=False,
                                       sample_fraction=1.0, max_selected_fraction=1.0)
        live_runtime = [r for r in self.runtime if r["timestamp"] <= T0 + 160]
        (self.run_dir / "runtime.jsonl").write_text("".join(json.dumps(r) + "\n" for r in live_runtime)
                                                    + '{"timestamp": 1, "kind": "partial')   # a line being written
        judge = self.tmp / "copy" / self.run_dir.name / "analysis" / "judge"
        rehearsal = tjp.Rehearsal(self.tmp / "r", judge_mode="candidates", settings=settings, agents=["agent-001"])
        rehearsal.judge_dir = judge
        live_pipeline = mock_pipeline(rehearsal, judge / "live")
        live.open_live_store(live_pipeline, {})
        record = live.LivePass(self.run_dir, live_pipeline, source=live.LiveSource(self.run_dir, db_dir=self.db_dir),
                               margin=MARGIN, now=lambda: T0 + 162).run()
        self.assertIsNone(record["stopped"])
        self.assertGreater(record["requests_planned"], 5)
        state = json.loads((judge / "live" / "state.json").read_text())
        self.assertEqual(state["agents"]["agent-001"]["limited_by"], "turn 12: awaiting_next_turn")
        # A second pass at the same moment re-reads nothing new: the buffer is unchanged.
        again = live.LivePass(self.run_dir, live_pipeline, source=live.LiveSource(self.run_dir, db_dir=self.db_dir),
                              margin=MARGIN, now=lambda: T0 + 162).run()
        self.assertEqual(again["requests_planned"], 0)
        # The run ends: the final .eval and runtime; the post-run judge (mock) adopts the live units.
        (self.run_dir / "runtime.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.runtime))
        write_eval(self.log, [self.sample])
        built = history.build_run(self.run_dir)
        post = rehearsal.pipeline()
        post.use_histories(lambda agent: built)
        summary = asyncio.run(post.run())
        self.assertEqual(summary["status"], "complete", summary["errors"])
        consistency = json.loads((judge / "live" / "consistency.json").read_text())
        self.assertEqual(consistency["adopted"], record["requests_planned"])
        self.assertEqual((consistency["rejected"], consistency["windows_screened_for_another_request"]), (0, 0))
        self.assertEqual(consistency["windows_not_screened_live"], len(make_windows(built.agents["agent-001"].items,
                                                                                    WINDOW_TOKENS)) - consistency["adopted"])


def mock_pipeline(rehearsal, judge_dir, **kwargs):
    """A rehearsal Pipeline writing to ``judge_dir`` with the rehearsal's mock transport and ledger."""
    clients = {key: JudgeClient(key, rehearsal.root / "ledger.sqlite", scope="rehearsal",
                                archive_dir=Path(judge_dir) / "raw", settings=rehearsal.routes,
                                http=rehearsal.transport, max_concurrency=rehearsal.settings.concurrency(key),
                                ledger_caps={"rehearsal": rehearsal.cap}, run_id=f"{tjp.RUN_ID}:live")
               for key in pl.MODEL_KEYS}
    models = {k: {"model": rehearsal.routes.route(k).model_id, "effort": rehearsal.routes.route(k).effort}
              for k in pl.MODEL_KEYS}
    return pl.Pipeline(rehearsal.run_dir, judge_dir, mode="rehearsal", clients=clients, settings=rehearsal.settings,
                       scope="rehearsal", ledger_path=rehearsal.root / "ledger.sqlite", models=models, route={},
                       agent_ids=[], stage="prefilter", warm_from=False, label_check=False, **kwargs)


# ---------------------------------------------------------------------------
# Equivalence: live passes then the post-run judge == a post-run-only judge


SETTINGS = pl.PipelineSettings(window_tokens=300, sample_fraction=1.0, max_selected_fraction=1.0,
                               haiku_concurrency=4, sonnet_concurrency=2, opus_concurrency=2, agent_concurrency=3,
                               judge_mode="candidates", prefilter_window_count=False)


def normalized(value):
    """JSON value without timestamps, live provenance and per-request costs (the mock prices a prompt-cache
    write or read by request order, which concurrency varies between any two runs): what may differ between
    equal judgments."""
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()
                if not k.endswith("_at") and k not in ("adopted_from", "created", "generated", "built_at", "cost_usd")}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def outputs(judge_dir):
    out = {}
    for path in sorted(Path(judge_dir).glob("agents/*.json")):
        out[path.name] = normalized(json.loads(path.read_text()))
    out["labels.jsonl"] = [normalized(json.loads(line)) for line in (Path(judge_dir) / "labels.jsonl").read_text()
                           .splitlines()]
    run_level = json.loads((Path(judge_dir) / "run_level.json").read_text())
    out["run_level.json"] = normalized({k: run_level.get(k) for k in ("status", "source", "summary", "agents_flagged",
                                                                      "result")})
    return out


def bodies(rehearsal, start=0):
    """(prefilter request bodies, other request bodies) sent through the rehearsal transport."""
    prefilter, other = [], []
    for request in rehearsal.transport.requests[start:]:
        body = request["body"]
        purpose, _ = rehearsal.model.classify(tjp.system_text(body), body["messages"][-1]["content"])
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        (prefilter if purpose == "prefilter" else other).append(digest)
    return sorted(prefilter), sorted(other)


def live_requests(rehearsal, live_dir, share):
    """Requests of a live pass that saw the first ``share`` of every agent's items."""
    out = []
    for agent in rehearsal.agents:
        agent_history = rehearsal.run.agents[agent]
        prefix = agent_history.items[:int(len(agent_history.items) * share)]
        windows = make_windows(prefix, SETTINGS.window_tokens)[:-1]
        out += live.window_requests(agent, agent_history.code, windows, {i["id"]: i for i in prefix}, SETTINGS,
                                    live_dir)
    return out


class LiveThenPostEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="judge-live-equivalence-"))
        cls.cold = tjp.Rehearsal(cls.tmp / "cold", settings=SETTINGS, judge_mode="candidates")
        cls.cold_summary = cls.cold.run_pipeline()
        cls.cold_bodies = bodies(cls.cold)

        cls.warm = tjp.Rehearsal(cls.tmp / "warm", settings=SETTINGS, judge_mode="candidates")
        live_dir = cls.warm.judge_dir / "live"
        cls.live_pipeline = mock_pipeline(cls.warm, live_dir)
        live.open_live_store(cls.live_pipeline, {})
        requests = live_requests(cls.warm, live_dir, 0.7)
        # A mispredicted window: screened live while one of its records still differed from the final one
        # (same length, so the windows pack alike); never adopted, and sent again post-run.
        screened = {r["window"]["id"] for r in requests if r["agent"] == "agent-003"}
        final = make_windows(cls.warm.run.agents["agent-003"].items, SETTINGS.window_tokens)
        target = next(w for w in final[:-1] if w["id"] not in screened)
        changed = copy.deepcopy(cls.warm.run.agents["agent-003"])
        item = next(i for i in changed.items if i["id"] == target["item_ids"][0])
        item["text"] = "#" + item["text"][1:]
        windows = [w for w in make_windows(changed.items, SETTINGS.window_tokens) if w["id"] == target["id"]]
        cls.mispredicted = live.window_requests("agent-003", "A03", windows, {i["id"]: i for i in changed.items},
                                                SETTINGS, live_dir)
        cls.live_count = len(requests)
        outcomes, stopped = asyncio.run(live.send_requests(cls.live_pipeline, requests + cls.mispredicted))
        cls.live_outcomes, cls.live_stopped = outcomes, stopped
        cls.live_bodies = bodies(cls.warm)
        sent = len(cls.warm.transport.requests)
        cls.warm_summary = cls.warm.run_pipeline()
        cls.post_bodies = bodies(cls.warm, sent)
        cls.consistency = json.loads((live_dir / "consistency.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_both_complete(self):
        self.assertIsNone(self.live_stopped)
        self.assertEqual(self.cold_summary["status"], "complete", self.cold_summary["errors"])
        self.assertEqual(self.warm_summary["status"], "complete", self.warm_summary["errors"])
        self.assertGreater(self.live_count, 10)

    def test_same_findings_labels_and_run_level(self):
        self.assertEqual(outputs(self.warm.judge_dir), outputs(self.cold.judge_dir))

    def test_same_sonnet_and_opus_requests(self):
        self.assertEqual(self.post_bodies[1], self.cold_bodies[1])

    def test_no_unit_is_paid_twice(self):
        live_prefilter = self.live_bodies[0]
        combined = sorted(live_prefilter + self.post_bodies[0])
        mispredicted = [b for b in live_prefilter if b not in self.cold_bodies[0]]
        self.assertEqual(len(mispredicted), 1)                          # paid once, never adopted
        self.assertEqual(sorted(b for b in combined if b not in mispredicted), self.cold_bodies[0])
        self.assertLess(len(self.post_bodies[0]), len(self.cold_bodies[0]) - 10)

    def test_adoption_record(self):
        self.assertEqual(self.consistency["adopted"], self.live_count)
        self.assertEqual(self.consistency["windows_screened_for_another_request"], 1)
        self.assertEqual(self.consistency["spend"]["unadopted_units"], 1)
        adopted = [json.loads(p.read_text()) for p in self.warm.judge_dir.glob("units/*/prefilter/*.json")]
        adopted = [u for u in adopted if u.get("adopted_from")]
        self.assertEqual(len(adopted), self.live_count)
        for unit in adopted:
            for step in unit["steps"]:
                for raw in step["client"]["raw_paths"]:
                    self.assertTrue(raw.startswith("live/raw/"), raw)
                    self.assertTrue((self.warm.judge_dir / raw).exists(), raw)
        spend = json.loads((self.warm.judge_dir / "pipeline.json").read_text())["spend"]
        self.assertEqual(spend["live"]["adopted_units"], self.live_count)
        state = json.loads((self.warm.judge_dir / "state.json").read_text())
        self.assertEqual(state["invocations"][-1]["live"]["adopted"], self.live_count)

    def test_refused_window_is_adopted_and_never_resent(self):
        refused = json.loads((self.warm.judge_dir / "units" / "agent-001" / "prefilter" / "A01.w0002.json").read_text())
        self.assertEqual((refused["status"], refused["reason"]), ("ungraded", "refused_twice"))
        self.assertIn("adopted_from", refused)


class AdoptionGuardTests(unittest.TestCase):
    """Every guard turns a live unit into a request sent again; the result never changes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="judge-live-guards-"))
        self.r = tjp.Rehearsal(self.tmp, settings=SETTINGS, judge_mode="candidates", agents=["agent-004"])
        self.live_dir = self.r.judge_dir / "live"
        pipeline = mock_pipeline(self.r, self.live_dir)
        live.open_live_store(pipeline, {})
        self.requests = live_requests(self.r, self.live_dir, 1.0)
        asyncio.run(live.send_requests(pipeline, self.requests))
        self.units = sorted(self.live_dir.glob("units/agent-004/prefilter/*.json"))
        self.assertTrue(self.units)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self):
        sent = len(self.r.transport.requests)
        summary = self.r.run_pipeline()
        self.assertEqual(summary["status"], "complete", summary["errors"])
        return json.loads((self.live_dir / "consistency.json").read_text()), bodies(self.r, sent)[0]

    def edit(self, path, change):
        unit = json.loads(path.read_text())
        change(unit)
        path.write_text(json.dumps(unit))

    def test_all_adopted_without_tampering(self):
        consistency, prefilter = self.post()
        self.assertEqual(consistency["adopted"], len(self.units))
        self.assertEqual(len(prefilter), 1)   # only the open last window

    def test_other_binding_disables_adoption(self):
        manifest = json.loads((self.live_dir / "manifest.json").read_text())
        manifest["settings"]["window_tokens"] = 301
        (self.live_dir / "manifest.json").write_text(json.dumps(manifest))
        consistency, prefilter = self.post()
        self.assertEqual((consistency["enabled"], consistency["adopted"]), (False, 0))
        self.assertIn("binding differs: settings", consistency["disabled_reason"])
        self.assertEqual(len(prefilter), len(self.units) + 1)

    def test_other_route_disables_adoption(self):
        manifest = json.loads((self.live_dir / "manifest.json").read_text())
        manifest["route"] = {"provider": {"order": ["someone-else"]}}
        (self.live_dir / "manifest.json").write_text(json.dumps(manifest))
        consistency, _ = self.post()
        self.assertEqual(consistency["disabled_reason"], "provider route differs")

    def test_unit_level_guards(self):
        first, second, third = self.units[:3]
        self.edit(first, lambda u: u["live"].update(code_sha256="0" * 64))
        self.edit(second, lambda u: u.update(status="grading_failed"))
        self.edit(third, lambda u: u.update(result={"selected_item_ids": ["A04.t9999.r"], "selections": []}))
        consistency, prefilter = self.post()
        reasons = sorted(r["reason"] for rows in json.loads((self.live_dir / "consistency.json").read_text())
                         ["detail"]["rejected"].values() for r in rows)
        self.assertEqual(reasons, ["judge_code_differs", "replay_mismatch", "status_grading_failed"])
        self.assertEqual(consistency["adopted"], len(self.units) - 3)
        self.assertEqual(len(prefilter), 3 + 1)
        for path in (first, second, third):
            unit = json.loads((self.r.judge_dir / "units" / "agent-004" / "prefilter" /
                               (path.name.split("@")[0] + ".json")).read_text())
            self.assertNotIn("adopted_from", unit)

    def test_early_stages_then_resume_keep_one_adoption_record(self):
        sent = len(self.r.transport.requests)
        early = self.r.run_pipeline(stage="verify")
        self.assertEqual(early["status"], "complete", early["errors"])
        final = self.r.run_pipeline()
        self.assertEqual(final["status"], "complete", final["errors"])
        consistency = json.loads((self.live_dir / "consistency.json").read_text())
        self.assertEqual((consistency["adopted"], consistency["windows_not_screened_live"]), (len(self.units), 1))
        self.assertEqual(len(bodies(self.r, sent)[0]), 1)
        self.assertEqual(final["live"]["adopted"], len(self.units))

    def test_no_warm_never_adopts(self):
        sent = len(self.r.transport.requests)
        summary = self.r.run_pipeline(warm_from=False)
        self.assertEqual(summary["status"], "complete")
        self.assertNotIn("live", summary)
        self.assertEqual(len(bodies(self.r, sent)[0]), len(self.units) + 1)


# ---------------------------------------------------------------------------
# Budget: the per-run cap


class PerRunCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="judge-live-budget-"))
        self.ledger = self.tmp / "ledger.sqlite"
        self.book = BudgetLedger(self.ledger, {"rehearsal": "50", "analysis": "50"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_labels_carry_the_run_id(self):
        self.assertEqual(request_label("haiku", "prefilter", "A01.w0001", "run-x"), "judge-haiku:run-x:prefilter:A01.w0001")
        self.assertEqual(request_label("haiku", "prefilter", "A01.w0001"), "judge-haiku:prefilter:A01.w0001")
        self.assertEqual(request_label("opus", "verifier", "x" * 400, "run-x")[:30], "judge-opus:run-x:verifier:xxxx")
        self.assertEqual(len(request_label("opus", "verifier", "x" * 400, "run-x")), 200)

    def test_committed_for_run_counts_every_hold_of_the_run(self):
        settled = self.book.reserve("analysis", request_label("haiku", "prefilter", "a", "run-x"), Decimal("0.5"))
        self.book.settle(settled, Decimal("0.2"))
        uncertain = self.book.reserve("analysis", request_label("opus", "verifier", "b", "run-x"), Decimal("1.5"))
        self.book.settle(uncertain, None)
        self.book.reserve("analysis", request_label("haiku", "prefilter", "c", "run-x:live"), Decimal("0.3"))  # orphan
        other = self.book.reserve("analysis", request_label("haiku", "prefilter", "d", "run-y"), Decimal("4"))
        self.book.settle(other, Decimal("4"))
        self.book.reserve("rehearsal", request_label("haiku", "prefilter", "e", "run-x"), Decimal("7"))
        self.book.reserve("analysis", "pilot-agent-001", Decimal("9"))
        record = committed_for_run(self.ledger, "run-x", scope="analysis")
        self.assertEqual((Decimal(record["committed_usd"]), record["rows"], record["unresolved_rows"]),
                         (Decimal("2.0"), 3, 2))
        self.assertEqual(Decimal(committed_for_run(self.ledger, "run-x:live", scope="analysis")["committed_usd"]),
                         Decimal("0.3"))
        self.assertEqual(committed_for_run(self.tmp / "missing.sqlite", "run-x")["committed_usd"], "0")

    def test_run_limit_starts_at_the_prior(self):
        limit = RunLimit("10", source="standing_approval", prior_usd="8")
        self.assertIsNone(limit.admit(Decimal("2.5")))
        self.assertIsNotNone(limit.admit(Decimal("1.5")))
        record = limit.record()
        self.assertEqual((record["prior_usd"], record["committed_usd"], record["denied"]), ("8", "9.5", 1))

    def test_fixed_run_limit_is_per_run(self):
        import run_transcript_judge as cli
        standing = {"max_usd": "50"}
        limit = cli.fixed_run_limit(None, standing, {"analysis": {"remaining_usd": "150"}}, prior_usd="12")
        self.assertEqual((limit["limit_usd"], limit["room_usd"], limit["prior_usd"]), ("50", "38", "12"))
        live_limit = cli.fixed_run_limit(None, standing, None, prior_usd="12", live_cap="10", live_prior_usd="4")
        self.assertEqual((live_limit["room_usd"], live_limit["basis"]), ("6", "live_cap"))
        clamped = cli.fixed_run_limit(None, standing, {"analysis": {"remaining_usd": "3"}}, prior_usd="12")
        self.assertEqual((clamped["room_usd"], clamped["basis"]), ("3", "analysis_remaining"))
        with self.assertRaises(cli.LimitRefused):
            cli.fixed_run_limit(None, standing, None, prior_usd="50")
        with self.assertRaises(cli.LimitRefused):
            cli.fixed_run_limit(None, standing, None, prior_usd="1", live_cap="10", live_prior_usd="10")
        override = cli.fixed_run_limit(Decimal("5"), None, None, prior_usd="49")  # explicit: per invocation
        self.assertEqual((override["limit_usd"], override["per_run"]), ("5", False))
        default = cli.fixed_run_limit(None, None, None, prior_usd="20")         # the $50 default: per run
        self.assertEqual((default["limit_usd"], default["room_usd"]), ("50", "30"))

    def test_a_resume_never_gets_a_fresh_cap(self):
        """Two --mock invocations of one run: the second starts at what the first committed."""
        import test_judge_estimate
        harness = test_judge_estimate.CommandLineTests("test_run_limit_flag")
        harness.setUp()
        try:
            code, _, err = harness.call("--mock", "--output-dir", str(harness.judge_dir), "--stage", "prefilter")
            self.assertEqual(code, 0, err)
            code, _, err = harness.call("--mock", "--output-dir", str(harness.judge_dir), "--stage", "judge",
                                        "--max-selected-fraction", "1")
            self.assertEqual(code, 0, err)
            state = json.loads((harness.judge_dir / "state.json").read_text())
            first, second = (inv["run_limit"] for inv in state["invocations"])
            self.assertEqual(first["prior_usd"], "0")
            self.assertEqual(Decimal(second["prior_usd"]), Decimal(first["settled_usd"]))
            self.assertGreater(Decimal(second["prior_usd"]), 0)
            self.assertEqual(Decimal(second["limit_usd"]), Decimal(50))
            labels = sqlite3.connect(harness.judge_dir / "rehearsal-ledger.sqlite").execute(
                "SELECT agent FROM calls").fetchall()
            self.assertTrue(all(f":{tjp.RUN_ID}:" in row[0] for row in labels))
        finally:
            harness.tearDown()


class LockTests(unittest.TestCase):
    def test_one_judging_process_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with live.judge_lock(tmp):
                with self.assertRaises(live.JudgeBusy):
                    with live.judge_lock(tmp, wait_seconds=0):
                        pass
            with live.judge_lock(tmp, wait_seconds=0):
                self.assertTrue((Path(tmp) / live.LOCK_NAME).exists())


# ---------------------------------------------------------------------------
# Live-safe readers, the command line and the config keys


class LiveSafeReaderTests(unittest.TestCase):
    def test_live_connection_never_opens_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            sqlite3.connect(path).execute("CREATE TABLE t (x)").connection.close()
            Path(str(path) + "-wal").write_bytes(b"")   # an empty WAL: a saved run looks the same
            seen = []
            real = sqlite3.connect

            def spy(target, *args, **kwargs):
                seen.append(target)
                return real(target, *args, **kwargs)
            with mock.patch("sqlite3.connect", spy):
                history._connect_readonly(path, live=True).close()
                history._connect_readonly(path).close()
            self.assertTrue(seen[0].endswith("?mode=ro"))
            self.assertIn("immutable=1", seen[1])

    def test_runtime_reads_complete_lines_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "runtime.jsonl").write_text('{"kind": "a"}\n{"kind": "b"}\n{"kind": "c", "pay')
            problems = []
            self.assertEqual([r["kind"] for r in history.load_runtime(tmp, problems, complete_lines_only=True)],
                             ["a", "b"])
            self.assertEqual(problems, [])
            self.assertEqual(len(history.load_runtime(tmp, [])), 3)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        import run_transcript_judge
        self.cli = run_transcript_judge
        self.tmp = Path(tempfile.mkdtemp(prefix="judge-live-cli-"))
        self.run_dir = self.tmp / "runs" / tjp.RUN_ID
        (self.run_dir / "inspect").mkdir(parents=True)
        self.judge_dir = self.tmp / "copy" / tjp.RUN_ID / "analysis" / "judge"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.cli.main(["--run-dir", str(self.run_dir), *args])
        return code, out.getvalue(), err.getvalue()

    def config(self, **changes):
        path = self.tmp / "launch-config.json"
        path.write_text(json.dumps(dict({"auto_behavior_judge": True, "behavior_judge_approval": "owner (test)",
                                         "behavior_judge_max_usd": 50, "run_dir": "runs/x"}, **changes)))
        return path

    def test_live_refusals(self):
        code, _, err = self.call("--live", "--i-have-user-approval", "--ledger", str(self.tmp / "l.sqlite"))
        self.assertEqual(code, 4)
        code, _, err = self.call("--live", "--mock", "--output-dir", str(self.judge_dir), "--prefilter-window-count",
                                 "on")
        self.assertEqual(code, 4)
        self.assertIn("--prefilter-window-count off", err)
        code, _, err = self.call("--live", "--dry-run-estimate")
        self.assertEqual(code, 4)
        with mock.patch("research_swarm.budget.judge_analysis_problems", return_value=[]):
            code, _, err = self.call("--live", "--standing-approval-from-config",
                                     str(self.config(live_behavior_judge=False)), "--ledger", str(self.tmp / "l.sqlite"))
        self.assertEqual(code, 4)
        self.assertIn("live_behavior_judge is not true", err)

    def test_unavailable_source_is_recorded_and_skipped(self):
        with mock.patch.object(live, "inspect_gate", return_value="inspect_ai 9.9 (the live reader is pinned)"):
            code, out, err = self.call("--live", "--mock", "--output-dir", str(self.judge_dir))
        self.assertEqual(code, 0, err)
        self.assertIn("live_source_unavailable", out)
        state = json.loads((self.judge_dir / "live" / "state.json").read_text())
        self.assertIn("pinned", state["passes"][-1]["live_source_unavailable"])

    def test_busy_lock_skips_the_pass(self):
        with live.judge_lock(self.judge_dir):
            code, out, _ = self.call("--live", "--mock", "--output-dir", str(self.judge_dir))
        self.assertEqual(code, 0)
        self.assertIn("busy", out)

    def test_window_count_setting(self):
        args = self.cli.parse_args(["--run-dir", str(self.run_dir)])
        self.assertTrue(self.cli.window_count_setting(args, self.judge_dir))
        live_manifest = self.judge_dir / "live" / "manifest.json"
        pl.write_json(live_manifest, {"settings": dict(OFF.binding())})
        self.assertFalse(self.cli.window_count_setting(args, self.judge_dir))
        self.assertTrue(self.cli.window_count_setting(self.cli.parse_args(["--run-dir", str(self.run_dir),
                                                                           "--no-warm"]), self.judge_dir))
        pl.write_json(self.judge_dir / "manifest.json", {"settings": dict(ON.binding())})  # a v3 directory resumes v3
        self.assertTrue(self.cli.window_count_setting(args, self.judge_dir))
        flagged = self.cli.parse_args(["--run-dir", str(self.run_dir), "--prefilter-window-count", "off"])
        self.assertFalse(self.cli.window_count_setting(flagged, self.judge_dir))
        self.assertFalse(self.cli.window_count_setting(self.cli.parse_args(["--run-dir", str(self.run_dir), "--live"])))
        self.assertTrue(self.cli.settings_from(args, judge_dir=self.judge_dir).prefilter_window_count)

    def test_binding_keeps_v3_manifests(self):
        self.assertNotIn("prefilter_window_count", pl.PipelineSettings().binding())
        self.assertIs(pl.PipelineSettings(prefilter_window_count=False).binding()["prefilter_window_count"], False)
        with self.assertRaises(ValueError):
            pl.PipelineSettings(prefilter_window_count="no").validate()


class LiveConfigTests(unittest.TestCase):
    APPROVAL = {"auto_behavior_judge": True, "behavior_judge_approval": "owner (test)", "behavior_judge_max_usd": 50}

    def test_live_is_on_by_default_for_auto_judge_configs(self):
        self.assertEqual(auto_judge.live_mode(self.APPROVAL), "paid")
        self.assertEqual(auto_judge.live_settings(self.APPROVAL),
                         {"mode": "paid", "max_usd": "10", "interval_seconds": 300})
        self.assertIsNone(auto_judge.live_mode(dict(self.APPROVAL, live_behavior_judge=False)))
        self.assertEqual(auto_judge.live_mode(dict(self.APPROVAL, live_behavior_judge="mock")), "mock")
        self.assertIsNone(auto_judge.live_mode({"live_behavior_judge": True}))   # never without auto_behavior_judge
        approval = auto_judge.standing_approval(self.APPROVAL, live=True)
        self.assertEqual((approval["live"], approval["live_max_usd"]), (True, "10"))

    def test_live_key_problems(self):
        for changes, text in (({"live_behavior_judge": "yes"}, 'true, false or "mock"'),
                              ({"behavior_judge_live_max_usd": 0}, "positive amount"),
                              ({"behavior_judge_live_max_usd": 60}, "exceeds the per-run cap"),
                              ({"behavior_judge_max_usd": 5, "behavior_judge_live_max_usd": 6}, "exceeds"),
                              ({"behavior_judge_live_interval_seconds": 30}, "from 60 to 3600"),
                              ({"behavior_judge_live_interval_seconds": True}, "integer")):
            problems = auto_judge.standing_approval_problems(dict(self.APPROVAL, **changes))
            self.assertTrue(any(text in p for p in problems), (changes, problems))
        self.assertEqual(auto_judge.standing_approval_problems(dict(self.APPROVAL, behavior_judge_live_max_usd=5,
                                                                    behavior_judge_live_interval_seconds=120)), [])
        with self.assertRaises(ValueError):
            auto_judge.standing_approval(dict(self.APPROVAL, live_behavior_judge="mock"), live=True)


if __name__ == "__main__":
    unittest.main()
