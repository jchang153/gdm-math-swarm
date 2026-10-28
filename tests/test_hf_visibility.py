"""The one visibility record (scripts/hf_visibility.py) and the tooling's promise never to make anything public.

docs/plan.md, "Public results Space, private logs dataset (October 24, 2026)". Offline: source inspection only.
"""
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import hf_visibility  # noqa: E402

SOURCES = sorted([*(ROOT / 'scripts').rglob('*.py'), *(ROOT / 'src').rglob('*.py')])


def code(path):
    return path.read_text(encoding='utf-8')


class RecordTests(unittest.TestCase):
    def test_space_public_dataset_private(self):
        self.assertFalse(hf_visibility.expected_private('gdm-math-swarm/gdm-math-swarm-experiments', 'space'))
        self.assertTrue(hf_visibility.expected_private(hf_visibility.DATASET_REPO, 'dataset'))
        self.assertIn('2026-10-24', hf_visibility.EXPECTED[('space', hf_visibility.SPACE_REPO)][1])
        self.assertTrue(hf_visibility.expected_private('anyone/unrecorded', 'space'))  # fail closed

    def test_check_compares_actual_with_recorded(self):
        space, dataset = hf_visibility.SPACE_REPO, hf_visibility.DATASET_REPO
        hf_visibility.check(SimpleNamespace(private=False), space, 'space')
        hf_visibility.check(SimpleNamespace(private=True), dataset, 'dataset')
        for info, repo, kind in ((SimpleNamespace(private=True), space, 'space'),
                                 (SimpleNamespace(private=False), dataset, 'dataset'),
                                 (SimpleNamespace(private=None), space, 'space'),
                                 (SimpleNamespace(), dataset, 'dataset')):
            with self.subTest(repo=repo, private=getattr(info, 'private', 'missing')):
                with self.assertRaisesRegex(ValueError, 'update scripts/hf_visibility.py'):
                    hf_visibility.check(info, repo, kind)
        self.assertTrue(hf_visibility.unchanged(SimpleNamespace(private=False, sha='a'), space, 'space', 'a'))
        self.assertFalse(hf_visibility.unchanged(SimpleNamespace(private=True, sha='a'), space, 'space', 'a'))
        self.assertFalse(hf_visibility.unchanged(SimpleNamespace(private=False, sha='b'), space, 'space', 'a'))

    def test_the_only_record_is_hf_visibility(self):
        """Every guard compares through hf_visibility: no other module tests ``.private`` itself."""
        offenders = []
        for path in SOURCES:
            if path.name == 'hf_visibility.py':
                continue
            for number, line in enumerate(code(path).splitlines(), 1):
                if re.search(r'\bnot\s+[\w.()\[\]\']+\.private\b|\.private\s+(?:is|==|!=)\s', line):
                    offenders.append(f'{path.relative_to(ROOT)}:{number}: {line.strip()}')
        self.assertEqual(offenders, [])


class NeverMakePublicTests(unittest.TestCase):
    def test_no_visibility_change_or_public_creation(self):
        offenders = []
        for path in SOURCES:
            text = code(path)
            for pattern in (r'\bupdate_repo_settings\s*\(', r'\bupdate_repo_visibility\s*\(',
                            r'\bprivate\s*=\s*False\b', r'["\']private["\']\s*:\s*False\b',
                            r'\bduplicate_space\s*\(', r'\bmove_repo\s*\('):
                for match in re.finditer(pattern, text):
                    line = text.count('\n', 0, match.start()) + 1
                    offenders.append(f'{path.relative_to(ROOT)}:{line}: {pattern}')
        self.assertEqual(offenders, [])

    def test_any_create_repo_creates_private(self):
        for path in SOURCES:
            text = code(path)
            for match in re.finditer(r'\bcreate_repo\s*\(', text):
                call = text[match.end():match.end() + 400]
                with self.subTest(path=str(path.relative_to(ROOT))):
                    self.assertRegex(call, r'\bprivate\s*=\s*True\b')

    def test_every_space_commit_goes_through_the_gate(self):
        """Every Space commit is publish_hf_library.space_commit (dataset visibility check + space_gate); the only
        direct create_commit calls left are the gate itself and dataset commits."""
        direct = []
        for path in SOURCES:
            text = code(path)
            for match in re.finditer(r'\.create_commit\s*\(', text):
                call = text[match.end():match.end() + 200]
                line = text.count('\n', 0, match.start()) + 1
                if path.name == 'publish_hf_library.py' and call.startswith('**commit)'):
                    continue  # space_commit itself
                if re.match(r"\s*repo_id=[\w.]+,\s*repo_type='dataset'", call):
                    continue
                direct.append(f'{path.relative_to(ROOT)}:{line}')
        self.assertEqual(direct, [])


if __name__ == '__main__':
    unittest.main()
