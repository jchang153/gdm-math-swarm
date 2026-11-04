"""scripts/offload_runs.py: what stays local, what never leaves, deterministic archives and repacking (offline)."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import offload_runs as o  # noqa: E402


class OffloadRules(unittest.TestCase):
    def test_run_core_stays_local(self):
        for path in ('conference/state.sqlite', 'evaluations/x.json', 'results.json', 'settings.json'):
            self.assertTrue(o.keep_locally('RUN', path, 1000), path)
        for path in ('runtime.jsonl', 'snapshots/a/b.txt', 'inspect/x.eval', 'resume/manifest.json'):
            self.assertFalse(o.keep_locally('RUN', path, 1000), path)
        self.assertFalse(o.keep_locally('RUN', 'big.json', 2 * o.SMALL))

    def test_never_uploaded(self):
        for path in ('a/.env', 'x/.credentials.json', 'budget-final.sqlite', 'budget-judge-ledger.sqlite',
                     'k/id_rsa', 'cert.pem', '.evaluate_run.lock', 'x/__pycache__/m.pyc', 'budget.sqlite'):
            self.assertIsNotNone(o.local_only(path), path)
        for path in ('conference/state.sqlite', 'runtime.jsonl', 'resume/agents/a/state.tar.gz'):
            self.assertIsNone(o.local_only(path), path)

    def test_archive_is_deterministic_and_repack_drops_key_members(self):
        with tempfile.TemporaryDirectory() as folder:
            unit = Path(folder) / 'unit'
            (unit / 'd').mkdir(parents=True)
            (unit / 'd' / 'a.txt').write_bytes(b'alpha')
            members = [('d/a.txt', hashlib.sha256(b'alpha').hexdigest(), 5)]
            o.build_archive(unit, members, Path(folder) / 'one.tar.gz')
            o.build_archive(unit, members, Path(folder) / 'two.tar.gz')
            self.assertEqual((Path(folder) / 'one.tar.gz').read_bytes(), (Path(folder) / 'two.tar.gz').read_bytes())
            source = Path(folder) / 'state.tar.gz'
            with tarfile.open(source, 'w:gz') as tar:
                for name, data in (('home/.agent/sessions/1.abc.key', b'k'), ('home/notes.md', b'n')):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            self.assertTrue(any('credential file name' in p for p in o.scan_members(source)))
            skipped = o.repack(source, Path(folder) / 'repacked.tar.gz')
            self.assertEqual(skipped, ['home/.agent/sessions/1.abc.key'])
            self.assertEqual(o.scan_members(Path(folder) / 'repacked.tar.gz'), [])

    def test_secret_scan_spans_chunks(self):
        key = b'sk-' + b'A' * 40
        data = b'x' * (o.CHUNK - 10) + b' ' + key + b' '
        self.assertTrue(o.scan_stream(io.BytesIO(data))[2])
        self.assertFalse(o.scan_stream(io.BytesIO(b'plain text'))[2])


class AutoOffload(unittest.TestCase):
    """Owner, November 3, 2026: offload after the judge, the story map and restore-and-run (offline fakes)."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.runs = Path(self.folder.name) / 'runs'
        self.run = self.runs / 'RUN'
        (self.run / 'conference').mkdir(parents=True)
        (self.run / 'conference' / 'state.sqlite').write_bytes(b'c')
        (self.run / 'results.json').write_text('{}')
        (self.run / 'analysis' / 'storymap').mkdir(parents=True)
        (self.run / 'analysis' / 'storymap' / 'map.jsonl').write_bytes(b'x' * 100)
        (self.run / 'OFFLOADED.json').write_text('{"files": []}')
        self.calls = []
        self.env = os.environ.get('GDM_LOGS_DATASET')
        os.environ['GDM_LOGS_DATASET'] = 'org/logs'

    def tearDown(self):
        self.folder.cleanup()
        if self.env is None:
            os.environ.pop('GDM_LOGS_DATASET', None)
        else:
            os.environ['GDM_LOGS_DATASET'] = self.env

    def fake_offload(self, unit, **kwargs):
        self.calls.append((unit, kwargs['repo']))
        return {'unit': unit, 'commit': 'abc', 'freed_bytes': 100, 'offloaded': 1}

    def auto(self, **kwargs):
        return o.auto_offload(self.run, label='test', runs_root=self.runs, offload=self.fake_offload,
                              log=lambda m: None, **kwargs)

    def test_units_and_pending_bytes(self):
        self.assertEqual(o.unit_of(self.run, self.runs), 'RUN')
        self.assertEqual(o.unit_of(self.runs / 'phase-b' / 'X', self.runs), 'phase-b/X')
        self.assertIsNone(o.unit_of(self.run / 'analysis', self.runs))
        self.assertIsNone(o.unit_of(Path(self.folder.name), self.runs))
        self.assertEqual(o.pending_bytes('RUN', self.runs), 100)   # the core does not count
        self.assertEqual(o.all_units(self.runs), ['RUN'])
        (self.run / 'resume').mkdir()
        (self.run / 'resume' / 'state.tar.gz').write_bytes(b'k' * 10)   # kept local by the credential screen
        (self.run / 'OFFLOADED.json').write_text(json.dumps(
            {'files': [], 'excluded_by_screen': [{'path': 'resume/state.tar.gz', 'bytes': 10}]}))
        self.assertEqual(o.pending_bytes('RUN', self.runs), 100)

    def test_offloads_after_a_step(self):
        result = self.auto()
        self.assertEqual(result['status'], 'offloaded')
        self.assertEqual(self.calls, [('RUN', 'org/logs')])

    def test_skip_flag_and_missing_dataset_name(self):
        self.assertEqual(self.auto(skip=True)['status'], 'skipped')
        os.environ.pop('GDM_LOGS_DATASET')
        with mock.patch.object(o.hf_visibility, 'DATASET_REPO', 'HF-ORG/LOGS-DATASET'):
            self.assertEqual(self.auto()['status'], 'no_dataset')
        self.assertEqual(self.calls, [])

    def test_core_only_run_is_left_alone(self):
        (self.run / 'analysis' / 'storymap' / 'map.jsonl').unlink()
        self.assertEqual(self.auto()['status'], 'nothing_to_offload')
        self.assertEqual(o.all_units(self.runs), [])

    def test_deferred_while_another_step_holds_a_lease_and_stale_leases_vanish(self):
        with o.lease(self.run, 'story map'):
            result = self.auto()
            self.assertEqual(result['status'], 'deferred')
            self.assertEqual(o.all_units(self.runs), [])   # --all skips runs in use
        self.assertFalse((self.run / o.LEASE_DIR).exists())
        (self.run / o.LEASE_DIR).mkdir()
        (self.run / o.LEASE_DIR / 'dead.json').write_text(json.dumps(
            {'label': 'gone', 'pid': 2 ** 22 + 12345, 'host': o.socket.gethostname()}))
        self.assertEqual(o.live_leases(self.run), [])
        self.assertEqual(self.auto()['status'], 'offloaded')

    def test_failure_is_reported_not_raised(self):
        def broken(unit, **kwargs):
            raise ValueError('network down')
        result = o.auto_offload(self.run, label='t', runs_root=self.runs, offload=broken, log=lambda m: None)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue((self.run / 'analysis' / 'storymap' / 'map.jsonl').exists())

    def test_restore_and_run(self):
        restored, ran = [], []
        args = argparse.Namespace(unit='RUN', prefix='inspect/', force=False, cache=None, no_offload=False,
                                  dest=None, run=['--', 'echo', 'hi'])
        with mock.patch.object(o, 'RUNS', self.runs), \
                mock.patch.object(o, 'restore', lambda *a: restored.append(a)), \
                mock.patch.object(o, 'auto_offload', lambda d, **k: self.calls.append((Path(d).name, k['skip']))):
            def runner(command):
                ran.append((command, o.live_leases(self.run)))
                return 7
            self.assertEqual(o.restore_and_run(args, runner=runner), 7)
            self.assertEqual(ran[0][0], ['echo', 'hi'])
            self.assertEqual(len(ran[0][1]), 1)   # the command runs under a lease
            self.assertEqual(self.calls, [('RUN', False)])   # then the run is offloaded again
            dest = Path(self.folder.name) / 'scratch'
            dest.mkdir()
            args.dest, args.run = str(dest), ['true']
            o.restore_and_run(args, runner=lambda c: 0)
            self.assertFalse(dest.exists())   # a scratch restore is deleted afterwards
            self.assertEqual(len(self.calls), 1)


