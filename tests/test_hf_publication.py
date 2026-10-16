"""Offline publication tests: no HF login, network, model calls or VM required."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from publish_hf_library import publish, digest, load_bundle, merge_entries
from build_experiment_index import write_index


def entry(run, hour):
    return {'run': run, 'start_utc': f'2026-10-15T{hour:02d}:00:00+00:00',
            'date': str(hour), 'track': 'spontaneous', 'agents': 20,
            'model': 'example', 'provider_route': 'example', 'elapsed_seconds': 12,
            'pilot_spent_usd': '1', 'distinct_verified_tasks': 1, 'task_count': 14,
            'agent_exits': {'budget_exhausted': 20},
            'links': {'Report': run + '/results.html'}}


class Hub:
    def __init__(self, root, private=True):
        self.root = root
        self.files = {}
        self.revision = 0
        self.private = private
        self.commits = []
        self.concurrent = False
        self.fail_after = None

    def repo_info(self, *args, **kwargs):
        if self.concurrent and not kwargs.get('files_metadata'):
            self.revision += 1
        return SimpleNamespace(private=self.private, sha=str(self.revision), siblings=[
            SimpleNamespace(rfilename=n, blob_id=hashlib.sha1(
                b'blob ' + str(len(d)).encode() + b'\0' + d).hexdigest(), lfs=None)
            for n, d in self.files.items()])

    def download(self, repo, name, **kwargs):
        f = self.root / 'remote-cache' / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(self.files[name])
        return f

    def create_commit(self, **kwargs):
        assert kwargs['parent_commit'] == str(self.revision)
        if self.fail_after is not None and len(self.commits) == self.fail_after:
            raise RuntimeError('simulated upload interruption')
        self.commits.append(kwargs)
        for op in kwargs['operations']:
            d = op.path_or_fileobj
            self.files[op.path_in_repo] = Path(d).read_bytes() if isinstance(d, str) else d
        self.revision += 1
        return SimpleNamespace(oid=str(self.revision))


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.new = entry('a-new', 20)
        for name in ('results.json', 'results.html', 'transcripts/index.html',
                     'conference-view/index.html', 'inspect-view/index.html',
                     'inspect-view/logs/listing.json'):
            f = self.bundle / 'a-new' / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('{}' if name.endswith('.json') else '<html>new</html>')
        (self.bundle / 'README.md').write_text('sdk: static')
        write_index(self.bundle, [self.new], hosted=True)
        self.seal()
        self.hub = Hub(self.root)
        old = entry('z-old', 10)
        olddir = self.root / 'old'
        write_index(olddir, [old], hosted=True)
        self.hub.files = {n: (olddir / n).read_bytes() for n in ('index.html', 'experiments.json')}
        self.hub.files['z-old/results.html'] = b'old run untouched'
        self.hub.files['README.md'] = b'original Space settings'
        self.hub.files['archive-manifest.json'] = json.dumps({'schema': 'gdm-private-space/1',
            'runs': ['z-old'], 'files': {n: digest(d) for n, d in self.hub.files.items()}}).encode()

    def seal(self):
        files = {f.relative_to(self.bundle).as_posix(): digest(f.read_bytes())
                 for f in self.bundle.rglob('*') if f.is_file() and f.name != 'archive-manifest.json'}
        (self.bundle / 'archive-manifest.json').write_text(json.dumps({
            'schema': 'gdm-private-space/1', 'runs': ['a-new'], 'files': files}))

    def run_publish(self, **kwargs):
        return publish(self.bundle, self.hub, self.hub.download,
                       operation=lambda **kw: SimpleNamespace(**kw), **kwargs)

    def test_merge_preserves_old_run_and_config_and_is_chronological(self):
        old = self.hub.files['z-old/results.html']
        result = self.run_publish(commit=True)
        self.assertTrue(result['committed'])
        self.assertEqual(self.hub.files['z-old/results.html'], old)
        self.assertEqual(self.hub.files['README.md'], b'original Space settings')
        self.assertEqual(result['runs'], ['z-old', 'a-new'])
        final_ops = {o.path_in_repo for o in self.hub.commits[-1]['operations']}
        self.assertEqual(final_ops, {'index.html', 'experiments.json', 'archive-manifest.json'})
        manifest = json.loads(self.hub.files['archive-manifest.json'])
        for name, expected in manifest['files'].items():
            self.assertEqual(digest(self.hub.files[name]), expected)

    def test_repeated_identical_publication_is_idempotent(self):
        self.run_publish(commit=True)
        commits = len(self.hub.commits)
        self.run_publish(commit=True)
        self.assertEqual(len(self.hub.commits), commits)

    def test_default_is_read_only(self):
        result = self.run_publish()
        self.assertFalse(result['committed'])
        self.assertEqual(self.hub.commits, [])

    def test_public_space_refused(self):
        self.hub.private = False
        with self.assertRaisesRegex(ValueError, 'private Space'):
            self.run_publish(commit=True)
        self.assertEqual(self.hub.commits, [])

    def test_concurrent_revision_refused_before_write(self):
        self.hub.concurrent = True
        with self.assertRaisesRegex(ValueError, 'concurrently'):
            self.run_publish(commit=True)
        self.assertEqual(self.hub.commits, [])

    def test_conflicting_existing_run_file_refused(self):
        self.hub.files['a-new/results.html'] = b'another researcher owns this run ID'
        with self.assertRaisesRegex(ValueError, 'Immutable remote run file differs'):
            self.run_publish(commit=True)
        self.assertEqual(self.hub.commits, [])

    def test_interrupted_upload_resumes_matching_files(self):
        self.hub.fail_after = 1
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.run_publish(commit=True)
        self.assertNotIn('a-new', self.hub.files['experiments.json'].decode())
        self.hub.fail_after = None
        result = self.run_publish(commit=True)
        self.assertTrue(result['committed'])
        self.assertEqual(result['runs'], ['z-old', 'a-new'])

    def test_tampered_bundle_refused(self):
        (self.bundle / 'a-new/results.html').write_text('changed after preparation')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.run_publish(commit=True)
        self.assertEqual(self.hub.commits, [])

    def test_unmanifested_host_file_refused(self):
        (self.bundle / '.env').write_text('not a real credential')
        with self.assertRaisesRegex(ValueError, 'Unsafe archive path'):
            load_bundle(self.bundle)

    def test_credential_inside_compressed_inspect_log_refused(self):
        with zipfile.ZipFile(self.bundle / 'a-new/record.eval', 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('sample.json', 'hf_' + 'A' * 32)  # fabricated fixture
        self.seal()
        with patch.dict(sys.modules, {'zipfile_zstd': SimpleNamespace()}):
            with self.assertRaisesRegex(ValueError, 'Inspect archive'):
                self.run_publish(commit=True)
        self.assertEqual(self.hub.commits, [])

    def test_malicious_existing_catalogue_link_refused(self):
        bad = entry('evil', 1)
        bad['links']['Report'] = 'javascript:alert(1)'
        with self.assertRaisesRegex(ValueError, 'leaves its run'):
            merge_entries([bad], [self.new])


if __name__ == '__main__':
    unittest.main()
