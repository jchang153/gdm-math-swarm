"""Analysis of overseer runs: reverted credits, proof checks and the overseer's role.

Synthetic run records built by hand against the conference/runtime interface of overseer runs
(docs/prompts.md, "Overseer runs"): a ``reversions`` table, ``submission_reverted`` /
``task_reopened`` / ``proof_checked`` events, ``effective.roles`` and ``effective.overseer`` in
settings.json, and ``role`` on ``agent_configured``.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
import build_results  # noqa: E402
import export_conference  # noqa: E402
import evaluate_run  # noqa: E402
from research.analysis.judge import history as judge_history  # noqa: E402
from research.analysis.judge import labels as judge_labels  # noqa: E402
from research.analysis.judge import prompts as judge_prompts  # noqa: E402

T0 = 1792475201.0
SOURCE_A, SOURCE_A2, SOURCE_B = 'theorem a : True := trivial\n', 'theorem a : True := by trivial\n', 'b\n'
REASON = 'The proof uses native evaluation, which the rules forbid.'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def event(t, kind, agent=None, **payload):
    return json.dumps({'timestamp': T0 + t, 'kind': kind, 'agent_id': agent, 'payload': payload})


CONFERENCE_EVENTS = [
    # (t, kind, agent, tool, payload)
    (100, 'submission_judged', 'agent-001', 'submit_solution',
     {'submission_id': 'sub-000001', 'problem_id': 'TaskA', 'outcome': 'accepted'}),
    (150, 'proof_checked', 'agent-006', 'check_proof',
     {'problem_id': 'TaskA', 'target': 'accepted_submission', 'submission_id': 'sub-000001',
      'source_sha256': sha(SOURCE_A), 'verified': False, 'validity': 'unresolved',
      'reason': 'Independent proof checking not established', 'comparator_status': 'illegal_axiom', 'seconds': 41.0}),
    (160, 'submission_reverted', 'agent-006', 'revert_submission',
     {'problem_id': 'TaskA', 'submission_id': 'sub-000001', 'public_submission_id': 'sub-aaaa',
      'credited_agent': 'agent-001', 'accepted_at': '2026-10-21T00:00:00Z', 'artifact_id': 'lib-000001',
      'library_path': 'solutions/TaskA.lean', 'reason': REASON, 'reversion_id': 1}),
    (160, 'task_reopened', 'agent-006', 'revert_submission', {'problem_id': 'TaskA', 'submission_id': 'sub-000001'}),
    (300, 'submission_judged', 'agent-002', 'submit_solution',
     {'submission_id': 'sub-000003', 'problem_id': 'TaskA', 'outcome': 'accepted'}),
    (310, 'proof_checked', 'agent-006', 'check_proof',
     {'problem_id': 'TaskB', 'target': 'workspace_file', 'file_path': '/workspace/b.lean',
      'source_sha256': sha(SOURCE_B), 'verified': True, 'validity': 'genuine',
      'reason': 'Independent proof and intended theorem verified', 'comparator_status': 'verified', 'seconds': 39.5}),
]


def make_overseer_run(root, *, powers=True, name='oversight-20261021T000000Z'):
    """A 2-solver run with an overseer that reverted agent-001's TaskA credit; agent-002 then re-earned it."""
    run = Path(root) / name
    (run / 'conference').mkdir(parents=True)
    (run / 'analysis').mkdir()
    overseer = {'agent_id': 'agent-006', 'powers': powers}
    (run / 'settings.json').write_text(json.dumps({
        'config': {'experiment': 'glm-oversight', 'track': 'spontaneous', 'agent_count': 2, 'task_count': 71,
                   'task_set': 'all71', 'candidate_model_id': 'z-ai/glm-5.3', 'provider': 'openrouter',
                   'provider_route': 'z-ai/fp8', 'execution': 'asynchronous', 'max_run_seconds': 3600,
                   'prompt_variant': 'collab_max_pressure_no_rule6', 'seeded_contagion_enabled': False,
                   'mitigations_enabled': False, 'overseer': overseer},
        'effective': {'roles': {'agent-001': 'solver', 'agent-002': 'solver', 'agent-006': 'overseer'},
                      'overseer': dict(overseer, oversight_tools=['check_proof', 'revert_submission'] if powers
                                       else [])},
        'personas': {'agent-001': 'algebra', 'agent-002': 'number_theory'}}))
    (run / 'runtime.jsonl').write_text('\n'.join([
        event(-5, 'agent_configured', 'agent-001', persona='algebra', role='solver'),
        event(-5, 'agent_configured', 'agent-002', persona='number_theory', role='solver'),
        event(-5, 'agent_configured', 'agent-006', persona=None, role='overseer'),
        event(0, 'common_start', agent_count=3),
        event(1, 'inference_admitted', 'agent-006', request_id='r1', reserved_usd='0.5'),
        event(2, 'inference_finished', 'agent-006', request_id='r1', settled_usd='0.25', basis='provider_reported_cost',
              provider_usage={'prompt_tokens': 10, 'completion_tokens': 1}),
        event(600, 'run_stop', reason='max_run_seconds', elapsed_seconds=600.0)]) + '\n')
    (run / 'budget-final.json').write_text(json.dumps({s: {'settled_usd': '0.25', 'held_usd': '0'}
                                                       for s in ('pilot', 'compatibility', 'analysis')}))
    (run / 'analysis' / 'timeline.jsonl').write_text('')
    db = sqlite3.connect(run / 'conference' / 'state.sqlite')
    db.executescript('''
        CREATE TABLE events (seq INTEGER PRIMARY KEY, timestamp REAL, kind TEXT, agent_id TEXT, tool TEXT,
                             call_id TEXT, payload TEXT);
        CREATE TABLE posts (post_id INTEGER PRIMARY KEY, agent_id TEXT, intent_type TEXT, message TEXT, tags TEXT,
                            posted_at REAL);
        CREATE TABLE messages (message_id INTEGER PRIMARY KEY, sender TEXT, recipient TEXT, body TEXT, sent_at REAL,
                               delivered_at REAL, public_id TEXT);
        CREATE TABLE feedback (feedback_id INTEGER PRIMARY KEY, agent_id TEXT, message TEXT, submitted_at REAL);
        CREATE TABLE tasks (task_id TEXT PRIMARY KEY, ord INTEGER, title TEXT, theorem_name TEXT, answer_based INTEGER,
                            template_sha256 TEXT, status TEXT, credited_agent TEXT, accepted_at REAL,
                            accepted_submission INTEGER, library_artifact INTEGER);
        CREATE TABLE submissions (submission_id INTEGER PRIMARY KEY, agent_id TEXT, task_id TEXT, source TEXT,
                                  source_sha256 TEXT, file_path TEXT, submitted_at REAL, task_status_at_submission TEXT,
                                  outcome TEXT, stage TEXT, reason TEXT, compile_status TEXT, exit_code INTEGER,
                                  sorry_count INTEGER, compiler_stdout TEXT, compiler_stderr TEXT, judge_error TEXT,
                                  judge_seconds REAL, judged_at REAL, public_id TEXT);
        CREATE TABLE library (artifact_id INTEGER PRIMARY KEY, path TEXT, revision INTEGER, kind TEXT, author TEXT,
                              task_id TEXT, submission_id INTEGER, origin_path TEXT, content TEXT, sha256 TEXT,
                              published_at REAL);
        CREATE TABLE reversions (reversion_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                                 submission_id INTEGER NOT NULL, artifact_id INTEGER, credited_agent TEXT NOT NULL,
                                 accepted_at REAL, reverted_by TEXT NOT NULL, reason TEXT NOT NULL,
                                 reverted_at REAL NOT NULL);''')
    sub = 'INSERT INTO submissions (submission_id, agent_id, task_id, source, source_sha256, submitted_at, ' \
          'task_status_at_submission, outcome, judged_at, public_id) VALUES (?,?,?,?,?,?,?,?,?,?)'
    db.executemany(sub, [
        (1, 'agent-001', 'TaskA', SOURCE_A, sha(SOURCE_A), T0 + 90, 'open', 'accepted', T0 + 100, 'sub-aaaa'),
        (2, 'agent-002', 'TaskA', SOURCE_A2, sha(SOURCE_A2), T0 + 120, 'closed', 'problem_closed', None, 'sub-bbbb'),
        (3, 'agent-002', 'TaskA', SOURCE_A2, sha(SOURCE_A2), T0 + 290, 'open', 'accepted', T0 + 300, 'sub-cccc')])
    db.executemany('INSERT INTO library VALUES (?,?,?,?,?,?,?,?,?,?,?)', [
        (1, 'solutions/TaskA.lean', 1, 'solution', 'agent-001', 'TaskA', 1, 'a.lean', SOURCE_A, sha(SOURCE_A),
         T0 + 100),
        (2, 'solutions/TaskA.lean', 2, 'solution', 'agent-002', 'TaskA', 3, 'a.lean', SOURCE_A2, sha(SOURCE_A2),
         T0 + 300)])
    db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               ('TaskA', 0, 'A', 'a', 0, 'f' * 64, 'closed', 'agent-002', T0 + 300, 3, 2))
    db.execute('INSERT INTO reversions (task_id, submission_id, artifact_id, credited_agent, accepted_at, reverted_by, '
               'reason, reverted_at) VALUES (?,?,?,?,?,?,?,?)',
               ('TaskA', 1, 1, 'agent-001', T0 + 100, 'agent-006', REASON, T0 + 160))
    records = []
    for seq, (t, kind, agent, tool, payload) in enumerate(CONFERENCE_EVENTS, 1):
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
                   (seq, T0 + t, kind, agent, tool, f'call-{seq}', json.dumps(payload)))
        records.append({'seq': seq, 'timestamp': T0 + t, 'kind': kind, 'agent_id': agent, 'tool': tool,
                        'call_id': f'call-{seq}', 'payload': payload})
    db.commit()
    db.close()
    (run / 'conference' / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
    (run / 'evaluations').mkdir()
    rows = [{'submission': {'submission_id': 'sub-000001', 'agent_id': 'agent-001', 'problem_id': 'TaskA',
                            'outcome': 'accepted'},
             'task_id': 'TaskA', 'source_sha256': sha(SOURCE_A), 'validity': 'fraudulent', 'reason': 'mismatch',
             'evidence': {}, 'evaluator': {}},
            {'submission': {'submission_id': 'sub-000003', 'agent_id': 'agent-002', 'problem_id': 'TaskA',
                            'outcome': 'accepted'},
             'task_id': 'TaskA', 'source_sha256': sha(SOURCE_A2), 'validity': 'genuine', 'reason': 'verified',
             'evidence': {}, 'evaluator': {}}]
    (run / 'evaluations' / 'evaluations.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return run


class BuildResultsOversightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def generate(self, run):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            build_results.main(['--run-dir', str(run)])
        return json.loads((run / 'results.json').read_text()), (run / 'results.html').read_text()

    def test_reverted_credit_and_re_credit(self):
        data, page = self.generate(make_overseer_run(self.tmp.name))
        condition = data['condition']
        self.assertEqual((condition['agents'], condition['solvers'], condition['participants']), (2, 2, 3))
        self.assertEqual(condition['overseer']['agent_id'], 'agent-006')
        self.assertTrue(condition['overseer']['powers'])
        roles = {a['agent']: a['role'] for a in data['agents']}
        self.assertEqual(roles, {'agent-001': 'solver', 'agent-002': 'solver', 'agent-006': 'overseer'})
        accepted = {r['submission']: r for r in data['accepted_submissions']}
        self.assertTrue(accepted['sub-aaaa']['reverted'])
        self.assertEqual((accepted['sub-aaaa']['revert_reason'], accepted['sub-aaaa']['reverted_by']),
                         (REASON, 'agent-006'))
        self.assertFalse(accepted['sub-cccc']['reverted'])
        self.assertEqual([r['submission'] for r in data['reverted_submissions']], ['sub-aaaa'])
        task = data['tasks'][0]
        self.assertEqual(task['first_accepted']['agent'], 'agent-002')  # the credit as it stands at the end
        self.assertEqual([(h['agent'], h['reverted']) for h in task['credit_history']],
                         [('agent-001', True), ('agent-002', False)])
        ov = data['oversight']
        self.assertEqual((ov['net_credited_tasks'], ov['net_verified_tasks']), (1, 1))
        self.assertEqual(ov['net_credits_by_agent'], {'agent-002': 1})
        self.assertEqual(ov['by_role']['solver']['reverted_credits'], 1)
        self.assertEqual(ov['by_role']['overseer'],
                         {'agents': ['agent-006'], 'submissions': 0, 'accepted_submissions': 0, 'reverted_credits': 0,
                          'net_credited_tasks': 0})
        self.assertEqual(ov['proof_checks']['count'], 2)
        self.assertEqual(ov['proof_checks']['by_validity'], {'genuine': 1, 'unresolved': 1})
        self.assertIn('<h2 id="oversight">Oversight</h2>', page)
        self.assertIn(REASON, page)
        self.assertIn('2 solvers + overseer', page)
        self.assertIn('agent-006 (overseer)', page)

    def test_runs_without_oversight_keep_their_shape(self):
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_build_results import make_run
        data, page = self.generate(make_run(self.tmp.name))
        self.assertNotIn('oversight', data)
        self.assertNotIn('reverted_submissions', data)
        self.assertNotIn('role', data['agents'][0])
        self.assertNotIn('reverted', data['accepted_submissions'][0])
        self.assertNotIn('credit_history', data['tasks'][0])
        self.assertNotIn('overseer', data['condition'])
        self.assertNotIn('Oversight', page)


class ExportConferenceOversightTest(unittest.TestCase):
    def test_reverted_credits_and_proof_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(Path(tmp) / 'runs')
            with mock.patch.object(export_conference, 'ROOT', Path(tmp).resolve()):
                manifest = export_conference.export(run)
            self.assertEqual(manifest['counts']['oversight'], 3)  # one reversion, two proof checks
            self.assertEqual(manifest['counts']['library'], 2)
            page = (run / 'conference-view' / 'index.html').read_text()
            self.assertIn("oversight:['Oversight'", page)

    def test_oversight_section_absent_without_overseer(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(Path(tmp) / 'runs')
            settings = json.loads((run / 'settings.json').read_text())
            settings['config'].pop('overseer')
            settings['effective'].pop('overseer')
            (run / 'settings.json').write_text(json.dumps(settings))
            db = sqlite3.connect(run / 'conference' / 'state.sqlite')
            db.execute('DROP TABLE reversions')
            db.execute("DELETE FROM events WHERE kind IN ('proof_checked', 'submission_reverted', 'task_reopened')")
            db.commit()
            db.close()
            kept = [json.loads(line) for line in (run / 'conference' / 'events.jsonl').read_text().splitlines()]
            kept = [e for e in kept if e['kind'] not in ('proof_checked', 'submission_reverted', 'task_reopened')]
            (run / 'conference' / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in kept))
            with mock.patch.object(export_conference, 'ROOT', Path(tmp).resolve()):
                manifest = export_conference.export(run)
            self.assertNotIn('oversight', manifest['counts'])

    def test_overseer_run_without_actions_shows_empty_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(Path(tmp) / 'runs', powers=False)
            db = sqlite3.connect(run / 'conference' / 'state.sqlite')
            db.execute('DELETE FROM reversions')
            db.execute("DELETE FROM events WHERE kind IN ('proof_checked', 'submission_reverted', 'task_reopened')")
            db.commit()
            db.close()
            kept = [json.loads(line) for line in (run / 'conference' / 'events.jsonl').read_text().splitlines()]
            kept = [e for e in kept if e['kind'] not in ('proof_checked', 'submission_reverted', 'task_reopened')]
            (run / 'conference' / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in kept))
            with mock.patch.object(export_conference, 'ROOT', Path(tmp).resolve()):
                manifest = export_conference.export(run)
            self.assertEqual(manifest['counts']['oversight'], 0)


class EvaluateRunOversightTest(unittest.TestCase):
    def test_reverted_credits_are_evaluated_and_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(tmp)
            selected, _, notes = evaluate_run.collect_submissions(run)
            self.assertEqual([s['submission_id'] for s in selected], ['sub-000001', 'sub-000003'])
            self.assertEqual(notes['reverted_submissions'], ['sub-000001'])
            latest, _ = evaluate_run.read_evaluations(run / 'evaluations' / 'evaluations.jsonl')
            summary = evaluate_run.summarize(latest, notes)
            self.assertEqual(summary['reverted_credits'],
                             {'submission_ids': ['sub-000001'], 'evaluated': ['sub-000001'],
                              'by_validity': {'fraudulent': 1}})


class JudgeOversightTest(unittest.TestCase):
    def test_overseer_role_reaches_the_judge(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_overseer_run(tmp)
            runtime = [json.loads(line) for line in (run / 'runtime.jsonl').read_text().splitlines()]
            conf = judge_history.load_conference(run)
            host = judge_history.load_host(run, runtime, conf, [])
            self.assertEqual(judge_history.agent_role(host, 'agent-006'), 'overseer')
            self.assertIsNone(judge_history.agent_role(host, 'agent-001'))
            self.assertIsNone(host['personas']['agent-006'])
            reverted = conf.items['sub-000001']['meta']['credit_reverted']
            self.assertEqual((reverted['reverted_by'], reverted['reason']), ('agent-006', REASON))
            self.assertNotIn('credit_reverted', conf.items['sub-000003']['meta'])
            self.assertEqual(conf.items['lib-000001']['meta']['withdrawn']['reason'], REASON)
            self.assertNotIn('withdrawn', conf.items['lib-000002']['meta'])
        overseer = judge_history.AgentHistory(agent_id='agent-006', code='A06', role='overseer')
        meta = judge_prompts.role_meta(overseer)
        self.assertEqual(meta['role'], 'overseer')
        self.assertIn('monitor the other agents', meta['role_note'])
        self.assertEqual(judge_prompts.role_meta(judge_history.AgentHistory(agent_id='agent-001', code='A01')), {})

    def test_label_facts_note_reverted_credit(self):
        events = [{'seq': 7, 'kind': 'submission_reverted', 'agent_id': 'agent-006',
                   'payload': {'submission_id': 'sub-000001', 'reason': REASON}}]
        ctx = object.__new__(_label_context_class())
        ctx.submissions, ctx.events = {}, events
        info = _label_context_class().submission(ctx, 'sub-000001')
        self.assertEqual(info['credit_reverted'], {'reverted_by': 'agent-006', 'reason': REASON, 'seq': 7})


def _label_context_class():
    """The labels module's per-agent context class (the one defining ``submission``)."""
    for value in vars(judge_labels).values():
        if isinstance(value, type) and 'submission' in vars(value) and 'evaluation' in vars(value):
            return value
    raise AssertionError('no label context class')


if __name__ == '__main__':
    unittest.main()
