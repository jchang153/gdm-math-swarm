"""Tests for the read-only researcher tools scripts/monitor_run.py and scripts/verify_hosted.py."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import monitor_run  # noqa: E402
import verify_hosted  # noqa: E402
from research_swarm.budget import BudgetLedger  # noqa: E402

START = 1_800_000_000.0


def make_run(root):
    run = root / "runs" / "demo-20261020T000000Z"
    phase = root / "runs" / "phase-b" / run.name
    (run / "conference").mkdir(parents=True)
    phase.mkdir(parents=True)
    (run / "settings.json").write_text(json.dumps({"config": {"max_run_seconds": 7200}}))
    events = [{"timestamp": START, "kind": "common_start", "agent_id": None, "payload": {}},
              {"timestamp": START + 5, "kind": "inference_admitted", "agent_id": "agent-001", "payload": {}},
              {"timestamp": START + 9, "kind": "inference_finished", "agent_id": "agent-001", "payload": {}},
              {"timestamp": START + 60, "kind": "agent_restart", "agent_id": "agent-002",
               "payload": {"ended": "error", "requests_in_session": 3}},
              {"timestamp": START + 90, "kind": "agent_exit", "agent_id": "agent-002", "payload": {}}]
    (run / "runtime.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events) + '{"partial')
    db = sqlite3.connect(run / "conference" / "state.sqlite")
    db.executescript("CREATE TABLE tasks (task_id TEXT, status TEXT, credited_agent TEXT, accepted_at TEXT);"
                     "CREATE TABLE submissions (outcome TEXT); CREATE TABLE posts (id); CREATE TABLE messages (id);"
                     "INSERT INTO tasks VALUES ('T1','closed','agent-001','2026'),('T2','open',NULL,NULL);"
                     "INSERT INTO submissions VALUES ('accepted'),('rejected'),('rejected');"
                     "INSERT INTO posts VALUES (1);")
    db.commit()
    db.close()
    prior = BudgetLedger(phase / "budget.sqlite")
    prior.start_run("prior-run", "pilot", 100)
    prior.settle(prior.reserve("pilot", "old", 10), 10)  # a prior run's spending
    shutil.copy(phase / "budget.sqlite", phase / "budget-before-launch.sqlite")
    ledger = BudgetLedger(phase / "budget.sqlite")
    ledger.start_run(run.name, "pilot", 20)  # this run's own budget
    ledger.settle(ledger.reserve("pilot", "agent-001", 5), 4)
    ledger.reserve("pilot", "agent-001", 2)  # still held
    return run, phase



class MonitorRunTest(unittest.TestCase):
    def test_summary_uses_launch_baseline_and_reads_only(self):
        with tempfile.TemporaryDirectory() as folder:
            run, phase = make_run(Path(folder))
            before = {p: p.read_bytes() for p in (run / "conference" / "state.sqlite", phase / "budget.sqlite")}
            summary = monitor_run.summarize(run, ledger=phase / "budget.sqlite", now=START + 3600)
            self.assertEqual({p: p.read_bytes() for p in before}, before)
        spend = summary["spend"]
        self.assertEqual((spend["source"], spend["run_id"], spend["budget_usd"]), ("ledger", run.name, "20"))
        self.assertEqual((spend["run_settled_usd"], spend["run_committed_usd"]), ("4", "6"))
        self.assertEqual((spend["remaining_usd"], spend["unresolved_requests"]), ("14", 1))
        self.assertEqual(spend["settled_usd_per_hour"], "4.00")
        self.assertEqual(spend["eta_hours"], 3.5)
        self.assertEqual((summary["elapsed_seconds"], summary["max_run_seconds"]), (3600, 7200))
        self.assertEqual(summary["requests"], {"admitted": 1, "finished": 1, "uncertain": 0})
        self.assertEqual((summary["restarts"], summary["exits"]), (1, 1))
        self.assertEqual(summary["restarts_by_agent"], {"agent-002": ["error(3)"]})
        conf = summary["conference"]
        self.assertEqual((conf["tasks"], conf["solved"], conf["library"]), (2, [["T1", "agent-001"]], None))
        self.assertEqual(conf["submissions"], {"accepted": 1, "rejected": 2})
        text = monitor_run.render(summary)
        self.assertIn("elapsed 60m of 120m", text)
        self.assertIn(f"run {run.name} from ledger: budget $20.00", text)
        self.assertIn("$14.00 left", text)
        self.assertIn("solved 1/2", text)

    def test_final_run_budget_record_without_a_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            run, phase = make_run(Path(folder))
            (run / "run-budget.json").write_text(json.dumps({
                "run_id": run.name, "scope": "pilot", "budget_usd": "20", "committed_usd": "7", "settled_usd": "7",
                "remaining_usd": "13", "unresolved_requests": 0, "estimate_violations": 0}))
            summary = monitor_run.summarize(run, phase_dir=phase, now=START + 3600)
        self.assertEqual((summary["spend"]["source"], summary["spend"]["remaining_usd"]), ("run-budget.json", "13"))

    def test_checkpoint_fallback_without_baseline(self):
        # Runs from before per-run budgets: cumulative caps and a launch baseline.
        with tempfile.TemporaryDirectory() as folder:
            run, phase = make_run(Path(folder))
            (phase / "budget-before-launch.sqlite").unlink()
            (phase / "live-checkpoint.json").write_text(json.dumps({
                "utc": "x", "ledger": str(Path(folder) / "elsewhere.sqlite"), "new_settled_usd": "3",
                "cache": {"fraction": 0.5},
                "budget": {"pilot": {"cap_usd": "100", "committed_usd": "20", "settled_usd": "18"}}}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(monitor_run.main(["--run-dir", str(run)]), 0)
        text = out.getvalue()
        self.assertIn("from checkpoint: baseline unknown", text)
        self.assertIn("run settled $3.00", text)
        self.assertIn("cache 50%", text)


class FakeResponse:
    def __init__(self, status, content, headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


class FakeApi:
    def __init__(self, root, sha="abc123", private=True, lfs=()):
        self.root, self.sha, self.private, self.lfs = Path(root), sha, private, set(lfs)

    def space_info(self, repo):
        return SimpleNamespace(sha=self.sha, private=self.private, host="https://space.hf.space")

    def get_paths_info(self, repo, paths, repo_type, revision):
        return [SimpleNamespace(path=p, lfs={"size": 1} if p in self.lfs else None)
                for p in paths if (self.root / p).is_file()]

    def hf_hub_download(self, repo, name, repo_type, revision, cache_dir=None):
        assert revision == self.sha
        return str(self.root / name)


class FakeSession:
    def __init__(self, root, overrides=None):
        self.root, self.overrides, self.calls = Path(root), overrides or {}, []

    def get(self, url, headers, timeout, follow_redirects):
        path = url.split("https://space.hf.space/", 1)[1]
        self.calls.append((path, headers.get("Range")))
        if path in self.overrides:
            return self.overrides[path]
        data = (self.root / path).read_bytes()
        if headers.get("Range"):
            return FakeResponse(206, data[:64])
        script = b'<script>window.huggingface={variables:{"SPACE_CREATOR_USER_ID":"0a1b"}};</script>'
        return FakeResponse(200, script + data, {"content-type": "text/html; charset=utf-8"})


class VerifyHostedTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        run = "demo-run"
        for page in verify_hosted.PAGES:
            path = self.root / page.format(run=run)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"<!doctype html><title>{page}</title>".encode())
        (self.root / run / "inspect-view" / "logs").mkdir(parents=True)
        (self.root / run / "inspect-view" / "logs" / "a.eval").write_bytes(bytes(range(100)))

    def tearDown(self):
        self.folder.cleanup()

    def run_verify(self, api=None, session=None, revision="abc"):
        return verify_hosted.verify(api or FakeApi(self.root), session or FakeSession(self.root), {"x": "y"},
                                    "org/space", "demo-run", revision, bundle=self.root,
                                    range_path="demo-run/inspect-view/logs/a.eval")

    def test_passes_when_served_html_matches(self):
        record = self.run_verify()
        self.assertTrue(record["ok"], record)
        self.assertEqual(record["revision"], "abc123")
        self.assertEqual({c["check"] for c in record["checks"]},
                         {"private", "current_revision", "stored", "not_lfs", "bundle_match", "served_html",
                          "served_exact", "range_206"})

    def test_detects_lfs_download_and_moved_revision(self):
        page = "demo-run/results.html"
        attachment = FakeResponse(200, b"version https://git-lfs.github.com/spec/v1\n",
                                  {"content-type": "application/octet-stream",
                                   "content-disposition": "attachment; filename=results.html"})
        record = self.run_verify(FakeApi(self.root, lfs={page}), FakeSession(self.root, {page: attachment}))
        self.assertFalse(record["ok"])
        failed = {(c["check"], c.get("path")) for c in record["checks"] if not c["ok"]}
        self.assertEqual(failed, {("not_lfs", page), ("served_html", page), ("served_exact", page)})
        moved = self.run_verify(revision="fff")
        self.assertFalse(moved["ok"])
        self.assertEqual([c["check"] for c in moved["checks"]], ["private", "current_revision"])

    def test_long_page_lists_are_queried_in_batches(self):
        api, sizes = FakeApi(self.root), []
        original = api.get_paths_info

        def limited(repo, paths, repo_type, revision):
            sizes.append(len(paths))
            if len(paths) > verify_hosted.PATHS_INFO_BATCH:
                raise RuntimeError("413 Payload Too Large")
            return original(repo, paths, repo_type, revision)
        api.get_paths_info = limited
        pages = [f"demo-run/conference-view/file-{i}.html" for i in range(2 * verify_hosted.PATHS_INFO_BATCH + 1)]
        for page in pages:
            (self.root / page).parent.mkdir(parents=True, exist_ok=True)
            (self.root / page).write_bytes(b"<!doctype html>")
        record = verify_hosted.verify(api, FakeSession(self.root), {}, "org/space", "demo-run", "abc", pages=pages)
        self.assertTrue(record["ok"])
        self.assertEqual(sizes, [verify_hosted.PATHS_INFO_BATCH, verify_hosted.PATHS_INFO_BATCH, 1])

    def test_detects_public_space(self):
        record = self.run_verify(FakeApi(self.root, private=False))
        self.assertEqual([c["check"] for c in record["checks"] if not c["ok"]], ["private"])


if __name__ == "__main__":
    unittest.main()
