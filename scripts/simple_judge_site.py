"""The run's simplified behavior judge on the static site: which files go where, its run-page tab and
its catalogue link.

The simplified one-pass judge (research/analysis/simple_judge/, the default judge by owner decision,
October 24, 2026) writes RUN/analysis/simple_judge/. On the site:

- **Space**: ``report.html`` (re-rendered from report.json and agents.jsonl with the current
  research/analysis/simple_judge/report.py, so it carries the comparability note and the site theme) and
  ``report.json``.
- **Private logs dataset**: ``agents.jsonl`` (one row per agent, about 200 KB), like every raw ``.jsonl``
  record (dataset_logs category ``jsonl``); no page loads it.
- **Local only, never the Space or the dataset**: ``cases.jsonl`` (case files with the full text of every
  cited record, 13 to 42 MB), ``raw/`` (request and response archives), ``run-budgets.json``,
  ``adjudication.json``, lock files, rehearsal ledgers and anything else in the folder.

The run page (results.html) gets the simplified judge as its "Behavior judge" tab; on a run that also has
the rubric-3.1 judge, that judge stays reachable as a "Behavior judge (rubric 3.1)" tab and from a link in
the simplified judge's page. The catalogue entry gains a "Simple behavior judge" link (the catalogue's
"Behavior judge" link and column keep meaning the rubric-3.1 judge, whose report.json feeds them). The
facts of results.html are unchanged. No model calls, network or credentials.
"""
import html
import json
from pathlib import Path
import re
import sys

SJ_DIR = 'analysis/simple_judge'
SPACE_FILES = ('report.html', 'report.json')
DATASET_FILES = ('agents.jsonl',)
PUBLISHED = SPACE_FILES + DATASET_FILES
SJ_REPORT = SJ_DIR + '/report.html'
OLD_REPORT = 'analysis/judge/report.html'
OLD_FROM_SJ = '../judge/report.html'  # the rubric-3.1 judge's page, relative to the simplified judge's page
LABEL = 'Behavior judge'
OLD_LABEL = 'Behavior judge (rubric 3.1)'
CATALOGUE_LABEL = 'Simple behavior judge'
REPORT_SCHEMA = 'gdm-simple-judge/1'
HELP_TEXT = ('The simplified behavior judge (default since October 24, 2026): one model call per agent labels '
             'cheating, whistleblowing and evaluation awareness, with quotes checked word for word by code; '
             'researcher-only. Its evaluation-awareness counts are not comparable with the rubric-3.1 '
             'judge\'s (it finds many more).')
OLD_HELP_TEXT = ('The earlier rubric-3.1 judge\'s reading of the transcripts (multi-stage, model-generated, '
                 'verified by a second model call); researcher-only.')
TAB_MARKER = 'data-tab="simple-judge"'
OLD_MARKER = 'data-tab="judge-rubric-3-1"'
TAB = f'<a {TAB_MARKER} href="{SJ_REPORT}">{LABEL}</a>'
OLD_TAB = f'<a {OLD_MARKER} href="{OLD_REPORT}">{html.escape(OLD_LABEL)}</a>'
HELP = f'<li {TAB_MARKER}><strong>{LABEL}</strong>: {html.escape(HELP_TEXT)}</li>'
OLD_HELP = f'<li {OLD_MARKER}><strong>{html.escape(OLD_LABEL)}</strong>: {html.escape(OLD_HELP_TEXT)}</li>'
TILE = (f'<div class="tile"><div class="k">{LABEL}</div><div class="v">completed</div>'
        '<div class="s">simplified judge; see the Behavior judge tab</div></div>')
# A published simplified-judge page must not link the files that stay local.
UNPUBLISHED_LINK = re.compile(r'''(?:href|src)\s*=\s*["'][^"']*(?:\braw/|(?:cases|run-budgets|adjudication)\.)''',
                              re.I)


def simple_judge_excluded(relative):
    """True for paths under analysis/simple_judge/ that never leave this machine (everything except
    report.html, report.json and agents.jsonl directly in the folder)."""
    parts = Path(relative).parts
    if parts[:2] != ('analysis', 'simple_judge') or len(parts) == 2:
        return False
    return len(parts) != 3 or parts[2] not in PUBLISHED


def simple_judge_files(run_dir):
    """The run's publishable simplified-judge files, or [] when it has no completed simplified judge
    (report.json and agents.jsonl, the inputs of the published report.html). Symlinks are refused."""
    folder = Path(run_dir) / SJ_DIR
    if folder.is_symlink():
        raise ValueError(f'Refusing symlink: {SJ_DIR}')
    names = []
    for name in PUBLISHED:
        path = folder / name
        if path.is_symlink():
            raise ValueError(f'Refusing symlink: {SJ_DIR}/{name}')
        if path.is_file():
            names.append(name)
    return names if {'report.json', 'agents.jsonl'} <= set(names) else []


