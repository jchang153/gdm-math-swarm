"""Resume bundle (research_swarm.resume_bundle; docs/runtime.md "Resume bundle"), offline with a
fake container that maps container paths into a temporary folder."""
import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import research_swarm.runtime as runtime
from research_swarm import resume_bundle
from research_swarm.resume_bundle import ResumeBundler
from research_swarm.runtime import RunClock, Snapshotter, StartBarrier

ROOT = Path(__file__).resolve().parents[1]
SESSION = "0d6c9a3e-2f43-4c6b-9a5e-1c2d3e4f5a6b"
FAKE_KEY = "sk-or-v1-" + "a1b2c3d4" * 6   # credential-shaped test value, never a real key
PLACEHOLDER = "sk-ant-api03-DOq5tyLPrk9M4hPE"  # Inspect SWE's public bridge placeholder


class FakeContainer:
    """Sandbox stand-in: container paths live under ``base``; exec runs on the host."""
    def __init__(self, base):
        self.base = str(base)
        self.execs = []

    def map(self, path):
        return self.base + path if path.startswith("/") else path

    async def exec(self, cmd, timeout=None, **kwargs):
        self.execs.append(cmd)
        cmd = list(cmd)
        try:
            spec = json.loads(cmd[-1])
        except ValueError:
            spec = None
        if isinstance(spec, dict) and "roots" in spec:
            spec.update(roots=[self.map(r) for r in spec["roots"]], out=self.map(spec["out"]), strip=self.base)
            cmd[-1] = json.dumps(spec)
        elif cmd[-1].startswith("/"):
            cmd[-1] = self.map(cmd[-1])
        cmd = [sys.executable if c == "python3" else c for c in cmd]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return types.SimpleNamespace(success=proc.returncode == 0, stdout=proc.stdout.replace(self.base, ""),
                                     stderr=proc.stderr)

    async def read_file(self, path, text=True):
        data = Path(self.map(path)).read_bytes()
        return data.decode() if text else data


