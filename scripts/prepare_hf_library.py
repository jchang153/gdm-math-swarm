#!/usr/bin/env python3
"""Prepare an offline static-Space archive of explicitly selected runs for the results Space (public by owner
decision, October 24, 2026; scripts/hf_visibility.py). Inspect logs and raw records are moved to dataset-files/,
bound for the private logs dataset, never the Space.

Does not authenticate, upload, read host credentials, or change original records.
Pass a locally generated Inspect View bundle for each selected run.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tarfile

from build_experiment_index import build
import dataset_logs
from dataset_logs import COMMIT_PLACEHOLDER, DATASET_REPO, DATASET_SCHEMA, category, dataset_path
from incident_site import INCIDENT_DIR, add_incident_tab, incident_excluded, incident_files, unpublished_links
from simple_judge_site import (DATASET_FILES as SJ_DATASET_FILES, SJ_DIR, SPACE_FILES as SJ_SPACE_FILES,
                               add_simple_judge_tab, simple_judge_excluded, simple_judge_files, simple_report,
                               site_page, space_report_json)
from publish_hf_library import SECRET
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
    return ((not transcript_export and bool(parts) and parts[0] in LOCAL_ONLY_DIRS) or judge_excluded(relative)
            or incident_excluded(relative) or simple_judge_excluded(relative))


def simple_judge_publication(source, judge_plan=None):
    """None when a run has no completed simplified judge; else where its files go (scripts/simple_judge_site.py):
    report.html (re-rendered by site_page, returned as 'page') and report.json to the Space, agents.jsonl to the
    private dataset, every other file of analysis/simple_judge/ ('excluded') stays local. Each published file
    is screened for credential-like content."""
    names = simple_judge_files(source)
    if not names:
        return None
    data = {name: (source / SJ_DIR / name).read_bytes() for name in names if name != 'report.html'}
    old_judge = bool(judge_plan) and 'analysis/judge/report.html' in judge_plan['included']
    from run_clock import run_start  # the per-agent times as run clock plus UTC (owner, October 25, 2026)
    page = site_page(data['report.json'], data['agents.jsonl'], source.name, old_judge=old_judge,
                     run_start=run_start(source)).encode('utf-8')
    for name, content in {**data, 'report.html': page}.items():
        if SECRET.search(content):
            raise ValueError(f'Credential-like content in {SJ_DIR}/{name}; not published')
    published = {f'{SJ_DIR}/{name}' for name in (*SJ_SPACE_FILES, *SJ_DATASET_FILES)}
    folder = source / SJ_DIR
    excluded = sorted(p.relative_to(source).as_posix() for p in folder.rglob('*')
                      if (p.is_file() or p.is_symlink()) and p.relative_to(source).as_posix() not in published)
    return {'space': [f'{SJ_DIR}/{name}' for name in SJ_SPACE_FILES],
            'dataset': [f'{SJ_DIR}/{name}' for name in SJ_DATASET_FILES], 'excluded': excluded, 'page': page}


def incident_publication(source):
    """None when a run has no incident report; else its included and locally excluded incident paths.
    Only report.html, report.md and evidence.json are published
    (scripts/incident_site.py); each is screened for credential-like content and the published page
    must not link the incident files that stay local."""
    names = incident_files(source)
    if not names:
        return None
    folder = source / INCIDENT_DIR
    included = [f'{INCIDENT_DIR}/{name}' for name in names]
    for name in included:
        data = (source / name).read_bytes()
        if SECRET.search(data):
            raise ValueError(f'Credential-like content in {name}; not published')
        if name.endswith('.html') and unpublished_links(data.decode('utf-8', 'replace')):
            raise ValueError(f'{name} links incident files that are not published (raw/, units/, route-pricing/, '
                             'digest.json, state.json or run-budgets.json)')
    excluded = sorted(p.relative_to(source).as_posix() for p in folder.rglob('*')
                      if (p.is_file() or p.is_symlink()) and p.relative_to(source).as_posix() not in included)
    return {'included': included, 'excluded': excluded}


# Runtime file snapshots (RUN/snapshots/AGENT/SHA256: thousands of small content-addressed blobs) are
# published as one deterministic archive per run, not as loose files: HF limits a repository to 20,000
# files (owner, October 21, 2026, "just go ahead and merge files"). No page loads them at runtime; the
# conference view embeds every captured file in its own pages when it is exported.
SNAPSHOTS_DIR = 'snapshots'
SNAPSHOT_ARCHIVE = 'snapshots.tar.gz'
SNAPSHOT_MANIFEST = 'snapshots.manifest.json'
SNAPSHOT_SCHEMA = 'gdm-snapshot-archive/1'
# A page that links (href/src) or fetches a loose snapshot file would break once they are archived.
SNAPSHOT_LINK = re.compile(r'''(?:href|src)\s*=\s*["'](?:\.\.?/)*snapshots/|fetch\(\s*["'`](?:\.\.?/)*snapshots/''',
                           re.I)


def snapshot_files(source):
    """The run's snapshot files as sorted (path relative to the run, file path), under the record rules
    of copy_records: hidden files and __pycache__ are left out, symlinks and credential names refused."""
    source = Path(source)
    root = source / SNAPSHOTS_DIR
    if root.is_symlink():
        raise ValueError(f'Refusing symlink: {SNAPSHOTS_DIR}')
    rows = []
    for path in sorted(root.rglob('*')) if root.is_dir() else []:
        relative = path.relative_to(source)
        if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'Refusing symlink: {relative}')
        if path.name.lower() in {'credentials', 'credentials.json', 'token', 'token.json'} \
                or path.suffix.lower() in {'.pem', '.key'}:
            raise ValueError(f'Refusing possible credential file: {relative}')
        if path.is_file():
            rows.append((relative.as_posix(), path))
    return rows


def snapshot_archive(source, run_id):
    """None without snapshot files; else (archive bytes, manifest bytes). The tar.gz is deterministic
    (sorted members, zero times and owners, fixed modes, no gzip name or time), so preparing it again
    from the same records gives the same bytes; the manifest lists every member's bytes and SHA-256."""
    rows = snapshot_files(source)
    if not rows:
        return None
    entries, raw = {}, io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.GNU_FORMAT) as tar:
        for name, path in rows:
            data = path.read_bytes()
            entries[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode, info.type = len(data), 0, 0o644, tarfile.REGTYPE
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            tar.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=packed, mtime=0, compresslevel=9) as stream:
        stream.write(raw.getvalue())
    archive = packed.getvalue()
    manifest = {'schema': SNAPSHOT_SCHEMA, 'run': run_id, 'archive': SNAPSHOT_ARCHIVE,
                'archive_bytes': len(archive), 'archive_sha256': hashlib.sha256(archive).hexdigest(),
                'file_count': len(entries), 'total_bytes': sum(x['bytes'] for x in entries.values()),
                'note': 'Runtime file snapshots of this run, archived instead of published as loose files '
                        '(HF 20,000-file repository limit). The conference view shows the captured files.',
                'files': entries}
    return archive, (json.dumps(manifest, indent=2) + '\n').encode()


