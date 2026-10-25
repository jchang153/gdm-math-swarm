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
the simplified judge's page; the run page's summary tile, Run table row and Behavior judge line show the
simplified judge's counts instead of "not run" (owner, October 25, 2026). The catalogue entry gains a "Simple
behavior judge" link (the "Behavior judge" link still means the rubric-3.1 judge) and its Behavior judge column
shows both judges, labelled "rubric 3.1" and "simple" (simple_summary; entries published earlier read the
counts from the published report.json in the browser). The facts of results.json are unchanged. No model calls, network or credentials.
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


def _strip_local_paths(value):
    if isinstance(value, dict):
        return {k: _strip_local_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_local_paths(v) for v in value]
    if isinstance(value, str) and value.startswith('/'):  # an absolute host path: keep only the run-relative tail
        tail = value.split('/runs/', 1)[1] if '/runs/' in value else value.rsplit('/', 1)[-1]
        return '<host>/' + tail
    return value


def space_report_json(data):
    """report.json as published on the (public) Space: absolute host paths reduced to their run-relative tail
    (or file name), everything else unchanged."""
    report = json.loads(data)
    return (json.dumps(_strip_local_paths(report), indent=1, ensure_ascii=False) + '\n').encode('utf-8')


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


def _render_html(report, rows, old_judge_href, run_start=None):
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:  # research/ lives at the repository root
        sys.path.insert(0, root)
    from research.analysis.simple_judge.report import render_html
    return render_html(report, rows, old_judge_href=old_judge_href, run_start=run_start)


def site_page(report_bytes, agents_bytes, run, old_judge=False, run_start=None):
    """The published report.html: re-rendered from report.json and agents.jsonl with the current report
    generator (comparability note, theme toggle support, filterable agents table; old_judge adds the link to the
    rubric-3.1 judge; run_start, epoch seconds from run_clock.run_start, shows the per-agent times as run clock
    plus UTC).
    Refuses a report of another run, of another schema or of a mock (rehearsal) run."""
    report = json.loads(report_bytes)
    if report.get('schema') != REPORT_SCHEMA:
        raise ValueError(f'{SJ_DIR}/report.json must have schema {REPORT_SCHEMA}')
    if report.get('run_id') != run:
        raise ValueError(f'{SJ_DIR}/report.json belongs to run {report.get("run_id")!r}')
    if report.get('mode') != 'paid':
        raise ValueError(f'Refusing to publish simplified judge outputs in mode {report.get("mode")!r}')
    rows = [json.loads(line) for line in agents_bytes.decode('utf-8').splitlines() if line.strip()]
    page = _render_html(report, rows, OLD_FROM_SJ if old_judge else None, run_start)
    if UNPUBLISHED_LINK.search(page):
        raise ValueError(f'{SJ_REPORT} links simplified-judge files that are not published')
    return page


def _block(document, start, end):
    opening = re.search(start, document)
    if not opening:
        return None
    closing = document.find(end, opening.end())
    return None if closing < 0 else (opening.end(), closing)


def simple_summary(report):
    """Counts of a simplified-judge report.json (dict) for the catalogue and the run page, or None when the
    report is not a valid gdm-simple-judge/1 report: agents, labelled, cheating_any (explored or further),
    levels (each cheating level), whistleblowers, evaluation_aware and mode. Counts only, no finding text."""
    if not isinstance(report, dict) or report.get('schema') != REPORT_SCHEMA:
        return None
    counts = report.get('counts')
    if not isinstance(counts, dict):
        return None
    number = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None  # noqa: E731
    levels = counts.get('cheating_level')
    if not isinstance(levels, dict) or any(not isinstance(k, str) or number(v) is None for k, v in levels.items()):
        return None
    flags = {}
    for key in ('whistleblower', 'evaluation_aware'):
        value = counts.get(key)
        if not isinstance(value, dict) or any(number(v) is None for v in value.values()):
            return None
        flags[key] = value.get('True', 0)
    out = {'mode': report.get('mode') if isinstance(report.get('mode'), str) else None,
           'agents': number(counts.get('agents')), 'labelled': number(counts.get('labelled')),
           'cheating_any': number(counts.get('cheating_any')), 'levels': {k: levels[k] for k in levels},
           'whistleblowers': flags['whistleblower'], 'evaluation_aware': flags['evaluation_aware']}
    if None in (out['agents'], out['labelled'], out['cheating_any']):
        return None
    return out


SHOWN_LEVELS = ('explored', 'submitted', 'credited')


def simple_text(summary):
    """(main, detail) plain text of simple_summary counts, e.g. ('25 submitted · 2 credited · 2 whistleblowers',
    'of 100 labelled'); the catalogue's JavaScript writes the same words."""
    parts = [f'{summary["levels"][k]} {k}' for k in SHOWN_LEVELS if summary['levels'].get(k)]
    if summary['whistleblowers']:
        parts.append(f'{summary["whistleblowers"]} whistleblower' + ('s' if summary['whistleblowers'] != 1 else ''))
    detail = f'of {summary["labelled"]} labelled' + (f' ({summary["mode"]})' if summary.get('mode') not in (None, 'paid')
                                                     else '')
    return ' · '.join(parts) or 'none flagged', detail


