"""Story map stage 0 for agentic-monitor runs (arm AG; storymap/agentic.py). Offline, no model calls.

Fixture: built into a temporary folder by ``fixtures/storymap/agentic/build_agentic_fixture.py`` (the scenario is in
its docstring); the Inspect reader is tested on synthetic sample objects.
"""
import filecmp
import importlib.util
import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research.analysis.storymap import agentic as ag
from research.analysis.storymap import extract as ex

FIX = Path(__file__).resolve().parent / "fixtures/storymap"
_spec = importlib.util.spec_from_file_location("storymap_agentic_fixture",
                                               FIX / "agentic/build_agentic_fixture.py")
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def read(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def events(items, typ=None):
    return [it for it in items if it["kind"] == "event" and (typ is None or it["event"]["type"] == typ)]


class AgenticExtractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.run_dir, cls.ids = fixture.build_run(root / "run")
        cls.out = root / "out"
        ex.extract(cls.run_dir, cls.out, workers=1)
        cls.a1 = read(cls.out / "timeline/agent-001.jsonl")
        cls.a2 = read(cls.out / "timeline/agent-002.jsonl")
        cls.m1 = read(cls.out / "agentic/agmon-001.jsonl")
        cls.m2 = read(cls.out / "agentic/agmon-002.jsonl")
        cls.context = json.loads((cls.out / "agentic_context.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_detection_and_roles(self):
        ctx = ex.RunContext(self.run_dir)
        self.assertIsNotNone(ctx.agentic)
        self.assertEqual(ctx.agentic.monitors, ["agmon-001", "agmon-002"])
        self.assertEqual(ctx.role("agmon-001"), "agentic_monitor")
        self.assertEqual(ctx.role("reviewer"), "agentic_monitor")
        self.assertEqual(ctx.solvers(), ["agent-001", "agent-002"])
        self.assertFalse(ag.is_agentic({"effective": {"roles": {"agent-001": "solver"}}}))
        self.assertTrue(ag.is_agentic({"effective": {"roles": {"agmon-001": "agentic_monitor"}}}))
        self.assertIsNone(ex.RunContext(FIX / "run").agentic)

    def test_g1_at_revert_time(self):
        g1 = events(self.a1, "Y1")
        self.assertEqual(len(g1), 1)
        it = g1[0]
        e = it["event"]
        self.assertEqual((it["t"], e["id"], e["author"], e["author_role"], e["fake"], e["outcome"]),
                         (10.0, "Y1:sub-000002", "agmon-001", "agentic_monitor", True, "reverted"))
        self.assertEqual(e["detail"], {"monitor": "agmon-001", "submission_id": "sub-000002",
                                       "public_submission_id": "sub-5c1e0a", "problem_id": "t2",
                                       "reason": fixture.REASON, "accepted_ts": 8.0, "latency_min": 2.0,
                                       "message_id": "dm-r1", "fake": True})
        # placed between the submission (8) and the next call (10.9)
        kinds = [(x["kind"], (x["event"] or {}).get("type")) for x in self.a1[5:9]]
        self.assertEqual(kinds, [("tool_result", None), ("event", "S1"), ("event", "Y1"), ("tool_call", None)])
        # the 1:1 fallback and a false revert (no runtime message id)
        y1b = events(self.a2, "Y1")[0]["event"]
        self.assertEqual((y1b["author"], y1b["fake"], y1b["detail"]["message_id"]), ("agmon-002", False, None))

    def test_g2_replaces_d1_once_per_message(self):
        self.assertEqual(events(self.a1, "D1"), [])
        g2 = events(self.a1, "Y2")
        self.assertEqual([x["event"]["id"] for x in g2], ["Y2:dm-000001", "Y2:dm-000002"])
        push, pull = g2
        d = push["event"]["detail"]
        self.assertEqual((push["t"], push["tool"], d["delivery"], d["placement"], d["delivery_items"]),
                         (11.0, "push", "push", "anchor", ["Y2:dm-000001"]))
        self.assertEqual((d["monitor"], d["message_id"], d["dm_id"], d["sent_ts"], d["delivered_ts"], d["lag_s"],
                          d["revert_notice"]), ("agmon-001", "dm-r1", "dm-000001", 10.0, 11.0, 60.0, True))
        self.assertIn("revert_notice", push["event"]["tags"])
        p = pull["event"]["detail"]
        self.assertEqual((pull["t"], pull["tool"], p["delivery"], p["message_id"], p["lag_s"], p["revert_notice"],
                          p["text"]), (15.0, "get_messages", "pull", "dm-s2", 180.0, False, fixture.STOP_DM))
        self.assertEqual(pull["event"]["author_role"], "agentic_monitor")

    def test_p2_and_s1_name_the_monitor(self):
        p2 = events(self.a1, "P2")[0]
        self.assertEqual((p2["event"]["author"], p2["event"]["author_role"]), ("agmon-001", "agentic_monitor"))
        self.assertIn("removed by agmon-001", p2["text"])
        s1 = events(self.a1, "S1")[0]
        self.assertEqual(s1["event"]["detail"]["reverted_by"], "agmon-001")
        self.assertIn("by agmon-001", s1["text"])

    def test_index_monitor_field(self):
        index = json.loads((self.out / "timeline/index.json").read_text())
        self.assertEqual(index["agent-001"]["monitor"], "agmon-001")
        mindex = json.loads((self.out / "agentic/index.json").read_text())
        self.assertEqual(mindex["agmon-002"]["solver"], "agent-002")
        self.assertEqual(mindex["agmon-001"]["source"], "runtime_only")

    def test_monitor_timeline_events(self):
        types_ = [x["event"]["type"] for x in events(self.m1)]
        self.assertEqual(types_, ["GW", "GL", "GR", "GF", "GO", "GM", "GC", "GE"])
        for it in self.m1:
            self.assertEqual(set(it), {"i", "t", "ts", "turn", "kind", "tool", "text", "event", "call_id", "mid"})
            self.assertEqual((it["event"]["author"], it["event"]["author_role"]), ("agmon-001", "agentic_monitor"))
        by = {x["event"]["type"]: x for x in events(self.m1)}
        gw = by["GW"]["event"]["detail"]
        self.assertEqual((gw["call_ids"], gw["call_lags_s"], gw["implicit"], gw["tool_call_id"]),
                         ([self.ids["ls"], self.ids["submit"]], [468.0, 60.0], False, None))
        gr = by["GR"]["event"]["detail"]
        self.assertEqual((gr["submission_id"], gr["delivered_ts"], gr["latency_min"], by["GR"]["event"]["fake"]),
                         ("sub-000002", 11.0, 2.0, True))
        gm = by["GM"]["event"]["detail"]
        self.assertEqual((gm["message_id"], gm["sent_ts"], gm["delivered_ts"], gm["lag_s"]), ("dm-s2", 12.0, 15.0,
                                                                                               180.0))
        self.assertEqual(by["GF"]["event"]["detail"]["why"], "no_accepted_submission")
        self.assertEqual(by["GO"]["event"]["detail"]["call_id"], self.ids["ls"])
        self.assertTrue(events(self.m2, "GW")[0]["event"]["detail"]["implicit"])

    def test_context(self):
        c = self.context
        self.assertEqual(c["version"], 1)
        p1, p2 = c["pairs"]["agmon-001"], c["pairs"]["agmon-002"]
        self.assertEqual({k: p1[k] for k in ("solver", "waits", "reverts", "refused", "messages", "get_output",
                                             "list_revertible", "errors", "first_fake_t", "first_revert_t",
                                             "time_to_first_revert_min", "fakes_accepted", "fakes_reverted",
                                             "fakes_credited_at_end", "false_reverts", "median_seen_lag_s",
                                             "message_lag_s_median", "cost_usd", "mean_wait_s")},
                         {"solver": "agent-001", "waits": 1, "reverts": 1, "refused": 1, "messages": 1,
                          "get_output": 1, "list_revertible": 1, "errors": 1, "first_fake_t": 8.0,
                          "first_revert_t": 10.0, "time_to_first_revert_min": 2.0, "fakes_accepted": 1,
                          "fakes_reverted": 1, "fakes_credited_at_end": 0, "false_reverts": 0,
                          "median_seen_lag_s": 264.0, "message_lag_s_median": 180.0, "cost_usd": 0.75,
                          "mean_wait_s": 60.0})
        self.assertEqual((p2["false_reverts"], p2["first_fake_t"], p2["cost_usd"]), (1, None, 0.0))
        self.assertEqual(c["seen"], {"agent-001": [[self.ids["ls"], 1.2, 9.0], [self.ids["submit"], 8.0, 9.0]]})
        # every seen call id is a solver timeline call_id
        calls = {x["call_id"] for x in self.a1 if x["kind"] == "tool_call"}
        self.assertTrue({s[0] for s in c["seen"]["agent-001"]} <= calls)


class NonAgenticUnchangedTests(unittest.TestCase):
    def test_shared_fixture_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex.extract(FIX / "run", tmp, workers=1)
            cmp = filecmp.dircmp(Path(tmp) / "timeline", FIX / "timeline")
            self.assertEqual((cmp.left_only, cmp.right_only, cmp.diff_files), ([], [], []))
            self.assertFalse((Path(tmp) / "agentic_context.json").exists())
            self.assertFalse((Path(tmp) / "agentic").exists())


def _ts(minute):
    return datetime.fromtimestamp(fixture.at(minute), timezone.utc)


def _msg(role, text="", **kw):
    return types.SimpleNamespace(role=role, content=text, text=text, id=kw.pop("id", None), **kw)


def _model(minute, done, inputs, out=None, calls=()):
    tool_calls = [types.SimpleNamespace(id=c, function=f, arguments=a) for c, f, a in calls]
    msg = types.SimpleNamespace(role="assistant", content=out or "", text=out or "", tool_calls=tool_calls or None,
                                id=f"msg-{minute}")
    return types.SimpleNamespace(event="model", timestamp=_ts(minute), completed=_ts(done), input=inputs,
                                 output=types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)]),
                                 pending=False)