def snapshot_archive_members(archive):
    """name -> bytes of a snapshot archive's members; any member that is not a regular file is refused."""
    members = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for info in tar:
            if not info.isreg() or info.name in members:
                raise ValueError('Unexpected snapshot archive member: ' + info.name)
            members[info.name] = tar.extractfile(info).read()
    return members


def resume_archive_members(archive):
    """name -> bytes of the regular files of a resume-bundle agent archive (research_swarm.resume_bundle
    state.tar.gz), for the credential screen. Unlike a snapshot archive it holds directories, symlinks and
    dot-folders (the agent's ~/.agent); only absolute or parent-relative member names are refused."""
    members = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for info in tar:
            if info.name.startswith('/') or '..' in info.name.split('/'):
                raise ValueError('Unsafe resume archive member: ' + info.name)
            if info.isreg():
                members[info.name] = tar.extractfile(info).read()
    return members


def archive_members(name, data):
    """The members to screen of a dataset-bound .tar.gz: resume-bundle archives or snapshot archives."""
    if '/resume/' in '/' + name.replace('\\', '/'):
        return resume_archive_members(data)
    return snapshot_archive_members(data)


def check_snapshot_archive(archive, manifest_bytes, run_id):
    """The manifest's member records ({path: {bytes, sha256}}) after checking that the manifest
    describes exactly this archive and its members."""
    manifest = json.loads(manifest_bytes)
    files = manifest.get('files') if isinstance(manifest, dict) else None
    if (not isinstance(files, dict) or manifest.get('schema') != SNAPSHOT_SCHEMA or manifest.get('run') != run_id
            or manifest.get('archive') != SNAPSHOT_ARCHIVE or manifest.get('archive_bytes') != len(archive)
            or manifest.get('archive_sha256') != hashlib.sha256(archive).hexdigest()
            or manifest.get('file_count') != len(files)
            or manifest.get('total_bytes') != sum(x.get('bytes', 0) for x in files.values())):
        raise ValueError(f'{run_id}/{SNAPSHOT_MANIFEST} does not describe {SNAPSHOT_ARCHIVE}')
    members = snapshot_archive_members(archive)
    if {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for name, data in members.items()} != files:
        raise ValueError(f'{run_id}/{SNAPSHOT_ARCHIVE} members differ from {SNAPSHOT_MANIFEST}')
    for name in files:
        parts = name.split('/')
        if len(parts) < 2 or parts[0] != SNAPSHOTS_DIR or any(p in ('', '.', '..') or p.startswith('.') for p in parts):
            raise ValueError('Unsafe snapshot archive member: ' + name)
    return files


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


