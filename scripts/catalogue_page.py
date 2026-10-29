"""Static style and script of the experiment catalogue (build_experiment_index.py).

Vanilla CSS and JavaScript, no external resources. Fixed text, so the catalogue stays a
deterministic function of its entries (maintain_hf_run.py reproduces it byte for byte).
"""
from site_layout import COLUMN  # the site's one centred content column

WIDE = '1360px'  # the run-card grid only

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
    # Redesign (owner, October 29, 2026: "clanky and old looking, text too dense"). Colours are the shared
    # site_style tokens (light/dark and the theme toggle); one accent; hairlines instead of boxes.
    '*{box-sizing:border-box}'
    # The card grid may use a wider column (WIDE; owner, October 29, 2026: "5 cards per row"); everything else stays in
    # the site's text column.
    'body{max-width:'+WIDE+';margin:0 auto 64px;padding:0 32px;font:16px/1.55 system-ui,-apple-system,"Segoe UI",'
    'Roboto,sans-serif;color:var(--fg);background:var(--bg);-webkit-font-smoothing:antialiased}'
    'h1,h2,h3{letter-spacing:-.015em;color:var(--fg)}h2{margin:0;font-size:22px;line-height:1.25;font-weight:650}'
    'p{max-width:70ch;color:var(--soft)}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}'
    'code{font-size:.9em;background:var(--chip);padding:1px 5px;border-radius:4px}'
    'small{font-size:12px;color:var(--muted)}.muted{color:var(--muted)}.pending{color:var(--muted)}'
    '.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}'
    # Top navigation and hero
    '.topnav{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:18px 0;'
    'border-bottom:1px solid var(--line);font-size:14px}.topnav .brand{font-weight:650;color:var(--fg)}'
    '.tn-links{display:flex;gap:22px}.tn-links a{color:var(--soft)}.tn-links a:hover{color:var(--accent);text-decoration:none}'
    '.hero{padding:56px 0 40px;border-bottom:1px solid var(--line)}'
    '.eyebrow{margin:0 0 12px;font-size:13px;font-weight:600;color:var(--accent)}'
    '.hero h1{font-size:clamp(2rem,4.2vw,3rem);line-height:1.1;margin:0 0 18px;font-weight:700;letter-spacing:-.025em;'
    'max-width:22ch}.hero .lead{font-size:clamp(1.05rem,1.6vw,1.2rem);line-height:1.6;color:var(--soft);max-width:62ch;margin:0}'
    '.stats{display:flex;flex-wrap:wrap;gap:16px 48px;margin:32px 0 28px;padding:0}.stats div{display:flex;flex-direction:column-reverse}'
    '.stats dd{margin:0;font-size:30px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums;color:var(--fg);line-height:1.1}'
    '.stats dt{font-size:13px;color:var(--muted);margin-top:4px}'
    '.paper{font-size:13.5px;line-height:1.55;margin:0;max-width:85ch;color:var(--soft)}'
    '.paper-k{font-weight:600;color:var(--fg)}.paper .muted{display:block;margin-top:2px}'
    # Sections
    '.block{margin:56px 0 0}.sub{margin:6px 0 0;font-size:15px}'
    '.runs-head{margin-bottom:20px}'
    # Series table of contents
    '.toc{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:0 32px;margin:0 0 28px;'
    'border-top:1px solid var(--line)}.toc a{display:flex;justify-content:space-between;gap:12px;padding:9px 0;'
    'border-bottom:1px solid var(--line);color:var(--fg);font-size:14.5px}.toc a:hover{color:var(--accent);text-decoration:none}'
    '.toc .n{color:var(--muted);font-variant-numeric:tabular-nums}'
    # Controls
    '.controls{padding:0 0 10px;border-bottom:1px solid var(--line)}'
    '.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.toolbar .grow{flex:1 1 220px;display:flex}'
    '.toolbar label{display:flex}'
    'input[type=search],select,button{font:inherit;font-size:14px;color:var(--fg);background:var(--card);'
    'border:1px solid var(--line);border-radius:8px;padding:7px 10px;min-height:36px}input[type=search]{width:100%}'
    'select{max-width:190px}button{cursor:pointer}button:hover:not(:disabled){border-color:var(--input-line)}'
    'button:disabled{opacity:.45;cursor:default}#compare:not(:disabled){background:var(--accent);color:var(--card);'
    'border-color:var(--accent)}button.quiet{background:transparent;border-color:transparent;color:var(--soft)}'
    '.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--card)}'
    '.seg button{border:0;border-radius:0;min-height:34px;padding:6px 14px;background:transparent;color:var(--soft)}'
    '.seg button[aria-pressed=true]{background:var(--chip);color:var(--fg);font-weight:600}'
    '.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}'
    '#shown{flex:1;color:var(--muted);font-size:13px}.actions button{font-size:13px;min-height:30px;padding:4px 10px}'
    # Series and run cards
    '.series{margin:44px 0 0}.series-head h2{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}'
    '.series-head .count{font-size:14px;font-weight:400;color:var(--muted);letter-spacing:0}'
    '.series-head p{margin:8px 0 0;font-size:15px;line-height:1.55}'
    '.legend{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12.5px;color:var(--muted)!important;margin:12px 0 16px!important;max-width:none!important}'
    '.legend i{display:inline-block;width:16px;height:9px;margin-right:6px;vertical-align:0}'
    '.lg-area{background:color-mix(in srgb,var(--bad) 28%,transparent);border-top:2px solid var(--bad)}'
    '.lg-cum{border-top:2px dashed var(--muted);height:0!important;vertical-align:3px!important}'
    '.lg-seed{border-left:2px dotted var(--accent);width:0!important;height:11px!important;margin-left:4px}'
    '.topnav,.hero,#live,.runs,.series-head,#table-view,.about,.foot{max-width:'+COLUMN+';margin-left:auto;margin-right:auto}'
    # 5 cards per row on a 1440px screen, then 4/3/2/1 as the window narrows.
    '.rc-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:14px}'
    '@media (max-width:1300px){.rc-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}'
    '@media (max-width:1060px){.rc-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}'
    '@media (max-width:820px){.rc-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}'
    '.rc{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);border-radius:12px;'
    'padding:14px 14px 10px;min-width:0;transition:border-color .15s}.rc:hover{border-color:var(--input-line)}'
    '.rc.picked{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}'
    '.rc-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}'
    '.rc-title{font-size:16px;line-height:1.3;margin:0;font-weight:650}'
    '.rc-pick{display:inline-flex;align-items:center;gap:5px;color:var(--muted);font-size:12px;cursor:pointer;flex:none}'
    '.rc-pick input{width:15px;height:15px;margin:0;accent-color:var(--accent)}'
    '.rc-meta{margin:3px 0 10px;font-size:12.5px;color:var(--muted)}'
    '.rc-body{flex:1}.rc-spark{margin:0 0 8px}.spark{display:block;width:100%;height:44px;overflow:visible}'
    '.sp-area{fill:color-mix(in srgb,var(--bad) 22%,transparent);stroke:var(--bad);stroke-width:1.5;'
    'vector-effect:non-scaling-stroke}.sp-cum{fill:none;stroke:var(--muted);stroke-width:1.2;stroke-dasharray:4 3;'
    'vector-effect:non-scaling-stroke}.sp-base{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke}'
    '.sp-seed{stroke:var(--accent);stroke-width:1.5;stroke-dasharray:2 2;vector-effect:non-scaling-stroke}'
    '.rc-spark figcaption{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:3px}'
    '.rc-fakes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0}'
    '.rc-fakes div,.rc-stats div{min-width:0;display:flex;flex-direction:column}'
    '.rc-fakes dt{order:2;font-size:11px;color:var(--muted);line-height:1.3}'
    '.rc-fakes dd{order:1;margin:0;font-weight:650;font-size:20px;line-height:1.2;font-variant-numeric:tabular-nums}'
    '.rc-fakes dd.bad{color:var(--bad)}.rc-fakes dd.sub{order:3;font-size:11.5px;font-weight:400;color:var(--muted)}'
    '.rc-empty{font-size:13px;line-height:1.45;color:var(--muted);margin:4px 0 0!important}'
    '.rc-links{display:flex;flex-wrap:wrap;gap:2px 14px;margin:12px 0 0;padding-top:10px;border-top:1px solid var(--line);'
    'font-size:13px;max-width:none}.rc-links a{white-space:nowrap}'
    '.rc-more{margin-top:6px;font-size:12.5px;color:var(--soft)}.rc-more summary{cursor:pointer;color:var(--muted);'
    'list-style:none;padding:2px 0;width:max-content}.rc-more summary::-webkit-details-marker{display:none}'
    '.rc-more summary::after{content:" +"}.rc-more[open] summary::after{content:" −"}'
    '.rc-more p{margin:6px 0;font-size:12.5px;line-height:1.45}.rc-cond{color:var(--fg)!important}'
    '.rc-note{color:var(--muted)!important}'
    '.rc-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 10px;margin:8px 0}'
    '.rc-stats dt{font-size:11px;color:var(--muted)}.rc-stats dd{margin:0;font-weight:600;font-size:13.5px;'
    'font-variant-numeric:tabular-nums;color:var(--fg)}.rc-stats dd small{font-weight:400}'
    '.rc-when{color:var(--muted)!important;overflow-wrap:anywhere}.rc-when code{font-size:11px}'
    # Table view and compare
    '.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card);font-size:14px}'
    'td,th{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}'
    'th{font-size:12.5px;color:var(--muted);white-space:nowrap;font-weight:600}th button{border:0;background:none;padding:0;'
    'min-height:0;color:inherit;font-weight:600;font-size:12.5px}th[aria-sort=ascending] button::after{content:" ▲"}'
    'th[aria-sort=descending] button::after{content:" ▼"}'
    'td.open{min-width:200px}td.pick{width:28px}tr.picked td{background:var(--pick)}input[type=checkbox]{width:16px;height:16px}'
    '#table-view>h2{margin-bottom:14px}'
    '#compare-view{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0 8px}'
    '.cmp-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}'
    '#compare-table th{white-space:normal;min-width:170px;color:var(--fg)}#compare-table th:first-child{min-width:150px;color:var(--muted)}'
    '.bar{height:5px;border-radius:3px;background:var(--bar);margin-top:4px}.warn{color:var(--warn);font-weight:600}'
    '.help{margin:18px 0;max-width:80ch;font-size:14px}.help summary{cursor:pointer;color:var(--soft)}.help dt{font-weight:600;margin-top:10px}'
    '.help dd{margin:2px 0 0 0;color:var(--soft)}'
    # About
    '.about{margin:80px 0 0;padding-top:40px;border-top:1px solid var(--line)}.about>.sub{margin-bottom:20px}'
    '.about details{border-top:1px solid var(--line)}.about details:last-of-type{border-bottom:1px solid var(--line)}'
    '.about summary{cursor:pointer;list-style:none;display:flex;justify-content:space-between;align-items:center;'
    'padding:16px 0;font-size:17px;font-weight:600;color:var(--fg)}.about summary::-webkit-details-marker{display:none}'
    '.about summary::after{content:"+";font-weight:400;font-size:22px;color:var(--muted);line-height:1}'
    '.about details[open]>summary::after{content:"−"}.about details>:last-child{margin-bottom:24px}'
    '.facts{margin:0;padding:0;list-style:none;display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));'
    'gap:14px 32px}.facts li{font-size:14.5px;line-height:1.55;color:var(--soft)}.facts strong{color:var(--fg);font-weight:600}'
    '.steps{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(min(200px,100%),1fr));'
    'gap:20px 28px;counter-reset:step}.steps li{counter-increment:step;font-size:14px;line-height:1.5;color:var(--soft)}'
    '.steps li strong{display:block;color:var(--fg);font-size:15px;margin-bottom:4px}.steps li strong::before{'
    'content:counter(step);display:block;font-size:12px;font-weight:600;color:var(--accent);margin-bottom:4px}'
    '.defs{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:16px 32px;margin:8px 0 0}'
    '.defs dt{font-weight:600;font-size:14.5px}.defs dd{margin:3px 0 0;color:var(--soft);font-size:14px;line-height:1.5}'
    '.foot{margin:56px 0 0;padding-top:20px;border-top:1px solid var(--line)}.foot p{font-size:13px;color:var(--muted);margin:0}'
    # Live runs (hosted)
    '#live{margin:40px 0 0}#live ul{padding-left:18px;font-size:14px}'
    # Narrow screens: table rows become stacked cards; 16px gutters come from site_style below 640px.
    '@media (max-width:760px){body{padding:0 20px}.hero{padding:36px 0 28px}.stats{gap:14px 28px;margin:24px 0 20px}'
    '.stats dd{font-size:24px}.tn-links{gap:14px}.block{margin-top:40px}'
    '.toolbar .sel{flex:1 1 45%}.toolbar select{max-width:none;width:100%}'
    '#runs thead{display:none}#runs,#runs tbody,#runs tr,#runs td{display:block;width:100%}'
    '#runs tr{border:1px solid var(--line);border-radius:10px;margin-bottom:12px;padding:6px 4px;background:var(--card)}'
    '#runs td{border:0;padding:5px 10px;display:grid;grid-template-columns:118px minmax(0,1fr);gap:8px}'
    '#runs td::before{content:attr(data-label);font-size:12px;color:var(--muted)}'
    '#runs td.pick{display:block}#runs td.pick::before{content:none}#runs td.open .v{overflow-wrap:anywhere}'
    '}@media (max-width:560px){.rc-grid{grid-template-columns:minmax(0,1fr)}}'
    '@media (max-width:420px){.topnav .brand{font-size:13px}.tn-links{gap:12px;font-size:13px}}'
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
// Run cards grouped by series (owner request, October 27, 2026; scripts/run_profile.py). RP is the JavaScript
// twin of run_profile.py, used for entries published before profiles existed.
/*CARDJS*/
const cards=[...document.querySelectorAll('article.rc[data-run]')];
const cardOf=new Map(cards.map(c=>[c.dataset.run,c]));
const cardBoxes=cards.map(c=>c.querySelector('.card-pick')).filter(Boolean);
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
  r.hidden=!ok;shown+=ok;const c=cardOf.get(r.dataset.run);if(c)c.hidden=!ok;}
 for(const s of document.querySelectorAll('section.series'))s.hidden=![...s.querySelectorAll('article.rc')].some(c=>!c.hidden);
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
const boxOf=new Map(boxes.map(b=>[b.value,b]));
const picked=()=>boxes.filter(b=>b.checked).map(b=>b.value);
function selection(){const n=picked().length;
 for(const b of boxes){b.disabled=!b.checked&&n>=MAX;b.closest('tr').classList.toggle('picked',b.checked);}
 for(const cb of cardBoxes){const b=boxOf.get(cb.value);if(!b)continue;cb.checked=b.checked;cb.disabled=b.disabled;cb.closest('article').classList.toggle('picked',b.checked);}
 $('compare').disabled=n<2;$('compare').textContent='Compare selected ('+n+')'+(n>=MAX?' · max '+MAX:'');}
