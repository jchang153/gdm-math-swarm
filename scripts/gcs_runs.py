"""The run archive in a private Google Cloud Storage bucket (owner decision, November 19, 2026: docs/plan.md,
"Run storage: GCS archive, HF viewable subset, nothing local").

The bucket named by ``GDM_RUNS_BUCKET`` (``gs://NAME``; HANDOFF.local.md; on a VM also
``~/.config/gdm-math-swarm/runs-bucket``) is the single complete archive of run data. A unit (a run ``RUN`` or
an operator folder ``phase-b/NAME``, as in scripts/offload_runs.py) lives at ``runs/<unit>/<path>``: one object
per file, the run's own layout. Every object is verified after upload and after download by size and MD5 (CRC32C
when an object has no MD5). The bucket name never appears in tracked files or in the records written here.

Offload (``offload_unit``): the same credential screen as the logs-dataset offload (offload_runs.local_only and
scan_members; dot-files, symlinks and credential-named files never leave the machine; a tar archive whose only
problem is credential-named members goes as a repacked copy without them and the original stays local), except
that budget ledgers are included (the bucket is the complete archive). Files already in the bucket with the same
bytes are not uploaded again; an object with other bytes is replaced only for the index files (INDEX_FILES) or
with ``replace=True`` (the VM's own upload); otherwise the file is a conflict, stays local and is reported. Then
``runs/<unit>/GCS.json`` (a small summary) and the full manifest (``runs/<unit>/_gcs/manifest-<stamp>.json.gz``
in the bucket: every file with bytes, SHA-256 and MD5) are written, and unless ``keep_all`` every uploaded file
except the index is deleted locally.

Restore (``restore_unit``): lists the unit's objects, downloads them (all, or under a prefix) into a scratch
folder beside the destination, verifies each against the listing and moves it into place (existing files are
left alone unless ``force``).

Everything goes through the ``gcloud storage`` CLI (``GCLOUD`` or ``gcloud`` on PATH) with the caller's own
login (the operator's on the Mac, the VM service account on a VM); no credential is read, printed or passed.
"""
import base64
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offload_runs  # noqa: E402
from publish_hf_library import SECRET  # noqa: E402

SCHEMA = 'gdm-run-gcs/1'
MANIFEST_SCHEMA = 'gdm-run-gcs-manifest/1'
RECORD = 'GCS.json'
INTERNAL = '_gcs'          # the unit's manifests in the bucket (never restored as run files)
STAGE = '.gcs-stage'       # hard-linked upload staging under the runs root (a dot-folder: never a unit)
RESTORE_TMP = '.gcs-restore'
BUCKET_FILE = Path.home() / '.config' / 'gdm-math-swarm' / 'runs-bucket'   # VM: the bucket's name
BUCKET_LABEL = 'GDM_RUNS_BUCKET (private; HANDOFF.local.md)'
BUCKET_RE = re.compile(r'gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9](?:/[A-Za-z0-9._/-]*[A-Za-z0-9._-])?')
# The small index a unit keeps locally after an offload (owner decision, November 19, 2026). Index objects are
# replaced in the bucket when their local bytes change (the local copy is authoritative).
INDEX_FILES = frozenset({'results.json', 'settings.json', 'run-budget.json', 'OFFLOADED.json', 'hf-upload.json',
                         RECORD, 'gcs-upload.json'})
CHUNK = 64 * 1024 * 1024
OVERLAP = 4096
SECRET_ENV = re.compile(r'TOKEN|SECRET|PASSWORD|API_KEY|_KEY$|^HF_|HUGGING|ANTHROPIC|OPENAI|GEMINI', re.I)
NO_MATCH = re.compile(r'matched no objects|One or more URLs matched no|No URLs matched', re.I)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def stamp_for(moment=None):
    moment = moment or datetime.datetime.now(datetime.timezone.utc)
    return moment.strftime('%Y%m%dT%H%M%SZ')