def build_container(base, neutral=True):
    base = Path(base)
    home = base / "home/researcher"
    config = home / (".agent" if neutral else ".claude")
    project = config / "projects/-workspace"
    for folder in ("workspace/work", "workspace/.lake", "tmp/w/memory/wiki", str(project.relative_to(base)),
                   str((project / SESSION / "subagents").relative_to(base)), "opt/mathlib"):
        (base / folder).mkdir(parents=True, exist_ok=True)
    (base / "opt/mathlib/Big.olean").write_bytes(b"x" * 10_000)
    os.symlink(str(base / "opt/mathlib"), str(base / "workspace/.lake/packages"))
    (base / "workspace/RESEARCH.md").write_text("# Notes\nTried induction on n.\n")
    (base / "workspace/work/attempt.lean").write_text("theorem t : True := trivial\n")
    (base / "tmp/w/memory/wiki/lemma.md").write_text("useful lemma\n")
    (base / "tmp/scratch.log").write_text("lake build output\n")
    rows = [
        {"type": "user", "sessionId": SESSION, "timestamp": "2026-10-22T06:00:00Z",
         "message": {"role": "user", "content": "Begin."}},
        {"type": "assistant", "sessionId": SESSION, "timestamp": "2026-10-22T06:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "plan", "signature": "research-swarm-reasoning-v1:abc:def"},
             {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "sessionId": SESSION, "timestamp": "2026-10-22T06:00:06Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}},
        {"type": "assistant", "sessionId": SESSION, "timestamp": "2026-10-22T06:00:09Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {"command": "lake env lean w.lean"}}]}},
    ]
    (project / f"{SESSION}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (project / SESSION / "subagents" / "agent-x.jsonl").write_text(json.dumps(rows[0]) + "\n")
    (home / ".claude.json").write_text(json.dumps({"apiKeyHelper": f"echo {PLACEHOLDER}"}))
    (base / "workspace/leaked.txt").write_text(f"OPENROUTER={FAKE_KEY}\n")
    return base


def members(archive):
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {i.name: (i, tar.extractfile(i).read() if i.isfile() else None) for i in tar}


def make_clock(started=True):
    barrier = StartBarrier(1)
    if started:
        barrier.started_at, barrier.started_wall = runtime.time.monotonic(), runtime.time.time()
        barrier.event.set()
    return RunClock(barrier)


class ResumeBundleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp()).resolve()
        self.base = build_container(self.folder / "container")
        self.run = self.folder / "runs/run-x"
        self.events = []

    def record(self, kind, agent, payload):
        self.events.append((kind, agent, payload))

    def bundler(self, **kwargs):
        return ResumeBundler(self.run, self.record, neutral=True, clock=make_clock(),
                             run_info={"agent_ids": ["agent-001", "agent-002"], "ledger_run_id": "run-x",
                                       "prompt_variant": "collab_max_pressure_no_rule6"}, **kwargs)

    async def test_final_bundle_holds_sessions_workspace_wiki_and_manifest(self):
        bundler = self.bundler()
        entry = await bundler.save_agent(FakeContainer(self.base), "agent-001",
                                         agent={"persona": "algebra", "role": "solver",
                                                "reasoning_transport_key_id": "0123456789abcdef"})
        self.assertEqual(entry["status"], "saved", entry.get("error"))
        archive = (self.run / "resume/agents/agent-001/state.tar.gz").read_bytes()
        files = members(archive)
        # workspace, RESEARCH.md, the private wiki, /tmp and the session transcripts are all there
        for name in ("workspace/RESEARCH.md", "workspace/work/attempt.lean", "tmp/w/memory/wiki/lemma.md",
                     "tmp/scratch.log", f"home/researcher/.agent/projects/-workspace/{SESSION}.jsonl",
                     f"home/researcher/.agent/projects/-workspace/{SESSION}/subagents/agent-x.jsonl",
                     "home/researcher/.claude.json"):
            self.assertIn(name, files)
        # symlinks are stored as links: the dependency tree is never copied
        link = files["workspace/.lake/packages"][0]
        self.assertTrue(link.issym())
        self.assertFalse(any("Big.olean" in n for n in files))
        # credential screen: the credential-shaped member is withheld, the public placeholder kept
        self.assertNotIn("workspace/leaked.txt", files)
        self.assertEqual(entry["archive"]["credential_withheld"], ["/workspace/leaked.txt"])
        self.assertNotIn(FAKE_KEY.encode(), archive)
        self.assertIn(("resume_bundle_credential_withheld", "agent-001",
                       {"label": "final", "paths": ["/workspace/leaked.txt"]}), self.events)
        # session ids, sidechains and the open tool call at stop time
        self.assertEqual(entry["session_ids"], [SESSION])
        self.assertEqual(len(entry["sidechain_files"]), 1)
        self.assertEqual([c["id"] for c in entry["open_tool_calls"][SESSION]], ["toolu_2"])
        self.assertEqual(entry["wiki_files"], ["/tmp/w/memory/wiki/lemma.md"])
        self.assertGreater(entry["research_md_bytes"], 0)
        # nothing is left behind in the container's /tmp
        self.assertEqual([p.name for p in (self.base / "tmp").iterdir() if p.name.startswith(".gdm-resume")], [])
        manifest = json.loads((self.run / "resume/manifest.json").read_text())
        self.assertEqual(manifest["schema"], resume_bundle.SCHEMA)
        self.assertEqual(manifest["ledger_run_id"], "run-x")
        self.assertEqual(manifest["missing_agents"], ["agent-002"])
        self.assertEqual(manifest["agents"]["agent-001"]["persona"], "algebra")
        self.assertEqual(manifest["agents"]["agent-001"]["reasoning_transport_key_id"], "0123456789abcdef")
        self.assertEqual(manifest["total_archive_bytes"], len(archive))
        self.assertIsNotNone(manifest["clock"]["common_start_utc"])

    async def test_non_neutral_config_dir(self):
        base = build_container(self.folder / "plain", neutral=False)
        bundler = ResumeBundler(self.run, self.record, neutral=False, clock=make_clock())
        entry = await bundler.save_agent(FakeContainer(base), "agent-001")
        self.assertEqual(entry["config_dir"], "/home/researcher/.claude")
        self.assertEqual(entry["session_ids"], [SESSION])

    async def test_chunked_transfer_is_lossless(self):
        data, info = await resume_bundle.capture_agent(FakeContainer(self.base), chunk=700)
        self.assertGreater(len(info["parts"]), 1)
        self.assertEqual(len(data), info["bytes"])
        self.assertIn("workspace/RESEARCH.md", members(data))

    async def test_size_limits_list_skipped_members(self):
        (self.base / "workspace/huge.bin").write_bytes(b"0" * 5000)
        data, info = await resume_bundle.capture_agent(FakeContainer(self.base), max_member=4000)
        self.assertNotIn("workspace/huge.bin", members(data))
        self.assertIn({"path": "/workspace/huge.bin", "reason": "member_size_limit", "bytes": 5000}, info["skipped"])

    async def test_failure_is_recorded_not_raised(self):
        class Broken(FakeContainer):
            async def exec(self, cmd, timeout=None, **kwargs):
                return types.SimpleNamespace(success=False, stdout="", stderr="no python")
        entry = await self.bundler().save_agent(Broken(self.base), "agent-002")
        self.assertEqual(entry["status"], "error")
        self.assertEqual(self.events[-1][0], "resume_bundle_error")
        manifest = json.loads((self.run / "resume/manifest.json").read_text())
        self.assertEqual(manifest["agents"]["agent-002"]["status"], "error")

    async def test_checkpoints_write_their_own_folders_until_stop(self):
        bundler = self.bundler(checkpoint_minutes=5)
        clock = bundler.clock
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                clock.request_stop("max_run_seconds", self.record)

        task = asyncio.ensure_future(bundler.run_checkpoints(FakeContainer(self.base), "agent-001",
                                                            lambda: {"persona": "algebra"}, sleep=fake_sleep))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if clock.stop.is_set():
                break
        task.cancel()
        self.assertTrue((self.run / "resume/checkpoint-0005/agents/agent-001/state.tar.gz").is_file())
        self.assertTrue((self.run / "resume/checkpoint-0010/manifest.json").is_file())
        self.assertFalse((self.run / "resume/checkpoint-0015").exists())
        self.assertFalse((self.run / "resume/manifest.json").exists())  # the final bundle is separate

    def test_checkpoint_setting(self):
        self.assertIsNone(resume_bundle.checkpoint_minutes({}))
        self.assertIsNone(resume_bundle.checkpoint_minutes({"resume_checkpoint_minutes": 0}))
        self.assertEqual(resume_bundle.checkpoint_minutes({"resume_checkpoint_minutes": 15}), 15.0)
        with self.assertRaises(ValueError):
            resume_bundle.checkpoint_minutes({"resume_checkpoint_minutes": -1})

    def test_screen_is_the_publication_screen(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import publish_hf_library
        self.assertEqual(resume_bundle._SECRET_PATTERN.pattern, publish_hf_library._SECRET_PATTERN.pattern)
        self.assertEqual(resume_bundle.PUBLIC_PLACEHOLDERS, publish_hf_library.PUBLIC_PLACEHOLDERS)
        self.assertTrue(resume_bundle.credential_like(FAKE_KEY.encode()))
        self.assertFalse(resume_bundle.credential_like(PLACEHOLDER.encode()))

    def test_transport_key_id_is_not_the_key(self):
        from research_swarm import reasoning_bridge
        self.assertIsNone(reasoning_bridge.agent_transport_key_id())
        with reasoning_bridge.agent_transport_scope():
            key = reasoning_bridge._AGENT_KEY.get()
            first = reasoning_bridge.agent_transport_key_id()
            self.assertEqual(first, reasoning_bridge.agent_transport_key_id())
            self.assertEqual(len(first), 16)
            self.assertNotIn(first, key.hex())
        with reasoning_bridge.agent_transport_scope():
            self.assertNotEqual(first, reasoning_bridge.agent_transport_key_id())


class PublicationExclusionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "scripts"))

    def test_prepare_never_copies_the_resume_bundle(self):
        import prepare_hf_library
        for flag in (False, True):
            self.assertTrue(prepare_hf_library.run_excluded("resume/manifest.json", flag))
            self.assertTrue(prepare_hf_library.run_excluded("resume/agents/agent-001/state.tar.gz", flag))
        self.assertFalse(prepare_hf_library.run_excluded("results.json"))

    def test_publish_refuses_a_bundle_with_resume_state(self):
        import publish_hf_library
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "run-x" / "resume" / "manifest.json"
            target.parent.mkdir(parents=True)
            target.write_text("{}")
            with self.assertRaisesRegex(ValueError, "Resume bundle"):
                publish_hf_library.load_bundle(folder)


class PeerSolverResumeTests(unittest.IsolatedAsyncioTestCase):
    """The production solver saves the bundle at stop; prompts, tools and messages are unchanged."""
    async def run_solver(self, folder, resume):
        try:
            import anyio
            from inspect_ai.model import ChatMessageUser
        except ImportError:
            self.skipTest("pinned Inspect packages not installed (.venv)")
        from research_swarm import inspect_adapter
        from research_swarm.tools import tool_definitions
        events, seen = [], {}
        record = lambda kind, agent, payload: events.append((kind, agent, payload))  # noqa: E731
        clock = resume.clock if resume is not None else make_clock()
        container = FakeContainer(build_container(Path(folder) / "container"))

        def fake_agent(prompt, tools, filter, **kwargs):
            seen["prompt"], seen["kwargs"] = prompt, kwargs
            seen["tools"] = [getattr(t, "__name__", repr(t)) for t in tools]

            async def agent(state):
                seen["messages"] = [m.text for m in state.messages]
                clock.request_stop("max_run_seconds", record)
                await anyio.sleep_forever()
            return agent

        def fake_tools(conference, agent_id, definitions, **kwargs):
            seen["definitions"] = json.dumps(definitions, sort_keys=True)
            return []

        async def no_snapshots(*args, **kwargs):
            await anyio.sleep_forever()

        async def no_final_snapshot(*args, **kwargs):
            return 0
        intercept = types.SimpleNamespace(state={"requests": 3})
        state = types.SimpleNamespace(sample_id="agent-001", messages=[ChatMessageUser(content="Begin.")], output=None)
        with mock.patch("inspect_ai.util.sandbox", return_value=container), \
                mock.patch.object(inspect_adapter, "build_claude_agent", fake_agent), \
                mock.patch.object(inspect_adapter, "conference_tools", fake_tools), \
                mock.patch.object(inspect_adapter, "prepare_neutral_home", mock.AsyncMock(return_value=True)), \
                mock.patch.object(Snapshotter, "run", no_snapshots), \
                mock.patch.object(Snapshotter, "poll", no_final_snapshot):
            solver = runtime.peer_solver(
                conference=types.SimpleNamespace(), definitions=tool_definitions(True),
                personas={"agent-001": "algebra"}, prompt_for=lambda *a: "the agent prompt",
                filter_for=lambda a: intercept, run_dir=Path(folder) / "run", record=record, clock=clock,
                neutral=True, resume=resume)
            await asyncio.wait_for(solver(state, None), 30)
        return seen, events

    async def test_bundle_saved_and_agent_inputs_byte_identical(self):
        with tempfile.TemporaryDirectory() as plain, tempfile.TemporaryDirectory() as bundled:
            without, _ = await self.run_solver(plain, None)
            bundler = ResumeBundler(Path(bundled) / "run", lambda *a: None, neutral=True, clock=make_clock(),
                                    run_info={"agent_ids": ["agent-001"]})
            bundler.record = lambda *a: None
            with_bundle, events = await self.run_solver(bundled, bundler)
            self.assertEqual(without, with_bundle)
            self.assertEqual((Path(plain) / "run/prompts/agent-001.txt").read_bytes(),
                             (Path(bundled) / "run/prompts/agent-001.txt").read_bytes())
            manifest = json.loads((Path(bundled) / "run/resume/manifest.json").read_text())
            entry = manifest["agents"]["agent-001"]
            self.assertEqual(entry["status"], "saved", entry.get("error"))
            self.assertEqual(entry["session_ids"], [SESSION])
            self.assertEqual(entry["persona"], "algebra")
            self.assertEqual(len(entry["reasoning_transport_key_id"]), 16)
            self.assertEqual(entry["requests"], 3)
            self.assertEqual(manifest["missing_agents"], [])
            kinds = [k for k, _, _ in events]
            # the bundle is written before the agent's exit record
            self.assertLess(kinds.index("agent_stopping"), kinds.index("agent_exit"))
            self.assertFalse(Path(plain, "run", "resume").exists())


if __name__ == "__main__":
    unittest.main()
