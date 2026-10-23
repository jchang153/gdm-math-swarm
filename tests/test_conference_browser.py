"""Offline checks for the compressed, self-contained conference browser, the export without the embedded
activity timeline (owner, October 23, 2026) and maintain_hf_run.py --scope conference."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dataset_logs  # noqa: E402
import export_conference  # noqa: E402
from export_conference import compact_browser_page  # noqa: E402
import maintain_hf_run  # noqa: E402
from publish_hf_library import digest  # noqa: E402
from test_hf_maintenance import Hub, operation  # noqa: E402


class BrowserPageTests(unittest.TestCase):
    def document(self, records):
        encoded = json.dumps(records).replace('<', '\\u003c')
        return '<html><a href="file.html">File</a><script id="data" type="application/json">' + encoded + '</script><script>\nconst data=JSON.parse(document.getElementById(\'data\').textContent);\ndocument.title=data.run;</script></html>'

    def test_large_inline_records_roundtrip_without_changing_content_or_links(self):
        data = {'run': 'example', 'records': [{'text': '</script><script>bad()</script> 😀' * 1000}] * 400}
        original = self.document(data)
        self.assertGreater(len(original.encode()), 10_000_000)
        exported = compact_browser_page(original)
        self.assertLess(len(exported.encode()), 8_000_000)
        packed = re.search(r'type="application/gzip\+base64">([^<]+)</script>', exported)[1]
        self.assertEqual(json.loads(gzip.decompress(base64.b64decode(packed))), data)
        self.assertIn('href="file.html"', exported)
        self.assertNotIn('<script>bad()', exported)
        self.assertIn('DecompressionStream', exported)
        self.assertIn('Could not load conference:', exported)

    def test_unknown_renderer_refused(self):
        with self.assertRaisesRegex(ValueError, 'bootstrap'):
            compact_browser_page(self.document({}).replace('const data=', 'let data='))


RUN, OFFLINE_RUN = 'conf-run', 'conf-offline'
COMMIT = 'f' * 40


def make_conference_run(runs, run_id):
    """A saved run with every conference record kind, a captured file and a large activity timeline."""
    run = Path(runs) / run_id
    (run / 'conference').mkdir(parents=True)
    (run / 'analysis').mkdir()
    source, note = 'theorem t : True := trivial', '# notes\n'
    blob = note.encode()
    sha = hashlib.sha256(blob).hexdigest()
    (run / 'snapshots' / 'agent-001').mkdir(parents=True)
    (run / 'snapshots' / 'agent-001' / sha).write_bytes(blob)
    events = [{'seq': 1, 'kind': 'post', 'timestamp': 10.0, 'agent_id': 'agent-001', 'payload': {'post_id': 1}},
              {'seq': 2, 'kind': 'submission', 'timestamp': 20.0, 'agent_id': 'agent-002', 'payload': {'id': 1}}]
    (run / 'conference/events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    db = sqlite3.connect(run / 'conference/state.sqlite')
    db.executescript('''
CREATE TABLE posts(post_id, agent_id, intent_type, message, posted_at, tags);
CREATE TABLE messages(message_id, sender, recipient, body, sent_at);
CREATE TABLE feedback(feedback_id, agent_id, message, submitted_at);
CREATE TABLE submissions(submission_id, agent_id, task_id, source, source_sha256, outcome, reason, submitted_at);
CREATE TABLE library(artifact_id, path, content, sha256, published_at, author, task_id);
CREATE TABLE events(seq, kind, payload);''')
    db.execute('INSERT INTO posts VALUES (1, "agent-001", "building", "hello </script>", 10.0, "[]")')
    db.execute('INSERT INTO messages VALUES (1, "agent-001", "agent-002", "dm", 11.0)')
    db.execute('INSERT INTO feedback VALUES (1, "agent-002", "bug", 12.0)')
    db.execute('INSERT INTO submissions VALUES (1, "agent-002", "T1", ?, ?, "accepted", "ok", 20.0)',
               (source, hashlib.sha256(source.encode()).hexdigest()))
    db.execute('INSERT INTO library VALUES (1, "T1.lean", ?, ?, 21.0, "agent-002", "T1")',
               (source, hashlib.sha256(source.encode()).hexdigest()))
    for e in events:
        db.execute('INSERT INTO events VALUES (?, ?, ?)', (e['seq'], e['kind'], json.dumps(e['payload'])))
    db.commit()
    db.close()
    (run / 'runtime.jsonl').write_text(json.dumps({
        'kind': 'file_snapshot', 'agent_id': 'agent-001', 'timestamp': 15.0,
        'payload': {'path': '/workspace/RESEARCH.md', 'blob_sha256': sha, 'sha256': sha, 'bytes': len(blob),
                    'mode': 'full'}}) + '\n')
    timeline = [{'order': i, 'source': 'inspect', 'kind': 'tool_call', 'agent_id': 'agent-001', 'timestamp': 1.0 + i,
                 'summary': 'x' * 200, 'ref': {}, 'payload': {}} for i in range(500)]
    (run / 'analysis/timeline.jsonl').write_text(''.join(json.dumps(t) + '\n' for t in timeline))
    return run


def with_timeline(document, manifest, entries=3):
    """The page and manifest an exporter before October 23, 2026 wrote: the same records plus embedded
    activity records, an "activity" count and the timeline as a manifest source."""
    data = export_conference.published_data(document)
    data.pop('timeline_location')
    data['records'] += [{'category': 'activity', 'id': f'activity-{i}', 'title': 'tool_call', 'text': 'step',
                         'timestamp': 1.0 + i, 'agents': ['agent-001'], 'task': '', 'detail': {'source': 'inspect'},
                         'links': []} for i in range(entries)]
    data['counts']['activity'] = entries
    data['timeline_entries'] = entries
    packed = base64.b64encode(gzip.compress(json.dumps(data).encode(), mtime=0)).decode()
    old = re.sub(r'(<script id="data" type="application/gzip\+base64">).*?(</script>)',
                 lambda m: m[1] + packed + m[2], document, count=1, flags=re.S)
    old = old.replace(export_conference.TIMELINE_NOTE + '\n', '').replace(
        export_conference.TIMELINE_NOTE_OFFLINE + '\n', '')
    old_manifest = {k: v for k, v in manifest.items() if k != 'timeline_location'}
    old_manifest.update(counts=data['counts'], timeline_entries=entries,
                        sources={**manifest['sources'], 'analysis/timeline.jsonl': 'a' * 64})
    return old, old_manifest


class ExportWithoutTimelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.run = make_conference_run(self.root / 'runs', RUN)
        root = patch.object(export_conference, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)

    def test_export_keeps_every_section_but_not_the_timeline(self):
        manifest = export_conference.export(self.run)
        page = (self.run / 'conference-view/index.html').read_text()
        data = export_conference.published_data(page)
        self.assertEqual(manifest['counts'], {'board': 1, 'dms': 1, 'library': 1, 'submissions': 1, 'feedback': 1,
                                              'files': 1})
        self.assertEqual((manifest['timeline_entries'], manifest['timeline_location']), (0, 'dataset'))
        self.assertNotIn('analysis/timeline.jsonl', manifest['sources'])
        self.assertEqual(sorted({r['category'] for r in data['records']}),
                         ['board', 'dms', 'feedback', 'files', 'library', 'submissions'])
        self.assertEqual((data['timeline_entries'], data['timeline_location']), (0, 'dataset'))
        self.assertIn(export_conference.TIMELINE_NOTE, page)
        self.assertIn('href="../analysis/timeline.jsonl"', page)
        renderer = ''.join(re.findall(r'<script>(.*?)</script>', page, re.S))
        for gone in ('All activity', "el('kind')", "activity:["):
            self.assertNotIn(gone, renderer)
        self.assertNotIn('<script>bad', page)
        # Empty-section and capture-limit notes stay explicit.
        self.assertIn('No direct messages were saved in this run. Inbox checks appear only in the full activity '
                      'timeline', page)
        self.assertIn('These are observations, not a complete edit history.', page)
        captured = [p for p in (self.run / 'conference-view').glob('file-*.html')]
        self.assertEqual(len(captured), 1)
        self.assertIn('# notes', captured[0].read_text())
        self.assertLess(len(page.encode()), 100_000)  # the 500-entry timeline is not embedded

    def test_published_view_with_timeline_strips_to_the_fresh_export(self):
        manifest = export_conference.export(self.run)
        page = (self.run / 'conference-view/index.html').read_text()
        old, old_manifest = with_timeline(page, manifest)
        self.assertEqual(len([r for r in export_conference.published_data(old)['records']
                              if r['category'] == 'activity']), 3)
        data, new_manifest = export_conference.strip_timeline(old, old_manifest)
        self.assertEqual(export_conference.render_index(data), page)
        self.assertEqual(new_manifest, manifest)
        # Idempotent on a page without the timeline.
        again, again_manifest = export_conference.strip_timeline(page, manifest)
        self.assertEqual((export_conference.render_index(again), again_manifest), (page, manifest))
        offline, offline_manifest = export_conference.strip_timeline(old, old_manifest, 'offline')
        self.assertEqual(offline_manifest['timeline_location'], 'offline')
        self.assertIn(export_conference.TIMELINE_NOTE_OFFLINE, export_conference.render_index(offline))

    def test_strip_refusals(self):
        manifest = export_conference.export(self.run)
        old, old_manifest = with_timeline((self.run / 'conference-view/index.html').read_text(), manifest)
        with self.assertRaisesRegex(ValueError, 'differ from the page'):
            export_conference.strip_timeline(old, {**old_manifest, 'timeline_entries': 2})
        data = export_conference.published_data(old)
        data['timeline_entries'] = 2
        plain = '<script id="data" type="application/json">' + json.dumps(data).replace('<', '\\u003c') + '</script>'
        with self.assertRaisesRegex(ValueError, 'recorded count'):
            export_conference.strip_timeline(plain, old_manifest)
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            export_conference.strip_timeline(old, {**old_manifest, 'run': 'other'})
        with self.assertRaisesRegex(ValueError, 'location'):
            export_conference.strip_timeline(old, old_manifest, 'space')
        data, _ = export_conference.strip_timeline(old, old_manifest)
        with self.assertRaisesRegex(ValueError, 'no longer embeds'):
            export_conference.render_index({**data, 'timeline_entries': 3})


class ConferenceScopeTests(unittest.TestCase):
    """--scope conference against an in-memory private Space holding pages with the embedded timeline."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        root = patch.object(export_conference, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        self.hub = Hub(self.root)
        files = {'index.html': b'<html>catalogue</html>', 'experiments.json': b'{}'}
        archive = {'schema': 'gdm-private-space/1', 'runs': [RUN, OFFLINE_RUN], 'files': {},
                   'dataset_repo': dataset_logs.DATASET_REPO, 'offline_files': {}, 'dataset_files': {}}
        for run_id in (RUN, OFFLINE_RUN):
            run = make_conference_run(self.root / 'runs', run_id)
            manifest = export_conference.export(run)
            hosted = maintain_hf_run.hosted_conference_page(
                (run / 'conference-view/index.html').read_text(), run_id,
                {'dataset_files': {f'{run_id}/conference/events.jsonl': {
                    'repo': dataset_logs.DATASET_REPO, 'commit': COMMIT}}})
            page, old_manifest = with_timeline(hosted, manifest, entries=400)
            files.update({f'{run_id}/conference-view/index.html': page.encode(),
                          f'{run_id}/conference-view/manifest.json': json.dumps(old_manifest, indent=2).encode(),
                          f'{run_id}/results.html': b'<html>report</html>',
                          f'{run_id}/inspect-view/index.html': b'<html>inspect</html>',
                          f'{run_id}/conference-view/' + next(run.glob('conference-view/file-*.html')).name:
                              next(run.glob('conference-view/file-*.html')).read_bytes()})
        for run_id, key in ((RUN, 'dataset_files'), (OFFLINE_RUN, 'dataset_files')):
            archive[key][f'{run_id}/conference/events.jsonl'] = {
                'repo': dataset_logs.DATASET_REPO, 'path': f'{run_id}/conference/events.jsonl', 'commit': COMMIT,
                'bytes': 1, 'sha256': '0' * 64, 'category': 'jsonl', 'moved': '2026-10-23'}
        archive['dataset_files'][f'{RUN}/analysis/timeline.jsonl'] = {
            'repo': dataset_logs.DATASET_REPO, 'path': f'{RUN}/analysis/timeline.jsonl', 'commit': COMMIT,
            'bytes': 1, 'sha256': '0' * 64, 'category': 'jsonl', 'moved': '2026-10-23'}
        archive['offline_files'][f'{OFFLINE_RUN}/analysis/timeline.jsonl'] = {
            'bytes': 1, 'sha256': '0' * 64, 'removed': '2026-10-22', 'description': 'raw JSONL record'}
        archive['files'] = {n: digest(d) for n, d in files.items()}
        archive['total_bytes'] = sum(x['bytes'] for x in archive['files'].values())
        files['archive-manifest.json'] = json.dumps(archive, indent=2).encode()
        self.hub.push({**self.hub.files, **files})
        self.published, self.base = dict(self.hub.files), self.hub.sha

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        records = self.root / 'records' / attempt
        if commit:
            kwargs.setdefault('backup_dir', records)
            kwargs.setdefault('receipt', records / 'receipt.json')
        return maintain_hf_run.maintain_conference(self.hub, self.hub.download, commit=commit, operation=operation,
                                                   **kwargs)

    def test_plan_is_read_only_and_reports_the_saving(self):
        result = self.maintain()
        self.assertEqual((result['committed'], self.hub.sha), (False, self.base))
        self.assertEqual(result['runs'], [RUN, OFFLINE_RUN])
        self.assertEqual(result['per_run'][RUN]['timeline_location'], 'dataset')
        self.assertEqual(result['per_run'][OFFLINE_RUN]['timeline_location'], 'offline')
        self.assertEqual(result['per_run'][RUN]['timeline_entries_removed'], 400)
        self.assertGreater(result['conference_page_bytes']['saved'], 0)
        self.assertEqual(result['to_replace'], sorted(['archive-manifest.json'] + [
            f'{r}/conference-view/{n}' for r in (RUN, OFFLINE_RUN) for n in ('index.html', 'manifest.json')]))
        self.assertFalse((self.root / 'records').exists())

    def test_commit_replaces_only_the_views_and_is_idempotent(self):
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        changed = {n for n in self.published if final[n] != self.published[n]}
        self.assertEqual(changed, set(result['to_replace']))
        self.assertEqual(set(final), set(self.published))
        for run_id in (RUN, OFFLINE_RUN):
            page = final[f'{run_id}/conference-view/index.html'].decode()
            data = export_conference.published_data(page)
            old = export_conference.published_data(self.published[f'{run_id}/conference-view/index.html'].decode())
            self.assertEqual(data['records'], [r for r in old['records'] if r['category'] != 'activity'])
            self.assertNotIn('activity', data['counts'])
            manifest = json.loads(final[f'{run_id}/conference-view/manifest.json'])
            self.assertEqual(manifest['timeline_entries'], 0)
        page = final[f'{RUN}/conference-view/index.html'].decode()
        self.assertIn('href="' + dataset_logs.file_url(f'{RUN}/analysis/timeline.jsonl', COMMIT) + '"', page)
        self.assertIn(export_conference.TIMELINE_NOTE.split('(')[0], page)
        offline = final[f'{OFFLINE_RUN}/conference-view/index.html'].decode()
        self.assertIn('href="../results.html#raw-records-offline" data-offline-file="analysis/timeline.jsonl"', offline)
        self.assertIn('kept offline by the researchers', offline)
        archive = json.loads(final['archive-manifest.json'])
        for name, info in archive['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        self.assertTrue((self.root / 'records/attempt-1/original' / RUN / 'conference-view/index.html').is_file())
        again = self.maintain(commit=True, attempt='attempt-2')
        self.assertEqual((again['already_applied'], again['uploads']), (True, 0))
        self.assertTrue(all(r['unchanged'] for r in again['per_run'].values()))

    def test_refusals(self):
        archive = json.loads(self.published['archive-manifest.json'])
        del archive['offline_files'][f'{OFFLINE_RUN}/analysis/timeline.jsonl']
        self.hub.push({**self.published, 'archive-manifest.json': json.dumps(archive).encode()})
        with self.assertRaisesRegex(ValueError, 'neither in the logs dataset nor recorded offline'):
            self.maintain()
        with self.assertRaisesRegex(ValueError, 'not published'):
            self.maintain(runs=['never-published'])
        name = f'{RUN}/conference-view/index.html'
        stripped = re.sub(r'<script id="private-space-navigation">.*?</script>',
                          '<script id="private-space-navigation">older()</script>', self.published[name].decode(),
                          flags=re.S)
        self.hub.push({**self.published, name: stripped.encode()})
        with self.assertRaisesRegex(ValueError, 'page head'):
            self.maintain(runs=[RUN])
        self.hub.push(self.published)
        self.hub.private = False
        with self.assertRaisesRegex(ValueError, 'private Space'):
            self.maintain()

    def test_cli_takes_no_bundle(self):
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            maintain_hf_run.main(['--all-published-runs', '--scope', 'conference', '--bundle', str(self.root)])


if __name__ == '__main__':
    unittest.main()
