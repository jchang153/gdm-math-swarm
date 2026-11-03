"""Frozen coding pool (task set ilcb100, Impossible-LiveCodeBench): load, hash-check and export.

Public files (data/coding/) hold what agents may see: statement, starter and the visible tests.
The variant of each task and the original (reference) tests are researcher-only and live under
the Git-ignored private/data/coding/; only `variant` and `reference_tests` read them.
Built by scripts/prepare_coding_dataset.py.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

CODING_TASK_SET = 'ilcb100'
CODING_TASK_SETS = (CODING_TASK_SET,)
PUBLIC_MANIFEST = 'data/coding/manifest.json'
PRIVATE_MANIFEST = 'private/data/coding/manifest.private.json'
TASK_FILES = ('problem.md', 'solution.py', 'tests.py')
VARIANTS = ('original', 'oneoff', 'conflicting')
POOL_SIZE = 100


class PrivateDataMissing(FileNotFoundError):
    """Researcher-only coding data (private/data/coding) is not present on this host."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_checked(repo: Path, relative: str, expected: str) -> str:
    path = (repo / relative).resolve()
    if not path.is_relative_to(repo):
        raise ValueError(f'{relative} escapes the repository')
    data = path.read_bytes()
    if _sha256(data) != expected:
        raise ValueError(f'Hash mismatch: {relative}')
    return data.decode('utf-8')


@dataclass(frozen=True)
class CodingTask:
    task_id: str
    title: str
    entry_point: str
    problem: str        # problem.md text
    starter: str        # solution.py starter text
    visible_tests: str  # tests.py text (what agents see and the operational judge runs)


class CodingDataset:
    def __init__(self, repo_root: str | Path, task_set: str = CODING_TASK_SET):
        if task_set not in CODING_TASK_SETS:
            raise ValueError(f'task_set {task_set!r} is not one of {list(CODING_TASK_SETS)}')
        self.task_set = task_set
        self.repository = Path(repo_root).resolve()
        raw = (self.repository / PUBLIC_MANIFEST).read_bytes()
        self.manifest_sha256 = _sha256(raw)
        self.manifest = json.loads(raw)
        if self.manifest.get('task_set') != task_set:
            raise ValueError(f'{PUBLIC_MANIFEST} is not task set {task_set}')
        tasks = []
        for entry in self.manifest['tasks']:
            texts = {name: _read_checked(self.repository, entry['files'][name]['path'],
                                         entry['files'][name]['sha256']) for name in TASK_FILES}
            tasks.append(CodingTask(task_id=entry['task_id'], title=entry['title'],
                                    entry_point=entry['entry_point'], problem=texts['problem.md'],
                                    starter=texts['solution.py'], visible_tests=texts['tests.py']))
        ids = [t.task_id for t in tasks]
        if len(tasks) != POOL_SIZE or len(set(ids)) != len(ids):
            raise ValueError(f'Expected {POOL_SIZE} distinct coding tasks, found {len(set(ids))}')
        self.tasks: list[CodingTask] = tasks
        self._by_id = {t.task_id: t for t in tasks}
        self._private: dict | None = None

    def task(self, task_id: str) -> CodingTask:
        return self._by_id[task_id]

    def export_agent_tasks(self, destination: str | Path) -> list[dict]:
        """Write the agent workspace problems/ tree; public files only (never variant or reference)."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        listing = []
        for task in self.tasks:
            task_dir = destination / task.task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / 'problem.md').write_text(task.problem, encoding='utf-8')
            (task_dir / 'solution.py').write_text(task.starter, encoding='utf-8')
            (task_dir / 'tests.py').write_text(task.visible_tests, encoding='utf-8')
            listing.append(dict(task_id=task.task_id, title=task.title,
                                entry_point=task.entry_point, dir=task.task_id))
        (destination / 'tasks.json').write_text(json.dumps(listing, indent=2, ensure_ascii=False) + '\n',
                                                encoding='utf-8')
        return listing

    # Researcher-only accessors (private/, Git-ignored; never exported to agents).
    def _private_manifest(self) -> dict:
        if self._private is None:
            path = self.repository / PRIVATE_MANIFEST
            if not path.exists():
                raise PrivateDataMissing(
                    f'{PRIVATE_MANIFEST} is missing: researcher-only coding data is not on this host '
                    '(rebuild it with scripts/prepare_coding_dataset.py RAW_DIR)')
            private = json.loads(path.read_text())
            if private.get('public_manifest_sha256') != self.manifest_sha256:
                raise ValueError(f'{PRIVATE_MANIFEST} does not match {PUBLIC_MANIFEST}')
            self._private = {t['task_id']: t for t in private['tasks']}
        return self._private

    def _private_entry(self, task_id: str) -> dict:
        if task_id not in self._by_id:
            raise KeyError(task_id)
        return self._private_manifest()[task_id]

    def variant(self, task_id: str) -> str:
        """'original', 'oneoff' or 'conflicting' (researcher-only)."""
        value = self._private_entry(task_id)['variant']
        if value not in VARIANTS:
            raise ValueError(f'Unknown variant {value!r} for {task_id}')
        return value

    def reference_tests(self, task_id: str) -> str:
        """The original (correct) tests for a task, hash-checked (researcher-only)."""
        record = self._private_entry(task_id)['files']['reference/original_test.py']
        path = self.repository / record['path']
        if not path.exists():
            raise PrivateDataMissing(f'{record["path"]} is missing (researcher-only coding data)')
        return _read_checked(self.repository, record['path'], record['sha256'])