for(const b of boxes)b.addEventListener('change',selection);
for(const cb of cardBoxes)cb.addEventListener('change',()=>{const b=boxOf.get(cb.value);if(b){b.checked=cb.checked;selection();}});
// Cards or table: a per-browser convenience kept in localStorage (a blocked store just means the default, cards).
const VIEW='gdm-catalogue-view';
function view(v,keep){const table=v==='table';if(!$('cards-view')||!$('table-view'))return;$('cards-view').hidden=table;$('table-view').hidden=!table;
 $('view-cards').setAttribute('aria-pressed',String(!table));$('view-table').setAttribute('aria-pressed',String(table));
 if(!keep)try{localStorage.setItem(VIEW,table?'table':'cards');}catch(_){}}
if($('view-cards')){$('view-cards').addEventListener('click',()=>view('cards'));$('view-table').addEventListener('click',()=>view('table'));
 let saved=null;try{saved=localStorage.getItem(VIEW);}catch(_){}view(saved==='table'?'table':'cards',true);
 for(const a of document.querySelectorAll('.jump a'))a.addEventListener('click',()=>{const h=a.getAttribute('href');
  if(h==='#table-view')view('table');else if(h.startsWith('#series-'))view('cards');});}
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
 r.querySelector('.c-judge .v').innerHTML=judgeCell(e)[0];
 const card=cardOf.get(e.run);if(card&&s)card.querySelector('.rc-accepted').textContent=s.accepted_tasks??'?';}
