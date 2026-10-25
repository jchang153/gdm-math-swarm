"""Offline tests of scripts/supervise_run.py: a stub checkout, no paid call, no shutdown."""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_swarm.budget import BudgetLedger, read_snapshot  # noqa: E402

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
        # No Docker in these tests: no comparator container of another evaluation is running.
        patcher = mock.patch.object(supervise.Supervisor, "comparators_running", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
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
        import contextlib
        import io
        import tarfile
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            code = supervise.Supervisor(self.args("--no-shutdown")).main()
        self.assertEqual(code, 0)
        # The run's budget is printed on the supervisor's own console before the launch and recorded.
        self.assertIn("Run budget: $40 (config run_budget_usd)", console.getvalue())
        launch = json.loads((self.phase / "launch-budget.json").read_text())
        self.assertEqual((launch["run_budget_usd"], launch["run_budget_source"]), ("40", "config run_budget_usd"))
        self.assertNotIn("judge_budget_usd", launch)   # no behavior judge in this config
        with tarfile.open(self.phase / "completed-phase.tgz") as tar:
            self.assertIn("launch-budget.json", tar.getnames())
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

    def test_teardown_cancel_after_normal_stop_still_runs_follow_ups(self):
        # Inspect's exit 130 after a stop at max_run_seconds and stop_grace_exceeded: every sample is logged.
        stop = ("import json\nopen(run + '/runtime.jsonl', 'a').write(''.join(json.dumps(e) + '\\n' for e in ["
                "{'kind': 'run_stop', 'payload': {'reason': 'max_run_seconds'}}, "
                "{'kind': 'evaluation_cancelled', 'payload': {'reason': 'stop_grace_exceeded'}}]))\n")
        (self.tmp / "scripts" / "run_pilot.py").write_text(STUB_PILOT + stop + "raise SystemExit(130)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertEqual((self.phase / "pilot.exit").read_text().strip(), "130")
        self.assertEqual((self.phase / "evaluate.exit").read_text().strip(), "0")

    def test_other_interrupt_skips_follow_ups(self):
        (self.tmp / "scripts" / "run_pilot.py").write_text(STUB_PILOT + "raise SystemExit(130)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 130)
        self.assertFalse((self.phase / "evaluate.exit").exists())

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

    def gemini_ledger(self):
        ledger = BudgetLedger(self.tmp / "state" / "gemini-budget.sqlite")
        ledger.start_run("compat-x", "compatibility", "400")
        ledger.settle(ledger.reserve("compatibility", "agent-001", "0.5"), "0.25", "provider_reported_cost")
        return ledger

    def test_track_config_uses_its_own_ledger(self):
        """budget_track gemini: backups, checkpoints and the reconcile dry run use state/gemini-budget.sqlite."""
        self.gemini_ledger()
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "budget_track": "gemini",
                                           "run_budget_usd": "400"}))
        supervisor = supervise.Supervisor(self.args("--no-shutdown"))
        self.assertEqual(supervisor.ledger, (self.tmp / "state" / "gemini-budget.sqlite").resolve())
        self.assertEqual(supervisor.main(), 0)
        for name in ("budget-before-launch.sqlite", "budget-after-pilot.sqlite", "budget-final.sqlite"):
            backup = read_snapshot(self.phase / name)
            self.assertEqual(backup["compatibility"]["settled_usd"], "0.25")   # the track ledger, not the baseline
        checkpoint = json.loads((self.phase / "live-checkpoint.json").read_text())
        self.assertEqual(checkpoint["ledger"], str(supervisor.ledger))
        self.assertEqual(checkpoint["budget"]["compatibility"]["settled_usd"], "0.25")
        launch = json.loads((self.phase / "launch-budget.json").read_text())
        self.assertIn("$400", " ".join(launch["lines"]))
        ran = (self.tmp / "scripts" / "reconcile_ledger.py.ran").read_text()
        self.assertIn("--ledger " + str(supervisor.ledger), ran)

    def test_ledger_option_and_missing_track_ledger(self):
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "budget_track": "gemini"}))
        # A missing track ledger is no refusal (the run creates it at its first request).
        self.assertEqual(supervise.Supervisor(self.args()).ledger,
                         (self.tmp / "state" / "gemini-budget.sqlite").resolve())
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
        self.config.write_text(json.dumps({"run_dir": "runs/run-x"}))
        self.assertEqual(supervise.Supervisor(self.args("--ledger", str(baseline))).ledger, baseline.resolve())
        with self.assertRaises(SystemExit):  # nor the other way round
            supervise.Supervisor(self.args("--ledger", str(own)))
        self.config.write_text(json.dumps({"run_dir": "runs/run-x", "budget_track": "gemini-typo"}))
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
# The full (rubric-3.1) judge, opt-in since the owner's decision of October 24, 2026 (the simplified
# judge is the default: SimpleBehaviorJudgeTests).
JUDGE_CONFIG = {"auto_behavior_judge": True, "behavior_judge": "full", "behavior_judge_max_usd": 50}
# scripts/run_simple_judge.py: logs to order.log, writes RUN/analysis/simple_judge/ and names a route record.
STUB_SIMPLE_JUDGE = STUB_ORDER + """assert 'OPENROUTER_API_KEY' not in os.environ, 'provider key leaked to the judge child'
run = sys.argv[sys.argv.index('--run-dir') + 1]
mode = open(os.path.join(root, 'judge-mode')).read().strip() if os.path.exists(os.path.join(root, 'judge-mode')) else 'ok'
if mode == 'refuse':
    print('refused: no room under this run budget', file=sys.stderr)
    raise SystemExit(2)
if mode == 'crash':
    raise RuntimeError('simple judge crashed')
out = os.path.join(run, 'analysis', 'simple_judge')
os.makedirs(out, exist_ok=True)
route = os.path.join(root, 'data', 'validation', 'simple-judge-route-x.json')
os.makedirs(os.path.dirname(route), exist_ok=True)
open(route, 'w').write('{}')
open(os.path.join(out, 'report.json'), 'w').write('{"route_record": "%s"}' % route)
open(os.path.join(out, 'agents.jsonl'), 'w').write('')
"""
# The post-run incident report (docs/plan.md, "Incident report per run (October 21, 2026)"): a fake
# that logs to incident.log (not order.log, so the judge's step order checks stay unchanged).
STUB_INCIDENT = """import json, os, sys
root = os.getcwd()
assert 'OPENROUTER_API_KEY' not in os.environ, 'provider key leaked to the incident child'
open(os.path.join(root, 'incident.log'), 'a').write(' '.join(sys.argv[1:]) + '\\n')
mode = open(os.path.join(root, 'incident-mode')).read().strip() if os.path.exists(os.path.join(root, 'incident-mode')) else 'ok'
if mode == 'crash':
    raise RuntimeError('incident report crashed')
run = sys.argv[sys.argv.index('--run-dir') + 1]
os.makedirs(os.path.join(run, 'analysis', 'incident'), exist_ok=True)
open(os.path.join(run, 'analysis', 'incident', 'report.html'), 'w').write('<p>report</p>')
status = 'stopped' if mode == 'stop' else 'complete'
open(os.path.join(run, 'analysis', 'incident', 'state.json'), 'w').write(json.dumps(
    {'status': status, 'budget': {'settled_usd': '1.50'}}))
raise SystemExit(3 if mode == 'stop' else 0)
"""
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
        (self.tmp / "scripts" / "run_simple_judge.py").write_text(STUB_SIMPLE_JUDGE)
        (self.tmp / "scripts" / "run_incident_report.py").write_text(STUB_INCIDENT)
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
        # The pricing record a judge invocation used (it keeps a copy in RUN/analysis/judge/route-pricing/).
        kept = self.tmp / "runs" / "run-x" / "analysis" / "judge" / "route-pricing" / "judge-route-pricing-x.json"
        kept.parent.mkdir(parents=True)
        kept.write_text("{}")
        import contextlib
        import io
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertIn("Behavior judge budget (full judge): $", console.getvalue())
        self.assertIn("judge_budget_usd", json.loads((self.phase / "launch-budget.json").read_text()))
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
        self.assertIn("judge-route-pricing/judge-route-pricing-x.json", phase)
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
                 ({"behavior_judge": "rubric"}, (), "invalid judge settings"),
                 ({"behavior_judge_max_usd": 0}, (), "invalid judge settings"),
                 ({"live_behavior_judge": "yes"}, (), "invalid judge settings"),
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
        self.assertFalse((self.phase / "incident-report.json").exists())
        self.assertFalse((self.tmp / "incident.log").exists())

    # -- the post-run incident report ------------------------------------------------------------
    def incident_record(self):
        return json.loads((self.phase / "incident-report.json").read_text())

    def test_incident_report_runs_after_the_judge_with_its_own_budget_and_is_packaged(self):
        import contextlib
        import io
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertIn("Incident report budget: $20 (default", console.getvalue())
        line = (self.tmp / "incident.log").read_text().splitlines()
        self.assertEqual(len(line), 1)
        root = self.tmp.resolve()
        for text in ("--run-dir " + str(root / "runs" / "run-x"), "--run-limit 20",
                     "--ledger " + str(root / "state" / "budget.sqlite"), "--credentials-root " + str(root)):
            self.assertIn(text, line[0])
        record = self.incident_record()
        self.assertEqual((record["outcome"], record["exit"], record["budget_usd"]), ("complete", 0, "20"))
        self.assertEqual(record["spent_usd"], "1.50")
        judged = json.loads((self.phase / "behavior-judge.json").read_text())["finished_utc"]
        self.assertLessEqual(judged, record["utc"])          # after the behavior judge
        phase, run = self.packaged()
        self.assertTrue({"incident-report.json", "incident-report.log", "incident-report.exit"} <= phase)
        self.assertIn("runs/run-x/analysis/incident/report.html", run)

    def test_incident_report_budget_from_the_config(self):
        self.write_config(incident_report_max_usd=7.5)
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0)
        self.assertIn("--run-limit 7.5", (self.tmp / "incident.log").read_text())
        self.assertEqual(self.incident_record()["budget_source"], "config incident_report_max_usd")

    def test_incident_report_failure_or_stop_never_fails_the_run(self):
        for n, mode in enumerate(("crash", "stop")):
            self.phase = self.tmp / "runs" / "phase-b" / f"run-{n}"
            self.phase.mkdir(parents=True)
            self.config = self.phase / "launch-config.json"
            self.write_config()
            (self.tmp / "incident-mode").write_text(mode)
            self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 0, mode)
            record = self.incident_record()
            self.assertEqual(record["outcome"], {"crash": "failed", "stop": "stopped"}[mode])
            self.assertTrue((self.phase / "completed-checksums.json").exists())
            self.assertEqual(json.loads((self.phase / "worker.exit").read_text()), 0)

    def test_incident_report_is_skipped_when_the_run_did_not_complete(self):
        (self.tmp / "research" / "analysis" / "timeline.py").write_text("raise SystemExit(7)\n")
        self.assertEqual(supervise.Supervisor(self.args("--no-shutdown")).main(), 7)
        record = self.incident_record()
        self.assertEqual(record["outcome"], "skipped")
        self.assertFalse((self.tmp / "incident.log").exists())

    def test_invalid_incident_budget_is_an_invalid_judge_setting(self):
        from research_swarm.auto_judge import judge_config_problems
        self.assertTrue(judge_config_problems({"auto_behavior_judge": True, "incident_report_max_usd": 0}))
        self.assertEqual(judge_config_problems({"auto_behavior_judge": True, "incident_report_max_usd": 3}), [])

    def test_gemini_track_run_is_judged_on_the_baseline_ledger(self):
        """budget_track gemini: the run reserves against state/gemini-budget.sqlite (its own per-run
        budget), the judge bills the baseline state/budget.sqlite with its own per-run budget and reads
        the pilot's run from the track ledger (--pilot-ledger) for its key-headroom deferral."""
        self.write_config(budget_track="gemini")
        supervisor = supervise.Supervisor(self.args("--no-shutdown"))
        self.assertEqual(supervisor.ledger, (self.tmp / "state" / "gemini-budget.sqlite").resolve())
        self.assertEqual(supervisor.main(), 0)
        lines = (self.tmp / "order.log").read_text().splitlines()
        judge = [line for line in lines if line.startswith("run_transcript_judge.py")]
        self.assertTrue(judge)
        self.assertIn("--ledger " + str(self.tmp.resolve() / "state" / "budget.sqlite"), judge[-1])
        self.assertIn("--pilot-ledger " + str(supervisor.ledger), judge[-1])
        reconcile = [line for line in lines if line.startswith("reconcile_ledger.py")]
        self.assertTrue(reconcile and "--ledger " + str(supervisor.ledger) in reconcile[0])  # the track ledger
        self.assertEqual(self.judge_record()["outcome"], "complete")

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
    test_track_config_uses_its_own_ledger = None
    test_ledger_option_and_missing_track_ledger = None
    test_evaluation_deferred_while_comparators_run = None
    test_idle_shutdown_blocked_by_other_users_containers_or_sessions = None
    test_a_failed_reconciliation_never_stops_the_other_follow_ups = None
    test_missing_ledger_is_not_an_error = None



