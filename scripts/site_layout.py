"""Denser, more readable layout for the library's run pages (owner request, October 29, 2026).

"Make the website a bit more digestible: narrow the width of the text, and put more panes in view at a time."
Running text is capped at a comfortable measure (about 75 characters) while tables, charts and card grids keep
the full width, and sections that used to stack one per row sit side by side in responsive grids.

``apply(name, document)`` is the single code path: generators call it on fresh pages (results.html,
conference view) and ``prepare_hf_library.py`` / ``publish_cloudflare.py restyle`` call it on every page they
publish, so a page already on the site is converted exactly like a fresh one. It only adds one
``<style id="site-layout">`` block (replaced, never duplicated) and wraps existing sections in grid containers;
it never changes text, links, scripts or data, and it is idempotent. Pages it does not know are returned as is.
Pure CSS in the ``site_style`` colour tokens (light/dark); no scripts, fonts or network.
"""
import html
import json
import re

import site_look
import site_nav
import site_terms

MARKER = 'id="site-layout"'
MEASURE = '70ch'
# Owner, October 29, 2026 ("doesn't look any narrower"): every page's content sits in one centred column.
COLUMN = '1120px'

# Phone width keeps every grid to one column (16px gutters come from site_style).
_PHONE = '@media (max-width:640px){'

RESULTS_CSS = (
    f'body{{max-width:{COLUMN};margin-left:auto;margin-right:auto;padding-left:24px;padding-right:24px}}'
    f'p,li,dd{{max-width:{MEASURE}}}td p,td li,.card p{{max-width:none}}'
    'h2{margin:1.5em 0 .5em;font-size:1.3rem}h3{margin:1.1em 0 .4em;font-size:1.05rem}'
    # Lead: the run's intro beside its summary tiles.
    '.lay-lead{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:16px;align-items:start;'
    'margin:14px 0}.lay-lead>.run-intro,.lay-lead>.card{margin:0}'
    '.lay-lead>.card{grid-template-columns:repeat(auto-fill,minmax(170px,1fr))}'
    '.tile{padding:9px 12px}.tile .v{font-size:17px}.tile .s{font-size:12.5px;line-height:1.4}'
    '.tabhelp{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(480px,100%),1fr));gap:0 28px;'
    'font-size:13px}.tabhelp li{margin:2px 0}'
    # Short fact tables and glossary groups side by side.
    '.lay-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(440px,100%),1fr));gap:0 28px;'
    'align-items:start}.lay-cols>section{min-width:0}'
    'td,th{padding:6px 9px}'
    # Long tables keep every row but scroll inside a bounded box, so the next sections stay in reach.
    'main .scroll{max-height:min(72vh,680px);overflow:auto;border:1px solid var(--line);border-radius:8px}'
    'main .scroll th{position:sticky;top:0;z-index:1;background:var(--card);box-shadow:0 1px 0 var(--line)}'
    '@media (max-width:980px){.lay-lead{grid-template-columns:minmax(0,1fr)}}'
    + _PHONE + 'main .scroll{max-height:none}.tabhelp{grid-template-columns:1fr}}'
)

CONFERENCE_CSS = (
    f'body{{max-width:{COLUMN};margin-left:auto;margin-right:auto}}'
    f'body>p,.banner,footer,#intro{{max-width:{MEASURE}}}.banner{{max-width:calc({MEASURE} + 40px)}}'
    # Records as a grid of compact cards instead of one full-width card per row.
    '#records{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(330px,100%),1fr));gap:12px;'
    'align-items:start;margin-top:6px}#records>article{margin:0;padding:12px 14px;min-width:0}'
    '#records>article>h2{font-size:1.02rem;line-height:1.3;margin:0 0 2px}#records>article p{margin:4px 0}'
    '#records>article .text{font-size:14.5px;line-height:1.5;max-height:26em;overflow:auto}'
    '#records>article details{margin:8px 0 0}#records>article pre{font-size:12px}'
    '#records>article article{padding:10px 12px;margin:8px 0}#records>.empty{grid-column:1/-1}'
)

SIMPLE_JUDGE_CSS = f'main{{max-width:{COLUMN};margin-left:auto;margin-right:auto;overflow-x:auto}}main>p,main>section>p,main>div>p,main li{{max-width:{MEASURE}}}td p,td li{{max-width:none}}'

KINDS = (
    (re.compile(r'(?:^|/)results\.html$'), 'results', RESULTS_CSS),
    (re.compile(r'(?:^|/)conference-view/index\.html$'), 'conference', CONFERENCE_CSS),
    (re.compile(r'(?:^|/)analysis/simple_judge/report\.html$'), 'simple_judge', SIMPLE_JUDGE_CSS),
    # Site look only (scripts/site_look.py; math site): the incident report and the story map's own pages.
    (re.compile(r'(?:^|/)analysis/incident/report\.html$'), 'incident', ''),
    (re.compile(r'(?:^|/)analysis/storymap/(?!graph\.html$)[^/]+\.html$'), 'storymap', ''),
)

_STYLE_BLOCK = re.compile(r'<style id="site-layout">.*?</style>', re.S)
_INTRO_CARD = re.compile(r'(<section class="run-intro"(?:(?!<section).)*?</section>(?:<!--/run-intro-->)?)'
                         r'(<section class="card" aria-label="Run summary">(?:(?!<section).)*?</section>)', re.S)
_RUN_CONDITION = re.compile(r'(<h2>Run</h2>(?:<div class="scroll">)?<table>.*?</table>(?:</div>)?)'
                            r'(<h2>Condition</h2>(?:<div class="scroll">)?<table>.*?</table>(?:</div>)?)', re.S)
_GLOSS_GROUP = r'<h3>[^<]*</h3><dl class="gloss">.*?</dl>'
_GLOSSARY = re.compile(r'(<h2 id="statuses">What the statuses mean</h2>)((?:' + _GLOSS_GROUP + r')+)', re.S)


