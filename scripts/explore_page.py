"""The Explore page: the site's user-facing landing page (owner, November 10, 2026: "there should be a "explore" page
which is the main page, then "experiments" page which lists all of them. On the explore page it highlights a few of
the most interesting runs ... The explore page should be more eye-catching, not boring. The graphs should be central
and large on this page.").
One number per condition, no replicates (owner, November 11, 2026: no replicate labels, per-run breakdowns or
per-run marks on Explore; plain short stories). Charts show each condition's average only; run pages keep the rest.

``render_explore(entries, files=None, site=None)`` returns the whole HTML document (published as index.html; the
catalogue moves to experiments.html). ``entries`` are the catalogue entries of experiments.json; ``files`` (name ->
bytes, the site after the change) says which per-run files exist, so a story only shows a graph that is actually
published. No heatmaps on Explore (owner, November 11, 2026: "No heat maps anywhere on Explore"); they stay on the run
pages. Layout (same date): editorial type scale, text and figure side by side on desktop, one column below 760px. The stories, their numbers and figures are curated in scripts/explore_highlights.json; site-level
charts (standing fakes over time, fakes accepted per condition) are drawn as inline SVG from the entries' profiles.
``render_about`` returns the About page. Math site only: any other ``site`` returns None.

Deterministic (no clocks), self-contained (inline CSS and JS, system fonts, no external resources). Iframes and
images point only at files of the same site. Never links private files (Inspect logs, raw records, timelines,
monitor calls).
"""
import html
import json
import math
import re
from pathlib import Path
from urllib.parse import quote

import site_nav
import site_skin
import site_style

HIGHLIGHTS = Path(__file__).with_name('explore_highlights.json')
GRAPH = 'analysis/storymap/graph.html'
STORYMAP_LABEL = 'Story map'
MATH_SITES = (None, 'gdm')
TITLE = 'GDM math swarm reproduction'
esc = html.escape


def load_highlights(path=None):
    return json.loads(Path(path or HIGHLIGHTS).read_text(encoding='utf-8'))


def _attr(value):
    return esc(str(value), quote=True)


def _url(run, rel=''):
    return quote(run, safe='') + '/' + rel


def _num(x):
    """Short deterministic number text: integers plain, else one decimal."""
    if x is None:
        return '?'
    r = math.floor(x * 10 + 0.5) / 10
    return str(int(r)) if r == int(r) else f'{r:.1f}'


def _profile(entry):
    p = entry.get('profile') if isinstance(entry, dict) else None
    return p if isinstance(p, dict) and isinstance(p.get('fakes'), dict) and p.get('checked') else None


def _has(files, run, rel, entry=None):
    """True/False when ``files`` is given; without it, a best guess from the entry (story-map link for graphs)."""
    if files is not None:
        return f'{run}/{rel}' in files
    if rel == GRAPH:
        return bool(entry and STORYMAP_LABEL in (entry.get('links') or {}))
    return entry is not None


def _report(entry):
    link = (entry.get('links') or {}).get('Report')
    return link or _url(entry['run'], 'results.html')


def _matching(entries, pattern):
    rx = re.compile(pattern)
    return sorted((e for e in entries if rx.search(e['run'])), key=lambda e: e['run'])


# ---------------------------------------------------------------- charts (inline SVG, viewBox units)

