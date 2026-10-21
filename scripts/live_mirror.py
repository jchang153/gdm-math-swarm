#!/usr/bin/env python3
"""Near-live mirror of running experiments to the private HF library (owner choice "option 1", October 21, 2026).

Runs on the operator's own computer, never on a VM:

    python scripts/live_mirror.py --vm collab-vm-1 [--vm owner-vm=~/gdm-math-swarm] [--interval 300]
    python scripts/live_mirror.py --vm collab-vm-1 --once --no-upload      # one local pass, nothing uploaded

Every pass (default every 5 minutes), for each RUNNING VM (never started, stopped or provisioned here):

1. lists the runs under ``REMOTE_ROOT/runs`` whose ``runtime.jsonl`` changed recently and has no
   ``run_stop`` yet (plus one final pass for a mirrored run that has just stopped);
2. pulls a consistent, read-only snapshot of each run's transcript sources over IAP SSH: the complete
   lines of ``runtime.jsonl``, ``settings.json``, an SQLite online backup of ``conference/state.sqlite``
   and of Inspect's realtime sample buffer (``log_realtime=True``; the live behavior judge reads the
   same buffer), the ``.eval`` logs when they changed, and the private-file snapshot blobs added since the
   last pass (Claude Code's own session JSONL blobs are left out: they duplicate the transcript and are
   never rendered). The VM side is a stdlib-only Python program sent inside the SSH command; it writes a
   tar stream to stdout and leaves nothing behind. Nothing is sent to the VM except that program and its
   arguments: no HF token, no provider key, no host environment;
3. rebuilds the transcript pages (the unchanged ``history.build_agent`` and ``export_transcripts.render_agent``,
   read through ``research.analysis.judge.live.LiveSource``) and the conference view
   (``export_conference.export``, with ``conference/events.jsonl`` rebuilt from the snapshot's events table
   and the timeline from conference and runtime records) into a local live bundle;
4. screens the bundle exactly like a normal publication (``publish_hf_library.safe_path`` and ``screen``),
   adds the private-Space navigation, and commits it with the operator's own HF login as ONE commit that
   touches only ``live/``: changed files are added, files of the previous snapshot that no longer exist are
   deleted, ``live/index.json`` (the catalogue's "Live now" list) is updated, and the live folder of any run
   that is now in the published catalogue is removed.

History growth is bounded: a pass that changed nothing makes no commit, unchanged files are never
re-uploaded, and transcript pages are stored gzip-compressed (decompressed in the browser), which also
keeps every page far below HF's 9.5 MiB LFS limit (a page that would still exceed it is replaced by a
notice). The Space history is never squashed or rewritten (published revisions are cited in the docs).

Coordination with the publication tools: a pass is deferred while the Space's head is not one of this
mirror's own commits and changed less than ``--quiet-seconds`` ago (default 600), so a publication or maintenance in progress is not
interrupted; every live commit is pinned to the revision it was planned on.

Researcher-only. Live pages are unfinished snapshots: conference (operational) outcomes only, no independent
evaluation and no behavior judge. No model calls, no paid inference, never reads credentials.
"""
import argparse
import base64
import datetime
import gzip
import hashlib
import html
import io
import json
import os
from pathlib import Path, PurePosixPath
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

from publish_hf_library import DEFAULT_REPO, SECRET, matches, safe_path, screen  # noqa: E402
from space_navigation import private_space_navigation  # noqa: E402

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
        out = io.BytesIO()
        self.ssh(vm, {'mode': 'discover', 'root': root, 'max_age': max_age}, out, timeout=300)
        found = json.loads(out.getvalue().decode() or '[]')
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

