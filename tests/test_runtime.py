import asyncio
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import research_swarm.runtime as runtime
from research_swarm.runtime import (LaunchBlocked, RunClock, Snapshotter, StartBarrier, assign_personas, exit_status,
                                    run_pilot, write_compose)

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_common_barrier(self):
        barrier=StartBarrier(20)
        waiting=[asyncio.create_task(barrier.ready(f"a{i}")) for i in range(19)]
        await asyncio.sleep(0)
        self.assertIsNone(barrier.started_at)
        self.assertFalse(any(task.done() for task in waiting))
        await barrier.ready("a19")
        await asyncio.gather(*waiting)
        self.assertIsNotNone(barrier.started_at)
    async def test_paid_launch_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(LaunchBlocked):await run_pilot({"agent_count":20,"track":"spontaneous"},folder,None)
    def test_balanced_personas(self):
        mapping=assign_personas(20,42)
        self.assertEqual(mapping,assign_personas(20,42))
        for name in set(mapping.values()):self.assertEqual(list(mapping.values()).count(name),5)
    def test_no_network_mounts(self):
        with tempfile.TemporaryDirectory() as folder:
            filename=write_compose(Path(folder)/"compose.yaml")
            text=Path(filename).read_text()
            self.assertIn("network_mode: none",text)
            self.assertNotIn("volumes:",text)
            self.assertNotIn("ports:",text)
            self.assertIn("no-new-privileges:true",text)


class RunClockTests(unittest.IsolatedAsyncioTestCase):
    class Conference:
        def __init__(self, depleted_after=None):
            self.started = time.monotonic()
            self.depleted_after = depleted_after
            self.marked = []
        def is_depleted(self):
            return self.depleted_after is not None and time.monotonic() - self.started >= self.depleted_after
        def mark_run_started(self, timestamp):
            self.marked.append(timestamp)

    async def _run(self, conference, arrive=True, **limits):
        events = []
        record = lambda kind, agent, payload: events.append((kind, payload))
        barrier = StartBarrier(1, record)
        clock = RunClock(barrier)
        if arrive:
            await barrier.ready("agent-001")
        await asyncio.wait_for(clock.monitor(conference, record, poll=0.02, **limits), timeout=5)
        return clock, events

    async def test_startup_timeout_aborts(self):
        clock, events = await self._run(self.Conference(), arrive=False, startup_timeout=0.05)
        self.assertEqual(clock.reason, "startup_timeout")
        self.assertTrue(clock.stop.is_set())

    async def test_max_run_seconds(self):
        conference = self.Conference()
        clock, events = await self._run(conference, startup_timeout=1, max_run_seconds=0.1, post_depletion_seconds=10)
        self.assertEqual(clock.reason, "max_run_seconds")
        self.assertEqual(len(conference.marked), 1)  # conference log aligned with the barrier

    async def test_monitor_cancellable_right_after_barrier(self):
        # Regression: asyncio.wait_for (Python 3.10) could swallow this cancellation.
        clock = RunClock(StartBarrier(1))
        await clock.barrier.ready("agent-001")
        task = asyncio.create_task(clock.monitor(None, lambda *a: None))
        await asyncio.sleep(0)
        task.cancel()
        done, _ = await asyncio.wait([task], timeout=3)
        self.assertEqual(done, {task})

    async def test_failing_depletion_check_is_recorded_and_max_still_enforced(self):
        """RT-4: an exception in is_depleted never ends the monitor; the maximum still stops the run."""
        class Broken(self.Conference):
            calls = 0

            def is_depleted(self):
                Broken.calls += 1
                if Broken.calls <= 3:
                    raise __import__("sqlite3").OperationalError("disk I/O error")
                return False
        clock, events = await self._run(Broken(), startup_timeout=1, max_run_seconds=0.3, post_depletion_seconds=10)
        self.assertEqual(clock.reason, "max_run_seconds")
        kinds = [k for k, _ in events]
        self.assertEqual(kinds.count("monitor_error"), 1)  # first failure of the streak only
        self.assertIn("monitor_recovered", kinds)
        error = next(p for k, p in events if k == "monitor_error")
        self.assertEqual((error["error"], error["consecutive_failures"]), ("OperationalError", 1))

    async def test_stop_is_set_even_when_recording_fails(self):
        clock = RunClock(StartBarrier(1))

        def broken_record(*args):
            raise OSError("disk full")
        with contextlib.redirect_stderr(io.StringIO()):
            clock.request_stop("max_run_seconds", broken_record)
        self.assertTrue(clock.stop.is_set())
        self.assertEqual(clock.reason, "max_run_seconds")

    async def test_seeded_trigger_polled_every_tick(self):
        """S3: the monitor polls poll_seeded_trigger each tick (before depletion), when present."""
        class Seeded(self.Conference):
            def __init__(self):
                super().__init__()
                self.polls = []

            def poll_seeded_trigger(self):
                self.polls.append(time.monotonic())
        conference = Seeded()
        clock, _ = await self._run(conference, startup_timeout=1, max_run_seconds=0.2, post_depletion_seconds=10)
        self.assertEqual(clock.reason, "max_run_seconds")
        self.assertGreaterEqual(len(conference.polls), 5)  # poll=0.02 over 0.2 s
        self.assertGreaterEqual(conference.polls[0], conference.started)
        # Without the hook (older services, fakes) nothing changes.
        clock, events = await self._run(self.Conference(), startup_timeout=1, max_run_seconds=0.1,
                                        post_depletion_seconds=10)
        self.assertNotIn("monitor_error", [k for k, _ in events])

    async def test_failing_seeded_poll_is_recorded_and_max_still_enforced(self):
        class Broken(self.Conference):
            def poll_seeded_trigger(self):
                raise RuntimeError("trigger store unavailable")
        clock, events = await self._run(Broken(), startup_timeout=1, max_run_seconds=0.2, post_depletion_seconds=10)
        self.assertEqual(clock.reason, "max_run_seconds")
        errors = [p for k, p in events if k == "monitor_error"]
        self.assertEqual([(e["stage"], e["consecutive_failures"]) for e in errors], [("seeded_trigger", 1)])

    async def test_seeded_time_trigger_fires_from_the_monitor_without_agent_actions(self):
        """S3 end to end: a real seeded ConferenceService injects on the run clock's tick."""
        from research_swarm.conference import ConferenceService
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "artifact.md"
            artifact.write_text("Seeded test post.\n")
            import hashlib
            config = {"track": "seeded", "seeded_contagion_enabled": True,
                      "seeded": {"enabled": True, "note": "test", "trigger_kind": "elapsed_seconds",
                                 "trigger_value": 0.05, "artifact_path": str(artifact),
                                 "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                                 "channel": "bulletin_board", "author": "agent-999", "intent_type": "contribution",
                                 "credit_effect": "none"}}
            arguments = runtime.conference_track_arguments(config)
            self.assertEqual(arguments["track"], "seeded")
            service = runtime.default_conference_factory(ROOT, compiler=object())(
                Path(folder) / "run", ["agent-001"], **arguments)
            try:
                self.assertEqual(service.track, "seeded")
                clock, _ = await self._run(service, startup_timeout=1, max_run_seconds=0.4, post_depletion_seconds=10)
            finally:
                service.close()
            events = [json.loads(line) for line in (Path(folder) / "run" / "conference" / "events.jsonl")
                      .read_text().splitlines()]
        kinds = [e["kind"] for e in events]
        self.assertIn("run_start_marked", kinds)
        self.assertIn("seeded_artifact_injected", kinds)
        self.assertLess(kinds.index("run_start_marked"), kinds.index("seeded_artifact_injected"))

    async def test_post_depletion_window_within_max(self):
        clock, events = await self._run(self.Conference(depleted_after=0.05), startup_timeout=1, max_run_seconds=5,
                                         post_depletion_seconds=0.1)
        self.assertEqual(clock.reason, "post_depletion_window")
        self.assertIn("task_pool_depleted", [k for k, _ in events])
        clock, _ = await self._run(self.Conference(depleted_after=0.05), startup_timeout=1, max_run_seconds=0.1,
                                   post_depletion_seconds=10)
        self.assertEqual(clock.reason, "max_run_seconds")


class _HostSandbox:
    """Runs the container-side listing with the host interpreter; reads host files."""
    def __init__(self, fail_reads=0):
        self.fail_reads = fail_reads
        self.reads = []

    async def exec(self, cmd, timeout=None, **kwargs):
        cmd = [sys.executable if cmd[0] == "python3" else cmd[0], *cmd[1:]]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return types.SimpleNamespace(success=proc.returncode == 0, stdout=proc.stdout, stderr=proc.stderr)

    async def read_file(self, path, text=True):
        self.reads.append(path)
        if self.fail_reads:
            self.fail_reads -= 1
            raise FileNotFoundError(path)
        return Path(path).read_bytes()


class SnapshotterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()).resolve()
        self.root = self.folder / "workspace"
        (self.root / ".lake").mkdir(parents=True)
        (self.root / ".lake" / "skip.lean").write_text("dependency")
        self.events = []
        self.now = [0.0]

    def snapshotter(self):
        return Snapshotter(self.folder / "snapshots", "agent-001", lambda k, a, p: self.events.append((k, p)),
                           roots=[str(self.root)], clock=lambda: self.now[0])

    def kinds(self, kind):
        return [p for k, p in self.events if k == kind]

    async def test_stamp_recorded_only_after_successful_read(self):
        (self.root / "Proof.lean").write_text("theorem t : True := trivial\n")
        snap, box = self.snapshotter(), _HostSandbox(fail_reads=1)
        self.assertEqual(await snap.poll(box), 0)
        self.assertEqual(self.kinds("snapshot_skipped")[0]["reason"], "FileNotFoundError")
        self.assertEqual(await snap.poll(box), 1)  # retried although the file did not change
        self.assertEqual(await snap.poll(box), 0)
        self.assertEqual(self.kinds("file_snapshot")[0]["path"], str(self.root / "Proof.lean"))
        self.assertFalse(any(".lake" in p["path"] for p in self.kinds("file_snapshot")))

    async def test_file_limit_stops_all_loops(self):
        for d in range(3):
            (self.root / f"d{d}").mkdir()
            for i in range(3):
                (self.root / f"d{d}" / f"f{i}.md").write_text(f"{d}{i}")
        with mock.patch.object(runtime, "SNAPSHOT_MAX_FILES", 4):
            await self.snapshotter().poll(_HostSandbox())
        self.assertEqual(len(self.kinds("file_snapshot")), 4)
        self.assertEqual(self.kinds("snapshot_skipped")[0]["reason"], "file_count_limit")

    async def test_transcript_deltas_and_slow_cadence(self):
        transcript = self.root / "session.jsonl"
        transcript.write_text('{"a": 1}\n')
        snap, box = self.snapshotter(), _HostSandbox()
        await snap.poll(box)
        first = self.kinds("file_snapshot")[0]
        self.assertEqual(first["mode"], "full")
        with transcript.open("a") as stream:
            stream.write('{"b": 2}\n')
        os.utime(transcript, ns=(time.time_ns(), time.time_ns() + 10**9))
        self.now[0] = runtime.TRANSCRIPT_INTERVAL / 2
        await snap.poll(box)
        self.assertEqual(len(self.kinds("file_snapshot")), 1)  # within the slow interval
        await snap.poll(box, final=True)  # the final poll always reads transcripts
        second = self.kinds("file_snapshot")[1]
        self.assertEqual((second["mode"], second["offset"]), ("append", len('{"a": 1}\n')))
        blob = Path(second["observer_blob"]).read_bytes()
        self.assertEqual(Path(first["observer_blob"]).read_bytes() + blob, transcript.read_bytes())

    async def test_size_limit_logged_once(self):
        big = self.root / "big.txt"
        big.write_text("x" * 50)
        snap, box = self.snapshotter(), _HostSandbox()
        with mock.patch.object(runtime, "SNAPSHOT_MAX_BYTES", 10):
            await snap.poll(box)
            big.write_text("y" * 60)
            await snap.poll(box)
        self.assertEqual([p["reason"] for p in self.kinds("snapshot_skipped")], ["size_limit"])
        self.assertEqual(box.reads, [])


