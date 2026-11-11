"""Top navigation of the math library (owner request, November 10, 2026: "make the website more user-facing").

The site has three root pages: ``index.html`` (Explore: a few highlighted runs with large graphs,
scripts/explore_page.py), ``experiments.html`` (the browsable catalogue of every run, formerly index.html) and
``about.html`` (the objective, the reconstructed paper, how a run works, how to read the site and the glossary,
formerly the catalogue's collapsed About section). Every generated page of the math site carries one top bar,
"Explore | Experiments | About", with the current page highlighted.

``apply(document, current=None, prefix='')`` is idempotent: it inserts (or replaces) one
``<nav id="site-nav">`` right after ``<body>`` and one ``<style id="site-nav-style">`` in the head. ``prefix`` is
the relative path from the page to the site root (``'../'`` for RUN/results.html), so the links work from run
subfolders, on the hosted site and in a local runs/ folder alike. ``back_links`` points the run pages' existing
"All experiments" / "Experiment library" links (and the library-glossary link) at experiments.html and about.html.
The ImpossibleBench site never gets the bar (its catalogue stays index.html).
"""
import html
import re

EXPLORE = 'index.html'
EXPERIMENTS = 'experiments.html'
ABOUT = 'about.html'
ROOT_PAGES = (EXPLORE, EXPERIMENTS, ABOUT)
PAGES = (('explore', EXPLORE, 'Explore'), ('experiments', EXPERIMENTS, 'Experiments'), ('about', ABOUT, 'About'))
BRAND = 'GDM Math Swarm'

NAV_ID = 'site-nav'
STYLE_MARKER = 'id="site-nav-style"'
CSS = (
    '#site-nav{display:flex;align-items:center;justify-content:space-between;gap:10px 18px;flex-wrap:wrap;'
    'margin:0 0 18px;padding:12px 0;border-bottom:1px solid var(--line,#8884);'
    'font:600 14px/1.2 var(--font-text,system-ui,-apple-system,"Segoe UI",sans-serif)}'
    '#site-nav .sn-brand{display:inline-flex;align-items:center;gap:9px;color:var(--fg,inherit);text-decoration:none;'
    'font:750 15.5px/1.1 var(--font-display,var(--font-text,system-ui,sans-serif));letter-spacing:-.01em}'
    '#site-nav .sn-dot{width:11px;height:11px;border-radius:50%;background:var(--accent,#2563eb);'
    'box-shadow:7px -5px 0 -2px color-mix(in srgb,var(--accent,#2563eb) 55%,transparent),'
    '-6px 5px 0 -3px color-mix(in srgb,var(--accent,#2563eb) 40%,transparent)}'
    '#site-nav .sn-links{display:flex;gap:4px;flex-wrap:wrap}'
    '#site-nav .sn-links a{padding:7px 13px;border-radius:999px;color:var(--muted,inherit);text-decoration:none;'
    'border:1px solid transparent;transition:color .15s,background-color .15s,border-color .15s}'
    '#site-nav .sn-links a:hover{color:var(--fg,inherit);'
    'background:color-mix(in srgb,var(--accent,#2563eb) 9%,transparent)}'
    '#site-nav .sn-links a[aria-current="page"]{color:var(--fg,inherit);border-color:var(--line,#8884);'
    'background:var(--card,transparent);box-shadow:inset 0 -2px 0 var(--accent,#2563eb)}'
    '@media (max-width:520px){#site-nav{padding:10px 0}#site-nav .sn-links a{padding:6px 10px;font-size:13.5px}}'
    '@media (prefers-reduced-motion:reduce){#site-nav .sn-links a{transition:none}}'
    '@media print{#site-nav{display:none}}'
)

_NAV = re.compile(r'<nav id="site-nav"[^>]*>.*?</nav>', re.S)
_STYLE = re.compile(r'<style id="site-nav-style">.*?</style>', re.S)


def bar(current=None, prefix=''):
    """The top bar: the brand (to Explore) and Explore / Experiments / About, ``current`` marked."""
    esc = lambda s: html.escape(s, quote=True)  # noqa: E731
    links = ''.join(f'<a href="{esc(prefix + target)}"' + (' aria-current="page"' if key == current else '')
                    + f'>{label}</a>' for key, target, label in PAGES)
    return (f'<nav id="{NAV_ID}" aria-label="Site"><a class="sn-brand" href="{esc(prefix + EXPLORE)}">'
            f'<span class="sn-dot" aria-hidden="true"></span>{html.escape(BRAND)}</a>'
            f'<span class="sn-links">{links}</span></nav>')


