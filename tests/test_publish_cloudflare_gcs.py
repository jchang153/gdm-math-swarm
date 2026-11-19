"""publish_cloudflare.py publish --run (owner decision, November 19, 2026: restore from the GCS archive, build,
publish, delete the local copy) and the HF-subset-aware dataset links. Offline: a fake bucket store and a fake
prepare runner; no gcloud, HF, wrangler or network."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import dataset_logs  # noqa: E402
import gcs_runs  # noqa: E402
import publish_cloudflare as cf  # noqa: E402

RUN = 'r1-20261119T000000Z'
COMMIT = 'b' * 40
REPO = 'https://huggingface.co/datasets/org/logs'


class FakeStore:
    """A dict bucket {key: bytes} with the GcloudStore methods."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.downloads = []

    def list(self, prefix):
        return {k[len(prefix):]: {'bytes': len(v), 'md5': gcs_runs.md5_b64(hashlib.md5(v).digest()), 'crc32c': None}
                for k, v in self.objects.items() if k.startswith(prefix)}

    def download_tree(self, prefix, local_dir, exclude=None):
        self.downloads.append(prefix)
        prefix = prefix.rstrip('/') + '/'
        for key, data in self.objects.items():
            if not key.startswith(prefix):
                continue
            rel = key[len(prefix):]
            if exclude and re.match(exclude, rel):
                continue
            target = Path(local_dir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def put(self, local_file, key):
        self.objects[key] = Path(local_file).read_bytes()

    def upload_tree(self, local_dir, prefix):
        for path in Path(local_dir).rglob('*'):
            if path.is_file():
                self.objects[prefix.rstrip('/') + '/' + path.relative_to(local_dir).as_posix()] = path.read_bytes()

    def delete(self, keys):
        for key in keys:
            self.objects.pop(key, None)

    def local_crc32c(self, path):
        return None


def write(root, files):
    for name, data in files.items():
        (Path(root) / name).parent.mkdir(parents=True, exist_ok=True)
        (Path(root) / name).write_bytes(data if isinstance(data, bytes) else data.encode())


def no_runner(*args, **kwargs):
    raise AssertionError('prepare_hf_library.py must not run')


UPLOAD = json.dumps({'outcome': 'uploaded', 'dataset_commit': COMMIT})


class BundleForRunTests(unittest.TestCase):
    def test_local_site_bundle_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / 'runs'
            write(runs, {f'phase-b/{RUN}/site-bundle/archive-manifest.json': '{}',
                         f'phase-b/{RUN}/hf-upload.json': UPLOAD})
            store = FakeStore()
            found = cf.bundle_for_run(RUN, Path(tmp) / 'work', store=store, runner=no_runner, runs_root=runs)
            self.assertEqual(found['bundle'], runs / 'phase-b' / RUN / 'site-bundle')
            self.assertEqual(found['dataset_commit'], COMMIT)
            self.assertEqual(found['resolved'], 'local site-bundle')
            self.assertEqual(store.downloads, [])

    def test_local_site_bundle_without_bucket_has_no_commit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'GDM_RUNS_BUCKET': ''}), \
                patch.object(gcs_runs, 'BUCKET_FILE', Path(tmp) / 'absent'):
            runs = Path(tmp) / 'runs'
            write(runs, {f'phase-b/{RUN}/site-bundle/archive-manifest.json': '{}'})
            found = cf.bundle_for_run(RUN, Path(tmp) / 'work', runner=no_runner, runs_root=runs)
            self.assertIsNone(found['dataset_commit'])

    def test_site_bundle_restored_from_bucket_and_scratch_deleted(self):
        store = FakeStore({f'runs/phase-b/{RUN}/site-bundle/archive-manifest.json': b'{"m": 1}',
                           f'runs/phase-b/{RUN}/site-bundle/{RUN}/results.html': b'<html></html>',
                           f'runs/phase-b/{RUN}/inspect-view/index.html': b'<html>viewer</html>',
                           f'runs/phase-b/{RUN}/hf-upload.json': UPLOAD.encode()})
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / 'runs'
            runs.mkdir()
            with tempfile.TemporaryDirectory(dir=tmp) as scratch:
                found = cf.bundle_for_run(RUN, scratch, store=store, runner=no_runner, runs_root=runs)
                self.assertEqual(found['resolved'], 'site-bundle restored from the bucket')
                self.assertEqual(found['dataset_commit'], COMMIT)
                self.assertEqual((found['bundle'] / 'archive-manifest.json').read_bytes(), b'{"m": 1}')
                self.assertTrue((found['bundle'] / RUN / 'results.html').is_file())
                self.assertFalse((Path(scratch) / 'phase' / 'inspect-view').exists())  # only the prefix
            self.assertFalse(Path(scratch).exists())
            self.assertFalse((runs / 'phase-b').exists())  # nothing restored into the local runs folder

    def test_raw_run_restored_and_prepare_invoked(self):
        store = FakeStore({f'runs/{RUN}/results.json': b'{}', f'runs/{RUN}/inspect/a.eval': b'PK',
                           f'runs/{RUN}/_gcs/manifest-x.json.gz': b'x',
                           f'runs/phase-b/{RUN}/inspect-view/index.html': b'<html>viewer</html>',
                           f'runs/phase-b/{RUN}/hf-upload.json': UPLOAD.encode()})
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            out = Path(command[command.index('--output-dir') + 1])
            write(out, {'archive-manifest.json': '{}'})
            return type('Done', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()

        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'GDM_LOGS_DATASET': 'org/logs',
                                                                             'HF_TOKEN': 'x'}):
            runs = Path(tmp) / 'runs'
            write(runs, {f'{RUN}/results.json': '{}', f'{RUN}/GCS.json': '{}'})  # offloaded: only the index
            work = Path(tmp) / 'work'
            found = cf.bundle_for_run(RUN, work, store=store, runner=runner, runs_root=runs)
            self.assertEqual(found['resolved'], 'raw run restored from the bucket')
            self.assertEqual(found['bundle'], work / 'bundle')
            self.assertEqual(found['dataset_commit'], COMMIT)
            self.assertEqual((work / 'runs' / RUN / 'inspect' / 'a.eval').read_bytes(), b'PK')
            self.assertFalse((work / 'runs' / RUN / '_gcs').exists())
            (command, kwargs), = calls
            self.assertEqual(command[1:], [str(ROOT / 'scripts' / 'prepare_hf_library.py'),
                                           '--runs-dir', str(work / 'runs'),
                                           '--run', f'{RUN}={work / "phase" / "inspect-view"}',
                                           '--output-dir', str(work / 'bundle')])
            self.assertEqual(kwargs['env']['GDM_LOGS_DATASET'], 'org/logs')
            self.assertNotIn('HF_TOKEN', kwargs['env'])
            self.assertIn(str(ROOT / 'scripts'), kwargs['env']['PYTHONPATH'])
            self.assertEqual(kwargs['cwd'], cf.ROOT)

    def test_local_complete_run_used_and_missing_viewer_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / 'runs'
            write(runs, {f'{RUN}/inspect/a.eval': 'PK', f'{RUN}/results.json': '{}'})
            self.assertTrue(cf.local_run_complete(runs / RUN))
            with self.assertRaisesRegex(ValueError, 'no Inspect View bundle'):
                cf.bundle_for_run(RUN, Path(tmp) / 'work', store=FakeStore(), runner=no_runner, runs_root=runs)
            write(runs, {f'phase-b/{RUN}/inspect-view/index.html': '<html></html>'})
            seen = []

            def runner(command, **kwargs):
                seen.append(command)
                return type('Done', (), {'returncode': 1, 'stdout': '', 'stderr': 'boom'})()
            with self.assertRaisesRegex(RuntimeError, 'boom'):
                cf.bundle_for_run(RUN, Path(tmp) / 'work', store=FakeStore(), runner=runner, runs_root=runs)
            self.assertIn(str(runs), seen[0])  # built from the local runs folder
            write(runs, {f'{RUN}/OFFLOADED.json': '{}'})
            self.assertFalse(cf.local_run_complete(runs / RUN))

    def test_nothing_anywhere_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'no site bundle and no complete run'):
                cf.bundle_for_run(RUN, Path(tmp) / 'work', store=FakeStore(), runner=no_runner,
                                  runs_root=Path(tmp) / 'runs')


