"""Workspace setup as one archive (runtime.build_task / write_workspace_files; HANDOFF item 57)."""
import asyncio
import base64
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from research_swarm import runtime

FILES = {
    "/workspace/problems/A_one.lean": "theorem a : True := by\n  trivial\n",
    "/workspace/problems/DedekindNumber_M'_three.lean": "-- non-ASCII é\nexample : 1 = 1 := rfl\n",
    "/workspace/problems/" + "x" * 150 + ".lean": "long name\n",
    "/workspace/problems/sub/tasks.json": "{\n}\n",
    "/workspace/RESEARCH.md": "",
}


class FakeSandbox:
    def __init__(self, success=True, raises=None):
        self.success, self.raises = success, raises
        self.execs, self.writes = [], []

    async def exec(self, cmd, input=None, timeout=None):
        self.execs.append((cmd, input, timeout))
        if self.raises:
            raise self.raises
        return SimpleNamespace(success=self.success, stderr="" if self.success else "tar: boom")

    async def write_file(self, path, contents):
        self.writes.append((path, contents))


class ArchiveTests(unittest.TestCase):
    def test_archivable(self):
        self.assertTrue(runtime.archivable_files(FILES))
        self.assertFalse(runtime.archivable_files({}))
        self.assertFalse(runtime.archivable_files({"problems/a.lean": "x\n"}))  # relative path
        self.assertFalse(runtime.archivable_files({"/w/a": "x"}))  # single line: could be a path or URI
        self.assertFalse(runtime.archivable_files({"/w/a": b"x\n"}))
        self.assertFalse(runtime.archivable_files({"/w/../a": "x\n"}))

    def test_archive_is_deterministic_with_exact_bytes(self):
        data = runtime.workspace_archive(FILES)
        self.assertEqual(data, runtime.workspace_archive(dict(reversed(list(FILES.items())))))
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            members = archive.getmembers()
            self.assertEqual(sorted("/" + m.name for m in members), sorted(FILES))
            for member in members:
                self.assertTrue(member.isreg())
                self.assertEqual(member.mode, 0o666)
                self.assertEqual(archive.extractfile(member).read(), FILES["/" + member.name].encode("utf-8"))

    def test_command_creates_every_parent(self):
        cmd = runtime.workspace_archive_command(FILES)
        self.assertEqual(cmd[:5], ["/bin/sh", "-c", runtime._WORKSPACE_ARCHIVE_SCRIPT, "sh", "/"])
        self.assertEqual(cmd[5:], sorted(FILES))

    @unittest.skipUnless(all(os.path.exists(p) for p in ("/bin/sh", "/bin/mkdir", "/usr/bin/dirname", "/usr/bin/tar",
                                                         "/usr/bin/base64")), "needs the shell utilities")
    def test_extraction_matches_per_file_writes(self):
        """The archive exec yields the bytes and modes that mkdir -p + tee (Inspect's write_file) give."""
        root = tempfile.mkdtemp()
        try:
            archived, direct = Path(root, "archived"), Path(root, "direct")
            archived.mkdir()
            direct.mkdir()
            cmd = runtime.workspace_archive_command(FILES, root=str(archived))
            data = base64.b64encode(runtime.workspace_archive(FILES)).decode("ascii")
            subprocess.run(cmd, input=data, text=True, check=True, capture_output=True)
            for path, text in FILES.items():  # Inspect: mkdir -p parent; tee -- file
                target = direct / path.lstrip("/")
                subprocess.run(["/bin/mkdir", "-p", "--", str(target.parent)], check=True)
                subprocess.run(["tee", "--", str(target)], input=text.encode("utf-8"), check=True,
                               stdout=subprocess.DEVNULL)

            def tree(base):
                out = {}
                for dirpath, dirnames, filenames in os.walk(base):
                    for name in dirnames + filenames:
                        full = Path(dirpath, name)
                        info = full.lstat()
                        out[str(full.relative_to(base))] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                                                            info.st_uid, None if full.is_dir() else full.read_bytes())
                return out
            self.maxDiff = None
            self.assertEqual(tree(archived), tree(direct))
            self.assertEqual(len([k for k, v in tree(archived).items() if v[3] is not None]), len(FILES))
        finally:
            shutil.rmtree(root)

    @unittest.skipUnless(all(os.path.exists(p) for p in ("/bin/sh", "/bin/mkdir", "/usr/bin/dirname", "/usr/bin/tar",
                                                         "/usr/bin/base64")), "needs the shell utilities")
    def test_empty_input_fails_the_check(self):
        """An exec that loses its stdin extracts nothing; the existence check fails it (then per-file writes)."""
        with tempfile.TemporaryDirectory() as root:
            proc = subprocess.run(runtime.workspace_archive_command(FILES, root=root), input="", text=True,
                                  capture_output=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("missing:", proc.stderr)

    def test_write_uses_one_exec(self):
        box = FakeSandbox()
        self.assertEqual(asyncio.run(runtime.write_workspace_files(box, FILES)), "archive")
        self.assertEqual(len(box.execs), 1)
        self.assertEqual(box.writes, [])
        cmd, data, timeout = box.execs[0]
        self.assertEqual(cmd, runtime.workspace_archive_command(FILES))
        self.assertEqual(base64.b64decode(data), runtime.workspace_archive(FILES))
        self.assertEqual(timeout, runtime.WORKSPACE_ARCHIVE_TIMEOUT)

    def test_failed_archive_falls_back_to_per_file_writes(self):
        for box in (FakeSandbox(success=False), FakeSandbox(raises=RuntimeError("docker"))):
            with self.assertLogs("research_swarm.runtime", level="WARNING"):
                self.assertEqual(asyncio.run(runtime.write_workspace_files(box, FILES)), "per_file")
            self.assertEqual(dict(box.writes), FILES)

    def test_build_task_moves_literal_files_into_the_setup_step(self):
        solver_calls = []
        from inspect_ai.solver import solver as inspect_solver

        @inspect_solver
        def peer():
            async def solve(state, generate):
                solver_calls.append(state.sample_id)
                return state
            return solve
        task = runtime.build_task(agent_ids=["agent-001", "agmon-001"], files=FILES, solver=peer(),
                                  compose="compose.yaml", metadata={}, initial_message="go",
                                  sample_files={"agmon-001": {}})
        samples = {s.id: s for s in task.dataset}
        self.assertEqual(samples["agent-001"].files or {}, {})
        self.assertEqual(samples["agmon-001"].files or {}, {})
        # Non-literal files keep Inspect's own copy and the plain solver.
        plain = runtime.build_task(agent_ids=["agent-001"], files={"problems/a.lean": "x"}, solver=peer(),
                                   compose="compose.yaml", metadata={}, initial_message="go")
        self.assertEqual(list(plain.dataset)[0].files, {"problems/a.lean": "x"})

    def test_setup_step_writes_the_sample_files_before_the_solver(self):
        from unittest import mock
        from inspect_ai.solver import solver as inspect_solver
        order = []

        @inspect_solver
        def peer():
            async def solve(state, generate):
                order.append(("solver", state.sample_id))
                return state
            return solve
        box = FakeSandbox()
        with mock.patch("inspect_ai.util.sandbox", return_value=box):
            task = runtime.build_task(agent_ids=["agent-001", "agmon-001"], files=FILES, solver=peer(),
                                      compose="compose.yaml", metadata={}, initial_message="go",
                                      sample_files={"agmon-001": {}})
        for sample_id in ("agent-001", "agmon-001"):
            from inspect_ai.model import ModelName
            from inspect_ai.solver import TaskState
            state = TaskState(ModelName("mockllm/model"), sample_id, 0, "go", [])
            asyncio.run(task.solver(state, None))
        self.assertEqual(len(box.execs), 1)  # the solver only; the monitor has no files
        self.assertEqual(order, [("solver", "agent-001"), ("solver", "agmon-001")])


if __name__ == "__main__":
    unittest.main()