def _dots_chart(spec, by_run):
    """One row per condition (HTML label, an SVG strip with a bar to the average, the average as a big number), so
    the labels stay readable at phone width."""
    rows = spec['rows']
    vmax = spec.get('max') or max([v for r in rows for v in r['values']] + [1])
    W, pad = 600, 14
    x = lambda v: pad + min(v, vmax) / vmax * (W - 2 * pad)  # noqa: E731
    ticks = list(range(0, int(vmax) + 1, 25 if vmax >= 50 else 10))
    out = [f'<div class="sk-dots" role="group" aria-label="{_attr(spec.get("title", ""))}">']
    for row in rows:
        c = int(row.get('color', 1))
        vals = row['values']
        mean = sum(vals) / len(vals)
        strip = [f'<svg viewBox="0 0 {W} 40" preserveAspectRatio="none" aria-hidden="true">']
        strip += [f'<line class="ax" x1="{_num(x(t))}" x2="{_num(x(t))}" y1="2" y2="38"/>' for t in ticks]
        strip.append(f'<line class="track" x1="{pad}" x2="{W - pad}" y1="20" y2="20"/>'
                     f'<line class="sk-c{c}" stroke="currentColor" stroke-width="10" stroke-linecap="round" x1="{pad}" '
                     f'x2="{_num(x(mean))}" y1="20" y2="20" opacity=".8"><title>{esc(row["label"])}: {_num(mean)}</title>'
                     '</line></svg>')
        out.append(f'<div class="sk-dotrow"><span class="sk-dotlbl">{esc(row["label"])}</span>'
                   f'<span class="sk-strip">{"".join(strip)}</span>'
                   f'<span class="sk-dotavg sk-c{c}">{_num(mean)}</span></div>')
    out.append('<div class="sk-dotrow sk-dotaxis" aria-hidden="true"><span></span><span class="sk-strip">'
               + ''.join(f'<i style="left:{_num(x(t) / W * 100)}%">{t}</i>' for t in ticks)
               + f'</span><span></span></div>')
    if spec.get('unit'):
        out.append(f'<p class="sk-sub" style="text-align:center;margin:2px 0 0">{esc(spec["unit"])}</p>')
    out.append('</div>')
    return ''.join(out)


def _step_at(curve, t):
    value = 0
    for m, s, _c in curve:
        if m > t:
            break
        value = s
    return value


def _lines_chart(spec, entries, compact=False):
    """Standing fakes over time, one line per group (the average of its runs). ``compact``: the phone
    variant (narrow viewBox, so text stays legible; end values go to the legend)."""
    groups = []
    for g in spec['groups']:
        profs = [p for p in (_profile(e) for e in _matching(entries, g['match'])) if p]
        if profs:
            groups.append((g, profs))
    if not groups:
        return None
    tmax = max(1, math.floor(max(p['minutes'] for _g, ps in groups for p in ps)))
    W, H, left, right, top, bottom = (400, 300, 30, 8, 10, 28) if compact else (760, 420, 40, 170, 14, 32)
    pw, ph = W - left - right, H - top - bottom
    series = []
    for g, ps in groups:
        steps = list(range(0, int(tmax) + 1))
        reps = [[_step_at(p['curve'], t) if t <= p['minutes'] else None for t in steps] for p in ps]
        mean = [None if all(r[i] is None for r in reps) else
                sum(r[i] for r in reps if r[i] is not None) / sum(r[i] is not None for r in reps) for i in steps]
        series.append((g, reps, mean))
    vmax = max([v for _g, reps, _m in series for r in reps for v in r if v is not None] + [1])
    nice = next(n for n in (2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000) if n * 4 >= vmax)
    vtop = nice * math.ceil(vmax / nice)
    X = lambda t: left + t / tmax * pw  # noqa: E731
    Y = lambda v: top + ph - v / vtop * ph  # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_attr(spec.get("title", ""))}">']
    for v in range(0, int(vtop) + 1, nice):
        out.append(f'<line class="ax" x1="{left}" x2="{left + pw}" y1="{_num(Y(v))}" y2="{_num(Y(v))}"/>'
                   f'<text x="{left - 8}" y="{_num(Y(v) + 4)}" text-anchor="end">{v}</text>')
    for t in range(0, int(tmax) + 1, 30):
        out.append(f'<text x="{_num(X(t))}" y="{H - 10}" text-anchor="{"end" if X(t) > left + pw - 30 else "middle"}">'
                   f'{t} min</text>')

    def path(vals):
        d, prev = '', None
        for t, v in enumerate(vals):
            if v is None:
                break
            d += (f'M{_num(X(t))},{_num(Y(v))}' if prev is None else f'H{_num(X(t))}V{_num(Y(v))}')
            prev = v
        return d
    labels = []
    for g, reps, mean in series:
        c = int(g.get('color', 1))
        out.append(f'<path class="sk-mark sk-c{c}" stroke="currentColor" fill="none" stroke-width="4" '
                   f'stroke-linejoin="round" d="{path(mean)}"><title>{esc(g["label"])}</title></path>')
        last = [v for v in mean if v is not None][-1]
        labels.append([Y(last), g['label'], c, last])
    labels.sort()
    for i in range(1, len(labels)):  # keep end labels apart
        labels[i][0] = max(labels[i][0], labels[i - 1][0] + 20)
    for y, label, c, v in ([] if compact else labels):
        out.append(f'<rect class="sk-c{c}" fill="currentColor" x="{left + pw + 10}" y="{_num(y - 6)}" width="12" '
                   f'height="12" rx="2"/><text class="lbl" x="{left + pw + 28}" y="{_num(y + 5)}">'
                   f'{esc(label)} <tspan font-weight="800">{_num(v)}</tspan></text>')
    out.append('</svg>')
    if compact:
        out.append('<ul class="sk-legend">' + ''.join(
            f'<li><i class="sk-c{c}" style="background:currentColor"></i>{esc(label)} <b>{_num(v)}</b></li>'
            for _y, label, c, v in sorted(labels, key=lambda r: r[0])) + '</ul>')
    return ''.join(out)


