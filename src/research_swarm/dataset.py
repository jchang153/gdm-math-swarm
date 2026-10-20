"""Load the frozen, researcher-owned benchmark and export only public task files."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

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
    def template_source(self, repository: Path) -> str:
        path = (repository / self.template_path).resolve()
        root = (repository / 'data' / 'templates').resolve()
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


class FrozenDataset:
    def __init__(self, repository: str | Path, task_set: str = PILOT_TASK_SET):
        if task_set not in TASK_SETS:
            raise ValueError(f'task_set {task_set!r} is not one of {list(TASK_SETS)}')
        self.task_set = task_set
        self.repository = Path(repository).resolve()
        path = self.repository / 'data' / 'manifest.json'
        raw = path.read_bytes()
        self.manifest = json.loads(raw)
        self.subset = json.loads((self.repository / 'data' / 'pilot_subset.json').read_text())
        if hashlib.sha256(raw).hexdigest() != self.subset['parent_manifest_sha256']:
            raise ValueError('Parent pool changed after pilot sampling')
        fields = DatasetTask.__dataclass_fields__
        self.tasks = {entry['task_id']: DatasetTask(**{k:entry[k] for k in fields})
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
