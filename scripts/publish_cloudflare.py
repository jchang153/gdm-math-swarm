#!/usr/bin/env python3
"""Publish the shared experiment library to Cloudflare Pages (owner decision, October 27, 2026: Cloudflare replaces
the HF Space as the library; docs/plan.md, "Experiment library on Cloudflare (October 27, 2026)").

The Cloudflare site is public, like the HF Space it replaces. Nothing private goes to it: Inspect logs, raw JSONL,
snapshots, databases, transcripts, timelines and resume bundles (``dataset_logs.category``) are never deployed,
and every Inspect View carries a notice shim instead of the HF sign-in shim (private logs answer 404 locally).
No private Cloudflare store is configured (R2 is not enabled on the account and Access cannot be managed with the
wrangler login); private files stay in the private HF logs dataset and on the researchers' machines.

Subcommands (each plans read-only by default; ``--commit`` deploys):

    publish_cloudflare.py mirror-space [--revision SHA] [--commit] [--receipt FILE]
        Mirror the public HF Space at a pinned revision (anonymous download, no credential) into the site.
        Runs already on Cloudflare but absent from the Space (another collaborator's) are preserved and stay in
        the catalogue. Changes to runs present in both come from the Space's guarded HF maintenance and are
        listed in the receipt.
    publish_cloudflare.py publish --bundle PREPARED_DIRECTORY [--dataset-commit SHA] [--replace-pages] [--commit]
        Merge a prepare_hf_library.py bundle: new runs are added, every existing run file is immutable (with
        --replace-pages only the bundle runs' .html pages may change; each replacement is recorded), the
        catalogue is merged newest first. Files of the bundle's dataset-files/ (private) are never deployed;
        with --dataset-commit (the HF logs-dataset commit that holds them) pages link there, otherwise links to
        them point to the site's private-records notice.
    publish_cloudflare.py restyle [--run RUN ...] [--commit] [--receipt FILE]
        Re-render the layout of pages already on the site (``site_layout.apply``: results.html, the conference
        view and the simplified judge report of every run, or only the given runs) and the catalogue page from
        its entries. Only existing .html pages change; text, links, records and every other file stay as
        served, and each replacement is recorded with old and new digests.
    publish_cloudflare.py verify [--url URL] [--full] [--run RUN ...]
        Read-only check of the served site against its site-manifest.json (see ``verify``).

The site's own state is its ``site-manifest.json`` (every served file with bytes and SHA-256). A deploy rebuilds
the full tree from that manifest (fetched from the pinned production deployment, with a local content-addressed
cache) plus the change, so every collaborator's runs are preserved. Guards: the production deployment must be the
same before and after planning (optimistic lock) and the new deployment must directly follow it; every file passes
the credential screen, path rules, the private-category screen and Cloudflare's limits (20,000 files, 25 MiB per
file); Pages special files (_worker.js, _headers, _redirects, _routes.json, functions/) never come from a source.
Wrangler runs with the operator's own login, from a clean working directory (never the repository, whose host-only
.env it must not see). Nothing here reads or prints a credential, changes visibility or adds a spending step.
"""
import argparse
import datetime
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_experiment_index import unlisted_runs, write_index  # noqa: E402
import dataset_logs  # noqa: E402
import hf_visibility  # noqa: E402
import site_layout  # noqa: E402
import site_style  # noqa: E402
from publish_hf_library import (SECRET, load_bundle, merge_entries, packed_text,  # noqa: E402
                                safe_path)

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'gdm-math-swarm-experiments'
SITE_URL = f'https://{PROJECT}.pages.dev'
BRANCH = 'main'
SITE_MANIFEST = 'site-manifest.json'
SITE_SCHEMA = 'gdm-cloudflare-site/1'
NOT_FOUND = '404.html'
PRIVATE_PAGE = 'private-records.html'
GENERATED = frozenset({SITE_MANIFEST, NOT_FOUND, PRIVATE_PAGE})
MAX_FILES = 20_000                # Cloudflare Pages: files per deployment
MAX_FILE_BYTES = 25 * 1024 * 1024  # Cloudflare Pages: bytes per file
# HF Space configuration and the HF OAuth sign-in page: meaningless on Cloudflare, never deployed.
HF_ONLY = frozenset({'.gitattributes', 'README.md', dataset_logs.SIGNIN_FILE})
# Pages special files: code or routing that must never come from a run bundle or the Space.
PAGES_SPECIAL = frozenset({'_worker.js', '_headers', '_redirects', '_routes.json'})
WRANGLER = os.environ.get('WRANGLER', 'npx --yes wrangler@4.154.0')
CACHE = ROOT / 'runs' / 'phase-b' / 'cloudflare' / 'cache'
SHIM_FILE = Path(__file__).resolve().parent / 'cf_site' / 'private-logs.js'
SHIM_MARKER = 'id="private-logs"'
HF_SHIM = re.compile(r'<script id="evallog-fetch"[^>]*>.*?</script>', re.S)
CF_SHIM = re.compile(r'<script id="private-logs"[^>]*>.*?</script>', re.S)
DATASET_LINK = re.compile(r'href="https://huggingface\.co/datasets/[^"/]+/[^"/]+/(?:blob|resolve|tree)/'
                          + re.escape(dataset_logs.COMMIT_PLACEHOLDER) + r'/[^"]*"')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def digest(data):
    return {'bytes': len(data), 'sha256': sha256(data)}