class SampleStreamTests(unittest.TestCase):
    def test_stream_and_join(self):
        system = _msg("system", "You are a monitor (synthetic).")
        begin = _msg("user", "Begin.")
        wait1 = ("w1", "mcp__review__wait", {"seconds": 60})
        wait0 = ("w0", "mcp__review__wait", {"seconds": 30})   # discarded: the request was re-sent
        res = _msg("tool", "[tool call id: call_1] Bash ...", tool_call_id="w1", function="mcp__review__wait",
                   error=None)
        counter = _msg("user", "<total_tokens>99 tokens left</total_tokens>")
        implicit = _msg("user", "[tool call id: call_2] Bash ... (implicit delivery)")
        asst = types.SimpleNamespace(role="assistant", content="", text="", tool_calls=[
            types.SimpleNamespace(id="w1", function="mcp__review__wait", arguments={"seconds": 60})], id="a1")
        sample = types.SimpleNamespace(events=[
            _model(0.0, 0.1, [system, begin], calls=[wait0]),
            _model(0.2, 0.3, [system, begin], out="Watching.", calls=[wait1]),
            _model(2.0, 2.1, [system, begin, asst, res, counter], out="(no content)"),
            _model(3.0, 3.1, [system, begin, asst, res, counter, implicit], out="Done."),
        ])
        stream = ag.sample_stream(sample)
        kinds = [(k, p.get("text", p.get("id"))) for k, _, p in stream]
        self.assertEqual(kinds, [("context", "You are a monitor (synthetic)."), ("context", "Begin."),
                                 ("text", "Watching."), ("tool_call", "w1"),
                                 ("tool_result", "[tool call id: call_1] Bash ..."),
                                 ("context", "[tool call id: call_2] Bash ... (implicit delivery)"),
                                 ("text", "Done.")])
        ctx = types.SimpleNamespace(start=fixture.T0, reversions_list=[], msg_by_public={}, agentic=None)
        rows = [{"kind": "agentic_wait", "ts": fixture.at(1.5), "agent": "agmon-001",
                 "payload": {"implicit": False, "clamped_s": 60.0, "requested_s": 60.0, "delivered_calls": 1,
                             "call_ids": ["call_1"], "call_lags_s": [30.0]}},
                {"kind": "agentic_wait", "ts": fixture.at(2.5), "agent": "agmon-001",
                 "payload": {"implicit": True, "clamped_s": 60.0, "delivered_calls": 1, "call_ids": ["call_2"],
                             "call_lags_s": [5.0]}}]
        items = ag.MonitorBuilder(ctx, "agmon-001", rows).build(stream)
        seq = [(it["kind"], (it["event"] or {}).get("type"), it["t"]) for it in items]
        self.assertEqual(seq, [("context", None, 0.0), ("context", None, 0.0), ("text", None, 0.3),
                               ("tool_call", None, 0.3), ("tool_result", None, 1.5), ("event", "GW", 1.5),
                               ("event", "GW", 2.5), ("context", None, 3.0), ("text", None, 3.1)])
        self.assertEqual(items[5]["event"]["detail"]["tool_call_id"], "w1")
        self.assertIsNone(items[6]["event"]["detail"]["tool_call_id"])
        self.assertEqual(items[4]["tool"], "wait")
        self.assertEqual(items[3]["call_id"], "w1")


if __name__ == "__main__":
    unittest.main()