def has_simple_judge(run_dir):
    return bool(simple_judge_files(run_dir))


def _render_html(report, rows, old_judge_href):
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:  # research/ lives at the repository root
        sys.path.insert(0, root)
    from research.analysis.simple_judge.report import render_html
    return render_html(report, rows, old_judge_href=old_judge_href)


def site_page(report_bytes, agents_bytes, run, old_judge=False):
    """The published report.html: re-rendered from report.json and agents.jsonl with the current report
    generator (comparability note, theme toggle support; old_judge adds the link to the rubric-3.1 judge).
    Refuses a report of another run, of another schema or of a mock (rehearsal) run."""
    report = json.loads(report_bytes)
    if report.get('schema') != REPORT_SCHEMA:
        raise ValueError(f'{SJ_DIR}/report.json must have schema {REPORT_SCHEMA}')
    if report.get('run_id') != run:
        raise ValueError(f'{SJ_DIR}/report.json belongs to run {report.get("run_id")!r}')
    if report.get('mode') != 'paid':
        raise ValueError(f'Refusing to publish simplified judge outputs in mode {report.get("mode")!r}')
    rows = [json.loads(line) for line in agents_bytes.decode('utf-8').splitlines() if line.strip()]
    page = _render_html(report, rows, OLD_FROM_SJ if old_judge else None)
    if UNPUBLISHED_LINK.search(page):
        raise ValueError(f'{SJ_REPORT} links simplified-judge files that are not published')
    return page


def _block(document, start, end):
    opening = re.search(start, document)
    if not opening:
        return None
    closing = document.find(end, opening.end())
    return None if closing < 0 else (opening.end(), closing)


def add_simple_judge_tab(document):
    """results.html with the simplified judge as its "Behavior judge" tab: the rubric-3.1 judge's tab (if
    any) becomes "Behavior judge (rubric 3.1)" after it, a "not run" placeholder is replaced, the tab help
    explains both, and the summary tile names the simplified judge. Idempotent; raises ValueError for a page
    without the run-page tab bar (regenerate it with the current build_results.py)."""
    if TAB_MARKER in document:
        return document
    nav = _block(document, r'<nav class="tabs"[^>]*>', '</nav>')
    if nav is None:
        raise ValueError('results.html has no run-page tab bar; regenerate it with the current build_results.py')
    start, end = nav
    bar = document[start:end]
    old = re.search(r'<a [^>]*href="' + re.escape(OLD_REPORT) + r'"[^>]*>[^<]*</a>', bar)
    off = re.search(r'<span class="off">' + LABEL + r'[^<]*</span>', bar)
    if old:
        bar = bar[:old.start()] + TAB + OLD_TAB + bar[old.end():]
    elif off:
        bar = bar[:off.start()] + TAB + bar[off.end():]
    else:
        later = re.search(r'<a [^>]*(?:data-tab="incident"|href="inspect-view/)', bar)
        at = later.start() if later else len(bar)
        bar = bar[:at] + TAB + bar[at:]
    document = document[:start] + bar + document[end:]
    helped = _block(document, r'<ul class="tabhelp"[^>]*>', '</ul>')
    if helped is not None:
        start, end = helped
        lines = document[start:end]
        entry = re.search(r'<li><strong>' + LABEL + r'</strong>:.*?</li>', lines)
        markup = HELP + (OLD_HELP if old else '')
        if entry:
            lines = lines[:entry.start()] + markup + lines[entry.end():]
        else:
            later = re.search(r'<li data-tab="incident"|<li><strong>Inspect</strong>', lines)
            at = later.start() if later else len(lines)
            lines = lines[:at] + markup + lines[at:]
        document = document[:start] + lines + document[end:]
    return re.sub(r'<div class="tile"><div class="k">' + LABEL + r'</div><div class="v">[^<]*</div>'
                  r'<div class="s">[^<]*</div></div>', TILE, document, count=1)


def with_simple_judge_link(links, run):
    """Catalogue links with 'Simple behavior judge' right after 'Behavior judge' (else before the Incident
    report or raw Inspect log link(s), else last); any earlier simple-judge link is replaced."""
    from urllib.parse import quote
    target = quote(run, safe='') + '/' + SJ_REPORT
    rest = {label: url for label, url in links.items() if label != CATALOGUE_LABEL}
    labels = list(rest)
    if 'Behavior judge' in rest:
        at = labels.index('Behavior judge') + 1
    else:
        later = [i for i, label in enumerate(labels)
                 if label in ('Incident report', 'Inspect logs') or label.startswith('Inspect log ')]
        at = later[0] if later else len(labels)
    labels.insert(at, CATALOGUE_LABEL)
    return {label: target if label == CATALOGUE_LABEL else rest[label] for label in labels}
