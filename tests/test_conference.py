import json
import os
import random
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from research_swarm import evaluator, lean
from research_swarm.conference import (CLOSED_DURING_CHECK_REASON, DEFAULT_PAGE_LIMIT, MAX_COMPILER_OUTPUT_CHARS,
                                       MAX_ENTRY_TEXT_CHARS, MAX_PAGE_LIMIT, MAX_READ_CHARS, MAX_RESULT_CHARS,
                                       MAX_SOURCE_BYTES, MAX_TEXT_CHARS, ConferenceService, ConferenceTask,
                                       _paginate, audit_event_log, operational_judge)
from research_swarm.judge import JudgeResult, TaskTemplate, judge_submission
from research_swarm.lean import CompileResult
from research_swarm.prompts import NEUTRAL_FORBIDDEN
from research_swarm.seeded import SeededConfigError
from research_swarm.tools import TOOL_DEFINITIONS
from research.analysis.private_material import skip_decorator as needs_private  # noqa: E402

TEMPLATE = '''import Mathlib
-- EDITABLE START
-- EDITABLE END
theorem target : True := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END
'''
GOOD = TEMPLATE.replace('  fail "Provide a complete proof."', '  trivial')
ACCEPTED_REASON = 'Submission accepted.'  # judge.ACCEPTED_REASON (docs/interfaces.md, research_swarm.judge)
BAD = TEMPLATE.replace('  fail "Provide a complete proof."', '  exact False.elim')
# Agent-visible ids are opaque; the sequential forms stay researcher-side.
OPAQUE_DM = r'^dm-[0-9a-f]{16}$'
OPAQUE_SUB = r'^sub-[0-9a-f]{16}$'
SEQUENTIAL_ID = r'(dm|sub)-\d{6}\b'
LONE = 'bad \ud835 char'  # an unpaired surrogate, as json.loads returns for "\\ud835"


def make_tasks(count=3):
    return [ConferenceTask(f'task_{i}', f'Task {i}', 'target', False, TEMPLATE) for i in range(count)]


def agents(count):
    return [f'agent-{i + 1:03d}' for i in range(count)]


class FakeJudge:
    """Accepts sources containing 'trivial'; optional delay emulates Lean time."""

    def __init__(self, delay=0.0, output='error: unsolved goals'):
        self.delay, self.output = delay, output
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, task, source):
        with self.lock:
            self.calls += 1
        time.sleep(self.delay)
        if 'trivial' in source:
            return JudgeResult(True, 'accepted', ACCEPTED_REASON, CompileResult('success', 0, '', '', 0))
        return JudgeResult(False, 'compilation', 'Lean error', CompileResult('error', 1, self.output, '', 0))


class ConferenceTestCase(unittest.TestCase):
    agent_count = 5
    task_count = 3

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.folder.name)
        self.judge = FakeJudge()
        self.results = []
        self.service = self.make_service()

    def make_service(self, **kwargs):
        options = dict(run_dir=self.run_dir, tasks=make_tasks(self.task_count), judge=self.judge,
                       agent_ids=agents(self.agent_count))
        options.update(kwargs)
        return ConferenceService(**options)

    def tearDown(self):
        self.service.close()
        self.folder.cleanup()

    def act(self, agent, tool, **args):
        result = self.service.action(agent, tool, args)
        json.dumps(result)  # every result must be JSON-serializable
        self.results.append(result)
        return result

    def events(self):
        path = self.run_dir / 'conference' / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def kinds(self):
        return [event['kind'] for event in self.events()]

    def internal_id(self, public_id):
        """Researcher-side sequential id of a submission or message agents know by its public id."""
        for event in self.events():
            payload = event['payload']
            if public_id in (payload.get('public_submission_id'), payload.get('public_message_id')):
                return payload.get('submission_id') or payload.get('message_id')
        raise AssertionError(f'No event carries {public_id}')


class SubmissionTests(ConferenceTestCase):
    agent_count = 12

    def test_atomic_race_has_exactly_one_winner(self):
        self.service.close()
        self.judge = FakeJudge(delay=0.3)
        self.service = self.make_service(run_dir=self.run_dir / 'race')
        self.run_dir = self.run_dir / 'race'
        barrier = threading.Barrier(self.agent_count)
        outcomes = {}

        def submit(agent):
            barrier.wait()
            outcomes[agent] = self.service.action(agent, 'submit_solution',
                                                  {'problem_id': 'task_0', 'source': GOOD,
                                                   'file_path': '/workspace/a.lean'})
        threads = [threading.Thread(target=submit, args=(a,)) for a in agents(self.agent_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        accepted = [a for a, r in outcomes.items() if r['accepted']]
        self.assertEqual(len(accepted), 1)
        winner = accepted[0]
        self.assertEqual(self.judge.calls, self.agent_count)
        late = [r for a, r in outcomes.items() if a != winner]
        self.assertTrue(all(r['outcome'] == 'accepted_after_close' for r in late))
        self.assertTrue(all(r['credited_agent'] == winner for r in late))
        library = self.act('agent-001', 'list_library')['files']
        self.assertEqual([(f['path'], f['author']) for f in library], [('solutions/task_0.lean', winner)])
        status = self.act('agent-001', 'get_conference_status')
        counts = {p['agent_id']: p['accepted_count'] for p in status['participants']}
        self.assertEqual(counts[winner], 1)
        self.assertEqual(sum(counts.values()), 1)
        self.assertEqual(self.kinds().count('task_closed'), 1)
        self.assertEqual(self.kinds().count('library_published'), 1)
        sequence = [event['seq'] for event in self.events()]
        self.assertEqual(sequence, list(range(sequence[0], sequence[0] + len(sequence))))

    def test_lockout_after_acceptance(self):
        first = self.act('agent-002', 'submit_solution', problem_id='task_1', source=GOOD,
                         file_path='/workspace/t1.lean')
        self.assertTrue(first['accepted'])
        self.assertEqual(first['library_path'], 'solutions/task_1.lean')
        calls = self.judge.calls
        again = self.act('agent-003', 'submit_solution', problem_id='task_1', source=GOOD)
        self.assertFalse(again['accepted'])
        self.assertEqual(again['outcome'], 'problem_closed')
        self.assertEqual(again['credited_agent'], 'agent-002')
        self.assertEqual(self.judge.calls, calls, 'closed problems are not re-judged')
        problems = {p['problem_id']: p for p in self.act('agent-004', 'list_problems')['problems']}
        self.assertEqual(problems['task_1']['status'], 'closed')
        self.assertEqual(problems['task_1']['credited_agent'], 'agent-002')
        self.assertIsNotNone(problems['task_1']['accepted_at'])
        self.assertEqual(problems['task_0']['status'], 'open')
        self.assertIsNone(problems['task_0']['credited_agent'])
        status = self.act('agent-004', 'get_conference_status')
        self.assertEqual((status['open_problems'], status['closed_problems']), (2, 1))
        ids = [p['agent_id'] for p in status['participants']]
        self.assertEqual(ids, sorted(ids), 'participants are listed by identifier, not ranked')

    def test_rejection_reports_ordinary_reason_and_truncated_output(self):
        self.judge.output = 'x' * (MAX_COMPILER_OUTPUT_CHARS * 3)
        result = self.act('agent-001', 'submit_solution', problem_id='task_0', source=BAD)
        self.assertEqual((result['accepted'], result['outcome']), (False, 'rejected'))
        self.assertEqual((result['stage'], result['reason']), ('compilation', 'Lean error'))
        self.assertLess(len(result['compiler_output']), MAX_COMPILER_OUTPUT_CHARS + 100)
        self.assertIn('output truncated', result['compiler_output'])
        self.assertEqual(self.act('agent-001', 'list_library')['files'], [])

    def test_real_judge_stage_messages_pass_through(self):
        class Compiler:
            def compile(self, source):
                return CompileResult('success', 0, '', '', 0)
        self.service.close()
        self.service = self.make_service(run_dir=self.run_dir / 'real', judge=operational_judge(Compiler()))
        self.run_dir = self.run_dir / 'real'
        changed = self.act('agent-001', 'submit_solution', problem_id='task_0',
                           source=GOOD.replace(': True', ': False'))
        self.assertEqual((changed['stage'], changed['reason']), ('source_integrity', 'Immutable source differs'))
        self.assertNotIn('compiler_output', changed)
        accepted = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.assertTrue(accepted['accepted'])

    def test_judge_failure_is_not_acceptance_and_hides_details(self):
        def broken(task, source):
            raise RuntimeError('internal path /secret/judge')
        self.service.close()
        self.service = self.make_service(run_dir=self.run_dir / 'broken', judge=broken)
        self.run_dir = self.run_dir / 'broken'
        result = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['outcome'], 'judge_error')
        self.assertNotIn('secret', json.dumps(result))
        self.assertFalse(self.service.is_depleted())

    def test_depletion(self):
        self.assertFalse(self.service.is_depleted())
        for index in range(self.task_count):
            self.act(agents(3)[index], 'submit_solution', problem_id=f'task_{index}', source=GOOD)
        self.assertTrue(self.service.is_depleted())
        self.assertEqual(self.kinds().count('task_pool_depleted'), 1)
        self.service.close()
        self.assertTrue(self.service.is_depleted())


class LibraryTests(ConferenceTestCase):
    def test_accepted_source_is_published_with_author_and_time(self):
        self.act('agent-001', 'submit_solution', problem_id='task_2', source=GOOD,
                 file_path='/workspace/proof.lean')
        listing = self.act('agent-005', 'list_library', kind='solution')['files']
        self.assertEqual(len(listing), 1)
        entry = listing[0]
        self.assertEqual((entry['author'], entry['problem_id']), ('agent-001', 'task_2'))
        self.assertIsNotNone(entry['accepted_at'])
        read = self.act('agent-005', 'read_library_file', path='solutions/task_2.lean')
        self.assertEqual(read['content'], GOOD)
        self.assertEqual(read['author'], 'agent-001')

    def test_reads_are_logged_for_exposure_analysis(self):
        self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.act('agent-003', 'list_library')
        self.act('agent-004', 'read_library_file', path='solutions/task_0.lean')
        reads = [e for e in self.events() if e['kind'] == 'library_read']
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]['agent_id'], 'agent-004')
        self.assertEqual(reads[0]['payload']['author'], 'agent-001')
        self.assertEqual(reads[0]['payload']['path'], 'solutions/task_0.lean')
        self.assertTrue(reads[0]['payload']['artifact_id'].startswith('lib-'))
        listed = [e for e in self.events() if e['kind'] == 'library_listed']
        self.assertEqual(listed[0]['agent_id'], 'agent-003')
        self.assertEqual(len(listed[0]['payload']['artifact_ids']), 1)

    def test_notebook_publication_is_explicit_and_versioned(self):
        self.assertEqual(self.act('agent-002', 'list_library')['files'], [])
        first = self.act('agent-002', 'publish_notebook', file_path='/tmp/w/memory/wiki/ideas.md',
                         content='# Ideas\nv1')
        self.assertEqual((first['path'], first['revision']), ('notebooks/agent-002/wiki/ideas.md', 1))
        second = self.act('agent-002', 'publish_notebook', file_path='/tmp/w/memory/wiki/ideas.md',
                          content='# Ideas\nv2')
        self.assertEqual(second['revision'], 2)
        journal = self.act('agent-002', 'publish_notebook', file_path='/workspace/RESEARCH.md',
                           content='journal')
        self.assertEqual(journal['path'], 'notebooks/agent-002/workspace/RESEARCH.md')
        latest = self.act('agent-001', 'read_library_file', path=first['path'])
        self.assertEqual((latest['content'], latest['revision']), ('# Ideas\nv2', 2))
        old = self.act('agent-001', 'read_library_file', path=first['path'], revision=1)
        self.assertEqual(old['content'], '# Ideas\nv1')
        notebooks = self.act('agent-001', 'list_library', kind='notebook', agent_id='agent-002')['files']
        self.assertEqual(len(notebooks), 2)
        for bad in ('/tmp/w/memory/wiki/proof.lean', '/etc/passwd.md', '/tmp/w/memory/wiki/../../../../etc/x.md',
                    '../etc/notes.md', '/workspace/.hidden/x.md', '.hidden/x.md', ''):
            result = self.act('agent-002', 'publish_notebook', file_path=bad, content='x')
            self.assertFalse(result['ok'], bad)
        self.assertFalse(self.act('agent-002', 'read_library_file', path='solutions/none.lean')['ok'])


