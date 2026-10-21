#!/usr/bin/env python3
"""Prepare an offline, private static-Space archive of explicitly selected runs.

Does not authenticate, upload, read host credentials, or change original records.
Pass a locally generated Inspect View bundle for each selected run.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

from build_experiment_index import build
from space_navigation import private_space_navigation
from site_style import themed

ROOT = Path(__file__).resolve().parents[1]
# Behavior judge outputs under RUN/analysis/judge/ (docs/experiment-library.md, "Behavior
# judge outputs"): published only from a paid run of the misbehavior judge (rubric v2 or,
# from judge revision v3, rubric v3; docs/plan.md "misbehavior-focused judge" and "Behavior
# judge iteration specified"; report and browser schemas unchanged) whose report and
# read-only evidence browser match the judge outputs. There is no human marking step
# (docs/plan.md, "no human marks").
# Allowlist; everything else stays local, notably raw/ provider request/response archives,
# units/, any ledger copy and any file in review/ the browser did not produce. Outputs of the
# superseded 12-dimension judge (report schema /2, browser manifest /2) are refused.
JUDGE_FILES = frozenset({'report.html', 'report.json', 'labels.jsonl', 'pipeline.json', 'run_level.json',
                         'review/index.html', 'review/manifest.json'})
JUDGE_DIRS = frozenset({'packages', 'agents', 'review/agents'})
JUDGE_SECTION_MARKER = '<!-- behavior-judge:start -->'
JUDGE_REVIEW_SCHEMA = 'gdm-judge-review-manifest/3'
JUDGE_REPORT_SCHEMA = 'gdm-judge-report/3'


def judge_excluded(relative):
    """True for paths under analysis/judge/ outside the publication allowlist."""
    parts = Path(relative).parts
    if parts[:2] != ('analysis', 'judge') or len(parts) == 2:
        return False
    rest = parts[2:]
    if rest == ('review',):  # the directory itself; its files are allowlisted one by one
        return False
    if '/'.join(rest) in JUDGE_FILES:
        return False
    for directory in JUDGE_DIRS:
        prefix = tuple(directory.split('/'))
        if rest[:len(prefix)] == prefix:
            return False
    return True


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def judge_review_problems(judge, report):
    """Why the evidence browser and report do not match the judge outputs (empty if they do)."""
    manifest_path = judge / 'review' / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except ValueError:
        return ['review/manifest.json is not valid JSON']
    if not isinstance(manifest, dict) or manifest.get('schema') != JUDGE_REVIEW_SCHEMA:
        return [f'review/manifest.json must have schema {JUDGE_REVIEW_SCHEMA} (rebuild the evidence browser)']
    problems = []
    if report.get('schema') != JUDGE_REPORT_SCHEMA:
        problems.append(f'report.json must have schema {JUDGE_REPORT_SCHEMA} (outputs of the superseded '
                        '12-dimension judge are not published)')
    page = report.get('review_page') if isinstance(report.get('review_page'), dict) else {}
    if page.get('status') != 'current' or page.get('manifest_file_sha256') != _sha256(manifest_path):
        problems.append('report.json was not rebuilt after the current evidence browser')
    recorded = manifest.get('inputs_sha256') if isinstance(manifest.get('inputs_sha256'), dict) else {}
    for name, digest in sorted(recorded.items()):
        path = judge / name
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            problems.append(f'judge input changed after the evidence browser was built: {name}')
    for pattern in ('agents/*.json', 'packages/*.json', 'run_level.json', 'pipeline.json', 'labels.jsonl'):
        for path in sorted(judge.glob(pattern)):
            if path.relative_to(judge).as_posix() not in recorded:
                problems.append(f'judge output added after the evidence browser was built: '
                                f'{path.relative_to(judge).as_posix()}')
    pages = manifest.get('pages') if isinstance(manifest.get('pages'), dict) else {}
    if 'index.html' not in pages:
        problems.append('review/manifest.json lists no index.html')
    for name, digest in sorted(pages.items()):
        path = judge / 'review' / name
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            problems.append(f'evidence browser page missing or edited: review/{name}')
    for path in sorted((judge / 'review' / 'agents').rglob('*')):
        if path.is_file() and path.relative_to(judge / 'review').as_posix() not in pages:
            problems.append(f'review/{path.relative_to(judge / "review").as_posix()} was not produced by the '
                            'current evidence browser build')
    return problems


def judge_publication(source):
    """None when a run has no judge outputs; else the included/excluded judge paths.
    Refuses rehearsal or unknown-mode outputs and a report or evidence browser that no
    longer matches the judge outputs."""
    judge = source / 'analysis' / 'judge'
    if not judge.exists() and not judge.is_symlink():
        if JUDGE_SECTION_MARKER in (source / 'results.html').read_text(encoding='utf-8', errors='replace'):
            raise ValueError('results.html has a behavior judge section but analysis/judge is missing')
        return None
    if judge.is_symlink() or not judge.is_dir():
        raise ValueError('analysis/judge must be a real directory')
    for name in ('report.json', 'review/manifest.json', 'review/index.html'):
        path = judge / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Behavior judge outputs need {path.relative_to(source)}: build the evidence '
                             'browser and the report before publishing')
    report = json.loads((judge / 'report.json').read_text(encoding='utf-8'))
    if report.get('mode') != 'paid':
        raise ValueError(f"Refusing to publish behavior judge outputs in mode {report.get('mode')!r}")
    problems = judge_review_problems(judge, report)
    if problems:
        raise ValueError('Behavior judge evidence browser or report is stale or superseded; rebuild both before '
                         'publishing:\n  '
                         + '\n  '.join(problems[:20]))
    files = [p.relative_to(source) for p in sorted(judge.rglob('*'))
             if p.is_symlink() or p.is_file()]
    hidden = [p for p in files if any(part.startswith('.') for part in p.parts)]
    return {'included': [p.as_posix() for p in files if not judge_excluded(p) and p not in hidden],
            'excluded': [p.as_posix() for p in files if judge_excluded(p) and p not in hidden]}


# The custom transcript export (scripts/export_transcripts.py, RUN/transcripts/) is local-only:
# agent transcripts are read in Inspect View (owner, October 21, 2026), so it is not published.
LOCAL_ONLY_DIRS = frozenset({'transcripts'})
# A page that links the unpublished transcript export was made by an older generator.
TRANSCRIPT_LINK = re.compile(r'href="(?:\.\./)*transcripts/', re.I)


def run_excluded(relative, transcript_export=False):
    """True for run paths that stay local: the transcript export (unless ``transcript_export``, only
    for ``maintain_hf_run.py --scope transcripts`` of runs published with one) and judge files off the
    allowlist."""
    parts = Path(relative).parts
    return (not transcript_export and bool(parts) and parts[0] in LOCAL_ONLY_DIRS) or judge_excluded(relative)


def copy_records(source, destination, skip=None):
    """Exclude hidden files and refuse symlinks without following them. Paths for which
    skip(relative) is true are neither copied nor followed."""
    destination.mkdir(parents=True)
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
            continue
        if skip is not None and skip(relative):
            continue
        if path.is_symlink():
            raise ValueError(f'Refusing symlink: {relative}')
        if path.name.lower() in {'credentials', 'credentials.json', 'token', 'token.json'} or path.suffix.lower() in {'.pem', '.key'}:
            raise ValueError(f'Refusing possible credential file: {relative}')
        target = destination / relative
        if path.is_dir():
            target.mkdir(exist_ok=True)
        elif path.is_file():
            shutil.copy2(path, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs-dir', type=Path, default=ROOT / 'runs')
    parser.add_argument('--run', action='append', required=True,
                        help='RUN_ID=INSPECT_BUNDLE_DIRECTORY; repeat for multiple runs')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--include-transcript-export', action='store_true',
                        help='keep RUN/transcripts/ (only to maintain the transcript pages of a run published '
                             'before October 21, 2026 with maintain_hf_run.py --scope transcripts)')
    args = parser.parse_args()
    selected, judge_plans = [], {}
    for specification in args.run:
        run_id, bundle = specification.split('=', 1)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', run_id):
            raise ValueError('Invalid run ID')
        source = args.runs_dir / run_id
        viewer = Path(bundle)
        if source.is_symlink() or viewer.is_symlink():
            raise ValueError('Source directories must not be symlinks')
        if not (source / 'results.json').is_file() or not (source / 'results.html').is_file():
            raise ValueError(f'Run has no completed report: {run_id}')
        if not (viewer / 'index.html').is_file() or not (viewer / 'logs/listing.json').is_file():
            raise ValueError(f'Incomplete Inspect bundle for {run_id}')
        judge_plans[run_id] = judge_publication(source)
        selected.append((run_id, source, viewer))
    if len({row[0] for row in selected}) != len(selected):
        raise ValueError('Duplicate run IDs')
    output = args.output_dir.resolve()
    # Refuse overwrite or output within source records: raw evidence stays immutable.
    for _, source, viewer in selected:
        if output.is_relative_to(source.resolve()) or output.is_relative_to(viewer.resolve()):
            raise ValueError('Output must be outside the sources')
    output.mkdir(parents=True, exist_ok=False)
    for run_id, source, viewer in selected:
        copy_records(source, output / run_id,
                     skip=lambda relative: run_excluded(relative, args.include_transcript_export))
        # The generated views only; archived pages (e.g. results.legacy.html) stay as recorded.
        views = [output / run_id / 'results.html', *sorted((output / run_id / 'conference-view').glob('*.html'))]
        for page in views:
            if not args.include_transcript_export and page.is_file() \
                    and TRANSCRIPT_LINK.search(page.read_text(errors='replace')):
                raise ValueError(f'{run_id}/{page.relative_to(output / run_id).as_posix()} links the unpublished '
                                 'transcript export; regenerate it with the current build_results.py / '
                                 'export_conference.py')
        copy_records(viewer, output / run_id / 'inspect-view')
    result = build(output, hosted=True)
    if result['warnings'] or result['experiments'] != len(selected):
        raise ValueError(f'Incomplete catalogue: {result}')
    (output / 'README.md').write_text('''---
title: GDM Math Swarm Experiments
emoji: 🐝
colorFrom: blue
colorTo: indigo
sdk: static
app_file: index.html
pinned: false
---

Private researcher archive of completed GDM Math Swarm experiments.
Open the app for experiment reports, the conference view, native Inspect View
(agent transcripts) and original run records. Operational acceptance and independent proof validity
are distinct; see each report for limitations. No model or experiment runs here.

This archive must be hosted in a **private** Space. Do not upload credentials.
''')
    (output / 'robots.txt').write_text('User-agent: *\nDisallow: /\n')
    for page in output.rglob('*.html'):
        modules = ['./assets/' + p.name for p in sorted((page.parent / 'assets').glob('*.js'))] if page.parent.name == 'inspect-view' else []
        text = page.read_text() if 'inspect-view' in page.relative_to(output).parts else themed(page.read_text())
        page.write_text(private_space_navigation(text, modules))
    files = {str(p.relative_to(output)): {'bytes': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(output.rglob('*')) if p.is_file()}
    manifest = {'schema': 'gdm-private-space/1', 'runs': [r[0] for r in selected],
                'files': files, 'total_bytes': sum(f['bytes'] for f in files.values())}
    judged = {run: {'included': len(plan['included']), 'excluded': plan['excluded']}
              for run, plan in judge_plans.items() if plan is not None}
    if judged:  # record which local judge records were deliberately left out
        manifest['behavior_judge'] = judged
    (output / 'archive-manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'output': str(output), 'runs': manifest['runs'],
                      'files': len(files), 'bytes': manifest['total_bytes'],
                      'behavior_judge': judged}))


if __name__ == '__main__':
    main()
