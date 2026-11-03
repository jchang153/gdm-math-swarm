"""Story map viewer: offline static HTML from the stage-0 timelines and the stage 1-3 outputs.

    python -m research.analysis.storymap.view --map runs/<id>/analysis/storymap --timeline DIR [--out DIR]

Writes (default ``--out`` = ``--map``):

* ``index.html``: one row per agent; stance phases as coloured spans on a 0-180 min axis (longer
  when the run is longer), event glyphs (library reads, overseer posts, DMs, reverts, submissions),
  final labels, and filters by source / faked / stopped / credited.
* ``agents/<agent>.html``: lanes (stance; thoughts by code letter A-G; library; board and DMs;
  status and reverts; submissions; own actions and restarts). Clicking a glyph shows its quote or
  event text with the stage-2 reaction; every item is also listed in time order below the lanes.
* ``validation.html``: ``validation.md`` (agreement with the hand labels) as a page, when it exists.
* ``events.html``: each overseer post or DM, other relevant board post, revert and library file,
  with who read it and when, and the breakdown of stage-2 reactions (none / belief / behaviour
  by direction).

Inputs follow private/research/analysis/storymap/INTERFACE.md. Every input is optional: an agent
with only a timeline, or only stage outputs, still gets a row, and missing stages are stated on the
page instead of guessed. Code-letter names come from the researcher-only taxonomy when it exists
(``--taxonomy``; pass ``--taxonomy ''`` for pages bound for the public site, which then show the code
letters only); this module holds no taxonomy text. The pages, ``validation.md`` and its page may be published
(owner, November 2, 2026; scripts/publish_cloudflare.py storymap); timelines and stage records stay local.
Each page has a legend (bar colour = stance, shape = event) and, on agent pages, a detail panel that steps
through the items in time order (arrow keys or j / k, Prev / Next, a kind filter, Esc closes). No network, no
external assets (the shared site theme is inlined); a little vanilla JavaScript.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))
from site_style import themed  # noqa: E402  (shared light/dark tokens and toggle, no external loads)

DEFAULT_TAXONOMY = REPO / "private" / "research" / "analysis" / "storymap" / "taxonomy.md"
AXIS_MIN = 180.0
LETTERS = "ABCDEFG"
STANCES = ("none", "considering", "deferred", "rejected", "faking", "stopped")
CHANGES = ("none", "belief", "behaviour")
DIRECTIONS = ("toward_cheating", "away", "neutral", None)
DIRECTION_LABEL = {"toward_cheating": "toward cheating", "away": "away", "neutral": "neutral", None: "no direction"}
RELEVANT_TAGS = {"describes_trick", "warning", "ruling", "revert_notice"}
TEXT_CAP = 4000

LANES = (  # (key, label, event types)
    ("library", "Library: event L1, L2", ("L1", "L2")),
    ("board", "Board/DMs: event B1, D1", ("B1", "D1")),
    ("status", "Status: event P1, P2, N1", ("P1", "P2", "N1")),
    ("subs", "Submissions: event S1", ("S1",)),
    ("own", "Own/restarts: event O1, C1", ("O1", "C1")),
)
TYPE_NAME = {"L1": "library listing", "L2": "library file read", "P1": "status / problem listing",
             "P2": "revert seen", "B1": "board post", "D1": "direct message", "S1": "submission",
             "O1": "own post / DM / note", "C1": "compaction / restart", "N1": "unread notice"}

# Page tokens (light, then dark), on top of the shared site theme in scripts/site_style.py.
# Two palettes that share no hue: five stance hues, used only by the stance bars, and two event hues, used only by
# event glyphs (pink = fake, cyan = from an overseer). Everything else on a glyph is neutral: ordinary events are
# grey outlines, thoughts are ink ticks, a submission's outcome is a mark (check = accepted or credited, slash = not
# accepted). The seven hues were validated together (dataviz validate_palette.js, --pairs all: lightness band,
# chroma, CVD >= 8, normal vision >= 15, contrast >= 3:1) on every surface they sit on: light #fafbfc, #ffffff,
# #f1f4f7, #e9edf2; dark #0f141a, #161d25, #18202a, #1d2631. "none" is a neutral grey bar and "unknown" an outline.
TOKENS_LIGHT = {"st-none": "#a7b1bd", "st-considering": "#9c8133", "st-deferred": "#1440bd",
                "st-rejected": "#027651", "st-faking": "#b41e07", "st-stopped": "#8254f9",
                "ev-fake": "#e13c85", "ev-ov": "#2593af",
                "lane": "#f1f4f7", "lane-alt": "#e9edf2", "panel": "#ffffff"}
TOKENS_DARK = {"st-none": "#5b6675", "st-considering": "#a3933c", "st-deferred": "#2b6dec",
               "st-rejected": "#337f53", "st-faking": "#c7461b", "st-stopped": "#9457b3",
               "ev-fake": "#e95296", "ev-ov": "#1aa0be",
               "lane": "#18202a", "lane-alt": "#1d2631", "panel": "#161d25"}


def _vars(tokens):
    return "".join(f"--{k}:{v};" for k, v in tokens.items())


CSS = (
    f":root{{{_vars(TOKENS_LIGHT)}}}"
    f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{_vars(TOKENS_DARK)}}}}}'
    f':root[data-theme="dark"]{{{_vars(TOKENS_DARK)}}}'
    """
