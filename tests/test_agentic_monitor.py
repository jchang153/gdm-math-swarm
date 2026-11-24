"""Agentic monitor arm AG (docs/plan.md, "Agentic monitor arm AG (owner decisions, November 23, 2026)"):
identifiers, the ``agentic_monitor`` config block, wait clamping, the feed (rendering identical to the reviewer's
window, result updates, the delivery cap), the five tools against a real ConferenceService (eligibility: only
the monitor's own solver's accepted submissions; refusals; DMs from the reviewer's sender), event payloads, the
bridge filter wrapper (compaction, tool list, ledger label) and the monitor's sample loop with a fake harness.
No network, no paid calls."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from research_swarm import agentic_monitor as ag
from research_swarm import async_review, runtime
from research_swarm.conference import ConferenceService
from tests.test_conference import GOOD, FakeJudge, agents, make_tasks
from tests.test_inspect_adapter import HAVE_INSPECT, run_ledger

BLOCK = {"enabled": True, "max_output_chars": 2000, "min_wait_seconds": 10, "max_wait_seconds": 1800,
         "default_wait_seconds": 60, "disclosed": False, "prompt_version": "agentic-review-v1", "mem_limit": "2g"}


def config(**extra):
    return {"agent_count": 3, "max_connections": 6, "agentic_monitor": dict(BLOCK), **extra}


def assistant(calls):
    return {"role": "assistant", "content": "x",
            "tool_calls": [{"id": cid, "function": name, "arguments": args} for cid, name, args in calls]}


def tool(cid, text, error=None):
    return {"role": "tool", "tool_call_id": cid, "content": text, **({"error": error} if error else {})}


class FakeHistory:
    """ReviewHistory.snapshot stand-in over JSON-dump messages."""

    def __init__(self):
        self.messages, self.times = {}, {}

    def set(self, agent, messages, times):
        self.messages[agent], self.times[agent] = list(messages), dict(times)

    def snapshot(self, agent):
        return list(self.messages.get(agent, [])), dict(self.times.get(agent, {})), None


class IdentifierAndConfigTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(ag.monitor_id("agent-007"), "agmon-007")
        self.assertEqual(ag.solver_of("agmon-101"), "agent-101")
        self.assertIsNone(ag.solver_of("agent-001"))
        self.assertTrue(ag.is_monitor_id("agmon-001"))
        with self.assertRaises(ValueError):
            ag.monitor_id("reviewer")

    def test_valid_block_and_defaults(self):
        self.assertEqual(ag.agentic_problems(config()), [])
        self.assertEqual(ag.agentic_settings(config()), BLOCK)
        minimal = {"agent_count": 3, "agentic_monitor": {"enabled": True}}
        self.assertEqual(ag.agentic_settings(minimal), BLOCK)
        self.assertIsNone(ag.agentic_settings({"agentic_monitor": {"enabled": False}}))
        self.assertIsNone(ag.agentic_settings({}))
        self.assertEqual(ag.agentic_problems({"agentic_monitor": {"enabled": False, "bogus": 1}}),
                         ["agentic_monitor has unknown keys ['bogus']"])

    def test_invalid_blocks(self):
        def problems(**changes):
            return ag.agentic_problems({"agent_count": 3, "agentic_monitor": {**BLOCK, **changes}})
        self.assertTrue(problems(max_output_chars=0))
        self.assertTrue(problems(min_wait_seconds=0))
        self.assertTrue(problems(min_wait_seconds=True))
        self.assertTrue(problems(default_wait_seconds=5))           # below the minimum
        self.assertTrue(problems(max_wait_seconds=30))              # below the default
        self.assertTrue(problems(disclosed=True))                   # not implemented
        self.assertTrue(problems(prompt_version="review-v1"))
        self.assertTrue(problems(enabled="yes"))
        with self.assertRaises(ValueError):
            ag.agentic_settings({"agentic_monitor": {**BLOCK, "max_wait_seconds": 1}})
        self.assertEqual(ag.agentic_problems({"agentic_monitor": []}), ["agentic_monitor must be an object"])

    def test_mutually_exclusive_with_other_oversight(self):
        self.assertTrue(ag.agentic_problems(config(monitor={"enabled": True})))
        self.assertTrue(ag.agentic_problems(config(reviewer={"enabled": True})))
        self.assertTrue(ag.agentic_problems(config(overseer={"agent_id": "agent-004", "powers": True})))
        self.assertEqual(ag.agentic_problems(config(monitor={"enabled": False}, reviewer=None)), [])
        self.assertIn("agentic_monitor and reviewer are mutually exclusive (disable one)",
                      runtime.agentic_launch_problems(config(reviewer={"enabled": True})))

    def test_participants_and_connections(self):
        from research_swarm import launch_gates, oversight
        self.assertEqual(ag.monitor_count(config()), 3)
        self.assertEqual(ag.monitor_count({"agent_count": 3}), 0)
        self.assertEqual(oversight.participant_count(config()), 6)
        self.assertEqual(launch_gates.max_connections_problems(config()), [])
        self.assertTrue(launch_gates.max_connections_problems(config(max_connections=3)))

    def test_clamp_wait(self):
        settings = ag.agentic_settings(config())
        self.assertEqual(ag.clamp_wait(1, settings), (1.0, 10.0))
        self.assertEqual(ag.clamp_wait(99999, settings), (99999.0, 1800.0))
        self.assertEqual(ag.clamp_wait(120, settings), (120.0, 120.0))
        self.assertEqual(ag.clamp_wait("45", settings), (45.0, 45.0))
        self.assertEqual(ag.clamp_wait(None, settings), (None, 60.0))
        self.assertEqual(ag.clamp_wait("soon", settings), (None, 60.0))
        self.assertEqual(ag.clamp_wait(True, settings), (None, 60.0))
        self.assertEqual(ag.clamp_wait(float("nan"), settings), (None, 60.0))

    def test_prompt_tools_and_env(self):
        from research_swarm import agentic_prompt
        settings = ag.agentic_settings(config())
        self.assertEqual(ag.system_prompt(settings), agentic_prompt.AGENTIC_REVIEW_PROMPT)
        specs = ag.tool_specs()
        self.assertEqual([s[0] for s in specs], list(ag.TOOL_NAMES))
        self.assertEqual(specs[0][1], agentic_prompt.TOOL_DESCRIPTIONS["wait"])
        env = ag.claude_code_env_extra(settings)
        self.assertEqual(env["MCP_TOOL_TIMEOUT"], str((1800 + 300) * 1000))
        # probe 20261124T085609Z: Claude Code 2.1.296 aborts a tool silent for 300 s unless this is raised
        self.assertEqual(env["CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT"], env["MCP_TOOL_TIMEOUT"])
        self.assertGreaterEqual(int(env["CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT"]), (1800 + 300) * 1000)
        self.assertEqual(env["CLAUDE_CODE_ATTRIBUTION_HEADER"], "0")
        self.assertFalse([k for k in env if "KEY" in k or "TOKEN" in k.replace("MAX_MCP_OUTPUT_TOKENS", "")])
        disallowed = ag._disallowed_tools()
        for name in ("Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch", "Agent", "Task", "TodoWrite",
                     "TaskStop", "Glob", "Grep", "NotebookEdit"):
            self.assertIn(name, disallowed)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.history = FakeHistory()
        self.feed = ag.Feed(self.history, "agent-001", 2000)

    def test_new_calls_render_like_the_reviewer_window_then_updates(self):
        first = assistant([("c1", "Bash", {"command": "ls"}), ("c2", "mcp__collab__get_status", {})])
        self.history.set("agent-001", [{"role": "user", "content": "go"}, first, tool("c1", "Y" * 2500)],
                         {"c1": 10.0, "c2": 10.0})
        text, new, updates = self.feed.deliver()
        calls = async_review.window_calls([first, tool("c1", "Y" * 2500)], {"c1": 10.0, "c2": 10.0}, 0, 100)
        self.assertEqual(text, async_review.build_window(calls, 2000))
        self.assertIn("[truncated]", text)
        self.assertIn("(no result yet)", text)
        self.assertEqual(([c["id"] for c in new], updates), (["c1", "c2"], []))
        self.assertEqual(self.feed.output("c1"), "Y" * 2500)  # full, uncut
        self.assertIsNone(self.feed.output("c2"))              # result not delivered yet
        # Nothing new.
        self.assertEqual(self.feed.deliver()[0], ag.NO_ACTIVITY_TEXT)
        # c2's result arrives, plus a new call c3: the update first, then the new call; c1 never again.
        second = assistant([("c3", "Read", {"file_path": "a"})])
        self.history.set("agent-001", [first, tool("c1", "Y" * 2500), tool("c2", "status", {"message": "boom"}),
                                       second], {"c1": 10.0, "c2": 10.0, "c3": 20.0})
        text, new, updates = self.feed.deliver()
        self.assertEqual(([c["id"] for c in updates], [c["id"] for c in new]), (["c2"], ["c3"]))
        self.assertTrue(text.startswith("[tool result id: c2, error, update]\nstatus\n\n[tool call id: c3] Read"))
        self.assertNotIn("c1", text)
        self.assertEqual(self.feed.output("c2"), "status")
        self.assertFalse(self.feed.peek())  # c3 has no result yet: nothing to show until it arrives

    def test_delivery_cap_keeps_the_rest_queued(self):
        feed = ag.Feed(self.history, "agent-001", 2000, max_chars=5000)
        calls = [(f"c{i}", "Bash", {"command": "x"}) for i in range(6)]
        message = assistant(calls)
        self.history.set("agent-001", [message] + [tool(c, "Z" * 1500) for c, _, _ in calls],
                         {c: 1.0 for c, _, _ in calls})
        text, new, _ = feed.deliver()
        self.assertTrue(feed.backlog)
        self.assertTrue(text.endswith(ag.MORE_TEXT))
        self.assertEqual([c["id"] for c in new], ["c0", "c1", "c2"])
        text, new, _ = feed.deliver(solver_stopped=True)
        self.assertEqual([c["id"] for c in new], ["c3", "c4", "c5"])
        self.assertFalse(feed.backlog)
        self.assertTrue(text.endswith(ag.STOPPED_TEXT))
        self.assertTrue(feed.stop_noted)

    def test_untimed_calls_are_not_delivered(self):
        self.history.set("agent-001", [assistant([("c1", "Bash", {})])], {})
        self.assertEqual(self.feed.deliver()[1], [])


class ToolTests(unittest.TestCase):
    """The five tools against a real ConferenceService. agmon-002 watches agent-002."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run_dir = Path(folder.name)
        self.conference = ConferenceService(run_dir=self.run_dir, tasks=make_tasks(3), judge=FakeJudge(),
                                            agent_ids=agents(3))
        self.addCleanup(self.conference.close)
        self.events, self.slept = [], []
        self.history = FakeHistory()

        async def sleeper(solver, seconds):
            self.slept.append((solver, seconds))

        self.monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=self.conference,
                                           record=lambda *e: self.events.append(e), solver_ids=agents(3),
                                           history=self.history, sleeper=sleeper, now=lambda: 17.5)
        for agent, task in (("agent-001", "task_0"), ("agent-002", "task_1")):
            self.assertTrue(self.conference.action(agent, "submit_solution",
                                                   {"problem_id": task, "source": GOOD})["accepted"])

    def kinds(self, kind):
        return [(e[1], e[2]) for e in self.events if e[0] == kind]

    def call(self, name, **args):
        return asyncio.run(self.monitors.call("agmon-002", name, args))

    def inbox(self, agent):
        return [(m["from_agent_id"], m["message"])
                for m in self.conference.action(agent, "get_messages", {})["messages"]]

    def test_identity(self):
        self.assertEqual(self.monitors.monitor_ids, ["agmon-001", "agmon-002", "agmon-003"])
        self.assertTrue(self.monitors.is_monitor("agmon-003"))
        self.assertFalse(self.monitors.is_monitor("agmon-004"))
        self.assertFalse(self.monitors.is_monitor("agent-001"))

    def test_list_revertible_only_own(self):
        text = self.call("list_revertible")
        self.assertIn("problem_id: task_1", text)
        self.assertIn("source:\n" + GOOD, text)
        self.assertNotIn("task_0", text)
        self.assertEqual(self.kinds("agentic_list_revertible"),
                         [("agmon-002", {"monitor": "agmon-002", "solver": "agent-002", "count": 1,
                                         "problem_ids": ["task_1"]})])
        entry = self.conference.accepted_submissions_of("agent-002")[0]
        self.assertEqual(set(entry), {"problem_id", "submission_id", "public_submission_id", "accepted_at", "source"})
        self.assertEqual(asyncio.run(self.monitors.call("agmon-003", "list_revertible", {})),
                         "(no accepted submissions by this agent)")

    def test_revert_own_submission_and_dm(self):
        submission = self.conference.accepted_submissions()["task_1"]["submission_id"]
        reply = self.call("revert_submission", problem_id="task_1", reason="It restates the goal.")
        self.assertIn("Reverted the accepted submission for task_1", reply)
        self.assertNotIn("task_1", self.conference.accepted_submissions())
        self.assertEqual(self.inbox("agent-002"), [("reviewer", async_review.revert_dm("task_1", "It restates the goal."))])
        (agent, payload), = self.kinds("agentic_revert")
        self.assertEqual(agent, "agmon-002")
        self.assertEqual({k: payload[k] for k in ("monitor", "solver", "problem_id", "submission_id", "reason",
                                                  "dm_sent")},
                         {"monitor": "agmon-002", "solver": "agent-002", "problem_id": "task_1",
                          "submission_id": submission, "reason": "It restates the goal.", "dm_sent": True})
        self.assertRegex(payload["message_id"], r"^dm-[0-9a-f]{16}$")  # the public id (reviewer_message)
        # The conference records the reviewer's revert, with the reviewed agent.
        lines = (self.run_dir / "conference" / "events.jsonl").read_text().splitlines()
        reverted = [e for e in map(json.loads, lines) if e["kind"] == "reviewer_revert"]
        self.assertEqual((reverted[0]["agent_id"], reverted[0]["payload"]["reviewed_agent"]), ("reviewer", "agent-002"))

    def test_refusals(self):
        # Another agent's accepted submission (e.g. one the solver read, or the seed's): never.
        self.assertEqual(self.call("revert_submission", problem_id="task_0", reason="copied"),
                         "Error: task_0 has no accepted submission by this agent.")
        self.assertIn("task_0", self.conference.accepted_submissions())
        self.call("revert_submission", problem_id="task_2", reason="x")      # open problem
        self.call("revert_submission", problem_id="nope", reason="x")        # unknown problem
        self.call("revert_submission", problem_id="task_1", reason="  ")     # no reason
        self.call("revert_submission", problem_id="", reason="x")
        whys = [p["why"] for _, p in self.kinds("agentic_revert_refused")]
        self.assertEqual(whys, ["not_own_submission", "no_accepted_submission", "no_accepted_submission",
                                "missing_reason", "missing_problem_id"])
        self.assertEqual(self.kinds("agentic_revert"), [])
        self.assertEqual(self.inbox("agent-001"), [])
        self.assertEqual(self.kinds("agentic_revert_refused")[0][1],
                         {"monitor": "agmon-002", "solver": "agent-002", "problem_id": "task_0",
                          "why": "not_own_submission"})

    def test_revert_checks_credit_atomically(self):
        self.assertIsNone(self.conference.reviewer_revert("task_0", "x", credited_agent="agent-002"))
        self.assertIn("task_0", self.conference.accepted_submissions())

    def test_message_solver(self):
        self.assertEqual(self.call("message_solver", text="Please explain the last submission."), "Message sent.")
        self.assertEqual(self.inbox("agent-002"), [("reviewer", "Please explain the last submission.")])
        (agent, payload), = self.kinds("agentic_message")
        self.assertEqual({k: payload[k] for k in ("monitor", "solver", "text")},
                         {"monitor": "agmon-002", "solver": "agent-002", "text": "Please explain the last submission."})
        self.assertRegex(payload["message_id"], r"^dm-[0-9a-f]{16}$")  # the public id (reviewer_message)
        self.assertEqual(self.call("message_solver", text=" "), "Error: the message is empty.")
        # Not a participant.
        status = self.conference.action("agent-001", "get_conference_status", {})
        self.assertFalse([p for p in status["participants"] if p["agent_id"].startswith("agmon")])

    def test_neutral_dm_text(self):
        self.monitors.neutral = True
        self.call("message_solver", text="Claude, stop.")
        self.assertNotIn("Claude", self.inbox("agent-002")[0][1])

    def test_wait_get_output_and_events(self):
        message = assistant([("c1", "Bash", {"command": "cat a"})])
        self.history.set("agent-002", [message, tool("c1", "W" * 3000)], {"c1": 5.0})
        text = self.call("wait", seconds=5)
        self.assertEqual(self.slept, [("agent-002", 10.0)])  # clamped to the minimum
        self.assertIn("[tool call id: c1] Bash", text)
        (agent, payload), = self.kinds("agentic_wait")
        self.assertEqual({k: payload[k] for k in ("monitor", "solver", "requested_s", "clamped_s", "delivered_calls",
                                                  "implicit")},
                         {"monitor": "agmon-002", "solver": "agent-002", "requested_s": 5.0, "clamped_s": 10.0,
                          "delivered_calls": 1, "implicit": False})
        self.assertEqual(payload["call_lags_s"], [12.5])  # first seen at 5.0, delivered at 17.5
        self.assertEqual(self.call("wait", seconds=5), ag.NO_ACTIVITY_TEXT)
        self.assertEqual(self.kinds("agentic_wait")[1][1]["call_lags_s"], [])
        self.assertEqual(self.call("get_output", call_id="c1"), "W" * 3000)
        self.assertTrue(self.call("get_output", call_id="zz").startswith("Error:"))
        self.assertEqual([p["found"] for _, p in self.kinds("agentic_get_output")], [True, False])
        # Another monitor never sees agent-002's calls.
        self.assertTrue(asyncio.run(self.monitors.call("agmon-001", "get_output", {"call_id": "c1"})).startswith("Error"))

    def test_wait_after_solver_ended_returns_with_note(self):
        self.monitors.solver_finished("agent-002")
        text = self.call("wait", seconds=600)
        self.assertTrue(text.endswith(ag.STOPPED_TEXT))
        self.assertTrue(self.kinds("agentic_wait")[0][1]["solver_stopped"])

    def test_real_sleep_returns_early_when_solver_ends(self):
        monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=None, record=lambda *e: None,
                                      solver_ids=agents(1), history=FakeHistory())

        async def scenario():
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, monitors.solver_finished, "agent-001")
            began = loop.time()
            await monitors._sleep("agent-001", 30)
            return loop.time() - began
        self.assertLess(asyncio.run(scenario()), 5)

    def test_tool_failure_is_neutral_and_recorded(self):
        def broken(*args, **kwargs):
            raise RuntimeError("db gone")
        self.conference.accepted_submissions_of = broken
        self.assertEqual(self.call("list_revertible"), "Error: the tool failed.")
        self.assertEqual(self.kinds("agentic_error")[0][1]["stage"], "list_revertible")


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class BridgeTests(unittest.TestCase):
    def test_inspect_tools(self):
        from inspect_ai.tool import ToolDef
        monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=None, record=lambda *e: None,
                                      solver_ids=agents(1), history=FakeHistory())
        tools = monitors.tools("agmon-001")
        defs = [ToolDef(t) for t in tools]
        self.assertEqual([d.name for d in defs], list(ag.TOOL_NAMES))
        self.assertEqual(defs[0].parameters.required, ["seconds"])
        self.assertEqual(asyncio.run(tools[3](call_id="x"))[:6], "Error:")

    def test_filter_wrapper_records_compaction_filters_tools_and_labels_ledger(self):
        from inspect_ai.model import ChatMessageUser, GenerateConfig
        from inspect_ai.tool import ToolInfo, ToolParams
        from research_swarm.inspect_adapter import budget_filter
        from research_swarm.provider import ProviderSettings
        from research_swarm.rollback import COMPACTION_MARKER
        from tests.test_monitor_live import FakeModel, agent_response, ledger_rows
        events = []
        monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=None,
                                      record=lambda *e: events.append(e), solver_ids=agents(1), history=FakeHistory())
        ledger_path = Path(tempfile.mkdtemp()) / "b.sqlite"
        model = FakeModel([agent_response([("w1", "mcp__review__wait", {"seconds": 60})])] * 3)
        inner = budget_filter("agmon-001", run_ledger(ledger_path, 100), ProviderSettings(pricing_verified=True),
                              runtime.StartBarrier(1), lambda *e: events.append(e), scope="validation")
        intercept = monitors.wrap_filter("agmon-001", inner)
        self.assertIs(intercept.state, inner.state)
        tools = [ToolInfo(name="mcp__review__wait", description="w", parameters=ToolParams()),
                 ToolInfo(name="Bash", description="b", parameters=ToolParams())]
        compaction = [ChatMessageUser(content="go"), ChatMessageUser(content=COMPACTION_MARKER + " please")]
        for messages in ([ChatMessageUser(content="go")], compaction, compaction):
            asyncio.run(intercept(model, messages, tools, None, GenerateConfig()))
        compactions = [e for e in events if e[0] == "agentic_compaction"]
        self.assertEqual(compactions, [("agentic_compaction", "agmon-001", {"monitor": "agmon-001", "messages": 2})])
        self.assertEqual({row[0] for row in ledger_rows(ledger_path)}, {"agmon-001"})
        admitted = [e for e in events if e[0] == "inference_admitted"]
        self.assertEqual(len(admitted), 3)
        self.assertEqual(inner.state["requests"], 3)

    def _claude_code_request(self, prompt):
        """Claude Code 2.1.296's replace-mode request as the bridge passes it (non-neutral run)."""
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser
        from tests.test_neutral_env import ATTRIBUTION, ENVIRONMENT
        return [ChatMessageSystem(content="x-anthropic-billing-header: cc_version=2.1.296.abc; cc_entrypoint=sdk-cli;"),
                ChatMessageSystem(content="You are a Claude agent, built on Anthropic's Claude Agent SDK."),
                ChatMessageSystem(content=prompt),
                ChatMessageUser(content=ATTRIBUTION + "Begin."),
                ChatMessageSystem(content=ENVIRONMENT)]

    def test_monitor_messages_leave_only_the_prompt(self):
        from inspect_ai.model import ChatMessageSystem
        prompt = ag.system_prompt(ag.agentic_settings(config()))
        messages = self._claude_code_request(prompt)
        out, counts = ag.monitor_messages(messages)
        system = [m.text for m in out if isinstance(m, ChatMessageSystem)]
        self.assertEqual(system, [prompt])
        self.assertEqual([m.text for m in out if m.role == "user"], ["Begin."])
        self.assertEqual(counts["environment_removed"], 1)
        self.assertEqual(len(messages), 5)  # never mutated
        self.assertEqual(ag.monitor_messages(out)[0], out)  # idempotent

    def test_filter_wrapper_removes_claude_code_system_text(self):
        from inspect_ai.model import ChatMessageSystem
        from inspect_ai.tool import ToolInfo, ToolParams
        seen = []

        async def inner(model, messages, tools, tool_choice, config):
            seen.append(messages)
            return "out"
        inner.state = {"requests": 0}
        monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=None, record=lambda *e: None,
                                      solver_ids=agents(1), history=FakeHistory())
        prompt = ag.system_prompt(monitors.settings)
        tools = [ToolInfo(name="mcp__review__wait", description="w", parameters=ToolParams())]
        asyncio.run(monitors.wrap_filter("agmon-001", inner)(None, self._claude_code_request(prompt), tools,
                                                             None, None))
        self.assertEqual([m.text for m in seen[0] if isinstance(m, ChatMessageSystem)], [prompt])

    def test_filter_wrapper_sends_only_monitor_tools(self):
        from inspect_ai.tool import ToolInfo, ToolParams
        seen = []

        async def inner(model, messages, tools, tool_choice, config):
            seen.append([t.name for t in tools])
            return "out"
        inner.state = {"requests": 0}
        events = []
        monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=None,
                                      record=lambda *e: events.append(e), solver_ids=agents(1), history=FakeHistory())
        intercept = monitors.wrap_filter("agmon-001", inner)
        both = [ToolInfo(name="mcp__review__wait", description="w", parameters=ToolParams()),
                ToolInfo(name="Bash", description="b", parameters=ToolParams())]
        only_bash = [both[1]]
        asyncio.run(intercept(None, [], both, None, None))
        asyncio.run(intercept(None, [], only_bash, None, None))
        asyncio.run(intercept(None, [], only_bash, None, None))
        self.assertEqual(seen, [["mcp__review__wait"], ["Bash"], ["Bash"]])
        self.assertEqual([e[2]["stage"] for e in events if e[0] == "agentic_error"], ["tools"])

    def test_solver_filter_feeds_the_monitor(self):
        """The solver's real budget filter (review=history) feeds its monitor's wait."""
        from inspect_ai.model import ChatMessageUser, GenerateConfig
        from research_swarm.inspect_adapter import budget_filter
        from research_swarm.provider import ProviderSettings
        from tests.test_monitor_live import FakeModel, a_tool, agent_response

        async def sleeper(solver, seconds):
            return None
        monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=None, record=lambda *e: None,
                                      solver_ids=agents(1), sleeper=sleeper)
        model = FakeModel([agent_response([("s1", "Bash", {"command": "ls"})])])
        intercept = budget_filter("agent-001", run_ledger(Path(tempfile.mkdtemp()) / "b.sqlite", 100),
                                  ProviderSettings(pricing_verified=True), runtime.StartBarrier(1),
                                  lambda *a: None, scope="validation", review=monitors.history)
        asyncio.run(intercept(model, [ChatMessageUser(content="go")], [a_tool()], None, GenerateConfig()))
        text = asyncio.run(monitors.call("agmon-001", "wait", {"seconds": 10}))
        self.assertEqual(text, '[tool call id: s1] Bash\n{"command": "ls"}\n\n[tool result id: s1]\n(no result yet)')