LOADER = r'''(async()=>{const s=document.getElementById('live-page');try{
if(typeof DecompressionStream==='undefined')throw new Error('this page needs a current browser with gzip decompression support');
const bytes=Uint8Array.from(atob(s.textContent),c=>c.charCodeAt(0));
const text=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
document.open();document.write(text);document.close();
if(location.hash){const t=document.getElementById(decodeURIComponent(location.hash.slice(1)));if(t)t.scrollIntoView();}
}catch(e){document.getElementById('live-loading').textContent='Could not load this live page: '+e.message;}})();'''
STYLE = ('<style>:root{color-scheme:light dark;--bg:#fafbfc;--fg:#18212b;--muted:#596579;--line:#dde3ea;--card:#fff;'
         '--accent:#155eaa;--warn:#fff6e0;--warnline:#c98a00}@media (prefers-color-scheme:dark){:root{--bg:#0f141a;'
         '--fg:#e3e8ee;--muted:#9aa7b6;--line:#2a333d;--card:#161d25;--accent:#7fb2ec;--warn:#2b2412;--warnline:#c98a00}}'
         'body{max-width:1100px;margin:32px auto;padding:0 16px;font:16px/1.5 system-ui,sans-serif;color:var(--fg);'
         'background:var(--bg)}a{color:var(--accent)}small,.muted{color:var(--muted)}.banner{background:var(--warn);'
         'border-left:4px solid var(--warnline);padding:10px 16px;border-radius:4px}.card{background:var(--card);'
         'border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:16px 0}table{border-collapse:collapse;'
         'width:100%}td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}'
         '.scroll{overflow-x:auto}dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}'
         'dt{color:var(--muted)}dd{margin:0}</style>')
LIVE_BANNER = ('<p class="banner live-banner"><strong>Live snapshot, not final.</strong> Mirrored from the running '
               'experiment; it may lag the run by several minutes and later turns are still missing. Conference '
               '(operational) outcomes only: no independent evaluation or behavior judge has run. The published run '
               'replaces this mirror when it finishes.</p>')


def screen_bytes(name, data):
    if SECRET.search(data):
        raise ValueError(f'Credential-like content detected in {name}; upload refused')


def compressed_page(document, title):
    """A small page that holds ``document`` gzip-compressed and writes it in the browser."""
    packed = base64.b64encode(gzip.compress(document.encode('utf-8'), mtime=0)).decode('ascii')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title>'
            '<p id="live-loading">Loading live snapshot…</p><script id="live-page" type="application/gzip+base64">'
            + packed + '</script><script>' + LOADER + '</script></html>')


def unpack_page(document):
    """The inner document of ``compressed_page`` (tests and checks)."""
    match = re.search(r'<script id="live-page" type="application/gzip\+base64">([A-Za-z0-9+/=]*)</script>', document)
    return gzip.decompress(base64.b64decode(match[1])).decode('utf-8')


def add_banner(document, banner=LIVE_BANNER):
    at = document.find('</style>')
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


def render_transcripts(source, out):
    """Write one transcript page per agent (``export_transcripts.render_agent``) from the snapshot's
    Inspect samples (realtime buffer, or the ``.eval`` once flushed); returns a row per agent."""
    from research.analysis.judge import history as hist, live
    import export_transcripts
    gate = live.inspect_gate()
    if gate:
        raise RuntimeError('Live transcripts unavailable: ' + gate)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob('agent-*.html'):
        old.unlink()
    inspect = source / 'inspect'
    for db in (source / 'samplebuffer').glob('*/*.db'):   # a buffer whose .eval header was not copied yet
        name = db.name.rsplit('.', 2)[0]
        if name.endswith('.eval') and not (inspect / name).exists():
            (inspect / name).touch()
    base = hist.load_run_base(source)
    reader = live.LiveSource(source, db_dir=source / 'samplebuffer')
    rows = []
    try:
        for agent, entry in reader.index().items():
            try:
                sample, complete = reader.read(entry)
            except live.LiveSourceUnavailable as error:
                rows.append({'agent': agent, 'error': str(error)})
                continue
            history = hist.build_agent(sample, base, log_name='inspect/' + Path(entry['log']).name)
            page, _ = export_transcripts.render_agent(history, source.name)
            (out / (agent + '.html')).write_text(page, encoding='utf-8')
            times = [i.get('time') for i in history.items if i.get('time')]
            rows.append({'agent': agent, 'turns': history.stats.get('turns'), 'items': history.stats.get('items'),
                         'tokens': history.stats.get('tokens_total'), 'complete': complete,
                         'exit': (history.exit or {}).get('status'), 'last_time': max(times) if times else None})
    finally:
        reader.close()
    return rows


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


