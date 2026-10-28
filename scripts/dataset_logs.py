"""Run files kept in the private HF dataset instead of the Space (docs/experiment-library.md,
"Logs and raw records in the dataset repo").

The Space hits HF's 1 GB repository cap, so (owner, October 22, 2026):

- **Inspect logs** (``.eval``) go to the private dataset named by ``GDM_LOGS_DATASET``
  (scripts/hf_visibility.py) at ``RUN/inspect/<name>.eval`` (live logs at
  ``live/RUN/inspect/<name>.eval``). The static Space reads them with the viewer's own "Sign in
  with HF" OAuth token (``hf_site/signin.html``) through a fetch shim (``hf_site/evallog-fetch.js``)
  inlined first into each Inspect View ``index.html``, pinned to the dataset commit. No secret is
  stored anywhere.
- **Files the site does not need to render or work** ("go ahead and move everything to dataset that's
  not necessary for current functionality of the website"): raw ``.jsonl`` records, snapshot archives and
  loose snapshot files, ``.db``/``.sqlite`` databases and the old custom transcript pages
  (``RUN/transcripts/``) go to the dataset at the same ``RUN/<path>``. A page link to one of them points to
  the dataset file page with a note that it is stored in the private logs dataset.
- **The resume bundle** (``RUN/resume/``, research_swarm.resume_bundle: agent session transcripts, workspace
  archives, the resume manifest) is private raw state (owner, October 22, 2026): it never goes to the Space;
  when a run is published it goes to the private dataset at ``RUN/resume/...``.
- **The activity timeline** (owner, October 23, 2026: timelines live only in the private dataset): the
  Markdown copy ``RUN/analysis/timeline.md`` (category ``timeline``, up to 9 MB) goes to the dataset at the
  same path, like ``RUN/analysis/timeline.jsonl`` (category ``jsonl``). The run page's "Timeline" link points
  to the dataset file page; the catalogue no longer shows a Timeline link (catalogue_page.HIDDEN_LINKS).
- **The simplified behavior judge's per-agent rows** ``RUN/analysis/simple_judge/agents.jsonl`` (category
  ``jsonl``) go to the dataset; its ``report.html`` and ``report.json`` stay in the Space and its working files
  (``cases.jsonl``, ``raw/``, ``run-budgets.json``, ``adjudication.json``, locks, rehearsal ledgers) never
  leave the machine (scripts/simple_judge_site.py), so ``category`` names none of them.

Everything here uses the operator's own HF login only through ``huggingface_hub``; no token value is
read, printed or embedded.
"""
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import re

import hf_visibility

DATASET_REPO = hf_visibility.DATASET_REPO  # private; its expected visibility is recorded in hf_visibility.py
# The organization the sign-in asks the viewer to grant (HF "orgIds"); its public ID, not a secret.
ORG_IDS = '6ad1650e6daadd58425fcb95'
HUB = 'https://huggingface.co'
SITE = Path(__file__).resolve().parent / 'hf_site'
SHIM_FILE = 'evallog-fetch.js'
SIGNIN_FILE = 'signin.html'
SHIM_MARKER = 'id="evallog-fetch"'
COMMIT_PLACEHOLDER = '__DATASET_COMMIT__'
NOTE_MARKER = 'data-dataset-file'
DATASET_SCHEMA = 'gdm-dataset-files/1'

# Run-relative files that live in the dataset. 'eval' (Inspect logs) are read by Inspect View through the
# shim; the other categories are download-only records the pages never load.
CATEGORIES = {
    'eval': 'Inspect log',
    'jsonl': 'raw JSONL record',
    'snapshots': 'runtime file snapshot archive',
    'database': 'database file',
    'transcripts': 'custom transcript export (replaced by Inspect View)',
    'resume': 'resume bundle (private agent state for continuing a run)',
    'timeline': 'activity timeline (Markdown copy of analysis/timeline.jsonl)',
}
DOWNLOAD_ONLY = ('jsonl', 'snapshots', 'database', 'transcripts', 'resume', 'timeline')
TIMELINE_MD = 'analysis/timeline.md'


