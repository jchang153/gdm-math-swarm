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
    publish_cloudflare.py repack [--commit] [--receipt FILE]
        Pack every run's folders of many files (``PACK_GROUPS``: per-agent instructions and prompts, the proof
        checker's sources, story map agent pages, behavior judge per-agent files) into RUN/packed/ (pack files with
        each file's exact bytes and SHA-256, an index and a viewer page); the old URLs keep working
        (RUN/<group>/404.html: the viewer, which renders a packed page in place). Every deployment packs every run
        the same way (plans work on the unpacked site); unpacking is checked to reproduce every original byte for
        byte.
    publish_cloudflare.py withdraw --run RUN [--run RUN ...] [--commit] [--receipt FILE]
        Remove runs already recorded in data/library-unlisted.json from the site (every file of each run; the
        catalogue is re-rendered without them). Other runs never change; private copies are untouched.
    publish_cloudflare.py catalogue [--commit] [--receipt FILE]
        Re-render only the catalogue (index.html, experiments.json) from its entries; each entry's "Story map" link
        follows whether RUN/analysis/storymap/index.html is on the site. No run file changes.
    publish_cloudflare.py correct --run RUN --file PATH=LOCAL [--file ...] [--entry ENTRY_JSON] [--commit]
        Replace named records of a run already on the site with corrected bytes, only after an owner decision
        recorded in docs/plan.md (first use: 2ov r1's complete-evaluation records, November 4, 2026). Each PATH
        (relative to the run) must already be served, must not be a page (.html), a pack file or a private
        category; --entry replaces the run's catalogue entry (same run, links inside the run) and re-renders the
        catalogue. Every replacement is recorded with old and new digests. Nothing else changes.
    publish_cloudflare.py storymap --run RUN=PAGES_DIRECTORY [--run ...] [--commit] [--receipt FILE]
        Add (or refresh) the agent story map of runs already on the site (owner, November 2, 2026): only the
        allowlisted pages of PAGES_DIRECTORY (``storymap_site``: index.html, events.html, validation.html/.md,
        agents/*.html, rendered with --taxonomy '') go to RUN/analysis/storymap/, screened for host paths, private
        paths, taxonomy names and unpublished links; results.html gains the Story map tab. No other file changes.
    publish_cloudflare.py heatmap --run RUN=FOLDER [--run ...] [--commit] [--receipt FILE]
        Add (or refresh) the "Faking over time" heatmap of runs already on the site (owner, November 3, 2026):
        FOLDER holds scripts/faking_heatmap.py's faking-heatmap.json (and faking-heatmap.png when the run has
        fakers); RUN/faking-heatmap.png is added and RUN/results.html gains the section at the top of its main
        report. No other file changes.
    publish_cloudflare.py judge-labels [--run RUN ...] [--commit] [--receipt FILE]
        Rewrite the simplified behavior judge's level wording in place on runs already on the site (owner, November
        3, 2026): "submitted" reads "submitted, never credited" and the counts say each agent is counted once, at
        its highest level. RUN/results.html (Behavior judge tile, Run table row and line; counts from the served
        RUN/analysis/simple_judge/report.json) and RUN/analysis/simple_judge/report.html change, only in those
        words; stored labels, report.json and every other file are unchanged.
    publish_cloudflare.py verify [--url URL] [--full] [--run RUN ...]
        Read-only check of the served site against its site-manifest.json (see ``verify``).

Every subcommand takes ``--site {gdm,ib}`` (``SITES``; default gdm, the math library, unchanged). Owner decision,
November 3, 2026: the ImpossibleBench coding runs go to their own Pages project (``ib``), built by the same code in
the same style (catalogue texts from catalogue_intro_ib.py); a run's domain (RUN/settings.json ``effective.domain``,
else ``config.domain``, absent = "lean") must match the site, so a coding run is never deployed to the math site
nor a math run to the ImpossibleBench site. ``mirror-space`` (the HF Space is the math library) is math-only.

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
import base64
import datetime
import importlib
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
from build_experiment_index import (STORYMAP_LABEL, STORYMAP_TARGET, unlisted_runs,  # noqa: E402
                                    with_storymap_link, write_index)
import dataset_logs  # noqa: E402
import hf_visibility  # noqa: E402
import site_layout  # noqa: E402
import site_style  # noqa: E402
import simple_judge_site  # noqa: E402
import storymap_site  # noqa: E402
from publish_hf_library import (SECRET, load_bundle, merge_entries, packed_text,  # noqa: E402
                                safe_path)
from typing import NamedTuple  # noqa: E402

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


class Site(NamedTuple):
    """One public Cloudflare Pages library: its project, the run domain it accepts, the module with its catalogue
    texts (catalogue_intro.py style), where its receipts and its content-addressed file cache live."""
    key: str
    project: str
    domain: str
    intro: str
    receipts: Path
    cache: Path
    mirror_space: bool = False

    @property
    def url(self):
        return f'https://{self.project}.pages.dev'


# Owner decision, November 3, 2026: the ImpossibleBench (coding) runs get their own site, never the math one.
SITES = {
    'gdm': Site('gdm', PROJECT, 'lean', 'catalogue_intro', ROOT / 'runs' / 'phase-b' / 'cloudflare' / 'receipts',
                CACHE, mirror_space=True),
    'ib': Site('ib', 'gdm-impossiblebench-experiments', 'code', 'catalogue_intro_ib',
               ROOT / 'runs' / 'phase-b' / 'cloudflare-ib' / 'receipts', ROOT / 'runs' / 'phase-b' / 'cloudflare-ib' / 'cache'),
}
DEFAULT_SITE = 'gdm'
DOMAIN_KEY = 'domain'
LEAN = 'lean'


def site_of(site):
    """The ``Site`` for a key, a Site or None (the math library)."""
    if site is None:
        return SITES[DEFAULT_SITE]
    return site if isinstance(site, Site) else SITES[site]


def settings_domain(data):
    """A run's domain from its settings.json bytes (research_swarm.coding.domain_of_run): ``effective.domain``,
    else ``config.domain``; "lean" when absent or unreadable (every run before the coding version)."""
    try:
        settings = json.loads(data)
    except (TypeError, ValueError):
        return LEAN
    if not isinstance(settings, dict):
        return LEAN
    effective = settings.get('effective') if isinstance(settings.get('effective'), dict) else {}
    config = settings.get('config') if isinstance(settings.get('config'), dict) else {}
    return effective.get(DOMAIN_KEY) or config.get(DOMAIN_KEY) or LEAN


def check_domains(files, site=None):
    """Refuse a site holding a run of another domain (a coding run on the math site, a math run on the
    ImpossibleBench site). Returns {run: domain}."""
    site = site_of(site)
    runs = sorted({run_of(n) for n in files if run_of(n) and run_of(n) != 'live'})
    domains = {run: settings_domain(files.get(f'{run}/settings.json')) for run in runs}
    wrong = sorted(run for run, domain in domains.items() if domain != site.domain)
    if wrong:
        raise ValueError(f'Run(s) of another domain refused on the {site.key} site ({site.project}, domain '
                         f'{site.domain!r}): ' + ', '.join(f'{r} ({domains[r]!r})' for r in wrong))
    return domains
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


# --- run file packs (owner, October 30, 2026; extended November 3, 2026: Pages' 20,000-file limit) ----------
# Folders of many small files per run are deployed as a few pack files per run, RUN/packed/<group>-NN.json (the
# group's folder with "/" read as "-"), with every file's exact bytes (UTF-8 text, else base64) and SHA-256, one
# index RUN/packed/index.json and a viewer page RUN/packed/index.html. October 30, 2026: the per-agent instructions
# (instructions/, instructions/received/) and prompts (prompts/). November 3, 2026 (owner decision, docs/plan.md
# "Pack more files per run (November 3, 2026)"): also the proof checker's submitted sources (evaluations/sources/),
# the story map agent pages (analysis/storymap/agents/) and the behavior judge's per-agent files
# (analysis/judge/agents/, analysis/judge/packages/, analysis/judge/review/agents/).
# Old URLs keep working: RUN/<group>/404.html is a copy of the viewer, which Pages serves (HTTP 404, the URL
# unchanged) for any missing file below that folder; the viewer reads the path and shows that file, and an HTML
# page (a story map agent page, a judge review page) is rendered in place as the page itself, so its relative
# links resolve as before. (Pages' _redirects cannot substitute a placeholder into a query or a 200 rewrite,
# tested October 30, 2026, so no _redirects is used.)
# The site's state is packed; every plan works on the unpacked tree (``unpack_site`` after loading) and
# ``finish`` packs it again. Nothing is dropped: unpack(pack) reproduces every original file byte for byte
# (checked on every deployment), and the credential screen reads every pack entry decoded.
PACK_GROUPS = ('instructions', 'prompts', 'evaluations/sources', 'analysis/storymap/agents',
               'analysis/judge/agents', 'analysis/judge/packages', 'analysis/judge/review/agents')
PACKED_DIRS = PACK_GROUPS  # earlier name
PAGE_GROUPS = frozenset({'analysis/storymap/agents', 'analysis/judge/review/agents'})
PACK_FOLDER = 'packed'
PACK_SCHEMA = 'gdm-site-pack/1'
PACK_PART_BYTES = 20 * 1024 * 1024
PAGE_PART_BYTES = 6 * 1024 * 1024  # page groups: smaller parts, so one page loads quickly
VIEWER_404 = '404.html'


def pack_slug(group):
    return group.replace('/', '-')


PACK_NAME = re.compile(r'[^/]+/packed/(?:' + '|'.join(re.escape(pack_slug(g)) for g in PACK_GROUPS)
                       + r')-\d{2}\.json')


def group_of(name):
    """The pack group of a site path (RUN/<group>/...), or None. The group's own 404.html is the viewer."""
    parts = name.split('/')
    if len(parts) < 3 or parts[0] == 'live':
        return None
    relative = '/'.join(parts[1:])
    for group in sorted(PACK_GROUPS, key=len, reverse=True):
        if relative.startswith(group + '/'):
            return None if relative == f'{group}/{VIEWER_404}' else group
    return None


def packable(name):
    return group_of(name) is not None


def viewer_names(run, groups=PACK_GROUPS):
    return [f'{run}/{PACK_FOLDER}/index.html'] + [f'{run}/{group}/{VIEWER_404}' for group in groups]


def is_pack_file(name):
    return bool(PACK_NAME.fullmatch(name))


def _entry(data):
    try:
        return {'encoding': 'utf-8', 'content': data.decode('utf-8')}
    except UnicodeDecodeError:
        return {'encoding': 'base64', 'content': base64.b64encode(data).decode('ascii')}


def _entry_bytes(entry):
    return entry['content'].encode('utf-8') if entry['encoding'] == 'utf-8' else base64.b64decode(entry['content'])


def pack_run(run, originals):
    """{name: bytes} of a run's pack files, index and viewers for ``originals`` ({RUN/<group>/...: bytes}).
    Instructions and prompts keep their viewer even when empty (as before November 3, 2026); other groups get
    one only when the run has files there."""
    out, index = {}, {}
    present = {group_of(n) for n in originals}
    groups = [g for g in PACK_GROUPS if g in present or g in ('instructions', 'prompts')]
    for group in PACK_GROUPS:
        names = sorted(n for n in originals if group_of(n) == group)
        limit = PAGE_PART_BYTES if group in PAGE_GROUPS else PACK_PART_BYTES
        parts, current, size = [], {}, 0
        for name in names:
            relative = name[len(run) + 1:]
            entry = {**digest(originals[name]), **_entry(originals[name])}
            weight = len(entry['content'].encode('utf-8')) + 200
            if current and size + weight > limit:
                parts.append(current)
                current, size = {}, 0
            current[relative] = entry
            size += weight
        if current:
            parts.append(current)
        for number, files in enumerate(parts, 1):
            pack = f'{pack_slug(group)}-{number:02d}.json'
            out[f'{run}/{PACK_FOLDER}/{pack}'] = json.dumps(
                {'schema': PACK_SCHEMA, 'run': run, 'folder': group, 'part': number, 'files': files},
                ensure_ascii=False, indent=0, sort_keys=True).encode('utf-8')
            for relative, entry in files.items():
                index[relative] = {'pack': pack, 'bytes': entry['bytes'], 'sha256': entry['sha256']}
    out[f'{run}/{PACK_FOLDER}/index.json'] = json.dumps(
        {'schema': PACK_SCHEMA, 'run': run, 'files': dict(sorted(index.items()))}, indent=1).encode()
    viewer = pack_viewer(run)
    for name in viewer_names(run, groups):
        out[name] = viewer
    return out


def unpack_run(run, files):
    """{RUN/<group>/...: bytes} restored from a run's pack files (checks every SHA-256)."""
    restored = {}
    for name, data in files.items():
        if name.startswith(run + '/') and is_pack_file(name):
            pack = json.loads(data)
            for relative, entry in pack['files'].items():
                raw = _entry_bytes(entry)
                if digest(raw) != {'bytes': entry['bytes'], 'sha256': entry['sha256']}:
                    raise ValueError(f'Pack entry differs from its digest: {name}: {relative}')
                restored[f'{run}/{relative}'] = raw
    return restored


def packed_run_text(name, data):
    """Every entry of a run file pack, decoded (the credential screen reads the exact original bytes)."""
    if not is_pack_file(name):
        return b''
    pack = json.loads(data)
    return b'\n'.join(_entry_bytes(e) for e in pack['files'].values())


def unpack_site(files):
    """The site with every run's packs replaced by the original files (pack files, index and viewers removed).
    Every index entry must be restored with its digest; an original also present unpacked must be identical."""
    files = dict(files)
    runs = sorted(n.split('/')[0] for n in files if n.count('/') == 2 and n.endswith(f'/{PACK_FOLDER}/index.json'))
    for run in runs:
        restored = unpack_run(run, files)
        index = json.loads(files[f'{run}/{PACK_FOLDER}/index.json'])['files']
        if {f'{run}/{r}': {'bytes': x['bytes'], 'sha256': x['sha256']} for r, x in index.items()} != \
                {n: digest(d) for n, d in restored.items()}:
            raise ValueError(f'{run}: packed index and pack files disagree')
        for name in [n for n in files if n.startswith(f'{run}/{PACK_FOLDER}/') and
                     (is_pack_file(n) or n.endswith(('/index.json', '/index.html')))] + viewer_names(run):
            files.pop(name, None)
        for name, data in restored.items():
            if name in files and files[name] != data:
                raise ValueError(f'{name}: differs from the packed copy already on the site')
            files[name] = data
    return files


def pack_site(files):
    """Pack every run's pack groups (packs already present are unpacked first). Returns (files, record);
    refuses any loss of content."""
    files = unpack_site(files)
    runs = sorted({run_of(n) for n in files if packable(n)})
    record = {}
    for run in runs:
        originals = {n: files.pop(n) for n in sorted(files) if n.startswith(run + '/') and packable(n)}
        packed = pack_run(run, originals)
        clash = sorted(n for n in packed if n in files)
        if clash:
            raise ValueError('Pack output would overwrite a site file: ' + clash[0])
        if unpack_run(run, packed) != originals:
            raise ValueError('Packing would lose content: ' + run)
        files.update(packed)
        record[run] = {'packed_files': len(originals), 'pack_files': len(packed),
                       'by_group': {g: n for g in PACK_GROUPS
                                    if (n := sum(1 for x in originals if group_of(x) == g))}}
    return files, record


def pack_viewer(run):
    from site_style import themed
    script = r"""
// The site may be served under a path prefix (a proxy such as /swarms/gdm-math-swarm/): keep it.
const run = document.body.dataset.run, at = location.pathname.indexOf('/' + run + '/');
const base = (at > 0 ? location.pathname.slice(0, at) : '') + '/' + run + '/';
const list = document.getElementById('list'), view = document.getElementById('view');
let wanted = '';
if (location.pathname.startsWith(base)) {
  const rest = decodeURIComponent(location.pathname.slice(base.length));
  if (rest && !rest.startsWith('packed/')) wanted = rest;
}
const packs = {};
function pack(name) {
  if (!packs[name]) packs[name] = fetch(base + 'packed/' + name).then(r => r.json());
  return packs[name];
}
function bytes(e) {
  if (e.encoding === 'utf-8') return new TextEncoder().encode(e.content);
  return Uint8Array.from(atob(e.content), c => c.charCodeAt(0));
}
function resolve(index, path) {
  if (!path) return '';
  for (const p of [path, path + '.html', path.replace(/\/$/, '') + '/index.html']) if (index.files[p]) return p;
  return '';
}
async function show(path, index) {
  const meta = index.files[path];
  const e = (await pack(meta.pack)).files[path];
  if (/\.html?$/i.test(path)) {  // a packed page: rendered in place, at its own URL
    const text = e.encoding === 'utf-8' ? e.content : new TextDecoder().decode(bytes(e));
    document.open(); document.write(text); document.close();
    return 'page';
  }
  document.getElementById('viewer').hidden = false;
  document.getElementById('name').textContent = path + ' (' + e.bytes + ' bytes, SHA-256 ' + e.sha256 + ')';
  const pre = document.createElement('pre');
  let text = e.encoding === 'utf-8' ? e.content : '[binary file: use the download link]';
  if (path.endsWith('.json')) { try { text = JSON.stringify(JSON.parse(e.content), null, 2); } catch (_) {} }
  pre.textContent = text;
  const a = document.createElement('a');
  a.textContent = 'Download the original file (exact bytes)';
  a.download = path.split('/').pop();
  a.href = URL.createObjectURL(new Blob([bytes(e)]));
  view.replaceChildren(a, pre);
  document.title = path + ' - ' + run;
}
fetch(base + 'packed/index.json').then(r => r.json()).then(async index => {
  const path = resolve(index, wanted);
  if (path && await show(path, index) === 'page') return;
  if (!path) {
    document.getElementById('viewer').hidden = false;
    document.getElementById('name').textContent = wanted ? 'Not found' : 'Run files';
    if (wanted) view.textContent = 'No such file in this run: ' + wanted;
  }
  const folder = wanted.includes('/') ? wanted.slice(0, wanted.lastIndexOf('/') + 1) : '';
  for (const p of Object.keys(index.files)) {
    if (folder && !p.startsWith(folder)) continue;
    const li = document.createElement('li'), a = document.createElement('a');
    a.textContent = p;
    a.setAttribute('href', base + p.split('/').map(encodeURIComponent).join('/'));
    li.append(a); list.append(li);
  }
  if (!path) document.getElementById('all').open = true;
  document.getElementById('wait').hidden = true;
});
"""
    safe = html.escape(run, quote=True)
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Run files</title>'
            '<style>body{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;'
            'background:var(--bg);color:var(--fg)}a{color:var(--link)}pre{white-space:pre-wrap;overflow-wrap:anywhere;'
            'border:1px solid var(--line, #8884);padding:12px;border-radius:6px}'
            'h1{font-size:20px;overflow-wrap:anywhere}details{margin:12px 0}ul{columns:2;font-size:13px}</style></head>'
            f'<body data-run="{safe}"><p id="wait">Loading...</p><div id="viewer" hidden>'
            f'<p><a href="/{safe}/results.html">Back to the run</a></p>'
            '<h1 id="name"></h1><div id="view"></div>'
            f'<p>{safe}: files kept in a few pack files (agent instructions and prompts, submitted proof sources, '
            'story map agent pages, behavior judge files; exact bytes and SHA-256 of each original file). The old '
            'file links open the file here; pages open as themselves.</p>'
            '<details id="all"><summary>All files</summary>'
            f'<ul id="list"></ul></details></div><script>{script}</script></body></html>')
    return themed(body).encode('utf-8')

def site_gate(name, data):
    """The last screen of every deployed file: path rules, host-only and credential file names, Pages special
    files, the credential screen (packed captured files included), no private category, Cloudflare's per-file
    limit. Raises ValueError; returns the bytes."""
    path = safe_path(name)  # no dot-folders, no traversal, no credential file names
    if path.name.lower() in {'.env', 'host.env'} or path.parts[0] == 'functions' or path.name in PAGES_SPECIAL:
        raise ValueError('Pages special or host-only file refused: ' + name)
    if SECRET.search(data) or SECRET.search(packed_text(name, data)) or SECRET.search(packed_run_text(name, data)):
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


LINK_ATTR = re.compile(r'''(\b(?:href|src)=)(["'])([^"'#?]*)([^"']*)\2''', re.I)


def link_target(page, url):
    """The site path a same-site relative link of ``page`` names; '' for links not checked (absolute, external,
    templated, empty); None for a link that leaves the site root."""
    if not url or re.match(r'[a-z][a-z0-9+.-]*:|//', url, re.I) or url.startswith(('/', '${', "'")):
        return ''
    return dataset_logs._normalise(PurePosixPath(page).parent / urllib.parse.unquote(url))


def link_ok(target, url, names):
    """True when a link target is served (``names``: every file of the site, packed files unpacked)."""
    return (target == '' or target in names or target + '/index.html' in names or target == SITE_MANIFEST
            or target + '.html' in names)


def broken_links(files, pages=None):
    """[(page, url, target)] of same-site links that name no file of ``files`` (an unpacked site), for every
    .html page (or ``pages``)."""
    names = set(files) | GENERATED
    out = []
    for page in sorted(pages if pages is not None else (n for n in files if n.endswith('.html'))):
        text = files[page].decode('utf-8', 'replace')
        for match in LINK_ATTR.finditer(text):
            url = match.group(3)
            target = link_target(page, url)
            if target is None or not link_ok(target, url, names):
                out.append((page, url + match.group(4), target))
    return out


def repoint_private_links(files, pages=None):
    """Links of pages to private files the site never serves (``private_category``: Inspect logs, raw JSONL,
    timelines, databases...) repointed to the site's private-records notice, as for an unpinned bundle (owner,
    November 3, 2026: a run page linked analysis/timeline.md, a file that was never produced). Pages that link to
    the logs dataset keep those links. Returns (files, [{page, url, target}]); other broken links are left alone
    (``broken_links``)."""
    files = dict(files)
    names = set(files) | GENERATED
    changed = []
    for page in sorted(pages if pages is not None else (n for n in files if n.endswith('.html'))):
        text = files[page].decode('utf-8', 'replace')

        def swap(match):
            url = match.group(3)
            target = link_target(page, url)
            if not target or link_ok(target, url, names) or private_category(target) is None:
                return match.group(0)
            changed.append({'page': page, 'url': url, 'target': target})
            notice = '../' * page.count('/') + PRIVATE_PAGE
            return f'{match.group(1)}{match.group(2)}{notice}{match.group(2)}'
        new = LINK_ATTR.sub(swap, text)
        if new != text:
            files[page] = new.encode('utf-8')
    return files, changed


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


def render_catalogue(entries, unlisted, files=None, site=None):
    """The catalogue pages; with ``files`` (the site after the change), each entry's Story map link follows
    whether RUN/analysis/storymap/index.html is on the site. ``site`` picks the catalogue texts."""
    site = site_of(site)
    entries = [x for x in entries if x['run'] not in unlisted]
    if files is not None:
        entries = [{**e, 'links': with_storymap_link(e['links'], e['run'], f'{e["run"]}/{STORYMAP_TARGET}' in files)}
                   if (STORYMAP_LABEL in e['links']) != (f'{e["run"]}/{STORYMAP_TARGET}' in files) else e
                   for e in entries]
    with tempfile.TemporaryDirectory() as tmp:
        if site.key == DEFAULT_SITE:
            write_index(tmp, entries, hosted=True)
        else:
            write_index(tmp, entries, hosted=True, intro=importlib.import_module(site.intro))
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


def plan_mirror(base_files, space, revision, unlisted, site=None):
    """The site after mirroring the Space: Space files win; runs only on the site are preserved and stay listed.
    The HF Space is the math library: refused for any other site."""
    if not site_of(site).mirror_space:
        raise ValueError(f'mirror-space copies the math library\'s HF Space; not for the {site_of(site).key} site')
    space_runs = {run_of(n) for n in space if run_of(n)}
    kept = {n: d for n, d in base_files.items() if run_of(n) and run_of(n) not in space_runs}
    files = {**kept, **space}
    preserved = sorted({run_of(n) for n in kept})
    if preserved:  # another collaborator's runs published only on the site: keep their catalogue entries
        site_only = [e for e in catalogue(base_files) if e['run'] in preserved]
        files.update(render_catalogue(merge_entries(catalogue(space), site_only), unlisted, files, site=site))
    changed = sorted(n for n in space if run_of(n) and n in base_files and base_files[n] != space[n])
    removed = sorted(n for n in base_files if n not in files and n not in GENERATED)  # run files the Space removed
    return files, {'source': {'kind': 'hf-space', 'repo': hf_visibility.SPACE_REPO, 'revision': revision},
                   'preserved_site_only_runs': preserved, 'changed_files': changed, 'removed_files': removed}


def dataset_checker(commit, api=None):
    """``remote(name, bytes, sha256)`` for load_bundle: True when the private logs dataset holds exactly those
    bytes at ``name`` at ``commit`` (LFS SHA-256, or a download hashed and deleted for a small regular file).
    Used for bundles fetched from a VM without their dataset files (scripts/fetch_run.py)."""
    import offload_runs
    trees = {}

    def check(name, size, sha256):
        from huggingface_hub import HfApi
        nonlocal api
        api = api or HfApi()
        run = name.split('/')[0]
        if run not in trees:
            hf_visibility.check_dataset(api, hf_visibility.DATASET_REPO)
            trees[run] = offload_runs.remote_tree(api, hf_visibility.DATASET_REPO, commit, run)
        entry = trees[run].get(name)
        return entry is not None and offload_runs.verified(api, hf_visibility.DATASET_REPO, commit, name, entry,
                                                           sha256, size)
    return check


def plan_bundle(base_files, bundle, unlisted, dataset_commit=None, replace_pages=False, remote=None, site=None):
    """The site after merging a prepared bundle (immutable existing run files unless --replace-pages). A bundle
    whose dataset files are already in the logs dataset (left on the VM) needs ``dataset_commit``; ``remote``
    defaults to dataset_checker(dataset_commit)."""
    if remote is None and dataset_commit:
        remote = dataset_checker(dataset_commit)
    manifest, incoming = load_bundle(bundle, remote)
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
    unstaged = sorted(n for n in base_files if run_of(n) in selected and n not in manifest['files']
                      and not ((n.startswith(f'{run_of(n)}/{PACK_FOLDER}/') or n in viewer_names(run_of(n)))
                               and any(packable(m) and run_of(m) == run_of(n) for m in manifest['files'])))
    if unstaged:
        raise ValueError('Site run contains an unstaged file: ' + unstaged[0])
    for run in sorted(selected):  # a run already packed on the site: its per-agent files are immutable too
        old = {n: d for n, d in base_files.items() if is_pack_file(n) or n == f'{run}/{PACK_FOLDER}/index.json'
               if n.startswith(f'{run}/{PACK_FOLDER}/')}
        originals = {n: d for n, d in files.items() if n.startswith(run + '/') and packable(n)}
        if old and originals:
            new = {n: d for n, d in pack_run(run, originals).items() if not n.endswith('.html')}
            if new != old:
                raise ValueError('Immutable site run file differs: ' + run + '/' + PACK_FOLDER)
    pages = [n for n in files if run_of(n) in selected and n.endswith('.html')]
    files, repointed = repoint_private_links(files, pages)
    broken = broken_links(files, pages)
    if broken:  # a page of the bundle links a file the site would not serve
        raise ValueError(f'{len(broken)} broken link(s) in the bundle\'s pages, e.g. '
                         + '; '.join(f'{p} -> {u}' for p, u, _ in broken[:5]))
    entries = merge_entries(catalogue(base_files), incoming)
    check_domains({n: d for n, d in files.items() if run_of(n) in selected}, site)
    files.update(render_catalogue(entries, unlisted, files, site=site))
    if 'archive-manifest.json' in base_files:  # the HF-origin run record; new runs are recorded in site-manifest
        files['archive-manifest.json'] = base_files['archive-manifest.json']
    return files, {'source': {'kind': 'bundle', 'runs': sorted(selected),
                              **({'dataset_commit': dataset_commit} if dataset_commit else {})},
                   'added_files': len(added), 'replaced_pages': replaced, 'repointed_private_links': repointed,
                   'private_files_not_deployed': {'files': len(private),
                                                  'bytes': sum(x['bytes'] for x in private.values())}}


def plan_restyle(base_files, unlisted, runs=None, site=None):
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
    files.update(render_catalogue(catalogue(base_files), unlisted, files, site=site))
    return files, {'source': {'kind': 'restyle', 'runs': sorted(runs) if runs else 'all'},
                   'replaced_pages': replaced, 'unthemed_pages_left_as_served': legacy}


def plan_withdraw(base_files, unlisted, runs, site=None):
    """The site without the given runs: every file of each run is removed and the catalogue re-rendered.

    Owner-requested take-down (for example a failed run). Each run must already be recorded in the tracked
    data/library-unlisted.json (run, date, reason), so its catalogue entry stays out and later publishes refuse
    it; other runs' files never change. Nothing private is touched: local runs/ and the logs dataset stay."""
    runs = sorted(set(runs or ()))
    if not runs:
        raise ValueError('withdraw needs at least one --run')
    known = {run_of(n) for n in base_files if run_of(n)}
    missing = sorted(set(runs) - known)
    if missing:
        raise ValueError('Run(s) not on the site: ' + ', '.join(missing))
    unrecorded = sorted(set(runs) - set(unlisted))
    if unrecorded:
        raise ValueError('Record the run(s) in data/library-unlisted.json first: ' + ', '.join(unrecorded))
    removed = sorted(n for n in base_files if run_of(n) in runs)
    files = {n: d for n, d in base_files.items() if run_of(n) not in runs}
    files.update(render_catalogue(catalogue(base_files), unlisted, files, site=site))
    return files, {'source': {'kind': 'withdraw', 'runs': runs},
                   'removed_files': len(removed),
                   'removed_by_run': {r: sum(1 for n in removed if run_of(n) == r) for r in runs}}


def plan_storymap(base_files, unlisted, folders, taxonomy=()):
    """The site with the story map pages of runs already on it (``folders``: run -> local folder of the public
    render). Each run's RUN/analysis/storymap/ becomes exactly the allowlisted, screened pages of its folder
    (``storymap_site.public_files``; earlier story map pages are replaced, never left stale) and its results.html
    gains the Story map tab. No other file of any run changes and the catalogue is unchanged."""
    if not folders:
        raise ValueError('storymap needs at least one --run RUN=PAGES_DIRECTORY')
    known = {run_of(n) for n in base_files if run_of(n)}
    missing = sorted(set(folders) - known)
    if missing:
        raise ValueError('Run(s) not on the site (publish the run first): ' + ', '.join(missing))
    hidden = sorted(set(folders) & set(unlisted))
    if hidden:
        raise ValueError('Run(s) unlisted by data/library-unlisted.json: ' + ', '.join(hidden))
    files, added, replaced, removed = dict(base_files), [], [], []
    for run in sorted(folders):
        prefix = f'{run}/{storymap_site.STORYMAP_DIR}/'
        pages = {prefix + name: data for name, data in
                 storymap_site.public_files(folders[run], taxonomy).items()}
        for name in sorted(n for n in base_files if n.startswith(prefix) and n not in pages):
            removed.append(name)
            del files[name]
        for name, data in sorted(pages.items()):
            if name not in base_files:
                added.append(name)
            elif base_files[name] != data:
                replaced.append({'path': name, 'old': digest(base_files[name]), 'new': digest(data)})
            files[name] = data
        page = f'{run}/results.html'
        if page not in base_files:
            raise ValueError(f'{page} is not on the site')
        new = storymap_site.add_storymap_tab(base_files[page].decode('utf-8')).encode('utf-8')
        if new != base_files[page]:
            replaced.append({'path': page, 'old': digest(base_files[page]), 'new': digest(new)})
            files[page] = new
    return files, {'source': {'kind': 'storymap', 'runs': sorted(folders)}, 'added_files': len(added),
                   'replaced_pages': replaced, 'removed_files': removed,
                   'added_by_run': {r: sum(1 for n in added if run_of(n) == r) for r in sorted(folders)}}


def plan_heatmap(base_files, unlisted, folders):
    """The site with the "Faking over time" heatmap of runs already on it (owner, November 3, 2026; ``folders``:
    run -> local folder holding scripts/faking_heatmap.py's faking-heatmap.json and, for a run with fakers,
    faking-heatmap.png). RUN/faking-heatmap.png is added or replaced (removed when the run has no fakers) and the
    section is put at the top of RUN/results.html's main report (``faking_heatmap.add_section``, idempotent). No
    other file changes and the catalogue is unchanged."""
    import faking_heatmap
    if not folders:
        raise ValueError('heatmap needs at least one --run RUN=FOLDER')
    known = {run_of(n) for n in base_files if run_of(n)}
    missing = sorted(set(folders) - known)
    if missing:
        raise ValueError('Run(s) not on the site (publish the run first): ' + ', '.join(missing))
    # An unlisted run still on the site (baseline100) is only left out of the catalogue, which this never
    # touches; its served page gets the section like any other (owner: every run on the site).
    files, added, replaced, removed = dict(base_files), [], [], []
    for run in sorted(folders):
        folder = Path(folders[run])
        summary = json.loads((folder / faking_heatmap.SUMMARY).read_text(encoding='utf-8'))
        if summary.get('run') != run:
            raise ValueError(f'{folder} holds the heatmap of {summary.get("run")}, not {run}')
        image = f'{run}/{faking_heatmap.IMAGE}'
        if summary['rows']:
            data = (folder / faking_heatmap.IMAGE).read_bytes()
            if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError(f'{folder / faking_heatmap.IMAGE} is not a PNG')
            if image not in base_files:
                added.append(image)
            elif base_files[image] != data:
                replaced.append({'path': image, 'old': digest(base_files[image]), 'new': digest(data)})
            files[image] = data
        elif image in files:
            removed.append(image)
            del files[image]
        page = f'{run}/results.html'
        if page not in base_files:
            raise ValueError(f'{page} is not on the site')
        new = faking_heatmap.add_section(base_files[page].decode('utf-8'), summary).encode('utf-8')
        if new != base_files[page]:
            replaced.append({'path': page, 'old': digest(base_files[page]), 'new': digest(new)})
            files[page] = new
    return files, {'source': {'kind': 'heatmap', 'runs': sorted(folders)}, 'added_files': len(added),
                   'replaced_pages': replaced, 'removed_files': removed,
                   'without_fakers': sorted(r for r in folders if not json.loads(
                       (Path(folders[r]) / faking_heatmap.SUMMARY).read_text(encoding='utf-8'))['rows'])}


JUDGE_WORDS = (simple_judge_site.level_text('submitted'), simple_judge_site.EXCLUSIVE_NOTE,
               'b.parentNode.textContent.trim()')


def _judge_only_changes(old, new):
    """True when every changed stretch of ``new`` (vs ``old``, split at tags) carries the new judge wording."""
    import difflib
    a, b = re.split(r'(?<=>)', old), re.split(r'(?<=>)', new)
    for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != 'equal' and not any(w in ''.join(b[j1:j2]) for w in JUDGE_WORDS):
            return False
    return True


def plan_judge_labels(base_files, runs=None):
    """The site with the simplified judge's level wording rewritten in place (owner, November 3, 2026): for every
    run (or ``runs``) with RUN/analysis/simple_judge/report.json on the site, RUN/results.html's judge spots
    (``simple_judge_site.add_simple_judge_tab`` with the served report.json; only a page that already has the
    simplified judge's tab) and RUN/analysis/simple_judge/report.html (``relabel_report_page``). Refuses a change
    outside the judge wording. No other file changes and the catalogue is unchanged."""
    sj = simple_judge_site.SJ_DIR
    known = sorted({run_of(n) for n in base_files if n.endswith(f'/{sj}/report.json') and run_of(n)})
    if runs:
        missing = sorted(set(runs) - set(known))
        if missing:
            raise ValueError('Run(s) without a published simplified judge: ' + ', '.join(missing))
        known = sorted(runs)
    files, replaced, skipped = dict(base_files), [], []
    for run in known:
        report = json.loads(base_files[f'{run}/{sj}/report.json'])
        pages = {}
        page = f'{run}/results.html'
        if page in base_files:
            text = base_files[page].decode('utf-8')
            if simple_judge_site.TAB_MARKER in text and simple_judge_site.simple_summary(report):
                pages[page] = simple_judge_site.add_simple_judge_tab(text, report)
            else:
                skipped.append(page)
        page = f'{run}/{sj}/report.html'
        if page in base_files:
            pages[page] = simple_judge_site.relabel_report_page(base_files[page].decode('utf-8'))
        for page, text in pages.items():
            old = base_files[page].decode('utf-8')
            if text == old:
                continue
            if not _judge_only_changes(old, text):
                raise ValueError(f'{page}: the relabel would change more than the judge wording')
            data = text.encode('utf-8')
            replaced.append({'path': page, 'old': digest(base_files[page]), 'new': digest(data)})
            files[page] = data
    return files, {'source': {'kind': 'judge-labels', 'runs': known}, 'added_files': 0,
                   'replaced_pages': replaced, 'skipped_pages': skipped}


def plan_catalogue(base_files, unlisted, site=None):
    """The site with only its catalogue re-rendered from its entries (index.html and experiments.json; Story map
    links follow the story map pages on the site). No run file changes."""
    files = dict(base_files)
    files.update(render_catalogue(catalogue(base_files), unlisted, base_files, site=site))
    changed = sorted(n for n in ('index.html', 'experiments.json') if base_files.get(n) != files[n])
    return files, {'source': {'kind': 'catalogue'}, 'changed_files': changed,
                   'storymap_links': sorted(e['run'] for e in catalogue(files) if STORYMAP_LABEL in e['links'])}


def plan_correct(base_files, unlisted, run, records, entry=None, site=None):
    """The site with named records of one run replaced by corrected bytes (owner decision, November 4, 2026:
    docs/plan.md, "Correct 2ov r1's immutable records"). ``records``: run-relative path -> local file. Each path must
    already be served, and must not be a page, a pack file or a private category; ``entry`` (a catalogue entry
    dict) replaces the run's catalogue entry. Every replacement is recorded with old and new digests."""
    if run not in {run_of(n) for n in base_files if run_of(n)}:
        raise ValueError('Run not on the site: ' + run)
    if not records and entry is None:
        raise ValueError('correct needs at least one --file or --entry')
    files, replaced = dict(base_files), []
    for relative in sorted(records):
        name = f'{run}/{relative}'
        if name not in base_files:
            raise ValueError('Not on the site (correct only replaces served files): ' + name)
        if name.endswith(('.html', '.htm')) or is_pack_file(name) or private_category(name) is not None:
            raise ValueError('Not a correctable record (page, pack file or private category): ' + name)
        data = Path(records[relative]).read_bytes()
        if data != base_files[name]:
            replaced.append({'path': name, 'old': digest(base_files[name]), 'new': digest(data)})
            files[name] = data
    entry_change = None
    if entry is not None:
        if entry.get('run') != run or any(not t.startswith(run + '/') for t in entry.get('links', {}).values()):
            raise ValueError('The corrected catalogue entry must be the run\'s, with links inside the run')
        entries = catalogue(base_files)
        old = [e for e in entries if e['run'] == run]
        if not old:
            raise ValueError('Run has no catalogue entry: ' + run)
        if old[0] != entry:
            dump = lambda e: json.dumps(e, sort_keys=True).encode()  # noqa: E731
            entry_change = {'old': digest(dump(old[0])), 'new': digest(dump(entry)),
                            'fields': sorted(k for k in set(old[0]) | set(entry) if old[0].get(k) != entry.get(k))}
            files.update(render_catalogue([entry if e['run'] == run else e for e in entries], unlisted, files,
                                          site=site))
    return files, {'source': {'kind': 'correct', 'runs': [run]}, 'replaced_records': replaced,
                   'catalogue_entry': entry_change}


def plan_repack(base_files):
    """The site with every run's pack groups packed (``pack_site``, applied by ``finish``). Nothing else
    changes: no file of any run other than the files of its pack groups (replaced by RUN/packed/ and the
    viewers), and unpacking reproduces each of them byte for byte."""
    pending = sorted({run_of(n) for n in base_files if packable(n)})
    return dict(base_files), {'source': {'kind': 'repack', 'runs': pending}}


def finish(files, base_manifest, plan, project=None, site=None):
    """Add the generated pages and the site manifest; gate every file, every run's domain (``check_domains``)
    and the deployment limits."""
    site = site_of(site)
    project = project or site.project
    if project != site.project:
        raise ValueError(f'Project {project} is not the {site.key} site\'s ({site.project})')
    files = {n: d for n, d in files.items() if n not in GENERATED}
    check_domains(files, site)
    files, repointed = repoint_private_links(unpack_site(files))
    if repointed:
        plan['repointed_private_links'] = repointed
    files, packed = pack_site(files)
    if packed:
        plan['packed_runs'] = packed
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
    served = files
    manifest = {'schema': SITE_SCHEMA, 'project': project, 'runs': runs,
                'public': True, 'private_store': None,
                'files': {n: digest(d) for n, d in sorted(served.items())},
                'total_bytes': sum(len(d) for d in served.values()), 'history': history}
    data = json.dumps(manifest, indent=1).encode()
    files[SITE_MANIFEST] = site_gate(SITE_MANIFEST, data)
    if len(files) > MAX_FILES:
        raise ValueError(f'{len(files)} files exceed Cloudflare Pages\' {MAX_FILES:,}-file limit per deployment')
    return files, manifest


def summary(files, base_files, plan, base, site=None):
    return {**plan, 'project': site_of(site).project, 'base_deployment': base,
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
    indexes = {}

    def packed_index(run):  # {relative: record} of a run's packed files ({} for a run without packs)
        if run not in indexes:
            indexes[run] = {}
            if f'{run}/{PACK_FOLDER}/index.json' in recorded:
                code, _, body = getter(file_url(base, f'{run}/{PACK_FOLDER}/index.json'))
                if code == 200:
                    indexes[run] = json.loads(body)['files']
        return indexes[run]

    def is_packed(target):
        run, _, relative = target.partition('/')
        index = packed_index(run) if run in recorded_runs else {}
        return relative in index or relative + '.html' in index

    recorded_runs = {n.split('/')[0] for n in recorded if '/' in n}
    bad, notice, links, members = [], [], [], {}
    for name in names:
        status, headers, served = getter(file_url(base, name))
        record = recorded[name]
        if status != 200 or digest(served) != {'bytes': record['bytes'], 'sha256': record['sha256']}:
            bad.append(f'{name} (HTTP {status})')
            continue
        content_type = {k.lower(): v for k, v in headers.items()}.get('content-type', '')
        if is_pack_file(name):  # --full: every pack entry against its own digest
            try:
                for relative, entry in json.loads(served)['files'].items():
                    ok = digest(_entry_bytes(entry)) == {'bytes': entry['bytes'], 'sha256': entry['sha256']}
                    members[f'{name.split("/")[0]}/{relative}'] = (PurePosixPath(name).name, ok)
            except (ValueError, KeyError) as error:
                bad.append(f'{name} (unreadable pack: {error})')
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
                        and not (target == '' or url.endswith('/') and (target + '/index.html') in recorded) \
                        and not is_packed(target):
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
    packed = sorted({n.split('/')[0] for n in recorded if is_pack_file(n)})
    if packed:  # an old file URL of each pack group shows the run's viewer (the group's 404.html, URL unchanged)
        old, tried = [], set()
        sample = packed if full else [r for r in packed if r in runs] or packed
        for run in sample:
            for relative in packed_index(run):
                group = group_of(f'{run}/{relative}')
                if group is None or (group, run) in tried or (not full and any(g == group for g, _ in tried)):
                    continue
                tried.add((group, run))
                viewer = recorded.get(f'{run}/{group}/{VIEWER_404}')
                code, _, body = getter(file_url(base, f'{run}/{relative}'))
                record = packed_index(run)[relative]
                # the viewer (HTTP 404, URL unchanged), or the original bytes themselves (the edge may still hold
                # a copy from before the file was packed)
                if not (viewer and code == 404 and digest(body) == {'bytes': viewer['bytes'],
                                                                    'sha256': viewer['sha256']}) \
                        and not (code == 200 and digest(body) == {'bytes': record['bytes'],
                                                                  'sha256': record['sha256']}):
                    old.append(f'{run}/{relative} (HTTP {code})')
        check('packed_old_urls', tried and not old, '; '.join(old[:5]) or f'{len(tried)} group(s) checked')
    if full and packed:  # every indexed file is in its pack with its digest, and nothing else is packed
        wrong = []
        for run in packed:
            for relative, record in packed_index(run).items():
                pack, ok = members.get(f'{run}/{relative}', (None, False))
                if pack != record['pack'] or not ok:
                    wrong.append(f'{run}/{relative}')
        indexed = {f'{r}/{x}' for r in packed for x in packed_index(r)}
        wrong += sorted(set(members) - indexed)
        check('packed_members', not wrong, ', '.join(wrong[:5]) or f'{len(members)} packed files')
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


def storymap_taxonomy():
    """The researcher-only taxonomy names, when this checkout has them, so that none reaches the site (read
    locally, never printed or deployed): {code: name}, {} without them."""
    try:
        from research.analysis.storymap.view import DEFAULT_TAXONOMY, load_taxonomy
    except ImportError:
        sys.path.insert(0, str(ROOT))
        from research.analysis.storymap.view import DEFAULT_TAXONOMY, load_taxonomy
    return load_taxonomy(DEFAULT_TAXONOMY)


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
    repack = sub.add_parser('repack', help='pack every run\'s pack groups (per-agent files, sources, story map '
                                           'agent pages, judge files; lossless)')
    withdraw = sub.add_parser('withdraw', help='remove recorded unlisted runs (all their files) from the site')
    withdraw.add_argument('--run', action='append', required=True, help='run to remove (repeatable)')
    story = sub.add_parser('storymap', help='add the agent story map pages to runs already on the site')
    story.add_argument('--run', action='append', required=True,
                       help='RUN=PAGES_DIRECTORY (the public render, --taxonomy \'\'); repeatable')
    heat = sub.add_parser('heatmap', help='add the "Faking over time" heatmap to runs already on the site')
    heat.add_argument('--run', action='append', required=True,
                      help='RUN=FOLDER holding faking_heatmap.py output (repeatable)')
    cat = sub.add_parser('catalogue', help='re-render only the catalogue (index.html, experiments.json)')
    fix = sub.add_parser('correct', help='replace named records of a run with corrected bytes (owner decision)')
    fix.add_argument('--run', required=True)
    fix.add_argument('--file', action='append', default=[], help='PATH=LOCAL: run-relative served record (repeatable)')
    fix.add_argument('--entry', type=Path, help='JSON file with the corrected catalogue entry of the run')
    labels = sub.add_parser('judge-labels', help='rewrite the simplified judge\'s level wording on the site')
    labels.add_argument('--run', action='append', help='only this run (repeatable; default every judged run)')
    for command in (mirror, bundle, restyle, withdraw, repack, story, heat, cat, labels, fix):
        command.add_argument('--site', choices=sorted(SITES), default=DEFAULT_SITE,
                             help='which library (default gdm, the math site; ib: the ImpossibleBench site)')
        command.add_argument('--commit', action='store_true', help='deploy; otherwise only plan')
        command.add_argument('--receipt', type=Path, help='receipt JSON (outside the bundle)')
        command.add_argument('--workdir', type=Path, help='scratch directory for staging (default: a temporary one)')
    check = sub.add_parser('verify', help='read-only check of the served site')
    check.add_argument('--site', choices=sorted(SITES), default=DEFAULT_SITE)
    check.add_argument('--url', help='deployment to check (default: the site\'s URL)')
    check.add_argument('--run', action='append')
    check.add_argument('--full', action='store_true', help='check every file, not only pages and records')
    check.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'verify':
        result = verify(args.url or SITES[args.site].url, args.run, args.full)
        text = json.dumps(result, indent=2)
        if args.output:
            args.output.write_text(text)
        print(text)
        return 0 if result['ok'] else 1
    if args.command == 'publish' and args.dataset_commit and not re.fullmatch(r'[0-9a-f]{40}', args.dataset_commit):
        parser.error('--dataset-commit must be a full commit')
    if args.receipt and args.command == 'publish' and args.receipt.resolve().is_relative_to(args.bundle.resolve()):
        parser.error('--receipt must be outside the prepared bundle')
    site = SITES[args.site]
    if args.command == 'mirror-space' and not site.mirror_space:
        parser.error(f'mirror-space copies the math library\'s HF Space; not for --site {site.key}')
    with tempfile.TemporaryDirectory(prefix='cf-publish-') as tmp:
        workdir = args.workdir or Path(tmp)
        workdir.mkdir(parents=True, exist_ok=True)
        current = deployments(workdir, project=site.project)
        base = current[0] if current else None
        base_manifest, served = load_site(base['url'] if base else None, cache=site.cache)
        base_files = unpack_site(served)  # plans see every file; finish packs again
        unlisted = unlisted_runs()
        if args.command == 'mirror-space':
            if args.snapshot:
                if not args.revision:
                    parser.error('--snapshot needs --revision')
                revision, snapshot = args.revision, args.snapshot
            else:
                revision, snapshot = download_space(args.revision, workdir / 'space')
            space, private = space_files(snapshot)
            files, plan = plan_mirror(base_files, space, revision, unlisted, site=site)
            plan['private_files_not_deployed'] = private
            message = f'Mirror HF Space {revision[:12]}'
        elif args.command == 'restyle':
            files, plan = plan_restyle(base_files, unlisted, args.run, site=site)
            message = f'Restyle {len(plan["replaced_pages"])} page(s) and the catalogue'
        elif args.command == 'repack':
            files, plan = plan_repack(base_files)
            message = f'Repack per-agent files of {len(plan["source"]["runs"])} run(s)'
        elif args.command == 'catalogue':
            files, plan = plan_catalogue(base_files, unlisted, site=site)
            message = 'Re-render the catalogue'
        elif args.command == 'storymap':
            folders = dict(spec.split('=', 1) for spec in args.run)
            files, plan = plan_storymap(base_files, unlisted, folders, storymap_taxonomy())
            message = 'Story map ' + ', '.join(plan['source']['runs'])
        elif args.command == 'heatmap':
            folders = dict(spec.split('=', 1) for spec in args.run)
            files, plan = plan_heatmap(base_files, unlisted, folders)
            message = f'Faking heatmap for {len(folders)} run(s)'
        elif args.command == 'correct':
            records = dict(spec.split('=', 1) for spec in args.file)
            entry = json.loads(args.entry.read_text()) if args.entry else None
            files, plan = plan_correct(base_files, unlisted, args.run, records, entry, site=site)
            message = 'Correct records of ' + args.run
        elif args.command == 'judge-labels':
            files, plan = plan_judge_labels(base_files, args.run)
            message = f'Judge level wording on {len(plan["replaced_pages"])} page(s)'
        elif args.command == 'withdraw':
            files, plan = plan_withdraw(base_files, unlisted, args.run, site=site)
            message = 'Withdraw ' + ', '.join(plan['source']['runs'])
        else:
            files, plan = plan_bundle(base_files, args.bundle, unlisted, args.dataset_commit, args.replace_pages,
                                      site=site)
            message = 'Publish ' + ', '.join(plan['source']['runs'])
        files, manifest = finish(files, base_manifest, plan, site=site)
        result = {**summary(files, served, plan, base, site=site), 'committed': False}
        if args.commit:
            remember(files, cache=site.cache)
            deployment, tail = deploy(files, base, message, workdir, project=site.project)
            result.update(committed=True, deployment=deployment, site=site.url)
            result['verify'] = verify(deployment['url'] or site.url)
        text = json.dumps(result, indent=2)
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(text)
        print(text)
        return 0 if not args.commit or result['verify']['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
