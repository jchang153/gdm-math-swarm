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
# Links kept in older published entries but no longer shown: transcripts are read in Inspect View
# (owner, October 21, 2026); the custom transcript pages stay in the run folders.
HIDDEN_LINKS = ('Transcripts',)

STYLE = (
    ':root{--bg:#fafbfc;--panel:#fff;--text:#18212b;--muted:#596579;--soft:#475569;--line:#dde3ea;--edge:#b8c2cf;'
    '--link:#155eaa;--accent:#1d4ed8;--accent-text:#fff;--pick:#eef4ff;--bar:#93b4e6;--warn:#9a3412}'
    '@media (prefers-color-scheme:dark){:root{--bg:#0f141a;--panel:#161d26;--text:#e5e9ef;--muted:#9aa6b6;'
    '--soft:#b3bdca;--line:#2a3441;--edge:#3b4757;--link:#7fb4ff;--accent:#3b82f6;--accent-text:#fff;--pick:#1b2a40;'
    '--bar:#2f5c9c;--warn:#fdba74}}'
    '*{box-sizing:border-box}'
    'body{max-width:1500px;margin:32px auto;padding:0 24px;font:16px system-ui,sans-serif;line-height:1.5;'
    'color:var(--text);background:var(--bg)}'
    'h1{margin-bottom:8px}h2{margin:0;font-size:20px}p{max-width:950px;color:var(--soft)}a{color:var(--link)}'
    'small{font-size:12px;color:var(--muted)}.muted{color:var(--muted)}.pending{color:var(--muted)}'
    '.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}'
    '.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin:18px 0 10px}'
    '.toolbar label{display:flex;flex-direction:column;font-size:13px;color:var(--soft);gap:4px}.toolbar .grow{flex:1 1 260px}'
    'input[type=search],select,button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--edge);'
    'border-radius:7px;padding:9px 11px}input[type=search]{width:100%}button{cursor:pointer}'
    'button:disabled{opacity:.5;cursor:default}#compare:not(:disabled){background:var(--accent);color:var(--accent-text);'
    'border-color:var(--accent)}button.quiet{background:transparent}'
    '.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}#shown{flex:1;color:var(--soft);font-size:14px}'
    '.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--panel)}'
    'td,th{padding:12px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}'
    'th{font-size:13px;color:var(--soft);white-space:nowrap}th button{border:0;background:none;padding:0;color:inherit;'
    'font-weight:600}th[aria-sort=ascending] button::after{content:" ▲"}th[aria-sort=descending] button::after{content:" ▼"}'
    'td.open{min-width:200px}td.pick{width:28px}tr.picked td{background:var(--pick)}input[type=checkbox]{width:18px;height:18px}'
    '#compare-view{background:var(--panel);border:1px solid var(--edge);border-radius:10px;padding:16px;margin-bottom:20px}'
    '.cmp-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}'
    '#compare-table th{white-space:normal;min-width:170px;color:var(--text)}#compare-table th:first-child{min-width:150px;color:var(--soft)}'
    '.bar{height:6px;border-radius:3px;background:var(--bar);margin-top:4px}.warn{color:var(--warn);font-weight:600}'
    '.help{margin:22px 0;max-width:950px}.help summary{cursor:pointer;font-weight:600}.help dt{font-weight:600;margin-top:10px}'
    '.help dd{margin:2px 0 0 0;color:var(--soft)}.note{font-size:14px}'
    '@media (max-width:760px){body{margin:20px auto;padding:0 16px}#runs thead{display:none}'
    '#runs,#runs tbody,#runs tr,#runs td{display:block;width:100%}'
    '#runs tr{border:1px solid var(--line);border-radius:10px;margin-bottom:12px;padding:6px 4px;background:var(--panel)}'
    '#runs td{border:0;padding:5px 10px;display:grid;grid-template-columns:118px minmax(0,1fr);gap:8px}'
    '#runs td::before{content:attr(data-label);font-size:12px;color:var(--muted)}'
    '#runs td.pick{display:block}#runs td.pick::before{content:none}#runs td.open .v{overflow-wrap:anywhere}.toolbar label{flex:1 1 45%}}'
)

