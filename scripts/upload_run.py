#!/usr/bin/env python3
"""Upload one finished run from the VM: the complete run to the private GCS archive bucket (when one is configured),
and the full run, or only the viewable subset once the run is safely in the bucket, to the private logs dataset
(owner decisions, November 19, 2026: docs/plan.md, "Run storage: GCS archive, HF viewable subset, nothing local"
and its note "Until a bucket exists, HF keeps full runs"; before that, October 29, 2026: "Runs go from the VM to
the logs dataset, never to the owner's computer").

scripts/supervise_run.py runs this on the VM at the end of every run (also after a failed or stopped run), before
it packages the small core the operator fetches. Nothing is deleted on the VM by either target.

``--target gcs`` (scripts/gcs_runs.py, ``offload_unit`` with keep_all and replace: the VM is authoritative):

- ``runs/RUN`` -> bucket ``runs/RUN/...``;
- ``runs/phase-b/RUN`` (the supervisor's records, the site bundle and the Inspect View bundle included: the full
  run) -> bucket ``runs/phase-b/RUN/...``, without the files still being written during this step
  (GCS_SKIP_FILES). ``--phase-only`` re-syncs only the phase unit (after the HF step, so hf-upload.json and
  gcs-upload.json reach the bucket).

Every object is verified by size and MD5; budget ledgers are included (the bucket is the complete archive), while
dot-files, symlinks and credential-named files never leave the VM, and tar archives / Inspect logs are screened
for credential content (gcs_runs.offload_unit). The bucket is GDM_RUNS_BUCKET (else the VM's
~/.config/gdm-math-swarm/runs-bucket); the records name it only as gcs_runs.BUCKET_LABEL. The VM's service
account reaches it through the ``gcloud storage`` CLI (VM access scope storage-rw or cloud-platform, plus bucket
IAM for that service account).

``--target hf`` (default) has two scopes (owner decision, November 19, 2026: "Until a bucket exists, HF keeps full
runs"; ``hf_plan``):

- ``full``, the default unless the run is safely in the bucket: the WHOLE run to the logs dataset exactly as before
  the bucket (``upload_full``, schema gdm-run-upload/1): ``runs/RUN`` -> dataset ``RUN/...`` and
  ``runs/phase-b/RUN`` -> ``phase-b/RUN/...`` (without packaged tarballs, the site and Inspect View bundles and files
  still being written; budget ledgers never), resume bundles (a ``state.tar.gz`` whose only problem is the agents'
  session keys goes up as a repacked copy without them) and snapshots included, plus the site bundle's
  ``dataset-files/`` at their exact paths, in ONE data commit, then a commit with the OFFLOADED.json manifests
  (also written locally);
- ``subset``, only when a bucket is configured (GDM_RUNS_BUCKET or ~/.config/gdm-math-swarm/runs-bucket) AND the
  run's GCS upload succeeded (PHASE/gcs-upload.json, outcome ``uploaded``), or when categories are given
  explicitly: only what the public site links to or the log viewer reads: the prepared site bundle's
  ``dataset-files/`` (prepare_hf_library.py) whose dataset_logs.category is in the HF subset (``--hf-categories``,
  else GDM_HF_CATEGORIES, else dataset_logs.HF_SUBSET_DEFAULT), each at its exact dataset path. Without a site
  bundle, the run's ``inspect/*.eval`` at dataset_logs.dataset_path. Resume bundles, snapshot archives and the rest
  of the run stay in the bucket; no OFFLOADED.json.

Explicit categories (``--hf-categories`` or GDM_HF_CATEGORIES) always select the subset with those categories;
otherwise ``--hf-scope full|subset`` (the supervisor passes it from its own GCS record) wins over the automatic
choice. Both scopes keep the same guards: the credential screen (members of Inspect logs and tar archives
included), the private-visibility check, add-only commits pinned to the head (an existing dataset file with the
same bytes is reused; other bytes at the same path stop the upload, never overwritten) and verification of every
dataset copy by SHA-256.

The HF credential is the VM host's own fine-grained token (``HF_TOKEN_PATH``, set by the supervisor from
``~/.config/gdm-math-swarm/hf-upload-token``); it is never printed, copied or passed to containers or to the GCS
step. The dataset is ``GDM_LOGS_DATASET``. The outcome is written to ``--record`` (JSON).

    PYTHONPATH=scripts:src:. GDM_RUNS_BUCKET=gs://... .venv/bin/python scripts/upload_run.py --target gcs \\
        --run-dir runs/RUN --phase-dir runs/phase-b/RUN [--phase-only] [--record runs/phase-b/RUN/gcs-upload.json]
    PYTHONPATH=scripts:src:. HF_TOKEN_PATH=~/.config/gdm-math-swarm/hf-upload-token GDM_LOGS_DATASET=... \\
        .venv/bin/python scripts/upload_run.py --run-dir runs/RUN --phase-dir runs/phase-b/RUN \\
        [--site-bundle runs/phase-b/RUN/site-bundle] [--hf-scope auto|full|subset] [--hf-categories eval,jsonl] \\
        [--record runs/phase-b/RUN/hf-upload.json]

Exit 0: uploaded and verified; 2: failed (recorded; the run stays complete on the VM, rerun this command).
"""
import argparse
import datetime
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dataset_logs  # noqa: E402
import gcs_runs  # noqa: E402
import offload_runs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'gdm-run-upload/2'          # the viewable subset (fetch_run and publish_cloudflare read the schema)
FULL_SCHEMA = 'gdm-run-upload/1'     # the whole run, as before the bucket (OFFLOADED.json manifests)
GCS_SCHEMA = 'gdm-run-gcs-upload/1'
DATASET_DIR = 'dataset-files'
# Phase-folder files still being written while the GCS step (or its --phase-only re-sync) runs.
GCS_SKIP_FILES = frozenset({'gcs-upload.log', 'gcs-upload.exit', 'gcs-sync.log', 'gcs-sync.exit',
                            'supervisor-status.json', 'supervisor-console.log', 'retrieved.marker'})
