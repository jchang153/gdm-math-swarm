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

class FrozenDataset:
    def __init__(self, repository: str | Path):
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
