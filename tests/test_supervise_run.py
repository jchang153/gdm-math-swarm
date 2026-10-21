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
if mode == 'defer' and '--live' in sys.argv:
    raise SystemExit(5)   # run_transcript_judge.py DEFERRED: nothing sent, try again next pass
"""
SLOW_EARLY_JUDGE = STUB_JUDGE + """import time
if '--stage' in sys.argv:          # the early model stages: slow, and they spend
    open(os.path.join(root, 'early-nice'), 'w').write(str(os.nice(0)))
    time.sleep(4)
    open(os.path.join(root, 'early-finished'), 'w').write('done')
"""
JUDGE_CONFIG = {"auto_behavior_judge": True, "behavior_judge_max_usd": 50}
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
    owner's runs (October 20, 2026)", "Live behavior judge during the run (October 20, 2026)" and "One
    per-run budget, no manual approvals (October 20, 2026)") with a fake judge: no paid call."""

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
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual(self.follow_up_order(), ["reconcile_ledger.py", "evaluate_run.py", "evaluate_run.py",
                                                  "timeline.py", "run_transcript_judge.py"])  # the judge refreshes pricing itself
        judge_line = (self.tmp / "order.log").read_text().splitlines()[-1]
        root = self.tmp.resolve()
        for text in ("--run-config " + str(self.config.resolve()),
                     "--ledger " + str(root / "state" / "budget.sqlite"), "--credentials-root " + str(root),
                     "--run-dir " + str(root / "runs" / "run-x")):
            self.assertIn(text, judge_line)
        self.assertNotIn("--i-have-user-approval", judge_line)
        self.assertNotIn("standing-approval", judge_line)
        self.assertNotIn("freeze_judge_route.py", self.order())
        self.assertNotIn("--stage", judge_line)   # the post-run judge finishes everything (a resume)
        # Live judging is on by default: the model stages started when the pilot exited, beside the follow-ups.
        early = [line for line in self.lines() if "--stage verify" in line]
        self.assertEqual(len(early), 1)
        self.assertIn("--run-config " + str(self.config.resolve()), early[0])
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["exit"]), ("complete", 0))
        self.assertEqual((record["early"]["outcome"], record["early"]["exit"]), ("complete", 0))
        self.assertEqual(record["live"]["mode"], "paid")
        self.assertEqual((self.phase / "behavior-judge.exit").read_text().strip(), "0")
        self.assertEqual((self.phase / "behavior-judge-early.exit").read_text().strip(), "0")
        phase, run = self.packaged()
        self.assertTrue({"behavior-judge.json", "behavior-judge.log", "behavior-judge.exit", "live-judge.json",
                         "behavior-judge-early.log", "behavior-judge-early.exit"} <= phase)
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

        def step(supervisor, name, arguments, on_wait=None):
            if name == "behavior-judge":
                raise OSError("disk")
            return real_step(supervisor, name, arguments, on_wait)
        with mock.patch.object(supervise.Supervisor, "step", step):
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        record = self.judge_record()
        self.assertEqual((record["outcome"], record["error_type"]), ("supervisor_error", "OSError"))
        self.assertTrue((self.phase / "completed-checksums.json").exists())

    def test_skips(self):
        cases = (({"behavior_judge_max_usd": "abc"}, (), "invalid judge settings"),
                 ({"behavior_judge_max_usd": 0}, (), "invalid judge settings"),
                 ({"behavior_judge_live_max_usd": 60}, (), "invalid judge settings"),
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
        # The early model stages had already started with the pilot's exit; the post-run judge is skipped.
        self.assertEqual([("--stage verify" in line) for line in self.lines()], [True])

    def test_without_live_judging_the_post_run_judge_runs_alone(self):
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
        self.config.write_text(json.dumps({"run_dir": "runs/run-x"}))
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
        self.live_pilot()
        self.assertEqual(self.live_run(), 0)
        passes = [line for line in self.lines() if "--live" in line]
        self.assertGreaterEqual(len(passes), 1)
        root = self.tmp.resolve()
        for text in ("--run-config " + str(self.config.resolve()),
                     "--ledger " + str(root / "state" / "budget.sqlite"), "--credentials-root " + str(root),
                     "--haiku-concurrency 8"):
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
        self.live_pilot(seconds=2.0)
        (self.tmp / "judge-mode").write_text("refuse")
        self.assertEqual(self.live_run(), 0)
        record = json.loads((self.phase / "live-judge.json").read_text())
        self.assertEqual(len(record["ticks"]), 1)
        self.assertIn("refused", record["stopped"])
        self.assertEqual(self.judge_record()["outcome"], "refused")   # the post-run judge still ran

    def test_mock_shadow_beside_the_paid_post_run_judge(self):
        self.live_pilot()
        self.write_config(live_behavior_judge="mock")
        self.assertEqual(self.live_run("mock"), 0)
        shadow = str((self.phase / supervise.SHADOW_DIR / "run-x" / "analysis" / "judge").resolve())
        passes = [line for line in self.lines() if "--live" in line]
        self.assertTrue(passes)
        self.assertTrue(all("--mock" in line and shadow in line and "--run-config" not in line
                            for line in passes))
        self.assertTrue(all("--haiku-concurrency 8" in line for line in passes))
        post = [line for line in self.lines() if "--live" not in line]
        self.assertEqual(len(post), 3)
        early, paid, resume = post
        # The shadow rehearses the paid order: model stages beside the follow-ups, the resume after them.
        for line in (early, resume):
            self.assertIn("--mock", line)
            self.assertIn(shadow, line)
            self.assertIn("--prefilter-window-count off", line)
            self.assertNotIn("--run-config", line)
        self.assertIn("--stage verify", early)
        self.assertNotIn("--stage", resume)
        self.assertIn("--run-config", paid)   # the paid post-run judge, unchanged
        self.assertNotIn("--stage", paid)
        self.assertNotIn("--prefilter-window-count", paid)
        order = self.follow_up_order()
        self.assertLess(order.index("timeline.py"), len(order) - 2)   # paid judge, then the shadow resume
        self.assertEqual(order[-2:], ["run_transcript_judge.py", "run_transcript_judge.py"])
        record = self.judge_record()
        self.assertEqual((record["early"]["name"], record["early"]["exit"]), ("behavior-judge-shadow", 0))
        self.assertEqual((record["shadow_resume"]["exit"], record["shadow_resume"]["outcome"]), (0, "complete"))
        self.assertTrue((self.phase / "behavior-judge-shadow-resume.exit").exists())
        self.assertIn("behavior-judge-shadow-resume.log", self.packaged()[0])

    # -- review findings (October 20, 2026) ------------------------------------------------------------
    def test_failed_follow_up_stops_the_early_stages(self):
        """Before live judging a failed follow-up meant no judge spend; the early stages, which no resume
        will finish, are stopped instead of running to completion."""
        (self.tmp / "scripts" / "run_transcript_judge.py").write_text(SLOW_EARLY_JUDGE)
        (self.tmp / "research" / "analysis" / "timeline.py").write_text("raise SystemExit(7)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 7)
        record = self.judge_record()
        self.assertEqual(record["outcome"], "skipped")
        self.assertFalse((self.tmp / "early-finished").exists())
        self.assertIn("did not complete (exit 7)", record["early"]["stopped"])
        self.assertNotEqual(record["early"]["exit"], 0)
        self.assertTrue((self.phase / "behavior-judge-early.exit").exists())

    def test_early_stages_are_reaped_when_a_follow_up_step_raises(self):
        (self.tmp / "scripts" / "run_transcript_judge.py").write_text(SLOW_EARLY_JUDGE)
        sup = supervise.Supervisor(self.args("--no-shutdown"))
        with mock.patch.object(supervise.Supervisor, "follow_ups", side_effect=OSError("No space left on device")):
            self.assertEqual(sup.main(), 2)
        self.assertIsNone((sup.early or {}).get("proc"))   # waited for and recorded, never left running
        self.assertIn("without resuming the judge", sup.early["stopped"])
        self.assertTrue((self.phase / "behavior-judge-early.exit").exists())
        self.assertFalse((self.tmp / "early-finished").exists())
        self.assertEqual(json.loads((self.phase / "worker.exit").read_text()), 2)

    def test_early_stages_run_at_low_priority_with_few_history_workers(self):
        (self.tmp / "scripts" / "run_transcript_judge.py").write_text(SLOW_EARLY_JUDGE)
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual((self.tmp / "early-nice").read_text(), "19")
        early = [line for line in self.lines() if "--stage verify" in line]
        self.assertIn(f"--history-workers {supervise.EARLY_HISTORY_WORKERS}", early[0])
        record = self.judge_record()
        self.assertEqual((record["early"]["exit"], record["early"]["low_priority"]), (0, True))
        self.assertEqual(json.loads((self.phase / "live-judge.json").read_text())["isolation"]["nice"], 19)

    def test_memory_scope_is_used_only_where_memory_max_applies(self):
        sup = supervise.Supervisor(self.args("--no-shutdown"))
        limit = str(8 << 30)
        for stdout, expected in (("max\n", False), (limit + "\n", True)):
            sup._scope_ok = None
            done = supervise.subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
            with mock.patch.object(supervise.shutil, "which", return_value="/usr/bin/systemd-run"), \
                    mock.patch.object(supervise.subprocess, "run", return_value=done) as run:
                self.assertIs(sup.scope_ok(), expected, sup.scope_detail)
                self.assertIn("MemoryMax=8G", run.call_args[0][0])
                prefix = sup.live_prefix()
            self.assertEqual("systemd-run" in prefix, expected)
        self.assertEqual(supervise._memory_bytes("512M"), 512 << 20)

    def test_a_deferred_live_pass_never_ends_live_judging(self):
        self.live_pilot(seconds=2.0)
        (self.tmp / "judge-mode").write_text("defer")
        self.assertEqual(self.live_run(), 0)
        record = json.loads((self.phase / "live-judge.json").read_text())
        self.assertIsNone(record["stopped"])
        self.assertGreater(record["deferred"], supervise.LIVE_MAX_FAILURES)
        self.assertTrue(all(t.get("deferred") for t in record["ticks"] if t.get("exit") == 5))
        self.assertEqual(record["failures"], 0)

    def test_live_pass_is_drained_after_the_pilot_exits(self):
        """A pass still running after the pilot exits gets SIGTERM and time to settle its in-flight requests
        (it exits 3), instead of being killed with its holds left reserved."""
        (self.tmp / "scripts" / "run_transcript_judge.py").write_text(STUB_ORDER + """import signal, time
if '--live' in sys.argv:
    def stop(signum, frame):
        time.sleep(1.0)   # the in-flight requests settle
        raise SystemExit(3)
    signal.signal(signal.SIGTERM, stop)
    time.sleep(30)
""")
        self.live_pilot(seconds=0.5)
        with mock.patch.object(supervise, "LIVE_STOP_WAIT_SECONDS", 0.5):
            self.assertEqual(self.live_run(), 0)
        record = json.loads((self.phase / "live-judge.json").read_text())
        last = record["ticks"][-1]
        self.assertEqual((last["exit"], last.get("terminated"), last.get("killed")), (3, True, None))

    # The inherited runs use the base config (no auto judge keys) and stub follow-ups.
    test_full_cycle_without_shutdown = None
    test_refuses_config_without_run_dir = None
    test_failed_run_skips_follow_ups = None
    test_a_failed_reconciliation_never_stops_the_other_follow_ups = None
    test_missing_ledger_is_not_an_error = None


if __name__ == "__main__":
    unittest.main()