def kind(name):
    """'results', 'conference', 'simple_judge' or None for a site path (or file name)."""
    for pattern, label, _ in KINDS:
        if pattern.search(name):
            return label
    return None


def _results_markup(document):
    if 'class="lay-lead"' not in document:
        document = _INTRO_CARD.sub(lambda m: f'<div class="lay-lead">{m[1]}{m[2]}</div>', document, count=1)
    if 'class="lay-cols lay-facts"' not in document:
        document = _RUN_CONDITION.sub(lambda m: f'<div class="lay-cols lay-facts"><section>{m[1]}</section>'
                                                f'<section>{m[2]}</section></div>', document, count=1)
    if 'class="lay-cols lay-gloss"' not in document:
        document = _GLOSSARY.sub(lambda m: m[1] + '<div class="lay-cols lay-gloss">' + ''.join(
            f'<section>{g}</section>' for g in re.findall(_GLOSS_GROUP, m[2], re.S)) + '</div>', document, count=1)
    return document


def _insert_style(document, css):
    block = f'<style {MARKER}>{css}</style>'
    if MARKER in document:
        return _STYLE_BLOCK.sub(lambda _: block, document, count=1)
    # After the last stylesheet of the head, so these rules follow the page's own; else before <body>/</head>.
    head_end = document.find('<body')
    head = document if head_end < 0 else document[:head_end]
    last = head.rfind('</style>')
    if last >= 0:
        at = last + len('</style>')
    else:
        found = re.search(r'</head>|<body', document)
        at = found.start() if found else len(document)
    return document[:at] + block + document[at:]


def apply(name, document, read=None, overview=True):
    """The page ``name`` (a site path such as RUN/results.html) in the dense layout; unknown pages unchanged.

    results.html also gets the run overview (math site; ``overview=False`` for the ImpossibleBench site).
    ``read(path)``, optional, returns the bytes of a file of the same run (path relative to the run folder, such
    as analysis/simple_judge/report.json) or None; without it the overview uses the page alone."""
    for pattern, label, css in KINDS:
        if pattern.search(name):
            if label == 'results':
                document = _results_markup(document)
                if overview:
                    document = _overview(document, read)
                    css = css + (OVERVIEW_CSS + TABS_CSS if OV_START in document else '')
            if css:
                document = _insert_style(document, css)
            # The site look (fonts, buttons, shadows; owner, November 6, 2026): math site only, like the overview.
            if not overview or 'eference-test evaluator' in document:
                return document
            # The top bar Explore | Experiments | About (owner, November 10, 2026; scripts/site_nav.py), with
            # paths from the page's place in its run folder, and the library links pointed at experiments.html.
            relative = name[pattern.search(name).start():].lstrip('/')
            document = site_nav.apply(site_nav.back_links(document), None, site_nav.prefix_for(relative))
            # Plain words for visitors (owner, November 13, 2026; scripts/site_terms.py): visible text only.
            return site_terms.apply(site_look.apply(document))
    return document


# ------------------------------------------------------------------------------------------------ run overview
# Owner request and decisions, November 6, 2026 (docs/plan.md, "Site redesign: run panes with the interaction
# graph"): the math site's run page opens with an overview: a short readable title (the run id kept small), the
# run spec as chips, the agent categorization bar (the behavior judge's labels with counts), the live interaction
# graph beside a behavior-judge summary card (runs without a graph show the fakes chart), and the tabs reordered
# Overview · Behavior judge · Story map · Conference · Incident · Report details · Inspect. The overview is built
# only from the page itself (the run intro, summary tiles, agent labels and accepted-submission order) plus, when
# the caller can read the run's published files (``read``), analysis/simple_judge/report.json and the graph shell.
# It is one block between <!--ov:start--> and <!--ov:end-->, replaced on every apply (idempotent); the tab bar is
# moved into it (same links, new order). Math site only: ``overview=False`` (the ImpossibleBench site) skips it.
OV_START, OV_END = '<!--ov:start-->', '<!--ov:end-->'
_OV_BLOCK = re.compile(re.escape(OV_START) + '.*?' + re.escape(OV_END), re.S)
GRAPH = 'analysis/storymap/graph.html'
JUDGE_JSON = 'analysis/simple_judge/report.json'
GRAPH_HEIGHT = 520

# Primary label kinds in bar order, then the extra ones shown as counts (research/analysis/agent_badges.py).
_PRIMARY = ('exploiter', 'convert', 'tempted', 'unaware', 'unlabelled')
_EXTRA = ('whistleblower', 'eval_aware', 'overseer', 'seed')
_NAMES = {'exploiter': 'Exploiter', 'convert': 'Convert', 'tempted': 'Tempted', 'unaware': 'Unaware / honest',
          'unlabelled': 'Unlabelled', 'whistleblower': 'Whistleblower', 'eval_aware': 'Evaluation-aware',
          'overseer': 'Overseer', 'seed': 'Seed'}
_LEVELS = ('none', 'considered', 'explored', 'submitted', 'credited', 'unclear')
_LEVEL_KIND = {'none': 'unaware', 'considered': 'tempted', 'explored': 'tempted', 'submitted': 'convert',
               'credited': 'exploiter', 'unclear': 'unlabelled'}
# Tab order (owner, November 6, 2026); keys match the tab's data-tab or link target.
_TAB_ORDER = ('overview', 'simple-judge', 'judge', 'storymap', 'conference', 'incident', 'details', 'inspect')

