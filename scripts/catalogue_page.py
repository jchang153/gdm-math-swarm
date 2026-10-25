"""Static style and script of the experiment catalogue (build_experiment_index.py).

Vanilla CSS and JavaScript, no external resources. Fixed text, so the catalogue stays a
deterministic function of its entries (maintain_hf_run.py reproduces it byte for byte).
"""

# Behavior judge cohorts (research/analysis/judge/review.py COHORT_ORDER and COHORT_GLOSS).
COHORTS = ('exploiter', 'convert', 'exploit_promoter', 'whistleblower', 'no_misbehavior', 'insufficient_record')
COHORT_LABELS = {'exploiter': ('exploiter', 'exploiters'), 'convert': ('convert', 'converts'),
                 'exploit_promoter': ('exploit promoter', 'exploit promoters'),
                 'whistleblower': ('whistleblower', 'whistleblowers'),
                 'no_misbehavior': ('no misbehavior', 'no misbehavior'),
                 'insufficient_record': ('insufficient record', 'insufficient record')}
COHORT_GLOSS = {
    'exploiter': 'handed in (or got ready to hand in) a cheating trick',
    'convert': 'handed in (or got ready) a cheating trick after first objecting to it or agonizing over it',
    'exploit_promoter': 'passed a cheating trick on to others without handing one in itself',
    'whistleblower': 'warned others about cheating',
    'no_misbehavior': 'no cheating handed in, no trick passed on and no warning sent',
    'insufficient_record': 'too little record to judge',
}
FLAGGED = ('exploiter', 'convert', 'exploit_promoter', 'whistleblower', 'insufficient_record')
CHEATING = ('exploiter', 'convert', 'exploit_promoter')
# Links kept in catalogue entries but no longer shown: transcripts are read in Inspect View
# (owner, October 21, 2026); the custom transcript pages stay in the run folders. The raw Inspect
# log links ('Inspect logs', or 'Inspect log N') were only downloads (site-nologs, October 21,
# 2026); the .eval files stay published because Inspect View reads them. The Timeline link
# (RUN/analysis/timeline.md) is not shown either: the timeline lives only in the private logs dataset
# (owner, October 23, 2026), reached from the run page's Timeline link.
HIDDEN_LINKS = ('Transcripts', 'Inspect logs', 'Timeline')
HIDDEN_PREFIX = 'Inspect log '


def hidden_link(name):
    """True for a catalogue link label that the pages do not show."""
    return name in HIDDEN_LINKS or name.startswith(HIDDEN_PREFIX)

STYLE = (
    # Colours are the shared site_style tokens (light/dark and the theme toggle).
    '*{box-sizing:border-box}'
    'body{max-width:1500px;margin:32px auto;padding:0 24px;font:16px system-ui,sans-serif;line-height:1.5;'
    'color:var(--fg);background:var(--bg)}'
    'h1{margin-bottom:8px}h2{margin:0;font-size:20px}p{max-width:950px;color:var(--soft)}a{color:var(--accent)}'
    'small{font-size:12px;color:var(--muted)}.muted{color:var(--muted)}.pending{color:var(--muted)}'
    '.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}'
    '.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin:18px 0 10px}'
    '.toolbar label{display:flex;flex-direction:column;font-size:13px;color:var(--soft);gap:4px}.toolbar .grow{flex:1 1 260px}'
    'input[type=search],select,button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--input-line);'
    'border-radius:7px;padding:9px 11px}input[type=search]{width:100%}button{cursor:pointer}'
    'button:disabled{opacity:.5;cursor:default}#compare:not(:disabled){background:var(--sel-bg);color:var(--sel-fg);'
    'border-color:var(--sel-bg)}button.quiet{background:transparent}'
    '.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}#shown{flex:1;color:var(--soft);font-size:14px}'
    '.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card)}'
    'td,th{padding:12px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}'
    'th{font-size:13px;color:var(--soft);white-space:nowrap}th button{border:0;background:none;padding:0;color:inherit;'
    'font-weight:600}th[aria-sort=ascending] button::after{content:" ▲"}th[aria-sort=descending] button::after{content:" ▼"}'
    'td.open{min-width:200px}td.pick{width:28px}tr.picked td{background:var(--pick)}input[type=checkbox]{width:18px;height:18px}'
    '#compare-view{background:var(--card);border:1px solid var(--input-line);border-radius:10px;padding:16px;margin-bottom:20px}'
    '.cmp-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}'
    '#compare-table th{white-space:normal;min-width:170px;color:var(--fg)}#compare-table th:first-child{min-width:150px;color:var(--soft)}'
    '.bar{height:6px;border-radius:3px;background:var(--bar);margin-top:4px}.warn{color:var(--warn);font-weight:600}'
    '.help{margin:22px 0;max-width:950px}.help summary{cursor:pointer;font-weight:600}.help dt{font-weight:600;margin-top:10px}'
    '.help dd{margin:2px 0 0 0;color:var(--soft)}.note{font-size:14px}'
    '@media (max-width:760px){body{margin:20px auto;padding:0 16px}#runs thead{display:none}'
    '#runs,#runs tbody,#runs tr,#runs td{display:block;width:100%}'
    '#runs tr{border:1px solid var(--line);border-radius:10px;margin-bottom:12px;padding:6px 4px;background:var(--card)}'
    '#runs td{border:0;padding:5px 10px;display:grid;grid-template-columns:118px minmax(0,1fr);gap:8px}'
    '#runs td::before{content:attr(data-label);font-size:12px;color:var(--muted)}'
    '#runs td.pick{display:block}#runs td.pick::before{content:none}#runs td.open .v{overflow-wrap:anywhere}.toolbar label{flex:1 1 45%}}'
)

