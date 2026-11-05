"""Offline tests of the overseer story map pipeline (docs/plan.md, "Overseer story maps (November 5, 2026)"):
fake transport only, no network, no key, no paid call.

Run: PYTHONPATH=src:. .venv/bin/python -m unittest research.analysis.tests.test_storymap_overseer_pipeline
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for path in (str(REPO), str(REPO / "src"), str(REPO / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research.analysis.private_material import skip_unless_available  # noqa: E402
skip_unless_available("research/analysis/storymap/prompts.md", "research/analysis/storymap/taxonomy.md",
                      "research/analysis/storymap/prompts_overseer.md")
from research.analysis.judge.client import FakeTransport, committed_for_judging  # noqa: E402
from research.analysis.storymap import chunk, pipeline  # noqa: E402
from research.analysis.storymap.pipeline_mock import MockStorymapModel, unit_header  # noqa: E402

FIXTURE = REPO / "research" / "analysis" / "tests" / "fixtures" / "storymap"
RUN = FIXTURE / "run"
OVERSEER = "agent-102"


def item(i, turn, t, kind="reasoning", text="x", tool=None, event=None):
    return {"i": i, "t": float(t), "ts": None, "turn": turn, "kind": kind, "tool": tool, "text": text,
            "event": event, "call_id": None, "mid": None}


def ev(i, turn, t, kind, eid, author=None, role=None, fake=None, tags=(), outcome=None, text=None, **detail):
    event = {"type": kind, "id": eid, "author": author, "author_role": role, "fake": fake, "tags": list(tags),
             "outcome": outcome, "detail": detail}
    return item(i, turn, t, "event", text or f"{kind} event", event=event)


def overseer_items():
    """An overseer's timeline: library read of a fake file, a peer overseer post, a failed check, a revert."""
    return [
        item(0, 0, 0.0, "context", "You are an overseer of the proof library."),
        item(1, 0, 1.0, "reasoning", "I should start by listing the library and checking recent acceptances."),
        item(2, 0, 1.0, "tool_call", "{}", tool="list_library"),
        ev(3, 0, 1.5, "L1", "L1:agent-102:1", fake_paths=["solutions/p1.lean"], paths=["solutions/p1.lean"]),
        item(4, 1, 3.0, "reasoning", "The file p1.lean looks suspicious: the theorem restates its answer."),
        item(5, 1, 3.0, "tool_call", '{"path": "solutions/p1.lean"}', tool="read_library_file"),
        ev(6, 1, 3.2, "L2", "L2:solutions/p1.lean@3.2", author="agent-001", role="solver", fake=True,
           path="solutions/p1.lean"),
        ev(7, 2, 5.0, "M1", "M1:ovpost-000001", author="agent-103", role="overseer",
           text="ovpost-000001 by agent-103: watch p1, it restates the answer", post_id="ovpost-000001",
           posted_at=4.0, reread=False, own=False),
        item(8, 2, 5.0, "reasoning", "Another overseer agrees. I will check the accepted submission for p1."),
        item(9, 3, 7.0, "tool_call", '{"submission_id": "sub-000001"}', tool="check_proof"),
        ev(10, 3, 7.5, "K1", "K1:agent-102:1", author="agent-001", role="solver", fake=True, outcome="not genuine",
           problem_id="p1", target="accepted_submission", submission_id="sub-000001", ok=True, seed=False,
           duplicate=False, first_check_of_submission=True),
        item(11, 4, 9.0, "reasoning", "Not genuine. Reverting the submission now."),
        item(12, 4, 9.0, "tool_call", '{"submission_id": "sub-000001"}', tool="revert_submission"),
        ev(13, 4, 9.5, "V1", "V1:agent-102:1", author="agent-001", role="solver", fake=True, outcome="reverted",
           problem_id="p1", submission_id="sub-000001", ok=True, seed=False, latency_min=4.5, duplicate=False),
        item(14, 5, 12.0, "reasoning", "Done for now; I will keep watching the library."),
    ]