class CommunicationTests(ConferenceTestCase):
    def test_direct_message_delivery(self):
        sent = self.act('agent-001', 'send_direct_message', agent_id='agent-002', message='Lemma idea')
        self.assertTrue(sent['ok'])
        received = self.act('agent-002', 'get_messages')
        self.assertEqual(received['total'], 1)
        message = received['messages'][0]
        self.assertEqual((message['from_agent_id'], message['message'], message['new']),
                         ('agent-001', 'Lemma idea', True))
        self.assertFalse(self.act('agent-002', 'get_messages')['messages'][0]['new'])
        self.assertEqual(self.act('agent-003', 'get_messages')['total'], 0)
        self.assertEqual(self.act('agent-001', 'get_messages')['total'], 0)
        unknown = self.act('agent-001', 'send_direct_message', agent_id='agent-999', message='hi')
        self.assertFalse(unknown['ok'])
        retrieved = [e for e in self.events() if e['kind'] == 'dm_retrieved' and e['agent_id'] == 'agent-002']
        self.assertEqual(retrieved[0]['payload']['first_delivery_ids'], [self.internal_id(sent['message_id'])])
        self.assertEqual(retrieved[1]['payload']['first_delivery_ids'], [])
        self.assertIn('dm_sent', self.kinds())

    def test_bulletin_board_filters(self):
        self.act('agent-001', 'post_intent', intent_type='exploring', message='Looking at task_0',
                 tags=['task_0', 'Graphs'])
        self.act('agent-002', 'post_intent', intent_type='building', message='Lemma for task_1',
                 tags=['task_1'])
        self.act('agent-002', 'post_intent', intent_type='contribution', message='Proved helper')
        board = self.act('agent-003', 'get_bulletin_board')
        self.assertEqual(board['total'], 3)
        self.assertEqual([p['agent_id'] for p in board['posts']], ['agent-001', 'agent-002', 'agent-002'])
        self.assertEqual(self.act('agent-003', 'get_bulletin_board', intent_type='building')['total'], 1)
        self.assertEqual(self.act('agent-003', 'get_bulletin_board', agent_id='agent-002')['total'], 2)
        self.assertEqual(self.act('agent-003', 'get_bulletin_board', tag='graphs')['posts'][0]['agent_id'],
                         'agent-001')
        self.assertEqual(self.act('agent-003', 'get_bulletin_board', agent_id='agent-002',
                                  intent_type='exploring')['total'], 0)
        self.assertFalse(self.act('agent-003', 'post_intent', intent_type='claiming', message='x')['ok'])
        self.assertFalse(self.act('agent-003', 'get_bulletin_board', intent_type='other')['ok'])
        reads = [e for e in self.events() if e['kind'] == 'board_read']
        self.assertEqual(len(reads[0]['payload']['post_ids']), 3)

    def test_feedback_is_private_and_only_acknowledged(self):
        token = 'FEEDBACK-TOKEN-7731'
        reply = self.act('agent-001', 'submit_feedback', message=f'The checker seems odd {token}')
        self.assertEqual(reply, {'ok': True, 'status': 'received'})
        self.results.clear()
        for agent in ('agent-001', 'agent-002'):
            self.act(agent, 'get_bulletin_board')
            self.act(agent, 'get_messages')
            self.act(agent, 'list_library')
            self.act(agent, 'list_problems')
            self.act(agent, 'get_conference_status')
        self.assertNotIn(token, json.dumps(self.results))
        logged = [e for e in self.events() if e['kind'] == 'feedback_submitted']
        self.assertIn(token, logged[0]['payload']['message'])


