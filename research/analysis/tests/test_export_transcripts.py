"""Offline tests for scripts/export_transcripts.py (full-history researcher transcripts).

Exports a synthetic run (built by test_judge_history) and, when present, one real
cache100 agent, always into a temporary directory: saved runs are never rewritten here.
"""
import html
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO / "src", REPO, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from research_swarm.inspect_adapter import disable_inspect_dotenv  # noqa: E402

disable_inspect_dotenv()
from test_judge_history import CACHE100, HAVE_INSPECT, build_synthetic_run  # noqa: E402

_spec = importlib.util.spec_from_file_location("export_transcripts", REPO / "scripts" / "export_transcripts.py")
export_transcripts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_transcripts)
# scripts/export_conference.py binds timeline message ids to these anchors.
CONFERENCE_BINDING = re.compile(r'<article id="(message-\d+)">(?:(?!</article>).)*?<small>Message ID: (.*?)</small>', re.S)


@unittest.skipUnless(HAVE_INSPECT, "inspect_ai not installed")
class SyntheticExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.run_dir, _ = build_synthetic_run(cls.tmp.name)
        cls.out = Path(cls.tmp.name) / "export"
        cls.result = export_transcripts.export(cls.run_dir, cls.out, require_saved_run=False)
        cls.page = (cls.out / "agent-001.html").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_outputs_and_manifest_contract(self):
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["agent-001.html", "agent-002.html", "index.html", "manifest.json"])
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual([m["agent_id"] for m in manifest], ["agent-001", "agent-002"])
        for entry in manifest:   # the original keys are kept
            self.assertTrue({"agent_id", "messages", "source", "output", "inspect_error"} <= set(entry))
            self.assertEqual(entry["source"], "inspect/synthetic.eval")
        self.assertEqual(manifest[0]["turns"], 4)
        self.assertEqual(manifest[0]["segments"], 2)
        self.assertEqual(self.result["agents"], 2)
        self.assertIn('href="agent-001.html"', (self.out / "index.html").read_text())

    def test_full_history_rendered_with_item_anchors(self):
        from research.analysis.judge import history
        built = history.build_run(self.run_dir, agents=["agent-001"])
        record = built.agents["agent-001"]
        for item in record.items:
            self.assertIn(f'id="{html.escape(item["id"])}"', self.page, item["id"])
        for item in record.harness_items:
            self.assertIn(f'id="{item["id"]}"', self.page)
        self.assertIn("R0: plan the survey.", self.page)            # pre-compaction reasoning
        self.assertIn("Compaction summary", self.page)
        self.assertIn('id="segment-1"', self.page)
        self.assertIn("notes after compaction", self.page)          # result first seen after compaction
        self.assertIn("Private file version", self.page)

    def test_escaping_and_conference_binding(self):
        self.assertNotIn("<script>alert(1)</script>", self.page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.page)
        found = [m[1] for m in CONFERENCE_BINDING.findall(self.page)]
        self.assertEqual(len(found), len(set(found)))          # one anchor per Inspect message
        self.assertTrue({"out-0", "out-1", "out-2", "out-3", "tr-a", "tr-d", "tr-g"} <= set(found), found)

    def test_saved_run_guard(self):
        with self.assertRaises(ValueError):
            export_transcripts.export(self.run_dir, Path(self.tmp.name) / "other")


@unittest.skipUnless(HAVE_INSPECT and (CACHE100 / "inspect").is_dir(), "cache100 run not present")
class RealAgentExportTests(unittest.TestCase):
    def test_one_real_agent_exports_full_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = export_transcripts.export(CACHE100, Path(tmp), agents=["agent-007"])
            self.assertEqual(result["agents"], 1)
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual((manifest[0]["turns"], manifest[0]["summary_turns"]), (95, 1))
            page = (Path(tmp) / "agent-007.html").read_text()
            self.assertIn('id="A07.t0050.s"', page)
            self.assertIn('id="A07.t0000.r"', page)                # before the compaction
            self.assertIn('id="segment-1"', page)
            self.assertIn('id="post-000012"', page)
            self.assertGreater(len(CONFERENCE_BINDING.findall(page)), 95)


if __name__ == "__main__":
    unittest.main()
