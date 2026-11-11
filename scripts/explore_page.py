"""Explore page of the math library (owner request, November 10, 2026). PLACEHOLDER: the real page is designed
separately and replaces this file; only the signature is the contract.

``render_explore(entries, files=None, site=None) -> str | None``: the site's index.html, or None for a site without
an Explore page (the ImpossibleBench site keeps its catalogue as index.html). ``entries``: the listed catalogue
entries (newest first); ``files``: the site after the change ({path: bytes}) or None. The top bar and the
deep-link forwarder are added by the caller (publish_cloudflare.render_catalogue, scripts/site_nav.py).
"""
import html

from site_style import themed


def render_explore(entries, files=None, site=None):
    if site not in (None, 'gdm'):
        return None
    esc = html.escape
    items = ''.join(
        f'<li><a href="{esc(e["run"], quote=True)}/results.html">{esc(e.get("date", ""))} · {esc(e["run"])}</a>'
        f' <small>{esc(e.get("model", ""))}</small></li>' for e in list(entries)[:6])
    return themed('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                  '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Explore · GDM Math Swarm'
                  '</title><style>body{max-width:1120px;margin:0 auto;padding:0 24px;font:16px/1.55 system-ui,'
                  'sans-serif;background:var(--bg);color:var(--fg)}a{color:var(--link)}</style></head><body>'
                  '<h1>Explore</h1><p>Highlighted runs (placeholder). Browse every run on the '
                  f'<a href="experiments.html">Experiments</a> page.</p><ul>{items}</ul></body></html>')