def private_category(name):
    """The logs-dataset category of a site path (``live/`` areas included), or None."""
    parts = name.split('/')
    if parts[0] == 'live':
        parts = parts[1:]
    return dataset_logs.category('/'.join(parts[1:])) if len(parts) > 1 else None


def site_gate(name, data):
    """The last screen of every deployed file: path rules, host-only and credential file names, Pages special
    files, the credential screen (packed captured files included), no private category, Cloudflare's per-file
    limit. Raises ValueError; returns the bytes."""
    path = safe_path(name)  # no dot-folders, no traversal, no credential file names
    if path.name.lower() in {'.env', 'host.env'} or path.parts[0] == 'functions' or path.name in PAGES_SPECIAL:
        raise ValueError('Pages special or host-only file refused: ' + name)
    if SECRET.search(data) or SECRET.search(packed_text(name, data)):
        raise ValueError('Credential-like content; deploy refused: ' + name)
    if private_category(name) is not None:
        raise ValueError(f'{name} is private ({private_category(name)}) and is never deployed to the public site')
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f'{name} exceeds Cloudflare Pages\' 25 MiB per-file limit')
    return data


# --- Inspect View notice and private links -------------------------------------------------------------------

def records_url(document):
    """Where members read a run's private logs: the HF logs dataset folder of the page's HF shim, or ''."""
    settings = dataset_logs.shim_settings(document)
    if not settings or not re.fullmatch(r'[0-9a-f]{40}', settings[1]):
        return ''
    repo, commit, folder = settings
    return f'{dataset_logs.HUB}/datasets/{repo}/tree/{commit}/{folder}'


def notice_shim(records=''):
    if records and not re.fullmatch(r'https://huggingface\.co/datasets/[A-Za-z0-9_./-]+', records):
        raise ValueError('Unsafe records link: ' + records)
    source = SHIM_FILE.read_text(encoding='utf-8')
    return f'<script {SHIM_MARKER} data-records="{html.escape(records, quote=True)}">\n{source}</script>'


def inspect_page(document, records=None):
    """An Inspect View index.html for Cloudflare: the HF sign-in shim (or an earlier notice) replaced by the
    private-logs notice, inlined first in <head>. ``records`` defaults to the HF shim's dataset folder."""
    if records is None:
        records = records_url(document)
    script = notice_shim(records)
    for pattern in (HF_SHIM, CF_SHIM):
        if pattern.search(document):
            return pattern.sub(lambda _: script, document, count=1)
    head = re.search(r'<head(?:\s[^>]*)?>', document, re.I)
    if not head:
        raise ValueError('Inspect View index.html has no <head>')
    return document[:head.end()] + script + document[head.end():]


def is_inspect_page(name):
    return name.endswith('/inspect-view/index.html')


def private_links(document, page):
    """Links to private files of an unpinned bundle (dataset placeholder) repointed to the site's notice."""
    target = '../' * page.count('/') + PRIVATE_PAGE
    return DATASET_LINK.sub(f'href="{target}"', document)


