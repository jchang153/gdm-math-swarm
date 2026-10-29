#!/usr/bin/env python3
"""One-time owner step: install the VM hosts' own HF upload token so finished runs go straight from each VM to
the private logs dataset (docs/plan.md, "Runs go from the VM to the logs dataset, never to the owner's computer
(October 29, 2026)"; scripts/upload_run.py, run by scripts/supervise_run.py).

Run by the OWNER on their own computer, with their own gcloud login. Agents never run it: it handles a
credential. For each named VM it:

1. installs the pinned ``publication`` extra (huggingface-hub, zipfile-zstd; versions from pyproject.toml) into
   the VM checkout's .venv;
2. writes the dataset name (GDM_LOGS_DATASET) to ``~/.config/gdm-math-swarm/logs-dataset``;
3. writes the token, sent over the SSH session's standard input (never in a command line, environment
   variable, log or the checkout), to ``~/.config/gdm-math-swarm/hf-upload-token`` with mode 600;
4. checks, with that token only, that the dataset is reachable and private, printing only yes/no.

The token comes from ``--token-file`` (a file only you can read) or, by default, a hidden prompt. Create it at
https://huggingface.co/settings/tokens as a fine-grained token with write access to the logs dataset only.
``--start`` starts TERMINATED VMs for the setup and stops them again afterwards (verifying TERMINATED); VMs
that were running are left running.

    export GCLOUD=... GDM_GCP_PROJECT=... GDM_GCP_ZONE=... GDM_LOGS_DATASET=...   # HANDOFF.local.md
    .venv/bin/python scripts/vm_hf_setup.py --vm VM [--vm VM2 ...] [--start] [--token-file PATH]
    .venv/bin/python scripts/vm_hf_setup.py --vm VM --check-only     # presence and access only
"""
import argparse
import getpass
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
VM_NAME = re.compile(r'[a-z]([-a-z0-9]*[a-z0-9])?')
DATASET = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*')
CONFIG = '~/.config/gdm-math-swarm'
SECRET_ENV = re.compile(r'TOKEN|SECRET|PASSWORD|API_KEY|_KEY$|^HF_|HUGGING', re.I)
# Runs on the VM with the token file only (HF_TOKEN_PATH); prints no credential.
CHECK = ("import os,sys\n"
         "from huggingface_hub import HfApi\n"
         "info = HfApi().repo_info(sys.argv[1], repo_type='dataset')\n"
         "print('dataset reachable: yes; private:', 'yes' if info.private else 'NO')\n")


def publication_pins(pyproject=ROOT / 'pyproject.toml'):
    match = re.search(r'^publication\s*=\s*\[([^\]]*)\]', pyproject.read_text(), re.M)
    if not match:
        raise SystemExit('pyproject.toml has no publication extra')
    return re.findall(r'"([^"]+)"', match.group(1))


def remote_script(dataset, remote_root, pins, *, check_only):
    root = shlex.quote(remote_root)
    lines = ['set -eu', 'umask 077', f'mkdir -p {CONFIG}', f'chmod 700 {CONFIG}']
    if not check_only:
        lines += [f'cd {root} && .venv/bin/python -m pip install --quiet ' + ' '.join(shlex.quote(p) for p in pins),
                  f'printf "%s\\n" {shlex.quote(dataset)} > {CONFIG}/logs-dataset',
                  f'cat > {CONFIG}/hf-upload-token.new',   # the token arrives on standard input
                  f'test -s {CONFIG}/hf-upload-token.new',
                  f'chmod 600 {CONFIG}/hf-upload-token.new',
                  f'mv {CONFIG}/hf-upload-token.new {CONFIG}/hf-upload-token']
    lines += [f'test -s {CONFIG}/hf-upload-token && echo "token file: present, mode $(stat -c %a {CONFIG}/hf-upload-token)"',
              f'cd {root} && env -i PATH="$PATH" HOME="$HOME" HF_TOKEN_PATH="$HOME/.config/gdm-math-swarm/hf-upload-token" '
              f'HF_HUB_DISABLE_TELEMETRY=1 .venv/bin/python -I -c {shlex.quote(CHECK)} {shlex.quote(dataset)}']
    return '\n'.join(lines)


def clean_env():
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env.setdefault('CLOUDSDK_PYTHON', sys.executable)
    return env


def gcloud(*args, **kwargs):
    flags = [f'--project={os.environ.get("GDM_GCP_PROJECT", "")}', f'--zone={os.environ.get("GDM_GCP_ZONE", "")}']
    return subprocess.run([os.environ.get('GCLOUD') or 'gcloud', 'compute', *args, *flags], env=clean_env(),
                          **kwargs)


def status(vm):
    return gcloud('instances', 'describe', vm, '--format=value(status)', capture_output=True,
                  text=True).stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--vm', action='append', required=True)
    parser.add_argument('--remote-root', default='gdm-math-swarm', help='checkout on the VM, relative to its home')
    parser.add_argument('--token-file', type=Path, help='file holding the token (default: a hidden prompt)')
    parser.add_argument('--start', action='store_true', help='start TERMINATED VMs and stop them again afterwards')
    parser.add_argument('--check-only', action='store_true', help='only report token presence and dataset access')
    args = parser.parse_args(argv)
    dataset = os.environ.get('GDM_LOGS_DATASET', '')
    if not DATASET.fullmatch(dataset):
        parser.error('export GDM_LOGS_DATASET first (HANDOFF.local.md)')
    for vm in args.vm:
        if not VM_NAME.fullmatch(vm):
            parser.error('invalid VM name: ' + vm)
    token = b''
    if not args.check_only:
        token = (args.token_file.read_bytes() if args.token_file
                 else getpass.getpass('HF token (hidden): ').encode()).strip()
        if not token:
            parser.error('empty token')
    script = remote_script(dataset, args.remote_root, publication_pins(), check_only=args.check_only)
    failed = []
    for vm in args.vm:
        started = False
        state = status(vm)
        if state != 'RUNNING':
            if not args.start:
                print(f'{vm}: {state or "status unavailable"}; skipped (use --start)')
                failed.append(vm)
                continue
            for _ in range(5):   # "not enough resources" is transient
                if gcloud('instances', 'start', vm, capture_output=True, text=True).returncode == 0:
                    started = True
                    break
                time.sleep(30)
            if not started:
                print(f'{vm}: could not start')
                failed.append(vm)
                continue
            time.sleep(30)
        try:
            for attempt in range(4):   # IAP is flaky right after a start
                done = gcloud('ssh', vm, '--tunnel-through-iap', '--quiet', '--command', script,
                              input=token + b'\n' if token else b'', capture_output=True)
                if done.returncode == 0:
                    break
                time.sleep(20)
            output = done.stdout.decode(errors='replace').strip()
            print(f'{vm}: ' + (output if done.returncode == 0 else
                               'FAILED: ' + done.stderr.decode(errors='replace').strip()[-400:]))
            if done.returncode:
                failed.append(vm)
        finally:
            if started:
                gcloud('instances', 'stop', vm, capture_output=True, text=True)
                print(f'{vm}: stopped, status {status(vm)}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