GCS_RECORD = 'gcs-upload.json'
# Full scope: phase-folder files that are packaged for the operator, derived, or still being written during the upload.
PHASE_SKIP_TOP = ('site-bundle', 'inspect-view')
PHASE_SKIP_FILES = {'upload.log', 'upload.exit', 'hf-upload.json', 'supervisor-status.json',
                    'supervisor-console.log', 'retrieved.marker', 'completed-checksums.json',
                    'gcs-sync.log', 'gcs-sync.exit'}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


UPLOAD_ATTEMPTS, RETRY_SECONDS = 5, 30


def stamp_for(moment=None):
    moment = moment or datetime.datetime.now(datetime.timezone.utc)
    return 'upload-' + moment.strftime('%Y%m%dT%H%M%SZ')


def phase_unit(phase_dir):
    """(unit name, runs root) of a phase-b/NAME folder."""
    phase_dir = Path(phase_dir).resolve()
    if phase_dir.parent.name != 'phase-b':
        raise ValueError('--phase-dir must be a phase-b/NAME folder: ' + str(phase_dir))
    return 'phase-b/' + phase_dir.name, phase_dir.parent.parent


# --- GCS: the complete run --------------------------------------------------------------------------------

def gcs_phase_exclude(phase_only):
    """The phase unit's exclusion: files still being written during the step (the full step's own record too)."""
    def exclude(relative):
        parts = PurePosixPath(relative).parts
        if len(parts) == 1 and (parts[0] in GCS_SKIP_FILES or (not phase_only and parts[0] == GCS_RECORD)):
            return 'still being written during the GCS upload'
        return None
    return exclude


def upload_gcs(run_dir, phase_dir, *, store=None, stamp=None, log=print, phase_only=False):
    """The run unit (unless ``phase_only``) and the phase unit to the archive bucket, verified; nothing deleted."""
    store = store if store is not None else gcs_runs.GcloudStore()
    stamp = stamp or gcs_runs.stamp_for()
    run_dir = Path(run_dir).resolve()
    units = [] if phase_only else [(run_dir.name, run_dir.parent, None)]
    if phase_dir is not None and Path(phase_dir).is_dir():
        unit, root = phase_unit(phase_dir)
        units.append((unit, root, gcs_phase_exclude(phase_only)))
    elif phase_only:
        raise ValueError('--phase-only needs an existing --phase-dir')
    results = []
    for unit, runs_root, exclude in units:
        result = gcs_runs.offload_unit(unit, runs_root=runs_root, store=store, keep_all=True, replace=True,
                                       exclude=exclude, log=log, stamp=stamp)
        results.append(result)
    return {'schema': GCS_SCHEMA, 'outcome': 'uploaded', 'target': 'gcs', 'run': run_dir.name, 'stamp': stamp,
            'phase_only': phase_only, 'bucket': gcs_runs.BUCKET_LABEL,
            'units': [{'unit': r['unit'], 'files': r['files'], 'uploaded': r['uploaded'],
                       'uploaded_bytes': r['uploaded_bytes'], 'local_only': r['local_only'],
                       'excluded_by_screen': len(r.get('excluded') or []),
                       'conflicts': len(r.get('conflicts') or []), 'manifest_key': r['manifest_key']}
                      for r in results],
            'manifest_keys': [r['manifest_key'] for r in results],
            'uploaded_files': sum(r['uploaded'] for r in results),
            'uploaded_bytes': sum(r['uploaded_bytes'] for r in results)}