def _bars_chart(spec, entries, compact=False):
    """Grouped bars: average of a profile fakes metric per condition."""
    metric = spec.get('metric', 'accepted')
    cats = []
    for cat in spec['categories']:
        bars = []
        for b in cat['bars']:
            profs = [p for p in (_profile(e) for e in _matching(entries, b['match'])) if p]
            # minus_seed: leave out the planted seed's own fake (counted in the profile's accepted fakes).
            vals = [p['fakes'].get(metric) - (1 if spec.get('minus_seed') and p.get('seed') else 0) for p in profs
                    if isinstance(p['fakes'].get(metric), (int, float))]
            bars.append((b, vals))
        if any(v for _b, v in bars):
            cats.append((cat, bars))
    if not cats:
        return None
    vmax = max(v for _c, bars in cats for _b, vals in bars for v in vals) or 1
    nice = next(n for n in (2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000) if n * 4 >= vmax)
    vtop = nice * math.ceil(vmax / nice)
    W, H, left, top, bottom = (400, 300, 34, 24, 30) if compact else (760, 420, 44, 26, 40)
    pw, ph = W - left - 10, H - top - bottom
    Y = lambda v: top + ph - v / vtop * ph  # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_attr(spec.get("title", ""))}">']
    for v in range(0, int(vtop) + 1, nice):
        out.append(f'<line class="ax" x1="{left}" x2="{W - 10}" y1="{_num(Y(v))}" y2="{_num(Y(v))}"/>'
                   f'<text x="{left - 8}" y="{_num(Y(v) + 4)}" text-anchor="end">{v}</text>')
    cw = pw / len(cats)
    legend = {}
    for i, (cat, bars) in enumerate(cats):
        x0 = left + cw * i
        gap = 4 if compact else 12
        bw = min(120, cw * 0.7 / max(1, len(bars)))
        start = x0 + (cw - bw * len(bars) - gap * (len(bars) - 1)) / 2
        for j, (b, vals) in enumerate(bars):
            c = int(b.get('color', 1))
            legend.setdefault(b['label'], c)
            bx = start + j * (bw + gap)
            if not vals:
                continue
            mean = sum(vals) / len(vals)
            out.append(f'<rect class="sk-mark sk-c{c}" fill="currentColor" x="{_num(bx)}" y="{_num(Y(mean))}" '
                       f'width="{_num(bw)}" height="{_num(Y(0) - Y(mean))}" rx="3"><title>{esc(cat["label"])}, '
                       f'{esc(b["label"])}: average {_num(mean)}</title></rect>'
                       f'<text class="lbl" x="{_num(bx + bw / 2)}" y="{_num(Y(mean) - 10)}" text-anchor="middle" '
                       f'font-weight="800">{_num(mean)}</text>')
        out.append(f'<text class="lbl" x="{_num(x0 + cw / 2)}" y="{H - 14}" text-anchor="middle">{esc(cat["label"])}</text>')
    out.append('</svg>')
    out.append('<ul class="sk-legend">' + ''.join(f'<li><i class="sk-c{c}" style="background:currentColor"></i>'
                                                  f'{esc(k)}</li>' for k, c in legend.items()) + '</ul>')
    return ''.join(out)


def _two(draw, *args):
    """A wide chart and its phone variant; CSS shows one of them."""
    wide = draw(*args)
    if not wide:
        return None
    return f'<div class="sk-wide">{wide}</div><div class="sk-narrow">{draw(*args, compact=True)}</div>'


