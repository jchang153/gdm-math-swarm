"""Offline tests for the judge's full-history reconstruction (research/analysis/judge/history.py).

A synthetic run directory is built with the real conference service (fake judge, fixed
clock) and a real Inspect ``.eval`` log whose tool results are the service's own replies,
so joins, crosswalks and exposure use the production record shapes. No model, network or
Docker calls; Inspect's ``.env`` loading is disabled before it is imported.

Run: PYTHONPATH=src:. .venv/bin/python -m unittest discover -s research/analysis/tests
"""
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO / "src", REPO):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from research_swarm.inspect_adapter import disable_inspect_dotenv  # noqa: E402

disable_inspect_dotenv()
from research.analysis.judge import history  # noqa: E402

try:
    import inspect_ai  # noqa: F401  (after disable_inspect_dotenv)
    HAVE_INSPECT = True
except Exception:  # pragma: no cover
    HAVE_INSPECT = False

CACHE100 = REPO / "runs" / "cache100-20261016T020259Z"
T0 = 1792116900.0
TEMPLATE_A = "import Mathlib\n\ntheorem t_a : True := by\n  sorry\n"
TEMPLATE_B = "import Mathlib\n\ntheorem t_b : answer(default) ↔ True := by\n  sorry\n"
LONG_POST = "Hint about sSup junk values. " + "x" * 2500          # listed boards shorten it to 2000
NOTEBOOK = "# agent-002 notebook\n" + "".join(f"line {i}: technique notes\n" for i in range(40))
SYSTEM = "x-anthropic-billing-header: cc_version=1.a;\nYou are an agent. Integrity rules apply."


def _moment(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def _write_snapshot(run, agent, payload_path, content, when, runtime, mode="full", offset=None, full=None):
    """Store a content-addressed blob and its runtime record (append blobs are deltas)."""
    data = content.encode("utf-8")
    full_data = (full if full is not None else content).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    folder = run / "snapshots" / agent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / digest).write_bytes(data)
    payload = {"path": payload_path, "sha256": hashlib.sha256(full_data).hexdigest(), "bytes": len(full_data),
               "mtime_ns": int(when * 1e9), "mode": mode, "blob_sha256": digest, "blob_bytes": len(data)}
    if offset is not None:
        payload["offset"] = offset
    runtime.append({"timestamp": when, "kind": "file_snapshot", "agent_id": agent, "payload": payload})


