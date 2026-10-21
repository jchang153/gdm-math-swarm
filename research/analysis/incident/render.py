"""Rendering of the incident report: report.md, report.html and the host-written appendix.

The writer's Markdown uses a small subset (headings, paragraphs, lists, bold, italic, code,
links) plus excerpt blocks (``verify.BLOCK_RE``). This module renders exactly that subset with
every record text HTML-escaped (record texts are agent-written and never trusted as markup), adds
the model-generated banner and appends the "Where to look" appendix, which is written by the host
from the locators (never by a model), so every pointer is exact.
"""
from __future__ import annotations

import html
import re

from . import verify
from .digest import ITEM_ID

BANNER = ("Post-hoc account written by Claude Opus 5.5 from the run's records after the run; "
          "it can be wrong. Every quoted excerpt was checked by a program to appear verbatim in the cited record "
          "(unverified ones are marked), but the selection and interpretation are the model's. Verify claims against "
          "the sources before relying on them. The deterministic results report (results.html) is the record of "
          "outcomes.")

KIND_CLASS = {"Reasoning (chain of thought)": "reasoning", "Agent message": "message",
              "Agent's own summary (written at a context compaction)": "summary",
              "Tool result (what the agent was shown)": "result", "Private file version": "file"}


def kind_class(label):
    if label in KIND_CLASS:
        return KIND_CLASS[label]
    low = label.lower()
    for key, value in (("submission", "submission"), ("submitted", "submission"), ("board", "board"),
                       ("direct message", "dm"), ("feedback", "feedback"), ("library", "library"),
                       ("tool call", "tool")):
        if key in low:
            return value
    return "other"


def _time(value):
    if not value:
        return "time unknown"
    match = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", str(value))
    return f"{match.group(1)} {match.group(2)} UTC" if match else str(value)


# ---------------------------------------------------------------------------
# Appendix (host-written)


def appendix_records(evidence, locator):
    """Every record the report cites or quotes, grouped: storyline records, then other citations."""
    rows, seen = [], set()
    for story in evidence.get("storylines") or []:
        for record in story["records"]:
            if record["id"] not in seen:
                seen.add(record["id"])
                rows.append(dict(record["locator"], storyline=story["id"]))
    for item_id in evidence.get("cited_ids") or []:
        if item_id not in seen:
            loc = locator.locate(item_id)
            if loc:
                seen.add(item_id)
                rows.append(dict(loc, storyline=None))
    return rows


def appendix_markdown(evidence, locator, digest):
    run_id = digest["run_id"]
    out = ["", "## Where to look", "",
           "Host-written (not model-written) pointers to every record this report quotes or cites. Item ids are "
           "the behavior judge history's stable ids (`research/analysis/judge/history.py`). Inspect View links "
           "open the agent's transcript at the model event that produced the record.", "",
           "Run records: `results.json`, `results.html` (deterministic results report), "
           + ("`analysis/judge/report.html` and `analysis/judge/agents/<agent>.json` (behavior judge), "
              if digest["judge"]["available"] else "")
           + "`evaluations/evaluations.jsonl` (independent proof checker), `conference/state.sqlite` (board, DMs, "
           "library, submissions), `analysis/timeline.jsonl`, " + ", ".join(f"`{s}`" for s in digest["sources"]["inspect"])
           + " (raw Inspect log).", ""]
    if not locator.inspect_view_present:
        out += ["Note: this run directory has no local `inspect-view/` bundle; the Inspect View links work where "
                "the bundle is published beside the run (the hosted experiment library).", ""]
    out += ["| Record | Kind | Agent | Time | Storyline | Raw record |", "| --- | --- | --- | --- | --- | --- |"]
    for row in appendix_records(evidence, locator):
        link = f"[{row['id']}]({row['inspect_url']})" if row.get("inspect_url") else f"`{row['id']}`"
        out.append(f"| {link} | {row['label']} | {row['agent']} | {_time(row['time'])} | {row['storyline'] or ''} | "
                   f"`{row.get('raw') or ''}` |")
    out += ["", "Per agent:", ""]
    for agent_id in digest["agents"]:
        url = locator.sample_url(agent_id)
        judge = f"; judge record `analysis/judge/agents/{agent_id}.json`" if digest["judge"]["available"] else ""
        out.append(f"- **{agent_id}**: " + (f"[Inspect View transcript]({url})" if url else "no Inspect sample") + judge)
    out += ["", "To read any record in full from a checkout (offline, no model):", "",
            "```",
            f"PYTHONPATH=src:. .venv/bin/python -c \"from research.analysis.judge.history import build_run; "
            f"r = build_run('runs/{run_id}', agents=['agent-007']); print(r.lookup('A07.t0123.r')['text'])\"",
            "```", "",
            "`evidence.json` beside this report lists every excerpt with its verification result and every record's "
            "locator (Inspect log, sample, epoch, event and message ids).", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Markdown subset -> HTML


def _inline(text, ctx):
    """Inline Markdown (escaped): code, bold, italic, links, citations, checked inline quotes."""
    tokens = []

    def stash(fragment):
        tokens.append(fragment)
        return f"\x00{len(tokens) - 1}\x00"

    def quote_html(match):
        quote, item_id = match.group(1), match.group(2)
        ok = ctx["inline_ok"].get((verify.normalize(quote), item_id), True)
        mark = "" if ok else ' <span class="unverified-mark" title="Not found verbatim in the cited record">' \
                             'unverified quote</span>'
        return stash(f"“{html.escape(quote)}” " + _cite(item_id, ctx) + mark)

    text = verify.INLINE_RE.sub(quote_html, text)
    text = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|\.{0,2}/?[^)\s]*)\)",
                  lambda m: stash(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'),
                  text)
    text = verify.CITE_RE.sub(lambda m: stash("[" + ", ".join(_cite(i.group(0), ctx) for i in
                                                            verify.ONE_ID_RE.finditer(m.group(1))) + "]"), text)
    text = text.replace("\\$", stash('<span class="nomath">$</span>'))
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], text)


