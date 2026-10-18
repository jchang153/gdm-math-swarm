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
            self.assertEqual({k:v["cap_usd"] for k,v in snap.items()},{"pilot":"307.809660142","analysis":"40","compatibility":"2"})
            # October 17, 2026: the transcript judge's $40 analysis allowance (audited raise from $5).
            self.assertEqual(DEFAULT_CAPS,{"pilot":"307.809660142","analysis":"40","compatibility":2})
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
    def setUp(self):
        caps = mock.patch.dict(DEFAULT_CAPS, {"pilot":"119.636804044","analysis":5,"compatibility":2}, clear=True)
        caps.start()
        self.addCleanup(caps.stop)

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


class EightGiBBudgetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "budget.sqlite"
        old = BudgetLedger(self.path, {"pilot":20,"analysis":5,"compatibility":2})
        old.settle(old.reserve("pilot", "first", 20), "19.636804044")
        raise_cap(self.path, "pilot", "119.636804044", "First repeat approved")
        old = BudgetLedger(self.path, {"pilot":"119.636804044","analysis":5,"compatibility":2})
        old.settle(old.reserve("pilot", "second", 90), "86.091194430")
        for amount in [".3"] * 6 + [".5276232"]:
            old.settle(old.reserve("pilot", "uncertain", amount), None)
        from research_swarm.budget import _pilot_calls_sha256, _read_only
        with closing(_read_only(self.path)) as db:
            digest = _pilot_calls_sha256(db)
        patch = mock.patch("research_swarm.budget.EIGHT_GIB_PRIOR_CALLS_SHA256", digest)
        patch.start()
        self.addCleanup(patch.stop)

    def test_preserves_all_calls_and_holds_and_admits_exactly_hundred(self):
        before = list_requests(self.path, states=("settled", "uncertain"))
        raise_cap(self.path, "pilot", "208.055621674", "User authorized 8 GiB fresh $100 baseline")
        self.assertEqual(before, list_requests(self.path, states=("settled", "uncertain")))
        current = BudgetLedger(self.path, {"pilot":"208.055621674","analysis":5,"compatibility":2})
        snapshot = current.snapshot()["pilot"]
        self.assertEqual((snapshot["settled_usd"], snapshot["held_usd"], snapshot["remaining_usd"]),
                         ("105.727998474", "2.3276232", "100"))
        self.assertEqual(snapshot["uncertain_requests"], 7)
        with self.assertRaises(BudgetExceeded):
            current.reserve("pilot", "third", "100.000000001")
        current.reserve("pilot", "third", 100)
        with self.assertRaises(BudgetExceeded):
            current.reserve("pilot", "third", ".000000001")
        self.assertEqual(len(cap_changes(self.path)), 2)
        with self.assertRaises(ValueError):
            raise_cap(self.path, "pilot", "208.055621674", "Repeat")

    def test_refuses_any_changed_prior_request_or_audit_history(self):
        cases = ["UPDATE calls SET agent='changed' WHERE agent='first'",
                 "UPDATE calls SET reserved=reserved+1 WHERE agent='uncertain'",
                 "UPDATE calls SET state='reserved' WHERE agent='uncertain'",
                 "DELETE FROM calls WHERE agent='first'",
                 "DELETE FROM cap_changes",
                 "UPDATE cap_changes SET previous_cap=21000000000 WHERE scope='pilot'"]
        original = self.path.read_bytes()
        for sql in cases:
            with self.subTest(sql=sql):
                self.path.write_bytes(original)
                with sqlite3.connect(self.path) as db:
                    db.execute(sql)
                with self.assertRaises(ValueError):
                    raise_cap(self.path, "pilot", "208.055621674", "User authorized")
                self.assertEqual(read_snapshot(self.path)["pilot"]["cap_usd"], "119.636804044")

    def test_direct_raise_from_original_or_replacement_is_refused(self):
        replacement = Path(self.folder.name) / "replacement.sqlite"
        BudgetLedger(replacement, {"pilot":20,"analysis":5,"compatibility":2})
        with self.assertRaises(ValueError):
            raise_cap(replacement, "pilot", "208.055621674", "User authorized")


