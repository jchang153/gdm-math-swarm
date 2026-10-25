#!/usr/bin/env python3
"""Near-live mirror of running experiments to the HF library (owner choice "option 1", October 21, 2026): pages to
the results Space (public by owner decision, October 24, 2026), Inspect logs to the private logs dataset. Both
repositories' actual visibility must equal the record in scripts/hf_visibility.py before every commit; every Space
commit passes publish_hf_library.space_commit (credential and dataset-category gate).

Runs on the operator's own computer, never on a VM:

    python scripts/live_mirror.py --vm collab-vm-1 [--vm owner-vm=~/gdm-math-swarm] [--interval 300]
    python scripts/live_mirror.py --vm collab-vm-1 --once --no-upload      # one local pass, nothing uploaded

Every pass (default every 5 minutes), for each RUNNING VM (never started, stopped or provisioned here):

1. lists the runs under ``REMOTE_ROOT/runs`` whose ``runtime.jsonl`` changed recently and has no
   ``run_stop`` yet (plus one final pass for a mirrored run that has just stopped);
2. pulls a consistent, read-only snapshot of each run over IAP SSH: the complete lines of ``runtime.jsonl``,
   ``settings.json``, an SQLite online backup of ``conference/state.sqlite`` and of Inspect's realtime
   sample buffer (``log_realtime=True``), the ``.eval`` logs when they changed, and the private-file
   snapshot blobs the conference view shows (Claude Code's own session JSONL blobs are left out). The VM
   side is a stdlib-only Python program sent inside the SSH command; it writes a tar stream to stdout and
   leaves nothing behind. Nothing is sent to the VM except that program and its arguments: no HF token,
   no provider key, no host environment;
3. rebuilds, locally, the run's Inspect log as it stands (the samples Inspect flushed to the ``.eval`` plus
   the ones still in its realtime buffer, in Inspect's own in-progress format) and the native Inspect View
   bundle over it (``inspect view bundle``), and the conference view (``export_conference.export``, with
   ``conference/events.jsonl`` rebuilt from the snapshot's events table), plus a live summary page.
   Transcripts are read in Inspect View; the custom transcript export is not published (owner, October 21,
   2026: "there's no need for a transcript view on the website when we already have inspect view");
4. screens the bundle exactly like a normal publication (``publish_hf_library.safe_path`` and ``screen``,
   Inspect archives included), adds the private-Space navigation, and uploads it with the operator's own HF
   login (docs/experiment-library.md, "Logs and raw records in the dataset repo"): first ONE commit to the private logs dataset
   (``--dataset-repo``) that touches only its ``live/``: the live Inspect logs (and any other file of a
   dataset category, ``dataset_logs.category``) at ``live/RUN/inspect/<name>.eval``, verified by size and
   SHA-256 after the commit; then ONE Space commit that touches only ``live/`` and carries pages only (never
   an ``.eval``): changed files are added, files of the previous snapshot that no longer exist are deleted,
   ``live/index.json`` (the catalogue's "Live now" list) is updated, and the live folder of any run that is
   now in the published catalogue is removed (in both repositories). The live Inspect View reads its logs
   through the inline fetch shim (``dataset_logs.with_log_shim``), pinned to that dataset commit, with the
   viewer's own "Sign in with HF" token;
5. bounds storage growth: after both commits land, the LFS objects of this mirror's own superseded live
   Inspect logs (synthesized snapshots, never byte-equal to a published file) that no file of the dataset's
   new head references are permanently deleted from the dataset, without rewriting history
   (``--keep-live-history`` keeps them). Live logs uploaded to the Space by earlier versions of the mirror are
   still pruned from the Space the same way once superseded.

A pass that changed nothing makes no commit and unchanged files (the Inspect View app, conference pages)
are never re-uploaded. The Space history is never squashed or rewritten (published revisions are cited in
the docs); only the live log objects of earlier live revisions become unavailable.

Coordination with the publication tools: a pass is deferred while the Space's head is not one of this
mirror's own commits and changed less than ``--quiet-seconds`` ago (default 600), so a publication or
maintenance in progress is not interrupted; every live commit is pinned to the revision it was planned on.

Researcher-only. Live pages are unfinished snapshots: conference (operational) outcomes only, no independent
evaluation and no behavior judge. No model calls, no paid inference, never reads credentials.
"""
import argparse
import base64
import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
for _path in (SCRIPTS, ROOT / 'src', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import dataset_logs  # noqa: E402
from dataset_logs import DATASET_REPO  # noqa: E402
import hf_visibility  # noqa: E402
from publish_hf_library import DEFAULT_REPO, SECRET, matches, safe_path, screen, space_commit  # noqa: E402
from site_style import MARKER as THEME_MARKER, themed  # noqa: E402
from space_navigation import private_space_navigation  # noqa: E402
import run_clock  # noqa: E402

DEFAULT_GCLOUD = '/Users/owner/.local/share/gdm-vm-tools/google-cloud-sdk/bin/gcloud'
DEFAULT_PROJECT = 'GCP-PROJECT'
DEFAULT_ZONE = 'us-central1-a'
DEFAULT_REMOTE_ROOT = '~/gdm-math-swarm'
DEFAULT_BUFFER_DIR = '~/.local/share/inspect_ai/samplebuffer'   # Inspect's inspect_data_dir("samplebuffer")
WORK = ROOT / 'runs' / 'phase-b' / 'live-mirror'                # Git-ignored host-only working copies
LIVE = 'live'
LIVE_INDEX = 'live/index.json'
LIVE_SCHEMA = 'gdm-live-mirror/1'
SNAPSHOT_SCHEMA = 'gdm-live-snapshot/1'
COMMIT_PREFIX = 'Live mirror: '
LFS_SAFE_LIMIT = 9_961_472                                       # 9.5 MiB, as in maintain_hf_run.py
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')
AGENT = re.compile(r'agent-\d+')
HEX64 = re.compile(r'[0-9a-f]{64}')
SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]*')
# Environment variables never passed on to gcloud (and so never available to an SSH session).
SECRET_ENV = re.compile(r'TOKEN|SECRET|PASSWORD|API_KEY|_KEY$|^HF_|HUGGING', re.I)
STATE_LABELS = {'running': 'running', 'finished': 'finished, awaiting publication',
                'vm_stopped': 'VM stopped, awaiting publication'}

# ---------------------------------------------------------------------------
# The VM side: stdlib only, read-only on the run, output is a tar stream on stdout (or JSON).