DATASET_DIR = 'dataset-files'  # bundle folder of the files bound for the private logs dataset
_SHIM_PROBE = '0' * 40


def log_shim(document, run):
    """An Inspect View page with the dataset fetch shim first in <head>, its commit left as the placeholder
    that publish_hf_library.py pins to the dataset commit."""
    shimmed = dataset_logs.with_log_shim(document, path=f'{run}/inspect', commit=_SHIM_PROBE,
                                         signin='../../' + dataset_logs.SIGNIN_FILE)
    return shimmed.replace(f'data-commit="{_SHIM_PROBE}"', f'data-commit="{COMMIT_PLACEHOLDER}"', 1)


def screen_bytes(name, data):
    """The credential screen of publish_hf_library.screen for a dataset-bound file (members of Inspect
    logs and snapshot archives included); the exact public placeholder stays exempt."""
    if SECRET.search(data):
        raise ValueError(f'Credential-like content in {name}; not published')
    if name.endswith('.tar.gz'):
        for member, content in archive_members(name, data).items():
            if SECRET.search(content):
                raise ValueError(f'Credential-like content in a member of {name}; not published')
    if name.endswith('.eval'):
        try:
            import zipfile_zstd  # noqa: F401; registers Inspect's ZIP method
        except ImportError as error:
            raise ValueError('Install the publication extra to screen Inspect archives') from error
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.namelist():
                if SECRET.search(archive.read(member)):
                    raise ValueError(f'Credential-like content in Inspect archive {name}; not published')