OVERVIEW_CSS = (
    'body>h1{font:600 13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);margin:22px 0 4px;'
    'overflow-wrap:anywhere}'
    '.ov{margin:10px 0 18px}.ov-head{position:relative;padding:18px 20px 16px;border:1px solid var(--line);'
    'border-radius:14px;background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 13%,var(--card)),'
    'var(--card) 60%);overflow:hidden}'
    '.ov-kicker{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--accent);'
    'margin:0 0 4px}.ov-title{font-size:clamp(1.35rem,3.2vw,1.9rem);line-height:1.2;font-weight:750;margin:0;'
    'letter-spacing:-.01em}.ov-id{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);'
    'margin:4px 0 12px;overflow-wrap:anywhere}'
    '.ov-chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px;padding:0;list-style:none}'
    '.ov-chips li{max-width:none;margin:0;padding:3px 10px;border:1px solid var(--line);border-radius:999px;'
    'background:var(--card);font-size:12.5px;line-height:1.5}.ov-chips b{font-weight:600;color:var(--muted);'
    'margin-right:4px;font-size:11px;text-transform:uppercase;letter-spacing:.04em}'
    '.ov-bar{display:flex;height:30px;border-radius:8px;overflow:hidden;gap:2px;margin:4px 0 6px}'
    '.ov-bar>span{display:flex;align-items:center;justify-content:center;min-width:22px;font:700 12.5px/1 system-ui,'
    'sans-serif;border-radius:0;height:auto;padding:0 4px;box-sizing:border-box}'
    '.ov-bar>.ab-unlabelled{box-shadow:inset 0 0 0 1.5px currentColor}'
    '.ov-key{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12.5px;color:var(--muted);margin:0}'
    '.ov-key span{white-space:nowrap}.ov-key .ab{margin-right:4px}.ov-key strong{color:var(--fg)}'
    '.ov-row{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(0,1fr);gap:14px;margin:14px 0 0;'
    'align-items:stretch}'
    '.ov-pane{border:1px solid var(--line);border-radius:14px;background:var(--card);min-width:0;'
    'display:flex;flex-direction:column;overflow:hidden}'
    '.ov-pane>header{display:flex;align-items:baseline;justify-content:space-between;gap:10px;padding:10px 14px;'
    'border-bottom:1px solid var(--line)}.ov-pane>header h2{margin:0;font-size:1rem}'
    '.ov-pane>header a{font-size:13px;font-weight:600;text-decoration:none;white-space:nowrap}'
    f'.ov-graph iframe{{display:block;width:100%;height:{GRAPH_HEIGHT}px;border:0;background:var(--bg)}}'
    '.ov-gwrap{position:relative}.ov-pane>header a.ov-full,a.ov-full{display:inline-flex;align-items:center;gap:7px;'
    'padding:7px 15px 7px 13px;border:1px solid var(--accent);border-radius:999px;background:var(--accent);'
    'color:var(--card);font-weight:700;font-size:13.5px;line-height:1.2;text-decoration:none;'
    'box-shadow:0 2px 10px -2px color-mix(in srgb,var(--accent) 55%,transparent)}'
    '.ov-pane>header a.ov-full:hover,a.ov-full:hover{background:color-mix(in srgb,var(--accent) 86%,var(--fg));'
    'border-color:color-mix(in srgb,var(--accent) 86%,var(--fg));color:var(--card);text-decoration:none}'
    'a.ov-full:focus-visible{outline:3px solid var(--fg);outline-offset:2px}'
    '.ov-gwrap.ov-open{position:fixed;inset:0;z-index:1000;background:var(--bg)}'
    '.ov-gwrap.ov-open iframe{width:100%;height:100%}html.ov-lock{overflow:hidden}'
    '#ov-close{position:absolute;top:10px;right:12px;z-index:2;width:38px;height:38px;border-radius:50%;'
    'border:1px solid var(--line);background:var(--card);color:var(--fg);font-size:18px;line-height:1;cursor:pointer;'
    'box-shadow:0 2px 10px -2px color-mix(in srgb,var(--fg) 35%,transparent)}#ov-close[hidden]{display:none}'
    '#ov-close:hover{background:var(--accent);border-color:var(--accent);color:var(--card)}'
    '#ov-close:focus-visible{outline:3px solid var(--fg);outline-offset:2px}'
    '.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}'
    '.ov-graph .ov-note{font-size:12px;color:var(--muted);margin:0;padding:6px 14px;'
    'border-top:1px solid var(--line)}.ov-fakes{padding:12px 14px}.ov-fakes figure{margin:0;max-width:none}'
    '.ov-fakes .spark{display:block;width:100%;height:220px}.ov-fakes figcaption{font-size:12px;color:var(--muted)}'
    '.ov-judge{padding:0 0 4px}.ov-judge .ov-big{display:flex;gap:18px;flex-wrap:wrap;padding:12px 14px 4px}'
    '.ov-judge .ov-big div{min-width:0}.ov-judge .ov-big .v{font-size:26px;font-weight:750;line-height:1.1}'
    '.ov-judge .ov-big .k{font-size:12px;color:var(--muted)}'
    '.ov-levels{margin:6px 14px 8px;display:grid;grid-template-columns:auto 1fr auto;gap:4px 8px;'
    'align-items:center;font-size:12.5px}.ov-levels .t{height:9px;border-radius:5px;background:'
    'color-mix(in srgb,var(--line) 70%,transparent);overflow:hidden}.ov-levels .t>span{display:block;height:100%;'
    'border-radius:5px}.ov-levels .n{text-align:right;font-variant-numeric:tabular-nums}'
    '.ov-judge h3{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);'
    'margin:10px 14px 4px}.ov-judge ul{list-style:none;margin:0 14px 8px;padding:0}'
    '.ov-judge li{max-width:none;margin:0;padding:5px 0;border-top:1px solid var(--line);font-size:13px;'
    'line-height:1.4}.ov-judge li:first-child{border-top:0}.ov-judge li a{font-weight:600}'
    '.ov-judge .ov-src{font-size:12px;color:var(--muted);margin:4px 14px 10px;max-width:none}'
    '.ov-judge .ov-more{margin:auto 14px 10px;font-size:13px;font-weight:600}'
    '@media (max-width:900px){.ov-row{grid-template-columns:minmax(0,1fr)}}'
    + _PHONE + f'.ov-head{{padding:14px 14px 12px}}.ov-graph iframe{{height:min({GRAPH_HEIGHT}px,72vh)}}'
    '.ov-bar{height:26px}}'
)

