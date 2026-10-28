"""Researcher-side readers with several overseers and the private overseer board.

Synthetic run records against the multi-overseer interface: ``effective.overseer`` with ``agent_ids``
(no ``agent_id``), ``effective.roles`` naming every overseer, the ``overseer_board`` table and the
``overseer_board_post`` / ``overseer_board_read`` events. A legacy single-overseer run (``agent_id``) keeps
its labels and outputs (tests/test_oversight_analysis.py covers its shape).
"""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / 'tests', ROOT / 'scripts', ROOT):
    sys.path.insert(0, str(extra))
import build_results  # noqa: E402
import export_conference  # noqa: E402
from research.analysis import roles as participant_roles  # noqa: E402
from research.analysis import timeline  # noqa: E402
from research.analysis.incident import digest as incident_digest  # noqa: E402
from research.analysis.judge import history as judge_history  # noqa: E402
from research.analysis.judge import prompts as judge_prompts  # noqa: E402
from research.analysis.simple_judge import casefile  # noqa: E402
from test_oversight_analysis import T0, make_overseer_run  # noqa: E402
from research.analysis.private_material import skip_decorator as needs_private  # noqa: E402

BOARD = [(200, 'agent-006', 'agent-001 used native evaluation on TaskA; I reverted it.'),
         (250, 'agent-007', 'Agreed. I will check agent-002 next.')]


