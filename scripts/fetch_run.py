#!/usr/bin/env python3
"""Fetch a finished run's small core from its VM; the full run never comes to this computer (owner decision,
October 29, 2026: docs/plan.md, "Runs go from the VM to the logs dataset, never to the owner's computer").

scripts/supervise_run.py on the VM has already uploaded the whole run to the private logs dataset
(scripts/upload_run.py, PHASE/hf-upload.json) and packaged only:

- ``completed-core.tgz``: the run's core (conference/, evaluations/, oversight/, results.json/html, settings and
  other top-level files up to 1 MiB, OFFLOADED.json) -> ``runs/RUN/``;
- ``completed-phase.tgz``: the supervisor's records, incl. the budget ledger backups that never go to the
  dataset -> ``runs/phase-b/RUN/``;
- ``completed-site.tgz``: the prepared site bundle without its dataset files (those are in the dataset), used
  only in a scratch folder for the Cloudflare publication and deleted afterwards.

This script copies those files over IAP (your own gcloud login; no token, key or HF variable is passed to
gcloud), checks each against ``completed-checksums.json`` (SHA-256), extracts the core and phase records
(regular files only, inside their folders, never replacing a different existing file), touches
``retrieved.marker`` on the VM (the supervisor then shuts an idle VM down) and rebuilds runs/index.html.
With ``--publish`` it also publishes the run to the Cloudflare site, pinned to the dataset commit of the upload
(``publish_cloudflare.py publish --bundle ... --dataset-commit ... --commit``, then ``verify --run``); without it,
it prints the publication preview. The scratch bundle is deleted either way.

    export GCLOUD=... GDM_GCP_PROJECT=... GDM_GCP_ZONE=... GDM_LOGS_DATASET=...   # HANDOFF.local.md
    PYTHONPATH=scripts:src:. .venv/bin/python scripts/fetch_run.py --vm VM --run RUN [--publish]

Files the core leaves in the dataset (Inspect logs, runtime.jsonl, snapshots, judge outputs...) are fetched
only when a step needs them, into a scratch folder: ``offload_runs.py restore --unit RUN --prefix PATH --dest
DIR`` (delete DIR afterwards).
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')
VM_NAME = re.compile(r'[a-z]([-a-z0-9]*[a-z0-9])?')
PACKAGES = ('completed-core.tgz', 'completed-phase.tgz', 'completed-site.tgz')
SECRET_ENV = re.compile(r'TOKEN|SECRET|PASSWORD|API_KEY|_KEY$|^HF_|HUGGING', re.I)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def clean_env():
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env.setdefault('CLOUDSDK_PYTHON', sys.executable)
    return env


class Cloud:
    """gcloud over IAP with the operator's own login; ``runner`` is subprocess.run (tests pass a fake)."""

    def __init__(self, vm, runner=subprocess.run):
        self.vm, self.runner = vm, runner
        self.gcloud = os.environ.get('GCLOUD') or 'gcloud'
        self.flags = [f'--project={os.environ.get("GDM_GCP_PROJECT") or "YOUR-GCP-PROJECT"}',
                      f'--zone={os.environ.get("GDM_GCP_ZONE") or "YOUR-GCP-ZONE"}']

    def scp(self, remote_paths, destination):
        sources = [f'{self.vm}:{p}' for p in remote_paths]
        for attempt in range(4):   # IAP is flaky
            done = self.runner([self.gcloud, 'compute', 'scp', *sources, str(destination), *self.flags,
                                '--tunnel-through-iap', '--quiet'], capture_output=True, text=True, timeout=3600,
                               env=clean_env())
            if done.returncode == 0:
                return
        raise RuntimeError(f'gcloud scp from {self.vm} failed: {done.stderr.strip()[-400:]}')

    def ssh(self, command):
        done = self.runner([self.gcloud, 'compute', 'ssh', self.vm, *self.flags, '--tunnel-through-iap', '--quiet',
                            '--command', command], capture_output=True, text=True, timeout=600, env=clean_env())
        if done.returncode:
            raise RuntimeError(f'gcloud ssh {self.vm} failed: {done.stderr.strip()[-400:]}')
        return done.stdout


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def safe_extract(archive, destination, *, strip=None, force=False):
    """Extract regular files of ``archive`` into ``destination`` (members under ``strip``/ only, that prefix
    removed). Refuses links, absolute paths and ``..``; an existing different file is kept unless ``force``.
    Returns (written, unchanged, kept_different)."""
    destination = Path(destination)
    written, unchanged, kept = [], [], []
    with tarfile.open(archive, 'r:gz') as tar:
        for info in tar:
            name = PurePosixPath(info.name)
            if info.isdir():
                continue
            if not info.isreg() or name.is_absolute() or '..' in name.parts:
                raise ValueError(f'{archive.name}: refused member {info.name}')
            if strip is not None:
                prefix = PurePosixPath(strip)
                if name.parts[:len(prefix.parts)] != prefix.parts:
                    raise ValueError(f'{archive.name}: member outside {strip}/: {info.name}')
                name = PurePosixPath(*name.parts[len(prefix.parts):])
            target = destination / name
            data = tar.extractfile(info).read()
            if target.is_file():
                if target.read_bytes() == data:
                    unchanged.append(str(name))
                    continue
                if not force:
                    kept.append(str(name))
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written.append(str(name))
    return written, unchanged, kept