_TAB_LINK = re.compile(r'<a\b[^>]*>.*?</a>|<span class="off">.*?</span>', re.S)
_NAV = re.compile(r'<nav class="tabs"[^>]*>.*?</nav>', re.S)
_AB_ID = re.compile(r'<span class="ab-id">(agent-\d+)<span class="ab-set"[^>]*>(.*?)</span></span>', re.S)
_AB_KIND = re.compile(r'class="ab ab-([a-z_]+)"')


def _tab_key(item):
    tab = re.search(r'data-tab="([^"]+)"', item)
    if tab:
        return tab[1]
    href = re.search(r'href="([^"]*)"', item)
    target = href[1] if href else ''
    label = re.sub(r'<[^>]+>', '', item).strip().lower()
    for key, needle in (('overview', '#overview'), ('details', '#report'), ('conference', 'conference-view/'),
                        ('inspect', 'inspect-view/'), ('simple-judge', 'simple_judge/'), ('judge', 'judge/'),
                        ('storymap', 'storymap/'), ('incident', 'incident/')):
        if needle in target:
            return key
    return 'simple-judge' if label.startswith('behavior judge') else label


def _tabs(nav):
    """The run-page tab bar in the overview order: Overview (this page) first, the old Report tab as Report
    details (#report), every other link kept as it was."""
    opening = '<nav class="tabs" id="ov-tabs" aria-label="Run records">'
    items = [m for m in _TAB_LINK.findall(nav) if _tab_key(m) not in ('overview', 'details', 'report')]
    # Other tabs keep their exact markup (other generators find them by it); the page script keys them by href.
    items += ['<a class="current" aria-current="page" data-tab="overview" data-ov="overview" href="#overview">'
              'Overview</a>', '<a data-tab="details" data-ov="report" href="#report">Report details</a>']
    rank = {k: i for i, k in enumerate(_TAB_ORDER)}
    items.sort(key=lambda m: rank.get(_tab_key(m), rank['judge'] if _tab_key(m).startswith('judge') else len(rank)))
    return opening + ''.join(items) + '</nav>'


def _agents(document):
    """{agent: [label kinds]} from the page's agent labels (first labelled mention of each agent)."""
    out = {}
    for agent, badges in _AB_ID.findall(document):
        if agent not in out:
            out[agent] = _AB_KIND.findall(badges)
    return out


def _accepted_order(document):
    """Agents in the order of their first accepted submission (the Accepted submissions table)."""
    at = document.find('<h2>Accepted submissions</h2>')
    if at < 0:
        return []
    end = document.find('</table>', at)
    return list(dict.fromkeys(re.findall(r'<span class="ab-id">(agent-\d+)', document[at:end])))


def _intro_parts(document):
    cond = re.search(r'<p class="ri-cond">([^<]*)</p>', document)
    return [p.strip() for p in html.unescape(cond[1]).rstrip('.').split(', ')] if cond else []


def _tile(document, key):
    found = re.search(r'<div class="tile"><div class="k">' + re.escape(key) + r'</div><div class="v">(.*?)</div>'
                      r'<div class="s">(.*?)</div>', document, re.S)
    return (html.unescape(re.sub(r'<[^>]+>', '', found[1])), html.unescape(found[2])) if found else (None, None)


def _short_overseers(part):
    m = re.match(r'(\d+) (.+?) overseers? (.*)$', part)
    if not m:
        return part
    count, rest = int(m[1]), m[3]
    powers = ('comms only' if 'without oversight powers' in rest else
              'enforcement only' if 'no posting' in rest else
              'check + revert' if 'check_proof' in rest else '')
    return f'{count} overseer{"s" if count != 1 else ""}' + (f' ({powers})' if powers else '')


# Top-down monitor arms (owner, November 12, 2026): the same M1-M4 labels as scripts/build_experiment_index.py.
_MONITOR_RUN = re.compile(r'-seeded-monitor-(actions|full)-(silent|reason)-')
_MONITOR_ARM = {('actions', 'silent'): 'M1', ('actions', 'reason'): 'M2', ('full', 'silent'): 'M3',
                ('full', 'reason'): 'M4'}


def _monitor(run):
    """(arm, short monitor spec) of a top-down monitor run, else None."""
    m = _MONITOR_RUN.search(run)
    if not m:
        return None
    return (_MONITOR_ARM[(m[1], m[2])], f'{"actions-only" if m[1] == "actions" else "full-transcript"}, '
                                       f'{"silent block" if m[2] == "silent" else "block with reason"}')


def _spec(document, run):
    """(title, [(chip key, chip value)]) from the run intro sentence, the summary tiles and the run id."""
    parts, chips = _intro_parts(document), []
    model, route = _tile(document, 'Model')
    variant, _ = _tile(document, 'Prompt variant')
    duration_tile, stop = _tile(document, 'Duration')
    solvers = next((p for p in parts if re.match(r'\d+ .+ solvers?$', p)), None)
    overseer = next((p for p in parts if 'overseer' in p), None)
    seed = next((p for p in parts if 'exploit' in p), None)
    duration = parts[-1] if parts and re.match(r'^[\d.]+ ?(?:h|min)', parts[-1]) else duration_tile
    delivery = ('pushed messages' if re.search(r'-push(?:-|$)', run) else
                'unread notices' if any('notices' in p for p in parts) or '-notices' in run else None)
    if solvers:
        chips.append(('Solvers', solvers))
    elif model:
        chips.append(('Model', model))
    agents_tile, _ = _tile(document, 'Agents')
    if not solvers and agents_tile:
        chips.append(('Agents', agents_tile))
    monitor = _monitor(run)
    if monitor:  # a monitor run's intro says "no overseer" (no overseer agents); the monitor is its oversight
        chips.append(('Monitor', monitor[1]))
    else:
        chips.append(('Oversight', overseer if overseer else ('no overseer' if 'no overseer' in parts else None)))
    chips.append(('Exploit', seed if seed and seed != 'no planted exploit' else
                  ('none planted' if seed else None)))
    chips.append(('Delivery', delivery))
    chips.append(('Duration', duration))
    chips.append(('Prompt', variant if variant and variant != 'not recorded' else None))
    if model and solvers:
        chips.append(('Model id', model))
    chips = [(k, v) for k, v in chips if v]
    rep = re.search(r'-r(\d+)-\d{8}T', run)
    if solvers:
        title = solvers
        if monitor:
            arm, spec = monitor
            kind, block = spec.split(', ')
            title += f' · {arm}: {kind} monitor, {block}'
        else:
            title += ' · ' + (_short_overseers(overseer) if overseer else 'no overseer')
    else:
        title = (model or run) + (f' · {agents_tile}' if agents_tile else '')
    if duration:
        title += ' · ' + duration
    if delivery == 'pushed messages':
        title += ' · pushed messages'
    if rep:
        title += f' · run {rep[1]}'
    return title, chips


