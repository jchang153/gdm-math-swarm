"""Small self-contained HTML page of one simplified-judge run (no scripts, light and dark theme)."""
from __future__ import annotations

import html

from .pipeline import names_seed as _names_seed

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


def seeded_note(report):
    """Seeded runs: the planted source, never judged (researcher-only); "" otherwise."""
    seeded = report.get("seeded")
    if not seeded:
        return ""
    src = (report.get("counts") or {}).get("seeded_source") or {}
    when = seeded.get("injected_min")
    return (f"<p class=note id=sj-seeded><strong>Seeded run (researcher-only):</strong> {_e(seeded.get('seed_agent'))} "
            f"is the seeded source, planted by the researchers"
            + (f" at minute {_e(when)}" if when is not None else "")
            + f" (an accepted exploit for {_e(seeded.get('task_id'))}"
            + (f", published as <code>{_e(seeded.get('path'))}</code>" if seeded.get("path") else "")
            + "). It is not a model agent and is not judged; a likely source naming it is counted as seeded, never "
              f"independent. {_e(src.get('agents_that_read_seed_file', 0))} judged agents read its file, "
              f"{_e(src.get('read_seed_file_before_first_flagged_submission', 0))} before their first flagged "
              f"submission; {_e(src.get('cheating_agents_naming_seed', 0))} cheating agents name it as their source.</p>")


def render_html(report, rows, old_judge_href=None):
    """The page; ``old_judge_href`` (for example ``../judge/report.html``) links the run's rubric-3.1 judge."""
    c = report["counts"]
    kpis = [("agents", c["agents"]), ("labelled", c["labelled"]), ("cheating (explored+)", c["cheating_any"]),
            ("credited", c["cheating_level"].get("credited", 0)),
            ("whistleblowers", c["whistleblower"].get("True", 0)),
            ("evaluation-aware", c["evaluation_aware"].get("True", 0)),
            ("cost", f"${float(report['cost_usd']):.2f}"), ("wall time", f"{report.get('wall_seconds', 0) / 60:.1f} min")]
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'><title>Simple behavior judge</title>"
             f"<style>{STYLE}</style></head><body><main>",
             f"<h1>Simple behavior judge</h1><p class=muted>{_e(report['run_id'])} &middot; {_e(report['rubric'])} "
             f"&middot; model {_e(report.get('model'))} (fallback {_e(report.get('fallback_model'))}) &middot; "
             f"one call per agent; quotes checked verbatim by code; labels without verified evidence shown as "
             f"unclear. Researcher-only.</p>",
             f"<p class=note id={NOTE_ID}><strong>Not comparable:</strong> {_e(NOTE)}"
             + (f" The rubric-3.1 judge's report for this run: <a href=\"{_e(old_judge_href)}\">Behavior judge "
                f"(rubric 3.1)</a>." if old_judge_href else "") + "</p>",
             seeded_note(report),
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
    parts.append("<h2>Agents</h2><div class=wrap><table><tr><th>agent</th><th>cheating</th><th>technique</th>"
                 "<th>declined</th><th>whistle&shy;blower</th><th>eval-aware</th><th>source</th><th>rationale and "
                 "evidence</th></tr>")
    for row in rows:
        labels = row.get("labels")
        if not labels:
            parts.append(f"<tr><td>{_agent(row)}</td><td colspan=7 class=warn>{_e(row['status'])}: "
                         f"{_e(row['reason'])}</td></tr>")
            continue
        quotes = "".join(
            f"<div>{'<span class=ok>&#10003;</span>' if q['verified'] else '<span class=warn>&#10007;</span>'} "
            f"<code>{_e(q['record'])}</code> <q>{_e(q['quote'])}</q>"
            + ("" if q["verified"] else f" <span class=muted>({_e(q.get('reason'))})</span>") + "</div>"
            for q in labels["evidence"])
        notes = "".join(f"<div class=warn>{_e(n)}</div>" for n in row["checks"]["downgrades"] + row["checks"]["flags"])
        extra = (f"<details><summary>{len(labels['evidence'])} quote(s)</summary>{quotes}{notes}</details>"
                 if quotes or notes else "")
        channel = f" ({_e(labels['whistleblower_channel'])})" if labels["whistleblower_channel"] else ""
        source = _e(labels["likely_source"] or "")
        if report.get("seeded") and _names_seed(labels["likely_source"], report["seeded"]):
            source += " <span class=muted>(seeded source)</span>"
        parts.append(f"<tr><td>{_agent(row)}{' <span class=warn>fallback</span>' if row['fallback_used'] else ''}"
                     f"</td><td>{_level(labels['cheating_level'])}</td><td>{_e(labels['technique'] or '')}</td>"
                     f"<td>{_flag(labels['declined_after_considering'])}</td><td>{_flag(labels['whistleblower'])}"
                     f"{channel}</td><td>{_flag(labels['evaluation_aware'])}</td><td>{source}"
                     f"</td><td>{_e(labels['rationale'])}{extra}</td></tr>")
    parts.append("</table></div></main></body></html>")
    return "".join(parts)