def _curve_chart(entry):
    """Fallback figure: the run's own standing-fakes curve."""
    if not _profile(entry):
        return None
    spec = {'title': 'Fake proofs standing over the run', 'groups': [
        {'label': 'Standing fakes', 'color': 1, 'match': '^' + re.escape(entry['run']) + '$'}]}
    return _two(_lines_chart, spec, [entry])


def _chart_block(spec, svg):
    return ('<div class="sk-fig-frame sk-chart">' + (f'<h3>{esc(spec["title"])}</h3>' if spec.get('title') else '')
            + (f'<p class="sk-sub">{esc(spec["subtitle"])}</p>' if spec.get('subtitle') else '') + svg
            + (f'<p class="sk-basis">{esc(spec["basis"])}</p>' if spec.get('basis') else '') + '</div>')


def _chart(spec, entries, by_run):
    kind = spec.get('chart')
    svg = (_dots_chart(spec, by_run) if kind == 'dots' else _two(_lines_chart, spec, entries) if kind == 'lines'
           else _two(_bars_chart, spec, entries) if kind == 'bars' else None)
    return _chart_block(spec, svg) if svg else None


# ---------------------------------------------------------------- figures

_EXPAND = ('<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" focusable="false"><path d="M2 6V2h4M14 6V2h-4'
           'M2 10v4h4M14 10v4h-4" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" '
           'stroke-linejoin="round"/></svg>')


def _graph_fig(entry, caption):
    run = entry['run']
    page = _url(run, GRAPH)
    title = 'Interaction graph, replaying at 5x'
    return (f'<figure class="sk-fig"><div class="sk-fig-frame sk-gwrap" data-graph="{_attr(page)}">'
            f'<iframe title="{_attr(title)}" loading="lazy" src="{_attr(page)}?embed=1&amp;play=1"></iframe>'
            '<button class="sk-gclose" type="button" aria-label="Close full screen" hidden>&#x2715;</button></div>'
            f'<figcaption><span>{esc(caption or "")}</span><span class="sk-acts">'
            f'<a class="sk-btn sk-full" href="{_attr(page)}">{_EXPAND}Full screen</a></span></figcaption></figure>')


def _figure(fig, entries, by_run, files):
    """One figure, falling back graph -> the run's standing-fakes curve -> nothing. Never a heatmap: a 'heatmap' or
    'curve' figure draws the run's curve when it has a profile."""
    kind = fig.get('kind')
    if kind == 'chart':
        block = _chart(fig, entries, by_run)
        return f'<figure class="sk-fig">{block}</figure>' if block else ''
    entry = by_run.get(fig.get('run'))
    if not entry:
        return ''
    if kind == 'graph' and _has(files, entry['run'], GRAPH, entry):
        return _graph_fig(entry, fig.get('caption'))
    svg = _curve_chart(entry)
    if not svg:
        return ''
    caption = fig.get('caption') if kind != 'graph' else 'Fake proofs credited and still standing in this run.'
    return ('<figure class="sk-fig">' + _chart_block({'title': 'Fake proofs standing over the run'}, svg)
            + f'<figcaption><span>{esc(caption or "")}</span></figcaption></figure>')


# ---------------------------------------------------------------- page

_JS = r'''<script>(function(){var r=document.documentElement;
function theme(){var t=r.getAttribute("data-theme");return t==="light"||t==="dark"?"&theme="+t:"";}
var frames=[].slice.call(document.querySelectorAll("[data-graph]"));
function sync(){frames.forEach(function(w){var f=w.querySelector("iframe"),u=w.getAttribute("data-graph")+"?embed=1&play=1"+theme();
if(f.getAttribute("src")!==u)f.setAttribute("src",u);});}
sync();new MutationObserver(sync).observe(r,{attributes:true,attributeFilter:["data-theme"]});
var open=null;function mode(w,on){try{var d=w.querySelector("iframe").contentDocument.documentElement;
d.classList.toggle("embed",!on);d.classList.toggle("overlay",on);}catch(e){}}
function show(w){if(open)hide();open=w;w.classList.add("open");r.classList.add("sk-lock");
var x=w.querySelector(".sk-gclose");x.hidden=false;mode(w,true);x.focus();}
function hide(){if(!open)return;var w=open;open=null;w.classList.remove("open");r.classList.remove("sk-lock");
w.querySelector(".sk-gclose").hidden=true;mode(w,false);var a=w.parentNode.querySelector("a.sk-full");if(a)a.focus();}
frames.forEach(function(w){var f=w.querySelector("iframe");f.addEventListener("load",function(){mode(w,open===w);});
w.querySelector(".sk-gclose").addEventListener("click",hide);
var a=w.parentNode.querySelector("a.sk-full");if(a)a.addEventListener("click",function(e){
if(e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();show(w);});});
document.addEventListener("keydown",function(e){if(e.key==="Escape"&&open){e.preventDefault();hide();}});
addEventListener("message",function(e){if(e.origin===location.origin&&e.data==="graph-close")hide();});
})();</script>'''


