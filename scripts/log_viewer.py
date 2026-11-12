"""The log viewer: a separate Cloudflare Pages origin where organization members read the private Inspect logs with
their own HF sign-in (owner approval, November 12, 2026; docs/experiment-library.md, "Log viewer with HF sign-in").

The public library (scripts/publish_cloudflare.py) renders agent-written content, so no token ever lives on its
origin. The viewer origin hosts only: one copy of the (byte-identical) Inspect View assets, one page per run
(the run's Inspect View page with the auth shim instead of the private-logs notice) with the run's public
``logs/listing.json``, the HF sign-in page, a run index and a strict ``_headers`` (CSP with script-src 'self',
frame-ancestors 'none', Referrer-Policy no-referrer). No ``.eval`` log or raw record is ever on it: Inspect View's
``logs/<name>.eval`` requests go to the HF logs dataset's resolve URL at the run's pinned commit, with the viewer's
bearer token (scripts/cf_site/viewer/auth-fetch.js).

The HF OAuth client ID is public by design but kept out of tracked files like other account details: it comes from
``$GDM_HF_OAUTH_CLIENT_ID`` (HANDOFF.local.md), else from the served viewer's ``viewer.json``; without one the
viewer is deployed with sign-in disabled (its pages say so).
"""
import hashlib
import html
import json
import os
import re

from pathlib import Path

import dataset_logs

PROJECT = 'gdm-math-swarm-viewer'
URL = f'https://{PROJECT}.pages.dev'
REDIRECT_URI = URL + '/signin'  # Pages serves signin.html at /signin (a .html URL answers 308)
CLIENT_ENV = 'GDM_HF_OAUTH_CLIENT_ID'
CONFIG = 'viewer.json'
NOT_FOUND = '404.html'
SCHEMA = 'gdm-log-viewer/1'
SOURCE = Path(__file__).resolve().parent / 'cf_site' / 'viewer'
AUTH = 'auth-fetch.js'
ASSETS = 'assets'
SCOPES = 'openid profile read-repos'

CLIENT_ID = re.compile(r'[A-Za-z0-9-]{8,80}')
REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*')
COMMIT = re.compile(r'[0-9a-f]{40}')
FOLDER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+=/-]*')
RUN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+=-]*')
RECORDS = re.compile(r'https://huggingface\.co/datasets/(' + REPO.pattern + r')/tree/(' + COMMIT.pattern
                     + r')/(' + FOLDER.pattern + r')')
CF_RECORDS = re.compile(r'<script id="private-logs"[^>]*\bdata-records="([^"]*)"')

# Strict policy for every viewer response. Inspect View needs inline styles (React style attributes) and data:/blob:
# images and fonts; its scripts are all served from this origin (inline scripts are moved to files at build time).
# 'unsafe-eval' is needed by Inspect View's zstd decompression worker (fzstd builds its worker code from strings;
# without it no log opens, tested November 12, 2026); no inline or foreign script can run, so eval only ever sees
# Inspect View's own strings. Logs come from huggingface.co, which redirects to its CDN hosts (*.hf.co).
CSP = ("default-src 'none'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self' data:; connect-src 'self' https://huggingface.co https://*.hf.co https://*.huggingface.co; "
       "worker-src 'self' blob:; manifest-src 'self'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'none'; object-src 'none'")
HEADERS = f"""/*
  Content-Security-Policy: {CSP}
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Resource-Policy: same-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()
  X-Robots-Tag: noindex, nofollow
"""


def run_url(run):
    return f'{URL}/{run}/'


def settings_from_records(records):
    """(dataset, commit, folder) from a run's records link (the logs dataset folder at its pinned commit), or None."""
    match = RECORDS.fullmatch(records or '')
    if not match or '..' in match.group(3).split('/'):
        return None
    return match.group(1), match.group(2), match.group(3)


def page_settings(document):
    """(dataset, commit, folder) of an Inspect View page of the public site (its private-logs notice's records
    link) or of an HF bundle (its evallog-fetch shim); None when the run's logs are kept offline."""
    match = CF_RECORDS.search(document)
    if match:
        return settings_from_records(html.unescape(match.group(1)))
    settings = dataset_logs.shim_settings(document)
    if settings and COMMIT.fullmatch(settings[1]) and REPO.fullmatch(settings[0]) and FOLDER.fullmatch(settings[2]):
        return tuple(settings)
    return None


def viewer_runs(files):
    """{run: (dataset, commit, folder)} for every run of the public site whose Inspect logs are in the dataset."""
    out = {}
    for name, data in files.items():
        if name.endswith('/inspect-view/index.html') and name.count('/') == 2 and RUN.fullmatch(name.split('/')[0]):
            settings = page_settings(data.decode('utf-8', 'replace'))
            if settings:
                out[name.split('/')[0]] = settings
    return out


