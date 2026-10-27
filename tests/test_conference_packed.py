"""Packed captured files of the conference view (October 26, 2026: HF's 20,000-file repository limit):
export_conference.py packing and viewer, prepare_hf_library.py repacking, maintain_hf_run.py --scope conference
--pack-files (deletions and guards), the credential screen of packs, publication leftovers and verify_hosted's
packed checks."""
import base64
import gzip
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dataset_logs  # noqa: E402
import export_conference  # noqa: E402
import maintain_hf_run  # noqa: E402
import publish_hf_library  # noqa: E402
from publish_hf_library import digest  # noqa: E402
import run_clock  # noqa: E402
from test_conference_browser import COMMIT, make_conference_run, with_timeline  # noqa: E402
from test_hf_maintenance import Hub, operation  # noqa: E402

RUN = 'pack-run'
START = '1970-01-01T00:00:05Z'


def delete(**kwargs):
    return SimpleNamespace(delete=True, **kwargs)


def add_captures(run, files=3, versions=2):
    """More captured files and versions (agent-002), appended to the run's runtime log and snapshots."""
    lines = []
    for f in range(files):
        for v in range(versions):
            blob = f'file {f} version {v} <tag> & "quotes"\n'.encode()
            sha = hashlib.sha256(blob).hexdigest()
            (run / 'snapshots/agent-002').mkdir(parents=True, exist_ok=True)
            (run / 'snapshots/agent-002' / sha).write_bytes(blob)
            lines.append(json.dumps({'kind': 'file_snapshot', 'agent_id': 'agent-002', 'timestamp': 30.0 + 10 * v + f,
                                     'payload': {'path': f'/workspace/notes/n{f}.md', 'blob_sha256': sha,
                                                 'sha256': sha, 'bytes': len(blob), 'mode': 'full'}}))
    with (run / 'runtime.jsonl').open('a') as handle:
        handle.write(''.join(line + '\n' for line in lines))


def legacy_capture_page(agent, path, rows, start):
    """A captured-file page as export_conference.py wrote them before October 26, 2026."""
    sections = ''.join(
        f'<article id="version-{i}"><h2>Capture {i}</h2><p>{run_clock.clock(r["timestamp"], start)} · '
        f'{html.escape(str(r["mode"]))} capture · {r["bytes"]} recorded bytes</p>'
        f'<p>Runtime line {r["runtime_line"]} · blob SHA-256 <code>{r["blob_sha256"]}</code></p>'
        '<pre>' + html.escape(r['content']) + '</pre></article>' for i, r in enumerate(rows, 1))
    return export_conference.page(agent + ' · ' + path, '<p><a href="index.html#files">← Captured files</a></p><h1>'
                                  + html.escape(agent + ' · ' + path) + '</h1>' + sections)


class PackingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.run = make_conference_run(self.root / 'runs', RUN)
        add_captures(self.run)
        (self.run / 'results.json').write_text(json.dumps({'start_utc': START}))
        root = patch.object(export_conference, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)

    def test_packs_roundtrip_deterministic_and_sharded(self):
        versions = export_conference.captured_versions(self.run)
        self.assertEqual(len(versions), 4)
        packs, index = export_conference.pack_captures(RUN, versions)
        self.assertEqual(list(packs), ['pack-01.json'])
        self.assertEqual(export_conference.pack_captures(RUN, versions), (packs, index))  # same bytes again
        entries = export_conference.read_pack(packs['pack-01.json'])
        self.assertEqual(set(entries), set(index['files']))
        for (agent, path), rows in versions.items():
            entry = entries[export_conference.file_identity(agent, path)]
            self.assertEqual((entry['agent'], entry['path']), (agent, path))
            self.assertEqual([v['content'] for v in entry['versions']], [r['content'] for r in rows])
        # A tiny target splits the files over several packs, each file whole in one pack, at most max_packs.
        small, small_index = export_conference.pack_captures(RUN, versions, target=1, max_packs=10)
        self.assertEqual(len(small), 4)
        merged = {}
        for name, data in small.items():
            part = export_conference.read_pack(data)
            self.assertEqual(set(part), {i for i, v in small_index['files'].items() if v[0] == name})
            merged.update(part)
        self.assertEqual(merged, entries)
        capped, _ = export_conference.pack_captures(RUN, versions, target=1, max_packs=2)
        self.assertLessEqual(len(capped), 2)
        with self.assertRaisesRegex(ValueError, 'above'):
            export_conference.pack_captures(RUN, versions, target=1, limit=10, max_packs=1)
        # Packs are text (JSON), never gzip bytes on disk, and a tampered pack is refused.
        document = json.loads(packs['pack-01.json'])
        self.assertEqual((document['schema'], document['encoding']), (export_conference.PACK_SCHEMA, 'gzip+base64'))
        document['data'] = base64.b64encode(gzip.compress(b'{}', mtime=0)).decode()
        with self.assertRaisesRegex(ValueError, 'digest'):
            export_conference.read_pack(json.dumps(document))

    def test_export_writes_the_viewer_and_removes_per_file_pages(self):
        stale = self.run / 'conference-view' / ('file-' + '0' * 20 + '.html')
        stale.parent.mkdir(parents=True)
        stale.write_text('old')
        (self.run / 'conference-view/files').mkdir()
        (self.run / 'conference-view/files/pack-09.json').write_text('{}')
        manifest = export_conference.export(self.run)
        out = self.run / 'conference-view'
        self.assertFalse(stale.exists())
        self.assertEqual(sorted(p.name for p in (out / 'files').iterdir()), ['pack-01.json'])
        self.assertEqual(manifest['captured_file_packs'],
                         {'pack-01.json': digest((out / 'files/pack-01.json').read_bytes())})
        viewer = (out / 'files.html').read_text()
        index = export_conference.files_page_index(viewer)
        self.assertEqual(index['run_start'], 5.0)
        self.assertEqual(len(index['files']), 4)
        for needed in ('id="site-theme"', 'DecompressionStream', "fetch('files/'+name)", 'Private researcher record.',
                       'Recorded times are capture times', "p.get('file')", "p.get('version')", 'Previous capture',
                       'Next capture', 'function runClock(t,s)', 'href="index.html#files"', 'width=device-width'):
            self.assertIn(needed, viewer)
        self.assertNotIn('<tag>', viewer)  # agent text is only in the packs, rendered with textContent
        data = export_conference.published_data((out / 'index.html').read_text())
        links = [r['links'] for r in data['records'] if r['category'] == 'files']
        self.assertEqual(sorted(links), sorted([['View captured versions', 'files.html#file=' + i]]
                                               for i in index['files']))
        # Export into another folder (prepare_hf_library.py) reads the run only.
        before = sorted(p.relative_to(self.run).as_posix() for p in self.run.rglob('*'))
        bundle = self.root / 'bundle' / RUN / 'conference-view'
        self.assertEqual(export_conference.export(self.run, out=bundle), manifest)
        self.assertEqual(sorted(p.relative_to(self.run).as_posix() for p in self.run.rglob('*')), before)
        self.assertEqual((bundle / 'files.html').read_bytes(), (out / 'files.html').read_bytes())
        with self.assertRaisesRegex(ValueError, 'records'):
            export_conference.export(self.run, out=self.run / 'conference')

    def test_prepare_repacks_a_legacy_view_into_the_bundle(self):
        import prepare_hf_library
        legacy = self.run / 'conference-view' / ('file-' + '0' * 20 + '.html')
        legacy.parent.mkdir(parents=True)
        legacy.write_text('<html>legacy</html>')
        self.assertTrue(prepare_hf_library.legacy_capture_pages(self.run))
        self.assertTrue(prepare_hf_library.legacy_capture_file(Path('conference-view') / legacy.name))
        self.assertTrue(prepare_hf_library.legacy_capture_file(Path('conference-view/files/pack-01.json')))
        self.assertFalse(prepare_hf_library.legacy_capture_file(Path('conference-view/index.html')))
        out = self.root / 'bundle' / RUN / 'conference-view'
        prepare_hf_library.pack_conference_view(self.run, out, RUN)
        self.assertEqual(sorted(p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()),
                         ['files.html', 'files/pack-01.json', 'index.html', 'manifest.json'])
        self.assertTrue(legacy.exists())  # the run's records are only read
        (self.run / 'runtime.jsonl').unlink()
        with self.assertRaisesRegex(ValueError, 'lacks the records'):
            prepare_hf_library.pack_conference_view(self.run, self.root / 'bundle2', RUN)
        legacy.unlink()
        prepare_hf_library.pack_conference_view(self.run, self.root / 'bundle3', RUN)  # packed: copied as is
        self.assertFalse((self.root / 'bundle3').exists())

    def test_viewer_gets_dynamic_agent_labels(self):
        from research.analysis import agent_badges
        badge_map = {'source': 'roles', 'agents': {'agent-002': [agent_badges._badge('overseer')]}}
        page = agent_badges.decorate_page(export_conference.render_files_page(
            export_conference.pack_captures(RUN, export_conference.captured_versions(self.run))[1], 5.0),
            'conference-view/files.html', badge_map)
        self.assertIn('id="ab-map"', page)
        self.assertIn('id="ab-legend"', page)
        export_conference.export(self.run)
        self.assertIn('conference-view/files.html', [rel for _, rel in agent_badges.run_pages(self.run)])

    def test_credential_screen_decodes_packs(self):
        versions = {('agent-001', '/workspace/x.md'): [dict(timestamp=1.0, sha256='a', blob_sha256='b', bytes=3,
                                                            mode='full', runtime_line=1,
                                                            content='key sk-' + 'A' * 40)]}
        packs, _ = export_conference.pack_captures(RUN, versions)
        data = packs['pack-01.json']
        self.assertIsNone(publish_hf_library.SECRET.search(data))  # invisible without decoding
        name = f'{RUN}/conference-view/files/pack-01.json'
        with self.assertRaisesRegex(ValueError, 'Credential-like'):
            publish_hf_library.space_gate(publish_hf_library.DEFAULT_REPO,
                                          [operation(path_in_repo=name, path_or_fileobj=data)])
        path = self.root / 'bundle-screen' / RUN / 'conference-view/files/pack-01.json'
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'captured-file pack'):
            publish_hf_library.screen(path)
        with self.assertRaisesRegex(ValueError, 'does not decode'):
            publish_hf_library.space_gate(publish_hf_library.DEFAULT_REPO,
                                          [operation(path_in_repo=name, path_or_fileobj=b'{"data": "!"}')])


