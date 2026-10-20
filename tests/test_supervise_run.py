"""Offline tests of scripts/supervise_run.py: a stub checkout, no paid call, no shutdown."""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import ABLATION_CAPS, GEMINI_CAPS, BudgetLedger, read_snapshot  # noqa: E402

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
        # No Docker in these tests: no comparator container of another evaluation is running.
        patcher = mock.patch.object(supervise.Supervisor, "comparators_running", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

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


    def gemini_ledger(self):
        ledger = BudgetLedger(self.tmp / "state" / "gemini-budget.sqlite", GEMINI_CAPS)
        ledger.settle(ledger.reserve("compatibility", "agent-001", "0.5"), "0.25", "provider_reported_cost")
        return ledger

    def test_track_config_uses_its_own_ledger(self):
        """budget_track gemini: backups, checkpoints and the reconcile dry run use state/gemini-budget.sqlite."""
        self.gemini_ledger()
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": True,
                                           "budget_track": "gemini"}))
        supervisor = supervise.Supervisor(self.args("--no-shutdown"))
        self.assertEqual(supervisor.ledger, (self.tmp / "state" / "gemini-budget.sqlite").resolve())
        self.assertEqual(supervisor.main(), 0)
        for name in ("budget-before-launch.sqlite", "budget-after-pilot.sqlite", "budget-final.sqlite"):
            backup = read_snapshot(self.phase / name)
            self.assertEqual((backup["pilot"]["cap_usd"], backup["compatibility"]["settled_usd"]), ("462.38", "0.25"))
        checkpoint = json.loads((self.phase / "live-checkpoint.json").read_text())
        self.assertEqual(checkpoint["budget"]["pilot"]["cap_usd"], "462.38")
        self.assertEqual(checkpoint["ledger"], str(supervisor.ledger))
        self.assertEqual(checkpoint["new_settled_usd"], "0")
        ran = (self.tmp / "scripts" / "reconcile_ledger.py.ran").read_text()
        self.assertIn("--ledger " + str(supervisor.ledger), ran)

    def test_ledger_option_and_missing_track_ledger(self):
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": True,
                                           "budget_track": "gemini"}))
        with self.assertRaises(SystemExit):  # the compatibility check creates the track ledger first
            supervise.Supervisor(self.args())
        # --ledger never replaces the ledger run_pilot reserves against (review finding): the baseline
        # ledger, or another track's, is refused for a gemini config even when it exists.
        baseline = self.tmp / "state" / "budget.sqlite"
        with self.assertRaises(SystemExit) as caught:
            supervise.Supervisor(self.args("--ledger", str(baseline)))
        self.assertIn("gemini-budget.sqlite", str(caught.exception))
        own = self.gemini_ledger().path
        self.assertEqual(supervise.Supervisor(self.args("--ledger", str(own))).ledger, Path(own).resolve())
        with self.assertRaises(SystemExit):
            supervise.Supervisor(self.args("--ledger", str(self.tmp / "state" / "ablation-budget.sqlite")))
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": True}))
        self.assertEqual(supervise.Supervisor(self.args("--ledger", str(baseline))).ledger, baseline.resolve())
        with self.assertRaises(SystemExit):  # nor the other way round
            supervise.Supervisor(self.args("--ledger", str(own)))
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": True,
                                           "budget_track": "gemini-typo"}))
        with self.assertRaises(SystemExit):
            supervise.Supervisor(self.args())
        self.assertFalse((self.phase / "supervisor.started").exists())

    def test_evaluation_deferred_while_comparators_run(self):
        with mock.patch.object(supervise.Supervisor, "comparators_running", return_value=["gdm-comparator-x"]):
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 4)
        deferred = json.loads((self.phase / "evaluate.deferred").read_text())
        self.assertEqual(deferred["comparators"], ["gdm-comparator-x"])
        self.assertEqual([(self.phase / f"{n}.exit").exists() for n in supervise.FOLLOW_UPS],
                         [True, True, False, False])
        self.assertFalse(json.loads(self.config.read_text())["paid_launch_enabled"])

    def idle(self, *, containers="", sessions="gdm-run-x\n", processes="", users=""):
        """idle_shutdown with every command faked; returns (code, commands run, final status)."""
        commands = []

        def run(cmd, **kwargs):
            commands.append(cmd)
            stdout = {"docker": containers, "tmux": sessions, "sudo": ""}.get(cmd[0])
            if cmd[0] == "ps":
                stdout = users if "-e" in cmd else processes
            return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        supervisor = supervise.Supervisor(self.args())
        with mock.patch.object(supervise.subprocess, "run", run), mock.patch.object(supervise.time, "sleep"), \
                mock.patch.object(supervise.os, "sync"):
            code = supervisor.idle_shutdown(0)
        return code, commands, json.loads((self.phase / "supervisor-status.json").read_text())

    def test_idle_shutdown_blocked_by_other_users_containers_or_sessions(self):
        me = os.getuid()
        other = 1000 if me != 1000 else 1001
        for name, changes in (("another login user's tmux", {"users": f"{other} 4242 tmux: server\n"}),
                              ("a container", {"containers": "inspect-other-default-1\n"}),
                              ("another tmux session", {"sessions": "gdm-run-x\ngdm-glm\n"})):
            with self.subTest(name=name):
                code, commands, status = self.idle(**changes)
                self.assertEqual(code, 3)
                self.assertNotIn("shutdown", [part for cmd in commands for part in cmd])
                self.assertEqual(status["stage"], "preserved_shutdown_blocked_by_other_work")
        self.assertEqual(status["other_sessions"], ["gdm-glm"])
        code, commands, status = self.idle(users=f"{other} 4242 tmux: server\n")
        self.assertEqual(status["other_users"], [{"uid": other, "pid": 4242, "name": "tmux: server"}])
        # System accounts, "nobody" and this user's own processes do not block.
        code, commands, status = self.idle(users=f"0 1 systemd\n{supervise.NOBODY_UID} 7 x\n{me} 9 bash\n")
        self.assertEqual((code, status["stage"]), (0, "preserved_shutdown_requested"))
        self.assertIn(["sudo", "-n", "shutdown", "-h", "now"], commands)  # faked: nothing is shut down


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
    print('REFUSED: standing approval: the run limit $1 cannot cover the central estimate $9; nothing was sent',
          file=sys.stderr)
    raise SystemExit(4)