# --- links of the public site --------------------------------------------------------------------------------

LINK = re.compile(r'''(<a\b[^>]*?\bhref=)(["'])([^"'#?]*)([^"']*)\2''', re.I)


def _normalise(page, url):
    parts = []
    for part in (page.split('/')[:-1] + url.split('/')):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return '/'.join(parts)


def rewrite_links(page, document, runs):
    """``document`` (a page of the public site at ``page``) with each same-site link to a run's Inspect View
    (``RUN/inspect-view/`` or ``RUN/inspect-view/index.html``, any hash kept) pointed at the run's viewer page,
    for the runs in ``runs``. Inspect View pages themselves are left alone (they are the fallback)."""
    if page.endswith('/inspect-view/index.html'):
        return document

    def swap(match):
        url = match.group(3)
        if not url or re.match(r'[a-z][a-z0-9+.-]*:|//|/', url, re.I):
            return match.group(0)
        target = _normalise(page, url)
        if not target:
            return match.group(0)
        parts = target.split('/')
        if len(parts) in (2, 3) and parts[1] == 'inspect-view' and (len(parts) == 2 or parts[2] in ('', 'index.html')) \
                and parts[0] in runs:
            rest = match.group(4)
            fragment = rest[rest.index('#'):] if '#' in rest else ''
            return f'{match.group(1)}{match.group(2)}{run_url(parts[0])}{fragment}{match.group(2)}'
        return match.group(0)
    return LINK.sub(swap, document)


# --- the viewer site -----------------------------------------------------------------------------------------

STRIP = [re.compile(p, re.S | re.I) for p in (
    r'<script id="private-logs"[^>]*>.*?</script>', r'<script id="evallog-fetch"[^>]*>.*?</script>',
    r'<script id="private-space-navigation"[^>]*>.*?</script>', r'<meta name="referrer"[^>]*>')]
SCRIPT = re.compile(r'<script\b([^>]*)>(.*?)</script>', re.S | re.I)


def _inline_assets(document, assets):
    """Move each inline executable script of a page to ``assets/inline-<sha>.js`` (CSP script-src 'self')."""
    def move(match):
        attributes, body = match.group(1), match.group(2)
        if re.search(r'\bsrc=', attributes, re.I):
            return match.group(0)
        kind = re.search(r'''\btype=["']?([^"'\s>]+)''', attributes, re.I)
        kind = kind.group(1).lower() if kind else ''
        if kind not in ('', 'module', 'text/javascript', 'application/javascript'):
            return match.group(0)  # data blocks (application/json) are not executed
        if kind == 'module':
            body = body.replace('"./assets/', '"./').replace("'./assets/", "'./")
        data = body.encode('utf-8')
        name = f'{ASSETS}/inline-{hashlib.sha256(data).hexdigest()[:16]}.js'
        assets[name] = data
        module = ' type="module"' if kind == 'module' else ''
        return f'<script{module} src="../{name}"></script>'
    return SCRIPT.sub(move, document)


def viewer_page(document, run, settings, assets):
    """The viewer page of a run from its public Inspect View page."""
    dataset, commit, folder = settings
    for pattern in STRIP:
        document = pattern.sub('', document)
    if re.search(r'<script\b[^>]*\bsrc=["\'](?:https?:)?//', document, re.I):
        raise ValueError(f'{run}: Inspect View page loads an external script')
    document = _inline_assets(document, assets)
    document = re.sub(r'''(\b(?:href|src)=["'])\./assets/''', r'\1../assets/', document)
    if re.search(r'''\b(?:href|src)=["'](?!\.\./assets/|#|data:)''', document):
        bad = re.findall(r'''\b(?:href|src)=["']((?!\.\./assets/|#|data:)[^"']*)''', document)
        raise ValueError(f'{run}: unexpected resource in the Inspect View page: {bad[:3]}')
    attributes = ' '.join(f'data-{k}="{html.escape(v, quote=True)}"' for k, v in
                          (('dataset', dataset), ('commit', commit), ('path', folder), ('signin', '/signin')))
    head = re.search(r'<head(?:\s[^>]*)?>', document, re.I)
    if not head:
        raise ValueError(f'{run}: Inspect View page has no <head>')
    script = f'<meta name="referrer" content="no-referrer"><script src="../{AUTH}" {attributes}></script>'
    return document[:head.end()] + script + document[head.end():]