def _judge_page(nav):
    """The behavior-judge page this run has, from its tab bar: the simplified judge's, else the rubric-3.1 judge's,
    else None (the overview then links to no judge page; production verify, November 6, 2026)."""
    for page in (JUDGE_JSON.replace('.json', '.html'), 'analysis/judge/report.html'):
        if f'href="{page}"' in nav:
            return page
    return None


def _bar(agents, judge=None):
    counts = {k: 0 for k in _PRIMARY + _EXTRA}
    for kinds in agents.values():
        for kind in kinds:
            if kind in counts:
                counts[kind] += 1
    judged = sum(counts[k] for k in _PRIMARY)
    if not judged:
        return '', counts
    bar = ''.join(f'<span class="ab ab-{k}" style="flex:{counts[k]} 1 0" title="{_NAMES[k]}: {counts[k]}">'
                  f'{counts[k]}</span>' for k in _PRIMARY if counts[k])
    key = ''.join(f'<span><span class="ab ab-{k}">{_letter(k)}</span>{_NAMES[k]} <strong>{counts[k]}</strong></span>'
                  for k in _PRIMARY + _EXTRA if counts[k])
    return (f'<div class="ov-bar" role="img" aria-label="Agent labels: ' + ', '.join(
        f'{_NAMES[k]} {counts[k]}' for k in _PRIMARY if counts[k]) + f'">{bar}</div>'
        f'<p class="ov-key">{key}' + (f' <a href="{judge}#agent-labels">Label definitions</a>' if judge else '') + '</p>',
        counts)


def _letter(kind):
    return {'exploiter': 'E', 'convert': 'C', 'tempted': 'T', 'unaware': 'U', 'unlabelled': '?',
            'whistleblower': 'W', 'eval_aware': 'A', 'overseer': 'O', 'seed': 'S'}[kind]


def _notable(agents, order, limit=5):
    """Up to ``limit`` agents worth opening first: independent exploiters (earliest accepted submission first),
    whistleblowers, then the earliest converts; within each group, agents without an accepted submission follow."""
    rank = {a: i for i, a in enumerate(order)}
    def by_time(group):
        return sorted(group, key=lambda a: (rank.get(a, len(rank)), a))
    picked = []
    for kind, why, cap in (('exploiter', 'found an exploit independently', 3),
                           ('whistleblower', 'reported the exploit', 2), ('convert', 'early convert', 5)):
        group = by_time([a for a, kinds in agents.items() if kind in kinds and a not in picked])
        for agent in group[:cap]:
            if len(picked) < limit:
                picked.append(agent)
    return picked


def _why(kinds, agent, order):
    labels = [(_NAMES[k], k) for k in kinds if k in _NAMES and k != 'unaware']
    first = order.index(agent) + 1 if agent in order else None
    return labels, first


def _judge_card(document, agents, counts, report, storymap, judge=None, has_page=None):
    levels = ((report or {}).get('counts') or {}).get('cheating_level') or {}
    c = (report or {}).get('counts') or {}
    labelled = c.get('labelled') if isinstance(c.get('labelled'), int) else sum(counts[k] for k in _PRIMARY)
    cheating = c.get('cheating_any')
    if cheating is None:
        cheating = counts['exploiter'] + counts['convert']
    whistle = (c.get('whistleblower') or {}).get('True', counts['whistleblower'])
    aware = (c.get('evaluation_aware') or {}).get('True', counts['eval_aware'])
    out = ['<section class="ov-pane ov-judge" aria-label="Behavior judge summary"><header><h2>Behavior judge</h2>'
           + (f'<a href="{judge}">Full report →</a>' if judge else '') + '</header>',
           '<div class="ov-big">'
           f'<div><div class="v">{cheating}<small style="font-size:14px;font-weight:600;color:var(--muted)">'
           f' / {labelled}</small></div><div class="k">{"explored, submitted or credited an exploit" if report else "submitted an exploit"}</div></div>'
           f'<div><div class="v">{whistle}</div><div class="k">whistleblowers</div></div>'
           f'<div><div class="v">{aware}</div><div class="k">evaluation-aware</div></div></div>']
    if levels:
        top = max([v for v in levels.values() if isinstance(v, int)] or [1]) or 1
        out.append('<div class="ov-levels" role="table" aria-label="Cheating level">' + ''.join(
            f'<span>{level}</span><span class="t"><span class="ab-{_LEVEL_KIND[level]}" '
            f'style="width:{100 * levels[level] / top:.1f}%"></span></span><span class="n">{levels[level]}</span>'
            for level in _LEVELS if isinstance(levels.get(level), int)) + '</div>')
    seed = c.get('seeded_source') or {}
    if seed.get('agents_that_read_seed_file') is not None:
        out.append(f'<p class="ov-src">{seed["agents_that_read_seed_file"]} agents read the planted seed file; '
                   f'{seed.get("cheating_agents_naming_seed", "?")} cheating agents named the seed as their source.</p>')
    order = _accepted_order(document)
    notable = _notable(agents, order)
    if notable:
        items = []
        for agent in notable:
            labels, first = _why(agents[agent], agent, order)
            href = (f'analysis/storymap/agents/{agent}.html' if storymap and (has_page is None or has_page(agent))
                    else judge)  # a sampled story map has pages for some agents only (verify, November 6, 2026)
            items.append('<li>' + (f'<a href="{href}"><code>{agent}</code></a> ' if href else f'<code>{agent}</code> ') + ''.join(
                f'<span class="ab ab-{k}" title="{n}">{_letter(k)}</span>' for n, k in labels) + ' <small>'
                + ', '.join(n.lower() for n, _ in labels) + '</small></li>')
        out.append('<h3>Agents to read first' + (' (story maps)' if storymap else '') + '</h3><ul>'
                   + ''.join(items) + '</ul>')
    out.append('</section>')
    return ''.join(out)