# --- HF: which scope (owner decision, November 19, 2026: "Until a bucket exists, HF keeps full runs") ------------

def gcs_archived(phase_dir):
    """(True, why) when a bucket is configured and PHASE/gcs-upload.json says the run's complete GCS upload
    succeeded; else (False, why)."""
    try:
        bucket = gcs_runs.bucket_url()
    except ValueError:
        bucket = None
    if not bucket:
        return False, 'no archive bucket configured (GDM_RUNS_BUCKET or ~/.config/gdm-math-swarm/runs-bucket)'
    try:
        record = json.loads((Path(phase_dir) / GCS_RECORD).read_text()) if phase_dir else None
    except (OSError, ValueError):
        record = None
    if not isinstance(record, dict):
        return False, 'no GCS upload record for this run'
    if record.get('outcome') != 'uploaded' or record.get('phase_only') or record.get('exit', 0) != 0:
        return False, f'GCS upload did not succeed (outcome {record.get("outcome")!r})'
    return True, 'the complete run is in the GCS archive bucket'


def hf_plan(override=None, scope='auto', phase_dir=None):
    """(categories, scope 'full' or 'subset', reason) of the HF upload. Explicit categories (``override``, else
    GDM_HF_CATEGORIES) select the subset with them; then ``scope`` 'full' or 'subset'; 'auto' picks the subset only
    when ``gcs_archived(phase_dir)``, else the full run."""
    text = override if override is not None else os.environ.get('GDM_HF_CATEGORIES')
    if text is not None and str(text).strip():
        return dataset_logs.hf_categories(text), 'subset', 'explicit categories (--hf-categories or GDM_HF_CATEGORIES)'
    if scope == 'subset':
        return frozenset(dataset_logs.HF_SUBSET_DEFAULT), 'subset', '--hf-scope subset'
    if scope == 'full':
        return frozenset(dataset_logs.CATEGORIES), 'full', '--hf-scope full'
    archived, why = gcs_archived(phase_dir)
    if archived:
        return frozenset(dataset_logs.HF_SUBSET_DEFAULT), 'subset', why
    return frozenset(dataset_logs.CATEGORIES), 'full', why


# --- HF: the whole run (as before the bucket) ---------------------------------------------------------------

def phase_exclude(relative):
    parts = PurePosixPath(relative).parts
    if parts[0] in PHASE_SKIP_TOP:
        return 'derived bundle (site bundle or Inspect View): its dataset files are uploaded separately'
    if len(parts) == 1 and (parts[0] in PHASE_SKIP_FILES or parts[0].startswith('completed-')):
        return 'packaged for the operator or still being written'
    return None


def units_for(run_dir, phase_dir):
    """[(unit name relative to the runs root, runs root, exclude)] for the run and its phase folder."""
    run_dir, units = Path(run_dir).resolve(), []
    units.append((run_dir.name, run_dir.parent, None))
    if phase_dir is not None and Path(phase_dir).is_dir():
        unit, root = phase_unit(phase_dir)
        units.append((unit, root, phase_exclude))
    return units


def offload_unit(*args, **kwargs):  # indirection for tests
    return offload_runs.offload_unit(*args, **kwargs)