REMOTE_PROGRAM = r'''
import base64, glob, gzip, hashlib, json, os, re, shutil, sqlite3, sys, tarfile, tempfile, time, urllib.parse
A = json.loads(base64.b64decode(sys.argv[1]).decode())
RUN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*\Z')
AGENT = re.compile(r'agent-\d+\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')
SESSION = re.compile(r'^/home/[^/]+/\.claude/projects/.*\.jsonl$')
runs = os.path.join(os.path.expanduser(A['root']), 'runs')

def tail_records(path, size=262144):
    with open(path, 'rb') as f:
        f.seek(0, 2); end = f.tell(); f.seek(max(0, end - size)); data = f.read()
    out = []
    for line in data.split(b'\n')[1 if end > size else 0:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out

def backup(path, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    src = sqlite3.connect('file:' + urllib.parse.quote(os.path.abspath(path)) + '?mode=ro', uri=True, timeout=60)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()

if A['mode'] == 'discover':
    found = []
    for name in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        d = os.path.join(runs, name); p = os.path.join(d, 'runtime.jsonl')
        if not RUN.match(name) or os.path.islink(d) or not os.path.isfile(p) or os.path.islink(p):
            continue
        age = time.time() - os.stat(p).st_mtime
        if age > A['max_age']:
            continue
        kinds = {r.get('kind') for r in tail_records(p) if isinstance(r, dict)}
        found.append({'run': name, 'stopped': 'run_stop' in kinds, 'age_seconds': round(age)})
    sys.stdout.write(json.dumps(found)); sys.exit(0)

run = A['run']
if not RUN.match(run):
    sys.exit('invalid run id')
d = os.path.join(runs, run)
if os.path.islink(d) or not os.path.isdir(d):
    sys.exit('no run directory')
tmp = tempfile.mkdtemp(prefix='gdm-live-')
try:
    files = {}
    with open(os.path.join(d, 'runtime.jsonl'), 'rb') as f:
        data = f.read()
    data = data[:data.rfind(b'\n') + 1]          # complete lines only
    with open(os.path.join(tmp, 'runtime.jsonl'), 'wb') as f:
        f.write(data)
    files['runtime.jsonl'] = os.path.join(tmp, 'runtime.jsonl')
    lines = data.split(b'\n')[:-1]
    since = A.get('since_line') or 0
    if since > len(lines):
        since = 0
    for line in lines[since:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        payload = rec.get('payload') or {}
        agent, blob, path = rec.get('agent_id'), payload.get('blob_sha256'), payload.get('path')
        if (rec.get('kind') != 'file_snapshot' or not isinstance(path, str) or SESSION.match(path)
                or not isinstance(agent, str) or not AGENT.match(agent) or not isinstance(blob, str) or not HEX64.match(blob)):
            continue
        source = os.path.join(d, 'snapshots', agent, blob)
        if os.path.isfile(source) and not os.path.islink(source):
            files['snapshots/%s/%s' % (agent, blob)] = source
    settings = os.path.join(d, 'settings.json')
    if os.path.isfile(settings) and not os.path.islink(settings):
        files['settings.json'] = settings
    state = os.path.join(d, 'conference', 'state.sqlite')
    if os.path.isfile(state) and not os.path.islink(state):
        backup(state, os.path.join(tmp, 'conference', 'state.sqlite'))
        files['conference/state.sqlite'] = os.path.join(tmp, 'conference', 'state.sqlite')
    known, evals = A.get('evals') or {}, {}
    buffers = os.path.expanduser(A['buffer_dir'])
    for log in sorted(glob.glob(os.path.join(glob.escape(d), 'inspect', '*.eval'))):
        name = os.path.basename(log)
        if os.path.islink(log):
            continue
        st = os.stat(log)
        evals[name] = [st.st_size, st.st_mtime_ns]
        if known.get(name) != evals[name]:
            shutil.copyfile(log, os.path.join(tmp, name))
            files['inspect/' + name] = os.path.join(tmp, name)
        for db in sorted(glob.glob(os.path.join(glob.escape(buffers), '*', glob.escape(name) + '.*.db'))):
            folder = os.path.basename(os.path.dirname(db))
            out = os.path.join(tmp, 'samplebuffer', folder, os.path.basename(db))
            try:
                backup(db, out)
            except sqlite3.Error:
                continue                         # finished and removed by Inspect meanwhile
            files['samplebuffer/%s/%s' % (folder, os.path.basename(db))] = out
    digests = {}
    for arc, path in files.items():
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        digests[arc] = h.hexdigest()
    manifest = json.dumps({'schema': 'gdm-live-snapshot/1', 'run': run, 'utc': time.time(),
                           'runtime_lines': len(lines), 'since_line': since, 'evals': evals,
                           'files': digests}).encode()
    with open(os.path.join(tmp, 'MANIFEST.json'), 'wb') as f:
        f.write(manifest)
    with gzip.GzipFile(fileobj=sys.stdout.buffer, mode='wb', compresslevel=6, mtime=0) as packed:
        with tarfile.open(fileobj=packed, mode='w|') as tar:
            tar.add(os.path.join(tmp, 'MANIFEST.json'), arcname='MANIFEST.json')
            for arc, path in sorted(files.items()):
                tar.add(path, arcname=arc, recursive=False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
'''


def remote_command(args):
    """The SSH command: the VM program (compressed) and its JSON arguments, nothing else."""
    program = base64.b64encode(zlib.compress(REMOTE_PROGRAM.encode(), 9)).decode()
    bootstrap = f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{program}')))"
    encoded = base64.b64encode(json.dumps(args).encode()).decode()
    return 'nice -n 10 python3 -c ' + shlex.quote(bootstrap) + ' ' + shlex.quote(encoded)