def _cite(item_id, ctx):
    loc = ctx["locate"](item_id)
    if loc is None:
        return f'<span class="cite missing" title="Unknown record id">[{html.escape(item_id)}?]</span>'
    href = loc.get("inspect_url") or f"#rec-{_anchor(item_id)}"
    title = f"{loc['label']}, {loc['agent']}, {_time(loc['time'])}"
    return f'<a class="cite" href="{html.escape(href, quote=True)}" title="{html.escape(title, quote=True)}">' \
           f'{html.escape(item_id)}</a>'


def _anchor(item_id):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", item_id)


def _excerpt_html(record, ctx):
    loc = ctx["locate"](record["item"]) or {"label": "Unknown record", "agent": "?", "time": None,
                                            "inspect_url": None, "raw": None}
    css = kind_class(loc["label"])
    body = html.escape(record["quote"].strip("\n")).replace("[...]", '<span class="elide">[…]</span>') \
        .replace("[…]", '<span class="elide">[…]</span>')
    links = []
    if loc.get("inspect_url"):
        links.append(f'<a href="{html.escape(loc["inspect_url"], quote=True)}">Open in Inspect View</a>')
    links.append(f'<a href="#rec-{_anchor(record["item"])}">Where to look</a>')
    status = "" if record["verified"] else (
        f'<div class="unverified">Not verified: {html.escape(record.get("reason") or "not found verbatim")}. Treat '
        "this excerpt as unreliable.</div>")
    return (f'<figure class="excerpt k-{css}{"" if record["verified"] else " bad"}">'
            f'<figcaption><span class="kind">{html.escape(loc["label"])}</span>'
            f'<span class="meta">{html.escape(str(loc["agent"]))} · {html.escape(_time(loc["time"]))} · '
            f'<code>{html.escape(record["item"])}</code></span></figcaption>{status}'
            f'<blockquote>{body}</blockquote><div class="links">{" · ".join(links)}</div></figure>')


def markdown_to_html(markdown, ctx):
    """Render the report subset. ``ctx``: {"locate", "blocks": {span start: record}, "inline_ok"}."""
    blocks = ctx["blocks"]
    out, para, items, list_kind = [], [], [], None
    in_code, code_lines = False, []

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(para), ctx) + "</p>")
            para.clear()

    def flush_list():
        nonlocal list_kind
        if items:
            tag = "ol" if list_kind == "ol" else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(i, ctx)}</li>" for i in items) + f"</{tag}>")
            items.clear()
        list_kind = None

    position, lines = 0, markdown.split("\n")
    offsets = []
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    index = 0
    table = []
    while index < len(lines):
        line, offset = lines[index], offsets[index]
        if in_code:
            if line.strip().startswith("```"):
                out.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                in_code, code_lines = False, []
            else:
                code_lines.append(line)
            index += 1
            continue
        if offset in blocks:
            flush_para(); flush_list()
            record = blocks[offset]
            out.append(_excerpt_html(record, ctx))
            end = record["span"][1]
            while index < len(lines) and offsets[index] < end:
                index += 1
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_para(); flush_list()
            table.append(stripped)
            index += 1
            if index >= len(lines) or not lines[index].strip().startswith("|"):
                out.append(_table_html(table, ctx))
                table = []
            continue
        if stripped.startswith("```"):
            flush_para(); flush_list()
            in_code = True
            index += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if not stripped:
            flush_para(); flush_list()
        elif heading:
            flush_para(); flush_list()
            level = len(heading.group(1))
            title = heading.group(2)
            ident = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            out.append(f'<h{level} id="{ident}">{_inline(title, ctx)}</h{level}>')
        elif bullet or number:
            flush_para()
            kind = "ul" if bullet else "ol"
            if list_kind not in (None, kind):
                flush_list()
            list_kind = kind
            items.append((bullet or number).group(1))
        elif items and line.startswith(("  ", "\t")):
            items[-1] += " " + stripped
        else:
            flush_list()
            para.append(stripped)
        index += 1
    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    flush_para(); flush_list()
    return "\n".join(out)