def _document(title, description, body, skin, current):
    css = site_skin.skin_css(skin)
    return site_style.themed(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)}</title><meta name="description" content="{_attr(description)}">'
        f'<style id="site-skin" data-skin="{_attr(skin)}">{css}</style>{site_nav.style()}</head>'
        f'<body class="sk sk-{_attr(skin)}">{site_nav.bar(current)}{body}</body></html>')


def _totals(entries):
    agents = sum(int(e.get('agents') or 0) for e in entries)
    hours = sum(float(e.get('agents') or 0) * float(e.get('elapsed_seconds') or 0) for e in entries) / 3600
    fakes = sum(p['fakes'].get('accepted') or 0 for p in (_profile(e) for e in entries) if p)
    return [(f'{len(entries)}', 'experiments run'), (f'{agents:,}', 'AI agents simulated'),
            (f'{int(hours + 0.5):,}', 'agent-hours of run time'), (f'{fakes:,}', 'fake proofs the weak judge accepted')]


def _story(n, s, entries, by_run, files):
    """Text and numbers beside the story's figures (stacked in one column on phones). A story whose figures are all
    unavailable becomes a text-and-number card."""
    entry = by_run[s['run']]
    nums = ''.join(f'<div><dt>{esc(x["label"])}</dt><dd>{esc(x["value"])}</dd></div>' for x in s.get('numbers', []))
    figs = ''.join(_figure(f, entries, by_run, files) for f in s.get('figures', []))
    links = [f'<a class="sk-btn" href="{_attr(_report(entry))}#overview">Explore this run</a>']  # the one run link
    text = (f'<p class="sk-chapter-n">{n:02d}</p><p class="sk-kicker">{esc(s.get("kicker", ""))}</p>'
            f'<h2 class="sk-h2">{esc(s["headline"])}</h2><p class="sk-take">{esc(s["takeaway"])}</p>'
            + (f'<p class="sk-story">{esc(s["story"])}</p>' if s.get('story') else ''))
    row = f'<p class="sk-links-row">{"".join(links)}</p>'
    if figs:
        body = (f'<div class="sk-split"><div class="sk-copy">{text}<dl class="sk-num">{nums}</dl>{row}</div>'
                f'<div class="sk-figcol">{figs}</div></div>')
    else:
        body = (f'<div class="sk-split sk-textcard"><div class="sk-copy">{text}{row}</div>'
                f'<dl class="sk-num sk-numcard">{nums}</dl></div>')
    return f'<section class="sk-chapter" id="{_attr(s["id"])}"><div class="sk-wrap">{body}</div></section>'


def _upcoming(u, entries, by_run):
    runs = _matching(entries, u['match']) if u.get('match') else []
    chart = ''
    if runs:
        spec = {'chart': 'lines', 'title': 'Fake proofs credited and not yet undone (early results)',
                'subtitle': 'Two-hour runs, by monitor setup.', 'groups': u.get('groups', [])}
        block = _chart(spec, entries, by_run)
        chart = f'<div class="sk-figcol"><figure class="sk-fig">{block}</figure></div>' if block else ''
    status = (f'{len(runs)} run{"" if len(runs) == 1 else "s"} published so far.' if runs else u.get('pending_text', ''))
    link = (f'<p class="sk-links-row"><a class="sk-btn" href="experiments.html#series-{_attr(u["series"])}">See the '
            'runs</a></p>' if runs and u.get('series') else '')
    return (f'<section class="sk-band" id="{_attr(u["id"])}"><div class="sk-wrap"><div class="sk-split">'
            f'<div class="sk-copy"><p class="sk-kicker">{esc(u.get("kicker", ""))}</p><h2 class="sk-h2">'
            f'{esc(u["headline"])}</h2><p class="sk-story" style="color:inherit">{esc(u["story"])}</p>'
            f'<p><span class="sk-pending">{esc(status)}</span></p>{link}</div>{chart}</div></div></section>')