def summarize(source, rows, *, vm, snapshot_utc, state):
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
    summary = {'run': source.name, 'vm': vm, 'state': state, 'state_label': STATE_LABELS[state],
               'snapshot_utc': _iso(snapshot_utc), 'updated_utc': _iso(snapshot_utc),
               'started_utc': _iso(start) if start else None,
               'elapsed_seconds': round(end - start) if start else None,
               'max_run_seconds': config.get('max_run_seconds'), 'model': config.get('candidate_model_id'),
               'track': config.get('track'), 'system_prompt_mode': config.get('system_prompt_mode'),
               'agents': config.get('agent_count') or len(rows) or None,
               'agent_exits': sum(1 for r in records if r.get('kind') == 'agent_exit'),
               'agent_restarts': sum(1 for r in records if r.get('kind') == 'agent_restart'),
               'transcripts': sum(1 for r in rows if 'error' not in r), 'solved': None, 'task_count': None,
               'submissions': {}}
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


def landing_page(summary, rows, problems, interval):
    esc = html.escape
    subs = ', '.join(f'{esc(str(k))} {v}' for k, v in sorted(summary['submissions'].items())) or 'none yet'
    facts = [('State', summary['state_label']), ('Snapshot taken', summary['snapshot_utc'] + ' (UTC)'),
             ('Elapsed', f"{_minutes(summary['elapsed_seconds'])} of {_minutes(summary['max_run_seconds'])}"),
             ('Model', summary['model'] or 'unknown'), ('Condition', summary['track'] or 'unknown'),
             ('System prompt', summary['system_prompt_mode'] or 'default'),
             ('Agents', f"{summary['agents'] or '?'} ({summary['agent_exits']} exited, {summary['agent_restarts']} restarts)"),
             ('Tasks solved (conference judge)', f"{summary['solved'] if summary['solved'] is not None else '?'} / "
                                                 f"{summary['task_count'] or '?'}"),
             ('Submissions by conference outcome', subs), ('VM', summary['vm'])]
    card = '<dl>' + ''.join(f'<dt>{esc(k)}</dt><dd>{esc(str(v)) if k != "Submissions by conference outcome" else v}</dd>'
                            for k, v in facts) + '</dl>'
    notes = ''.join(f'<li>{esc(p)}</li>' for p in problems)
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>Live · ' + esc(summary['run']) + '</title>' + STYLE +
            '<p><a href="../../index.html">Experiment library</a></p><h1>' + esc(summary['run']) + ' <small>live</small></h1>'
            + LIVE_BANNER + f'<div class="card">{card}</div>'
            '<p><a href="transcripts/index.html">Agent transcripts</a> · <a href="conference-view/index.html">Conference</a></p>'
            f'<p class="muted">Refreshed about every {max(1, round(interval / 60))} minutes while the run is going. '
            '"Solved" and submission outcomes are the conference\'s operational judge: acceptance there is not '
            'independent mathematical validity. Exits and restarts are counted from the runtime log.</p>'
            + (f'<h2>Mirror notes</h2><ul>{notes}</ul>' if notes else '') + '</html>')


def transcripts_index(summary, rows):
    esc = html.escape
    body = []
    for row in rows:
        if 'error' in row:
            body.append(f'<tr><td>{esc(row["agent"])}</td><td colspan="5">not available in this snapshot: '
                        f'{esc(row["error"])}</td></tr>')
            continue
        state = 'session ended' if row['complete'] else 'in progress'
        body.append(f'<tr><td><a href="{esc(row["agent"])}.html">{esc(row["agent"])}</a></td><td>{state}</td>'
                    f'<td>{row["turns"]}</td><td>{row["items"]}</td><td>{esc(str(row["exit"] or "—"))}</td>'
                    f'<td>{esc(str(row["last_time"] or "—"))}</td></tr>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>Live transcripts · ' + esc(summary['run']) + '</title>'
            + STYLE + '<p><a href="../index.html">Live summary</a> · <a href="../conference-view/index.html">Conference</a></p>'
            '<h1>Agent transcripts <small>live</small></h1>' + LIVE_BANNER +
            f'<p class="muted">Run {esc(summary["run"])} · snapshot {esc(summary["snapshot_utc"])} (UTC). Each page is the '
            'same full reconstructed history as the published transcripts, up to this snapshot. Private researcher '
            'export: never provide it to experimental agents.</p>'
            '<div class="scroll"><table><thead><tr><th>Agent</th><th>Session</th><th>Model turns</th><th>Items</th>'
            '<th>Exit</th><th>Last recorded activity (UTC)</th></tr></thead><tbody>' + ''.join(body) +
            '</tbody></table></div></html>')