class SimpleBehaviorJudgeTests(SuperviseRunTests):
    """Owner decision (October 24, 2026): the simplified one-pass judge (scripts/run_simple_judge.py) is
    the default automatic behavior judge; the rubric-3.1 judge is opt-in (``behavior_judge: "full"``);
    ``"off"`` runs no judge but still the incident report. Fake judges: no paid call."""

    def setUp(self):
        super().setUp()
        for name in ("scripts/reconcile_ledger.py", "scripts/evaluate_run.py", "research/analysis/timeline.py"):
            (self.tmp / name).write_text(STUB_ORDER)
        for name, stub in (("run_transcript_judge.py", STUB_JUDGE), ("run_simple_judge.py", STUB_SIMPLE_JUDGE),
                           ("run_incident_report.py", STUB_INCIDENT)):
            (self.tmp / "scripts" / name).write_text(stub)
        self.write_config()

    order = AutoBehaviorJudgeTests.order
    lines = AutoBehaviorJudgeTests.lines
    judge_record = AutoBehaviorJudgeTests.judge_record
    incident_record = AutoBehaviorJudgeTests.incident_record
    packaged = AutoBehaviorJudgeTests.packaged

    def write_config(self, **changes):  # no behavior_judge key: the default
        config = dict({"auto_behavior_judge": True}, run_dir="runs/run-x", **changes)
        self.config.write_text(json.dumps({k: v for k, v in config.items() if v is not None}))

    def run_supervisor(self, *extra):
        import contextlib
        import io
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            code = supervise.Supervisor(self.args("--no-shutdown", "--poll-seconds", "0.1", *extra)).main()
        return code, console.getvalue()

    def test_auto_judge_config_without_behavior_judge_gets_the_simple_judge(self):
        code, console = self.run_supervisor()
        self.assertEqual(code, 0)
        self.assertIn("Behavior judge budget (simple judge): $10 (default; set simple_judge_max_usd", console)
        self.assertIn("Incident report budget: $20", console)
        budget = json.loads((self.phase / "launch-budget.json").read_text())
        self.assertEqual((budget["judge"], budget["judge_budget_usd"]), ("simple", "10"))
        self.assertEqual(self.order(), ["reconcile_ledger.py", "evaluate_run.py", "evaluate_run.py", "timeline.py",
                                        "run_simple_judge.py"])
        self.assertEqual(self.lines(), [])   # no live pass, no early stages, no full judge
        line = self.lines("run_simple_judge.py")[0]
        root = self.tmp.resolve()
        for text in ("--run-dir " + str(root / "runs" / "run-x"), "--run-limit 10",
                     "--ledger " + str(root / "state" / "budget.sqlite"), "--credentials-root " + str(root)):
            self.assertIn(text, line)
        self.assertNotIn("--output-dir", line)   # its default, RUN/analysis/simple_judge
        self.assertFalse((self.phase / "live-judge.json").exists())
        record = self.judge_record()
        self.assertEqual((record["judge"], record["outcome"], record["exit"], record["budget_usd"],
                          record["budget_source"], record["budget_key"]),
                         ("simple", "complete", 0, "10", "default", "simple_judge_max_usd"))
        self.assertNotIn("live", record)
        self.assertNotIn("early", record)
        self.assertEqual(self.incident_record()["outcome"], "complete")   # after the simple judge
        self.assertLessEqual(record["finished_utc"], self.incident_record()["utc"])
        phase, run = self.packaged()
        self.assertIn("runs/run-x/analysis/simple_judge/report.json", run)
        self.assertIn("simple-judge-route/simple-judge-route-x.json", phase)
        self.assertTrue({"behavior-judge.json", "behavior-judge.log", "behavior-judge.exit"} <= phase)

    def test_simple_judge_budget_and_explicit_mode(self):
        self.write_config(behavior_judge="simple", simple_judge_max_usd=3.5, behavior_judge_max_usd=80)
        code, console = self.run_supervisor()
        self.assertEqual(code, 0)
        self.assertIn("Behavior judge budget (simple judge): $3.5 (config simple_judge_max_usd)", console)
        self.assertIn("--run-limit 3.5", self.lines("run_simple_judge.py")[0])
        self.assertEqual(self.judge_record()["budget_source"], "config simple_judge_max_usd")

    def test_simple_judge_never_starts_live_early_or_shadow_judging(self):
        """Even with live_behavior_judge true or "mock", the simple judge has no live passes."""
        (self.tmp / "scripts" / "run_pilot.py").write_text(STUB_LIVE_PILOT.replace("SECONDS", "1.0"))
        for n, live in enumerate((True, "mock")):
            self.phase = self.tmp / "runs" / "phase-b" / f"run-{n}"
            self.phase.mkdir(parents=True)
            self.config = self.phase / "launch-config.json"
            self.write_config(live_behavior_judge=live, behavior_judge_live_interval_seconds=60)
            self.assertEqual(self.run_supervisor()[0], 0)
            self.assertEqual(self.lines(), [], live)
            self.assertFalse((self.phase / "live-judge.json").exists())
            self.assertFalse((self.phase / supervise.SHADOW_DIR).exists())
            self.assertEqual(self.judge_record()["judge"], "simple")

    def test_full_judge_is_opt_in(self):
        self.write_config(behavior_judge="full", live_behavior_judge=False)
        code, console = self.run_supervisor()
        self.assertEqual(code, 0)
        self.assertIn("Behavior judge budget (full judge): $50 (default; set behavior_judge_max_usd", console)
        self.assertEqual(self.order()[-1], "run_transcript_judge.py")
        self.assertNotIn("run_simple_judge.py", self.order())
        record = self.judge_record()
        self.assertEqual((record["judge"], record["budget_usd"], record["outcome"]), ("full", "50", "complete"))

    def test_off_runs_no_judge_but_the_incident_report(self):
        self.write_config(behavior_judge="off")
        code, console = self.run_supervisor()
        self.assertEqual(code, 0)
        self.assertIn('Behavior judge: off', console)
        self.assertIn("Incident report budget: $20", console)
        self.assertEqual(self.order(), ["reconcile_ledger.py", "evaluate_run.py", "evaluate_run.py", "timeline.py"])
        record = self.judge_record()
        self.assertEqual((record["judge"], record["outcome"], record["budget_usd"]), ("off", "skipped", None))
        self.assertIn('"off"', record["reason"])
        self.assertEqual(self.incident_record()["outcome"], "complete")
        self.assertEqual(len((self.tmp / "incident.log").read_text().splitlines()), 1)

    def test_auto_incident_report_key(self):
        cases = (({"auto_behavior_judge": None, "auto_incident_report": True}, True, False),
                 ({"auto_incident_report": False}, False, True),
                 ({"auto_behavior_judge": False}, False, False))
        for n, (changes, report, judged) in enumerate(cases):
            self.phase = self.tmp / "runs" / "phase-b" / f"run-{n}"
            self.phase.mkdir(parents=True)
            self.config = self.phase / "launch-config.json"
            self.write_config(**changes)
            (self.tmp / "incident.log").unlink(missing_ok=True)
            (self.tmp / "order.log").unlink(missing_ok=True)
            self.assertEqual(self.run_supervisor()[0], 0, changes)
            self.assertEqual((self.phase / "incident-report.json").exists(), report, changes)
            self.assertEqual((self.tmp / "incident.log").exists(), report, changes)
            self.assertEqual((self.phase / "behavior-judge.json").exists(), judged, changes)
            self.assertEqual("run_simple_judge.py" in self.order(), judged, changes)

    def test_simple_judge_refusal_or_crash_never_fails_the_run(self):
        for n, (mode, outcome, code) in enumerate((("refuse", "refused_or_failed", 2), ("crash", "failed", 1))):
            self.phase = self.tmp / "runs" / "phase-b" / f"run-{n}"
            self.phase.mkdir(parents=True)
            self.config = self.phase / "launch-config.json"
            self.write_config()
            (self.tmp / "judge-mode").write_text(mode)
            self.assertEqual(self.run_supervisor()[0], 0, mode)
            record = self.judge_record()
            self.assertEqual((record["outcome"], record["exit"]), (outcome, code))
            if mode == "refuse":
                self.assertIn("refused:", record["reason"])
            self.assertEqual(self.incident_record()["outcome"], "complete")
            self.assertTrue((self.phase / "completed-checksums.json").exists())
            self.assertEqual(json.loads((self.phase / "worker.exit").read_text()), 0)

    def test_failed_follow_up_skips_the_simple_judge(self):
        (self.tmp / "research" / "analysis" / "timeline.py").write_text("raise SystemExit(7)\n")
        self.assertEqual(self.run_supervisor()[0], 7)
        record = self.judge_record()
        self.assertEqual((record["judge"], record["outcome"]), ("simple", "skipped"))
        self.assertNotIn("run_simple_judge.py", self.order())

    # The inherited runs use the base config and stub follow-ups.
    test_full_cycle_without_shutdown = None
    test_refuses_config_without_run_dir = None
    test_failed_run_skips_follow_ups = None
    test_track_config_uses_its_own_ledger = None
    test_ledger_option_and_missing_track_ledger = None
    test_evaluation_deferred_while_comparators_run = None
    test_idle_shutdown_blocked_by_other_users_containers_or_sessions = None
    test_a_failed_reconciliation_never_stops_the_other_follow_ups = None
    test_missing_ledger_is_not_an_error = None