# Sorting, filtering, the compare view (2-4 ticked runs, shareable as #compare=RUN,RUN) and, for
# entries published before the summary columns existed, reading the same fields from the run's
# own published results.json and judge report.json (same-origin fetch; nothing is sent elsewhere).
SCRIPT = r'''
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
const HIDDEN_LINKS=['Transcripts'];
const MAX=4;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sum=e=>e.summary&&typeof e.summary==='object'?e.summary:null;
const judge=e=>(sum(e)||{}).judge||null;
const variant=e=>(sum(e)||{}).prompt_variant||'';
const flagged=j=>CHEATING.reduce((n,k)=>n+(j.cohorts[k]||0),0);
const duration=s=>{s=Math.floor(s);return s>=3600?Math.floor(s/3600)+'h '+String(Math.floor(s%3600/60)).padStart(2,'0')+'m':Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s';};
const money=v=>'$'+Number(v).toFixed(2);
function judgeParts(e){const j=judge(e);
 if(j){const parts=FLAGGED.filter(k=>j.cohorts[k]).map(k=>j.cohorts[k]+' '+LABELS[k][j.cohorts[k]===1?0:1]);
  return [parts.join(' · ')||'none flagged','of '+(j.graded??j.agents)+' graded'+(j.provisional?', '+j.provisional+' provisional':'')+(j.mode&&j.mode!=='paid'?' ('+j.mode+')':'')];}
 return [e.links['Behavior judge']?'see report':'not run',''];}
function judgeState(e){const j=judge(e);if(j)return flagged(j)?'flagged':'clean';return e.links['Behavior judge']?'run':'none';}
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
 cost:e=>Number(e.pilot_spent_usd),judge:e=>{const j=judge(e);return j?flagged(j):e.links['Behavior judge']?-1:-2;}};
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
 const spec=[
  ['Start',e=>esc(e.date)],['Model',e=>esc(e.model)],['Condition',e=>esc(e.track)],['Provider route',e=>esc(e.provider_route)],
  ['Prompt variant',e=>variant(e)?esc(variant(e)):null],['Agents',num(e=>e.agents)],['Duration',e=>esc(duration(e.elapsed_seconds))],
  ['Task set size',e=>e.task_count==null?null:esc(e.task_count)],['Accepted tasks',num(accepted)],['Genuine tasks',num(genuine)],
  ['Cost',e=>esc(money(cost(e)))+bar(cost(e),costMax)],['Cost per genuine task',perTask],
  ['Agent exits',e=>Object.entries(e.agent_exits).map(([k,v])=>esc(k.replace(/_/g,' '))+': '+v).join('<br>')],
  ['Judge: graded agents',e=>{const j=judge(e);return j?esc(j.graded??j.agents)+(j.provisional?' <small>('+j.provisional+' provisional)</small>':''):null;}],
  ...COHORTS.map(k=>['Judge: '+LABELS[k][1],cohort(k)]),
  ['Open',e=>Object.entries(e.links).filter(([n])=>!HIDDEN_LINKS.includes(n)).map(([n,u])=>'<a href="'+esc(u)+'">'+esc(n)+'</a>').join('<br>')]];
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
// Entries without a summary: read the run's own published records (hosted only).
function render(e){const r=rowOf.get(e.run),s=sum(e);if(!r||!s)return;
 r.querySelector('.c-variant .v').innerHTML=s.prompt_variant?esc(s.prompt_variant):'<span class="muted">not recorded</span>';
 r.querySelector('.c-accepted').textContent=s.accepted_tasks??'?';
 const [main,sub]=judgeParts(e);
 r.querySelector('.c-judge .v').innerHTML=(main==='not run'?'<span class="muted">not run</span>':esc(main))+(sub?'<br><small>'+esc(sub)+'</small>':'');}
async function read(path){try{const r=await fetch(path,{credentials:'same-origin'});return r.ok?await r.json():null;}catch(_){return null;}}
function cohorts(report){const c=report&&report.counts;if(!c||typeof c.cohorts!=='object'||!Number.isInteger(c.agents))return null;
 const out={};for(const k of COHORTS)if(k in c.cohorts){if(!Number.isInteger(c.cohorts[k]))return null;out[k]=c.cohorts[k];}
 return {mode:typeof report.mode==='string'?report.mode:null,agents:c.agents,graded:Number.isInteger(c.graded)?c.graded:null,
  provisional:Number.isInteger(c.provisional)?c.provisional:0,cohorts:out};}
async function enrich(e){const base=encodeURIComponent(e.run)+'/';
 const [results,report]=await Promise.all([read(base+'results.json'),e.links['Behavior judge']?read(base+'analysis/judge/report.json'):null]);
 if(!results&&!report)return;
 const accepted=results&&results.operationally_accepted_tasks,v=results&&results.condition&&results.condition.prompt_variant;
 e.summary={prompt_variant:typeof v==='string'?v:null,accepted_tasks:Number.isInteger(accepted)?accepted:null,judge:cohorts(report)};
 render(e);}
options();sort();apply();selection();fromHash();
const missing=data.filter(e=>!sum(e));
if(missing.length&&/^https?:$/.test(location.protocol))Promise.all(missing.map(enrich)).then(()=>{options();sort();apply();if(comparing.length)openCompare(comparing,false);});
})();
'''