def private_page():
    from site_style import themed
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Private records</title>'
            '<style>body{font:16px/1.55 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;'
            'background:var(--bg);color:var(--fg)}a{color:var(--link)}</style></head><body>'
            '<h1>Private records</h1><p>Inspect logs (agent transcripts), raw JSONL records, snapshots, databases, '
            'activity timelines and resume bundles are private. They are not served by this public site; the '
            'researchers keep them.</p><p><a href="index.html">Back to the experiment library</a></p></body></html>')
    return themed(body).encode()


def not_found_page():
    from site_style import themed
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Not found</title>'
            '<style>body{font:16px/1.55 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;'
            'background:var(--bg);color:var(--fg)}a{color:var(--link)}</style></head><body>'
            '<h1>Not found</h1><p>This page is not part of the experiment library. Private records are never '
            'served here.</p><p><a href="/">Experiment library</a></p></body></html>')
    return themed(body).encode()


# --- current site --------------------------------------------------------------------------------------------

def run_wrangler(args, cwd, runner=subprocess.run):
    """Wrangler with the operator's own login, from ``cwd`` (a clean scratch directory, never the repository)."""
    cwd = Path(cwd).resolve()
    if cwd == ROOT or ROOT in cwd.parents:
        raise ValueError('Run wrangler outside the repository checkout (its host-only .env stays unseen)')
    result = runner([*shlex.split(WRANGLER), *args], cwd=cwd, capture_output=True, text=True,
                    env={**os.environ, 'CI': '1', 'WRANGLER_SEND_METRICS': 'false'})
    if result.returncode:
        raise RuntimeError('wrangler ' + ' '.join(args[:3]) + ' failed:\n' + (result.stdout + result.stderr)[-2000:])
    return result.stdout


def _field(record, *names):
    lowered = {k.lower(): v for k, v in record.items()}
    for name in names:
        if lowered.get(name) is not None:
            return lowered[name]
    return None


def deployments(cwd, runner=subprocess.run, project=PROJECT):
    """Production deployments, newest first: [{'id', 'url'}]."""
    out = run_wrangler(['pages', 'deployment', 'list', '--project-name', project, '--environment', 'production',
                        '--json'], cwd, runner)
    start = out.find('[')
    records = json.loads(out[start:]) if start >= 0 else []
    result = []
    for record in records:
        result.append({'id': str(_field(record, 'id')), 'url': _field(record, 'deployment', 'url'),
                       'status': _field(record, 'status'), 'branch': _field(record, 'branch')})
    return result


def fetch(url, opener=None):
    """GET ``url`` following redirects (Pages answers foo.html with a 308 to foo); (status, headers, bytes)."""
    request = urllib.request.Request(url, headers={'User-Agent': 'gdm-math-swarm-publisher'})
    for _ in range(5):
        try:
            with (opener or urllib.request.urlopen)(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 307, 308) and error.headers.get('Location'):
                request = urllib.request.Request(urllib.parse.urljoin(request.full_url, error.headers['Location']),
                                                 headers=request.headers)
                continue
            return error.code, dict(error.headers), error.read()
    raise RuntimeError('Too many redirects: ' + url)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NO_REDIRECT = urllib.request.build_opener(_NoRedirect).open


def file_url(base, name):
    return base.rstrip('/') + '/' + '/'.join(urllib.parse.quote(part) for part in name.split('/'))


def load_site(base, getter=fetch, cache=CACHE):
    """(manifest, {name: bytes}) of the deployment at ``base``; ({}, {}) for an empty project. Every file is
    checked against the manifest's SHA-256 (from the content-addressed local cache when present)."""
    if not base:
        return {}, {}
    status, _, data = getter(file_url(base, SITE_MANIFEST))
    if status != 200:
        raise ValueError(f'{SITE_MANIFEST} not served by the current deployment (HTTP {status})')
    manifest = json.loads(data)
    if manifest.get('schema') != SITE_SCHEMA:
        raise ValueError('Unexpected site manifest schema')
    files = {}
    cache = Path(cache)
    for name, record in sorted(manifest['files'].items()):
        cached = cache / record['sha256'][:2] / record['sha256']
        data = cached.read_bytes() if cached.is_file() else None
        if data is None or sha256(data) != record['sha256']:
            status, _, data = getter(file_url(base, name))
            if status != 200 or digest(data) != {'bytes': record['bytes'], 'sha256': record['sha256']}:
                raise ValueError(f'Served file differs from the site manifest: {name} (HTTP {status})')
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        files[name] = data
    return manifest, files


