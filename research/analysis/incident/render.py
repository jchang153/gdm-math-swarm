"""Rendering of the incident report: report.md, report.html and the host-written appendix.

The writer's Markdown uses a small subset (headings, paragraphs, lists, bold, italic, code,
links) plus excerpt blocks (``verify.BLOCK_RE``). This module renders exactly that subset with
every record text HTML-escaped (record texts are agent-written and never trusted as markup), adds
the model-generated banner and labels every excerpt with its agent, run-relative and absolute time
(scripts/run_clock.py) and Inspect View link, from the host's locators (never from a model).

The report has no per-agent section ("The agents, one by one") and no host appendix ("Where to look")
since the owner's request of October 25, 2026; ``relayout`` removes both from an already rendered report
without any model call or run record.
"""
from __future__ import annotations

import html
from pathlib import Path
import re
import sys

from . import verify
from .digest import ITEM_ID

try:
    import run_clock
except ImportError:  # scripts/ (the shared run clock) is not on the path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    import run_clock

BANNER = ("Post-hoc account written by Claude Opus 5.5 from the run's records after the run; "
          "it can be wrong. Every quoted excerpt was checked by a program to appear verbatim in the cited record "
          "(unverified ones are marked), but the selection and interpretation are the model's. Verify claims against "
          "the sources before relying on them. The deterministic results report (results.html) is the record of "
          "outcomes.")

KIND_CLASS = {"Reasoning (chain of thought)": "reasoning", "Agent message (assistant text)": "message",
              "Visible thinking (assistant text)": "thinking",
              "Reasoning (encrypted by the provider; unreadable)": "reasoning",
              "Agent's own summary (written at a context compaction)": "summary",
              "Tool result (what the agent was shown)": "result", "Private file version": "file"}


def kind_class(label):
    if label in KIND_CLASS:
        return KIND_CLASS[label]
    low = label.lower()
    for key, value in (("template text", "template"), ("submission", "submission"), ("submitted", "submission"),
                       ("board", "board"),
                       ("direct message", "dm"), ("feedback", "feedback"), ("library", "library"),
                       ("tool call", "tool")):
        if key in low:
            return value
    return "other"


def _time(value, start=None):
    """An event time as every page shows it: ``+12:34 (05:17:40 UTC)`` relative to the run start
    (scripts/run_clock.py); only the absolute time when the start is unknown."""
    if not value:
        return "time unknown"
    match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", str(value))
    if not match:
        return str(value)
    if start is None:
        return f"{match.group(1)} {match.group(2)} UTC"
    return run_clock.clock(f"{match.group(1)}T{match.group(2)}Z", start)


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
    title = f"{loc['label']}, {loc['agent']}, {_time(loc['time'], ctx.get('start'))}"
    if not loc.get("inspect_url"):  # no same-page appendix any more: a labelled record id, not a link
        return f'<span class="cite" title="{html.escape(title, quote=True)}">{html.escape(item_id)}</span>'
    return f'<a class="cite" href="{html.escape(loc["inspect_url"], quote=True)}" ' \
           f'title="{html.escape(title, quote=True)}">{html.escape(item_id)}</a>'


def _anchor(item_id):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", item_id)


def excerpt_meta(record, ctx):
    """(label, evidence excerpt or None) of an excerpt block: the evidence pack's host-assigned label
    (a submission excerpt says whether it is template text or the agent's own lines)."""
    excerpt = (ctx.get("excerpts") or {}).get(record.get("eid"))
    if excerpt and excerpt.get("label"):
        return excerpt["label"], excerpt
    label = ctx["excerpt_label"](record["item"], record["quote"]) if ctx.get("excerpt_label") else None
    return label, excerpt


FALLBACK_NOTE = "Program-chosen passage"
MODEL_FALLBACK_NOTE = "Fallback model"


