"""scripts/offload_runs.py: what stays local, what never leaves, deterministic archives and repacking (offline)."""
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