def style():
    return f'<style {STYLE_MARKER}>{CSS}</style>'


def _insert_style(document):
    if STYLE_MARKER in document:
        return _STYLE.sub(lambda _: style(), document, count=1)
    found = re.search(r'</head>|<body|<nav id="site-nav"', document)
    at = found.start() if found else 0
    return document[:at] + style() + document[at:]


# Pages without <body> (the conference view: head styles run straight into its first paragraph): the bar goes
# where the last head style meets the first content element.
_CONTENT_START = re.compile(r'</style>(?=\s*<(?:p|h1|h2|header|main|div|section|nav)\b)', re.I)


def apply(document, current=None, prefix=''):
    """``document`` with the site's top bar (replaced if present, else right after ``<body ...>``, or for a page
    without <body> right before its first content element)."""
    nav = bar(current, prefix)
    if f'<nav id="{NAV_ID}"' in document:
        document = _NAV.sub(lambda _: nav, document, count=1)
    else:
        body = re.search(r'<body(?:\s[^>]*)?>', document, re.I) or _CONTENT_START.search(document)
        if body is None:
            return document
        document = document[:body.end()] + nav + document[body.end():]
    return _insert_style(document)


# Run pages written before November 10, 2026 point "All experiments" (results.html), "Experiment library"
# (conference view) and "library glossary" (run intro) at the root index.html, which is now the Explore page.
_BACK = re.compile(r'(<a href=")((?:\.\./)+)index\.html(#glossary)?(">)(All experiments|Experiment library|library glossary)'
                   r'(</a>)')


def back_links(document):
    """Library links of a run page pointed at experiments.html (and the glossary at about.html#glossary)."""
    def swap(m):
        target = ABOUT + m[3] if m[3] else EXPERIMENTS
        return f'{m[1]}{m[2]}{target}{m[4]}{m[5]}{m[6]}'
    return _BACK.sub(swap, document)


def prefix_for(relative):
    """The path to the site root from a page at ``relative`` inside a run folder (RUN/<relative>)."""
    return '../' * (relative.strip('/').count('/') + 1)


# The Explore page forwards catalogue-style deep links (the catalogue was index.html until November 10, 2026):
# ?page=..., #series-..., #compare=..., #glossary, #RUN and any hash naming no element of the Explore page.
FORWARD_ID = 'catalogue-forward'
FORWARD_JS = (
    f'<script id="{FORWARD_ID}">(function(){{var h=location.hash,s=location.search;'
    'var about={"#about":1,"#experiment":1,"#how":1,"#read":1,"#glossary":1};'
    'if(about[h]){location.replace("' + ABOUT + '"+h);return;}'
    'var id=h.length>1?decodeURIComponent(h.slice(1)):"";'
    'if(/[?&]page=/.test(s)||(id&&!document.getElementById(id)))'
    'location.replace("' + EXPERIMENTS + '"+s+h);})();</script>'
)
_FORWARD = re.compile(r'<script id="catalogue-forward">.*?</script>', re.S)


def with_forwarder(document):
    """The Explore page with the deep-link forwarder at the end of its body (idempotent)."""
    if FORWARD_ID in document:
        return _FORWARD.sub(lambda _: FORWARD_JS, document, count=1)
    at = document.rfind('</body>')
    return (document[:at] + FORWARD_JS + document[at:]) if at >= 0 else document + FORWARD_JS


_TO_ROOT = re.compile(r'(<a href=")((?:\.\./)+)(?:experiments\.html|about\.html(?=#glossary))')


def for_hf_space(document):
    """A page for the former HF Space library, which has only its catalogue at index.html: no top bar, and the
    library links back at index.html (the HF maintenance re-renders, scripts/maintain_hf_run.py)."""
    document = _STYLE.sub('', _NAV.sub('', document, count=1), count=1)
    return _TO_ROOT.sub(lambda m: f'{m[1]}{m[2]}index.html', document)