def fetch(run, cloud, *, remote_root='gdm-math-swarm', publish=False, marker=True, force=False, scratch=None,
          runner=subprocess.run, log=print):
    if not RUN_ID.fullmatch(run):
        raise ValueError('Invalid run ID')
    phase_remote = f'{remote_root}/runs/phase-b/{run}'
    record = {'run': run, 'vm': cloud.vm, 'started_utc': now()}
    with tempfile.TemporaryDirectory(prefix='fetch-run-', dir=scratch) as tmp:
        tmp = Path(tmp)
        cloud.scp([f'{phase_remote}/completed-checksums.json', f'{phase_remote}/hf-upload.json'], tmp)
        checksums = json.loads((tmp / 'completed-checksums.json').read_text())
        upload = json.loads((tmp / 'hf-upload.json').read_text())
        record['hf_upload'] = {k: upload.get(k) for k in ('outcome', 'dataset_commit', 'reason', 'owner_step')}
        names = [n for n in PACKAGES if n in checksums]
        if 'completed-core.tgz' not in names:
            raise ValueError('The VM has no completed-core.tgz for this run (an older supervisor?)')
        cloud.scp([f'{phase_remote}/{n}' for n in names], tmp)
        for name in names:
            if sha256(tmp / name) != checksums[name]:
                raise ValueError(f'SHA-256 mismatch for {name}')
        record['verified'] = {n: checksums[n] for n in names}
        core = safe_extract(tmp / 'completed-core.tgz', ROOT / 'runs' / run, strip=f'runs/{run}', force=force)
        phase = safe_extract(tmp / 'completed-phase.tgz', ROOT / 'runs' / 'phase-b' / run, force=force)
        (ROOT / 'runs' / 'phase-b' / run / 'hf-upload.json').write_text(json.dumps(upload, indent=2) + '\n')
        record['core'] = {'written': len(core[0]), 'unchanged': len(core[1]), 'kept_existing_different': core[2]}
        record['phase'] = {'written': len(phase[0]), 'unchanged': len(phase[1]),
                           'kept_existing_different': phase[2]}
        if upload.get('outcome') == 'uploaded':   # the upload's manifests (newest, also after a rerun on the VM)
            for remote, local in ((f'{remote_root}/runs/{run}/OFFLOADED.json', ROOT / 'runs' / run),
                                  (f'{phase_remote}/OFFLOADED.json', ROOT / 'runs' / 'phase-b' / run)):
                folder = tmp / 'manifest' / ('phase' if local.parent.name == 'phase-b' else 'run')
                folder.mkdir(parents=True)
                cloud.scp([remote], folder)
                if json.loads((folder / 'OFFLOADED.json').read_text()).get('dataset_commit') != \
                        upload.get('dataset_commit'):
                    raise ValueError(f'{remote} does not name the upload commit')
                shutil.copyfile(folder / 'OFFLOADED.json', local / 'OFFLOADED.json')
        log(f'{run}: core {len(core[0])} files, phase records {len(phase[0])} files; '
            f'dataset upload {upload.get("outcome")} {upload.get("dataset_commit") or ""}')
        if 'completed-site.tgz' in names:
            safe_extract(tmp / 'completed-site.tgz', tmp, force=True)
            record['publication'] = publish_site(run, tmp / 'site-bundle', upload, publish=publish, runner=runner,
                                                 log=log)
        else:
            record['publication'] = {'outcome': 'no site bundle on the VM (see site-steps.json)'}
    if marker:
        cloud.ssh('touch ' + shlex.quote(f'{phase_remote}/retrieved.marker'))
        record['retrieved_marker'] = True
    index = runner([sys.executable, str(SCRIPTS / 'build_experiment_index.py')], cwd=ROOT, capture_output=True,
                   text=True)
    record['index_exit'] = index.returncode
    record['finished_utc'] = now()
    (ROOT / 'runs' / 'phase-b' / run / 'fetch.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def publish_site(run, bundle, upload, *, publish, runner, log):
    """Preview (default) or publish the bundle on Cloudflare, pinned to the upload's dataset commit."""
    commit = upload.get('dataset_commit')
    if upload.get('outcome') != 'uploaded' or not commit:
        return {'outcome': 'not published: the run is not in the logs dataset yet',
                'hf_upload': upload.get('outcome')}
    receipt = ROOT / 'runs' / 'phase-b' / 'cloudflare' / 'receipts' / f'{run}.json'
    command = [sys.executable, str(SCRIPTS / 'publish_cloudflare.py'), 'publish', '--bundle', str(bundle),
               '--dataset-commit', commit]
    if publish:
        command += ['--commit', '--receipt', str(receipt)]
    done = runner(command, cwd=ROOT, capture_output=True, text=True)
    result = {'outcome': ('published' if publish else 'previewed') if done.returncode == 0 else 'failed',
              'exit': done.returncode, 'dataset_commit': commit}
    if done.returncode:
        result['detail'] = (done.stderr or done.stdout or '').strip()[-1500:]
    elif publish:
        result['receipt'] = str(receipt.relative_to(ROOT))
        check = runner([sys.executable, str(SCRIPTS / 'publish_cloudflare.py'), 'verify', '--run', run], cwd=ROOT,
                       capture_output=True, text=True)
        result['verify_exit'] = check.returncode
    log(f'{run}: Cloudflare {result["outcome"]}')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--vm', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--remote-root', default='gdm-math-swarm', help='checkout on the VM, relative to its home')
    parser.add_argument('--publish', action='store_true', help='deploy to the Cloudflare site (default: preview)')
    parser.add_argument('--no-marker', action='store_true', help='do not touch retrieved.marker on the VM')
    parser.add_argument('--force', action='store_true', help='replace existing local files that differ')
    args = parser.parse_args(argv)
    if not VM_NAME.fullmatch(args.vm):
        parser.error('invalid VM name')
    record = fetch(args.run, Cloud(args.vm), remote_root=args.remote_root, publish=args.publish,
                   marker=not args.no_marker, force=args.force)
    print(json.dumps(record, indent=2))
    publication = record.get('publication') or {}
    return 1 if publication.get('outcome') == 'failed' or publication.get('verify_exit') else 0


if __name__ == '__main__':
    sys.exit(main())
