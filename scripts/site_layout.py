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
import re

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


def apply(name, document):
    """The page ``name`` (a site path such as RUN/results.html) in the dense layout; unknown pages unchanged."""
    for pattern, label, css in KINDS:
        if pattern.search(name):
            if label == 'results':
                document = _results_markup(document)
            return _insert_style(document, css)
    return document
