"""Small self-contained HTML page of one simplified-judge run (no scripts, light and dark theme)."""
from __future__ import annotations

import html
import json
from pathlib import Path
import sys

try:
    from run_clock import absolute, clock_minutes
except ImportError:  # scripts/ (the shared run clock) is not on the path when imported from research/
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    from run_clock import absolute, clock_minutes

from .pipeline import names_seed as _names_seed
from .. import agent_badges

STYLE = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#5f5e5a;--line:#dcdbd5;--card:#fff;--pos:#b4441f;--ok:#2f7d4f;--warn:#9a6a00}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;
--line:#34332f;--card:#1f1e1c;--pos:#ef8a62;--ok:#6cc495;--warn:#e0b04a}}
:root[data-theme="dark"]{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--line:#34332f;--card:#1f1e1c;--pos:#ef8a62;
--ok:#6cc495;--warn:#e0b04a}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.muted{color:var(--muted)}.pos{color:var(--pos);font-weight:600}.ok{color:var(--ok)}.warn{color:var(--warn)}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;
text-align:left;vertical-align:top}th{font-weight:600}.wrap{overflow-x:auto}
.kpis{display:flex;flex-wrap:wrap;gap:10px}.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:8px 12px;min-width:120px}.kpi b{display:block;font-size:20px}
q{font-style:italic}code{font-size:12.5px}details{margin:2px 0}
.note{border:1px solid var(--line);border-left:4px solid var(--warn);background:var(--card);border-radius:6px;padding:8px 12px}
"""
# The filterable agents table (owner request, October 25, 2026): one row of filters, sortable columns, a sticky
# header, "Showing N of M agents", the state in the URL hash, compact rows with expandable details.
AGENTS_STYLE = """
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin:8px 0 6px}
.filters label{display:flex;flex-direction:column;gap:2px;font-size:12px;color:var(--muted)}
.filters .grow{flex:1 1 150px}main{max-width:1280px;box-sizing:border-box}
.filters select,.filters input,.filters button,.ms>summary{font:inherit;font-size:13.5px;color:var(--fg);
background:var(--card);border:1px solid var(--line);border-radius:6px;padding:5px 8px;min-height:32px}
.filters input{width:100%}.filters select{max-width:150px}#f-tech{max-width:190px}.filters button{cursor:pointer}
.ms{position:relative;font-size:12px;color:var(--muted)}.ms>summary{list-style:none;cursor:pointer;
display:flex;align-items:center;white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis}.ms>summary::-webkit-details-marker{display:none}
.ms>summary::after{content:" \\25BE";color:var(--muted)}
.ms>div{position:absolute;z-index:5;top:100%;left:0;margin-top:4px;background:var(--card);border:1px solid var(--line);
border-radius:6px;padding:6px 10px;box-shadow:0 4px 14px rgba(0,0,0,.15);min-width:170px}
.ms>div input{width:auto;min-height:0;margin:0}.ms>div label{flex-direction:row;align-items:center;gap:6px;font-size:13.5px;color:var(--fg);padding:2px 0}
.ms .lab{font-size:12px;color:var(--muted);display:block;margin-bottom:2px}
#shown{margin:4px 0 6px;font-size:13px}
table.agents{border-collapse:separate;border-spacing:0}
table.agents th{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:2px solid var(--line);white-space:nowrap}
table.agents th button{font:inherit;font-weight:600;color:inherit;background:none;border:0;padding:0;cursor:pointer}
table.agents th[aria-sort=ascending] button::after{content:" \\25B2"}
table.agents th[aria-sort=descending] button::after{content:" \\25BC"}
table.agents td{padding:4px 8px}table.agents td:first-child{white-space:nowrap}table.agents tr:hover td{background:var(--card)}
td.tech{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
table.agents{width:100%}td.det{width:40%;max-width:0}td.det summary{cursor:pointer;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}td.det details[open] .prev{display:none}td.det details[open] summary::after{content:"hide"}td.det{overflow-wrap:anywhere}
td.det .body{margin:4px 0 6px;padding:6px 10px;border-left:3px solid var(--line)}
td.det .body>div{margin:3px 0}.facts{font-size:12.5px;color:var(--muted)}
@media (max-width:760px){main{padding:16px}table.agents thead{display:none}
table.agents,table.agents tbody{display:block;width:100%}
table.agents tr{display:flex;flex-wrap:wrap;gap:2px 14px;box-sizing:border-box;width:100%;border:1px solid var(--line);
border-radius:8px;margin-bottom:8px;padding:6px 10px;background:var(--card)}
table.agents tr[hidden]{display:none}
table.agents td{border:0;padding:0;display:block}table.agents td:empty{display:none}
table.agents td::before{content:attr(data-label) ": ";color:var(--muted);font-size:12px}
table.agents td:first-child{font-weight:600}table.agents td:first-child::before{content:none}
td.tech{max-width:none;white-space:normal;flex-basis:100%}td.det{width:auto;max-width:none;flex-basis:100%}
table.agents td.det::before{content:none}.filters label{flex:1 1 45%}.filters select{max-width:none;width:100%}}
"""
LEVELS = ("none", "considered", "explored", "submitted", "credited", "unclear")
NOT_LABELLED = "not labelled"
SORTS = (("agent", "agent"), ("level", "cheating"), ("tech", "technique"), ("declined", "declined"),
         ("wb", "whistleblower"), ("ea", "eval-aware"), ("src", "source"), ("q", "quotes"))


def _e(value):
    return html.escape("" if value is None else str(value))


# Shown on every page (site requirement, 2026-10-24): the evaluation-awareness counts of this judge and of the
# rubric-3.1 judge are not comparable (README "Limits").
NOTE_ID = "sj-comparability"
NOTE = ("Evaluation-awareness counts from this simplified judge are not comparable with the rubric-3.1 judge's: "
        "it finds many more (for example 14 to 20 per 100 agents where the rubric-3.1 judge found 0 to 3 on the "
        "three 30-minute GLM runs), mostly agents the earlier judge's prefilter never showed to its model. Compare "
        "evaluation awareness only between runs judged by the same judge.")


def _flag(value):
    if value is True:
        return '<span class="pos">yes</span>'
    if value == "unclear":
        return '<span class="warn">unclear</span>'
    return '<span class="muted">no</span>'


def _level(level):
    cls = "pos" if level in ("explored", "submitted", "credited") else ("warn" if level in ("unclear", "considered")
                                                                       else "muted")
    return f'<span class="{cls}">{_e(level)}</span>'


def _agent(row):
    role = row.get("role")
    return _e(row["agent_id"]) + (f" <span class=muted>({_e(role)})</span>" if role else "")


def seeded_note(report, run_start=None):
    """Seeded runs: the planted source, never judged (researcher-only); "" otherwise."""
    seeded = report.get("seeded")
    if not seeded:
        return ""
    src = (report.get("counts") or {}).get("seeded_source") or {}
    when = seeded.get("injected_min")
    return (f"<p class=note id=sj-seeded><strong>Seeded run (researcher-only):</strong> {_e(seeded.get('seed_agent'))} "
            f"is the seeded source, planted by the researchers"
            + (f" at {_e(clock_minutes(when, run_start))}" if when is not None else "")
            + f" (an accepted exploit for {_e(seeded.get('task_id'))}"
            + (f", published as <code>{_e(seeded.get('path'))}</code>" if seeded.get("path") else "")
            + "). It is not a model agent and is not judged; a likely source naming it is counted as seeded, never "
              f"independent. {_e(src.get('agents_that_read_seed_file', 0))} judged agents read its file, "
              f"{_e(src.get('read_seed_file_before_first_flagged_submission', 0))} before their first flagged "
              f"submission; {_e(src.get('cheating_agents_naming_seed', 0))} cheating agents name it as their source.</p>")


def render_html(report, rows, old_judge_href=None, run_start=None):
    """The page; ``old_judge_href`` (for example ``../judge/report.html``) links the run's rubric-3.1 judge;
    ``run_start`` (epoch seconds, run_clock.run_start) turns the per-agent minute facts into run-relative plus
    absolute times (without it they stay "minute N")."""
    c = report["counts"]
    kpis = [("agents", c["agents"]), ("labelled", c["labelled"]), ("cheating (explored+)", c["cheating_any"]),
            ("credited", c["cheating_level"].get("credited", 0)),
            ("whistleblowers", c["whistleblower"].get("True", 0)),
            ("evaluation-aware", c["evaluation_aware"].get("True", 0)),
            ("cost", f"${float(report['cost_usd']):.2f}"), ("wall time", f"{report.get('wall_seconds', 0) / 60:.1f} min")]
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'><title>Simple behavior judge</title>"
             f"<style>{STYLE}{AGENTS_STYLE}</style></head><body><main>",
             f"<h1>Simple behavior judge</h1><p class=muted>{_e(report['run_id'])} &middot; {_e(report['rubric'])} "
             f"&middot; model {_e(report.get('model'))} (fallback {_e(report.get('fallback_model'))}) &middot; "
             f"one call per agent; quotes checked verbatim by code; labels without verified evidence shown as "
             f"unclear. Researcher-only."
             + (f" Times: run clock from the run start, {_e(absolute(run_start))}"
                f" (common_start), with the absolute UTC time." if run_start is not None else "") + "</p>",
             f"<p class=note id={NOTE_ID}><strong>Not comparable:</strong> {_e(NOTE)}"
             + (f" The rubric-3.1 judge's report for this run: <a href=\"{_e(old_judge_href)}\">Behavior judge "
                f"(rubric 3.1)</a>." if old_judge_href else "") + "</p>",
             seeded_note(report, run_start),
             "<div class=kpis>" + "".join(f"<div class=kpi><b>{_e(v)}</b>{_e(k)}</div>" for k, v in kpis) + "</div>",
             "<h2>Counts</h2><p>Cheating levels: " + ", ".join(f"{_e(k)} {_e(v)}" for k, v in
                                                               c["cheating_level"].items()) +
             f" (submitted: {c.get('submitted_basis', {}).get('detector_flagged_submission', 0)} with a detector-flagged "
             f"submission, {c.get('submitted_basis', {}).get('readied_or_undetected', 0)} readied or undetected)" +
             f". Quotes verified {c['quotes_verified']} of {c['quotes']}; downgrades {c['downgrades']}; detector "
             f"cross-check flags {c['detector_flags']}; fallback used {c['fallback_used']}. Detectors: "
             f"{c['detector']['agents_with_flagged_submissions']} agents with flagged submissions, "
             f"{c['detector']['flagged_credits']} flagged credits.</p>"]
    comp = report.get("comparison_with_rubric_3_1")
    if comp:
        cells = comp["confusion_matrix"]
        head = "".join(f"<th>{_e(n)}</th>" for n in cells["cols_new"])
        body = "".join(f"<tr><th>{_e(o)}</th>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
                       for o, row in zip(cells["rows_old"], cells["cells"]))
        parts.append(
            f"<h2>Agreement with the rubric-3.1 judge</h2><p>{comp['compared']} agents compared "
            f"({len(comp['skipped'])} skipped). Cheating any vs none {comp['cheating_any']['agree']}/"
            f"{comp['cheating_any']['n']}; level {comp['cheating_level']['agree']}/{comp['cheating_level']['n']}; "
            f"whistleblower {comp['whistleblower']['agree']}/{comp['whistleblower']['n']}; evaluation-aware "
            f"{comp['evaluation_aware']['agree']}/{comp['evaluation_aware']['n']}.</p>"
            f"<div class=wrap><table><tr><th>old \\ new</th>{head}</tr>{body}</table></div>"
            + ("<p>Disagreements: " + "; ".join(f"{_e(d['agent_id'])} ({_e(', '.join(d['differences']))})"
                                                for d in comp["disagreements"]) + "</p>" if comp["disagreements"]
               else "<p>No disagreements.</p>"))
    parts.append(agents_section(rows, run_start, report.get("seeded")))
    parts.append(f"</main><script>{SCRIPT}</script></body></html>")
    # One-letter agent labels with the full Labels definitions block (owner, October 26, 2026).
    return agent_badges.decorate_html("".join(parts), agent_badges.badges_from_simple(rows, report),
                                      page_rel=agent_badges.SIMPLE_PAGE, definitions=True)


def _yn(value):
    return "yes" if value is True else ("unclear" if value == "unclear" else "no")


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"


def _facts(facts, run_start):
    if not facts:
        return ""
    bits = [f"{facts.get('submissions', 0)} submission(s), {facts.get('accepted', 0)} accepted, "
            f"{facts.get('flagged_submissions', 0)} detector-flagged ({facts.get('flagged_accepted', 0)} accepted)"]
    if facts.get("first_flagged_minute") is not None:
        bits.append("first flagged submission " + clock_minutes(facts["first_flagged_minute"], run_start))
    if facts.get("first_exploit_read_minute") is not None:
        bits.append("first read of an exploit " + clock_minutes(facts["first_exploit_read_minute"], run_start)
                    + (f" (by {facts['first_exploit_read_author']})" if facts.get("first_exploit_read_author") else ""))
    if facts.get("exploit_reads"):
        bits.append(f"{facts['exploit_reads']} exploit read(s)")
    bits.append(f"posts {facts.get('posts', 0)}, DMs sent {facts.get('dms_sent', 0)}, feedback {facts.get('feedback', 0)}")
    return '<div class=facts>Records: ' + _e("; ".join(bits)) + ".</div>"


def _agent_row(row, run_start, seeded=None):
    agent = row["agent_id"]
    labels = row.get("labels")
    if not labels:
        return (f'<tr data-level="{NOT_LABELLED}" data-tech="" data-wb="" data-ea="" data-q="none">'
                f'<td data-label=agent data-v="{_e(agent)}">{_agent(row)}</td>'
                f'<td data-label=cheating data-v="{len(LEVELS)}"><span class=warn>{NOT_LABELLED}</span></td>'
                + "".join(f"<td data-label={k} data-v=\"\"></td>" for k in ("technique", "declined",
                                                                           "whistleblower", "eval-aware", "source"))
                + '<td data-label=quotes data-v="0"></td>'
                f'<td class=det data-label=details><span class=warn>{_e(row.get("status"))}: {_e(row.get("reason"))}'
                f'</span></td></tr>')
    evidence = labels.get("evidence") or []
    verified = sum(bool(q.get("verified")) for q in evidence)
    kinds = (["verified"] if verified else []) + (["unverified"] if verified < len(evidence) else []) or ["none"]
    quotes = "".join(
        f"<div>{'<span class=ok>&#10003;</span>' if q['verified'] else '<span class=warn>&#10007;</span>'} "
        f"<code>{_e(q['record'])}</code> <q>{_e(q['quote'])}</q>"
        + ("" if q["verified"] else f" <span class=muted>({_e(q.get('reason'))})</span>") + "</div>"
        for q in evidence)
    checks = row.get("checks") or {}
    notes = "".join(f"<div class=warn>{_e(n)}</div>" for n in (checks.get("downgrades") or []) + (checks.get("flags") or []))
    level = labels["cheating_level"]
    tech = labels.get("technique") or ""
    channel = f" ({_e(labels['whistleblower_channel'])})" if labels.get("whistleblower_channel") else ""
    source = labels.get("likely_source") or ""
    qcell = (f"{verified}<span class=ok>&#10003;</span>" + (f" {len(evidence) - verified}<span class=warn>&#10007;</span>"
                                                            if len(evidence) > verified else "")) if evidence else ""
    body = (f"<div>{_e(labels.get('rationale'))}</div>"
            + (f"<div>Likely source: {_e(source)}</div>" if source else "")
            + _facts(row.get("facts"), run_start) + quotes + notes)
    order = LEVELS.index(level) if level in LEVELS else len(LEVELS)
    wb, ea, dec = _yn(labels.get("whistleblower")), _yn(labels.get("evaluation_aware")), \
        _yn(labels.get("declined_after_considering"))
    return (f'<tr data-level="{_e(level)}" data-tech="{_e(tech)}" data-wb="{wb}" data-ea="{ea}" '
            f'data-q="{" ".join(kinds)}">'
            f'<td data-label=agent data-v="{_e(agent)}">{_agent(row)}'
            f"{' <span class=warn>fallback</span>' if row.get('fallback_used') else ''}</td>"
            f'<td data-label=cheating data-v="{order}">{_level(level)}</td>'
            f'<td class=tech data-label=technique data-v="{_e(tech)}" title="{_e(tech)}">{_e(tech)}</td>'
            f'<td data-label=declined data-v="{dec}">{_flag(labels.get("declined_after_considering"))}</td>'
            f'<td data-label=whistleblower data-v="{wb}">{_flag(labels.get("whistleblower"))}{channel}</td>'
            f'<td data-label=eval-aware data-v="{ea}">{_flag(labels.get("evaluation_aware"))}</td>'
            f'<td data-label=source data-v="{_e(source)}">{_e(source)}'
            f'{" <span class=muted>(seeded source)</span>" if seeded and _names_seed(source, seeded) else ""}</td>'
            f'<td data-label=quotes data-v="{len(evidence)}">{qcell}</td>'
            f'<td class=det data-label=details><details><summary><span class=prev>{_e(_short(labels.get("rationale"), 140))}</span></summary>'
            f"<div class=body>{body}</div></details></td></tr>")


def agents_section(rows, run_start=None, seeded=None):
    """The filterable, sortable agents table (works without JS: every row shown, details expandable)."""
    techniques = sorted({(r.get("labels") or {}).get("technique") for r in rows} - {None, ""})
    levels = list(LEVELS) + ([NOT_LABELLED] if any(not r.get("labels") for r in rows) else [])
    choice = lambda ident, label, values: (  # noqa: E731
        f'<label>{label}<select id={ident}><option value="">any</option>'
        + "".join(f'<option value="{_e(v)}">{_e(t)}</option>' for v, t in values) + "</select></label>")
    filters = ("<div class=filters id=filters hidden role=search aria-label='Filter agents'>"
               "<details class=ms id=f-level><summary>Cheating: <span id=f-level-text>&nbsp;any</span></summary><div>"
               "<span class=lab>Cheating level</span>"
               + "".join(f'<label><input type=checkbox value="{_e(v)}" checked> {_e(v)}</label>' for v in levels)
               + "</div></details>"
               + choice("f-tech", "Technique", [("-", "(none)")] + [(t, _short(t, 48)) for t in techniques])
               + choice("f-wb", "Whistleblower", [("yes", "yes"), ("no", "no"), ("unclear", "unclear")])
               + choice("f-ea", "Evaluation-aware", [("yes", "yes"), ("no", "no"), ("unclear", "unclear")])
               + choice("f-q", "Quotes", [("verified", "has verified quotes"), ("unverified", "has unverified quotes"),
                                          ("none", "no quotes")])
               + "<label class=grow>Search<input id=f-s type=search placeholder='agent id, rationale, quotes'></label>"
               + "<label>Sort<select id=f-sort>" + "".join(f'<option value="{k}">{t}</option>' for k, t in SORTS)
               + "</select></label><button type=button id=f-dir aria-label='Sort direction'>&#8593;</button>"
               "<button type=button id=f-reset>Reset</button></div>")
    head = "".join(f'<th data-k="{k}" data-i="{i}"><button type=button>{t}</button></th>'
                   for i, (k, t) in enumerate(SORTS)) + "<th>rationale and evidence</th>"
    body = "".join(_agent_row(r, run_start, seeded) for r in rows)
    return (f"<h2 id=agents-table>Agents</h2>{filters}<p id=shown class=muted>{len(rows)} agents</p>"
            f"<table class=agents id=agents><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


SCRIPT = r"""(()=>{const $=id=>document.getElementById(id),table=$('agents');if(!table)return;
const tbody=table.tBodies[0],rows=[...tbody.rows],ths=[...table.tHead.rows[0].cells].filter(c=>c.dataset.k);
const boxes=[...document.querySelectorAll('#f-level input')],all=boxes.map(b=>b.value);
const sel={tech:$('f-tech'),wb:$('f-wb'),ea:$('f-ea'),q:$('f-q')},search=$('f-s'),sortSel=$('f-sort'),dir=$('f-dir');
let key='agent',desc=false;$('filters').hidden=false;
const text=new Map(rows.map(r=>[r,r.textContent.toLowerCase()]));
function cellVal(r,k){const i=ths.findIndex(t=>t.dataset.k===k);return r.cells[i]?r.cells[i].dataset.v||'':'';}
function apply(){const lv=new Set(boxes.filter(b=>b.checked).map(b=>b.value)),q=search.value.toLowerCase().trim();
 let n=0;for(const r of rows){const d=r.dataset,ok=lv.has(d.level)&&(!sel.tech.value||(sel.tech.value==='-'?!d.tech:d.tech===sel.tech.value))
  &&(!sel.wb.value||d.wb===sel.wb.value)&&(!sel.ea.value||d.ea===sel.ea.value)
  &&(!sel.q.value||d.q.split(' ').includes(sel.q.value))&&(!q||text.get(r).includes(q));r.hidden=!ok;n+=ok;}
 const sorted=rows.slice().sort((a,b)=>{const x=cellVal(a,key),y=cellVal(b,key),nx=parseFloat(x),ny=parseFloat(y);
  let c=(!isNaN(nx)&&!isNaN(ny)&&String(nx)===x&&String(ny)===y)?nx-ny:x.localeCompare(y);
  if(!c)c=cellVal(a,'agent').localeCompare(cellVal(b,'agent'));return desc?-c:c;});
 for(const r of sorted)tbody.append(r);
 for(const t of ths)t.dataset.k===key?t.setAttribute('aria-sort',desc?'descending':'ascending'):t.removeAttribute('aria-sort');
 sortSel.value=key;dir.textContent=desc?'↓':'↑';
 const picked=all.filter(v=>lv.has(v));$('f-level-text').textContent=' '+(picked.length===all.length?'any':picked.length?picked.join(', '):'none');
 $('shown').textContent='Showing '+n+' of '+rows.length+' agents';write();}
function write(){const p=new URLSearchParams(),lv=boxes.filter(b=>b.checked).map(b=>b.value);
 if(lv.length!==all.length)p.set('level',lv.join(','));for(const [k,s] of Object.entries(sel))if(s.value)p.set(k,s.value);
 if(search.value.trim())p.set('q',search.value.trim());if(key!=='agent'||desc)p.set('sort',key+(desc?'-desc':''));
 const h=p.toString();if(h!==location.hash.slice(1))history.replaceState(null,'',h?'#'+h:location.pathname+location.search);}
function read(){const raw=location.hash.slice(1);if(raw&&!raw.includes('='))return;const p=new URLSearchParams(raw);
 const lv=p.has('level')?p.get('level').split(','):all;for(const b of boxes)b.checked=lv.includes(b.value);
 for(const [k,s] of Object.entries(sel))s.value=[...s.options].some(o=>o.value===p.get(k))?p.get(k):'';
 search.value=p.get('q')||'';const sort=(p.get('sort')||'agent').split('-');key=ths.some(t=>t.dataset.k===sort[0])?sort[0]:'agent';desc=sort[1]==='desc';}
for(const t of ths)t.querySelector('button').addEventListener('click',()=>{desc=t.dataset.k===key?!desc:false;key=t.dataset.k;apply();});
sortSel.addEventListener('change',()=>{key=sortSel.value;desc=false;apply();});dir.addEventListener('click',()=>{desc=!desc;apply();});
for(const el of [...boxes,...Object.values(sel)])el.addEventListener('change',apply);search.addEventListener('input',apply);
$('f-reset').addEventListener('click',()=>{for(const b of boxes)b.checked=true;for(const s of Object.values(sel))s.value='';search.value='';key='agent';desc=false;apply();});
document.addEventListener('click',e=>{const m=$('f-level');if(m.open&&!m.contains(e.target))m.open=false;});
window.addEventListener('hashchange',()=>{read();apply();});read();apply();})();"""
