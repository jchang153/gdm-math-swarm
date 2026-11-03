import ast
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from research_swarm.coding.dataset import CodingDataset, PrivateDataMissing, PRIVATE_MANIFEST

REPO = Path(__file__).resolve().parents[1]
HAS_PRIVATE = (REPO / PRIVATE_MANIFEST).exists()
LEAK = re.compile(r'\b(impossible[_ ]?(bench|livecodebench)|oneoff|one-off|conflicting|mutated|variant)\b', re.I)


def check_asserts(source):
    module = ast.parse(source)
    (func,) = module.body
    assert isinstance(func, ast.FunctionDef) and func.name == 'check'
    return [n for n in func.body if isinstance(n, ast.Assert)]


class PublicCodingDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = CodingDataset(REPO)

    def test_pool_shape(self):
        self.assertEqual(len(self.ds.tasks), 100)
        ids = [t.task_id for t in self.ds.tasks]
        self.assertEqual(len(set(ids)), 100)
        self.assertEqual(ids, sorted(ids, key=lambda t: int(t.split('_')[1])))
        for task in self.ds.tasks:
            self.assertRegex(task.task_id, r'^lcbhard_\d+$')
            self.assertTrue(task.entry_point.isidentifier())

    def test_files_parse_and_match(self):
        for task in self.ds.tasks:
            starter = ast.parse(task.starter)
            names = [n.name for n in starter.body if isinstance(n, ast.FunctionDef)]
            self.assertEqual(names, [task.entry_point], task.task_id)
            self.assertTrue(check_asserts(task.visible_tests), task.task_id)
            self.assertIn(f'`{task.entry_point}`', task.problem)

    def test_public_manifest_has_no_variant_labels(self):
        manifest = self.ds.manifest
        self.assertEqual(manifest['upstream']['dataset'], 'fjzzq2002/impossible_livecodebench')
        self.assertRegex(manifest['upstream']['revision'], r'^[0-9a-f]{40}$')
        self.assertEqual(manifest['selection_seed'], 260904170)
        for entry in manifest['tasks']:
            self.assertEqual(set(entry), {'task_id', 'title', 'entry_point', 'dir', 'files'})
            self.assertNotIn('variant', json.dumps(entry))

    def test_agent_visible_text_is_neutral(self):
        for task in self.ds.tasks:
            for text in (task.problem, task.starter):
                self.assertIsNone(LEAK.search(text), task.task_id)

    def test_export_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'problems'
            listing = self.ds.export_agent_tasks(out)
            self.assertEqual(json.loads((out / 'tasks.json').read_text()), listing)
            self.assertEqual(len(listing), 100)
            first = listing[0]
            self.assertEqual(set(first), {'task_id', 'title', 'entry_point', 'dir'})
            self.assertEqual(sorted(p.name for p in (out / first['dir']).iterdir()),
                             ['problem.md', 'solution.py', 'tests.py'])
            exported = {p.name for p in out.iterdir()}
            self.assertEqual(exported, {'tasks.json', *(t['task_id'] for t in listing)})
            for path in out.rglob('*'):
                self.assertNotIn('original_test', path.name)

    def test_hash_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(REPO / 'data' / 'coding', root / 'data' / 'coding')
            task = self.ds.tasks[0]
            path = root / 'data' / 'coding' / 'tasks' / task.task_id / 'tests.py'
            path.write_text(path.read_text() + '\n# edited\n')
            with self.assertRaisesRegex(ValueError, 'Hash mismatch'):
                CodingDataset(root)

    def test_private_missing_is_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(REPO / 'data' / 'coding', root / 'data' / 'coding')
            ds = CodingDataset(root)
            with self.assertRaises(PrivateDataMissing):
                ds.variant(ds.tasks[0].task_id)
            with self.assertRaises(PrivateDataMissing):
                ds.reference_tests(ds.tasks[0].task_id)

    def test_unknown_task_set(self):
        with self.assertRaises(ValueError):
            CodingDataset(REPO, task_set='all71')


@unittest.skipUnless(HAS_PRIVATE, 'researcher-only coding data not present')
class PrivateCodingDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = CodingDataset(REPO)

    def test_private_dir_is_git_ignored(self):
        import subprocess
        result = subprocess.run(['git', 'check-ignore', '-q', PRIVATE_MANIFEST], cwd=REPO)
        self.assertEqual(result.returncode, 0)

    def test_variant_counts(self):
        counts = {}
        for task in self.ds.tasks:
            counts[self.ds.variant(task.task_id)] = counts.get(self.ds.variant(task.task_id), 0) + 1
        self.assertEqual(counts, {'original': 33, 'oneoff': 34, 'conflicting': 33})

    def test_variant_structure(self):
        for task in self.ds.tasks:
            variant = self.ds.variant(task.task_id)
            reference = self.ds.reference_tests(task.task_id)
            ref, vis = check_asserts(reference), check_asserts(task.visible_tests)
            if variant == 'original':
                self.assertEqual(reference, task.visible_tests, task.task_id)
            elif variant == 'oneoff':
                self.assertEqual(len(ref), len(vis), task.task_id)
                diffs = [i for i, (a, b) in enumerate(zip(ref, vis)) if ast.dump(a) != ast.dump(b)]
                self.assertEqual(len(diffs), 1, task.task_id)
            else:
                self.assertNotEqual(reference, task.visible_tests, task.task_id)
                self.assertGreater(len(vis), len(ref) - 1, task.task_id)


if __name__ == '__main__':
    unittest.main()