def _table_html(rows, ctx):
    cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
    cells = [r for r in cells if not all(re.fullmatch(r":?-{3,}:?", c) for c in r)]
    if not cells:
        return ""
    head, body = cells[0], cells[1:]
    out = ['<div class="table"><table><thead><tr>' + "".join(f"<th>{_inline(c, ctx)}</th>" for c in head)
           + "</tr></thead><tbody>"]
    for row in body:
        anchor = ""
        match = re.match(r"^\[(" + ITEM_ID + r")\]\(|^`(" + ITEM_ID + r")`$", row[0]) if row else None
        if match:
            anchor = f' id="rec-{_anchor(match.group(1) or match.group(2))}"'
        out.append(f"<tr{anchor}>" + "".join(f"<td>{_inline(c, ctx)}</td>" for c in row) + "</tr>")
    return "\n".join(out) + "</tbody></table></div>"


STYLE = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#5f6368;--rule:#e3e0d8;--card:#ffffff;--accent:#2f5d8a;
--warn-bg:#fff4e5;--warn-fg:#7a4b00;--bad:#b3261e;--k-reasoning:#6b4fa3;--k-message:#2f5d8a;--k-tool:#3b6e4f;
--k-submission:#9a5b13;--k-board:#7a3e6e;--k-dm:#1f6f78;--k-feedback:#a33b3b;--k-library:#4d5f2a;
--k-summary:#555;--k-result:#666;--k-file:#555;--k-other:#555}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#17181a;--fg:#e8e6e1;--muted:#a2a6ab;
--rule:#34363a;--card:#202225;--accent:#8db7e3;--warn-bg:#3a2c12;--warn-fg:#f3cf8c;--bad:#f2867d;
--k-reasoning:#b59ce6;--k-message:#8db7e3;--k-tool:#8fcaa3;--k-submission:#e2ad6a;--k-board:#d79bc9;
--k-dm:#7fcbd3;--k-feedback:#ef9a9a;--k-library:#b8c98a;--k-summary:#bbb;--k-result:#aaa;--k-file:#bbb;--k-other:#bbb}}
:root[data-theme="dark"]{--bg:#17181a;--fg:#e8e6e1;--muted:#a2a6ab;--rule:#34363a;--card:#202225;--accent:#8db7e3;
--warn-bg:#3a2c12;--warn-fg:#f3cf8c;--bad:#f2867d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.65 Georgia,"Iowan Old Style","Times New Roman",serif;
font-variant-numeric:lining-nums}
main{max-width:760px;margin:0 auto;padding:28px 16px 80px}
h1{font:700 clamp(1.5rem,4.5vw,2rem)/1.25 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;margin:.4em 0 .6em}
h2{font:700 1.35rem/1.3 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;margin:2.2em 0 .6em;
padding-top:.6em;border-top:1px solid var(--rule)}
h3{font:600 1.12rem/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;margin:1.8em 0 .5em}
a{color:var(--accent)}
code{font:0.86em/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:rgba(127,127,127,.12);
padding:.05em .3em;border-radius:4px}
pre{background:rgba(127,127,127,.10);padding:12px;border-radius:6px;overflow-x:auto}
pre code{background:none;padding:0}
.banner{background:var(--warn-bg);color:var(--warn-fg);border-radius:8px;padding:12px 14px;
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.runline{color:var(--muted);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
margin:.3em 0 1.2em}
figure.excerpt{margin:1.2em 0;background:var(--card);border:1px solid var(--rule);border-left:4px solid var(--kc,#555);
border-radius:6px;padding:10px 14px 8px}
figure.excerpt.bad{border-color:var(--bad);border-left-color:var(--bad)}
figcaption{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline;
font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
figcaption .kind{font-weight:700;color:var(--kc,#555);text-transform:uppercase;letter-spacing:.04em;font-size:12px}
figcaption .meta{color:var(--muted)}
figure.excerpt blockquote{margin:.5em 0;white-space:pre-wrap;word-break:break-word;
font:14.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
figure.excerpt .links{font:12.5px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.unverified{color:var(--bad);font:13px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;margin:.3em 0}
.unverified-mark{color:var(--bad);font-size:.8em;border:1px solid var(--bad);border-radius:3px;padding:0 .3em}
.elide{color:var(--muted)}
a.cite{font:0.8em ui-monospace,SFMono-Regular,Menlo,monospace;text-decoration:none;border-bottom:1px dotted}
.cite.missing{color:var(--bad);font:0.8em ui-monospace,monospace}
.k-reasoning{--kc:var(--k-reasoning)}.k-message{--kc:var(--k-message)}.k-tool{--kc:var(--k-tool)}
.k-submission{--kc:var(--k-submission)}.k-board{--kc:var(--k-board)}.k-dm{--kc:var(--k-dm)}
.k-feedback{--kc:var(--k-feedback)}.k-library{--kc:var(--k-library)}.k-summary{--kc:var(--k-summary)}
.k-result{--kc:var(--k-result)}.k-file{--kc:var(--k-file)}.k-other{--kc:var(--k-other)}
.table{overflow-x:auto;margin:1em 0}
table{border-collapse:collapse;font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
th,td{border-bottom:1px solid var(--rule);padding:6px 8px;text-align:left;vertical-align:top}
.table table{min-width:980px}
td code{word-break:break-word;font-size:11.5px}
td:nth-child(4){white-space:nowrap}
footer{color:var(--muted);font:13px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;margin-top:3em}
"""

KATEX = ('<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">'
         '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>'
         '<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js" '
         'onload="renderMathInElement(document.querySelector(\'main\'),{delimiters:[{left:\'$$\',right:\'$$\','
         'display:true},{left:\'$\',right:\'$\',display:false}],ignoredClasses:[\'excerpt\',\'nomath\'],'
         'throwOnError:false})"></script>')


def render_html(markdown, checks, locator, digest, *, generated_at, models):
    """report.html: banner, the writer's report, the appendix (already appended to ``markdown``)."""
    blocks = {q["span"][0]: q for q in checks["quotes"] if q["form"] == "block"}
    inline_ok = {(verify.normalize(q["quote"]), q["item"]): q["verified"] for q in checks["quotes"]
                 if q["form"] == "inline"}
    ctx = {"locate": locator.locate, "blocks": blocks, "inline_ok": inline_ok}
    title_match = re.search(r"^#\s+(.+)$", markdown, re.M)
    title = title_match.group(1).strip() if title_match else f"Incident report: {digest['run_id']}"
    summary = checks["summary"]
    body = markdown_to_html(markdown, ctx)
    run_line = (f"Run <code>{html.escape(digest['run_id'])}</code> · generated {html.escape(generated_at)} · "
                f"{summary['verified']} of {summary['quotes']} quotes verified verbatim"
                + (f" · <strong>{summary['unverified']} unverified</strong>" if summary["unverified"] else "")
                + ' · <a href="../../results.html">Results report</a>'
                + (' · <a href="../judge/report.html">Behavior judge</a>' if digest["judge"]["available"] else "")
                + ' · <a href="../../inspect-view/index.html">Inspect View</a>'
                + ' · <a href="evidence.json">evidence.json</a> · <a href="report.md">report.md</a>')
    if title_match:
        heading = re.search(r"<h1[^>]*>.*?</h1>", body, re.S)
        if heading:
            body = body[:heading.end()] + f'\n<p class="runline">{run_line}</p>' + body[heading.end():]
    else:
        body = f"<h1>{html.escape(title)}</h1>\n<p class=\"runline\">{run_line}</p>\n" + body
    model_line = ", ".join(f"{k}: {v}" for k, v in sorted(models.items()))
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(_plain_title(title))}</title><meta name=\"robots\" content=\"noindex\">"
            f"<style>{STYLE}</style>{KATEX}</head><body><main>"
            f"<div class=\"banner\" role=\"note\"><strong>Model-generated.</strong> {html.escape(BANNER)}</div>\n"
            f"{body}\n<footer>Incident report (research/analysis/incident), {html.escape(model_line)}. "
            "Researcher-only: never shown to experimental agents.</footer></main></body></html>\n")


def _plain_title(title):
    plain = re.sub(r"[*`$]", "", title)
    return plain if len(plain) <= 90 else plain[:87] + "…"


def annotate_markdown(markdown, checks):
    """report.md: unverified excerpt blocks and inline quotes get a visible note."""
    out, last = [], 0
    for record in sorted(checks["quotes"], key=lambda q: q["span"][0]):
        if record["verified"]:
            continue
        start, end = record["span"]
        out.append(markdown[last:end])
        note = (f"\n> **Unverified excerpt** ({record.get('reason') or 'not found verbatim'})."
                if record["form"] == "block" else " *(unverified quote)*")
        out.append(note)
        last = end
    out.append(markdown[last:])
    return "".join(out)


def banner_markdown(generated_at):
    return f"> **Model-generated.** {BANNER} Generated {generated_at}.\n\n"
