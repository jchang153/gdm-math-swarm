#!/usr/bin/env python3
"""Export a saved run's full reconstructed agent histories as private, escaped HTML; no model calls.

Each agent page renders the transcript judge's own reconstruction
(research/analysis/judge/history.py): every compaction segment, returned reasoning,
visible text, tool calls with their deduplicated results, compaction summaries (derived
self-report), the agent's own conference records, private file versions and the
harness inputs it was shown, in chronological order. Every judge item's citation ID
(for example ``A07.t0123.r`` or ``post-000012``) is its HTML anchor. Turn outputs and tool
results keep ``<article id="message-N">`` anchors with their Inspect message IDs, which
scripts/export_conference.py binds to.

The original .eval files, conference database and runtime records remain authoritative.
Viewer: a sticky bar on every agent page switches agents, finds text in the page (opening
collapsed blocks that match) and links to the index's search across all agents (it fetches the
sibling agent pages in the browser; no extra files). Long tool calls, results and records start
collapsed with a preview. Submissions are highlighted with their conference outcome and, when
RUN/evaluations/ exists, the independent proof checker's validity. When the behavior judge has run
(paid outputs of this run under RUN/analysis/judge/), its verified findings are shown beside the
items they cite, labelled as model-generated. Light and dark mode, phone width.

This is a derived transcript view, not a judge or behavioral evaluator. Researcher-only:
never provide it to experimental agents. Run only after experiment logging finishes.
"""
import argparse
import collections
import html
import json
from pathlib import Path
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_theme import BASE, badge_class  # noqa: E402

STYLE = ('<style>' + BASE +
         'body{max-width:1150px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--code);padding:10px 12px;'
         'margin:6px 0;border-radius:6px;font-size:13.5px;line-height:1.45}'
         'article{border-top:1px solid var(--line);margin:18px 0;padding-top:4px}details{margin:6px 0}'
         'summary{cursor:pointer;color:var(--muted)}summary .pv{font-family:ui-monospace,monospace;font-size:12.5px;'
         'color:var(--text);opacity:.8}section{margin:8px 0;scroll-margin-top:120px}.meta{font-size:13px;color:var(--muted);'
         'margin:2px 0}h2,h3{scroll-margin-top:120px}'
         '.segment{border-top:4px solid var(--muted);padding-top:8px;margin-top:40px}.derived{border-left:4px solid var(--warn);'
         'padding-left:10px}.harness{color:var(--muted)}.record{border-left:4px solid var(--accent);padding-left:10px}'
         '.file{border-left:4px solid var(--good);padding-left:10px}table{border-collapse:collapse}'
         '.hl-sub{border-left:5px solid var(--good);background:var(--good-bg);padding:4px 10px;border-radius:4px}'
         '.hl-sub.b-bad-row{border-left-color:var(--bad);background:var(--bad-bg)}'
         '.hl-sub.b-warn-row{border-left-color:var(--warn);background:var(--warn-bg)}'
         '.hl-judge{outline:2px solid var(--info);outline-offset:3px;border-radius:4px}'
         'aside.finding{border:1px solid var(--info);background:var(--info-bg);border-radius:8px;padding:8px 12px;'
         'margin:8px 0 14px;font-size:14px}aside.finding p{margin:3px 0}.flags{margin:4px 0}'
         '.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);'
         'padding:8px 0;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;font-size:14px}'
         '.bar input,.bar select,.bar button{font:inherit;font-size:14px;padding:5px 8px;border:1px solid var(--line);'
         'border-radius:6px;background:var(--panel);color:var(--text)}.bar input{min-width:0;flex:1 1 160px}'
         '.bar button{cursor:pointer}.bar .grow{flex:1 1 auto}mark{background:var(--mark);color:var(--mark-text)}'
         'mark.now{outline:2px solid var(--bad)}section.target{outline:3px solid var(--accent);outline-offset:3px}'
         '.hits li{margin:4px 0}.hits .snip{font-family:ui-monospace,monospace;font-size:12.5px;color:var(--muted)}'
         '.highlights li{margin:5px 0}'
         '@media (max-width:640px){.bar{font-size:13px;gap:4px 6px}.bar .grow{display:none}.bar [data-details]{display:none}'
         'pre{font-size:12.5px}section,h2,h3{scroll-margin-top:130px}}</style>')
COLLAPSE_CHARS = 1500   # longer tool calls, results and records start collapsed (full text is always present)
COLLAPSE_LINES = 30
PREVIEW_CHARS = 140
LABELS = {"reasoning": "Reasoning (returned; untrusted self-report)", "text": "Visible text",
          "summary": "Compaction summary (derived self-report written by the agent's model)",
          "tool_call": "Tool call", "tool_result": "Tool result", "conference": "Conference record",
          "file_version": "Private file version", "runtime": "Harness input"}
JUDGE_LABEL = ('Behavior judge finding: model-generated (judge model, then confirmed by an independent verifier '
               'model call); not a researcher verdict')