class ExitAndConfigTests(unittest.TestCase):
    def test_exit_status_distinguishes_truncation(self):
        self.assertEqual(exit_status("harness_completed", {"consecutive_truncations": 0}), "harness_completed")
        self.assertEqual(exit_status("harness_completed", {"consecutive_truncations": 4}),
                         "harness_completed_after_truncation")
        self.assertEqual(exit_status("error", {"consecutive_truncations": 1}), "error_after_truncation")
        self.assertEqual(exit_status("stopped:max_run_seconds", {"consecutive_truncations": 2}), "stopped:max_run_seconds")
        self.assertEqual(exit_status("harness_completed", None), "harness_completed")

    def test_exit_status_separates_voluntary_exits_from_budget_and_empty_replies(self):
        """RT-3: only a clean end after a visible reply is labelled harness_completed (voluntary)."""
        finished = {"last_request_outcome": "finished", "consecutive_truncations": 0}
        self.assertEqual(exit_status("harness_completed", {**finished, "last_output_kind": "text"}), "harness_completed")
        for kind in ("reasoning_only", "empty"):
            self.assertEqual(exit_status("harness_completed", {**finished, "last_output_kind": kind}),
                             "harness_completed_empty_reply", kind)
        self.assertEqual(exit_status("harness_completed", {"last_request_outcome": "error", "last_output_kind": "text"}),
                         "harness_completed_after_provider_error")
        denied = {"last_request_outcome": "not_admitted", "last_denial": "budget_admission_failed"}
        self.assertEqual(exit_status("interrupted", denied), "budget_exhausted")
        self.assertEqual(exit_status("interrupted", {**denied, "last_denial": "budget_ledger_unavailable"}),
                         "budget_ledger_unavailable")
        # A run-stop refusal is labelled by the solver (stopped:<reason>), never as budget.
        self.assertEqual(exit_status("interrupted", {**denied, "last_denial": "run_stopped"}), "interrupted")
        self.assertEqual(exit_status("stopped:max_run_seconds", denied), "stopped:max_run_seconds")
        self.assertEqual(exit_status("interrupted", {}), "interrupted")

    def test_compose_fingerprint_matches_written_file(self):
        """X3: compose_fingerprint is the sha256 of exactly what make_compose writes."""
        import hashlib
        image = "sha256:" + "a" * 64
        for config in ({}, {"agent_memory": "2g", "agent_cpus": 1.5}):
            with tempfile.TemporaryDirectory() as folder:
                written = Path(runtime.make_compose(folder, config, image)).read_bytes()
            self.assertEqual(runtime.compose_fingerprint(config, image), hashlib.sha256(written).hexdigest())
        self.assertNotEqual(runtime.compose_fingerprint({}, image), runtime.compose_fingerprint({}, "sha256:" + "b" * 64))
        self.assertNotEqual(runtime.compose_fingerprint({}, image),
                            runtime.compose_fingerprint({"agent_memory": "2g"}, image))
        with self.assertRaises(ValueError):
            runtime.compose_fingerprint({}, "image; rm -rf /")

    def test_build_task_disables_inspect_dotenv(self):
        try:
            import inspect_ai._util.dotenv as inspect_dotenv
        except ImportError:
            self.skipTest("pinned Inspect packages not installed (.venv)")
        runtime.build_task(agent_ids=["agent-001"], files={}, solver=None, compose="compose.yaml", metadata={},
                           initial_message="hello")
        self.assertTrue(getattr(inspect_dotenv, "_gdm_dotenv_disabled", False))
        self.assertEqual(inspect_dotenv.find_dotenv(usecwd=True), "")

    def test_lean_compiler_receives_i1_parameters(self):
        captured = {}

        class FakeCompiler:
            def __init__(self, **kwargs):
                captured.update(kwargs)
        with mock.patch("research_swarm.lean.DockerLeanCompiler", FakeCompiler):
            runtime.make_lean_compiler({"lean_image": "img:x", "compilation_timeout_seconds": 120, "judge_memory": "3g",
                                        "judge_cpus": 2, "judge_max_concurrent": 4})
        self.assertEqual(captured, {"image": "img:x", "timeout": 120, "memory": "3g", "cpus": 2, "max_concurrent": 4})

        class OldCompiler:
            def __init__(self, image="i", timeout=120, memory="2g", cpus=1):
                pass
        with mock.patch("research_swarm.lean.DockerLeanCompiler", OldCompiler):
            with self.assertRaises(RuntimeError):
                runtime.make_lean_compiler({})

    def test_queue_observer_fallback_is_loud(self):
        events = []
        fake = types.ModuleType("research_swarm.evaluator")  # no make_queue_observer
        with mock.patch.dict(sys.modules, {"research_swarm.evaluator": fake}):
            with self.assertWarns(RuntimeWarning):
                self.assertIsNone(runtime.queue_observer(Path(tempfile.mkdtemp()), lambda *a: events.append(a)))
        self.assertEqual(events[0][0], "observer_unavailable")
        made = []
        fake.make_queue_observer = lambda out_dir: made.append(out_dir) or "observer"
        fake.run_evaluations_dir = lambda run_dir: Path(run_dir) / "evaluations"
        with mock.patch.dict(sys.modules, {"research_swarm.evaluator": fake}):
            self.assertEqual(runtime.queue_observer(Path("/r")), "observer")
        self.assertEqual(made, [Path("/r/evaluations")])
        from research_swarm import evaluator  # the real I2 convention evaluate_run.py reads
        self.assertEqual(evaluator.run_evaluations_dir(Path("/r")), Path("/r/evaluations"))

    def test_ledger_path_outside_runs(self):
        path = runtime.ledger_path(Path("/repo"))
        self.assertEqual(path, Path("/repo/state/budget.sqlite"))

    def test_default_factory_wires_observer_and_compiler(self):
        observer = lambda *a: None
        compiler = object()
        with tempfile.TemporaryDirectory() as run_dir:
            service = runtime.default_conference_factory(ROOT, observer=observer, compiler=compiler)(
                Path(run_dir), ["agent-001", "agent-002"])
            try:
                self.assertIs(service._observer, observer)
            finally:
                service.close()


class TrackWiringTests(unittest.TestCase):
    """S3: the conference receives a seeded configuration only for an enabled, complete seeded track."""
    def test_spontaneous_configs_pass_no_track_arguments(self):
        pilot = json.loads((ROOT / "configs" / "pilot.json").read_text())
        for config in ({}, {"track": "spontaneous"}, pilot, {"track": "spontaneous", "seeded": {"enabled": False}}):
            self.assertEqual(runtime.conference_track_arguments(config), {})
        for config in ({"track": "spontaneous", "seeded_contagion_enabled": True},
                       {"track": "spontaneous", "seeded": {"enabled": True}}, {"track": "mitigation"}):
            with self.assertRaises(LaunchBlocked):
                runtime.conference_track_arguments(config)

    def test_seeded_track_needs_enabled_and_every_semantic_field(self):
        example = json.loads((ROOT / "configs" / "seeded.example.json").read_text())
        with self.assertRaises(LaunchBlocked):  # disabled template
            runtime.conference_track_arguments(example)
        enabled = dict(example, seeded_contagion_enabled=True, seeded=dict(example["seeded"], enabled=True))
        with self.assertRaises(LaunchBlocked) as caught:  # enabled but semantics undecided (null)
            runtime.conference_track_arguments(enabled)
        self.assertIn("trigger_kind", str(caught.exception))
        complete = dict(enabled, seeded=dict(enabled["seeded"], trigger_kind="after_n_acceptances", trigger_value=1,
                                             artifact_path="/researcher/artifact.md", artifact_sha256="0" * 64,
                                             channel="bulletin_board", author="agent-999", intent_type="contribution",
                                             credit_effect="none", tags=[]))
        arguments = runtime.conference_track_arguments(complete)
        self.assertEqual(arguments["track"], "seeded")
        self.assertEqual(arguments["seeded_track"].trigger_kind, "after_n_acceptances")

    def test_make_conference_passes_track_arguments_only_for_seeded(self):
        calls = []

        def factory(run_dir, agent_ids, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(track=kwargs.get("track", "spontaneous"), close=lambda: calls.append("closed"))
        with mock.patch.object(runtime, "make_lean_compiler", lambda config, image=None: "compiler"):
            runtime.make_conference(ROOT, Path("/r"), ["agent-001"], {"track": "spontaneous"}, None,
                                    conference_factory=factory)
            seeded = {"track": "seeded", "seeded": {"enabled": True, "trigger_kind": "after_n_acceptances",
                                                    "trigger_value": 1, "artifact_path": "/a.md",
                                                    "artifact_sha256": "0" * 64, "channel": "bulletin_board",
                                                    "author": "agent-999", "intent_type": "contribution",
                                                    "credit_effect": "none"}}
            runtime.make_conference(ROOT, Path("/r"), ["agent-001"], seeded, None, conference_factory=factory)
            self.assertEqual(calls[0], {})
            self.assertEqual(sorted(calls[1]), ["seeded_track", "track"])
            # A factory that ignores the seeded track is refused (and its service closed), never run as baseline.
            ignoring = lambda run_dir, agent_ids, **kwargs: types.SimpleNamespace(  # noqa: E731
                track="spontaneous", close=lambda: calls.append("closed"))
            with self.assertRaises(LaunchBlocked):
                runtime.make_conference(ROOT, Path("/r"), ["agent-001"], seeded, None, conference_factory=ignoring)
            self.assertEqual(calls[-1], "closed")
            # Refused before any compiler or factory call.
            with self.assertRaises(LaunchBlocked):
                runtime.make_conference(ROOT, Path("/r"), ["agent-001"], {"track": "seeded"}, None,
                                        conference_factory=lambda *a, **k: self.fail("factory called"))


class LaunchInterlockTests(unittest.TestCase):
    """S1: the runtime-side interlock (read-only listing; every Docker call faked)."""
    COMPARATOR = {"id": "c" * 12, "name": "gdm-comparator-x", "image": "sha256:c", "kind": "comparator",
                  "compose_project": None}
    AGENT = {"id": "a" * 12, "name": "inspect-gdm_capacity-iabcdef1-default-1", "image": "sha256:a", "kind": "agent",
             "compose_project": "inspect-gdm_capacity-iabcdef1"}

    def interlock(self, judges=(), comparators=(), live=()):
        with mock.patch("research_swarm.lean.judge_containers",
                        lambda docker="docker": None if judges is None else list(judges)), \
                mock.patch.object(runtime, "_running_comparators",
                                  lambda docker="docker": None if comparators is None else list(comparators)), \
                mock.patch("research_swarm.evaluator.live_experiment_containers",
                           lambda docker="docker", agent_image=None: None if live is None else list(live)):
            return runtime.launch_interlock("gdm-math-swarm-agent:local")

    def test_kinds_and_blockers(self):
        self.assertEqual(self.interlock()["blockers"], [])
        status = self.interlock(["gdm-lean-old"], [self.COMPARATOR], [self.AGENT])
        self.assertEqual(len(status["blockers"]), 3)
        self.assertEqual(status["judge_containers"], ["gdm-lean-old"])
        self.assertEqual(sorted(c["kind"] for c in status["containers"]), ["agent", "comparator", "judge"])
        # A comparator also reported by the evaluator's listing is counted once.
        status = self.interlock([], [self.COMPARATOR], [dict(self.COMPARATOR, kind="comparator")])
        self.assertEqual(len(status["containers"]), 1)
        self.assertEqual(len(self.interlock(None, None, None)["blockers"]), 3)

    def test_running_comparators_lists_only_the_evaluator_label(self):
        seen = []

        def run(cmd, **kwargs):
            seen.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="%s\tgdm-comparator-1\tsha256:c\n\n" % ("f" * 64),
                                         stderr="")
        with mock.patch.object(runtime.subprocess, "run", run):
            found = runtime._running_comparators()
        self.assertEqual(found, [{"id": "f" * 12, "name": "gdm-comparator-1", "image": "sha256:c",
                                  "kind": "comparator", "compose_project": None}])
        self.assertIn("label=gdm-evaluator=1", seen[0])
        self.assertNotIn("-a", seen[0])  # running containers only
        self.assertNotIn("rm", seen[0])
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon")
        with mock.patch.object(runtime.subprocess, "run", return_value=failed):
            self.assertIsNone(runtime._running_comparators())
        with mock.patch.object(runtime.subprocess, "run", side_effect=OSError("no docker")):
            self.assertIsNone(runtime._running_comparators())

    def test_enforce_refuses_or_forces_with_a_recorded_warning(self):
        busy = lambda image: {"checked_at": "t", "blockers": ["1 running comparator container(s)"],  # noqa: E731
                              "containers": [self.COMPARATOR], "judge_containers": []}
        logged = []
        self.assertIsNone(runtime.enforce_launch_interlock(False, check=busy, log=logged.append))
        self.assertIn("Refusing to start", logged[-1])
        status = runtime.enforce_launch_interlock(True, check=busy, log=logged.append)
        self.assertTrue(status["forced"])
        self.assertIn("--force-while-live", logged[-1])
        clear = lambda image: {"checked_at": "t", "blockers": [], "containers": [], "judge_containers": []}  # noqa
        self.assertFalse(runtime.enforce_launch_interlock(False, check=clear, log=logged.append)["forced"])