if mode == 'crash':
    raise RuntimeError('judge crashed')
"""
STUB_FREEZE = STUB_ORDER + """import datetime, json
if not os.path.exists(os.path.join(root, 'freeze-fails')):
    os.makedirs(os.path.join(root, 'data', 'validation'), exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc)
    open(os.path.join(root, 'data', 'validation', 'judge-route-pricing-' + stamp.strftime('%Y%m%dT%H%M%SZ') + '.json'),
         'w').write(json.dumps({'created_at': stamp.isoformat()}))
else:
    raise SystemExit(1)
"""
APPROVAL = {"auto_behavior_judge": True, "behavior_judge_approval": "owner standing approval (test)",
            "behavior_judge_max_usd": 50}


class AutoBehaviorJudgeTests(SuperviseRunTests):
    """The automatic post-run behavior judge (docs/plan.md, "Automatic post-run behavior judge for the
    owner's runs (October 20, 2026)") with a fake judge and a fake route freeze: no paid call."""

    def setUp(self):
        super().setUp()
        for name in ("scripts/reconcile_ledger.py", "scripts/evaluate_run.py", "research/analysis/timeline.py"):
            (self.tmp / name).write_text(STUB_ORDER)
        (self.tmp / "scripts" / "run_transcript_judge.py").write_text(STUB_JUDGE)
        (self.tmp / "scripts" / "freeze_judge_route.py").write_text(STUB_FREEZE)
        self.write_config()

    def write_config(self, **changes):
        self.config.write_text(json.dumps(dict(APPROVAL, run_dir="runs/run-x", paid_launch_enabled=True, **changes)))

    def pricing(self, hours_old):
        import datetime
        folder = self.tmp / "data" / "validation"
        folder.mkdir(parents=True, exist_ok=True)
        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_old)
        (folder / "judge-route-pricing-20261001T000000Z.json").write_text(json.dumps({"created_at": created.isoformat()}))

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
        self.pricing(1)
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual(self.order(), ["reconcile_ledger.py", "evaluate_run.py", "evaluate_run.py", "timeline.py",
                                        "run_transcript_judge.py"])  # fresh record: no freeze
        judge_line = (self.tmp / "order.log").read_text().splitlines()[-1]
        root = self.tmp.resolve()
        for text in ("--standing-approval-from-config " + str(self.config.resolve()),
                     "--ledger " + str(root / "state" / "budget.sqlite"), "--credentials-root " + str(root),
                     "--run-dir " + str(root / "runs" / "run-x")):
            self.assertIn(text, judge_line)
        self.assertNotIn("--i-have-user-approval", judge_line)
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["exit"]), ("complete", 0))
        self.assertEqual((self.phase / "behavior-judge.exit").read_text().strip(), "0")
        phase, run = self.packaged()
        self.assertTrue({"behavior-judge.json", "behavior-judge.log", "behavior-judge.exit"} <= phase)
        self.assertIn("judge-route-pricing/judge-route-pricing-20261001T000000Z.json", phase)
        self.assertIn("runs/run-x/analysis/judge/report.json", run)
        status = json.loads((self.phase / "supervisor-status.json").read_text())
        self.assertEqual(status["stage"], "done_vm_left_running")
        self.assertEqual(json.loads((self.phase / "worker.exit").read_text()), 0)

    def test_stale_pricing_is_refreshed_first(self):
        self.pricing(73)
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual(self.order()[-2:], ["freeze_judge_route.py", "run_transcript_judge.py"])
        record = self.judge_record()
        self.assertEqual((record["freeze_exit"], record["outcome"]), (0, "complete"))
        self.assertLess(record["pricing_age_hours"], 1)
        self.assertIn("judge-route-freeze.log", self.packaged()[0])

    def test_still_stale_pricing_skips_the_judge(self):
        (self.tmp / "freeze-fails").write_text("")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)  # no record at all
        self.assertEqual(self.order()[-1], "freeze_judge_route.py")
        record = self.judge_record()
        self.assertEqual(record["outcome"], "skipped")
        self.assertIn("no judge route pricing record at most 72 h old (freeze_judge_route.py exit 1)", record["reason"])
        self.assertFalse((self.phase / "behavior-judge.exit").exists())
        self.assertTrue((self.phase / "completed-checksums.json").exists())

    def test_judge_refusal_or_failure_never_fails_the_run(self):
        self.pricing(1)
        (self.tmp / "judge-mode").write_text("refuse")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["exit"]), ("refused", 4))
        self.assertIn("cannot cover the central estimate", record["reason"])
        self.assertTrue((self.phase / "completed-checksums.json").exists())
        self.assertTrue((self.phase / "budget-final.sqlite").exists())
        status = json.loads((self.phase / "supervisor-status.json").read_text())
        self.assertEqual(status["stage"], "done_vm_left_running")

    def test_judge_crash_never_fails_the_run(self):
        self.pricing(1)
        (self.tmp / "judge-mode").write_text("crash")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual((self.judge_record()["outcome"], self.judge_record()["exit"]), ("failed", 1))
        self.assertTrue((self.phase / "completed-run.tgz").exists())

    def test_supervisor_error_in_the_judge_step_is_recorded(self):
        self.pricing(1)
        with mock.patch.object(supervise.Supervisor, "judge_fresh", side_effect=OSError("disk")):
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["error_type"]), ("supervisor_error", "OSError"))
        self.assertTrue((self.phase / "completed-checksums.json").exists())

    def test_skips(self):
        self.pricing(1)
        BudgetLedger(self.tmp / "state" / "ablation-budget.sqlite", ABLATION_CAPS)  # the ablation case's own ledger
        cases = (({"budget_track": "ablation"}, (), "invalid standing approval"),
                 ({"behavior_judge_max_usd": 60}, (), "invalid standing approval"),
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
        self.pricing(1)
        (self.tmp / "research" / "analysis" / "timeline.py").write_text("raise SystemExit(7)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 7)
        record = self.judge_record()
        self.assertEqual(record["outcome"], "skipped")
        self.assertIn("did not complete (exit 7)", record["reason"])
        self.assertNotIn("run_transcript_judge.py", self.order())

    def test_no_judge_without_the_config_keys(self):
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": True}))
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertFalse((self.phase / "behavior-judge.json").exists())
        self.assertNotIn("run_transcript_judge.py", self.order())

    def test_gemini_track_run_is_judged_on_the_baseline_ledger(self):
        """budget_track gemini (an owner track): the run reserves against state/gemini-budget.sqlite,
        the judge bills the analysis scope of the baseline state/budget.sqlite."""
        self.pricing(1)
        BudgetLedger(self.tmp / "state" / "gemini-budget.sqlite", GEMINI_CAPS)
        self.write_config(budget_track="gemini")
        supervisor = supervise.Supervisor(self.args("--no-shutdown"))
        self.assertEqual(supervisor.ledger, (self.tmp / "state" / "gemini-budget.sqlite").resolve())
        self.assertEqual(supervisor.main(), 0)
        lines = (self.tmp / "order.log").read_text().splitlines()
        self.assertTrue(lines[-1].startswith("run_transcript_judge.py"))
        self.assertIn("--ledger " + str(self.tmp.resolve() / "state" / "budget.sqlite"), lines[-1])
        self.assertIn("--ledger " + str(supervisor.ledger), lines[0])  # reconcile dry run: the track ledger
        self.assertEqual(self.judge_record()["outcome"], "complete")
        self.assertEqual(read_snapshot(self.phase / "budget-final.sqlite")["pilot"]["cap_usd"], "462.38")

    # The inherited runs use the base config (no auto judge keys) and stub follow-ups.
    test_full_cycle_without_shutdown = None
    test_refuses_config_without_run_dir_or_disabled_launch = None
    test_failed_run_skips_follow_ups = None
    test_track_config_uses_its_own_ledger = None
    test_ledger_option_and_missing_track_ledger = None
    test_evaluation_deferred_while_comparators_run = None
    test_idle_shutdown_blocked_by_other_users_containers_or_sessions = None


if __name__ == "__main__":
    unittest.main()