def upload_full(run_dir, phase_dir=None, site_bundle=None, *, repo=None, work=None, stamp=None, log=print,
                api=None, reason=None):
    """The whole run and its phase records to the logs dataset (the upload before the bucket, unchanged): one data
    commit, then one commit with the OFFLOADED.json manifests; every copy verified by SHA-256."""
    from huggingface_hub import HfApi
    api = api or HfApi()
    repo = repo or offload_runs.hf_visibility.DATASET_REPO
    if repo == 'HF-ORG/LOGS-DATASET':
        raise ValueError('Set GDM_LOGS_DATASET (the private logs dataset)')
    stamp = stamp or stamp_for()
    work = Path(work or tempfile.mkdtemp(prefix='upload-run-'))
    states = []
    for unit, runs_root, exclude in units_for(run_dir, phase_dir):
        states.append(offload_unit(unit, repo=repo, work=work, keep_all=True, prepare_only=True,
                                   runs_root=runs_root, stamp=stamp, exclude=exclude, log=log))
    run = Path(run_dir).resolve().name
    extra = bundle_dataset_files(site_bundle, {run}) if site_bundle else {}
    screen_extra(extra)
    uploads, present = {}, set()
    for state in states:
        for target, path in state['uploads'].items():
            uploads[target] = path
        present |= set(state['present'])
    head = offload_runs.hf_visibility.check_dataset(api, repo).sha
    remote = offload_runs.remote_tree(api, repo, head, run)
    extra_records = {}
    for name, path in extra.items():
        sha256, size = offload_runs.file_sha256(path), path.stat().st_size
        extra_records[name] = {'sha256': sha256, 'bytes': size}
        if name in remote:
            if not offload_runs.matches(remote[name], sha256, size, path):
                raise ValueError('The dataset already holds other bytes at ' + name)
            continue
        if name in uploads and offload_runs.file_sha256(uploads[name]) != sha256:
            raise ValueError('Two different files bound for ' + name)
        uploads[name] = path
    label = f'{run} ({stamp})'
    revision = (offload_runs.commit_uploads(api, repo, uploads, present | set(remote), label,
                                            message='Upload run from its VM') if uploads
                else offload_runs.hf_visibility.check_dataset(api, repo).sha)
    uploaded_bytes = sum(p.stat().st_size for p in uploads.values())   # before finish_unit clears the work dir
    log(f'data commit {revision}: {len(uploads)} files, {uploaded_bytes / 1e6:.1f} MB (full run)')
    results = [offload_runs.finish_unit(state, revision, log=log) for state in states]
    final = offload_runs.remote_tree(api, repo, revision, run)
    bad = [n for n, r in extra_records.items()
           if n not in final or not offload_runs.verified(api, repo, revision, n, final[n], r['sha256'], r['bytes'])]
    if bad:
        raise ValueError(f'bundle dataset files missing or differing after commit {revision}: {bad[:10]}')
    # The manifests themselves, so the dataset is self-describing even without the VM or the operator's core.
    manifests = {}
    for state in states:
        manifest = Path(state['unit_dir']) / 'OFFLOADED.json'
        manifests[f'{state["unit"]}/{stamp}/OFFLOADED.json'] = manifest
    record_commit = offload_runs.commit_uploads(api, repo, manifests, set(), f'{run} manifests ({stamp})',
                                                message='Upload run from its VM')
    shutil.rmtree(work, ignore_errors=True)
    return {'schema': FULL_SCHEMA, 'outcome': 'uploaded', 'target': 'hf', 'hf_scope': 'full',
            'hf_scope_reason': reason, 'hf_categories': sorted(dataset_logs.CATEGORIES), 'run': run, 'stamp': stamp,
            'dataset': 'GDM_LOGS_DATASET (private)', 'dataset_commit': revision, 'manifest_commit': record_commit,
            'units': [{k: r.get(k) for k in ('unit', 'commit', 'uploaded', 'files', 'local_only')}
                      | {'excluded_by_screen': len(r.get('excluded') or [])} for r in results],
            'site_dataset_files': len(extra_records), 'uploaded_files': len(uploads),
            'uploaded_bytes': uploaded_bytes}


# --- HF: the viewable subset ------------------------------------------------------------------------------

def bundle_dataset_files(bundle, runs):
    """{dataset path: local Path} of a prepared site bundle's dataset-files/, checked against its manifest
    (sizes and SHA-256) and confined to the bundle's own runs."""
    root = Path(bundle) / DATASET_DIR
    manifest_path = root / 'manifest.json'
    if not manifest_path.is_file():
        return {}
    manifest = json.loads(manifest_path.read_text())
    files = {}
    for name, info in sorted(manifest.get('files', {}).items()):
        if PurePosixPath(name).parts[0] not in runs or '..' in PurePosixPath(name).parts:
            raise ValueError('Bundle dataset file outside the uploaded run: ' + name)
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Bundle dataset file missing: ' + name)
        if (path.stat().st_size, offload_runs.file_sha256(path)) != (info['bytes'], info['sha256']):
            raise ValueError('Bundle dataset file differs from its manifest: ' + name)
        files[name] = path
    return files