class PreflightTests(unittest.TestCase):
    def _preflight(self, config, gates=None):
        modules = {}
        if gates is not None:
            module = types.ModuleType("research_swarm.launch_gates")
            module.evaluate = gates
            modules["research_swarm.launch_gates"] = module
        else:
            modules["research_swarm.launch_gates"] = None  # import fails
        with mock.patch.dict(sys.modules, modules), mock.patch.object(runtime.shutil, "which", return_value=None):
            return runtime.preflight(config, ROOT)

    def base(self, **overrides):
        return {"agent_count": 20, "track": "spontaneous", "max_connections": 20, **overrides}

    def test_max_connections_is_required_and_equals_agent_count(self):
        """S2: no silent default; one in-flight request per agent."""
        ok = lambda c, r: {"ready": True, "blockers": [], "evidence": {}, "ledger": {}}  # noqa: E731
        self.assertFalse(any("max_connections" in b for b in self._preflight(self.base(), ok)["blockers"]))
        missing = self.base()
        del missing["max_connections"]
        for config, fragment in ((missing, "set explicitly"), (self.base(max_connections=None), "set explicitly"),
                                 (self.base(max_connections=10), "differs from agent_count"),
                                 (self.base(max_connections=True), "positive integer"),
                                 (self.base(max_connections="20"), "positive integer"),
                                 (self.base(max_connections=0), "positive integer")):
            blockers = self._preflight(config, ok)["blockers"]
            self.assertTrue(any("max_connections" in b and fragment in b for b in blockers), (config, blockers))
        with self.assertRaises(LaunchBlocked):
            runtime.max_connections({})
        self.assertEqual(runtime.max_connections({"max_connections": 20}), 20)

    def test_any_seeded_configuration_blocks_the_pilot(self):
        """S3: the approved pilot refuses every seeded configuration, even a disabled template."""
        ok = lambda c, r: {"ready": True, "blockers": [], "evidence": {}, "ledger": {}}  # noqa: E731
        example = json.loads((ROOT / "configs" / "seeded.example.json").read_text())
        blockers = self._preflight(example, ok)["blockers"]
        self.assertIn("Initial pilot must be spontaneous baseline", blockers)
        self.assertTrue(any("seeded configuration is present" in b for b in blockers))
        disabled = self.base(seeded={"enabled": False})
        self.assertTrue(any("seeded configuration is present" in b for b in self._preflight(disabled, ok)["blockers"]))
        contradictory = self.base(seeded_contagion_enabled=True)
        self.assertTrue(any("Track configuration refused" in b
                            for b in self._preflight(contradictory, ok)["blockers"]))
        self.assertFalse(any("eeded" in b for b in self._preflight(self.base(), ok)["blockers"]))

    def test_missing_launch_gates_blocks(self):
        report = self._preflight(self.base())
        self.assertFalse(report["ready"])
        self.assertTrue(any("launch_gates" in b for b in report["blockers"]))
        self.assertFalse(report["credentials_read"])

    def test_launch_gate_blockers_are_merged(self):
        seen = []

        def evaluate(config, repo_root):
            seen.append(repo_root)
            return {"ready": False, "blockers": ["Isolation evidence missing"], "evidence": {"x": 1}, "ledger": {}}
        report = self._preflight(self.base(), evaluate)
        self.assertIn("Isolation evidence missing", report["blockers"])
        self.assertIn("Docker CLI unavailable", report["blockers"])
        self.assertEqual(report["launch_gates"]["evidence"], {"x": 1})
        self.assertEqual(seen, [ROOT])

    def test_design_invariants(self):
        ok = lambda c, r: {"ready": True, "blockers": [], "evidence": {}, "ledger": {}}
        blockers = self._preflight(self.base(agent_count=10, system_prompt_mode="replace"), ok)["blockers"]
        self.assertTrue(any("20 peers" in b for b in blockers))
        self.assertTrue(any("append + auto-memory" in b for b in blockers))
        # No attestation or approval: a config without paid_launch_enabled is not blocked for it.
        self.assertFalse(any("Paid launch" in b or "paid_launch_enabled" in b for b in blockers), blockers)
        raising = lambda c, r: 1 / 0
        self.assertTrue(any("evaluation failed" in b for b in self._preflight(self.base(), raising)["blockers"]))


class PreflightDockerTests(unittest.TestCase):
    """Docker-dependent preflight checks with every Docker call faked."""
    def _preflight(self, config, gates, leftover, comparators=(), live=()):
        module = types.ModuleType("research_swarm.launch_gates")
        module.evaluate = gates
        info = types.SimpleNamespace(returncode=0, stdout=json.dumps({"NCPU": 8, "MemTotal": 8 << 30}), stderr="")
        listed = []
        with mock.patch.dict(sys.modules, {"research_swarm.launch_gates": module}), \
                mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/docker"), \
                mock.patch.object(runtime.subprocess, "run", return_value=info), \
                mock.patch.object(runtime, "image_ids", lambda tags: {t: "sha256:" + "c" * 64 for t in tags}), \
                mock.patch("research_swarm.lean.judge_containers", return_value=leftover), \
                mock.patch.object(runtime, "_running_comparators",
                                  lambda docker="docker": None if comparators is None else list(comparators)), \
                mock.patch("research_swarm.evaluator.live_experiment_containers",
                           lambda docker="docker", agent_image=None: listed.append(agent_image) or (
                               None if live is None else list(live))):
            report = runtime.preflight(config, ROOT)
        self.listed_agent_images = listed
        return report

    def base(self, **overrides):
        return {"agent_count": runtime.APPROVED_AGENT_COUNT, "track": "spontaneous",
                "max_connections": runtime.APPROVED_AGENT_COUNT, **overrides}

    def test_interlock_blocks_comparators_and_other_agent_sandboxes(self):
        """S1: preflight refuses while a comparator or another run's agent sandbox runs (no override)."""
        ok = lambda c, r: {"ready": True, "blockers": [], "evidence": {}, "ledger": {}}  # noqa: E731
        comparator = {"id": "c" * 12, "name": "gdm-comparator-1a2b", "image": "sha256:x", "kind": "comparator",
                      "compose_project": None}
        agent = {"id": "a" * 12, "name": "inspect-gdm_math_swar-iabc123-default-1", "image": "sha256:y",
                 "kind": "agent", "compose_project": "inspect-gdm_math_swar-iabc123"}
        running_judge = {"id": "j" * 12, "name": "gdm-lean-1", "image": "l", "kind": "judge", "compose_project": None}
        report = self._preflight(self.base(agent_image="custom:agent"), ok, ["gdm-lean-1"], [comparator],
                                 [running_judge, agent])
        blockers = report["blockers"]
        self.assertTrue(any("comparator container" in b and "gdm-comparator-1a2b" in b and "gdm-evaluator=1" in b
                            for b in blockers), blockers)
        self.assertTrue(any("agent sandbox container" in b and "inspect-gdm_math_swar-iabc123" in b
                            for b in blockers), blockers)
        self.assertTrue(any("leftover judge container" in b for b in blockers))
        self.assertEqual(self.listed_agent_images, ["custom:agent"])  # the configured agent image is matched
        kinds = sorted(c["kind"] for c in report["docker"]["interlock"]["containers"])
        self.assertEqual(kinds, ["agent", "comparator", "judge"])  # the running judge is listed once
        self.assertFalse(report["ready"])
        # Listing failures block too.
        blockers = self._preflight(self.base(), ok, [], None, None)["blockers"]
        self.assertTrue(any("Could not list comparator containers" in b for b in blockers))
        self.assertTrue(any("Could not list running agent sandbox containers" in b for b in blockers))
        clear = self._preflight(self.base(), ok, [])
        self.assertFalse(any("container" in b for b in clear["blockers"]), clear["blockers"])
        self.assertEqual(clear["docker"]["interlock"]["containers"], [])

    def test_leftover_judge_containers_block(self):
        ok = lambda c, r: {"ready": True, "blockers": [], "evidence": {}, "ledger": {}}  # noqa: E731
        blockers = self._preflight(self.base(), ok, ["gdm-lean-0123"])["blockers"]
        self.assertTrue(any("leftover judge container" in b and "gdm-lean-0123" in b for b in blockers))
        blockers = self._preflight(self.base(), ok, None)["blockers"]
        self.assertTrue(any("Could not list leftover judge containers" in b for b in blockers))
        report = self._preflight(self.base(), ok, [])
        self.assertFalse(any("judge container" in b for b in report["blockers"]))
        self.assertEqual(report["docker"]["judge_containers"], [])
        self.assertEqual(report["docker"]["agent_image"]["id"], "sha256:" + "c" * 64)

    def test_no_paid_launch_attestation_is_required(self):
        ok = lambda c, r: {"ready": True, "blockers": [], "evidence": {}, "ledger": {}}  # noqa: E731
        report = self._preflight(self.base(paid_launch_enabled=False), ok, [])
        self.assertTrue(report["ready"], report["blockers"])

    def test_persona_counts_must_match_balanced_allocation(self):
        ok = lambda c, r: {"ready": True, "blockers": [], "evidence": {}, "ledger": {}}  # noqa: E731
        balanced = {"algebra": 5, "analysis_topology": 5, "combinatorics": 5, "number_theory": 5}
        report = self._preflight(self.base(persona_counts=balanced), ok, [])
        self.assertEqual(report["persona_counts"], balanced)
        self.assertFalse(any("persona_counts" in b for b in report["blockers"]))
        skewed = dict(balanced, algebra=6, number_theory=4)
        self.assertTrue(any("persona_counts" in b for b in self._preflight(self.base(persona_counts=skewed), ok,
                                                                           [])["blockers"]))