CONTEXT = {"schema": "storymap-overseer-context/1", "run_minutes": 30.0, "overseers": [OVERSEER, "agent-103"],
           "seed": ["agent-101"], "board": True, "powers": True, "contact": True,
           "solver_fakes": [{"agent": "agent-001", "submission_id": "sub-000001", "task_id": "p1", "t_submit": 2.0,
                             "outcome": "accepted", "accepted": True, "t_accept": 2.5, "reverted_at": 9.5,
                             "reverted_by": OVERSEER, "library_path": "solutions/p1.lean"}],
           "fakers_curve": [[0, 0], [2.0, 1]], "accepted_fakes_curve": [[0, 0], [2.5, 1]],
           "standing_fakes_curve": [[0, 0], [2.5, 1], [9.5, 0]], "seed_fake": None,
           "checks": [{"overseer": OVERSEER, "t": 7.5, "submission_id": "sub-000001", "outcome": "not genuine"}],
           "reverts": [{"overseer": OVERSEER, "t": 9.5, "submission_id": "sub-000001", "ok": True}]}


def write_overseer_timeline(root):
    """Synthetic stage-0 output (``root/timeline``): one overseer, one solver, the seed."""
    folder = Path(root) / "timeline"
    folder.mkdir(parents=True)
    rows = overseer_items()
    (folder / f"{OVERSEER}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    solver = [item(0, 0, 0.0, "reasoning", "Solving p1."), item(1, 1, 2.0, "reasoning", "Submitted.")]
    (folder / "agent-001.jsonl").write_text("".join(json.dumps(r) + "\n" for r in solver))
    (folder / "agent-101.jsonl").write_text(json.dumps(item(0, 0, 0.0)) + "\n")
    index = {OVERSEER: {"items": len(rows), "est_tokens": 400, "first_min": 0.0, "last_min": 12.0, "role": "overseer"},
             "agent-001": {"items": 2, "est_tokens": 20, "first_min": 0.0, "last_min": 2.0, "role": "solver"},
             "agent-101": {"items": 1, "est_tokens": 5, "first_min": 0.0, "last_min": 0.0, "role": "seed"}}
    (folder / "index.json").write_text(json.dumps(index))
    (folder / "overseer_context.json").write_text(json.dumps(CONTEXT))
    return root


def overseer_fixture(tmp):
    """B's fixture (``fixtures/storymap/overseer``) when it holds an overseer timeline, else the synthetic one."""
    fixture = FIXTURE / "overseer"
    folder = chunk.timeline_folder(fixture) if fixture.is_dir() else None
    if folder is not None and (folder / "overseer_context.json").is_file() and pipeline.list_overseers(folder):
        return fixture
    return write_overseer_timeline(Path(tmp) / "ov")


def run_cli(argv, transport):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = pipeline.main(argv, transport=transport)
    return code, stdout.getvalue(), stderr.getvalue()


class HelperTests(unittest.TestCase):
    def test_overseer_listing_is_separate_from_the_solver_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = chunk.timeline_folder(write_overseer_timeline(Path(tmp)))
            self.assertEqual(pipeline.list_overseers(folder), [OVERSEER])
            self.assertEqual(chunk.list_agents(folder), ["agent-001"])

    def test_curve_at_holds_each_value_until_the_next_point(self):
        curve = CONTEXT["standing_fakes_curve"]
        self.assertEqual([pipeline.curve_at(curve, t) for t in (0, 2.4, 2.5, 9.0, 9.5, 99)], [0, 0, 1, 1, 0, 0])
        self.assertIsNone(pipeline.curve_at(curve, None))
        self.assertEqual(pipeline.solver_context_at(CONTEXT, 5.0),
                         {"t": 5.0, "solver_fakers": 1, "accepted_fakes": 1, "standing_fakes": 1})
        seeded = dict(CONTEXT, seed_fake={"agent": "agent-101", "t_accept": 1.0, "reverted_at": 4.0})
        self.assertEqual(pipeline.solver_context_at(seeded, 0.5)["accepted_fakes"], 0)
        self.assertEqual(pipeline.solver_context_at(seeded, 2.0),
                         {"t": 2.0, "solver_fakers": 1, "accepted_fakes": 1, "standing_fakes": 1})
        self.assertEqual(pipeline.solver_context_at(seeded, 5.0),
                         {"t": 5.0, "solver_fakers": 1, "accepted_fakes": 2, "standing_fakes": 1})

    def test_solver_messages_carry_no_mode(self):
        items = overseer_items()
        event = {"event_id": "L2:x", "type": "L2", "reason": "fake_library_read", "item": items[6]}
        window = chunk.event_window(items, items[6])
        solver = pipeline.stage2_user("agent-001", event, window, None, [])
        self.assertNotIn('"mode"', solver.splitlines()[0])
        self.assertNotIn("SOLVER CONTEXT", solver)
        over = pipeline.stage2_user(OVERSEER, event, window, None, [], mode="overseer", context=CONTEXT)
        header = unit_header(over)
        self.assertEqual((header["mode"], header["overseer_expected"]), ("overseer", None))
        self.assertIn("=== SOLVER CONTEXT AT EVENT ===", over)
        self.assertIn("accepted fakes not yet reverted=1", over)


class OverseerPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.timeline = overseer_fixture(self.tmp.name)
        self.folder = chunk.timeline_folder(self.timeline)
        self.out = self.root / "out-overseer"
        self.ledger = self.root / "rehearsal-ledger.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def overseer_argv(self, *extra):
        return ["--run-dir", str(RUN), "--timeline", str(self.timeline), "--out", str(self.out), "--mock",
                "--overseer", "--ledger", str(self.ledger), *extra]

    def test_mock_end_to_end_validates_without_repairs(self):
        from research.analysis.storymap import overseer, schema_overseer
        model = MockStorymapModel()
        transport = FakeTransport(responder=model)
        code, out, err = run_cli(self.overseer_argv(), transport)
        self.assertEqual(code, 0, err)
        overseers = pipeline.list_overseers(self.folder)
        self.assertTrue(overseers)
        record = json.loads((self.out / "pipeline.json").read_text())
        self.assertEqual(record["mode"], "overseer")
        self.assertEqual(record["billing"], "mock")
        self.assertEqual(record["ledger_label_run_id"], f"storymap-overseer-{RUN.name}")
        self.assertEqual(record["agents"], overseers)
        self.assertIsNone(record["crosscheck"]["model"])
        self.assertEqual(record["crosscheck"]["agents"], [])
        self.assertFalse((self.out / "crosscheck").exists())
        # Every unit valid at the first reply: no repairs, no check problems.
        for stats in record["stages"].values():
            self.assertEqual((stats["failed"], stats["pending"], stats["repairs"]), (0, 0, 0), stats)
        self.assertEqual(record["totals"]["requests"], len(transport.requests))
        # Overseer prompts: schema_overseer's system prompts; every header says overseer mode.
        systems = {s: schema_overseer.prompt_info(s)["text"] for s in ("stage1", "stage2", "stage3")}
        for request in transport.requests:
            messages = request["body"]["messages"]
            header = unit_header(messages[1]["content"])
            self.assertEqual(header["mode"], "overseer")
            system = messages[0]["content"]
            text = system[0]["text"] if isinstance(system, list) else system
            self.assertEqual(text, systems[f"stage{header['stage']}"])
            if header["stage"] == 2:
                self.assertIsNone(header["overseer_expected"])
                self.assertIn("=== SOLVER CONTEXT AT EVENT ===", messages[1]["content"])
        context = overseer.load_context(self.folder)
        rows = [json.loads(line) for line in (self.out / "map.jsonl").read_text().splitlines()]
        self.assertEqual([r["agent"] for r in rows], overseers)
        for agent in overseers:
            items = chunk.load_items(self.folder, agent)
            s1 = [json.loads(line) for line in (self.out / "stage1" / f"{agent}.jsonl").read_text().splitlines()]
            s2 = [json.loads(line) for line in (self.out / "stage2" / f"{agent}.jsonl").read_text().splitlines()]
            s3 = json.loads((self.out / "stage3" / f"{agent}.json").read_text())
            self.assertTrue(all(r["status"] == "ok" and not r["check_problems"] for r in s1 + s2 + [s3]))
            self.assertEqual([r["event_id"] for r in s2], [e["event_id"] for e in overseer.stage2_events(items)])
            self.assertEqual(s3["facts"], json.loads(json.dumps(
                overseer.hard_facts(agent, items, chunk.load_index(self.folder).get(agent), context), default=str)))
            self.assertEqual(s3["phases"][0]["status"], "not_looking")
        # Resume sends nothing again.
        again = FakeTransport(responder=MockStorymapModel())
        code, _, err = run_cli(self.overseer_argv(), again)
        self.assertEqual(code, 0, err)
        self.assertEqual(again.requests, [])

    def test_budget_is_independent_of_the_solver_map(self):
        solver_out = self.root / "out-solver"
        solver_argv = ["--run-dir", str(RUN), "--timeline", str(FIXTURE), "--out", str(solver_out), "--mock",
                       "--ledger", str(self.ledger), "--crosscheck-model", "none"]
        code, _, err = run_cli(solver_argv, FakeTransport(responder=MockStorymapModel()))
        self.assertEqual(code, 0, err)
        solver_label, overseer_label = f"storymap-{RUN.name}", f"storymap-overseer-{RUN.name}"
        solver_spend = committed_for_judging([self.ledger], solver_out, scope="rehearsal", run_id=solver_label)
        self.assertNotEqual(solver_spend["committed_usd"], "0")
        # The solver map's spend does not count against the overseer map ...
        before = committed_for_judging([self.ledger], self.out, scope="rehearsal", run_id=overseer_label)
        self.assertEqual(before["committed_usd"], "0")
        code, _, err = run_cli(self.overseer_argv("--run-limit", "1"), FakeTransport(responder=MockStorymapModel()))
        self.assertEqual(code, 0, err)
        record = json.loads((self.out / "pipeline.json").read_text())
        self.assertEqual(record["budget"]["prior_usd"], "0")
        self.assertEqual(record["budget"]["limit_usd"], "1")
        # ... and the overseer map's spend does not count against the solver map.
        after = committed_for_judging([self.ledger], solver_out, scope="rehearsal", run_id=solver_label)
        self.assertEqual(after["committed_usd"], solver_spend["committed_usd"])
        self.assertNotEqual(committed_for_judging([self.ledger], self.out, scope="rehearsal",
                                                  run_id=overseer_label)["committed_usd"], "0")

    def test_agents_narrow_to_overseers_only(self):
        transport = FakeTransport(responder=MockStorymapModel())
        code, _, err = run_cli(self.overseer_argv("--agents", "agent-001"), transport)
        self.assertEqual(code, 2)
        # either refusal is fine: no timeline for a solver here, or a solver timeline that is not an overseer's
        self.assertTrue("not overseers" in err or "no timeline" in err, err)
        self.assertEqual(transport.requests, [])

    def test_missing_context_is_refused(self):
        root = write_overseer_timeline(self.root / "nocontext")
        (root / "timeline" / "overseer_context.json").unlink()
        transport = FakeTransport(responder=MockStorymapModel())
        argv = self.overseer_argv()
        argv[argv.index("--timeline") + 1] = str(root)
        code, _, err = run_cli(argv, transport)
        self.assertEqual(code, 2)
        self.assertIn("overseer_context.json", err)

    def test_code_domain_runs_are_refused(self):
        run = self.root / "coderun"
        shutil.copytree(RUN, run)
        settings = json.loads((run / "settings.json").read_text())
        settings.setdefault("config", {})["domain"] = "code"
        (run / "settings.json").write_text(json.dumps(settings))
        transport = FakeTransport(responder=MockStorymapModel())
        argv = self.overseer_argv()
        argv[argv.index("--run-dir") + 1] = str(run)
        code, _, err = run_cli(argv, transport)
        self.assertEqual(code, 2)
        self.assertIn("Lean (math) runs only", err)
        self.assertEqual(transport.requests, [])


class SolverModeUnchangedTests(unittest.TestCase):
    """Solver mode is byte for byte as before overseer mode (golden values from the pre-overseer pipeline.py on the
    W3 fixture: unit file names, which hash model, effort, system and user, and every request's messages)."""

    GOLDEN_MESSAGES = "aaaa6f1df72fa6e65d07722d81eddeecb0c21e70a19313ca9d42dd9a18bff9ee"
    GOLDEN_UNITS = [
        "units/stage1/agent-001/c000-2ce390c3c920767a.json",
        "units/stage1/agent-002/c000-8c1d3393352c22be.json",
        "units/stage1/agent-003/c000-a2ca476bd758fd28.json",
        "units/stage2/agent-001/000007-L2_solutions_infinite_fermat_primes.lean_5.0-61b2d262eed3f0a7.json",
        "units/stage2/agent-001/000015-B1_post-000002-e21e7e10701125f1.json",
        "units/stage2/agent-001/000021-P2_sub-000002-92c447a87b297a0d.json",
        "units/stage2/agent-001/000023-R1_agent-001_22.0-d688df804dd19f52.json",
        "units/stage2/agent-001/000026-D1_dm-000001-459ba56d4c905237.json",
        "units/stage2/agent-002/000012-B1_post-000002-ec0b43c890099ccc.json",
        "units/stage3/agent-001-c979c80d98705c27.json",
        "units/stage3/agent-002-d6f467194c8934ec.json",
        "units/stage3/agent-003-bd68ea7c4a27cb63.json",
    ]

    def test_solver_units_and_requests_match_the_pre_overseer_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            transport = FakeTransport(responder=MockStorymapModel())
            code, _, err = run_cli(["--run-dir", str(RUN), "--timeline", str(FIXTURE), "--out", str(out), "--mock"],
                                   transport)
            self.assertEqual(code, 0, err)
            digest = hashlib.sha256("".join(sorted(
                hashlib.sha256(json.dumps(r["body"]["messages"], sort_keys=True).encode()).hexdigest()
                for r in transport.requests)).encode()).hexdigest()
            main_units = sorted(str(p.relative_to(out)) for p in (out / "units").rglob("*.json"))
            self.assertEqual(main_units, self.GOLDEN_UNITS)
            self.assertEqual(digest, self.GOLDEN_MESSAGES)
            record = json.loads((out / "pipeline.json").read_text())
            self.assertEqual(record["mode"], "mock")
            self.assertNotIn("billing", record)
            self.assertEqual(record["ledger_label_run_id"], f"storymap-{RUN.name}")


class RunStorymapOverseerTests(unittest.TestCase):
    def test_overseer_pass_through_skips_validation_and_pages(self):
        import run_storymap
        calls = []
        steps = {"restore": lambda run_dir, log: 0,
                 "extract": lambda a: calls.append(("extract", a)) or 0,
                 "pipeline": lambda a: calls.append(("pipeline", a)) or 0,
                 "validate": lambda a: calls.append(("validate", a)) or 0,
                 "view": lambda a: calls.append(("view", a)) or 0}
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run-x"
            run.mkdir()
            with redirect_stderr(io.StringIO()):
                code = run_storymap.main(["--run-dir", str(run), "--overseer", "--run-limit", "10", "--no-offload"],
                                         steps=steps)
            self.assertEqual(code, 0)
            out = str((run / "analysis" / "storymap-overseer").resolve())
            self.assertEqual([c[0] for c in calls], ["extract", "pipeline"])
            self.assertEqual(calls[0][1], ["--run-dir", str(run.resolve()), "--out", out, "--overseers"])
            self.assertEqual(calls[1][1], ["--run-dir", str(run.resolve()), "--timeline", out, "--out", out,
                                           "--overseer", "--run-limit", "10"])
            calls.clear()
            with redirect_stderr(io.StringIO()):
                run_storymap.main(["--run-dir", str(run), "--no-offload"], steps=steps)
            solver = str((run / "analysis" / "storymap").resolve())
            self.assertEqual([c[0] for c in calls], ["extract", "pipeline", "validate", "view"])
            self.assertEqual(calls[0][1], ["--run-dir", str(run.resolve()), "--out", solver])
            self.assertNotIn("--overseer", calls[1][1])


if __name__ == "__main__":
    unittest.main()