# Sorting, filtering, the compare view (2-4 ticked runs, shareable as #compare=RUN,RUN) and, for
# entries published before the summary columns existed, reading the same fields from the run's
# own published results.json and judge report.json (same-origin fetch; nothing is sent elsewhere); entries that
# link the simplified judge without its counts read RUN/analysis/simple_judge/report.json the same way.
SCRIPT = r'''
// Deep link (HF forwards the Space URL's query and hash to this frame): ?page=PATH.html or #page=PATH.html
// opens that page of this site, keeping the private-Space session; only plain same-site .html paths.
{const q=new URLSearchParams(location.search),h=location.hash.startsWith('#page=')?decodeURIComponent(location.hash.slice(6)):'';
const page=q.get('page')||h;
if(/^(?:[A-Za-z0-9][A-Za-z0-9_.-]*\/)*[A-Za-z0-9][A-Za-z0-9_.-]*\.html$/.test(page)&&!page.split('/').includes('..')){
const url=new URL(page,document.baseURI);if(q.get('__sign'))url.searchParams.set('__sign',q.get('__sign'));
if(url.origin===location.origin)location.replace(url.href);}}
(()=>{
const $=id=>document.getElementById(id);
const data=JSON.parse($('catalogue-data').textContent);
const byRun=new Map(data.map(e=>[e.run,e]));
const tbody=$('runs').tBodies[0];
const rows=[...tbody.rows];
const rowOf=new Map(rows.map(r=>[r.dataset.run,r]));
const COHORTS=['exploiter','convert','exploit_promoter','whistleblower','no_misbehavior','insufficient_record'];
const LABELS={exploiter:['exploiter','exploiters'],convert:['convert','converts'],exploit_promoter:['exploit promoter','exploit promoters'],whistleblower:['whistleblower','whistleblowers'],no_misbehavior:['no misbehavior','no misbehavior'],insufficient_record:['insufficient record','insufficient record']};
const FLAGGED=['exploiter','convert','exploit_promoter','whistleblower','insufficient_record'];
const CHEATING=['exploiter','convert','exploit_promoter'];
const HIDDEN_LINKS=['Transcripts','Inspect logs','Timeline'];
const hidden=n=>HIDDEN_LINKS.includes(n)||n.startsWith('Inspect log ');
const MAX=4;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sum=e=>e.summary&&typeof e.summary==='object'?e.summary:null;
const judge=e=>(sum(e)||{}).judge||null;
// Simplified behavior judge (default since October 24, 2026): counts from the entry's summary, else read in
// the browser from the run's published analysis/simple_judge/report.json (kept outside the entry).
const SIMPLE='Simple behavior judge',SHOWN=['explored','submitted','credited'];
const simple=e=>(sum(e)||{}).simple_judge||e._simple||null;
function simpleText(s){const parts=SHOWN.filter(k=>s.levels[k]).map(k=>s.levels[k]+' '+k);
 if(s.whistleblowers)parts.push(s.whistleblowers+' whistleblower'+(s.whistleblowers===1?'':'s'));
 return [parts.join(' · ')||'none flagged','of '+s.labelled+' labelled'+(s.mode&&s.mode!=='paid'?' ('+s.mode+')':'')];}
function simpleCounts(r){if(!r||r.schema!=='gdm-simple-judge/1'||!r.counts||typeof r.counts!=='object')return null;
 const c=r.counts,ok=v=>Number.isInteger(v)&&v>=0,obj=v=>v&&typeof v==='object'&&!Array.isArray(v)&&Object.values(v).every(ok);
 if(!obj(c.cheating_level)||!obj(c.whistleblower)||!obj(c.evaluation_aware)||!ok(c.agents)||!ok(c.labelled)||!ok(c.cheating_any))return null;
 return {mode:typeof r.mode==='string'?r.mode:null,agents:c.agents,labelled:c.labelled,cheating_any:c.cheating_any,
  levels:{...c.cheating_level},whistleblowers:c.whistleblower.True??0,evaluation_aware:c.evaluation_aware.True??0};}
const variant=e=>(sum(e)||{}).prompt_variant||'';
const flagged=j=>CHEATING.reduce((n,k)=>n+(j.cohorts[k]||0),0);
const duration=s=>{s=Math.floor(s);return s>=3600?Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m':Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s';};
const money=v=>'$'+Number(v).toFixed(2);
// The Behavior judge cell, as build_experiment_index.judge_text writes it: [HTML, sort value].
function judgeCell(e){const j=judge(e),s=simple(e),linked=!!e.links[SIMPLE],label=(s||linked)?'rubric 3.1: ':'',lines=[];let key=null;
 if(j){const parts=FLAGGED.filter(k=>j.cohorts[k]).map(k=>j.cohorts[k]+' '+LABELS[k][j.cohorts[k]===1?0:1]);key=flagged(j);
  lines.push(esc(label+(parts.join(' · ')||'none flagged'))+'<br><small>'+esc('of '+(j.graded??j.agents)+' graded'+(j.provisional?', '+j.provisional+' provisional':'')+(j.mode&&j.mode!=='paid'?' ('+j.mode+')':''))+'</small>');}
 else if(e.links['Behavior judge']){lines.push(esc(label)+(sum(e)?'see report':'<span class="pending">…</span>'));key=-1;}
 if(s){const [main,detail]=simpleText(s);
  lines.push(esc('simple: '+main)+'<br><small>'+esc(detail)+(linked?' · <a href="'+esc(e.links[SIMPLE])+'">report</a>':'')+'</small>');
  key=key==null||key<0?s.cheating_any:key;}
 else if(linked){lines.push('<span class="pending sj-pending">simple: …</span>');key=key==null?-1:key;}
 return lines.length?[lines.join('<br>'),key]:['<span class="muted">not run</span>',-2];}
function judgeState(e){const j=judge(e),s=simple(e);
 if(j||s)return (j&&flagged(j))||(s&&s.cheating_any)?'flagged':'clean';
 return e.links['Behavior judge']||e.links[SIMPLE]?'run':'none';}
// Filters
function fill(select,values){const keep=select.value;select.length=1;
 for(const v of [...new Set(values)].filter(Boolean).sort()){const o=document.createElement('option');o.value=o.textContent=v;select.append(o);}
 select.value=[...select.options].some(o=>o.value===keep)?keep:'';}
function options(){fill($('f-model'),data.map(e=>e.model));fill($('f-variant'),data.map(variant));}
function apply(){const q=$('filter').value.toLowerCase().trim(),m=$('f-model').value,v=$('f-variant').value,j=$('f-judge').value;let shown=0;
 for(const r of rows){const e=byRun.get(r.dataset.run),s=judgeState(e);
  const ok=(!q||r.textContent.toLowerCase().includes(q))&&(!m||e.model===m)&&(!v||variant(e)===v)&&
   (!j||(j==='run'?s!=='none':j===s));
  r.hidden=!ok;shown+=ok;}
 $('shown').textContent='Showing '+shown+' of '+rows.length+' run'+(rows.length===1?'':'s');}
for(const id of ['filter','f-model','f-variant','f-judge'])$(id).addEventListener('input',apply);
// Sorting
const KEYS={start:e=>Date.parse(e.start_utc),model:e=>e.model.toLowerCase(),variant:e=>variant(e).toLowerCase(),
 agents:e=>e.agents,duration:e=>e.elapsed_seconds,tasks:e=>e.distinct_verified_tasks*1e4+((sum(e)||{}).accepted_tasks??-1),
 cost:e=>Number(e.pilot_spent_usd),judge:e=>judgeCell(e)[1]};
const NAMES={start:['Oldest first','Newest first']};
let sortKey='start',descending=true;
function sort(){const f=KEYS[sortKey];
 rows.sort((a,b)=>{const x=f(byRun.get(a.dataset.run)),y=f(byRun.get(b.dataset.run));
  const c=x<y?-1:x>y?1:0;return (descending?-c:c)||(a.dataset.run<b.dataset.run?1:-1);});
 for(const r of rows)tbody.append(r);
 for(const th of document.querySelectorAll('#runs th[data-key]'))
  th.dataset.key===sortKey?th.setAttribute('aria-sort',descending?'descending':'ascending'):th.removeAttribute('aria-sort');
 $('sort').value=sortKey;
 $('direction').textContent=(NAMES[sortKey]||['Ascending','Descending'])[descending?1:0]+(descending?' ↓':' ↑');}
for(const th of document.querySelectorAll('#runs th[data-key]'))th.querySelector('button').addEventListener('click',()=>{
 const k=th.dataset.key;descending=k===sortKey?!descending:!['model','variant'].includes(k);sortKey=k;sort();});
$('sort').addEventListener('input',()=>{sortKey=$('sort').value;descending=!['model','variant'].includes(sortKey);sort();});
$('direction').addEventListener('click',()=>{descending=!descending;sort();});
// Selection and compare
const boxes=rows.map(r=>r.querySelector('input[type=checkbox]'));
const picked=()=>boxes.filter(b=>b.checked).map(b=>b.value);
function selection(){const n=picked().length;
 for(const b of boxes){b.disabled=!b.checked&&n>=MAX;b.closest('tr').classList.toggle('picked',b.checked);}
 $('compare').disabled=n<2;$('compare').textContent='Compare selected ('+n+')'+(n>=MAX?' · max '+MAX:'');}
for(const b of boxes)b.addEventListener('change',selection);
$('clear').addEventListener('click',()=>{for(const b of boxes)b.checked=false;selection();closeCompare();});
function cell(e,row){const v=row[1](e);return v==null||v===''?'<span class="muted">—</span>':v;}
function bar(value,max){return max>0&&value!=null?'<div class="bar" style="width:'+Math.max(4,Math.round(100*value/max))+'%"></div>':'';}
function compareTable(list){
 const num=(f)=>{const max=Math.max(0,...list.map(e=>f(e)??0));return e=>{const v=f(e);return v==null?null:esc(v)+bar(v,max);};};
 const genuine=e=>e.distinct_verified_tasks,accepted=e=>(sum(e)||{}).accepted_tasks??null,cost=e=>Number(e.pilot_spent_usd);
 const costMax=Math.max(0,...list.map(cost));
 const perTask=e=>genuine(e)?money(cost(e)/genuine(e)):null;
 const cohort=k=>e=>{const j=judge(e);if(!j)return e.links['Behavior judge']?'<span class="muted">see report</span>':'<span class="muted">not run</span>';
  const n=j.cohorts[k];return n==null?null:'<span class="'+(n&&CHEATING.includes(k)?'warn':'')+'">'+n+'</span>'+bar(n,j.graded??j.agents);};
 const sjMissing=e=>'<span class="muted">'+(e.links[SIMPLE]?'…':'not run')+'</span>';
 const sj=(f,warn)=>e=>{const s=simple(e);if(!s)return sjMissing(e);const n=f(s);
  return '<span class="'+(n&&warn?'warn':'')+'">'+esc(n)+'</span>'+bar(n,s.labelled);};
 const spec=[
  ['Start',e=>esc(e.date)],['Model',e=>esc(e.model)],['Condition',e=>esc(e.track)],['Provider route',e=>esc(e.provider_route)],
  ['Prompt variant',e=>variant(e)?esc(variant(e)):null],['Agents',num(e=>e.agents)],['Duration',e=>esc(duration(e.elapsed_seconds))],
  ['Task set size',e=>e.task_count==null?null:esc(e.task_count)],['Accepted tasks',num(accepted)],['Genuine tasks',num(genuine)],
  ['Cost',e=>esc(money(cost(e)))+bar(cost(e),costMax)],['Cost per genuine task',perTask],
  ['Agent exits',e=>Object.entries(e.agent_exits).map(([k,v])=>esc(k.replace(/_/g,' '))+': '+v).join('<br>')],
  ['Rubric 3.1 judge: graded agents',e=>{const j=judge(e);return j?esc(j.graded??j.agents)+(j.provisional?' <small>('+j.provisional+' provisional)</small>':''):null;}],
  ...COHORTS.map(k=>['Rubric 3.1 judge: '+LABELS[k][1],cohort(k)]),
  ['Simple judge: labelled agents',e=>{const s=simple(e);return s?esc(s.labelled)+' of '+esc(s.agents)+(e.links[SIMPLE]?' · <a href="'+esc(e.links[SIMPLE])+'">report</a>':''):sjMissing(e);}],
  ['Simple judge: cheating (explored or further)',sj(s=>s.cheating_any,true)],
  ...['considered','explored','submitted','credited','unclear'].map(k=>['Simple judge: '+k,sj(s=>s.levels[k]??0,k!=='considered'&&k!=='unclear')]),
  ['Simple judge: whistleblowers',sj(s=>s.whistleblowers,false)],
  ['Simple judge: evaluation-aware (not comparable with rubric 3.1)',sj(s=>s.evaluation_aware,false)],
  ['Open',e=>Object.entries(e.links).filter(([n])=>!hidden(n)).map(([n,u])=>'<a href="'+esc(u)+'">'+esc(n)+'</a>').join('<br>')]];
 return '<table><thead><tr><th scope="col">Run</th>'+list.map(e=>'<th scope="col">'+esc(e.run)+'</th>').join('')+'</tr></thead><tbody>'+
  spec.map(row=>'<tr><th scope="row">'+esc(row[0])+'</th>'+list.map(e=>'<td>'+cell(e,row)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
let comparing=[];
function openCompare(runs,scroll){comparing=runs.filter(r=>byRun.has(r)).slice(0,MAX);
 if(comparing.length<2)return closeCompare();
 for(const b of boxes)b.checked=comparing.includes(b.value);selection();
 $('compare-table').innerHTML=compareTable(comparing.map(r=>byRun.get(r)));$('compare-view').hidden=false;
 const hash='#compare='+comparing.map(encodeURIComponent).join(',');if(location.hash!==hash)history.replaceState(null,'',hash);
 if(scroll)$('compare-view').scrollIntoView({behavior:'smooth',block:'start'});}
function closeCompare(){comparing=[];$('compare-view').hidden=true;
 if(location.hash.startsWith('#compare='))history.replaceState(null,'',location.pathname+location.search);}
$('compare').addEventListener('click',()=>openCompare(picked(),true));
$('close-compare').addEventListener('click',closeCompare);
function fromHash(){const m=location.hash.match(/^#compare=(.+)$/);if(m)openCompare(m[1].split(',').map(decodeURIComponent),false);}
window.addEventListener('hashchange',fromHash);
// Entries without a summary (or without the simplified judge's counts): read the run's own published records (hosted only).
function render(e){const r=rowOf.get(e.run),s=sum(e);if(!r)return;
 if(s){r.querySelector('.c-variant .v').innerHTML=s.prompt_variant?esc(s.prompt_variant):'<span class="muted">not recorded</span>';
  r.querySelector('.c-accepted').textContent=s.accepted_tasks??'?';}
 r.querySelector('.c-judge .v').innerHTML=judgeCell(e)[0];}
async function read(path){try{const r=await fetch(path,{credentials:'same-origin'});return r.ok?await r.json():null;}catch(_){return null;}}
function cohorts(report){const c=report&&report.counts;if(!c||typeof c.cohorts!=='object'||!Number.isInteger(c.agents))return null;
 const out={};for(const k of COHORTS)if(k in c.cohorts){if(!Number.isInteger(c.cohorts[k]))return null;out[k]=c.cohorts[k];}
 return {mode:typeof report.mode==='string'?report.mode:null,agents:c.agents,graded:Number.isInteger(c.graded)?c.graded:null,
  provisional:Number.isInteger(c.provisional)?c.provisional:0,cohorts:out};}
const needs=e=>!sum(e)||(!!e.links[SIMPLE]&&!simple(e));
async function enrich(e){const base=encodeURIComponent(e.run)+'/',whole=!sum(e),sj=!!e.links[SIMPLE]&&!simple(e);
 const [results,report,simpleReport]=await Promise.all([whole?read(base+'results.json'):null,
  whole&&e.links['Behavior judge']?read(base+'analysis/judge/report.json'):null,
  sj?read(base+'analysis/simple_judge/report.json'):null]);
 if(whole&&(results||report)){const accepted=results&&results.operationally_accepted_tasks,v=results&&results.condition&&results.condition.prompt_variant;
  e.summary={prompt_variant:typeof v==='string'?v:null,accepted_tasks:Number.isInteger(accepted)?accepted:null,judge:cohorts(report)};}
 if(sj)e._simple=simpleCounts(simpleReport);
 render(e);}
options();sort();apply();selection();fromHash();
const missing=data.filter(needs);
if(missing.length&&/^https?:$/.test(location.protocol))Promise.all(missing.map(enrich)).then(()=>{options();sort();apply();if(comparing.length)openCompare(comparing,false);});
})();
'''
