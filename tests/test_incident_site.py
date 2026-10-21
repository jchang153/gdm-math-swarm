"""Offline tests of the incident report on the static site: allowlisted publication, the run-page tab,
the catalogue link and maintain_hf_run.py --scope incident (in-memory private Space fake)."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_experiment_index  # noqa: E402
from incident_site import add_incident_tab, incident_excluded, with_incident_link  # noqa: E402
import maintain_hf_run  # noqa: E402
from maintain_hf_run import maintain_incident  # noqa: E402
import prepare_hf_library  # noqa: E402
from publish_hf_library import digest, load_bundle, publish  # noqa: E402
from test_hf_maintenance import RULE, Hub, operation  # noqa: E402
from test_judge_publication import make_run, make_viewer  # noqa: E402

RUN, OTHER = 'incident-run', 'a-old'
PAGE = ('<!doctype html><html><head><meta charset="utf-8"></head><body><h1>R</h1>'
        '<nav class="tabs" aria-label="Run records"><a class="current" aria-current="page" href="#report">Report</a>'
        '<a href="conference-view/index.html">Conference</a><span class="off">Behavior judge · not run</span>'
        '<a href="inspect-view/index.html">Inspect</a></nav><ul class="tabhelp"><li><strong>Report</strong>: r</li>'
        '<li><strong>Inspect</strong>: i</li></ul><main id="report">facts</main></body></html>')
LOCAL_ONLY = ('digest.json', 'state.json', 'run-budgets.json', 'raw/opus/request-0001.json',
              'units/agent-001.json', 'route-pricing/x/pricing.json', '.incident.lock')
PUBLISHED = ('report.html', 'report.md', 'evidence.json')


def write_incident(run, report='<!doctype html><html><head><meta charset="utf-8"></head><body>'
                               '<a href="../../inspect-view/index.html#/logs/x">x</a>Story</body></html>'):
    folder = Path(run) / 'analysis' / 'incident'
    texts = {'report.html': report, 'report.md': '# Story\n', 'evidence.json': '{"excerpts": []}',
             **{name: '{"local": true}' for name in LOCAL_ONLY}}
    for name, text in texts.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    return folder


def prepare(runs, output, *selected, viewer):
    argv = ['prepare_hf_library.py', '--runs-dir', str(runs), '--output-dir', str(output)]
    for run in selected:
        argv += ['--run', f'{run}={viewer}']
    with patch.object(sys, 'argv', argv), patch('builtins.print'):
        prepare_hf_library.main()
    return output


class HelperTests(unittest.TestCase):
    def test_allowlist(self):
        for name in PUBLISHED:
            self.assertFalse(incident_excluded('analysis/incident/' + name))
        for name in LOCAL_ONLY:
            self.assertTrue(incident_excluded('analysis/incident/' + name), name)
        self.assertTrue(incident_excluded('analysis/incident/raw/report.html'))
        self.assertFalse(incident_excluded('analysis/incident'))
        self.assertFalse(incident_excluded('analysis/judge/report.html'))

    def test_tab_placed_before_inspect_and_idempotent(self):
        page = add_incident_tab(PAGE)
        self.assertIn('<span class="off">Behavior judge · not run</span><a data-tab="incident" '
                      'href="analysis/incident/report.html">Incident report</a><a href="inspect-view/', page)
        self.assertIn('<li data-tab="incident"><strong>Incident report</strong>', page)
        self.assertLess(page.index('<li data-tab="incident">'), page.index('<li><strong>Inspect</strong>'))
        self.assertEqual(add_incident_tab(page), page)
        self.assertEqual(page.replace(page[page.index('<a data-tab'):page.index('<a href="inspect-view/')], '')
                         .replace(page[page.index('<li data-tab'):page.index('<li><strong>Inspect')], ''), PAGE)
        with self.assertRaisesRegex(ValueError, 'tab bar'):
            add_incident_tab('<html><h1>old page</h1></html>')

    def test_catalogue_link_before_inspect_logs(self):
        links = {'Report': 'r/results.html', 'Behavior judge': 'r/analysis/judge/report.html',
                 'Inspect logs': 'r/inspect/a.eval'}
        self.assertEqual(list(with_incident_link(links, 'r')),
                         ['Report', 'Behavior judge', 'Incident report', 'Inspect logs'])
        self.assertEqual(with_incident_link({'Report': 'r/results.html'}, 'r'),
                         {'Report': 'r/results.html', 'Incident report': 'r/analysis/incident/report.html'})
        once = with_incident_link(links, 'r')
        self.assertEqual(with_incident_link(once, 'r'), once)


class LocalAndPreparedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        make_run(self.runs, RUN)
        (self.runs / RUN / 'results.html').write_text(PAGE)
        write_incident(self.runs / RUN)

    def test_local_index_links_only_runs_with_a_report(self):
        result = build_experiment_index.build(self.runs, unlisted=())
        self.assertEqual(result['warnings'], [])
        entries = {e['run']: e for e in json.loads((self.runs / 'experiments.json').read_text())['experiments']}
        self.assertEqual(entries[RUN]['links']['Incident report'], RUN + '/analysis/incident/report.html')
        self.assertNotIn('Incident report', entries[OTHER]['links'])
        self.assertIn(f'href="{RUN}/analysis/incident/report.html">Incident report</a>',
                      (self.runs / 'index.html').read_text())
        self.assertNotIn('data-tab="incident"', (self.runs / RUN / 'results.html').read_text())  # local page kept

    def test_prepare_publishes_only_the_three_files_with_tab_and_manifest_record(self):
        bundle = prepare(self.runs, self.root / 'bundle', RUN, OTHER, viewer=self.viewer)
        manifest, entries = load_bundle(bundle)
        incident = sorted(n for n in manifest['files'] if '/analysis/incident/' in n)
        self.assertEqual(incident, sorted(f'{RUN}/analysis/incident/{n}' for n in PUBLISHED))
        self.assertEqual(manifest['incident_report'], {RUN: {
            'included': [f'analysis/incident/{n}' for n in PUBLISHED], 'excluded': len(LOCAL_ONLY)}})
        self.assertIn('data-tab="incident"', (bundle / RUN / 'results.html').read_text())
        self.assertNotIn('data-tab="incident"', (bundle / OTHER / 'results.html').read_text())
        self.assertIn('id="site-theme"', (bundle / RUN / 'analysis/incident/report.html').read_text())
        links = {e['run']: e['links'] for e in entries}
        self.assertIn('Incident report', links[RUN])
        self.assertNotIn('Incident report', links[OTHER])

    def test_prepare_screens_credentials_and_local_links(self):
        (self.runs / RUN / 'analysis/incident/report.md').write_text('key sk-' + 'a' * 40 + '\n')
        with self.assertRaisesRegex(ValueError, 'Credential-like'):
            prepare(self.runs, self.root / 'b1', RUN, viewer=self.viewer)
        write_incident(self.runs / RUN, report='<html><body><a href="raw/opus/request-0001.json">r</a></body></html>')
        with self.assertRaisesRegex(ValueError, 'not published'):
            prepare(self.runs, self.root / 'b2', RUN, viewer=self.viewer)

    def test_prepare_refuses_a_run_page_without_tabs(self):
        (self.runs / RUN / 'results.html').write_text('<!doctype html><html><h1>R</h1></html>')
        with self.assertRaisesRegex(ValueError, 'tab bar'):
            prepare(self.runs, self.root / 'b3', RUN, viewer=self.viewer)

    def test_publish_refuses_unlisted_incident_files(self):
        bundle = prepare(self.runs, self.root / 'bundle', RUN, viewer=self.viewer)
        extra = bundle / RUN / 'analysis/incident/state.json'
        extra.write_text('{}')
        manifest = json.loads((bundle / 'archive-manifest.json').read_text())
        manifest['files'][f'{RUN}/analysis/incident/state.json'] = digest(b'{}')
        (bundle / 'archive-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            load_bundle(bundle)


class IncidentScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        make_run(self.runs, RUN)
        (self.runs / RUN / 'results.html').write_text(PAGE)
        self.hub = Hub(self.root)
        unlisted = patch.object(maintain_hf_run, 'unlisted_runs', return_value={})
        unlisted.start()
        self.addCleanup(unlisted.stop)
        with patch.object(build_experiment_index, 'unlisted_runs', return_value={}):
            publish(prepare(self.runs, self.root / 'published', RUN, OTHER, viewer=self.viewer),
                    self.hub, self.hub.download, commit=True, operation=operation)
            write_incident(self.runs / RUN)
            # A local results.html that drifted since publication must not be published by this scope.
            (self.runs / RUN / 'results.html').write_text(PAGE.replace('facts', 'regenerated facts'))
            self.bundle = prepare(self.runs, self.root / 'bundle', RUN, viewer=self.viewer)
        self.published, self.base, self.commits = dict(self.hub.files), self.hub.sha, len(self.hub.commits)
        self.incident = [f'{RUN}/analysis/incident/{n}' for n in sorted(PUBLISHED)]

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        records = self.root / 'records' / attempt
        if commit:
            kwargs.setdefault('backup_dir', records)
            kwargs.setdefault('receipt', records / 'receipt.json')
        return maintain_incident(self.bundle, self.hub, self.hub.download, run=RUN, commit=commit,
                                 operation=operation, **kwargs)

    def test_plan_is_read_only(self):
        result = self.maintain()
        self.assertEqual((result['committed'], self.hub.sha, len(self.hub.commits)), (False, self.base, self.commits))
        self.assertEqual(result['to_add'], self.incident)
        self.assertEqual(result['to_replace'], sorted([f'{RUN}/results.html', 'archive-manifest.json',
                                                       'experiments.json', 'index.html']))
        self.assertEqual(result['catalogue']['entry_diff'], {'links': {
            'added': {'Incident report': RUN + '/analysis/incident/report.html'}, 'removed': {}, 'changed': {}}})
        self.assertEqual(result['local_incident_exclusions'], len(LOCAL_ONLY))
        self.assertFalse((self.root / 'records').exists())

    def test_commit_changes_exactly_the_planned_files(self):
        result = self.maintain(commit=True)
        self.assertEqual((result['status'], len(self.hub.commits)), ('verified', self.commits + 1))
        final = self.hub.files
        self.assertEqual(set(final), set(self.published) | set(self.incident))
        changed = {n for n in self.published if final[n] != self.published[n]}
        self.assertEqual(changed, {f'{RUN}/results.html', 'index.html', 'experiments.json', 'archive-manifest.json'})
        self.assertFalse(any(('/raw/' in n or '/units/' in n or n.endswith(('state.json', 'digest.json',
                                                                            'run-budgets.json'))) for n in final))
        page = final[f'{RUN}/results.html'].decode()
        self.assertEqual(page, add_incident_tab(self.published[f'{RUN}/results.html'].decode()))
        self.assertNotIn('regenerated facts', page)
        for name in self.incident:
            self.assertEqual(final[name], (self.bundle / name).read_bytes())
        old, new = (json.loads(x['experiments.json'])['experiments'] for x in (self.published, final))
        self.assertEqual([e for e in new if e['run'] != RUN], [e for e in old if e['run'] != RUN])
        entry = next(e for e in new if e['run'] == RUN)
        self.assertEqual(entry, {**next(e for e in old if e['run'] == RUN), 'links': with_incident_link(
            next(e for e in old if e['run'] == RUN)['links'], RUN)})
        manifest = json.loads(final['archive-manifest.json'])
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        self.assertEqual(final['.gitattributes'], self.published['.gitattributes'])
        receipt = json.loads((self.root / 'records/attempt-1/receipt.json').read_text())
        self.assertEqual(receipt['status'], 'verified')
        self.assertTrue((self.root / 'records/attempt-1/original' / RUN / 'results.html').is_file())
        # Running it again finds nothing to do.
        again = self.maintain(commit=True, attempt='attempt-2')
        self.assertEqual((again['already_applied'], again['uploads'], len(self.hub.commits)),
                         (True, 0, self.commits + 1))

    def test_replaces_a_changed_report(self):
        self.maintain(commit=True)
        write_incident(self.runs / RUN, report='<!doctype html><html><head></head><body>Revised</body></html>')
        with patch.object(build_experiment_index, 'unlisted_runs', return_value={}):
            self.bundle = prepare(self.runs, self.root / 'bundle2', RUN, viewer=self.viewer)
        result = self.maintain(commit=True, attempt='attempt-2')
        self.assertEqual(result['to_replace'], ['archive-manifest.json', f'{RUN}/analysis/incident/report.html'])
        self.assertIn(b'Revised', self.hub.files[f'{RUN}/analysis/incident/report.html'])

    def test_refusals(self):
        self.hub.push({**self.published, f'{RUN}/analysis/incident/state.json': b'{}'})
        with self.assertRaisesRegex(ValueError, 'not in the bundle'):
            self.maintain()
        self.hub.push({**self.published, '.gitattributes': self.published['.gitattributes']
                       + f'{RUN}/analysis/incident/*'.encode() + RULE + b'\n'})
        with self.assertRaisesRegex(ValueError, 'LFS'):
            self.maintain()
        self.hub.push(self.published)
        self.hub.concurrent = True
        with self.assertRaisesRegex(ValueError, 'changed concurrently'):
            self.maintain(commit=True)
        self.hub.concurrent = False
        self.hub.push({k: v for k, v in self.published.items() if not k.startswith(RUN + '/')})
        with self.assertRaisesRegex(ValueError, 'not published'):
            self.maintain()

    def test_bundle_without_report_or_with_extra_files_is_refused(self):
        manifest = json.loads((self.bundle / 'archive-manifest.json').read_text())
        manifest['incident_report'][RUN]['included'] = ['analysis/incident/report.html']
        (self.bundle / 'archive-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'preparation record'):
            self.maintain()

    def test_cli_rejects_resume(self):
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            maintain_hf_run.main(['--maintain-run', RUN, '--scope', 'incident', '--bundle', str(self.bundle),
                                  '--resume-from', str(self.root)])


if __name__ == '__main__':
    unittest.main()
