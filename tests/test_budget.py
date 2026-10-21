"""Per-run budget admission (docs/plan.md "One per-run budget, no manual approvals (October 20, 2026)")."""
import concurrent.futures
import datetime
from decimal import Decimal
import multiprocessing
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from research_swarm import budget
from research_swarm.budget import (BudgetExceeded, BudgetLedger, DEFAULT_JUDGE_RUN_BUDGET_USD, DEFAULT_LEDGER_PATH,
                                   DEFAULT_RUN_BUDGET_USD, MANUAL_BASIS, ORPHANED_BASIS, cap_changes, describe_budget,
                                   list_requests, list_runs, read_run, read_snapshot, run_budget)


# Module-level workers so the spawn start method (macOS default) can import them.
def _reserve_worker(path, attempts):
    ledger = BudgetLedger(path)
    ledger.start_run("shared-run", "pilot", 1)
    admitted = 0
    for _ in range(attempts):
        try:
            ledger.reserve("pilot", "agent", 0.05)
            admitted += 1
        except BudgetExceeded:
            pass
    return admitted


def _open_worker(path, start, results):
    start.wait(10)
    try:
        BudgetLedger(path)
        results.put("ok")
    except Exception as ex:  # reported to the parent test
        results.put(f"{type(ex).__name__}: {ex}")


