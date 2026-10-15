import concurrent.futures
import multiprocessing
import sqlite3
import tempfile
import unittest
from unittest import mock
from contextlib import closing
from pathlib import Path
from research_swarm.budget import (BudgetLedger, BudgetExceeded, DEFAULT_CAPS, DEFAULT_LEDGER_PATH, MANUAL_BASIS,
                                   cap_changes, list_requests, raise_cap, read_snapshot)


# Module-level workers so the spawn start method (macOS default) can import them.
def _reserve_worker(path, attempts):
    ledger = BudgetLedger(path, {"pilot": 1})
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


class BudgetTests(unittest.TestCase):
    def setUp(self):
        # Historical accounting regressions retain the original experiment caps.
        self.historical_caps = mock.patch.dict(DEFAULT_CAPS, {"pilot":20,"analysis":5,"compatibility":2}, clear=True)
        self.historical_caps.start()
        self.addCleanup(self.historical_caps.stop)

    def test_concurrent_shared_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"cost.sqlite",{"pilot":1})
            def reserve(_):
                try: return ledger.reserve("pilot","agent",0.1)
                except BudgetExceeded:return None
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                calls=list(pool.map(reserve,range(30)))
            self.assertEqual(sum(x is not None for x in calls),10)
            self.assertEqual(ledger.snapshot()["pilot"]["remaining_usd"],"0")
    def test_multiprocess_shared_admission(self):
        # Separate processes (each opening the fresh ledger itself) share one cap.
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/"cost.sqlite")
            with multiprocessing.get_context("spawn").Pool(6) as pool:
                admitted=pool.starmap(_reserve_worker,[(path,8)]*6)
            self.assertEqual(sum(admitted),20)  # $1 / $0.05, out of 48 attempts
            snap=read_snapshot(path)["pilot"]
            self.assertEqual((snap["remaining_usd"],snap["in_flight_requests"]),("0",20))
    def test_fresh_ledger_concurrent_open(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/"fresh"/"cost.sqlite")
            context=multiprocessing.get_context("spawn")
            start,results=context.Event(),context.Queue()
            workers=[context.Process(target=_open_worker,args=(path,start,results)) for _ in range(8)]
            for worker in workers: worker.start()
            start.set()
            outcomes=[results.get(timeout=60) for _ in workers]
            for worker in workers: worker.join(30)
            self.assertEqual(outcomes,["ok"]*8)
            with closing(sqlite3.connect(path)) as db:
                columns={row[1] for row in db.execute("PRAGMA table_info(calls)")}
            self.assertTrue({"basis","settled"}<=columns)
    def test_legacy_schema_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"old.sqlite"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE scopes (name TEXT PRIMARY KEY, cap INTEGER NOT NULL)")
                db.execute("CREATE TABLE calls (id TEXT PRIMARY KEY, scope TEXT NOT NULL, agent TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER, state TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP)")
                db.execute("INSERT INTO scopes VALUES ('pilot', 20000000000)")
                db.execute("INSERT INTO calls (id,scope,agent,reserved,actual,state) VALUES ('old','pilot','a',1000000000,500000000,'settled')")
            self.assertEqual(read_snapshot(path)["pilot"]["committed_usd"],"0.5")  # read-only works pre-migration
            ledger=BudgetLedger(path,{"pilot":20})
            self.assertEqual(ledger.snapshot()["pilot"]["remaining_usd"],"19.5")
            ledger.settle(ledger.reserve("pilot","b",1),0.25,"usage_priced_upper_bound")
            self.assertEqual(ledger.snapshot()["pilot"]["remaining_usd"],"19.25")
    def test_uncertain_survives_restart_and_needs_reconcile(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cost.sqlite"
            ledger=BudgetLedger(path)
            call=ledger.reserve("pilot","a",20)
            ledger.settle(call,None)
            reopened=BudgetLedger(path)
            with self.assertRaises(BudgetExceeded):reopened.reserve("pilot","b",0.01)
            # No silent release: settle() refuses an uncertain request.
            with self.assertRaises(ValueError):ledger.settle(call,2)
            with self.assertRaises(ValueError):ledger.reconcile(call,2,note="")
            with self.assertRaises(ValueError):ledger.reconcile(call,None,note="x")
            self.assertEqual([r["request_id"] for r in list_requests(path)],[call])
            result=ledger.reconcile(call,2,note="generation gen-1 total_cost")
            self.assertEqual((result["previous_state"],result["state"]),("uncertain","settled"))
            self.assertEqual(reopened.snapshot()["pilot"]["remaining_usd"],"18")
            self.assertEqual(reopened.snapshot()["pilot"]["reconciliations"],1)
            audit=ledger.audit_log()
            self.assertEqual((audit[0]["request_id"],audit[0]["previous_state"],audit[0]["new_actual_usd"]),(call,"uncertain","2"))
            with self.assertRaises(ValueError):ledger.reconcile(call,1,note="again")
            self.assertEqual(list_requests(path),[])
    def test_stale_reserved_hold_needs_explicit_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"cost.sqlite")
            call=ledger.reserve("pilot","a",1)
            with self.assertRaises(ValueError):ledger.reconcile(call,0.1,note="crashed")
            ledger.reconcile(call,0.1,note="process exited without settling",allow_reserved=True)
            self.assertEqual(ledger.snapshot()["pilot"]["remaining_usd"],"19.9")
    def test_reconcile_above_reservation_is_violation(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"cost.sqlite")
            call=ledger.reserve("pilot","a",1)
            ledger.settle(call,None)
            self.assertEqual(ledger.reconcile(call,1.5,note="reported")["state"],"estimate_violation")
            with self.assertRaises(BudgetExceeded):ledger.reserve("pilot","b",0.01)
    def test_paid_compatibility_capped_separately(self):
        # Plan "Live compatibility check": separate $2 compatibility allowance.
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"cost.sqlite")
            with self.assertRaises(BudgetExceeded):ledger.reserve("compatibility","check",2.01)
            ledger.reserve("compatibility","check",1.5)
            with self.assertRaises(BudgetExceeded):ledger.reserve("compatibility","check",0.51)
            self.assertEqual(ledger.snapshot()["pilot"]["remaining_usd"],"20")
    def test_cap_cannot_be_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cost.sqlite"
            BudgetLedger(path)
            with self.assertRaises(ValueError):BudgetLedger(path,{"pilot":21})
    def test_unexpected_bill_stops_calls_until_acknowledged(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"cost.sqlite",{"pilot":3})
            call=ledger.reserve("pilot","a",1)
            ledger.settle(call,2)
            self.assertTrue(ledger.snapshot()["pilot"]["violation"])
            with self.assertRaises(BudgetExceeded):ledger.reserve("pilot","b",0.001)
            with self.assertRaises(ValueError):ledger.acknowledge_violation(call,"")
            ledger.acknowledge_violation(call,"estimator corrected; user informed")
            snap=ledger.snapshot()["pilot"]
            self.assertEqual((snap["violation"],snap["acknowledged_violations"],snap["committed_usd"]),(False,1,"2"))
            ledger.reserve("pilot","b",0.5)
            self.assertEqual(ledger.audit_log()[0]["action"],"acknowledge_violation")
    def test_settlement_basis_recorded_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"cost.sqlite")
            call=ledger.reserve("pilot","a",1)
            with self.assertRaises(ValueError):ledger.settle(call,0.5,"token_estimate")
            ledger.settle(call,0.5,"usage_priced_upper_bound")
            self.assertEqual(ledger.snapshot()["pilot"]["remaining_usd"],"19.5")
            self.assertTrue(ledger.can_cover("pilot",19.5))
            self.assertFalse(ledger.can_cover("pilot",19.6))
    def test_unbilled_error_releases_with_zero_cost_only(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"cost.sqlite")
            call=ledger.reserve("pilot","a",1)
            with self.assertRaises(ValueError):ledger.settle(call,0.01,"unbilled_error")
            with self.assertRaises(ValueError):ledger.settle(call,None,"unbilled_error")
            ledger.settle(call,0,"unbilled_error")
            snap=ledger.snapshot()["pilot"]
            self.assertEqual((snap["remaining_usd"],snap["unbilled_error_requests"],snap["unresolved_requests"]),("20",1,0))
    def test_default_caps_follow_latest_decision(self):
        self.historical_caps.stop()
        with tempfile.TemporaryDirectory() as folder:
            snap=BudgetLedger(Path(folder)/"cost.sqlite").snapshot()
            self.assertEqual({k:v["cap_usd"] for k,v in snap.items()},{"pilot":"119.636804044","analysis":"5","compatibility":"2"})
            self.assertEqual(DEFAULT_CAPS,{"pilot":"119.636804044","analysis":5,"compatibility":2})
    def test_default_ledger_path_outside_runs(self):
        path=DEFAULT_LEDGER_PATH(Path("/repo"))
        self.assertEqual(path,Path("/repo/state/budget.sqlite"))
        self.assertNotIn("runs",path.parts)
    def test_zero_compatibility_ledger_needs_audited_raise(self):
        # A ledger created under the superseded $0 compatibility cap is never silently changed.
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cost.sqlite"
            old=BudgetLedger(path,{"pilot":20,"analysis":5,"compatibility":0})
            old.settle(old.reserve("pilot","a",1),0.3)
            held=old.reserve("pilot","b",2)
            with self.assertRaises(ValueError) as caught:BudgetLedger(path)
            self.assertIn("raise_cap",str(caught.exception))
            for scope,cap,note in (("compatibility",2,""),("compatibility",3,"approved"),("compatibility",1,"approved"),
                                   ("pilot",20,"approved"),("unknown",2,"approved")):
                with self.assertRaises(ValueError):raise_cap(path,scope,cap,note)
            with self.assertRaises(ValueError):raise_cap(Path(folder)/"absent.sqlite","compatibility",2,"approved")
            self.assertFalse((Path(folder)/"absent.sqlite").exists())
            self.assertEqual(read_snapshot(path)["compatibility"]["cap_usd"],"0")
            self.assertEqual(cap_changes(path),[])
            result=raise_cap(path,"compatibility",2,"user approved $2 compatibility allowance in chat")
            self.assertEqual((result["previous_cap_usd"],result["new_cap_usd"]),("0","2"))
            snap=BudgetLedger(path).snapshot()
            self.assertEqual({k:v["cap_usd"] for k,v in snap.items()},{"pilot":"20","analysis":"5","compatibility":"2"})
            self.assertEqual((snap["pilot"]["committed_usd"],snap["pilot"]["unresolved_requests"]),("2.3",1))  # spending kept
            self.assertEqual(snap["compatibility"]["cap_raises"],1)
            self.assertEqual([(c["scope"],c["previous_cap_usd"],c["new_cap_usd"],c["note"]) for c in cap_changes(path)],
                             [("compatibility","0","2","user approved $2 compatibility allowance in chat")])
            with self.assertRaises(ValueError):raise_cap(path,"compatibility",2,"again")  # never re-applied
            self.assertEqual([r["request_id"] for r in list_requests(path)],[held])
    def test_manual_reconciliation_basis_only_through_reconcile(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=BudgetLedger(Path(folder)/"cost.sqlite")
            call=ledger.reserve("pilot","a",1)
            with self.assertRaises(ValueError):ledger.settle(call,0.2,MANUAL_BASIS)  # never from the runtime
            ledger.settle(call,None)
            with self.assertRaises(ValueError):ledger.reconcile(call,0.2,"made_up",note="x")
            result=ledger.reconcile(call,0.2,MANUAL_BASIS,note="activity log; user approved")
            self.assertEqual((result["state"],result["basis"]),("settled",MANUAL_BASIS))
            snap=ledger.snapshot()["pilot"]
            self.assertEqual((snap["remaining_usd"],snap["manual_reconciliations"],snap["reconciliations"]),("19.8",1,1))
            self.assertEqual(ledger.audit_log()[0]["basis"],MANUAL_BASIS)
    def test_read_snapshot_never_creates(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"absent.sqlite"
            self.assertIsNone(read_snapshot(path))
            self.assertEqual(list_requests(path),[])
            self.assertFalse(path.exists())


class RepeatBudgetTests(unittest.TestCase):
    def test_additional_hundred_preserves_original_requests_and_caps(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "budget.sqlite"
            old = BudgetLedger(path, {"pilot":20,"analysis":5,"compatibility":2})
            request = old.reserve("pilot", "old-run-agent", 20)
            old.settle(request, "19.636804044")
            before = list_requests(path, states=("settled",))
            self.assertEqual([entry["request_id"] for entry in before], [request])
            with self.assertRaises(ValueError):
                BudgetLedger(path)  # raising the approved cap is always explicit
            raise_cap(path, "pilot", "119.636804044", "User approved fresh $100 baseline on 2026-10-15")
            self.assertEqual(list_requests(path, states=("settled",)), before)
            current = BudgetLedger(path)
            snapshot = current.snapshot()
            self.assertEqual(snapshot["pilot"]["remaining_usd"], "100")
            self.assertEqual(snapshot["analysis"]["cap_usd"], "5")
            self.assertEqual(snapshot["compatibility"]["cap_usd"], "2")
            with self.assertRaises(BudgetExceeded):
                current.reserve("pilot", "new-run-agent", "100.000000001")
            current.reserve("pilot", "new-run-agent", "100")
            with self.assertRaises(BudgetExceeded):
                current.reserve("pilot", "another-new-agent", "0.000000001")
            self.assertEqual(current.snapshot()["pilot"]["remaining_usd"], "0")
            self.assertEqual(cap_changes(path)[0]["previous_cap_usd"], "20")

    def test_repeat_raise_refuses_changed_spending_or_unresolved_calls(self):
        for spend, hold in (("19.636804043", False), ("19.636804045", False), ("19.636804044", True)):
            with self.subTest(spend=spend, hold=hold), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "budget.sqlite"
                old = BudgetLedger(path, {"pilot":20,"analysis":5,"compatibility":2})
                old.settle(old.reserve("pilot", "old", 20), spend)
                if hold:
                    old.reserve("pilot", "uncertain", ".1")
                with self.assertRaises(ValueError):
                    raise_cap(path, "pilot", "119.636804044", "User approved")
                self.assertEqual(read_snapshot(path)["pilot"]["cap_usd"], "20")
                self.assertEqual(cap_changes(path), [])