def fake_catalogue(entries, unlisted, files=None, site=None):
    data = json.dumps({'experiments': entries, 'warnings': []}).encode()
    return {'index.html': b'<html></html>', 'experiments.json': data}


class SubsetLinkTests(unittest.TestCase):
    def link(self, path, kind='blob'):
        return f'<a href="{REPO}/{kind}/{dataset_logs.COMMIT_PLACEHOLDER}/{path}">x</a>'

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_non_subset_files_repointed_and_not_checked(self, _):
        page = ('<html>' + self.link(f'{RUN}/resume/state.tar.gz') + self.link(f'{RUN}/snapshots.tar.gz', 'resolve')
                + self.link(f'{RUN}/resume', 'tree') + self.link(f'{RUN}/raw/events.jsonl')
                + self.link(f'{RUN}/inspect/a.eval') + '</html>')
        dataset = {name: {'bytes': 1} for name in (f'{RUN}/resume/state.tar.gz', f'{RUN}/snapshots.tar.gz',
                                                    f'{RUN}/raw/events.jsonl', f'{RUN}/inspect/a.eval')}
        checked = []

        def checker(name, size, sha256):
            checked.append(name)
            return True

        def load_bundle(bundle, remote):
            for name in dataset:
                self.assertTrue(remote(name, 1, '0' * 64))
            manifest = {'runs': [RUN], 'files': {f'{RUN}/results.html': {}}, 'dataset': dataset}
            return manifest, [{'run': RUN, 'start_utc': '2026-11-19T00:00:00Z', 'links': {}}]

        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, {f'{RUN}/results.html': page})
            with patch.object(cf, 'load_bundle', side_effect=load_bundle):
                files, plan = cf.plan_bundle({}, tmp, unlisted=(), dataset_commit=COMMIT, remote=checker)
        self.assertEqual(sorted(checked), [f'{RUN}/inspect/a.eval', f'{RUN}/raw/events.jsonl'])
        self.assertEqual(plan['repointed_off_hf_links'], 3)
        self.assertEqual(plan['bucket_only_dataset_files'], 2)
        text = files[f'{RUN}/results.html'].decode()
        self.assertEqual(text.count(f'href="../{cf.PRIVATE_PAGE}"'), 3)
        self.assertIn(f'{REPO}/blob/{COMMIT}/{RUN}/raw/events.jsonl', text)
        self.assertIn(f'{REPO}/blob/{COMMIT}/{RUN}/inspect/a.eval', text)
        self.assertNotIn('resume', text)
        self.assertNotIn('snapshots', text)
        # A wider subset (an older run's upload) keeps every link pinned to HF and checks every file.
        checked.clear()
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, {f'{RUN}/results.html': page})
            with patch.object(cf, 'load_bundle', side_effect=load_bundle):
                files, plan = cf.plan_bundle({}, tmp, unlisted=(), dataset_commit=COMMIT, remote=checker,
                                             hf_subset=dataset_logs.CATEGORIES)
        self.assertEqual(len(checked), 4)
        self.assertEqual(plan['repointed_off_hf_links'], 0)
        self.assertIn(f'{REPO}/blob/{COMMIT}/{RUN}/resume/state.tar.gz', files[f'{RUN}/results.html'].decode())

    def test_dataset_category(self):
        self.assertEqual(cf.dataset_category(f'live/{RUN}/resume/a%20b.json'), 'resume')
        self.assertEqual(cf.dataset_category(f'{RUN}/inspect/a.eval'), 'eval')
        self.assertIsNone(cf.dataset_category(f'{RUN}/results.html'))


if __name__ == '__main__':
    unittest.main()
