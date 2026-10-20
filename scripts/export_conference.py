#!/usr/bin/env python3
"""Render saved conference records and captured private files, without inference."""
import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import gzip
import html
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
STYLE = '''
:root{font-family:system-ui,sans-serif;color:#182536;background:#f5f7fa;line-height:1.55}
body{max-width:1280px;margin:32px auto;padding:0 24px}a{color:#175ba6}h1{margin-bottom:8px}
.muted,small{color:#58677b}.banner{background:#eaf0fb;border-left:4px solid #6084bf;padding:12px 18px}
nav,.filters{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}button,input,select{font:inherit;padding:9px 12px;border:1px solid #bac6d6;border-radius:7px;background:white;color:inherit}
button{cursor:pointer}button[aria-selected=true]{background:#204e85;color:white;border-color:#204e85}
input{flex:1;min-width:200px}article{background:white;border:1px solid #dce3ec;border-radius:10px;padding:20px;margin:14px 0}
h2,h3{margin:0 0 8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f8;padding:14px;border-radius:6px;font:13px/1.6 ui-monospace,monospace}
.text{white-space:pre-wrap;overflow-wrap:anywhere}details{margin:12px 0}.badge{display:inline-block;background:#edf1f7;padding:2px 8px;border-radius:4px;font-size:12px}
.row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.empty{padding:32px;color:#58677b;border:1px dashed #bcc9d9;background:white;border-radius:10px}
footer{margin:30px 0;color:#58677b}[hidden]{display:none!important}
'''


def json_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def page(title, body):
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{STYLE}</style>{body}</html>')


def compact_browser_page(document):
    """Keep self-contained HTML below HF's large-file attachment threshold.

    Only the embedded records change representation. Browser-side decompression
    preserves the original JSON, links, filters and textContent rendering.
    """
    pattern = r'<script id="data" type="application/json">(.*?)</script><script>(.*?)</script>'
    match = re.search(pattern, document, re.S)
    if match is None:
        raise ValueError('Missing conference data and renderer')
    encoded, script = match.groups()
    json.loads(encoded)  # Refuse malformed records before producing an export.
    prefix = "const data=JSON.parse(document.getElementById('data').textContent);"
    if not script.lstrip().startswith(prefix):
        raise ValueError('Unexpected conference renderer bootstrap')
    packed = base64.b64encode(gzip.compress(encoded.encode('utf-8'), mtime=0)).decode('ascii')
    bootstrap = '''(async()=>{
if(typeof DecompressionStream==='undefined')throw new Error('This viewer needs a browser with gzip decompression support. Please use a current Chrome, Firefox, Edge or Safari.');
document.getElementById('status').textContent='Loading conference records…';
const bytes=Uint8Array.from(atob(document.getElementById('data').textContent),c=>c.charCodeAt(0));
const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
const data=JSON.parse(await new Response(stream).text());
'''
    renderer = bootstrap + script.lstrip()[len(prefix):] + '''
})().catch(error=>{document.getElementById('status').textContent='Could not load conference: '+error.message;});'''
    replacement = '<script id="data" type="application/gzip+base64">' + packed + '</script><script>' + renderer + '</script>'
    result = document[:match.start()] + replacement + document[match.end():]
    if len(result.encode('utf-8')) >= 8_000_000:
        raise ValueError('Conference HTML needs a split-data export before HF publication')
    return result


