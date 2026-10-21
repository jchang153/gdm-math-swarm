"""Offline tests of per-run snapshot archives (owner, October 21, 2026: HF's 20,000-files-per-repository
limit): prepare_hf_library.py archives RUN/snapshots/ deterministically, publish refuses loose snapshot
files and screens archive members, and maintain_hf_run.py --scope snapshots replaces published loose
files with the archive in guarded batches. In-memory private Space; no HF login, network or credentials.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import maintain_hf_run  # noqa: E402
from maintain_hf_run import archive_snapshots  # noqa: E402
from publish_hf_library import digest, load_bundle, publish  # noqa: E402
import prepare_hf_library  # noqa: E402
from prepare_hf_library import check_snapshot_archive, snapshot_archive  # noqa: E402
from test_hf_maintenance import RULE, Hub, operation  # noqa: E402
from test_judge_publication import make_run, make_viewer  # noqa: E402

RUN, OTHER = 'snap-run', 'a-old'
TAR_RULE = b'*.tar.*' + RULE + b'\n'


def delete(**kwargs):
    return SimpleNamespace(delete=True, **kwargs)


def add_snapshots(run, count=5):
    blobs = {}
    for i in range(count):
        data = f'captured file {i}\n'.encode()
        name = f'snapshots/agent-00{i % 2 + 1}/{hashlib.sha256(data).hexdigest()}'
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_bytes(data)
        blobs[name] = data
    return blobs


class SnapshotArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        self.blobs = add_snapshots(make_run(self.runs, RUN))
        self.hub = Hub(self.root)
        self.hub.history['0']['.gitattributes'] += TAR_RULE  # as in the Space's HF default rules
        publish(self.prepare('published', RUN, OTHER), self.hub, self.hub.download, commit=True,
                operation=operation)
        self.archived = dict(self.hub.files)
        self.legacy()

    def prepare(self, name, *runs):
        output = self.root / 'bundles' / name
        argv = ['prepare_hf_library.py', '--runs-dir', str(self.runs), '--output-dir', str(output)]
        for run in runs:
            argv += ['--run', f'{run}={self.viewer}']
        with patch.object(sys, 'argv', argv), patch('builtins.print'):
            prepare_hf_library.main()
        return output

    def legacy(self, run=RUN, recorded=True, blobs=None):
        """The Space as published before snapshot archives: loose files instead of the archive."""
        files = {n: d for n, d in self.archived.items()
                 if n not in (run + '/snapshots.tar.gz', run + '/snapshots.manifest.json')}
        manifest = json.loads(files['archive-manifest.json'])
        for name in (run + '/snapshots.tar.gz', run + '/snapshots.manifest.json'):
            manifest['files'].pop(name, None)
        for name, data in (blobs or self.blobs).items():
            files[run + '/' + name] = data
            if recorded:
                manifest['files'][run + '/' + name] = digest(data)
        if not recorded:
            manifest['runs'].remove(run)
        manifest['total_bytes'] = sum(x['bytes'] for x in manifest['files'].values())
        files['archive-manifest.json'] = json.dumps(manifest, indent=2).encode()
        self.hub.push(files)
        self.published, self.base, self.commits = dict(self.hub.files), self.hub.sha, len(self.hub.commits)

    def scope(self, commit=False, attempt='attempt-1', **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.root / 'records' / attempt)
            kwargs.setdefault('receipt', self.root / 'records' / attempt / 'receipt.json')
        return archive_snapshots(self.hub, self.hub.download, run=RUN, runs_dir=self.runs, commit=commit,
                                 operation=operation, delete_operation=delete, **kwargs)

    def assert_archived(self):
        final = self.hub.files
        loose = {RUN + '/' + n for n in self.blobs}
        self.assertEqual(set(final), (set(self.published) - loose)
                         | {RUN + '/snapshots.tar.gz', RUN + '/snapshots.manifest.json'})
        for name, data in self.published.items():
            if name not in loose and name != 'archive-manifest.json':
                self.assertEqual(final[name], data, name)
        self.assertEqual(final['.gitattributes'], self.published['.gitattributes'])  # existing *.tar.* rule
        records = check_snapshot_archive(final[RUN + '/snapshots.tar.gz'], final[RUN + '/snapshots.manifest.json'], RUN)
        self.assertEqual(records, {n: digest(d) for n, d in self.blobs.items()})
        manifest = json.loads(final['archive-manifest.json'])
        self.assertEqual(manifest, json.loads(self.archived['archive-manifest.json']))  # as a fresh publication
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)

    def test_preparation_archives_snapshots_deterministically(self):
        bundle = self.prepare('again', RUN)
        names = {p.relative_to(bundle).as_posix() for p in bundle.rglob('*') if p.is_file()}
        self.assertFalse(any('/snapshots/' in n for n in names))
        archive = (bundle / RUN / 'snapshots.tar.gz').read_bytes()
        self.assertEqual(archive, self.archived[RUN + '/snapshots.tar.gz'])  # same bytes on every preparation
        self.assertEqual((archive, (bundle / RUN / 'snapshots.manifest.json').read_bytes()),
                         snapshot_archive(self.runs / RUN, RUN))
        self.assertEqual(json.loads((bundle / RUN / 'snapshots.manifest.json').read_text())['file_count'], 5)
        self.assertIsNone(snapshot_archive(self.runs / OTHER, OTHER))

    def test_publication_refuses_loose_snapshots_secrets_and_snapshot_links(self):
        bundle = self.prepare('loose', RUN)
        loose = bundle / RUN / 'snapshots/agent-001/x'
        loose.parent.mkdir(parents=True)
        loose.write_bytes(b'x')
        manifest = json.loads((bundle / 'archive-manifest.json').read_text())
        manifest['files'][RUN + '/snapshots/agent-001/x'] = digest(b'x')
        (bundle / 'archive-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'loose snapshot'):
            load_bundle(bundle)
        secret = self.runs / RUN / 'snapshots/agent-002/secret'
        secret.write_bytes(b'key=sk-' + b'a' * 30)
        with self.assertRaisesRegex(ValueError, 'Credential-like content in snapshot archive'):
            load_bundle(self.prepare('secret', RUN))
        secret.unlink()
        (self.runs / RUN / 'conference-view/index.html').write_text('<a href="../snapshots/agent-001/x">f</a>')
        with self.assertRaisesRegex(ValueError, 'links loose snapshot'):
            self.prepare('linked', RUN)

    def test_dry_run_then_commit_in_batches(self):
        plan = self.scope()
        self.assertEqual((plan['loose_published'], plan['to_delete'], plan['archive_manifest_update'],
                          plan['files_after']), (5, 5, True, len(self.published) - 3))
        self.assertEqual((self.hub.sha, len(self.hub.commits)), (self.base, self.commits))
        with patch.object(maintain_hf_run, 'DELETE_BATCH', 2):
            result = self.scope(commit=True)
        self.assertEqual((result['status'], result['committed_batches']), ('verified', 5))  # add, 3 deletes, manifest
        self.assertEqual(result['verification']['loose_files_left'], 0)
        self.assert_archived()
        records = self.root / 'records/attempt-1'
        self.assertEqual((records / 'snapshots.tar.gz').read_bytes(), self.hub.files[RUN + '/snapshots.tar.gz'])
        self.assertEqual(len(json.loads((records / 'deleted.json').read_text())), 5)
        self.assertEqual((records / 'original/archive-manifest.json').read_bytes(),
                         self.published['archive-manifest.json'])
        self.assertTrue(self.scope(commit=True, attempt='attempt-2')['already_applied'])
        self.assertEqual(len(self.hub.commits), self.commits + 5)

    def test_interrupted_update_continues_when_run_again(self):
        self.hub.fail_after = self.commits + 2  # the archive and one deletion batch
        with patch.object(maintain_hf_run, 'DELETE_BATCH', 2), self.assertRaises(RuntimeError):
            self.scope(commit=True)
        self.assertTrue(json.loads((self.root / 'records/attempt-1/receipt.json').read_text())['status']
                        .startswith('failed'))
        self.hub.fail_after = None
        plan = self.scope()
        self.assertEqual((plan['to_upload'], plan['to_delete']), ([], 3))
        self.scope(commit=True, attempt='attempt-2')
        self.assert_archived()

    def test_partly_published_run_needs_only_a_verified_subset(self):
        subset = dict(list(self.blobs.items())[:2])
        self.legacy(recorded=False, blobs=subset)
        result = self.scope(commit=True)
        self.assertEqual((result['to_delete'], result['archive_manifest_update']), (2, False))
        self.assertEqual(self.hub.files['archive-manifest.json'], self.published['archive-manifest.json'])
        self.assertFalse(any(n.startswith(RUN + '/snapshots/') for n in self.hub.files))

    def test_refusals_before_any_write(self):
        name = next(iter(self.blobs))
        self.hub.push({**self.published, RUN + '/' + name: b'changed'})
        with self.assertRaisesRegex(ValueError, 'differ from the archived bytes'):
            self.scope(commit=True)
        self.hub.push({**self.published, RUN + '/snapshots/agent-009/extra': b'extra'})
        with self.assertRaisesRegex(ValueError, 'not in the local archive'):
            self.scope(commit=True)
        (self.runs / RUN / name).unlink()  # local records lost a published member
        self.hub.push(self.published)
        with self.assertRaisesRegex(ValueError, 'not in the local archive'):
            self.scope()
        (self.runs / RUN / name).write_bytes(self.blobs[name])
        extra = self.runs / RUN / 'snapshots/agent-001/later'
        extra.write_bytes(b'later')  # the published records lack a local member
        with self.assertRaisesRegex(ValueError, 'differ from the local archive'):
            self.scope(commit=True)
        extra.unlink()
        self.hub.private = False
        with self.assertRaisesRegex(ValueError, 'private'):
            self.scope()
        self.hub.private = True
        self.hub.concurrent = True
        with self.assertRaisesRegex(ValueError, 'concurrently'):
            self.scope(commit=True, attempt='attempt-2')
        self.assertEqual(set(self.hub.files), set(self.published))
        self.assertEqual(sum(c is not None and c['message'].startswith('Archive') for c in self.hub.commits), 0)


if __name__ == '__main__':
    unittest.main()
