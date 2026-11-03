"""Offline tests of the story-map pipeline (W3): fake transport only, no network, no key, no paid call.

Run: PYTHONPATH=scripts:src:. .venv/bin/python -m unittest research.analysis.tests.test_storymap_pipeline
"""
from __future__ import annotations

import io
import json
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
skip_unless_available("research/analysis/storymap/prompts.md", "research/analysis/storymap/taxonomy.md")
from research.analysis.judge.client import FakeTransport, fake_completion  # noqa: E402
from research.analysis.storymap import chunk, pipeline  # noqa: E402
from research.analysis.storymap.pipeline_mock import MockStorymapModel, reply, unit_header  # noqa: E402

FIXTURE = REPO / "research" / "analysis" / "tests" / "fixtures" / "storymap"
RUN = FIXTURE / "run"


def item(i, turn, kind="reasoning", text="x", t=None, event=None, tool=None):
    return {"i": i, "t": float(turn if t is None else t), "ts": None, "turn": turn, "kind": kind, "tool": tool,
            "text": text, "event": event}


def ev(i, turn, kind, **fields):
    event = {"type": kind, "id": fields.pop("id", f"{kind}:{i}"), "author": None, "author_role": None, "fake": None,
             "tags": [], "outcome": None, "detail": {}}
    event.update(fields)
    return item(i, turn, "event", f"{kind} event", event=event)


