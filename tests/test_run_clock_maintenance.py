"""Offline tests of the run clock on the hosted site (owner, October 25, 2026): the conference view's run start
and captured-file pages (export_conference, maintain_hf_run.py --scope conference [--capture-pages]), --scope site
without a bundle (restamp of published run and judge pages), --scope incident without a bundle (files read from
--runs-dir) and verify_hosted --expect-run-clock. In-memory Space fakes only."""
import base64
import gzip
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_experiment_index  # noqa: E402
import dataset_logs  # noqa: E402
import export_conference  # noqa: E402
import maintain_hf_run  # noqa: E402
from publish_hf_library import digest  # noqa: E402
from research.analysis.judge import review  # noqa: E402
import run_clock  # noqa: E402
from test_conference_browser import COMMIT, make_conference_run, with_timeline  # noqa: E402
from test_hf_maintenance import Hub, operation  # noqa: E402
import test_incident_site  # noqa: E402
from test_simple_judge_site import write_simple_judge  # noqa: E402

RUN = 'clock-run'
START = '1970-01-01T00:00:05+00:00'  # the fixture records are at epoch seconds 10 to 21
OLD_CAPTURE = ('<html><body><h1>agent-001 · /workspace/RESEARCH.md</h1><article id="version-1"><h2>Capture 1</h2>'
               '<p>Oct 22, 2026 12:44:41 PM PDT · full capture · 8 recorded bytes</p></article><article id="version-2">'
               '<h2>Capture 2</h2><p>Nov 02, 2026 01:00:00 AM PST · full capture · 9 recorded bytes</p></article>'
               '</body></html>')


class CapturePageTests(unittest.TestCase):
    def test_restamp_uses_the_zone_offset_and_is_idempotent(self):
        start = run_clock.epoch('2026-10-22T18:43:07.957+00:00')
        page = export_conference.restamp_capture_page(OLD_CAPTURE, start)
        self.assertIn('<h2>Capture 1</h2><p>+1:01:34 (19:44:41 UTC) · full capture', page)
        self.assertIn('<h2>Capture 2</h2><p>+254:16:53 (09:00:00 UTC) · full capture', page)
        self.assertEqual(export_conference.restamp_capture_page(page, start), page)
        self.assertNotIn('PDT', page)


class ExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.run = make_conference_run(self.root / 'runs', RUN)
        (self.run / 'results.json').write_text(json.dumps({'start_utc': START}))
        root = patch.object(export_conference, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)

    def test_export_embeds_the_run_start_and_writes_clock_captures(self):
        export_conference.export(self.run)
        page = (self.run / 'conference-view/index.html').read_text()
        data = export_conference.published_data(page)
        self.assertEqual(data['run_start'], 5.0)
        self.assertIn('function runClock(t,s)', page)
        self.assertIn('runClock(value,data.run_start)', page)
        self.assertNotIn('America/Los_Angeles', page)
        self.assertEqual(list((self.run / 'conference-view').glob('file-*.html')), [])
        viewer = (self.run / 'conference-view/files.html').read_text()
        self.assertEqual(export_conference.files_page_index(viewer)['run_start'], 5.0)
        self.assertIn("runClock(v.timestamp,meta.run_start)+' · '+v.mode+' capture · '", viewer)
        # strip_timeline keeps the run start; render_index reproduces the page.
        manifest = json.loads((self.run / 'conference-view/manifest.json').read_text())
        old, old_manifest = with_timeline(page, manifest)
        again, _ = export_conference.strip_timeline(old, old_manifest)
        self.assertEqual(export_conference.render_index(again), page)
        with self.assertRaises(ValueError):
            export_conference.render_index({**data, 'run_start': 'soon'})
        without = {k: v for k, v in data.items() if k != 'run_start'}
        self.assertNotIn('"run_start"', gzip.decompress(base64.b64decode(re.search(
            r'gzip\+base64">([^<]+)<', export_conference.render_index(without))[1])).decode())


class ConferenceScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        root = patch.object(export_conference, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        self.hub = Hub(self.root)
        run = make_conference_run(self.root / 'runs', RUN)
        manifest = export_conference.export(run)  # no results.json yet: an export without the run start
        page = maintain_hf_run.hosted_conference_page((run / 'conference-view/index.html').read_text(), RUN, {})
        self.capture = f'{RUN}/conference-view/file-{"0" * 20}.html'
        files = {'index.html': b'<html>catalogue</html>', 'experiments.json': b'{}',
                 f'{RUN}/conference-view/index.html': page.encode(),
                 f'{RUN}/conference-view/manifest.json': json.dumps(manifest, indent=2).encode(),
                 f'{RUN}/results.html': b'<html>report</html>', f'{RUN}/inspect-view/index.html': b'<html>i</html>',
                 f'{RUN}/results.json': json.dumps({'start_utc': '2026-10-22T18:43:07.957+00:00'}).encode(),
                 f'{RUN}/conference/events.jsonl': b'{}\n', self.capture: OLD_CAPTURE.encode()}
        for name in re.findall(r'href="(file-[0-9a-f]{20}\.html)"', page):
            files[f'{RUN}/conference-view/{name}'] = b'<html>x</html>'
        archive = {'schema': 'gdm-private-space/1', 'runs': [RUN], 'files': {}, 'offline_files': {},
                   'dataset_files': {f'{RUN}/analysis/timeline.jsonl': {
                       'repo': dataset_logs.DATASET_REPO, 'path': f'{RUN}/analysis/timeline.jsonl', 'commit': COMMIT,
                       'bytes': 1, 'sha256': '0' * 64, 'category': 'jsonl', 'moved': '2026-10-23'}}}
        archive['files'] = {n: digest(d) for n, d in files.items()}
        archive['total_bytes'] = sum(x['bytes'] for x in archive['files'].values())
        files['archive-manifest.json'] = json.dumps(archive, indent=2).encode()
        self.hub.push({**self.hub.files, **files})
        self.published = dict(self.hub.files)

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        records = self.root / 'records' / attempt
        if commit:
            kwargs.update(backup_dir=records, receipt=records / 'receipt.json')
        return maintain_hf_run.maintain_conference(self.hub, self.hub.download, commit=commit, operation=operation,
                                                   **kwargs)

    def test_run_start_from_published_results(self):
        plan = self.maintain()
        self.assertEqual(plan['per_run'][RUN]['run_start'], run_clock.epoch('2026-10-22T18:43:07.957+00:00'))
        self.assertEqual(plan['capture_pages'], 'not selected (--capture-pages)')
        self.assertNotIn(self.capture, plan['to_replace'])
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        data = export_conference.published_data(self.hub.files[f'{RUN}/conference-view/index.html'].decode())
        old = export_conference.published_data(self.published[f'{RUN}/conference-view/index.html'].decode())
        self.assertEqual(data['records'], old['records'])
        self.assertEqual(data['run_start'], plan['per_run'][RUN]['run_start'])
        self.assertEqual(self.hub.files[self.capture], self.published[self.capture])

    def test_capture_pages_restamped_in_one_commit_per_run_and_idempotent(self):
        commits = len(self.hub.commits)
        plan = self.maintain(capture_pages=True)
        self.assertEqual(plan['capture_pages']['to_replace'], 1)
        self.assertEqual(plan['per_run'][RUN]['capture_pages']['restamped'], 1)
        result = self.maintain(commit=True, capture_pages=True)
        self.assertEqual((result['status'], len(self.hub.commits)), ('verified', commits + 1))
        self.assertIn(b'+1:01:34 (19:44:41 UTC)', self.hub.files[self.capture])
        archive = json.loads(self.hub.files['archive-manifest.json'])
        for name, info in archive['files'].items():
            self.assertEqual(digest(self.hub.files[name]), info, name)
        again = self.maintain(commit=True, capture_pages=True, attempt='attempt-2')
        self.assertEqual((again['already_applied'], again['uploads']), (True, 0))

    def test_pages_dir_is_used_only_where_byte_identical(self):
        pages = self.root / 'pages'
        (pages / self.capture).parent.mkdir(parents=True)
        (pages / self.capture).write_bytes(self.hub.files[self.capture])  # identical: used, not downloaded
        stale = f'{RUN}/conference-view/index.html'
        (pages / stale).write_bytes(b'<html>not the published page</html>')  # differs: downloaded instead
        downloads = []
        original = self.hub.download

        def counting(repo, name, **kwargs):
            downloads.append(name)
            return original(repo, name, **kwargs)
        self.hub.download = counting
        expected = self.maintain(capture_pages=True)
        downloads.clear()
        plan = maintain_hf_run.maintain_conference(self.hub, counting, capture_pages=True, pages_dir=pages)
        self.assertNotIn(self.capture, downloads)
        self.assertIn(stale, downloads)
        self.assertEqual(plan['published_files_read']['from_pages_dir'], 1)
        self.assertEqual(plan['capture_pages'], expected['capture_pages'])
        self.assertEqual(plan['to_replace'], expected['to_replace'])


def judge_review_page(time):
    return (f'<html><body><div class="item-head">reasoning · agent-001 · turn 21 · {time} · authored by agent</div>'
            '<pre>{"accepted_at": "2026-10-22T06:21:45.857Z"}</pre></body></html>')


class SitePublishedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.hub = Hub(self.root)
        sj = write_simple_judge(self.root / 'runs' / RUN)
        page = test_incident_site.PAGE.replace(
            '<main id="report">facts</main>', '<p><code>clock-run</code> · 2026-10-22T05:05:36+00:00 to '
            '2026-10-22T05:35:36+00:00</p><main id="report"><table><tr><th>Time (UTC)</th></tr><tr><td>'
            '2026-10-22T05:17:40.120+00:00</td></tr></table></main>')
        agent_page = judge_review_page('2026-10-22T06:11:42.345289Z')
        core = {'schema': review.MANIFEST_SCHEMA, 'pages': {'agents/agent-001.html': review.sha256_bytes(
            agent_page.encode())}}
        core['manifest_sha256'] = review.canonical_sha256(core)
        files = {'index.html': b'<html>catalogue</html>', 'experiments.json': b'{}',
                 f'{RUN}/results.html': page.encode(),
                 f'{RUN}/results.json': json.dumps({'start_utc': '2026-10-22T05:05:36+00:00'}).encode(),
                 f'{RUN}/analysis/judge/report.html': b'<html>no times</html>',
                 f'{RUN}/analysis/judge/report.json': b'{}',
                 f'{RUN}/analysis/judge/review/agents/agent-001.html': agent_page.encode(),
                 f'{RUN}/analysis/judge/review/manifest.json': json.dumps(core).encode(),
                 f'{RUN}/analysis/simple_judge/report.json': (sj / 'report.json').read_bytes()}
        archive = {'schema': 'gdm-private-space/1', 'runs': [RUN], 'files': {n: digest(d) for n, d in files.items()}}
        archive['total_bytes'] = sum(x['bytes'] for x in archive['files'].values())
        files['archive-manifest.json'] = json.dumps(archive, indent=2).encode()
        self.hub.push({**self.hub.files, **files})
        self.published = dict(self.hub.files)

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        records = self.root / 'records' / attempt
        if commit:
            kwargs.update(backup_dir=records, receipt=records / 'receipt.json')
        return maintain_hf_run.maintain_site_published(self.hub, self.hub.download, commit=commit,
                                                       operation=operation, **kwargs)

    def test_plan_and_commit_restamp_only_pages(self):
        plan = self.maintain()
        self.assertEqual(plan['to_replace'], sorted([
            'archive-manifest.json', f'{RUN}/results.html', f'{RUN}/analysis/judge/review/agents/agent-001.html',
            f'{RUN}/analysis/judge/review/manifest.json']))
        self.assertEqual(plan['per_run'][RUN]['judge_pages'], {'published': 2, 'restamped': 1})
        self.assertNotIn(f'{RUN}/analysis/judge/report.html', plan['to_replace'])
        self.assertTrue(plan['per_run'][RUN]['simple_judge_on_run_page'])
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        results = final[f'{RUN}/results.html'].decode()
        self.assertIn('+12:04 (05:17:40 UTC)', results)
        self.assertIn('data-tab="simple-judge"', results)
        agent = final[f'{RUN}/analysis/judge/review/agents/agent-001.html'].decode()
        self.assertIn('+1:06:06 (06:11:42 UTC)', agent)
        self.assertIn('"accepted_at": "2026-10-22T06:21:45.857Z"', agent)  # JSON detail keeps its value
        manifest = json.loads(final[f'{RUN}/analysis/judge/review/manifest.json'])
        self.assertEqual(manifest['pages']['agents/agent-001.html'], review.sha256_bytes(agent.encode()))
        for name in ('index.html', 'experiments.json', f'{RUN}/results.json', f'{RUN}/analysis/judge/report.html'):
            self.assertEqual(final[name], self.published[name])
        archive = json.loads(final['archive-manifest.json'])
        for name, info in archive['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        again = self.maintain(commit=True, attempt='attempt-2')
        self.assertEqual((again['already_applied'], again['uploads']), (True, 0))

    def test_refusals_and_cli(self):
        with self.assertRaisesRegex(ValueError, 'not published'):
            self.maintain(runs=['never-published'])
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            maintain_hf_run.main(['--maintain-run', RUN, '--scope', 'site', '--resume-from', str(self.root)])


class IncidentWithoutBundleTests(test_incident_site.IncidentScopeTests):
    """The incident scope's tests, with the three files read from --runs-dir instead of a bundle."""

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        records = self.root / 'records' / attempt
        if commit:
            kwargs.setdefault('backup_dir', records)
            kwargs.setdefault('receipt', records / 'receipt.json')
        return maintain_hf_run.maintain_incident(None, self.hub, self.hub.download, run=test_incident_site.RUN,
                                                 commit=commit, operation=operation, runs_dir=self.runs, **kwargs)

    def test_plan_is_read_only(self):
        result = self.maintain()
        self.assertEqual(result['to_add'], self.incident)
        self.assertIsNone(result['local_incident_exclusions'])

    def test_bundle_without_report_or_with_extra_files_is_refused(self):
        self.skipTest('no bundle in this mode')

    def test_commit_changes_exactly_the_planned_files(self):
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        self.assertEqual(set(final), set(self.published) | set(self.incident))
        for name in self.incident:  # the same bytes a bundle publishes (theme, Space navigation)
            self.assertEqual(final[name], (self.bundle / name).read_bytes(), name)
        again = self.maintain(commit=True, attempt='attempt-2')
        self.assertEqual((again['already_applied'], again['uploads']), (True, 0))

    def test_replaces_a_changed_report(self):
        self.maintain(commit=True)
        test_incident_site.write_incident(self.runs / test_incident_site.RUN,
                                          report='<!doctype html><html><head></head><body>Revised</body></html>')
        result = self.maintain(commit=True, attempt='attempt-2')
        self.assertEqual(result['to_replace'], ['archive-manifest.json',
                                                f'{test_incident_site.RUN}/analysis/incident/report.html'])

    def test_rerender_leaves_a_stale_catalogue_alone_when_the_entry_has_the_link(self):
        self.maintain(commit=True)
        # The catalogue page no longer matches the current generator (for example after a catalogue page change
        # that is re-rendered separately); the entry already links the report, so the catalogue is not touched.
        self.hub.files['index.html'] = self.hub.files['index.html'].replace(b'</body>', b'<!-- stale --></body>')
        test_incident_site.write_incident(self.runs / test_incident_site.RUN,
                                          report='<!doctype html><html><head></head><body>Re-laid out</body></html>')
        result = self.maintain(commit=True, attempt='attempt-2')
        self.assertFalse(result['catalogue']['regenerated'])
        self.assertEqual(result['to_replace'], ['archive-manifest.json',
                                                f'{test_incident_site.RUN}/analysis/incident/report.html'])
        self.assertIn(b'<!-- stale -->', self.hub.files['index.html'])

    def test_refuses_local_only_links_and_missing_report(self):
        test_incident_site.write_incident(self.runs / test_incident_site.RUN,
                                          report='<html><a href="raw/opus/request-0001.json">x</a></html>')
        with self.assertRaisesRegex(ValueError, 'never published'):
            self.maintain()
        (self.runs / test_incident_site.RUN / 'analysis/incident/report.html').unlink()
        with self.assertRaisesRegex(ValueError, 'No local incident report'):
            self.maintain()


class VerifyRunClockTests(unittest.TestCase):
    def test_expect_run_clock_checks(self):
        import verify_hosted
        from test_research_tools import FakeApi, FakeSession
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for page in verify_hosted.PAGES:
                path = root / page.format(run='demo-run')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'<!doctype html><title>x</title>')
            run = root / 'demo-run'
            (run / 'analysis/incident').mkdir(parents=True)
            (run / 'analysis/incident/report.html').write_text(
                '<!doctype html><h2 id="where-to-look">Where to look</h2><span>2026-10-22 05:17:40 UTC</span>')

            def failed(**kwargs):
                record = verify_hosted.verify(FakeApi(root), FakeSession(root), {}, 'org/space',
                                              'demo-run', 'abc', **kwargs)
                return {c['check'] for c in record['checks'] if not c['ok']}
            (run / 'results.html').write_text('<!doctype html><td>2026-10-22T05:17:40.123+00:00</td>')
            self.assertEqual(failed(), set())  # the incident page is checked like the others
            self.assertEqual(failed(expect_run_clock=True), {'results_run_clock', 'conference_run_start',
                                                             'incident_sections_removed', 'incident_run_clock'})
            (run / 'analysis/incident/report.html').write_text('<!doctype html><span>+12:04 (05:17:40 UTC)</span>')
            (run / 'results.html').write_text('<!doctype html><td>+12:04 (05:17:40 UTC)</td>')
            data = {'run': 'demo-run', 'records': [], 'run_start': 5.0}
            (run / 'conference-view/index.html').write_text(
                '<!doctype html><script id="data" type="application/json">' + json.dumps(data) + '</script>')
            self.assertEqual(failed(expect_run_clock=True), set())
            (run / 'results.html').write_text('<!doctype html><p>No event times on this page.</p>')
            self.assertEqual(failed(expect_run_clock=True), set())  # nothing to convert (baseline100's report)


if __name__ == '__main__':
    unittest.main()