def bucket_url(value=None):
    """The archive bucket (``gs://NAME`` with an optional prefix, no trailing slash) or None when not configured:
    ``value``, else GDM_RUNS_BUCKET, else the VM's ~/.config/gdm-math-swarm/runs-bucket."""
    value = (value or os.environ.get('GDM_RUNS_BUCKET') or '').strip()
    if not value and BUCKET_FILE.is_file():
        value = BUCKET_FILE.read_text().strip()
    value = value.rstrip('/')
    if not value:
        return None
    if not BUCKET_RE.fullmatch(value):
        raise ValueError('GDM_RUNS_BUCKET must look like gs://BUCKET[/PREFIX]')
    return value


def unit_key(unit, relative=''):
    """The bucket key of a unit file: ``runs/<unit>/<relative>`` (``runs/phase-b/NAME/...`` for phase folders)."""
    parts = PurePosixPath(unit).parts
    if not parts or any(p in ('', '.', '..') or p.startswith('.') for p in parts) or len(parts) > 2 or (
            len(parts) == 2 and parts[0] != 'phase-b'):
        raise ValueError('Not a run unit: ' + unit)
    return f'runs/{unit}/' + relative


# --- hashing -----------------------------------------------------------------------------------------------

def md5_b64(digest):
    return base64.b64encode(digest).decode()


def scan_file(path):
    """(sha256 hex, md5 base64, bytes, credential-like content found) of a file, scanned in overlapping chunks."""
    sha, md5, total, tail, found = hashlib.sha256(), hashlib.md5(), 0, b'', False
    with open(path, 'rb') as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            sha.update(block)
            md5.update(block)
            total += len(block)
            if not found and SECRET.search(tail + block):
                found = True
            tail = block[-OVERLAP:]
    return sha.hexdigest(), md5_b64(md5.digest()), total, found


def file_md5(path):
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(CHUNK), b''):
            digest.update(block)
    return md5_b64(digest.digest())


# --- the gcloud storage layer ------------------------------------------------------------------------------

class GcloudStore:
    """The bucket through the ``gcloud storage`` CLI. Tests replace it with a fake that has the same methods:
    ``list(prefix)``, ``upload_tree(local_dir, prefix)``, ``download_tree(prefix, local_dir, exclude=None)``,
    ``put(local_file, key)``, ``delete(keys)`` and ``local_crc32c(path)``. Keys are relative to the bucket URL."""

    def __init__(self, bucket=None, runner=subprocess.run, gcloud=None):
        self.bucket = bucket_url(bucket)
        if self.bucket is None:
            raise ValueError('Set GDM_RUNS_BUCKET (gs://...; HANDOFF.local.md)')
        self.runner = runner
        self.gcloud = gcloud or os.environ.get('GCLOUD') or 'gcloud'

    def url(self, key=''):
        return f'{self.bucket}/{key}'

    def env(self):
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        # Single-request uploads keep an MD5 on every object (parallel composite objects have only a CRC32C).
        env.update(CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED='False', CLOUDSDK_CORE_DISABLE_PROMPTS='1')
        return env

    def run(self, arguments, check=True):
        result = self.runner([self.gcloud, 'storage', *arguments], capture_output=True, text=True,
                             env=self.env(), timeout=24 * 3600)
        if check and result.returncode != 0:
            raise RuntimeError(f'gcloud storage {arguments[0]} failed ({result.returncode}): '
                               + (result.stderr or result.stdout or '')[-1500:])
        return result

    def list(self, prefix):
        """{key relative to ``prefix``: {'bytes', 'md5', 'crc32c'}} of the objects under ``prefix``."""
        result = self.run(['objects', 'list', self.url(prefix) + '**',
                           '--format=json(name,size,md5_hash,crc32c_hash)'], check=False)
        if result.returncode != 0:
            if NO_MATCH.search(result.stderr or ''):
                return {}
            raise RuntimeError('gcloud storage objects list failed: ' + (result.stderr or '')[-1500:])
        base = PurePosixPath(self.bucket.split('/', 3)[3]) if self.bucket.count('/') > 2 else None
        rows = {}
        for item in json.loads(result.stdout or '[]'):
            name = item['name']
            if base is not None:
                name = PurePosixPath(name).relative_to(base).as_posix()
            if not name.startswith(prefix) or name.endswith('/'):
                continue
            rows[name[len(prefix):]] = {'bytes': int(item.get('size') or 0), 'md5': item.get('md5_hash'),
                                        'crc32c': item.get('crc32c_hash')}
        return rows

    def upload_tree(self, local_dir, prefix):
        self.run(['rsync', str(local_dir), self.url(prefix.rstrip('/')), '--recursive', '--checksums-only',
                  '--ignore-symlinks'])

    def download_tree(self, prefix, local_dir, exclude=None):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        self.run(['rsync', self.url(prefix.rstrip('/')), str(local_dir), '--recursive',
                  *(['--exclude', exclude] if exclude else [])])

    def put(self, local_file, key):
        self.run(['cp', str(local_file), self.url(key)])

    def delete(self, keys):
        if keys:
            self.run(['rm', *[self.url(k) for k in keys]])

    def local_crc32c(self, path):
        result = self.run(['hash', str(path), '--skip-md5', '--format=json'])
        data = json.loads(result.stdout)
        data = data[0] if isinstance(data, list) else data
        return data.get('crc32c_hash')