def build_live_bundle(source, bundle, *, vm, snapshot_utc, state, interval, render=render_transcripts):
    """Render the live views of the local source copy into ``bundle`` (replaced). Returns the summary."""
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
    try:
        rows = render(source, source / 'transcripts')
    except Exception as error:
        problems.append(f'transcripts unavailable in this snapshot: {error}')
        rows = []
    conference = False
    try:
        import export_conference
        export_conference.export(source)
        conference = True
    except Exception as error:
        problems.append(f'conference view unavailable in this snapshot ({type(error).__name__}: {error})')
    # The screening of a normal publication, applied to the records behind the views (which the
    # conference view and the transcript pages hold compressed) and to every page before compression.
    for name in ('runtime.jsonl', 'conference/state.sqlite', 'conference/events.jsonl', 'settings.json'):
        if (source / name).is_file():
            screen_bytes(name, (source / name).read_bytes())
    summary = summarize(source, rows, vm=vm, snapshot_utc=snapshot_utc, state=state)
    summary['problems'] = problems
    pages = {'index.html': landing_page(summary, rows, problems, interval),
             'transcripts/index.html': transcripts_index(summary, rows)}
    available = set(pages) | {f'transcripts/{r["agent"]}.html' for r in rows if 'error' not in r}
    if conference:
        for path in sorted((source / 'conference-view').iterdir()):
            if path.is_file() and not path.is_symlink():
                available.add('conference-view/' + path.name)
    for name in sorted(available - set(pages)):
        document = (source / name).read_text(encoding='utf-8')
        screen_bytes(name, document.encode('utf-8'))
        if name.endswith('.html'):
            document = fix_links(document, name, available)
            if name.startswith('transcripts/'):
                inner = private_space_navigation(add_banner(document))
                document = compressed_page(inner, name.split('/')[-1][:-5] + ' · live')
            elif name == 'conference-view/index.html':
                document = add_banner(document)
        pages[name] = document
    notices = []
    for name, document in pages.items():
        if name.endswith('.html'):
            document = private_space_navigation(document)
        data = document.encode('utf-8')
        if len(data) >= LFS_SAFE_LIMIT:
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
    for path in sorted(bundle.rglob('*')):   # the screening of a normal publication
        if path.is_file():
            safe_path(path.relative_to(bundle).as_posix())
            screen(path)
    return summary


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


