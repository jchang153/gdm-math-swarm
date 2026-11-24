"""Bridge startup with 200 samples (arm AG): runtime.StartupGate, the agentic block's bridge settings and the
monitors' model-service polling scope (inspect_adapter.model_service_polling)."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research_swarm import agentic_monitor as ag
from research_swarm import runtime
from tests.test_inspect_adapter import HAVE_INSPECT


def ag_config(**block):
    return {"agent_count": 3, "max_connections": 6, "agentic_monitor": {"enabled": True, **block}}


class StartupGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_concurrent_starts_and_releases_on_arrival(self):
        events = []
        barrier = runtime.StartBarrier(6, lambda *e: events.append(e))
        gate = runtime.StartupGate(2, barrier, lambda *e: events.append(e))
        starting, peak = set(), [0]

        async def agent(agent_id):
            try:
                await gate.acquire(agent_id)
                starting.add(agent_id)
                peak[0] = max(peak[0], len(starting))
                await asyncio.sleep(0.01)  # bridge start
                starting.discard(agent_id)
                await barrier.ready(agent_id)  # first request: releases the slot, then waits for all
            finally:
                gate.release(agent_id)

        await asyncio.wait_for(asyncio.gather(*(agent(f"agent-{i}") for i in range(6))), 5)
        self.assertEqual(peak[0], 2)
        self.assertEqual(gate.peak, 2)
        self.assertTrue(barrier.event.is_set())
        self.assertEqual(gate.held, set())
        slots = [e for e in events if e[0] == "startup_slot"]
        self.assertEqual(len(slots), 6)
        self.assertEqual(slots[0][2]["limit"], 2)

    async def test_failed_start_frees_slot_and_requeues(self):
        barrier = runtime.StartBarrier(2)
        gate = runtime.StartupGate(1, barrier)
        attempts = []

        async def agent(agent_id, failures):
            while True:
                try:
                    await gate.acquire(agent_id)
                    attempts.append(agent_id)
                    if failures:
                        failures -= 1
                        raise RuntimeError("MCPEndpointsUnreachableError")
                    await barrier.ready(agent_id)
                    return
                except RuntimeError:
                    await asyncio.sleep(0)
                finally:
                    gate.release(agent_id)

        await asyncio.wait_for(asyncio.gather(agent("a", 2), agent("b", 0)), 5)
        self.assertEqual(sorted(attempts), ["a", "a", "a", "b"])
        self.assertTrue(barrier.event.is_set())

    async def test_open_after_arrival_and_common_start(self):
        barrier = runtime.StartBarrier(1)
        gate = runtime.StartupGate(1, barrier)
        self.assertTrue(await gate.acquire("a"))
        self.assertFalse(await gate.acquire("a"))  # already held: no second slot
        await barrier.ready("a")
        self.assertEqual(gate.held, set())  # released by the barrier
        self.assertFalse(await gate.acquire("a"))  # restarts after the start are never gated
        self.assertFalse(await gate.acquire("b"))
        gate.release("b")  # no-op
        self.assertEqual(gate._semaphore._value, 1)

    def test_rejects_bad_limit(self):
        for bad in (0, -1, True, 2.5, None):
            with self.assertRaises(ValueError):
                runtime.StartupGate(bad, runtime.StartBarrier(1))

    async def test_barrier_survives_failing_callback(self):
        barrier = runtime.StartBarrier(1)
        barrier.on_arrive.append(lambda agent_id: 1 / 0)
        await asyncio.wait_for(barrier.ready("a"), 1)
        self.assertTrue(barrier.event.is_set())


class AgenticBridgeSettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = ag.agentic_settings(ag_config())
        self.assertEqual(settings["startup_concurrency"], 24)
        self.assertEqual(settings["mcp_ready_timeout_seconds"], 300)
        self.assertIsNone(settings["monitor_bridge_polling_seconds"])
        monitors = ag.AgenticMonitors(settings, conference=None, record=lambda *e: None, solver_ids=["agent-001"])
        self.assertEqual(monitors.bridge_options(), {"mcp_ready_timeout": 300.0})
        self.assertIsNone(monitors.startup_gate)

    def test_validation(self):
        for block, fragment in (({"startup_concurrency": 0}, "startup_concurrency"),
                                ({"startup_concurrency": True}, "startup_concurrency"),
                                ({"mcp_ready_timeout_seconds": 0}, "mcp_ready_timeout_seconds"),
                                ({"mcp_ready_timeout_seconds": "300"}, "mcp_ready_timeout_seconds"),
                                ({"monitor_bridge_polling_seconds": 0.1}, "monitor_bridge_polling_seconds"),
                                ({"monitor_bridge_polling_seconds": 31}, "monitor_bridge_polling_seconds")):
            problems = ag.agentic_problems(ag_config(**block))
            self.assertTrue(any(fragment in p for p in problems), (block, problems))
        self.assertEqual(ag.agentic_problems(ag_config(startup_concurrency=8, mcp_ready_timeout_seconds=90,
                                                       monitor_bridge_polling_seconds=5)), [])

    def test_monitor_session_uses_gate_and_releases_after_error(self):
        from inspect_ai.model import ChatMessageUser
        events, seen = [], []

        class Clock:
            def __init__(self):
                self.stop = asyncio.Event()
                self.reason = None

        async def scenario():
            clock = Clock()
            barrier = runtime.StartBarrier(2)
            settings = ag.agentic_settings(ag_config(startup_concurrency=1))

            async def sleeper(solver, seconds):
                await asyncio.sleep(0)

            monitors = ag.AgenticMonitors(settings, conference=None, record=lambda *e: events.append(e),
                                          solver_ids=["agent-001"], stop=clock.stop, sleeper=sleeper)
            monitors.startup_gate = gate = runtime.StartupGate(1, barrier)

            def build_agent(prompt, tools, gen_filter, **kwargs):
                seen.append(("kwargs", kwargs.get("mcp_ready_timeout")))

                async def agent(state):
                    seen.append(("held", set(gate.held)))
                    if len([s for s in seen if s[0] == "held"]) == 1:
                        raise RuntimeError("MCPEndpointsUnreachableError")
                    clock.reason = "max_run_seconds"
                    clock.stop.set()
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
            return gate
        with mock.patch.object(runtime, "restart_delay", lambda n: 0):
            gate = asyncio.run(scenario())
        self.assertEqual(seen[0], ("kwargs", 300.0))
        self.assertEqual([s for s in seen if s[0] == "held"], [("held", {"agmon-001"})] * 2)
        self.assertEqual(gate.held, set())


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class SolverGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_solver_sessions_take_a_slot_and_get_the_ag_timeout(self):
        import types
        import anyio
        from inspect_ai.model import ChatMessageUser
        from research_swarm import inspect_adapter
        from research_swarm.runtime import RunClock, Snapshotter, StartBarrier
        folder = Path(tempfile.mkdtemp()).resolve()
        events = []
        record = lambda kind, agent, payload: events.append((kind, agent, payload))  # noqa: E731
        barrier = StartBarrier(2, record)
        clock = RunClock(barrier)
        gate = runtime.StartupGate(1, barrier, record)
        seen = []

        def fake_agent(prompt, tools, filter, **kwargs):
            seen.append(("kwargs", kwargs.get("mcp_ready_timeout")))

            async def agent(state):
                seen.append(("held", set(gate.held)))
                if len(seen) == 2:
                    raise RuntimeError("MCPEndpointsUnreachableError")  # before reaching the barrier
                clock.request_stop("max_run_seconds", record)
                await anyio.sleep_forever()
            return agent

        async def no_snapshots(*args, **kwargs):
            await anyio.sleep_forever()

        async def no_final_snapshot(*args, **kwargs):
            return 0
        agentic = types.SimpleNamespace(is_monitor=lambda a: False, startup_gate=gate,
                                        bridge_options=lambda: {"mcp_ready_timeout": 300.0},
                                        solver_finished=lambda a: None)
        intercept = types.SimpleNamespace(state={"requests": 0})
        state = types.SimpleNamespace(sample_id="agent-001", messages=[ChatMessageUser(content="Begin.")], output=None)
        with mock.patch("inspect_ai.util.sandbox", return_value=object()), \
                mock.patch.object(inspect_adapter, "build_claude_agent", fake_agent), \
                mock.patch.object(inspect_adapter, "conference_tools", lambda *a, **k: []), \
                mock.patch.object(runtime, "restart_delay", lambda quick: 0), \
                mock.patch.object(Snapshotter, "run", no_snapshots), \
                mock.patch.object(Snapshotter, "poll", no_final_snapshot):
            solver = runtime.peer_solver(
                conference=types.SimpleNamespace(), definitions=[], personas={"agent-001": "algebra"},
                prompt_for=lambda *a: "prompt", filter_for=lambda a: intercept, run_dir=folder,
                record=record, clock=clock, auto_restart=True, agentic=agentic)
            await asyncio.wait_for(solver(state, None), 10)
        self.assertEqual(seen, [("kwargs", 300.0), ("held", {"agent-001"}), ("held", {"agent-001"})])
        self.assertEqual(gate.held, set())  # released when the session was cancelled at the stop
        self.assertEqual(len([e for e in events if e[0] == "startup_slot"]), 2)


@unittest.skipUnless(HAVE_INSPECT, "pinned Inspect packages not installed (.venv)")
class ModelServicePollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_override_of_model_service_only(self):
        from inspect_ai.agent._bridge.sandbox import service as bridge_service
        from research_swarm import inspect_adapter
        calls = []

        async def fake_sandbox_service(name, methods=None, until=None, sandbox=None, polling_interval=None, **kw):
            calls.append((name, polling_interval))

        saved = bridge_service.sandbox_service
        bridge_service.sandbox_service = fake_sandbox_service
        try:
            self.assertTrue(inspect_adapter.install_model_service_polling())
            self.assertTrue(inspect_adapter.install_model_service_polling())  # idempotent
            model = bridge_service.MODEL_SERVICE
            await bridge_service.sandbox_service(name=model, polling_interval=2)
            with inspect_adapter.model_service_polling(5):
                await bridge_service.sandbox_service(name=model, polling_interval=2)
                await bridge_service.sandbox_service(name="other", polling_interval=2)

                async def child():  # a task started inside the scope (the bridge's task group) inherits it
                    await bridge_service.sandbox_service(name=model, polling_interval=2)
                await asyncio.create_task(child())
            with inspect_adapter.model_service_polling(None):
                await bridge_service.sandbox_service(name=model, polling_interval=2)
            await bridge_service.sandbox_service(name=model, polling_interval=2)
        finally:
            bridge_service.sandbox_service = saved
        self.assertEqual([c[1] for c in calls], [2, 5.0, 2, 5.0, 2, 2])

    def test_run_model_service_looks_up_sandbox_service_at_call_time(self):
        # The patch relies on run_model_service calling the module-level name (inspect_ai 0.3.279).
        import inspect
        from inspect_ai.agent._bridge.sandbox import service as bridge_service
        source = inspect.getsource(bridge_service.run_model_service)
        self.assertIn("await sandbox_service(", source)
        self.assertIn("polling_interval=2", source)

    def test_build_claude_agent_passes_mcp_ready_timeout_only_when_set(self):
        import inspect_swe
        from research_swarm import inspect_adapter
        captured = []
        with mock.patch.object(inspect_swe, "claude_code", lambda **kw: captured.append(kw)):
            inspect_adapter.build_claude_agent("p", [], None)
            inspect_adapter.build_claude_agent("p", [], None, mcp_ready_timeout=300)
        self.assertNotIn("mcp_ready_timeout", captured[0])
        self.assertEqual(captured[1]["mcp_ready_timeout"], 300.0)


if __name__ == "__main__":
    unittest.main()