# The embedded viewer follows the system theme by itself; a theme forced with the page's toggle (data-theme on
# <html>, scripts/site_style.py) is passed on as &theme= so both match.
_THEME_SYNC = ('<script>(function(){var f=document.getElementById("ov-graph"),r=document.documentElement;'
               'function s(){var t=r.getAttribute("data-theme"),u="analysis/storymap/graph.html?embed=1&play=1"+'
               '(t==="light"||t==="dark"?"&theme="+t:"");if(f.getAttribute("src")!==u)f.setAttribute("src",u);}'
               's();new MutationObserver(s).observe(r,{attributes:true,attributeFilter:["data-theme"]});})();'
               '</script>')


# "Open full-screen graph" expands the embedded viewer over the page in place (owner, November 8, 2026): a fixed
# overlay filling the window, never browser full screen (owner, November 9, 2026); the same replay keeps playing,
# with the full sidebar. The close button or Esc returns to the run page.
_FULL_JS = ('<button id="ov-close" type="button" aria-label="Close full screen" title="Close full screen (Esc)" hidden>'
            '&#x2715;</button><script>(function(){var a=document.querySelector("a.ov-full"),w=document.querySelector('
            '".ov-gwrap"),f=document.getElementById("ov-graph"),x=document.getElementById("ov-close"),r=document.'
            'documentElement,open=false;if(!a||!w||!f||!x)return;w.appendChild(x);function mode(){try{var d=f.'
            'contentDocument.documentElement;d.classList.toggle("embed",!open);d.classList.toggle("overlay",open);}'
            'catch(e){}}f.addEventListener("load",mode);function show(){if(open)return;open=true;w.classList.add('
            '"ov-open");r.classList.add("ov-lock");x.hidden=false;mode();x.focus();}'
            'function hide(){if(!open)return;open=false;w.classList.remove("ov-open");'
            'r.classList.remove("ov-lock");x.hidden=true;mode();a.focus();}'
            'a.addEventListener("click",function(e){if(e.button||e.metaKey||e.ctrlKey||'
            'e.shiftKey||e.altKey)return;e.preventDefault();e.stopPropagation();show();});x.addEventListener("click",hide);document.'
            'addEventListener("keydown",function(e){if(e.key==="Escape"&&open){e.preventDefault();hide();}});'
            'addEventListener("message",function(e){if(e.origin===location.origin&&e.data==="graph-close")hide();});'
            '})();</script>')


_EXPAND = ('<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" focusable="false"><path d="M2 6V2h4M14 6V2h-4'
           'M2 10v4h4M14 10v4h-4" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" '
           'stroke-linejoin="round"/></svg>')


def _graph_pane(document, has_graph):
    if has_graph:
        return ('<section class="ov-pane ov-graph" aria-label="Interaction graph"><header><h2>Interaction graph</h2>'
                f'<a class="ov-full" href="{GRAPH}">{_EXPAND}Open full-screen graph</a></header>'
                f'<div class="ov-gwrap"><iframe id="ov-graph" src="{GRAPH}?embed=1&amp;play=1" title="Interaction graph '
                'of this run (playing at 5x)" loading="lazy"></iframe></div>'
                + _THEME_SYNC + _FULL_JS +
                '<p class="ov-note">Agents and the messages, files and submissions between them over the run; '
                'click a bubble to open it. Each agent links to its <a href="analysis/storymap/index.html">story '
                'map</a>.</p></section>')
    figure = re.search(r'<section class="run-intro".*?(<figure>.*?</figure>)', document, re.S)
    body = (figure[1] if figure else '<p class="muted">No interaction graph or fakes chart for this run.</p>')
    link = '<a href="#faking-over-time">Faking over time ↓</a>' if 'id="faking-over-time"' in document else ''
    return ('<section class="ov-pane ov-fakes-pane" aria-label="Standing fakes"><header><h2>Standing fakes over '
            f'the run</h2>{link}</header><div class="ov-fakes">{body}</div></section>')


def _fix_series(document, run):
    """The run intro's Series line from the current run_profile.SERIES (a page rendered before its series was
    added keeps the stale "Earlier pilots" text otherwise)."""
    try:
        import run_profile
    except ImportError:
        return document
    _, title, text = run_profile.series_of(run)
    line = (f'<p><span class="ri-k">Series</span><strong>{html.escape(title)}.</strong> '
            f'{html.escape(text)}</p>')
    return re.sub(r'<p><span class="ri-k">Series</span>.*?</p>', lambda _: line, document, count=1, flags=re.S)


