"""The agent story map on the static site: which files are published, how they are screened, and the run-page tab.

Owner decision, November 2, 2026 (docs/plan.md, "Story maps on the public site (November 2, 2026)"): the story map
pages of research/analysis/storymap/view.py are published. Only the pages and the validation summary go to the site:
RUN/analysis/storymap/index.html, events.html, validation.html, validation.md and agents/<agent>.html. Everything
else in the folder stays local: the stage-0 timelines (timeline/, a private category), stage1/-stage3/, units/,
raw/, crosscheck/, bakeoff/, map.jsonl, pipeline.json, validation.json and run-budgets.json. The pages are rendered
for the site with ``--taxonomy ''`` (code letters only; the researcher-only taxonomy names stay local), and the
validation summary loses the path of the private hand-label reference. The run page (results.html) gains a
"Story map" tab before Inspect. No model calls, network or credentials.
"""
import html
from pathlib import Path
import re

from incident_site import _insert

STORYMAP_DIR = 'analysis/storymap'
STORYMAP_FILES = ('index.html', 'events.html', 'validation.html', 'validation.md')
AGENT_PAGE = re.compile(r'agents/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.html')
STORYMAP_INDEX = STORYMAP_DIR + '/index.html'
STORYMAP_LABEL = 'Story map'
STORYMAP_HELP = ('Per-agent map of reasoning about cheating next to what each agent read, saw and did, as read by '
                 'models (stage 1 Claude Haiku 5.5, stages 2-3 GPT-5.6 Terra); model readings are interpretations, '
                 'not ground truth.')
TAB_MARKER = 'data-tab="storymap"'
TAB = f'<a {TAB_MARKER} href="{STORYMAP_INDEX}">{STORYMAP_LABEL}</a>'
HELP = f'<li {TAB_MARKER}><strong>{STORYMAP_LABEL}</strong>: {html.escape(STORYMAP_HELP)}</li>'
# A public story map page carries no researcher-only taxonomy names (rendered with --taxonomy ''): every thought
# lane is labelled "thought X: codes".
TAXONOMY_LANE = re.compile(r'>thought [A-G]: (?!codes<)')
# Host paths never appear in a published file. The agents' own container home (/home/researcher, quoted from
# their transcripts) is not a host path and may appear.
HOST_PATH = re.compile(rb'/(?:Users|private/tmp|private/var|var/folders)/')
# The hand-label reference is researcher-only (private/); its path is removed from the validation summary.
# The whole line goes (Markdown hard break or <br>), so the public summary does not mention the reference at all.
REFERENCE_PATH = re.compile(r'Reference: (?:`|<code>)private/[^`<\s]*(?:`|</code>)(?: *\n|<br>)?')
REFERENCE_PUBLIC = ''
PRIVATE_PATH = re.compile(rb'(?<![A-Za-z0-9_./-])private/')
HREF = re.compile(r'''\b(?:href|src)=["']([^"'#?]*)''', re.I)


def published(relative):
    """True for a path inside RUN/analysis/storymap/ (relative to that folder) that goes to the site."""
    return relative in STORYMAP_FILES or bool(AGENT_PAGE.fullmatch(relative))


def storymap_excluded(relative):
    """True for run paths under analysis/storymap/ outside the publication allowlist (prepare_hf_library)."""
    parts = Path(relative).parts
    if parts[:2] != ('analysis', 'storymap') or len(parts) == 2:
        return False
    rest = parts[2:]
    return not (rest == ('agents',) or published('/'.join(rest)))


def public_copy(relative, data):
    """The published bytes of a story map file: the validation summary without the private reference path."""
    if relative.startswith('validation.'):
        return REFERENCE_PATH.sub(REFERENCE_PUBLIC, data.decode('utf-8')).encode('utf-8')
    return data


def page_problems(relative, data, names, taxonomy=()):
    """Why a story map file may not be published (empty if it may). ``names``: the published folder's files;
    ``taxonomy``: {code: researcher-only name}; a name labelling its code (``C1: name``, ``C1 &middot; name``) is
    refused (names are never echoed in the result; an agent quote that happens to contain one is allowed)."""
    problems = []
    if HOST_PATH.search(data):
        problems.append('host path')
    if PRIVATE_PATH.search(data):
        problems.append('path into private/')
    text = data.decode('utf-8', 'replace')
    if any(name and re.search(r'\b' + re.escape(code) + r'(?::|\s*(?:&middot;|\u00b7|[-\u2013\u2014]))\s*'
                              + re.escape(html.escape(name, quote=False)), text)
           for code, name in dict(taxonomy).items()):
        problems.append('researcher-only taxonomy name next to its code')
    if relative.endswith('.html'):
        if TAXONOMY_LANE.search(text):
            problems.append('researcher-only taxonomy names (render with --taxonomy \'\')')
        folder = relative.rsplit('/', 1)[0] + '/' if '/' in relative else ''
        for url in HREF.findall(text):
            if not url or re.match(r'[a-z][a-z0-9+.-]*:|//', url, re.I):
                continue
            parts = []
            for part in (folder + url).split('/'):
                if part == '..':
                    if not parts:
                        parts = None
                        break
                    parts.pop()
                elif part not in ('', '.'):
                    parts.append(part)
            target = '/'.join(parts) if parts is not None else None
            if target is None or target not in names:
                problems.append(f'links {url}, which is not published')
    return problems


def public_files(folder, taxonomy=()):
    """{relative: bytes} of a story map folder's publishable files (public copies), after the screens. Raises
    ValueError for symlinks, a missing index.html or any problem; files off the allowlist are ignored."""
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f'{folder} must be a real directory')
    files = {}
    for path in sorted(folder.rglob('*')):
        relative = path.relative_to(folder).as_posix()
        if not published(relative):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Refusing non-regular story map file: {relative}')
        files[relative] = public_copy(relative, path.read_bytes())
    if 'index.html' not in files:
        raise ValueError(f'{folder} has no index.html')
    problems = [f'{name}: {problem}' for name, data in files.items()
                for problem in page_problems(name, data, files, taxonomy)]
    if problems:
        raise ValueError('Story map not publishable:\n  ' + '\n  '.join(problems[:20]))
    return files


def add_storymap_tab(document):
    """results.html with the Story map tab (before Inspect) and its one-line explanation. Idempotent; raises
    ValueError for a page without the run-page tab bar."""
    if TAB_MARKER in document:
        return document
    tabbed = _insert(document, r'<nav class="tabs"[^>]*>', '</nav>', r'<a [^>]*href="inspect-view/', TAB)
    if tabbed is None:
        raise ValueError('results.html has no run-page tab bar; regenerate it with the current build_results.py')
    helped = _insert(tabbed, r'<ul class="tabhelp"[^>]*>', '</ul>', r'<li><strong>Inspect</strong>', HELP)
    return helped if helped is not None else tabbed
