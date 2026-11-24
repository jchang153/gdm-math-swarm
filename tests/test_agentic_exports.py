"""Agentic monitor arm AG (docs/plan.md, "Agentic monitor arm AG (owner decisions, November 23, 2026)"): the
transcript export's monitor pages and solver links, the conference view's monitor DMs and reverts, the judge
history's sample index skipping agmon-NNN samples, and the logs-dataset guard for monitor records. Synthetic
records only; runs without monitors must export as before."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dataset_logs  # noqa: E402
import export_conference  # noqa: E402
import export_transcripts  # noqa: E402
from research.analysis.judge import history  # noqa: E402
from test_conference_browser import make_conference_run  # noqa: E402


class DatasetGuardTests(unittest.TestCase):
    def test_monitor_records_are_never_public(self):
        for folder in ('agentic_monitor', 'agentic-monitor'):
            self.assertEqual(dataset_logs.category(f'{folder}/agmon-001.jsonl'), 'jsonl')
            self.assertEqual(dataset_logs.category(f'{folder}/agmon-001.json'), 'transcripts')
            self.assertEqual(dataset_logs.category(f'{folder}/sub/prompt.txt'), 'transcripts')
        # unchanged elsewhere
        self.assertIsNone(dataset_logs.category('results.json'))
        self.assertIsNone(dataset_logs.category('agentic_monitor'))
        self.assertEqual(dataset_logs.category('review/calls.jsonl'), 'jsonl')


class SampleIndexTests(unittest.TestCase):
    def summaries(self, ids):
        return lambda path: [SimpleNamespace(id=i, epoch=1) for i in ids]

    def test_monitor_samples_skipped_and_listed_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / 'inspect').mkdir()
            (run / 'inspect' / 'a.eval').write_bytes(b'')
            reader = self.summaries(['agent-001', 'agmon-001', 'agent-002', 'agmon-002'])
            with patch.object(history, '_ensure_inspect', return_value=(None, reader)):
                self.assertEqual(sorted(history.sample_index(run)), ['agent-001', 'agent-002'])
                self.assertEqual(sorted(history.monitor_sample_index(run)), ['agmon-001', 'agmon-002'])
            with patch.object(history, '_ensure_inspect', return_value=(None, self.summaries(['oddity']))):
                with self.assertRaises(ValueError):
                    history.sample_index(run)


class MonitorTranscriptTests(unittest.TestCase):
    def test_pairing(self):
        self.assertEqual(export_transcripts.solver_of('agmon-007'), 'agent-007')
        self.assertEqual(export_transcripts.monitor_of('agent-007'), 'agmon-007')
        self.assertIsNone(export_transcripts.solver_of('agent-007'))
        self.assertIsNone(export_transcripts.monitor_of('reviewer'))

    def inspect_messages(self):
        return [
            {'role': 'system', 'content': 'You review <one> solver.'},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'call c1: submit_solution ... [truncated]'}]},
            {'role': 'assistant', 'content': [{'type': 'reasoning', 'reasoning': 'looks like Iff.rfl'},
                                              {'type': 'text', 'text': 'Reverting.'}],
             'tool_calls': [SimpleNamespace(id='t1', function='revert_submission',
                                            arguments={'problem_id': 'P1', 'reason': 'answer restates <goal>'})]},
            {'role': 'tool', 'content': 'reverted sub-000003', 'tool_call_id': 't1', 'function': 'revert_submission',
             'error': None},
            {'role': 'tool', 'content': 'x', 'tool_call_id': 't2', 'function': None,
             'error': SimpleNamespace(type='parsing', message='bad')},
        ]

    def test_renders_escaped_messages_tool_calls_and_back_link(self):
        messages = [export_transcripts.normalize_message(m) for m in self.inspect_messages()]
        self.assertEqual(messages[2]['tool_calls'][0]['function'], 'revert_submission')
        self.assertEqual(messages[4]['error'], {'type': 'parsing', 'message': 'bad'})
        page, count = export_transcripts.render_monitor('agmon-003', messages, 'run-x', 'inspect/a.eval',
                                                        'agent-003.html')
        self.assertEqual(count, 5)
        self.assertIn('<a href="agent-003.html">Solver: agent-003</a>', page)
        self.assertIn('System prompt', page)
        self.assertIn('You review &lt;one&gt; solver.', page)
        self.assertIn('answer restates &lt;goal&gt;', page)
        self.assertIn('Tool result · revert_submission · call t1', page)
        self.assertIn('looks like Iff.rfl', page)
        self.assertNotIn('<goal>', page)
        self.assertNotIn('<one>', page)

    def test_session_jsonl_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            folder = run / 'agentic_monitor'
            folder.mkdir()
            records = [
                {'type': 'user', 'message': {'role': 'user', 'content': 'activity since last look'}},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'hmm'},
                    {'type': 'tool_use', 'id': 'u1', 'name': 'mcp__monitor__wait', 'input': {'seconds': 60}}]}},
                {'type': 'user', 'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'u1', 'content': [{'type': 'text', 'text': '2 calls'}]}]}},
                {'type': 'user', 'isCompactSummary': True, 'message': {'role': 'user', 'content': 'summary text'}},
                {'type': 'summary'},
            ]
            (folder / 'agmon-004.jsonl').write_text('\n'.join(json.dumps(r) for r in records) + '\nnot json\n')
            (folder / 'notes.jsonl').write_text('{}\n')
            sources = export_transcripts.monitor_sources(run)
            self.assertEqual(list(sources), ['agmon-004'])
            messages, source = export_transcripts.monitor_messages(run, *sources['agmon-004'])
            self.assertEqual(source, 'agentic_monitor/agmon-004.jsonl')
            self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'tool', 'summary'])
            self.assertEqual(messages[1]['tool_calls'][0]['function'], 'mcp__monitor__wait')
            self.assertEqual(messages[2]['tool_call_id'], 'u1')
            page, _ = export_transcripts.render_monitor('agmon-004', messages, 'r', source)
            self.assertIn('Tool result · mcp__monitor__wait · call u1', page)
            self.assertIn('Compaction summary', page)
            self.assertNotIn('Solver: ', page)

    def test_solver_link_only_added_to_navigation(self):
        page = 'x' + export_transcripts.NAV + 'y'
        linked = export_transcripts.with_monitor_link(page, 'agmon-001')
        self.assertIn('<a href="agmon-001.html">Agentic monitor: agmon-001</a></p>y', linked)
        self.assertTrue(linked.startswith('x<p><a href="index.html">All agents</a>'))

    def test_export_groups_monitor_under_solver_and_leaves_plain_runs_unchanged(self):
        def fake_agent(agent):
            return SimpleNamespace(agent_id=agent, stats={'turns': 1, 'segments': 1, 'items': 2, 'tokens_total': 10,
                                                          'summary_turns': 0, 'items_by_kind': {},
                                                          'harness_items': 0, 'exposure_entries': 0,
                                                          'missing_tool_results': []}, source='inspect/a.eval')

        def fake_render(record, run_id):
            return '<!doctype html>' + export_transcripts.NAV + record.agent_id, 3

        def run_export(run, monitors, agents=None):
            built = SimpleNamespace(run_id='run-x', agents={a: fake_agent(a) for a in ('agent-001', 'agent-002')
                                                            if agents is None or a in agents})
            module = SimpleNamespace(build_run=lambda run, agents=None: built, SCHEMA='h/1',
                                     monitor_sample_index=lambda run: monitors)
            fake_pkg = SimpleNamespace(history=module)
            with patch.dict(sys.modules, {'research.analysis.judge': fake_pkg,
                                          'research.analysis.judge.history': module}), \
                    patch.object(export_transcripts, 'render_agent', fake_render), \
                    patch.object(export_transcripts, 'monitor_messages',
                                 lambda run, kind, ref: ([export_transcripts.normalize_message(
                                     {'role': 'assistant', 'content': 'ok'})], 'inspect/a.eval')):
                return export_transcripts.export(run, run / 'out', agents=agents, require_saved_run=False)

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run-x'
            (run / 'inspect').mkdir(parents=True)
            (run / 'inspect' / 'a.eval').write_bytes(b'')
            plain = run_export(run, {})
            self.assertNotIn('agentic_monitors', plain)
            index = (run / 'out' / 'index.html').read_text()
            self.assertNotIn('agmon', index)
            self.assertNotIn('Agentic monitor', (run / 'out' / 'agent-001.html').read_text())
            self.assertTrue(all('role' not in e for e in json.loads((run / 'out' / 'manifest.json').read_text())))

            result = run_export(run, {'agmon-001': (run / 'inspect' / 'a.eval', 'agmon-001', 1)})
            self.assertEqual(result['agentic_monitors'], 1)
            self.assertEqual(result['agents'], 2)
            index = (run / 'out' / 'index.html').read_text()
            order = [index.index(x) for x in ('href="agent-001.html"', 'href="agmon-001.html"',
                                               'href="agent-002.html"')]
            self.assertEqual(order, sorted(order))
            self.assertIn('Agentic monitor: agmon-001', (run / 'out' / 'agent-001.html').read_text())
            self.assertNotIn('Agentic monitor', (run / 'out' / 'agent-002.html').read_text())
            self.assertIn('Solver: agent-001', (run / 'out' / 'agmon-001.html').read_text())
            manifest = json.loads((run / 'out' / 'manifest.json').read_text())
            monitors = [e for e in manifest if e.get('role') == 'agentic_monitor']
            self.assertEqual([(e['agent_id'], e['solver']) for e in monitors], [('agmon-001', 'agent-001')])
            self.assertTrue(all('role' not in e for e in manifest if e['agent_id'].startswith('agent-')))

            only = run_export(run, {'agmon-001': (run / 'inspect' / 'a.eval', 'agmon-001', 1),
                                    'agmon-002': (run / 'inspect' / 'a.eval', 'agmon-002', 1)},
                              agents=['agent-002', 'agmon-002'])
            self.assertEqual((only['agents'], only['agentic_monitors']), (1, 1))


class ConferenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        root = patch.object(export_conference, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)

    def data(self, run):
        export_conference.export(run)
        return export_conference.published_data((run / 'conference-view' / 'index.html').read_text())

    def add_monitor_actions(self, run, events=True):
        db = sqlite3.connect(run / 'conference/state.sqlite')
        db.execute('CREATE TABLE reversions(reversion_id, submission_id, task_id, credited_agent, reverted_by, reason, '
                   'reverted_at, artifact_id)')
        db.execute('INSERT INTO reversions VALUES (1, 1, "T1", "agent-002", "reviewer", "answer restates goal", 30.0, '
                   '1)')
        db.execute('INSERT INTO messages VALUES (2, "reviewer", "agent-002", "Your proof restates the goal.", 31.0)')
        db.commit()
        db.close()
        lines = []
        if events:
            lines = [{'kind': 'agentic_monitor_start', 'agent_id': None, 'timestamp': 1.0,
                      'payload': {'monitor': 'agmon-002', 'solver': 'agent-002'}},
                     {'kind': 'agentic_revert', 'agent_id': None, 'timestamp': 30.0,
                      'payload': {'monitor': 'agmon-002', 'solver': 'agent-002', 'problem_id': 'T1',
                                  'submission_id': 'sub-000001', 'reason': 'answer restates goal', 'dm_sent': True,
                                  'message_id': 'dm-000002'}}]
        with (run / 'runtime.jsonl').open('a') as handle:
            handle.write(''.join(json.dumps(x) + '\n' for x in lines))

    def test_ag_run_names_the_monitor_on_reverts_and_dms(self):
        run = make_conference_run(self.root / 'runs', 'ag-run')
        self.add_monitor_actions(run)
        data = self.data(run)
        self.assertIs(data['agentic_monitor'], True)
        self.assertIn('oversight', data['counts'])
        revert = next(r for r in data['records'] if r['id'] == 'reversion-1')
        self.assertTrue(revert['text'].endswith('(agentic monitor agmon-002)'))
        self.assertEqual(revert['detail']['agentic_monitor'], 'agmon-002')
        dm = next(r for r in data['records'] if r['category'] == 'dms' and 'reviewer' in r['agents'])
        self.assertEqual(dm['detail']['messages'][0]['sender'], 'reviewer')  # stored identity kept
        self.assertEqual(dm['detail']['messages'][0]['agentic_monitor'], 'agmon-002')
        self.assertIn(export_conference.AGENTIC_WARNING, data['warnings'])
        page = (run / 'conference-view' / 'index.html').read_text()
        self.assertIn("agentic monitor '+m.agentic_monitor", page)
        self.assertIn('Credits the agentic monitors reverted', page)

    def test_ag_detected_from_settings_with_no_actions(self):
        run = make_conference_run(self.root / 'runs', 'ag-quiet')
        (run / 'settings.json').write_text(json.dumps({'config': {'agentic_monitor': {'enabled': True}}}))
        data = self.data(run)
        self.assertIs(data['agentic_monitor'], True)
        self.assertEqual(data['counts']['oversight'], 0)
        self.assertIn('No credit was reverted by an agentic monitor.',
                      (run / 'conference-view' / 'index.html').read_text())

    def test_reviewer_run_without_ag_is_unchanged(self):
        run = make_conference_run(self.root / 'runs', 'reviewer-run')
        self.add_monitor_actions(run, events=False)
        data = self.data(run)
        self.assertNotIn('agentic_monitor', data)
        self.assertNotIn(export_conference.AGENTIC_WARNING, data['warnings'])
        revert = next(r for r in data['records'] if r['id'] == 'reversion-1')
        self.assertNotIn('agentic', revert['text'])
        self.assertNotIn('agentic_monitor', revert['detail'])
        page = (run / 'conference-view' / 'index.html').read_text()
        self.assertNotIn('agentic', page)


if __name__ == '__main__':
    unittest.main()