def store_for(bucket=None, store=None):
    return store if store is not None else GcloudStore(bucket)


def same(local, remote, path=None, store=None):
    """A local file record ({'bytes', 'md5'}) equals a bucket object (MD5, else CRC32C of ``path``)."""
    if remote is None or remote.get('bytes') != local['bytes']:
        return False
    if remote.get('md5'):
        return remote['md5'] == local['md5']
    if remote.get('crc32c') and path is not None and store is not None:
        return store.local_crc32c(path) == remote['crc32c']
    return False


# --- offload ----------------------------------------------------------------------------------------------

def gcs_local_only(relative):
    """Why a unit file never goes to the bucket, or None: the dataset offload's rule without the ledger
    exclusion (budget ledgers belong in the complete archive)."""
    parts = PurePosixPath(relative).parts
    if any(p.startswith('.') or p == '__pycache__' for p in parts):
        return 'dot-file or cache'
    if offload_runs.credential_name(relative):
        return 'credential or host-only file name'
    try:
        offload_runs.safe_path(relative)
    except ValueError as error:
        return str(error)
    return None


def keep_index(relative):
    return relative in INDEX_FILES


def stage_tree(runs_root, unit, files):
    """A staging folder holding ``files`` {relative: local Path} at their relative paths (hard links on the
    same disk, copies otherwise)."""
    root = Path(runs_root) / STAGE / f'{unit.replace("/", "__")}-{os.getpid()}'
    if root.exists():
        shutil.rmtree(root)
    for relative, source in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    return root