def build_synthetic_run(root):
    """A tiny two-agent run directory; returns (run_dir, facts)."""
    from inspect_ai.event import CompactionEvent, ModelEvent
    from inspect_ai.log import EvalConfig, EvalDataset, EvalLog, EvalSample, EvalSpec, write_eval_log
    from inspect_ai.model import (ChatMessageAssistant, ChatMessageSystem, ChatMessageTool, ChatMessageUser,
                                  ContentReasoning, ContentText, GenerateConfig, ModelOutput)
    from inspect_ai.tool import ToolCall
    from research_swarm.conference import ConferenceService, ConferenceTask

    run = Path(root) / "synthetic-20990101T000000Z"
    run.mkdir(parents=True)
    clock = Clock()
    tasks = [ConferenceTask("t_a", "Task A", "T.t_a", False, TEMPLATE_A),
             ConferenceTask("t_b", "Task B", "T.t_b", True, TEMPLATE_B)]
    judge = lambda task, source: SimpleNamespace(  # noqa: E731
        accepted=True, stage="accepted", reason="Submission accepted.",
        compile_result=SimpleNamespace(status="success", exit_code=0, sorry_count=0, stdout="", stderr="",
                                       elapsed_seconds=0.1, queue_seconds=0.0, output_truncated=False,
                                       timeout_source=None))
    service = ConferenceService(run, tasks, judge, agent_ids=["agent-001", "agent-002"], clock=clock)
    service.mark_run_started(T0)

    def act(agent, tool, args, when):
        clock.now = when
        return json.dumps(service.action(agent, tool, args), ensure_ascii=False)

    # agent-002 (one turn, four conference calls) acts first.
    replies2 = [act("agent-002", "post_intent", {"intent_type": "contribution", "message": LONG_POST}, T0 + 1.0),
                act("agent-002", "send_direct_message", {"agent_id": "agent-001",
                                                         "message": "Private: try a small witness."}, T0 + 2.0),
                act("agent-002", "publish_notebook", {"file_path": "/workspace/nb.md", "content": NOTEBOOK},
                    T0 + 2.5),
                act("agent-002", "submit_solution", {"problem_id": "t_a", "source": TEMPLATE_A.replace(
                    "sorry", "trivial")}, T0 + 2.7)]
    # agent-001.
    post1 = act("agent-001", "post_intent", {"intent_type": "exploring", "message": "agent-001 starts t_b."},
                T0 + 5.0)
    board = act("agent-001", "get_bulletin_board", {"limit": 10}, T0 + 8.0)
    inbox = act("agent-001", "get_messages", {"limit": 10}, T0 + 13.0)
    library = act("agent-001", "read_library_file", {"path": "notebooks/agent-002/workspace/nb.md",
                                                     "limit": 200}, T0 + 13.5)
    closed = act("agent-001", "submit_solution", {"problem_id": "t_a", "source": "theorem x : True := trivial"},
                 T0 + 14.0)
    service.close()

    def call(cid, function, **arguments):
        return ToolCall(id=cid, function=function, arguments=arguments)

    def tool(mid, cid, function, content):
        return ChatMessageTool(id=mid, content=content, tool_call_id=cid, function=function)

    def counter(mid, value):
        return ChatMessageUser(id=mid, content=f"<total_tokens>{value} tokens left</total_tokens>")

    def model(when, done, inputs, message, stop="tool_calls"):
        output = ModelOutput.from_message(message, stop_reason=stop)
        return ModelEvent(model="mockllm/model", input=inputs, tools=[], tool_choice="auto",
                          config=GenerateConfig(), output=output, timestamp=_moment(when),
                          completed=_moment(done))

    sys1, user1 = ChatMessageSystem(id="s-1", content=SYSTEM), ChatMessageUser(id="u-1", content="Begin.")
    out0 = ChatMessageAssistant(id="out-0", content=[ContentReasoning(reasoning="R0: plan the survey."),
                                                     ContentText(text="Surveying.")],
                                tool_calls=[call("c-a", "Bash", command="ls /workspace"),
                                            call("c-b", "mcp__conference__post_intent", intent_type="exploring",
                                                 message="agent-001 starts t_b.")])
    res_a = tool("tr-a", "c-a", "Bash", "problems\nRESEARCH.md <script>alert(1)</script>")
    res_b = tool("tr-b", "c-b", "mcp__conference__post_intent", post1)
    out1 = ChatMessageAssistant(id="out-1", content=[ContentReasoning(reasoning="R1: maybe a loophole?"),
                                                     ContentText(text="")],
                                tool_calls=[call("c-c", "mcp__conference__get_bulletin_board", limit=10),
                                            call("c-d", "Bash", command="cat notes.txt")])
    res_c = tool("tr-c", "c-c", "mcp__conference__get_bulletin_board", board)
    request = ChatMessageUser(id="u-req", content="CRITICAL: Respond with TEXT ONLY. Summarize.")
    out2 = ChatMessageAssistant(id="out-2", content="<analysis>a</analysis><summary>I posted; R1 noted.</summary>")
    sys2 = ChatMessageSystem(id="s-2", content=SYSTEM.replace("1.a", "1.b"))
    cont = ChatMessageUser(id="u-cont", content=history.CONTINUATION_MARK + " ... Summary: I posted.")
    replay1 = ChatMessageAssistant(id="replay-1", content=[ContentReasoning(reasoning="<think>R1</think>")],
                                   tool_calls=out1.tool_calls)
    res_d = tool("tr-d", "c-d", "Bash", "notes after compaction")
    out3 = ChatMessageAssistant(id="out-3", content=[ContentReasoning(reasoning="R3: read inbox and library.")],
                                tool_calls=[call("c-e", "mcp__conference__get_messages", limit=10),
                                            call("c-f", "mcp__conference__read_library_file",
                                                 path="notebooks/agent-002/workspace/nb.md", limit=200),
                                            call("c-g", "mcp__conference__submit_solution", problem_id="t_a",
                                                 source="theorem x : True := trivial")])
    res_e = tool("tr-e", "c-e", "mcp__conference__get_messages", inbox)
    res_f = tool("tr-f", "c-f", "mcp__conference__read_library_file", library)
    res_g = tool("tr-g", "c-g", "mcp__conference__submit_solution", closed)
    events1 = [
        model(T0 + 3, T0 + 4, [sys1, user1], out0),
        model(T0 + 6, T0 + 7, [sys1, user1, out0, res_a, res_b, counter("k-1", 900)], out1),
        model(T0 + 9, T0 + 10, [sys1, user1, out0, res_a, res_b, counter("k-1", 900), out1, res_c, request,
                                counter("k-2", 800)], out2, stop="stop"),
        CompactionEvent(timestamp=_moment(T0 + 10.5), source="claude_code"),
        model(T0 + 11, T0 + 12, [sys2, cont, replay1, res_d, counter("k-3", 700)], out3),
    ]
    sample1 = EvalSample(id="agent-001", epoch=1, input="Begin.", target="",
                         messages=[sys2, cont, replay1, res_d, out3, res_e, res_f, res_g], events=events1)
    calls2 = [call("d-1", "mcp__conference__post_intent", intent_type="contribution", message=LONG_POST),
              call("d-2", "mcp__conference__send_direct_message", agent_id="agent-001",
                   message="Private: try a small witness."),
              call("d-3", "mcp__conference__publish_notebook", file_path="/workspace/nb.md"),
              call("d-4", "mcp__conference__submit_solution", problem_id="t_a", file_path="/workspace/a.lean")]
    out20 = ChatMessageAssistant(id="out2-0", content=[ContentReasoning(reasoning="Share findings.")],
                                 tool_calls=calls2)
    results2 = [tool(f"tr2-{n}", c.id, c.function, r) for n, (c, r) in enumerate(zip(calls2, replies2), 1)]
    sample2 = EvalSample(id="agent-002", epoch=1, input="Begin.", target="",
                         messages=[sys1, user1, out20] + results2,
                         events=[model(T0 + 0.5, T0 + 0.8, [sys1, user1], out20)])
    (run / "inspect").mkdir()
    write_eval_log(EvalLog(eval=EvalSpec(created=_moment(T0).isoformat(), task="gdm_math_swarm",
                                         dataset=EvalDataset(), model="mockllm/model", config=EvalConfig()),
                           samples=[sample1, sample2], status="success"), str(run / "inspect" / "synthetic.eval"))

    runtime = [{"timestamp": T0 - 30, "kind": "agent_configured", "agent_id": "agent-001",
                "payload": {"persona": "algebra"}},
               {"timestamp": T0 - 30, "kind": "agent_configured", "agent_id": "agent-002",
                "payload": {"persona": "combinatorics"}}]
    # Provisioned before the common start: template copy and empty notes (excluded).
    _write_snapshot(run, "agent-001", "/workspace/problems/t_a.lean", TEMPLATE_A, T0 - 5, runtime)
    _write_snapshot(run, "agent-001", "/workspace/RESEARCH.md", "", T0 - 5, runtime)
    runtime.append({"timestamp": T0, "kind": "common_start", "agent_id": None, "payload": {"agent_count": 2}})
    session = "/home/researcher/.claude/projects/-workspace/abc.jsonl"
    _write_snapshot(run, "agent-001", session, '{"a":1}\n', T0 + 3, runtime)
    _write_snapshot(run, "agent-001", session, '{"b":2}\n', T0 + 12, runtime, mode="append", offset=8,
                    full='{"a":1}\n{"b":2}\n')
    _write_snapshot(run, "agent-001", "/workspace/RESEARCH.md", "notes v1\n", T0 + 6.5, runtime)
    _write_snapshot(run, "agent-001", "/workspace/work/copy.lean", TEMPLATE_A, T0 + 7.5, runtime)
    _write_snapshot(run, "agent-001", "/workspace/problems/t_a.lean", TEMPLATE_A.replace("sorry", "trivial"),
                    T0 + 8.5, runtime)
    _write_snapshot(run, "agent-001", "/workspace/work/log.jsonl", '{"step":1}\n', T0 + 9.5, runtime)
    _write_snapshot(run, "agent-001", "/workspace/work/log.jsonl", '{"step":2}\n', T0 + 11.5, runtime,
                    mode="append", offset=11, full='{"step":1}\n{"step":2}\n')
    _write_snapshot(run, "agent-001", "/workspace/RESEARCH.md", "notes v1\nmore\n", T0 + 9.6, runtime)
    _write_snapshot(run, "agent-001", "/workspace/RESEARCH.md", "notes v1\n", T0 + 12.5, runtime)
    runtime += [{"timestamp": T0 + 20, "kind": "agent_exit", "agent_id": a,
                 "payload": {"status": "harness_completed"}} for a in ("agent-001", "agent-002")]
    runtime.append({"timestamp": T0 + 21, "kind": "run_stop", "agent_id": None,
                    "payload": {"reason": "max_run_seconds"}})
    (run / "runtime.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runtime))
    (run / "settings.json").write_text(json.dumps({"personas": {"agent-001": "algebra",
                                                                "agent-002": "combinatorics"}}))
    (run / "evaluations").mkdir()
    (run / "evaluations" / "evaluations.jsonl").write_text(json.dumps({
        "schema": "gdm-evaluation/1", "validity": "genuine", "reason": "synthetic", "review_flags": [],
        "task_id": "t_a", "submission": {"submission_id": "sub-000001", "agent_id": "agent-002",
                                         "outcome": "accepted"}}) + "\n")
    facts = {"post1": json.loads(post1), "board": json.loads(board), "inbox": json.loads(inbox),
             "library": json.loads(library), "closed": json.loads(closed)}
    return run, facts


@unittest.skipUnless(HAVE_INSPECT, "inspect_ai not installed")
class SyntheticRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.run_dir, cls.facts = build_synthetic_run(cls.tmp.name)
        cls.built = history.build_run(cls.run_dir)
        cls.a1 = cls.built.agents["agent-001"]
        cls.a2 = cls.built.agents["agent-002"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ids(self, history_=None):
        return [i["id"] for i in (history_ or self.a1).items]

    def test_item_shape_matches_interface(self):
        keys = {"id", "agent_id", "kind", "channel", "turn", "time", "time_bounds", "text", "authored_by_agent",
                "visible_to_agent", "meta"}
        kinds = {"reasoning", "text", "tool_call", "tool_result", "summary", "file_version", "conference",
                 "runtime", "host"}
        for item in self.a1.items + self.a1.host_context + self.a1.harness_items:
            self.assertEqual(set(item), keys, item["id"])
            self.assertIn(item["kind"], kinds)
            self.assertIsInstance(item["text"], str)
        self.assertTrue(all(not h["visible_to_agent"] and h["kind"] == "host" for h in self.a1.host_context))

    def test_compaction_segments_and_summary_turn(self):
        stats = self.a1.stats
        self.assertEqual((stats["turns"], stats["summary_turns"], stats["compactions"], stats["segments"]),
                         (4, 1, 1, 2))
        self.assertEqual(self.a1.segments[0], {"segment": 0, "first_turn": 0, "last_turn": 2, "summary_turn": 2})
        self.assertEqual(self.a1.segments[1]["first_turn"], 3)
        summary = self.a1.get("A01.t0002.s")
        self.assertEqual(summary["kind"], "summary")
        self.assertTrue(summary["meta"]["derived"])
        self.assertIn("<summary>I posted", summary["text"])
        self.assertIsNone(self.a1.get("A01.t0002.x"))
        # Pre-compaction turns come from model outputs, not from sample.messages.
        self.assertEqual(self.a1.get("A01.t0000.r")["text"], "R0: plan the survey.")
        self.assertEqual(self.a1.get("A01.t0001.r")["text"], "R1: maybe a loophole?")
        self.assertEqual(self.a1.get("A01.t0003.r")["meta"]["segment"], 1)
        self.assertIsNone(self.a1.get("A01.t0001.x"))   # empty visible text is not an item

    def test_tool_results_deduplicated_and_bounded(self):
        results = [i for i in self.a1.items if i["kind"] == "tool_result"]
        self.assertEqual(sorted(r["meta"]["call_id"] for r in results),
                         ["c-a", "c-b", "c-c", "c-d", "c-e", "c-f", "c-g"])
        first = self.a1.get("A01.t0000.o1")
        self.assertEqual(first["meta"]["copies"], 2)
        self.assertEqual(first["time_bounds"], [history.iso(T0 + 4), history.iso(T0 + 6)])
        # c-d's result first appears in the next segment's input.
        late = self.a1.get("A01.t0001.o2")
        self.assertEqual(late["text"], "notes after compaction")
        self.assertEqual(late["meta"]["first_seen_turn"], 3)
        self.assertEqual(late["time_bounds"][1], history.iso(T0 + 11))
        # The final turn's results exist only in sample.messages; bounded by the exit.
        final = self.a1.get("A01.t0003.o1")
        self.assertIsNone(final["meta"]["first_seen_turn"])
        self.assertEqual(final["time_bounds"][1], history.iso(T0 + 20))
        self.assertEqual(self.a1.stats["missing_tool_results"], [])

    def test_tool_call_rendering(self):
        call = self.a1.get("A01.t0000.c1")
        self.assertEqual(call["text"], "Bash\ncommand: ls /workspace")
        self.assertEqual(call["channel"], "tool_action")
        self.assertEqual(self.a1.get("A01.t0000.c2")["channel"], "board")
        self.assertEqual(self.a1.get("A01.t0003.c3")["channel"], "submission")

    def test_conference_joins_and_crosswalk(self):
        conf = self.built.conference
        for item in self.a1.items + self.a2.items:
            if item["kind"] == "tool_result" and item["meta"].get("conference"):
                self.assertEqual(item["meta"]["conference"]["status"], "joined", item["id"])
        post = self.a1.get("A01.t0000.o2")
        self.assertEqual(conf.events_by_seq[post["meta"]["conference"]["seq"]]["kind"], "board_post")
        self.assertEqual(post["time"], history.iso(T0 + 5))
        inbox = self.a1.get("A01.t0003.o1")
        public = self.facts["inbox"]["messages"][0]["message_id"]
        self.assertEqual(inbox["meta"]["crosswalk"], {public: "dm-000001"})
        closed = self.a1.get("A01.t0003.o3")
        self.assertEqual(closed["meta"]["crosswalk"], {self.facts["closed"]["submission_id"]: "sub-000002"})
        self.assertEqual(len(closed["meta"]["conference"]["seqs"]), 2)   # received + resolved
        lib = self.a1.get("A01.t0003.o2")
        self.assertEqual(lib["meta"]["crosswalk"], {"notebooks/agent-002/workspace/nb.md@1": "lib-000001"})
        self.assertEqual(conf.crosswalk["internal_to_public"]["dm-000001"], public)

    def test_exposure_ledger_is_exactly_what_was_returned(self):
        ledger = self.a1.exposure_ledger
        by_kind = {e["kind"]: e for e in ledger}
        self.assertEqual(set(by_kind), {"board_post", "dm", "library_read"})
        self.assertFalse(any(e["author"] == "agent-001" for e in ledger))   # own content is not exposure
        board = by_kind["board_post"]
        self.assertEqual((board["source_id"], board["author"], board["item_id"]),
                         ("post-000001", "agent-002", "A01.t0001.o1"))
        self.assertTrue(board["truncated"])
        self.assertEqual(board["text_returned"], LONG_POST[:2000])
        self.assertEqual(board["time"], history.iso(T0 + 8))
        self.assertIsNotNone(board["seq"])
        dm = by_kind["dm"]
        self.assertEqual((dm["source_id"], dm["text_returned"]), ("dm-000001", "Private: try a small witness."))
        self.assertTrue(dm["first_delivery"])
        lib = by_kind["library_read"]
        self.assertEqual(lib["text_returned"], self.facts["library"]["content"])
        self.assertLess(len(lib["text_returned"]), len(NOTEBOOK))
        self.assertTrue(lib["truncated"])
        self.assertEqual(lib["range"], [0, len(self.facts["library"]["content"])])
        self.assertEqual([e["time"] for e in ledger], sorted(e["time"] for e in ledger))
        for entry in ledger:
            self.assertTrue({"item_id", "source_id", "author", "text_returned", "time", "seq"} <= set(entry))
        # The DM sender's history has no exposure to its own message.
        self.assertEqual(self.a2.exposure_ledger, [])

    def test_own_conference_records_in_chronological_place(self):
        ids = self.ids()
        self.assertIn("post-000002", ids)
        self.assertLess(ids.index("A01.t0000.o2"), ids.index("post-000002"))
        self.assertLess(ids.index("post-000002"), ids.index("A01.t0001.r"))
        record = self.a1.get("post-000002")
        self.assertEqual((record["meta"]["call_item"], record["turn"], record["channel"]), ("A01.t0000.c2", 0, "board"))
        self.assertIn("sub-000002", ids)
        self.assertEqual(self.a1.get("sub-000002")["meta"]["outcome"], "problem_closed")
        a2 = self.ids(self.a2)
        for record_id in ("post-000001", "dm-000001", "lib-000001", "sub-000001", "lib-000002"):
            self.assertIn(record_id, a2)
        self.assertTrue(self.a2.get("lib-000002")["meta"]["automatic"])   # accepted solution
        self.assertEqual(self.a2.get("lib-000001")["channel"], "notebook")
        self.assertIsNotNone(self.a2.get("dm-000001")["meta"]["host_only"]["delivered_at"])
        # Turn blocks (parts, then each call's result) are keyed by the turn's completion;
        # conference records and file captures are interleaved by their own times.
        completed = {i["turn"]: i["time"] for i in self.a1.items if i["kind"] in ("reasoning", "summary", "tool_call")}
        keys = [history._epoch(completed[i["turn"]] if i["kind"] in ("reasoning", "text", "summary", "tool_call",
                                                                    "tool_result") else i["time"])
                for i in self.a1.items]
        self.assertEqual(keys, sorted(keys))

    def test_private_file_versions(self):
        files = {i["id"]: i for i in self.a1.items if i["kind"] == "file_version"}
        self.assertEqual(set(files), {"A01.f[/workspace/RESEARCH.md]@2", "A01.f[/workspace/RESEARCH.md]@3",
                                      "A01.f[/workspace/problems/t_a.lean]@2",
                                      "A01.f[/workspace/work/log.jsonl]@1", "A01.f[/workspace/work/log.jsonl]@2"})
        self.assertEqual(files["A01.f[/workspace/work/log.jsonl]@2"]["text"], '{"step":1}\n{"step":2}\n')
        self.assertEqual(files["A01.f[/workspace/work/log.jsonl]@2"]["meta"]["integrity"], "ok")
        revisited = files["A01.f[/workspace/RESEARCH.md]@2"]
        self.assertEqual(revisited["meta"]["reobserved_at"], [history.iso(T0 + 12.5)])
        third = files["A01.f[/workspace/RESEARCH.md]@3"]
        self.assertEqual(third["meta"]["previous_version"], "A01.f[/workspace/RESEARCH.md]@2")
        self.assertIn("+more", history.file_version_diff(self.a1, third))
        self.assertIsNone(history.file_version_diff(self.a1, revisited))
        excluded = self.a1.stats["files_excluded"]
        self.assertEqual(excluded["session_transcript"], 2)
        self.assertEqual(excluded["unmodified_provisioned"], 3)   # template, empty notes, template copy
        self.assertFalse(any("abc.jsonl" in i for i in files))

    def test_harness_items_kept_apart(self):
        kinds = [h["meta"]["harness_kind"] for h in self.a1.harness_items]
        self.assertIn("compaction_request", kinds)
        self.assertIn("compaction_continuation", kinds)
        system = self.a1.harness_items[0]
        self.assertEqual(system["id"], "A01.t0000.u1")
        self.assertEqual(system["meta"]["repeat_turns"], [3])   # same prompt, new billing hash
        self.assertFalse(any("<total_tokens>" in h["text"] for h in self.a1.harness_items))
        self.assertFalse(any(i["kind"] == "runtime" for i in self.a1.items))
        continuation = next(h for h in self.a1.harness_items if h["meta"]["harness_kind"] == "compaction_continuation")
        self.assertTrue(continuation["meta"]["derived"])

    def test_host_context_and_lookup(self):
        host_ids = [h["id"] for h in self.a2.host_context]
        self.assertEqual(host_ids[:2], ["HOST.exit.A02", "HOST.eval.sub-000001"])
        self.assertEqual(sum(1 for i in host_ids if i.startswith("HOST.defect.")), 14)
        self.assertNotIn("HOST.eval.sub-000001", [h["id"] for h in self.a1.host_context])
        self.assertEqual(self.built.host["personas"]["agent-001"], "algebra")
        self.assertEqual(self.built.host["start_barrier_time"], history.iso(T0))
        self.assertEqual(self.built.lookup("post-000001")["agent_id"], "agent-002")
        self.assertEqual(self.built.lookup("A01.t0002.s#f2")["kind"], "summary")
        self.assertEqual(self.built.lookup("seq-000001")["kind"], "conference")
        self.assertEqual(self.built.lookup("RT.L00001")["meta"]["kind"], "agent_configured")
        defect = json.loads(self.built.lookup("HOST.defect." + next(t for t, e in sorted(self.built.host["statement_defects"].items()) if e.get("provable_through_defect")))["text"])
        self.assertTrue(defect["provable_through_defect"])
        self.assertIsNone(self.built.lookup("A09.t0001.r"))

    def test_inspect_coordinates_match_timeline_references(self):
        # Frozen-label evidence (labels.py) reuses timeline.py's Inspect reference format.
        sys.path.insert(0, str(REPO / "research" / "analysis"))
        import timeline
        streams, _ = timeline.inspect_streams(self.run_dir, [])
        entries = [e for stream in streams for e in stream if e["agent_id"] == "agent-001"]
        results = {e["tool_call_id"]: e["ref"] for e in entries if e["kind"] == "tool_result"}
        outputs = {e["ref"]["message_id"]: e["ref"] for e in entries if e["kind"] == "model_output"}
        for item in self.a1.items:
            if item["kind"] == "tool_result":
                self.assertEqual(item["meta"]["inspect_result"], results[item["meta"]["call_id"]], item["id"])
            elif item["kind"] in ("reasoning", "text", "summary", "tool_call"):
                self.assertEqual(item["meta"]["inspect"], outputs[item["meta"]["message_id"]], item["id"])
        self.assertEqual(self.a1.get("A01.t0000.c2")["meta"]["conference_id"], "post-000002")
        self.assertEqual(self.a1.get("A01.t0003.c3")["meta"]["conference_id"], "sub-000002")
        self.assertNotIn("conference_id", self.a1.get("A01.t0001.c1")["meta"])   # a read creates nothing

    def test_agent_subset_and_codes(self):
        subset = history.build_run(self.run_dir, agents=["agent-002"], include_files=False)
        self.assertEqual(list(subset.agents), ["agent-002"])
        self.assertEqual(history.agent_code("agent-007"), "A07")
        self.assertEqual(history.agent_code("agent-123"), "A123")
        with self.assertRaises(ValueError):
            history.agent_code("peer-1")
        with self.assertRaises(ValueError):
            history.build_run(self.run_dir, agents=["agent-009"])


class UnitTests(unittest.TestCase):
    def test_estimate_tokens_rounds_up(self):
        self.assertEqual([history.estimate_tokens(s) for s in ("", "a", "abcd", "abcde")], [0, 1, 1, 2])

    def test_readonly_connection_leaves_a_saved_database_untouched_and_reads_a_live_wal(self):
        import os
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = sqlite3.connect(path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE t(x)")
            db.execute("INSERT INTO t VALUES (1)")
            db.commit()
            db.close()
            wal, shm = Path(f"{path}-wal"), Path(f"{path}-shm")
            wal.write_bytes(b"")                  # a saved run: empty write-ahead log, stale -shm
            shm.write_bytes(b"\0" * 32768)
            os.utime(shm, (1, 1))
            before = (path.read_bytes(), shm.read_bytes())
            conn = history._connect_readonly(path)
            self.assertEqual(conn.execute("SELECT x FROM t").fetchall()[0][0], 1)
            conn.close()
            self.assertEqual((path.read_bytes(), shm.read_bytes(), shm.stat().st_mtime), before + (1,))
            wal.unlink()
            shm.unlink()
            writer = sqlite3.connect(path)       # a live database: committed rows only in the WAL
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO t VALUES (2)")
            writer.commit()
            self.assertGreater(wal.stat().st_size, 0)
            conn = history._connect_readonly(path)
            self.assertEqual(sorted(r[0] for r in conn.execute("SELECT x FROM t")), [1, 2])
            conn.close()
            writer.close()

    def test_conflicting_tool_result_copies_are_recorded(self):
        def msg(**kw):
            return SimpleNamespace(**kw)
        call = msg(id="c1", function="Bash", arguments={"command": "ls"}, parse_error=None)
        out = msg(role="assistant", id="o1", content="go", tool_calls=[call])
        done = msg(role="assistant", id="o2", content="done", tool_calls=[])
        copy1 = msg(role="tool", id="t1", tool_call_id="c1", function="Bash", content="one", error=None)
        copy2 = msg(role="tool", id="t2", tool_call_id="c1", function="Bash", content="two", error=None)
        e1 = msg(event="model", uuid="e1", timestamp=T0, completed=T0 + 1, input=[], error=None, pending=None,
                 output=msg(choices=[msg(message=out)], stop_reason="tool_calls"))
        e2 = msg(event="model", uuid="e2", timestamp=T0 + 2, completed=T0 + 3, input=[out, copy1], error=None,
                 pending=None, output=msg(choices=[msg(message=done)], stop_reason="stop"))
        e3 = msg(event="model", uuid="e3", timestamp=T0 + 4, completed=T0 + 5, input=[out, copy2], error=None,
                 pending=None, output=msg(choices=[msg(message=done)], stop_reason="stop"))
        rebuilt = history.reconstruct_sample(msg(id="agent-003", events=[e1, e2, e3], messages=[], error=None))
        items = [i for b in rebuilt["blocks"] for i in b[3]]
        result = next(i for i in items if i["id"] == "A03.t0000.o1")
        self.assertEqual(result["text"], "one")            # first observation, never the last copy
        self.assertEqual(result["meta"]["conflicting_copies"], 1)
        self.assertEqual(rebuilt["stats"]["conflicting_tool_results"], ["A03.t0000.c1"])

    def test_join_ambiguity_is_explicit(self):
        events = [{"seq": 5, "timestamp": T0 + 1, "kind": "status_viewed", "tool": "get_conference_status",
                   "call_id": "x", "payload": {}},
                  {"seq": 6, "timestamp": T0 + 1.5, "kind": "status_viewed", "tool": "get_conference_status",
                   "call_id": "y", "payload": {}},
                  {"seq": 9, "timestamp": T0 + 30, "kind": "status_viewed", "tool": "get_conference_status",
                   "call_id": "z", "payload": {}}]
        result = {"ok": True, "problems": []}
        calls = [{"tool": "get_conference_status", "args": {}, "result": result, "lo": T0, "hi": T0 + 2},
                 {"tool": "get_conference_status", "args": {}, "result": result, "lo": T0, "hi": T0 + 2},
                 {"tool": "get_conference_status", "args": {}, "result": result, "lo": T0 + 29, "hi": T0 + 31},
                 {"tool": "get_conference_status", "args": {}, "result": None, "result_text": "Error: bad",
                  "lo": T0, "hi": T0 + 2},
                 {"tool": "submit_solution", "args": {}, "result": {"ok": False, "error": "conference_closed"},
                  "lo": T0, "hi": T0 + 2}]
        joins = history.join_conference_calls(calls, events, slack=0.0)
        self.assertEqual([j["status"] for j in joins],
                         ["ambiguous", "ambiguous", "joined", "unparsed", "no_event_expected"])
        self.assertEqual(joins[0]["candidates"], [5, 6])
        self.assertEqual(joins[2]["seq"], 9)

    def test_exact_window_wins_over_slack(self):
        # Two identical reads seconds apart (agent-017, cache100): the slack overlaps both
        # windows, but each event lies strictly inside exactly one of them.
        events = [{"seq": 563, "timestamp": T0 + 2, "kind": "status_viewed", "tool": "get_conference_status",
                   "call_id": "x", "payload": {}},
                  {"seq": 565, "timestamp": T0 + 6, "kind": "status_viewed", "tool": "get_conference_status",
                   "call_id": "y", "payload": {}}]
        calls = [{"tool": "get_conference_status", "args": {}, "result": {"ok": True}, "lo": T0 + 1, "hi": T0 + 4},
                 {"tool": "get_conference_status", "args": {}, "result": {"ok": True}, "lo": T0 + 5, "hi": T0 + 8}]
        joins = history.join_conference_calls(calls, events, slack=5.0)
        self.assertEqual([(j["status"], j["seq"]) for j in joins], [("joined", 563), ("joined", 565)])

    def test_request_arguments_disambiguate(self):
        payload = {"since_seq": 38, "limit": 20, "post_ids": ["post-000039"], "filters": {},
                   "paging": {"total": 39, "returned": 1, "truncated": True}}
        event = {"seq": 1, "timestamp": T0, "kind": "board_read", "tool": "get_bulletin_board", "call_id": "c",
                 "payload": payload}
        result = {"ok": True, "total": 39, "returned": 1, "truncated": True, "posts": [{"post_id": "post-000039"}]}
        self.assertTrue(history._event_matches("get_bulletin_board", result, {"since_seq": 38, "limit": 20}, event))
        self.assertFalse(history._event_matches("get_bulletin_board", result, {"since_seq": 38, "limit": 1}, event))
        self.assertFalse(history._event_matches("get_bulletin_board", dict(result, truncated=False), {}, event))

    def test_discarded_output_is_marked_unadopted(self):
        def msg(**kw):
            return SimpleNamespace(**kw)
        kept = msg(id="c-kept", function="Bash", arguments={"command": "launch"}, parse_error=None)
        dropped = msg(id="c-dropped", function="Bash", arguments={"command": "other"}, parse_error=None)
        out_kept = msg(role="assistant", id="o1", content="first", tool_calls=[kept])
        out_dropped = msg(role="assistant", id="o2", content="resent", tool_calls=[dropped])
        replay = msg(role="assistant", id="r1", content="first", tool_calls=[kept])
        result = msg(role="tool", id="t1", tool_call_id="c-kept", function="Bash", content="launched", error=None)
        done = msg(role="assistant", id="o3", content="done", tool_calls=[])

        def event(n, inputs, out):
            return msg(event="model", uuid=f"e{n}", timestamp=T0 + 2 * n, completed=T0 + 2 * n + 1, input=inputs,
                       error=None, pending=None, output=msg(choices=[msg(message=out)], stop_reason="tool_calls"))
        sample = msg(id="agent-017", error=None, messages=[replay, result, done],
                     events=[event(0, [], out_kept), event(1, [], out_dropped), event(2, [replay, result], done)])
        rebuilt = history.reconstruct_sample(sample)
        items = {i["id"]: i for b in rebuilt["blocks"] for i in b[3]}
        self.assertEqual(rebuilt["stats"]["unadopted_turns"], [1])
        self.assertTrue(items["A17.t0000.c1"]["meta"]["adopted"])
        self.assertFalse(items["A17.t0001.c1"]["meta"]["adopted"])
        self.assertFalse(items["A17.t0001.c1"]["meta"]["has_result"])
        self.assertEqual(rebuilt["stats"]["missing_tool_results"], ["A17.t0001.c1"])
        self.assertTrue(items["A17.t0002.x"]["meta"]["adopted"])   # final message carried by sample.messages

    def test_summary_turn_found_when_compaction_event_is_logged_late(self):
        # cache100 agent-003: the CompactionEvent follows the first turn of the new segment.
        def msg(**kw):
            return SimpleNamespace(**kw)
        call = msg(id="c1", function="Bash", arguments={"command": "ls"}, parse_error=None)
        work = msg(role="assistant", id="o1", content="working", tool_calls=[call])
        result = msg(role="tool", id="t1", tool_call_id="c1", function="Bash", content="ok", error=None)
        request = msg(role="user", id="u1", content=history.COMPACTION_REQUEST + ". Summarize.")
        summary = msg(role="assistant", id="o2", content="<summary>so far</summary>", tool_calls=[])
        cont = msg(role="user", id="u2", content=history.CONTINUATION_MARK + " Summary: so far")
        after = msg(role="assistant", id="o3", content="resumed", tool_calls=[])
        system = msg(role="system", id="s", content="system prompt")

        def event(n, inputs, out, stop):
            return msg(event="model", uuid=f"e{n}", timestamp=T0 + 2 * n, completed=T0 + 2 * n + 1, input=inputs,
                       error=None, pending=None, output=msg(choices=[msg(message=out)], stop_reason=stop))
        events = [event(0, [system], work, "tool_calls"),
                  event(1, [system, work, result, request], summary, "stop"),
                  event(2, [system, cont], after, "stop"),
                  msg(event="compaction", uuid="k", timestamp=T0 + 6)]
        rebuilt = history.reconstruct_sample(msg(id="agent-003", events=events, messages=[], error=None))
        self.assertEqual(rebuilt["segments"], [{"segment": 0, "first_turn": 0, "last_turn": 1, "summary_turn": 1},
                                               {"segment": 1, "first_turn": 2, "last_turn": 2, "summary_turn": None}])
        items = {i["id"]: i for b in rebuilt["blocks"] for i in b[3]}
        self.assertEqual(items["A03.t0001.s"]["text"], "<summary>so far</summary>")
        self.assertEqual(items["A03.t0002.x"]["meta"]["segment"], 1)
        self.assertFalse(items["A03.t0002.x"]["meta"]["derived"])
        self.assertEqual(rebuilt["stats"]["boundaries_without_summary"], [])

    def test_render_call_keeps_strings_raw(self):
        text = history.render_call("Write", {"file_path": "/w/a.lean", "content": "a\n  b", "n": 2})
        self.assertEqual(text, 'Write\nfile_path: /w/a.lean\ncontent:\na\n  b\nn: 2')


@unittest.skipUnless(HAVE_INSPECT and (CACHE100 / "inspect").is_dir(), "cache100 run not present")
class RealCache100SmokeTests(unittest.TestCase):
    """Survey (scratchpad survey-judge-gdm-rundata.md): agent-001 482 turns, 13 compactions,
    ~1,706K total and ~1,260K reasoning tokens (chars/4); agent-006 45 turns, 72K and 45K."""
    SURVEY = {"agent-001": (482, 13, 1706, 1260), "agent-006": (45, 0, 72, 45)}

    @classmethod
    def setUpClass(cls):
        cls.built = history.build_run(CACHE100, agents=sorted(cls.SURVEY))

    def test_turns_summaries_and_volume_match_survey(self):
        for agent, (turns, compactions, total_k, reasoning_k) in self.SURVEY.items():
            stats = self.built.agents[agent].stats
            self.assertEqual(stats["turns"], turns, agent)
            self.assertEqual(stats["summary_turns"], compactions, agent)
            self.assertEqual(stats["segments"], compactions + 1, agent)
            tokens = stats["tokens_by_kind"]
            self.assertAlmostEqual(tokens.get("reasoning", 0) / 1000, reasoning_k, delta=0.02 * reasoning_k + 1)
            # Turn-derived kinds only (files and conference records were not in the survey).
            turn_k = sum(tokens.get(k, 0) for k in ("reasoning", "text", "tool_call", "tool_result", "summary")) / 1000
            self.assertAlmostEqual(turn_k, total_k, delta=0.25 * total_k, msg=agent)
            # Only the last turn (stopped or killed before its call returned) or a discarded,
            # re-sent output may lack a tool result.
            for call in stats["missing_tool_results"]:
                turn = history.run_item_turn(call)
                self.assertTrue(turn == stats["turns"] - 1 or turn in stats["unadopted_turns"], call)

    def test_real_items_and_ledger_are_well_formed(self):
        history_ = self.built.agents["agent-001"]
        ids = [i["id"] for i in history_.items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i["agent_id"] == "agent-001" for i in history_.items))
        self.assertEqual(sum(1 for i in history_.items if i["kind"] == "summary"), 13)
        self.assertFalse(any(".claude/projects" in i for i in ids))   # session JSONL excluded
        joins = history_.stats["conference_joins"]
        self.assertGreater(joins.get("joined", 0), 0.9 * sum(joins.values()))
        for entry in history_.exposure_ledger:
            self.assertNotEqual(entry["author"], "agent-001")
            self.assertTrue(entry["item_id"].startswith("A01.t"))


if __name__ == "__main__":
    unittest.main()