class ImagePinningTests(unittest.TestCase):
    def test_pin_images_resolves_ids_or_refuses(self):
        ids = {"gdm-math-swarm-agent:local": "sha256:" + "a" * 64, "gdm-math-swarm-lean:local": "sha256:" + "b" * 64}
        images = runtime.pin_images({}, lookup=lambda tags: {t: ids.get(t) for t in tags})
        self.assertEqual(images, {"agent": {"tag": "gdm-math-swarm-agent:local", "id": ids["gdm-math-swarm-agent:local"]},
                                  "lean": {"tag": "gdm-math-swarm-lean:local", "id": ids["gdm-math-swarm-lean:local"]}})
        with self.assertRaises(LaunchBlocked) as caught:
            runtime.pin_images({"lean_image": "missing:tag"}, lookup=lambda tags: {t: ids.get(t) for t in tags})
        self.assertIn("missing:tag", str(caught.exception))

    def test_preflight_and_pin_must_agree(self):
        images = {"agent": {"tag": "a:1", "id": "sha256:new"}, "lean": {"tag": "l:1", "id": "sha256:l"}}
        same = {"docker": {"agent_image": {"tag": "a:1", "id": "sha256:new"}, "lean_image": {"tag": "l:1", "id": "sha256:l"}}}
        runtime.check_pinned_against_preflight(images, same)
        changed = {"docker": {"agent_image": {"tag": "a:1", "id": "sha256:old"}}}
        with self.assertRaises(LaunchBlocked):
            runtime.check_pinned_against_preflight(images, changed)

    def test_compose_uses_image_id_and_never_pulls(self):
        image = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as folder:
            text = Path(runtime.make_compose(folder, {"agent_memory": "1g"}, image)).read_text()
        self.assertIn(f"image: {image}\n", text)
        self.assertIn("pull_policy: never", text)

    def test_lean_compiler_image_override(self):
        captured = {}

        class FakeCompiler:
            def __init__(self, **kwargs):
                captured.update(kwargs)
        with mock.patch("research_swarm.lean.DockerLeanCompiler", FakeCompiler):
            runtime.make_lean_compiler({"lean_image": "tag:x"}, image="sha256:" + "b" * 64)
        self.assertEqual(captured["image"], "sha256:" + "b" * 64)

    def test_make_conference_uses_pinned_lean_image(self):
        seen = []
        with mock.patch.object(runtime, "make_lean_compiler", lambda config, image=None: seen.append(image) or "c"):
            conference, compiler, observer = runtime.make_conference(
                ROOT, Path("/r"), ["agent-001"], {}, None, lean_image="sha256:l",
                conference_factory=lambda run_dir, agent_ids: ("service", run_dir, agent_ids))
        self.assertEqual((conference, compiler, observer, seen), (("service", Path("/r"), ["agent-001"]), "c", None,
                                                                   ["sha256:l"]))


class PersonaAllocationTests(unittest.TestCase):
    def test_balanced_for_any_count(self):
        for count in range(0, 41):
            counts = runtime.persona_counts(assign_personas(count, 260904170))
            self.assertEqual(sorted(counts), sorted(runtime.PERSONA_NAMES))
            self.assertEqual(sum(counts.values()), count)
            self.assertLessEqual(max(counts.values()) - min(counts.values()), 1, count)

    def test_approved_count_constant(self):
        self.assertEqual(runtime.APPROVED_AGENT_COUNT, 20)
        from research_swarm import launch_gates  # the evidence gates must not drift from the runtime constant
        self.assertEqual(launch_gates.APPROVED_SETTINGS["agent_count"], runtime.APPROVED_AGENT_COUNT)
        self.assertEqual(set(runtime.persona_counts(assign_personas(runtime.APPROVED_AGENT_COUNT, 1)).values()), {5})


class WarmUpTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.record = lambda kind, agent, payload: self.events.append((kind, payload))

    def test_success_records_vm_state_before_and_after(self):
        from research_swarm.lean import CompileResult
        calls = []

        class Compiler:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def compile(self, source):
                calls.append(source)
                return CompileResult("success", 0, "", "", 0, elapsed_seconds=41.5)
        states = iter([{"vm_uptime_seconds": 30.0, "Cached_kib": 10}, {"vm_uptime_seconds": 90.0, "Cached_kib": 5_800_000}])
        result = runtime.warm_up_lean("sha256:lean", self.record, compiler_factory=Compiler,
                                      state=lambda image: next(states))
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [{"image": "sha256:lean", "timeout": 900, "memory": "6g", "cpus": 2},
                                 runtime.WARM_UP_SOURCE])
        self.assertIn("import FormalConjectures.Util.ProblemImports", runtime.WARM_UP_SOURCE)
        self.assertEqual((result["vm_before"]["Cached_kib"], result["vm_after"]["Cached_kib"]), (10, 5_800_000))
        self.assertEqual(self.events, [("lean_warm_up", result)])

    def test_failure_is_recorded_loudly_but_not_fatal(self):
        from research_swarm.lean import CompileResult

        class TimesOut:
            def __init__(self, **kwargs):
                pass

            def compile(self, source):
                return CompileResult("timeout", 137, "", "timeout: sending signal KILL", None, elapsed_seconds=900,
                                     timeout_source="container")

        class Broken:
            def __init__(self, **kwargs):
                raise OSError("docker missing")
        for factory, status in ((TimesOut, "timeout"), (Broken, "exception")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertWarns(RuntimeWarning):
                result = runtime.warm_up_lean("img", self.record, compiler_factory=factory, state=lambda image: {})
            self.assertEqual((result["ok"], result["status"]), (False, status))
            self.assertIn("LEAN WARM-UP FAILED", stderr.getvalue())
        self.assertEqual([k for k, _ in self.events], ["lean_warm_up", "lean_warm_up"])

    def test_options_from_config(self):
        self.assertEqual(runtime.warm_up_options({}), {"memory": "6g", "timeout": 900})
        self.assertEqual(runtime.warm_up_options({"warm_up_memory": "5g", "warm_up_timeout_seconds": 600}),
                         {"memory": "5g", "timeout": 600})

    def test_vm_state_reads_proc_from_a_trivial_container(self):
        commands = []

        def runner(cmd, **kwargs):
            commands.append(cmd)
            return types.SimpleNamespace(returncode=0, stderr="", stdout=(
                "379.12 2000.0\nMemTotal:        8126464 kB\nMemFree:  1000 kB\nCached:         2097152 kB\n"
                "Active(file):     1048576 kB\nSwapTotal: 0 kB\n"))
        state = runtime.vm_state("sha256:lean", runner=runner)
        self.assertEqual(state, {"vm_uptime_seconds": 379.12, "MemTotal_kib": 8126464, "MemFree_kib": 1000,
                                 "Cached_kib": 2097152, "Active(file)_kib": 1048576})
        self.assertIn("--network", commands[0])
        self.assertIn("sha256:lean", commands[0])
        failed = runtime.vm_state("x", runner=lambda cmd, **k: types.SimpleNamespace(returncode=125, stdout="",
                                                                                     stderr="no such image"))
        self.assertEqual(failed["error"], "exit 125")

        def raising(cmd, **kwargs):
            raise OSError("no docker")
        self.assertEqual(runtime.vm_state("x", runner=raising), {"error": "OSError"})


class SharedPaidPathTests(unittest.IsolatedAsyncioTestCase):
    def _build(self, check, keys=None, events=None, *, record_dir=None):
        from research_swarm.provider import settings_from_config
        config = json.loads((ROOT / "configs" / "pilot.json").read_text())
        settings = settings_from_config(config, pricing_verified=True)
        captured, keys = {}, keys if keys is not None else []
        events = events if events is not None else []

        async def generate(*args):
            return None

        def fake_get_model(name, **kwargs):
            captured.update(name=name, **kwargs)
            return types.SimpleNamespace(api=types.SimpleNamespace(generate=generate))
        with mock.patch("inspect_ai.model.get_model", fake_get_model), \
                mock.patch("research_swarm.secrets.load_openrouter_key",
                           lambda root: keys.append(root) or "sk-or-v1-unpaidofflinetestplaceholder"):
            result = runtime.build_openrouter_model(settings, ROOT, 20, run_budget_usd="100",
                                                    record=lambda kind, agent, payload: events.append((kind, payload)),
                                                    check_limit=check, record_dir=record_dir)
        return settings, captured, keys, events, result

    def test_build_openrouter_model_reads_key_only_at_build(self):
        checked = []

        def check(key, budget):
            checked.append((key, budget))
            return {"limit": 20, "limit_remaining": 19.5, "limit_reset": None, "checks": {}, "warnings": []}
        settings, captured, keys, events, (model, limit) = self._build(check)
        self.assertEqual(keys, [ROOT])
        # X2: recorded with the loaded key and the run's budget, before the model exists.
        self.assertEqual(checked, [("sk-or-v1-unpaidofflinetestplaceholder", Decimal("100"))])
        self.assertEqual(limit["limit_remaining"], 19.5)
        self.assertEqual(events, [("provider_limit_recorded", {**limit, "run_budget_usd": "100"})])
        self.assertNotIn("unpaidofflinetestplaceholder", json.dumps(events))
        self.assertEqual(captured["name"], "openrouter/" + settings.model_id)
        self.assertEqual((captured["api_key"], captured["max_retries"], captured["reasoning_enabled"]),
                         ("sk-or-v1-unpaidofflinetestplaceholder", 0, True))
        self.assertEqual(captured["provider"], settings.request_options()["provider"])
        generate_config = captured["config"]
        self.assertEqual((generate_config.max_tokens, generate_config.max_retries, generate_config.reasoning_history,
                          generate_config.max_connections, generate_config.cache),
                         (settings.max_tokens, 0, "all", 20, False))
        self.assertTrue(model.api._gdm_cost_capture)

    def test_provider_limit_is_informational_and_never_refuses(self):
        def failing(key, budget):
            raise RuntimeError(f"limit_remaining 250 exceeds {budget} for {key}")
        events = []
        _, captured, _, _, (model, limit) = self._build(failing, events=events)
        self.assertIsNotNone(model)  # the model is built anyway
        self.assertEqual([k for k, _ in events], ["provider_limit_recorded"])
        self.assertIn("RuntimeError", limit["error"])
        self.assertNotIn("unpaidofflinetestplaceholder", json.dumps(events) + json.dumps(limit))
        unlimited = lambda key, budget: {"limit": None, "limit_remaining": None,  # noqa: E731
                                         "warnings": ["the key has no provider-side limit"]}
        _, _, _, _, (model, limit) = self._build(unlimited)
        self.assertEqual(limit["warnings"], ["the key has no provider-side limit"])

    def test_provider_limit_record_is_refreshed_automatically(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, _, _, (_, limit) = self._build(lambda key, budget: {"limit": 5, "limit_remaining": 5,
                                                                      "all_required_passed": True},
                                                 record_dir=folder)
            files = list(Path(folder).glob("provider-limit-*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(limit["record"], str(files[0]))
            record = json.loads(files[0].read_text())
        self.assertEqual((record["source"], record["limit_remaining"], record["credential_recorded"]),
                         ("paid launch", 5, False))
        self.assertNotIn("unpaidofflinetestplaceholder", json.dumps(record))

    def test_eval_options(self):
        options = runtime.eval_options(1, Path("/r"), {})
        self.assertEqual((options["max_samples"], options["max_sandboxes"], options["retry_on_error"],
                          options["sandbox_prebuilt"], options["log_dir"], options["max_subprocesses"]),
                         (1, 1, 0, True, "/r/inspect", 64))

    async def _clock(self, events):
        record = lambda kind, agent, payload: events.append(kind)  # noqa: E731
        clock = RunClock(StartBarrier(1, record))
        await clock.barrier.ready("agent-001")
        return clock, record

    async def test_supervise_returns_result_and_cancels_after_grace(self):
        events = []
        clock, record = await self._clock(events)

        async def quick():
            return "logs"
        self.assertEqual(await runtime.supervise(quick(), clock, None, record, {}), "logs")
        clock, record = await self._clock(events)
        with self.assertRaises(asyncio.CancelledError):
            await runtime.supervise(asyncio.sleep(30), clock, None, record,
                                    {"stop_grace_seconds": 0.05, "in_flight_grace_seconds": 0}, max_run_seconds=0)
        self.assertIn("run_stop", events)
        self.assertIn("evaluation_cancelled", events)

    async def test_natural_completion_logs_stop_after_all_sample_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runtime.jsonl"
            record = runtime.make_recorder(path)
            clock = RunClock(StartBarrier(1, record))
            await clock.barrier.ready("agent-001")

            async def completed_samples():
                record("agent_exit", "agent-001", {"status": "harness_completed", "error": None})
                return "logs"

            result = await runtime.supervise(completed_samples(), clock, None, record, {})
            self.assertEqual(result, "logs")
            events = [json.loads(line) for line in path.read_text().splitlines()]
            exit_event = next(e for e in events if e["kind"] == "agent_exit")
            stop_event = next(e for e in events if e["kind"] == "run_stop")
            self.assertLess(events.index(exit_event), events.index(stop_event))
            self.assertLess(exit_event["timestamp"], stop_event["timestamp"])
            self.assertEqual(clock.reason, "evaluation_finished")
            self.assertEqual(len([e for e in events if e["kind"] == "run_stop"]), 1)

    async def test_supervise_grace_covers_the_in_flight_grace(self):
        events = []
        clock, record = await self._clock(events)

        async def slow_stop():  # samples need the in-flight grace plus their own stop time
            await clock.stop.wait()
            await asyncio.sleep(0.3)
            return "logs"
        self.assertEqual(await runtime.supervise(slow_stop(), clock, None, record,
                                                 {"stop_grace_seconds": 0.1, "in_flight_grace_seconds": 0.5},
                                                 max_run_seconds=0), "logs")
        self.assertNotIn("evaluation_cancelled", events)
        with self.assertRaises(ValueError):
            runtime.in_flight_grace_seconds({"in_flight_grace_seconds": -1})
        self.assertEqual(runtime.in_flight_grace_seconds({}), runtime.IN_FLIGHT_GRACE_SECONDS)

    async def test_wait_for_in_flight(self):
        """SPEND-5: after a stop, wait (bounded) for the admitted request to settle."""
        state = {"in_flight": 1}

        async def settle():
            await asyncio.sleep(0.2)
            state["in_flight"] = 0
        task = asyncio.create_task(settle())
        at_stop, remaining, waited = await runtime.wait_for_in_flight(state, grace=5, poll=0.01)
        await task
        self.assertEqual((at_stop, remaining), (1, 0))
        self.assertLess(waited, 2)
        stuck = {"in_flight": 1}
        self.assertEqual((await runtime.wait_for_in_flight(stuck, grace=0.05, poll=0.01))[:2], (1, 1))
        self.assertEqual((await runtime.wait_for_in_flight(None, grace=5))[:2], (0, 0))

    async def test_monitor_exception_requests_stop_and_is_recorded(self):
        """RT-4: a monitor that raises sets the stop (reason monitor_error) so samples stop themselves."""
        events = []
        record = lambda kind, agent, payload: events.append((kind, payload))  # noqa: E731
        clock = RunClock(StartBarrier(1, record))
        await clock.barrier.ready("agent-001")

        async def broken_monitor(*args, **kwargs):
            raise OSError("runtime.jsonl write failed")

        async def samples():
            await clock.stop.wait()  # stop themselves on the signal
            return "logs"
        with mock.patch.object(clock, "monitor", broken_monitor):
            result = await runtime.supervise(samples(), clock, None, record,
                                             {"stop_grace_seconds": 5, "in_flight_grace_seconds": 0})
        self.assertEqual(result, "logs")
        self.assertEqual(clock.reason, "monitor_error")
        kinds = [k for k, _ in events]
        self.assertIn("monitor_error", kinds)
        self.assertLess(kinds.index("monitor_error"), kinds.index("run_stop"))
        error = next(p for k, p in events if k == "monitor_error")
        self.assertEqual((error["error"], error["detail"]), ("OSError", "runtime.jsonl write failed"))
        self.assertNotIn("evaluation_cancelled", kinds)

    async def test_supervise_disables_inspect_dotenv(self):
        events = []
        clock, record = await self._clock(events)
        calls = []
        with mock.patch("research_swarm.inspect_adapter.disable_inspect_dotenv", lambda: calls.append(1) or {}):
            async def quick():
                self.assertEqual(calls, [1])  # before the evaluation coroutine first runs
                return "logs"
            self.assertEqual(await runtime.supervise(quick(), clock, None, record, {}), "logs")


class RunPilotOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    """run_pilot with every Docker/Inspect/model boundary faked: order and records."""
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()).resolve()
        self.config = dict(json.loads((ROOT / "configs" / "pilot.json").read_text()),
                           run_dir=str(self.folder / "runs" / "pilot-test"))
        self.config.setdefault("max_connections", self.config["agent_count"])
        self.images = {"agent": {"tag": self.config["agent_image"], "id": "sha256:" + "a" * 64},
                       "lean": {"tag": self.config["lean_image"], "id": "sha256:" + "b" * 64}}
        self.report = {"ready": True, "blockers": [], "docker": {
            "agent_image": dict(self.images["agent"]), "lean_image": dict(self.images["lean"])}}

    async def run_pilot(self, report=None, during_eval=None):
        order = []

        class Conference:
            closed = False

            def close(self):
                self.closed = True

            def is_depleted(self):
                return False
        conference = Conference()
        compiler = types.SimpleNamespace(image=self.images["lean"]["id"], timeout=120, memory="2g", cpus=1,
                                         max_concurrent=None)

        def make_conference(repo_root, run_dir, agent_ids, config, record, *, lean_image=None, conference_factory=None):
            order.append(("conference", lean_image, len(agent_ids)))
            return conference, compiler, None

        def warm_up(image, record, **options):
            order.append(("warm_up", image))
            result = {"ok": True, "status": "success", "vm_before": {}, "vm_after": {}}
            record("lean_warm_up", None, result)
            return result

        def build_model(settings, repo_root, max_connections, *, run_budget_usd, record=None, record_dir=None):
            self.model_record_dir = record_dir
            order.append(("model", max_connections, str(run_budget_usd)))
            return "paid-model", {"limit": 20, "limit_remaining": 20.0, "limit_reset": None}

        async def eval_async(task, model, **options):
            order.append(("eval", model, options["max_samples"], options["sandbox_prebuilt"]))
            if during_eval is not None:
                await during_eval()
            return ["log"]
        with mock.patch.object(runtime, "preflight", return_value=report or self.report), \
                mock.patch.object(runtime, "pin_images", return_value=self.images), \
                mock.patch.object(runtime, "export_public_tasks", return_value=([], {})), \
                mock.patch.object(runtime, "make_conference", make_conference), \
                mock.patch.object(runtime, "warm_up_lean", warm_up), \
                mock.patch.object(runtime, "build_openrouter_model", build_model), \
                mock.patch("inspect_ai.eval_async", eval_async):
            result = await run_pilot(self.config, self.folder)
        return result, order, conference

    async def test_pins_images_and_warms_up_before_evaluation(self):
        result, order, conference = await self.run_pilot()
        self.assertEqual(result, ["log"])
        self.assertEqual(order, [("warm_up", self.images["lean"]["id"]), ("conference", self.images["lean"]["id"], 20),
                                 ("model", 20, "100"), ("eval", "paid-model", 20, True)])
        self.assertEqual(self.model_record_dir, self.folder / "data" / "validation")
        run_dir = Path(self.config["run_dir"])
        self.assertIn(f"image: {self.images['agent']['id']}", (run_dir / "compose.yaml").read_text())
        effective = json.loads((run_dir / "settings.json").read_text())["effective"]
        # X2/X3/X1 records: provider limit, compose fingerprint, Inspect .env loading disabled.
        self.assertEqual(effective["provider_limit"]["limit_remaining"], 20.0)
        self.assertEqual(effective["compose_sha256"], runtime.compose_fingerprint(self.config, self.images["agent"]["id"]))
        self.assertTrue(effective["inspect_dotenv"]["dotenv_disabled"])
        self.assertEqual(effective["in_flight_grace_seconds"], runtime.IN_FLIGHT_GRACE_SECONDS)
        records = [json.loads(line) for line in (run_dir / "runtime.jsonl").read_text().splitlines()]
        credential = [r["payload"] for r in records if r["kind"] == "credential_environment"]
        self.assertEqual(credential, [{"credentials_in_environment": "OPENROUTER_API_KEY" in os.environ}])
        self.assertEqual(effective["images"], self.images)
        self.assertTrue(effective["lean_warm_up"]["ok"])
        self.assertEqual(effective["persona_counts"], {"algebra": 5, "analysis_topology": 5, "combinatorics": 5,
                                                       "number_theory": 5})
        kinds = [json.loads(line)["kind"] for line in (run_dir / "runtime.jsonl").read_text().splitlines()]
        self.assertLess(kinds.index("images_pinned"), kinds.index("lean_warm_up"))
        self.assertTrue(conference.closed)
        self.assertTrue((run_dir / "budget-final.json").exists())
        # The run and its budget are registered in the shared ledger under the run directory's name.
        final = json.loads((run_dir / "run-budget.json").read_text())
        self.assertEqual((final["run_id"], final["scope"], final["budget_usd"], final["budget_source"]),
                         ("pilot-test", "pilot", "100", "config run_budget_usd"))
        self.assertIsNotNone(final["ended"])
        self.assertEqual((effective["run_id"], effective["run_budget_usd"]), ("pilot-test", "100"))
        self.assertIn("run_budget", kinds)

    async def test_max_connections_is_explicit_and_recorded(self):
        """S2: the model gets exactly config max_connections, recorded in settings.json; no default."""
        self.config["max_connections"] = runtime.APPROVED_AGENT_COUNT
        result, order, _ = await self.run_pilot()
        self.assertIn(("model", runtime.APPROVED_AGENT_COUNT, "100"), order)
        settings = json.loads((Path(self.config["run_dir"]) / "settings.json").read_text())
        self.assertEqual(settings["effective"]["max_connections"], runtime.APPROVED_AGENT_COUNT)
        self.assertEqual(settings["config"]["max_connections"], runtime.APPROVED_AGENT_COUNT)
        self.assertEqual(settings["effective"]["track"], "spontaneous")

    async def test_default_budget_of_one_hundred_applies_when_unset(self):
        del self.config["run_budget_usd"]
        _, order, _ = await self.run_pilot()
        self.assertIn(("model", 20, "100"), order)
        from research_swarm.budget import read_run
        run = read_run(self.folder / "state" / "budget.sqlite", "pilot-test")
        self.assertEqual((run["budget_usd"], run["budget_source"]), ("100", "default"))
        records = [json.loads(line) for line in (Path(self.config["run_dir"]) / "runtime.jsonl").read_text()
                   .splitlines()]
        budget = next(r["payload"] for r in records if r["kind"] == "run_budget")
        self.assertEqual((budget["budget_usd"], budget["budget_source"]), ("100", "default"))

    async def test_run_budget_stops_the_pilot_and_old_holds_never_block_it(self):
        """The production budget filter of this run admits requests only within its own budget,
        while an earlier run's unresolved holds in the same ledger never count against it."""
        from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelOutput, ModelUsage
        from inspect_ai.util import LimitExceededError
        from research_swarm.budget import BudgetLedger, list_requests, read_run
        ledger_file = self.folder / "state" / "budget.sqlite"
        earlier = BudgetLedger(ledger_file)
        earlier.start_run("earlier-run", "pilot", 1)
        earlier.settle(earlier.reserve("pilot", "agent-001", 1), None)  # an unresolved hold of an earlier run
        self.config["run_budget_usd"] = "0.1"
        captured, outcomes = {}, []

        def fake_peer_solver(**kwargs):
            captured.update(kwargs)
            return "solver"

        class FakeModel:
            async def generate(self, **kwargs):
                output = ModelOutput.from_content(model="deepseek/deepseek-v4.1-flash", content="ok")
                output.usage = ModelUsage(input_tokens=5, output_tokens=2, total_tokens=7, total_cost=0.00001)
                return output

        async def during_eval():
            async def one(agent_id):
                intercept = captured["filter_for"](agent_id)
                try:
                    await intercept(FakeModel(), [ChatMessageUser(content="hi")], [], None, GenerateConfig())
                    outcomes.append("finished")
                except LimitExceededError:
                    outcomes.append(intercept.state["last_denial"])
            await asyncio.gather(*(one(agent) for agent in captured["personas"]))
        with mock.patch.object(runtime, "peer_solver", fake_peer_solver), \
                mock.patch.object(runtime, "build_task", lambda **kwargs: "task"):
            await self.run_pilot(during_eval=during_eval)
        admitted = outcomes.count("finished")
        self.assertGreater(admitted, 0)
        self.assertEqual(outcomes.count("budget_admission_failed"), 20 - admitted)
        self.assertGreater(20 - admitted, 0)  # the budget stopped the rest
        run = read_run(ledger_file, "pilot-test")
        self.assertEqual((run["budget_usd"], run["requests"]), ("0.1", admitted))
        self.assertLessEqual(Decimal(run["committed_usd"]), Decimal("0.1"))
        self.assertEqual(read_run(ledger_file, "earlier-run")["uncertain_requests"], 1)  # untouched, still counted
        self.assertEqual(len(list_requests(ledger_file, scope="pilot")), 1)

    async def test_missing_max_connections_refuses_before_anything_runs(self):
        del self.config["max_connections"]
        with self.assertRaises(LaunchBlocked) as caught:
            await self.run_pilot()  # even when a (faked) preflight passes
        self.assertIn("max_connections", str(caught.exception))
        self.assertFalse(Path(self.config["run_dir"]).exists())

    async def test_image_change_between_preflight_and_launch_blocks(self):
        report = {"ready": True, "blockers": [], "docker": {"agent_image": {"tag": self.config["agent_image"],
                                                                            "id": "sha256:" + "f" * 64}}}
        with self.assertRaises(LaunchBlocked):
            await self.run_pilot(report)
        self.assertFalse(Path(self.config["run_dir"]).exists())


class InspectAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_mocked_bridge_preserves_settings_and_accounts(self):
        try:
            from inspect_ai.model import ModelOutput, ModelUsage, GenerateConfig, ChatMessageUser
        except ImportError:
            self.skipTest("Install optional pinned Inspect dependencies for adapter API checks")
        from research_swarm.inspect_adapter import budget_filter
        from research_swarm.provider import ProviderSettings
        from research_swarm.budget import BudgetLedger
        class FakeModel:
            async def generate(self, **kwargs):
                self.config=kwargs['config']
                output=ModelOutput.from_content(model='deepseek/deepseek-v4.1-flash',content='complete')
                output.usage=ModelUsage(input_tokens=5,output_tokens=2,total_tokens=7,total_cost=0.00001)
                return output
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/'budget.sqlite')
            ledger.start_run('run-1','pilot',100)
            model=FakeModel()
            events=[]
            intercept=budget_filter('agent-001',ledger,ProviderSettings(pricing_verified=True),StartBarrier(1),lambda *args:events.append(args))
            await intercept(model,[ChatMessageUser(content='hello')],[],None,GenerateConfig())
            self.assertEqual(model.config.reasoning_history,'all')
            self.assertEqual(model.config.max_retries,0)
            self.assertEqual(model.config.max_tokens,16384)
            self.assertFalse(model.config.extra_body['provider']['allow_fallbacks'])
            self.assertEqual(ledger.snapshot()['pilot']['unresolved_requests'],0)
            self.assertEqual(len(events),2)
    async def test_conference_tool_identity_bound(self):
        try:
            from research_swarm.inspect_adapter import conference_tools
            import inspect_ai
        except ImportError:
            self.skipTest("Install optional pinned Inspect dependencies")
        class Service:
            def action(self,agent_id,tool,args):return {'agent':agent_id,'tool':tool,'args':args}
        tools=conference_tools(Service(),'agent-003',[{'name':'post_intent','description':'Publish research intent.','inputSchema':{'type':'object','properties':{'message':{'type':'string'}},'required':['message']}}])
        result=json.loads(await tools[0](message='Working on algebra'))
        self.assertEqual(result['agent'],'agent-003')
        self.assertEqual(result['args'],{'message':'Working on algebra'})


class ValidationScriptTests(unittest.IsolatedAsyncioTestCase):
    """Offline checks of the Phase B scripts' shared helpers (no Docker)."""
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts"))
        import validate_runtime
        cls.vr = validate_runtime

    def test_evidence_naming_and_keys(self):
        stamp = self.vr.utc_stamp()
        self.assertRegex(stamp, r"^\d{8}T\d{6}Z$")
        checks = {"a": self.vr.check(True), "b": self.vr.check(False, required=False)}
        with tempfile.TemporaryDirectory() as folder:
            path, report = self.vr.write_evidence("runtime-e2e", "s/1", checks, {"agent": "sha256:x"}, stamp, folder)
            self.assertEqual(path.name, f"runtime-e2e-{stamp}.json")
            for key in ("schema", "created_at", "all_required_passed", "image_ids"):
                self.assertIn(key, json.loads(path.read_text()))
            self.assertTrue(report["all_required_passed"])  # optional checks do not gate

    async def test_scripted_model_routing(self):
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser
        served = []
        model = self.vr.ScriptedModel({"agent-001": [("one", self.vr.text("first")), ("two", self.vr.text("second"))]},
                                      on_step=lambda a, n: served.append(n))
        own = [ChatMessageSystem(content="You are agent-001, a researcher"), ChatMessageUser(content="go")]
        self.assertEqual((await model(own, [], None, None)).completion, "ok")  # unattributed
        token = self.vr.AGENT_CONTEXT.set("agent-001")
        try:
            self.assertEqual((await model(own, [], None, None)).completion, "first")
            nudge = own + [ChatMessageUser(content="Output token limit hit. Resume directly")]
            self.assertEqual((await model(nudge, [], None, None)).completion, "second")
            compact = own + [ChatMessageUser(content="Your task is to create a detailed summary of the conversation so far")]
            self.assertIn("<summary>", (await model(compact, [], None, None)).completion)
            nested = [ChatMessageSystem(content="You are Claude Code"), ChatMessageUser(content="nested")]
            self.assertEqual((await model(nested, [], None, None)).completion, "nested-ok-agent-001")
        finally:
            self.vr.AGENT_CONTEXT.reset(token)
        kinds = [x["kind"] for x in model.log["agent-001"]]
        self.assertEqual(kinds, ["step", "truncation_nudge", "step", "compaction_request", "foreign_session"])
        self.assertEqual(served, ["one", "two"])

    async def test_attributed_filter_keeps_state_and_context(self):
        seen = []

        async def inner(model, messages, tools, tool_choice, config):
            seen.append(self.vr.AGENT_CONTEXT.get())
            return "out"
        inner.state = {"requests": 0}
        arrivals = {}
        wrapped = self.vr.attributed("agent-002", inner, first_delay=0.01, arrivals=arrivals)
        self.assertIs(wrapped.state, inner.state)
        self.assertEqual(await wrapped(None, [], [], None, None), "out")
        self.assertEqual((seen, list(arrivals)), (["agent-002"], ["agent-002"]))
        self.assertIsNone(self.vr.AGENT_CONTEXT.get())

    def test_large_unicode_notebook_fixture(self):
        text_value = self.vr.large_notebook_text()
        self.assertEqual(len(text_value), self.vr.LARGE_NOTEBOOK_CHARS)
        self.assertEqual(self.vr.LARGE_NOTEBOOK_CHARS, 40_000)
        self.assertLessEqual(len(text_value.encode("utf-8")), self.vr.MAX_NOTEBOOK_BYTES)
        self.assertGreater(len(text_value.encode("utf-8")), 2 * len(text_value))  # Unicode-heavy
        self.assertGreater(self.vr.utf16_length(text_value), len(text_value))  # astral characters present
        command = self.vr.large_notebook_command()
        self.assertTrue(command.isascii())
        self.assertLess(len(command), 4096)
        with tempfile.TemporaryDirectory() as folder:  # the container-side writer reproduces the text exactly
            target = Path(folder) / "large.md"
            script = shlex.split(command.split(" && ")[1])[3]
            subprocess.run([sys.executable, "-I", "-c", script.replace(self.vr.LARGE_NOTEBOOK, str(target))],
                           check=True, timeout=60)
            self.assertEqual(target.read_text(encoding="utf-8"), text_value)

    def test_capacity_defaults_follow_the_pilot_config(self):
        import capacity_probe
        config = json.loads((ROOT / "configs" / "pilot.json").read_text())
        args = capacity_probe.parse_args([])
        validated = capacity_probe.validated_config(args)
        defaults = {"max_subprocesses": 64, "warm_up_memory": "6g", "warm_up_timeout_seconds": 900}
        for key in validated:
            self.assertEqual(validated[key], config.get(key, defaults.get(key)), key)
        self.assertEqual((args.image, args.lean_image), (config["agent_image"], config["lean_image"]))
        self.assertEqual(capacity_probe.parse_args(["--judge-max-concurrent", "2"]).judge_max_concurrent, 2)
        self.assertIsNone(capacity_probe.parse_args(["--judge-max-concurrent", "none"]).judge_max_concurrent)
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "c.json"
            other.write_text(json.dumps({"agent_count": 5, "judge_max_concurrent": 3}))
            args = capacity_probe.parse_args(["--config", str(other)])
            self.assertEqual((args.agents, args.judge_max_concurrent), (5, 3))

    def test_compaction_probe_steps_report_ascending_fabricated_usage(self):
        steps = self.vr.scenario_scripts("/tmp/run")["agent-001"]
        names = [name for name, _ in steps]
        probe = [self.vr.COMPACTION_PROBE_STEP.format(t) for t in self.vr.COMPACTION_PROBE_LEVELS]
        start = names.index(probe[0])
        self.assertEqual(names[start:start + len(probe) + 1], [*probe, "after_compaction"])
        self.assertEqual(list(self.vr.COMPACTION_PROBE_LEVELS), sorted(self.vr.COMPACTION_PROBE_LEVELS))
        self.assertEqual(self.vr.COMPACTION_PROBE_LEVELS, (120_000, 150_000, 170_000, 185_000, 195_000))
        makers = dict(steps)
        for tokens in self.vr.COMPACTION_PROBE_LEVELS:
            output = makers[self.vr.COMPACTION_PROBE_STEP.format(tokens)]([])
            self.assertEqual(output.usage.input_tokens, tokens)
            self.assertEqual(output.message.tool_calls[0].function, "Bash")
            self.assertEqual(output.metadata, {"openrouter_usage": {"cost": 0.0001}})  # never an estimate violation

    def test_compaction_bracket(self):
        step = lambda name: {"kind": "step", "step": name}  # noqa: E731
        probe = lambda tokens: step(self.vr.COMPACTION_PROBE_STEP.format(tokens))  # noqa: E731
        compaction = {"kind": "compaction_request"}
        log = [step("get_bulletin_board"), compaction,  # not after a probe step: ignored
               probe(120_000), probe(150_000), probe(170_000), compaction, probe(185_000), compaction,
               probe(195_000), {"kind": "foreign_session"}, compaction, step("after_compaction")]
        bracket = self.vr.compaction_bracket(log)
        self.assertEqual(bracket["compaction_threshold_tokens"], 170_000)
        self.assertEqual(bracket["highest_level_without_compaction"], 150_000)
        self.assertEqual(bracket["compacted_after_levels"], [170_000, 185_000, 195_000])
        self.assertTrue(bracket["complete"])
        none = self.vr.compaction_bracket([probe(t) for t in self.vr.COMPACTION_PROBE_LEVELS])
        self.assertEqual((none["compaction_threshold_tokens"], none["highest_level_without_compaction"]),
                         (None, 195_000))
        # A nudge or another step between the probe and the compaction breaks attribution.
        self.assertIsNone(self.vr.compaction_bracket([probe(120_000), {"kind": "truncation_nudge"}, compaction])
                          ["compaction_threshold_tokens"])
        partial = self.vr.compaction_bracket([probe(120_000), compaction])
        self.assertEqual((partial["compaction_threshold_tokens"], partial["complete"]), (120_000, False))
        self.assertIsNone(partial["highest_level_without_compaction"])

    def test_interlock_check_and_flag(self):
        clear = {"checked_at": "t", "blockers": [], "containers": [], "forced": False}
        self.assertTrue(self.vr.interlock_check(clear)["passed"])
        self.assertTrue(self.vr.interlock_check(clear)["required"])
        forced = dict(clear, blockers=["1 running comparator container(s)"], forced=True)
        self.assertFalse(self.vr.interlock_check(forced)["passed"])
        self.assertFalse(self.vr.interlock_check(None)["passed"])
        import capacity_probe
        import isolation_probes
        for module in (self.vr, isolation_probes, capacity_probe):
            self.assertFalse(module.parse_args([]).force_while_live, module.__name__)
            self.assertTrue(module.parse_args(["--force-while-live"]).force_while_live, module.__name__)

    def test_scripts_refuse_before_starting_anything_while_containers_run(self):
        """S1: each Phase B script's main() returns 2 without starting work or writing evidence."""
        import capacity_probe
        import isolation_probes
        busy = {"checked_at": "t", "blockers": ["1 running comparator container(s) with label gdm-evaluator=1"],
                "containers": [], "judge_containers": []}
        never = mock.Mock(side_effect=AssertionError("must not start"))
        for module, starts in ((self.vr, "run"), (isolation_probes, "run"), (capacity_probe, "docker_info")):
            with tempfile.TemporaryDirectory() as folder, \
                    mock.patch.object(sys, "argv", [module.__name__, "--output-dir", folder]), \
                    mock.patch.object(runtime, "launch_interlock", lambda image, docker="docker": busy), \
                    mock.patch.object(module, starts, never), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(module.main(), 2, module.__name__)
                self.assertEqual(list(Path(folder).iterdir()), [])
            self.assertIn("Refusing to start", err.getvalue())
            self.assertIn("gdm-evaluator=1", err.getvalue())

    def test_capacity_probe_uses_an_explicit_connection_limit(self):
        import capacity_probe
        config = json.loads((ROOT / "configs" / "pilot.json").read_text())
        args = capacity_probe.parse_args([])
        self.assertEqual(capacity_probe.mock_max_connections(args), config.get("max_connections", args.agents))
        self.assertEqual(capacity_probe.mock_max_connections(capacity_probe.parse_args(["--max-connections", "3"])), 3)
        with tempfile.TemporaryDirectory() as folder:
            other = Path(folder) / "c.json"
            other.write_text(json.dumps({"agent_count": 5}))
            self.assertEqual(capacity_probe.mock_max_connections(capacity_probe.parse_args(["--config", str(other)])), 5)

    def test_capacity_config_matches_launch_gate_keys(self):
        import capacity_probe
        args = capacity_probe.parse_args(["--judge-max-concurrent", "2"])
        try:
            from research_swarm.launch_gates import CAPACITY_KEYS
        except ImportError:
            self.skipTest("launch_gates not available")
        self.assertEqual(set(capacity_probe.validated_config(args)), set(CAPACITY_KEYS))
        steps = capacity_probe.peer_scripts(["agent-001"], ["DedekindNumber_M'_three"], {"at": None}, 1)["agent-001"]
        command = steps[0][1]([]).message.tool_calls[0].arguments["command"]
        self.assertIn('cp "problems/DedekindNumber_M\'_three.lean"', command)
        expired = capacity_probe.peer_scripts(["agent-001"], ["t"], {"at": time.monotonic() - 1}, 1)["agent-001"]
        self.assertEqual(expired[0][1]([]).completion, "Capacity script complete.")


def _fake_sandbox_class():
    """Register an in-process sandbox that maps container paths into a temp dir."""
    from inspect_ai.util import ExecResult, SandboxEnvironment, sandboxenv

    @sandboxenv(name="gdm_offline_fake")
    class OfflineSandbox(SandboxEnvironment):
        root = None
        prefixes = ("/workspace", "/tmp/w", "/home/researcher")

        def __init__(self, base):
            super().__init__()
            self.base = str(base)

        def _map(self, text):
            for prefix in self.prefixes:
                text = text.replace(prefix, self.base + prefix)
            return text

        @classmethod
        async def sample_init(cls, task_name, config, metadata):
            base = Path(tempfile.mkdtemp(dir=cls.root)).resolve()  # realpath-stable (macOS /private)
            for folder in ("workspace", "tmp/w/memory/wiki", "home/researcher/.claude/projects/p"):
                (base / folder).mkdir(parents=True, exist_ok=True)
            (base / "home/researcher/.claude/projects/p/session.jsonl").write_text('{"turn": 1}\n')
            return {"default": cls(base)}

        @classmethod
        async def sample_cleanup(cls, task_name, config, environments, interrupted):
            pass

        async def exec(self, cmd, input=None, cwd=None, env=None, user=None, timeout=None, timeout_retry=True,
                       concurrency=True):
            cmd = [sys.executable if c == "python3" else self._map(c) for c in cmd]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return ExecResult(success=proc.returncode == 0, returncode=proc.returncode,
                              stdout=proc.stdout.replace(self.base, ""), stderr=proc.stderr)

        async def write_file(self, file, contents):
            path = Path(self._map(file))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())

        async def read_file(self, file, text=True):
            data = Path(self._map(file)).read_bytes()
            return data.decode() if text else data
    return OfflineSandbox


def _fake_claude_code(prompt, tools, filter, env=None, cwd="/workspace", *, system_prompt_mode="append",
                      auto_memory=True):
    """Stands in for the CLI: drives the production filter and bridged tools, with
    Claude Code's observed output-limit recovery (3 nudges, then the turn ends)."""
    from inspect_ai.model import ChatMessageSystem, ChatMessageTool, ChatMessageUser, GenerateConfig, get_model
    from inspect_ai.tool import ToolInfo, ToolParams
    from inspect_ai.tool._tool_util import tool_to_tool_info
    from inspect_ai.util import sandbox
    infos, by_name = [], {}
    for tool in tools:
        info = tool_to_tool_info(tool)
        by_name[info.name] = tool
        infos.append(info.model_copy(update={"name": "mcp__conference__" + info.name}))
    infos.append(ToolInfo(name="Bash", description="Run a command", parameters=ToolParams()))

    async def generate(model, messages):
        # As Inspect's bridge service does: a LimitExceededError from the filter
        # (budget or run-stop refusal) ends the sample through its task group.
        import anyio
        from inspect_ai.log._samples import sample_active
        from inspect_ai.util import LimitExceededError
        try:
            return await filter(model, messages, infos, None, GenerateConfig())
        except LimitExceededError as ex:
            sample_active().limit_exceeded(ex)
            await anyio.sleep_forever()

    async def execute(state):
        model, truncations = get_model(), 0
        messages = [ChatMessageSystem(content="You are Claude Code (offline stand-in)."),
                    ChatMessageSystem(content=prompt), *state.messages]
        for _ in range(60):
            output = await generate(model, messages)
            messages.append(output.message)
            if output.stop_reason == "max_tokens":
                truncations += 1
                if truncations > 3:
                    break
                messages.append(ChatMessageUser(content="Output token limit hit. Resume directly"))
                continue
            truncations = 0
            if not output.message.tool_calls:
                break
            for call in output.message.tool_calls:
                if call.function.startswith("mcp__conference__"):
                    result = await by_name[call.function.split("__")[-1]](**call.arguments)
                elif call.function == "Bash":
                    command = call.arguments["command"]
                    if "lake env lean" in command:
                        await sandbox().exec(["bash", "-c", command.split(" && start")[0]])
                        result = "error: Provide a complete proof.\nLEAN_EXIT=1\nELAPSED=1"
                    elif "sleep 150" in command:
                        result = "Command timed out after 2m"
                    elif "truncated.txt" in command:
                        result = "ABSENT"
                    else:
                        # The container's python3 is the host interpreter here.
                        command = command.replace("python3 -I -c", shlex.quote(sys.executable) + " -I -c")
                        done = await sandbox().exec(["bash", "-c", command])
                        result = done.stdout + done.stderr
                else:
                    result = f"Error: No such tool available: {call.function}"
                messages.append(ChatMessageTool(content=str(result), tool_call_id=call.id, function=call.function))
        state.messages = messages
        return state
    return execute


class OfflineRehearsalTests(unittest.TestCase):
    """validate_runtime's scenario through real Inspect machinery, the production
    solver/filter/barrier/snapshotter/conference, with a fake sandbox, CLI stand-in
    and judge compiler (no Docker, no network)."""
    def test_validate_runtime_scenario_with_fakes(self):
        from research_swarm import inspect_adapter
        inspect_adapter.disable_inspect_dotenv()  # X1: before any Inspect evaluation
        from research_swarm.lean import CompileResult
        sys.path.insert(0, str(ROOT / "scripts"))
        import validate_runtime as vr
        sandbox_cls = _fake_sandbox_class()

        class FakeCompiler:
            image, timeout, memory, cpus, max_concurrent = "fake", 120, "2g", 1, 1

            def compile(self, source):
                return CompileResult("error", 1, "Submission.lean:10:2: error: Provide a complete proof.", "", 0,
                                     elapsed_seconds=0.1)
        original_build_task = runtime.build_task

        def build_task(**kwargs):
            from inspect_ai.util import SandboxEnvironmentSpec
            task = original_build_task(**kwargs)
            task.sandbox = SandboxEnvironmentSpec("gdm_offline_fake")
            return task
        from inspect_ai.util._display import display_type, init_display_type
        previous_display = display_type()
        init_display_type("none")
        self.addCleanup(init_display_type, previous_display)
        with tempfile.TemporaryDirectory() as folder:
            sandbox_cls.root = folder
            args = types.SimpleNamespace(image="fake:agent", lean_image="fake:lean", agent_memory="1g", judge_memory="2g",
                                         judge_max_concurrent=1, max_seconds=300, runs_dir=folder)
            warm_ups = []

            def fake_warm_up(image, record, **options):
                warm_ups.append((image, options))
                result = {"ok": True, "status": "success", "image": image, **options}
                record("lean_warm_up", None, result)
                return result
            pinned = {"agent": {"tag": "fake:agent", "id": "sha256:" + "a" * 64},
                      "lean": {"tag": "fake:lean", "id": "sha256:" + "b" * 64}}
            with mock.patch.object(inspect_adapter, "build_claude_agent", _fake_claude_code), \
                    mock.patch.object(runtime, "make_lean_compiler", lambda config, image=None: FakeCompiler()), \
                    mock.patch.object(runtime, "pin_images", lambda config: pinned), \
                    mock.patch.object(runtime, "warm_up_lean", fake_warm_up), \
                    mock.patch.object(runtime, "build_task", build_task), \
                    mock.patch.object(vr, "LATE_AGENT_DELAY", 1.0):
                result = asyncio.run(vr.run(args, vr.utc_stamp()))
            checks, _ = vr.build_checks(result)
            self.assertEqual(warm_ups, [("sha256:" + "b" * 64, {"memory": "6g", "timeout": 900})])
        failed = {k: v for k, v in checks.items() if v["required"] and not v["passed"]}
        self.assertEqual(failed, {})
        for name in ("publish_notebook_relative_path", "read_library_file_large_unicode_full_range",
                     "compose_uses_pinned_image_id", "cross_agent_direct_message"):
            self.assertTrue(checks[name]["passed"] and checks[name]["required"], name)
        large = checks["read_library_file_large_unicode_full_range"]
        self.assertEqual(large["received_chars"], vr.LARGE_NOTEBOOK_CHARS)
        # Every compaction probe level was served through the production filter; the stand-in
        # CLI never compacts, so the informational bracket stays open (no threshold).
        bracket = checks["compaction_threshold_bracketed"]
        self.assertEqual(bracket["levels_served"], list(vr.COMPACTION_PROBE_LEVELS))
        self.assertIsNone(bracket["compaction_threshold_tokens"])
        self.assertFalse(bracket["required"])
        self.assertTrue(checks["lean_warm_up"]["passed"])
        exits = {e["agent_id"]: e["payload"]["status"] for e in result["runtime_events"] if e["kind"] == "agent_exit"}
        self.assertEqual(exits, {"agent-001": "harness_completed", "agent-002": "harness_completed",
                                 "agent-003": "harness_completed_after_truncation"})


class PeerStopReservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_solver_stop_during_shielded_reservation_never_dispatches(self):
        """Exercise cancellation by the real AnyIO solver while reserve is shielded."""
        try:
            import anyio
            from inspect_ai.model import ChatMessageUser, GenerateConfig
        except ImportError:
            self.skipTest("pinned Inspect packages not installed (.venv)")
        from research_swarm import inspect_adapter
        from research_swarm.budget import BudgetLedger
        from research_swarm.provider import ProviderSettings

        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            events = []
            record = lambda kind, agent, payload: events.append((kind, agent, payload))
            barrier = StartBarrier(1, record)
            clock = RunClock(barrier)
            ledger = BudgetLedger(folder / "budget.sqlite")
            ledger.start_run("validation-run", "validation", 100)
            entered, release = threading.Event(), threading.Event()
            stopping = asyncio.Event()
            reserve = ledger.reserve

            def blocked_reserve(*args):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release reservation")
                return reserve(*args)

            ledger.reserve = blocked_reserve

            class Model:
                async def generate(self, **kwargs):
                    raise AssertionError("must not enter generation after stop")

            def fake_agent(prompt, tools, filter, **kwargs):
                async def agent(state):
                    await filter(Model(), state.messages, [], None, GenerateConfig())
                    return state
                return agent

            async def no_snapshots(*args, **kwargs):
                await anyio.sleep_forever()

            async def no_final_snapshot(*args, **kwargs):
                return 0

            def observed_record(kind, agent, payload):
                record(kind, agent, payload)
                if kind == "agent_stopping":
                    stopping.set()

            async def stop_during_reservation():
                async def wait_entered():
                    while not entered.is_set():
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait_entered(), 5)
                clock.request_stop("max_run_seconds", observed_record)
                await asyncio.wait_for(stopping.wait(), 5)
                # watch_stop has zero in-flight requests and cancels the group
                # before this coroutine resumes and releases the reserve worker.
                release.set()

            intercept = inspect_adapter.budget_filter(
                "agent-001", ledger, ProviderSettings(pricing_verified=True), barrier,
                observed_record, "validation", stop=clock.stop)
            state = types.SimpleNamespace(sample_id="agent-001", messages=[ChatMessageUser(content="hi")], output=None)
            with mock.patch("inspect_ai.util.sandbox", return_value=object()), \
                    mock.patch.object(inspect_adapter, "build_claude_agent", fake_agent), \
                    mock.patch.object(Snapshotter, "run", no_snapshots), \
                    mock.patch.object(Snapshotter, "poll", no_final_snapshot):
                solver = runtime.peer_solver(
                    conference=types.SimpleNamespace(), definitions=[], personas={"agent-001": "algebra"},
                    prompt_for=lambda *a: "prompt", filter_for=lambda a: intercept, run_dir=folder,
                    record=observed_record, clock=clock)
                try:
                    await asyncio.wait_for(asyncio.gather(solver(state, None), stop_during_reservation()), 10)
                finally:
                    release.set()
            snapshot = ledger.snapshot()["validation"]
            self.assertEqual((snapshot["unresolved_requests"], Decimal(snapshot["held_usd"])), (0, 0))
            kinds = [kind for kind, _, _ in events]
            self.assertNotIn("inference_admitted", kinds)
            self.assertNotIn("inference_uncertain", kinds)
            self.assertEqual(intercept.state["last_denial"], "run_stopped")
            stopped = next(payload for kind, _, payload in events if kind == "agent_stopping")
            self.assertEqual(stopped["in_flight"], 0)
            exit_record = next(payload for kind, _, payload in events if kind == "agent_exit")
            self.assertEqual(exit_record["status"], "stopped:max_run_seconds")