class JudgeModeTests(unittest.TestCase):
    def test_resolution_budgets_and_validation(self):
        from research_swarm import auto_judge as aj
        self.assertEqual(aj.judge_mode({"auto_behavior_judge": True}), "simple")
        self.assertEqual(aj.judge_mode({"auto_behavior_judge": True, "behavior_judge": "full"}), "full")
        self.assertEqual(aj.judge_mode({"auto_behavior_judge": True, "behavior_judge": "off"}), "off")
        self.assertEqual(aj.judge_mode({"behavior_judge": "full"}), "off")   # auto_behavior_judge is the switch
        self.assertEqual(aj.judge_mode({}), "off")
        self.assertEqual(aj.active_judge_budget({"auto_behavior_judge": True}),
                         ("simple", Decimal("10"), "default", "simple_judge_max_usd"))
        self.assertEqual(aj.active_judge_budget({"auto_behavior_judge": True, "behavior_judge": "full"}),
                         ("full", Decimal("50"), "default", "behavior_judge_max_usd"))
        self.assertEqual(aj.active_judge_budget({"auto_behavior_judge": True, "behavior_judge": "off"}),
                         ("off", None, None, None))
        for config, mode in (({"auto_behavior_judge": True}, None),
                             ({"auto_behavior_judge": True, "behavior_judge": "simple"}, None),
                             ({"auto_behavior_judge": True, "behavior_judge": "off"}, None),
                             ({"auto_behavior_judge": True, "behavior_judge": "full"}, "paid"),
                             ({"auto_behavior_judge": True, "behavior_judge": "full", "live_behavior_judge": "mock"},
                              "mock")):
            self.assertEqual(aj.live_mode(config), mode, config)
        for bad in ({"behavior_judge": "rubric"}, {"behavior_judge": True}, {"simple_judge_max_usd": 0},
                    {"simple_judge_max_usd": "abc"}, {"auto_incident_report": "yes"}):
            self.assertTrue(aj.judge_config_problems(dict(bad, auto_behavior_judge=True)), bad)
        self.assertEqual(aj.judge_config_problems({"auto_behavior_judge": True, "behavior_judge": "simple",
                                                   "simple_judge_max_usd": "2.5", "auto_incident_report": False}), [])
        self.assertTrue(aj.incident_enabled({"auto_behavior_judge": True}))
        self.assertTrue(aj.incident_enabled({"auto_behavior_judge": True, "behavior_judge": "off"}))
        self.assertTrue(aj.incident_enabled({"auto_incident_report": True}))
        self.assertFalse(aj.incident_enabled({"auto_behavior_judge": True, "auto_incident_report": False}))
        self.assertFalse(aj.incident_enabled({}))


if __name__ == "__main__":
    unittest.main()
