"""Shared look for every generated library page: light/dark theme, toggle and phone layout.

Page generators keep their own page-specific CSS but write colours as the
``var(--...)`` tokens defined here, and pass their finished document through
``themed()``. The theme follows the visitor's ``prefers-color-scheme`` until they
pick Light or Dark with the corner toggle; that choice is a per-browser
convenience kept in ``localStorage`` (wrapped in try/except, so a blocked store
just means "follow the system"). Nothing here loads external resources, sends
data anywhere, or touches the hosted-Space session handling.

Token names match ``research/analysis/judge/review.py`` BASE_CSS, whose static
(script-free) pages honour the same ``data-theme`` attribute and system setting.
"""
import re

MARKER = 'id="site-theme"'
STORAGE_KEY = 'gdm-theme'

LIGHT = {
    'bg': '#fafbfc', 'fg': '#18212b', 'muted': '#56637a', 'card': '#ffffff', 'line': '#dde3ea',
    'input-line': '#a9b5c4', 'accent': '#155eaa', 'chip': '#eef2f7', 'pre': '#f3f5f8', 'hl': '#fff1a6',
    'warn': '#a1260d', 'ok': '#1d6b35', 'amber': '#8a5a00', 'banner': '#eaf0fb', 'banner-line': '#6084bf',
    'sel-bg': '#204e85', 'sel-fg': '#ffffff', 'derived': '#b26b00', 'file': '#4f7c32', 'record': '#1263a3',
    'segment': '#56637a', 'host': '#fdf0e6', 'shadow': 'rgba(15,20,26,.12)',
    # Status badges (run page): text colour and background per tone.
    'good': '#1f7a3a', 'good-bg': '#e6f4ea', 'caution': '#9a5b00', 'caution-bg': '#fdf1dc',
    'bad': '#b42318', 'bad-bg': '#fde8e6', 'info': '#4b5cc4', 'info-bg': '#eceffd',
}
DARK = {
    'bg': '#0f141a', 'fg': '#e4e9ef', 'muted': '#9eabba', 'card': '#161d25', 'line': '#2a3542',
    'input-line': '#45546a', 'accent': '#80b6f0', 'chip': '#222c38', 'pre': '#10161d', 'hl': '#5a4b00',
    'warn': '#ff8f75', 'ok': '#82d69d', 'amber': '#f0c26b', 'banner': '#18263a', 'banner-line': '#6f96d6',
    'sel-bg': '#80b6f0', 'sel-fg': '#0f141a', 'derived': '#e0a040', 'file': '#8cc26a', 'record': '#6fa8e8',
    'segment': '#8d9aab', 'host': '#33261b', 'shadow': 'rgba(0,0,0,.45)',
    'good': '#6fcf8b', 'good-bg': '#14301d', 'caution': '#f0b357', 'caution-bg': '#3a2a10',
    'bad': '#f08679', 'bad-bg': '#3d1714', 'info': '#a3b0ff', 'info-bg': '#1e2547',
}


def _vars(tokens):
    return ''.join(f'--{name}:{value};' for name, value in tokens.items())


CSS = (
    f':root{{color-scheme:light;{_vars(LIGHT)}}}'
    f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;{_vars(DARK)}}}}}'
    f':root[data-theme="dark"]{{color-scheme:dark;{_vars(DARK)}}}'
    'html{-webkit-text-size-adjust:100%;text-size-adjust:100%}'
    'body{color:var(--fg);background:var(--bg)}'
    'a{color:var(--accent)}img,video,svg{max-width:100%;height:auto}'
    'pre,code{overflow-wrap:anywhere}'
    'input,select,textarea,button{color:inherit;background:var(--card);border-color:var(--input-line)}'
    ':focus-visible{outline:3px solid var(--accent);outline-offset:2px}'
    '.site-theme-toggle{position:fixed;right:12px;bottom:12px;z-index:50;font:13px/1 system-ui,sans-serif;'
    'padding:9px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--fg);'
    'box-shadow:0 2px 8px var(--shadow);cursor:pointer;min-height:36px}'
    '@media print{.site-theme-toggle{display:none}}'
    # Phone width: 16px gutters, no horizontal page scroll; wide tables scroll inside themselves.
    '@media (max-width:640px){'
    'body{margin-top:16px!important;margin-bottom:56px!important;padding-left:16px!important;'
    'padding-right:16px!important;font-size:15px}'
    'p,li,dd,h1,h2,h3,summary{overflow-wrap:anywhere}'
    'h1{font-size:1.45rem;line-height:1.25}h2{font-size:1.2rem}'
    # Wide tables scroll sideways inside their own box; cells wrap only at word boundaries.
    'table{display:block;max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}'
    'td,th{padding:8px 9px!important;overflow-wrap:normal!important;word-break:normal!important}'
    'th{white-space:nowrap}td:last-child{min-width:0!important}'
    'pre{font-size:12px!important}input,select,button{max-width:100%}'
    '}'
)

# Runs in <head> before first paint so a saved choice never flashes the other theme.
SCRIPT = (
    '(()=>{const K=%r,root=document.documentElement;'
    'let saved=null;try{saved=localStorage.getItem(K);}catch(e){}'
    'if(saved==="light"||saved==="dark")root.dataset.theme=saved;'
    'const label={auto:"Theme: auto",light:"Theme: light",dark:"Theme: dark"};'
    'function current(){return root.dataset.theme||"auto";}'
    'function add(){if(document.querySelector(".site-theme-toggle"))return;'
    'const b=document.createElement("button");b.type="button";b.className="site-theme-toggle";'
    'const show=()=>{b.textContent=label[current()];b.setAttribute("aria-label",label[current()]+" (click to change)");};'
    'b.addEventListener("click",()=>{const next={auto:"light",light:"dark",dark:"auto"}[current()];'
    'if(next==="auto")delete root.dataset.theme;else root.dataset.theme=next;'
    'try{if(next==="auto")localStorage.removeItem(K);else localStorage.setItem(K,next);}catch(e){}show();});'
    'show();document.body.append(b);}'
    'if(document.body)add();else document.addEventListener("DOMContentLoaded",add);})();'
) % STORAGE_KEY

VIEWPORT = '<meta name="viewport" content="width=device-width,initial-scale=1">'


def head(viewport=True):
    """The theme markup for a page head: colour-scheme hint, tokens/base CSS, toggle script."""
    return ((VIEWPORT if viewport else '') + '<meta name="color-scheme" content="light dark">'
            f'<style {MARKER}>{CSS}</style><script id="site-theme-toggle">{SCRIPT}</script>')


def themed(document):
    """Insert the shared theme once, before the page's own styles (so page rules still win).

    Goes right after ``<meta charset>`` (which must stay within the first 1024 bytes), else
    after ``<head>``, else after the doctype.
    A viewport meta is added only when the page has none.
    """
    if MARKER in document:
        return document
    markup = head(viewport=not re.search(r'<meta\s+name="viewport"', document, re.I))
    for pattern in (r'<meta\s+charset="?[^">]*"?\s*/?>', r'<head(?:\s[^>]*)?>', r'<!doctype[^>]*>'):
        found = re.search(pattern, document, re.I)
        if found:
            return document[:found.end()] + markup + document[found.end():]
    return markup + document