class ChunkTests(unittest.TestCase):
    def test_chunks_cut_only_at_turn_boundaries(self):
        items = [item(n, n // 3, text="w" * 400) for n in range(30)]   # 10 turns of 3 items
        chunks = chunk.chunk_items(items, target_tokens=400)
        self.assertGreater(len(chunks), 1)
        self.assertEqual([i["i"] for c in chunks for i in c["items"]], list(range(30)))
        for left, right in zip(chunks, chunks[1:]):
            self.assertLess(left["items"][-1]["turn"], right["items"][0]["turn"])
        self.assertFalse(any(c["split_turn"] for c in chunks))

    def test_an_oversized_turn_is_split_and_flagged(self):
        items = [item(n, 0, text="w" * 4000) for n in range(6)]
        chunks = chunk.chunk_items(items, target_tokens=1000)
        self.assertTrue(all(c["split_turn"] for c in chunks))
        self.assertEqual(len(chunks), 6)

    def test_render_layout_matches_the_prompts(self):
        line = chunk.render_item(item(3, 2, "tool_call", "a\nb", t=4.5, tool="Bash"))
        self.assertEqual(line, "[i=3 turn=2 t=4.5 tool_call/Bash] a\n    b")
        event = chunk.render_item(ev(4, 2, "B1", author="agent-102", author_role="overseer", tags=["warning"],
                                     detail={"post": "p1"}))
        self.assertTrue(event.startswith("[i=4 turn=2 t=2 event B1 id=B1:4 author=agent-102 role=overseer fake=- "
                                         "tags=warning] B1 event\n    {\"post\":\"p1\"}"))

    def test_stage2_selection(self):
        items = [ev(0, 0, "L2", fake=True), ev(1, 0, "L2", fake=False), ev(2, 1, "B1", author_role="overseer"),
                 ev(3, 1, "B1", author_role="solver"), ev(4, 1, "D1", tags=["describes_trick"]),
                 ev(5, 2, "B1", author_role="overseer", detail={"reread": True}), ev(6, 2, "P2"), ev(7, 3, "S1")]
        chosen = chunk.stage2_events(items)
        self.assertEqual([e["item"]["i"] for e in chosen], [0, 2, 4, 6])
        self.assertTrue(pipeline.overseer_expected(chosen[1]))
        self.assertTrue(pipeline.overseer_expected(chosen[3]))
        self.assertFalse(pipeline.overseer_expected(chosen[2]))

    def test_window_ends_at_the_next_decision_or_ten_turns(self):
        items = [item(n, n) for n in range(40)]
        event = items[20]
        thoughts = [{"code": "D1", "item": 25, "turn": 25}, {"code": "C1", "item": 22, "turn": 22}]
        decision = chunk.decision_turn_after(thoughts, 20)
        window = chunk.event_window(items, event, decision_turn=decision)
        self.assertEqual((window["first_turn"], window["last_turn"], window["end_rule"]), (17, 25, "decision"))
        window = chunk.event_window(items, event, decision_turn=None)
        self.assertEqual((window["last_turn"], window["end_rule"]), (30, "turn_limit"))
        window = chunk.event_window(items, items[35])
        self.assertEqual(window["end_rule"], "end_of_timeline")
        small = chunk.event_window([item(n, n, text="w" * 4000) for n in range(40)], items[20], max_tokens=2500)
        self.assertTrue(small["truncated"])
        self.assertLessEqual(small["first_item"], 20)
        self.assertGreaterEqual(small["last_item"], 20)

    def test_crosscheck_sample_is_seeded(self):
        solvers = [f"agent-{n:03d}" for n in range(1, 21)]
        a = pipeline.crosscheck_sample(solvers, 0.1, 7)
        self.assertEqual(a, pipeline.crosscheck_sample(list(reversed(solvers)), 0.1, 7))
        self.assertEqual(len(a), 2)
        self.assertEqual(pipeline.crosscheck_sample(solvers, 0.0, 7), [])
        self.assertEqual(len(pipeline.crosscheck_sample(solvers[:3], 0.1, 7)), 1)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra, transport=None):
        argv = ["--run-dir", str(RUN), "--timeline", str(FIXTURE), "--out", str(self.out), "--mock", *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = pipeline.main(argv, transport=transport)
        return code, stdout.getvalue(), stderr.getvalue()

    def rows(self, relative):
        return [json.loads(line) for line in (self.out / relative).read_text().splitlines()]

    def test_end_to_end_outputs_caching_pins_and_resume(self):
        model = MockStorymapModel()
        transport = FakeTransport(responder=model)
        code, _, err = self.run_cli("--crosscheck-fraction", "0.34", transport=transport)
        self.assertEqual(code, 0, err)
        for agent in ("agent-001", "agent-002", "agent-003"):
            self.assertTrue((self.out / "stage1" / f"{agent}.jsonl").is_file())
            self.assertTrue((self.out / "stage2" / f"{agent}.jsonl").is_file())
            self.assertTrue((self.out / "stage3" / f"{agent}.json").is_file())
        self.assertEqual(len(self.rows("map.jsonl")), 3)
        record = json.loads((self.out / "pipeline.json").read_text())
        self.assertEqual(record["models"]["stage1"]["model_id"], "anthropic/claude-haiku-5.5")
        self.assertEqual(record["models"]["stage2"]["model_id"], "anthropic/claude-sonnet-5.5")
        self.assertEqual(record["crosscheck"]["model"], "openai/gpt-5.6-terra")
        self.assertEqual(len(record["crosscheck"]["agents"]), 1)
        for stage in ("stage1", "stage2", "stage3"):
            self.assertEqual(len(record["prompts"][stage]["sha256"]), 64)
            self.assertIn("taxonomy_sha256", record["prompts"][stage])
        self.assertEqual(record["prompt_cache"]["normalization"], pipeline.PREFIX_NORMALIZATION)
        self.assertEqual(record["budget"]["limit_usd"], "50")
        self.assertEqual(record["totals"]["requests"], len(transport.requests))
        # Anthropic requests mark the fixed system prefix for caching; the cross-check (OpenAI) does not.
        for request in transport.requests:
            body = request["body"]
            system = body["messages"][0]
            self.assertEqual(system["role"], "system")
            self.assertFalse(body["provider"]["allow_fallbacks"])
            if body["model"].startswith("anthropic/"):
                self.assertEqual(system["content"][0]["cache_control"], {"type": "ephemeral"})
                self.assertEqual(body["provider"]["only"], ["anthropic"])
            else:
                self.assertIsInstance(system["content"], str)
                self.assertEqual(body["provider"]["only"], ["openai"])
        # Stage-1 system prompts are identical across requests (cacheable prefix).
        stage1 = {json.dumps(r["body"]["messages"][0]) for r in transport.requests
                  if unit_header(r["body"]["messages"][1]["content"]).get("stage") == 1
                  and r["body"]["model"].startswith("anthropic/")}
        self.assertEqual(len(stage1), 1)
        # P2 and overseer events get one stage-2 call each; agent-001 has four selected events.
        s2 = self.rows("stage2/agent-001.jsonl")
        self.assertEqual({r["event_type"] for r in s2} >= {"P2"}, True)
        self.assertTrue(all(r["status"] == "ok" for r in s2))
        # Resume: nothing is sent again.
        again = FakeTransport(responder=MockStorymapModel())
        code, _, err = self.run_cli("--crosscheck-fraction", "0.34", transport=again)
        self.assertEqual(code, 0, err)
        self.assertEqual(again.requests, [])
        record = json.loads((self.out / "pipeline.json").read_text())
        self.assertEqual(len(record["invocations"]), 2)
        self.assertTrue(all(r["reused_units"] == r["units"] for r in record["stages"].values()))

    def test_one_repair_then_ok_or_failed(self):
        calls = {"n": 0}

        def responder(request):
            user = request["body"]["messages"][1]["content"]
            header = unit_header(user)
            if header.get("stage") == 1 and header.get("agent") == "agent-002":
                return fake_completion("not json at all", model=request["body"]["model"], provider="Anthropic")
            if header.get("stage") == 1 and header.get("agent") == "agent-001" and "YOUR PREVIOUS REPLY" not in user:
                calls["n"] += 1
                return fake_completion("{\"thoughts\": [}", model=request["body"]["model"], provider="Anthropic")
            return MockStorymapModel()(request)
        transport = FakeTransport(responder=responder)
        code, _, err = self.run_cli("--agents", "agent-001,agent-002", "--crosscheck-model", "none",
                                    transport=transport)
        self.assertEqual(code, 0, err)
        ok = self.rows("stage1/agent-001.jsonl")[0]
        self.assertEqual((ok["status"], ok["reason"]), ("ok", "repaired"))
        bad = self.rows("stage1/agent-002.jsonl")[0]
        self.assertEqual((bad["status"], bad["unit_status"], bad["reason"]), ("failed", "invalid",
                                                                             "malformed_after_repair"))
        self.assertIsNone(bad["thoughts"])
        self.assertIsNone(bad["state"])
        sent = [unit_header(r["body"]["messages"][1]["content"]) for r in transport.requests]
        self.assertEqual(sum(1 for h in sent if h.get("stage") == 1 and h.get("agent") == "agent-002"), 2)
        repairs = [r for r in transport.requests if "YOUR PREVIOUS REPLY" in r["body"]["messages"][1]["content"]]
        self.assertTrue(all(r["body"]["messages"][0]["content"][0]["text"] for r in repairs))
        # The stage-3 request sees the gap explicitly.
        s3 = [r for r in transport.requests if unit_header(r["body"]["messages"][1]["content"]).get("stage") == 3
              and unit_header(r["body"]["messages"][1]["content"]).get("agent") == "agent-002"]
        self.assertIn("a gap in the record", s3[0]["body"]["messages"][1]["content"])

    def test_record_check_problems_are_soft(self):
        def responder(request):
            user = request["body"]["messages"][1]["content"]
            if unit_header(user).get("stage") == 1:
                obj = reply(user)
                obj["state"]["summary"] = "s" * 900   # over the cap: soft (truncated), not a failed unit
                return fake_completion(json.dumps(obj), model=request["body"]["model"], provider="Anthropic")
            return MockStorymapModel()(request)
        code, _, err = self.run_cli("--agents", "agent-001", "--crosscheck-model", "none",
                                    transport=FakeTransport(responder=responder))
        self.assertEqual(code, 0, err)
        row = self.rows("stage1/agent-001.jsonl")[0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["reason"], "repaired")   # one repair asked; its output is valid, checks kept
        self.assertTrue(any("state.summary" in p for p in row["check_problems"]))

    def test_refusal_is_unresolved_never_empty(self):
        def responder(request):
            header = unit_header(request["body"]["messages"][1]["content"])
            if header.get("stage") == 1:
                return fake_completion("", finish_reason="content_filter", model=request["body"]["model"],
                                       provider="Anthropic")
            return MockStorymapModel()(request)
        code, _, err = self.run_cli("--agents", "agent-003", "--crosscheck-model", "none",
                                    transport=FakeTransport(responder=responder))
        self.assertEqual(code, 0, err)
        row = self.rows("stage1/agent-003.jsonl")[0]
        self.assertEqual(row["status"], "unresolved")
        self.assertIsNone(row["thoughts"])

    def test_run_limit_stops_cleanly_and_resume_continues(self):
        code, out, err = self.run_cli("--run-limit", "0.001", "--crosscheck-model", "none",
                                      transport=FakeTransport(responder=MockStorymapModel()))
        self.assertEqual(code, 3, err)
        record = json.loads((self.out / "pipeline.json").read_text())
        self.assertEqual(record["stop"]["reason"], "run_limit")
        rows = [r for a in ("agent-001", "agent-002", "agent-003") for r in self.rows(f"stage1/{a}.jsonl")]
        self.assertTrue(any(r["status"] == "not_run" for r in rows))
        transport = FakeTransport(responder=MockStorymapModel())
        code, _, err = self.run_cli("--run-limit", "5", "--crosscheck-model", "none", transport=transport)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.rows("map.jsonl")), 3)

    def test_agent_concurrency_maps_agents_in_order(self):
        transport = FakeTransport(responder=MockStorymapModel())
        code, _, err = self.run_cli("--agent-concurrency", "1", "--crosscheck-model", "none", transport=transport)
        self.assertEqual(code, 0, err)
        order = [unit_header(r["body"]["messages"][1]["content"]).get("agent") for r in transport.requests]
        self.assertEqual(list(dict.fromkeys(order)), ["agent-001", "agent-002", "agent-003"])
        last = {a: max(n for n, x in enumerate(order) if x == a) for a in set(order)}
        first = {a: min(n for n, x in enumerate(order) if x == a) for a in set(order)}
        self.assertLess(last["agent-001"], first["agent-002"])
        self.assertLess(last["agent-002"], first["agent-003"])

    def test_close_mid_run_with_refusals_stops_without_hanging(self):
        import os
        import signal
        import threading
        sent = {"n": 0}

        def responder(request):
            sent["n"] += 1
            if sent["n"] == 6:
                os.kill(os.getpid(), signal.SIGTERM)   # the pipeline's handler closes the run limit
            if unit_header(request["body"]["messages"][1]["content"]).get("stage") == 2:
                return fake_completion("", finish_reason="content_filter", model=request["body"]["model"],
                                       provider="Anthropic")
            return MockStorymapModel()(request)
        previous = signal.getsignal(signal.SIGTERM)
        done = {}
        watchdog = threading.Timer(60, lambda: os._exit(9))   # a deadlock would hang the suite
        watchdog.start()
        try:
            done["result"] = self.run_cli("--crosscheck-model", "none", transport=FakeTransport(responder=responder))
        finally:
            watchdog.cancel()
            signal.signal(signal.SIGTERM, previous)
        code, _, err = done["result"]
        self.assertEqual(code, 3, err)
        record = json.loads((self.out / "pipeline.json").read_text())
        self.assertEqual(record["stop"]["reason"], "run_limit")

    def test_stage1_bakeoff_writes_one_folder_per_model(self):
        transport = FakeTransport(responder=MockStorymapModel())
        code, _, err = self.run_cli("--stage1-bakeoff", "--agents", "agent-001,agent-002", transport=transport)
        self.assertEqual(code, 0, err)
        for model in ("claude-haiku-5.5", "gpt-5.6-luna", "gemini-3.8-flash"):
            for agent in ("agent-001", "agent-002"):
                self.assertTrue((self.out / "bakeoff" / model / "stage1" / f"{agent}.jsonl").is_file())
            self.assertFalse((self.out / "bakeoff" / model / "stage2").exists())
        self.assertFalse((self.out / "stage1").exists())
        self.assertEqual({r["body"]["model"] for r in transport.requests},
                         {"anthropic/claude-haiku-5.5", "openai/gpt-5.6-luna", "google/gemini-3.8-flash"})
        self.assertEqual({r["body"]["provider"]["only"][0] for r in transport.requests},
                         {"anthropic", "openai", "google-ai-studio"})
        record = json.loads((self.out / "bakeoff" / "pipeline.json").read_text())
        self.assertEqual(set(record["bakeoff"]["models"]), set(pipeline.DEFAULT_BAKEOFF_MODELS))
        # W5 reads each model's stage-1 cost from this one record (keyed by the model's folder).
        from research.analysis.storymap import validate
        costs = validate.bakeoff_costs(self.out / "bakeoff")
        self.assertEqual(set(costs), {"claude-haiku-5.5", "gpt-5.6-luna", "gemini-3.8-flash"})
        self.assertTrue(all(v is not None and v > 0 for v in costs.values()))

    def test_mock_refuses_to_write_into_the_run_dir(self):
        argv = ["--run-dir", str(RUN), "--timeline", str(FIXTURE), "--mock"]
        with redirect_stderr(io.StringIO()):
            self.assertEqual(pipeline.main(argv), 2)

    def test_mock_replies_validate(self):
        from research.analysis.storymap import schema
        items = chunk.load_items(chunk.timeline_folder(FIXTURE), "agent-001")
        user = pipeline.stage1_user("agent-001", chunk.chunk_items(items)[0], 1, None, False)
        schema.validate_stage1(json.dumps(reply(user)), items=items)


if __name__ == "__main__":
    unittest.main()