def remember(files, cache=CACHE):
    for data in files.values():
        target = Path(cache) / sha256(data)[:2] / sha256(data)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


# --- plans ---------------------------------------------------------------------------------------------------

def catalogue(files):
    data = files.get('experiments.json')
    if data is None:
        return []
    index = json.loads(data)
    if index.get('warnings'):
        raise ValueError('Existing catalogue requires review')
    return index['experiments']


def run_of(name):
    return name.split('/')[0] if '/' in name else None


def render_catalogue(entries, unlisted):
    entries = [x for x in entries if x['run'] not in unlisted]
    with tempfile.TemporaryDirectory() as tmp:
        write_index(tmp, entries, hosted=True)
        return {name: (Path(tmp) / name).read_bytes() for name in ('index.html', 'experiments.json')}


def space_files(snapshot):
    """The deployable files of an HF Space snapshot directory: {name: bytes}, plus {category: count} left out."""
    snapshot = Path(snapshot)
    files, private = {}, {}
    for path in sorted(snapshot.rglob('*')):
        relative = path.relative_to(snapshot)
        if path.is_symlink() and relative.parts[0] != '.git':
            raise ValueError('Linked file in the Space snapshot refused')
        if relative.parts[0] in ('.cache', '.git') or not path.is_file():
            continue
        name = relative.as_posix()
        if name in HF_ONLY:
            continue
        category = private_category(name)
        if category is not None:
            private[category] = private.get(category, 0) + 1
            continue
        data = path.read_bytes()
        if data.startswith(LFS_POINTER):
            raise ValueError('LFS pointer instead of the file: ' + name)
        files[name] = inspect_page(data.decode('utf-8')).encode() if is_inspect_page(name) else data
    return files, private


def plan_mirror(base_files, space, revision, unlisted):
    """The site after mirroring the Space: Space files win; runs only on the site are preserved and stay listed."""
    space_runs = {run_of(n) for n in space if run_of(n)}
    kept = {n: d for n, d in base_files.items() if run_of(n) and run_of(n) not in space_runs}
    files = {**kept, **space}
    preserved = sorted({run_of(n) for n in kept})
    if preserved:  # another collaborator's runs published only on the site: keep their catalogue entries
        site_only = [e for e in catalogue(base_files) if e['run'] in preserved]
        files.update(render_catalogue(merge_entries(catalogue(space), site_only), unlisted))
    changed = sorted(n for n in space if run_of(n) and n in base_files and base_files[n] != space[n])
    removed = sorted(n for n in base_files if n not in files and n not in GENERATED)  # run files the Space removed
    return files, {'source': {'kind': 'hf-space', 'repo': hf_visibility.SPACE_REPO, 'revision': revision},
                   'preserved_site_only_runs': preserved, 'changed_files': changed, 'removed_files': removed}