def offload_unit(unit, *, runs_root=None, store=None, bucket=None, keep_all=False, dry_run=False, replace=False,
                 exclude=None, log=print, stamp=None):
    """Upload one unit to the bucket, verify every object, write GCS.json and the bucket manifest and (unless
    ``keep_all``/``dry_run``) delete the uploaded files except the index. ``exclude(relative)`` gives a reason to
    leave a file out of this upload (it stays where it is) or None. Returns a result dict."""
    runs_root = Path(runs_root or offload_runs.RUNS)
    unit_dir = runs_root / unit
    if not unit_dir.is_dir():
        raise ValueError('No such unit: ' + unit)
    store = store_for(bucket, store)
    stamp = stamp or stamp_for()
    prefix = unit_key(unit)
    previous_path = unit_dir / RECORD
    previous = json.loads(previous_path.read_text()) if previous_path.is_file() else {}
    earlier_repacked = {r['path']: r for r in previous.get('excluded_by_screen', []) if r.get('repacked_md5')}
    remote = store.list(prefix)
    files, local, excluded, conflicts, uploads = [], [], [], [], {}
    work = Path(tempfile.mkdtemp(prefix='gcs-offload-'))
    try:
        for relative, size in offload_runs.unit_files(unit_dir):
            if relative == RECORD:
                continue
            if size is None:
                local.append({'path': relative, 'reason': 'symlink'})
                continue
            reason = gcs_local_only(relative) or (exclude(relative) if exclude else None)
            if reason:
                local.append({'path': relative, 'reason': reason})
                continue
            path = unit_dir / relative
            sha256, md5, length, secret = scan_file(path)
            row = {'path': relative, 'bytes': length, 'sha256': sha256, 'md5': md5}
            here = remote.get(relative)
            if same(row, here, path, store):
                files.append({**row, 'already_present': True})
                continue
            problems = ['credential-like content'] if secret else []
            if relative.endswith(('.eval', '.tar.gz', '.tgz', '.tar')):
                problems += offload_runs.scan_members(path)
            if problems:
                entry = {**row, 'problems': problems[:5], 'problem_count': len(problems), 'kept_locally': True}
                earlier = earlier_repacked.get(relative)
                if earlier and earlier.get('sha256') == sha256 and here and here.get('md5') == earlier['repacked_md5']:
                    entry.update({k: earlier[k] for k in ('repacked_md5', 'repacked_bytes', 'repacked_without')})
                    entry['repacked_already_present'] = True
                elif all(p.startswith('credential file name in member ') for p in problems) and not dry_run:
                    repacked = work / 'repacked' / relative
                    skipped = offload_runs.repack(path, repacked)
                    r_sha, r_md5, r_len, r_secret = scan_file(repacked)
                    if not r_secret and not offload_runs.scan_members(repacked):
                        entry.update({'repacked_md5': r_md5, 'repacked_bytes': r_len, 'repacked_sha256': r_sha,
                                      'repacked_without': skipped})
                        uploads[relative] = repacked
                excluded.append(entry)
                continue
            if here is not None and not (replace or keep_index(relative)):
                conflicts.append({**row, 'remote_bytes': here.get('bytes'), 'remote_md5': here.get('md5'),
                                  'kept_locally': True})
                continue
            files.append({**row, **({'replaced': True} if here is not None else {})})
            uploads[relative] = path
        upload_bytes = sum(p.stat().st_size for p in uploads.values())
        log(f'{unit}: {len(files)} files ({len(uploads)} to upload, {upload_bytes / 1e6:.1f} MB), '
            f'{len(local)} local-only, {len(excluded)} excluded by the screen, {len(conflicts)} conflicts')
        if dry_run:
            return {'unit': unit, 'dry_run': True, 'files': len(files), 'upload_files': len(uploads),
                    'upload_bytes': upload_bytes, 'local_only': len(local), 'excluded': excluded,
                    'conflicts': conflicts}
        if uploads:
            staged = stage_tree(runs_root, unit, uploads)
            try:
                store.upload_tree(staged, prefix)
            finally:
                shutil.rmtree(staged, ignore_errors=True)
                try:
                    (runs_root / STAGE).rmdir()
                except OSError:
                    pass
        final = store.list(prefix)
        bad = [r['path'] for r in files if not same(r, final.get(r['path']), unit_dir / r['path'], store)]
        bad += [r['path'] for r in excluded if r.get('repacked_md5') and (
            final.get(r['path']) or {}).get('md5') != r['repacked_md5']]
        if bad:
            raise ValueError(f'{unit}: bucket objects missing or differing after the upload: {bad[:10]}')
        manifest = {'schema': MANIFEST_SCHEMA, 'unit': unit, 'created': now(), 'stamp': stamp,
                    'files': sorted(({k: r[k] for k in ('path', 'bytes', 'sha256', 'md5')} for r in files),
                                    key=lambda r: r['path']),
                    'repacked': [{k: r.get(k) for k in ('path', 'sha256', 'repacked_md5', 'repacked_bytes',
                                                        'repacked_sha256', 'repacked_without')}
                                 for r in excluded if r.get('repacked_md5')]}
        manifest_file = work / 'manifest.json.gz'
        with gzip.GzipFile(manifest_file, 'wb', mtime=0) as handle:
            handle.write(json.dumps(manifest, separators=(',', ':')).encode())
        manifest_key = f'{INTERNAL}/manifest-{stamp}.json.gz'
        store.put(manifest_file, prefix + manifest_key)
        manifest_row = {'bytes': manifest_file.stat().st_size, 'md5': file_md5(manifest_file)}
        if not same(manifest_row, store.list(prefix + INTERNAL + '/').get(manifest_key[len(INTERNAL) + 1:])):
            raise ValueError(f'{unit}: manifest object missing or differing after the upload')
        kept = keep_all
        record = {
            'schema': SCHEMA, 'unit': unit, 'created': now(), 'bucket': BUCKET_LABEL, 'prefix': prefix,
            'stamp': stamp, 'manifest': {'key': prefix + manifest_key, **manifest_row},
            'files': len(files), 'bytes': sum(r['bytes'] for r in files),
            'uploaded': len(uploads), 'uploaded_bytes': upload_bytes,
            'kept_locally': 'all (keep_all)' if kept else sorted(INDEX_FILES),
            'local_only': local, 'excluded_by_screen': excluded, 'conflicts': conflicts,
            'note': f'Restore: python scripts/offload_runs.py restore --from gcs --unit {unit} [--dest DIR]',
        }
        if previous:
            record['previous'] = [*previous.get('previous', []),
                                  {k: previous.get(k) for k in ('created', 'stamp', 'manifest')}]
        previous_path.write_text(json.dumps(record, indent=1) + '\n')
        store.put(previous_path, prefix + RECORD)
        freed = 0
        if not kept:
            for row in files:
                if keep_index(row['path']):
                    continue
                path = unit_dir / row['path']
                if path.is_file() and not path.is_symlink() and path.stat().st_size == row['bytes']:
                    path.unlink()
                    freed += row['bytes']
            for directory in sorted((p for p in unit_dir.rglob('*') if p.is_dir() and not p.is_symlink()),
                                    key=lambda p: len(p.parts), reverse=True):
                try:
                    if all(c.name == '.DS_Store' for c in directory.iterdir()):
                        for c in directory.iterdir():
                            c.unlink()
                        directory.rmdir()
                except OSError:
                    pass
        log(f'{unit}: {len(uploads)} objects uploaded and {len(files)} verified in the bucket; '
            f'freed {freed / 1e6:.1f} MB locally')
        return {'unit': unit, 'backend': 'gcs', 'stamp': stamp, 'files': len(files), 'uploaded': len(uploads),
                'uploaded_bytes': upload_bytes, 'freed_bytes': freed, 'local_only': len(local),
                'excluded': excluded, 'conflicts': conflicts, 'manifest_key': prefix + manifest_key}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def pending(unit, runs_root=None):
    """Local files of ``unit`` an offload to the bucket would upload or delete (index, local-only, screened and
    conflicting files of the last offload with the same size do not count)."""
    unit_dir = Path(runs_root or offload_runs.RUNS) / unit
    record = {}
    if (unit_dir / RECORD).is_file():
        try:
            record = json.loads((unit_dir / RECORD).read_text())
        except (OSError, ValueError):
            record = {}
    held = {r['path']: r.get('bytes') for r in record.get('excluded_by_screen', []) + record.get('conflicts', [])}
    total = 0
    for relative, size in offload_runs.unit_files(unit_dir):
        if size is None or keep_index(relative) or gcs_local_only(relative) or held.get(relative) == size:
            continue
        total += size
    return total


