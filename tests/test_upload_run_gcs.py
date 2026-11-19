"""Offline tests of scripts/upload_run.py's two targets (owner decision, November 19, 2026): the complete run to
the GCS archive bucket (a fake in-memory store) and only the viewable subset to the logs dataset (a fake HF
dataset)."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import dataset_logs  # noqa: E402
import gcs_runs  # noqa: E402
import offload_runs  # noqa: E402
import upload_run  # noqa: E402
from test_upload_run import FakeDataset, REPO, write  # noqa: E402


def md5_b64(data):
    return base64.b64encode(hashlib.md5(data).digest()).decode()


class FakeStore:
    """An in-memory bucket with gcs_runs.GcloudStore's methods (keys relative to the bucket URL)."""

    def __init__(self):
        self.objects = {}

    def list(self, prefix):
        return {k[len(prefix):]: {'bytes': len(v), 'md5': md5_b64(v), 'crc32c': None}
                for k, v in self.objects.items() if k.startswith(prefix)}

    def upload_tree(self, local_dir, prefix):
        local_dir = Path(local_dir)
        for path in local_dir.rglob('*'):
            if path.is_file() and not path.is_symlink():
                self.objects[prefix.rstrip('/') + '/' + path.relative_to(local_dir).as_posix()] = path.read_bytes()

    def download_tree(self, prefix, local_dir, exclude=None):
        for key, data in self.objects.items():
            if key.startswith(prefix):
                write(Path(local_dir) / key[len(prefix):].lstrip('/'), data)

    def put(self, local_file, key):
        self.objects[key] = Path(local_file).read_bytes()

    def delete(self, keys):
        for key in keys:
            self.objects.pop(key, None)

    def local_crc32c(self, path):
        return None


class FailingStore(FakeStore):
    def upload_tree(self, local_dir, prefix):
        raise RuntimeError('gcloud storage rsync failed (1): denied')


def build_run(tmp):
    runs = tmp / 'runs'
    run, phase = runs / 'run-x', runs / 'phase-b' / 'run-x'
    write(run / 'results.json', '{}')
    write(run / 'runtime.jsonl', '{"kind":"run_stop"}\n')
    write(run / 'conference' / 'state.sqlite', b'SQLite format 3\0')
    log = io.BytesIO()
    with zipfile.ZipFile(log, 'w') as archive:
        archive.writestr('header.json', '{"status": "success"}')
    eval_bytes = log.getvalue()
    write(run / 'inspect' / 'x.eval', eval_bytes)
    write(run / 'snapshots' / 'agent-001' / 'a.txt', 'note')
    write(run / 'analysis' / 'timeline.md', '# timeline\n')
    write(run / 'credentials.json', '{}')          # credential-named: never leaves the VM
    write(run / '.env', 'X=1')                      # dot-file: never leaves the VM
    write(phase / 'budget-final.sqlite', b'ledger')
    write(phase / 'pilot.log', 'ok')
    write(phase / 'supervisor-status.json', '{}')
    write(phase / 'gcs-upload.log', 'writing')
    write(phase / 'hf-upload.json', '{"outcome": "uploaded"}')
    write(phase / 'inspect-view' / 'index.html', '<html></html>')
    bundle = phase / 'site-bundle'
    write(bundle / 'archive-manifest.json', '{}')
    snap = io.BytesIO()
    with tarfile.open(fileobj=snap, mode='w:gz') as tar:
        info = tarfile.TarInfo('agent-001/a.txt')
        info.size = 4
        tar.addfile(info, io.BytesIO(b'note'))
    files = {'run-x/inspect/x.eval': eval_bytes, 'run-x/snapshots.tar.gz': snap.getvalue(),
             'run-x/runtime.jsonl': b'{"kind":"run_stop"}\n', 'run-x/analysis/timeline.md': b'# timeline\n',
             'run-x/resume/agents/agent-001/state.tar.gz': snap.getvalue()}
    manifest = {}
    for name, data in files.items():
        write(bundle / 'dataset-files' / name, data)
        manifest[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    write(bundle / 'dataset-files' / 'manifest.json', json.dumps({'files': manifest}))
    return run, phase


class GcsTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='upload-gcs-'))
        self.run, self.phase = build_run(self.tmp)
        self.store = FakeStore()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_run_with_ledgers_and_bundles_and_no_credentials(self):
        result = upload_run.upload_gcs(self.run, self.phase, store=self.store, stamp='20261119T000000Z',
                                       log=lambda m: None)
        self.assertEqual((result['schema'], result['outcome']), ('gdm-run-gcs-upload/1', 'uploaded'))
        self.assertEqual(result['bucket'], gcs_runs.BUCKET_LABEL)
        self.assertEqual([u['unit'] for u in result['units']], ['run-x', 'phase-b/run-x'])
        keys = set(self.store.objects)
        for key in ('runs/run-x/results.json', 'runs/run-x/inspect/x.eval', 'runs/run-x/snapshots/agent-001/a.txt',
                    'runs/run-x/conference/state.sqlite', 'runs/phase-b/run-x/budget-final.sqlite',
                    'runs/phase-b/run-x/site-bundle/dataset-files/run-x/resume/agents/agent-001/state.tar.gz',
                    'runs/phase-b/run-x/inspect-view/index.html', 'runs/phase-b/run-x/hf-upload.json',
                    'runs/run-x/GCS.json', 'runs/phase-b/run-x/GCS.json'):
            self.assertIn(key, keys)
        for key in ('runs/run-x/credentials.json', 'runs/run-x/.env', 'runs/phase-b/run-x/supervisor-status.json',
                    'runs/phase-b/run-x/gcs-upload.log'):
            self.assertNotIn(key, keys)
        self.assertEqual(len(result['manifest_keys']), 2)
        self.assertTrue((self.run / 'runtime.jsonl').exists())   # nothing deleted on the VM
        self.assertNotIn('gs://', json.dumps(result))

    def test_phase_only_resync_and_replace(self):
        upload_run.upload_gcs(self.run, self.phase, store=self.store, stamp='a', log=lambda m: None)
        write(self.phase / 'hf-upload.json', '{"outcome": "uploaded", "dataset_commit": "c"}')
        write(self.phase / 'gcs-upload.json', '{"outcome": "uploaded"}')
        result = upload_run.upload_gcs(self.run, self.phase, store=self.store, stamp='b', log=lambda m: None,
                                       phase_only=True)
        self.assertEqual([u['unit'] for u in result['units']], ['phase-b/run-x'])
        self.assertIn(b'dataset_commit', self.store.objects['runs/phase-b/run-x/hf-upload.json'])
        self.assertIn('runs/phase-b/run-x/gcs-upload.json', self.store.objects)

    def test_main_records_failure_and_exit_2(self):
        record = self.tmp / 'gcs-upload.json'
        with mock.patch.object(gcs_runs, 'GcloudStore', lambda *a, **k: FailingStore()):
            code = upload_run.main(['--target', 'gcs', '--run-dir', str(self.run), '--phase-dir', str(self.phase),
                                    '--record', str(record)])
        self.assertEqual(code, 2)
        data = json.loads(record.read_text())
        self.assertEqual((data['schema'], data['outcome']), ('gdm-run-gcs-upload/1', 'failed'))
        with mock.patch.object(gcs_runs, 'GcloudStore', lambda *a, **k: self.store):
            self.assertEqual(upload_run.main(['--target', 'gcs', '--run-dir', str(self.run), '--phase-dir',
                                              str(self.phase), '--record', str(record)]), 0)
        self.assertEqual(json.loads(record.read_text())['outcome'], 'uploaded')


class HfSubsetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='upload-hf-'))
        self.run, self.phase = build_run(self.tmp)
        self.dataset = FakeDataset()
        self.patches = [mock.patch('huggingface_hub.HfApi', self.dataset),
                        mock.patch.dict(offload_runs.hf_visibility.EXPECTED, {('dataset', REPO): (True, 'test')}),
                        mock.patch.dict(os.environ, {}, clear=False)]
        for patch in self.patches:
            patch.start()
        os.environ.pop('GDM_HF_CATEGORIES', None)

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def upload(self, bundle=True, categories=None):
        return upload_run.upload(self.run, self.phase, self.phase / 'site-bundle' if bundle else None, repo=REPO,
                                 api=self.dataset, categories=categories, stamp='upload-test', log=lambda m: None)

    def test_default_subset_only_viewable_files(self):
        result = self.upload()
        self.assertEqual(set(self.dataset.files),
                         {'run-x/inspect/x.eval', 'run-x/runtime.jsonl', 'run-x/analysis/timeline.md'})
        self.assertEqual(len(self.dataset.commits), 1)
        self.assertEqual(result['hf_categories'], sorted(dataset_logs.HF_SUBSET_DEFAULT))
        self.assertEqual(result['uploaded_categories'], ['eval', 'jsonl', 'timeline'])
        self.assertEqual(result['skipped_categories'], ['resume', 'snapshots'])
        self.assertEqual(result['left_out'], ['run-x/resume/agents/agent-001/state.tar.gz', 'run-x/snapshots.tar.gz'])
        self.assertFalse([p for p in self.dataset.files if 'OFFLOADED' in p or 'phase-b' in p or 'resume' in p])
        self.assertFalse((self.run / 'OFFLOADED.json').exists())
        # A rerun reuses the same bytes and makes no commit.
        again = self.upload()
        self.assertEqual((again['uploaded_files'], again['already_present_files']), (0, 3))
        self.assertEqual(len(self.dataset.commits), 1)

    def test_category_override_and_no_bundle_fallback(self):
        result = self.upload(categories={'eval'})
        self.assertEqual(set(self.dataset.files), {'run-x/inspect/x.eval'})
        self.assertEqual(result['hf_categories'], ['eval'])
        with mock.patch.dict(os.environ, {'GDM_HF_CATEGORIES': 'eval,timeline'}):
            self.assertEqual(dataset_logs.hf_categories(), {'eval', 'timeline'})
        self.dataset.files.clear()
        result = self.upload(bundle=False)
        self.assertEqual(set(self.dataset.files), {'run-x/inspect/x.eval'})
        self.assertIn('no site bundle', result['source'])

    def test_other_bytes_stop_the_upload(self):
        self.dataset.files['run-x/runtime.jsonl'] = b'other'
        with self.assertRaises(ValueError):
            self.upload()
        self.assertEqual(self.dataset.commits, [])

    def test_main_cli_hf_categories(self):
        record = self.tmp / 'hf-upload.json'
        with mock.patch.object(offload_runs.hf_visibility, 'DATASET_REPO', REPO):
            code = upload_run.main(['--run-dir', str(self.run), '--phase-dir', str(self.phase), '--site-bundle',
                                    str(self.phase / 'site-bundle'), '--hf-categories', 'eval,jsonl',
                                    '--record', str(record)])
        self.assertEqual(code, 0)
        data = json.loads(record.read_text())
        self.assertEqual((data['outcome'], data['hf_categories']), ('uploaded', ['eval', 'jsonl']))
        self.assertEqual(set(self.dataset.files), {'run-x/inspect/x.eval', 'run-x/runtime.jsonl'})
        self.assertIn('run-x/analysis/timeline.md', data['left_out'])


if __name__ == '__main__':
    unittest.main()
