"""Offload local run records to the private logs dataset and restore them (owner decision, October 29, 2026:
docs/plan.md, "Local run offload to the private logs dataset").

The Mac keeps only each run's small core (conference/, evaluations/, oversight/, results, settings and budget
files); everything else under ``runs/<unit>/`` is uploaded to the private dataset named by
``GDM_LOGS_DATASET`` (scripts/hf_visibility.py) and deleted locally only after the dataset copy is verified by
SHA-256. A unit is a directory relative to ``runs/``: a run (``RUN``) or an operator folder
(``phase-b/NAME``). Its files go to the dataset at ``<unit>/<path>`` (the repository's ``RUN/<path>``
layout):

- a file already in the dataset at that path with the same bytes is not uploaded again;
- a file of 8 MB or more is uploaded at ``<unit>/<path>`` (or at ``<unit>/offload-<stamp>/files/<path>`` when
  that path already holds other bytes: dataset files are never overwritten or deleted);
- smaller files are packed (deterministically) into ``<unit>/offload-<stamp>/<top-level entry>[.partN].tar.gz``
  (the dataset must stay far below HF's file-count limits; runs/ holds over a million small files).

Guards: the dataset's actual visibility must be private (hf_visibility.check_dataset) before every commit;
commits are pinned to the head just read and retried on a concurrent change; nothing is deleted from the
dataset. Every uploaded byte passes the credential screen (publish_hf_library.SECRET; members of Inspect
logs and of tar archives included). Credential or host-only file names (publish_hf_library.safe_path's list,
.env, auth caches), dot-files, symlinks and budget ledgers (``*.sqlite`` with budget/ledger in the name) are
never uploaded and stay local; a file or archive that fails the screen stays local and is reported.

Each unit gets ``runs/<unit>/OFFLOADED.json``: every uploaded path with its sha256, size, dataset object
(direct path or archive member), the dataset commit, and whether it was kept locally.

Usage (export GDM_LOGS_DATASET first; HANDOFF.local.md):

    python scripts/offload_runs.py offload --unit RUN [--unit phase-b/NAME ...] [--all] [--workers 4]
                                           [--dry-run] [--keep-all]
    python scripts/offload_runs.py restore --unit RUN [--prefix snapshots/] [--force]

``restore`` downloads the dataset objects named in OFFLOADED.json at the recorded commit and writes each
offloaded file back to its original path after checking its SHA-256 (existing files are left alone).
"""
import argparse
import concurrent.futures
import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
import time
import traceback
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dataset_logs  # noqa: E402
import hf_visibility  # noqa: E402
from publish_hf_library import SECRET, safe_path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / 'runs'
STAMP = 'offload-20261029'
SCHEMA = 'gdm-run-offload/1'
DIRECT_MIN = 8 * 1024 * 1024
ARCHIVE_MAX = 1536 * 1024 * 1024  # raw bytes per archive part
CHUNK = 64 * 1024 * 1024
OVERLAP = 4096
# Run units keep these top-level entries locally (plus top-level files up to 1 MiB, except runtime.jsonl).
RUN_KEEP_DIRS = {'conference', 'evaluations', 'oversight'}
SMALL = 1024 * 1024
OPERATOR_KEEP_SUFFIXES = ('.json', '.log', '.exit', '.sh', '.txt', '.yaml', '.yml', '.md', '.csv', '.tsv')
CREDENTIAL_NAMES = {'.env', 'host.env', '.credentials.json', 'credentials', 'credentials.json', 'auth.json',
                    '.netrc', '.git-credentials', 'token', 'token.json', 'stored_tokens', 'budget.sqlite',
                    'application_default_credentials.json', 'credentials.db', 'access_tokens.db',
                    'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa'}
SKIP_UNITS = {'phase-b', 'analysis', 'build-logs', 'discarded'}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def credential_name(name):
    base = PurePosixPath(name).name.lower()
    return base in CREDENTIAL_NAMES or base.endswith(('.pem', '.key')) or base.startswith('.env')


