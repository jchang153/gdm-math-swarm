"""Offline approval-boundary regressions; synthetic temporary ledgers only."""
import datetime
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from research_swarm import budget, launch_gates


class CachedRepeatBudgetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'budget.sqlite'
        ledger = budget.BudgetLedger(self.path, {'pilot':20,'analysis':5,'compatibility':2})
        ledger.settle(ledger.reserve('pilot', 'first',20), '19.636804044')
        budget.raise_cap(self.path,'pilot',budget.FIRST_REPEAT_CAP_USD,'Historical approval fixture')
        ledger = budget.BudgetLedger(self.path, {'pilot':budget.FIRST_REPEAT_CAP_USD,'analysis':5,'compatibility':2})
        ledger.settle(ledger.reserve('pilot','second',90),'86.091194430')
        for amount in ['.3'] * 6 + ['.5276232']:
            ledger.settle(ledger.reserve('pilot','old-uncertain',amount),None)
        with closing(budget._read_only(self.path)) as db:
            old_digest = budget._pilot_calls_sha256(db)
        with mock.patch.object(budget,'EIGHT_GIB_PRIOR_CALLS_SHA256',old_digest):
            budget.raise_cap(self.path,'pilot',budget.EIGHT_GIB_CAP_USD,'Historical eight GiB fixture approval')
        ledger = budget.BudgetLedger(self.path, {'pilot':budget.EIGHT_GIB_CAP_USD,'analysis':5,'compatibility':2})
        ledger.settle(ledger.reserve('pilot','third','99.4'),'99.321801318')
        ledger.settle(ledger.reserve('pilot','new-uncertain','.43223715'),None)
        with closing(budget._read_only(self.path)) as db:
            self.digest = budget._pilot_calls_sha256(db)
        patch = mock.patch.object(budget,'CACHE_REPEAT_PRIOR_CALLS_SHA256',self.digest)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(launch_gates,'CACHE_REPEAT_PRIOR_CALLS_SHA256',self.digest)
        patch.start()
        self.addCleanup(patch.stop)
        self.config = {
            'pilot_prior_settled_usd':budget.CACHE_REPEAT_PRIOR_SETTLED_USD,
            'pilot_prior_held_usd':budget.CACHE_REPEAT_PRIOR_HELD_USD,
            'pilot_prior_committed_usd':budget.CACHE_REPEAT_PRIOR_COMMITTED_USD,
            'pilot_additional_allowance_usd':'100', 'pilot_prior_uncertain_requests':8,
            'pilot_prior_calls_sha256':self.digest, 'prompt_cache_normalization':'counter-user-v1'}

    def gate(self, config=None):
        gates = launch_gates._Gates(self.root,self.config if config is None else config,None,
                                   datetime.datetime.now(datetime.timezone.utc),None)
        gates.ledger(self.path,self.root/'absent-legacy.sqlite')
        return gates.results['ledger']

    def raise_new(self):
        return budget.raise_cap(self.path,'pilot',budget.CACHE_REPEAT_CAP_USD,'User approved cached fresh $100 run')

    def test_exact_hundred_and_all_prior_rows_unchanged(self):
        before = budget.list_requests(self.path, states=('settled','uncertain'))
        self.assertFalse(self.gate()['passed'])
        self.raise_new()
        self.assertTrue(self.gate()['passed'],self.gate())
        self.assertEqual(budget.list_requests(self.path, states=('settled','uncertain')),before)
        ledger = budget.BudgetLedger(self.path)
        pilot = ledger.snapshot()['pilot']
        self.assertEqual((pilot['remaining_usd'],pilot['held_usd'],pilot['uncertain_requests']),('100','2.75986035',8))
        self.assertEqual(len(budget.cap_changes(self.path)),3)
        with self.assertRaises(ValueError): self.raise_new()
        with self.assertRaises(budget.BudgetExceeded): ledger.reserve('pilot','too-much','100.000000001')
        ledger.reserve('pilot','exact',100)
        with self.assertRaises(budget.BudgetExceeded): ledger.reserve('pilot','more','.000000001')

    def test_refuses_changed_rows_holds_spending_and_audit_history(self):
        original = self.path.read_bytes()
        for sql in ("UPDATE calls SET agent='changed' WHERE agent='first'",
                    "UPDATE calls SET actual=actual+1 WHERE agent='third'",
                    "UPDATE calls SET reserved=reserved-1 WHERE agent='new-uncertain'",
                    "UPDATE calls SET state='reserved' WHERE agent='new-uncertain'",
                    "UPDATE calls SET state='settled',actual=0 WHERE agent='new-uncertain'",
                    "DELETE FROM calls WHERE agent='first'", "DELETE FROM cap_changes",
                    "UPDATE cap_changes SET previous_cap=21000000000 WHERE scope='pilot'"):
            with self.subTest(sql=sql):
                self.path.write_bytes(original)
                with sqlite3.connect(self.path) as db: db.execute(sql)
                with self.assertRaises(ValueError): self.raise_new()
                self.assertEqual(budget.read_snapshot(self.path)['pilot']['cap_usd'],budget.EIGHT_GIB_CAP_USD)

    def test_replacement_ledger_and_wrong_hash_refused(self):
        replacement = self.root/'replacement.sqlite'
        budget.BudgetLedger(replacement)
        self.assertTrue(budget.cache_repeat_prior_problems(replacement))
        with self.assertRaises(ValueError): budget.raise_cap(replacement,'pilot',budget.CACHE_REPEAT_CAP_USD,'Approved')
        with mock.patch.object(budget,'CACHE_REPEAT_PRIOR_CALLS_SHA256','0'*64):
            with self.assertRaisesRegex(ValueError,'request history'): self.raise_new()
        self.assertTrue(budget.cache_repeat_prior_problems(self.root/'missing.sqlite'))

    def test_launch_requires_every_exact_metadata_field(self):
        self.raise_new()
        for key in self.config:
            for replacement in (None,'incorrect'):
                config = dict(self.config)
                if replacement is None: del config[key]
                else: config[key] = replacement
                with self.subTest(key=key,replacement=replacement):
                    result = self.gate(config)
                    self.assertFalse(result['passed'])
                    self.assertIn('metadata',result['reason'])

    def test_launch_detects_post_raise_hold_change_and_spending(self):
        self.raise_new()
        original = self.path.read_bytes()
        for sql in ("UPDATE calls SET agent='changed' WHERE agent='first'",
                    "UPDATE calls SET state='settled',actual=0 WHERE agent='new-uncertain'",
                    "DELETE FROM cap_changes"):
            with self.subTest(sql=sql):
                self.path.write_bytes(original)
                with sqlite3.connect(self.path) as db: db.execute(sql)
                self.assertFalse(self.gate()['passed'])
        self.path.write_bytes(original)
        budget.BudgetLedger(self.path).reserve('pilot','unexpected','.01')
        self.assertFalse(self.gate()['passed'])