def plan_bundle(base_files, bundle, unlisted, dataset_commit=None, replace_pages=False):
    """The site after merging a prepared bundle (immutable existing run files unless --replace-pages)."""
    manifest, incoming = load_bundle(bundle)
    if manifest.get('layout') == 'records-in-space':
        raise ValueError('Legacy records-in-space bundle refused: raw records are private')
    manifest = dict(manifest)
    private = manifest.pop('dataset', {})
    hidden = sorted(set(manifest['runs']) & set(unlisted))
    if hidden:
        raise ValueError('Run(s) unlisted by data/library-unlisted.json: ' + ', '.join(hidden))
    selected = set(manifest['runs'])
    bundle = Path(bundle)
    files = dict(base_files)
    added, replaced = [], []
    for name in sorted(manifest['files']):
        if '/' not in name or name in HF_ONLY:
            continue  # root pages are regenerated below; HF configuration never deploys
        data = (bundle / name).read_bytes()
        if dataset_logs.COMMIT_PLACEHOLDER.encode() in data:
            if dataset_commit:
                data = dataset_logs.pin_commit(data, dataset_commit)
            else:
                data = private_links(data.decode('utf-8'), name).encode()
        if is_inspect_page(name):
            data = inspect_page(data.decode('utf-8')).encode()
        if dataset_logs.COMMIT_PLACEHOLDER.encode() in data and not is_inspect_page(name):
            raise ValueError('Page still carries the dataset commit placeholder: ' + name)
        if name in base_files and base_files[name] != data:
            if not (replace_pages and name.endswith('.html')):
                raise ValueError('Immutable site run file differs: ' + name)
            replaced.append({'path': name, 'old': digest(base_files[name]), 'new': digest(data)})
        elif name not in base_files:
            added.append(name)
        files[name] = data
    unstaged = sorted(n for n in base_files if run_of(n) in selected and n not in manifest['files'])
    if unstaged:
        raise ValueError('Site run contains an unstaged file: ' + unstaged[0])
    entries = merge_entries(catalogue(base_files), incoming)
    files.update(render_catalogue(entries, unlisted))
    if 'archive-manifest.json' in base_files:  # the HF-origin run record; new runs are recorded in site-manifest
        files['archive-manifest.json'] = base_files['archive-manifest.json']
    return files, {'source': {'kind': 'bundle', 'runs': sorted(selected),
                              **({'dataset_commit': dataset_commit} if dataset_commit else {})},
                   'added_files': len(added), 'replaced_pages': replaced,
                   'private_files_not_deployed': {'files': len(private),
                                                  'bytes': sum(x['bytes'] for x in private.values())}}


def plan_restyle(base_files, unlisted, runs=None):
    """The site with its run pages in the current layout (``site_layout.apply``) and the catalogue re-rendered.

    Pages only: no file is added or removed, non-HTML files never change, and a page changes only by the layout
    transform (which keeps text, links and scripts). ``runs`` limits the run pages touched (default: all)."""
    known = {run_of(n) for n in base_files if run_of(n)}
    missing = sorted(set(runs or ()) - known)
    if missing:
        raise ValueError('Run(s) not on the site: ' + ', '.join(missing))
    files, replaced, legacy = dict(base_files), [], []
    for name in sorted(base_files):
        run = run_of(name)
        if not run or run == 'live' or (runs and run not in runs) or site_layout.kind(name) is None:
            continue
        old = base_files[name]
        if site_style.MARKER.encode() not in old:  # a page from before the shared theme: left as served
            legacy.append(name)
            continue
        new = site_layout.apply(name, old.decode('utf-8')).encode('utf-8')
        if new == old:
            continue
        replaced.append({'path': name, 'old': digest(old), 'new': digest(new)})
        files[name] = new
    files.update(render_catalogue(catalogue(base_files), unlisted))
    return files, {'source': {'kind': 'restyle', 'runs': sorted(runs) if runs else 'all'},
                   'replaced_pages': replaced, 'unthemed_pages_left_as_served': legacy}


