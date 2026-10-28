"""Offline tests of the experiment catalogue page (owner request, October 21, 2026): run list
columns, filters, sorting, the compare view, the optional per-run summary and its rollout
(maintain_hf_run.py --rerender-catalogue; transcripts maintenance of entries without a summary).
In-memory private Space; no HF login, network, credentials or model calls.
"""
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_experiment_index import build, judge_summary, write_index  # noqa: E402
from maintain_hf_run import main, unlist_run  # noqa: E402
from publish_hf_library import digest, publish  # noqa: E402
import test_hf_maintenance as maintenance  # noqa: E402  (module import: its test classes are not re-run here)
from test_judge_publication import make_run, make_viewer  # noqa: E402
from research.analysis.private_material import skip_decorator as needs_private  # noqa: E402

COUNTS = {'agents': 10, 'graded': 9, 'provisional': 1,
          'cohorts': {'convert': 1, 'exploiter': 4, 'exploit_promoter': 0, 'whistleblower': 2,
                      'no_misbehavior': 2, 'insufficient_record': 1}}


def entry(run, hour, **extra):
    return {'run': run, 'date': f'2026-10-2{hour % 10} 10:00 PDT', 'start_utc': f'2026-10-20T{hour:02d}:00:00+00:00',
            'track': 'spontaneous', 'agents': 10, 'model': 'vendor/model-' + run, 'provider_route': 'route/us',
            'elapsed_seconds': 3725.5, 'pilot_spent_usd': '12.345', 'distinct_verified_tasks': 3, 'task_count': 14,
            'agent_exits': {'harness_completed': 9, 'error': 1}, 'links': {'Report': run + '/results.html'}, **extra}


def page_data(page):
    blob = re.search(r'<script type="application/json" id="catalogue-data">(.*?)</script>', page, re.S)[1]
    return json.loads(blob)


class CataloguePageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, entries, hosted=True):
        out = self.root / 'out'
        write_index(out, entries, hosted=hosted)
        return (out / 'index.html').read_text(), (out / 'experiments.json').read_text()

    def test_build_records_summary_from_settings_results_and_judge_counts(self):
        runs = self.root / 'runs'
        run = make_run(runs, 'judged', judge={})
        report = json.loads((run / 'analysis/judge/report.json').read_text())
        (run / 'analysis/judge/report.json').write_text(json.dumps({**report, 'counts': COUNTS}))
        results = json.loads((run / 'results.json').read_text())
        (run / 'results.json').write_text(json.dumps({**results, 'operationally_accepted_tasks': 5,
                                                      'condition': {'prompt_variant': 'collaboration_continuous'}}))
        make_run(runs, 'plain')
        build(runs, unlisted=set())
        entries = {e['run']: e for e in json.loads((runs / 'experiments.json').read_text())['experiments']}
        self.assertEqual(entries['judged']['summary'], {
            'prompt_variant': 'collaboration_continuous', 'accepted_tasks': 5,
            'judge': {'mode': 'paid', 'agents': 10, 'graded': 9, 'provisional': 1, 'cohorts': {
                'exploiter': 4, 'convert': 1, 'exploit_promoter': 0, 'whistleblower': 2, 'no_misbehavior': 2,
                'insufficient_record': 1}}, 'simple_judge': None})
        self.assertEqual(entries['plain']['summary'], {'prompt_variant': None, 'accepted_tasks': None, 'judge': None,
                                                       'simple_judge': None})
        page = (runs / 'index.html').read_text()
        self.assertIn('4 exploiters · 1 convert · 2 whistleblowers · 1 insufficient record', page)
        self.assertIn('of 9 graded, 1 provisional', page)
        self.assertIn('collaboration_continuous', page)
        self.assertIn('<span class="muted">not run</span>', page)

    @needs_private("research/analysis/simple_judge/prompts.py")
    def test_simple_judge_counts_shown_labelled_and_linked(self):
        """Owner, October 25, 2026: a run with the simplified judge shows its counts ("simple"), next to the
        rubric-3.1 cohorts ("rubric 3.1") when both ran; a published entry without them gets them in the browser."""
        from simple_judge_site import simple_summary
        import test_simple_judge_site as sjs
        runs = self.root / 'runs'
        both = make_run(runs, 'both', judge={})
        report = json.loads((both / 'analysis/judge/report.json').read_text())
        (both / 'analysis/judge/report.json').write_text(json.dumps({**report, 'counts': COUNTS}))
        sjs.write_simple_judge(both, local=False)
        only = make_run(runs, 'only')
        sjs.write_simple_judge(only, local=False)
        build(runs, unlisted=set())
        entries = {e['run']: e for e in json.loads((runs / 'experiments.json').read_text())['experiments']}
        expected = simple_summary(json.loads((only / 'analysis/simple_judge/report.json').read_text()))
        self.assertEqual(entries['only']['summary']['simple_judge'], expected)
        self.assertEqual(expected['levels']['credited'], 1)
        page = (runs / 'index.html').read_text()
        self.assertIn('rubric 3.1: 4 exploiters · 1 convert · 2 whistleblowers · 1 insufficient record', page)
        self.assertIn('simple: 1 credited<br><small>of 2 labelled · <a href="only/analysis/simple_judge/report.html">'
                      'report</a></small>', page)
        cell = re.search(r'data-run="only".*?class="c-judge"><div class="v">(.*?)</div>', page, re.S)[1]
        self.assertNotIn('not run', cell)
        # A published entry that links the simplified judge but predates the counts: a marker the page fills.
        old = entry('a-old', 1, summary={'prompt_variant': 'v1', 'accepted_tasks': 4, 'judge': None},
                    links={'Report': 'a-old/results.html',
                           'Simple behavior judge': 'a-old/analysis/simple_judge/report.html'})
        page, index = self.write([old])
        self.assertEqual(json.loads(index)['experiments'][0], old)  # entry unchanged
        self.assertIn('<span class="pending sj-pending">simple: …</span>', page)
        self.assertIn("read(base+'analysis/simple_judge/report.json')", page)
        self.assertIn("r.schema!=='gdm-simple-judge/1'", page)
        self.assertIn("'Simple judge: evaluation-aware (not comparable with rubric 3.1)'", page)
        self.assertIsNone(simple_summary({'schema': 'gdm-simple-judge/1', 'counts': {'agents': 2}}))
        self.assertIsNone(simple_summary({'schema': 'other', 'counts': {}}))

    def test_judge_summary_rejects_reports_without_valid_counts(self):
        self.assertIsNone(judge_summary({'schema': 'gdm-judge-report/3'}))
        self.assertIsNone(judge_summary({'counts': {**COUNTS, 'cohorts': {'exploiter': '4'}}}))
        self.assertIsNone(judge_summary({'counts': {**COUNTS, 'agents': True}}))
        self.assertIsNone(judge_summary([]))

    def test_columns_controls_and_embedded_entries(self):
        entries = [entry('a-old', 1), entry('c-new', 9, summary={'prompt_variant': 'v1', 'accepted_tasks': 4,
                                                                 'judge': None})]
        page, index = self.write(entries)
        for column in ('Start', 'Model', 'Prompt variant', 'Agents', 'Duration', 'Accepted / genuine', 'Cost',
                       'Behavior judge', 'Open'):
            self.assertIn(f'<button type="button">{column}</button>' if column != 'Open' else '>Open</th>', page)
        for control in ('id="filter"', 'id="f-model"', 'id="f-variant"', 'id="f-judge"', 'id="sort"',
                        'id="compare"', 'id="compare-view"', 'value="c-new"', 'aria-sort="descending"'):
            self.assertIn(control, page)
        self.assertEqual(page_data(page), json.loads(index)['experiments'])
        self.assertEqual([e['run'] for e in page_data(page)], ['c-new', 'a-old'])  # newest first
        rows = page.split('<tbody>', 1)[1]
        self.assertLess(rows.index('c-new'), rows.index('a-old'))
        self.assertIn('1h 02m', page)
        self.assertIn('$12.34', page)  # Decimal rounding, as before
        self.assertIn('1 error exit', page)
        self.assertIn('<span class="c-accepted">4</span> / <strong>3</strong>', page)
        # Entries published before the summary: placeholders, filled in the browser.
        self.assertIn('<span class="c-accepted"><span class="pending"', page)

    def test_transcript_pages_are_not_linked(self):
        """Transcripts are read in Inspect View (owner, Oct 21, 2026); older entries keep the link field."""
        runs = self.root / 'runs'
        run = make_run(runs, 'fresh')
        (run / 'transcripts').mkdir(exist_ok=True)
        (run / 'transcripts/index.html').write_text('<!doctype html><title>t</title>')
        build(runs, unlisted=set())
        built = json.loads((runs / 'experiments.json').read_text())['experiments'][0]
        self.assertNotIn('Transcripts', built['links'])
        self.assertNotIn('transcripts/index.html', (runs / 'index.html').read_text())
        older = entry('a-old', 1, links={'Report': 'a-old/results.html', 'Transcripts': 'a-old/transcripts/index.html'})
        page, index = self.write([older])
        self.assertEqual(json.loads(index)['experiments'][0]['links'], older['links'])  # entry unchanged
        self.assertNotIn('href="a-old/transcripts/index.html"', page)
        self.assertIn("HIDDEN_LINKS=['Transcripts','Inspect logs','Timeline']", page)

    def test_inspect_log_links_are_kept_as_data_but_not_shown(self):
        """site-nologs: the raw .eval links were only downloads; the entry keeps them, no page shows them."""
        runs = self.root / 'runs'
        run = make_run(runs, 'b-new')
        (run / 'inspect').mkdir(exist_ok=True)
        (run / 'inspect' / 'one.eval').write_bytes(b'x')
        (run / 'inspect-view').mkdir(exist_ok=True)
        (run / 'inspect-view' / 'index.html').write_text('<!doctype html><title>v</title>')
        build(runs, unlisted=set())
        built = json.loads((runs / 'experiments.json').read_text())['experiments'][0]
        self.assertEqual(built['links']['Inspect logs'], 'b-new/inspect/one.eval')
        self.assertIn('href="b-new/inspect-view/index.html">Inspect View</a>', (runs / 'index.html').read_text())
        self.assertNotIn('href="b-new/inspect/', (runs / 'index.html').read_text())
        several = entry('a-old', 1, links={'Report': 'a-old/results.html', 'Inspect log 1': 'a-old/inspect/a.eval',
                                           'Inspect log 2': 'a-old/inspect/b.eval'})
        page, index = self.write([several])
        self.assertEqual(json.loads(index)['experiments'][0]['links'], several['links'])  # entry unchanged
        self.assertNotIn('href="a-old/inspect/', page)
        self.assertNotIn('>Inspect log', page)
        self.assertIn("n.startsWith('Inspect log ')", page)  # the compare view hides them too

    def test_hosted_page_is_self_contained_dark_mode_and_mobile(self):
        page, _ = self.write([entry('a-old', 1)])
        self.assertIn('id="private-space-navigation"', page)
        self.assertIn('prefers-color-scheme:dark', page)
        self.assertIn('@media (max-width:760px)', page)
        self.assertIn('name="viewport"', page)
        # No external scripts, styles or links; the one external link is the reconstructed paper (October 27, 2026).
        self.assertNotRegex(page.replace('href="https://arxiv.org/pdf/2609.04170"', ''), r'(src|href)="(https?:)?//')
        self.assertNotIn('<script src', page)

    def test_embedded_json_cannot_close_its_script_and_text_is_escaped(self):
        hostile = entry('a-old', 1, model='</script><script>alert(1)</script>')
        page, _ = self.write([hostile])
        self.assertNotIn('<script>alert(1)', page)
        self.assertEqual(page_data(page)[0]['model'], hostile['model'])

    def test_rendering_is_deterministic(self):
        entries = [entry('a-old', 1), entry('b-mid', 5, summary={'prompt_variant': None, 'accepted_tasks': None,
                                                                 'judge': None})]
        self.assertEqual(self.write(entries), self.write(list(reversed(entries))))

    def test_judge_link_without_summary_is_pending_and_with_summary_points_to_report(self):
        links = {'Report': 'r/results.html', 'Behavior judge': 'r/analysis/judge/report.html'}
        page, _ = self.write([entry('r', 1, links=links)])
        self.assertIn('class="c-judge"><div class="v"><span class="pending">…</span>', page)
        page, _ = self.write([entry('r', 1, links=links, summary={'prompt_variant': None, 'accepted_tasks': 1,
                                                                   'judge': None})])
        self.assertIn('class="c-judge"><div class="v">see report</div>', page)