def _excerpt_html(record, ctx):
    loc = ctx["locate"](record["item"]) or {"label": "Unknown record", "agent": "?", "time": None,
                                            "inspect_url": None, "raw": None}
    label, excerpt = excerpt_meta(record, ctx)
    label = label or loc["label"]
    css = kind_class(label)
    body = html.escape(record["quote"].strip("\n")).replace("[...]", '<span class="elide">[…]</span>') \
        .replace("[…]", '<span class="elide">[…]</span>')
    links = []
    if loc.get("inspect_url"):
        links.append(f'<a href="{html.escape(loc["inspect_url"], quote=True)}">Open in Inspect View</a>')
    status = "" if record["verified"] else (
        f'<div class="unverified">Not verified: {html.escape(record.get("reason") or "not found verbatim")}. Treat '
        "this excerpt as unreliable.</div>")
    if record.get("incomplete"):
        status += f'<div class="unverified">Incomplete excerpt: {html.escape(record["incomplete"])}.</div>'
    badge = ""
    if excerpt and excerpt.get("fallback"):
        why = excerpt.get("fallback_reason") or "the extraction model gave no passage"
        badge = (f'<span class="fallback" title="Chosen by the program, not the extraction model, because '
                 f'{html.escape(why, quote=True)}">{FALLBACK_NOTE}</span>')
    elif excerpt and excerpt.get("model_fallback"):
        badge = (f'<span class="fallback" title="Chosen by {html.escape(str(excerpt.get("model") or "the fallback model"), quote=True)}'
                 ' because the primary model provider refused the request">'
                 f'{MODEL_FALLBACK_NOTE}</span>')
    return (f'<figure class="excerpt k-{css}{"" if record["verified"] else " bad"}">'
            f'<figcaption><span class="kind">{html.escape(label)}</span>'
            f'<span class="meta">{html.escape(str(loc["agent"]))} · {html.escape(_time(loc["time"], ctx.get("start")))} · '
            f'<code>{html.escape(record["item"])}</code>'
            + (f' · <code>{html.escape(record["eid"])}</code>' if record.get("eid") else "")
            + f'</span>{badge}</figcaption>{status}'
            f'<blockquote>{body}</blockquote>'
            + (f'<div class="links">{" · ".join(links)}</div>' if links else "") + '</figure>')