const fetched=new Map();
function read(path){if(!fetched.has(path))fetched.set(path,(async()=>{try{const r=await fetch(path,{credentials:'same-origin'});return r.ok?await r.json():null;}catch(_){return null;}})());
 return fetched.get(path);}
// Run cards of entries without a profile: computed from the run's published results.json (and settings.json for the
// overseer model) with the same rules as run_profile.py; then every card of the series is drawn on the shared scale.
const prof=e=>{const p=e.profile;return p&&typeof p==='object'&&p.schema===RP.SCHEMA?p:(e._profile||null);};
async function enrichProfile(e){const base=encodeURIComponent(e.run)+'/',results=await read(base+'results.json');
 if(!results||typeof results!=='object'||!results.start_utc){e._profileFailed=true;return;}
 const settings=results.condition&&results.condition.overseer?await read(base+'settings.json'):null;
 try{e._profile=RP.profile(results,settings);}catch(_){e._profileFailed=true;}}
function drawSeries(){for(const s of document.querySelectorAll('section.series')){
 const members=[...s.querySelectorAll('article.rc')].map(c=>byRun.get(c.dataset.run)).filter(Boolean),scale=RP.scaleOf(members.map(prof));
 const label=s.querySelector('.sc-fakes');if(label)label.textContent=Math.max(scale.fakes,1);
 for(const e of members){const body=cardOf.get(e.run).querySelector('.rc-body'),p=prof(e);
  if(p){body.innerHTML=RP.inner(p,scale);body.dataset.profile='1';const more=cardOf.get(e.run).querySelector('.rc-prof');if(more)more.innerHTML=RP.more(p);}
  else if(e._profileFailed){const note=body.querySelector('.rc-empty');if(note)note.textContent='This run\'s records could not be read here.';}}}}
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
// A link to a section inside a collapsed <details> (About, glossary; run pages link #glossary) opens it.
function reveal(){const id=decodeURIComponent(location.hash.slice(1));if(!id||id.includes('='))return;
 const t=document.getElementById(id);const d=t&&(t.tagName==='DETAILS'?t:t.closest('details'));if(d)d.open=true;}
window.addEventListener('hashchange',reveal);reveal();
options();sort();apply();selection();fromHash();
const missing=data.filter(needs);
if(missing.length&&/^https?:$/.test(location.protocol))Promise.all(missing.map(enrich)).then(()=>{options();sort();apply();if(comparing.length)openCompare(comparing,false);});
const unprofiled=data.filter(e=>!prof(e)&&cardOf.has(e.run));
if(unprofiled.length&&/^https?:$/.test(location.protocol))Promise.all(unprofiled.map(enrichProfile)).then(drawSeries);
})();
'''
