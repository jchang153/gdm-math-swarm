"""Owner-decided corrections of the proof checker's verdict (data/validity-corrections.json).

The independent evaluator's records stay as written; derived views (story-map stage 0, the faking heatmap) read
the verdict through ``corrected`` so a recorded owner decision applies everywhere. A correction is keyed on the
run ID and the submission's source SHA-256 and names the verdict it replaces, which must match the record (a
changed record is not silently overridden). First use: docs/plan.md, owner decision of November 6, 2026 (Gemini 4
Argon sweep run, two genuine WrittenOnTheWallII_Test_C6_size submissions)."""
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / 'data' / 'validity-corrections.json'


def run_id(run_dir):
    """The run ID of a run folder: results.json "run" when present, else the folder name."""
    run_dir = Path(run_dir)
    try:
        run = json.loads((run_dir / 'results.json').read_text(encoding='utf-8')).get('run')
    except (OSError, ValueError, AttributeError):
        run = None
    return run if isinstance(run, str) and run else run_dir.name


def corrections(run, path=PATH):
    """{source_sha256: correction} of one run."""
    if not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return {c['source_sha256']: c for c in data.get('corrections') or [] if c.get('run') == run}


def corrected(run_dir, validity, path=PATH):
    """``validity`` ({source_sha256: verdict} from evaluations.jsonl) with the run's recorded corrections applied."""
    out = dict(validity)
    for sha, c in corrections(run_id(run_dir), path).items():
        if sha in out:
            if out[sha] != c['evaluator_validity']:
                raise ValueError(f'validity correction for {sha[:12]} expects {c["evaluator_validity"]!r}, '
                                 f'the record says {out[sha]!r}')
            out[sha] = c['validity']
    return out