class PeerExitLabelTests(unittest.TestCase):
    """RT-3 through the production solver, budget filter and a real Inspect eval (fake sandbox/CLI)."""
    def run_peer(self, outputs, budget=100):
        try:
            from inspect_ai import eval_async
            from inspect_ai.model import get_model
            from inspect_ai.util import SandboxEnvironmentSpec
            from inspect_ai.util._display import display_type, init_display_type
        except ImportError:
            self.skipTest("pinned Inspect packages not installed (.venv)")
        from research_swarm import inspect_adapter
        from research_swarm.budget import BudgetLedger
        from research_swarm.provider import ProviderSettings
        inspect_adapter.disable_inspect_dotenv()  # X1: before any Inspect evaluation
        previous_display = display_type()
        init_display_type("none")
        self.addCleanup(init_display_type, previous_display)
        sandbox_cls = _fake_sandbox_class()
        folder = Path(tempfile.mkdtemp()).resolve()
        sandbox_cls.root = str(folder)
        events = []
        record = lambda kind, agent, payload: events.append((kind, agent, payload))  # noqa: E731
        ledger = BudgetLedger(folder / "b.sqlite")
        ledger.start_run("peer-run", "validation", budget)
        barrier = StartBarrier(1, record)
        clock = RunClock(barrier)

        async def answer(input, tools, tool_choice, config):
            return outputs()
        with mock.patch.object(inspect_adapter, "build_claude_agent", _fake_claude_code):
            solver = runtime.peer_solver(
                conference=types.SimpleNamespace(action=lambda *a: {"ok": True}), definitions=[],
                personas={"agent-001": "algebra"}, prompt_for=lambda a, p: "prompt",
                filter_for=lambda a: inspect_adapter.budget_filter(a, ledger, ProviderSettings(pricing_verified=True),
                                                                   barrier, record, "validation", None, clock.stop),
                run_dir=folder, record=record, clock=clock)
            task = runtime.build_task(agent_ids=["agent-001"], files={}, solver=solver, compose="unused.yaml",
                                      metadata={}, initial_message="Begin.")
            task.sandbox = SandboxEnvironmentSpec("gdm_offline_fake")
            asyncio.run(eval_async(task, model=get_model("mockllm/model", custom_outputs=answer), max_samples=1,
                                   score=False, log_dir=str(folder / "inspect"), fail_on_error=False))
        return next(payload for kind, _, payload in events if kind == "agent_exit")

    def test_reasoning_only_final_response_is_not_a_voluntary_exit(self):
        from inspect_ai.model import ContentReasoning, ModelOutput
        output = ModelOutput.from_content(model="m", content="")
        output.message.content = [ContentReasoning(reasoning="thinking only")]
        exit_record = self.run_peer(lambda: output)
        self.assertEqual((exit_record["status"], exit_record["harness_status"], exit_record["final_output"]),
                         ("harness_completed_empty_reply", "harness_completed", "reasoning_only"))

    def test_visible_final_reply_is_a_voluntary_exit(self):
        from inspect_ai.model import ModelOutput
        exit_record = self.run_peer(lambda: ModelOutput.from_content(model="m", content="I am done."))
        self.assertEqual((exit_record["status"], exit_record["final_output"]), ("harness_completed", "text"))

    def test_budget_denial_is_labelled_budget_exhausted(self):
        from inspect_ai.model import ModelOutput
        exit_record = self.run_peer(lambda: ModelOutput.from_content(model="m", content="x"),
                                    budget=0.000001)
        self.assertEqual((exit_record["status"], exit_record["harness_status"]), ("budget_exhausted", "interrupted"))
        self.assertEqual(exit_record["inference"]["last_denial"], "budget_admission_failed")


if __name__ == "__main__":
    unittest.main()