esc = html.escape


def js_data(value):
    """JSON for an inline <script> (no closing tags or HTML comments can form)."""
    return (json.dumps(value, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e')
            .replace('&', '\\u0026').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def badge(text, css=None):
    return f'<span class="badge {css or badge_class(text)}">{esc(str(text))}</span>'


def anchor(item_id):
    """Link to an item anchor (IDs may contain brackets, slashes and @)."""
    return f'<a class="id" href="#{quote(item_id, safe="")}">{esc(item_id)}</a>'


class Marks:
    """Highlights for one agent page: submissions (with outcomes) and verified judge findings."""

    def __init__(self, history, findings=(), evaluations=None):
        self.submissions = {i['id']: i for i in history.items
                            if i['kind'] == 'conference' and str(i['id']).startswith('sub-')}
        self.evaluations = evaluations or {}
        self.findings = list(findings)
        self.by_item = collections.defaultdict(list)
        present = {i['id'] for i in history.items} | {h['id'] for h in history.harness_items}
        for finding in self.findings:
            finding['present'] = [x for x in finding['items'] if x in present]
            for item in finding['present']:
                self.by_item[item].append(finding)

    def validity(self, sub_id):
        """Proof-checker validity of a submission, only when the record's source hash matches."""
        record = self.evaluations.get(sub_id)
        item = self.submissions.get(sub_id)
        if not record or not item or record.get('source_sha256') != item['meta'].get('source_sha256'):
            return None
        return record.get('validity')

    def submission_of(self, item):
        if item['kind'] == 'conference' and item['id'] in self.submissions:
            return item['id']
        if item['kind'] in ('tool_call', 'tool_result'):
            sub = item['meta'].get('conference_id')
            if sub in self.submissions:
                return sub
        return None

    def classes(self, item):
        out = []
        sub = self.submission_of(item)
        if sub and item['kind'] != 'tool_result':
            outcome = self.submissions[sub]['meta'].get('outcome')
            kind = badge_class(outcome)
            out.append('hl-sub' + {'b-bad': ' b-bad-row', 'b-warn': ' b-warn-row'}.get(kind, ''))
        if item['id'] in self.by_item:
            out.append('hl-judge')
        return out

    def badges(self, item):
        sub = self.submission_of(item)
        if not sub or item['kind'] == 'tool_result':
            return ''
        meta = self.submissions[sub]['meta']
        parts = [f'<strong>Submission</strong> {anchor(sub)}', esc(str(meta.get('problem_id'))),
                 'conference ' + badge(meta.get('outcome'))]
        if meta.get('outcome') == 'rejected' and (meta.get('stage') or meta.get('reason')):
            parts.append(esc(' / '.join(str(x) for x in (meta.get('stage'), meta.get('reason')) if x)))
        validity = self.validity(sub)
        if validity:
            parts.append('proof checker ' + badge(validity))
        return '<p class="flags">' + ' · '.join(parts) + '</p>'

    def after(self, item):
        return ''.join(finding_card(f) for f in self.by_item.get(item['id'], ()))


def finding_card(finding, *, linked=False):
    level = ' '.join(str(x) for x in (finding.get('category'), finding.get('level')) if x).replace('_', ' ')
    head = finding.get('heading') or finding.get('summary') or ''
    cites = ', '.join(anchor(x) for x in finding.get('present') or ()) if linked else ''
    quotes = ''.join(f'<p>“{esc(q)}” <small>({esc(i)})</small></p>' for i, q in finding.get('quotes') or ())
    return (f'<aside class="finding" data-finding="{esc(finding["id"], quote=True)}"><p>{badge("judge " + finding["id"], "b-info")}'
            f' <span class="badge b-info">{esc(level)}</span> <strong>{esc(head)}</strong></p>'
            + (f'<p>{esc(finding["summary"])}</p>' if finding.get('heading') and finding.get('summary') else '')
            + quotes + (f'<p><small>Cited: {cites}</small></p>' if cites else '')
            + f'<p><small>{esc(JUDGE_LABEL)}.</small></p></aside>')


def _meta_line(item, extra=""):
    when = item.get("time") or (" to ".join(t or "?" for t in item["time_bounds"]) if item.get("time_bounds")
                                else "time unknown")
    channel = item.get("channel")
    return (f'<p class="meta">{anchor(item["id"])} · {esc(LABELS.get(item["kind"], item["kind"]))} · '
            f'channel {esc(str(channel))} · {esc(when)}{extra}</p>')


def _long(text):
    return len(text) > COLLAPSE_CHARS or text.count('\n') >= COLLAPSE_LINES


def _body(item, collapse_label=None, open_=False):
    text = item.get("text") or ""
    block = f'<pre>{esc(text)}</pre>'
    if collapse_label is None:
        return block
    first = ' '.join(text.split())[:PREVIEW_CHARS]
    preview = f' <span class="pv">{esc(first)}{"…" if len(text) > PREVIEW_CHARS else ""}</span>' if first else ''
    lines = text.count('\n') + 1 if text else 0
    return (f'<details{" open" if open_ else ""}><summary>{esc(collapse_label)} ({len(text):,} characters, '
            f'{lines:,} lines){preview}</summary>{block}</details>')


def _section(item, extra, body, marks, css=None):
    classes = [css] if css else []
    note = after = ''
    if marks is not None:
        classes += marks.classes(item)
        note, after = marks.badges(item), marks.after(item)
    cls = f' class="{" ".join(classes)}"' if classes else ''
    return f'<section id="{esc(item["id"])}"{cls}>{_meta_line(item, extra)}{note}{body}</section>{after}'


def _part(item, marks=None):
    """One turn part (reasoning, visible text, summary or tool call) as a section."""
    kind = item["kind"]
    text = item.get("text") or ""
    if kind == "reasoning":
        body = _body(item, "Reasoning")
    elif kind == "summary":
        body = _body(item, "Compaction summary", open_=False)
    elif kind == "tool_call" and _long(text):
        body = _body(item, "Tool call input")
    else:
        body = _body(item)
    css = "derived" if item["meta"].get("derived") else None
    extra = ""
    if kind == "tool_call" and item["meta"].get("conference"):
        join = item["meta"]["conference"]
        extra = f' · conference {esc(join["status"])}' + (f' seq {join["seq"]}' if join.get("seq") else "")
    return _section(item, extra, body, marks, css)


def _result_article(item, number, marks=None):
    meta = item["meta"]
    extra = f' · {esc(str(meta.get("function")))}'
    join = meta.get("conference")
    if join:
        extra += f' · conference {esc(join["status"])}' + (f' seq {join["seq"]}' if join.get("seq") else "")
        if join.get("candidates"):
            extra += f' (candidates {esc(", ".join(map(str, join["candidates"])))})'
    if meta.get("crosswalk"):
        extra += ' · IDs: ' + esc(", ".join(f"{k} = {v}" for k, v in meta["crosswalk"].items()))
    if meta.get("error"):
        extra += ' · error ' + esc(json.dumps(meta["error"], ensure_ascii=False))
    text = item.get("text") or ""
    body = _body(item, "Result", open_=False) if _long(text) else _body(item)
    message_id = meta.get("tool_message_id")
    small = f'<small>Message ID: {esc(str(message_id))}</small>' if message_id else ""
    prefix, _, call_number = item["id"].rpartition(".o")
    return (f'<article id="message-{number}"><h3>Result of {esc(prefix)}.c{esc(call_number)}</h3>{small}'
            + _section(item, extra, body, marks) + '</article>')


def _record_article(item, marks=None):
    meta = item["meta"]
    facts = []
    for key in ("intent_type", "recipient", "public_id", "path", "revision", "problem_id", "outcome", "stage",
                "reason", "call_item", "event_seq"):
        if meta.get(key) not in (None, "", []):
            facts.append(f"{key} {meta[key]}")
    if meta.get("automatic"):
        facts.append("published automatically on acceptance")
    extra = (" · " + esc("; ".join(map(str, facts)))) if facts else ""
    text = item.get("text") or ""
    body = _body(item, "Record", open_=False) if _long(text) else _body(item)
    return '<article class="record">' + _section(item, extra, body, marks) + '</article>'


def _file_article(item, marks=None):
    meta = item["meta"]
    extra = (f' · version {meta.get("version")} · {meta.get("bytes")} bytes · runtime line {meta.get("runtime_line")}'
             + (f' · integrity {esc(meta["integrity"])}' if meta.get("integrity") != "ok" else "")
             + (f' · same content as {esc(meta["duplicate_of"])}' if meta.get("duplicate_of") else ""))
    return '<article class="file">' + _section(item, extra, _body(item, meta.get("path") or "File"), marks) + '</article>'


def _harness_article(item, number, marks=None):
    meta = item["meta"]
    repeats = meta.get("repeat_turns") or []
    extra = f' · {esc(str(meta.get("role")))} · {esc(str(meta.get("harness_kind")))}'
    if repeats:
        extra += f' · repeated before {len(repeats)} later turn(s)'
    small = f'<small>Message ID: {esc(str(meta["message_id"]))}</small>' if meta.get("message_id") else ""
    label = "Harness input (token counters removed)"
    return (f'<article id="message-{number}" class="harness">{small}'
            + _section(item, extra, _body(item, label), marks) + '</article>')


PAGE_SCRIPT = r"""<script>
(()=>{
const $=s=>document.querySelector(s);
// Same-origin navigation keeps the page's query (the hosted Space's short-lived session).
function go(href){const u=new URL(href,location.href);if(u.origin===location.origin)u.search=location.search;location.href=u.href;}
function openTo(el){for(let d=el&&el.closest('details');d;d=d.parentElement&&d.parentElement.closest('details'))d.open=true;}
let lastTarget=null;
function goHash(){let id='';try{id=decodeURIComponent(location.hash.slice(1));}catch(e){return;}
 const el=id&&document.getElementById(id);if(!el)return;openTo(el);
 if(lastTarget)lastTarget.classList.remove('target');el.classList.add('target');lastTarget=el;el.scrollIntoView({block:'start'});}
addEventListener('hashchange',goHash);
const sw=$('#agent-switch');if(sw)sw.addEventListener('change',()=>go(sw.value));
for(const b of document.querySelectorAll('[data-details]'))b.addEventListener('click',()=>{
 const open=b.dataset.details==='open';for(const d of document.querySelectorAll('main details'))d.open=open;});
const box=$('#find'),count=$('#find-count');let marks=[],cur=-1;
function clear(){for(const m of marks){const p=m.parentNode;if(!p)continue;m.replaceWith(document.createTextNode(m.textContent));p.normalize();}marks=[];cur=-1;}
function show(i){if(!marks.length){count.textContent=box.value.trim().length>1?'no matches':'';return;}
 if(cur>=0)marks[cur].classList.remove('now');cur=(i+marks.length)%marks.length;const m=marks[cur];m.classList.add('now');
 openTo(m);m.scrollIntoView({block:'center'});count.textContent=(cur+1)+' of '+marks.length+(marks.length>=2000?'+':'');}
function find(){clear();const q=box.value.trim();if(q.length<2){show(0);return;}
 const ql=q.toLowerCase(),walk=document.createTreeWalker($('main'),NodeFilter.SHOW_TEXT),nodes=[];
 while(walk.nextNode())nodes.push(walk.currentNode);
 for(const n of nodes){const s=n.nodeValue,l=s.toLowerCase();let i=l.indexOf(ql);if(i<0)continue;
  const f=document.createDocumentFragment();let last=0;
  while(i>=0&&marks.length<2000){f.append(s.slice(last,i));const m=document.createElement('mark');m.textContent=s.slice(i,i+q.length);
   f.append(m);marks.push(m);last=i+q.length;i=l.indexOf(ql,last);}
  f.append(s.slice(last));n.replaceWith(f);if(marks.length>=2000)break;}
 show(0);}
if(box){box.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();
  if(marks.length&&box.dataset.q===box.value)show(cur+(e.shiftKey?-1:1));else{box.dataset.q=box.value;find();}}});
 $('#find-next').addEventListener('click',()=>{if(box.dataset.q!==box.value){box.dataset.q=box.value;find();}else show(cur+1);});
 $('#find-prev').addEventListener('click',()=>show(cur-1));
 const all=$('#find-all');if(all)all.addEventListener('click',e=>{e.preventDefault();
  go('index.html#search='+encodeURIComponent(box.value.trim()));});
 let q=null;try{q=sessionStorage.getItem('transcript-find');sessionStorage.removeItem('transcript-find');}catch(e){}
 if(q){box.value=q;box.dataset.q=q;find();}}
goHash();
})();
</script>"""

INDEX_SCRIPT = r"""<script>
(()=>{
const AGENTS=AGENT_DATA;
const $=s=>document.querySelector(s),box=$('#search'),out=$('#hits'),status=$('#search-status');
const cache=new Map();let run=0;
function esc(s){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function load(a){if(cache.has(a.file))return cache.get(a.file);
 const r=await fetch(a.file);if(!r.ok)throw new Error('HTTP '+r.status);
 const doc=new DOMParser().parseFromString(await r.text(),'text/html');
 const rows=[...doc.querySelectorAll('main section[id]')].map(s=>{const m=s.querySelector('.meta');const b=s.querySelector('pre');
  return [s.id,m?m.textContent:'',b?b.textContent:''];});cache.set(a.file,rows);return rows;}
function snippet(t,i,n){const a=Math.max(0,i-70),b=Math.min(t.length,i+n+90);
 return (a?'…':'')+esc(t.slice(a,i))+'<mark>'+esc(t.slice(i,i+n))+'</mark>'+esc(t.slice(i+n,b))+(b<t.length?'…':'');}
async function search(q){const mine=++run;out.innerHTML='';q=q.trim();if(q.length<2){status.textContent='Type at least two characters.';return;}
 if(location.protocol==='file:'){status.textContent='Search across agents needs the pages served over http(s): the hosted library or a local web server (python -m http.server).';return;}
 const ql=q.toLowerCase();let done=0,total=0,failed=0;const queue=AGENTS.slice();
 status.textContent='Searching 0 of '+AGENTS.length+' agents…';
 async function worker(){while(queue.length&&mine===run){const a=queue.shift();let rows;
  try{rows=await load(a);}catch(e){failed++;done++;continue;}
  if(mine!==run)return;const hits=[];
  for(const [id,meta,text] of rows){const i=text.toLowerCase().indexOf(ql);const j=i<0?meta.toLowerCase().indexOf(ql):-1;
   if(i>=0||j>=0)hits.push([id,i>=0?snippet(text,i,q.length):snippet(meta,j,q.length)]);}
  done++;total+=hits.length;
  if(hits.length){const li=document.createElement('li');
   li.innerHTML='<details'+(hits.length<=12?' open':'')+'><summary><strong>'+esc(a.id)+'</strong> · '+hits.length+' matching item(s)</summary><ul class="hits">'+
    hits.slice(0,200).map(([id,sn])=>'<li><a class="id" data-q="'+esc(q)+'" href="'+esc(a.file)+'#'+encodeURIComponent(id)+'">'+esc(id)+'</a> <span class="snip">'+sn+'</span></li>').join('')+
    (hits.length>200?'<li>… '+(hits.length-200)+' more; open the agent page and use Find.</li>':'')+'</ul></details>';
   out.append(li);}
  status.textContent=(done<AGENTS.length?'Searching '+done+' of ':'Searched ')+AGENTS.length+' agents: '+total+' matching item(s)'+(failed?'; '+failed+' page(s) failed to load':'')+'.';}}
 await Promise.all([worker(),worker(),worker()]);}
out.addEventListener('click',e=>{const a=e.target.closest('a[data-q]');if(a){try{sessionStorage.setItem('transcript-find',a.dataset.q);}catch(err){}}});
$('#search-form').addEventListener('submit',e=>{e.preventDefault();search(box.value);});
const h=location.hash.match(/^#search=(.*)$/);if(h){try{box.value=decodeURIComponent(h[1]);}catch(e){}search(box.value);}
})();
</script>"""


def _head(title):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{esc(title)}</title>{STYLE}</head><body>')


def _toolbar(history, agents):
    """Sticky bar: run links, agent switcher with previous/next, find in page, search all agents."""
    names = [a for a, _ in agents] or [history.agent_id]
    files = dict(agents) or {history.agent_id: history.agent_id + '.html'}
    at = names.index(history.agent_id) if history.agent_id in names else 0
    options = ''.join(f'<option value="{esc(files[a], quote=True)}"{" selected" if a == history.agent_id else ""}>'
                      f'{esc(a)}</option>' for a in names)
    prev_next = ''
    if len(names) > 1:
        prev_next = (f'<a href="{esc(files[names[at - 1]], quote=True)}" title="Previous agent">‹ prev</a> '
                     f'<a href="{esc(files[names[(at + 1) % len(names)]], quote=True)}" title="Next agent">next ›</a>')
    return ('<p class="crumbs"><a href="../../index.html">All experiments</a> · <a href="../results.html">Run report</a>'
            ' · <a href="index.html">All agents</a> · <a href="../conference-view/index.html">Conference</a></p>'
            '<div class="bar" role="navigation">'
            f'<label class="muted" for="agent-switch">Agent</label><select id="agent-switch">{options}</select>{prev_next}'
            '<span class="grow"></span><input id="find" type="search" placeholder="Find in this agent…" '
            'aria-label="Find in this agent"><button id="find-prev" type="button" title="Previous match">↑</button>'
            '<button id="find-next" type="button" title="Next match">↓</button><span id="find-count" class="muted"></span>'
            '<a id="find-all" href="index.html">Search all agents</a>'
            '<button type="button" data-details="open">Expand all</button>'
            '<button type="button" data-details="close">Collapse all</button></div>')


def _highlights(history, marks, judge_info):
    """Jump list of this agent's submissions and verified judge findings."""
    subs = []
    for sub_id, item in marks.submissions.items():
        meta = item['meta']
        validity = marks.validity(sub_id)
        call = meta.get('call_item')
        subs.append(f'<li>{anchor(sub_id)} · {esc(item.get("time") or "")} · {esc(str(meta.get("problem_id")))} · '
                    f'conference {badge(meta.get("outcome"))}'
                    + (f' · proof checker {badge(validity)}' if validity else '')
                    + (f' · call {anchor(call)}' if call else '') + '</li>')
    out = ['<h2 id="highlights">Highlights</h2>',
           f'<h3>Submissions ({len(subs)})</h3>',
           ('<ul class="highlights">' + ''.join(subs) + '</ul>') if subs else '<p class="muted">No submissions.</p>',
           '<p class="muted"><small>Conference outcomes are the automatic operational checks, not mathematical '
           'validity. Proof-checker validity is shown only where RUN/evaluations/ has a record for the same source.'
           '</small></p>']
    status = judge_info.get('status')
    out.append(f'<h3>Behavior judge findings ({len(marks.findings)} verified)</h3>')
    if status == 'shown':
        out.append(f'<p class="muted"><small>{esc(JUDGE_LABEL)}. From the judge outputs of '
                   f'{esc(str(judge_info.get("generated_at") or "an unknown time"))}; refuted and unverified findings are '
                   'not shown. The judge\'s own pages are authoritative for its results.</small></p>')
        if judge_info.get('evidence', {}).get(history.agent_id):
            out.append(f'<p><a href="{esc(judge_info["evidence"][history.agent_id], quote=True)}">This agent\'s judge '
                       'evidence page</a> · <a href="../analysis/judge/report.html">Judge report</a></p>')
        if marks.findings:
            out.append(''.join(finding_card(f, linked=True) for f in marks.findings))
        else:
            out.append('<p class="muted">No verified findings for this agent.</p>')
    else:
        out.append(f'<p class="muted">{esc(judge_info.get("note") or "Behavior judge: not run.")}</p>')
    return ''.join(out)


def render_agent(history, run_id, *, agents=(), findings=(), evaluations=None, judge_info=None):
    """(HTML page, number of message articles) for one AgentHistory.

    agents: (agent_id, file name) pairs for the agent switcher; findings: this agent's verified
    judge findings (see judge_findings); evaluations: host proof-checker records by submission ID.
    """
    marks = Marks(history, findings, evaluations)
    judge_info = judge_info or {'status': 'not_run', 'note': 'Behavior judge: not run.'}
    stats = history.stats
    harness = sorted(history.harness_items, key=lambda h: (h["turn"], h["id"]))
    segments = {s["segment"]: s for s in history.segments}
    turn_kinds = ("reasoning", "text", "summary", "tool_call", "tool_result")
    # Group each turn's contiguous items: one article for the output (reasoning, text or
    # summary, calls), then one article per result, so each Inspect message ID has one anchor.
    groups = []
    for item in history.items:
        turn = item.get("turn") if item["kind"] in turn_kinds else None
        if turn is not None and groups and groups[-1][0] == turn:
            groups[-1][1].append(item)
        else:
            groups.append((turn, [item]))
    out, number, segment = [], 0, None
    for turn, members in groups:
        if turn is None:
            item = members[0]
            if item["kind"] == "conference":
                out.append(_record_article(item, marks))
            elif item["kind"] == "file_version":
                out.append(_file_article(item, marks))
            else:
                out.append('<article>' + _section(item, '', _body(item), marks) + '</article>')
            continue
        seg = members[0]["meta"].get("segment")
        if seg is not None and seg != segment:
            segment = seg
            info = segments.get(seg, {})
            out.append(f'<h2 class="segment" id="segment-{seg}">Segment {seg} · turns '
                       f'{info.get("first_turn", "?")}–{info.get("last_turn", "?")}</h2>')
        while harness and harness[0]["turn"] <= turn:
            number += 1
            out.append(_harness_article(harness.pop(0), number, marks))
        parts = [i for i in members if i["kind"] != "tool_result"]
        if parts:
            number += 1
            first = parts[0]["meta"]
            small = f'<small>Message ID: {esc(str(first["message_id"]))}</small>' if first.get("message_id") else ""
            title = f'Turn {turn}' + (' · compaction summary turn' if first.get("derived") else '')
            notes = [f'stop {first.get("stop_reason")}']
            if first.get("adopted") is False:
                notes.append('output discarded by the harness: never executed or carried forward')
            out.append(f'<article id="message-{number}"><h3>{esc(title)}</h3>{small}<p class="meta">'
                       f'{esc(parts[0].get("time") or "")} · {esc(" · ".join(notes))}</p>'
                       + ''.join(_part(i, marks) for i in parts) + '</article>')
        for item in members:
            if item["kind"] == "tool_result":
                number += 1
                out.append(_result_article(item, number, marks))
    for item in harness:
        number += 1
        out.append(_harness_article(item, number, marks))

    exit_status = (history.exit or {}).get("status")
    rows = [("Persona", history.persona), ("Exit", exit_status), ("Model turns", stats.get("turns")),
            ("Compaction summary turns", stats.get("summary_turns")), ("Segments", stats.get("segments")),
            ("Items", stats.get("items")), ("Estimated tokens (characters / 4)", stats.get("tokens_total")),
            ("Items by kind", json.dumps(stats.get("items_by_kind"), ensure_ascii=False)),
            ("Conference joins", json.dumps(stats.get("conference_joins"), ensure_ascii=False)),
            ("Exposure-ledger entries", stats.get("exposure_entries")),
            ("Tool calls without a recorded result", len(stats.get("missing_tool_results") or [])),
            ("Model events with errors", ", ".join(map(str, stats.get("model_errors") or [])) or "none"),
            ("Inspect sample error", stats.get("inspect_error_message") or "none"),
            ("Private file versions excluded", json.dumps(stats.get("files_excluded"), ensure_ascii=False))]
    table = ('<details><summary>Agent record facts</summary><table>' + ''.join(
        f'<tr><th>{esc(k)}</th><td>{esc(str(v))}</td></tr>' for k, v in rows) + '</table></details>')
    toc = ''.join(f'<li><a href="#segment-{s["segment"]}">Segment {s["segment"]}</a>: turns {s["first_turn"]}–'
                  f'{s["last_turn"]}' + (f' (summary {anchor("%s.t%04d.s" % (history.code, s["summary_turn"]))})'
                                         if s.get("summary_turn") is not None else '') + '</li>'
                  for s in history.segments)
    exit_note = f' · exit {badge(exit_status)}' if exit_status else ''
    page = (_head(f'{history.agent_id} · {run_id}') + _toolbar(history, agents) +
            f'<h1>{esc(history.agent_id)} <small>({esc(history.code)})</small></h1>'
            f'<p class="meta">Run {esc(run_id)}{exit_note} · {esc(str(stats.get("turns")))} model turns · '
            f'{esc(str(stats.get("segments")))} segment(s) · <a href="#highlights">highlights</a></p>'
            '<p><small>Private researcher export: do not provide to experimental agents. Full '
            'reconstructed history across every compaction segment, shared with the transcript judge '
            '(research/analysis/judge/history.py). IDs are the judge\'s citation anchors. Returned reasoning '
            'is untrusted self-report; compaction summaries are derived from earlier turns. Tool-result times '
            'are the joined conference event time or the bounds between the issuing turn and the next request. '
            'Private-file times are capture times, not edit times. Times are UTC. Raw Inspect logs and '
            'conference records remain authoritative. Long tool inputs, results and records start collapsed; '
            'Find opens the ones that match.</small></p>' + table + (f'<ul>{toc}</ul>' if toc else '') +
            _highlights(history, marks, judge_info) + '<main>' + ''.join(out) + '</main>' + PAGE_SCRIPT +
            '</body></html>')
    return page, number


def _read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def judge_findings(run, agent_ids):
    """(agent -> verified findings, info) from this run's paid behavior-judge outputs, if any.

    Reads RUN/analysis/judge/report.json (must name this run and be paid) and
    RUN/analysis/judge/agents/<agent>.json; keeps only findings the verifier confirmed
    (state "confirmed"). Each finding: id, category, level, heading, summary, cited items, quotes.
    """
    judge = Path(run) / 'analysis' / 'judge'
    report = _read_json(judge / 'report.json')
    if report is None:
        return {}, {'status': 'not_run', 'note': 'Behavior judge: not run when these pages were exported.'}
    if report.get('run_id') not in (None, Path(run).name):
        return {}, {'status': 'skipped', 'note': 'Behavior judge outputs belong to another run; not shown.'}
    if report.get('mode') != 'paid':
        return {}, {'status': 'skipped', 'note': f'Behavior judge outputs are mode {report.get("mode")!r}, not a '
                                                  'paid judge run; not shown.'}
    found, evidence = {}, {}
    for agent in agent_ids:
        record = _read_json(judge / 'agents' / f'{agent}.json')
        if not isinstance(record, dict) or record.get('agent_id') != agent or record.get('mode') != 'paid':
            continue
        rows = []
        for f in ((record.get('verified') or {}).get('findings') or []):
            if not isinstance(f, dict) or f.get('state') != 'confirmed' or not f.get('id'):
                continue
            cited = [e for e in (f.get('evidence') or []) if isinstance(e, dict) and e.get('item')]
            rows.append({'id': str(f['id']), 'category': f.get('category'),
                         'level': f.get('established_level') or f.get('level'),
                         'heading': f.get('plain_heading'), 'summary': f.get('summary'),
                         'items': list(dict.fromkeys(str(e['item']) for e in cited)),
                         'quotes': [(str(e['item']), str(e['quote'])) for e in cited if e.get('quote')]})
        found[agent] = rows
        page = judge / 'review' / 'agents' / f'{agent}.html'
        if page.is_file() and not page.is_symlink():
            evidence[agent] = f'../analysis/judge/review/agents/{quote(agent, safe="")}.html'
    return found, {'status': 'shown', 'generated_at': report.get('generated_at'), 'evidence': evidence}


def render_index(run_id, rows, judge_info):
    """Agent table and the search across all agents (client-side, over the sibling agent pages)."""
    data = [{'id': r['agent_id'], 'file': r['output']} for r in rows]
    table = []
    for r in rows:
        outcomes = ' '.join(badge(f'{k} {n}', badge_class(k)) for k, n in sorted(r['submission_outcomes'].items()))
        findings = r['verified_findings']
        table.append(f'<tr><td><a href="{esc(r["output"], quote=True)}">{esc(r["agent_id"])}</a></td>'
                     f'<td>{badge(r["exit"]) if r["exit"] else "<span class=muted>not recorded</span>"}</td>'
                     f'<td>{r["turns"]:,}</td><td>{r["segments"]}</td><td>{outcomes or "<span class=muted>none</span>"}</td>'
                     f'<td>{findings if findings is not None else "<span class=muted>—</span>"}</td>'
                     f'<td>{r["tokens_estimate"]:,}</td></tr>')
    judge_line = ('Verified behavior-judge findings are shown beside the items they cite; ' + JUDGE_LABEL.lower() + '.'
                  if judge_info.get('status') == 'shown' else judge_info.get('note') or 'Behavior judge: not run.')
    return (_head(f'Agent transcripts · {run_id}') +
            '<p class="crumbs"><a href="../../index.html">All experiments</a> · <a href="../results.html">Run report</a> · '
            '<a href="../conference-view/index.html">Conference</a></p>'
            f'<h1>Agent transcripts</h1><p class="meta">Run {esc(run_id)} · {len(rows)} agent(s)</p>'
            '<p><small>Private researcher exports; do not provide to experimental agents. Each page shows the full '
            'reconstructed history across compactions (reasoning, tool calls and results, summaries, own conference '
            'records, private file versions), with judge citation IDs as anchors. ' + esc(judge_line) + '</small></p>'
            '<h2>Search all agents</h2><form id="search-form" role="search" class="bar" style="position:static">'
            '<input id="search" type="search" placeholder="Text to find in every agent\'s transcript…" '
            'aria-label="Search all agents"><button type="submit">Search</button></form>'
            '<p id="search-status" class="muted"><small>Loads each agent page in your browser (several MB each) the '
            'first time; later searches reuse them.</small></p><ol id="hits" class="hits"></ol>'
            '<h2>Agents</h2><div class="scroll"><table><tr><th>Agent</th><th>Exit</th><th>Model turns</th>'
            '<th>Segments</th><th>Submissions (conference outcome)</th><th>Verified judge findings</th>'
            '<th>About tokens</th></tr>' + ''.join(table) + '</table></div>'
            + INDEX_SCRIPT.replace('AGENT_DATA', js_data(data)) + '</body></html>')


def export(run_dir, out_dir=None, *, agents=None, require_saved_run=True):
    """Write ``<out>/<agent>.html``, ``index.html`` and ``manifest.json`` (default out: RUN/transcripts)."""
    run = Path(run_dir).resolve()
    if require_saved_run and (not run.is_relative_to(ROOT / 'runs') or run == ROOT / 'runs'):
        raise ValueError('Use a saved researcher run under this repository runs directory')
    if not list((run / 'inspect').glob('*.eval')):
        raise ValueError('No saved Inspect logs found')
    for path in (ROOT / 'src', ROOT):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from research.analysis.judge import history as judge_history  # disables Inspect .env loading first
    built = judge_history.build_run(run, agents=agents)
    out = Path(out_dir).resolve() if out_dir else run / 'transcripts'
    out.mkdir(parents=True, exist_ok=True)
    names = sorted(built.agents)
    switcher = [(a, a + '.html') for a in names]
    findings, judge_info = judge_findings(run, names)
    evaluations = (getattr(built, 'host', None) or {}).get('evaluations') or {}
    rows, manifest = [], []
    for agent in names:
        record = built.agents[agent]
        page, messages = render_agent(record, built.run_id, agents=switcher, findings=findings.get(agent, ()),
                                      evaluations=evaluations, judge_info=judge_info)
        name = agent + '.html'
        (out / name).write_text(page, encoding='utf-8')
        stats = record.stats
        outcomes = collections.Counter(i['meta'].get('outcome') for i in record.items
                                       if i['kind'] == 'conference' and str(i['id']).startswith('sub-'))
        rows.append({'agent_id': agent, 'output': name, 'exit': (record.exit or {}).get('status'),
                     'turns': stats['turns'], 'segments': stats['segments'], 'tokens_estimate': stats['tokens_total'],
                     'submission_outcomes': {str(k): n for k, n in outcomes.items()},
                     'verified_findings': len(findings[agent]) if agent in findings else None})
        manifest.append({'agent_id': agent, 'messages': messages, 'source': record.source, 'output': name,
                         'inspect_error': bool(stats.get('inspect_error')), 'history_schema': judge_history.SCHEMA,
                         'turns': stats['turns'], 'summary_turns': stats['summary_turns'],
                         'segments': stats['segments'], 'items': stats['items'],
                         'items_by_kind': stats['items_by_kind'], 'tokens_estimate': stats['tokens_total'],
                         'harness_items': stats['harness_items'], 'exposure_entries': stats['exposure_entries'],
                         'missing_tool_results': len(stats['missing_tool_results'])})
    (out / 'index.html').write_text(render_index(built.run_id, rows, judge_info), encoding='utf-8')
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return {'agents': len(manifest), 'messages': sum(x['messages'] for x in manifest),
            'items': sum(x['items'] for x in manifest), 'index': str(out / 'index.html'),
            'behavior_judge': judge_info['status'],
            'verified_findings': sum(len(v) for v in findings.values())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, help='default: RUN/transcripts')
    parser.add_argument('--agent', action='append', help='export only this agent (repeatable)')
    options = parser.parse_args()
    print(json.dumps(export(options.run_dir, options.output_dir, agents=options.agent)))
