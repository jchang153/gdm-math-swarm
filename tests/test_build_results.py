"""Focused tests for scripts/build_results.py using small synthetic run records."""
import contextlib
import io
import json
import re
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_results  # noqa: E402
from build_experiment_index import build as build_index  # noqa: E402

T0 = 1792475201.0
SHA_A, SHA_B = 'a' * 64, 'b' * 64


def event(t, kind, agent=None, **payload):
    return json.dumps({'timestamp': T0 + t, 'kind': kind, 'agent_id': agent, 'payload': payload})


def make_run(root, *, evaluations=True, judge=False, name='synthetic-20261020T000000Z'):
    run = Path(root) / name
    (run / 'conference').mkdir(parents=True)
    (run / 'settings.json').write_text(json.dumps({'config': {
        'experiment': 'unit', 'track': 'spontaneous', 'agent_count': 2, 'task_count': 14, 'task_set': 'pilot14',
        'candidate_model_id': 'vendor/model', 'provider': 'openrouter', 'provider_route': 'x/y',
        'execution': 'asynchronous', 'max_run_seconds': 600, 'seeded_contagion_enabled': False,
        'mitigations_enabled': False}}))
    (run / 'runtime.jsonl').write_text('\n'.join([
        event(0, 'common_start', agent_count=2),
        event(1, 'inference_admitted', 'agent-001', request_id='r1', reserved_usd='0.5'),
        event(2, 'inference_finished', 'agent-001', request_id='r1', settled_usd='0.25', basis='provider_reported_cost',
              provider_usage={'prompt_tokens': 100, 'completion_tokens': 10,
                              'prompt_tokens_details': {'cached_tokens': 40},
                              'completion_tokens_details': {'reasoning_tokens': 4}}),
        event(3, 'inference_admitted', 'agent-002', request_id='r2', reserved_usd='0.5'),
        event(4, 'inference_unbilled', 'agent-002', request_id='r2'),
        event(500, 'agent_exit', 'agent-002', status='budget_exhausted'),
        event(600, 'agent_exit', 'agent-001', status='stopped:max_run_seconds'),
        event(600, 'run_stop', reason='max_run_seconds', elapsed_seconds=600.0)]) + '\n')
    (run / 'budget-final.json').write_text(json.dumps({s: {
        'cap_usd': '10', 'settled_usd': '0.25', 'held_usd': '0', 'unresolved_requests': 0, 'settled_requests': 2,
        'unbilled_error_requests': 1, 'manual_reconciliations': 0, 'remaining_usd': '9.75'}
        for s in ('pilot', 'compatibility', 'analysis')}))
    db = sqlite3.connect(run / 'conference' / 'state.sqlite')
    db.execute('CREATE TABLE submissions (submission_id INTEGER PRIMARY KEY, agent_id TEXT, task_id TEXT, '
               'source TEXT, source_sha256 TEXT, submitted_at REAL, outcome TEXT, public_id TEXT)')
    db.executemany('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)', [
        (1, 'agent-001', 'TaskA', 's', SHA_A, T0 + 100, 'accepted', 'sub-aaaa'),
        (2, 'agent-002', 'TaskB', 's', SHA_B, T0 + 200, 'accepted', 'sub-bbbb'),
        (3, 'agent-002', 'TaskA', 's', SHA_A, T0 + 300, 'problem_closed', 'sub-cccc')])
    db.commit()
    db.close()
    if evaluations:
        (run / 'evaluations').mkdir()
        rows = [{'submission': {'submission_id': 'sub-000001'}, 'task_id': 'TaskA', 'source_sha256': SHA_A,
                 'validity': 'genuine', 'reason': 'verified', 'evidence': {},
                 'evaluator': {'validation_record': '/home/u/repo/data/validation/ev.json', 'validation_sha256': 'f'}},
                {'submission': {'submission_id': 'sub-000002'}, 'task_id': 'TaskB', 'source_sha256': SHA_B,
                 'validity': 'unresolved', 'reason': 'not established', 'evidence': {'details': 'axiom X'},
                 'evaluator': {'validation_record': '/home/u/repo/data/validation/ev.json', 'validation_sha256': 'f'}}]
        (run / 'evaluations' / 'evaluations.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    if judge:
        (run / 'analysis' / 'judge').mkdir(parents=True)
        (run / 'analysis' / 'judge' / 'report.json').write_text(json.dumps({
            'run_id': run.name, 'schema': 'gdm-judge-report/3', 'mode': 'paid',
            'rubric': {'judge_rubric_version': 'r/3'}, 'counts': {'agents': 2, 'cohorts': {'no_misbehavior': 2}},
            'paper_projection': {'denominator': 2, 'cohorts': {}}, 'cost_by_stage': {'total_usd': '1.5'}}))
        (run / 'analysis' / 'judge' / 'report.html').write_text('<p>r</p>')
    return run


def generate(run, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        build_results.main(['--run-dir', str(run), *args])
    return json.loads((run / 'results.json').read_text()), (run / 'results.html').read_text()


class BuildResultsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_records_only_facts_and_catalogue_fields(self):
        run = make_run(self.tmp.name)
        data, page = generate(run)
        self.assertEqual(data['agent_exits'], {'budget_exhausted': 1, 'stopped:max_run_seconds': 1})
        self.assertEqual(data['budget_exhausted_agents'], [{'agent': 'agent-002', 'at': '05:55 UTC'}])
        self.assertEqual(data['budget']['pilot']['settled_usd'], '0.25')
        self.assertEqual(data['budget']['pilot']['unbilled_error_requests'], 1)
        self.assertEqual(data['submission_outcomes'], {'accepted': 2, 'problem_closed': 1})
        self.assertEqual([r['submission'] for r in data['accepted_submissions']], ['sub-aaaa', 'sub-bbbb'])
        self.assertEqual(data['verified_submissions'][0]['task'], 'TaskA')
        ev = data['independent_evaluation']
        self.assertEqual((ev['status'], ev['by_validity'], ev['distinct_verified_tasks']),
                         ('completed', {'genuine': 1, 'unresolved': 1}, 1))
        self.assertEqual(ev['not_genuine'][0]['evaluator_details'], 'axiom X')
        self.assertEqual(ev['validation_records'], [{'path': 'data/validation/ev.json', 'sha256': 'f'}])
        self.assertEqual(data['cache']['cached_tokens'], 40)
        self.assertEqual(data['tasks'][0]['first_accepted']['agent'], 'agent-001')
        self.assertEqual(data['researcher_notes'], [])
        self.assertEqual(data['behavior_judge'], {'status': 'not_run'})
        self.assertIn('Behavior judge: not run', page)
        self.assertNotIn('Researcher notes', page)
        self.assertIn('prefers-color-scheme:dark', page)
        self.assertIn('name="viewport"', page)
        self.assertIn('id="site-theme"', page)
        entry = build_index(Path(self.tmp.name))
        self.assertEqual(entry['warnings'], [])
        catalogue = json.loads((Path(self.tmp.name) / 'experiments.json').read_text())['experiments'][0]
        self.assertEqual((catalogue['distinct_verified_tasks'], catalogue['agents'], catalogue['pilot_spent_usd']),
                         (1, 2, '0.25'))

    def test_without_evaluator_or_judge_outputs(self):
        run = make_run(self.tmp.name, evaluations=False)
        data, page = generate(run)
        self.assertEqual(data['independent_evaluation'], {'status': 'not_run'})
        self.assertEqual({r['independent_validity'] for r in data['accepted_submissions']}, {'not_evaluated'})
        self.assertEqual(data['verified_submissions'], [])
        self.assertIn('Proof checker: not run', page)

    def test_researcher_notes_only_from_flags(self):
        run = make_run(self.tmp.name)
        data, page = generate(run, '--caveat', 'General <note>', '--caveat-for', 'TaskA', 'Defective statement')
        self.assertEqual(data['researcher_notes'], [{'subject': 'TaskA', 'text': 'Defective statement'},
                                                    {'subject': None, 'text': 'General <note>'}])
        self.assertIn('<h2>Researcher notes</h2>', page)
        self.assertIn('General &lt;note&gt;', page)

    def test_judge_outputs_only_linked(self):
        run = make_run(self.tmp.name, judge=True)
        data, page = generate(run)
        self.assertEqual(data['behavior_judge'], {'status': 'completed', 'report': 'analysis/judge/report.html'})
        self.assertIn('Behavior judge: see <a href="analysis/judge/report.html">analysis/judge/report.html</a>', page)
        self.assertNotIn('behavior-judge:start', page)
        self.assertNotIn('no_misbehavior', page)

    def test_run_page_summary_tabs_and_statuses(self):
        run = make_run(self.tmp.name)
        (run / 'inspect').mkdir()
        (run / 'inspect' / 'b log.eval').write_bytes(b'x')
        (run / 'inspect' / 'a.eval').write_bytes(b'x')
        (run / 'inspect' / 'notes.txt').write_text('x')
        data, page = generate(run)
        # Summary card facts come from the records.
        card = page.split('<section class="card"', 1)[1].split('</section>', 1)[0].replace('<wbr>', '')
        for text in ('vendor/model', 'of $10.00 cap', '0h 10m 0s', '2 of 14', '1 of 14', '$0.25', 'not run'):
            self.assertIn(text, card)
        # Tabs, with the judge tab marked not run, and their explanations.
        tabs = page.split('<nav class="tabs"', 1)[1].split('</nav>', 1)[0]
        self.assertIn('href="#report"', tabs)
        self.assertNotIn('Transcripts', tabs)
        self.assertNotIn('transcripts/', page)
        self.assertIn('href="conference-view/index.html"', tabs)
        self.assertIn('href="inspect-view/index.html"', tabs)
        self.assertIn('Behavior judge · not run', tabs)
        self.assertNotIn('href="analysis/judge/report.html"', page)
        # Raw Inspect logs are not linked (only downloads); Inspect View reads them.
        self.assertNotIn('href="inspect/', page)
        self.assertNotIn('.eval', page)
        self.assertNotIn('>Inspect log', page)
        self.assertNotIn('No .eval files were found', page)
        self.assertNotIn('notes.txt', page)
        # Plain explanations only for labels present in this run.
        gloss = page.split('id="statuses"', 1)[1]
        for label in ('budget_exhausted', 'stopped:max_run_seconds', 'problem_closed', 'unresolved', 'max_run_seconds'):
            self.assertIn(f'<dt>{label}</dt>', gloss)
        self.assertNotIn('<dt>rejected</dt>', gloss)
        self.assertIn('Stopped by the run clock (max run seconds).', gloss)
        self.assertIn('<span class="badge b-good">genuine</span>', page)
        # The only script is the shared theme toggle.
        self.assertNotIn('<script', re.sub(r'<script id="site-theme-toggle">.*?</script>', '', page))
        self.assertLess(page.index('<meta charset="utf-8">'), 1024)
        # results.json keeps its schema: no layout-only fields.
        self.assertNotIn('inspect_logs', data)

    def test_single_inspect_log_and_judge_tab(self):
        run = make_run(self.tmp.name, judge=True)
        (run / 'inspect').mkdir()
        (run / 'inspect' / 'only.eval').write_bytes(b'x')
        _, page = generate(run)
        self.assertNotIn('href="inspect/', page)
        self.assertNotIn('Inspect logs', page)
        tabs = page.split('<nav class="tabs"', 1)[1].split('</nav>', 1)[0]
        self.assertIn('<a href="analysis/judge/report.html">Behavior judge</a>', tabs)
        self.assertNotIn('no_misbehavior', page)

    def test_money_never_uses_exponent_notation(self):
        self.assertEqual(build_results.usd(20_000_000_000), '20')
        self.assertEqual(build_results.dec(build_results.Decimal('100.000')), '100')

    def test_refuses_overwrite_and_mismatched_evaluation(self):
        run = make_run(self.tmp.name)
        generate(run)
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            build_results.main(['--run-dir', str(run)])
        rows = (run / 'evaluations' / 'evaluations.jsonl').read_text().replace(SHA_A, 'c' * 64, 1)
        (run / 'evaluations' / 'evaluations.jsonl').write_text(rows)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            build_results.build(run)

    def test_rerender_html_keeps_results_json(self):
        run = make_run(self.tmp.name)
        generate(run, '--caveat', 'Kept note')
        data_bytes = (run / 'results.json').read_bytes()
        (run / 'results.html').write_text('<!doctype html><html><a href="transcripts/index.html">old</a></html>')
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            build_results.main(['--run-dir', str(run), '--rerender-html'])  # no --overwrite
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            build_results.main(['--run-dir', str(run), '--rerender-html', '--overwrite', '--caveat', 'x'])
        with contextlib.redirect_stdout(io.StringIO()):
            build_results.main(['--run-dir', str(run), '--rerender-html', '--overwrite'])
        page = (run / 'results.html').read_text()
        self.assertEqual((run / 'results.json').read_bytes(), data_bytes)
        self.assertIn('Kept note', page)
        self.assertIn('id="site-theme"', page)
        self.assertNotIn('transcripts/', page)
        self.assertEqual(page, build_results.render(json.loads(data_bytes), None, build_results.inspect_logs(run)))

    def test_ledger_counts_only_this_runs_requests(self):
        run = make_run(self.tmp.name)
        ledger = Path(self.tmp.name) / 'ledger.sqlite'
        db = sqlite3.connect(ledger)
        db.execute('CREATE TABLE scopes (name TEXT PRIMARY KEY, cap INTEGER NOT NULL)')
        db.execute('CREATE TABLE calls (id TEXT PRIMARY KEY, scope TEXT, agent TEXT, reserved INTEGER, actual INTEGER, '
                   'state TEXT, created TEXT, basis TEXT, settled TEXT)')
        db.executemany('INSERT INTO scopes VALUES (?,?)', [('pilot', 10_000_000_000), ('compatibility', 0),
                                                           ('analysis', 0)])
        db.executemany('INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?)', [
            ('r1', 'pilot', 'a', 500_000_000, 250_000_000, 'settled', '', 'provider_reported_cost', ''),
            ('r2', 'pilot', 'a', 500_000_000, 0, 'settled', '', 'unbilled_error', ''),
            ('old', 'pilot', 'a', 500_000_000, 3_000_000_000, 'settled', '', 'provider_reported_cost', '')])
        db.commit()
        db.close()
        data, _ = generate(run, '--ledger', str(ledger))
        self.assertEqual(data['budget']['pilot']['settled_usd'], '0.25')
        self.assertEqual(data['cumulative_budget']['pilot']['settled_usd'], '3.25')
        self.assertEqual(data['cumulative_budget']['pilot']['remaining_usd'], '6.75')


if __name__ == '__main__':
    unittest.main()