def sync(api, download, *, updates, add, delete, repo=DEFAULT_REPO, now=None, quiet_seconds=600, commit=True,
         own_revisions=()):
    """One live commit. ``updates``: {run: {'bundle': Path | None, 'entry': dict}} (bundle None: only the
    catalogue entry changes, e.g. the VM stopped). Touches only live/; never another path. Deferred while
    the Space's head is not one of this mirror's own commits (``own_revisions``) and changed less than
    ``quiet_seconds`` ago: another publisher is probably still committing."""
    now = time.time() if now is None else now
    info = api.repo_info(repo, repo_type='space')
    if not info.private:
        raise ValueError('The live mirror requires a private Space')
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
    adds, deletes, removed, changed = {}, set(), [], []
    for run in sorted(set(entries) | set(updates)):
        prefix = f'{LIVE}/{run}/'
        if run in published:   # published normally: its live mirror is retired
            deletes |= {name for name in remote if name.startswith(prefix)}
            if entries.pop(run, None) is not None or any(n.startswith(prefix) for n in remote):
                removed.append(run)
            continue
        update = updates.get(run)
        if update is None:
            continue
        if update.get('bundle') is not None:
            bundle = Path(update['bundle'])
            local = {prefix + p.relative_to(bundle).as_posix(): p for p in sorted(bundle.rglob('*')) if p.is_file()}
            for name, path in local.items():
                safe_path(name)
                if name not in remote or not matches(path.read_bytes(), remote[name]):
                    adds[name] = path.read_bytes()
            deletes |= {name for name in remote if name.startswith(prefix) and name not in local}
        entries[run] = update['entry']
        changed.append(run)
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
    for name, data in adds.items():
        if len(data) >= LFS_SAFE_LIMIT:
            raise ValueError('Live file at or above the LFS limit: ' + name)
    result = {'base_revision': info.sha, 'runs_updated': changed, 'runs_removed': removed,
              'uploads': len(adds), 'deletions': len(deletes), 'bytes': sum(len(d) for d in adds.values()),
              'committed': False}
    if not commit or not (adds or deletes):
        return result
    message = COMMIT_PREFIX + ', '.join([*(f'update {r}' for r in changed), *(f'retire {r}' for r in removed)])
    response = api.create_commit(repo_id=repo, repo_type='space', parent_commit=info.sha, commit_message=message,
                                 operations=[*(add(path_in_repo=n, path_or_fileobj=d) for n, d in sorted(adds.items())),
                                             *(delete(path_in_repo=n) for n in sorted(deletes))])
    result.update(committed=True, revision=response.oid)
    return result


# ---------------------------------------------------------------------------
# One pass and the loop


def load_state(path):
    try:
        state = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        state = {}
    state.setdefault('schema', 'gdm-live-mirror-state/1')
    state.setdefault('runs', {})
    return state


def mirror_run(cloud, vm, root, run, record, *, work, buffer_dir, interval, render, now):
    """Snapshot, apply and render one run; returns its summary and bundle directory."""
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
    summary = build_live_bundle(source, bundle, vm=vm, snapshot_utc=manifest['utc'], state=state,
                                interval=interval, render=render)
    record.update(vm=vm, root=root, final=state == 'finished', entry=_entry(summary), last_pass_utc=_iso(now))
    return summary, bundle


def run_pass(cloud, vms, *, state, work=WORK, api=None, download=None, add=None, delete=None, repo=DEFAULT_REPO,
             only=None, max_age=3600, buffer_dir=DEFAULT_BUFFER_DIR, interval=300, quiet_seconds=600,
             render=render_transcripts, now=None):
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
                summary, bundle = mirror_run(cloud, vm, root, run, record, work=work, buffer_dir=buffer_dir,
                                             interval=interval, render=render, now=now)
            except Exception as error:
                report['runs'][run] = f'failed: {type(error).__name__}: {error}'
                continue
            state['runs'][run] = record
            updates[run] = {'bundle': bundle, 'entry': record['entry']}
            report['runs'][run] = {'state': summary['state'], 'transcripts': summary['transcripts'],
                                   'problems': summary['problems']}
    if api is not None:
        try:
            report['upload'] = sync(api, download, updates=updates, add=add, delete=delete, repo=repo, now=now,
                                    quiet_seconds=quiet_seconds, own_revisions=state.get('own_revisions', ()))
            if report['upload'].get('revision'):
                state['own_revisions'] = [*state.get('own_revisions', []), report['upload']['revision']][-50:]
            for run in report['upload'].get('runs_removed', []):
                state['runs'].get(run, {})['final'] = True
        except Exception as error:   # the next pass plans again from the latest revision
            report['upload'] = f'failed: {type(error).__name__}: {error}'
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
    parser.add_argument('--repo-id', default=DEFAULT_REPO)
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
                          delete=delete, repo=args.repo_id, only=set(args.run or ()), max_age=args.max_age,
                          buffer_dir=args.remote_buffer_dir, interval=args.interval, quiet_seconds=args.quiet_seconds)
        args.work_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2))
        print(json.dumps(report), flush=True)
        if args.once:
            return 0
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == '__main__':
    raise SystemExit(main())
