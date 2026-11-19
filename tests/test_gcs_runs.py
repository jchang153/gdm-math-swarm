"""scripts/gcs_runs.py and the GCS backend of scripts/offload_runs.py (offload --to gcs, restore --from gcs) against
an in-memory bucket and a fake gcloud runner (offline)."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import gcs_runs  # noqa: E402
import offload_runs  # noqa: E402


def md5_b64(data):
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())


class FakeStore:
    """An in-memory bucket with GcloudStore's methods; ``corrupt`` names keys whose stored bytes get damaged."""

    def __init__(self, corrupt=()):
        self.objects, self.corrupt, self.uploads = {}, set(corrupt), []

    def list(self, prefix):
        return {k[len(prefix):]: {'bytes': len(v), 'md5': md5_b64(v), 'crc32c': None}
                for k, v in self.objects.items() if k.startswith(prefix)}

    def upload_tree(self, local_dir, prefix):
        for path in Path(local_dir).rglob('*'):
            if path.is_file():
                key = prefix.rstrip('/') + '/' + path.relative_to(local_dir).as_posix()
                data = path.read_bytes()
                self.objects[key] = data + b'!' if key in self.corrupt else data
                self.uploads.append(key)

    def download_tree(self, prefix, local_dir, exclude=None):
        base = prefix.rstrip('/') + '/' if prefix else ''
        for key, data in self.objects.items():
            if key.startswith(base):
                rel = key[len(base):]
                if exclude and re.search(exclude, rel):
                    continue
                write(Path(local_dir) / rel, data)

    def put(self, local_file, key):
        self.objects[key] = Path(local_file).read_bytes()

    def delete(self, keys):
        for key in keys:
            self.objects.pop(key, None)

    def local_crc32c(self, path):
        return None


def build_run(runs):
    run = runs / 'run-a'
    write(run / 'results.json', '{"ok": true}')
    write(run / 'settings.json', '{}')
    write(run / 'runtime.jsonl', '{"kind": "x"}\n')
    log = io.BytesIO()
    with zipfile.ZipFile(log, 'w') as archive:
        archive.writestr('header.json', '{"status": "success"}')
    write(run / 'inspect' / 'a.eval', log.getvalue())
    write(run / 'conference' / 'state.sqlite', b'SQLite format 3\0')
    write(run / 'budget-final.sqlite', b'ledger')              # ledgers go to the bucket
    write(run / 'credentials.json', '{}')                       # never leaves
    write(run / '.env', 'X=1')                                  # never leaves
    write(run / 'leak.txt', 'key sk-ant-api03-' + 'A' * 90)     # fails the screen
    state = io.BytesIO()
    with tarfile.open(fileobj=state, mode='w:gz') as tar:
        for name, data in (('home/.agent/sessions/1.abc.key', b'k'), ('home/notes.md', b'n')):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    write(run / 'resume' / 'agents' / 'a1' / 'state.tar.gz', state.getvalue())
    return run


class GcsOffloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / 'runs'
        self.run = build_run(self.runs)
        (self.tmp / 'eval').write_bytes((self.run / 'inspect' / 'a.eval').read_bytes())
        self.store = FakeStore()

    def offload(self, **kw):
        return gcs_runs.offload_unit('run-a', runs_root=self.runs, store=self.store, log=lambda m: None,
                                     stamp='20261119T000000Z', **kw)

    def test_offload_uploads_verifies_and_keeps_only_the_index(self):
        result = self.offload()
        keys = set(self.store.objects)
        for rel in ('results.json', 'runtime.jsonl', 'inspect/a.eval', 'conference/state.sqlite',
                    'budget-final.sqlite', 'GCS.json'):
            self.assertIn('runs/run-a/' + rel, keys)
        for rel in ('credentials.json', '.env', 'leak.txt'):
            self.assertNotIn('runs/run-a/' + rel, keys)
        self.assertIn('runs/run-a/_gcs/manifest-20261119T000000Z.json.gz', keys)
        # the repacked resume archive went up without its key member; the original stays local
        repacked = self.store.objects['runs/run-a/resume/agents/a1/state.tar.gz']
        with tarfile.open(fileobj=io.BytesIO(repacked)) as tar:
            self.assertEqual(tar.getnames(), ['home/notes.md'])
        left = sorted(p.relative_to(self.run).as_posix() for p in self.run.rglob('*') if p.is_file())
        self.assertEqual(left, ['.env', 'GCS.json', 'credentials.json', 'leak.txt', 'results.json',
                                'resume/agents/a1/state.tar.gz', 'settings.json'])
        record = json.loads((self.run / 'GCS.json').read_text())
        self.assertEqual(record['bucket'], gcs_runs.BUCKET_LABEL)
        self.assertEqual(record['prefix'], 'runs/run-a/')
        self.assertEqual(result['conflicts'], [])
        self.assertGreater(result['freed_bytes'], 0)
        self.assertEqual(gcs_runs.pending('run-a', self.runs), 0)

    def test_phase_unit_key(self):
        self.assertEqual(gcs_runs.unit_key('phase-b/run-a', 'x.json'), 'runs/phase-b/run-a/x.json')
        for bad in ('../x', 'a/b', '.hidden', 'phase-b/a/b'):
            with self.assertRaises(ValueError):
                gcs_runs.unit_key(bad)

    def test_rerun_skips_identical_objects_and_conflicts_stay_local(self):
        self.offload(keep_all=True)
        first = len(self.store.uploads)
        self.offload(keep_all=True)
        self.assertEqual(len(self.store.uploads), first)   # identical objects are not uploaded again
        write(self.run / 'runtime.jsonl', 'changed\n')          # differs from the bucket: a conflict
        write(self.run / 'results.json', '{"ok": false}')       # index file: replaced
        result = self.offload()
        self.assertEqual([c['path'] for c in result['conflicts']], ['runtime.jsonl'])
        self.assertTrue((self.run / 'runtime.jsonl').is_file())
        self.assertEqual(self.store.objects['runs/run-a/results.json'], b'{"ok": false}')
        result = self.offload(replace=True)
        self.assertEqual(result['conflicts'], [])
        self.assertEqual(self.store.objects['runs/run-a/runtime.jsonl'], b'changed\n')
        self.assertFalse((self.run / 'runtime.jsonl').exists())

    def test_verification_failure_deletes_nothing(self):
        self.store.corrupt = {'runs/run-a/inspect/a.eval'}
        with self.assertRaises(ValueError):
            self.offload()
        self.assertTrue((self.run / 'inspect' / 'a.eval').is_file())
        self.assertFalse((self.run / 'GCS.json').exists())
        self.assertFalse((self.runs / gcs_runs.STAGE).exists())

    def test_dry_run_changes_nothing(self):
        result = self.offload(dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual(self.store.objects, {})
        self.assertTrue((self.run / 'runtime.jsonl').exists())

    def test_restore_full_prefix_and_verification(self):
        self.offload()
        dest = self.tmp / 'scratch' / 'run-a'
        count = gcs_runs.restore_unit('run-a', dest=dest, store=self.store, log=lambda m: None)
        self.assertEqual((dest / 'inspect' / 'a.eval').read_bytes(), (self.tmp / 'eval').read_bytes())
        self.assertEqual((dest / 'budget-final.sqlite').read_bytes(), b'ledger')
        self.assertFalse((dest / '_gcs').exists())
        self.assertGreaterEqual(count, 7)
        # in place, only missing files, under a prefix
        written = gcs_runs.restore_unit('run-a', runs_root=self.runs, prefix='inspect/', store=self.store,
                                        log=lambda m: None)
        self.assertEqual(written, 1)
        self.assertEqual(sorted(p.name for p in self.run.iterdir() if not p.name.startswith('.')),
                         ['GCS.json', 'credentials.json', 'inspect', 'leak.txt', 'results.json', 'resume',
                          'settings.json'])
        self.assertEqual(gcs_runs.restore_unit('run-a', runs_root=self.runs, prefix='runtime.jsonl',
                                               store=self.store, log=lambda m: None), 1)
        self.assertEqual(gcs_runs.restored_paths('run-a', self.run, store=self.store),
                         ['inspect/a.eval', 'runtime.jsonl'])
        # a damaged object is refused and nothing is written
        self.store.objects['runs/run-a/conference/state.sqlite'] = b'x'

        class Lying(FakeStore):
            def list(inner, prefix):
                rows = FakeStore.list(self.store, prefix)
                if 'conference/state.sqlite' in rows:
                    rows['conference/state.sqlite']['md5'] = md5_b64(b'y')
                return rows

            def download_tree(inner, *a, **k):
                return FakeStore.download_tree(self.store, *a, **k)
        with self.assertRaises(ValueError):
            gcs_runs.restore_unit('run-a', dest=self.tmp / 'other', store=Lying(), log=lambda m: None)
        self.assertFalse((self.tmp / 'other' / 'conference').exists())

    def test_cli_offload_and_restore_with_command_and_cleanup(self):
        args = argparse.Namespace(unit=['run-a'], all=False, keep_all=False, dry_run=False, replace=False,
                                  report=None)
        with mock.patch.object(offload_runs, 'RUNS', self.runs):
            self.assertEqual(offload_runs.offload_gcs_main(args, store=self.store, runs_root=self.runs), 0)
            self.assertFalse((self.run / 'runtime.jsonl').exists())
            seen = []

            def runner(command):
                seen.append((self.run / 'runtime.jsonl').read_text())
                write(self.run / 'analysis' / 'new.json', '{}')   # an output of CMD: offloaded afterwards
                return 3
            restore = argparse.Namespace(unit='run-a', prefix='', force=False, dest=None, cleanup=False,
                                         no_offload=False, run=['--', 'echo'])
            with mock.patch.object(gcs_runs, 'GcloudStore', lambda *a, **k: self.store), \
                    mock.patch.dict(os.environ, {'GDM_RUNS_BUCKET': 'gs://example-bucket'}):
                code = offload_runs.restore_gcs_and_run(restore, runner=runner, store=self.store,
                                                        runs_root=self.runs)
            self.assertEqual(code, 3)
            self.assertEqual(seen, ['{"kind": "x"}\n'])
            self.assertIn('runs/run-a/analysis/new.json', self.store.objects)
            self.assertFalse((self.run / 'runtime.jsonl').exists())
            # --cleanup: restore in place and delete the unchanged restored files again
            restore.cleanup, restore.run = True, []
            offload_runs.restore_gcs_and_run(restore, store=self.store, runs_root=self.runs)
            self.assertFalse((self.run / 'inspect' / 'a.eval').exists())
            self.assertTrue((self.run / 'results.json').exists())
            # --dest with a command: the folder is deleted afterwards
            dest = self.tmp / 'scratch'
            restore.cleanup, restore.dest, restore.run = False, str(dest), ['--', 'true']
            offload_runs.restore_gcs_and_run(restore, runner=lambda c: (dest / 'inspect' / 'a.eval').is_file() and 0,
                                             store=self.store, runs_root=self.runs)
            self.assertFalse(dest.exists())

    def test_restore_source_auto(self):
        with mock.patch.dict(os.environ, {'GDM_RUNS_BUCKET': 'gs://example-bucket'}):
            self.assertEqual(offload_runs.restore_source('run-a', 'auto', self.runs), 'gcs')
            write(self.run / 'OFFLOADED.json', '{}')
            self.assertEqual(offload_runs.restore_source('run-a', 'auto', self.runs), 'hf')
            write(self.run / 'GCS.json', '{}')
            self.assertEqual(offload_runs.restore_source('run-a', 'auto', self.runs), 'gcs')
        with mock.patch.dict(os.environ, {'GDM_RUNS_BUCKET': ''}), \
                mock.patch.object(gcs_runs, 'BUCKET_FILE', self.tmp / 'none'):
            self.assertEqual(offload_runs.restore_source('run-a', 'auto', self.runs), 'hf')
        self.assertEqual(offload_runs.restore_source('run-a', 'gcs', self.runs), 'gcs')

    def test_auto_offload_prefers_the_bucket(self):
        with mock.patch.dict(os.environ, {'GDM_RUNS_BUCKET': 'gs://example-bucket'}), \
                mock.patch.object(gcs_runs, 'GcloudStore', lambda *a, **k: self.store):
            status = offload_runs.auto_offload(self.run, label='test', runs_root=self.runs, log=lambda m: None)
        self.assertEqual((status['status'], status['backend']), ('offloaded', 'gcs'))
        self.assertIn('runs/run-a/inspect/a.eval', self.store.objects)
        self.assertFalse((self.run / 'inspect').exists())

    def test_all_units_skips_dot_folders_and_finished_units(self):
        write(self.runs / '.gcs-stage' / 'x' / 'f', 'x')
        write(self.runs / 'phase-b' / 'run-a' / 'pilot.log', 'log')
        self.assertEqual(gcs_runs.all_units(self.runs), ['run-a', 'phase-b/run-a'])
        self.offload()
        self.assertEqual(gcs_runs.all_units(self.runs), ['phase-b/run-a'])
        self.assertNotIn('.gcs-stage', offload_runs.all_units(self.runs))


class GcloudCommandTests(unittest.TestCase):
    """The gcloud storage commands and environment GcloudStore builds (fake runner)."""

    def test_commands_env_and_listing(self):
        calls = []

        def runner(args, **kw):
            calls.append((args, kw['env']))
            if args[2:4] == ['objects', 'list']:
                out = json.dumps([{'name': 'runs/run-a/x.json', 'size': 3, 'md5_hash': 'abc', 'crc32c_hash': 'c'},
                                  {'name': 'runs/run-a/sub/', 'size': 0}])
                return types.SimpleNamespace(returncode=0, stdout=out, stderr='')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.dict(os.environ, {'HF_TOKEN': 'x', 'ANTHROPIC_API_KEY': 'y', 'GCLOUD': '/bin/gcloud'}):
            store = gcs_runs.GcloudStore('gs://example-bucket/', runner=runner)
            self.assertEqual(store.list('runs/run-a/'), {'x.json': {'bytes': 3, 'md5': 'abc', 'crc32c': 'c'}})
            store.upload_tree('/tmp/stage', 'runs/run-a/')
        args, env = calls[0]
        self.assertEqual(args[:5], ['/bin/gcloud', 'storage', 'objects', 'list', 'gs://example-bucket/runs/run-a/**'])
        self.assertNotIn('HF_TOKEN', env)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertEqual(env['CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED'], 'False')
        self.assertEqual(calls[1][0][2:5], ['rsync', '/tmp/stage', 'gs://example-bucket/runs/run-a'])

    def test_no_match_is_empty_and_other_errors_raise(self):
        def runner(args, **kw):
            return types.SimpleNamespace(returncode=1, stdout='', stderr='ERROR: One or more URLs matched no objects.')
        self.assertEqual(gcs_runs.GcloudStore('gs://example-bucket', runner=runner).list('runs/x/'), {})

        def denied(args, **kw):
            return types.SimpleNamespace(returncode=1, stdout='', stderr='403 denied')
        with self.assertRaises(RuntimeError):
            gcs_runs.GcloudStore('gs://example-bucket', runner=denied).list('runs/x/')

    def test_bucket_url(self):
        with mock.patch.dict(os.environ, {'GDM_RUNS_BUCKET': 'gs://ok-bucket/sub/'}):
            self.assertEqual(gcs_runs.bucket_url(), 'gs://ok-bucket/sub')
        with mock.patch.dict(os.environ, {'GDM_RUNS_BUCKET': 'http://nope'}):
            with self.assertRaises(ValueError):
                gcs_runs.bucket_url()


if __name__ == '__main__':
    unittest.main()
