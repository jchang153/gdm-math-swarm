"""Site-wide look of the math library (owner request, November 6, 2026): "make the site more aesthetic in general;
like "all experiments" buttons. something more interesting than just that default font".

One ``<style id="site-look">`` block on the math site's pages (run page, conference view, behavior judge, incident
report, story-map pages and the catalogue), on top of the shared theme tokens (scripts/site_style.py): a type
pairing instead of system-ui (Bricolage Grotesque for headings, IBM Plex Sans for text, IBM Plex Mono for run ids
and code), "All experiments" as a back button, one button style for tabs and pane actions, softer card shadows and
a quiet dotted header motif echoing the interaction graph. Colours come from the theme tokens only (light and dark).

The fonts are self-hosted under ``_assets/fonts/`` at the site root (``font_assets``, added by the deploy's finish
step like the shared graph viewer): Latin subsets of open-licence (SIL OFL 1.1) Google Fonts files, with their
licence texts, about 160 KB in all, ``font-display: swap`` and system fallbacks, so a page without them (a local
file, the ImpossibleBench site) still reads normally. ``apply`` is idempotent (the block is replaced, never
duplicated) and changes no text, link or data. The ImpossibleBench site never gets the block.
"""
from pathlib import Path
import re

MARKER = 'id="site-look"'
ASSET_DIR = '_assets/fonts'  # site-root folder of the shared fonts
SOURCE = Path(__file__).resolve().parent / 'site_assets' / 'fonts'
FONT_FILES = ('bricolage-grotesque-latin-var.woff2', 'ibm-plex-sans-latin-var.woff2',
              'ibm-plex-sans-latin-italic.woff2', 'ibm-plex-mono-latin-400.woff2', 'ibm-plex-mono-latin-500.woff2')
LICENCES = ('OFL-bricolage-grotesque.txt', 'OFL-ibm-plex.txt')
LATIN = ('U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,'
         'U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD')


def _face(family, file, weight, style='normal'):
    return (f'@font-face{{font-family:"{family}";src:url(/{ASSET_DIR}/{file}) format("woff2");font-weight:{weight};'
            f'font-style:{style};font-display:swap;unicode-range:{LATIN}}}')


FACES = (_face('Bricolage Grotesque', FONT_FILES[0], '500 800') + _face('IBM Plex Sans', FONT_FILES[1], '400 600')
         + _face('IBM Plex Sans', FONT_FILES[2], '400', 'italic') + _face('IBM Plex Mono', FONT_FILES[3], '400')
         + _face('IBM Plex Mono', FONT_FILES[4], '500'))

