"""Load the frozen, researcher-owned benchmark and export only public task files."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from pathlib import Path

# Dataset revisions (docs/dataset.md, "Revisions"). Same 71 identities and pilot 14 in every
# revision; only template text differs. Revision 1 is archived for re-evaluating and analyzing
# runs made before 2026-10-21; new runs use the current revision in data/.
CURRENT_REVISION = 2

# Template eligibility (not the judge's keyword stage, which stays the paper's literal policy):
# every spelling of native evaluation, each of which closes goals through the Lean.ofReduceBool
# axiom. Mirrors scripts/prepare_dataset.py NATIVE_EVALUATION (a test checks they agree).
NATIVE_EVALUATION = re.compile(r'\bnative_decide\b|\+\s*native\b|\bnative\s*:=\s*true\b|\bofReduce(?:Bool|Nat)\b')


def native_evaluation_spellings(source: str) -> list[str]:
    """Native-evaluation spellings in a template (a revision 2 template has none)."""
    return sorted({m.group(0) for m in NATIVE_EVALUATION.finditer(source)})
REVISION_DIRS = {1: 'data/revisions/v1', 2: 'data'}

@dataclass(frozen=True)
class DatasetTask:
    task_id: str
    title: str
    theorem_name: str
    source_path: str
    template_path: str
    answer_based: bool
    description_url: str
    source_url: str
    original_statement: str
    sha256: str
    category: str
    template_root: str = 'data/templates'  # the revision's template directory
    def template_source(self, repository: Path) -> str:
        root = (Path(repository) / self.template_root).resolve()
        path = (root / Path(self.template_path).name).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Task template escapes the template directory')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError(f'Template hash mismatch: {self.task_id}')
        return data.decode('utf-8')

# Task sets (docs/plan.md, "Gemini 3.1 Pro collaboration run authorized"): the frozen 14-task
# pilot subset (default, every baseline) or the whole 71-task parent pool (owner decision for
# the Gemini collaboration run; launch gates allow it only on that budget track).
PILOT_TASK_SET = 'pilot14'
ALL_TASK_SET = 'all71'
TASK_SETS = (PILOT_TASK_SET, ALL_TASK_SET)
TASK_SET_KEY = 'task_set'


def task_set_of(config) -> str:
    """The config's task set (absent: the pilot subset); unknown values are refused."""
    value = (config or {}).get(TASK_SET_KEY, PILOT_TASK_SET)
    if value not in TASK_SETS:
        raise ValueError(f'{TASK_SET_KEY} {value!r} is not one of {list(TASK_SETS)}')
    return value


def run_task_set(run_dir) -> str:
    """The task set a saved run used (settings.json effective/config), default the pilot subset."""
    path = Path(run_dir) / 'settings.json'
    try:
        settings = json.loads(path.read_text())
    except (OSError, ValueError):
        return PILOT_TASK_SET
    effective = (settings.get('effective') or {}).get(TASK_SET_KEY)
    return effective or (settings.get('config') or {}).get(TASK_SET_KEY) or PILOT_TASK_SET


def manifest_revision(manifest) -> int:
    """The revision a manifest declares (revision 1 predates the field)."""
    return int(manifest.get('dataset_revision', 1))


def revision_manifest_sha256(repository, revision) -> str | None:
    try:
        return hashlib.sha256((Path(repository) / REVISION_DIRS[revision] / 'manifest.json').read_bytes()).hexdigest()
    except (KeyError, OSError):
        return None


def revision_of_manifest_sha256(repository, sha256) -> int | None:
    """The revision whose parent manifest has this SHA-256 (None: unknown)."""
    return next((r for r in sorted(REVISION_DIRS) if sha256 and revision_manifest_sha256(repository, r) == sha256), None)


def revision_template_hashes(repository, revision) -> dict:
    manifest = json.loads((Path(repository) / REVISION_DIRS[revision] / 'manifest.json').read_text())
    return {t['task_id']: t['sha256'] for t in manifest['tasks']}