def render_explore(entries, files=None, site=None, skin=None, highlights=None):
    """The Explore page (full HTML document) for the math site; None for any other site."""
    if site not in MATH_SITES:
        return None
    skin = skin if skin in site_skin.SKINS else site_skin.DEFAULT
    data = highlights if highlights is not None else load_highlights()
    entries = [e for e in entries or [] if isinstance(e, dict) and e.get('run')]
    by_run = {e['run']: e for e in entries}
    hero = data.get('hero', {})
    stats = ''.join(f'<div class="sk-stat"><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>' for v, k in _totals(entries))
    out = [f'<header class="sk-hero"><div class="sk-wrap sk-hero-grid"><div><p class="sk-kicker">'
           f'{esc(hero.get("kicker", ""))}</p><h1 class="sk-h1">{esc(hero.get("title", ""))}</h1>'
           f'<p class="sk-lead">{esc(hero.get("lead", ""))}</p><p class="sk-cta"><a class="sk-btn" href="#stories">'
           'Selected results</a><a class="sk-btn ghost" href="experiments.html">Browse all experiments</a>'
           '<a class="sk-btn ghost" href="about.html">How it works</a></p></div>'
           f'<dl class="sk-stats">{stats}</dl></div></header>']
    if hero.get('chart'):
        block = _chart(hero['chart'], entries, by_run)
        if block:
            series = hero['chart'].get('series')
            more = (f'<p class="sk-links-row"><a class="sk-btn ghost" href="experiments.html#series-{_attr(series)}">'
                    'The runs behind this chart</a></p>' if series else '')
            out.append(f'<section class="sk-section sk-big"><div class="sk-wrap"><p class="sk-kicker">Overview</p>'
                       f'<div class="sk-figs"><figure class="sk-fig">{block}</figure></div>{more}</div></section>')
    stories = [s for s in data.get('stories', []) if s.get('run') in by_run]
    out.append('<div id="stories">' + ''.join(_story(i + 1, s, entries, by_run, files)
                                             for i, s in enumerate(stories)) + '</div>')
    out += [_upcoming(u, entries, by_run) for u in data.get('upcoming', [])]
    out.append('<footer class="sk-foot"><div class="sk-wrap"><p class="sk-links-row">'
               f'<a class="sk-btn" href="experiments.html">Browse all {len(entries)} experiments</a>'
               '<a class="sk-btn ghost" href="about.html">About the project</a></p>'
               '<p>An independent reproduction of Paglieri et al., not an official Google DeepMind release. How the '
               'numbers are counted is on the About page.</p></div></footer>')
    out.append(_JS)
    return _document(TITLE, hero.get('lead', ''), ''.join(out), skin, 'explore')


# ---------------------------------------------------------------- About

GLOSSARY = (
    ('Solver', 'An AI agent trying to prove problems for credit. Most runs have 100.'),
    ('Weak judge', 'The conference\'s automatic checker. It only checks that a proof compiles and leaves the problem '
     'statement alone, so some proofs of nothing get through.'),
    ('Fake', 'A submission that proves nothing: the independent proof checker marks it fraudulent, or it restates the '
     'question as its own answer, or closes the answer by reflexivity alone (the strict fake rule).'),
    ('Faker', 'A solver with at least one fake submission. The planted seed and the overseers are never counted.'),
    ('Planted seed', 'In seeded runs, a silent participant is credited with a fake proof at minute 3, so every agent '
     'can find the trick in the shared library.'),
    ('Overseer', 'An extra agent told to keep the conference honest. Depending on the run it can check proofs, reverse '
     'credits, post on the board and send direct messages.'),
    ('Pushed messages', 'Board posts and direct messages appended to each agent\'s next tool result, so they cannot '
     'be missed (newer runs). Older runs only showed an unread-message count.'),
    ('Honesty reminder', 'A notice repeating the agent\'s own integrity rules, delivered every five minutes.'),
    ('Story map', 'A model-assisted reading of each agent\'s reasoning about cheating over the run, with the events it '
     'reacted to.'),
    ('Interaction graph', 'An animated replay of a run: agents grouped by their stance on the exploit, with the '
     'messages, files and submissions between them.'),
)