def dataset_category(name):
    """dataset_logs.category of a dataset path ``RUN/<relative>`` (``live/RUN/...`` too)."""
    parts = PurePosixPath(name).parts
    parts = parts[1:] if parts[:1] == ('live',) else parts
    return dataset_logs.category(PurePosixPath(*parts[1:]).as_posix()) if len(parts) > 1 else None


def subset_files(run_dir, site_bundle, categories):
    """({dataset path: local Path} to upload, [dataset paths left out: category not in the subset]) for a run."""
    run_dir = Path(run_dir).resolve()
    run = run_dir.name
    if site_bundle:
        candidates = bundle_dataset_files(site_bundle, {run})
    else:
        candidates = {}
        for path in sorted((run_dir / 'inspect').glob('*.eval')):
            if path.is_file() and not path.is_symlink():
                candidates[dataset_logs.dataset_path(run, 'inspect/' + path.name)] = path
    chosen, left_out = {}, []
    for name, path in candidates.items():
        if dataset_category(name) in categories:
            chosen[name] = path
        else:
            left_out.append(name)
    return chosen, sorted(left_out)


def screen_extra(files):
    """The offload credential screen for the dataset files (a failure stops the upload)."""
    for name, path in files.items():
        with open(path, 'rb') as handle:
            if offload_runs.scan_stream(handle)[2]:
                raise ValueError('Credential-like content in bundle dataset file ' + name)
        if name.endswith(('.eval', '.tar.gz', '.tgz', '.tar')):
            problems = offload_runs.scan_members(path)
            if problems:
                raise ValueError(f'{name}: {problems[0]}')