def ledger(name):
    base = PurePosixPath(name).name.lower()
    return re.search(r'\.(?:sqlite|db)(?:-(?:shm|wal|journal))?$', base) is not None and (
        'budget' in base or 'ledger' in base)


def local_only(relative):
    """Why a unit file never leaves the machine, or None."""
    parts = PurePosixPath(relative).parts
    if any(p.startswith('.') or p == '__pycache__' for p in parts):
        return 'dot-file or cache'
    if credential_name(relative):
        return 'credential or host-only file name'
    if ledger(relative):
        return 'budget ledger (host-only)'
    try:
        safe_path(relative)
    except ValueError as error:
        return str(error)
    return None


def keep_locally(unit, relative, size):
    parts = PurePosixPath(relative).parts
    if unit.startswith('phase-b/'):
        if len(parts) <= 2 and size <= SMALL and relative.endswith(OPERATOR_KEEP_SUFFIXES):
            return True
        return any(p in ('receipts',) or p.startswith('verify') for p in parts[:-1]) and size <= SMALL
    if parts[0] in RUN_KEEP_DIRS or parts[0] == 'OFFLOADED.json':
        return True
    return len(parts) == 1 and size <= SMALL and parts[0] != 'runtime.jsonl'


# --- screening -------------------------------------------------------------------------------------------

def scan_stream(stream):
    """(sha256, bytes, secret found) of a binary stream, scanned in overlapping chunks."""
    digest, total, tail, found = hashlib.sha256(), 0, b'', False
    while True:
        block = stream.read(CHUNK)
        if not block:
            break
        digest.update(block)
        total += len(block)
        if not found and SECRET.search(tail + block):
            found = True
        tail = block[-OVERLAP:]
    return digest.hexdigest(), total, found


def scan_members(path):
    """Problems inside an archive or Inspect log (credential member names or credential-like content)."""
    name = path.name
    problems = []
    try:
        if name.endswith('.eval'):
            import zipfile_zstd  # noqa: F401; registers Inspect's ZIP method
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    with archive.open(member) as handle:
                        if scan_stream(handle)[2]:
                            problems.append(f'credential-like content in member {member}')
        elif name.endswith(('.tar.gz', '.tgz', '.tar')):
            with tarfile.open(path, 'r:*') as archive:
                for info in archive:
                    if credential_name(info.name):
                        problems.append(f'credential file name in member {info.name}')
                    if info.isreg():
                        with archive.extractfile(info) as handle:
                            if scan_stream(handle)[2]:
                                problems.append(f'credential-like content in member {info.name}')
    except Exception as error:  # an unreadable archive is uploaded as bytes only after the outer screen
        problems.append(f'archive not readable for the member screen: {error}')
    return problems


# --- inventory -----------------------------------------------------------------------------------------

def unit_files(unit_dir):
    rows = []
    for path in sorted(unit_dir.rglob('*')):
        if path.is_symlink():
            rows.append((path.relative_to(unit_dir).as_posix(), None))
        elif path.is_file():
            rows.append((path.relative_to(unit_dir).as_posix(), path.stat().st_size))
    return rows


def revision_not_found(error):
    """HF's answer for a commit another uploader just made that this replica does not know yet (several VMs
    uploading at once, 2026-10-30): retry, never read it as an empty folder."""
    text = str(error)
    return 'Revision Not Found' in text or 'Invalid rev id' in text


def remote_tree(api, repo, revision, prefix, attempts=12):
    for attempt in range(attempts):
        rows = {}
        try:
            for item in api.list_repo_tree(repo, repo_type='dataset', revision=revision, path_in_repo=prefix,
                                           recursive=True):
                if hasattr(item, 'blob_id'):
                    lfs = getattr(item, 'lfs', None)
                    rows[item.path] = (item.size, lfs.sha256 if lfs else None, item.blob_id)
        except Exception as error:
            if revision_not_found(error) and attempt < attempts - 1:
                time.sleep(5 + 5 * attempt)
                continue
            if revision_not_found(error) or ('not found' not in str(error).lower() and '404' not in str(error)):
                raise
        return rows