class JudgeAnalysisBudgetTests(unittest.TestCase):
    """October 17, 2026 transcript-judge authorization: one audited $5 -> $40 analysis raise.

    Synthetic temporary ledgers only. The fixture reproduces the authoritative lineage
    (current caps plus the three audited pilot raises, inserted directly) with pilot
    spending and an uncertain hold that the raise must leave untouched.
    """
    def setUp(self):
        from research_swarm import budget
        self.budget = budget
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / "budget.sqlite"
        self.caps = {"pilot": budget.CACHE_REPEAT_CAP_USD, "analysis": 5, "compatibility": 2}
        ledger = BudgetLedger(self.path, self.caps)
        ledger.settle(ledger.reserve("pilot", "old-run", 3), "2.5")
        ledger.settle(ledger.reserve("pilot", "old-uncertain", ".25"), None)
        ledger.settle(ledger.reserve("compatibility", "check", ".5"), ".32")
        self._insert_pilot_history(self.path)

    def _insert_pilot_history(self, path):
        budget = self.budget
        with closing(sqlite3.connect(path)) as db, db:
            for previous, new in budget.JUDGE_ANALYSIS_PILOT_CAP_HISTORY:
                db.execute("INSERT INTO cap_changes (scope,previous_cap,new_cap,note) VALUES ('pilot',?,?,?)",
                           (budget.units(previous), budget.units(new), "historical approval fixture"))

    def test_raise_admits_exactly_forty_and_keeps_every_prior_row(self):
        budget = self.budget
        before = list_requests(self.path, states=("settled", "uncertain"))
        # Opening with the approved $40 never raises the $5 scope implicitly.
        BudgetLedger(self.path, {"analysis": budget.JUDGE_ANALYSIS_CAP_USD})
        self.assertEqual(read_snapshot(self.path)["analysis"]["cap_usd"], "5")
        self.assertEqual([c["scope"] for c in cap_changes(self.path)], ["pilot", "pilot", "pilot"])
        self.assertTrue(budget.judge_analysis_problems(self.path))
        result = budget.raise_judge_analysis_cap(self.path, "User approved the $40 judge allowance in chat")
        self.assertEqual((result["previous_cap_usd"], result["new_cap_usd"]), ("5", "40"))
        self.assertTrue(result["note"].startswith(budget.JUDGE_ANALYSIS_PLAN_SECTION))
        self.assertIn(budget.JUDGE_ANALYSIS_RUN_ID, result["note"])
        self.assertEqual(budget.judge_analysis_problems(self.path), [])
        self.assertEqual(list_requests(self.path, states=("settled", "uncertain")), before)
        snap = read_snapshot(self.path)
        self.assertEqual((snap["pilot"]["cap_usd"], snap["pilot"]["committed_usd"], snap["pilot"]["held_usd"]),
                         (budget.CACHE_REPEAT_CAP_USD, "2.75", "0.25"))
        self.assertEqual((snap["compatibility"]["cap_usd"], snap["compatibility"]["committed_usd"]), ("2", "0.32"))
        self.assertEqual((snap["analysis"]["cap_usd"], snap["analysis"]["remaining_usd"],
                          snap["analysis"]["cap_raises"]), ("40", "40", 1))
        judge = BudgetLedger(self.path, {"analysis": budget.JUDGE_ANALYSIS_CAP_USD})
        with self.assertRaises(BudgetExceeded):
            judge.reserve("analysis", "judge", "40.000000001")
        judge.reserve("analysis", "judge", "40")
        with self.assertRaises(BudgetExceeded):
            judge.reserve("analysis", "judge", ".000000001")
        # Applied once only, also through raise_cap (which delegates to the audited raise).
        with self.assertRaises(ValueError):
            budget.raise_judge_analysis_cap(self.path, "again")
        with self.assertRaises(ValueError):
            raise_cap(self.path, "analysis", 40, "approved")
        self.assertEqual([c["scope"] for c in cap_changes(self.path)], ["pilot", "pilot", "pilot", "analysis"])
        # DEFAULT_CAPS["analysis"] is the approved $40: the raised ledger opens with the
        # default caps (reconciliation, the pilot runtime) and keeps its spending.
        defaults = BudgetLedger(self.path)
        snap = defaults.snapshot()
        self.assertEqual((snap["analysis"]["cap_usd"], snap["analysis"]["committed_usd"]), ("40", "40"))
        self.assertEqual(snap["pilot"]["held_usd"], "0.25")

    def test_refuses_missing_note_or_ledger(self):
        budget = self.budget
        with self.assertRaises(ValueError):
            budget.raise_judge_analysis_cap(self.path, "  ")
        absent = self.folder / "absent.sqlite"
        with self.assertRaises(ValueError):
            budget.raise_judge_analysis_cap(absent, "approved")
        self.assertFalse(absent.exists())
        self.assertTrue(budget.judge_analysis_problems(absent))
        self.assertFalse(absent.exists())
        self.assertEqual(read_snapshot(self.path)["analysis"]["cap_usd"], "5")

    def test_refuses_existing_analysis_spending_holds_or_raise(self):
        budget = self.budget
        original = self.path.read_bytes()
        cases = {
            "settled": lambda: BudgetLedger(self.path, self.caps).settle(
                BudgetLedger(self.path, self.caps).reserve("analysis", "earlier", ".1"), ".05"),
            "held": lambda: BudgetLedger(self.path, self.caps).reserve("analysis", "earlier", ".1"),
            "unbilled": lambda: BudgetLedger(self.path, self.caps).settle(
                BudgetLedger(self.path, self.caps).reserve("analysis", "earlier", ".1"), 0, "unbilled_error"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.path.write_bytes(original)
                mutate()
                with self.assertRaises(ValueError):
                    budget.raise_judge_analysis_cap(self.path, "approved")
                self.assertEqual(read_snapshot(self.path)["analysis"]["cap_usd"], "5")
        with self.subTest(name="earlier analysis raise"):
            self.path.write_bytes(original)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE scopes SET cap=? WHERE name='analysis'", (budget.units(4),))
                db.execute("INSERT INTO cap_changes (scope,previous_cap,new_cap,note) VALUES ('analysis',?,?,?)",
                           (budget.units(3), budget.units(4), "earlier"))
            with self.assertRaises(ValueError):
                budget.raise_judge_analysis_cap(self.path, "approved")

    def test_refuses_replacement_or_changed_lineage(self):
        budget = self.budget
        replacement = self.folder / "replacement.sqlite"
        BudgetLedger(replacement, self.caps)  # the prior caps, but no audited pilot raises
        with self.assertRaises(ValueError):
            budget.raise_judge_analysis_cap(replacement, "approved")
        with self.assertRaises(ValueError):
            raise_cap(replacement, "analysis", 40, "approved")
        self.assertEqual(read_snapshot(replacement)["analysis"]["cap_usd"], "5")
        # A fresh ledger with today's DEFAULT_CAPS has $40 but no audit row and no
        # lineage: the judge refuses it and the raise cannot apply.
        fresh = self.folder / "fresh.sqlite"
        BudgetLedger(fresh)
        self.assertEqual(read_snapshot(fresh)["analysis"]["cap_usd"], "40")
        self.assertTrue(budget.judge_analysis_problems(fresh))
        with self.assertRaises(ValueError):
            budget.raise_judge_analysis_cap(fresh, "approved")
        self.assertEqual([c for c in cap_changes(fresh) if c["scope"] == "analysis"], [])
        original = self.path.read_bytes()
        for sql in ("DELETE FROM cap_changes WHERE id=(SELECT MAX(id) FROM cap_changes)",
                    "UPDATE cap_changes SET new_cap=new_cap+1 WHERE id=1",
                    "UPDATE scopes SET cap=6000000000 WHERE name='analysis'"):
            with self.subTest(sql=sql):
                self.path.write_bytes(original)
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(sql)
                with self.assertRaises(ValueError):
                    budget.raise_judge_analysis_cap(self.path, "approved")
                self.assertEqual(len([c for c in cap_changes(self.path) if c["scope"] == "analysis"]), 0)

    def test_post_raise_check_detects_tampering(self):
        budget = self.budget
        budget.raise_judge_analysis_cap(self.path, "approved")
        raised = self.path.read_bytes()
        for sql in ("UPDATE cap_changes SET note='unbound' WHERE scope='analysis'",
                    "UPDATE scopes SET cap=50000000000 WHERE name='analysis'",
                    "DELETE FROM cap_changes WHERE scope='pilot' AND id=1"):
            with self.subTest(sql=sql):
                self.path.write_bytes(raised)
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(sql)
                self.assertTrue(budget.judge_analysis_problems(self.path))

    def test_default_caps_open_pending_ledger_unchanged(self):
        """Before the audited raise, the default caps open the authoritative $5 scope as is."""
        budget = self.budget
        before = self.path.read_bytes()
        ledger = BudgetLedger(self.path)  # DEFAULT_CAPS: analysis $40, ledger still $5
        snap = ledger.snapshot()
        self.assertEqual({k: v["cap_usd"] for k, v in snap.items()},
                         {"pilot": budget.CACHE_REPEAT_CAP_USD, "analysis": "5", "compatibility": "2"})
        self.assertEqual(len(cap_changes(self.path)), 3)
        self.assertTrue(budget.judge_analysis_problems(self.path))
        # Other tools keep working on it: reconcile the preserved pilot hold.
        hold = list_requests(self.path, scope="pilot")[0]["request_id"]
        self.assertEqual(ledger.reconcile(hold, ".2", note="generation total_cost")["state"], "settled")
        # It keeps enforcing $5 (no implicit raise) ...
        with self.assertRaises(BudgetExceeded):
            ledger.reserve("analysis", "too-much", "5.000000001")
        # ... and the raise still applies afterwards while the analysis scope is untouched.
        self.path.write_bytes(before)
        BudgetLedger(self.path)
        budget.raise_judge_analysis_cap(self.path, "approved")
        self.assertEqual(budget.judge_analysis_problems(self.path), [])

    def test_other_cap_differences_still_refuse(self):
        budget = self.budget
        for sql in ("UPDATE scopes SET cap=6000000000 WHERE name='analysis'",
                    "UPDATE scopes SET cap=4000000000 WHERE name='analysis'",
                    "UPDATE scopes SET cap=3000000000 WHERE name='compatibility'"):
            with self.subTest(sql=sql), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "budget.sqlite"
                path.write_bytes(self.path.read_bytes())
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute(sql)
                with self.assertRaises(ValueError):
                    BudgetLedger(path)
        with self.assertRaises(ValueError):  # the tolerance is only $5 opened with exactly $40
            BudgetLedger(self.path, {"analysis": 41})

    def test_raise_cap_delegates_to_the_audited_judge_raise(self):
        budget = self.budget
        for cap, note in ((39, "approved"), (50, "approved"), ("40.000000001", "approved"), (40, " ")):
            with self.subTest(cap=cap, note=note), self.assertRaises(ValueError):
                raise_cap(self.path, "analysis", cap, note)
        self.assertEqual(read_snapshot(self.path)["analysis"]["cap_usd"], "5")
        result = raise_cap(self.path, "analysis", budget.JUDGE_ANALYSIS_CAP_USD, "User approved the $40 judge run")
        self.assertEqual((result["scope"], result["previous_cap_usd"], result["new_cap_usd"]), ("analysis", "5", "40"))
        self.assertTrue(result["note"].startswith(budget.JUDGE_ANALYSIS_PLAN_SECTION))
        self.assertEqual(budget.judge_analysis_problems(self.path), [])

    def test_preflight_cli_applies_the_audited_raise(self):
        import importlib.util
        import io
        from contextlib import redirect_stdout
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("preflight_k1", root / "scripts" / "preflight.py")
        preflight = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preflight)
        lines = []
        self.assertEqual(preflight.apply_cap_raise(self.path, "analysis", "approved", False, out=lines.append), 2)
        self.assertEqual(read_snapshot(self.path)["analysis"]["cap_usd"], "5")
        with redirect_stdout(io.StringIO()):
            code = preflight.apply_cap_raise(self.path, "analysis", "User approved in chat", True, out=lines.append)
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.budget.judge_analysis_problems(self.path), [])
        self.assertTrue(cap_changes(self.path)[-1]["note"].startswith(self.budget.JUDGE_ANALYSIS_PLAN_SECTION))
        self.assertEqual(preflight.apply_cap_raise(self.path, "analysis", "again", True, out=lines.append), 2)