def clean_env(environ=None):
    """The environment for gcloud: without any token, key or HF variable."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if not SECRET_ENV.search(k)}
    env.setdefault('CLOUDSDK_PYTHON', sys.executable)   # gcp-vm.md: the CLI needs the project's Python
    return env


class Cloud:
    """gcloud over IAP. ``runner`` is ``subprocess.run`` (tests pass a fake)."""

    def __init__(self, gcloud=DEFAULT_GCLOUD, project=DEFAULT_PROJECT, zone=DEFAULT_ZONE, runner=subprocess.run):
        self.gcloud, self.project, self.zone, self.runner = gcloud, project, zone, runner

    def _flags(self):
        return [f'--project={self.project}', f'--zone={self.zone}']

    def status(self, vm):
        done = self.runner([self.gcloud, 'compute', 'instances', 'describe', vm, *self._flags(),
                            '--format=value(status)'], capture_output=True, text=True, timeout=180, env=clean_env())
        if done.returncode:
            raise RuntimeError(f'gcloud describe {vm} failed: {done.stderr.strip()[-300:]}')
        return done.stdout.strip()

    def ssh(self, vm, args, stdout, timeout=1800):
        """Run the VM program with ``args``; its stdout goes to the open binary file ``stdout``."""
        done = self.runner([self.gcloud, 'compute', 'ssh', vm, *self._flags(), '--tunnel-through-iap', '--quiet',
                            '--command', remote_command(args)], stdout=stdout, stderr=subprocess.PIPE,
                           timeout=timeout, env=clean_env())
        if done.returncode:
            error = done.stderr.decode(errors='replace') if isinstance(done.stderr, bytes) else str(done.stderr)
            raise RuntimeError(f'remote {args["mode"]} on {vm} failed: {error.strip()[-400:]}')

    def discover(self, vm, root, max_age):
        # A real file: subprocess.run needs a file descriptor for stdout (an io.BytesIO has none).
        with tempfile.TemporaryFile() as out:
            self.ssh(vm, {'mode': 'discover', 'root': root, 'max_age': max_age}, out, timeout=300)
            out.seek(0)
            found = json.loads(out.read().decode() or '[]')
        return [r for r in found if isinstance(r, dict) and RUN_ID.fullmatch(str(r.get('run', '')))]

    def snapshot(self, vm, root, run, *, since_line, evals, buffer_dir, target):
        with open(target, 'wb') as handle:
            self.ssh(vm, {'mode': 'snapshot', 'root': root, 'run': run, 'since_line': since_line,
                          'evals': evals, 'buffer_dir': buffer_dir}, handle)


# ---------------------------------------------------------------------------
# Applying a snapshot locally


def _allowed_member(name):
    parts = name.split('/')
    if name in ('MANIFEST.json', 'runtime.jsonl', 'settings.json', 'conference/state.sqlite'):
        return True
    if len(parts) == 3 and parts[0] == 'snapshots':
        return bool(AGENT.fullmatch(parts[1]) and HEX64.fullmatch(parts[2]))
    if len(parts) == 2 and parts[0] == 'inspect':
        return bool(SAFE_NAME.fullmatch(parts[1]) and parts[1].endswith('.eval'))
    if len(parts) == 3 and parts[0] == 'samplebuffer':
        return bool(HEX64.fullmatch(parts[1]) and SAFE_NAME.fullmatch(parts[2]) and parts[2].endswith('.db'))
    return False


def apply_snapshot(archive, source, run):
    """Verify the tar stream against its manifest and update the local source copy ``source``
    (``WORK/work/RUN``). Snapshot blobs accumulate; everything else is replaced. Returns the manifest."""
    staging = Path(tempfile.mkdtemp(prefix='live-snapshot-', dir=source.parent))
    try:
        with tarfile.open(archive, mode='r:gz') as tar:
            for member in tar:
                if not member.isfile() or not _allowed_member(member.name):
                    raise ValueError(f'Unexpected snapshot entry: {member.name!r}')
                target = staging / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as data, open(target, 'wb') as out:
                    shutil.copyfileobj(data, out)
        manifest = json.loads((staging / 'MANIFEST.json').read_text())
        if manifest.get('schema') != SNAPSHOT_SCHEMA or manifest.get('run') != run:
            raise ValueError('Snapshot manifest does not describe this run')
        present = {p.relative_to(staging).as_posix() for p in staging.rglob('*') if p.is_file()} - {'MANIFEST.json'}
        if present != set(manifest['files']):
            raise ValueError('Snapshot contents differ from its manifest')
        for name, digest in manifest['files'].items():
            if hashlib.sha256((staging / name).read_bytes()).hexdigest() != digest:
                raise ValueError('Snapshot checksum mismatch: ' + name)
        source.mkdir(parents=True, exist_ok=True)
        for name in ('runtime.jsonl', 'settings.json'):
            if (staging / name).exists():
                os.replace(staging / name, source / name)
        conference = source / 'conference'
        conference.mkdir(exist_ok=True)
        for leftover in conference.glob('state.sqlite*'):
            leftover.unlink()
        if (staging / 'conference' / 'state.sqlite').exists():
            os.replace(staging / 'conference' / 'state.sqlite', conference / 'state.sqlite')
        for blob in (staging / 'snapshots').glob('*/*'):
            (source / 'snapshots' / blob.parent.name).mkdir(parents=True, exist_ok=True)
            os.replace(blob, source / 'snapshots' / blob.parent.name / blob.name)
        inspect = source / 'inspect'
        inspect.mkdir(exist_ok=True)
        for log in inspect.glob('*.eval'):
            if log.name not in manifest['evals']:
                log.unlink()
        for log in (staging / 'inspect').glob('*.eval'):
            os.replace(log, inspect / log.name)
        shutil.rmtree(source / 'samplebuffer', ignore_errors=True)
        if (staging / 'samplebuffer').exists():
            os.replace(staging / 'samplebuffer', source / 'samplebuffer')
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def runtime_records(source):
    records = []
    path = Path(source) / 'runtime.jsonl'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def missing_blobs(source, records=None):
    """Snapshot blobs the views need (non-session file captures) that the local copy lacks."""
    from research.analysis.judge.history import SESSION_TRANSCRIPT
    missing = []
    for record in runtime_records(source) if records is None else records:
        payload = record.get('payload') or {}
        path, agent, blob = payload.get('path'), record.get('agent_id'), payload.get('blob_sha256')
        if (record.get('kind') == 'file_snapshot' and isinstance(path, str) and not SESSION_TRANSCRIPT.match(path)
                and isinstance(agent, str) and AGENT.fullmatch(agent) and isinstance(blob, str) and HEX64.fullmatch(blob)
                and not (Path(source) / 'snapshots' / agent / blob).is_file()):
            missing.append(f'{agent}/{blob}')
    return missing


# ---------------------------------------------------------------------------
# Rendering the live bundle

INSPECT_VERSION = '0.3.279'   # the live log writer uses Inspect internals of exactly this version
RUNNING_NOTE = ('Still running at this live snapshot: not an error. This is the agent\'s transcript so far; '
                'the published run replaces it when the run finishes.')
# Runs in a separate interpreter (``python -I``, started in a temporary directory outside the repository so
# no .env is loaded, with the same token-free environment as gcloud). For each .eval of the snapshot it
# writes a live copy in Inspect's own in-progress format (``_journal/start.json`` and samples, no
# ``header.json``, which Inspect View shows as a running eval): the samples Inspect already flushed to
# the .eval, then the samples still in its realtime buffer rebuilt with Inspect's crash-recovery reader
# (``reconstruct_eval_sample``), a running sample carrying RUNNING_NOTE in place of the recovery's
# "cancelled" error. A finished log (with ``header.json``) is copied unchanged. Then
# ``inspect view bundle`` (``bundle_log_dir``) builds the static Inspect View over those copies.
INSPECT_PROGRAM = r'''
import asyncio, json, os, shutil, sqlite3, sys
from pathlib import Path
A = json.loads(open(sys.argv[1]).read())
import inspect_ai
if inspect_ai.__version__ != A['version']:
    sys.exit('inspect_ai %s (the live log writer is pinned to %s)' % (inspect_ai.__version__, A['version']))
from inspect_ai._util.asyncfiles import AsyncFilesystem
from inspect_ai._util.file import filesystem
from inspect_ai.log import EvalError
from inspect_ai.log._bundle import bundle_log_dir
from inspect_ai.log._recorders.buffer.database import SampleBufferDatabase, location_dir_and_file
from inspect_ai.log._recorders.eval import EvalRecorder, _sample_filename
from inspect_ai.log._recover._read import AsyncZipReader, read_crashed_eval_log, read_flushed_sample
from inspect_ai.log._recover._reconstruct import reconstruct_eval_sample

async def live_copy(log, buffers, scratch, out):
    try:
        crashed = await read_crashed_eval_log(log)
    except ValueError as error:
        if 'has header.json' in str(error):      # finished: the final log as Inspect wrote it
            shutil.copyfile(log, out)
            return {'mode': 'final'}
        return {'mode': 'skipped', 'reason': 'no journal start yet'}
    recorder = EvalRecorder(os.path.dirname(out))
    await recorder.log_init(crashed.eval, location=out, clean=True)
    await recorder.log_start(crashed.eval, crashed.plan)
    written, counts = set(), {'mode': 'live', 'flushed': 0, 'completed': 0, 'running': 0}
    if crashed.sample_entries:
        async with AsyncFilesystem() as fs:
            reader = AsyncZipReader(fs, log)
            for name in crashed.sample_entries:
                await recorder.log_sample(crashed.eval, await read_flushed_sample(reader, name))
                written.add(name)
                counts['flushed'] += 1
    hashed, file = location_dir_and_file(filesystem(log).path_as_uri(log))
    for i, db in enumerate(sorted(buffers)):
        folder = Path(scratch) / ('buffer-%d' % i) / hashed
        folder.mkdir(parents=True)
        target = folder / ('%s.%d.db' % (file, 999999900 + i))   # a process number no host uses
        src = sqlite3.connect(Path(db).as_uri() + '?mode=ro', uri=True)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
        buffer = SampleBufferDatabase(log, create=False, db_dir=folder.parent)
        buffer.db_path = target
        try:
            listed = buffer.get_samples()
            for summary in getattr(listed, 'samples', None) or []:
                key = _sample_filename(summary.id, summary.epoch)
                data = None if key in written else buffer.get_sample_data(summary.id, summary.epoch)
                if data is None:
                    continue
                running = summary.completed_at is None
                sample = reconstruct_eval_sample(summary, data, sample_metadata=buffer.get_sample_metadata(
                    summary.id, summary.epoch), cancelled=running, incomplete_action='retry', include_events=True)
                if running:
                    note = A['note']
                    sample = sample.model_copy(update={'error': EvalError(message=note, traceback=note + '\n',
                                                                          traceback_ansi=note + '\n')})
                await recorder.log_sample(crashed.eval, sample)
                written.add(key)
                counts['running' if running else 'completed'] += 1
        finally:
            buffer.close()
    await recorder.flush(crashed.eval)
    await recorder.log_discard(crashed.eval, keep_destination=True)   # close it, keeping the file
    return counts

async def main():
    out = Path(A['live_logs'])
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for item in A['logs']:
        name = os.path.basename(item['log'])
        scratch = Path(A['scratch']) / name
        scratch.mkdir(parents=True)
        results[name] = await live_copy(item['log'], item['buffers'], scratch, str(out / name))
    return results

results = asyncio.run(main())
if any(r['mode'] != 'skipped' for r in results.values()):
    bundle_log_dir(log_dir=A['live_logs'], output_dir=A['bundle'], overwrite=True)
open(A['result'], 'w').write(json.dumps(results))
'''


def build_inspect_view(source, out, *, python=sys.executable, runner=subprocess.run):
    """Write the live Inspect View of the snapshot ``source`` into ``out`` (replaced); returns
    ``{log name: {'mode': 'live' | 'final' | 'skipped', ...counts}}``. ``out`` is left absent when
    no log could be copied yet."""
    live_logs = source / 'live-inspect'          # a stable path: the bundle's index.html names it
    shutil.rmtree(live_logs, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    logs = []
    for log in sorted((source / 'inspect').glob('*.eval')):
        buffers = sorted(str(db) for db in (source / 'samplebuffer').glob(f'*/{glob_escape(log.name)}.*.db'))
        logs.append({'log': str(log), 'buffers': buffers})
    if not logs:
        return {}
    with tempfile.TemporaryDirectory(prefix='gdm-live-inspect-') as tmp:   # outside the repository
        tmp = Path(tmp)
        args = {'version': INSPECT_VERSION, 'logs': logs, 'live_logs': str(live_logs), 'scratch': str(tmp / 'scratch'),
                'bundle': str(out), 'result': str(tmp / 'result.json'), 'note': RUNNING_NOTE}
        (tmp / 'args.json').write_text(json.dumps(args))
        env = clean_env()
        env.pop('PYTHONPATH', None)
        env['INSPECT_DISPLAY'] = 'none'
        done = runner([python, '-I', '-c', INSPECT_PROGRAM, str(tmp / 'args.json')], cwd=tmp, env=env,
                      capture_output=True, text=True, timeout=1800)
        if done.returncode:
            raise RuntimeError('Inspect View build failed: ' + (done.stderr or '').strip()[-400:])
        return json.loads((tmp / 'result.json').read_text())


def glob_escape(name):
    return re.sub(r'([*?\[])', r'[\1]', name)


LIVE_BANNER = ('<p class="banner live-banner"><strong>Live snapshot, not final.</strong> Mirrored from the running '
               'experiment; it may lag the run by several minutes and later turns are still missing. Conference '
               '(operational) outcomes only: no independent evaluation or behavior judge has run. The published run '
               'replaces this mirror when it finishes.</p>')
STYLE = ('<style>'  # colours are the shared site_style tokens (light/dark, theme toggle)
         'body{max-width:1100px;margin:32px auto;padding:0 16px;font:16px/1.5 system-ui,sans-serif;color:var(--fg);'
         'background:var(--bg)}a{color:var(--accent)}small,.muted{color:var(--muted)}.banner{background:var(--host);'
         'border-left:4px solid var(--amber);padding:10px 16px;border-radius:4px}.card{background:var(--card);'
         'border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:16px 0}'
         'dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}dt{color:var(--muted)}dd{margin:0}'
         '@media (max-width:560px){dl{grid-template-columns:1fr}dd{margin-bottom:8px}}</style>')


def screen_bytes(name, data):
    if SECRET.search(data):
        raise ValueError(f'Credential-like content detected in {name}; upload refused')


def add_banner(document, banner=LIVE_BANNER):
    """Insert the banner after the page's own style block (past the shared theme's, if present)."""
    theme = document.find(THEME_MARKER)
    at = document.find('</style>', document.find('</style>', theme) + 1 if theme >= 0 else 0)
    at = at + len('</style>') if at >= 0 else (re.match(r'(?:<!doctype[^>]*>)?', document, re.I).end())
    return document[:at] + banner + document[at:]


HREF = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)


def fix_links(document, page, available):
    """Links of a page at ``page`` (relative to the run's live folder) to files the live bundle lacks
    (the report, raw records) become plain text; a link to the library catalogue keeps working."""
    base = posixpath.dirname(page)

    def replace(match):
        href = html.unescape(match[1])
        if not href or href.startswith(('#', '/')) or re.match(r'[A-Za-z][A-Za-z0-9+.-]*:', href):
            return match[0]
        path, _, anchor = href.partition('#')
        target = posixpath.normpath(posixpath.join(base, path)) if path else page
        if target in available:
            return match[0]
        if target == '../index.html':                     # the library catalogue (live/ is one level deeper)
            return f'<a href="{html.escape("../" + href, quote=True)}">{match[2]}</a>'
        return f'<span class="unavailable" title="Available once the run is published">{match[2]}</span>'
    return HREF.sub(replace, document)


def rebuild_event_log(source):
    """``conference/events.jsonl`` from the snapshot's events table (the conference's own rebuild)."""
    from research_swarm import conference
    path = source / 'conference' / 'state.sqlite'
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        records = conference._stored_records(db, conference._stored_track(db))
    finally:
        db.close()
    (source / 'conference' / 'events.jsonl').write_bytes(conference._encode_records(records))


def write_timeline(source):
    from research.analysis import timeline
    entries, _, _ = timeline.build(source, include_inspect=False)
    (source / 'analysis').mkdir(exist_ok=True)
    timeline._write_jsonl(source / 'analysis' / 'timeline.jsonl', entries)


def summarize(source, logs, *, vm, snapshot_utc, state):
    """Live facts for the catalogue and the live summary page."""
    records = runtime_records(source)
    try:
        settings = json.loads((source / 'settings.json').read_text())
    except (OSError, ValueError):
        settings = {}
    config = settings.get('config') or {}
    start = next((r.get('timestamp') for r in records if r.get('kind') == 'common_start'), None)
    stop = next((r.get('timestamp') for r in records if r.get('kind') == 'run_stop'), None)
    end = stop if stop is not None else snapshot_utc
    samples = {k: sum(r.get(k, 0) for r in logs.values()) for k in ('flushed', 'completed', 'running')}
    summary = {'run': source.name, 'vm': vm, 'state': state, 'state_label': STATE_LABELS[state],
               'snapshot_utc': _iso(snapshot_utc), 'updated_utc': _iso(snapshot_utc),
               'started_utc': _iso(start) if start else None,
               'elapsed_seconds': round(end - start) if start else None,
               'max_run_seconds': config.get('max_run_seconds'), 'model': config.get('candidate_model_id'),
               'track': config.get('track'), 'system_prompt_mode': config.get('system_prompt_mode'),
               'agents': config.get('agent_count'),
               'agent_exits': sum(1 for r in records if r.get('kind') == 'agent_exit'),
               'agent_restarts': sum(1 for r in records if r.get('kind') == 'agent_restart'),
               'inspect_logs': {name: r.get('mode') for name, r in sorted(logs.items())},
               'inspect_samples': samples, 'solved': None, 'task_count': None, 'submissions': {}}
    path = source / 'conference' / 'state.sqlite'
    if path.exists():
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            summary['task_count'] = db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            summary['solved'] = db.execute("SELECT COUNT(*) FROM tasks WHERE status='closed'").fetchone()[0]
            summary['submissions'] = dict(db.execute('SELECT outcome, COUNT(*) FROM submissions GROUP BY outcome'))
        except sqlite3.Error:
            pass
        finally:
            db.close()
    return summary


def _iso(epoch):
    return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def _minutes(seconds):
    return '?' if seconds is None else f'{int(seconds) // 60} min'


def landing_page(summary, problems, interval, *, inspect_view, conference):
    esc = html.escape
    subs = ', '.join(f'{k} {v}' for k, v in sorted(summary['submissions'].items())) or 'none yet'
    samples = summary['inspect_samples']
    start = summary.get('started_utc')  # run start (common_start); times show offset plus UTC (run_clock)
    facts = [('State', summary['state_label']),
             ('Run started', run_clock.clock(start, start) if start else 'not yet recorded'),
             ('Snapshot taken', run_clock.clock(summary['snapshot_utc'], start)),
             ('Elapsed', f"{_minutes(summary['elapsed_seconds'])} of {_minutes(summary['max_run_seconds'])}"),
             ('Model', summary['model'] or 'unknown'), ('Condition', summary['track'] or 'unknown'),
             ('System prompt', summary['system_prompt_mode'] or 'default'),
             ('Agents', f"{summary['agents'] or '?'} ({summary['agent_exits']} exited, {summary['agent_restarts']} restarts)"),
             ('Agent transcripts in Inspect View', f"{samples['running']} still running, "
                                                   f"{samples['flushed'] + samples['completed']} finished"),
             ('Tasks solved (conference judge)', f"{summary['solved'] if summary['solved'] is not None else '?'} / "
                                                 f"{summary['task_count'] or '?'}"),
             ('Submissions by conference outcome', subs), ('VM', summary['vm'])]
    card = '<dl>' + ''.join(f'<dt>{esc(k)}</dt><dd>{esc(str(v))}</dd>' for k, v in facts) + '</dl>'
    links = [('inspect-view/index.html', 'Inspect View (agent transcripts)') if inspect_view else None,
             ('conference-view/index.html', 'Conference') if conference else None]
    nav = ' · '.join(f'<a href="{href}">{label}</a>' for href, label in filter(None, links)) or 'No views in this snapshot yet.'
    notes = ''.join(f'<li>{esc(p)}</li>' for p in problems)
    return themed('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
                  'content="width=device-width,initial-scale=1"><title>Live · ' + esc(summary['run']) + '</title>' + STYLE +
            '<p><a href="../../index.html">Experiment library</a></p><h1>' + esc(summary['run']) + ' <small>live</small></h1>'
            + LIVE_BANNER + f'<div class="card">{card}</div><p>{nav}</p>'
            f'<p class="muted">Refreshed about every {max(1, round(interval / 60))} minutes while the run is going. '
            'Read the agents\' transcripts in Inspect View: one sample per agent, up to this snapshot. A sample that '
            'is still running shows the note "Still running at this live snapshot: not an error" where Inspect '
            'would show an error, and the eval itself shows as running. "Solved" and submission outcomes are the '
            'conference\'s operational judge: acceptance there is not independent mathematical validity. Exits and '
            'restarts are counted from the runtime log.</p>'
            + (f'<h2>Mirror notes</h2><ul>{notes}</ul>' if notes else '') + '</html>')


LIVE_SIGNIN = '../../../signin.html'   # the site's sign-in page, from live/RUN/inspect-view/index.html


def build_live_bundle(source, bundle, *, vm, snapshot_utc, state, interval, inspect_view=build_inspect_view, run=None):
    """Render the live views of the local source copy into ``bundle`` (replaced). Returns the summary and
    the bundle paths of live (synthesized) Inspect logs, whose superseded copies may later be pruned.
    ``run`` (default: the bundle folder's name) names the dataset folder the Inspect View's shim reads.
    The shim carries ``dataset_logs.COMMIT_PLACEHOLDER``; ``sync`` pins it to the dataset commit."""
    run = run or bundle.name
    if not RUN_ID.fullmatch(run):
        raise ValueError('Invalid run ID')
    shutil.rmtree(bundle, ignore_errors=True)
    bundle.mkdir(parents=True)
    problems = []
    for step, action in (('conference event log', rebuild_event_log), ('timeline', write_timeline)):
        try:
            action(source)
        except Exception as error:   # the view still renders, with this gap stated
            problems.append(f'{step} unavailable in this snapshot ({type(error).__name__})')
            if step == 'timeline':
                (source / 'analysis').mkdir(exist_ok=True)
                (source / 'analysis' / 'timeline.jsonl').write_text('')
    viewer = source / 'inspect-view-build'
    try:
        logs = inspect_view(source, viewer)
    except Exception as error:
        problems.append(f'Inspect View unavailable in this snapshot: {error}')
        logs = {}
    for name, result in logs.items():
        if result.get('mode') == 'skipped':
            problems.append(f'{name}: not shown yet ({result.get("reason")})')
    has_viewer = (viewer / 'index.html').is_file() and (viewer / 'logs' / 'listing.json').is_file()
    conference = False
    try:
        import export_conference
        export_conference.export(source)
        conference = True
    except Exception as error:
        problems.append(f'conference view unavailable in this snapshot ({type(error).__name__}: {error})')
    # The screening of a normal publication, applied to the records behind the conference view (which
    # holds them compressed) and to every page before it is written.
    for name in ('runtime.jsonl', 'conference/state.sqlite', 'conference/events.jsonl', 'settings.json'):
        if (source / name).is_file():
            screen_bytes(name, (source / name).read_bytes())
    summary = summarize(source, logs, vm=vm, snapshot_utc=snapshot_utc, state=state)
    summary['problems'] = problems
    files = {}                                             # bundle path -> source file
    if has_viewer:
        for path in sorted(viewer.rglob('*')):
            if path.is_symlink():
                raise ValueError('Linked file in the Inspect View bundle')
            if path.is_file():
                files['inspect-view/' + path.relative_to(viewer).as_posix()] = path
    if conference:
        for path in sorted((source / 'conference-view').iterdir()):
            if path.is_file() and not path.is_symlink():
                files['conference-view/' + path.name] = path
    pages = {'index.html': landing_page(summary, problems, interval, inspect_view=has_viewer, conference=conference)}
    available = set(pages) | set(files)
    notices = []
    for name, path in files.items():
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == 'inspect-view/index.html':             # as prepare_hf_library.py does for Inspect View
            modules = ['./assets/' + p.name for p in sorted((viewer / 'assets').glob('*.js'))]
            document = private_space_navigation(path.read_text(encoding='utf-8'), modules)
            # The shim goes first in <head>: Inspect View reads its logs from the dataset's live/ folder.
            document = dataset_logs.with_log_shim(document, path=f'{LIVE}/{run}/inspect', signin=LIVE_SIGNIN)
            target.write_text(document, encoding='utf-8')
        elif name.startswith('conference-view/') and name.endswith('.html'):
            document = path.read_text(encoding='utf-8')
            screen_bytes(name, document.encode('utf-8'))
            document = fix_links(document, name, available)
            if name == 'conference-view/index.html':
                document = add_banner(document)
            pages[name] = document
        else:
            shutil.copyfile(path, target)
    for name, document in pages.items():
        data = private_space_navigation(document).encode('utf-8')
        if len(data) >= LFS_SAFE_LIMIT:                    # an HTML page must not become an LFS download
            notices.append(name)
            data = private_space_navigation(
                '<!doctype html><meta charset="utf-8"><title>Too large for the live mirror</title><p>This live page is '
                'too large to mirror; it will be available once the run is published.</p>').encode('utf-8')
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if notices:
        summary['problems'].append('too large to mirror: ' + ', '.join(notices))
    (bundle / 'status.json').write_text(json.dumps(summary, indent=2))
    for path in sorted(bundle.rglob('*')):   # the screening of a normal publication (Inspect archives included)
        if path.is_file():
            safe_path(path.relative_to(bundle).as_posix())
            screen(path)
    live_logs = sorted(f'inspect-view/logs/{name}' for name, r in logs.items()
                       if r.get('mode') == 'live' and (bundle / 'inspect-view' / 'logs' / name).is_file())
    return summary, live_logs


# ---------------------------------------------------------------------------
# Uploading one pass


def _entry(summary):
    keys = ('run', 'vm', 'state', 'state_label', 'updated_utc', 'started_utc', 'elapsed_seconds', 'max_run_seconds',
            'model', 'track', 'system_prompt_mode', 'agents', 'solved', 'task_count', 'submissions')
    return {k: summary.get(k) for k in keys}


def _remote_live(api, repo, revision):
    try:
        return {item.path: item for item in api.list_repo_tree(repo, repo_type='space', revision=revision,
                                                               recursive=True, path_in_repo=LIVE)
                if hasattr(item, 'blob_id')}
    except Exception as error:   # no live/ folder yet
        if type(error).__name__ in ('EntryNotFoundError', 'RemoteEntryNotFoundError') or '404' in str(error):
            return {}
        raise


def _read_remote_json(download, repo, revision, name, item, empty):
    if item is None:
        return empty
    data = Path(download(repo, name, repo_type='space', revision=revision)).read_bytes()
    if not matches(data, item):
        raise ValueError('Remote metadata mismatch: ' + name)
    return json.loads(data)


def sync_dataset(api, *, files, refresh, retire, add, delete, repo=DATASET_REPO, commit=True, message=''):
    """One commit to the private logs dataset that touches only its live/ folder. ``files``: {dataset path:
    bytes} wanted at the head; ``refresh``: runs whose live/RUN/ files other than ``files`` are removed (the
    snapshot no longer has them); ``retire``: runs whose live/RUN/ files are all removed (published). Pinned
    to the planned head, then verified: every wanted file by size and SHA-256 (or Git blob id), removed files
    absent, nothing else changed. Returns {revision (the head to pin pages to), committed, uploads, ...}."""
    info = hf_visibility.check_dataset(api, repo)
    present = dataset_logs.dataset_inventory(api, repo, info.sha)
    records = {n: {**dataset_logs.digest(d), 'git_sha1': dataset_logs._git_sha1(d)} for n, d in files.items()}
    adds = {n: d for n, d in files.items() if n not in present or not dataset_logs.remote_matches(records[n], present[n])}
    deletes = set()
    for run in refresh:
        deletes |= {n for n in present if n.startswith(f'{LIVE}/{run}/') and n not in files}
    for run in retire:
        deletes |= {n for n in present if n.startswith(f'{LIVE}/{run}/')}
    deletes -= set(adds)
    for name in [*adds, *deletes]:
        if not name.startswith(LIVE + '/'):
            raise ValueError('The live mirror may only change live/ in the logs dataset: ' + name)
        safe_path(name)
    result = {'repo': repo, 'base_revision': info.sha, 'revision': info.sha, 'committed': False,
              'uploads': sorted(adds), 'deletions': sorted(deletes), 'bytes': sum(len(d) for d in adds.values())}
    if not commit or not (adds or deletes):
        return result
    response = api.create_commit(repo_id=repo, repo_type='dataset', parent_commit=info.sha,
                                 commit_message=message or COMMIT_PREFIX + 'live logs',
                                 operations=[*(add(path_in_repo=n, path_or_fileobj=d) for n, d in sorted(adds.items())),
                                             *(delete(path_in_repo=n) for n in sorted(deletes))])
    final = dataset_logs.dataset_inventory(api, repo, response.oid)
    bad = [n for n in files if n not in final or not dataset_logs.remote_matches(records[n], final[n])]
    bad += [n for n in deletes if n in final]
    bad += [n for n in set(present) | set(final) if n not in files and n not in deletes
            and present.get(n) != final.get(n)]
    if bad:
        raise ValueError('Logs dataset differs from the plan after the live commit: ' + ', '.join(sorted(bad)[:20]))
    result.update(committed=True, revision=response.oid)
    return result


def sync(api, download, *, updates, add, delete, repo=DEFAULT_REPO, dataset_repo=DATASET_REPO, now=None,
         quiet_seconds=600, commit=True, own_revisions=()):
    """One live pass upload. ``updates``: {run: {'bundle': Path | None, 'entry': dict, 'live_logs': [bundle
    paths]}} (bundle None: only the catalogue entry changes, e.g. the VM stopped). Bundle files of a dataset
    category (``dataset_logs.category``: the live Inspect logs) go to the logs dataset's live/ folder first
    (``sync_dataset``); then one Space commit touches only live/ with the remaining pages, the Inspect View's
    shim pinned to the dataset commit. Deferred while the Space's head is not one of this mirror's own
    commits (``own_revisions``) and changed less than ``quiet_seconds`` ago: another publisher is probably
    still committing. The result's ``live_log_uploads`` lists the synthesized live Inspect logs this pass
    uploaded to the dataset (candidates for pruning once superseded)."""
    now = time.time() if now is None else now
    info = hf_visibility.check(api.repo_info(repo, repo_type='space'), repo, 'space')
    hf_visibility.check_dataset(api, dataset_repo)  # the live pages link to the private logs dataset
    modified = getattr(info, 'last_modified', None)
    if info.sha not in set(own_revisions) and modified is not None:
        age = now - modified.timestamp()
        if age < quiet_seconds:
            return {'committed': False, 'deferred': f'the Space changed {round(age)} s ago by another commit '
                                                    f'(quiet period {quiet_seconds} s)', 'base_revision': info.sha}
    root = {item.path: item for item in api.list_repo_tree(repo, repo_type='space', revision=info.sha)
            if hasattr(item, 'blob_id')}
    remote = _remote_live(api, repo, info.sha)
    catalogue = _read_remote_json(download, repo, info.sha, 'experiments.json', root.get('experiments.json'),
                                  {'experiments': []})
    published = {e.get('run') for e in catalogue.get('experiments', [])}
    index = _read_remote_json(download, repo, info.sha, LIVE_INDEX, remote.get(LIVE_INDEX),
                              {'schema': LIVE_SCHEMA, 'runs': []})
    if index.get('schema') != LIVE_SCHEMA or not isinstance(index.get('runs'), list):
        raise ValueError(f'{LIVE_INDEX} is not a {LIVE_SCHEMA} file')
    entries = {e['run']: e for e in index['runs'] if isinstance(e, dict) and RUN_ID.fullmatch(str(e.get('run', '')))}
    adds, deletes, removed, changed, live_logs = {}, set(), [], [], set()
    pages, dataset_files, refresh = {}, {}, []   # Space path -> bytes (pinned below); dataset path -> bytes
    for run in sorted(set(entries) | set(updates)):
        prefix = f'{LIVE}/{run}/'
        if run in published:   # published normally: its live mirror is retired (Space and dataset)
            deletes |= {name for name in remote if name.startswith(prefix)}
            if entries.pop(run, None) is not None or any(n.startswith(prefix) for n in remote):
                removed.append(run)
            continue
        update = updates.get(run)
        if update is None:
            continue
        if update.get('bundle') is not None:
            bundle = Path(update['bundle'])
            local = {}
            for path in sorted(bundle.rglob('*')):
                if not path.is_file():
                    continue
                relative = path.relative_to(bundle).as_posix()
                safe_path(prefix + relative)
                if dataset_logs.category(relative) is not None:   # never uploaded to the Space
                    dataset_files[dataset_logs.dataset_path(run, relative, live=True)] = path.read_bytes()
                else:
                    local[prefix + relative] = path.read_bytes()
            pages.update(local)
            refresh.append(run)
            deletes |= {name for name in remote if name.startswith(prefix) and name not in local}
            live_logs |= {dataset_logs.dataset_path(run, name, live=True) for name in update.get('live_logs', ())}
        entries[run] = update['entry']
        changed.append(run)
    # The dataset first: the Space's live Inspect View pages are pinned to the commit holding their logs.
    logs = sync_dataset(api, files=dataset_files, refresh=refresh, retire=removed, add=add, delete=delete,
                        repo=dataset_repo, commit=commit,
                        message=COMMIT_PREFIX + ', '.join([*(f'update {r}' for r in changed),
                                                           *(f'retire {r}' for r in removed)]) + ' (live logs)')
    for name, page in pages.items():
        if name.endswith('/inspect-view/index.html'):
            page = dataset_logs.pin_commit(page, logs['revision'])
        if name not in remote or not matches(page, remote[name]):
            adds[name] = page
    new_index = {'schema': LIVE_SCHEMA, 'runs': sorted(entries.values(), key=lambda e: e.get('started_utc') or '',
                                                       reverse=True)}
    data = json.dumps(new_index, indent=2).encode()
    if LIVE_INDEX not in remote or not matches(data, remote[LIVE_INDEX]):
        adds[LIVE_INDEX] = data
    deletes -= set(adds)
    for name in [*adds, *deletes]:
        if not name.startswith(LIVE + '/'):
            raise ValueError('The live mirror may only change live/: ' + name)
        safe_path(name)
    for name, page in adds.items():
        if dataset_logs.category(name.split('/', 2)[2] if name.count('/') >= 2 else name) is not None:
            raise ValueError('A dataset file is never uploaded to the Space: ' + name)
        if len(page) >= LFS_SAFE_LIMIT:   # pages only: nothing in the Space's live/ may become LFS
            raise ValueError('Live file at or above the LFS limit: ' + name)
    result = {'base_revision': info.sha, 'runs_updated': changed, 'runs_removed': removed,
              'uploads': len(adds), 'deletions': len(deletes), 'bytes': sum(len(d) for d in adds.values()),
              'committed': False, 'dataset': logs,
              'live_log_uploads': [{'path': n, 'sha256': hashlib.sha256(dataset_files[n]).hexdigest()}
                                   for n in sorted(live_logs & set(logs['uploads']))]}
    if not commit or not (adds or deletes):
        return result
    message = COMMIT_PREFIX + ', '.join([*(f'update {r}' for r in changed), *(f'retire {r}' for r in removed)])
    response = space_commit(api, dataset_repo=dataset_repo, repo_id=repo, repo_type='space', parent_commit=info.sha,
                            commit_message=message,
                            operations=[*(add(path_in_repo=n, path_or_fileobj=d) for n, d in sorted(adds.items())),
                                        *(delete(path_in_repo=n) for n in sorted(deletes))])
    result.update(committed=True, revision=response.oid)
    return result


def prune_superseded(api, repo, revision, candidates, repo_type='space'):
    """Permanently delete the LFS objects of this mirror's superseded live Inspect logs, without rewriting
    history. ``candidates``: [{'path', 'sha256'}] recorded when the mirror uploaded them (synthesized
    snapshots only, never a log copied unchanged). An object is deleted only when no file of ``revision``
    (the whole repository: the logs dataset, or the Space for logs uploaded there by earlier versions of the
    mirror) references it and the Hub lists it under ``live/``; earlier live revisions then lack that log,
    nothing else changes. Returns (pruned, kept)."""
    referenced = set()
    for item in api.list_repo_tree(repo, repo_type=repo_type, revision=revision, recursive=True):
        lfs = getattr(item, 'lfs', None)
        if lfs is not None:
            referenced.add(lfs.sha256 if hasattr(lfs, 'sha256') else lfs['sha256'])
    kept = [c for c in candidates if c['sha256'] in referenced]
    wanted = {c['sha256'] for c in candidates if c['sha256'] not in referenced and HEX64.fullmatch(c['sha256'])}
    if not wanted:
        return [], kept
    objects = [f for f in api.list_lfs_files(repo, repo_type=repo_type)
               if (f.oid in wanted or f.file_oid in wanted) and str(f.filename).startswith(LIVE + '/')]
    if objects:
        api.permanently_delete_lfs_files(repo, objects, rewrite_history=False, repo_type=repo_type)
    found = {f.oid for f in objects} | {f.file_oid for f in objects}
    # An object the Hub does not list (stored as a regular file, or already gone) is not retried.
    return sorted(wanted & found), kept


# ---------------------------------------------------------------------------
# One pass and the loop


def load_state(path):
    try:
        state = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault('schema', 'gdm-live-mirror-state/1')
    state.setdefault('runs', {})
    state.setdefault('live_log_blobs', [])     # Space live logs of earlier mirror versions (pruned from the Space)
    state.setdefault('dataset_log_blobs', [])  # live logs uploaded to the logs dataset
    return state


def mirror_run(cloud, vm, root, run, record, *, work, buffer_dir, interval, inspect_view, now):
    """Snapshot, apply and render one run; returns its summary, bundle directory and live log paths."""
    source, bundle = work / 'work' / run, work / 'bundle' / run
    source.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        with tempfile.TemporaryDirectory(prefix='live-pull-', dir=work) as tmp:
            archive = Path(tmp) / 'snapshot.tar.gz'
            cloud.snapshot(vm, root, run, since_line=record.get('since_line', 0), evals=record.get('evals', {}),
                           buffer_dir=buffer_dir, target=archive)
            manifest = apply_snapshot(archive, source, run)
        record.update(since_line=manifest['runtime_lines'], evals=manifest['evals'])
        if not missing_blobs(source) or attempt:
            break
        record.update(since_line=0)   # a blob is missing locally: pull every referenced blob once
    records = runtime_records(source)
    state = 'finished' if any(r.get('kind') == 'run_stop' for r in records) else 'running'
    summary, live_logs = build_live_bundle(source, bundle, vm=vm, snapshot_utc=manifest['utc'], state=state,
                                           interval=interval, inspect_view=inspect_view, run=run)
    record.update(vm=vm, root=root, final=state == 'finished', entry=_entry(summary), last_pass_utc=_iso(now))
    return summary, bundle, live_logs


def run_pass(cloud, vms, *, state, work=WORK, api=None, download=None, add=None, delete=None, repo=DEFAULT_REPO,
             dataset_repo=DATASET_REPO, only=None, max_age=3600, buffer_dir=DEFAULT_BUFFER_DIR, interval=300, quiet_seconds=600,
             inspect_view=build_inspect_view, prune=True, now=None):
    now = time.time() if now is None else now
    work.mkdir(parents=True, exist_ok=True)
    updates, report = {}, {'utc': _iso(now), 'vms': {}, 'runs': {}}
    for vm, root in vms:
        known = {run: r for run, r in state['runs'].items() if r.get('vm') == vm}
        try:
            status = cloud.status(vm)
        except Exception as error:
            report['vms'][vm] = f'status unavailable: {error}'
            continue
        report['vms'][vm] = status
        if status != 'RUNNING':   # never start a VM; its unfinished runs keep their last snapshot
            for run, record in known.items():
                if not record.get('final') and record.get('entry') and record['entry'].get('state') != 'vm_stopped':
                    record['entry'] = dict(record['entry'], state='vm_stopped', state_label=STATE_LABELS['vm_stopped'])
                    updates[run] = {'bundle': None, 'entry': record['entry']}
            continue
        try:
            found = cloud.discover(vm, root, max_age)
        except Exception as error:
            report['vms'][vm] = f'RUNNING; discovery failed: {error}'
            continue
        for row in found:
            run = row['run']
            record = state['runs'].get(run, {})
            if only and run not in only:
                continue
            if row.get('stopped') and (run not in state['runs'] or record.get('final')):
                continue   # stopped before this mirror saw it, or already mirrored to the end
            try:
                summary, bundle, live_logs = mirror_run(cloud, vm, root, run, record, work=work, buffer_dir=buffer_dir,
                                                        interval=interval, inspect_view=inspect_view, now=now)
            except Exception as error:
                report['runs'][run] = f'failed: {type(error).__name__}: {error}'
                continue
            state['runs'][run] = record
            updates[run] = {'bundle': bundle, 'entry': record['entry'], 'live_logs': live_logs}
            report['runs'][run] = {'state': summary['state'], 'inspect_samples': summary['inspect_samples'],
                                   'problems': summary['problems']}
    if api is not None:
        try:
            report['upload'] = sync(api, download, updates=updates, add=add, delete=delete, repo=repo,
                                    dataset_repo=dataset_repo, now=now, quiet_seconds=quiet_seconds,
                                    own_revisions=state.get('own_revisions', ()))
            revision = report['upload'].get('revision')
            if revision:
                state['own_revisions'] = [*state.get('own_revisions', []), revision][-50:]
            if (report['upload'].get('dataset') or {}).get('committed'):
                known = {c['sha256'] for c in state['dataset_log_blobs']}
                state['dataset_log_blobs'] += [c for c in report['upload']['live_log_uploads']
                                               if c['sha256'] not in known]
            for run in report['upload'].get('runs_removed', []):
                state['runs'].get(run, {})['final'] = True
        except Exception as error:   # the next pass plans again from the latest revision
            report['upload'] = f'failed: {type(error).__name__}: {error}'
        else:
            # Only after the Space commit landed: its live pages no longer point at the superseded logs.
            dataset_head = (report['upload'].get('dataset') or {}).get('revision')
            if prune and report['upload'].get('revision') and dataset_head and state['dataset_log_blobs']:
                try:
                    pruned, kept = prune_superseded(api, dataset_repo, dataset_head, state['dataset_log_blobs'],
                                                    repo_type='dataset')
                    state['dataset_log_blobs'] = kept
                    report['pruned_live_logs'] = len(pruned)
                except Exception as error:   # kept as candidates; retried after the next commit
                    report['pruned_live_logs'] = f'failed: {type(error).__name__}: {error}'
            if prune and report['upload'].get('revision') and state['live_log_blobs']:
                try:   # live logs earlier mirror versions uploaded to the Space
                    pruned, kept = prune_superseded(api, repo, report['upload']['revision'], state['live_log_blobs'])
                    state['live_log_blobs'] = kept
                    report['pruned_space_live_logs'] = len(pruned)
                except Exception as error:
                    report['pruned_space_live_logs'] = f'failed: {type(error).__name__}: {error}'
    return report


def parse_vm(text):
    name, _, root = text.partition('=')
    if not re.fullmatch(r'[a-z]([-a-z0-9]*[a-z0-9])?', name):
        raise argparse.ArgumentTypeError('invalid VM name')
    return name, root or DEFAULT_REMOTE_ROOT


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--vm', action='append', type=parse_vm, required=True,
                        help=f'VM to mirror, optionally NAME=REMOTE_REPO_ROOT (default root {DEFAULT_REMOTE_ROOT}); repeatable')
    parser.add_argument('--run', action='append', help='mirror only this run ID (repeatable; default: every running run)')
    parser.add_argument('--interval', type=float, default=300, help='seconds between pass starts (default 300)')
    parser.add_argument('--once', action='store_true', help='one pass, then exit')
    parser.add_argument('--no-upload', action='store_true', help='build the live bundle locally only')
    parser.add_argument('--keep-live-history', action='store_true',
                        help="keep the LFS objects of superseded live Inspect logs (default: delete them once superseded)")
    parser.add_argument('--repo-id', default=DEFAULT_REPO)
    parser.add_argument('--dataset-repo', default=DATASET_REPO,
                        help=f'private logs dataset that receives the live Inspect logs (default {DATASET_REPO})')
    parser.add_argument('--quiet-seconds', type=float, default=600,
                        help='defer while another commit changed the Space less than this many seconds ago (default 600)')
    parser.add_argument('--max-age', type=float, default=3600,
                        help='a run counts as running while runtime.jsonl changed within this many seconds (default 3600)')
    parser.add_argument('--remote-buffer-dir', default=DEFAULT_BUFFER_DIR, help="Inspect's sample buffer directory on the VM")
    parser.add_argument('--work-dir', type=Path, default=WORK, help='local working copies (default runs/phase-b/live-mirror)')
    parser.add_argument('--gcloud', default=os.environ.get('GCLOUD', DEFAULT_GCLOUD if Path(DEFAULT_GCLOUD).exists() else 'gcloud'))
    parser.add_argument('--project', default=DEFAULT_PROJECT)
    parser.add_argument('--zone', default=DEFAULT_ZONE)
    args = parser.parse_args(argv)
    cloud = Cloud(args.gcloud, args.project, args.zone)
    api = download = add = delete = None
    if not args.no_upload:
        from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi, hf_hub_download
        api, download, add, delete = HfApi(), hf_hub_download, CommitOperationAdd, CommitOperationDelete
    state_path = args.work_dir / 'state.json'
    while True:
        started = time.monotonic()
        state = load_state(state_path)
        report = run_pass(cloud, args.vm, state=state, work=args.work_dir, api=api, download=download, add=add,
                          delete=delete, repo=args.repo_id, dataset_repo=args.dataset_repo, only=set(args.run or ()), max_age=args.max_age,
                          buffer_dir=args.remote_buffer_dir, interval=args.interval, quiet_seconds=args.quiet_seconds,
                          prune=not args.keep_live_history)
        args.work_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2))
        print(json.dumps(report), flush=True)
        if args.once:
            return 0
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == '__main__':
    raise SystemExit(main())