class StoryMapWrapper(unittest.TestCase):
    def test_steps_then_offload_and_no_offload(self):
        import run_storymap
        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder) / 'runs'
            run = runs / 'RUN'
            run.mkdir(parents=True)
            order = []

            def step(name, code=0):
                def call(argv):
                    order.append((name, list(argv)))
                    return code
                return call
            steps = {'restore': lambda d, log: order.append(('restore', [])), 'extract': step('extract'),
                     'pipeline': step('pipeline'), 'validate': step('validate'), 'view': step('view')}
            offloads = []
            with mock.patch.object(o, 'RUNS', runs), \
                    mock.patch.object(o, 'auto_offload', lambda d, **k: offloads.append(k['skip'])):
                self.assertEqual(run_storymap.main(['--run-dir', str(run), '--run-limit', '75'], steps=steps), 0)
                self.assertEqual([n for n, _ in order], ['restore', 'extract', 'pipeline', 'validate', 'view'])
                self.assertIn('--run-limit', order[2][1])
                self.assertEqual(offloads, [False])
                steps['pipeline'] = step('pipeline', 3)   # stopped: not offloaded
                self.assertEqual(run_storymap.main(['--run-dir', str(run)], steps=steps), 3)
                self.assertEqual(offloads, [False])
                steps['pipeline'] = step('pipeline')
                run_storymap.main(['--run-dir', str(run), '--no-offload'], steps=steps)
                self.assertEqual(offloads, [False, True])


class JudgeFlag(unittest.TestCase):
    def test_simple_judge_offloads_by_default(self):
        import run_simple_judge
        self.assertFalse(run_simple_judge.parse_args(['--run-dir', 'runs/X']).no_offload)
        self.assertTrue(run_simple_judge.parse_args(['--run-dir', 'runs/X', '--no-offload']).no_offload)


if __name__ == '__main__':
    unittest.main()
