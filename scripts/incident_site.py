"""The run's incident report on the static site: which files are published, its run-page tab and
its catalogue link.

The incident report (docs/plan.md, "Incident report per run (October 21, 2026)") writes
RUN/analysis/incident/. Only report.html, report.md and evidence.json are published; everything
else there stays local (raw/ provider archives, units/ cached model units, route-pricing/,
digest.json, run-budgets.json, state.json and lock files). The run page (results.html) gains an
"Incident report" navigation tab and the catalogue entry an "Incident report" link; the
report's content and the facts of results.html are unchanged. No model calls, network or
credentials.
"""
import html
from pathlib import Path
import re

INCIDENT_DIR = 'analysis/incident'
INCIDENT_FILES = ('report.html', 'report.md', 'evidence.json')
INCIDENT_REPORT = INCIDENT_DIR + '/report.html'
INCIDENT_LABEL = 'Incident report'
INCIDENT_HELP = ('A model-written narrative of what happened in this run (Claude Opus 5.5), with excerpts checked '
                 'word for word against the records and linked into Inspect View; researcher-only.')
TAB_MARKER = 'data-tab="incident"'
TAB = f'<a {TAB_MARKER} href="{INCIDENT_REPORT}">{INCIDENT_LABEL}</a>'
HELP = f'<li {TAB_MARKER}><strong>{INCIDENT_LABEL}</strong>: {html.escape(INCIDENT_HELP)}</li>'
# A published incident page must not link the incident files that stay local.
UNPUBLISHED_LINK = re.compile(r'''(?:href|src)\s*=\s*["'][^"']*(?:\b(?:raw|units|route-pricing)/|'''
                              r'''(?:digest|state|run-budgets)\.json)''', re.I)


def incident_excluded(relative):
    """True for paths under analysis/incident/ outside the publication allowlist."""
    parts = Path(relative).parts
    if parts[:2] != ('analysis', 'incident') or len(parts) == 2:
        return False
    return len(parts) != 3 or parts[2] not in INCIDENT_FILES


def incident_files(run_dir):
    """The run's publishable incident files (names in INCIDENT_FILES that are regular files), or []
    when it has no incident report.html. Symlinks are refused."""
    folder = Path(run_dir) / INCIDENT_DIR
    if folder.is_symlink():
        raise ValueError(f'Refusing symlink: {INCIDENT_DIR}')
    report = folder / 'report.html'
    if not report.is_file() and not report.is_symlink():
        return []
    names = []
    for name in INCIDENT_FILES:
        path = folder / name
        if path.is_symlink():
            raise ValueError(f'Refusing symlink: {INCIDENT_DIR}/{name}')
        if path.is_file():
            names.append(name)
    return names


def has_incident_report(run_dir):
    return bool(incident_files(run_dir))


def unpublished_links(page_text):
    """True when an incident page links incident files that stay local."""
    return bool(UNPUBLISHED_LINK.search(page_text))


def _insert(document, start, end, before, markup):
    """Insert markup inside the first start...end block: before the first `before` match, else at end."""
    opening = re.search(start, document)
    if not opening:
        return None
    closing = document.find(end, opening.end())
    if closing < 0:
        return None
    found = re.compile(before).search(document, opening.end(), closing) if before else None
    at = found.start() if found else closing
    return document[:at] + markup + document[at:]


def add_incident_tab(document):
    """results.html with the Incident report tab (after Behavior judge, before Inspect) and its one-line
    explanation. Idempotent; raises ValueError for a page without the run-page tab bar (regenerate it with
    the current build_results.py)."""
    if TAB_MARKER in document:
        return document
    tabbed = _insert(document, r'<nav class="tabs"[^>]*>', '</nav>', r'<a [^>]*href="inspect-view/', TAB)
    if tabbed is None:
        raise ValueError('results.html has no run-page tab bar; regenerate it with the current build_results.py')
    helped = _insert(tabbed, r'<ul class="tabhelp"[^>]*>', '</ul>', r'<li><strong>Inspect</strong>', HELP)
    return helped if helped is not None else tabbed


def with_incident_link(links, run):
    """Catalogue links with 'Incident report' placed before the raw Inspect log link(s) (else last);
    any earlier incident link is replaced."""
    from urllib.parse import quote
    target = quote(run, safe='') + '/' + INCIDENT_REPORT
    out, placed = {}, False
    for label, url in links.items():
        if label == INCIDENT_LABEL:
            continue
        if not placed and (label == 'Inspect logs' or label.startswith('Inspect log ')):
            out[INCIDENT_LABEL], placed = target, True
        out[label] = url
    if not placed:
        out[INCIDENT_LABEL] = target
    return out