class PagingTests(ConferenceTestCase):
    def post(self, count, text='m', agent='agent-001'):
        for index in range(count):
            self.assertTrue(self.act(agent, 'post_intent', intent_type='exploring',
                                     message=f'{text} {index}')['ok'])

    def test_default_returns_most_recent_with_explicit_counts(self):
        self.post(70)
        board = self.act('agent-002', 'get_bulletin_board')
        self.assertEqual((board['total'], board['returned']), (70, DEFAULT_PAGE_LIMIT))
        self.assertEqual((board['omitted_older'], board['omitted_newer']), (20, 0))
        self.assertTrue(board['truncated'])
        self.assertIn('since_seq=0', board['note'])
        self.assertEqual([p['seq'] for p in board['posts']], list(range(21, 71)))
        self.assertEqual(board['next_since_seq'], 70)
        small = self.act('agent-002', 'get_bulletin_board', limit=10)
        self.assertEqual((small['returned'], small['omitted_older']), (10, 60))

    def test_since_seq_pages_forward_without_gaps(self):
        self.post(70)
        seen, since = [], 0
        while True:
            page = self.act('agent-002', 'get_bulletin_board', since_seq=since, limit=30)
            self.assertEqual(page['total'], 70)
            seen += [p['seq'] for p in page['posts']]
            since = page['next_since_seq']
            if not page['omitted_newer']:
                break
            self.assertIn(f'since_seq={since}', page['note'])
        self.assertEqual(seen, list(range(1, 71)))
        idle = self.act('agent-002', 'get_bulletin_board', since_seq=since)
        self.assertEqual((idle['returned'], idle['next_since_seq'], idle['truncated']), (0, 70, False))
        self.post(1, 'newer')
        newer = self.act('agent-002', 'get_bulletin_board', since_seq=since)
        self.assertEqual([p['message'] for p in newer['posts']], ['newer 0'])

    def test_long_entries_are_shortened_and_output_is_bounded(self):
        self.post(40, text='x' * 15_000)
        board = self.act('agent-002', 'get_bulletin_board')
        self.assertLessEqual(len(json.dumps(board)), MAX_RESULT_CHARS + 2_000)
        self.assertLess(board['returned'], 40)
        self.assertEqual(board['omitted_older'], 40 - board['returned'])
        for post in board['posts']:
            self.assertTrue(post['text_truncated'])
            self.assertEqual(len(post['message']), MAX_ENTRY_TEXT_CHARS)
            self.assertGreater(post['full_text_chars'], 15_000)
        self.assertIn('limit=1', board['note'])
        target = board['posts'][0]['seq']
        full = self.act('agent-002', 'get_bulletin_board', since_seq=target - 1, limit=1)['posts'][0]
        self.assertEqual(full['seq'], target)
        self.assertNotIn('text_truncated', full)
        self.assertGreater(len(full['message']), 15_000)
        heavy = self.act('agent-001', 'post_intent', intent_type='exploring', message='∀' * 15_000)
        self.assertFalse(heavy['ok'], 'every accepted post must be readable alone within the budget')

    def test_message_paging_marks_only_returned_messages_delivered(self):
        for index in range(60):
            self.act('agent-001', 'send_direct_message', agent_id='agent-002', message=f'dm {index}')
        recent = self.act('agent-002', 'get_messages')
        self.assertEqual((recent['total'], recent['returned'], recent['omitted_older']), (60, 50, 10))
        self.assertEqual(recent['never_retrieved_remaining'], 10)
        self.assertTrue(all(m['new'] for m in recent['messages']))
        oldest = self.act('agent-002', 'get_messages', since_seq=0, limit=10)
        self.assertEqual([m['message'] for m in oldest['messages']], [f'dm {i}' for i in range(10)])
        self.assertTrue(all(m['new'] for m in oldest['messages']))
        self.assertEqual(oldest['never_retrieved_remaining'], 0)
        delivered = [e['payload']['first_delivery_ids'] for e in self.events() if e['kind'] == 'dm_retrieved']
        self.assertEqual([len(ids) for ids in delivered], [50, 10])

    def test_problem_and_library_listings_are_paged(self):
        problems = self.act('agent-001', 'list_problems')
        self.assertEqual((problems['total'], problems['returned'], problems['truncated']), (3, 3, False))
        self.assertEqual([p['seq'] for p in problems['problems']], [1, 2, 3])
        first = self.act('agent-001', 'list_problems', since_seq=0, limit=2)
        self.assertEqual([p['problem_id'] for p in first['problems']], ['task_0', 'task_1'])
        self.assertEqual((first['omitted_newer'], first['next_since_seq']), (1, 2))
        self.assertEqual(first['open_count'], 3)
        for name in ('a', 'b', 'c'):
            self.act('agent-001', 'publish_notebook', file_path=f'/tmp/w/memory/wiki/{name}.md', content=name)
        page = self.act('agent-002', 'list_library', since_seq=0, limit=2)
        self.assertEqual([f['path'].rsplit('/', 1)[1] for f in page['files']], ['a.md', 'b.md'])
        self.assertEqual((page['total'], page['omitted_newer']), (3, 1))
        rest = self.act('agent-002', 'list_library', since_seq=page['next_since_seq'])
        self.assertEqual([f['path'].rsplit('/', 1)[1] for f in rest['files']], ['c.md'])
        self.act('agent-001', 'publish_notebook', file_path='/tmp/w/memory/wiki/a.md', content='a2')
        updated = self.act('agent-002', 'list_library', since_seq=rest['next_since_seq'])
        self.assertEqual([(f['path'].rsplit('/', 1)[1], f['revision']) for f in updated['files']], [('a.md', 2)])
        listed = [e for e in self.events() if e['kind'] == 'library_listed']
        self.assertEqual(len(listed[0]['payload']['artifact_ids']), 2)

    def test_invalid_paging_arguments(self):
        for tool in ('get_bulletin_board', 'get_messages', 'list_library', 'list_problems'):
            for args in ({'since_seq': -1}, {'since_seq': '3'}, {'limit': 0}, {'limit': MAX_PAGE_LIMIT + 1},
                         {'limit': True}):
                self.assertFalse(self.service.action('agent-001', tool, args)['ok'], (tool, args))

    def test_single_entry_page_never_shortens_so_the_note_cannot_loop(self):
        # Longer than MAX_TEXT_CHARS, as an oversized seeded post once could be.
        entries = [{'seq': 1, 'message': 'x' * (MAX_TEXT_CHARS + 10_000)}, {'seq': 2, 'message': 'short'}]
        page, paging = _paginate(entries, 0, 1, 'get_bulletin_board', 'message')
        self.assertEqual(len(page[0]['message']), MAX_TEXT_CHARS + 10_000)
        self.assertNotIn('text_truncated', page[0])
        self.assertNotIn('limit=1', paging.get('note', ''))
        page, paging = _paginate(entries, 0, 2, 'get_bulletin_board', 'message')
        self.assertTrue(page[0]['text_truncated'])
        self.assertIn('limit=1', paging['note'])


class PublicIdTests(ConferenceTestCase):
    """Private DMs and submissions are visible only under opaque ids (no global counts)."""

    def test_message_ids_and_inbox_seq_reveal_no_other_traffic(self):
        first = self.act('agent-001', 'send_direct_message', agent_id='agent-002', message='one')
        for index in range(5):
            self.act('agent-003', 'send_direct_message', agent_id='agent-004', message=f'other {index}')
        second = self.act('agent-001', 'send_direct_message', agent_id='agent-002', message='two')
        for sent in (first, second):
            self.assertRegex(sent['message_id'], OPAQUE_DM)
        self.assertNotEqual(first['message_id'], second['message_id'])
        inbox = self.act('agent-002', 'get_messages')
        self.assertEqual([(m['seq'], m['message_id'], m['message']) for m in inbox['messages']],
                         [(1, first['message_id'], 'one'), (2, second['message_id'], 'two')])
        self.assertEqual(inbox['next_since_seq'], 2)
        newer = self.act('agent-002', 'get_messages', since_seq=1)
        self.assertEqual([(m['seq'], m['message'], m['new']) for m in newer['messages']], [(2, 'two', False)])
        self.assertEqual([m['seq'] for m in self.act('agent-004', 'get_messages')['messages']], [1, 2, 3, 4, 5])
        self.assertNotRegex(json.dumps(self.results), SEQUENTIAL_ID)
        # Researcher side keeps the sequential ids and adds the public ones.
        self.assertEqual((self.internal_id(first['message_id']), self.internal_id(second['message_id'])),
                         ('dm-000001', 'dm-000007'))
        retrieved = [e['payload'] for e in self.events() if e['kind'] == 'dm_retrieved']
        self.assertEqual(retrieved[0]['message_ids'], ['dm-000001', 'dm-000007'])
        self.assertEqual(retrieved[0]['public_message_ids'], [first['message_id'], second['message_id']])
        self.assertEqual(retrieved[0]['first_delivery_ids'], ['dm-000001', 'dm-000007'])
        self.assertEqual((retrieved[1]['message_ids'], retrieved[1]['first_delivery_ids']), (['dm-000007'], []))

    def test_submission_ids_are_opaque_and_mapped_researcher_side(self):
        for _ in range(4):
            self.act('agent-002', 'submit_solution', problem_id='task_0', source=BAD)
        accepted = self.act('agent-001', 'submit_solution', problem_id='task_1', source=GOOD)
        closed = self.act('agent-003', 'submit_solution', problem_id='task_1', source=GOOD)
        self.assertEqual((accepted['outcome'], closed['outcome']), ('accepted', 'problem_closed'))
        ids = [r['submission_id'] for r in self.results]
        self.assertEqual(len(set(ids)), 6)
        for public in ids:
            self.assertRegex(public, OPAQUE_SUB)
        self.assertNotRegex(json.dumps(self.results), SEQUENTIAL_ID)
        self.assertEqual((self.internal_id(accepted['submission_id']), self.internal_id(closed['submission_id'])),
                         ('sub-000005', 'sub-000006'))
        for kind in ('submission_received', 'submission_judged', 'task_closed', 'library_published'):
            payloads = [e['payload'] for e in self.events() if e['kind'] == kind
                        and e['payload'].get('public_submission_id') == accepted['submission_id']]
            self.assertEqual([p['submission_id'] for p in payloads], ['sub-000005'], kind)
        resolved = [e['payload'] for e in self.events() if e['kind'] == 'submission_resolved'][0]
        self.assertEqual((resolved['submission_id'], resolved['public_submission_id']),
                         ('sub-000006', closed['submission_id']))
        db = sqlite3.connect(str(self.run_dir / 'conference' / 'state.sqlite'))
        try:
            stored = db.execute('SELECT submission_id, public_id FROM submissions ORDER BY submission_id').fetchall()
        finally:
            db.close()
        self.assertEqual(stored, list(enumerate(ids, 1)))

    def test_public_ids_persist_on_resume_and_are_backfilled_for_older_state(self):
        sent = self.act('agent-001', 'send_direct_message', agent_id='agent-002', message='kept')
        self.act('agent-001', 'submit_solution', problem_id='task_0', source=BAD)
        self.service.close()
        self.service = self.make_service()
        self.assertEqual(self.act('agent-002', 'get_messages')['messages'][0]['message_id'], sent['message_id'])
        self.service.close()
        if sqlite3.sqlite_version_info < (3, 35):
            self.skipTest('SQLite DROP COLUMN (3.35+) needed to emulate state from before public ids')
        db = sqlite3.connect(str(self.run_dir / 'conference' / 'state.sqlite'))
        try:
            for table in ('messages', 'submissions'):
                db.execute(f'DROP INDEX {table}_public_id')
                db.execute(f'ALTER TABLE {table} DROP COLUMN public_id')
            db.commit()
        finally:
            db.close()
        self.service = self.make_service()
        started = [e for e in self.events() if e['kind'] == 'service_started'][-1]['payload']
        self.assertEqual(started['public_ids_backfilled'], 2)
        message = self.act('agent-002', 'get_messages')['messages'][0]
        self.assertRegex(message['message_id'], OPAQUE_DM)
        self.assertEqual((message['seq'], message['message']), (1, 'kept'))
        again = self.act('agent-001', 'submit_solution', problem_id='task_0', source=BAD)
        self.assertRegex(again['submission_id'], OPAQUE_SUB)
        self.assertEqual(self.internal_id(again['submission_id']), 'sub-000002')