def export(run):
    run = Path(run).resolve()
    if not run.is_relative_to(ROOT / 'runs') or run == ROOT / 'runs' or run.is_symlink():
        raise ValueError('Use a saved run under this repository runs directory')
    out = run / 'conference-view'
    if out.is_symlink():
        raise ValueError('Conference output must not be a symlink')
    out.mkdir(exist_ok=True)
    events = json_lines(run / 'conference/events.jsonl')
    timeline = json_lines(run / 'analysis/timeline.jsonl')
    runtime = json_lines(run / 'runtime.jsonl')
    db = sqlite3.connect((run / 'conference/state.sqlite').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    tables = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name)]
              for name in ('posts', 'messages', 'feedback', 'submissions', 'library', 'events')}
    db.close()
    if [x['seq'] for x in tables['events']] != [x['seq'] for x in events]:
        raise ValueError('Database/event-log sequence mismatch')
    for row, event in zip(tables['events'], events):
        if row['kind'] != event['kind'] or json.loads(row['payload']) != event['payload']:
            raise ValueError('Database/event-log content mismatch')

    # Bind existing transcript message IDs to their real HTML anchors.
    message_links = {}
    for file in (run / 'transcripts').glob('agent-*.html'):
        for match in re.finditer(r'<article id="(message-\d+)">(?:(?!</article>).)*?<small>Message ID: (.*?)</small>', file.read_text(), re.S):
            message_links[file.stem, html.unescape(match[2])] = '../transcripts/' + file.name + '#' + match[1]
    call_links = {}
    for entry in timeline:
        ref = entry.get('ref', {})
        target = message_links.get((entry.get('agent_id'), ref.get('message_id')))
        if target:
            for call in entry.get('tool_calls', []):
                call_links[call['id']] = target
            if entry.get('call_id'):
                call_links[entry['call_id']] = target

    def transcript(agent, call_id=None):
        if call_id in call_links:
            return call_links[call_id]
        if agent and re.fullmatch(r'agent-\d+', agent) and (run / 'transcripts' / (agent + '.html')).exists():
            return '../transcripts/' + agent + '.html'
        return None

    def record(category, identity, title, text, timestamp, agents=(), task='', detail=None, links=None):
        return dict(category=category, id=identity, title=title, text=text or '', timestamp=timestamp,
                    agents=[a for a in agents if a], task=task or '', detail=detail,
                    links=links or [])

    records = []
    for row in tables['posts']:
        records.append(record('board', 'post-' + str(row['post_id']), row['intent_type'], row['message'], row['posted_at'],
                       [row['agent_id']], detail={'tags': json.loads(row['tags'])},
                       links=[['Agent transcript', transcript(row['agent_id'])]]))
    # The board stores posts, not a reply graph. Do not invent reply threads.
    conversations = defaultdict(list)
    for row in tables['messages']:
        conversations[tuple(sorted((row['sender'], row['recipient'])))].append(row)
    for peers, rows in sorted(conversations.items()):
        rows.sort(key=lambda r: (r['sent_at'], r['message_id']))
        records.append(record('dms', 'dm-' + '-'.join(peers), ' ↔ '.join(peers), '', rows[0]['sent_at'], peers,
                       detail={'messages': rows}, links=[['Transcript ' + a, transcript(a)] for a in peers]))
    for row in tables['feedback']:
        records.append(record('feedback', 'feedback-' + str(row['feedback_id']), 'Organizer feedback', row['message'],
                       row['submitted_at'], [row['agent_id']], links=[['Agent transcript', transcript(row['agent_id'])]]))
    for row in tables['library']:
        if hashlib.sha256(row['content'].encode()).hexdigest() != row['sha256']:
            raise ValueError('Library source hash mismatch')
        records.append(record('library', 'library-' + str(row['artifact_id']), row['path'], row['content'], row['published_at'],
                       [row['author']], row['task_id'], detail={k: v for k, v in row.items() if k != 'content'},
                       links=[['Author transcript', transcript(row['author'])]]))
    # A run whose independent evaluation has not run yet (e.g. refused) shows every submission as
    # "not independently evaluated" and records the evaluation source as absent.
    evaluations_path = run / 'evaluations/evaluations.jsonl'
    evaluations = ({x['submission']['submission_id']: x for x in json_lines(evaluations_path)}
                   if evaluations_path.exists() else {})
    for row in tables['submissions']:
        if hashlib.sha256(row['source'].encode()).hexdigest() != row['source_sha256']:
            raise ValueError('Submission source hash mismatch')
        sid = f"sub-{row['submission_id']:06d}"
        evaluation = evaluations.get(sid)
        if evaluation:
            s = evaluation['submission']
            if (s['agent_id'], s['problem_id'], evaluation['source_sha256']) != (row['agent_id'], row['task_id'], row['source_sha256']):
                raise ValueError('Independent evaluation binding mismatch')
        detail = {k: v for k, v in row.items() if k != 'source'}
        detail['independent_evaluation'] = ({'validity': evaluation['validity'], 'reason': evaluation['reason']}
                                            if evaluation else {'validity': 'not independently evaluated'})
        records.append(record('submissions', sid, sid + ' · ' + row['outcome'], row['source'], row['submitted_at'],
                       [row['agent_id']], row['task_id'], detail=detail,
                       links=[['Agent transcript', transcript(row['agent_id'])], ['Evaluation records', '../evaluations/evaluations.jsonl']]))

    # Browse only workspace working files and notes; SDK session JSONL already has a transcript viewer.
    versions = defaultdict(list)
    for line_number, entry in enumerate(runtime, 1):
        p = entry.get('payload', {})
        path = p.get('path', '')
        if entry['kind'] != 'file_snapshot' or not path.startswith('/workspace/'):
            continue
        relative = Path(path).relative_to('/workspace')
        if relative.parts[0] == 'problems' or relative.name in {'lakefile.toml', 'lake-manifest.json'}:
            continue
        agent = entry['agent_id']; digest = p.get('blob_sha256')
        if not re.fullmatch(r'agent-\d+', agent) or not digest or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Unsafe snapshot identity')
        blob = run / 'snapshots' / agent / digest
        if blob.is_symlink() or not blob.is_file():
            raise ValueError('Missing or linked snapshot blob')
        content = blob.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('Snapshot blob hash mismatch')
        text = content.decode('utf-8', errors='replace')
        versions[agent, path].append(dict(timestamp=entry['timestamp'], sha256=p.get('sha256'),
                  blob_sha256=digest, bytes=p.get('bytes'), mode=p.get('mode'), content=text, runtime_line=line_number))
    for (agent, path), rows in sorted(versions.items()):
        rows.sort(key=lambda x: (x['timestamp'], x['runtime_line']))
        identity = 'file-' + hashlib.sha256((agent + ':' + path).encode()).hexdigest()[:20]
        filename = identity + '.html'
        sections = []
        for i, row in enumerate(rows, 1):
            captured_at = datetime.fromtimestamp(row['timestamp'], ZoneInfo('America/Los_Angeles')).strftime('%b %d, %Y %I:%M:%S %p %Z')
            sections.append(f'<article id="version-{i}"><h2>Capture {i}</h2><p>{captured_at} · '
                            f'{html.escape(str(row["mode"]))} capture · {row["bytes"]} recorded bytes</p>'
                            f'<p>Runtime line {row["runtime_line"]} · blob SHA-256 <code>{row["blob_sha256"]}</code></p>'
                            '<pre>' + html.escape(row['content']) + '</pre></article>')
        (out / filename).write_text(page(agent + ' · ' + path,
            '<p><a href="index.html#files">← Captured files</a></p><h1>' + html.escape(agent + ' · ' + path) + '</h1>'
            '<p class="banner">Private researcher record. Captures are observations, not a complete edit history. '
            'A partial capture may omit content. Recorded times are capture times, not necessarily edit times.</p>' + ''.join(sections)))
        note = 'All captured versions are empty.' if all(not r['content'] for r in rows) else 'Open to inspect captured contents and versions.'
        records.append(record('files', identity, path, f'{len(rows)} captured version(s). {note}', rows[-1]['timestamp'], [agent],
                       detail={'captures': len(rows), 'empty': all(not r['content'] for r in rows),
                               'capture_modes': dict(Counter(r['mode'] for r in rows))},
                       links=[['View captured versions', filename], ['Agent transcript', transcript(agent)]]))

    for entry in timeline:
        agent = entry.get('agent_id'); ref = entry.get('ref', {})
        target = message_links.get((agent, ref.get('message_id'))) or transcript(agent, entry.get('call_id'))
        if entry.get('source') == 'conference':
            target = transcript(agent, entry.get('call_id'))
        text = entry.get('summary') or ''
        # Preserve provenance and useful fields; full tool outputs remain in original transcripts.
        detail = {'source': entry['source'], 'reference': ref, 'conference_seq': entry.get('conference_seq'),
                  'ids': entry.get('ids', {})}
        if entry['source'] != 'inspect':
            detail['payload'] = entry.get('payload', {})
        payload = entry.get('payload', {})
        task = payload.get('problem_id') or payload.get('task_id') or entry.get('ids', {}).get('problem_id') or ''
        records.append(record('activity', 'activity-' + str(entry['order']), entry['kind'], text,
                       entry.get('timestamp'), [agent], task=task, detail=detail,
                       links=[['Transcript message' if (agent, ref.get('message_id')) in message_links else 'Agent transcript', target]]))
    counts = {category: 0 for category in ('board', 'dms', 'library', 'submissions', 'feedback', 'files', 'activity')}
    counts.update(Counter(r['category'] for r in records))
    warnings = ['Private files are researcher-only and were not automatically shared with peers.',
                'Board records have no reply relationship; posts are shown chronologically.',
                'Missing timestamps stay unknown. Event order does not establish causal influence.',
                'Operational outcomes are separate from independent validity. Closed-task attempts may be unevaluated.']
    if not evaluations_path.exists():
        warnings.append('No independent evaluation records: evaluations/evaluations.jsonl is absent, so no '
                        'submission was independently evaluated.')
    data = dict(run=run.name, records=records, counts=counts, warnings=warnings,
                conference_events=len(events), timeline_entries=len(timeline), captured_files=len(versions))
    # Script-safe JSON; agent-controlled strings are rendered only with textContent.
    encoded = json.dumps(data, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    body = '''<p><a href="../../index.html">Experiment library</a> · <a href="../results.html">Run report</a> · <a href="../transcripts/index.html">Agent transcripts</a></p>
<h1>Inside the conference</h1><p id="run" class="muted"></p>
<p class="banner">Researcher view: private conversations and captured private files appear here alongside public conference activity. Agents did not have access to this combined view.</p>
<nav id="tabs" aria-label="Conference sections"></nav><p id="intro"></p>
<div class="filters"><input id="search" aria-label="Search records" placeholder="Search messages, tasks, sources…"><select id="agent" aria-label="Filter by agent"><option value="">All agents</option></select><select id="task" aria-label="Filter by task"><option value="">All tasks</option></select><select id="kind" aria-label="Filter activity type"><option value="">All activity types</option></select></div>
<div class="row"><p id="status" aria-live="polite"></p><div><button id="previous">Previous</button> <button id="next">Next</button></div></div>
<main id="records"></main><footer>Derived from the saved conference database, event log, runtime snapshots and timeline. <a href="../conference/events.jsonl">Conference events</a> · <a href="../analysis/timeline.jsonl">Full timeline</a> · <a href="manifest.json">Export manifest</a></footer>
'''
    script = r'''
const data=JSON.parse(document.getElementById('data').textContent);
const sections={board:['Bulletin board','Posts shown in posting order. The conference did not store a reply-thread relationship.','No bulletin-board posts were recorded.'],dms:['Direct messages','Private conversations grouped by the two participants.','No direct messages were saved in this run. Inbox checks are visible under All activity.'],library:['Shared library','Published artifacts and revisions. Complete accepted proofs were published automatically; private notes required deliberate publication.','No shared artifacts were recorded.'],submissions:['Submissions & judging','Conference outcomes and independently bound evaluations. “problem_closed” does not mean a proof was mathematically rejected.','No submissions were recorded.'],feedback:['Organizer feedback','Private feedback submitted to the conference organizers.','No organizer feedback was recorded.'],files:['Private notes & files','Captured versions of RESEARCH.md and private workspace working files. These are observations, not a complete edit history.','No private workspace file captures were recorded.'],activity:['All activity','Saved timeline in recorded chronological order, followed by entries without known timestamps. Filter by agent, task or event type; open details for provenance.','No timeline records were available.']};
let selected=sections[location.hash.slice(1)]?location.hash.slice(1):'board',pageNo=0;const size=40;
const el=id=>document.getElementById(id);const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
el('run').textContent=data.run+' · '+data.conference_events+' conference events · '+data.timeline_entries+' timeline entries';
function timestamp(value){return value==null?'Time unknown':new Date(value*1000).toLocaleString(undefined,{timeZone:'America/Los_Angeles',dateStyle:'medium',timeStyle:'medium'})+' America/Los_Angeles';}
for(const [id,[label]] of Object.entries(sections)){const b=node('button',label+' ('+(data.counts[id]||0)+')');b.onclick=()=>{selected=id;location.hash=id;pageNo=0;options();render();};b.dataset.section=id;el('tabs').append(b);}
function options(){const rows=data.records.filter(x=>x.category===selected);for(const [id,values,label] of [['agent',rows.flatMap(x=>x.agents),'All agents'],['task',rows.map(x=>x.task).filter(Boolean),'All tasks'],['kind',selected==='activity'?rows.map(x=>x.title):[],'All activity types']]){const old=el(id).value;el(id).replaceChildren(new Option(label,''));for(const v of [...new Set(values)].sort())el(id).append(new Option(v,v));if([...el(id).options].some(o=>o.value===old))el(id).value=old;}el('kind').hidden=selected!=='activity';el('task').hidden=el('task').options.length===1;}
function render(){document.querySelectorAll('[data-section]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.section===selected)));el('intro').textContent=sections[selected][1];const q=el('search').value.toLowerCase(),agent=el('agent').value,task=el('task').value,kind=el('kind').value;
let rows=data.records.filter(x=>x.category===selected&&(!agent||x.agents.includes(agent))&&(!task||x.task===task)&&(!kind||x.title===kind)&&(!q||JSON.stringify(x).toLowerCase().includes(q)));
if(selected!=='activity')rows.sort((a,b)=>(a.timestamp??Infinity)-(b.timestamp??Infinity));
pageNo=Math.min(pageNo,Math.max(0,Math.ceil(rows.length/size)-1));el('status').textContent=rows.length+' records · page '+(pageNo+1)+' of '+Math.max(1,Math.ceil(rows.length/size));el('previous').disabled=pageNo===0;el('next').disabled=(pageNo+1)*size>=rows.length;
const main=el('records');main.replaceChildren();if(!rows.length)main.append(node('p',(data.counts[selected]||0)?'No records match these filters.':sections[selected][2],'empty'));
for(const r of rows.slice(pageNo*size,(pageNo+1)*size)){const card=node('article');card.id=r.id;card.append(node('h2',r.title),node('p',[timestamp(r.timestamp),...r.agents,r.task].filter(Boolean).join(' · '),'muted'));
if(r.text){if(['library','submissions'].includes(selected)){const d=node('details');d.append(node('summary','Complete source'),node('pre',r.text));card.append(d);}else card.append(node('div',r.text,'text'));}
if(selected==='dms'&&r.detail){for(const m of r.detail.messages){const d=node('article');d.append(node('strong',m.sender+' → '+m.recipient),node('p',timestamp(m.sent_at),'muted'),node('div',m.body,'text'));card.append(d);}}
if(selected==='submissions'){card.append(node('p','Conference judge: '+(r.detail.reason||r.detail.outcome)));if(r.detail.compile_status)card.append(node('p','Compilation: '+r.detail.compile_status));const v=r.detail.independent_evaluation;card.append(node('p','Independent validity: '+v.validity,'badge'));if(v.reason)card.append(node('p',v.reason));}
const links=node('p');for(const [label,url] of r.links){if(!url)continue;const a=node('a',label);a.href=url;links.append(a,document.createTextNode(' · '));}card.append(links);
if(r.detail){const d=node('details');d.append(node('summary',selected==='submissions'?'Judge feedback & evidence':'Record details & provenance'),node('pre',JSON.stringify(r.detail,null,2)));card.append(d);}main.append(card);}}
for(const id of ['search','agent','task','kind'])el(id).addEventListener(id==='search'?'input':'change',()=>{pageNo=0;render();});el('previous').onclick=()=>{pageNo--;render();};el('next').onclick=()=>{pageNo++;render();};window.addEventListener('hashchange',()=>{if(sections[location.hash.slice(1)]&&selected!==location.hash.slice(1)){selected=location.hash.slice(1);pageNo=0;options();render();}});options();render();
'''
    (out / 'index.html').write_text(compact_browser_page(page('Conference · ' + run.name, body + '<script id="data" type="application/json">' + encoded + '</script><script>' + script + '</script>')))
    manifest = {'schema': 'gdm-conference-view/1', 'run': run.name, 'counts': counts, 'captured_files': len(versions),
                'conference_events': len(events), 'timeline_entries': len(timeline), 'warnings': warnings,
                'sources': {str(p.relative_to(run)): hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else 'absent'
                            for p in [run/'conference/state.sqlite', run/'conference/events.jsonl',run/'runtime.jsonl',run/'analysis/timeline.jsonl',run/'evaluations/evaluations.jsonl']}}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    print(json.dumps(export(parser.parse_args().run_dir)))