_BUTTONS = '.ov-pane>header a,.ov-pbar a,.pane-btns a'
CSS = (
    FACES
    + ':root{--font-display:"Bricolage Grotesque","Avenir Next","Segoe UI",system-ui,sans-serif;'
    '--font-text:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;'
    '--font-mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
    '--lift:0 1px 2px color-mix(in srgb,var(--shadow) 55%,transparent),0 12px 28px -18px var(--shadow);'
    '--dots:radial-gradient(color-mix(in srgb,var(--accent) 26%,transparent) 1.1px,transparent 1.6px)}'
    # Type: text face everywhere, the display face on headings, the mono face on ids and code.
    'body{font-family:var(--font-text);font-optical-sizing:auto;-webkit-font-smoothing:antialiased}'
    'h1,h2,h3,.ov-title,.rc-title,.hero h1{font-family:var(--font-display);letter-spacing:-.012em;'
    'text-wrap:balance}h1,.ov-title{font-weight:760}h2{font-weight:700}h3{font-weight:650}'
    'code,pre,kbd,samp,.ov-id,#ov-end~h1{font-family:var(--font-mono)}code{font-size:.92em}'
    'table,.ov-levels,.ov-big,.tile .v{font-variant-numeric:tabular-nums}'
    '.ov-kicker,.ri-k,.ov-chips b,.ov-judge h3,th{letter-spacing:.06em}'
    # "All experiments": a back button.
    '.crumbs a{display:inline-flex;align-items:center;gap:7px;padding:6px 15px 6px 11px;border:1px solid var(--line);'
    'border-radius:999px;background:var(--card);color:var(--fg);font:600 13.5px/1.2 var(--font-text);'
    'text-decoration:none;box-shadow:var(--lift);transition:border-color .15s,color .15s,background-color .15s}'
    '.crumbs a::before{content:"\\2190";font-size:15px;line-height:1;transition:transform .15s}'
    '.crumbs a:hover{border-color:var(--accent);color:var(--accent);'
    'background:color-mix(in srgb,var(--accent) 6%,var(--card))}.crumbs a:hover::before{transform:translateX(-3px)}'
    # Tabs: quiet labels with an accent underline on the current one.
    '#ov-tabs{gap:2px;border-bottom:1px solid var(--line);box-shadow:none}'
    '#ov-tabs a,#ov-tabs span{border:0;border-radius:8px 8px 0 0;background:none;margin:0;padding:10px 14px 11px;'
    'color:var(--muted);font:600 14.5px/1.2 var(--font-text);position:relative;transition:color .15s,background-color .15s}'
    '#ov-tabs a:hover{color:var(--fg);background:color-mix(in srgb,var(--accent) 8%,transparent)}'
    '#ov-tabs a.current{color:var(--fg);border:0}#ov-tabs a.current::after{content:"";position:absolute;left:12px;'
    'right:12px;bottom:-1px;height:3px;border-radius:3px 3px 0 0;background:var(--accent)}'
    # One small-button style for pane actions.
    + _BUTTONS + '{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;border:1px solid var(--line);'
    'border-radius:999px;background:var(--card);color:var(--accent);font:600 12.5px/1.4 var(--font-text);'
    'text-decoration:none;transition:border-color .15s,background-color .15s}'
    + ','.join(s + ':hover' for s in _BUTTONS.split(',')) + '{border-color:var(--accent);'
    'background:color-mix(in srgb,var(--accent) 8%,var(--card));text-decoration:none}'
    '.pane-btns a.b-graph{background:var(--accent);border-color:var(--accent);color:var(--card)}'
    '.pane-btns a.b-graph:hover{background:color-mix(in srgb,var(--accent) 86%,var(--fg));color:var(--card)}'
    # Cards and the header: softer shadows and a dotted motif fading in from the right.
    '.ov-head,.ov-pane,.run-intro,section.card[aria-label="Run summary"]{box-shadow:var(--lift)}'
    '.ov-head{border-radius:16px}.ov-head>*,.hero>*{position:relative}'
    '.ov-head::before,.hero::before{content:"";position:absolute;top:0;right:0;bottom:0;left:48%;pointer-events:none;'
    'background:var(--dots);background-size:14px 14px;-webkit-mask-image:linear-gradient(90deg,transparent,#000 80%);'
    'mask-image:linear-gradient(90deg,transparent,#000 80%)}'
    '.ov-pane{border-radius:14px}.ov-chips li{background:color-mix(in srgb,var(--card) 82%,transparent)}'
    '.hero{position:relative}'
    '.site-theme-toggle{font:600 12.5px/1 var(--font-text);box-shadow:var(--lift)}'
    '@media (prefers-reduced-motion:reduce){.crumbs a,.crumbs a::before,#ov-tabs a,' + _BUTTONS
    + '{transition:none}}'
)

_BLOCK = re.compile(r'<style id="site-look">.*?</style>', re.S)


def block():
    return f'<style {MARKER}>{CSS}</style>'


def apply(document):
    """The page with the site-look block (replaced if present; else after the page's last head style)."""
    if MARKER in document:
        return _BLOCK.sub(lambda _: block(), document, count=1)
    head_end = document.find('<body')
    head = document if head_end < 0 else document[:head_end]
    last = head.rfind('</style>')
    if last >= 0:
        at = last + len('</style>')
    else:
        found = re.search(r'</head>|<body', document)
        at = found.start() if found else len(document)
    return document[:at] + block() + document[at:]


def font_assets():
    """{site path: bytes} of the self-hosted fonts and their licences (the math site's _assets/fonts/)."""
    return {f'{ASSET_DIR}/{name}': (SOURCE / name).read_bytes() for name in FONT_FILES + LICENCES}
