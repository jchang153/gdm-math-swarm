"""The agent story map on the static site: which files are published, how they are screened, and the run-page tab.

Owner decision, November 2, 2026 (docs/plan.md, "Story maps on the public site (November 2, 2026)"): the story map
pages of research/analysis/storymap/view.py are published. Only the pages and the validation summary go to the site:
RUN/analysis/storymap/index.html, events.html, validation.html, validation.md, taxonomy.html and agents/<agent>.html;
with overseer story maps (owner, November 5, 2026) also taxonomy_overseer.html, from the public overseer codebook
research/analysis/storymap/taxonomy_overseer.md (overseer pages are agents/<overseer>.html; the overseer prompts,
private/.../prompts_overseer.md, are screened like the solver prompts). The experimental interaction graph (owner, November 5,
2026; docs/plan.md "Story-map interaction graph") is graph.html, built by scripts/storymap_graph.py from the same
public readings plus conference records, and screened like the other pages.
Owner, November 2, 2026 (docs/plan.md): the taxonomy (codes, names, definitions, examples) is public; its committed
copy research/analysis/storymap/taxonomy.md is rendered as taxonomy.html and its names appear on every page. Everything
else in the folder stays local: the stage-0 timelines (timeline/, a private category), stage1/-stage3/, units/,
raw/, crosscheck/, bakeoff/, map.jsonl, pipeline.json, validation.json and run-budgets.json. The stage prompts
(private/.../prompts.md), INTERFACE.md and the hand-label references stay private: a page quoting a prompt line that is
not also taxonomy text is refused, as are host paths, paths into private/ and credential-shaped strings. The
validation summary loses the path of the private hand-label reference. The run page (results.html) gains a
"Story map" tab before Inspect. No model calls, network or credentials.
"""
import html
from pathlib import Path
import re

from incident_site import _insert

STORYMAP_DIR = 'analysis/storymap'
STORYMAP_FILES = ('index.html', 'events.html', 'validation.html', 'validation.md', 'taxonomy.html',
                  'taxonomy_overseer.html', 'graph.html')
AGENT_PAGE = re.compile(r'agents/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.html')
STORYMAP_INDEX = STORYMAP_DIR + '/index.html'
STORYMAP_LABEL = 'Story map'
STORYMAP_HELP = ('Per-agent map of reasoning about cheating next to what each agent read, saw and did, as read by '
                 'models (stage 1 Claude Haiku 5.5, stages 2-3 GPT-5.6 Terra); model readings are interpretations, '
                 'not ground truth.')
TAB_MARKER = 'data-tab="storymap"'
TAB = f'<a {TAB_MARKER} href="{STORYMAP_INDEX}">{STORYMAP_LABEL}</a>'
HELP = f'<li {TAB_MARKER}><strong>{STORYMAP_LABEL}</strong>: {html.escape(STORYMAP_HELP)}</li>'
REPO = Path(__file__).resolve().parents[1]
PROMPTS = REPO / 'private' / 'research' / 'analysis' / 'storymap' / 'prompts.md'
# Overseer story maps (owner, November 5, 2026): their prompts are private too, their codebook public.
PROMPT_FILES = (PROMPTS, PROMPTS.with_name('prompts_overseer.md'))
PUBLIC_TAXONOMY = REPO / 'research' / 'analysis' / 'storymap' / 'taxonomy.md'
PUBLIC_TAXONOMIES = (PUBLIC_TAXONOMY, PUBLIC_TAXONOMY.with_name('taxonomy_overseer.md'))
PROMPT_LINE_MIN = 40  # a prompt line this long (stripped) that is not taxonomy text must not appear on a page
# Credential-shaped strings (provider keys, tokens, private keys): never on a published page.
SECRET = re.compile(rb'sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{10,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}'
                    rb'|AIza[0-9A-Za-z_-]{35}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}'
                    rb'|-----BEGIN [A-Z ]*PRIVATE KEY-----')
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


OVERSEER_DIR = 'analysis/storymap-overseer'  # the overseer map's records (pipeline --overseer): never published


def storymap_excluded(relative):
    """True for run paths under analysis/storymap/ outside the publication allowlist (prepare_hf_library), and for
    everything under analysis/storymap-overseer/ (overseer timelines and stage records; the overseer pages are
    rendered into analysis/storymap/)."""
    parts = Path(relative).parts
    if parts[:2] == ('analysis', 'storymap-overseer'):
        return True
    if parts[:2] != ('analysis', 'storymap') or len(parts) == 2:
        return False
    rest = parts[2:]
    return not (rest == ('agents',) or published('/'.join(rest)))


def public_copy(relative, data):
    """The published bytes of a story map file: the validation summary without the private reference path."""
    if relative.startswith('validation.'):
        return REFERENCE_PATH.sub(REFERENCE_PUBLIC, data.decode('utf-8')).encode('utf-8')
    return data


def _paths(value):
    return [value] if isinstance(value, (str, Path)) else list(value)


def prompt_lines(prompts=PROMPT_FILES, taxonomy=PUBLIC_TAXONOMIES):
    """Stripped lines of the private stage prompts (at least PROMPT_LINE_MIN characters) that are not public
    taxonomy text; empty when this checkout has no prompts file. ``prompts`` and ``taxonomy`` are a path or several
    (default: the solver and overseer prompts; the solver and overseer codebooks); missing files are skipped."""
    lines, public = [], []
    for path in _paths(prompts):
        try:
            lines += Path(path).read_text(encoding='utf-8').splitlines()
        except OSError:
            continue
    if not lines:
        return frozenset()
    for path in _paths(taxonomy):
        try:
            public.append(Path(path).read_text(encoding='utf-8'))
        except OSError:
            continue
    public = '\n'.join(public)
    return frozenset(s for s in (x.strip() for x in lines)
                     if len(s) >= PROMPT_LINE_MIN and s not in public and not s.startswith(('#', '|', '```')))


def page_problems(relative, data, names, taxonomy=(), prompts=None):
    """Why a story map file may not be published (empty if it may). ``names``: the published folder's files.
    ``taxonomy`` is accepted for older callers and ignored: the taxonomy is public (owner, November 2, 2026).
    ``prompts``: private prompt lines (default: prompt_lines()); a page holding one, raw or HTML-escaped, is refused
    (the line is never echoed in the result)."""
    problems = []
    if HOST_PATH.search(data):
        problems.append('host path')
    if PRIVATE_PATH.search(data):
        problems.append('path into private/')
    if SECRET.search(data):
        problems.append('credential-shaped string')
    text = data.decode('utf-8', 'replace')
    lines = prompt_lines() if prompts is None else prompts
    if any(line in text or html.escape(line, quote=True) in text or html.escape(line, quote=False) in text
           for line in lines):
        problems.append('text of the private stage prompts')
    if relative.endswith('.html'):
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