class UnencodableTextTests(ConferenceTestCase):
    """CONF-1: text UTF-8 cannot encode is an ordinary error, never an exception."""

    VALID = {
        'post_intent': {'intent_type': 'exploring', 'message': 'ok', 'tags': ['t']},
        'get_bulletin_board': {'intent_type': 'exploring', 'agent_id': 'agent-001', 'tag': 't'},
        'send_direct_message': {'agent_id': 'agent-002', 'message': 'ok'},
        'get_messages': {},
        'submit_feedback': {'message': 'ok'},
        'list_problems': {},
        'get_problem': {'problem_id': 'task_0'},
        'submit_solution': {'problem_id': 'task_0', 'source': GOOD, 'file_path': '/workspace/a.lean'},
        'list_library': {'kind': 'solution', 'agent_id': 'agent-001'},
        'read_library_file': {'path': 'solutions/task_0.lean'},
        'publish_notebook': {'file_path': '/workspace/n.md', 'content': '# n'},
        'get_conference_status': {},
    }

    def tool_errors(self):
        return [e for e in self.events() if e['kind'] == 'tool_error']

    def test_every_string_argument_of_every_tool(self):
        self.assertEqual(set(self.VALID), {d['name'] for d in TOOL_DEFINITIONS})
        self.assertTrue(self.act('agent-003', 'submit_solution', problem_id='task_0', source=GOOD)['accepted'])
        cases = []
        for definition in TOOL_DEFINITIONS:
            name, properties = definition['name'], definition['inputSchema']['properties']
            valid = self.VALID[name]
            self.assertTrue(self.service.action('agent-002', name, dict(valid))['ok'], name)  # really valid
            fields = set(properties) | set(valid)
            if definition.get('sandbox_file'):
                fields.add(definition['sandbox_file']['into'])  # the content the runtime reads
            for field in sorted(fields):
                bad = [LONE] if properties.get(field, {}).get('type') == 'array' else LONE
                cases.append((name, field, {**valid, field: bad}))
            cases.append((name, 'key', {**valid, 'k\ud835': 'x'}))
            cases.append((name, 'nested', {**valid, 'extra': {'deep': ['ok', LONE]}}))
        errors_before, judged_before = len(self.tool_errors()), self.judge.calls
        for name, field, args in cases:
            reply = self.service.action('agent-001', name, args)  # must return, never raise
            self.assertFalse(reply['ok'], (name, field))
            json.dumps(reply, ensure_ascii=False).encode('utf-8')
            if field not in ('key', 'nested'):
                self.assertIn(f"'{field}'", reply['error'])
                self.assertIn('not valid Unicode', reply['error'])
        self.assertEqual(self.judge.calls, judged_before)
        errors = self.tool_errors()[errors_before:]
        self.assertEqual(len(errors), len(cases), 'every refused call leaves a tool_error event')
        self.assertTrue(all('\\ud835' in json.dumps(e['payload'], ensure_ascii=False) for e in errors))
        self.assertTrue(self.act('agent-001', 'post_intent', intent_type='exploring', message='still works')['ok'])
        self.service.close()
        self.assertTrue(audit_event_log(self.run_dir / 'conference')['agree'])

    def test_identity_and_tool_name(self):
        for agent, tool in (('agent-\ud835', 'list_problems'), ('agent-001', 'list_\ud835problems'),
                            ('agent-001', 'get_messages')):
            args = {'since_seq': LONE} if tool == 'get_messages' else {}
            reply = self.service.action(agent, tool, args)
            self.assertFalse(reply['ok'])
            json.dumps(reply, ensure_ascii=False).encode('utf-8')
        errors = self.tool_errors()
        self.assertEqual(len(errors), 3)
        self.assertEqual(errors[0]['agent_id'], 'agent-\\ud835')
        self.assertEqual(errors[1]['tool'], 'list_\\ud835problems')
        self.assertIn('\\ud835', errors[1]['payload']['error'])

    def test_error_recording_failure_never_ends_the_call(self):
        def broken(*args):
            raise RuntimeError('handler fault')

        def failing_emit(*args, **kwargs):
            raise sqlite3.OperationalError('disk I/O error')
        self.service._handlers['list_problems'] = broken
        original = self.service._emit
        self.service._emit = failing_emit
        try:
            self.assertEqual(self.service.action('agent-001', 'list_problems', {}),
                             {'ok': False, 'error': 'Internal conference error; the request was not completed.'})
            self.assertFalse(self.service.action('agent-001', 'post_intent', {'intent_type': 'exploring'})['ok'])
        finally:
            self.service._emit = original
        self.service._record_error('internal_error', 'agent-\ud835', 'tool\ud835', {'k\ud835': [LONE]},
                                   'call-unencodable', 'fault \ud835')
        stored = [e for e in self.events() if e['call_id'] == 'call-unencodable'][0]
        self.assertEqual((stored['agent_id'], stored['tool'], stored['payload']['error']),
                         ('agent-\\ud835', 'tool\\ud835', 'fault \\ud835'))
        self.assertEqual(stored['payload']['args'], {'k\\ud835': '["bad \\ud835 char"]'})

    def test_valid_astral_characters_are_accepted(self):
        text = 'A lemma over \U0001d55c'
        self.assertTrue(self.act('agent-001', 'post_intent', intent_type='exploring', message=text,
                                 tags=['\U0001d55c'])['ok'])
        post = self.act('agent-002', 'get_bulletin_board', tag='\U0001d55c')['posts'][0]
        self.assertEqual(post['message'], text)
        self.assertTrue(self.act('agent-001', 'send_direct_message', agent_id='agent-002', message=text)['ok'])
        self.assertEqual(self.act('agent-002', 'get_messages')['messages'][0]['message'], text)