def all_units(runs_root=None):
    """Units with something to offload to the bucket (in-use units and Cloudflare caches left out)."""
    root = Path(runs_root or offload_runs.RUNS)
    candidates = [p.name for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith('.')
                  and p.name not in offload_runs.SKIP_UNITS]
    if (root / 'phase-b').is_dir():
        candidates += ['phase-b/' + p.name for p in sorted((root / 'phase-b').iterdir())
                       if p.is_dir() and not p.name.startswith(('.', 'cloudflare')) and p.name != offload_runs.STAMP]
    return [u for u in candidates if not offload_runs.live_leases(root / u) and pending(u, root)]


# --- restore ----------------------------------------------------------------------------------------------

def restore_unit(unit, *, runs_root=None, dest=None, prefix='', force=False, store=None, bucket=None, log=print):
    """Download the unit's objects (under ``prefix``, a run-relative folder ending in "/" or one file) into
    ``dest`` (default runs/<unit>), each verified against the bucket listing; existing files are kept unless
    ``force``. Returns the number of files written."""
    store = store_for(bucket, store)
    base = unit_key(unit)
    listing = {k: v for k, v in store.list(base).items()
               if not k.startswith(INTERNAL + '/') and k.startswith(prefix)}
    if not listing:
        raise ValueError(f'{unit}: no objects in the bucket under {base}{prefix}')
    target = Path(dest) if dest else Path(runs_root or offload_runs.RUNS) / unit
    target.mkdir(parents=True, exist_ok=True)
    wanted = {k: v for k, v in listing.items() if force or not (target / k).exists()}
    if not wanted:
        log(f'{unit}: nothing to restore ({len(listing)} objects already present)')
        return 0
    scratch = Path(tempfile.mkdtemp(prefix=RESTORE_TMP + '-', dir=target.parent))
    try:
        if prefix and not prefix.endswith('/') and prefix in listing:
            folder = PurePosixPath(prefix).parent.as_posix()
            folder = '' if folder == '.' else folder + '/'
            (scratch / prefix).parent.mkdir(parents=True, exist_ok=True)
            store.download_tree(base + folder, scratch / folder,
                                exclude='^(?!' + re.escape(PurePosixPath(prefix).name) + '$).*')
        else:
            folder = prefix if prefix.endswith('/') or not prefix else prefix.rsplit('/', 1)[0] + '/'
            folder = '' if folder == '/' else folder
            store.download_tree(base + folder, scratch / folder,
                                exclude=None if folder else '^' + re.escape(INTERNAL) + '/')
        written = 0
        for key, remote in sorted(wanted.items()):
            got = scratch / key
            if not got.is_file():
                raise ValueError(f'{unit}: {key} was not downloaded')
            if not same({'bytes': got.stat().st_size, 'md5': file_md5(got)}, remote, got, store):
                raise ValueError(f'{unit}: {key} differs from its bucket object after download')
        for key in sorted(wanted):
            destination = target / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(scratch / key, destination)
            written += 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    log(f'{unit}: restored {written} of {len(listing)} files from the bucket (verified)')
    return written


def restored_paths(unit, dest, store=None, bucket=None):
    """Relative paths of ``dest`` that equal their bucket objects (for --cleanup after an in-place restore)."""
    store = store_for(bucket, store)
    listing = store.list(unit_key(unit))
    found = []
    for key, remote in listing.items():
        path = Path(dest) / key
        if key.startswith(INTERNAL + '/') or keep_index(key) or not path.is_file() or path.is_symlink():
            continue
        if same({'bytes': path.stat().st_size, 'md5': file_md5(path)}, remote, path, store):
            found.append(key)
    return sorted(found)


def has_unit(unit, store=None, bucket=None):
    """True when the bucket holds objects of ``unit``."""
    return bool(store_for(bucket, store).list(unit_key(unit)))