def simple_report(run_dir):
    """The parsed RUN/analysis/simple_judge/report.json of a run with a completed simplified judge, or None."""
    if not has_simple_judge(run_dir):
        return None
    try:
        return json.loads((Path(run_dir) / SJ_DIR / 'report.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


SJ_SPOT = 'data-sj="1"'


def _judge_spot(document, pattern, inner, *, first, separator):
    """Replace the simplified-judge part of one run-page spot (the Run table row or the paragraph after the
    proof checker): ``pattern`` captures the spot's content; an earlier simplified-judge span is replaced, a
    "not run" content is dropped and a rubric-3.1 content is labelled as such, then ``inner`` follows."""
    match = re.search(pattern, document)
    if not match:
        return document
    content = match.group(1)
    at = content.find('<span ' + SJ_SPOT)
    if at >= 0:
        prefix = content[:at]
    elif content.strip() in ('not run', 'not&nbsp;run'):
        prefix = ''
    else:
        prefix = content + first + separator
    span = f'<span {SJ_SPOT}>{inner}</span>'
    return document[:match.start(1)] + prefix + span + document[match.end(1):]


def add_simple_judge_tab(document, report=None):
    """results.html with the simplified judge as its "Behavior judge" tab: the rubric-3.1 judge's tab (if
    any) becomes "Behavior judge (rubric 3.1)" after it, a "not run" placeholder is replaced, the tab help
    explains both, and the summary tile, the Run table's Behavior judge row and the Behavior judge line after
    the proof checker name the simplified judge (with its counts when ``report``, the parsed report.json, is
    given) instead of "not run"; a rubric-3.1 status there is kept and labelled "(rubric 3.1)". The facts of
    results.json are unchanged. Idempotent, also on a page that already has the tab (only those spots are
    updated); raises ValueError for a page without the run-page tab bar (regenerate it with the current
    build_results.py)."""
    if TAB_MARKER not in document:
        document = _add_tab(document)
    summary = simple_summary(report) if report is not None else None
    href = html.escape(SJ_REPORT, quote=True)
    if summary:
        main, detail = simple_text(summary)
        tile = (f'<div class="tile"><div class="k">{LABEL}</div><div class="v">{html.escape(main)}</div>'
                f'<div class="s">simplified judge, {html.escape(detail)}; see the Behavior judge tab</div></div>')
        inner = f'simplified judge: {html.escape(main)}, {html.escape(detail)} (<a href="{href}">report</a>)'
    else:
        tile = TILE
        inner = f'simplified judge completed (<a href="{href}">report</a>)'
    document = re.sub(r'<div class="tile"><div class="k">' + LABEL + r'</div><div class="v">[^<]*</div>'
                      r'<div class="s">[^<]*</div></div>', lambda _: tile, document, count=1)
    document = _judge_spot(document, r'<tr><td>' + LABEL + r'</td><td>(.*?)</td></tr>', inner,
                           first=' (rubric 3.1)', separator=' · ')
    return _judge_spot(document, r'<p>' + LABEL + r': (.*?)</p>', inner, first=' (rubric 3.1)', separator='; ')


def _add_tab(document):
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
    return document


def add_rubric_tab(document):
    """A run page that has the simplified judge's tab but not the rubric-3.1 judge's (for example
    glm100-repeat2, whose rubric-3.1 judge was published after the simplified judge's tab was added, so it was
    reachable only as "Behavior judge report" / "Evidence browser" links) gains the "Behavior judge (rubric 3.1)"
    tab right after the Behavior judge tab, and its help line. Call it only when the run's rubric-3.1 judge page
    (analysis/judge/report.html) exists. Idempotent; a page without the simplified judge's tab is unchanged."""
    nav = _block(document, r'<nav class="tabs"[^>]*>', '</nav>')
    if nav is None or TAB not in document[nav[0]:nav[1]] or OLD_MARKER in document[nav[0]:nav[1]]:
        return document
    start, end = nav
    bar = document[start:end].replace(TAB, TAB + OLD_TAB, 1)
    document = document[:start] + bar + document[end:]
    helped = _block(document, r'<ul class="tabhelp"[^>]*>', '</ul>')
    if helped is not None and OLD_MARKER not in document[helped[0]:helped[1]]:
        start, end = helped
        lines = document[start:end]
        entry = re.search(r'<li ' + re.escape(TAB_MARKER) + r'>.*?</li>', lines)
        at = entry.end() if entry else len(lines)
        document = document[:start] + lines[:at] + OLD_HELP + lines[at:] + document[end:]
    return document


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
