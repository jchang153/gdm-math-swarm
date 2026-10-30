#!/usr/bin/env python3
"""Upload one finished run from the VM straight to the private logs dataset (owner decision, October 29, 2026:
docs/plan.md, "Runs go from the VM to the logs dataset, never to the owner's computer").

scripts/supervise_run.py runs this on the VM at the end of every run (also after a failed or stopped run),
before it packages the small core the operator fetches. It reuses scripts/offload_runs.py: the same dataset
layout (``RUN/<path>``; files of 8 MB or more at their own path, smaller ones packed deterministically into
``RUN/<stamp>/<top-level entry>.tar.gz``), the same credential screen (members of Inspect logs and tar archives
included; a resume ``state.tar.gz`` whose only problem is the agents' ``~/.agent/sessions/*.key`` files is
uploaded as a repacked copy without them), the same private-visibility check before every commit, add-only
commits pinned to the head, and verification of every dataset copy by SHA-256. Budget ledgers, dot-files,
symlinks and credential-named files never leave the VM. Nothing is deleted on the VM.

Units, all in ONE data commit (HF allows 128 commits per hour per repository), then one small commit with
the manifests:

- ``runs/RUN`` -> dataset ``RUN/...`` (manifest ``runs/RUN/OFFLOADED.json``, copy at ``RUN/<stamp>/OFFLOADED.json``);
- ``runs/phase-b/RUN`` (the supervisor's records) -> ``phase-b/RUN/...``, without the packaged tarballs, the
  site bundle and Inspect View bundle (derived) and the files still being written;
- with ``--site-bundle``: the prepared site bundle's ``dataset-files/`` (prepare_hf_library.py: Inspect logs,
  raw records, ``snapshots.tar.gz``), each at its exact dataset path, so the Cloudflare pages published from that
  bundle with ``--dataset-commit`` link files that exist. An existing dataset file with the same bytes is reused;
  other bytes at the same path stop the upload (never overwritten).

The HF credential is the VM host's own fine-grained token (``HF_TOKEN_PATH``, set by the supervisor from
``~/.config/gdm-math-swarm/hf-upload-token``); it is never printed, copied or passed to containers. The
dataset is ``GDM_LOGS_DATASET``. The outcome is written to ``--record`` (JSON).

    PYTHONPATH=scripts:src:. HF_TOKEN_PATH=~/.config/gdm-math-swarm/hf-upload-token GDM_LOGS_DATASET=... \\
        .venv/bin/python scripts/upload_run.py --run-dir runs/RUN --phase-dir runs/phase-b/RUN \\
        [--site-bundle runs/phase-b/RUN/site-bundle] [--record runs/phase-b/RUN/hf-upload.json]

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
import offload_runs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'gdm-run-upload/1'
DATASET_DIR = 'dataset-files'
# Phase-folder files that are packaged for the operator, derived, or still being written during the upload.
PHASE_SKIP_TOP = ('site-bundle', 'inspect-view')
PHASE_SKIP_FILES = {'upload.log', 'upload.exit', 'hf-upload.json', 'supervisor-status.json',
                    'supervisor-console.log', 'retrieved.marker', 'completed-checksums.json'}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


UPLOAD_ATTEMPTS, RETRY_SECONDS = 5, 30


def stamp_for(moment=None):
    moment = moment or datetime.datetime.now(datetime.timezone.utc)
    return 'upload-' + moment.strftime('%Y%m%dT%H%M%SZ')


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
    runs_root = run_dir.parent
    units.append((run_dir.name, runs_root, None))
    if phase_dir is not None and Path(phase_dir).is_dir():
        phase_dir = Path(phase_dir).resolve()
        if phase_dir.parent.name != 'phase-b':
            raise ValueError('--phase-dir must be a phase-b/NAME folder: ' + str(phase_dir))
        units.append(('phase-b/' + phase_dir.name, phase_dir.parent.parent, phase_exclude))
    return units


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


def screen_extra(files):
    """The offload credential screen for the bundle's dataset files (a failure stops the upload)."""
    for name, path in files.items():
        with open(path, 'rb') as handle:
            if offload_runs.scan_stream(handle)[2]:
                raise ValueError('Credential-like content in bundle dataset file ' + name)
        if name.endswith(('.eval', '.tar.gz', '.tgz', '.tar')):
            problems = offload_runs.scan_members(path)
            if problems:
                raise ValueError(f'{name}: {problems[0]}')


def upload(run_dir, phase_dir=None, site_bundle=None, *, repo=None, work=None, stamp=None, log=print, api=None):
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
    log(f'data commit {revision}: {len(uploads)} files, {uploaded_bytes / 1e6:.1f} MB')
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
    return {'schema': SCHEMA, 'outcome': 'uploaded', 'run': run, 'stamp': stamp,
            'dataset': 'GDM_LOGS_DATASET (private)', 'dataset_commit': revision, 'manifest_commit': record_commit,
            'units': [{k: r.get(k) for k in ('unit', 'commit', 'uploaded', 'files', 'local_only')}
                      | {'excluded_by_screen': len(r.get('excluded') or [])} for r in results],
            'site_dataset_files': len(extra_records), 'uploaded_files': len(uploads),
            'uploaded_bytes': uploaded_bytes}


def offload_unit(*args, **kwargs):  # indirection for tests
    return offload_runs.offload_unit(*args, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--phase-dir', type=Path)
    parser.add_argument('--site-bundle', type=Path, help="prepared bundle whose dataset-files/ go to the dataset")
    parser.add_argument('--record', type=Path, help='write the outcome here (JSON)')
    parser.add_argument('--work', type=Path, help='scratch folder for archives (default: a temporary one)')
    args = parser.parse_args(argv)
    record = {'schema': SCHEMA, 'started_utc': now(), 'run_dir': str(args.run_dir)}
    code = 0
    try:
        if not args.run_dir.is_dir():
            raise ValueError('No run directory: ' + str(args.run_dir))
        bundle = args.site_bundle if args.site_bundle and (args.site_bundle / 'archive-manifest.json').is_file() \
            else None
        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                record.update(upload(args.run_dir, args.phase_dir, bundle, work=args.work,
                                     log=lambda m: print(m, flush=True)))
                break
            except Exception as error:
                # Several VMs upload at once: a head another one just committed can be briefly unknown to
                # this replica ("Revision Not Found"). The upload is add-only, so a fresh attempt is safe.
                if not offload_runs.revision_not_found(error) or attempt == UPLOAD_ATTEMPTS - 1:
                    raise
                print(f'upload attempt {attempt + 1}: {str(error)[:200]}; retrying', flush=True)
                time.sleep(RETRY_SECONDS * (attempt + 1))
    except Exception as error:
        record.update(outcome='failed', error_type=type(error).__name__, detail=str(error)[:2000])
        traceback.print_exc()
        code = 2
    record['finished_utc'] = now()
    if args.record:
        args.record.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({k: record.get(k) for k in ('outcome', 'dataset_commit', 'uploaded_files', 'detail')}))
    return code


if __name__ == '__main__':
    sys.exit(main())