def category(relative):
    """The dataset category of a run-relative path, or None when the file stays in the Space."""
    parts = PurePosixPath(relative).parts
    if not parts:
        return None
    name = parts[-1]
    if name.endswith('.eval') and (parts[:1] == ('inspect',) or parts[:2] == ('inspect-view', 'logs')):
        return 'eval'
    if parts[0] == 'transcripts':
        return 'transcripts'
    if parts[0] == 'resume':  # RUN/resume/ (research_swarm.resume_bundle): private raw state, never the Space
        return 'resume'
    if '/'.join(parts) == TIMELINE_MD:  # the timeline lives only in the dataset (owner, October 23, 2026)
        return 'timeline'
    if parts[:2] == ('analysis', 'simple_judge') and len(parts) > 2:
        # Simplified behavior judge (scripts/simple_judge_site.py): only agents.jsonl goes to the dataset;
        # cases.jsonl, raw/ and the other working files stay local and are excluded before this is asked.
        return 'jsonl' if parts[2:] == ('agents.jsonl',) else None
    if parts[0] == 'snapshots' or name in ('snapshots.tar.gz', 'snapshots.manifest.json'):
        return 'snapshots'
    if name.endswith('.jsonl'):
        return 'jsonl'
    if re.search(r'\.(?:db|sqlite|sqlite3)(?:-(?:shm|wal|journal))?$', name):
        return 'database'
    return None


def dataset_path(run, relative, live=False):
    """Where a run file lives in the dataset: Inspect logs (either Space copy) at RUN/inspect/<name>, any
    other file at RUN/<relative>; live runs under live/."""
    kind = category(relative)
    if kind is None:
        raise ValueError('Not a dataset file: ' + relative)
    rest = 'inspect/' + PurePosixPath(relative).name if kind == 'eval' else relative
    return ('live/' if live else '') + f'{run}/{rest}'


def file_url(path, commit, repo=DATASET_REPO, kind='blob'):
    """HF page (blob) or raw (resolve) URL of a dataset file at a commit."""
    quoted = '/'.join(_quote(part) for part in path.split('/'))
    return f'{HUB}/datasets/{repo}/{kind}/{commit}/{quoted}'


def _quote(part):
    from urllib.parse import quote
    return quote(part, safe='')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def digest(data):
    return {'bytes': len(data), 'sha256': sha256(data)}


# --- pages -----------------------------------------------------------------------------------------------

def shim_script(*, path, commit=COMMIT_PLACEHOLDER, signin='../../' + SIGNIN_FILE, repo=DATASET_REPO):
    """The inline fetch shim for one Inspect View, reading logs from ``repo`` at ``commit`` under ``path``."""
    for value in (repo, commit, path):
        if value != COMMIT_PLACEHOLDER and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', value) or '..' in value):
            raise ValueError('Unsafe shim setting: ' + value)
    if not re.fullmatch(r'(?:\.\./)*[A-Za-z0-9_./-]+\.html', signin):
        raise ValueError('Unsafe sign-in path: ' + signin)
    source = (SITE / SHIM_FILE).read_text(encoding='utf-8')
    attributes = ' '.join(f'data-{k}="{html.escape(v, quote=True)}"'
                          for k, v in (('dataset', repo), ('commit', commit), ('path', path), ('signin', signin)))
    return f'<script {SHIM_MARKER} {attributes}>\n{source}</script>'


def with_log_shim(document, **settings):
    """An Inspect View index.html with the shim inlined first in <head> (so that it wraps fetch before the
    private-Space navigation script and Inspect's modules). An existing shim is replaced."""
    script = shim_script(**settings)
    existing = re.compile(r'<script id="evallog-fetch"[^>]*>.*?</script>', re.S)
    if SHIM_MARKER in document:
        return existing.sub(lambda _: script, document, count=1)
    head = re.search(r'<head(?:\s[^>]*)?>', document, re.I)
    if not head:
        raise ValueError('Inspect View index.html has no <head>')
    return document[:head.end()] + script + document[head.end():]


def shim_settings(document):
    """The (dataset, commit, path) of a page's shim, or None."""
    match = re.search(r'<script id="evallog-fetch" data-dataset="([^"]*)" data-commit="([^"]*)" '
                      r'data-path="([^"]*)"', document)
    return match.groups() if match else None


def pin_commit(data, commit):
    """Bytes of a prepared page with the dataset commit placeholder replaced."""
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Expected a full dataset commit')
    return data.replace(COMMIT_PLACEHOLDER.encode(), commit.encode())


