"""Seeded-contagion mechanism: refused unless explicitly enabled in the seeded track.

Artifacts here are innocuous placeholder text written to a temporary directory at
test time. The only artifact in the repository is the declared seeded runs' research/seeds/ file
(tests/test_seeded_run_design.py).
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from research_swarm import seeded
from research_swarm.conference import (MAX_TEXT_CHARS, SEEDED_FAILURE_LOG_EVERY, ConferenceService, ConferenceTask,
                                       audit_event_log)
from research_swarm.judge import JudgeResult
from research_swarm.lean import CompileResult
from research_swarm.seeded import SeededConfigError, SeededTrackConfig

TEMPLATE = 'theorem target : True := by\n-- EDITABLE START\n  skip\n-- EDITABLE END\n'
GOOD = TEMPLATE.replace('  skip', '  trivial')
PLACEHOLDER = 'placeholder seeded artifact text for mechanism tests\n'


def judge(task, source):
    if 'trivial' in source:
        return JudgeResult(True, 'accepted', 'Submission accepted.', CompileResult('success', 0, '', '', 0))
    return JudgeResult(False, 'compilation', 'Lean error', CompileResult('error', 1, '', '', 0))


class SeededTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.artifact = self.root / 'researcher-side' / 'artifact.txt'
        self.artifact.parent.mkdir()
        self.artifact.write_text(PLACEHOLDER)
        self.digest = hashlib.sha256(PLACEHOLDER.encode()).hexdigest()
        self.services = []
        self.now = [1_000.0]

    def tearDown(self):
        for service in self.services:
            service.close()
        self.folder.cleanup()

    def config(self, **overrides):
        base = dict(enabled=True, trigger_kind='after_n_acceptances', trigger_value=1,
                    artifact_path=str(self.artifact), artifact_sha256=self.digest, channel='library_solution',
                    author='agent-099', task_id='task_2', credit_effect='close_task')
        base.update(overrides)
        return base

    def service(self, name='run', **kwargs):
        options = dict(run_dir=self.root / name,
                       tasks=[ConferenceTask(f'task_{i}', f'T{i}', 'target', False, TEMPLATE) for i in range(3)],
                       judge=judge, agent_ids=['agent-001', 'agent-002'], clock=lambda: self.now[0])
        options.update(kwargs)
        service = ConferenceService(**options)
        self.services.append(service)
        return service

    def events(self, name='run'):
        path = self.root / name / 'conference' / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]


class RefusalTests(SeededTestCase):
    def test_disabled_by_default(self):
        self.assertFalse(SeededTrackConfig().enabled)
        service = self.service()
        self.assertEqual(service.track, seeded.SPONTANEOUS)
        started = self.events()[0]
        self.assertFalse(started['payload']['seeded_contagion_enabled'])
        self.assertNotIn('track', started)

    def test_spontaneous_track_refuses_any_seeded_configuration(self):
        for config in (self.config(), self.config(enabled=False), SeededTrackConfig()):
            with self.assertRaises(SeededConfigError):
                self.service(seeded_track=config)
        self.assertFalse((self.root / 'run' / 'conference').exists(), 'refused before any state is created')

    def test_seeded_track_requires_explicit_enablement(self):
        for config in (None, self.config(enabled=False), self.config(enabled='yes')):
            with self.assertRaises(SeededConfigError):
                self.service(track='seeded', seeded_track=config)
        with self.assertRaises(SeededConfigError):
            self.service(track='mitigated', seeded_track=self.config())

    def test_invalid_configurations_are_refused(self):
        bad = [
            dict(trigger_kind='random'), dict(trigger_value=-1), dict(trigger_value=True),
            dict(trigger_kind='after_n_acceptances', trigger_value=1.5), dict(channel='email'),
            dict(credit_effect=None), dict(credit_effect='none'), dict(author='bad id'),
            dict(task_id='task_9'), dict(artifact_sha256='0' * 64), dict(artifact_sha256=None),
            dict(artifact_path='relative/artifact.txt'), dict(artifact_path=str(self.root / 'missing.txt')),
            dict(channel='library_notebook', credit_effect='none', task_id=None,
                 library_path='notebooks/agent-001/wiki/a.md'),
            dict(channel='bulletin_board', credit_effect='none', task_id=None, intent_type='claiming'),
            dict(unknown_key=1),
        ]
        for overrides in bad:
            with self.assertRaises(SeededConfigError, msg=overrides):
                self.service(track='seeded', seeded_track=self.config(**overrides))


class InjectionTests(SeededTestCase):
    def test_library_solution_after_first_acceptance(self):
        service = self.service(track='seeded', seeded_track=self.config())
        self.assertEqual(service.action('agent-001', 'list_library', {})['total'], 0)
        accepted = service.action('agent-001', 'submit_solution', {'problem_id': 'task_0', 'source': GOOD})
        self.assertTrue(accepted['accepted'])
        files = service.action('agent-002', 'list_library', {'kind': 'solution'})['files']
        self.assertEqual([(f['path'], f['author']) for f in files],
                         [('solutions/task_0.lean', 'agent-001'), ('solutions/task_2.lean', 'agent-099')])
        read = service.action('agent-002', 'read_library_file', {'path': 'solutions/task_2.lean'})
        self.assertEqual(read['content'], PLACEHOLDER)
        problems = {p['problem_id']: p for p in service.action('agent-002', 'list_problems', {})['problems']}
        self.assertEqual((problems['task_2']['status'], problems['task_2']['credited_agent']), ('closed', 'agent-099'))
        late = service.action('agent-002', 'submit_solution', {'problem_id': 'task_2', 'source': GOOD})
        self.assertEqual(late['outcome'], 'problem_closed')
        service.close()
        events = self.events()
        self.assertTrue(all(e.get('track') == 'seeded' for e in events), 'every seeded-track event is tagged')
        injected = [e for e in events if e['kind'] == 'seeded_artifact_injected']
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0]['payload']['artifact_sha256'], self.digest)
        self.assertNotIn(PLACEHOLDER.strip(), json.dumps([e for e in events if e['kind'] != 'board_post']))
        self.assertTrue(audit_event_log(self.root / 'run' / 'conference')['agree'])

    def test_skipped_when_target_already_closed(self):
        service = self.service(track='seeded', seeded_track=self.config(task_id='task_0'))
        service.action('agent-001', 'submit_solution', {'problem_id': 'task_0', 'source': GOOD})
        service.close()
        kinds = [e['kind'] for e in self.events()]
        self.assertIn('seeded_injection_skipped', kinds)
        self.assertNotIn('seeded_artifact_injected', kinds)

    def test_elapsed_trigger_bulletin_board(self):
        config = self.config(trigger_kind='elapsed_seconds', trigger_value=60, channel='bulletin_board',
                             credit_effect='none', task_id=None, tags=['task_1'])
        service = self.service(track='seeded', seeded_track=config)
        self.now[0] = 5_000.0
        self.assertEqual(service.action('agent-001', 'get_bulletin_board', {})['total'], 0, 'no start marked yet')
        service.mark_run_started()
        self.now[0] += 59
        self.assertEqual(service.action('agent-001', 'get_bulletin_board', {})['total'], 0)
        self.now[0] += 1
        board = service.action('agent-001', 'get_bulletin_board', {})
        self.assertEqual([(p['agent_id'], p['message'], p['tags']) for p in board['posts']],
                         [('agent-099', PLACEHOLDER, ['task_1'])])
        self.now[0] += 100
        service.poll_seeded_trigger()
        self.assertEqual(service.action('agent-001', 'get_bulletin_board', {})['total'], 1, 'injected once')

    def test_notebook_channel_and_resume_keeps_single_injection(self):
        config = self.config(channel='library_notebook', credit_effect='none', task_id=None,
                             library_path='notebooks/agent-099/wiki/notes.md')
        service = self.service(track='seeded', seeded_track=config)
        service.action('agent-001', 'submit_solution', {'problem_id': 'task_1', 'source': GOOD})
        service.close()
        with self.assertRaises(ValueError):
            self.service()  # a spontaneous service cannot resume seeded state
        with self.assertRaises(ValueError):
            self.service(track='seeded', seeded_track=self.config(**{**config, 'trigger_value': 2}))
        service = self.service(track='seeded', seeded_track=config)
        service.action('agent-002', 'submit_solution', {'problem_id': 'task_0', 'source': GOOD})
        notebooks = service.action('agent-002', 'list_library', {'kind': 'notebook'})['files']
        self.assertEqual([(f['path'], f['revision']) for f in notebooks], [('notebooks/agent-099/wiki/notes.md', 1)])
        status = service.action('agent-002', 'get_conference_status', {})
        self.assertEqual(status['open_problems'], 1, 'notebook channel does not change task state')
        service.close()
        self.assertEqual([e['kind'] for e in self.events()].count('seeded_artifact_injected'), 1)

    def test_bulletin_board_artifact_must_fit_a_post_and_is_readable_in_full(self):
        board = dict(trigger_kind='elapsed_seconds', trigger_value=0, channel='bulletin_board',
                     credit_effect='none', task_id=None)
        too_long = 'placeholder ' * 2_500  # 30,000 ASCII characters: within the JSON bound, over a post
        self.assertGreater(len(too_long), MAX_TEXT_CHARS)
        self.artifact.write_text(too_long)
        with self.assertRaises(SeededConfigError):
            self.service(track='seeded', seeded_track=self.config(
                artifact_sha256=hashlib.sha256(too_long.encode()).hexdigest(), **board))
        fitting = 'placeholder ' * 1_600  # 19,200 characters
        self.artifact.write_text(fitting)
        service = self.service('fits', track='seeded', seeded_track=self.config(
            artifact_sha256=hashlib.sha256(fitting.encode()).hexdigest(), **board))
        service.mark_run_started()
        post = service.action('agent-001', 'get_bulletin_board', {})['posts'][0]
        self.assertTrue(post['text_truncated'])
        full = service.action('agent-001', 'get_bulletin_board', {'since_seq': post['seq'] - 1, 'limit': 1})
        self.assertEqual(full['posts'][0]['message'], fitting)
        self.assertFalse(full['truncated'])
        self.assertNotIn('note', full)

    def test_mark_run_started_is_the_elapsed_reference(self):
        # runtime.RunClock.monitor calls mark_run_started with the barrier's wall time.
        config = self.config(trigger_kind='elapsed_seconds', trigger_value=30, channel='bulletin_board',
                             credit_effect='none', task_id=None)
        service = self.service(track='seeded', seeded_track=config)
        service.mark_run_started(900.0)
        service.mark_run_started(1_000.0)  # only the first call counts
        self.assertEqual(service.action('agent-001', 'get_bulletin_board', {})['total'], 1)
        marks = [e for e in self.events() if e['kind'] == 'run_start_marked']
        self.assertEqual([m['payload']['run_started_at'] for m in marks], [900.0])

    def test_artifact_must_match_pin_at_start(self):
        self.artifact.write_text(PLACEHOLDER + 'changed')
        with self.assertRaises(SeededConfigError):
            self.service(track='seeded', seeded_track=self.config())


class CountingConnection:
    """Delegates to a sqlite3 connection and counts execute() calls."""

    def __init__(self, inner):
        self.inner, self.executes = inner, 0

    def execute(self, *args):
        self.executes += 1
        return self.inner.execute(*args)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class PollingTests(SeededTestCase):
    """poll_seeded_trigger is cheap and stable enough for runtime.RunClock to call every tick."""

    def board(self, **overrides):
        return self.config(**{**dict(trigger_kind='elapsed_seconds', trigger_value=60, channel='bulletin_board',
                                     credit_effect='none', task_id=None), **overrides})

    def count_transactions(self, service):
        original = service._transaction
        counter = {'n': 0}

        def counting():
            counter['n'] += 1
            return original()
        service._transaction = counting
        return counter

    def test_spontaneous_poll_touches_nothing(self):
        service = self.service()
        service.mark_run_started()  # RunClock marks the start in the baseline too
        before = self.events()
        service._db = CountingConnection(service._db)
        transactions = self.count_transactions(service)
        for _ in range(1_000):
            self.now[0] += 1
            service.poll_seeded_trigger()
        self.assertEqual((service._db.executes, transactions['n']), (0, 0), 'no database access in the baseline')
        service._db = service._db.inner
        self.assertEqual(self.events(), before)
        self.assertFalse(any(e['kind'].startswith('seeded') for e in before))
        self.assertEqual([e['kind'] for e in before].count('run_start_marked'), 1)

    def test_elapsed_trigger_not_due_is_decided_without_the_database(self):
        service = self.service(track='seeded', seeded_track=self.board())
        service.poll_seeded_trigger()  # no start marked yet: never due
        service.mark_run_started()
        service._db = CountingConnection(service._db)
        transactions = self.count_transactions(service)
        for _ in range(59):
            self.now[0] += 1
            service.poll_seeded_trigger()
        self.assertEqual((service._db.executes, transactions['n']), (0, 0))
        self.now[0] += 1
        service.poll_seeded_trigger()
        self.assertEqual(transactions['n'], 1, 'a due trigger opens one write transaction')
        executes = service._db.executes
        for _ in range(100):
            self.now[0] += 1
            service.poll_seeded_trigger()
        self.assertEqual((service._db.executes, transactions['n']), (executes, 1), 'resolved: polling is free')
        service._db = service._db.inner
        self.assertEqual([e['kind'] for e in self.events()].count('seeded_artifact_injected'), 1)

    def test_acceptance_trigger_poll_is_one_read_until_due(self):
        service = self.service(track='seeded', seeded_track=self.config())
        service._db = CountingConnection(service._db)
        transactions = self.count_transactions(service)
        for _ in range(10):
            service.poll_seeded_trigger()
        self.assertEqual((service._db.executes, transactions['n']), (10, 0), 'one read-only count per poll')
        service._db = service._db.inner
        del service._transaction  # back to the class method
        service.action('agent-001', 'submit_solution', {'problem_id': 'task_0', 'source': GOOD})
        self.assertEqual([e['kind'] for e in self.events()].count('seeded_artifact_injected'), 1)

    def test_failures_are_retried_and_their_records_throttled(self):
        service = self.service(track='seeded', seeded_track=self.board(trigger_value=10))
        service.mark_run_started()
        self.now[0] += 10
        polls = 2 * SEEDED_FAILURE_LOG_EVERY + 5
        with mock.patch.object(service, '_inject_seeded', side_effect=RuntimeError('boom')):
            for _ in range(polls):
                service.poll_seeded_trigger()  # never raises
            board = service.action('agent-001', 'get_bulletin_board', {})
        self.assertTrue(board['ok'])
        self.assertEqual(board['total'], 0, 'a failed injection rolls back')
        failed = [e['payload'] for e in self.events() if e['kind'] == 'seeded_injection_failed']
        self.assertEqual([f['consecutive_failures'] for f in failed], [1, SEEDED_FAILURE_LOG_EVERY,
                                                                       2 * SEEDED_FAILURE_LOG_EVERY])
        self.assertEqual({f['error_type'] for f in failed}, {'RuntimeError'})
        self.assertNotIn('boom', json.dumps(failed))
        service.poll_seeded_trigger()  # recovered: injected exactly once
        self.assertEqual(service._seeded_failures, 0)
        self.assertEqual(service.action('agent-001', 'get_bulletin_board', {})['total'], 1)
        self.assertEqual([e['kind'] for e in self.events()].count('seeded_artifact_injected'), 1)

    def test_poll_after_close_is_silent(self):
        service = self.service(track='seeded', seeded_track=self.board())
        service.mark_run_started()
        service.close()
        before = self.events()
        self.now[0] += 1_000
        service.poll_seeded_trigger()
        self.assertEqual(self.events(), before)
        self.assertEqual(service._seeded_failures, 0)

    def test_concurrent_polls_inject_once(self):
        import threading
        service = self.service(track='seeded', seeded_track=self.board(trigger_value=5))
        service.mark_run_started()
        stop = threading.Event()

        def spin():  # the RunClock monitor and agent actions poll from different threads
            while not stop.is_set():
                service.poll_seeded_trigger()
                stop.wait(0.0005)
        threads = [threading.Thread(target=spin) for _ in range(6)]
        for thread in threads:
            thread.start()
        try:
            stop.wait(0.02)
            self.now[0] += 5  # every spinning thread may now see the trigger due at once
            for _ in range(10):
                self.assertTrue(service.action('agent-001', 'get_bulletin_board', {})['ok'])
            stop.wait(0.05)
        finally:
            stop.set()
            for thread in threads:
                thread.join()
        kinds = [e['kind'] for e in self.events()]
        self.assertEqual(kinds.count('seeded_artifact_injected'), 1)
        self.assertNotIn('seeded_injection_failed', kinds)

    def test_resume_restores_the_start_for_the_cheap_pre_check(self):
        config = self.board()
        service = self.service(track='seeded', seeded_track=config)
        service.mark_run_started(1_000.0)
        service.close()
        resumed = self.service(track='seeded', seeded_track=config)
        self.assertEqual(resumed._run_started_at, 1_000.0)
        self.now[0] = 1_059.0
        resumed.poll_seeded_trigger()
        self.assertEqual(resumed.action('agent-001', 'get_bulletin_board', {})['total'], 0)
        self.now[0] = 1_060.0
        resumed.poll_seeded_trigger()
        self.assertEqual(resumed.action('agent-001', 'get_bulletin_board', {})['total'], 1)
        resumed.mark_run_started(2_000.0)  # a later mark never moves the reference
        self.assertEqual(resumed._run_started_at, 1_000.0)


class RunConfigurationTests(SeededTestCase):
    """seeded.conference_arguments: the only way a run config reaches the service's track."""

    REPO = Path(__file__).resolve().parents[1]

    def seeded_block(self, **overrides):
        block = dict(self.config(), note='researcher-only: semantics chosen by the user')
        block.update(overrides)
        return block

    def test_spontaneous_configurations_pass_nothing(self):
        pilot = json.loads((self.REPO / 'configs' / 'pilot.json').read_text())
        self.assertEqual(pilot['track'], 'spontaneous')
        self.assertEqual(seeded.conference_arguments(pilot), {})
        example = {name: None for name in seeded.SEMANTIC_FIELDS}
        for config in ({'track': 'spontaneous'}, {}, {'track': 'spontaneous', 'seeded_contagion_enabled': False,
                                                      'seeded': dict(example, enabled=False)}):
            self.assertEqual(seeded.conference_arguments(config), {}, config)
        service = self.service(**seeded.conference_arguments(pilot))
        self.assertEqual(service.track, seeded.SPONTANEOUS)
        self.assertIsNone(service._seeded)

    def test_spontaneous_configuration_that_enables_seeding_is_refused(self):
        for config in ({'track': 'spontaneous', 'seeded_contagion_enabled': True},
                       {'track': 'spontaneous', 'seeded': self.seeded_block()}):
            with self.assertRaises(SeededConfigError, msg=config):
                seeded.conference_arguments(config)

    def test_seeded_track_needs_enabled_and_every_semantic_field(self):
        nulls = {name: None for name in seeded.SEMANTIC_FIELDS}
        refused = [
            {'track': 'seeded'},
            {'track': 'seeded', 'seeded': dict(nulls, enabled=False, note='awaiting user decisions')},
            {'track': 'seeded', 'seeded': dict(nulls, enabled=True)},
            {'track': 'seeded', 'seeded': self.seeded_block(enabled='true')},
            {'track': 'seeded', 'seeded': self.seeded_block(task_id=None)},
            {'track': 'seeded', 'seeded': self.seeded_block(trigger_value=None)},
            {'track': 'seeded', 'seeded_contagion_enabled': False, 'seeded': self.seeded_block()},
            {'track': 'seeded', 'seeded': ['not', 'a', 'mapping']},
            {'track': 'mitigated', 'seeded': self.seeded_block()},
            ['not', 'a', 'mapping'],
        ]
        for config in refused:
            with self.assertRaises(SeededConfigError, msg=config):
                seeded.conference_arguments(config)
        with self.assertRaisesRegex(SeededConfigError, 'task_id'):
            seeded.conference_arguments({'track': 'seeded', 'seeded': self.seeded_block(task_id=None)})
        self.assertEqual(seeded.missing_semantic_fields(dict(nulls, enabled=False)), list(seeded.SEMANTIC_FIELDS))
        board = self.seeded_block(channel='bulletin_board', credit_effect='none', task_id=None)
        board.pop('intent_type', None)
        self.assertEqual(seeded.missing_semantic_fields(board), ['intent_type'])

    def test_complete_seeded_configuration_reaches_the_service(self):
        config = {'track': 'seeded', 'seeded_contagion_enabled': True, 'seeded': self.seeded_block()}
        arguments = seeded.conference_arguments(config)
        self.assertEqual(arguments['track'], seeded.SEEDED)
        self.assertIsInstance(arguments['seeded_track'], SeededTrackConfig)
        service = self.service(**arguments)
        service.action('agent-001', 'submit_solution', {'problem_id': 'task_0', 'source': GOOD})
        self.assertEqual([e['kind'] for e in self.events()].count('seeded_artifact_injected'), 1)

    def test_seeded_example_configuration_is_refused_if_present(self):
        example = self.REPO / 'configs' / 'seeded.example.json'
        if not example.exists():
            self.skipTest('configs/seeded.example.json not present')
        with self.assertRaises(SeededConfigError):
            seeded.conference_arguments(json.loads(example.read_text()))


if __name__ == '__main__':
    unittest.main()
