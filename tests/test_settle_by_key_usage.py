import importlib.util
import json
import tempfile
import time
import types
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("settle_by_key_usage", ROOT / "scripts" / "settle_by_key_usage.py")
settle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settle)
from research_swarm.budget import BudgetLedger  # noqa: E402


class SettleByKeyUsageTests(unittest.TestCase):
    def test_allocation_is_proportional_capped_and_never_releases_below_the_excess(self):
        holds = {"a": Decimal("1"), "b": Decimal("3")}
        plan = settle.allocate(Decimal("0.4"), holds)
        self.assertEqual(plan, {"a": Decimal("0.1"), "b": Decimal("0.3")})
        self.assertEqual(settle.allocate(Decimal("4"), holds), {})
        self.assertGreaterEqual(sum(settle.allocate(Decimal("0.1"), {"a": Decimal(1), "b": Decimal(2)}).values()),
                                Decimal("0.1"))

    def test_pass_settles_old_uncertain_holds_from_the_key_counter(self):
        folder = Path(tempfile.mkdtemp())
        ledger_path = folder / "gemini-budget.sqlite"
        ledger = BudgetLedger(ledger_path)
        ledger.start_run("run", "pilot", "100")
        run = folder / "run"
        run.mkdir()
        old, now = time.time() - 3600, time.time()
        records = [{"timestamp": old, "kind": "provider_limit_verified", "payload": {"usage": 500.0}}]
        ids = {}
        for name, ts in (("paid", old), ("empty1", old), ("empty2", old), ("young", now)):
            ids[name] = ledger.reserve("pilot", "agent-001", "2")
            records.append({"timestamp": ts, "kind": "inference_admitted", "payload": {"request_id": ids[name]}})
        ledger.settle(ids["paid"], "1.5")
        for name in ("empty1", "empty2", "young"):
            ledger.settle(ids[name], None)
        (run / "runtime.jsonl").write_text("\n".join(json.dumps(r) for r in records))
        args = types.SimpleNamespace(run_dir=run, ledger=ledger_path, margin_usd=Decimal("0.5"), track="gemini")
        # billed 1.6 since launch, 1.5 settled: 0.1 unexplained + 0.5 margin over the two old holds
        summary = settle.one_pass(args, True, 10, usage_reader=lambda: Decimal("501.6"))
        self.assertEqual(summary["outcome"], "settled")
        self.assertEqual(sorted((e["request_id"], e["cost_usd"]) for e in summary["entries"]),
                         sorted([(ids["empty1"], "0.300000000"), (ids["empty2"], "0.300000000")]))
        snapshot = ledger.snapshot()["pilot"]
        self.assertEqual(snapshot["unresolved_requests"], 1)  # the young hold stays held at its reservation
        self.assertEqual(Decimal(snapshot["committed_usd"]), Decimal("1.5") + Decimal("0.6") + Decimal("2"))

    def test_settling_needs_no_approval_flag(self):
        """docs/plan.md, "One per-run budget, no manual approvals": no --i-have-user-approval; --dry-run plans."""
        calls = []
        original = settle.one_pass
        settle.one_pass = lambda args, approved, minutes: calls.append(approved)
        try:
            settle.main(["--run-dir", "x", "--ledger", "y.sqlite"])
            settle.main(["--run-dir", "x", "--ledger", "y.sqlite", "--dry-run"])
            with self.assertRaises(SystemExit):
                settle.main(["--run-dir", "x", "--i-have-user-approval"])
        finally:
            settle.one_pass = original
        self.assertEqual(calls, [True, False])


if __name__ == "__main__":
    unittest.main()