def signin_page(default_return=''):
    """The sign-in page (no external libraries): PKCE against the Space's HF OAuth variables."""
    if default_return and not re.fullmatch(r'/[A-Za-z0-9_./-]*', default_return):
        raise ValueError('Unsafe default return path')
    return ((SITE / SIGNIN_FILE).read_text(encoding='utf-8')
            .replace('__ORG_IDS__', ORG_IDS).replace('__DEFAULT_RETURN__', default_return))


_LINK = re.compile(r'''(?P<attr>\b(?:href|src))=(?P<q>["'])(?P<url>[^"'#?]*)(?P<rest>[^"']*)(?P=q)''', re.I)


def repoint_links(document, page, run, moved, commit, repo=DATASET_REPO):
    """Rewrite links of a page (``page``: its path relative to the run) to moved files (``moved``: run-relative
    paths now in the dataset) to the dataset file page, opened in a new tab, with a visible note.
    Returns (document, [repointed run-relative paths])."""
    base = PurePosixPath(page).parent
    changed = []

    def replace(match):
        url = match['url']
        if not url or re.match(r'[a-z][a-z0-9+.-]*:|//', url, re.I):
            return match[0]
        target = _normalise(base / url)
        if target is None or target not in moved:
            return match[0]
        changed.append(target)
        href = file_url(dataset_path(run, target), commit, repo)
        return (f'{match["attr"]}="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer" '
                f'{NOTE_MARKER}="{html.escape(target, quote=True)}" title="Stored in the private logs dataset '
                f'(sign in to huggingface.co with read access)"')
    document = _LINK.sub(replace, document)
    if changed and 'id="dataset-file-note"' not in document:
        note = ('<style id="dataset-file-note">a[data-dataset-file]::after{content:" (in the private logs '
                'dataset)";font-size:.85em;opacity:.75}</style>')
        head = re.search(r'</head>', document, re.I)
        document = document[:head.start()] + note + document[head.start():] if head else note + document
    return document, changed


def offline_links(document, page, offline):
    """Point links of a page (``page`` relative to the run) to files kept only offline (``offline``:
    run-relative paths pruned from the Space without a dataset copy) at the run page's "Raw records kept
    offline" notice. Returns (document, [noted run-relative paths])."""
    import posixpath
    base = PurePosixPath(page).parent
    # Rooted paths: relpath of two absolute paths never consults the process's working directory.
    notice = posixpath.relpath('/run/results.html', posixpath.normpath('/run/' + base.as_posix())) \
        + '#raw-records-offline'
    noted = []

    def replace(match):
        url = match['url']
        if not url or re.match(r'[a-z][a-z0-9+.-]*:|//', url, re.I):
            return match[0]
        target = _normalise(base / url)
        if target is None or target not in offline:
            return match[0]
        noted.append(target)
        return (f'{match["attr"]}="{html.escape(notice, quote=True)}" data-offline-file="'
                f'{html.escape(target, quote=True)}" title="Kept offline by the researchers (not in this Space)"')
    return _LINK.sub(replace, document), noted


def _normalise(path):
    parts = []
    for part in PurePosixPath(path).parts:
        if part == '..':
            if not parts:
                return None
            parts.pop()
        elif part not in ('.', ''):
            parts.append(part)
    return '/'.join(parts) if parts else None


def moved_links(document, page, moved):
    """Same-site links of a page that still point at a moved file directly (should be empty)."""
    base = PurePosixPath(page).parent
    found = []
    for match in _LINK.finditer(document):
        url = match['url']
        if not url or re.match(r'[a-z][a-z0-9+.-]*:|//', url, re.I) or NOTE_MARKER in match[0]:
            continue
        target = _normalise(base / url)
        if target in moved:
            found.append(target)
    return found


# --- dataset commits ----------------------------------------------------------------------------------------

def dataset_inventory(api, repo, revision):
    """path -> (bytes, sha256 or None) of every file of the dataset at ``revision``."""
    rows = {}
    for item in api.list_repo_tree(repo, repo_type='dataset', revision=revision, recursive=True):
        if hasattr(item, 'blob_id'):
            lfs = getattr(item, 'lfs', None)
            rows[item.path] = (item.size, lfs.sha256 if lfs else None, item.blob_id)
    return rows


