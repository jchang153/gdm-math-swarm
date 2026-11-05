"""Story map, overseer mode stage 0: K1/V1/M1/M2 events, activity, stage-2 selection, hard facts, run context.

Fixture: ``fixtures/storymap/overseer/`` (``build_fixture.py`` documents the scenario). Offline, no model calls.
"""
import json
import tempfile
import unittest
from pathlib import Path

from research.analysis.storymap import extract as ex
from research.analysis.storymap import overseer as ov

FIX = Path(__file__).resolve().parent / "fixtures/storymap"
OVFIX = FIX / "overseer"
OV = "agent-102"


def load(folder, agent):
    return [json.loads(line) for line in (Path(folder) / f"{agent}.jsonl").read_text(encoding="utf-8").splitlines()]


class OverseerExtractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.index = ex.extract(OVFIX / "run", cls.tmp.name, workers=1, overseers=True)
        cls.tl = Path(cls.tmp.name) / "timeline"
        cls.items = load(cls.tl, OV)
        cls.events = {it["event"]["id"]: it for it in cls.items if it["kind"] == "event"}
        cls.context = ov.load_context(cls.tl)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ev(self, eid):
        return self.events[eid]["event"]

    def test_selection_and_fixture_current(self):
        # overseers only (never the seed or solvers); agent-103 has no transcript: reported per agent, as for solvers
        self.assertEqual(list(self.index), [OV, "agent-103"])
        self.assertIn("error", self.index["agent-103"])
        self.assertEqual(self.index[OV]["role"], "overseer")
        self.assertEqual(ex.RunContext(OVFIX / "run").overseers(), [OV, "agent-103"])
        for name in (f"{OV}.jsonl", "index.json", "overseer_context.json"):
            self.assertEqual((self.tl / name).read_text(encoding="utf-8"),
                             (OVFIX / "timeline" / name).read_text(encoding="utf-8"), name)

    def test_check_events(self):
        seed = self.ev(f"K1:{OV}:1")
        self.assertEqual((seed["outcome"], seed["fake"], seed["author"], seed["author_role"]),
                         ("not genuine", True, "agent-101", "seed"))
        self.assertTrue(seed["detail"]["seed"] and seed["detail"]["first_check_of_submission"])
        self.assertEqual(seed["detail"]["submission_id"], "sub-000001")
        real = self.ev(f"K1:{OV}:2")
        self.assertEqual((real["outcome"], real["fake"], real["detail"]["checked_author_role"]),
                         ("genuine", False, "solver"))
        bridge = self.ev(f"K1:{OV}:3")      # refused by the MCP bridge: no conference record
        self.assertEqual((bridge["outcome"], bridge["detail"]["ok"], bridge["detail"]["conference_seq"],
                          bridge["detail"]["target"], bridge["author"]),
                         ("failed", False, None, "workspace_file", OV))
        self.assertEqual(bridge["detail"]["error"], "path_outside_allowed_roots")
        dup = self.ev(f"K1:{OV}:4")         # agent-103 checked sub-000002 first
        self.assertEqual((dup["outcome"], dup["detail"]["duplicate"], dup["detail"]["first_check_of_submission"]),
                         ("not genuine", True, False))
        late = self.ev(f"K1:{OV}:5")        # MCP timeout; the conference still recorded a genuine verdict
        self.assertEqual((late["outcome"], late["detail"]["ok"], late["detail"]["conference_verdict"],
                          late["detail"]["verified"], late["detail"]["duplicate"]),
                         ("failed", False, "genuine", True, True))
        self.assertIsNotNone(late["detail"]["conference_seq"])
        for key in ("problem_id", "target", "submission_id", "public_submission_id", "file_path", "checked_author",
                    "checked_author_role", "seed", "verified", "validity", "accepted_at", "ok", "error",
                    "duplicate", "first_check_of_submission"):
            self.assertIn(key, seed["detail"])

    def test_revert_events(self):
        ok = self.ev(f"V1:{OV}:1")
        d = ok["detail"]
        self.assertEqual((ok["outcome"], ok["fake"], ok["author"], d["credited_role"], d["submission_id"],
                          d["library_path"], d["duplicate"], d["ok"]),
                         ("reverted", True, "agent-001", "solver", "sub-000002", "solutions/t2.lean", False, True))
        self.assertAlmostEqual(d["latency_min"], 1.99, places=2)
        bad = self.ev(f"V1:{OV}:2")         # the seed was already reverted by agent-103
        d = bad["detail"]
        self.assertEqual((bad["outcome"], d["ok"], d["duplicate"], d["submission_id"], d["seed"]),
                         ("failed", False, True, "sub-000001", True))
        self.assertIn("no accepted submission", d["error"])

    def test_board_events(self):
        post = self.ev("M2:ovpost-000002")
        self.assertEqual((post["detail"]["ok"], post["author"]), (True, OV))
        failed = self.ev(f"M2:{OV}:failed-1")
        self.assertEqual((failed["detail"]["ok"], failed["detail"]["post_id"]), (False, None))
        reads = [it["event"] for it in self.items if it["kind"] == "event" and it["event"]["type"] == "M1"]
        self.assertEqual([(e["id"], e["detail"]["reread"], e["detail"]["own"]) for e in reads],
                         [("M1:ovpost-000001", False, False), ("M1:ovpost-000002", False, True),
                          ("M1:ovpost-000001", True, False), ("M1:ovpost-000002", True, True)])
        self.assertEqual(reads[0]["author_role"], "overseer")
        first = next(it for it in self.items if it["kind"] == "event" and it["event"]["id"] == "M1:ovpost-000001")
        self.assertTrue(first["text"].startswith("ovpost-000001 by agent-103: agent-103 here"))
        public = self.ev("O1:post-000001")
        self.assertIn("ruling", public["tags"])      # overseer O1 posts are tagged with the overseer role

    def test_activity(self):
        rows = ov.activity(self.items)
        cats = [r["category"] for r in rows]
        for c in ("check", "revert", "board_post", "board_read", "dm_send", "dm_read", "overseer_board_post",
                  "overseer_board_read", "library_list", "library_read", "workspace", "sleep"):
            self.assertIn(c, cats)
        self.assertTrue(set(cats) <= set(ov.CATEGORIES))
        sleep = next(r for r in rows if r["category"] == "sleep")
        self.assertEqual(sleep["seconds"], 300.0)
        pushed = [r for r in rows if r["tool"] == "push"]
        self.assertEqual([(r["category"], r["event_ids"]) for r in pushed],
                         [("board_read", ["B1:post-000002"]), ("dm_read", ["D1:dm-000002"])])
        checks = [r for r in rows if r["category"] == "check"]
        self.assertEqual([r["ok"] for r in checks], [True, True, False, True, False])
        self.assertEqual(checks[0]["event_ids"], [f"K1:{OV}:1"])
        self.assertTrue(all(len(r["summary"]) <= 200 for r in rows))

    def test_classify_call(self):
        def call(tool, text="{}"):
            return {"kind": "tool_call", "tool": tool, "text": text}
        self.assertEqual(ov.classify_call(call("Bash", json.dumps({"command": "sleep 60 && echo x"}))), "sleep")
        self.assertEqual(ov.classify_call(call("Bash", json.dumps({"command": "ls"}))), "workspace")
        self.assertEqual(ov.classify_call(call("TodoWrite")), "workspace")
        self.assertEqual(ov.classify_call(call("get_conference_status")), "status")
        self.assertEqual(ov.classify_call(call("submit_solution")), "submit")
        self.assertEqual(ov.classify_call(call("publish_notebook")), "other")
        self.assertEqual(ov.classify_call(call("mystery")), "other")
        for tool in ex.OVERSEER_TOOLS | ex.COLLAB_TOOLS:
            self.assertIn(ov.classify_call(call(tool)), ov.CATEGORIES)

    def test_stage2(self):
        sel = ov.stage2_events(self.items)
        self.assertEqual([(s["event_id"], s["reason"]) for s in sel], [
            (f"L1:{OV}@1.0", "new_fake_listed"),
            ("L2:solutions/infinite_fermat_primes.lean@2.0", "fake_library_read"),
            (f"K1:{OV}:1", "failed_check"), (f"K1:{OV}:4", "failed_check"),
            ("M1:ovpost-000001", "peer_overseer_post"),
            ("B1:post-000002", "solver_message"), ("D1:dm-000002", "solver_message")])
        self.assertEqual(set(sel[0]), {"event_id", "type", "reason", "item"})

    def test_hard_facts(self):
        hf = ov.hard_facts(OV, self.items, self.index[OV], self.context)
        f = hf["first"]
        self.assertEqual((f["library_list"], f["check_any"], f["check_seed"], f["check_real"],
                          f["check_not_genuine"], f["revert"], f["overseer_board_post"], f["sleep"]),
                         (1.0, 4.0, 4.0, 5.0, 4.0, 10.0, 11.0, 16.0))
        self.assertEqual(f["overseer_board_read"], 11.9)
        # the seed's planted fake (accepted 3.0, reverted 5.99) counts in the accepted and standing fakes
        self.assertEqual(hf["at_first"]["revert"], {"t": 10.0, "solver_fakers": 1, "accepted_fakes": 2,
                                                    "standing_fakes": 0})
        self.assertEqual(hf["at_first"]["check_seed"], {"t": 4.0, "solver_fakers": 0, "accepted_fakes": 1,
                                                        "standing_fakes": 1})
        self.assertEqual(hf["at_first"]["library_list"]["solver_fakers"], 0)
        c = hf["counts"]
        self.assertEqual((c["reverts_ok"], c["reverts_failed"], c["reverts_duplicate"], c["checks_duplicate"],
                          c["dms_to_known_fakers"], c["dms_received"], c["board_posts_read"],
                          c["overseer_board_posts"], c["overseer_board_reads"], c["sleep_seconds"]),
                         (1, 1, 1, 2, 1, 1, 1, 1, 1, 300.0))
        self.assertEqual(c["checks_by_outcome"], {"not genuine": 2, "genuine": 1, "failed": 2})
        self.assertEqual(hf["dm_targets"], [{"t": 15.0, "to": "agent-001", "known_faker": True}])
        self.assertEqual(hf["citable_event_ids"], [s["event_id"] for s in ov.stage2_events(self.items)])
        self.assertTrue(hf["board_enabled"])
        none = ov.hard_facts(OV, self.items, self.index[OV], None)
        self.assertEqual(none["at_first"]["revert"]["solver_fakers"], None)
        self.assertIsNone(none["board_enabled"])

    def test_seed_fake_counts_and_known_faker(self):
        c = self.context
        self.assertEqual([ov.seed_fake_at(c, t) for t in (None, 2.9, 3.0, 5.0, 5.99, 20.0)],
                         [(0, 0), (0, 0), (1, 1), (1, 1), (1, 0), (1, 0)])
        self.assertEqual(ov.seed_agents(c), {"agent-101"})
        legacy = dict(c, seed_fake={k: v for k, v in c["seed_fake"].items() if k != "agent"})
        self.assertEqual(ov.seed_agents(legacy), {"agent-101"})      # contexts written before the fix
        self.assertNotIn("agent-101", ov._known_fakers(c, 2.0))      # not yet accepted
        self.assertIn("agent-101", ov._known_fakers(c, 3.5))
        self.assertEqual(ov.fakes_at(c, 4.0), {"t": 4.0, "solver_fakers": 0, "accepted_fakes": 1, "standing_fakes": 1})
        self.assertEqual(ov.fakes_at(None, 4.0)["accepted_fakes"], None)
        no_seed = dict(c, seed_fake=None)
        self.assertEqual(ov.fakes_at(no_seed, 4.0)["accepted_fakes"], 0)
        dm = [it for it in self.items if (it.get("event") or {}).get("type") == "O1"
              and (it["event"].get("detail") or {}).get("channel") == "dm"]
        self.assertTrue(dm)
        to_seed = [dict(it, event=dict(it["event"], detail=dict(it["event"]["detail"], to="agent-101"))) for it in dm]
        items = [to_seed[dm.index(it)] if it in dm else it for it in self.items]
        hf = ov.hard_facts(OV, items, self.index[OV], c)
        self.assertTrue(all(x["known_faker"] for x in hf["dm_targets"] if x["t"] >= 3.0))
        self.assertEqual(hf["counts"]["dms_to_known_fakers"], sum(x["t"] >= 3.0 for x in hf["dm_targets"]))

    def test_context(self):
        c = self.context
        self.assertEqual((c["schema"], c["run_minutes"], c["overseers"], c["seed"], c["board"]),
                         ("storymap-overseer-context/1", 30.0, [OV, "agent-103"], ["agent-101"], True))
        self.assertEqual(c["fakers_curve"], [[0.0, 0], [8.0, 1]])               # seed excluded; one solver faker
        self.assertEqual(c["accepted_fakes_curve"], [[0.0, 0], [8.0, 1]])
        self.assertEqual(c["standing_fakes_curve"], [[0.0, 0], [8.0, 1], [9.98, 0]])
        self.assertEqual([f["submission_id"] for f in c["solver_fakes"]], ["sub-000002", "sub-000004"])
        self.assertEqual(c["seed_fake"]["reverted_by"], "agent-103")
        self.assertEqual(c["seed_fake"]["agent"], "agent-101")
        self.assertEqual([(x["overseer"], x["submission_id"], x["outcome"]) for x in c["checks"]][2],
                         ("agent-103", "sub-000002", "not genuine"))
        self.assertEqual([x["ok"] for x in c["reverts"]], [True, True, False])
        self.assertEqual(ov.curve_value(c["standing_fakes_curve"], 9.0), 1)
        self.assertEqual(ov.curve_value(c["standing_fakes_curve"], 20.0), 0)


class SolverOutputUnchangedTests(unittest.TestCase):
    def test_shared_fixture_solver_timelines_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex.extract(FIX / "run", tmp, workers=1)
            out = Path(tmp) / "timeline"
            self.assertFalse((out / "overseer_context.json").exists())
            for path in sorted((FIX / "timeline").iterdir()):
                self.assertEqual((out / path.name).read_text(encoding="utf-8"), path.read_text(encoding="utf-8"),
                                 path.name)

    def test_overseers_flag_on_run_without_transcripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = ex.extract(FIX / "run", tmp, workers=1, overseers=True)
            self.assertEqual(list(index), ["agent-102"])      # no transcript: reported per agent, never the seed
            self.assertIn("error", index["agent-102"])
            self.assertTrue((Path(tmp) / "timeline/overseer_context.json").exists())


if __name__ == "__main__":
    unittest.main()