def run_dataset_revision(run_dir, repository) -> int:
    """The dataset revision a saved run used.

    In order: settings ``dataset_revision`` (recorded from revision 2 on); the run's pinned parent
    manifest hash (``fixed_task_manifest``); the template hashes in its conference state, when
    they all match exactly one revision. Otherwise the current revision.
    """
    run_dir = Path(run_dir)
    try:
        settings = json.loads((run_dir / 'settings.json').read_text())
    except (OSError, ValueError):
        settings = {}
    for section in (settings.get('effective') or {}, settings.get('config') or {}):
        if section.get('dataset_revision') in REVISION_DIRS:
            return section['dataset_revision']
    for section in (settings.get('effective') or {}, settings.get('config') or {}):
        pin = section.get('fixed_task_manifest') or {}
        found = revision_of_manifest_sha256(repository, pin.get('parent_manifest_sha256')) if isinstance(pin, dict) else None
        if found:
            return found
    path = run_dir / 'conference' / 'state.sqlite'
    if path.exists():
        try:
            db = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=30)
            try:
                recorded = dict(db.execute('SELECT task_id, template_sha256 FROM tasks'))
            finally:
                db.close()
        except sqlite3.Error:
            recorded = {}
        if recorded:
            matches = [r for r in sorted(REVISION_DIRS)
                       if all(revision_template_hashes(repository, r).get(t) == h for t, h in recorded.items())]
            if len(matches) == 1:
                return matches[0]
    return CURRENT_REVISION


class FrozenDataset:
    def __init__(self, repository: str | Path, task_set: str = PILOT_TASK_SET, revision: int | None = None):
        if task_set not in TASK_SETS:
            raise ValueError(f'task_set {task_set!r} is not one of {list(TASK_SETS)}')
        revision = CURRENT_REVISION if revision is None else revision
        if revision not in REVISION_DIRS:
            raise ValueError(f'dataset revision {revision!r} is not one of {sorted(REVISION_DIRS)}')
        self.task_set = task_set
        self.revision = revision
        self.repository = Path(repository).resolve()
        base = self.repository / REVISION_DIRS[revision]
        raw = (base / 'manifest.json').read_bytes()
        self.manifest = json.loads(raw)
        if manifest_revision(self.manifest) != revision:
            raise ValueError(f'{REVISION_DIRS[revision]}/manifest.json is not dataset revision {revision}')
        self.subset = json.loads((base / 'pilot_subset.json').read_text())
        if hashlib.sha256(raw).hexdigest() != self.subset['parent_manifest_sha256']:
            raise ValueError('Parent pool changed after pilot sampling')
        fields = [k for k in DatasetTask.__dataclass_fields__ if k != 'template_root']
        root = REVISION_DIRS[revision] + '/templates'
        self.tasks = {entry['task_id']: DatasetTask(**{k:entry[k] for k in fields}, template_root=root)
                      for entry in self.manifest['tasks']}
        if len(self.tasks) != 71 or len(self.subset['task_ids']) != 14:
            raise ValueError('Expected frozen parent71 and pilot14')
        if len(set(self.subset['task_ids'])) != 14 or not set(self.subset['task_ids']) <= self.tasks.keys():
            raise ValueError('Invalid pilot subset')
    @property
    def pilot_tasks(self) -> list[DatasetTask]:
        """The run's tasks: the frozen 14-task subset, or all 71 in manifest order (task_set all71)."""
        if self.task_set == ALL_TASK_SET:
            return [self.tasks[entry['task_id']] for entry in self.manifest['tasks']]
        return [self.tasks[task_id] for task_id in self.subset['task_ids']]
    def export_agent_tasks(self, destination: str | Path) -> list[dict]:
        """Export Lean templates and public metadata only; never observer statements/solutions."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        metadata = []
        for task in self.pilot_tasks:
            filename = task.task_id + '.lean'
            (destination / filename).write_text(task.template_source(self.repository))
            metadata.append(dict(task_id=task.task_id, title=task.title,
                                 theorem_name=task.theorem_name, answer_based=task.answer_based,
                                 file=filename))
        (destination / 'tasks.json').write_text(json.dumps(metadata, indent=2, ensure_ascii=False)+'\n')
        return metadata