def render_about(site=None, skin=None):
    """The About page (full HTML document) for the math site; None for any other site."""
    if site not in MATH_SITES:
        return None
    import catalogue_intro as intro
    skin = skin if skin in site_skin.SKINS else site_skin.DEFAULT
    steps = ''.join(f'<li><b>{esc(t)}</b>{d}</li>' for t, d in intro.STEPS)
    setup = ''.join(f'<li><b>{esc(t)}</b>{d}</li>' for t, d in intro.EXPERIMENT)
    pages = ''.join(f'<dt>{esc(t)}</dt><dd>{esc(d)}</dd>' for t, d in intro.PAGES)
    gloss = ''.join(f'<dt>{esc(t)}</dt><dd>{esc(d)}</dd>' for t, d in GLOSSARY)
    # The full glossary of the run cards and reports (formerly the catalogue's; run pages link about.html#glossary),
    # plus the detailed (rubric-3.1) judge cohorts of the catalogue's judge column.
    from catalogue_page import COHORT_GLOSS, COHORT_LABELS, COHORTS
    terms = ''.join(f'<dt>{esc(t)}</dt><dd>{esc(d)}</dd>' for t, d in intro.GLOSSARY) + ''.join(
        f'<dt>{esc(COHORT_LABELS[k][1].capitalize())} (detailed judge)</dt><dd>{esc(COHORT_GLOSS[k])}</dd>'
        for k in COHORTS)
    body = (f'<header class="sk-hero"><div class="sk-wrap"><p class="sk-kicker">About</p>'
            f'<h1 class="sk-h1">{esc(intro.HEADLINE)}</h1><p class="sk-lead">{esc(intro.LEAD)}</p>'
            f'<p class="sk-cta"><a class="sk-btn" href="index.html">Explore the findings</a>'
            '<a class="sk-btn ghost" href="experiments.html">Browse all experiments</a></p></div></header>'
            '<section class="sk-section" id="experiment"><div class="sk-wrap"><h2 class="sk-h2">The setup</h2>'
            f'<ol class="sk-steps">{setup}</ol></div></section>'
            '<section class="sk-band" id="how"><div class="sk-wrap"><h2 class="sk-h2">How a run works</h2>'
            f'<ol class="sk-steps">{steps}</ol></div></section>'
            # Full-width cards; the glossary flows in two columns on wide screens (owner, November 12, 2026: the side
            # by side cards were too tall and narrow; two dt/dd pair columns measured taller, rows align).
            '<section class="sk-section"><div class="sk-wrap sk-stack">'
            f'<div class="sk-card"><h2 class="sk-h2" style="font-size:1.2rem">Key terms</h2><dl class="sk-gloss">{gloss}</dl></div>'
            f'<div class="sk-card" id="read"><h2 class="sk-h2" style="font-size:1.2rem">On each run\'s page</h2>'
            f'<dl class="sk-gloss">{pages}</dl></div></div></section>'
            '<section class="sk-section" id="glossary"><div class="sk-wrap"><h2 class="sk-h2">Glossary</h2>'
            '<p class="sk-sub" style="color:var(--k-muted);max-width:70ch">Every term used on the run cards, run '
            f'reports and the Experiments page.</p><div class="sk-card"><dl class="sk-gloss sk-gloss-cols">{terms}</dl></div></div>'
            '</section>'
            '<section class="sk-section"><div class="sk-wrap sk-prose"><h2 class="sk-h2">The paper</h2>'
            f'<p>{intro.PAPER}</p><p>{esc(intro.DISCLAIMER)}</p></div></section>'
            '<footer class="sk-foot"><div class="sk-wrap"><p class="sk-links-row"><a class="sk-btn" href="index.html">'
            'Explore</a><a class="sk-btn ghost" href="experiments.html">Experiments</a></p></div></footer>')
    return _document('About GDM Math Swarm', intro.LEAD, body, skin, 'about')