def upload(run_dir, phase_dir=None, site_bundle=None, *, repo=None, categories=None, stamp=None, log=print,
           api=None, work=None, reason=None):
    """The viewable subset of one run to the logs dataset in one add-only commit, verified. ``phase_dir`` and
    ``work`` are accepted for compatibility and unused (phase records go to the bucket only)."""
    from huggingface_hub import HfApi
    api = api or HfApi()
    repo = repo or offload_runs.hf_visibility.DATASET_REPO
    if repo == 'HF-ORG/LOGS-DATASET':
        raise ValueError('Set GDM_LOGS_DATASET (the private logs dataset)')
    categories = frozenset(categories if categories is not None else dataset_logs.hf_categories())
    stamp = stamp or stamp_for()
    run = Path(run_dir).resolve().name
    selected, left_out = subset_files(run_dir, site_bundle, categories)
    screen_extra(selected)
    head = offload_runs.hf_visibility.check_dataset(api, repo).sha
    remote = offload_runs.remote_tree(api, repo, head, run)
    records, uploads, reused = {}, {}, []
    for name, path in selected.items():
        sha256, size = offload_runs.file_sha256(path), path.stat().st_size
        records[name] = {'sha256': sha256, 'bytes': size}
        if name in remote:
            if not offload_runs.matches(remote[name], sha256, size, path):
                raise ValueError('The dataset already holds other bytes at ' + name)
            reused.append(name)
            continue
        uploads[name] = path
    revision = (offload_runs.commit_uploads(api, repo, uploads, set(remote), f'{run} ({stamp})',
                                            message='Upload run from its VM (viewable subset)') if uploads
                else offload_runs.hf_visibility.check_dataset(api, repo).sha)
    uploaded_bytes = sum(p.stat().st_size for p in uploads.values())
    log(f'data commit {revision}: {len(uploads)} files, {uploaded_bytes / 1e6:.1f} MB '
        f'({len(reused)} already present, {len(left_out)} left out of the subset)')
    final = offload_runs.remote_tree(api, repo, revision, run)
    bad = [n for n, r in records.items()
           if n not in final or not offload_runs.verified(api, repo, revision, n, final[n], r['sha256'], r['bytes'])]
    if bad:
        raise ValueError(f'dataset files missing or differing after commit {revision}: {bad[:10]}')
    counts = {}
    for name in selected:
        kind = dataset_category(name)
        entry = counts.setdefault(kind, {'uploaded': 0, 'already_present': 0})
        entry['uploaded' if name in uploads else 'already_present'] += 1
    left_counts = {}
    for name in left_out:
        kind = str(dataset_category(name))
        left_counts[kind] = left_counts.get(kind, 0) + 1
    return {'schema': SCHEMA, 'outcome': 'uploaded', 'target': 'hf', 'hf_scope': 'subset', 'hf_scope_reason': reason,
            'run': run, 'stamp': stamp, 'dataset': 'GDM_LOGS_DATASET (private)', 'dataset_commit': revision,
            'source': 'site-bundle dataset-files' if site_bundle else 'run inspect/*.eval (no site bundle)',
            'hf_categories': sorted(categories), 'categories': counts,
            'uploaded_categories': sorted(k for k, v in counts.items() if v['uploaded']),
            'skipped_categories': sorted(left_counts), 'left_out_by_category': left_counts,
            'left_out': left_out, 'dataset_files': len(records), 'uploaded_files': len(uploads),
            'already_present_files': len(reused), 'uploaded_bytes': uploaded_bytes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--target', choices=('hf', 'gcs'), default='hf',
                        help='hf: the logs dataset (default; full run or viewable subset, see --hf-scope); gcs: the '
                             'complete run to the archive bucket')
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--phase-dir', type=Path)
    parser.add_argument('--site-bundle', type=Path, help="hf: prepared bundle whose dataset-files/ hold the subset")
    parser.add_argument('--hf-scope', choices=('auto', 'full', 'subset'), default='auto',
                        help='hf: auto (default): the viewable subset only when a bucket is configured and '
                             'PHASE/gcs-upload.json records a successful GCS upload, else the full run')
    parser.add_argument('--hf-categories',
                        help='hf: comma-separated dataset categories to upload (the subset with them; default '
                             'GDM_HF_CATEGORIES, else ' + ','.join(dataset_logs.HF_SUBSET_DEFAULT) + ')')
    parser.add_argument('--phase-only', action='store_true', help='gcs: re-sync only the phase folder')
    parser.add_argument('--record', type=Path, help='write the outcome here (JSON)')
    parser.add_argument('--work', type=Path, help='hf full scope: scratch folder for archives (default: temporary)')
    args = parser.parse_args(argv)
    record = {'schema': GCS_SCHEMA if args.target == 'gcs' else SCHEMA, 'target': args.target,
              'started_utc': now(), 'run_dir': str(args.run_dir)}
    code = 0
    try:
        if not args.run_dir.is_dir():
            raise ValueError('No run directory: ' + str(args.run_dir))
        if args.target == 'gcs':
            record.update(upload_gcs(args.run_dir, args.phase_dir, phase_only=args.phase_only,
                                     log=lambda m: print(m, flush=True)))
        else:
            categories, scope, reason = hf_plan(args.hf_categories, args.hf_scope, args.phase_dir)
            record.update(hf_scope=scope, hf_scope_reason=reason)
            if scope == 'full':
                record['schema'] = FULL_SCHEMA
            print(f'HF upload scope: {scope} ({reason})', flush=True)
            bundle = args.site_bundle if args.site_bundle and (args.site_bundle / 'archive-manifest.json').is_file() \
                else None
            for attempt in range(UPLOAD_ATTEMPTS):
                try:
                    if scope == 'full':
                        record.update(upload_full(args.run_dir, args.phase_dir, bundle, work=args.work, reason=reason,
                                                  log=lambda m: print(m, flush=True)))
                    else:
                        record.update(upload(args.run_dir, args.phase_dir, bundle, categories=categories,
                                             reason=reason, log=lambda m: print(m, flush=True)))
                    break
                except Exception as error:
                    # Several VMs upload at once: a head another one just committed can be briefly unknown to
                    # this replica ("Revision Not Found"). The upload is add-only, so a fresh attempt is safe.
                    if not offload_runs.revision_not_found(error) or attempt == UPLOAD_ATTEMPTS - 1:
                        raise
                    print(f'upload attempt {attempt + 1}: {str(error)[:200]}; retrying', flush=True)
                    time.sleep(RETRY_SECONDS * (attempt + 1))
    except Exception as error:
        record.update(outcome='failed', error_type=type(error).__name__,
                      detail=gcs_runs.BUCKET_RE.sub('gs://BUCKET', str(error))[:2000])
        traceback.print_exc()
        code = 2
    record['finished_utc'] = now()
    if args.record:
        args.record.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({k: record.get(k) for k in ('target', 'outcome', 'dataset_commit', 'uploaded_files',
                                                 'detail')}))
    return code


if __name__ == '__main__':
    sys.exit(main())