class FakeClock:
    def __init__(self):
        self.stop = asyncio.Event()
        self.reason = None


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class SampleLoopTests(unittest.TestCase):
    def test_monitor_sample_waits_resumes_implicitly_and_stops(self):
        """A fake harness: session 1 calls wait and ends its turn; the runtime resumes it after the default
        wait with the new activity as the next user message; session 2 ends; the run stops."""
        from inspect_ai.model import ChatMessageAssistant, ChatMessageUser
        events, built, sessions = [], {}, []
        history = FakeHistory()
        run_dir = Path(tempfile.mkdtemp())

        async def scenario():
            clock = FakeClock()

            async def sleeper(solver, seconds):
                sessions.append(("sleep", seconds))
                await asyncio.sleep(0)

            monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=None,
                                          record=lambda *e: events.append(e), solver_ids=agents(1),
                                          history=history, stop=clock.stop, sleeper=sleeper)

            def build_agent(prompt, tools, gen_filter, **kwargs):
                built.update(prompt=prompt, tools=tools, kwargs=kwargs)

                async def agent(state):
                    sessions.append(("session", [m.text for m in state.messages if m.role == "user"]))
                    if len(sessions) == 1:
                        history.set("agent-001", [assistant([("c1", "Bash", {"command": "ls"})])], {"c1": 1.0})
                        await tools[0](seconds=30)
                        history.set("agent-001", [assistant([("c1", "Bash", {"command": "ls"})]), tool("c1", "ok"),
                                                  assistant([("c2", "Bash", {"command": "pwd"})])],
                                    {"c1": 1.0, "c2": 2.0})
                    else:
                        clock.reason = "max_run_seconds"
                        clock.stop.set()
                    state.messages = list(state.messages) + [ChatMessageAssistant(content="done")]
                    return state
                return agent

            class State:
                sample_id = "agmon-001"
                messages = [ChatMessageUser(content=ag.initial_message())]
                output = None

            gen_filter = lambda *a: None  # noqa: E731
            gen_filter.state = {"requests": 2, "in_flight": 0, "last_request_outcome": "finished"}
            return await monitors.run_sample(State(), sbox=None, gen_filter=gen_filter, clock=clock,
                                             record=lambda *e: events.append(e), run_dir=run_dir,
                                             in_flight_grace=0.1, build_agent=build_agent)

        state = asyncio.run(scenario())
        self.assertEqual(built["kwargs"]["mcp_server"], "review")
        self.assertEqual(built["kwargs"]["auto_memory"], False)
        self.assertIn("Bash", built["kwargs"]["disallowed_tools"])
        self.assertEqual(sessions[0], ("session", ["Begin."]))
        self.assertEqual(sessions[1:3], [("sleep", 30.0), ("sleep", 60.0)])  # the wait tool, then the default wait
        self.assertEqual(sessions[3][0], "session")
        resumed = sessions[3][1][-1]
        self.assertEqual(resumed, "[tool result id: c1, update]\nok\n\n[tool call id: c2] Bash\n"
                                  '{"command": "pwd"}\n\n[tool result id: c2]\n(no result yet)')
        waits = [e[2] for e in events if e[0] == "agentic_wait"]
        self.assertEqual([(w["implicit"], w["clamped_s"], w["delivered_calls"]) for w in waits],
                         [(False, 30.0, 1), (True, 60.0, 1)])
        kinds = [e[0] for e in events]
        self.assertEqual(kinds[:2], ["agent_configured", "agentic_monitor_start"])
        self.assertEqual(events[1][2], {"monitor": "agmon-001", "solver": "agent-001"})
        exit_event = [e for e in events if e[0] == "agent_exit"][0]
        self.assertEqual((exit_event[1], exit_event[2]["role"]), ("agmon-001", "agentic_monitor"))
        self.assertTrue((run_dir / "prompts" / "agmon-001.txt").exists())
        self.assertEqual(state.messages[-1].text, "done")

    def test_monitor_ends_after_solver_ended_and_everything_seen(self):
        from inspect_ai.model import ChatMessageUser
        events, sessions = [], []

        async def scenario():
            clock = FakeClock()

            async def sleeper(solver, seconds):
                await asyncio.sleep(0)

            monitors = ag.AgenticMonitors(ag.agentic_settings(config()), conference=None,
                                          record=lambda *e: events.append(e), solver_ids=agents(1),
                                          history=FakeHistory(), stop=clock.stop, sleeper=sleeper)
            monitors.solver_finished("agent-001")

            def build_agent(prompt, tools, gen_filter, **kwargs):
                async def agent(state):
                    sessions.append([m.text for m in state.messages][-1])
                    return state
                return agent

            class State:
                sample_id = "agmon-001"
                messages = [ChatMessageUser(content="Begin.")]
                output = None

            gen_filter = lambda *a: None  # noqa: E731
            gen_filter.state = {}
            await monitors.run_sample(State(), sbox=None, gen_filter=gen_filter, clock=clock,
                                      record=lambda *e: events.append(e), run_dir=Path(tempfile.mkdtemp()),
                                      in_flight_grace=0.1, build_agent=build_agent)
        asyncio.run(scenario())
        self.assertEqual(sessions, ["Begin.", ag.NO_ACTIVITY_TEXT + "\n\n" + ag.STOPPED_TEXT])
        exit_event = [e for e in events if e[0] == "agent_exit"][0][2]
        self.assertEqual(exit_event["harness_status"], "harness_completed")


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class AgenticOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    """run_pilot with an agentic_monitor block, every Docker/Inspect/model boundary faked (tests.test_runtime)."""

    def setUp(self):
        from tests.test_runtime import RunPilotOrchestrationTests
        RunPilotOrchestrationTests.setUp(self)
        self.config.update(agent_count=20, max_connections=40, agentic_monitor={"enabled": True})
        self.config.pop("monitor", None)
        self.config.pop("reviewer", None)

    async def test_monitors_are_extra_samples_with_their_own_filter(self):
        from unittest import mock
        from tests.test_runtime import RunPilotOrchestrationTests
        captured = {}

        def fake_peer_solver(**kwargs):
            captured.update(kwargs)
            return "solver"
        with mock.patch.object(runtime, "peer_solver", fake_peer_solver), \
                mock.patch.object(runtime, "build_task", side_effect=lambda **kw: captured.update(task=kw) or "task"):
            result, order, _ = await RunPilotOrchestrationTests.run_pilot(self)
        self.assertEqual(order[-1], ("eval", "paid-model", 40, True))
        self.assertIn(("model", 40, "100"), order)
        task = captured["task"]
        monitors = [f"agmon-{i:03d}" for i in range(1, 21)]
        self.assertEqual(task["agent_ids"][20:], monitors)
        self.assertEqual({task["initial_messages"][m] for m in monitors}, {"Begin."})
        self.assertEqual(task["sample_files"], {m: {} for m in monitors})
        run_dir = Path(self.config["run_dir"])
        agentic_compose = str((run_dir / runtime.AGENTIC_COMPOSE_NAME).resolve())
        self.assertEqual(task["sample_compose"], {m: agentic_compose for m in monitors})
        self.assertIn("mem_limit: 2g\n", Path(agentic_compose).read_text())
        self.assertNotIn("mem_limit: 2g\n", Path(task["compose"]).read_text())
        agentic = captured["agentic"]
        self.assertEqual(agentic.monitor_ids, monitors)
        self.assertEqual(captured["roles"]["agmon-007"], "agentic_monitor")
        self.assertEqual(captured["roles"]["agent-007"], "solver")
        # The monitor's filter wraps a budget filter under its own id; the solver's feeds the shared history.
        monitor_filter = captured["filter_for"]("agmon-007")
        self.assertIn("wrap_filter", monitor_filter.__qualname__)
        self.assertIsNotNone(monitor_filter.state)
        self.assertNotIn("wrap_filter", captured["filter_for"]("agent-007").__qualname__)
        run_dir = Path(self.config["run_dir"])
        effective = json.loads((run_dir / "settings.json").read_text())["effective"]
        self.assertEqual(effective["participant_count"], 40)
        self.assertEqual(effective["agentic_monitor"]["prompt_version"], "agentic-review-v1")
        self.assertEqual((effective["agentic_monitor"]["mem_limit"], effective["agentic_monitor"]["compose_file"]),
                         ("2g", runtime.AGENTIC_COMPOSE_NAME))
        self.assertEqual(len(effective["agentic_monitor"]["compose_sha256"]), 64)
        self.assertEqual(effective["roles"]["agmon-001"], "agentic_monitor")
        kinds = [json.loads(line)["kind"] for line in (run_dir / "runtime.jsonl").read_text().splitlines()]
        self.assertIn("agentic_monitor_enabled", kinds)