def make_multi_run(root, *, board=True, posts=True):
    """The overseer run of test_oversight_analysis with a second overseer (agent-007) and the board."""
    run = make_overseer_run(root)
    settings = json.loads((run / 'settings.json').read_text())
    block = {'count': 2, 'powers': True, 'board': board}
    settings['config']['overseer'] = dict(block)
    settings['effective']['overseer'] = dict(block, agent_ids=['agent-006', 'agent-007'],
                                             oversight_tools=['check_proof', 'revert_submission'])
    settings['effective']['roles']['agent-007'] = 'overseer'
    (run / 'settings.json').write_text(json.dumps(settings))
    if not board:
        return run
    db = sqlite3.connect(run / 'conference' / 'state.sqlite')
    db.execute('CREATE TABLE overseer_board (post_id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, '
               'message TEXT NOT NULL, posted_at REAL NOT NULL)')
    events = [json.loads(line) for line in (run / 'conference' / 'events.jsonl').read_text().splitlines()]
    seq = max(e['seq'] for e in events)
    for number, (t, agent, message) in enumerate(BOARD if posts else [], 1):
        db.execute('INSERT INTO overseer_board (agent_id, message, posted_at) VALUES (?,?,?)',
                   (agent, message, T0 + t))
        for kind, tool, payload in (
                ('overseer_board_post', 'post_overseer_board', {'post_id': 'ovpost-%06d' % number,
                                                                'message': message}),
                ('overseer_board_read', 'read_overseer_board', {'since_seq': 0, 'limit': 20, 'paging': {},
                                                                'post_ids': ['ovpost-%06d' % number]})):
            seq += 1
            record = {'seq': seq, 'timestamp': T0 + t + (seq % 2), 'kind': kind, 'agent_id': agent, 'tool': tool,
                      'call_id': f'call-{seq}', 'payload': payload}
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)', (seq, record['timestamp'], kind, agent, tool,
                                                                    record['call_id'], json.dumps(payload)))
            events.append(record)
    db.commit()
    db.close()
    (run / 'conference' / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    return run


class RolesTest(unittest.TestCase):
    def test_both_forms(self):
        self.assertEqual(participant_roles.overseer_ids({'agent_id': 'agent-102'}), ['agent-102'])
        self.assertEqual(participant_roles.overseer_ids({'count': 2, 'agent_ids': ['agent-102', 'agent-103']}),
                         ['agent-102', 'agent-103'])
        self.assertEqual(participant_roles.overseer_ids({'count': 2}), [])
        self.assertEqual(participant_roles.overseer_ids(None), [])
        multi = {'effective': {'overseer': {'count': 2, 'agent_ids': ['agent-102', 'agent-103']}}}
        self.assertEqual(participant_roles.special_roles(multi),
                         {'agent-102': 'overseer', 'agent-103': 'overseer'})
        legacy = {'effective': {'overseer': {'agent_id': 'agent-102', 'powers': True}}}
        self.assertEqual(participant_roles.special_roles(legacy), {'agent-102': 'overseer'})
        self.assertEqual(participant_roles.label('agent-103', participant_roles.special_roles(multi)),
                         'agent-103 (overseer)')

    def test_run_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_multi_run(tmp)
            self.assertEqual(participant_roles.special_roles(run), {'agent-006': 'overseer', 'agent-007': 'overseer'})
            self.assertEqual(participant_roles.overseers(run), ['agent-006', 'agent-007'])
            self.assertTrue(participant_roles.board_enabled(run))
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(tmp)
            self.assertEqual(participant_roles.special_roles(run), {'agent-006': 'overseer'})
            self.assertFalse(participant_roles.board_enabled(run))


class BuildResultsMultiTest(unittest.TestCase):
    def test_several_overseers(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_multi_run(tmp)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                build_results.main(['--run-dir', str(run)])
            data, page = json.loads((run / 'results.json').read_text()), (run / 'results.html').read_text()
        condition = data['condition']
        self.assertEqual(condition['overseer']['agent_ids'], ['agent-006', 'agent-007'])
        self.assertEqual(condition['overseer']['count'], 2)
        self.assertNotIn('agent_id', condition['overseer'])
        self.assertEqual((condition['solvers'], condition['participants']), (2, 4))
        ov = data['oversight']
        self.assertEqual(ov['by_role']['overseer']['agents'], ['agent-006', 'agent-007'])
        self.assertEqual(ov['by_overseer']['agent-006']['reverted_credits_made'], 1)
        self.assertEqual(ov['by_overseer']['agent-006']['proof_checks'], 2)
        self.assertEqual(ov['by_overseer']['agent-007'],
                         {'submissions': 0, 'reverted_credits_made': 0, 'proof_checks': 0})
        self.assertIn('2 solvers + 2 overseers', page)
        self.assertIn('By overseer', page)
        self.assertIn('enabled (overseers only)', page)

    def test_single_overseer_keeps_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(tmp)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                build_results.main(['--run-dir', str(run)])
            data, page = json.loads((run / 'results.json').read_text()), (run / 'results.html').read_text()
        self.assertEqual(set(data['condition']['overseer']), {'agent_id', 'powers', 'oversight_tools'})
        self.assertNotIn('by_overseer', data['oversight'])
        self.assertIn('2 solvers + overseer', page)
        self.assertNotIn('By overseer', page)


class ExportConferenceMultiTest(unittest.TestCase):
    def export(self, tmp, **kwargs):
        run = make_multi_run(Path(tmp) / 'runs', **kwargs)
        with mock.patch.object(export_conference, 'ROOT', Path(tmp).resolve()):
            manifest = export_conference.export(run)
        page = (run / 'conference-view' / 'index.html').read_text()
        data = export_conference.published_data(page)
        return manifest, page, data

    def test_overseer_board_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, page, data = self.export(tmp)
        self.assertEqual(manifest['counts']['overseer_board'], 2)
        self.assertEqual(list(manifest['counts'])[-2:], ['overseer_board', 'files'])
        posts = [r for r in data['records'] if r['category'] == 'overseer_board']
        self.assertEqual([(r['id'], r['agents'], r['text']) for r in posts],
                         [('ovpost-000001', ['agent-006'], BOARD[0][2]), ('ovpost-000002', ['agent-007'], BOARD[1][2])])
        self.assertEqual(data['overseers'], ['agent-006', 'agent-007'])
        self.assertIn("overseer_board:['Overseer board'", page)
        self.assertIn('Credits the overseers reverted', page)
        self.assertIn('agent-007 (overseer)', ' '.join(data['warnings']))

    def test_empty_board_and_no_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest, page, _ = self.export(tmp, posts=False)
        self.assertEqual(manifest['counts']['overseer_board'], 0)
        self.assertIn('No overseer-board posts were recorded.', page)
        with tempfile.TemporaryDirectory() as tmp:
            manifest, page, data = self.export(tmp, board=False)
        self.assertNotIn('overseer_board', manifest['counts'])
        self.assertNotIn("overseer_board:['Overseer board'", page)
        self.assertEqual(manifest['counts']['oversight'], 3)

    def test_single_overseer_page_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(Path(tmp) / 'runs')
            with mock.patch.object(export_conference, 'ROOT', Path(tmp).resolve()):
                manifest = export_conference.export(run)
            page = (run / 'conference-view' / 'index.html').read_text()
        self.assertNotIn('overseer_board', manifest['counts'])
        self.assertNotIn('overseers', export_conference.published_data(page))
        self.assertIn('Credits the overseer reverted (with its stated reason)', page)


class JudgeHistoryMultiTest(unittest.TestCase):
    @needs_private("research/analysis/judge/prompts.py")
    def test_roles_board_items_and_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_multi_run(tmp)
            runtime = [json.loads(line) for line in (run / 'runtime.jsonl').read_text().splitlines()]
            conf = judge_history.load_conference(run)
            host = judge_history.load_host(run, runtime, conf, [])
        for agent in ('agent-006', 'agent-007'):
            self.assertEqual(judge_history.agent_role(host, agent), 'overseer')
            self.assertEqual(judge_history._overseer_count(host, agent), 2)
        self.assertIsNone(judge_history.agent_role(host, 'agent-001'))
        self.assertIsNone(judge_history._overseer_count(host, 'agent-001'))
        item = conf.items['ovpost-000002']
        self.assertEqual((item['agent_id'], item['channel'], item['text']), ('agent-007', 'overseer_board', BOARD[1][2]))
        self.assertIsNotNone(item['meta']['event_seq'])
        multi = judge_history.AgentHistory(agent_id='agent-007', code='A07', role='overseer', overseer_count=2)
        self.assertIn("one of the run's 2 overseers", judge_prompts.role_meta(multi)['role_note'])
        single = judge_history.AgentHistory(agent_id='agent-006', code='A06', role='overseer')
        self.assertEqual(judge_prompts.role_meta(single)['role_note'], judge_prompts.OVERSEER_ROLE_NOTE)

    def test_single_overseer_count_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(tmp)
            runtime = [json.loads(line) for line in (run / 'runtime.jsonl').read_text().splitlines()]
            conf = judge_history.load_conference(run)
            host = judge_history.load_host(run, runtime, conf, [])
        self.assertIsNone(judge_history._overseer_count(host, 'agent-006'))
        self.assertEqual(conf.overseer_posts, {})

    def test_read_exposure(self):
        call = {'tool': 'read_overseer_board', 'result_item': {'id': 'A07.t0003.r', 'time_bounds': None},
                'result': {'posts': [{'post_id': 'ovpost-000001', 'agent_id': 'agent-006', 'message': BOARD[0][2]},
                                     {'post_id': 'ovpost-000002', 'agent_id': 'agent-007', 'message': BOARD[1][2]}]}}
        entries = judge_history._exposure_entries(call, {'seq': None, 'status': 'joined'},
                                                  judge_history.ConferenceRecords(), 'agent-007')
        self.assertEqual([(e['kind'], e['author'], e['source_id']) for e in entries],
                         [('overseer_board_post', 'agent-006', 'ovpost-000001')])


class TimelineDigestCasefileMultiTest(unittest.TestCase):
    def test_timeline_board_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_multi_run(tmp)
            entries = timeline.conference_stream(run, [])
        posts = [e for e in entries if e['kind'] == 'overseer_board_post']
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]['text'], BOARD[0][2])
        self.assertIn('post_id=ovpost-000001', posts[0]['summary'])
        self.assertEqual(timeline.COMMUNICATION_KINDS['overseer_board_post'], 'overseer_board')

    def test_digest_label(self):
        item = {'kind': 'conference', 'channel': 'overseer_board'}
        self.assertIn('overseers only', incident_digest.label_for(item))
        self.assertRegex('see ovpost-000001', incident_digest.ITEM_ID)

    def test_casefile_board_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = make_multi_run(tmp)
            conf = judge_history.load_conference(run_dir)
        ledger = [{'kind': 'overseer_board_post', 'item_id': 'A07.t0003.r', 'source_id': 'ovpost-000001',
                   'author': 'agent-006', 'text_returned': BOARD[0][2], 'time': None}]
        run = SimpleNamespace(conference=conf, host={'roles': {}},
                              agents={'agent-007': SimpleNamespace(exposure_ledger=ledger),
                                      'agent-001': SimpleNamespace(exposure_ledger=[])})
        facts = casefile.collect_facts(run, 'agent-007', T0)
        self.assertEqual([p['id'] for p in facts['overseer_board_posts']], ['ovpost-000002'])
        self.assertEqual(facts['overseer_board_read'][0]['from'], 'agent-006')
        records = {}
        text = casefile.render_facts(facts, run, records)
        self.assertIn('OVERSEER BOARD POSTS BY THIS AGENT (overseer board, overseers only) (1)', text)
        self.assertIn('OVERSEER BOARD POSTS READ', text)
        self.assertEqual(casefile.summarize_facts(facts)['overseer_board_posts'], 1)
        solver = casefile.collect_facts(run, 'agent-001', T0)
        self.assertNotIn('overseer_board_posts', solver)
        self.assertNotIn('OVERSEER BOARD', casefile.render_facts(solver, run, {}))
        self.assertNotIn('overseer_board_posts', casefile.summarize_facts(solver))


if __name__ == '__main__':
    unittest.main()