def git_sha1_file(path):
    digest = hashlib.sha1(b'blob ' + str(path.stat().st_size).encode() + b'\0')
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def matches(entry, sha256, size, path=None):
    if entry[0] != size:
        return False
    if entry[1] is not None:
        return entry[1] == sha256
    return path is not None and entry[2] == git_sha1_file(path)


# --- archives ------------------------------------------------------------------------------------------

def build_archive(unit_dir, members, target):
    """Deterministic tar.gz of ``members`` [(relative, sha256, size)]; members re-hashed while packing."""
    with open(target, 'wb') as raw, gzip.GzipFile(filename='', fileobj=raw, mode='wb', compresslevel=4, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for relative, sha256, size in members:
            data = (unit_dir / relative).read_bytes()
            if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
                raise ValueError(f'{relative} changed while packing')
            info = tarfile.TarInfo(relative)
            info.size, info.mtime, info.mode = size, 0, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            tar.addfile(info, io.BytesIO(data))


def repack(source, target):
    """Deterministic copy of a tar archive without its credential-named members; returns their names."""
    target.parent.mkdir(parents=True, exist_ok=True)
    skipped = []
    with tarfile.open(source, 'r:*') as old, open(target, 'wb') as raw, \
            gzip.GzipFile(filename='', fileobj=raw, mode='wb', compresslevel=6, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as new:
        for info in old:
            if credential_name(info.name):
                skipped.append(info.name)
                continue
            new.addfile(info, old.extractfile(info) if info.isreg() else None)
    return skipped


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


# --- one unit ------------------------------------------------------------------------------------------

def offload_unit(unit, *, repo, work, dry_run=False, keep_all=False, log=print, prepare_only=False,
                 runs_root=None, stamp=STAMP, exclude=None):
    """Plan (and unless ``prepare_only``/``dry_run`` add, verify and finish) one unit. ``runs_root`` is the
    folder holding the unit (default: this checkout's runs/), ``stamp`` names the unit's archive folder in the
    dataset, ``exclude(relative)`` returns a reason to leave a file out (it stays where it is) or None."""
    from huggingface_hub import HfApi
    api = HfApi()
    unit_dir = Path(runs_root or RUNS) / unit
    if not unit_dir.is_dir():
        raise ValueError('No such unit: ' + unit)
    info = hf_visibility.check_dataset(api, repo)
    present = remote_tree(api, repo, info.sha, unit)
    work_dir = Path(work) / unit.replace('/', '__')
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    files, local, excluded = [], [], []
    for relative, size in unit_files(unit_dir):
        if relative == 'OFFLOADED.json':
            continue
        if size is None:
            local.append({'path': relative, 'reason': 'symlink'})
            continue
        reason = local_only(relative) or (exclude(relative) if exclude else None)
        if reason:
            local.append({'path': relative, 'reason': reason})
            continue
        with open(unit_dir / relative, 'rb') as handle:
            sha256, length, secret = scan_stream(handle)
        target = f'{unit}/{relative}'
        if target in present and matches(present[target], sha256, length, unit_dir / relative):
            files.append({'path': relative, 'bytes': length, 'sha256': sha256, 'dataset_path': target,
                          'already_present': True,
                          'kept_locally': keep_all or keep_locally(unit, relative, length)})
            continue
        problems = ['credential-like content'] if secret else []
        if relative.endswith(('.eval', '.tar.gz', '.tgz', '.tar')):
            problems += scan_members(unit_dir / relative)
        if problems:
            row = {'path': relative, 'bytes': length, 'sha256': sha256, 'problems': problems[:5],
                   'problem_count': len(problems), 'kept_locally': True}
            if all(x.startswith('credential file name in member ') for x in problems) and not dry_run:
                repacked = Path(work) / unit.replace('/', '__') / 'repacked' / relative
                skipped = repack(unit_dir / relative, repacked)
                with open(repacked, 'rb') as handle:
                    r_sha, r_len, r_secret = scan_stream(handle)
                if not r_secret and not scan_members(repacked):
                    row.update({'repacked_dataset_path': f'{unit}/{stamp}/repacked/{relative}',
                                'repacked_sha256': r_sha, 'repacked_bytes': r_len,
                                'repacked_without': skipped, '_local': repacked})
            excluded.append(row)
            continue
        files.append({'path': relative, 'bytes': length, 'sha256': sha256,
                      'kept_locally': keep_all or keep_locally(unit, relative, length)})
    log(f'{unit}: {len(files)} files to record, {len(local)} local-only, {len(excluded)} excluded by the screen')
    previous_file = unit_dir / 'OFFLOADED.json'
    if previous_file.is_file():  # a rerun: files already recorded with the same bytes are not uploaded again
        earlier = {r['path']: r for r in json.loads(previous_file.read_text()).get('files', [])}
        for row in files:
            old = earlier.get(row['path'])
            if (not row.get('already_present') and old and old['sha256'] == row['sha256']
                    and old['dataset_path'] in present):
                row.update({k: old[k] for k in ('dataset_path', 'archive_member') if k in old})
                row['already_present'] = True
    direct, packed = {}, {}
    for row in files:
        if row.get('already_present'):
            continue
        target = f'{unit}/{row["path"]}'
        if target in present and matches(present[target], row['sha256'], row['bytes'], unit_dir / row['path']):
            row['dataset_path'] = target
            row['already_present'] = True
            continue
        if row['bytes'] >= DIRECT_MIN:
            if target in present:
                target = f'{unit}/{stamp}/files/{row["path"]}'
                if target in present and not matches(present[target], row['sha256'], row['bytes']):
                    raise ValueError('Dataset already holds other bytes at ' + target)
            row['dataset_path'] = target
            if target in present:
                row['already_present'] = True
            else:
                direct[target] = unit_dir / row['path']
        else:
            top = PurePosixPath(row['path']).parts[0] if '/' in row['path'] else '_files'
            packed.setdefault(top, []).append(row)
    archives = {}
    work_dir = Path(work) / unit.replace('/', '__')
    for top, rows in sorted(packed.items()):
        parts, current, size = [], [], 0
        for row in rows:
            if current and size + row['bytes'] > ARCHIVE_MAX:
                parts.append(current)
                current, size = [], 0
            current.append(row)
            size += row['bytes']
        if current:
            parts.append(current)
        for number, part in enumerate(parts, 1):
            name = re.sub(r'[^A-Za-z0-9._-]', '_', top).lstrip('.') or 'entry'
            name = f'{name}.part{number}.tar.gz' if len(parts) > 1 else f'{name}.tar.gz'
            target = f'{unit}/{stamp}/{name}'
            local_archive = work_dir / name
            if not dry_run:
                build_archive(unit_dir, [(r['path'], r['sha256'], r['bytes']) for r in part], local_archive)
                with open(local_archive, 'rb') as handle:
                    sha256, length, secret = scan_stream(handle)
                if secret:
                    raise ValueError('credential-like bytes in the packed archive ' + target)
                version = 1
                while target in present and not matches(present[target], sha256, length, local_archive):
                    version += 1  # never overwrite: a later archive of the same entry gets a new name
                    target = f'{unit}/{stamp}/{name[:-len(".tar.gz")]}.v{version}.tar.gz'
                archives[target] = {'local': local_archive, 'sha256': sha256, 'bytes': length,
                                    'members': len(part), 'already_present': target in present}
            for row in part:
                row['dataset_path'] = target
                row['archive_member'] = row['path']
    uploads = {t: p for t, p in direct.items()}
    uploads.update({t: a['local'] for t, a in archives.items() if not a['already_present']})
    for row in excluded:
        if '_local' in row:
            target = row['repacked_dataset_path']
            if target in present:
                if not matches(present[target], row['repacked_sha256'], row['repacked_bytes'], row['_local']):
                    raise ValueError('Dataset already holds other bytes at ' + target)
            else:
                uploads[target] = row['_local']
    upload_bytes = sum(p.stat().st_size for p in uploads.values()) if not dry_run else sum(
        r['bytes'] for r in files if 'already_present' not in r)
    log(f'{unit}: {len(direct)} direct + {len(archives) or len(packed)} archives to upload, '
        f'{upload_bytes / 1e6:.1f} MB')
    if dry_run:
        shutil.rmtree(work_dir, ignore_errors=True)
        return {'unit': unit, 'dry_run': True, 'files': len(files), 'upload_bytes': upload_bytes,
                'local_only': len(local), 'excluded': excluded}
    state = {'unit': unit, 'repo': repo, 'files': files, 'direct': direct, 'archives': archives,
             'excluded': excluded, 'local': local, 'uploads': uploads, 'present': sorted(present),
             'work_dir': work_dir, 'head': info.sha, 'unit_dir': unit_dir, 'stamp': stamp}
    if prepare_only:
        return state
    revision = commit_uploads(api, repo, uploads, set(present), f'{unit} ({stamp})') if uploads else \
        hf_visibility.check_dataset(api, repo).sha
    return finish_unit(state, revision, log=log)


def commit_uploads(api, repo, uploads, present, label, message='Offload local run records'):
    """One guarded commit adding ``uploads`` ({dataset path: local Path}); pinned to the head just read
    (private visibility checked) and retried on a concurrent change or rate limit. Never overwrites: a target
    that appeared since planning stops the commit."""
    from huggingface_hub import CommitOperationAdd
    from huggingface_hub.utils import HfHubHTTPError
    operations = [CommitOperationAdd(path_in_repo=t, path_or_fileobj=str(p)) for t, p in sorted(uploads.items())]
    # Upload the data first in parallel sessions (no commit; one HF upload session is slow), then commit once.
    groups = [operations[i::6] for i in range(6) if operations[i::6]]
    with concurrent.futures.ThreadPoolExecutor(len(groups)) as threads:
        for future in [threads.submit(_preupload, api, repo, group) for group in groups]:
            future.result()
    prefixes = sorted({'/'.join(t.split('/')[:2]) if t.startswith('phase-b/') else t.split('/')[0] for t in uploads})
    for attempt in range(40):
        current = hf_visibility.check_dataset(api, repo)
        now_present = set()
        for prefix in prefixes:
            now_present |= set(remote_tree(api, repo, current.sha, prefix))
        clash = [t for t in uploads if t in now_present and t not in present]
        if clash:
            raise ValueError('Paths appeared in the dataset concurrently: ' + ', '.join(clash[:5]))
        try:
            return api.create_commit(repo_id=repo, repo_type='dataset', parent_commit=current.sha,
                                     operations=operations,
                                     commit_message=f'{message}: {label}').oid
        except HfHubHTTPError as error:
            status = getattr(error.response, 'status_code', None)
            if status == 404 and revision_not_found(error):
                status = 409  # a head another uploader just committed, not yet visible here: retry
            if status in (409, 412, 429, 500, 502, 503, 504) and attempt < 39:
                wait = 5 + attempt
                if status == 429:
                    match = re.search(r'about (\d+) hour|Retry after (\d+) seconds', str(error))
                    wait = 300 if match is None else (3600 if match.group(1) else int(match.group(2)) + 5)
                print(f'commit {label}: HTTP {status}, retrying in {wait} s', flush=True)
                time.sleep(wait)
                continue
            raise
    raise ValueError('Commit did not succeed')


def _preupload(api, repo, operations):
    for attempt in range(10):
        try:
            return api.preupload_lfs_files(repo, additions=operations, repo_type='dataset')
        except Exception as error:  # transient network or rate errors: the commit stage re-checks everything
            print(f'preupload retry {attempt + 1}: {type(error).__name__}', flush=True)
            time.sleep(30 * (attempt + 1))
    raise ValueError('preupload failed')


def finish_unit(state, revision, log=print):
    """Verify a unit's dataset copies at ``revision``, write OFFLOADED.json, delete the offloaded files."""
    from huggingface_hub import HfApi
    api = HfApi()
    unit, repo, files, direct, archives = (state[k] for k in ('unit', 'repo', 'files', 'direct', 'archives'))
    excluded, local, uploads, work_dir = (state[k] for k in ('excluded', 'local', 'uploads', 'work_dir'))
    unit_dir = state.get('unit_dir') or RUNS / unit
    stamp = state.get('stamp', STAMP)
    final = remote_tree(api, repo, revision, unit)
    bad = []
    for target, path in direct.items():
        row = next(r for r in files if r.get('dataset_path') == target)
        if target not in final or not verified(api, repo, revision, target, final[target], row['sha256'], row['bytes']):
            bad.append(target)
    for target, archive in archives.items():
        if target not in final or not verified(api, repo, revision, target, final[target], archive['sha256'],
                                               archive['bytes']):
            bad.append(target)
    for row in excluded:
        target = row.get('repacked_dataset_path')
        if target and (target not in final or not verified(api, repo, revision, target, final[target],
                                                             row['repacked_sha256'], row['repacked_bytes'])):
            bad.append(target)
        row.pop('_local', None)
    for row in files:
        if row.get('already_present') and row['dataset_path'] not in final:
            bad.append(row['dataset_path'])
    if bad:
        raise ValueError(f'{unit}: dataset copies missing or differing after commit {revision}: {bad[:10]}')
    manifest = {
        'schema': SCHEMA, 'unit': unit, 'created': now(), 'dataset': 'GDM_LOGS_DATASET (private; HANDOFF.local.md)',
        'dataset_commit': revision, 'stamp': stamp,
        'note': 'Restore: python scripts/offload_runs.py restore --unit ' + unit + ' [--prefix PATH]',
        'archives': {t: {'sha256': a['sha256'], 'bytes': a['bytes'], 'members': a['members']}
                     for t, a in sorted(archives.items())},
        'files': sorted(files, key=lambda r: r['path']),
        'local_only': local, 'excluded_by_screen': excluded,
    }
    previous = unit_dir / 'OFFLOADED.json'
    if previous.is_file():
        old = json.loads(previous.read_text())
        manifest['previous'] = [*old.get('previous', []), {k: old[k] for k in ('created', 'dataset_commit')}]
        seen = {r['path'] for r in files}
        manifest['files'] += [r for r in old.get('files', []) if r['path'] not in seen and not r['kept_locally']]
        manifest['files'].sort(key=lambda r: r['path'])
        for t, a in old.get('archives', {}).items():
            manifest['archives'].setdefault(t, a)
    previous.write_text(json.dumps(manifest, separators=(',', ':')) + '\n')  # compact: up to 25k rows
    freed = 0
    for row in files:
        if row['kept_locally']:
            continue
        path = unit_dir / row['path']
        if path.is_file() and path.stat().st_size == row['bytes']:
            path.unlink()
            freed += row['bytes']
    for directory in sorted((p for p in unit_dir.rglob('*') if freed and p.is_dir() and not p.is_symlink()),
                            key=lambda p: len(p.parts), reverse=True):  # only folders this offload emptied
        try:
            if not any(directory.iterdir()) or all(c.name == '.DS_Store' for c in directory.iterdir()):
                for c in directory.iterdir():
                    c.unlink()
                directory.rmdir()
        except OSError:
            pass
    shutil.rmtree(work_dir, ignore_errors=True)
    kept = sum(r['bytes'] for r in files if r['kept_locally'])
    log(f'{unit}: commit {revision}, freed {freed / 1e6:.1f} MB, kept {kept / 1e6:.1f} MB')
    return {'unit': unit, 'commit': revision, 'uploaded': len(uploads), 'files': len(files),
            'offloaded': sum(1 for r in files if not r['kept_locally']), 'freed_bytes': freed, 'kept_bytes': kept,
            'local_only': len(local), 'excluded': excluded}


def verified(api, repo, revision, target, entry, sha256, size):
    if entry[0] != size:
        return False
    if entry[1] is not None:
        return entry[1] == sha256
    from huggingface_hub import hf_hub_download
    with tempfile.TemporaryDirectory() as folder:
        path = hf_hub_download(repo, target, repo_type='dataset', revision=revision, local_dir=folder)
        return file_sha256(Path(path)) == sha256


def all_units():
    """Units not offloaded yet (no OFFLOADED.json); name a unit with --unit to run it again."""
    units = []
    for path in sorted(RUNS.iterdir()):
        if path.is_dir() and path.name not in SKIP_UNITS and not (path / 'OFFLOADED.json').is_file():
            units.append(path.name)
    for path in sorted((RUNS / 'phase-b').iterdir()):
        if path.is_dir() and path.name != STAMP and not (path / 'OFFLOADED.json').is_file():
            units.append('phase-b/' + path.name)
    return units


def _prepare(args):
    unit, repo, work, keep_all = args
    try:
        return offload_unit(unit, repo=repo, work=work, keep_all=keep_all, prepare_only=True,
                            log=lambda m: print(m, flush=True))
    except Exception as error:
        return {'unit': unit, 'error': f'{type(error).__name__}: {error}', 'trace': traceback.format_exc()}


def batched(units, repo, work, workers, keep_all, report, max_bytes=3_000_000_000, max_units=40):
    """Prepare units in parallel, then add them to the dataset in few commits (HF allows 128 commits per
    hour per repository), then verify and finish each unit."""
    from huggingface_hub import HfApi
    api = HfApi()
    results, batch = [], []

    def flush():
        if not batch:
            return
        uploads, present = {}, set()
        for state in batch:
            uploads.update(state['uploads'])
            present |= set(state['present'])
        label = f'{len(batch)} units ({STAMP})'
        revision = (commit_uploads(api, repo, uploads, present, label) if uploads
                    else hf_visibility.check_dataset(api, repo).sha)
        print(f'batch commit {revision}: {len(batch)} units, {len(uploads)} files, '
              f'{sum(p.stat().st_size for p in uploads.values()) / 1e6:.1f} MB', flush=True)
        for state in batch:
            try:
                results.append(finish_unit(state, revision, log=lambda m: print(m, flush=True)))
            except Exception as error:
                results.append({'unit': state['unit'], 'error': f'{type(error).__name__}: {error}',
                                'trace': traceback.format_exc()})
                print(f'ERROR {state["unit"]}: {error}', flush=True)
        batch.clear()
        if report:
            dataset_logs.write_json(report, {'created': now(), 'results': results})

    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        pending = list(units)
        running = set()
        while pending or running:
            while pending and len(running) < workers:
                running.add(pool.submit(_prepare, (pending.pop(0), repo, work, keep_all)))
            done, running = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                state = future.result()
                if 'error' in state:
                    results.append(state)
                    print(f'ERROR {state["unit"]}: {state["error"]}', flush=True)
                    continue
                batch.append(state)
            size = sum(p.stat().st_size for s in batch for p in s['uploads'].values())
            if batch and (size >= max_bytes or len(batch) >= max_units or not (pending or running)):
                flush()
    flush()
    return results


def _worker(args):
    unit, repo, work, dry_run, keep_all = args
    lines = []
    try:
        result = offload_unit(unit, repo=repo, work=work, dry_run=dry_run, keep_all=keep_all,
                              log=lambda m: (lines.append(m), print(m, flush=True)))
        return {**result, 'log': lines}
    except Exception as error:
        return {'unit': unit, 'error': f'{type(error).__name__}: {error}', 'trace': traceback.format_exc(),
                'log': lines}


def restore(unit, prefix='', force=False, cache=None, dest=None):
    """Write the unit's offloaded files (under ``prefix``) back from the dataset, each checked by SHA-256, into
    runs/UNIT, or into ``dest`` (a scratch folder: fetch only what one step needs and delete it afterwards)."""
    from huggingface_hub import hf_hub_download
    repo = hf_visibility.DATASET_REPO
    manifest = json.loads((RUNS / unit / 'OFFLOADED.json').read_text())
    unit_dir = Path(dest) if dest else RUNS / unit
    wanted = [r for r in manifest['files'] if r['path'].startswith(prefix)
              and (force or not (unit_dir / r['path']).exists())]
    by_object = {}
    for row in wanted:
        by_object.setdefault(row['dataset_path'], []).append(row)
    cache = Path(cache or tempfile.mkdtemp(prefix='offload-restore-'))
    restored = 0
    for target, rows in sorted(by_object.items()):
        commit = manifest['dataset_commit']
        local = Path(hf_hub_download(repo, target, repo_type='dataset', revision=commit, local_dir=cache))
        if any('archive_member' in r for r in rows):
            members = {r['archive_member']: r for r in rows}
            with tarfile.open(local, 'r:gz') as archive:
                for info in archive:
                    row = members.get(info.name)
                    if row is None or not info.isreg():
                        continue
                    data = archive.extractfile(info).read()
                    if hashlib.sha256(data).hexdigest() != row['sha256']:
                        raise ValueError('SHA-256 mismatch for ' + row['path'])
                    destination = unit_dir / row['path']
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    restored += 1
        else:
            row = rows[0]
            if file_sha256(local) != row['sha256']:
                raise ValueError('SHA-256 mismatch for ' + row['path'])
            destination = unit_dir / row['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, destination)
            restored += 1
        local.unlink()
    print(f'{unit}: restored {restored} of {len(wanted)} files from dataset commit {manifest["dataset_commit"]}')
    return restored


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    off = sub.add_parser('offload')
    off.add_argument('--unit', action='append', default=[])
    off.add_argument('--all', action='store_true')
    off.add_argument('--workers', type=int, default=4)
    off.add_argument('--dry-run', action='store_true')
    off.add_argument('--keep-all', action='store_true', help='upload only; delete nothing locally')
    off.add_argument('--batch', action='store_true', help='few large commits (HF: 128 commits per hour)')
    off.add_argument('--work', default=None, help='scratch folder for archives')
    off.add_argument('--report', default=None)
    res = sub.add_parser('restore')
    res.add_argument('--unit', required=True)
    res.add_argument('--prefix', default='')
    res.add_argument('--force', action='store_true')
    res.add_argument('--cache', default=None)
    res.add_argument('--dest', default=None, help='write into this folder instead of runs/UNIT')
    args = parser.parse_args()
    if hf_visibility.DATASET_REPO == 'HF-ORG/LOGS-DATASET':
        parser.error('Set GDM_LOGS_DATASET (HANDOFF.local.md)')
    if args.command == 'restore':
        restore(args.unit, args.prefix, args.force, args.cache, args.dest)
        return
    units = all_units() if args.all else args.unit
    work = args.work or tempfile.mkdtemp(prefix='offload-work-')
    if args.batch and not args.dry_run:
        results = batched(units, hf_visibility.DATASET_REPO, work, args.workers, args.keep_all, args.report)
        failed = [r['unit'] for r in results if 'error' in r]
        print(f'{len(results) - len(failed)} units done, {len(failed)} failed: {failed}')
        sys.exit(1 if failed else 0)
    jobs = [(u, hf_visibility.DATASET_REPO, work, args.dry_run, args.keep_all) for u in units]
    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_worker, job) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            if 'error' in result:
                print(f'ERROR {result["unit"]}: {result["error"]}', flush=True)
            if args.report:
                dataset_logs.write_json(args.report, {'created': now(), 'results': results})
    failed = [r['unit'] for r in results if 'error' in r]
    print(f'{len(results) - len(failed)} units done, {len(failed)} failed: {failed}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
