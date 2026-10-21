"""Offline tests of scripts/supervise_run.py: a stub checkout, no paid call, no shutdown."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import BudgetLedger  # noqa: E402

spec = importlib.util.spec_from_file_location("supervise_run", ROOT / "scripts" / "supervise_run.py")
supervise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervise)

STUB_PILOT = """import json, os, sys
config = json.load(open(sys.argv[sys.argv.index('--config') + 1]))
assert '--launch-paid-pilot' in sys.argv and '--yes' not in sys.argv  # no confirmation flag
assert 'OPENROUTER_API_KEY' not in os.environ, 'provider key leaked to the child'
run = config['run_dir']
os.makedirs(run, exist_ok=True)
open(os.path.join(run, 'settings.json'), 'w').write('{}')
sys.path.insert(0, os.path.join(os.environ['GDM_TEST_REPO'], 'src'))
from research_swarm.budget import BudgetLedger
ledger = BudgetLedger(os.path.join('state', 'budget.sqlite'))
ledger.start_run(os.path.basename(run), 'pilot', config.get('run_budget_usd', 100))
ledger.settle(ledger.reserve('pilot', 'agent-001', '0.5'), '0.25')
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
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "run_budget_usd": "40"}))
        os.environ["OPENROUTER_API_KEY"] = "test-not-a-real-key"
        os.environ["GDM_TEST_REPO"] = str(ROOT)
        supervise.ENV_KEYS = (*supervise.ENV_KEYS, "GDM_TEST_REPO")

    def tearDown(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("GDM_TEST_REPO", None)
        supervise.ENV_KEYS = tuple(k for k in supervise.ENV_KEYS if k != "GDM_TEST_REPO")
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
        self.assertEqual(json.loads(self.config.read_text()), {"run_dir": "runs/run-x", "run_budget_usd": "40"})
        checkpoint = json.loads((self.phase / "live-checkpoint.json").read_text())
        self.assertEqual((checkpoint["cache"]["fraction"], checkpoint["new_settled_usd"]), (0.9, "0.25"))
        run = checkpoint["run_budget"]  # this run's own budget, read from the ledger
        self.assertEqual((run["run_id"], run["budget_usd"], run["committed_usd"], run["remaining_usd"]),
                         ("run-x", "40", "0.25", "39.75"))
        reconcile = (self.tmp / "scripts" / "reconcile_ledger.py.ran").read_text()
        self.assertNotIn("--dry-run", reconcile)  # settles uncertain holds automatically
        self.assertIn("--ledger " + str(self.tmp.resolve() / "state" / "budget.sqlite"), reconcile)
        self.assertIn("--dry-run", (self.tmp / "scripts" / "evaluate_run.py.ran").read_text())
        self.assertEqual(json.loads((self.phase / "supervisor-status.json").read_text())["stage"],
                         "done_vm_left_running")
        with self.assertRaises(FileExistsError):  # the surviving marker: a phase never launches twice
            supervise.Supervisor(self.args("--no-shutdown")).main()

    def test_refuses_config_without_run_dir(self):
        self.config.write_text(json.dumps({"run_budget_usd": "40"}))
        with self.assertRaises(SystemExit):
            supervise.Supervisor(self.args())
        self.assertFalse((self.phase / "supervisor.started").exists())
        # No attestation is needed: a config without paid_launch_enabled (or with it false) launches.
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": False}))
        supervise.Supervisor(self.args())

    def test_failed_run_skips_follow_ups(self):
        (self.tmp / "scripts" / "run_pilot.py").write_text("raise SystemExit(2)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 2)
        self.assertFalse((self.phase / "evaluate.exit").exists())
        self.assertTrue((self.phase / "budget-final.sqlite").exists())

    def test_a_failed_reconciliation_never_stops_the_other_follow_ups(self):
        (self.tmp / "scripts" / "reconcile_ledger.py").write_text("raise SystemExit(1)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual((self.phase / "reconcile.exit").read_text().strip(), "1")
        self.assertEqual((self.phase / "timeline.exit").read_text().strip(), "0")

    def test_missing_ledger_is_not_an_error(self):
        (self.tmp / "state" / "budget.sqlite").unlink()
        (self.tmp / "scripts" / "run_pilot.py").write_text("raise SystemExit(2)\n")  # blocked before any request
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 2)
        self.assertFalse((self.phase / "budget-final.sqlite").exists())
        self.assertEqual(json.loads((self.phase / "supervisor-status.json").read_text())["stage"],
                         "done_vm_left_running")



STUB_ORDER = """import os, sys
root = os.getcwd()  # the supervisor runs every step from the checkout root
open(os.path.join(root, 'order.log'), 'a').write(os.path.basename(sys.argv[0]) + ' ' + ' '.join(sys.argv[1:]) + '\\n')
"""
STUB_JUDGE = STUB_ORDER + """assert 'OPENROUTER_API_KEY' not in os.environ, 'provider key leaked to the judge child'
run = sys.argv[sys.argv.index('--run-dir') + 1]
os.makedirs(os.path.join(run, 'analysis', 'judge'), exist_ok=True)
open(os.path.join(run, 'analysis', 'judge', 'report.json'), 'w').write('{}')
mode = open(os.path.join(root, 'judge-mode')).read().strip() if os.path.exists(os.path.join(root, 'judge-mode')) else 'ok'
if mode == 'refuse':
    print('REFUSED: judge route pricing record x: no longer validates; nothing was sent',
          file=sys.stderr)
    raise SystemExit(4)
if mode == 'crash':
    raise RuntimeError('judge crashed')
"""
JUDGE_CONFIG = {"auto_behavior_judge": True, "behavior_judge_max_usd": 50}


class AutoBehaviorJudgeTests(SuperviseRunTests):
    """The automatic post-run behavior judge (docs/plan.md, "Automatic post-run behavior judge for the
    owner's runs (October 20, 2026)" and "One per-run budget, no manual approvals (October 20, 2026)")
    with a fake judge: no paid call."""

    def setUp(self):
        super().setUp()
        for name in ("scripts/reconcile_ledger.py", "scripts/evaluate_run.py", "research/analysis/timeline.py"):
            (self.tmp / name).write_text(STUB_ORDER)
        (self.tmp / "scripts" / "run_transcript_judge.py").write_text(STUB_JUDGE)
        self.write_config()

    def write_config(self, **changes):
        self.config.write_text(json.dumps(dict(JUDGE_CONFIG, run_dir="runs/run-x", **changes)))

    def order(self):
        path = self.tmp / "order.log"
        return [line.split()[0] for line in path.read_text().splitlines()] if path.exists() else []

    def judge_record(self):
        return json.loads((self.phase / "behavior-judge.json").read_text())

    def packaged(self):
        import tarfile
        with tarfile.open(self.phase / "completed-phase.tgz") as tar:
            phase = set(tar.getnames())
        with tarfile.open(self.phase / "completed-run.tgz") as tar:
            run = set(tar.getnames())
        return phase, run

    def test_judge_runs_after_the_follow_ups_and_is_packaged(self):
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual(self.order(), ["reconcile_ledger.py", "evaluate_run.py", "evaluate_run.py", "timeline.py",
                                        "run_transcript_judge.py"])  # the judge refreshes its own pricing record
        judge_line = (self.tmp / "order.log").read_text().splitlines()[-1]
        root = self.tmp.resolve()
        for text in ("--run-config " + str(self.config.resolve()),
                     "--ledger " + str(root / "state" / "budget.sqlite"), "--credentials-root " + str(root),
                     "--run-dir " + str(root / "runs" / "run-x")):
            self.assertIn(text, judge_line)
        self.assertNotIn("--i-have-user-approval", judge_line)
        self.assertNotIn("standing-approval", judge_line)
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["exit"]), ("complete", 0))
        self.assertEqual((self.phase / "behavior-judge.exit").read_text().strip(), "0")
        phase, run = self.packaged()
        self.assertTrue({"behavior-judge.json", "behavior-judge.log", "behavior-judge.exit"} <= phase)
        self.assertIn("runs/run-x/analysis/judge/report.json", run)
        status = json.loads((self.phase / "supervisor-status.json").read_text())
        self.assertEqual(status["stage"], "done_vm_left_running")
        self.assertEqual(json.loads((self.phase / "worker.exit").read_text()), 0)

    def test_any_track_and_any_positive_budget_is_judged_without_an_approval_note(self):
        for n, changes in enumerate(({"budget_track": "ablation"}, {"behavior_judge_max_usd": 120},
                                     {"behavior_judge_max_usd": None})):
            self.phase = self.tmp / "runs" / "phase-b" / f"run-{n}"
            self.phase.mkdir(parents=True)
            self.config = self.phase / "launch-config.json"
            self.write_config(**changes)
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
            self.assertEqual(self.judge_record()["outcome"], "complete", changes)

    def test_judge_refusal_or_failure_never_fails_the_run(self):
        (self.tmp / "judge-mode").write_text("refuse")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["exit"]), ("refused", 4))
        self.assertIn("REFUSED:", record["reason"])
        self.assertTrue((self.phase / "completed-checksums.json").exists())
        self.assertTrue((self.phase / "budget-final.sqlite").exists())
        status = json.loads((self.phase / "supervisor-status.json").read_text())
        self.assertEqual(status["stage"], "done_vm_left_running")

    def test_judge_crash_never_fails_the_run(self):
        (self.tmp / "judge-mode").write_text("crash")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual((self.judge_record()["outcome"], self.judge_record()["exit"]), ("failed", 1))
        self.assertTrue((self.phase / "completed-run.tgz").exists())

    def test_supervisor_error_in_the_judge_step_is_recorded(self):
        real_step = supervise.Supervisor.step

        def step(supervisor, name, arguments):
            if name == "behavior-judge":
                raise OSError("disk")
            return real_step(supervisor, name, arguments)
        with mock.patch.object(supervise.Supervisor, "step", step):
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["error_type"]), ("supervisor_error", "OSError"))
        self.assertTrue((self.phase / "completed-checksums.json").exists())

    def test_skips(self):
        cases = (({"behavior_judge_max_usd": "abc"}, (), "invalid judge settings"),
                 ({"behavior_judge_max_usd": 0}, (), "invalid judge settings"),
                 ({}, ("--no-postrun",), "--no-postrun"))
        for n, (changes, extra, reason) in enumerate(cases):
            self.phase = self.tmp / "runs" / "phase-b" / f"run-{n}"
            self.phase.mkdir(parents=True)
            self.config = self.phase / "launch-config.json"
            self.write_config(**changes)
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown", *extra)).main(), 0)
            record = self.judge_record()
            self.assertEqual(record["outcome"], "skipped")
            self.assertIn(reason, record["reason"])
        self.assertNotIn("run_transcript_judge.py", self.order())

    def test_failed_follow_up_skips_the_judge(self):
        (self.tmp / "research" / "analysis" / "timeline.py").write_text("raise SystemExit(7)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 7)
        record = self.judge_record()
        self.assertEqual(record["outcome"], "skipped")
        self.assertIn("did not complete (exit 7)", record["reason"])
        self.assertNotIn("run_transcript_judge.py", self.order())

    def test_no_judge_without_the_config_keys(self):
        self.config.write_text(json.dumps({"run_dir": "runs/run-x"}))
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertFalse((self.phase / "behavior-judge.json").exists())
        self.assertNotIn("run_transcript_judge.py", self.order())

    # The inherited runs use the base config (no auto judge keys) and stub follow-ups.
    test_full_cycle_without_shutdown = None
    test_refuses_config_without_run_dir = None
    test_failed_run_skips_follow_ups = None
    test_a_failed_reconciliation_never_stops_the_other_follow_ups = None
    test_missing_ledger_is_not_an_error = None


if __name__ == "__main__":
    unittest.main()
