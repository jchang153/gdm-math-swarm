"""Offline tests: behavior judge link in the experiment index and judge outputs in HF preparation.

Synthetic run folders only; no network, HF authentication, model calls or credentials.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_experiment_index  # noqa: E402
import prepare_hf_library  # noqa: E402
from publish_hf_library import load_bundle  # noqa: E402


def make_run(runs, run_id, *, judge=None):
    run = Path(runs) / run_id
    run.mkdir(parents=True)
    (run / 'results.json').write_text(json.dumps({
        'run': run_id, 'start_utc': '2026-10-16T02:14:58+00:00', 'elapsed_seconds': 10800.0,
        'agent_exits': {'harness_completed': 2}, 'budget': {'pilot': {'settled_usd': '1.5'}},
        'verified_submissions': []}, indent=2))
    (run / 'results.html').write_text('<!doctype html><html><h1>R</h1></html>')
    for name in ('transcripts/index.html', 'conference-view/index.html'):
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text('<!doctype html><html><head></head><body>x</body></html>')
    if judge is not None:
        write_judge(run, **judge)
    return run


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_judge(run, *, mode='paid', report_current=True, stale_input=False, legacy_marks=False,
                report_schema='gdm-judge-report/3'):
    """Synthetic judge outputs with a read-only evidence browser bound to its inputs
    (review.py manifest schema gdm-judge-review-manifest/3, misbehavior judge rubric v2) and a
    report bound to it."""
    judge = run / 'analysis' / 'judge'
    files = {'report.html': '<!doctype html><html><head></head><body>J</body></html>',
             'labels.jsonl': '{"label": "detection"}\n', 'pipeline.json': '{"mode": "%s"}' % mode,
             'run_level.json': '{}', 'agents/agent-001.json': '{}', 'packages/agent-001.json': '{}',
             'units/agent-001/verify-b01.json': '{"purpose": "verifier"}',
             'review/index.html': '<!doctype html><html><head></head><body>R</body></html>',
             'review/agents/agent-001.html': '<!doctype html><html><head></head><body>A</body></html>',
             'raw/opus/request-0001.json': '{"messages": "full request"}',
             'raw/opus/response-0001.json': '{"id": "gen-1"}', 'ledger-copy.sqlite': 'db',
             'scratch.txt': 'unlisted'}
    if legacy_marks:  # files from the withdrawn spot-check design must never be published
        files.update({'review/marks.json': '{"reviewer": "u"}', 'review/reported.json': '{}',
                      'review/labels-reported.jsonl': '', 'review/history/marks-1.json': '{}'})
    for name, text in files.items():
        path = judge / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    inputs = {name: _sha(judge / name) for name in ('agents/agent-001.json', 'labels.jsonl', 'packages/agent-001.json',
                                                    'pipeline.json', 'run_level.json',
                                                    'units/agent-001/verify-b01.json')}
    manifest = {'schema': 'gdm-judge-review-manifest/3', 'run_id': run.name, 'read_only': True,
                'inputs_sha256': inputs, 'pages': {'index.html': _sha(judge / 'review/index.html'),
                                                   'agents/agent-001.html': _sha(judge / 'review/agents/agent-001.html')}}
    (judge / 'review/manifest.json').write_text(json.dumps(manifest))
    manifest_sha = _sha(judge / 'review/manifest.json')
    (judge / 'report.json').write_text(json.dumps({
        'schema': report_schema, 'mode': mode,
        'review_page': {'status': 'current', 'manifest_file_sha256': manifest_sha if report_current else '0' * 64}}))
    if stale_input:
        (judge / 'agents/agent-001.json').write_text('{"changed": true}')
    html = run / 'results.html'
    html.write_text(html.read_text().replace('</html>', '<!-- behavior-judge:start --><section>B</section>'
                                             '<!-- behavior-judge:end --></html>'))


def make_viewer(root):
    viewer = Path(root) / 'viewer'
    (viewer / 'logs').mkdir(parents=True)
    (viewer / 'index.html').write_text('<!doctype html><html><head></head><body>v</body></html>')
    (viewer / 'logs' / 'listing.json').write_text('{}')
    return viewer


class ExperimentIndexLinkTests(unittest.TestCase):
    def test_behavior_judge_link_only_when_report_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(tmp, 'judged-run', judge={})
            make_run(tmp, 'plain-run')
            result = build_experiment_index.build(tmp)
            self.assertEqual(result['warnings'], [])
            entries = {e['run']: e for e in json.loads((Path(tmp) / 'experiments.json').read_text())['experiments']}
            self.assertEqual(entries['judged-run']['links']['Behavior judge'], 'judged-run/analysis/judge/report.html')
            self.assertNotIn('Behavior judge', entries['plain-run']['links'])
            index = (Path(tmp) / 'index.html').read_text()
            self.assertIn('href="judged-run/analysis/judge/report.html">Behavior judge</a>', index)


class PrepareJudgeOutputsTests(unittest.TestCase):
    def prepare(self, tmp, run_id):
        output = Path(tmp) / 'bundle'
        argv = ['prepare_hf_library.py', '--runs-dir', str(Path(tmp) / 'runs'),
                '--run', f'{run_id}={make_viewer(tmp)}', '--output-dir', str(output)]
        with patch.object(sys, 'argv', argv), patch('builtins.print'):
            prepare_hf_library.main()
        return output

    def test_paid_judge_outputs_published_without_raw_archives_or_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_run(Path(tmp) / 'runs', 'judged-run', judge={'legacy_marks': True})
            output = self.prepare(tmp, 'judged-run')
            judge = output / 'judged-run' / 'analysis' / 'judge'
            for name in ('report.html', 'report.json', 'labels.jsonl', 'pipeline.json', 'run_level.json',
                         'agents/agent-001.json', 'packages/agent-001.json', 'review/index.html',
                         'review/manifest.json', 'review/agents/agent-001.html'):
                self.assertTrue((judge / name).is_file(), name)
            for name in ('raw', 'units', 'ledger-copy.sqlite', 'scratch.txt', 'review/marks.json',
                         'review/reported.json', 'review/labels-reported.jsonl', 'review/history'):
                self.assertFalse((judge / name).exists(), name)
            manifest = json.loads((output / 'archive-manifest.json').read_text())
            self.assertFalse(any('/raw/' in name or '/units/' in name for name in manifest['files']))
            self.assertEqual(sorted(manifest['behavior_judge']['judged-run']['excluded']), [
                'analysis/judge/ledger-copy.sqlite', 'analysis/judge/raw/opus/request-0001.json',
                'analysis/judge/raw/opus/response-0001.json', 'analysis/judge/review/history/marks-1.json',
                'analysis/judge/review/labels-reported.jsonl', 'analysis/judge/review/marks.json',
                'analysis/judge/review/reported.json', 'analysis/judge/scratch.txt',
                'analysis/judge/units/agent-001/verify-b01.json'])
            self.assertIn('id="private-space-navigation"', (judge / 'review' / 'index.html').read_text())
            self.assertIn('id="private-space-navigation"', (judge / 'review' / 'agents' / 'agent-001.html').read_text())
            _, entries = load_bundle(output)  # the publisher accepts the prepared bundle
            self.assertEqual(entries[0]['links']['Behavior judge'], 'judged-run/analysis/judge/report.html')

    def test_rehearsal_or_stale_outputs_refused(self):
        cases = [{'mode': 'rehearsal'}, {'report_current': False}, {'stale_input': True},
                 {'report_schema': 'gdm-judge-report/2'}]  # a report of the superseded 12-dimension judge
        for judge in cases:
            with self.subTest(judge=judge), tempfile.TemporaryDirectory() as tmp:
                make_run(Path(tmp) / 'runs', 'judged-run', judge=judge)
                with self.assertRaises(ValueError):
                    self.prepare(tmp, 'judged-run')
                self.assertFalse((Path(tmp) / 'bundle').exists())
        with tempfile.TemporaryDirectory() as tmp:  # an output added after the browser was built
            run = make_run(Path(tmp) / 'runs', 'judged-run', judge={})
            (run / 'analysis/judge/agents/agent-002.json').write_text('{}')
            with self.assertRaises(ValueError):
                self.prepare(tmp, 'judged-run')
        with tempfile.TemporaryDirectory() as tmp:  # an agent page the current browser did not produce
            run = make_run(Path(tmp) / 'runs', 'judged-run', judge={})
            (run / 'analysis/judge/review/agents/agent-099.html').write_text('<html></html>')
            with self.assertRaises(ValueError):
                self.prepare(tmp, 'judged-run')
        for schema in ('gdm-judge-review-manifest/1', 'gdm-judge-review-manifest/2'):  # spot-check / 12-dimension
            with tempfile.TemporaryDirectory() as tmp:
                run = make_run(Path(tmp) / 'runs', 'judged-run', judge={})
                (run / 'analysis/judge/review/manifest.json').write_text(json.dumps({'schema': schema}))
                with self.assertRaises(ValueError):
                    self.prepare(tmp, 'judged-run')

    def test_section_without_judge_directory_refused_and_plain_runs_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp) / 'runs', 'judged-run', judge={})
            for path in sorted((run / 'analysis' / 'judge').rglob('*'), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            (run / 'analysis' / 'judge').rmdir()
            with self.assertRaises(ValueError):
                self.prepare(tmp, 'judged-run')
        with tempfile.TemporaryDirectory() as tmp:
            make_run(Path(tmp) / 'runs', 'plain-run')
            output = self.prepare(tmp, 'plain-run')
            self.assertNotIn('behavior_judge', json.loads((output / 'archive-manifest.json').read_text()))
            self.assertTrue((output / 'plain-run' / 'results.json').is_file())

    def test_judge_exclusion_rule(self):
        excluded = prepare_hf_library.judge_excluded
        for path in ('analysis/judge', 'analysis/judge/report.html', 'analysis/judge/review',
                     'analysis/judge/review/index.html', 'analysis/judge/review/manifest.json',
                     'analysis/judge/review/agents', 'analysis/judge/review/agents/agent-001.html',
                     'analysis/judge/agents/agent-001.json', 'analysis/timeline.md'):
            self.assertFalse(excluded(path), path)
        for path in ('analysis/judge/raw', 'analysis/judge/raw/request.json', 'analysis/judge/units/a/verify-b01.json',
                     'analysis/judge/report.html/nested', 'analysis/judge/unknown.json',
                     'analysis/judge/review/marks.json', 'analysis/judge/review/reported.json',
                     'analysis/judge/review/labels-reported.jsonl', 'analysis/judge/review/history/marks-1.json',
                     'analysis/judge/review/other.html'):
            self.assertTrue(excluded(path), path)

if __name__ == '__main__':
    unittest.main()