def _overview(document, read=None):
    """The results page with the overview block (see above); pages without the run-page tab bar unchanged."""
    nav = _NAV.search(document)  # the tab bar, inside an earlier overview block or still in place
    run = re.search(r'<p><code>([^<]+)</code> · ', document)
    if (not nav or not run or ('href="#report"' not in nav[0] and 'href="#overview"' not in nav[0])
            or 'eference-test evaluator' in document):  # coding (ImpossibleBench) pages keep their layout
        return document
    nav = nav[0]
    document = _NAV.sub('', _OV_BLOCK.sub('', document), count=1)
    run = html.unescape(run[1])
    document = _fix_series(document, run)

    def load(path):
        if read is None:
            return None
        try:
            return read(path)
        except (OSError, KeyError, ValueError):
            return None
    report = load(JUDGE_JSON)
    try:
        report = json.loads(report) if report else None
    except ValueError:
        report = None
    storymap = 'data-tab="storymap"' in nav
    graph = load(GRAPH)
    has_graph = bool(graph) if read is not None and graph is not None else storymap
    agents = _agents(document)
    title, chips = _spec(document, run)
    judge = _judge_page(nav)
    # Agent story-map pages: checked one by one when the run's story-map files are readable, else assumed.
    has_page = (lambda agent: load(f'analysis/storymap/agents/{agent}.html') is not None) if graph else None
    bar, counts = _bar(agents, judge)
    judged = bool(report) or any(counts[k] for k in _PRIMARY)
    row = _graph_pane(document, has_graph) + (_judge_card(document, agents, counts, report, storymap, judge, has_page) if judged
                                               else '')
    block = (f'{OV_START}<section class="ov" id="overview" aria-label="Run overview"><div class="ov-head">'
             '<p class="ov-kicker">Run overview</p>'
             f'<p class="ov-title" role="heading" aria-level="1">{html.escape(title)}</p>'
             f'<p class="ov-id">{html.escape(run)}</p>'
             '<ul class="ov-chips">' + ''.join(f'<li><b>{html.escape(k)}</b>{html.escape(v)}</li>' for k, v in chips)
             + '</ul>' + bar + '</div></section>' + _tabs(nav)
             + f'<div class="ov-row"{"" if judged else " style=grid-template-columns:1fr"}>{row}</div>'
             '<div id="ov-panel" hidden><div class="ov-pbar">'
             '<a id="ov-popen" href="#" target="_blank" rel="noopener">Open in new tab ↗</a></div>'
             f'<div id="ov-frames"></div></div>{TABS_JS}<i id="ov-end" hidden></i>{OV_END}')
    crumbs = re.search(r'<p class="crumbs">.*?</p>', document, re.S)
    at = crumbs.end() if crumbs else document.find('>', document.find('<body')) + 1
    return document[:at] + block + document[at:]


# ------------------------------------------------------------------------------------------- in-page tabs
# Owner request, November 6, 2026: "make that one page so it cleanly tabs to those instead of redirecting".
# The tab bar switches panels in place: Overview (the overview row only), Report details (the report body below
# the overview) and, loaded lazily into a same-origin iframe that fills the viewport below the sticky tab bar,
# Behavior judge, the rubric-3.1 judge, Story map, Conference, Incident report and Inspect. The hash selects the
# tab (#overview, #report, #judge, #judge-rubric, #storymap, #conference, #incident, #inspect) and back/forward
# works; tabs keep their real hrefs (middle-click / cmd-click open the page, and without JavaScript they are plain
# links). Target pages are unchanged on disk: on each load inside the panel their own back-link line is hidden
# when it only repeats the run's navigation, links back to the run page or the library open at the top, and links
# to another tab's pages switch to that tab. Links in the overview to a tab's pages (story-map agents, the judge
# report) open in that tab's panel.

TABS_CSS = (
    '#ov-tabs{position:sticky;top:0;z-index:30;background:var(--bg);margin:10px 0 0;padding:6px 0 0;'
    'box-shadow:0 1px 0 var(--line)}'
    '#ov-panel{margin:0}#ov-panel[hidden]{display:none}'
    '.ov-pbar{display:flex;justify-content:flex-end;padding:3px 2px;font-size:12.5px;line-height:1.4}'
    '.ov-pbar a{font-weight:600;text-decoration:none;white-space:nowrap}'
    '#ov-frames iframe{display:block;width:100%;height:calc(100vh - var(--ov-top,56px) - 30px);'
    'height:calc(100dvh - var(--ov-top,56px) - 30px);min-height:360px;border:1px solid var(--line);'
    'border-radius:10px;background:var(--bg)}#ov-frames iframe[hidden]{display:none}'
    'html[data-ov-tab]:not([data-ov-tab="report"]) #ov-end~*:not(.site-theme-toggle){display:none}'
    'html[data-ov-tab]:not([data-ov-tab="overview"]) .ov-row{display:none}'
    + _PHONE + '#ov-tabs{display:flex;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;'
    'scrollbar-width:none;margin-left:-16px;margin-right:-16px;padding-left:16px;padding-right:16px}'
    '#ov-tabs::-webkit-scrollbar{display:none}#ov-tabs>*{flex:none;white-space:nowrap}}'
)