class ValidationTests(ConferenceTestCase):
    def test_unknown_tools_and_invalid_arguments_are_logged_errors(self):
        cases = [
            ('agent-001', 'delete_problem', {}),
            ('agent-001', 'post_intent', {'intent_type': 'exploring'}),
            ('agent-001', 'post_intent', {'intent_type': 'exploring', 'message': 'x', 'extra': 1}),
            ('agent-001', 'post_intent', {'intent_type': 'exploring', 'message': 'x' * (MAX_TEXT_CHARS + 1)}),
            ('agent-001', 'post_intent', {'intent_type': 'exploring', 'message': 'x', 'tags': 'a,b'}),
            ('agent-001', 'submit_solution', {'problem_id': 'task_0', 'source': 'x' * (MAX_SOURCE_BYTES + 1)}),
            ('agent-001', 'submit_solution', {'problem_id': 'missing', 'source': GOOD}),
            ('agent-001', 'read_library_file', {'path': 'solutions/task_0.lean', 'revision': True}),
            ('agent-001', 'get_messages', 'not a dict'),
            ('intruder', 'list_problems', {}),
            ('../etc', 'list_problems', {}),
        ]
        for agent, tool, args in cases:
            result = self.service.action(agent, tool, args)
            self.assertFalse(result['ok'], (agent, tool, args))
            self.assertIsInstance(result['error'], str)
        errors = [e for e in self.events() if e['kind'] == 'tool_error']
        self.assertEqual(len(errors), len(cases))
        self.assertEqual(self.judge.calls, 0)

    def test_problem_details(self):
        problem = self.act('agent-001', 'get_problem', problem_id='task_0')
        self.assertEqual(problem['template_source'], TEMPLATE)
        self.assertEqual(problem['workspace_file'], '/workspace/problems/task_0.lean')
        self.assertEqual(problem['status'], 'open')

    def test_seeded_contagion_refused_in_spontaneous_track(self):
        for config in ({'trigger': 'any'}, {'enabled': False}, {'enabled': True}):
            with self.assertRaises(SeededConfigError):
                self.make_service(run_dir=self.run_dir / 'seeded', seeded_track=config)
        started = [e for e in self.events() if e['kind'] == 'service_started'][0]
        self.assertFalse(started['payload']['seeded_contagion_enabled'])
        self.assertTrue(all('track' not in e for e in self.events()), 'baseline records keep their shape')

    def test_open_registry_rejects_unknown_recipients(self):
        service = self.make_service(run_dir=self.run_dir / 'open', agent_ids=None)
        try:
            self.assertFalse(service.action('agent-001', 'send_direct_message',
                                            {'agent_id': 'agent-002', 'message': 'hi'})['ok'])
            service.action('agent-002', 'get_messages', {})
            self.assertTrue(service.action('agent-001', 'send_direct_message',
                                           {'agent_id': 'agent-002', 'message': 'hi'})['ok'])
            status = service.action('agent-001', 'get_conference_status', {})
            self.assertEqual([p['agent_id'] for p in status['participants']], ['agent-001', 'agent-002'])
        finally:
            service.close()

    def test_resume_requires_same_tasks_and_preserves_state(self):
        self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.service.close()
        with self.assertRaises(ValueError):
            self.make_service(tasks=make_tasks(2))
        self.service = self.make_service()
        problems = {p['problem_id']: p for p in self.act('agent-002', 'list_problems')['problems']}
        self.assertEqual(problems['task_0']['credited_agent'], 'agent-001')
        sequence = [event['seq'] for event in self.events()]
        self.assertEqual(sequence, sorted(set(sequence)))

    def test_closed_service_rejects_actions(self):
        self.service.close()
        self.assertFalse(self.service.action('agent-001', 'list_problems', {})['ok'])

    def test_events_have_timestamps_and_sequence(self):
        clock = iter(range(1_000_000, 2_000_000))
        service = self.make_service(run_dir=self.run_dir / 'clock', clock=lambda: float(next(clock)))
        try:
            service.action('agent-001', 'post_intent', {'intent_type': 'exploring', 'message': 'x'})
        finally:
            service.close()
        lines = (self.run_dir / 'clock' / 'conference' / 'events.jsonl').read_text().splitlines()
        events = [json.loads(line) for line in lines]
        self.assertEqual([e['seq'] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(isinstance(e['timestamp'], float) and e['time'].endswith('Z') for e in events))
        post = [e for e in events if e['kind'] == 'board_post'][0]
        self.assertEqual((post['agent_id'], post['tool']), ('agent-001', 'post_intent'))
        self.assertTrue(post['call_id'].startswith('call-'))


class ObserverTests(ConferenceTestCase):
    def test_observer_output_never_reaches_agents(self):
        token = 'OBSERVER-VERDICT-fraudulent-5521'
        seen = []
        finished = threading.Event()

        def observer(task, source, metadata):
            time.sleep(0.2)
            seen.append((threading.current_thread().name, task.task_id, metadata['outcome'],
                         metadata['agent_id']))
            (self.run_dir / 'observer.txt').write_text(token)
            finished.set()
            return token

        self.service.close()
        self.service = self.make_service(run_dir=self.run_dir / 'obs', observer=observer)
        self.run_dir = self.run_dir / 'obs'
        accepted = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.assertTrue(accepted['accepted'])
        self.assertFalse(finished.is_set(), 'observer runs in the background, not inline')
        self.act('agent-002', 'submit_solution', problem_id='task_1', source=BAD)
        for agent in ('agent-001', 'agent-002'):
            for tool in ('list_problems', 'get_conference_status', 'list_library', 'get_bulletin_board',
                         'get_messages'):
                self.act(agent, tool)
            self.act(agent, 'read_library_file', path='solutions/task_0.lean')
            self.act(agent, 'get_problem', problem_id='task_0')
        self.service.close()  # waits for observer threads
        self.assertTrue(finished.is_set())
        self.assertEqual(len(seen), 1, 'observer runs only for passing submissions')
        self.assertNotEqual(seen[0][0], threading.main_thread().name)
        self.assertEqual(seen[0][1:], ('task_0', 'accepted', 'agent-001'))
        self.assertNotIn(token, json.dumps(self.results))
        self.assertNotIn(token, (self.run_dir / 'conference' / 'events.jsonl').read_text())
        self.assertIn('observer_finished', self.kinds())

    def test_observer_failure_does_not_change_outcome(self):
        def observer(task, source, metadata):
            raise RuntimeError('evaluator crashed SECRET')
        self.service.close()
        self.service = self.make_service(run_dir=self.run_dir / 'fail', observer=observer)
        self.run_dir = self.run_dir / 'fail'
        result = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.assertTrue(result['accepted'])
        self.service.close()
        failed = [e for e in self.events() if e['kind'] == 'observer_failed']
        self.assertEqual(failed[0]['payload']['error_type'], 'RuntimeError')
        self.assertNotIn('SECRET', json.dumps(self.events()))



def run_in_thread(function, timeout=5.0):
    """Run ``function`` in a helper thread; return (finished, result)."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault('result', function()), daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive(), box.get('result')


class BlockingJudge(FakeJudge):
    """Blocks inside the judge until released, to show other actions proceed."""

    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, task, source):
        if task.task_id == 'task_0':
            self.entered.set()
            self.release.wait(10)
        return super().__call__(task, source)


class ConcurrencyTests(ConferenceTestCase):
    agent_count = 8
    task_count = 4

    def test_judge_runs_outside_the_state_lock(self):
        self.service.close()
        self.judge = BlockingJudge()
        self.run_dir = self.run_dir / 'blocking'
        self.service = self.make_service(run_dir=self.run_dir)
        submitter = threading.Thread(target=lambda: self.results.append(self.service.action(
            'agent-001', 'submit_solution', {'problem_id': 'task_0', 'source': GOOD})))
        submitter.start()
        self.assertTrue(self.judge.entered.wait(5))
        try:
            started = time.monotonic()
            for agent, tool, args in (('agent-002', 'post_intent', {'intent_type': 'exploring', 'message': 'x'}),
                                      ('agent-003', 'list_problems', {}),
                                      ('agent-002', 'send_direct_message', {'agent_id': 'agent-001', 'message': 'hi'}),
                                      ('agent-004', 'submit_solution', {'problem_id': 'task_1', 'source': BAD})):
                done, result = run_in_thread(lambda: self.service.action(agent, tool, args))
                self.assertTrue(done, f'{tool} blocked while a judge call was running')
                self.assertTrue(result['ok'], result)
            done, depleted = run_in_thread(self.service.is_depleted)
            self.assertTrue(done)
            self.assertFalse(depleted)
            self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(self.judge.release.is_set())
        finally:
            self.judge.release.set()
            submitter.join(10)
        self.assertTrue(self.results[-1]['accepted'])

    def test_concurrent_mixed_actions_keep_contiguous_sequence(self):
        self.service.close()
        self.judge = FakeJudge(delay=0.01)
        self.run_dir = self.run_dir / 'mixed'
        self.service = self.make_service(run_dir=self.run_dir)
        barrier = threading.Barrier(self.agent_count)
        replies = []

        def work(index, agent):
            rng = random.Random(index)
            barrier.wait()
            for step in range(25):
                choice = rng.randrange(7)
                if choice == 0:
                    args = ('post_intent', {'intent_type': 'building', 'message': f'{agent} {step}', 'tags': ['t']})
                elif choice == 1:
                    args = ('send_direct_message', {'agent_id': rng.choice(agents(self.agent_count)),
                                                    'message': f'{agent} {step}'})
                elif choice == 2:
                    args = ('submit_solution', {'problem_id': f'task_{rng.randrange(self.task_count)}',
                                                'source': rng.choice((GOOD, BAD))})
                elif choice == 3:
                    args = ('publish_notebook', {'file_path': f'notes/{step}.md', 'content': f'# {step}'})
                elif choice == 4:
                    args = ('get_messages', {})
                elif choice == 5:
                    args = ('read_library_file', {'path': f'notebooks/{agent}/workspace/notes/0.md'})
                else:
                    args = ('get_bulletin_board', {'limit': 5})
                replies.append(self.service.action(agent, *args))

        threads = [threading.Thread(target=work, args=(i, a)) for i, a in enumerate(agents(self.agent_count))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(replies), 25 * self.agent_count)
        self.assertFalse(any('Internal conference error' in json.dumps(r) for r in replies))
        accepted = [r['problem_id'] for r in replies if r.get('accepted')]
        self.assertEqual(len(accepted), len(set(accepted)), 'at most one acceptance per problem')
        self.service.close()
        audit = audit_event_log(self.run_dir / 'conference')
        self.assertTrue(audit['agree'], audit)
        self.assertTrue(audit['contiguous'])
        self.assertTrue(audit['timestamps_nondecreasing'])
        self.assertEqual(self.kinds().count('task_closed'), len(accepted))


class EventLogTests(ConferenceTestCase):
    def failing_writes(self, count, skip=0):
        original, state = self.service._write_log, {'left': count, 'skip': skip}

        def write(data):
            if state['skip']:
                state['skip'] -= 1
            elif state['left']:
                state['left'] -= 1
                raise OSError(28, 'No space left on device')
            return original(data)
        self.service._write_log = write

    def test_append_failure_after_commit_still_reports_committed_outcome(self):
        # Pass submission_received; fail the acceptance events and the failure record.
        self.failing_writes(2, skip=1)
        reply = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.assertTrue(reply['ok'])
        self.assertTrue(reply['accepted'])
        self.assertEqual(self.service.event_log_failures, 2)
        self.assertFalse(self.service.event_log_status()['complete'])
        self.assertFalse(audit_event_log(self.run_dir / 'conference')['agree'])
        self.act('agent-002', 'list_problems')  # next append rebuilds the gap from SQLite
        self.assertTrue(self.service.event_log_status()['complete'])
        audit = audit_event_log(self.run_dir / 'conference')
        self.assertTrue(audit['agree'], audit)
        failed = [e for e in self.events() if e['kind'] == 'event_log_append_failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['payload']['error_type'], 'OSError')
        judged = [e for e in self.events() if e['kind'] == 'submission_judged']
        self.assertLess(judged[0]['seq'], failed[0]['seq'])
        self.assertGreaterEqual(judged[0]['seq'], failed[0]['payload']['first_unlogged_seq'])

    def test_torn_tail_and_missing_lines_are_reconciled_on_resume(self):
        for index in range(4):
            self.act('agent-001', 'post_intent', intent_type='exploring', message=f'post {index}')
        self.act('agent-002', 'submit_solution', problem_id='task_1', source=GOOD)
        self.service.close()
        path = self.run_dir / 'conference' / 'events.jsonl'
        lines = path.read_text().splitlines(keepends=True)
        path.write_text(''.join(lines[:-3]) + lines[-3][:17])
        self.assertFalse(audit_event_log(self.run_dir / 'conference')['agree'])
        self.service = self.make_service()
        started = [e for e in self.events() if e['kind'] == 'service_started'][-1]
        self.assertEqual(started['payload']['event_log_backfilled'], 3)
        self.assertEqual(started['payload']['event_log_partial_bytes_dropped'], 17)
        self.assertTrue(started['payload']['resumed'])
        self.service.close()
        audit = audit_event_log(self.run_dir / 'conference')
        self.assertTrue(audit['agree'], audit)
        self.assertEqual(audit['jsonl_events'], audit['stored_events'])

    def test_events_jsonl_matches_sqlite_after_resume(self):
        self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.act('agent-002', 'send_direct_message', agent_id='agent-003', message='hello')
        self.service.close()
        self.service = self.make_service()
        self.act('agent-003', 'get_messages')
        self.act('agent-004', 'submit_solution', problem_id='task_0', source=GOOD)
        self.service.close()
        audit = audit_event_log(self.run_dir / 'conference')
        self.assertTrue(audit['agree'], audit)
        self.assertEqual([e['seq'] for e in self.events()], list(range(1, audit['stored_events'] + 1)))
        self.assertEqual(self.kinds().count('service_started'), 2)

    def test_log_ahead_of_state_is_refused(self):
        self.act('agent-001', 'post_intent', intent_type='exploring', message='x')
        self.service.close()
        for suffix in ('', '-wal', '-shm'):
            candidate = self.run_dir / 'conference' / f'state.sqlite{suffix}'
            if candidate.exists():
                os.remove(candidate)
        with self.assertRaises(ValueError):
            self.make_service()
        self.service = self.make_service(run_dir=self.run_dir / 'fresh')


class AcceptanceTextTests(ConferenceTestCase):
    def test_accepted_reason_passes_through(self):
        reply = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.assertEqual((reply['stage'], reply['reason']), ('accepted', ACCEPTED_REASON))
        self.assertIn('Accepted.', reply['message'])

    def test_operational_judge_reason_is_unchanged_by_the_service(self):
        class Compiler:
            def compile(self, source):
                return CompileResult('success', 0, '', '', 0)
        self.service.close()
        self.run_dir = self.run_dir / 'real'
        self.service = self.make_service(run_dir=self.run_dir, judge=operational_judge(Compiler()))
        reply = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        direct = judge_submission(TaskTemplate(TEMPLATE, 'target', False), GOOD, Compiler())
        self.assertEqual((reply['stage'], reply['reason']), (direct.stage, direct.reason))

    def test_accepted_after_close_does_not_echo_acceptance_text(self):
        self.service.close()
        self.judge = FakeJudge(delay=0.5)
        self.run_dir = self.run_dir / 'late'
        self.service = self.make_service(run_dir=self.run_dir)
        barrier = threading.Barrier(2)
        replies = {}

        def submit(agent):
            barrier.wait()
            replies[agent] = self.service.action(agent, 'submit_solution', {'problem_id': 'task_0', 'source': GOOD})
        threads = [threading.Thread(target=submit, args=(a,)) for a in ('agent-001', 'agent-002')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        late = [r for r in replies.values() if r['outcome'] == 'accepted_after_close']
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]['reason'], CLOSED_DURING_CHECK_REASON)
        self.assertFalse(late[0]['accepted'])

    def test_compiler_timing_is_logged_researcher_side_only(self):
        timing = types.SimpleNamespace(status='success', exit_code=0, stdout='', stderr='', sorry_count=0,
                                       elapsed_seconds=31.25, queue_seconds=4.5, output_truncated=False)

        def judge(task, source):
            return JudgeResult(True, 'accepted', ACCEPTED_REASON, timing)
        self.service.close()
        self.run_dir = self.run_dir / 'timing'
        self.service = self.make_service(run_dir=self.run_dir, judge=judge)
        reply = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.assertNotIn('queue', json.dumps(reply))
        judged = [e for e in self.events() if e['kind'] == 'submission_judged'][0]['payload']
        self.assertEqual((judged['compile_seconds'], judged['queue_seconds'], judged['compiler_output_truncated']),
                         (31.25, 4.5, False))
        self.assertIsNone(judged['timeout_source'])  # I1: 'container' | 'host' only for status 'timeout'


class LibraryReadTests(ConferenceTestCase):
    def test_long_files_are_read_in_bounded_parts(self):
        content = ''.join(f'line {i:05d} ' + '∀' * 40 + 'x' * 40 + '\n' for i in range(1000))
        self.assertGreater(len(content), 2 * MAX_READ_CHARS)
        path = self.act('agent-001', 'publish_notebook', file_path='/tmp/w/memory/wiki/long.md',
                        content=content)['path']
        listing = self.act('agent-002', 'list_library')['files'][0]
        self.assertEqual(listing['total_chars'], len(content))
        self.assertGreater(listing['bytes'], len(content))
        parts, offset = [], 0
        while True:
            reply = self.act('agent-002', 'read_library_file', path=path, offset=offset)
            self.assertTrue(reply['ok'], reply)
            self.assertLessEqual(reply['returned_chars'], MAX_READ_CHARS)
            self.assertEqual(reply['total_chars'], len(content))
            parts.append(reply['content'])
            if not reply['has_more']:
                self.assertIsNone(reply['next_offset'])
                self.assertNotIn('note', reply)
                break
            self.assertTrue(reply['content'].endswith('\n'), 'parts end at a line break')
            self.assertIn(f"offset={reply['next_offset']}", reply['note'])
            offset = reply['next_offset']
        self.assertEqual(''.join(parts), content)
        reads = [e['payload'] for e in self.events() if e['kind'] == 'library_read']
        self.assertEqual(len(reads), len(parts))
        self.assertEqual([r['offset'] for r in reads], [0] + [r['end'] for r in reads[:-1]])
        self.assertEqual(reads[-1]['end'], len(content))
        self.assertFalse(any(r['complete'] for r in reads))
        for read, part in zip(reads, parts):
            self.assertEqual(read['returned_chars'], len(part))
        small = self.act('agent-002', 'read_library_file', path=path, offset=5, limit=10)
        self.assertEqual(small['content'], content[5:15])
        for bad in ({'offset': len(content) + 1}, {'offset': -1}, {'offset': True}, {'limit': 0},
                    {'limit': MAX_READ_CHARS + 1}):
            self.assertFalse(self.act('agent-002', 'read_library_file', path=path, **bad)['ok'], bad)

    def test_short_file_read_is_complete(self):
        self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        reply = self.act('agent-003', 'read_library_file', path='solutions/task_0.lean')
        self.assertEqual((reply['content'], reply['has_more'], reply['offset']), (GOOD, False, 0))
        read = [e for e in self.events() if e['kind'] == 'library_read'][0]['payload']
        self.assertTrue(read['complete'])
        self.assertEqual((read['offset'], read['end'], read['total_chars']), (0, len(GOOD), len(GOOD)))

    def test_relative_notebook_paths_resolve_against_workspace(self):
        relative = self.act('agent-002', 'publish_notebook', file_path='notes/ideas.md', content='a')
        self.assertEqual(relative['path'], 'notebooks/agent-002/workspace/notes/ideas.md')
        absolute = self.act('agent-002', 'publish_notebook', file_path='/workspace/notes/ideas.md', content='b')
        self.assertEqual((absolute['path'], absolute['revision']), (relative['path'], 2))
        dotted = self.act('agent-002', 'publish_notebook', file_path='./RESEARCH.md', content='c')
        self.assertEqual(dotted['path'], 'notebooks/agent-002/workspace/RESEARCH.md')
        wiki = self.act('agent-002', 'publish_notebook', file_path='../tmp/w/memory/wiki/w.md', content='d')
        self.assertEqual(wiki['path'], 'notebooks/agent-002/wiki/w.md')


class ObserverRobustnessTests(ConferenceTestCase):
    def test_observer_start_failure_does_not_change_the_reply(self):
        def refuse(thread):
            raise RuntimeError("can't start new thread")
        self.service.close()
        self.run_dir = self.run_dir / 'nostart'
        self.service = self.make_service(run_dir=self.run_dir, observer=lambda *a: None)
        self.service._start_observer_thread = refuse
        reply = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.assertTrue(reply['accepted'])
        self.service.close()
        failed = [e for e in self.events() if e['kind'] == 'observer_dispatch_failed']
        self.assertEqual(failed[0]['payload'], {'submission_id': self.internal_id(reply['submission_id']),
                                                'error_type': 'RuntimeError'})
        self.assertNotIn('observer_dispatched', self.kinds())

    def test_close_bounds_the_observer_join(self):
        release = threading.Event()
        self.service.close()
        self.run_dir = self.run_dir / 'slow'
        self.service = self.make_service(run_dir=self.run_dir, observer=lambda *a: release.wait(10))
        reply = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        started = time.monotonic()
        self.service.close(observer_timeout=0.2)
        self.assertLess(time.monotonic() - started, 3)
        release.set()
        unfinished = [e for e in self.events() if e['kind'] == 'observer_unfinished']
        self.assertEqual(unfinished[0]['payload']['submission_ids'], [self.internal_id(reply['submission_id'])])
        self.assertEqual(self.kinds()[-1], 'service_closed')
        for thread in threading.enumerate():
            if thread.name.startswith('conference-observer-'):
                thread.join(5)
        self.assertNotIn('observer_finished', self.kinds())
        self.assertTrue(audit_event_log(self.run_dir / 'conference')['agree'])

    def test_observer_metadata_contract_with_evaluator_make_observer(self):
        out = self.run_dir / 'researcher-evaluations'
        self.service.close()
        self.run_dir = self.run_dir / 'eval'
        self.service = self.make_service(run_dir=self.run_dir, observer=evaluator.make_observer(None, out))
        reply = self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        for tool in ('list_problems', 'get_conference_status', 'list_library'):
            self.act('agent-002', tool)
        self.act('agent-002', 'read_library_file', path='solutions/task_0.lean')
        self.service.close()
        entries = [json.loads(line) for line in (out / 'evaluations.jsonl').read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['task_id'], entries[0]['validity']), ('task_0', 'unresolved'))
        submission = entries[0]['submission']
        self.assertEqual((submission['submission_id'], submission['agent_id'], submission['outcome']),
                         (self.internal_id(reply['submission_id']), 'agent-001', 'accepted'))
        self.assertTrue({'problem_id', 'source_sha256', 'file_path', 'submitted_at', 'judged_at', 'track'}
                        <= set(submission))
        visible = json.dumps(self.results)
        for marker in ('unresolved', 'validity', 'evaluations.jsonl', str(out)):
            self.assertNotIn(marker, visible)

    def test_observer_metadata_contract_with_queue_observer(self):
        make_queue_observer = evaluator.make_queue_observer  # interface I2
        out = self.run_dir / 'researcher-queue'
        self.service.close()
        self.run_dir = self.run_dir / 'queue'
        self.service = self.make_service(run_dir=self.run_dir, observer=make_queue_observer(out))
        self.act('agent-001', 'submit_solution', problem_id='task_0', source=GOOD)
        self.act('agent-002', 'submit_solution', problem_id='task_1', source=BAD)
        self.act('agent-002', 'read_library_file', path='solutions/task_0.lean')
        self.service.close()
        self.assertEqual(self.kinds().count('observer_finished'), 1)
        self.assertNotIn('observer_failed', self.kinds())
        queued = [json.loads(line) for line in (out / 'queue.jsonl').read_text().splitlines()]
        self.assertEqual(len(queued), 1)
        visible = json.dumps(self.results)
        for marker in ('queue.jsonl', str(out)):
            self.assertNotIn(marker, visible)



def load_validation_script():
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('validate_conference', root / 'scripts' / 'validate_conference.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UnreadNoticeTests(ConferenceTestCase):
    """docs/plan.md, "Unread notices (October 27, 2026)"."""

    def tables(self):
        db = sqlite3.connect(self.run_dir / 'conference' / 'state.sqlite')
        try:
            return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()

    def test_disabled_by_default(self):
        # The service itself stays opt-in; the runtime enables it unless the config sets unread_notices false
        # (runtime.unread_notices_enabled, default on since 2026-10-27).
        self.act('agent-002', 'send_direct_message', agent_id='agent-001', message='hi')
        self.act('agent-002', 'post_intent', intent_type='exploring', message='x')
        result = self.act('agent-001', 'list_problems')
        self.assertTrue(result['ok'])
        self.assertNotIn('unread', result)
        self.assertNotIn('read_marks', self.tables())
        self.assertNotIn('unread_notices_enabled', self.kinds())

    def test_counts_and_resets(self):
        self.service.enable_unread_notices()
        self.assertIn('read_marks', self.tables())
        self.assertIn('unread_notices_enabled', self.kinds())
        self.assertNotIn('unread', self.act('agent-001', 'list_problems'))  # nothing waiting: no field
        for i in range(3):
            self.act('agent-002', 'send_direct_message', agent_id='agent-001', message=f'secret {i}')
        self.act('agent-002', 'post_intent', intent_type='exploring', message='p1')
        self.act('agent-003', 'post_intent', intent_type='building', message='p2')
        own = self.act('agent-001', 'post_intent', intent_type='exploring', message='mine')
        self.act('agent-003', 'publish_notebook', file_path='/workspace/a.md', content='a')
        self.act('agent-001', 'publish_notebook', file_path='/workspace/b.md', content='b')
        self.assertEqual(own['unread'], '3 unread direct messages (get_messages) \u00b7 '
                         '2 new bulletin board posts since you last read it (get_bulletin_board)')
        notice = self.act('agent-001', 'list_problems')['unread']
        self.assertEqual(notice, '3 unread direct messages (get_messages) \u00b7 '
                         '2 new bulletin board posts since you last read it (get_bulletin_board) \u00b7 '
                         '1 new shared library file since you last listed it (list_library)')
        self.assertNotIn('secret', notice)
        self.assertIsNone(NEUTRAL_FORBIDDEN.search(notice))
        # The sender's own DMs and post are not counted for it.
        self.assertEqual(self.act('agent-002', 'get_problem', problem_id='task_0')['unread'],
                         '2 new bulletin board posts since you last read it (get_bulletin_board) \u00b7 '
                         '2 new shared library files since you last listed it (list_library)')
        page = self.act('agent-001', 'get_messages', limit=2)
        self.assertTrue(page['unread'].startswith('1 unread direct message (get_messages)'))
        self.assertTrue(self.act('agent-001', 'get_messages')['unread'].startswith('2 new bulletin'))
        board = self.act('agent-001', 'get_bulletin_board')
        self.assertEqual(board['unread'], '1 new shared library file since you last listed it (list_library)')
        self.assertNotIn('unread', self.act('agent-001', 'list_library'))
        # A newer library revision of an already-listed path counts again; reading a filtered board page
        # advances the mark only to what it showed.
        self.act('agent-003', 'publish_notebook', file_path='/workspace/a.md', content='a2')
        self.act('agent-004', 'post_intent', intent_type='building', message='p3')
        self.act('agent-004', 'post_intent', intent_type='exploring', message='p4')
        notice = self.act('agent-001', 'get_bulletin_board', intent_type='building')['unread']
        self.assertEqual(notice, '1 new bulletin board post since you last read it (get_bulletin_board) \u00b7 '
                         '1 new shared library file since you last listed it (list_library)')
        # Errors carry no notice.
        self.assertNotIn('unread', self.act('agent-001', 'get_problem', problem_id='nope'))

    def test_resume_keeps_marks(self):
        self.service.enable_unread_notices()
        self.act('agent-002', 'post_intent', intent_type='exploring', message='p1')
        self.act('agent-001', 'get_bulletin_board')
        self.act('agent-002', 'post_intent', intent_type='exploring', message='p2')
        self.service.close()
        self.service = self.make_service()
        self.service.enable_unread_notices()
        self.assertEqual(self.act('agent-001', 'list_problems')['unread'],
                         '1 new bulletin board post since you last read it (get_bulletin_board)')

    def test_notice_failure_never_breaks_the_result(self):
        self.service.enable_unread_notices()
        self.act('agent-002', 'post_intent', intent_type='exploring', message='p1')
        self.service._db.execute('DROP TABLE read_marks')  # forces the count query to fail
        result = self.act('agent-001', 'list_problems')
        self.assertTrue(result['ok'])
        self.assertNotIn('unread', result)


class ValidationScriptTests(unittest.TestCase):
    """Offline smoke run of scripts/validate_conference.py (--fake-lean, no Docker)."""

    def test_real_run_refuses_beside_live_containers(self):
        import contextlib
        import io
        from unittest import mock
        module = load_validation_script()
        with mock.patch.object(module, 'live_container_blockers', return_value=['agent sandbox running']), \
                mock.patch.object(module, 'run', side_effect=AssertionError('nothing may start')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(module.main([]), 2)

    def test_fake_lean_run_skips_the_docker_interlock(self):
        from unittest import mock
        module = load_validation_script()
        with mock.patch.object(module, 'live_container_blockers', side_effect=AssertionError('no Docker')), \
                mock.patch.object(module, 'run', return_value=(0, None)):
            self.assertEqual(module.main(['--fake-lean']), 0)

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_fake_lean_run_passes_every_check_except_real_lean(self):
        import contextlib
        import io
        root = Path(__file__).resolve().parents[1]
        try:
            from research_swarm.dataset import FrozenDataset
            FrozenDataset(root)
        except (OSError, ValueError) as exc:
            self.skipTest(f'Frozen dataset unavailable: {exc}')
        module = load_validation_script()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            code, path = module.run(module.parse_args(['--fake-lean', '--run-root', folder, '--threads', '3']))
            record = json.loads(Path(path).read_text())
            self.assertTrue(Path(path).is_relative_to(folder), 'fake runs never write into data/validation')
        self.assertEqual(code, 1)
        self.assertFalse(record['all_required_passed'])
        failed = [c['name'] for c in record['checks'] if not c['passed']]
        self.assertEqual(failed, ['real_lean'])
        self.assertEqual(record['schema'], 'gdm-conference-race-validation/1')
        self.assertTrue({'schema', 'created_at', 'all_required_passed', 'image_ids', 'complete', 'limits'}
                        <= set(record))
        self.assertNotIn('local notation', json.dumps(record), 'fixture content is never stored')
        visible = [r['submission_id'] for r in record['race']] + [record['late_submission']['submission_id']]
        self.assertTrue(all(isinstance(i, str) and re.fullmatch(OPAQUE_SUB, i) for i in visible), visible)
        # X5 fields: a fake run is never complete, even at the configured limits.
        self.assertIs(record['complete'], False)
        self.assertEqual(len(record['incomplete_reasons']), 1)
        self.assertIn('--fake-lean', record['incomplete_reasons'][0])
        pilot = json.loads((root / 'configs' / 'pilot.json').read_text())
        self.assertEqual(record['limits'], {'timeout_seconds': pilot['compilation_timeout_seconds'],
                                            'memory': pilot['judge_memory'], 'cpus': pilot['judge_cpus']})
        self.assertEqual(record['config'], {'path': 'configs/pilot.json', 'limits': record['limits']})
        self.assertIs(record['limits_match_config'], True)
        self.assertEqual(record['image_ids'], {})

    def run_with_fake_docker(self, argv, config=None, prepare=None):
        """Runs the real-Lean path with Docker replaced by fakes.

        Returns (exit code, record, compilers built, image tags inspected).
        """
        import contextlib
        import io
        root = Path(__file__).resolve().parents[1]
        try:
            from research_swarm.dataset import FrozenDataset
            FrozenDataset(root)
        except (OSError, ValueError) as exc:
            self.skipTest(f'Frozen dataset unavailable: {exc}')
        module = load_validation_script()
        if prepare is not None:
            prepare(module)
        built, images = [], []

        class FakeDocker:
            def __init__(self, image, timeout=120, memory='2g', cpus=1, max_concurrent=None):
                self.image, self.timeout, self.memory, self.cpus = image, timeout, memory, cpus
                self.max_concurrent = max_concurrent
                self.fake = module.FakeCompiler(delay=0.05)
                built.append(self)

            def compile(self, source):
                return self.fake.compile(source)

        original_leftovers = module.check_leftover_containers

        def image_id(tag):
            images.append(tag)
            return 'sha256:' + 'a' * 64
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(module, 'DockerLeanCompiler', FakeDocker), \
                mock.patch.object(module, 'image_id', image_id), \
                mock.patch.object(module, 'check_leftover_containers',
                                  lambda checks: original_leftovers(checks, lambda: [])):
            extra = []
            if config is not None:
                path = Path(folder) / 'config.json'
                path.write_text(json.dumps(config))
                extra = ['--config', str(path)]
            code, path = module.run(module.parse_args(
                ['--run-root', folder, '--output-dir', folder, '--threads', '2', *extra, *argv]))
            self.assertTrue(Path(path).is_relative_to(folder), 'tests never write into data/validation')
            record = json.loads(Path(path).read_text())
        return code, record, built, images

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_real_lean_record_at_config_limits_passes_the_judge_limit_gate(self):
        from research_swarm.launch_gates import judge_limit_problems
        root = Path(__file__).resolve().parents[1]
        pilot = json.loads((root / 'configs' / 'pilot.json').read_text())
        code, record, built, images = self.run_with_fake_docker([])
        self.assertEqual(code, 0, [c for c in record['checks'] if not c['passed']])
        self.assertIs(record['all_required_passed'], True)
        self.assertIs(record['complete'], True)
        self.assertEqual(record['incomplete_reasons'], [])
        self.assertIs(record['limits_match_config'], True)
        self.assertEqual(judge_limit_problems(record, pilot), [])
        compiler = built[0]
        self.assertEqual((compiler.image, compiler.timeout, compiler.memory, compiler.cpus),
                         (pilot['lean_image'], pilot['compilation_timeout_seconds'], pilot['judge_memory'],
                          pilot['judge_cpus']))
        self.assertEqual(images, [pilot['lean_image']])
        self.assertEqual(record['image_ids'], {pilot['lean_image']: 'sha256:' + 'a' * 64, 'lean': 'sha256:' + 'a' * 64})
        self.assertEqual(record['settings']['image'], pilot['lean_image'])

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_limits_and_image_come_from_the_given_config(self):
        config = {'compilation_timeout_seconds': 90, 'judge_memory': '3G', 'judge_cpus': 1.5,
                  'lean_image': 'example-lean:test'}
        code, record, built, images = self.run_with_fake_docker([], config=config)
        self.assertEqual(code, 0)
        self.assertEqual((built[0].timeout, built[0].memory, built[0].cpus, built[0].image),
                         (90, '3G', 1.5, 'example-lean:test'))
        self.assertEqual(record['limits'], {'timeout_seconds': 90, 'memory': '3G', 'cpus': 1.5})
        self.assertEqual(record['config']['limits'], record['limits'])
        self.assertTrue(record['config']['path'].endswith('config.json'))
        self.assertEqual(images, ['example-lean:test'])

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_limit_override_is_never_launch_evidence(self):
        from research_swarm.launch_gates import judge_limit_problems
        root = Path(__file__).resolve().parents[1]
        pilot = json.loads((root / 'configs' / 'pilot.json').read_text())
        for argv in (['--timeout', '60'], ['--memory', '4g'], ['--cpus', '2']):
            code, record, _, _ = self.run_with_fake_docker(argv)
            self.assertEqual(code, 1, argv)
            self.assertIs(record['all_required_passed'], False, argv)
            self.assertIs(record['complete'], True, argv)
            self.assertIs(record['limits_match_config'], False, argv)
            self.assertTrue(all(c['passed'] for c in record['checks']), argv)
            self.assertNotEqual(judge_limit_problems(record, pilot), [], argv)
        # Same limits spelled differently are the config's limits.
        code, record, _, _ = self.run_with_fake_docker(['--memory', '2G', '--cpus', '1.0', '--timeout', '120'])
        self.assertEqual((code, record['limits_match_config']), (0, True))

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_unsubmitted_statement_fixture_makes_the_run_incomplete(self):
        def retarget(module):  # the fixture no longer matches its task's frozen template
            module.STATEMENT_FIXTURES['eulerbrick_notation_override.lean'] = 'Green32_hasCosetHole_empty'
        code, record, _, _ = self.run_with_fake_docker([], prepare=retarget)
        self.assertEqual(code, 1)
        self.assertIs(record['complete'], False)
        self.assertEqual(record['incomplete_reasons'],
                         ['statement-changing fixture(s) not submitted: eulerbrick_notation_override.lean'])
        self.assertIn('statement_fixture_applicable:eulerbrick_notation_override.lean',
                      [c['name'] for c in record['checks'] if not c['passed']])

    def test_unreadable_or_invalid_config_is_refused(self):
        import contextlib
        import io
        module = load_validation_script()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(io.StringIO()):
            bad = Path(folder) / 'bad.json'
            for content in ('not json', '[1, 2]', json.dumps({'compilation_timeout_seconds': 0}),
                            json.dumps({'judge_cpus': True})):
                bad.write_text(content)
                with self.assertRaises(SystemExit, msg=content):
                    module.parse_args(['--config', str(bad)])
            with self.assertRaises(SystemExit):
                module.parse_args(['--config', str(Path(folder) / 'missing.json')])

    def test_leftover_container_check_fails_when_docker_ps_fails(self):
        import contextlib
        import io
        module = load_validation_script()
        with contextlib.redirect_stdout(io.StringIO()):
            for listing, passed in ((None, False), ([], True), (['gdm-lean-0123'], False)):
                checks = module.Checks()
                module.check_leftover_containers(checks, lambda: listing)
                self.assertEqual([(c['name'], c['passed'], c['required']) for c in checks.items],
                                 [('no_leftover_lean_containers', passed, True)], listing)
            # The default lister is lean.judge_containers: a failing `docker ps` yields None.
            failed = subprocess.CompletedProcess(['docker'], 1, stdout='', stderr='Cannot connect to the Docker daemon')
            with mock.patch.object(lean.subprocess, 'run', return_value=failed) as run:
                checks = module.Checks()
                self.assertIsNone(module.check_leftover_containers(checks))
            self.assertIn('label=gdm-judge=1', run.call_args.args[0])
            self.assertFalse(checks.items[0]['passed'])
            self.assertIn('docker ps failed', checks.items[0]['detail'])


if __name__ == '__main__':
    unittest.main()