:root{font-family:system-ui,sans-serif;line-height:1.5}
body{max-width:1320px;margin:28px auto;padding:0 24px;background:var(--bg);color:var(--fg)}
h1{margin:0 0 6px;font-size:1.6rem}h2{margin:28px 0 8px;font-size:1.2rem}h3{margin:0 0 6px;font-size:1.02rem}
.muted,small{color:var(--muted)}.banner{background:var(--banner);border-left:4px solid var(--banner-line);
padding:10px 16px;margin:12px 0}nav.top{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0 14px}
.badge{display:inline-block;background:var(--chip);padding:1px 7px;border-radius:4px;font-size:12px;
margin:1px 2px 1px 0;white-space:nowrap}.b-bad{background:var(--chip);color:var(--fg);box-shadow:inset 0 0 0 1px var(--fg);font-weight:600}
.b-good{background:var(--chip);color:var(--fg)}.b-caution{background:var(--chip);color:var(--fg);
box-shadow:inset 0 0 0 1px var(--muted)}.b-info{background:var(--chip);color:var(--fg)}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:14px 0}
.filters label{display:flex;gap:6px;align-items:center;font-size:14px}
select,button{font:inherit;padding:6px 9px;border:1px solid var(--input-line);border-radius:6px;
background:var(--card);color:inherit}button{cursor:pointer}
.sw{display:inline-block;width:18px;height:10px;border-radius:2px;flex:none}
details.legend{position:static;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
padding:4px 0 6px;margin:6px 0 10px}details.legend>summary{cursor:pointer;font-size:13px;font-weight:600;padding:2px 0}
.lg{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.6fr);gap:8px 14px;font-size:12.5px;line-height:1.35}
.lgbox{border:1px solid var(--line);border-radius:8px;padding:4px 10px 6px;background:var(--card)}
.lg ul.marks,.lg ul.ex{margin-top:4px;padding-top:4px;border-top:1px dashed var(--line)}.lg svg.swatch{width:22px}
.lg h4{margin:4px 0 3px;font-size:12.5px}.lg ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:3px 14px}
.lg li{display:inline-flex;gap:5px;align-items:center}.lg svg{width:18px;height:16px;flex:none;overflow:visible}
.lg .note{color:var(--muted);margin:3px 0 0}.lg .g{cursor:default}
.hglyph{width:16px;height:14px;vertical-align:-2px;margin-right:6px;overflow:visible}
.stepper{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding-bottom:8px;margin-bottom:8px;
border-bottom:1px solid var(--line);font-size:13px}.stepper button{padding:3px 9px}.stepper select{padding:3px 6px}
#pos{min-width:9em;font-variant-numeric:tabular-nums}#pclose{margin-left:auto}.keys{font-size:12px;margin:8px 0 0}
section.ev.cur{outline:2px solid var(--fg)}section.ev{scroll-margin-top:var(--legend-h,0px)}
.grid{display:grid;grid-template-columns:96px minmax(420px,1fr) 300px;gap:0 12px;align-items:center}
.grid>.hd{font-size:12px;color:var(--muted);padding:4px 0}
.row{display:contents}.row>*{border-top:1px solid var(--line);padding:5px 0}
.row:hover>*{background:var(--pick)}.agent a{font-weight:600;text-decoration:none}
.strip svg{display:block;width:100%;height:30px}.labels{font-size:12px}
svg text{fill:var(--muted);font:11px system-ui,sans-serif}
.ph-none{fill:var(--st-none)}.ph-considering{fill:var(--st-considering)}.ph-deferred{fill:var(--st-deferred)}
.ph-rejected{fill:var(--st-rejected)}.ph-faking{fill:var(--st-faking)}.ph-stopped{fill:var(--st-stopped)}
.g{cursor:pointer;stroke-width:1.3}.g:hover{stroke-width:2.4}
.g .m{fill:var(--card);stroke:var(--muted);stroke-width:1.4}.g.ring .m{stroke:var(--ev-ov)}
.g.g-fill .m{fill:var(--ev-fake);stroke:var(--ev-fake)}.g .ring{fill:none;stroke:var(--ev-ov);stroke-width:1.4}.g .mk{fill:none;stroke:var(--fg);stroke-width:1.6;stroke-linecap:round}
.g .halo{fill:none;stroke:var(--card);stroke-width:4;stroke-linecap:round}.g .hit{fill:transparent;stroke:none}
.g .x{fill:none;stroke:var(--fg);stroke-width:2.4;stroke-linecap:round}.g.g-other .m{fill:var(--muted);stroke:none}
.g.g-th .m{fill:var(--fg);stroke:var(--fg);stroke-width:.8}.g.g-th-low .m{fill:var(--card);stroke:var(--fg);stroke-width:1}
.g:hover .m,.g:hover .x{stroke-width:2.2}.g.sel .m,.g.sel .ring{stroke-width:2.6}.g.sel .x{stroke-width:3.4}
.tp.sel{stroke-width:3;stroke-dasharray:none}.cursor{stroke:var(--fg);stroke-width:1.5;opacity:.55;pointer-events:none}
.ph-unknown{fill:none;stroke:var(--muted);stroke-dasharray:3 2}
.lane-bg{fill:var(--lane)}.lane-bg.alt{fill:var(--lane-alt)}.tick{stroke:var(--line)}
.tp{stroke:var(--fg);stroke-dasharray:3 3;stroke-width:1.2;cursor:pointer}
.lanes{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin:12px 0}
.lanes svg{display:block;width:100%;height:auto}
.layout{display:block}
#panel{position:fixed;right:16px;bottom:16px;width:min(500px,calc(100vw - 32px));z-index:30;background:var(--panel);
border:1px solid var(--line);border-radius:10px;padding:10px 14px;max-height:44vh;overflow:auto;font-size:14px;
box-shadow:0 6px 24px var(--shadow,rgba(0,0,0,.25))}
#panel:not(.open) #pbody,#panel:not(.open) .keys{display:none}#panel:not(.open) .stepper{border:0;margin:0;padding:0}
body.has-panel{padding-bottom:80px}body.has-panel.panel-open{padding-bottom:46vh}
article.item{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);
border-radius:8px;padding:9px 12px;margin:8px 0;font-size:14px;scroll-margin-top:calc(var(--legend-h,0px) + 12px)}
article.item.k-th{border-left-color:var(--muted)}article.item.k-ov{border-left:5px double var(--fg)}
article.item.k-fake{border-left-color:var(--fg)}article.item.k-tp{border-left-style:dashed;border-left-color:var(--fg)}
article.item.hl{outline:2px solid var(--fg)}
blockquote{margin:6px 0;padding:4px 10px;border-left:3px solid var(--input-line);white-space:pre-wrap;
overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--pre);padding:8px 10px;
border-radius:6px;font:12px/1.5 ui-monospace,monospace;margin:6px 0;max-height:320px;overflow:auto}
.react{background:var(--chip);border-radius:6px;padding:6px 10px;margin:6px 0}
.react p{margin:3px 0}dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:2px 10px;margin:6px 0}
dl.kv dt{color:var(--muted)}dl.kv dd{margin:0}
table{border-collapse:collapse;margin:8px 0;font-size:14px}th,td{border:1px solid var(--line);padding:4px 8px;
text-align:left;vertical-align:top}th{background:var(--chip);font-weight:600}td.n{text-align:right}
section.ev{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
.empty{padding:16px;color:var(--muted);border:1px dashed var(--input-line);border-radius:8px}
.story{max-width:900px}[hidden]{display:none!important}
@media (max-width:1000px){#panel{left:6px;right:6px;bottom:6px;width:auto;padding:8px 10px}
.grid{grid-template-columns:80px minmax(0,1fr)}.grid>.labels,.grid>.hd.lab{grid-column:2}}
@media (max-width:700px){body{padding:0 16px;margin:16px auto}.lg{grid-template-columns:1fr}}
@media (min-width:701px) and (min-height:600px){details.legend{position:sticky;top:0}}
"""
)

PANEL_JS = r"""
(()=>{const panel=document.getElementById('panel');if(!panel)return;
const body=document.getElementById('pbody'),pos=document.getElementById('pos'),kind=document.getElementById('kind'),
prev=document.getElementById('pprev'),next=document.getElementById('pnext'),
all=[...document.querySelectorAll('#items article.item[id]')];let cur=null;
document.body.classList.add('has-panel');
function ok(a){return !kind.value||a.dataset.kind===kind.value;}
function list(){return all.filter(ok);}
function status(){const L=list(),src=cur&&document.getElementById(cur),i=src?L.indexOf(src):-1;
const t=src?' \u00b7 t='+src.dataset.t+' min':'';
pos.textContent=src?(i>=0?(i+1)+' of '+L.length:'('+kind.selectedOptions[0].text.toLowerCase()+': '+L.length+')')+t
:L.length+' '+(kind.value?kind.selectedOptions[0].text.toLowerCase():'items');
const ix=src?all.indexOf(src):-1;
prev.disabled=!all.some((a,j)=>ok(a)&&(ix<0?true:j<ix));next.disabled=!all.some((a,j)=>ok(a)&&j>ix);}
function show(key,how){const src=document.getElementById(key);if(!src||!src.matches('article.item'))return;cur=key;
document.querySelectorAll('.sel').forEach(e=>e.classList.remove('sel'));
document.querySelectorAll('[data-k="'+key+'"]').forEach(e=>e.classList.add('sel'));
all.forEach(a=>a.classList.toggle('hl',a===src));body.innerHTML=src.innerHTML;body.scrollTop=0;panel.scrollTop=0;
panel.classList.add('open');document.body.classList.add('panel-open');status();
const g=document.querySelector('.lanes [data-k="'+key+'"]'),cu=document.getElementById('cursor');
if(cu&&g&&g.getBBox){const b=g.getBBox(),x=g.tagName==='line'?+g.getAttribute('x1'):b.x+b.width/2;
cu.setAttribute('x1',x);cu.setAttribute('x2',x);cu.setAttribute('visibility','visible');}
if(how!=='click'&&g)reveal(g);
try{history.replaceState(null,'','#sel='+key);}catch(e){}}
function reveal(el){const r=el.getBoundingClientRect(),
top=(parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--legend-h'))||0)+8,
bot=innerHeight-panel.offsetHeight-12;if(r.top<top||r.bottom>bot)scrollBy(0,r.top-(top+Math.max(bot-top,0)/2));}
function close(){cur=null;const cu=document.getElementById('cursor');if(cu)cu.setAttribute('visibility','hidden');document.querySelectorAll('.sel').forEach(e=>e.classList.remove('sel'));
all.forEach(a=>a.classList.remove('hl'));panel.classList.remove('open');document.body.classList.remove('panel-open');
body.innerHTML='<p class="muted">Click a shape or a turning-point line, or press \u2192, to read the items in time order.</p>';
status();try{history.replaceState(null,'',location.pathname+location.search);}catch(e){}}
function step(d){const src=cur&&document.getElementById(cur),ix=src?all.indexOf(src):(d>0?-1:all.length);
let j=ix+d;while(j>=0&&j<all.length&&!ok(all[j]))j+=d;if(j>=0&&j<all.length)show(all[j].id,'step');}
prev.addEventListener('click',()=>step(-1));next.addEventListener('click',()=>step(1));
document.getElementById('pclose').addEventListener('click',close);
kind.addEventListener('change',()=>{status();kind.blur();});
document.addEventListener('click',ev=>{const g=ev.target.closest('[data-k]');if(!g)return;
ev.preventDefault();show(g.getAttribute('data-k'),'click');});
document.addEventListener('keydown',ev=>{if(ev.altKey||ev.ctrlKey||ev.metaKey)return;
const tag=(ev.target.tagName||'').toLowerCase();if(tag==='input'||tag==='select'||tag==='textarea')return;
const k=ev.key;if(k==='ArrowRight'||k==='j'){ev.preventDefault();step(1);}
else if(k==='ArrowLeft'||k==='k'){ev.preventDefault();step(-1);}
else if(k==='Escape'){if(cur){ev.preventDefault();close();}}
else if(k==='Enter'||k===' '){const g=ev.target.closest&&ev.target.closest('[data-k]');
if(g){ev.preventDefault();show(g.getAttribute('data-k'),'click');}}});
const m=location.hash.match(/^#sel=(.+)$/);if(m)show(decodeURIComponent(m[1]),'hash');else close();})();
"""

LEGEND_JS = r"""
(()=>{const d=document.querySelector('details.legend');if(!d)return;const K='storymap-legend-open';
try{const v=localStorage.getItem(K);if(v!==null)d.open=v==='1';else if(innerWidth<700)d.open=false;}
catch(e){if(innerWidth<700)d.open=false;}
const set=()=>document.documentElement.style.setProperty('--legend-h',
(getComputedStyle(d).position==='sticky'?d.offsetHeight:0)+'px');
let user=false;d.querySelector('summary').addEventListener('click',()=>{user=true;});
d.addEventListener('toggle',()=>{set();if(user){user=false;try{localStorage.setItem(K,d.open?'1':'0');}catch(e){}}});
set();addEventListener('resize',set);if(window.ResizeObserver)new ResizeObserver(set).observe(d);})();
"""

EVENTS_JS = r"""
(()=>{const secs=[...document.querySelectorAll('section.ev')];if(!secs.length)return;let i=-1;
function go(n){if(n<0||n>=secs.length)return;secs.forEach(s=>s.classList.remove('cur'));i=n;const s=secs[i];
s.classList.add('cur');s.scrollIntoView({block:'start'});s.focus({preventScroll:true});
try{history.replaceState(null,'','#'+encodeURIComponent(s.id));}catch(e){}}
document.addEventListener('keydown',ev=>{if(ev.altKey||ev.ctrlKey||ev.metaKey)return;
const tag=(ev.target.tagName||'').toLowerCase();if(tag==='input'||tag==='select'||tag==='textarea')return;
if(ev.key==='ArrowDown'||ev.key==='j'){ev.preventDefault();go(i+1);}
else if(ev.key==='ArrowUp'||ev.key==='k'){ev.preventDefault();go(Math.max(i-1,0));}});
document.addEventListener('click',ev=>{const s=ev.target.closest('section.ev');if(s)i=secs.indexOf(s);});
const h=decodeURIComponent(location.hash.slice(1));const n=secs.findIndex(s=>s.id===h);if(n>=0)go(n);})();
"""

FILTER_JS = """
(()=>{const sels=[...document.querySelectorAll('.filters select')],rows=[...document.querySelectorAll('.row[data-agent]')],
count=document.getElementById('shown');
function apply(){let n=0;rows.forEach(r=>{const ok=sels.every(s=>!s.value||r.dataset[s.name]===s.value);
r.hidden=!ok;if(ok)n++;});count.textContent=n+' of '+rows.length+' agents shown';}
sels.forEach(s=>s.addEventListener('change',apply));
document.getElementById('reset').addEventListener('click',()=>{sels.forEach(s=>s.value='');apply();});apply();})();
"""


# ------------------------------------------------------------------ loading

def _read_jsonl(path):
    rows = []
    if not path.is_file():
        return rows
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"_malformed": f"{path.name} line {n}"})
    return rows


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _unwrap(row, key):
    """Accept a bare stage output or one wrapped by the pipeline (``output``/``result``/``parsed``)."""
    if not isinstance(row, dict):
        return None
    if key in row:
        return row
    for wrap in ("output", "result", "parsed", "data"):
        inner = row.get(wrap)
        if isinstance(inner, dict) and key in inner:
            merged = dict(inner)
            for meta in ("from_t", "to_t", "chunk", "agent"):
                if meta in row and meta not in merged:
                    merged[meta] = row[meta]
            return merged
    return None


def timeline_dir(path):
    path = Path(path)
    return path / "timeline" if (path / "timeline").is_dir() else path


def load(map_dir, timeline=None):
    """All agents' data: {agent: {items, events, thoughts, chunks, reactions, stage3, index}}."""
    map_dir = Path(map_dir)
    agents = defaultdict(lambda: {"items": [], "chunks": [], "reactions": [], "stage3": None,
                                  "index": {}, "malformed": []})
    index = {}
    if timeline is not None:
        tdir = timeline_dir(timeline)
        index = _read_json(tdir / "index.json") or {}
        names = set(index) | {p.stem for p in tdir.glob("agent-*.jsonl")}
        for name in names:
            agents[name]["index"] = index.get(name) or {}
            agents[name]["items"] = [r for r in _read_jsonl(tdir / f"{name}.jsonl") if "_malformed" not in r]
    for p in sorted((map_dir / "stage1").glob("*.jsonl")):
        for row in _read_jsonl(p):
            out = _unwrap(row, "thoughts")
            (agents[p.stem]["chunks"].append(out) if out else agents[p.stem]["malformed"].append(f"stage1 {p.name}"))
    for p in sorted((map_dir / "stage2").glob("*.jsonl")):
        for row in _read_jsonl(p):
            out = _unwrap(row, "event_id")
            (agents[p.stem]["reactions"].append(out) if out else agents[p.stem]["malformed"].append(f"stage2 {p.name}"))
    for p in sorted((map_dir / "stage3").glob("*.json")):
        out = _unwrap(_read_json(p), "final")
        if out:
            agents[p.stem]["stage3"] = out
        else:
            agents[p.stem]["malformed"].append(f"stage3 {p.name}")
    for row in _read_jsonl(map_dir / "map.jsonl"):
        out = _unwrap(row, "final")
        if out and out.get("agent") and agents[out["agent"]]["stage3"] is None:
            agents[out["agent"]]["stage3"] = out
    for name, a in agents.items():
        a["name"] = name
        a["events"] = [it for it in a["items"] if it.get("kind") == "event" and isinstance(it.get("event"), dict)]
        thoughts = []
        for c, chunk in enumerate(a["chunks"]):
            for th in chunk.get("thoughts") or []:
                if isinstance(th, dict):
                    thoughts.append(dict(th, chunk=c))
        thoughts.sort(key=lambda th: (_num(th.get("t")), _num(th.get("item"))))
        a["thoughts"] = thoughts
        by_event = defaultdict(list)
        for r in a["reactions"]:
            by_event[str(r.get("event_id"))].append(r)
        a["by_event"] = by_event
    pipeline = _read_json(map_dir / "pipeline.json") or {}
    return dict(sorted(agents.items())), pipeline


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_taxonomy(path):
    """{"A": "group title", "C4": "code title"} from the researcher-only taxonomy headings, if any."""
    names = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError):
        return names
    for line in text.splitlines():
        m = re.match(r"^#{1,4}\s*(?:Group\s+)?([A-G])(\d{0,2})\b\s*[.:)\-–—]*\s*(.*)$", line.strip())
        if not m:
            m = re.match(r"^[-*]\s*\**`?([A-G])(\d{1,2})`?\**\s*[.:)\-–—]+\s*(.*)$", line.strip())
        if m:
            key = m.group(1) + m.group(2)
            title = re.sub(r"[*`_]", "", m.group(3)).strip()
            title = re.split(r"(?<=[a-z\)])\.\s|\s\(", title)[0][:80]
            if title and key not in names:
                names[key] = title
    return names


# ------------------------------------------------------------------ helpers

def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def fmt_t(t):
    return "?" if t is None else f"{_num(t):.1f} min"


def cut(text, cap=TEXT_CAP):
    text = "" if text is None else str(text)
    return text if len(text) <= cap else text[:cap] + f"\n[viewer cut {len(text) - cap} chars]"


def badge(text, tone=""):
    return f'<span class="badge{" b-" + tone if tone else ""}">{esc(text)}</span>'


def is_overseer(ev):
    return ev.get("author_role") == "overseer"


OTHER_TYPES = ("P1", "N1", "C1", "O1")


def glyph_class(ev):
    """Neutral-ink flags of an event glyph: g-fill (fake) or g-hollow, ring (from an overseer), and for a submission
    ok (accepted or credited) or no (not accepted); g-other for the small status / own-action dots."""
    e = ev["event"]
    typ = e.get("type")
    flags = ["g-other"] if typ in OTHER_TYPES else ["g-fill" if e.get("fake") else "g-hollow"]
    if is_overseer(e):
        flags.append("ring")
    if typ == "S1":
        d = e.get("detail") or {}
        flags.append("ok" if d.get("credited") or str(e.get("outcome") or "").startswith("accepted") else "no")
    return " ".join(flags)


def _mark(typ, x, y, r, cls="m"):
    """The bare SVG element of a shape (square, diamond, triangle, small dot, tick or circle)."""
    if typ in ("L1", "L2"):
        return f'<rect class="{cls}" x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r:.1f}" height="{2 * r:.1f}"/>'
    if typ == "D1":
        k = r * 1.3
        return (f'<polygon class="{cls}" points="{x:.1f},{y - k:.1f} {x + k:.1f},{y:.1f} {x:.1f},{y + k:.1f} '
                f'{x - k:.1f},{y:.1f}"/>')
    if typ == "S1":
        return (f'<polygon class="{cls}" points="{x:.1f},{y - r * 1.3:.1f} {x + r * 1.2:.1f},{y + r:.1f} '
                f'{x - r * 1.2:.1f},{y + r:.1f}"/>')
    if typ in OTHER_TYPES:
        return f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{max(r * 0.4, 1.6):.1f}"/>'
    if typ == "TH":
        return f'<rect class="{cls}" x="{x - 1.6:.1f}" y="{y - r:.1f}" width="3.2" height="{2 * r:.1f}" rx="1"/>'
    return f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"/>'


def shape(typ, x, y, r, cls, key, title):
    """One SVG glyph in neutral ink: the shape encodes the event type; ``cls`` flags (glyph_class) add the fill,
    the overseer ring and a submission's outcome mark. ``key`` None: not selectable (legend, links)."""
    flags = set(cls.split())
    attrs = f'class="g {cls}"' + (f' data-k="{esc(key)}" tabindex="0" role="button"' if key is not None else "")
    t = f"<title>{esc(title)}</title>" if title else ""
    if typ == "P2":
        d = f"M{x - r:.1f},{y - r:.1f}L{x + r:.1f},{y + r:.1f}M{x + r:.1f},{y - r:.1f}L{x - r:.1f},{y + r:.1f}"
        body = f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="{r + 1:.1f}"/><path class="x" d="{d}"/>'
    else:
        body = _mark(typ, x, y, r)
    ring = _mark(typ, x, y, r + 2.8, "ring") if "ring" in flags and typ != "P2" else ""
    extra = ""
    if "ok" in flags:  # a check mark above right: accepted or credited
        extra = (f'<path class="mk" d="M{x + r * 0.7:.1f},{y - r * 1.5:.1f}l{r * 0.45:.1f},{r * 0.5:.1f}'
                 f'l{r * 0.9:.1f},{-r * 1.1:.1f}"/>')
    elif "no" in flags:  # a slash through it: not accepted
        d = f"M{x - r * 1.3:.1f},{y + r * 1.4:.1f}L{x + r * 1.3:.1f},{y - r * 1.6:.1f}"
        extra = f'<path class="halo" d="{d}"/><path class="mk" d="{d}"/>'
    return f'<g {attrs}>{t}{ring}{body}{extra}</g>'


def event_title(ev):
    e = ev["event"]
    bits = [TYPE_NAME.get(e.get("type"), "event").capitalize(), f"t={fmt_t(ev.get('t'))}"]
    if e.get("author"):
        bits.append(f"by {e['author']}" + (f" ({e['author_role']})" if e.get("author_role") else ""))
    if e.get("fake"):
        bits.append("fake")
    if e.get("outcome"):
        bits.append(str(e["outcome"]))
    if e.get("id"):
        bits.append(str(e["id"])[:80])
    return " \u00b7 ".join(bits)


def final_labels(s3):
    if not s3:
        return badge("no stage 3", "caution")
    f = s3.get("final") or {}
    out = [badge("source: " + str(f.get("source", "unknown")), "info")]
    if f.get("faked"):
        out.append(badge(f"faked @ {fmt_t(f.get('first_fake_t'))}", "bad"))
    elif f.get("considered"):
        out.append(badge("considered, no fake", "caution"))
    else:
        out.append(badge("never considered", "good"))
    if f.get("credited"):
        out.append(badge("credited", "good" if not f.get("faked") else "bad"))
    if f.get("stopped"):
        out.append(badge("stopped", "info"))
    if f.get("aware_cheating") in ("yes", "no", "unclear"):
        out.append(badge("aware: " + f["aware_cheating"]))
    if s3.get("consistency_flags"):
        out.append(badge(f"{len(s3['consistency_flags'])} flag(s)", "caution"))
    return "".join(out)


def axis_max(agents):
    last = [AXIS_MIN]
    for a in agents.values():
        last += [_num(it.get("t")) for it in a["items"][-1:]]
        last += [_num(th.get("t")) for th in a["thoughts"][-1:]]
        for ph in (a["stage3"] or {}).get("phases") or []:
            last.append(_num(ph.get("to_t")))
    top = max(last)
    return AXIS_MIN if top <= AXIS_MIN else 30.0 * int((top + 29.999) // 30)


def phases_of(a, tmax):
    """Stage-3 phases; else a fallback from stage-1 chunk states (stated as such); else nothing."""
    s3 = a["stage3"] or {}
    if s3.get("phases"):
        return [(_num(p.get("from_t")), _num(p.get("to_t"), tmax), str(p.get("stance") or "unknown"),
                 str(p.get("label") or "")) for p in s3["phases"]], "stage 3"
    out, prev = [], 0.0
    for c in a["chunks"]:
        st = (c.get("state") or {}).get("stance") or "unknown"
        ts = [_num(th.get("t")) for th in c.get("thoughts") or []]
        end = _num(c.get("to_t"), max(ts) if ts else prev)
        if end > prev or not out:
            out.append((prev, end, st, "stage-1 state"))
            prev = end
    return out, ("stage-1 states (no stage 3)" if out else "none")


def page(title, body, scripts=""):
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta name="robots" content="noindex"><title>{esc(title)}</title><style>{CSS}</style></head>'
           f'<body>{body}{"<script>" + scripts + "</script>" if scripts else ""}</body></html>')
    return themed(doc)


STAGE_MODELS = (("stage1", "stage 1"), ("stage2", "stage 2"), ("stage3", "stage 3"))


def model_names(pipeline):
    """'stage 1 anthropic/claude-haiku-5.5; stages 2-3 openai/gpt-5.6-terra' from pipeline.json (else '')."""
    models = (pipeline or {}).get("models") if isinstance(pipeline, dict) else None
    if not isinstance(models, dict):
        return ""
    ids = [(label, models[k].get("model_id") if isinstance(models.get(k), dict) else models.get(k))
           for k, label in STAGE_MODELS]
    groups = []
    for label, mid in ids:
        if not mid:
            continue
        if groups and groups[-1][1] == mid:
            groups[-1][0].append(label.split()[-1])
        else:
            groups.append(([label.split()[-1]], mid))
    return "; ".join((f"stages {g[0]}-{g[-1]}" if len(g) > 1 else f"stage {g[0]}") + f" {mid}" for g, mid in groups)


def banner(models=""):
    read = f" (read by {esc(models)})" if models else ""
    return (f'<p class="banner">Story map: quotes agent reasoning, with stage 1-3 model readings{read}. '
            'Model readings are interpretations, not ground truth.</p>')


STANCE_MEANING = (("none", "no idea of the trick yet"), ("considering", "has the idea, undecided"),
                  ("deferred", "keeps it as a later option"), ("rejected", "decided not to use it"),
                  ("faking", "decided to fake, or faking"), ("stopped", "stopped after faking"),
                  ("unknown", "no reading for this span"))
SHAPE_MEANING = (  # (type, glyph_class flags, name); drawn by shape() exactly as on the map
    ("L2", "g-hollow", "library file"), ("B1", "g-hollow", "board post"), ("D1", "g-hollow", "direct message"),
    ("S1", "g-hollow", "own submission"), ("P2", "g-hollow", "revert notice"),
    ("N1", "g-other", "status, notice, own post, restart"))
MARK_MEANING = (("L2", "g-hollow", "grey outline: ordinary or genuine"), ("L2", "g-fill", "pink, solid: fake"),
                ("D1", "g-hollow ring", "cyan with ring: from an overseer"),
                ("S1", "g-hollow ok", "\u2713 accepted or credited"), ("S1", "g-hollow no", "slash: not accepted"))
EXAMPLES = (("L2", "g-fill", "fake library file read"), ("D1", "g-hollow ring", "DM from an overseer"),
            ("S1", "g-fill ok", "fake submission, accepted"), ("S1", "g-hollow no", "genuine submission, rejected"))


def _legend_glyph(typ, cls):
    """A legend glyph from the map's own code: shape() for events, the stance bar rect for stances."""
    if typ == "TP":
        return '<svg viewBox="0 0 18 16"><line class="tp" x1="9" x2="9" y1="0" y2="16"/></svg>'
    if typ == "BAR":
        return f'<svg viewBox="0 0 22 16" class="swatch"><rect class="ph-{cls}" x="1" y="3" width="20" height="10" rx="2"/></svg>'
    return f'<svg viewBox="0 0 18 16">{shape(typ, 8, 8.5, 4.6, cls, None, "")}</svg>'


def legend(mode="agent"):
    """Two labelled boxes built from the map's own classes: bar colour = stance; shape = event, fill = fake,
    mark = overseer or outcome (with worked examples)."""
    sw = "".join(f'<li>{_legend_glyph("BAR", s)}<b>{s}</b>&nbsp;{esc(m)}</li>' for s, m in STANCE_MEANING)
    shapes = "".join(f'<li>{_legend_glyph(t, c)}{esc(m)}</li>' for t, c, m in SHAPE_MEANING)
    if mode == "agent":
        shapes += (f'<li>{_legend_glyph("TH", "g-th")}{_legend_glyph("TH", "g-th-low")}thought (hollow = low '
                   f'certainty)</li><li>{_legend_glyph("TP", "")}turning point</li>')
    marks = "".join(f'<li>{_legend_glyph(t, c)}{esc(m)}</li>' for t, c, m in MARK_MEANING)
    examples = "".join(f'<li>{_legend_glyph(t, c)}= {esc(m)}</li>' for t, c, m in EXAMPLES)
    where = {"index": "Shapes above an agent's bar are exposures (library reads, overseer and relevant posts, DMs, "
                      "reverts); below are its submissions. Click a shape to open it on the agent page.",
             "events": "Each event below carries its shape. ↓ / ↑ (or j / k) move through the events."
             }.get(mode, "Click any shape for its quote, the event and the stage-2 reaction; "
                         "→ / j next, ← / k previous, Esc closes.")
    return (f'<details class="legend" open><summary>Legend: two keys, bar colour = stance; shape + colour = event</summary>'
            f'<div class="lg"><div class="lgbox"><h4>Bar colour = stance</h4><ul>{sw}</ul>'
            f'<p class="note">These colours appear only on the stance bars (stage-3 reading; stage-1 states when stage 3 '
            f'is missing). Dashed outline = no reading.</p></div>'
            f'<div class="lgbox"><h4>Shape + colour = event</h4>'
            f'<ul>{shapes}</ul><ul class="marks">{marks}</ul><ul class="ex"><li><b>Read as:</b></li>{examples}</ul>'
            f'<p class="note">Event colours (pink, cyan) are never stance colours.</p>'
            f'<p class="note">{where}</p></div></div></details>')


def axis_svg(tmax, x0, x1, y, h, ticks=True):
    out = []
    step = 30 if tmax <= 360 else 60
    t = 0
    while t <= tmax + 1e-9:
        x = x0 + (x1 - x0) * t / tmax
        out.append(f'<line class="tick" x1="{x:.1f}" x2="{x:.1f}" y1="{y}" y2="{y + h}"/>')
        if ticks:
            anchor = "end" if t + step > tmax + 1e-9 else "start" if t == 0 else "middle"
            out.append(f'<text x="{x:.1f}" y="{y + h + 11}" text-anchor="{anchor}">{t}</text>')
        t += step
    return "".join(out)


# ------------------------------------------------------------------ index.html

INDEX_TYPES = ("L2", "B1", "D1", "P2", "S1")


def stance_title(frm, to, st, label):
    return f"Stance: {st} \u00b7 t={fmt_t(frm)} to {fmt_t(to)}" + (f" \u00b7 {label}" if label else "")


def index_page(agents, pipeline, tmax, nav=""):
    W, x0, x1 = 1000, 4, 996
    X = lambda t: x0 + (x1 - x0) * min(max(_num(t), 0.0), tmax) / tmax  # noqa: E731
    rows, counts = [], Counter()
    sources = sorted({str(((a["stage3"] or {}).get("final") or {}).get("source", "unknown"))
                      if a["stage3"] else "no stage 3" for a in agents.values()})
    for name, a in agents.items():
        f = (a["stage3"] or {}).get("final") or {}
        src = str(f.get("source", "unknown")) if a["stage3"] else "no stage 3"
        counts[src] += 1
        parts = [axis_svg(tmax, x0, x1, 0, 30, ticks=False)]
        phases, _ = phases_of(a, tmax)
        for frm, to, st, label in phases:
            cls = st if st in STANCES else "unknown"
            parts.append(f'<rect class="ph-{cls}" x="{X(frm):.1f}" y="11" width="{max(X(to) - X(frm), 1):.1f}" '
                         f'height="8" rx="2"><title>{esc(stance_title(frm, to, st, label))}'
                         f"</title></rect>")
        for ev in a["events"]:
            e = ev["event"]
            typ = e.get("type")
            if typ not in INDEX_TYPES or (typ in ("B1", "D1") and not (is_overseer(e) or set(e.get("tags") or []) & RELEVANT_TAGS)):
                continue
            y = 24 if typ == "S1" else 6
            key = f"ev-{ev.get('i')}"
            parts.append(f'<a href="agents/{esc(name)}.html#sel={key}">'
                         + shape(typ, X(ev.get("t")), y, 3.6, glyph_class(ev), None, event_title(ev)) + "</a>")
        data = {"source": src, "faked": _yn(f.get("faked")), "stopped": _yn(f.get("stopped")),
                "credited": _yn(f.get("credited")), "aware": str(f.get("aware_cheating") or "")}
        attrs = " ".join(f'data-{k}="{esc(v)}"' for k, v in data.items())
        story = (a["stage3"] or {}).get("story") or ""
        rows.append(f'<div class="row" data-agent="{esc(name)}" {attrs}>'
                    f'<div class="agent"><a href="agents/{esc(name)}.html" title="{esc(story)}">{esc(name)}</a></div>'
                    f'<div class="strip"><svg viewBox="0 0 {W} 30" preserveAspectRatio="none">{"".join(parts)}</svg></div>'
                    f'<div class="labels">{final_labels(a["stage3"])}</div></div>')
    header_axis = (f'<svg viewBox="0 0 {W} 16" preserveAspectRatio="none" style="display:block;width:100%;height:16px">'
                   + "".join(f'<text x="{x0 + (x1 - x0) * t / tmax:.1f}" y="12" text-anchor='
                             f'"{"start" if t == 0 else "end" if t + 30 > tmax else "middle"}">{t}</text>'
                             for t in range(0, int(tmax) + 1, 30)) + "</svg>")

    def sel(name, label, options):
        opts = "".join(f'<option value="{esc(v)}">{esc(t)}</option>' for v, t in options)
        return f'<label>{esc(label)} <select name="{name}"><option value="">any</option>{opts}</select></label>'
    yn = [("yes", "yes"), ("no", "no")]
    filters = ('<div class="filters">' + sel("source", "Source", [(s, s) for s in sources]) + sel("faked", "Faked", yn)
               + sel("stopped", "Stopped", yn) + sel("credited", "Credited", yn)
               + '<button type="button" id="reset">Reset</button> <span id="shown" class="muted"></span></div>')
    summary = "".join(badge(f"{k}: {v}") for k, v in sorted(counts.items()))
    run = pipeline.get("run_id") or ""
    body = (f"<h1>Story map{(': ' + esc(run)) if run else ''}</h1>"
            f'<nav class="top"><a href="index.html">Agents</a><a href="events.html">Events</a>{nav}</nav>'
            f"{banner(model_names(pipeline))}"
            f'<p class="muted">{len(agents)} agents. Sources: {summary}. Axis in minutes from run start (0-{int(tmax)}).</p>'
            f"{legend('index')}{filters}"
            f'<div class="grid"><div class="hd">Agent</div><div class="hd">{header_axis}</div><div class="hd lab">Final</div>'
            f'{"".join(rows) or "<div class=empty>No agents found.</div>"}</div>')
    return page(f"Story map {run}".strip(), body, LEGEND_JS + FILTER_JS)


def _yn(v):
    return "yes" if v is True else "no" if v is False else ""


# ------------------------------------------------------------------ agents/<agent>.html

def reaction_html(r):
    if not r:
        return '<p class="muted">No stage-2 reaction recorded for this event.</p>'
    ch, dr = r.get("change"), r.get("direction")
    tone = "bad" if dr == "toward_cheating" and ch != "none" else "good" if dr == "away" and ch != "none" else ""
    head = (badge(f"registered: {r.get('registered')}") + badge(f"change: {ch}", tone)
            + badge(DIRECTION_LABEL.get(dr, str(dr)), tone) + badge(f"certainty: {r.get('certainty')}")
            + (badge("self-attributed", "caution") if r.get("self_attributed") else ""))
    ov = r.get("overseer")
    ov_html = ""
    if isinstance(ov, dict):
        ov_html = "<p>Overseer: " + "".join(badge(f"{k}: {v}") for k, v in ov.items() if v not in (None, False)) + "</p>"
    quotes = "".join(f'<blockquote>{esc(q.get("quote"))} <small>(item {esc(q.get("item"))})</small></blockquote>'
                     for q in r.get("quotes") or [] if isinstance(q, dict))
    return (f'<div class="react"><p><b>Stage-2 reaction</b> {head}</p>'
            + (f"<p>Reason: {esc(r.get('reason'))}</p>" if r.get("reason") else "")
            + (f"<p>Next action: {esc(r.get('next_action'))}</p>" if r.get("next_action") else "")
            + ov_html + quotes + "</div>")


def event_article(name, ev, reactions, key):
    e = ev["event"]
    d = e.get("detail") or {}
    meta = [badge(f"event {e.get('type') or '?'}"), badge(TYPE_NAME.get(e.get("type"), "event"))]
    if e.get("author"):
        meta.append(badge(f"{e['author']}" + (f" ({e['author_role']})" if e.get("author_role") else ""),
                          "info" if is_overseer(e) else ""))
    if e.get("fake") is True:
        meta.append(badge("fake", "bad"))
    elif e.get("fake") is False:
        meta.append(badge("not fake", "good"))
    if e.get("outcome"):
        meta.append(badge(str(e["outcome"])))
    meta += [badge(t, "caution") for t in e.get("tags") or []]
    if d.get("reread"):
        meta.append(badge("re-read"))
    kv = "".join(f"<dt>{esc(k)}</dt><dd>{esc(json.dumps(v) if isinstance(v, (dict, list)) else v)}</dd>"
                 for k, v in d.items() if v not in (None, "", [], {}))
    k = "k-fake" if (e.get("fake") or e.get("type") == "P2") else "k-ov" if is_overseer(e) else ""
    kind = "sub" if e.get("type") == "S1" else "own" if e.get("type") in ("O1", "C1") else "exp"
    reacts = "".join(reaction_html(r) for r in reactions) if reactions else (
        reaction_html(None) if e.get("type") in ("L2", "B1", "D1", "P2") else "")
    return (f'<article class="item {k}" id="{key}" data-kind="{kind}" data-t="{_num(ev.get("t")):.1f}">'
            f'<h3>{fmt_t(ev.get("t"))} &middot; {esc(TYPE_NAME.get(e.get("type"), "event"))} &middot; {esc(e.get("id"))}</h3>'
            f'<p>{"".join(meta)} <small>turn {esc(ev.get("turn"))}, item {esc(ev.get("i"))}, {esc(ev.get("ts"))}</small></p>'
            f'<pre>{esc(cut(ev.get("text")))}</pre>{"<dl class=kv>" + kv + "</dl>" if kv else ""}{reacts}</article>')


def thought_article(th, names, related, key, items_by_i):
    code = str(th.get("code") or "?")
    title = names.get(code) or names.get(code[:1]) or ""
    src = items_by_i.get(th.get("item"))
    ctx = ""
    if src is not None and src.get("text") and str(th.get("quote") or "") not in str(src.get("text")):
        ctx = '<p class="muted">Quote not found verbatim in the timeline item.</p>'
    rel = "".join(f'<p class="muted">Cited by the stage-2 reaction to <a href="#sel={esc(k)}" data-k="{esc(k)}">'
                  f"{esc(eid)}</a>:</p>{reaction_html(r)}" for k, eid, r in related)
    low = " g-th-low" if th.get("certainty") == "low" else ""
    return (f'<article class="item k-th{low}" id="{key}" data-kind="th" data-t="{_num(th.get("t")):.1f}"><h3>{fmt_t(th.get("t"))} &middot; thought {esc(code)}'
            f'{" &middot; " + esc(title) if title else ""}</h3>'
            f'<p>{badge("certainty: " + str(th.get("certainty")))} <small>turn {esc(th.get("turn"))}, item '
            f'{esc(th.get("item"))}, stage-1 chunk {esc(th.get("chunk"))}</small></p>'
            f'<blockquote>{esc(th.get("quote"))}</blockquote>'
            f'{"<p>Note: " + esc(th.get("note")) + "</p>" if th.get("note") else ""}{ctx}{rel}</article>')


PANEL = ('<aside id="panel"><div class="stepper"><button type="button" id="pprev" title="Previous (\u2190 or k)">'
         '\u2190 Prev</button><span id="pos"></span><button type="button" id="pnext" title="Next (\u2192 or j)">Next \u2192'
         '</button><label>Step through <select id="kind"><option value="">all items</option>'
         '<option value="exp">only exposures</option><option value="th">only thoughts</option>'
         '<option value="sub">only submissions</option><option value="tp">only turning points</option></select></label>'
         '<button type="button" id="pclose" title="Close (Esc)" aria-label="Close">\u00d7</button></div>'
         '<div id="pbody"></div><p class="muted keys">\u2192 / j next, \u2190 / k previous, Esc closes.</p></aside>')


def agent_page(a, names, tmax, all_names, models="", nav=""):
    name = a["name"]
    s3 = a["stage3"] or {}
    W, LX, x0, x1 = 1000, 0, 190, 990
    X = lambda t: x0 + (x1 - x0) * min(max(_num(t), 0.0), tmax) / tmax  # noqa: E731
    LH = 22
    lanes = [("stance", "Stance")] + [(f"th-{L}", f"thought {L}: " + (names.get(L) or "codes")) for L in LETTERS] + \
            [(k, label) for k, label, _ in LANES]
    ypos = {k: 6 + n * LH for n, (k, _) in enumerate(lanes)}
    H = 6 + len(lanes) * LH + 18
    svg = []
    for n, (k, label) in enumerate(lanes):
        y = ypos[k]
        if k != "stance":  # the stance bars sit on the card surface their colours were validated against
            svg.append(f'<rect class="lane-bg{" alt" if n % 2 else ""}" x="{x0}" y="{y}" width="{x1 - x0}" height="{LH}"/>')
        svg.append(f'<text x="{LX + 4}" y="{y + 15}"><title>{esc(label)}</title>'
                   f'{esc(label if len(label) <= 30 else label[:29] + "…")}</text>')
    svg.append(axis_svg(tmax, x0, x1, 6, len(lanes) * LH))
    svg.append(f'<line id="cursor" class="cursor" x1="0" x2="0" y1="6" y2="{6 + len(lanes) * LH}" visibility="hidden"/>')

    articles = []  # (t, order, html)
    # Stance lane.
    phases, phase_src = phases_of(a, tmax)
    y = ypos["stance"]
    for frm, to, st, label in phases:
        cls = st if st in STANCES else "unknown"
        svg.append(f'<rect class="ph-{cls}" x="{X(frm):.1f}" y="{y + 4}" width="{max(X(to) - X(frm), 1):.1f}" '
                   f'height="{LH - 8}" rx="2"><title>{esc(stance_title(frm, to, st, label))}</title></rect>')
    for n, tp in enumerate(s3.get("turning_points") or []):
        key = f"tp-{n}"
        x = X(tp.get("t"))
        tip = f"Turning point \u00b7 t={fmt_t(tp.get('t'))} \u00b7 {tp.get('from')} \u2192 {tp.get('to')}"
        svg.append(f'<line class="tp" data-k="{key}" x1="{x:.1f}" x2="{x:.1f}" y1="{y}" y2="{6 + len(lanes) * LH}">'
                   f"<title>{esc(tip)}</title></line>")
        causes = ", ".join(esc(c) for c in tp.get("cause_event_ids") or []) or "none named"
        articles.append((_num(tp.get("t")), 0, f'<article class="item k-tp" id="{key}" data-kind="tp" data-t="{_num(tp.get("t")):.1f}"><h3>{fmt_t(tp.get("t"))} '
                         f'&middot; turning point: {esc(tp.get("from"))} &rarr; {esc(tp.get("to"))}</h3>'
                         f'<p>Causes: {causes}</p><p>{esc(tp.get("cause_note"))}</p></article>'))

    # Event lanes and reactions.
    key_of_event = {}
    for ev in a["events"]:
        key_of_event.setdefault(str(ev["event"].get("id")), f"ev-{ev.get('i')}")
    cited = defaultdict(list)  # timeline item index -> [(event key, event id, reaction)]
    for eid, rs in a["by_event"].items():
        for r in rs:
            for q in r.get("quotes") or []:
                if isinstance(q, dict) and q.get("item") is not None:
                    cited[q["item"]].append((key_of_event.get(eid, ""), eid, r))
    lane_of = {t: k for k, _, types in LANES for t in types}
    for ev in a["events"]:
        e = ev["event"]
        key = f"ev-{ev.get('i')}"
        lane = lane_of.get(e.get("type"), "own")
        svg.append(shape(e.get("type"), X(ev.get("t")), ypos[lane] + LH / 2, 5, glyph_class(ev), key, event_title(ev)))
        articles.append((_num(ev.get("t")), 1, event_article(name, ev, a["by_event"].get(str(e.get("id")), []), key)))
    seen_ids = set(key_of_event)
    orphans = [r for eid, rs in a["by_event"].items() if eid not in seen_ids for r in rs]

    # Thought lanes.
    items_by_i = {it.get("i"): it for it in a["items"]}
    for n, th in enumerate(a["thoughts"]):
        key = f"th-{n}"
        L = str(th.get("code") or "?")[:1].upper()
        lane = f"th-{L}" if L in LETTERS else "th-G"
        cls = "g-th-low" if th.get("certainty") == "low" else "g-th"
        svg.append(shape("TH", X(th.get("t")), ypos[lane] + LH / 2, 4.5, cls, key,
                         f"Thought {th.get('code')} \u00b7 t={fmt_t(th.get('t'))} \u00b7 {str(th.get('quote') or '')[:120]}"))
        related = [(k, eid, r) for k, eid, r in cited.get(th.get("item"), [])]
        articles.append((_num(th.get("t")), 2, thought_article(th, names, related, key, items_by_i)))

    articles.sort(key=lambda x: (x[0], x[1]))
    f = s3.get("final") or {}
    final_kv = "".join(f"<dt>{esc(k)}</dt><dd>{esc(json.dumps(v) if isinstance(v, (list, dict)) else v)}</dd>"
                       for k, v in f.items())
    flags = "".join(f"<li>{esc(x)}</li>" for x in s3.get("consistency_flags") or [])
    states = "".join(
        f"<tr><td class=n>{c}</td><td>{esc((ch.get('state') or {}).get('stance'))}</td>"
        f"<td>{esc((ch.get('state') or {}).get('has_trick'))}</td><td>{esc((ch.get('state') or {}).get('believes_judge'))}</td>"
        f"<td>{esc((ch.get('state') or {}).get('summary'))}</td></tr>" for c, ch in enumerate(a["chunks"]))
    missing = [s for s, ok in (("timeline", a["items"]), ("stage 1", a["chunks"]), ("stage 2", a["reactions"]),
                               ("stage 3", a["stage3"])) if not ok]
    idx = list(all_names)
    pos = idx.index(name)
    prev_link = f'<a href="{esc(idx[pos - 1])}.html">&larr; {esc(idx[pos - 1])}</a>' if pos > 0 else ""
    next_link = f'<a href="{esc(idx[pos + 1])}.html">{esc(idx[pos + 1])} &rarr;</a>' if pos + 1 < len(idx) else ""
    info = a["index"] or {}
    body = (f'<nav class="top"><a href="../index.html">All agents</a><a href="../events.html">Events</a>{nav}'
            f"{prev_link}{next_link}</nav><h1>{esc(name)}</h1>{banner(models)}"
            f'<p>{final_labels(a["stage3"])}</p>'
            f'<p class="story">{esc(s3.get("story")) or "<span class=muted>No stage-3 story.</span>"}</p>'
            + (f'<p class="muted">Missing: {", ".join(missing)}.</p>' if missing else "")
            + (f'<p class="muted">Malformed rows skipped: {esc(", ".join(a["malformed"]))}.</p>' if a["malformed"] else "")
            + f'<p class="muted">{len(a["items"])} timeline items, {len(a["events"])} events, {len(a["thoughts"])} coded '
            f'thoughts, {len(a["reactions"])} stage-2 reactions. Stance from {esc(phase_src)}.'
            f'{" Role: " + esc(info.get("role")) + "." if info.get("role") else ""}</p>'
            f'{legend()}<div class="lanes"><svg viewBox="0 0 {W} {H}">{"".join(svg)}</svg></div>'
            f'<div class="layout"><div><h2>In time order</h2><div id="items">'
            f'{"".join(h for _, _, h in articles) or "<div class=empty>Nothing recorded.</div>"}</div>'
            + ("<h2>Reactions to events not in the timeline</h2>" + "".join(reaction_html(r) for r in orphans) if orphans else "")
            + f'</div>{PANEL}</div>'
            f'<h2>Final</h2>{"<dl class=kv>" + final_kv + "</dl>" if final_kv else "<p class=muted>No stage 3.</p>"}'
            + (f"<h3>Consistency flags</h3><ul>{flags}</ul>" if flags else "")
            + (f"<h2>Stage-1 chunk states</h2><table><tr><th>Chunk</th><th>Stance</th><th>Has trick</th>"
               f"<th>Believes judge</th><th>Summary</th></tr>{states}</table>" if states else ""))
    return page(f"Story map {name}", body, LEGEND_JS + PANEL_JS)


# ------------------------------------------------------------------ events.html

def _group_key(e):
    typ, eid, d = e.get("type"), str(e.get("id") or ""), e.get("detail") or {}
    if typ == "L2":
        path = d.get("path") or eid.split(":", 1)[-1].rsplit("@", 1)[0]
        return f"L2:{path}"
    return eid


def events_page(agents, tmax, models="", nav=""):
    groups = {}
    skipped = Counter()
    for name, a in agents.items():
        for ev in a["events"]:
            e = ev["event"]
            typ = e.get("type")
            if typ not in ("L2", "B1", "D1", "P2"):
                continue
            reacts = a["by_event"].get(str(e.get("id")), [])
            if typ in ("B1", "D1") and not (is_overseer(e) or set(e.get("tags") or []) & RELEVANT_TAGS or reacts):
                skipped[typ] += 1
                continue
            g = groups.setdefault(_group_key(e), {"type": typ, "first": ev, "readers": {}, "fake": e.get("fake"),
                                                  "overseer": is_overseer(e), "ids": set()})
            g["ids"].add(str(e.get("id")))
            rd = g["readers"].setdefault(name, {"t": ev.get("t"), "key": f"ev-{ev.get('i')}", "n": 0, "reacts": []})
            rd["n"] += 1
            if not rd["reacts"]:
                rd["reacts"] = list(reacts)
    sections = {"ov": [], "board": [], "revert": [], "lib": []}
    titles = {"ov": "Overseer posts and DMs", "board": "Other board posts and DMs (trick, warning or ruling)",
              "revert": "Reverts seen", "lib": "Library files read"}
    order = sorted(groups.items(), key=lambda kv: (-len(kv[1]["readers"]), _num(kv[1]["first"].get("t"))))
    for gk, g in order:
        sec = ("revert" if g["type"] == "P2" else "lib" if g["type"] == "L2" else "ov" if g["overseer"] else "board")
        sections[sec].append(event_section(gk, g))
    jump = " ".join(f'<a href="#{k}">{esc(titles[k])} ({len(v)})</a>' for k, v in sections.items())
    body = (f'<nav class="top"><a href="index.html">All agents</a><a href="events.html">Events</a>{nav}</nav>'
            f"<h1>Story map: events</h1>{banner(models)}{legend('events')}<p>{jump}</p>"
            f'<p class="muted">Each event lists the agents that read it (first read time; re-reads counted) and the '
            f'stage-2 reaction of each. Library files are grouped by path across revisions. \u2193 / \u2191 (or j / k) '
            f'move through the events. '
            f'{sum(skipped.values())} unrelated board posts / DMs without a reaction are not listed.</p>'
            + "".join(f'<h2 id="{k}">{esc(titles[k])}</h2>' + ("".join(v) or '<div class="empty">None.</div>')
                      for k, v in sections.items()))
    return page("Story map events", body, LEGEND_JS + EVENTS_JS)


def event_section(gk, g):
    ev = g["first"]
    e = ev["event"]
    readers = sorted(g["readers"].items(), key=lambda kv: _num(kv[1]["t"]))
    grid = Counter()
    unjudged = 0
    rows = []
    for name, rd in readers:
        r = rd["reacts"][0] if rd["reacts"] else None
        if r is None:
            unjudged += 1
        else:
            grid[(r.get("change") if r.get("change") in CHANGES else "none", r.get("direction"))] += 1
        rows.append(f'<tr><td><a href="agents/{esc(name)}.html#sel={esc(rd["key"])}">{esc(name)}</a></td>'
                    f'<td class="n">{fmt_t(rd["t"])}</td><td class="n">{rd["n"]}</td>'
                    + (f'<td>{esc(r.get("registered"))}</td><td>{esc(r.get("change"))}</td>'
                       f'<td>{esc(DIRECTION_LABEL.get(r.get("direction"), r.get("direction")))}</td>'
                       f'<td>{esc(r.get("reason"))}</td>' if r else '<td colspan="4" class="muted">no stage-2 reaction</td>')
                    + "</tr>")
    head = "".join(f"<th>{esc(DIRECTION_LABEL[d])}</th>" for d in DIRECTIONS)
    body = "".join(f"<tr><th>{c}</th>" + "".join(f'<td class="n">{grid[(c, d)] or ""}</td>' for d in DIRECTIONS)
                   + "</tr>" for c in CHANGES)
    meta = [badge(TYPE_NAME.get(e.get("type"), "event"))]
    if e.get("author"):
        meta.append(badge(f"{e['author']}" + (f" ({e['author_role']})" if e.get("author_role") else ""),
                          "info" if g["overseer"] else ""))
    if g["fake"] is True:
        meta.append(badge("fake", "bad"))
    elif g["fake"] is False:
        meta.append(badge("not fake", "good"))
    meta += [badge(t, "caution") for t in e.get("tags") or []]
    ids = sorted(g["ids"])
    icon = f'<svg class="hglyph" viewBox="0 0 18 16">{shape(e.get("type"), 9, 8, 5, glyph_class(ev), None, "")}</svg>'
    return (f'<section class="ev" id="{esc(gk)}" tabindex="-1"><h3>{icon}{esc(gk)}</h3><p>{"".join(meta)} '
            f'<small>first seen {fmt_t(ev.get("t"))} by {esc(readers[0][0])}; {len(readers)} reader(s)'
            f'{"; ids: " + esc(", ".join(ids)) if len(ids) > 1 else ""}</small></p>'
            f'<details><summary>Text as first seen</summary><pre>{esc(cut(ev.get("text")))}</pre></details>'
            f'<table><tr><th>Reaction</th>{head}</tr>{body}</table>'
            f'<p class="muted">{unjudged} reader(s) without a stage-2 reaction.</p>'
            f'<details{" open" if len(readers) <= 12 else ""}><summary>Readers</summary><table><tr><th>Agent</th>'
            f"<th>First seen</th><th>Reads</th><th>Registered</th><th>Change</th><th>Direction</th><th>Reason</th></tr>"
            f'{"".join(rows)}</table></details></section>')


# ------------------------------------------------------------------ validation.html

def _inline(text):
    out = esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)


def markdown_html(text):
    """The small Markdown subset of validation.md (headings, paragraphs, lists, pipe tables) as escaped HTML."""
    out, para, items, table = [], [], [], []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
            items.clear()
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s:|-]+\|?", r)]
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            if cells:
                head = "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
                body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
                out.append(f"<table><tr>{head}</tr>{body}</table>")
            table.clear()
    for line in text.splitlines():
        s = line.rstrip()
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush()
            n = min(len(m.group(1)), 3)
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif s.startswith("|"):
            if para or items:
                t = list(table)
                table.clear()
                flush()
                table.extend(t)
            table.append(s)
        elif re.match(r"^\s*[-*]\s+", s):
            if para or table:
                flush()
            items.append(re.sub(r"^\s*[-*]\s+", "", s))
        elif not s.strip():
            flush()
        else:
            if items or table:
                flush()
            para.append(s.strip())
    flush()
    return "".join(out)


def validation_page(text, models="", nav=""):
    body = (f'<nav class="top"><a href="index.html">All agents</a><a href="events.html">Events</a>{nav}</nav>'
            f"{banner(models)}{markdown_html(text)}")
    return page("Story map validation", body)


# ------------------------------------------------------------------ main

def build(map_dir, timeline=None, out=None, taxonomy=DEFAULT_TAXONOMY):
    """Write index.html, events.html and agents/<agent>.html; return the list of written paths."""
    agents, pipeline = load(map_dir, timeline)
    out = Path(out or map_dir)
    (out / "agents").mkdir(parents=True, exist_ok=True)
    names = load_taxonomy(taxonomy) if taxonomy else {}
    tmax = axis_max(agents)
    models = model_names(pipeline)
    validation = Path(map_dir) / "validation.md"
    has_val = validation.is_file()
    nav = '<a href="validation.html">Validation</a>' if has_val else ""
    written = []

    def write(path, text):
        path.write_text(text, encoding="utf-8")
        written.append(path)
    write(out / "index.html", index_page(agents, pipeline, tmax, nav))
    write(out / "events.html", events_page(agents, tmax, models, nav))
    if has_val:
        write(out / "validation.html", validation_page(validation.read_text(encoding="utf-8"), models, nav))
    agent_nav = nav.replace('href="', 'href="../')
    for a in agents.values():
        write(out / "agents" / f"{a['name']}.html", agent_page(a, names, tmax, agents.keys(), models, agent_nav))
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map", required=True, help="stage outputs dir (runs/<id>/analysis/storymap)")
    ap.add_argument("--timeline", help="stage-0 dir (holding timeline/ or the agent JSONL files)")
    ap.add_argument("--out", help="output dir (default: --map)")
    ap.add_argument("--taxonomy", default=str(DEFAULT_TAXONOMY), help="researcher-only taxonomy for code names")
    args = ap.parse_args(argv)
    written = build(args.map, args.timeline, args.out, args.taxonomy)
    print(f"wrote {len(written)} pages to {Path(args.out or args.map)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