# Inline, right after the panel: runs before the rest of the page is parsed, so the chosen tab shows without a
# flash of the other content. ES5, no globals, no network beyond the panel pages themselves.
TABS_JS = r'''<script>(function(){
var R=document.documentElement,nav=document.getElementById("ov-tabs"),panel=document.getElementById("ov-panel");
if(!nav||!panel||!window.history||!history.replaceState)return;
var frames=document.getElementById("ov-frames"),
open=document.getElementById("ov-popen"),base=location.href.replace(/[#?].*$/,"").replace(/[^\/]*$/,""),
tabs={},order=[],cur=null,
PRE={judge:"analysis/simple_judge/","judge-rubric":"analysis/judge/",storymap:"analysis/storymap/",
conference:"conference-view/",incident:"analysis/incident/",inspect:"inspect-view/"};
function abs(u){try{return new URL(u,base).href;}catch(e){return null;}}
function keyOf(u){if(!u||u.indexOf(base)!==0)return null;var rel=u.slice(base.length);
 for(var k in PRE)if(rel.indexOf(PRE[k])===0)return k;return null;}
function tabOf(u){var k=keyOf(u);return k&&tabs[k]?k:null;}
[].forEach.call(nav.querySelectorAll("a[href]"),function(a){var k=a.getAttribute("data-ov")||keyOf(abs(a.getAttribute("href")));
 if(!k||tabs[k])return;a.setAttribute("data-ov",k);tabs[k]={a:a,href:a.getAttribute("href"),label:a.textContent};order.push(k);});
function top(){var h=nav.getBoundingClientRect().height;R.style.setProperty("--ov-top",Math.ceil(h)+"px");return h;}
function fix(f){var d,k=f.getAttribute("data-ov");try{d=f.contentDocument;}catch(e){return;}if(!d||!d.body)return;
 var t=R.getAttribute("data-theme");if(t)d.documentElement.setAttribute("data-theme",t);
 else d.documentElement.removeAttribute("data-theme");
 if(!d.getElementById("ov-embed")){var s=d.createElement("style");s.id="ov-embed";
  s.textContent=".site-theme-toggle{display:none!important}";(d.head||d.body).appendChild(s);}
 [].forEach.call(d.querySelectorAll("p,nav"),function(p){var as=p.querySelectorAll("a");if(!as.length||p.closest("main article"))return;
  var txt=p.textContent,own=0;[].forEach.call(as,function(a){var u=abs(a.href);txt=txt.replace(a.textContent,"");
   if(u&&(u.indexOf(base)!==0||/results\.html$/.test(u)||/^(?:inspect-view|conference-view)\/(?:index\.html)?$/.test(u.slice(base.length))||(tabOf(u)&&tabOf(u)!==k)))own++;});
  if(own===as.length&&!/[^\s·|\/-]/.test(txt)&&p.offsetTop<400)p.style.display="none";});
 if(!d.__ovClicks){d.__ovClicks=1;d.addEventListener("click",function(e){var a=e.target.closest&&e.target.closest("a[href]");
  if(!a||e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||a.target)return;
  var u=abs(a.href),here=f.contentWindow.location.href.replace(/#.*$/,"");if(!u||u.replace(/#.*$/,"")===here)return;
  var j=tabOf(u);if(j&&j!==k){e.preventDefault();go(j,u,true);}
  else if(!j&&(u.indexOf(location.origin)!==0||u.indexOf(base)!==0||/results\.html(?:#.*)?$/.test(u))){e.preventDefault();
   window.location.href=u;}},true);}}
// A page loading in a panel may scroll this page (Inspect focuses its viewer): for a few seconds after a panel
// starts loading, the scroll position is held unless the reader scrolls themselves.
var hold=null;function release(){hold=null;}
["wheel","touchstart","keydown","mousedown"].forEach(function(e){window.addEventListener(e,release,{passive:true});});
window.addEventListener("scroll",function(){if(hold&&Date.now()<hold.t&&window.pageYOffset!==hold.y)window.scrollTo(window.pageXOffset,hold.y);});
function frame(k,u){var t=tabs[k];hold={y:window.pageYOffset,t:Date.now()+4000};if(!t.f){t.f=document.createElement("iframe");t.f.title=t.label;
 t.f.setAttribute("data-ov",k);t.f.src=u||t.href;t.f.addEventListener("load",function(){fix(t.f);});frames.appendChild(t.f);}
 else if(u&&abs(u)!==t.f.src){try{t.f.contentWindow.location.replace(abs(u));}catch(e){t.f.src=u;}}return t.f;}
function show(k,u){if(!tabs[k])k="overview";cur=k;var y=window.pageYOffset,p=PRE[k]!==undefined,f=null;
 // Every DOM change first, with no layout read in between, then the scroll position restored: a tab switch never
 // moves the page (owner, November 6, 2026); only the active tab's iframe is shown.
 if(p){f=frame(k,u);f.hidden=false;panel.hidden=false;}
 R.setAttribute("data-ov-tab",k);
 [].forEach.call(frames.children,function(x){if(x!==f)x.hidden=true;});
 if(!p)panel.hidden=true;
 order.forEach(function(x){var a=tabs[x].a;if(x===k){a.classList.add("current");a.setAttribute("aria-current","page");}
  else{a.classList.remove("current");a.removeAttribute("aria-current");}});
 if(p){open.href=u||tabs[k].href;try{var w=f.contentWindow.location.href;if(w&&w!=="about:blank"&&!u)open.href=w;}catch(e){}}
 if(window.pageYOffset!==y)window.scrollTo(window.pageXOffset,y);
 if(nav.scrollWidth>nav.clientWidth){var at=tabs[k].a;if(at.offsetLeft<nav.scrollLeft||at.offsetLeft+at.offsetWidth>nav.scrollLeft+nav.clientWidth)nav.scrollLeft=at.offsetLeft-16;}}
function go(k,u,push){var h="#"+k;if(push&&location.hash!==h)history.pushState({ov:k,u:u||null},"",h);
 else history.replaceState({ov:k,u:u||null},"",h);show(k,u);
 top();}  // the scroll position never changes on a tab switch (owner, November 6, 2026)
function fromHash(){var h=location.hash.slice(1);return tabs[h]?h:"overview";}
nav.addEventListener("click",function(e){var a=e.target.closest&&e.target.closest("a[data-ov]");
 if(!a||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();go(a.getAttribute("data-ov"),null,true);});
document.addEventListener("click",function(e){var a=e.target.closest&&e.target.closest("#overview a[href],.ov-row a[href]");
 if(!a||a.target||e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;var k=tabOf(abs(a.getAttribute("href")));
 if(k){e.preventDefault();go(k,a.getAttribute("href"),true);}});
window.addEventListener("popstate",function(e){var s=e.state||{};show(s.ov||fromHash(),s.u||null);});
window.addEventListener("resize",top);
new MutationObserver(function(){[].forEach.call(frames.children,fix);}).observe(R,{attributes:true,attributeFilter:["data-theme"]});
var h=location.hash.slice(1);if(tabs[h]){history.replaceState({ov:h},"",location.href);show(h);}else show("overview");
top();})();</script>'''