class RuntimeWiringTests(unittest.TestCase):
    def test_build_task_sample_files_and_messages(self):
        if not HAVE_INSPECT:
            self.skipTest("pinned Inspect packages not installed (.venv)")
        from inspect_ai.solver import solver

        @solver
        def noop():
            async def execute(state, generate):
                return state
            return execute
        task = runtime.build_task(agent_ids=["agent-001", "agmon-001"], files={"problems/a.lean": "x"},
                                  solver=noop(), compose="compose.yaml", metadata={}, initial_message="Go.",
                                  initial_messages={"agmon-001": "Begin."}, sample_files={"agmon-001": {}})
        samples = {s.id: s for s in task.dataset}
        self.assertEqual((samples["agent-001"].files, samples["agent-001"].input), ({"problems/a.lean": "x"}, "Go."))
        self.assertEqual((samples["agmon-001"].files or {}, samples["agmon-001"].input), ({}, "Begin."))

    def test_mem_limit_validation(self):
        self.assertEqual(ag.agentic_settings({"agentic_monitor": {"enabled": True, "mem_limit": "1536m"}})["mem_limit"],
                         "1536m")
        for bad in ("2", "2gb", "", 2, None, "2g; rm"):
            problems = ag.agentic_problems({"agentic_monitor": {"enabled": True, "mem_limit": bad}})
            self.assertTrue(any("mem_limit" in p for p in problems), bad)

    def test_agentic_compose_differs_only_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"agent_memory": "7g", "agent_cpus": 2}
            solver = Path(runtime.make_compose(tmp, cfg, "sha256:abc")).read_text()
            monitor_path = runtime.make_agentic_compose(tmp, cfg, "sha256:abc", "2g")
            monitor = Path(monitor_path).read_text()
        self.assertTrue(monitor_path.endswith(runtime.AGENTIC_COMPOSE_NAME))
        self.assertIn("mem_limit: 7g\n    memswap_limit: 7g\n", solver)
        self.assertEqual(monitor, solver.replace("mem_limit: 7g", "mem_limit: 2g").replace("memswap_limit: 7g",
                                                                                            "memswap_limit: 2g"))

    @unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
    def test_build_task_sample_compose(self):
        from inspect_ai.solver import solver

        @solver
        def noop():
            async def execute(state, generate):
                return state
            return execute
        kwargs = dict(agent_ids=["agent-001", "agmon-001"], files={}, solver=noop(), compose="compose.yaml",
                      metadata={}, initial_message="Go.")
        plain = runtime.build_task(**kwargs)
        self.assertEqual([s.sandbox for s in plain.dataset], [None, None])
        task = runtime.build_task(**kwargs, sample_compose={"agmon-001": "compose-agentic-monitor.yaml"})
        samples = {s.id: s for s in task.dataset}
        self.assertIsNone(samples["agent-001"].sandbox)
        self.assertEqual((samples["agmon-001"].sandbox.type, samples["agmon-001"].sandbox.config),
                         ("docker", "compose-agentic-monitor.yaml"))
        self.assertEqual((task.sandbox.type, task.sandbox.config), ("docker", "compose.yaml"))

    def test_effective_settings(self):
        from research_swarm.provider import ProviderSettings
        effective = runtime.agentic_effective(ag.agentic_settings(config()), ProviderSettings(pricing_verified=True))
        self.assertEqual((effective["prompt_version"], effective["mcp_server"], effective["sender_id"],
                          effective["ledger_agent"]), ("agentic-review-v1", "review", "reviewer", "agmon-<NNN>"))
        self.assertEqual(effective["tool_names"], list(ag.TOOL_NAMES))


if __name__ == "__main__":
    unittest.main()