def relocate_dataset_files(output, runs):
    """Move every dataset-bound run file (dataset_logs.category) out of the Space bundle into
    OUTPUT/dataset-files/<dataset path>, deduplicating the two copies of each Inspect log. Returns
    (dataset manifest files, archive-manifest dataset_files, run -> moved run-relative paths)."""
    files, by_space, moved = {}, {}, {}
    target_root = output / DATASET_DIR
    for run in runs:
        moved[run] = set()
        for path in sorted((output / run).rglob('*')):
            if not path.is_file():
                continue
            rel = path.relative_to(output / run).as_posix()
            kind = category(rel)
            if kind is None:
                continue
            data = path.read_bytes()
            name = dataset_path(run, rel)
            screen_bytes(f'{run}/{rel}', data)
            record = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            if name in files:
                if {k: files[name][k] for k in record} != record:
                    raise ValueError(f'{run}/{rel} differs from another copy bound for {name}')
                files[name]['sources'].append(rel)
            else:
                target = target_root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                files[name] = {**record, 'category': kind, 'run': run, 'sources': [rel]}
            by_space[f'{run}/{rel}'] = {'repo': DATASET_REPO, 'path': name, **record}
            moved[run].add(rel)
            path.unlink()
        for directory in sorted((output / run).rglob('*'), reverse=True):  # emptied folders (logs/, snapshots/)
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
    if files:
        (target_root / 'manifest.json').write_text(json.dumps(
            {'schema': DATASET_SCHEMA, 'repo': DATASET_REPO, 'files': files}, indent=2))
    return files, by_space, moved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs-dir', type=Path, default=ROOT / 'runs')
    parser.add_argument('--run', action='append', required=True,
                        help='RUN_ID=INSPECT_BUNDLE_DIRECTORY; repeat for multiple runs')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--include-transcript-export', action='store_true',
                        help='keep RUN/transcripts/ (only to maintain the transcript pages of a run published '
                             'before October 21, 2026 with maintain_hf_run.py --scope transcripts)')
    parser.add_argument('--records-in-space', action='store_true',
                        help='legacy layout of runs published before October 22, 2026: keep Inspect logs and raw '
                             'records in the Space bundle (no dataset-files/, no sign-in shim); only for maintenance '
                             'scopes that compare a bundle with such a run. publish_hf_library.py refuses it.')
    args = parser.parse_args()
    selected, judge_plans, incident_plans, simple_plans = [], {}, {}, {}
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
        incident_plans[run_id] = incident_publication(source)
        simple_plans[run_id] = simple_judge_publication(source, judge_plans[run_id])
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
                     skip=lambda relative: run_excluded(relative, args.include_transcript_export)
                     or relative.parts[:1] == (SNAPSHOTS_DIR,))
        packed = snapshot_archive(source, run_id)
        if packed is not None:
            for name, data in zip((SNAPSHOT_ARCHIVE, SNAPSHOT_MANIFEST), packed):
                (output / run_id / name).write_bytes(data)
        for page in sorted((output / run_id).rglob('*.html')):
            if SNAPSHOT_LINK.search(page.read_text(errors='replace')):
                raise ValueError(f'{run_id}/{page.relative_to(output / run_id).as_posix()} links loose snapshot '
                                 'files, which are published only inside snapshots.tar.gz; link the conference '
                                 "view's captured-file pages instead")
        if simple_plans[run_id] is not None:  # the simplified judge's page and the run page's Behavior judge tab
            (output / run_id / SJ_DIR).mkdir(parents=True, exist_ok=True)
            (output / run_id / SJ_DIR / 'report.html').write_bytes(simple_plans[run_id]['page'])
            (output / run_id / SJ_DIR / 'report.json').write_bytes(
                space_report_json((source / SJ_DIR / 'report.json').read_bytes()))  # no host paths on the Space
            page = output / run_id / 'results.html'
            page.write_text(add_simple_judge_tab(page.read_text(encoding='utf-8'), simple_report(source)),
                            encoding='utf-8')
        if incident_plans[run_id] is not None:  # the run page's Incident report tab (navigation only)
            page = output / run_id / 'results.html'
            page.write_text(add_incident_tab(page.read_text(encoding='utf-8')), encoding='utf-8')
        # The generated views only; archived pages (e.g. results.legacy.html) stay as recorded.
        views = [output / run_id / 'results.html', *sorted((output / run_id / 'conference-view').glob('*.html'))]
        for page in views:
            if not args.include_transcript_export and page.is_file() \
                    and TRANSCRIPT_LINK.search(page.read_text(errors='replace')):
                raise ValueError(f'{run_id}/{page.relative_to(output / run_id).as_posix()} links the unpublished '
                                 'transcript export; regenerate it with the current build_results.py / '
                                 'export_conference.py')
        copy_records(viewer, output / run_id / 'inspect-view')
    # The catalogue is built while the Inspect logs are still in the bundle, so that each entry keeps its
    # (hidden) Inspect log links exactly as published entries have them.
    result = build(output, hosted=True)
    if result['warnings'] or result['experiments'] != len(selected):
        raise ValueError(f'Incomplete catalogue: {result}')
    dataset_files, space_records, moved = ({}, {}, {}) if args.records_in_space else \
        relocate_dataset_files(output, [r[0] for r in selected])
    for run, paths in moved.items():  # links to moved records point to the dataset file (pinned on publish)
        for page in sorted((output / run).rglob('*.html')):
            rel = page.relative_to(output / run).as_posix()
            if rel.startswith('inspect-view/'):
                continue
            text, changed = dataset_logs.repoint_links(page.read_text(encoding='utf-8'), rel, run, paths,
                                                       COMMIT_PLACEHOLDER)
            if changed:
                page.write_text(text, encoding='utf-8')
    (output / 'README.md').write_text('''---
title: GDM Math Swarm Experiments
emoji: 🐝
colorFrom: blue
colorTo: indigo
sdk: static
app_file: index.html
pinned: false
---

Researcher archive of completed GDM Math Swarm experiments.
Open the app for experiment reports, the conference view, native Inspect View
(agent transcripts) and original run records. Operational acceptance and independent proof validity
are distinct; see each report for limitations. No model or experiment runs here.

This Space is public by owner decision (October 24, 2026). Inspect logs and raw records are kept in a
private Hugging Face dataset (Sign in with HF for read access). Do not upload credentials.
''')
    (output / 'robots.txt').write_text('User-agent: *\nDisallow: /\n')
    for page in output.rglob('*.html'):
        if page.relative_to(output).parts[:1] == (DATASET_DIR,):
            continue  # dataset-bound transcript pages stay as recorded
        modules = ['./assets/' + p.name for p in sorted((page.parent / 'assets').glob('*.js'))] if page.parent.name == 'inspect-view' else []
        text = page.read_text() if 'inspect-view' in page.relative_to(output).parts else themed(page.read_text())
        text = private_space_navigation(text, modules)
        if page.relative_to(output).parts[1:] == ('inspect-view', 'index.html') \
                and any(category(rel) == 'eval' for rel in moved.get(page.relative_to(output).parts[0], ())):
            text = log_shim(text, page.relative_to(output).parts[0])  # first in <head>, before the navigation
        page.write_text(text)
    # Sign in with HF (the viewer's own OAuth token for the private dataset); no external libraries.
    if not args.records_in_space:
        (output / dataset_logs.SIGNIN_FILE).write_text(private_space_navigation(themed(dataset_logs.signin_page())))
    files = {str(p.relative_to(output)): {'bytes': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in sorted(output.rglob('*')) if p.is_file() and p.relative_to(output).parts[0] != DATASET_DIR}
    manifest = {'schema': 'gdm-private-space/1', 'runs': [r[0] for r in selected],
                'files': files, 'total_bytes': sum(f['bytes'] for f in files.values())}
    if args.records_in_space:
        manifest['layout'] = 'records-in-space'
    if space_records:  # where each run file that is not in the Space lives in the private dataset
        manifest['dataset_repo'] = DATASET_REPO
        manifest['dataset_files'] = space_records
    judged = {run: {'included': len(plan['included']), 'excluded': plan['excluded']}
              for run, plan in judge_plans.items() if plan is not None}
    if judged:  # record which local judge records were deliberately left out
        manifest['behavior_judge'] = judged
    incidents = {run: {'included': plan['included'], 'excluded': len(plan['excluded'])}
                 for run, plan in incident_plans.items() if plan is not None}
    for run, plan in incidents.items():  # the published copies (report.html themed) are screened again
        for name in plan['included']:
            if f'{run}/{name}' not in files or SECRET.search((output / run / name).read_bytes()):
                raise ValueError(f'{run}/{name} is missing from the bundle or has credential-like content')
    if incidents:  # which incident files are published; the rest of analysis/incident/ stays local
        manifest['incident_report'] = incidents
    simple = {run: {'space': plan['space'], 'dataset': plan['dataset'], 'excluded': len(plan['excluded'])}
              for run, plan in simple_plans.items() if plan is not None}
    for run, plan in simple.items():  # Space copies present and screened again, agents.jsonl in the dataset part
        for name in plan['space']:
            if f'{run}/{name}' not in files or SECRET.search((output / run / name).read_bytes()):
                raise ValueError(f'{run}/{name} is missing from the bundle or has credential-like content')
        for name in plan['dataset']:
            if not args.records_in_space and f'{run}/{name}' not in space_records:
                raise ValueError(f'{run}/{name} is not bound for the private dataset')
    if simple:  # where the simplified judge's files went; its working files stay local
        manifest['simple_judge'] = simple
    (output / 'archive-manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'output': str(output), 'runs': manifest['runs'],
                      'files': len(files), 'bytes': manifest['total_bytes'],
                      'dataset_files': len(dataset_files),
                      'dataset_bytes': sum(f['bytes'] for f in dataset_files.values()),
                      'behavior_judge': judged, 'incident_report': incidents, 'simple_judge': simple}))


if __name__ == '__main__':
    main()
