"""Offline tests of the judge's run-config path (docs/plan.md, "One per-run budget, no manual approvals
(October 20, 2026)"): the judged run's config sets the judging run's budget; no approval, no prompt.
No model call, no credential, no real ledger."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ANALYSIS = Path(__file__).resolve().parents[1]
REPO = ANALYSIS.parents[1]
for path in (str(REPO), str(REPO / "src"), str(Path(__file__).resolve().parent), str(REPO / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research.analysis.judge.route import JudgeRouteSettings  # noqa: E402
import test_judge_estimate  # noqa: E402

JUDGE = {"auto_behavior_judge": True, "behavior_judge_max_usd": 50}


class Stop(Exception):
    pass


class RunConfigTests(unittest.TestCase):
    # The synthetic run and the CLI harness of test_judge_estimate.CommandLineTests.
    setUp = test_judge_estimate.CommandLineTests.setUp
    tearDown = test_judge_estimate.CommandLineTests.tearDown
    call = test_judge_estimate.CommandLineTests.call

    def config(self, **changes):
        path = self.tmp / "launch-config.json"
        path.write_text(json.dumps({k: v for k, v in dict(JUDGE, run_dir="runs/x", **changes).items()
                                    if v is not None}))
        return path

    def paid(self, *args):
        created = []

        class FakeClient:
            def __init__(self, key, ledger, **kwargs):
                created.append(kwargs)

            def prepare(self):
                raise Stop()

        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch.object(self.cli, "route_settings", return_value=JudgeRouteSettings.defaults(
                    pricing_verified=True)), \
                mock.patch("research.analysis.judge.client.JudgeClient", FakeClient), \
                mock.patch("builtins.input", side_effect=AssertionError("nothing may prompt")):
            try:
                code, _, err = self.call("--ledger", str(self.tmp / "ledger.sqlite"), "--output-dir",
                                         str(self.judge_dir), *args)
            except Stop:
                code, err = "prepared", ""
        return code, err, created

    def records(self):
        return json.loads((self.judge_dir / "run-budgets.json").read_text())

    def test_the_configs_budget_is_the_run_limit_without_any_prompt(self):
        code, err, created = self.paid("--run-config", str(self.config()))
        self.assertEqual(code, "prepared", err)  # reached the key loading without input(), a flag or a terminal
        self.assertEqual(len(created), 3)
        limit = created[0]["run_limit"]
        self.assertEqual((limit.limit, limit.source, limit.basis), (Decimal("50"), "run_config",
                                                                     "config behavior_judge_max_usd"))
        self.assertEqual({id(kwargs["run_limit"]) for kwargs in created}, {id(limit)})  # shared by the three clients
        [record] = self.records()
        self.assertEqual((record["limit_usd"], record["ledger_run_id"], record["run_config"]["config_path"]),
                         ("50", limit.run_id, str((self.tmp / "launch-config.json").resolve())))

    def test_any_positive_budget_any_track_no_approval_note(self):
        code, err, created = self.paid("--run-config", str(self.config(behavior_judge_max_usd=120,
                                                                       budget_track="ablation")))
        self.assertEqual(code, "prepared", err)
        limit = created[0]["run_limit"]
        self.assertEqual(limit.limit, Decimal("120"))  # never clamped to $50 or to an analysis cap
        self.assertEqual(limit.record()["run_config"]["track"], "ablation")
        code, err, created = self.paid("--run-config", str(self.config()), "--run-limit", "200")
        self.assertEqual((code, created[0]["run_limit"].limit, created[0]["run_limit"].source),
                         ("prepared", Decimal("200"), "override"))

    def test_default_budget_is_fifty_when_unset(self):
        code, err, created = self.paid("--run-config", str(self.config(behavior_judge_max_usd=None)))
        self.assertEqual(code, "prepared", err)
        self.assertEqual((created[0]["run_limit"].limit, created[0]["run_limit"].source), (Decimal("50"), "default"))
        code, err, created = self.paid()  # no config at all
        self.assertEqual((created[0]["run_limit"].limit, created[0]["run_limit"].source), (Decimal("50"), "default"))

    def test_invalid_configs_are_refused_before_anything(self):
        for changes, text in (({"behavior_judge_max_usd": "abc"}, "not an amount"),
                              ({"behavior_judge_max_usd": 0}, "positive"),
                              ({"auto_behavior_judge": "yes"}, "must be a boolean")):
            code, err, created = self.paid("--run-config", str(self.config(**changes)))
            self.assertEqual((code, created), (4, []), changes)
            self.assertIn(text, err)
        code, err, _ = self.paid("--run-config", str(self.tmp / "missing.json"))
        self.assertEqual(code, 4)
        code, _, err = self.call("--mock", "--run-config", str(self.config()), "--output-dir", str(self.judge_dir))
        self.assertEqual(code, 4)
        self.assertIn("paid judging run only", err)

    def test_the_old_flag_name_still_reads_the_config_and_approval_flags_are_gone(self):
        code, err, created = self.paid("--standing-approval-from-config", str(self.config(behavior_judge_max_usd=7)))
        self.assertEqual((code, created[0]["run_limit"].limit), ("prepared", Decimal("7")))
        for flag in ("--i-have-user-approval",):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.cli.parse_args(["--run-dir", str(self.run_dir), flag])

    def test_research_integrity_refusals_stay(self):
        with mock.patch.object(self.cli, "pricing_record", return_value=None):
            code, _, err = self.call("--run-config", str(self.config()), "--ledger", str(self.tmp / "ledger.sqlite"),
                                     "--output-dir", str(self.judge_dir))
        self.assertEqual(code, 4)
        self.assertIn("no judge route pricing record", err)


if __name__ == "__main__":
    unittest.main()