def signin_page(client_id, org_name):
    text = (SOURCE / 'signin.html').read_text(encoding='utf-8')
    for key, value in (('__CLIENT_ID__', client_id or ''), ('__REDIRECT_URI__', REDIRECT_URI if client_id else ''),
                       ('__ORG_IDS__', dataset_logs.ORG_IDS), ('__ORG_NAME__', org_name)):
        text = text.replace(key, html.escape(value, quote=True))
    return text


def index_page(entries, runs, client_id):
    listed = [e for e in entries if e.get('run') in runs]
    items = []
    for entry in listed:
        run = entry['run']
        detail = ', '.join(str(entry[k]) for k in ('model', 'agents') if entry.get(k))
        items.append(f'<li><a href="{html.escape(run)}/">{html.escape(run)}</a>'
                     + (f' <span class="muted">({html.escape(detail)})</span>' if detail else '') + '</li>')
    state = ('' if client_id else '<p id="status" class="error">HF sign-in is not configured for this viewer yet.</p>')
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Inspect log viewer</title>
<link rel="stylesheet" href="viewer.css">
</head>
<body>
<main>
<h1>Inspect log viewer</h1>
<p>The agent transcripts (Inspect logs) of the <a href="https://gdm-math-swarm-experiments.pages.dev/experiments.html">experiment
library</a> are private. Members of the organization read them here with their own Hugging Face sign-in; the logs
come straight from the private HF dataset to your browser and are never stored on this site.</p>
{state}<p><a href="signin">Sign in with HF</a> (or sign out)</p>
<ul>
{chr(10).join(items)}
</ul>
</main>
</body>
</html>
'''


def not_found_page():
    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not found</title><link rel="stylesheet" href="/viewer.css"></head>
<body><main><h1>Not found</h1><p>No such page on the log viewer. <a href="/">All runs</a></p></main></body></html>
'''


def build(files, entries, client_id=None):
    """(viewer files {name: bytes}, config) from the public site's files (unpacked or packed: Inspect View folders
    are never packed) and its catalogue entries. Only runs whose logs are in the dataset get a page."""
    if client_id and not CLIENT_ID.fullmatch(client_id):
        raise ValueError('Unexpected HF OAuth client ID format')
    runs = viewer_runs(files)
    out, assets, shared = {}, {}, {}
    datasets = {s[0] for s in runs.values()}
    if len(datasets) > 1:
        raise ValueError('Runs read from more than one dataset: ' + ', '.join(sorted(datasets)))
    for run, settings in sorted(runs.items()):
        prefix = f'{run}/inspect-view/'
        for name, data in files.items():
            if not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
            if relative.startswith(ASSETS + '/'):
                if relative in shared and shared[relative] != data:
                    raise ValueError(f'Inspect View assets differ between runs: {relative}')
                shared[relative] = data
        listing = files.get(prefix + 'logs/listing.json')
        out[f'{run}/index.html'] = viewer_page(files[prefix + 'index.html'].decode('utf-8'), run, settings,
                                               assets).encode()
        if listing is not None:
            out[f'{run}/logs/listing.json'] = listing
    for relative, data in {**shared, **assets}.items():
        if relative in shared and relative in assets and shared[relative] != assets[relative]:
            raise ValueError('Inline script name clashes with an asset: ' + relative)
        out[relative] = data
    org = next(iter(datasets)).split('/')[0] if datasets else ''
    for name in ('viewer.css', 'signin.js', AUTH):
        out[name] = (SOURCE / name).read_bytes()
    out['signin.html'] = signin_page(client_id, org).encode()
    out['index.html'] = index_page(entries, runs, client_id).encode()
    out['_headers'] = HEADERS.encode()
    out['robots.txt'] = b'User-agent: *\nDisallow: /\n'
    config = {'schema': SCHEMA, 'project': PROJECT, 'client_id': client_id or None,
              'redirect_uri': REDIRECT_URI if client_id else None, 'scopes': SCOPES,
              'runs': {run: {'dataset': s[0], 'commit': s[1], 'path': s[2]} for run, s in sorted(runs.items())},
              'files': {n: hashlib.sha256(d).hexdigest() for n, d in sorted(out.items())}}
    out[CONFIG] = json.dumps(config, indent=1).encode()
    for name, data in out.items():  # nothing private, ever
        if name.endswith('.eval') or b'PK\x03\x04' == data[:4]:
            raise ValueError('Log data refused on the viewer: ' + name)
    return out, config


def client_id_from(served_config=None):
    """The HF OAuth client ID: $GDM_HF_OAUTH_CLIENT_ID, else the served viewer's, else None."""
    value = os.environ.get(CLIENT_ENV, '').strip()
    if value:
        return value
    if served_config and isinstance(served_config.get('client_id'), str):
        return served_config['client_id']
    return None