def markdown_to_html(markdown, ctx):
    """Render the report subset. ``ctx``: {"locate", "blocks": {span start: record}, "inline_ok"}."""
    blocks = ctx["blocks"]
    out, para, items, list_kind = [], [], [], None
    in_code, code_lines = False, []

    def flush_para():
        if para:
            joined = " ".join(para)
            css = ' class="hostnote"' if joined.startswith(HOST_NOTE_LEAD) else ""
            out.append(f"<p{css}>" + _inline(joined, ctx) + "</p>")
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
--k-summary:#555;--k-result:#666;--k-file:#555;--k-other:#555;--k-thinking:#2f5d8a;--k-template:#7a6a55}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#17181a;--fg:#e8e6e1;--muted:#a2a6ab;
--rule:#34363a;--card:#202225;--accent:#8db7e3;--warn-bg:#3a2c12;--warn-fg:#f3cf8c;--bad:#f2867d;
--k-reasoning:#b59ce6;--k-message:#8db7e3;--k-tool:#8fcaa3;--k-submission:#e2ad6a;--k-board:#d79bc9;
--k-dm:#7fcbd3;--k-feedback:#ef9a9a;--k-library:#b8c98a;--k-summary:#bbb;--k-result:#aaa;--k-file:#bbb;--k-other:#bbb;
--k-thinking:#8db7e3;--k-template:#c9b79a}}
:root[data-theme="dark"]{--bg:#17181a;--fg:#e8e6e1;--muted:#a2a6ab;--rule:#34363a;--card:#202225;--accent:#8db7e3;
--warn-bg:#3a2c12;--warn-fg:#f3cf8c;--bad:#f2867d;--k-reasoning:#b59ce6;--k-message:#8db7e3;--k-tool:#8fcaa3;
--k-submission:#e2ad6a;--k-board:#d79bc9;--k-dm:#7fcbd3;--k-feedback:#ef9a9a;--k-library:#b8c98a;--k-summary:#bbb;
--k-result:#aaa;--k-file:#bbb;--k-other:#bbb;--k-thinking:#8db7e3;--k-template:#c9b79a}
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
.cite{font:0.8em ui-monospace,SFMono-Regular,Menlo,monospace;text-decoration:none;border-bottom:1px dotted}
.cite.missing{color:var(--bad);font:0.8em ui-monospace,monospace}
.k-reasoning{--kc:var(--k-reasoning)}.k-message{--kc:var(--k-message)}.k-tool{--kc:var(--k-tool)}
.k-submission{--kc:var(--k-submission)}.k-board{--kc:var(--k-board)}.k-dm{--kc:var(--k-dm)}
.k-feedback{--kc:var(--k-feedback)}.k-library{--kc:var(--k-library)}.k-summary{--kc:var(--k-summary)}
.k-result{--kc:var(--k-result)}.k-file{--kc:var(--k-file)}.k-other{--kc:var(--k-other)}
.k-thinking{--kc:var(--k-thinking)}.k-template{--kc:var(--k-template)}
.fallback{font-size:11.5px;border:1px solid var(--muted);color:var(--muted);border-radius:3px;padding:0 .35em}
.hostnote{background:rgba(127,127,127,.08);border-left:3px solid var(--muted);padding:8px 12px;border-radius:4px;
font-size:.95em}
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


def excerpt_table(evidence):
    return {e["eid"]: e for s in (evidence or {}).get("storylines") or [] for r in s["records"]
            for e in r["excerpts"] if e.get("eid")}


def render_html(markdown, checks, locator, digest, *, generated_at, models, evidence=None, start=None):
    """report.html: banner and the writer's report; ``start`` is the run start (epoch seconds) for the
    run-relative times."""
    blocks = {q["span"][0]: q for q in checks["quotes"] if q["form"] == "block"}
    inline_ok = {(verify.normalize(q["quote"]), q["item"]): q["verified"] for q in checks["quotes"]
                 if q["form"] == "inline"}
    ctx = {"locate": locator.locate, "blocks": blocks, "inline_ok": inline_ok, "excerpts": excerpt_table(evidence),
           "excerpt_label": getattr(locator, "excerpt_label", None), "start": start}
    title_match = re.search(r"^#\s+(.+)$", markdown, re.M)
    title = title_match.group(1).strip() if title_match else f"Incident report: {digest['run_id']}"
    summary = checks["summary"]
    body = markdown_to_html(markdown, ctx)
    run_line = (f"Run <code>{html.escape(digest['run_id'])}</code> · generated {html.escape(generated_at)} · "
                f"{summary['verified']} of {summary['quotes']} quotes verified verbatim"
                + (f" · <strong>{summary['unverified']} unverified</strong>" if summary["unverified"] else "")
                + ' · <a href="../../results.html">Results report</a>'
                + (f' · <a href="{judge_report_href(digest)}">Behavior judge</a>' if digest["judge"]["available"] else "")
                + ' · <a href="../../inspect-view/index.html">Inspect View</a>'
                + ' · <a href="evidence.json">evidence.json</a> · <a href="report.md">report.md</a>')
    if title_match:
        heading = re.search(r"<h1[^>]*>.*?</h1>", body, re.S)
        if heading:
            body = body[:heading.end()] + f'\n<p class="runline">{run_line}</p>' + body[heading.end():]
    else:
        body = f"<h1>{html.escape(title)}</h1>\n<p class=\"runline\">{run_line}</p>\n" + body
    model_line = ", ".join(f"{k}: {v}" for k, v in sorted(models.items()))
    return add_toc("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
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
    """The Markdown with unverified excerpt blocks and inline quotes given a visible note."""
    return reader_markdown(markdown, checks, None, None)


def reader_markdown(markdown, checks, locator, evidence, start=None):
    """report.md: each excerpt block becomes a labelled quotation (label, agent, time, record and
    excerpt ids, Inspect View link, program-chosen flag) around a verbatim code block; unverified or
    incomplete excerpts and unverified inline quotes get a visible note."""
    run_start = start   # the run start; "start" is reused below for excerpt spans
    excerpts = excerpt_table(evidence)
    out, last = [], 0
    for record in sorted(checks["quotes"], key=lambda q: q["span"][0]):
        start, end = record["span"]
        if record["form"] == "inline":
            if not record["verified"]:
                out.append(markdown[last:end] + " *(unverified quote)*")
                last = end
            continue
        out.append(markdown[last:start])
        loc = locator.locate(record["item"]) if locator is not None else None
        label, excerpt = excerpt_meta(record, {"excerpts": excerpts, "excerpt_label": getattr(locator, "excerpt_label",
                                                                                              None)})
        label = label or (loc or {}).get("label") or "Excerpt"
        head = [f"**{label}**"]
        if loc:
            head += [str(loc["agent"]), _time(loc["time"], run_start)]
        head.append(f"`{record['item']}`" + (f" ({record['eid']})" if record.get("eid") else ""))
        if loc and loc.get("inspect_url"):
            head.append(f"[Inspect View]({loc['inspect_url']})")
        if excerpt and excerpt.get("fallback"):
            head.append(f"*{FALLBACK_NOTE.lower()}: {excerpt.get('fallback_reason') or 'no model passage'}*")
        elif excerpt and excerpt.get("model_fallback"):
            head.append(f"*{MODEL_FALLBACK_NOTE.lower()}: {excerpt.get('model') or 'non-Anthropic model'}*")
        fence = "~~~~" if "```" in record["quote"] else "```"
        lines = ["> " + " · ".join(head), ">", "> " + fence + "text"]
        lines += [("> " + line).rstrip() for line in record["quote"].split("\n")]
        lines.append("> " + fence)
        if not record["verified"]:
            lines.append(f"> **Unverified excerpt** ({record.get('reason') or 'not found verbatim'}).")
        if record.get("incomplete"):
            lines.append(f"> **Incomplete excerpt** ({record['incomplete']}).")
        out.append("\n".join(lines))
        last = end
    out.append(markdown[last:])
    return "".join(out)


HOST_NOTE_LEAD = "**Note from the program (not the model).**"


def _simple(digest):
    judge = digest.get("judge") or {}
    return judge.get("available") and judge.get("source") == "simple"


def judge_records_phrase(digest):
    if not (digest.get("judge") or {}).get("available"):
        return ""
    if _simple(digest):
        return ("`analysis/simple_judge/report.html` and `analysis/simple_judge/agents.jsonl` (simplified behavior "
                "judge, one row per agent), ")
    return "`analysis/judge/report.html` and `analysis/judge/agents/<agent>.json` (behavior judge), "


def judge_agent_phrase(digest, agent_id):
    if not (digest.get("judge") or {}).get("available"):
        return ""
    if _simple(digest):
        return f"; judge row `{agent_id}` in `analysis/simple_judge/agents.jsonl`"
    return f"; judge record `analysis/judge/agents/{agent_id}.json`"


def judge_report_href(digest):
    return "../simple_judge/report.html" if _simple(digest) else "../judge/report.html"


def behavior_findings_statement(digest):
    """Which behavior judge the report's findings come from (host-written), e.g. "Behavior findings:
    simplified one-pass judge (gdm-simple-judge/1)."; with the simplified judge, also that its
    evaluation-awareness counts are not comparable with the rubric-3.1 judge's."""
    judge = digest.get("judge") or {}
    if judge.get("statement"):
        line = judge["statement"]
    elif judge.get("available"):
        line = "Behavior findings: rubric-3.1 judge."
    else:
        line = "Behavior findings: none, deterministic detectors."
    if _simple(digest):
        line += (" That judge labels each agent in one model call, checks its quotes and labels by code and writes "
                 "no run-level summary; its evaluation-awareness counts are much higher than the rubric-3.1 judge's "
                 "and are not comparable with reports that used that judge.")
    elif not judge.get("available"):
        line += " No behavior judge output was used; exploit counts come from the program's detectors."
    return line


def host_note_markdown(digest, evidence, refusals, units, draft="", *, model_fallbacks=()):
    """The host-written note added to "How sure we are": whether the chain of thought was readable, who
    chose each excerpt shown, and how many extraction requests the model provider refused."""
    shown = set(re.findall(r"^:::excerpt[ \t]+\S+[ \t]+(E\d+)", draft or "", re.M))
    table = excerpt_table(evidence)
    rows = [table[e] for e in sorted(shown, key=lambda x: int(x[1:])) if e in table] if shown else list(table.values())
    whole = sum(e["source"] == "host" for e in rows)
    model = sum(e["source"] == "model" for e in rows)
    fallback = [e for e in rows if e["source"] == "host_fallback"]
    refused_units = [u for u in units or [] if u.get("status") == "ungraded"]
    parts = [HOST_NOTE_LEAD, behavior_findings_statement(digest), (digest.get("reasoning") or {}).get("statement") or ""]
    parts.append(f"This report shows {len(rows)} excerpts: {whole} are whole short records, {model} are passages the "
                 f"extraction model picked out of longer records, and {len(fallback)} "
                 + ("were" if len(fallback) != 1 else "was") + " chosen by the program itself"
                 + (" (" + ", ".join(e["eid"] for e in fallback) + ")" if fallback else "") + ".")
    if fallback:
        reasons = sorted({e.get("fallback_reason") or "no model passage" for e in fallback})
        parts.append("The program chose those because " + "; or ".join(reasons) + ". They are marked "
                     f"\"{FALLBACK_NOTE}\"; they are verbatim but may not be the most telling part of their record.")
    if refused_units:
        parts.append(f"{len(refused_units)} extraction request" + ("s were" if len(refused_units) != 1 else " was")
                     + " refused by the model provider (see `extract_refusals` in evidence.json).")
    else:
        parts.append("No extraction request was refused.")
    answered = [f for f in model_fallbacks or () if f.get("status") == "ok"]
    if answered:
        models = sorted({f["fallback_model"] for f in answered})
        stages = sorted({f["stage"] for f in answered})
        picked = [e for e in rows if e.get("model_fallback")]
        parts.append(f"{len(answered)} request" + ("s" if len(answered) != 1 else "") + " refused by "
                     + ", ".join(sorted({f["refused_by"] for f in answered})) + " (stage "
                     + ", ".join(stages) + ") " + ("were" if len(answered) != 1 else "was")
                     + " answered by the fallback model " + ", ".join(models) + " instead"
                     + (f"; its {len(picked)} excerpt" + ("s are" if len(picked) != 1 else " is")
                        + f" marked \"{MODEL_FALLBACK_NOTE}\"" if picked else "")
                     + " (see `model_fallbacks` in evidence.json).")
    return " ".join(p for p in parts if p)


def insert_into_section(markdown, heading, text):
    """``text`` as a paragraph at the end of the "## <heading>" section (appended at the end when the
    section is missing)."""
    match = re.search(r"^##[ \t]+" + re.escape(heading).replace("\\'", "['’]") + r"[ \t]*$", markdown, re.M | re.I)
    if not match:
        return markdown.rstrip() + "\n\n" + text + "\n"
    following = re.search(r"^##[ \t]", markdown[match.end():], re.M)
    at = match.end() + following.start() if following else len(markdown)
    return markdown[:at].rstrip() + "\n\n" + text + "\n\n" + markdown[at:].lstrip("\n")


def banner_markdown(generated_at):
    return f"> **Model-generated.** {BANNER} Generated {generated_at}.\n\n"


# ---------------------------------------------------------------------------
# Table of contents (owner request, October 22, 2026): a sticky left-margin list on wide screens, a
# "Contents" button at the top on narrow ones; the section in view is highlighted (IntersectionObserver)
# and a thin bar shows reading progress. Inline CSS and JS only (works offline and on the static site).

TOC_START, TOC_END = "<!-- toc:start -->", "<!-- toc:end -->"
TOC_SUBSECTIONS = ("case-studies", "how-the-exploit-spread")
TOC_STYLE = """
html{scroll-behavior:smooth}
@media (prefers-reduced-motion: reduce){html{scroll-behavior:auto}}
h2[id],h3[id]{scroll-margin-top:16px}
.readbar{position:fixed;top:0;left:0;height:3px;width:0;background:var(--accent);z-index:20;pointer-events:none}
nav.toc{font:13.5px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;color:var(--muted)}
nav.toc ol{list-style:none;margin:0;padding:0}
nav.toc ol ol{padding-left:12px;margin:2px 0 4px}
nav.toc a{display:block;color:var(--muted);text-decoration:none;padding:3px 8px;border-left:2px solid transparent;
border-radius:0 4px 4px 0}
nav.toc a:hover{color:var(--fg);background:rgba(127,127,127,.08)}
nav.toc a.active{color:var(--fg);border-left-color:var(--accent);font-weight:600}
nav.toc .toc-title{font-weight:700;text-transform:uppercase;letter-spacing:.06em;font-size:11.5px;margin:0 0 8px 10px}
nav.toc .toc-toggle{display:none}
@media (min-width:1240px){
nav.toc{position:fixed;top:72px;left:max(16px,calc(50% - 380px - 250px));width:220px;max-height:calc(100vh - 96px);
overflow-y:auto;padding-bottom:12px}
}
@media (max-width:1239.98px){
nav.toc{max-width:760px;margin:12px auto 0;padding:0 16px}
nav.toc .toc-title{display:none}
nav.toc .toc-toggle{display:inline-flex;align-items:center;gap:6px;font:600 14px/1 -apple-system,BlinkMacSystemFont,
"Segoe UI",Helvetica,Arial,sans-serif;color:var(--fg);background:var(--card);border:1px solid var(--rule);
border-radius:6px;padding:8px 12px;cursor:pointer}
nav.toc.js > ol{display:none;margin-top:8px;background:var(--card);border:1px solid var(--rule);border-radius:6px;
padding:6px 4px}
nav.toc.js.open > ol{display:block}
}
"""
TOC_SCRIPT = """
(function(){
var nav=document.querySelector('nav.toc');if(!nav)return;nav.classList.add('js');
var btn=nav.querySelector('.toc-toggle');
btn.addEventListener('click',function(){var o=nav.classList.toggle('open');btn.setAttribute('aria-expanded',o)});
nav.addEventListener('click',function(e){if(e.target.tagName==='A'&&btn.offsetParent!==null){nav.classList.remove('open');
btn.setAttribute('aria-expanded','false')}});
var links=[].slice.call(nav.querySelectorAll('a[href^="#"]'));
var heads=links.map(function(a){return document.getElementById(decodeURIComponent(a.getAttribute('href').slice(1)))});
var bar=document.querySelector('.readbar');
function update(){var cur=-1;for(var i=0;i<heads.length;i++){if(heads[i]&&heads[i].getBoundingClientRect().top<=window.innerHeight*0.25)cur=i}
links.forEach(function(a,i){a.classList.toggle('active',i===cur)});
if(bar){var h=document.documentElement;var max=h.scrollHeight-h.clientHeight;bar.style.width=(max>0?Math.min(100,100*h.scrollTop/max):0)+'%'}}
if('IntersectionObserver' in window){var io=new IntersectionObserver(update,{rootMargin:'0px 0px -75% 0px',threshold:[0,1]});
heads.forEach(function(h){if(h)io.observe(h)})}
var queued=false;window.addEventListener('scroll',function(){if(!queued){queued=true;requestAnimationFrame(function(){queued=false;update()})}},{passive:true});
window.addEventListener('resize',update);update();
})();
"""


def _heading_text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def toc_entries(page):
    """[(id, text, [(id, text), ...])] for the page's h2 sections, with h3 sub-entries under the
    case studies and the contagion section."""
    entries = []
    for match in re.finditer(r'<h([23]) id="([^"]+)"[^>]*>(.*?)</h\1>', page, re.S):
        level, ident, text = match.group(1), match.group(2), _heading_text(match.group(3))
        if level == "2":
            entries.append((ident, text, []))
        elif entries and entries[-1][0] in TOC_SUBSECTIONS:
            entries[-1][2].append((ident, text))
    return entries


def _strip_toc(page):
    page = re.sub(re.escape(TOC_START) + r".*?" + re.escape(TOC_END), "", page, flags=re.S)
    return re.sub(r'<style id="toc-style">.*?</style>|<script id="toc-script">.*?</script>', "", page, flags=re.S)


def add_toc(page):
    """The page with its table of contents, reading-progress bar, style and script (idempotent)."""
    page = _strip_toc(page)
    entries = toc_entries(page)
    if not entries:
        return page
    items = []
    for ident, text, subs in entries:
        sub = ("<ol>" + "".join(f'<li><a href="#{html.escape(i)}">{html.escape(t)}</a></li>' for i, t in subs)
               + "</ol>") if subs else ""
        items.append(f'<li><a href="#{html.escape(ident)}">{html.escape(text)}</a>{sub}</li>')
    nav = (f'{TOC_START}<div class="readbar" aria-hidden="true"></div>'
           '<nav class="toc" aria-label="Contents"><p class="toc-title">Contents</p>'
           '<button class="toc-toggle" type="button" aria-expanded="false">&#9776; Contents</button>'
           f'<ol>{"".join(items)}</ol></nav>{TOC_END}')
    page = page.replace("</head>", f'<style id="toc-style">{TOC_STYLE}</style></head>', 1)
    page = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + nav, page, count=1)
    return page.replace("</body>", f'<script id="toc-script">{TOC_SCRIPT}</script></body>', 1)


# ---------------------------------------------------------------------------
# Host-side relayout of an existing report (owner request, October 25, 2026): no model call, no run record.

REMOVED_SECTIONS = ("the agents, one by one", "where to look")
_EVENT_TIME_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC\b")
_HTML_H2_RE = re.compile(r'<h2\b[^>]*>(.*?)</h2>', re.S)
_MD_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
_SECTION_END_HTML = re.compile(r'<h2\b|<!-- contagion:start|<!-- oversight:start|<footer|</main>')
_SECTION_END_MD = re.compile(r"^##[ \t]|^<!-- contagion:start|^<!-- oversight:start", re.M)


def _removed(title):
    return _heading_text(title).replace("’", "'").lower().rstrip(".") in REMOVED_SECTIONS


def _drop_sections(text, heading_re, end_re):
    while True:
        match = next((m for m in heading_re.finditer(text) if _removed(m.group(1))), None)
        if match is None:
            return text
        following = end_re.search(text, match.end())
        stop = following.start() if following else len(text)
        text = text[:match.start()].rstrip("\n") + "\n" + text[stop:]


def restamp(text, start):
    """``text`` with every event time written as ``YYYY-MM-DD HH:MM:SS UTC`` (the earlier excerpt-header and
    citation format) shown as ``+12:34 (05:17:40 UTC)``; other times (the minute-precision 'generated' line)
    are left alone. Idempotent."""
    if start is None:
        return text
    return _EVENT_TIME_RE.sub(lambda m: run_clock.clock(f"{m.group(1)}T{m.group(2)}Z", start), text)


def report_start(evidence, run_dir=None):
    """The run start of a report: evidence.json's contagion clock (runtime.jsonl common_start), else
    run_clock.run_start(run_dir) (results.json start_utc or runtime.jsonl), else None."""
    from .contagion import _start
    record = (evidence or {}).get("contagion")
    start = _start(record) if isinstance(record, dict) else None
    if start is None and run_dir is not None:
        start = run_clock.run_start(run_dir)
    return start


def relayout(page, md, evidence, start=None):
    """(report.html, report.md) of an already rendered report in the current layout: without the
    "The agents, one by one" and "Where to look" sections (and their contents entries), without the
    per-excerpt "Where to look" links and the same-page record links that pointed into that appendix,
    with run-relative plus absolute event times, the contagion section re-inserted from
    evidence["contagion"] and the table of contents rebuilt. evidence.json is not changed. Idempotent."""
    from . import contagion, oversight
    start = report_start(evidence) if start is None else start
    record = (evidence or {}).get("contagion")
    if page is not None:
        page = _strip_toc(page)
        page = _drop_sections(page, _HTML_H2_RE, _SECTION_END_HTML)
        page = re.sub(r'(?: · )?<a href="#rec-[^"]*">Where to look</a>', "", page)
        page = page.replace('<div class="links"></div>', "")
        page = re.sub(r'<a class="cite" href="#rec-[^"]*"( title="[^"]*")?>(.*?)</a>',
                      lambda m: f'<span class="cite"{m.group(1) or ""}>{m.group(2)}</span>', page)
        page = page.replace("\na.cite{font:", "\n.cite{font:", 1)   # record ids without a link keep their look
        page = restamp(page, start)
        if isinstance(record, dict) and record.get("clock"):
            page = contagion.insert_html(page, record)
        if isinstance((evidence or {}).get("oversight"), dict):
            page = oversight.insert_html(page, evidence["oversight"])
        page = add_toc(page)
    if md is not None:
        md = _drop_sections(md, _MD_H2_RE, _SECTION_END_MD)
        md = restamp(md, start)
        if isinstance(record, dict) and record.get("clock"):
            md = contagion.insert_markdown(md, record)
        if isinstance((evidence or {}).get("oversight"), dict):
            md = oversight.insert_markdown(md, evidence["oversight"])
        md = md.rstrip("\n") + "\n"
    return page, md