def push_catalogue(hub, entries, html=None):
    """An out-of-band catalogue commit with consistent manifest digests (an older publication)."""
    with tempfile.TemporaryDirectory() as tmp:
        write_index(Path(tmp), entries, hosted=True)
        files = {n: (Path(tmp) / n).read_bytes() for n in ('index.html', 'experiments.json')}
    if html is not None:
        files['index.html'] = html
    manifest = json.loads(hub.files['archive-manifest.json'])
    for name, data in files.items():
        manifest['files'][name] = digest(data)
    manifest['total_bytes'] = sum(x['bytes'] for x in manifest['files'].values())
    hub.push({**hub.files, **files, 'archive-manifest.json': json.dumps(manifest, indent=2).encode()})


class RerenderCatalogueTests(unittest.TestCase):
    prepare = maintenance.MaintenanceTests.prepare

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, 'a-old')
        make_run(self.runs, 'b-new')
        self.hub = maintenance.Hub(self.root)
        maintenance.legacy_publish(self.prepare('published', 'a-old', 'b-new'), self.hub, self.hub.download, commit=True,
                operation=maintenance.operation, unlisted=set(), records_in_space=True)
        entries = json.loads(self.hub.files['experiments.json'])['experiments']
        push_catalogue(self.hub, entries, html=b'<!doctype html><html>page of an older generator</html>')
        self.published = dict(self.hub.files)
        self.commits = len(self.hub.commits)

    def rerender(self, commit=False, attempt='attempt-1'):
        kwargs = {}
        if commit:
            kwargs = {'backup_dir': self.root / 'records' / attempt,
                      'receipt': self.root / 'records' / attempt / 'receipt.json'}
        return unlist_run(self.hub, self.hub.download, run=None, rerender=True, commit=commit,
                          operation=maintenance.operation, **kwargs)

    def test_rerender_replaces_only_the_page(self):
        plan = self.rerender()
        self.assertEqual((plan['scope'], plan['catalogue_files'], plan['entries_after']),
                         ('rerender-catalogue', ['index.html', 'archive-manifest.json'], 2))
        self.assertEqual(len(self.hub.commits), self.commits)
        result = self.rerender(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        for name, data in self.published.items():
            if name not in ('index.html', 'archive-manifest.json'):
                self.assertEqual(final[name], data, name)
        entries = json.loads(final['experiments.json'])['experiments']
        with tempfile.TemporaryDirectory() as tmp:
            write_index(Path(tmp), entries, hosted=True)
            self.assertEqual(final['index.html'], (Path(tmp) / 'index.html').read_bytes())
        manifest = json.loads(final['archive-manifest.json'])
        self.assertNotIn('unlisted_runs', manifest)
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        self.assertTrue(self.rerender(commit=True, attempt='attempt-2')['already_applied'])
        self.assertEqual(len(self.hub.commits), self.commits + 1)

    def test_rerender_reorders_an_oldest_first_catalogue_newest_first(self):
        entries = json.loads(self.published['experiments.json'])['experiments']
        self.assertEqual([x['run'] for x in entries], ['b-new', 'a-old'])
        oldest_first = json.dumps({**json.loads(self.published['experiments.json']),
                                   'experiments': entries[::-1]}, indent=2).encode()
        manifest = json.loads(self.published['archive-manifest.json'])
        manifest['files']['experiments.json'] = digest(oldest_first)
        manifest['total_bytes'] = sum(x['bytes'] for x in manifest['files'].values())
        self.hub.push({**self.published, 'experiments.json': oldest_first,
                       'archive-manifest.json': json.dumps(manifest, indent=2).encode()})
        before = dict(self.hub.files)
        plan = self.rerender()
        self.assertTrue(plan['reordered'])
        self.assertEqual(sorted(plan['catalogue_files']), ['archive-manifest.json', 'experiments.json', 'index.html'])
        result = self.rerender(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = json.loads(self.hub.files['experiments.json'])['experiments']
        self.assertEqual(final, entries)  # same entries, newest first
        for name, data in before.items():
            if name not in ('index.html', 'experiments.json', 'archive-manifest.json'):
                self.assertEqual(self.hub.files[name], data, name)

    def test_rerender_refuses_entries_the_generator_would_change(self):
        reformatted = json.dumps(json.loads(self.published['experiments.json']), indent=1).encode()
        self.hub.push({**self.published, 'experiments.json': reformatted})
        with self.assertRaisesRegex(ValueError, 'experiments.json byte for byte'):
            self.rerender()

    def test_cli_rerender_takes_no_run_or_bundle(self):
        with self.assertRaises(SystemExit), unittest.mock.patch('sys.stderr'):
            main(['--rerender-catalogue', '--maintain-run', 'a-old'])
        with self.assertRaises(SystemExit), unittest.mock.patch('sys.stderr'):
            main(['--rerender-catalogue', '--bundle', str(self.root)])


class TranscriptsScopeWithOlderEntryTests(unittest.TestCase):
    """A run published before the summary existed keeps its entry; transcripts maintenance proceeds."""
    setUp = maintenance.TranscriptMaintenanceTests.setUp
    prepare, records = maintenance.MaintenanceTests.prepare, maintenance.MaintenanceTests.records
    edit_bundle, maintain = maintenance.MaintenanceTests.edit_bundle, maintenance.TranscriptMaintenanceTests.maintain

    def test_bundle_summary_is_ignored_for_an_entry_without_one(self):
        entries = json.loads(self.hub.files['experiments.json'])['experiments']
        push_catalogue(self.hub, [{k: v for k, v in e.items() if k != 'summary'} for e in entries])
        before = {n: self.hub.files[n] for n in ('index.html', 'experiments.json')}
        self.assertIn('summary', json.loads((self.bundle / 'experiments.json').read_text())['experiments'][0])
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        for name, data in before.items():
            self.assertEqual(self.hub.files[name], data, name)

    def test_a_different_entry_is_still_refused(self):
        entries = json.loads(self.hub.files['experiments.json'])['experiments']
        push_catalogue(self.hub, [{**{k: v for k, v in e.items() if k != 'summary'}, 'track': 'edited'}
                                  for e in entries])
        with self.assertRaisesRegex(ValueError, 'differs from the published entry'):
            self.maintain()


if __name__ == '__main__':
    unittest.main()
