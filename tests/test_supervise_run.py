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
STUB_LIVE_PILOT = """import datetime, json, os, sys, time
config = json.load(open(sys.argv[sys.argv.index('--config') + 1]))
run = config['run_dir']
os.makedirs(run, exist_ok=True)
open(os.path.join(run, 'settings.json'), 'w').write('{}')
def record(kind):
    open(os.path.join(run, 'runtime.jsonl'), 'a').write(json.dumps({'kind': kind, 'timestamp': time.time()}) + '\\n')
record('common_start')
time.sleep(SECONDS)
open('stop-at', 'w').write(datetime.datetime.now(datetime.timezone.utc).isoformat())
record('run_stop')
time.sleep(0.4)
"""


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

    def lines(self, script="run_transcript_judge.py"):
        path = self.tmp / "order.log"
        return [line for line in path.read_text().splitlines() if line.startswith(script)] if path.exists() else []

    def follow_up_order(self):
        """The order of the sequential steps (the early judge runs beside the follow-ups)."""
        lines = (self.tmp / "order.log").read_text().splitlines() if (self.tmp / "order.log").exists() else []
        return [line.split()[0] for line in lines if "--stage verify" not in line and "--live" not in line]

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
        self.assertEqual(self.follow_up_order(), ["reconcile_ledger.py", "evaluate_run.py", "evaluate_run.py",
                                                  "timeline.py", "run_transcript_judge.py"])  # fresh record: no freeze
        judge_line = (self.tmp / "order.log").read_text().splitlines()[-1]
        root = self.tmp.resolve()
        for text in ("--standing-approval-from-config " + str(self.config.resolve()),
                     "--ledger " + str(root / "state" / "budget.sqlite"), "--credentials-root " + str(root),
                     "--run-dir " + str(root / "runs" / "run-x")):
            self.assertIn(text, judge_line)
        self.assertNotIn("--i-have-user-approval", judge_line)
        self.assertNotIn("--stage", judge_line)   # the post-run judge finishes everything (a resume)
        # Live judging is on by default: the model stages started when the pilot exited, beside the follow-ups.
        early = [line for line in self.lines() if "--stage verify" in line]
        self.assertEqual(len(early), 1)
        self.assertIn("--standing-approval-from-config " + str(self.config.resolve()), early[0])
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["exit"]), ("complete", 0))
        self.assertEqual((record["early"]["outcome"], record["early"]["exit"]), ("complete", 0))
        self.assertEqual(record["live"]["mode"], "paid")
        self.assertEqual((self.phase / "behavior-judge.exit").read_text().strip(), "0")
        self.assertEqual((self.phase / "behavior-judge-early.exit").read_text().strip(), "0")
        phase, run = self.packaged()
        self.assertTrue({"behavior-judge.json", "behavior-judge.log", "behavior-judge.exit", "live-judge.json",
                         "behavior-judge-early.log", "behavior-judge-early.exit"} <= phase)
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
        # The early model stages had already started with the pilot's exit; the post-run judge is skipped.
        self.assertEqual([("--stage verify" in line) for line in self.lines()], [True])

    def test_without_live_judging_the_post_run_judge_runs_alone(self):
        self.pricing(1)
        self.write_config(live_behavior_judge=False)
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual(self.order(), ["reconcile_ledger.py", "evaluate_run.py", "evaluate_run.py", "timeline.py",
                                        "run_transcript_judge.py"])
        record = self.judge_record()
        self.assertNotIn("early", record)
        self.assertNotIn("live", record)
        self.assertFalse((self.phase / "live-judge.json").exists())
        (self.tmp / "order.log").unlink()
        self.phase = self.tmp / "runs" / "phase-b" / "run-y"
        self.phase.mkdir(parents=True)
        self.config = self.phase / "launch-config.json"
        self.write_config(live_behavior_judge=False)
        (self.tmp / "research" / "analysis" / "timeline.py").write_text("raise SystemExit(7)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 7)
        self.assertNotIn("run_transcript_judge.py", self.order())

    def test_no_judge_without_the_config_keys(self):
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "paid_launch_enabled": True}))
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertFalse((self.phase / "behavior-judge.json").exists())
        self.assertNotIn("run_transcript_judge.py", self.order())

    # -- live passes while the pilot runs ------------------------------------------------------
    def live_pilot(self, seconds=1.5):
        """A stub pilot that marks the common start, runs ``seconds`` and then the run stop."""
        (self.tmp / "scripts" / "run_pilot.py").write_text(STUB_LIVE_PILOT.replace("SECONDS", str(seconds)))

    def live_run(self, *extra):
        settings = {"mode": "paid", "max_usd": "10", "interval_seconds": 0}
        if "mock" in extra:
            settings["mode"] = "mock"
        with mock.patch("research_swarm.auto_judge.live_settings", return_value=settings):
            return supervise.Supervisor(self.args("--no-shutdown", "--poll-seconds", "0.1")).main()

    def test_live_passes_run_while_the_pilot_runs(self):
        self.pricing(1)
        self.live_pilot()
        self.assertEqual(self.live_run(), 0)
        passes = [line for line in self.lines() if "--live" in line]
        self.assertGreaterEqual(len(passes), 1)
        root = self.tmp.resolve()
        for text in ("--standing-approval-from-config " + str(self.config.resolve()),
                     "--ledger " + str(root / "state" / "budget.sqlite"), "--credentials-root " + str(root)):
            self.assertIn(text, passes[0])
        record = json.loads((self.phase / "live-judge.json").read_text())
        self.assertEqual(record["mode"], "paid")
        self.assertIsNone(record["stopped"])
        self.assertTrue(record["ticks"] and all(t.get("exit") == 0 for t in record["ticks"]))
        self.assertIn("=== live pass 1", (self.phase / "live-judge.log").read_text())
        import datetime
        stop = datetime.datetime.fromisoformat((self.tmp / "stop-at").read_text().strip())
        late = [t for t in record["ticks"]   # a pass starts only while the run stop is not recorded
                if datetime.datetime.fromisoformat(t["started_utc"]) > stop + datetime.timedelta(seconds=0.15)]
        self.assertEqual(late, [], (record["ticks"], stop))
        self.assertEqual(self.follow_up_order()[-1], "run_transcript_judge.py")
        phase, _ = self.packaged()
        self.assertTrue({"live-judge.json", "live-judge.log"} <= phase)

    def test_a_refused_live_pass_ends_the_live_passes(self):
        self.pricing(1)
        self.live_pilot(seconds=2.0)
        (self.tmp / "judge-mode").write_text("refuse")
        self.assertEqual(self.live_run(), 0)
        record = json.loads((self.phase / "live-judge.json").read_text())
        self.assertEqual(len(record["ticks"]), 1)
        self.assertIn("refused", record["stopped"])
        self.assertEqual(self.judge_record()["outcome"], "refused")   # the post-run judge still ran

    def test_mock_shadow_beside_the_paid_post_run_judge(self):
        self.pricing(1)
        self.live_pilot()
        self.write_config(live_behavior_judge="mock")
        self.assertEqual(self.live_run("mock"), 0)
        shadow = str((self.phase / supervise.SHADOW_DIR / "run-x" / "analysis" / "judge").resolve())
        passes = [line for line in self.lines() if "--live" in line]
        self.assertTrue(passes)
        self.assertTrue(all("--mock" in line and shadow in line and "--standing-approval" not in line
                            for line in passes))
        post = [line for line in self.lines() if "--live" not in line]
        self.assertEqual(len(post), 2)
        mocked = [line for line in post if "--mock" in line]
        self.assertEqual(len(mocked), 1)
        self.assertIn(shadow, mocked[0])
        self.assertIn("--standing-approval-from-config", post[-1])   # the paid post-run judge, unchanged
        self.assertNotIn("--stage", post[-1])
        record = self.judge_record()
        self.assertEqual((record["early"]["name"], record["early"]["exit"]), ("behavior-judge-shadow", 0))

    # The inherited runs use the base config (no auto judge keys) and stub follow-ups.
    test_full_cycle_without_shutdown = None
    test_refuses_config_without_run_dir_or_disabled_launch = None
    test_failed_run_skips_follow_ups = None


if __name__ == "__main__":
    unittest.main()