class PackedScopeTests(unittest.TestCase):
    """--scope conference --pack-files against a Space holding the legacy per-file pages."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        root = patch.object(export_conference, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        self.runs = self.root / 'runs'
        run = make_conference_run(self.runs, RUN)
        add_captures(run)
        (run / 'results.json').write_text(json.dumps({'start_utc': START}))
        manifest = export_conference.export(run)
        data = export_conference.published_data((run / 'conference-view/index.html').read_text())
        for record in data['records']:  # the links of the legacy layout
            if record['category'] == 'files':
                record['links'] = [['View captured versions', record['id'] + '.html']]
        legacy_manifest = {k: v for k, v in manifest.items() if k not in export_conference.MANIFEST_OPTIONAL_KEYS}
        moved = {f'{RUN}/{rel}': {'repo': dataset_logs.DATASET_REPO, 'commit': COMMIT, 'path': f'{RUN}/{rel}', 'bytes': 1,
                                  'sha256': '0' * 64, 'category': 'jsonl'}
                 for rel in ('conference/events.jsonl', 'analysis/timeline.jsonl')}
        hosted = maintain_hf_run.hosted_conference_page(export_conference.render_index(data), RUN,
                                                        {'dataset_files': moved})
        page, old_manifest = with_timeline(hosted, legacy_manifest, entries=5)
        files = {'index.html': b'<html>catalogue</html>', 'experiments.json': b'{}',
                 f'{RUN}/conference-view/index.html': page.encode(),
                 f'{RUN}/conference-view/manifest.json': json.dumps(old_manifest, indent=2).encode(),
                 f'{RUN}/results.html': b'<html>report</html>', f'{RUN}/inspect-view/index.html': b'<html>i</html>',
                 f'{RUN}/results.json': json.dumps({'start_utc': START}).encode(),
                 'other-run/results.html': b'<html>other</html>',
                 'other-run/conference-view/file-' + 'a' * 20 + '.html': b'<html>other run capture</html>'}
        self.pages = []
        for (agent, path), rows in export_conference.captured_versions(run).items():
            name = f'{RUN}/conference-view/{export_conference.file_identity(agent, path)}.html'
            files[name] = legacy_capture_page(agent, path, rows, 5.0).encode()
            self.pages.append(name)
        archive = {'schema': 'gdm-private-space/1', 'runs': [RUN, 'other-run'], 'files': {},
                   'dataset_repo': dataset_logs.DATASET_REPO, 'offline_files': {}, 'dataset_files': moved}
        archive['files'] = {n: digest(d) for n, d in files.items()}
        archive['total_bytes'] = sum(x['bytes'] for x in archive['files'].values())
        files['archive-manifest.json'] = json.dumps(archive, indent=2).encode()
        self.hub = Hub(self.root)
        self.hub.push({**self.hub.files, **files})
        self.published, self.base = dict(self.hub.files), self.hub.sha
        self.run = run

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        records = self.root / 'records' / attempt
        if commit:
            kwargs.setdefault('backup_dir', records)
            kwargs.setdefault('receipt', records / 'receipt.json')
        kwargs.setdefault('runs', [RUN])
        return maintain_hf_run.maintain_conference_packed(self.hub, self.hub.download, runs_dir=self.runs,
                                                          commit=commit, operation=operation,
                                                          delete_operation=delete, **kwargs)

    def test_plan_is_read_only(self):
        plan = self.maintain()
        self.assertEqual((plan['committed'], self.hub.sha), (False, self.base))
        self.assertEqual(plan['to_delete'], {'capture_pages': 4, 'stale_packs': 0})
        self.assertEqual(plan['to_add'], [f'{RUN}/conference-view/files.html',
                                          f'{RUN}/conference-view/files/pack-01.json'])
        self.assertEqual(plan['space_files']['after'], plan['space_files']['before'] - 4 + 2)
        self.assertEqual(len(plan['per_run'][RUN]['sample_pages_compared']), 2)
        self.assertFalse((self.root / 'records').exists())

    def test_commit_packs_deletes_pages_and_is_idempotent(self):
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        for name in self.pages:
            self.assertNotIn(name, final)
        self.assertIn('other-run/conference-view/file-' + 'a' * 20 + '.html', final)  # other runs untouched
        for name in self.published:
            if name not in self.pages and not name.startswith(f'{RUN}/conference-view/') \
                    and name != 'archive-manifest.json':
                self.assertEqual(final[name], self.published[name], name)
        self.assertEqual(len(self.hub.commits), 1)
        self.assertEqual(sum(1 for p in self.hub.commits[0]['paths'] if p in self.pages), 4)
        viewer = final[f'{RUN}/conference-view/files.html'].decode()
        self.assertIn('id="private-space-navigation"', viewer)
        self.assertIn('id="site-theme"', viewer)
        index = export_conference.files_page_index(viewer)
        entries = export_conference.read_pack(final[f'{RUN}/conference-view/files/pack-01.json'])
        self.assertEqual(set(entries), set(index['files']))
        for name in self.pages:  # every deleted page's versions are in the pack
            found = re.findall(r'<pre>(.*?)</pre>', self.published[name].decode(), re.S)
            self.assertEqual([html.unescape(t) for t in found],
                             [v['content'] for v in entries[name.split('/')[-1][:-5]]['versions']])
        page = final[f'{RUN}/conference-view/index.html'].decode()
        data = export_conference.published_data(page)
        self.assertNotIn('activity', data['counts'])
        old = export_conference.published_data(self.published[f'{RUN}/conference-view/index.html'].decode())
        for new, before in zip(data['records'], [r for r in old['records'] if r['category'] != 'activity']):
            if new['category'] == 'files':
                self.assertEqual(new['links'], [['View captured versions', 'files.html#file=' + new['id']]])
                self.assertEqual({k: v for k, v in new.items() if k != 'links'},
                                 {k: v for k, v in before.items() if k != 'links'})
            else:
                self.assertEqual(new, before)
        manifest = json.loads(final[f'{RUN}/conference-view/manifest.json'])
        self.assertEqual(manifest['captured_file_layout'], 'packed')
        archive = json.loads(final['archive-manifest.json'])
        self.assertEqual(set(archive['files']), set(final) - {'archive-manifest.json', '.gitattributes'})
        for name, info in archive['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        receipt = json.loads((self.root / 'records/attempt-1/receipt.json').read_text())
        self.assertEqual(receipt['status'], 'verified')
        self.assertTrue((self.root / 'records/attempt-1/original' / RUN / 'conference-view/index.html').is_file())
        before = json.loads((self.root / 'records/attempt-1/before-inventory.json').read_text())
        self.assertEqual(sorted(before['deleted']), sorted(self.pages))
        again = self.maintain(commit=True, attempt='attempt-2')
        self.assertTrue(again['already_applied'])
        self.assertTrue(again['per_run'][RUN]['unchanged'])
        self.assertEqual(len(self.hub.commits), 1)

    def test_local_records_must_match_the_published_view(self):
        with (self.run / 'runtime.jsonl').open('a') as handle:  # a capture the published view never had
            blob = b'late'
            sha = hashlib.sha256(blob).hexdigest()
            (self.run / 'snapshots/agent-002' / sha).write_bytes(blob)
            handle.write(json.dumps({'kind': 'file_snapshot', 'agent_id': 'agent-002', 'timestamp': 99.0,
                                     'payload': {'path': '/workspace/late.md', 'blob_sha256': sha, 'sha256': sha,
                                                 'bytes': 4, 'mode': 'full'}}) + '\n')
        with self.assertRaisesRegex(ValueError, 'differ from the published conference view'):
            self.maintain()

    def test_sampled_page_must_match_the_packs(self):
        name = sorted(self.pages)[0]
        self.hub.push({**self.published, name: self.published[name].replace(b'<pre>', b'<pre>tampered ', 1)})
        with self.assertRaisesRegex(ValueError, 'differs from the packed captured versions'):
            self.maintain()

    def test_offloaded_or_missing_records_are_skipped_without_reading(self):
        with patch.object(maintain_hf_run, 'offloaded_records', return_value=['snapshots/agent-002/x']), \
                patch.object(export_conference, 'captured_versions', side_effect=AssertionError('read')):
            plan = self.maintain()
        self.assertIn('iCloud-offloaded', plan['skipped'][RUN])
        self.assertEqual((plan['commits'], plan['to_delete']['capture_pages']), (0, 0))
        plan = maintain_hf_run.maintain_conference_packed(self.hub, self.hub.download, runs=[RUN],
                                                          runs_dir=self.root / 'elsewhere')
        self.assertIn('no local run records', plan['skipped'][RUN])
        placeholder = self.root / 'offloaded'
        (placeholder / 'snapshots/agent-001').mkdir(parents=True)
        (placeholder / 'snapshots/agent-001/.blob.icloud').write_text('')
        (placeholder / 'runtime.jsonl').write_text('')
        self.assertEqual(maintain_hf_run.offloaded_records(placeholder), ['snapshots/agent-001/.blob.icloud'])

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, 'not published'):
            self.maintain(runs=['never-published'])
        with self.assertRaisesRegex(ValueError, 'runs-dir'):
            maintain_hf_run.maintain_conference_packed(self.hub, self.hub.download, runs=[RUN], runs_dir=None)
        with self.assertRaisesRegex(ValueError, 'delete operations'):
            maintain_hf_run.maintain_conference_packed(self.hub, self.hub.download, runs=[RUN], runs_dir=self.runs,
                                                       commit=True, operation=operation, receipt=self.root / 'r.json',
                                                       backup_dir=self.root / 'b')
        self.hub.concurrent = True
        with self.assertRaisesRegex(ValueError, 'concurrently'):
            self.maintain(commit=True)
        self.hub.concurrent = False
        self.assertNotIn(f'{RUN}/conference-view/files.html', self.hub.files)  # nothing committed
        self.assertTrue(all(name in self.hub.files for name in self.pages))

    def test_cli_flags(self):
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            maintain_hf_run.main(['--maintain-run', RUN, '--scope', 'conference', '--pack-files', '--capture-pages'])


class PublishLeftoverTests(unittest.TestCase):
    """An interrupted publication's per-file pages of a never-published run are deleted; recorded runs stay
    immutable."""

    def setUp(self):
        import test_hf_publication
        self.case = test_hf_publication.PublicationTests('test_concurrent_revision_refused_before_write')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_leftover_capture_pages_of_an_unrecorded_run_are_deleted(self):
        hub = self.case.hub
        stale = 'a-new/conference-view/file-' + 'b' * 20 + '.html'
        hub.files[stale] = b'<html>legacy page</html>'
        plan = self.case.run_publish()
        self.assertEqual(plan['unpublished_leftovers']['deleted'], 1)
        result = self.case.run_publish(commit=True, delete_operation=delete)
        self.assertTrue(result['committed'])
        self.assertNotIn(stale, hub.files)
        self.assertTrue(hub.commits[0]['commit_message'].startswith('Remove leftovers of an interrupted publication'))

    def test_other_unstaged_files_still_refused(self):
        self.case.hub.files['a-new/notes.html'] = b'someone else'
        with self.assertRaisesRegex(ValueError, 'unstaged file'):
            self.case.run_publish(commit=True, delete_operation=delete)


class VerifyPackedTests(unittest.TestCase):
    def setUp(self):
        import verify_hosted
        from test_research_tools import FakeApi, FakeSession
        self.verify_hosted, self.FakeApi, self.FakeSession = verify_hosted, FakeApi, FakeSession
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        run = make_conference_run(self.root / 'runs', 'demo-run')
        add_captures(run)
        with patch.object(export_conference, 'ROOT', self.root):
            export_conference.export(run, out=self.root / 'space/demo-run/conference-view')
        self.space = self.root / 'space'
        for page in verify_hosted.PAGES:
            path = self.space / page.format(run='demo-run')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'<!doctype html><title>x</title>')

    def failed(self, **kwargs):
        record = self.verify_hosted.verify(self.FakeApi(self.space), self.FakeSession(self.space), {}, 'org/space',
                                           'demo-run', 'abc', **kwargs)
        return record, {c['check'] for c in record['checks'] if not c['ok']}

    def test_packed_view_checks(self):
        record, failed = self.failed(space_file_count=True)
        self.assertEqual(failed, set())
        checks = {c['check'] for c in record['checks']}
        self.assertTrue({'conference_packed', 'no_capture_pages', 'conference_packs_stored', 'conference_pack_decodes',
                         'conference_deep_link'} <= checks)
        self.assertEqual(record['conference_layout'], 'packed')
        self.assertEqual(record['space_files_run'], sum(1 for p in (self.space / 'demo-run').rglob('*') if p.is_file()))
        self.assertEqual(record['space_files_total'], record['space_files_run'] + 1)
        self.assertIn('demo-run/conference-view/files.html', [c.get('path') for c in record['checks']
                                                              if c['check'] == 'served_html'])
        stray = self.space / 'demo-run/conference-view' / ('file-' + 'c' * 20 + '.html')
        stray.write_text('<html></html>')
        self.assertIn('no_capture_pages', self.failed()[1])
        stray.unlink()
        pack = self.space / 'demo-run/conference-view/files/pack-01.json'
        pack.write_bytes(pack.read_bytes().replace(b'"files"', b'"files" ', 1))
        self.assertIn('conference_pack_decodes', self.failed()[1])
        pack.unlink()
        self.assertTrue({'conference_packs_stored', 'conference_pack_decodes'} <= self.failed()[1])

    def test_legacy_layout_reported_and_refused_when_packed_is_expected(self):
        (self.space / 'demo-run/conference-view/files.html').unlink()
        (self.space / 'demo-run/conference-view' / ('file-' + 'c' * 20 + '.html')).write_text('<html></html>')
        record, failed = self.failed()
        self.assertEqual((record['conference_layout'], failed), ('pages', set()))
        self.assertEqual(self.failed(expect_packed_files=True)[1], {'conference_packed'})


if __name__ == '__main__':
    unittest.main()