def dead_owner():
    """``host:pid`` of a process that has exited."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return f"{socket.gethostname()}:{process.pid}"


def run(path, run_id="run-1", amount=20, scope="pilot", **kwargs):
    ledger = BudgetLedger(path, **kwargs)
    ledger.start_run(run_id, scope, amount)
    return ledger


class RunBudgetConfigTests(unittest.TestCase):
    def test_default_is_one_hundred_dollars_when_unset(self):
        self.assertEqual(DEFAULT_RUN_BUDGET_USD, Decimal("100"))
        for config in ({}, None, {"run_budget_usd": None}):
            self.assertEqual(run_budget(config), (Decimal("100"), "default"))
        self.assertIn("default", describe_budget(*run_budget({})))
        self.assertIn("$100", describe_budget(*run_budget({})))
        self.assertEqual(DEFAULT_JUDGE_RUN_BUDGET_USD, Decimal("50"))

    def test_specified_budget_and_invalid_values(self):
        self.assertEqual(run_budget({"run_budget_usd": "25"}), (Decimal("25"), "config run_budget_usd"))
        self.assertEqual(run_budget({"run_budget_usd": 40}), (Decimal("40"), "config run_budget_usd"))
        self.assertEqual(run_budget({"run_budget_usd": "$1,000.50"})[0], Decimal("1000.50"))
        self.assertIn("config run_budget_usd", describe_budget(*run_budget({"run_budget_usd": "25"})))
        for bad in ("abc", 0, "-5", True, "nan", "inf", [], {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run_budget({"run_budget_usd": bad})


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "cost.sqlite"

    def test_concurrent_admission_within_one_run(self):
        ledger = run(self.path, amount=1)

        def reserve(_):
            try:
                return ledger.reserve("pilot", "agent", 0.1)
            except BudgetExceeded:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            calls = list(pool.map(reserve, range(30)))
        self.assertEqual(sum(x is not None for x in calls), 10)
        self.assertEqual(ledger.run_snapshot()["remaining_usd"], "0")

    def test_multiprocess_admission_shares_the_run_budget(self):
        path = str(self.path)
        with multiprocessing.get_context("spawn").Pool(6) as pool:
            admitted = pool.starmap(_reserve_worker, [(path, 8)] * 6)
        self.assertEqual(sum(admitted), 20)  # $1 / $0.05, out of 48 attempts
        state = read_run(path, "shared-run")
        self.assertEqual((state["remaining_usd"], state["in_flight_requests"]), ("0", 20))

    def test_fresh_ledger_concurrent_open(self):
        path = str(self.root / "fresh" / "cost.sqlite")
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        workers = [context.Process(target=_open_worker, args=(path, start, results)) for _ in range(8)]
        for worker in workers:
            worker.start()
        start.set()
        outcomes = [results.get(timeout=60) for _ in workers]
        for worker in workers:
            worker.join(30)
        self.assertEqual(outcomes, ["ok"] * 8)
        with closing(sqlite3.connect(path)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"basis", "settled", "run_id", "requested", "owner"} <= columns)
        self.assertIn("runs", tables)

    def test_run_budget_stops_the_run(self):
        ledger = run(self.path, amount=2)
        ledger.settle(ledger.reserve("pilot", "a", 1), "0.75")
        ledger.reserve("pilot", "b", 1)  # in flight: committed 1.75
        with self.assertRaises(BudgetExceeded) as caught:
            ledger.reserve("pilot", "c", "0.26")
        self.assertIn("run-1", str(caught.exception))
        ledger.reserve("pilot", "c", "0.25")  # exactly fits
        self.assertFalse(ledger.can_cover("0.000000001"))
        state = ledger.run_snapshot()
        self.assertEqual((state["budget_usd"], state["committed_usd"], state["remaining_usd"]), ("2", "2", "0"))

    def test_reserve_needs_a_registered_run_in_its_scope(self):
        ledger = BudgetLedger(self.path)
        with self.assertRaises(ValueError):
            ledger.reserve("pilot", "a", 1)
        ledger.start_run("run-1", "pilot", 5)
        with self.assertRaises(ValueError):
            ledger.reserve("analysis", "a", 1)
        with self.assertRaises(ValueError):
            ledger.reserve("pilot", "a", 0)
        for bad in (0, "-1", "x", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                BudgetLedger(self.path).start_run("run-2", "pilot", bad)

    def test_a_runs_budget_is_fixed_when_it_starts(self):
        ledger = run(self.path, amount=5)
        again = BudgetLedger(self.path)
        again.start_run("run-1", "pilot", 5)  # the same run from another process or client
        again.settle(again.reserve("pilot", "a", 4), 4)
        with self.assertRaises(BudgetExceeded):  # the run's committed spend is shared
            ledger.reserve("pilot", "b", 2)
        for scope, amount in (("pilot", 6), ("analysis", 5)):
            with self.subTest(scope=scope, amount=amount), self.assertRaises(ValueError):
                BudgetLedger(self.path).start_run("run-1", scope, amount)

    def test_new_run_is_never_blocked_by_an_old_runs_holds(self):
        old = run(self.path, "old-run", amount=1)
        held = old.reserve("pilot", "a", 1)  # in flight, budget exhausted
        with self.assertRaises(BudgetExceeded):
            old.reserve("pilot", "b", "0.01")
        other = BudgetLedger(self.path)
        other.start_run("other-old-run", "pilot", 1)
        other.settle(other.reserve("pilot", "c", 1), None)  # uncertain hold of a second earlier run
        new = run(self.path, "new-run", amount=1)
        new.settle(new.reserve("pilot", "d", 1), "0.5")
        state = new.run_snapshot()
        self.assertEqual((state["committed_usd"], state["remaining_usd"]), ("0.5", "0.5"))
        # The old holds stay counted against their own runs and in the scope's history.
        self.assertEqual(read_run(self.path, "old-run")["committed_usd"], "1")
        self.assertEqual(read_run(self.path, "other-old-run")["uncertain_requests"], 1)
        self.assertEqual(read_snapshot(self.path)["pilot"]["committed_usd"], "2.5")
        self.assertIn(held, [r["request_id"] for r in list_requests(self.path)])

    def test_legacy_ledger_caps_never_limit_a_run(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE scopes (name TEXT PRIMARY KEY, cap INTEGER NOT NULL)")
            db.execute("CREATE TABLE calls (id TEXT PRIMARY KEY, scope TEXT NOT NULL, agent TEXT NOT NULL, "
                       "reserved INTEGER NOT NULL, actual INTEGER, state TEXT NOT NULL, "
                       "created TEXT DEFAULT CURRENT_TIMESTAMP)")
            db.execute("INSERT INTO scopes VALUES ('pilot', 20000000000)")
            db.execute("INSERT INTO calls (id,scope,agent,reserved,actual,state) "
                       "VALUES ('old','pilot','a',20000000000,20000000000,'settled')")
        snapshot = read_snapshot(self.path)["pilot"]  # read-only works before the migration
        self.assertEqual((snapshot["committed_usd"], snapshot["legacy_cap_usd"]), ("20", "20"))
        self.assertNotIn("remaining_usd", snapshot)
        ledger = run(self.path, amount=100)  # the old $20 cap is fully committed: a new run still admits
        ledger.settle(ledger.reserve("pilot", "b", 30), 25)
        self.assertEqual(ledger.run_snapshot()["remaining_usd"], "75")
        self.assertEqual(read_snapshot(self.path)["pilot"]["committed_usd"], "45")
        self.assertEqual(cap_changes(self.path), [])

    def test_estimate_violation_is_recorded_and_the_run_continues(self):
        ledger = run(self.path, amount=10)
        call = ledger.reserve("pilot", "a", 1)
        ledger.settle(call, 2)  # billed twice its reservation
        state = ledger.run_snapshot()
        self.assertEqual((state["estimate_violations"], state["committed_usd"], state["reservation_factor"]),
                         (1, "2", "2.00"))
        self.assertFalse(hasattr(ledger, "acknowledge_violation"))  # nothing to acknowledge by hand
        later = ledger.reserve("pilot", "b", 1)  # admitted, scaled by the observed ratio
        reserved = {r["request_id"]: r["reserved_usd"] for r in list_requests(self.path)}
        self.assertEqual(reserved[later], "2")
        with self.assertRaises(BudgetExceeded):  # 2 + 2 + (3.5 x 2) > 10: the budget still bounds the run
            ledger.reserve("pilot", "c", "3.5")
        ledger.reserve("pilot", "c", 3)  # 2 + 2 + (3 x 2) = 10 fits exactly
        audit = [entry["action"] for entry in ledger.audit_log()]
        self.assertEqual(audit, ["reservation_factor"])
        # A later run starts at factor 1.
        self.assertEqual(run(self.path, "run-2", amount=10).run_snapshot()["reservation_factor"], "1")

    def test_reconcile_above_reservation_is_a_violation_and_raises_the_factor(self):
        ledger = run(self.path)
        call = ledger.reserve("pilot", "a", 1)
        ledger.settle(call, None)
        self.assertEqual(ledger.reconcile(call, 1.5, note="reported")["state"], "estimate_violation")
        self.assertEqual(ledger.run_snapshot()["reservation_factor"], "1.50")
        ledger.reserve("pilot", "b", "0.01")  # never blocked


class SettlementTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "cost.sqlite"

    def test_uncertain_survives_restart_and_needs_reconcile(self):
        ledger = run(self.path)
        call = ledger.reserve("pilot", "a", 20)
        ledger.settle(call, None)
        reopened = BudgetLedger(self.path)
        reopened.start_run("run-1", "pilot", 20)
        with self.assertRaises(BudgetExceeded):
            reopened.reserve("pilot", "b", 0.01)  # the uncertain hold stays counted against its run
        with self.assertRaises(ValueError):
            ledger.settle(call, 2)  # no silent release
        with self.assertRaises(ValueError):
            ledger.reconcile(call, 2, note="")
        with self.assertRaises(ValueError):
            ledger.reconcile(call, None, note="x")
        self.assertEqual([r["request_id"] for r in list_requests(self.path)], [call])
        result = ledger.reconcile(call, 2, note="generation gen-1 total_cost")
        self.assertEqual((result["previous_state"], result["state"]), ("uncertain", "settled"))
        self.assertEqual(reopened.run_snapshot()["remaining_usd"], "18")
        self.assertEqual(reopened.snapshot()["pilot"]["reconciliations"], 1)
        audit = ledger.audit_log()
        self.assertEqual((audit[0]["request_id"], audit[0]["previous_state"], audit[0]["new_actual_usd"]),
                         (call, "uncertain", "2"))
        with self.assertRaises(ValueError):
            ledger.reconcile(call, 1, note="again")
        self.assertEqual(list_requests(self.path), [])

    def test_stale_reserved_hold_needs_explicit_flag(self):
        ledger = run(self.path)
        call = ledger.reserve("pilot", "a", 1)
        with self.assertRaises(ValueError):
            ledger.reconcile(call, 0.1, note="crashed")
        ledger.reconcile(call, 0.1, note="process exited without settling", allow_reserved=True)
        self.assertEqual(ledger.run_snapshot()["remaining_usd"], "19.9")

    def test_settlement_basis_recorded_and_validated(self):
        ledger = run(self.path)
        call = ledger.reserve("pilot", "a", 1)
        with self.assertRaises(ValueError):
            ledger.settle(call, 0.5, "token_estimate")
        ledger.settle(call, 0.5, "usage_priced_upper_bound")
        self.assertEqual(ledger.run_snapshot()["remaining_usd"], "19.5")
        self.assertTrue(ledger.can_cover(19.5))
        self.assertFalse(ledger.can_cover(19.6))

    def test_unbilled_error_releases_with_zero_cost_only(self):
        ledger = run(self.path)
        call = ledger.reserve("pilot", "a", 1)
        with self.assertRaises(ValueError):
            ledger.settle(call, 0.01, "unbilled_error")
        with self.assertRaises(ValueError):
            ledger.settle(call, None, "unbilled_error")
        ledger.settle(call, 0, "unbilled_error")
        state = ledger.run_snapshot()
        self.assertEqual((state["remaining_usd"], state["unbilled_error_requests"], state["unresolved_requests"]),
                         ("20", 1, 0))

    def test_manual_reconciliation_basis_only_through_reconcile(self):
        ledger = run(self.path)
        call = ledger.reserve("pilot", "a", 1)
        with self.assertRaises(ValueError):
            ledger.settle(call, 0.2, MANUAL_BASIS)  # never from the runtime
        ledger.settle(call, None)
        with self.assertRaises(ValueError):
            ledger.reconcile(call, 0.2, "made_up", note="x")
        result = ledger.reconcile(call, 0.2, MANUAL_BASIS, note="activity log")
        self.assertEqual((result["state"], result["basis"]), ("settled", MANUAL_BASIS))
        snap = ledger.snapshot()["pilot"]
        self.assertEqual((snap["committed_usd"], snap["manual_reconciliations"], snap["reconciliations"]),
                         ("0.2", 1, 1))
        self.assertEqual(ledger.audit_log()[0]["basis"], MANUAL_BASIS)

    def test_read_only_helpers_never_create(self):
        absent = self.path.parent / "absent.sqlite"
        self.assertIsNone(read_snapshot(absent))
        self.assertIsNone(read_run(absent, "x"))
        self.assertEqual((list_requests(absent), list_runs(absent), cap_changes(absent)), ([], [], []))
        self.assertFalse(absent.exists())

    def test_default_ledger_path_outside_runs(self):
        path = DEFAULT_LEDGER_PATH(Path("/repo"))
        self.assertEqual(path, Path("/repo/state/budget.sqlite"))
        self.assertNotIn("runs", path.parts)

    def test_runs_are_listed_with_their_budgets(self):
        first = run(self.path, "a", amount=3)
        first.settle(first.reserve("pilot", "x", 1), 1)
        first.end_run()
        run(self.path, "b", amount=7, scope="compatibility")
        runs = list_runs(self.path)
        self.assertEqual([(r["run_id"], r["scope"], r["budget_usd"], r["committed_usd"]) for r in runs],
                         [("a", "pilot", "3", "1"), ("b", "compatibility", "7", "0")])
        self.assertIsNotNone(runs[0]["ended"])
        self.assertIsNone(runs[1]["ended"])
        self.assertEqual([r["run_id"] for r in list_runs(self.path, scope="compatibility")], ["b"])
        self.assertIn("compatibility", read_snapshot(self.path))  # a started run's scope appears


class LeftoverHoldTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "cost.sqlite"

    def test_dead_process_holds_become_uncertain_and_stay_counted(self):
        gone = run(self.path, "crashed-run", amount=5, owner=dead_owner())
        orphan = gone.reserve("pilot", "a", 2)
        live = BudgetLedger(self.path)  # this process: alive
        self.assertEqual(live.start_run("live-run", "pilot", 5)["orphaned_holds"], [orphan])
        in_flight = live.reserve("pilot", "b", 1)
        started = BudgetLedger(self.path, owner="other-host:1").start_run("next-run", "pilot", 5)
        self.assertEqual(started["orphaned_holds"], [])  # this process is alive: its hold stays reserved
        rows = {r["request_id"]: r for r in list_requests(self.path, states=budget.LEDGER_STATES)}
        self.assertEqual((rows[orphan]["state"], rows[orphan]["basis"]), ("uncertain", ORPHANED_BASIS))
        self.assertEqual(rows[in_flight]["state"], "reserved")  # a live process's hold is never touched
        self.assertEqual(read_run(self.path, "crashed-run")["committed_usd"], "2")  # still counted
        live.settle(in_flight, "0.5")
        self.assertEqual(ledger_actions(self.path), ["orphaned_hold"])

    def test_late_settlement_by_the_owning_process_is_recorded(self):
        ledger = run(self.path, amount=5)
        call = ledger.reserve("pilot", "a", 1)
        ledger.end_run()  # the run ended without the settlement: explicit uncertain hold
        self.assertEqual(list_requests(self.path)[0]["state"], "uncertain")
        ledger.settle(call, "0.4")  # its own process settles after all
        state = read_run(self.path, "run-1")
        self.assertEqual((state["committed_usd"], state["unresolved_requests"]), ("0.4", 0))
        self.assertEqual(ledger_actions(self.path), ["orphaned_hold", "late_settlement"])
        other = run(self.path, "run-2", amount=5)
        hold = other.reserve("pilot", "b", 1)
        other.end_run()
        with self.assertRaises(ValueError):  # another process never settles it
            BudgetLedger(self.path, owner="elsewhere:1").settle(hold, "0.1")

    def test_ownerless_rows_count_as_left_over_only_when_old(self):
        ledger = run(self.path, amount=5)
        old, recent = ledger.reserve("pilot", "a", 1), ledger.reserve("pilot", "b", 1)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE calls SET owner=NULL, run_id=NULL")
            db.execute("UPDATE calls SET created=? WHERE id=?",
                       ((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=30))
                        .strftime("%Y-%m-%d %H:%M:%S"), old))
        started = BudgetLedger(self.path, owner="other:1").start_run("run-2", "pilot", 5)
        self.assertEqual(started["orphaned_holds"], [old])
        states = {r["request_id"]: r["state"] for r in list_requests(self.path)}
        self.assertEqual(states, {old: "uncertain", recent: "reserved"})

    def test_owner_gone_rules(self):
        host = socket.gethostname()
        self.assertTrue(budget.owner_gone(dead_owner()))
        self.assertFalse(budget.owner_gone(budget.current_owner()))
        self.assertTrue(budget.owner_gone("another-host:1", host=host))
        self.assertFalse(budget.owner_gone(None))
        self.assertFalse(budget.owner_gone(f"{host}:not-a-pid"))


def ledger_actions(path):
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT action FROM reconciliations ORDER BY id")]


if __name__ == "__main__":
    unittest.main()