def finish(files, base_manifest, plan, project=PROJECT):
    """Add the generated pages and the site manifest; gate every file and the deployment limits."""
    files = {n: d for n, d in files.items() if n not in GENERATED}
    files[NOT_FOUND] = not_found_page()
    files[PRIVATE_PAGE] = private_page()
    for name, data in files.items():
        site_gate(name, data)
    entries = catalogue(files)
    listed = [e['run'] for e in entries]
    runs = listed + sorted({run_of(n) for n in files if run_of(n) and run_of(n) != 'live'} - set(listed))
    history = list(base_manifest.get('history', []))[-49:]
    history.append({'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                    **{k: plan[k] for k in ('source',)}})
    manifest = {'schema': SITE_SCHEMA, 'project': project, 'runs': runs,
                'public': True, 'private_store': None,
                'files': {n: digest(d) for n, d in sorted(files.items())},
                'total_bytes': sum(len(d) for d in files.values()), 'history': history}
    data = json.dumps(manifest, indent=1).encode()
    files[SITE_MANIFEST] = site_gate(SITE_MANIFEST, data)
    if len(files) > MAX_FILES:
        raise ValueError(f'{len(files)} files exceed Cloudflare Pages\' {MAX_FILES:,}-file limit per deployment')
    return files, manifest


def summary(files, base_files, plan, base):
    return {**plan, 'project': PROJECT, 'base_deployment': base,
            'files': len(files), 'bytes': sum(len(d) for d in files.values()),
            'largest_file_bytes': max(len(d) for d in files.values()),
            'new_or_changed': sum(1 for n, d in files.items() if base_files.get(n) != d and n != SITE_MANIFEST),
            'runs': len({run_of(n) for n in files if run_of(n)})}


# --- deploy --------------------------------------------------------------------------------------------------

def stage(files, directory):
    directory = Path(directory)
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return directory


def deploy(files, base, message, workdir, runner=subprocess.run, project=PROJECT):
    """Stage and deploy; the production deployment must still be ``base`` and the new one must follow it."""
    current = deployments(workdir, runner, project)
    if (current[0]['id'] if current else None) != (base['id'] if base else None):
        raise ValueError('Production deployment changed since planning; rerun to merge the latest site')
    site = stage(files, Path(workdir) / 'site')
    out = run_wrangler(['pages', 'deploy', str(site), '--project-name', project, '--branch', BRANCH,
                        '--commit-message', message[:300], '--commit-dirty=true'], workdir, runner)
    after = deployments(workdir, runner, project)
    if not after or (len(after) > 1 and base and after[1]['id'] != base['id']) or (base and after[0]['id'] == base['id']):
        raise ValueError('Another deployment interleaved with this one; inspect the project before rerunning')
    shutil.rmtree(site)
    return after[0], out[-500:]


# --- verify --------------------------------------------------------------------------------------------------

PAGES = ('index.html', '{run}/results.html', '{run}/conference-view/index.html', '{run}/inspect-view/index.html')
LOG_DOWNLOAD_LINK = re.compile(rb'href="[^"]*\binspect/[^"/]*\.eval"')
SAME_SITE = re.compile(r'''\b(?:href|src)=["']([^"'#?]*)''', re.I)


def verify(base, runs=None, full=False, getter=fetch, head_getter=None):
    """Read-only: the served site against its own manifest. Checks: manifest served; every checked file served
    with HTTP 200 and its exact recorded bytes (pages as text/html); no private category recorded or served (a
    probe of each run's Inspect logs gets no log); every Inspect View carries the notice shim and no HF sign-in
    shim; no page links a raw .eval download; every same-site link of a checked page names a recorded file;
    unknown paths answer 404 (no single-page fallback); file count and sizes within Cloudflare's limits."""
    checks = []

    def check(name, ok, detail=''):
        checks.append({'check': name, 'ok': bool(ok), **({'detail': detail} if detail else {})})

    status, _, data = getter(file_url(base, SITE_MANIFEST))
    check('site_manifest', status == 200, f'HTTP {status}')
    if status != 200:
        return {'url': base, 'ok': False, 'checks': checks}
    manifest = json.loads(data)
    recorded = manifest['files']
    check('limits', len(recorded) + 1 <= MAX_FILES and all(x['bytes'] <= MAX_FILE_BYTES for x in recorded.values()),
          f'{len(recorded) + 1} files')
    leaked = [n for n in recorded if private_category(n) is not None or n in HF_ONLY or n.endswith('.eval')]
    check('no_private_files', not leaked, ', '.join(leaked[:5]))
    runs = list(runs or manifest['runs'])
    if full:
        names = sorted(recorded)
    else:
        names = sorted({p.format(run=r) for r in runs for p in PAGES if p.format(run=r) in recorded}
                       | {n for n in recorded if '/' not in n}
                       | {n for n in recorded for r in runs
                          if n.startswith(r + '/') and n.endswith(('.html', 'results.json', 'listing.json'))})
    bad, notice, links = [], [], []
    for name in names:
        status, headers, served = getter(file_url(base, name))
        record = recorded[name]
        if status != 200 or digest(served) != {'bytes': record['bytes'], 'sha256': record['sha256']}:
            bad.append(f'{name} (HTTP {status})')
            continue
        content_type = {k.lower(): v for k, v in headers.items()}.get('content-type', '')
        if name.endswith('.html'):
            if 'text/html' not in content_type or 'attachment' in str(headers.get('Content-Disposition', '')):
                bad.append(f'{name} ({content_type})')
            if LOG_DOWNLOAD_LINK.search(served):
                links.append(name + ' links a raw .eval')
            text = served.decode('utf-8', 'replace')
            if is_inspect_page(name) and (SHIM_MARKER not in text or 'id="evallog-fetch"' in text):
                notice.append(name)
            folder = PurePosixPath(name).parent
            for url in SAME_SITE.findall(text):
                if not url or re.match(r'[a-z][a-z0-9+.-]*:|//', url, re.I) or url.startswith(('/', '${', "'")):
                    continue
                target = dataset_logs._normalise(folder / urllib.parse.unquote(url))
                if target is None:
                    links.append(f'{name} -> {url}')
                    continue
                if target not in recorded and target + '/index.html' not in recorded and target != SITE_MANIFEST \
                        and not (target == '' or url.endswith('/') and (target + '/index.html') in recorded):
                    links.append(f'{name} -> {url}')
    check('served_bytes', not bad, '; '.join(bad[:5]))
    check('inspect_notice', not notice, ', '.join(notice[:5]))
    check('no_broken_links', not links, '; '.join(links[:5]))
    probes = []
    for run in runs:
        listing = recorded.get(f'{run}/inspect-view/logs/listing.json')
        if not listing:
            continue
        status, _, data = getter(file_url(base, f'{run}/inspect-view/logs/listing.json'))
        try:
            names_ = [k for k in json.loads(data)] if status == 200 else []
        except ValueError:
            names_ = []
        for log in names_[:1]:
            log_status, _, body = getter(file_url(base, f'{run}/inspect-view/logs/{log}'))
            if log_status == 200 and body[:2] == b'PK':
                probes.append(f'{run}/{log}')
    check('no_private_logs_served', not probes, ', '.join(probes[:5]))
    status, _, _ = getter(file_url(base, 'no-such-page-' + sha256(base.encode())[:8] + '.html'))
    check('not_found_404', status == 404, f'HTTP {status}')
    return {'url': base, 'ok': all(c['ok'] for c in checks), 'checked_files': len(names),
            'site_files': len(recorded) + 1, 'runs': len(runs), 'checks': checks}


# --- CLI -----------------------------------------------------------------------------------------------------

LFS_POINTER = b'version https://git-lfs.github.com/spec/'


def git(args, cwd):
    result = subprocess.run(['git', '-c', 'credential.helper=', '-c', 'filter.lfs.smudge=cat', '-c',
                             'filter.lfs.process=', '-c', 'filter.lfs.required=false', *args], cwd=cwd,
                            capture_output=True, text=True, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                                                                  'GIT_LFS_SKIP_SMUDGE': '1'})
    if result.returncode:
        raise RuntimeError('git ' + args[0] + ' failed: ' + result.stderr[-1000:])
    return result.stdout.strip()


def download_space(revision, directory):
    """An anonymous shallow Git checkout of the public Space at its pinned revision (one pack instead of one
    request per file: HF rate-limits anonymous file downloads). LFS files that are deployable are fetched one by
    one and checked against their pointer's SHA-256; private ones (the only LFS file is a live Inspect log) are
    never fetched."""
    from huggingface_hub import HfApi, hf_hub_download
    api = HfApi(token=False)  # the Space is public: anonymous, no credential is read
    info = hf_visibility.check(api.space_info(hf_visibility.SPACE_REPO, revision=revision, token=False),
                               hf_visibility.SPACE_REPO, 'space')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    git(['init', '-q'], directory)
    git(['fetch', '-q', '--depth', '1', f'https://huggingface.co/spaces/{hf_visibility.SPACE_REPO}', info.sha],
        directory)
    git(['checkout', '-q', '-f', 'FETCH_HEAD'], directory)
    if git(['rev-parse', 'HEAD'], directory) != info.sha:
        raise ValueError('Checked-out Space revision differs from the pinned one')
    for path in directory.rglob('*'):
        relative = path.relative_to(directory)
        if relative.parts[0] == '.git' or not path.is_file() or path.stat().st_size > 1024:
            continue
        pointer = path.read_bytes()
        if not pointer.startswith(LFS_POINTER) or private_category(relative.as_posix()) is not None:
            continue
        oid = re.search(rb'oid sha256:([0-9a-f]{64})', pointer).group(1).decode()
        data = Path(hf_hub_download(hf_visibility.SPACE_REPO, relative.as_posix(), repo_type='space',
                                    revision=info.sha, token=False)).read_bytes()
        if sha256(data) != oid:
            raise ValueError('LFS file differs from its pointer: ' + relative.as_posix())
        path.write_bytes(data)
    return info.sha, directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    mirror = sub.add_parser('mirror-space', help='mirror the public HF Space at a pinned revision')
    mirror.add_argument('--revision', help='Space revision (default: current head, pinned at planning)')
    mirror.add_argument('--snapshot', type=Path, help='an existing local snapshot of exactly --revision')
    bundle = sub.add_parser('publish', help='merge a prepare_hf_library.py bundle')
    bundle.add_argument('--bundle', required=True, type=Path)
    bundle.add_argument('--dataset-commit', help='HF logs-dataset commit holding the bundle\'s private files')
    bundle.add_argument('--replace-pages', action='store_true', help='allow the bundle runs\' .html pages to change')
    restyle = sub.add_parser('restyle', help='re-render the layout of published run pages and the catalogue')
    restyle.add_argument('--run', action='append', help='only this run\'s pages (repeatable; default all runs)')
    for command in (mirror, bundle, restyle):
        command.add_argument('--commit', action='store_true', help='deploy; otherwise only plan')
        command.add_argument('--receipt', type=Path, help='receipt JSON (outside the bundle)')
        command.add_argument('--workdir', type=Path, help='scratch directory for staging (default: a temporary one)')
    check = sub.add_parser('verify', help='read-only check of the served site')
    check.add_argument('--url', default=SITE_URL)
    check.add_argument('--run', action='append')
    check.add_argument('--full', action='store_true', help='check every file, not only pages and records')
    check.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'verify':
        result = verify(args.url, args.run, args.full)
        text = json.dumps(result, indent=2)
        if args.output:
            args.output.write_text(text)
        print(text)
        return 0 if result['ok'] else 1
    if args.command == 'publish' and args.dataset_commit and not re.fullmatch(r'[0-9a-f]{40}', args.dataset_commit):
        parser.error('--dataset-commit must be a full commit')
    if args.receipt and args.command == 'publish' and args.receipt.resolve().is_relative_to(args.bundle.resolve()):
        parser.error('--receipt must be outside the prepared bundle')
    with tempfile.TemporaryDirectory(prefix='cf-publish-') as tmp:
        workdir = args.workdir or Path(tmp)
        workdir.mkdir(parents=True, exist_ok=True)
        current = deployments(workdir)
        base = current[0] if current else None
        base_manifest, base_files = load_site(base['url'] if base else None)
        unlisted = unlisted_runs()
        if args.command == 'mirror-space':
            if args.snapshot:
                if not args.revision:
                    parser.error('--snapshot needs --revision')
                revision, snapshot = args.revision, args.snapshot
            else:
                revision, snapshot = download_space(args.revision, workdir / 'space')
            space, private = space_files(snapshot)
            files, plan = plan_mirror(base_files, space, revision, unlisted)
            plan['private_files_not_deployed'] = private
            message = f'Mirror HF Space {revision[:12]}'
        elif args.command == 'restyle':
            files, plan = plan_restyle(base_files, unlisted, args.run)
            message = f'Restyle {len(plan["replaced_pages"])} page(s) and the catalogue'
        else:
            files, plan = plan_bundle(base_files, args.bundle, unlisted, args.dataset_commit, args.replace_pages)
            message = 'Publish ' + ', '.join(plan['source']['runs'])
        files, manifest = finish(files, base_manifest, plan)
        result = {**summary(files, base_files, plan, base), 'committed': False}
        if args.commit:
            remember(files)
            deployment, tail = deploy(files, base, message, workdir)
            result.update(committed=True, deployment=deployment, site=SITE_URL)
            result['verify'] = verify(deployment['url'] or SITE_URL)
        text = json.dumps(result, indent=2)
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(text)
        print(text)
        return 0 if not args.commit or result['verify']['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
