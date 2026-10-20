"""Offline tests of the judge's standing-approval path (docs/plan.md, "Automatic post-run behavior
judge for the owner's runs (October 20, 2026)"): no model call, no credential, no real ledger."""
from __future__ import annotations

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
from research_swarm.auto_judge import clamp_run_limit  # noqa: E402
import test_judge_estimate  # noqa: E402

APPROVAL = {"auto_behavior_judge": True, "behavior_judge_approval": "owner standing approval (test)",
            "behavior_judge_max_usd": 50}


class Stop(Exception):
    pass


class StandingApprovalTests(unittest.TestCase):
    # The synthetic run and the CLI harness of test_judge_estimate.CommandLineTests.
    setUp = test_judge_estimate.CommandLineTests.setUp
    tearDown = test_judge_estimate.CommandLineTests.tearDown
    call = test_judge_estimate.CommandLineTests.call

    def config(self, **changes):
        path = self.tmp / "launch-config.json"
        path.write_text(json.dumps(dict(APPROVAL, run_dir="runs/x", **changes)))
        return path

    def paid(self, config, *extra, remaining="150.000000000"):
        created = []

        class FakeClient:
            def __init__(self, key, ledger, **kwargs):
                created.append(kwargs)

            def prepare(self):
                raise Stop()

        snapshot = {"analysis": {"remaining_usd": remaining, "cap_usd": "200"}}
        typed = mock.Mock(side_effect=AssertionError("no typed confirmation under a standing approval"))
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch("research_swarm.budget.judge_analysis_problems", return_value=[]), \
                mock.patch("research_swarm.budget.read_snapshot", return_value=snapshot), \
                mock.patch.object(self.cli, "route_settings", return_value=JudgeRouteSettings.defaults(
                    pricing_verified=True)), \
                mock.patch("research.analysis.judge.client.JudgeClient", FakeClient), \
                mock.patch("builtins.input", typed):
            try:
                code, _, err = self.call("--standing-approval-from-config", str(config), "--ledger",
                                         str(self.tmp / "ledger.sqlite"), "--output-dir", str(self.judge_dir), *extra)
            except Stop:
                code, err = "prepared", ""
        return code, err, created

    def test_clamp_rule(self):
        self.assertEqual(clamp_run_limit("12.40", "50", "150"), (Decimal("12.40"), "invocation"))
        self.assertEqual(clamp_run_limit("80", "50", "150"), (Decimal("50"), "standing_approval_cap"))
        self.assertEqual(clamp_run_limit("80", "500", "150"), (Decimal("50"), "standing_approval_cap"))  # never > $50
        self.assertEqual(clamp_run_limit("80", "50", "7.5"), (Decimal("7.5"), "analysis_remaining"))
        self.assertEqual(clamp_run_limit("80", "20", None), (Decimal("20"), "standing_approval_cap"))

    def test_standing_approval_runs_without_typed_confirmation_and_records_the_approval(self):
        code, err, created = self.paid(self.config(), "--run-limit", "200")
        self.assertEqual(code, "prepared", err)  # reached the key loading without input() or a terminal
        self.assertEqual(len(created), 3)
        limit = created[0]["run_limit"]
        self.assertEqual((limit.limit, limit.source), (Decimal("50"), "standing_approval"))  # clamped to the cap
        self.assertEqual(limit.standing_approval["run_limit_basis"], "standing_approval_cap")
        self.assertEqual(limit.standing_approval["unclamped_limit_usd"], "200")
        self.assertEqual(limit.record()["standing_approval"]["approval"], "owner standing approval (test)")
        records = json.loads((self.judge_dir / "standing-approvals.json").read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["max_usd"], records[0]["track"], records[0]["config_path"]),
                         ("50", "baseline", str((self.tmp / "launch-config.json").resolve())))

    def test_limit_never_exceeds_the_remaining_allowance_or_the_automatic_limit(self):
        code, err, created = self.paid(self.config(), remaining="150")
        self.assertEqual(code, "prepared", err)
        automatic = created[0]["run_limit"]
        self.assertEqual(automatic.standing_approval["run_limit_basis"], "invocation")  # the synthetic run is cheap
        self.assertEqual(automatic.limit, Decimal(automatic.standing_approval["unclamped_limit_usd"]))
        central = Decimal(automatic.standing_approval["estimate_central_usd"])
        self.assertLess(automatic.limit, Decimal(50))
        tight = (central + Decimal("0.01")).quantize(Decimal("0.01"))  # covers the estimate, below the automatic limit
        self.assertLess(tight, automatic.limit)
        (self.judge_dir / "standing-approvals.json").unlink()
        code, err, created = self.paid(self.config(), remaining=str(tight))
        self.assertEqual(code, "prepared", err)
        limit = created[0]["run_limit"]
        self.assertEqual((limit.limit, limit.standing_approval["run_limit_basis"]), (tight, "analysis_remaining"))

    def test_refused_when_the_clamped_limit_cannot_cover_the_central_estimate(self):
        code, err, created = self.paid(self.config(behavior_judge_max_usd="0.0001"), remaining="150")
        self.assertEqual(code, 4)
        self.assertIn("cannot cover the central estimate", err)
        self.assertEqual(created, [])
        self.assertFalse((self.judge_dir / "standing-approvals.json").exists())
        code, err, created = self.paid(self.config(), remaining="0.0001")
        self.assertEqual((code, created), (4, []))
        self.assertIn("analysis_remaining", err)

    def test_invalid_configs_are_refused_before_anything(self):
        for changes, text in (({"budget_track": "ablation"}, "owner's runs"),
                              ({"behavior_judge_approval": " "}, "nonempty behavior_judge_approval"),
                              ({"behavior_judge_max_usd": 51}, "exceeds the owner's per-run cap"),
                              ({"auto_behavior_judge": False}, "is not true")):
            code, err, created = self.paid(self.config(**changes))
            self.assertEqual((code, created), (4, []), changes)
            self.assertIn(text, err)
        code, err, _ = self.paid(self.tmp / "missing.json")
        self.assertEqual(code, 4)
        code, _, err = self.call("--mock", "--standing-approval-from-config", str(self.config()), "--output-dir",
                                 str(self.judge_dir))
        self.assertEqual(code, 4)
        self.assertIn("paid judging run only", err)

    def test_other_refusals_stay(self):
        with mock.patch("research_swarm.budget.judge_analysis_problems", return_value=["fifth raise missing"]):
            code, _, err = self.call("--standing-approval-from-config", str(self.config()), "--ledger",
                                     str(self.tmp / "ledger.sqlite"), "--output-dir", str(self.judge_dir))
        self.assertEqual(code, 4)
        self.assertIn("analysis ledger not ready", err)
        with mock.patch("research_swarm.budget.judge_analysis_problems", return_value=[]), \
                mock.patch.object(self.cli, "pricing_record", return_value=None):
            code, _, err = self.call("--standing-approval-from-config", str(self.config()), "--ledger",
                                     str(self.tmp / "ledger.sqlite"), "--output-dir", str(self.judge_dir))
        self.assertEqual(code, 4)
        self.assertIn("no judge route pricing record", err)
        # The gemini track (the owner's) is accepted.
        code, err, _ = self.paid(self.config(budget_track="gemini"))
        self.assertEqual(code, "prepared", err)


if __name__ == "__main__":
    unittest.main()
