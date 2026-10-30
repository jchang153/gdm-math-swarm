"""Offline tests of the VM-to-dataset path: scripts/upload_run.py against a fake HF dataset, the stripped site
bundle accepted by publish_hf_library.load_dataset_files, and scripts/fetch_run.py's safe extraction and flow."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fetch_run  # noqa: E402
import offload_runs  # noqa: E402
import publish_hf_library  # noqa: E402
import upload_run  # noqa: E402

REPO = 'org/logs'


class FakeDataset:
    """An in-memory private dataset: commits add files, nothing else (stands in for HfApi)."""

    def __init__(self, private=True):
        self.files, self.commits, self.private = {}, [], private

    def __call__(self, *args, **kwargs):   # used as the HfApi class
        return self

    def repo_info(self, repo, repo_type=None):
        return types.SimpleNamespace(private=self.private, sha=f'{len(self.commits):040x}')

    def list_repo_tree(self, repo, repo_type=None, revision=None, path_in_repo='', recursive=True):
        for path, data in sorted(self.files.items()):
            if path.startswith(path_in_repo + '/'):
                yield types.SimpleNamespace(path=path, size=len(data), blob_id='x',
                                            lfs=types.SimpleNamespace(sha256=hashlib.sha256(data).hexdigest()))

    def preupload_lfs_files(self, repo, additions=(), repo_type=None):
        return None

    def create_commit(self, repo_id, repo_type, parent_commit, operations, commit_message):
        for op in operations:
            assert op.path_in_repo not in self.files, 'overwrite attempted: ' + op.path_in_repo
            self.files[op.path_in_repo] = Path(op.path_or_fileobj).read_bytes()
        self.commits.append(commit_message)
        return types.SimpleNamespace(oid=f'{len(self.commits):040x}')


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


class UploadRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='upload-run-'))
        self.runs = self.tmp / 'runs'
        self.run = self.runs / 'run-x'
        self.phase = self.runs / 'phase-b' / 'run-x'
        write(self.run / 'results.json', '{}')
        write(self.run / 'runtime.jsonl', '{"kind":"run_stop"}\n')
        write(self.run / 'conference' / 'state.sqlite', b'SQLite format 3\0')
        log = io.BytesIO()
        with zipfile.ZipFile(log, 'w') as archive:
            archive.writestr('header.json', '{"status": "success"}')
        write(self.run / 'inspect' / 'x.eval', log.getvalue())
        write(self.run / 'snapshots' / 'agent-001' / 'a.txt', 'note')
        state = io.BytesIO()
        with tarfile.open(fileobj=state, mode='w:gz') as tar:
            for name, data in (('home/.agent/sessions/1.abc.key', b'k'), ('home/notes.md', b'n')):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        write(self.run / 'resume' / 'agents' / 'agent-001' / 'state.tar.gz', state.getvalue())
        write(self.phase / 'budget-final.sqlite', b'ledger')
        write(self.phase / 'pilot.log', 'ok')
        write(self.phase / 'completed-phase.tgz', b'packaged')
        write(self.phase / 'supervisor-status.json', '{}')
        bundle = self.phase / 'site-bundle'
        write(bundle / 'archive-manifest.json', '{}')
        eval_bytes = (self.run / 'inspect' / 'x.eval').read_bytes()
        write(bundle / 'dataset-files' / 'run-x' / 'inspect' / 'x.eval', eval_bytes)
        snap = io.BytesIO()
        with tarfile.open(fileobj=snap, mode='w:gz') as tar:
            info = tarfile.TarInfo('agent-001/a.txt')
            info.size = 4
            tar.addfile(info, io.BytesIO(b'note'))
        snap = snap.getvalue()
        write(bundle / 'dataset-files' / 'run-x' / 'snapshots.tar.gz', snap)
        write(bundle / 'dataset-files' / 'manifest.json', json.dumps({'files': {
            'run-x/inspect/x.eval': {'bytes': len(eval_bytes), 'sha256': hashlib.sha256(eval_bytes).hexdigest()},
            'run-x/snapshots.tar.gz': {'bytes': len(snap), 'sha256': hashlib.sha256(snap).hexdigest()}}}))
        self.snap = snap
        self.dataset = FakeDataset()
        self.patches = [mock.patch('huggingface_hub.HfApi', self.dataset),
                        mock.patch.dict(offload_runs.hf_visibility.EXPECTED, {('dataset', REPO): (True, 'test')})]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def upload(self):
        return upload_run.upload(self.run, self.phase, self.phase / 'site-bundle', repo=REPO, api=self.dataset,
                                 work=self.tmp / 'work', stamp='upload-test', log=lambda m: None)

    def test_whole_run_uploaded_in_one_data_commit_and_verified(self):
        result = self.upload()
        self.assertEqual(result['outcome'], 'uploaded')
        self.assertEqual(len(self.dataset.commits), 2)   # data, then the manifests
        files = self.dataset.files
        # The site bundle's dataset files at their exact paths (pages pinned to the commit link them).
        self.assertIn('run-x/inspect/x.eval', files)
        self.assertEqual(files['run-x/snapshots.tar.gz'], self.snap)
        self.assertIn('run-x/upload-test/OFFLOADED.json', files)
        self.assertIn('phase-b/run-x/upload-test/OFFLOADED.json', files)
        manifest = json.loads((self.run / 'OFFLOADED.json').read_text())
        recorded = {r['path'] for r in manifest['files']}
        self.assertTrue({'results.json', 'runtime.jsonl', 'conference/state.sqlite', 'inspect/x.eval',
                         'snapshots/agent-001/a.txt'} <= recorded)
        # Nothing is deleted on the VM.
        self.assertTrue((self.run / 'runtime.jsonl').exists() and (self.run / 'snapshots').is_dir())
        # The resume archive goes up only as a copy without the agents' session keys.
        excluded = manifest['excluded_by_screen']
        self.assertEqual(excluded[0]['repacked_without'], ['home/.agent/sessions/1.abc.key'])
        repacked = files[excluded[0]['repacked_dataset_path']]
        with tarfile.open(fileobj=io.BytesIO(repacked)) as tar:
            self.assertEqual(tar.getnames(), ['home/notes.md'])
        # Ledgers, packaged tarballs, the site bundle and live files never go to the dataset.
        members = set()
        for path, data in files.items():
            if path.endswith('.tar.gz') and '/upload-test/' in path:
                with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                    members |= {f'{path}:{m}' for m in tar.getnames()}
        self.assertFalse([m for m in [*files, *members] if 'budget' in m or 'completed-' in m or 'status' in m])
        phase = json.loads((self.phase / 'OFFLOADED.json').read_text())
        self.assertEqual({r['path'] for r in phase['files']}, {'pilot.log'})
        reasons = {r['path']: r['reason'] for r in phase['local_only']}
        self.assertIn('budget ledger', reasons['budget-final.sqlite'])
        for name in ('completed-phase.tgz', 'supervisor-status.json', 'site-bundle/archive-manifest.json'):
            self.assertIn(name, reasons)

    def test_never_overwrites_and_refuses_a_public_dataset(self):
        self.dataset.files['run-x/snapshots.tar.gz'] = b'other bytes'
        with self.assertRaises(ValueError):
            self.upload()
        self.assertEqual(self.dataset.commits, [])
        self.dataset.files.clear()
        self.dataset.private = False
        with self.assertRaises(ValueError):
            self.upload()

    def test_main_records_failure_without_a_dataset_name(self):
        record = self.tmp / 'hf-upload.json'
        with mock.patch.object(offload_runs.hf_visibility, 'DATASET_REPO', 'HF-ORG/LOGS-DATASET'):
            code = upload_run.main(['--run-dir', str(self.run), '--phase-dir', str(self.phase), '--record',
                                    str(record), '--work', str(self.tmp / 'w')])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(record.read_text())['outcome'], 'failed')


class StrippedBundleTests(unittest.TestCase):
    def test_missing_dataset_files_need_the_remote_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'dataset-files'
            data = b'{"kind": "x"}\n'
            info = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'run': 'run-x',
                    'sources': ['runtime.jsonl']}
            write(root / 'manifest.json', json.dumps({'schema': publish_hf_library.DATASET_SCHEMA,
                                                      'files': {'run-x/runtime.jsonl': info}}))
            with self.assertRaises(ValueError):
                publish_hf_library.load_dataset_files(folder, ['run-x'])
            seen = []
            files = publish_hf_library.load_dataset_files(folder, ['run-x'],
                                                          lambda *a: seen.append(a) or True)
            self.assertEqual(list(files), ['run-x/runtime.jsonl'])
            self.assertEqual(seen, [('run-x/runtime.jsonl', info['bytes'], info['sha256'])])
            with self.assertRaises(ValueError):
                publish_hf_library.load_dataset_files(folder, ['run-x'], lambda *a: False)


class FetchRunTests(unittest.TestCase):
    def tar(self, path, members):
        with tarfile.open(path, 'w:gz') as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    def test_safe_extract_refuses_escapes_and_keeps_different_files(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            self.tar(folder / 'bad.tgz', {'runs/run-x/../../evil': b'x'})
            with self.assertRaises(ValueError):
                fetch_run.safe_extract(folder / 'bad.tgz', folder / 'out', strip='runs/run-x')
            self.tar(folder / 'other.tgz', {'runs/other/a': b'x'})
            with self.assertRaises(ValueError):
                fetch_run.safe_extract(folder / 'other.tgz', folder / 'out', strip='runs/run-x')
            self.tar(folder / 'ok.tgz', {'runs/run-x/results.json': b'{}', 'runs/run-x/conference/e.jsonl': b'1'})
            write(folder / 'out' / 'results.json', b'local')
            written, _, kept = fetch_run.safe_extract(folder / 'ok.tgz', folder / 'out', strip='runs/run-x')
            self.assertEqual((written, kept), (['conference/e.jsonl'], ['results.json']))
            self.assertEqual((folder / 'out' / 'results.json').read_bytes(), b'local')

    def test_fetch_verifies_extracts_core_and_touches_the_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            remote = folder / 'vm'
            remote.mkdir()
            self.tar(remote / 'completed-core.tgz', {'runs/run-q/results.json': b'{}'})
            self.tar(remote / 'completed-phase.tgz', {'budget-final.sqlite': b'ledger', 'pilot.log': b'ok'})
            self.tar(remote / 'completed-site.tgz', {'site-bundle/archive-manifest.json': b'{}'})
            sums = {n: fetch_run.sha256(remote / n) for n in fetch_run.PACKAGES}
            (remote / 'completed-checksums.json').write_text(json.dumps(sums))
            (remote / 'hf-upload.json').write_text(json.dumps({'outcome': 'uploaded', 'dataset_commit': 'a' * 40}))
            (remote / 'OFFLOADED.json').write_text(json.dumps({'dataset_commit': 'a' * 40, 'files': []}))
            calls = []

            class Cloud:
                vm = 'vm-1'

                def scp(self, paths, destination):
                    for p in paths:
                        (Path(destination) / Path(p).name).write_bytes((remote / Path(p).name).read_bytes())

                def ssh(self, command):
                    calls.append(command)

            def runner(command, **kwargs):
                calls.append(command)
                return types.SimpleNamespace(returncode=0, stdout='', stderr='')
            local = folder / 'repo'
            with mock.patch.object(fetch_run, 'ROOT', local):
                record = fetch_run.fetch('run-q', Cloud(), runner=runner, log=lambda m: None)
            self.assertEqual((local / 'runs' / 'run-q' / 'results.json').read_bytes(), b'{}')
            self.assertEqual((local / 'runs' / 'phase-b' / 'run-q' / 'budget-final.sqlite').read_bytes(), b'ledger')
            self.assertEqual(record['publication']['outcome'], 'previewed')
            self.assertEqual(json.loads((local / 'runs' / 'run-q' / 'OFFLOADED.json').read_text())['dataset_commit'],
                             'a' * 40)
            publish = next(c for c in calls if isinstance(c, list) and 'publish' in c)
            self.assertIn('--dataset-commit', publish)
            self.assertNotIn('--commit', publish)
            self.assertIn('touch gdm-math-swarm/runs/phase-b/run-q/retrieved.marker', calls)
            # The scratch bundle is gone; only the core and records stay.
            self.assertFalse(any(p.name == 'site-bundle' for p in local.rglob('*')))
            # A tampered package is refused.
            (remote / 'completed-checksums.json').write_text(json.dumps({**sums, 'completed-core.tgz': '0' * 64}))
            with mock.patch.object(fetch_run, 'ROOT', local), self.assertRaises(ValueError):
                fetch_run.fetch('run-q', Cloud(), runner=runner, log=lambda m: None)


if __name__ == '__main__':
    unittest.main()


class SiteBundleResumeArchiveTests(unittest.TestCase):
    """The site bundle's dataset copy of a resume archive leaves out the agents' session keys, so the VM upload's
    credential screen passes (first 180-minute run under the VM upload, 2026-10-30)."""

    def archive(self, members):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as tar:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return raw.getvalue()

    def test_relocated_resume_archive_has_no_credential_members(self):
        import prepare_hf_library
        data = self.archive([('home/.agent/sessions/1.abc.key', b'k'), ('home/notes.md', b'n')])
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            target = out / 'run-1' / 'resume' / 'agents' / 'agent-001' / 'state.tar.gz'
            target.parent.mkdir(parents=True)
            target.write_bytes(data)
            files, _, moved = prepare_hf_library.relocate_dataset_files(out, ['run-1'])
            (name, record), = files.items()
            self.assertEqual(record['without_credential_members'], 1)
            stored = out / prepare_hf_library.DATASET_DIR / name
            self.assertEqual(offload_runs.scan_members(stored), [])
            self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), record['sha256'])
            with tarfile.open(stored) as tar:
                self.assertEqual(tar.getnames(), ['home/notes.md'])
            upload_run.screen_extra({name: stored})  # no longer refused
        clean = self.archive([('home/notes.md', b'n')])
        self.assertEqual(prepare_hf_library.without_credential_members('resume/agents/a/state.tar.gz', clean),
                         (clean, []))
