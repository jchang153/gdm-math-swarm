"""Offline tests of scripts/supervise_run.py: a stub checkout, no paid call, no shutdown."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import BudgetLedger  # noqa: E402

spec = importlib.util.spec_from_file_location("supervise_run", ROOT / "scripts" / "supervise_run.py")
supervise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervise)

STUB_PILOT = """import json, os, sys
config = json.load(open(sys.argv[sys.argv.index('--config') + 1]))
assert '--launch-paid-pilot' in sys.argv and '--yes' in sys.argv
assert 'OPENROUTER_API_KEY' not in os.environ, 'provider key leaked to the child'
run = config['run_dir']
os.makedirs(run, exist_ok=True)
open(os.path.join(run, 'settings.json'), 'w').write('{}')
open(os.path.join(run, 'runtime.jsonl'), 'w').write(json.dumps({'kind': 'inference_finished', 'payload': {
    'provider_usage': {'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 90}}}}) + '\\n')
"""
STUB_OK = "import sys\nopen(sys.argv[0] + '.ran', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"


class SuperviseRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="supervise-"))
        (self.tmp / ".venv" / "bin").mkdir(parents=True)
        os.symlink(sys.executable, self.tmp / ".venv" / "bin" / "python")
        (self.tmp / "scripts").mkdir()
        (self.tmp / "research" / "analysis").mkdir(parents=True)
        (self.tmp / "scripts" / "run_pilot.py").write_text(STUB_PILOT)
        for name in ("scripts/reconcile_ledger.py", "scripts/evaluate_run.py", "research/analysis/timeline.py"):
            (self.tmp / name).write_text(STUB_OK)
        BudgetLedger(self.tmp / "state" / "budget.sqlite")
        self.phase = self.tmp / "runs" / "phase-b" / "run-x"
        self.phase.mkdir(parents=True)
        self.config = self.phase / "launch-config.json"
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": True}))
        os.environ["OPENROUTER_API_KEY"] = "test-not-a-real-key"

    def tearDown(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def args(self, *extra):
        return supervise.parse_args(["--config", str(self.config), "--phase-dir", str(self.phase), "--tmux-session",
                                     "gdm-run-x", "--root", str(self.tmp), "--retrieval-wait", "0", *extra])

    def test_full_cycle_without_shutdown(self):
        code = supervise.Supervisor(self.args("--no-shutdown")).main()
        self.assertEqual(code, 0)
        for name in ("budget-before-launch.sqlite", "budget-after-pilot.sqlite", "budget-final.sqlite",
                     "completed-run.tgz", "completed-phase.tgz", "completed-checksums.json", "live-checkpoint.json"):
            self.assertTrue((self.phase / name).exists(), name)
        self.assertEqual([(self.phase / f"{n}.exit").read_text().strip() for n in ("pilot", *supervise.FOLLOW_UPS)],
                         ["0"] * 5)
        self.assertFalse(json.loads(self.config.read_text())["paid_launch_enabled"])  # never relaunched
        checkpoint = json.loads((self.phase / "live-checkpoint.json").read_text())
        self.assertEqual((checkpoint["cache"]["fraction"], checkpoint["new_settled_usd"]), (0.9, "0"))
        self.assertIn("--dry-run", (self.tmp / "scripts" / "evaluate_run.py.ran").read_text())
        self.assertEqual(json.loads((self.phase / "supervisor-status.json").read_text())["stage"],
                         "done_vm_left_running")
        with self.assertRaises(SystemExit):  # the disabled config refuses a relaunch ...
            supervise.Supervisor(self.args("--no-shutdown"))
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": True}))
        with self.assertRaises(FileExistsError):  # ... and so does the surviving marker, even if re-enabled
            supervise.Supervisor(self.args("--no-shutdown")).main()

    def test_refuses_config_without_run_dir_or_disabled_launch(self):
        self.config.write_text(json.dumps({"paid_launch_enabled": True}))
        with self.assertRaises(SystemExit):
            supervise.Supervisor(self.args())
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": False}))
        with self.assertRaises(SystemExit):
            supervise.Supervisor(self.args())
        self.assertFalse((self.phase / "supervisor.started").exists())

    def test_failed_run_skips_follow_ups(self):
        (self.tmp / "scripts" / "run_pilot.py").write_text("raise SystemExit(2)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 2)
        self.assertFalse((self.phase / "evaluate.exit").exists())
        self.assertTrue((self.phase / "budget-final.sqlite").exists())
        self.assertFalse(json.loads(self.config.read_text())["paid_launch_enabled"])


if __name__ == "__main__":
    unittest.main()