def _git_sha1(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def remote_matches(record, entry):
    """True when a dataset tree entry (bytes, lfs sha256, blob id) holds the bytes of ``record``
    ({bytes, sha256, [git_sha1]})."""
    size, lfs, blob = entry
    if size != record['bytes']:
        return False
    if lfs is not None:
        return lfs == record['sha256']
    return record.get('git_sha1') is not None and blob == record['git_sha1']


def commit_files(api, files, *, operation, message, repo=DATASET_REPO, progress=None, max_bytes=2_000_000_000):
    """Add ``files`` ({dataset path: Path or bytes}) to the private dataset in guarded commits pinned to the
    head, then verify every path (size and LFS SHA-256, or the Git blob id of a small file). Files already
    present with identical bytes are skipped; a present file with other bytes is refused (dataset files
    are immutable). Returns {revision, added, already_present, verified}."""
    info = hf_visibility.check_dataset(api, repo)
    records = {}
    for name, data in files.items():
        if PurePosixPath(name).is_absolute() or any(p in ('', '.', '..') or p.startswith('.')
                                                     for p in name.split('/')):
            raise ValueError('Unsafe dataset path: ' + name)
        payload = data.read_bytes() if isinstance(data, Path) else data
        records[name] = {**digest(payload), 'git_sha1': _git_sha1(payload)}
    present = dataset_inventory(api, repo, info.sha)
    pending, existing = [], []
    for name in sorted(files):
        if name in present:
            if not remote_matches(records[name], present[name]):
                raise ValueError('Dataset already holds different bytes at ' + name)
            existing.append(name)
        else:
            pending.append(name)
    revision = info.sha
    batch, size = [], 0
    groups = []
    for name in pending:
        length = records[name]['bytes']
        if batch and size + length > max_bytes:
            groups.append(batch)
            batch, size = [], 0
        batch.append(name)
        size += length
    if batch:
        groups.append(batch)
    for number, group in enumerate(groups, 1):
        current = api.repo_info(repo, repo_type='dataset')
        if not hf_visibility.unchanged(current, repo, 'dataset', revision):
            raise ValueError('Dataset changed concurrently or its visibility differs from the record (it must stay '
                             'private); run again')
        operations = [operation(path_in_repo=n, path_or_fileobj=str(files[n]) if isinstance(files[n], Path)
                                else files[n]) for n in group]
        response = api.create_commit(repo_id=repo, repo_type='dataset', parent_commit=revision,
                                     commit_message=message + (f' ({number}/{len(groups)})' if len(groups) > 1 else ''),
                                     operations=operations)
        revision = response.oid
        if progress:
            progress(revision, group)
    final = dataset_inventory(api, repo, revision)
    bad = [n for n in files if n not in final or not remote_matches(records[n], final[n])]
    if bad:
        raise ValueError('Dataset files missing or differing after the commit: ' + ', '.join(bad[:20]))
    return {'revision': revision, 'added': pending, 'already_present': existing, 'verified': len(files),
            'records': {n: {'bytes': r['bytes'], 'sha256': r['sha256']} for n, r in records.items()}}


def ranged_read(session, headers, url, start, end):
    """(status, bytes) of a Range request for bytes start..end inclusive, following HF's redirect."""
    response = session.get(url, headers={**headers, 'Range': f'bytes={start}-{end}'}, timeout=60,
                           follow_redirects=True)
    return response.status_code, response.content


def check_ranges(session, headers, repo, commit, path, data_head, data_tail, size):
    """Read the first and last 64 bytes of a dataset file through resolve with the operator's login;
    both must answer 206 with the expected bytes."""
    url = file_url(path, commit, repo, 'resolve')
    first = ranged_read(session, headers, url, 0, min(63, size - 1))
    last = ranged_read(session, headers, url, max(0, size - 64), size - 1)
    return {'first_status': first[0], 'last_status': last[0],
            'ok': first[0] == 206 and last[0] == 206 and first[1] == data_head and last[1] == data_tail}


def check_preflight(session, origin, repo, commit, path):
    """A CORS preflight from the Space origin for a Range GET with Authorization must be allowed."""
    response = session.request('OPTIONS', file_url(path, commit, repo, 'resolve'), timeout=30, headers={
        'Origin': origin, 'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'authorization,range'})
    allowed = {h.strip().lower() for h in response.headers.get('access-control-allow-headers', '').split(',')}
    allow_origin = response.headers.get('access-control-allow-origin')
    ok = (response.status_code in (200, 204) and allow_origin in (origin, '*')
          and {'authorization', 'range'} <= allowed)
    return {'status': response.status_code, 'allow_origin': allow_origin, 'allow_headers': sorted(allowed), 'ok': ok}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n')
