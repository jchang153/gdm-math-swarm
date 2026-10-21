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
This is a derived transcript view, not a judge or behavioral evaluator. Researcher-only:
never provide it to experimental agents. Run only after experiment logging finishes.
"""
import argparse
import html
import json
from pathlib import Path
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]

STYLE = ('<style>body{max-width:1100px;margin:36px auto;padding:0 20px;font:16px system-ui;line-height:1.5}'
         'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f4f4;padding:12px;margin:6px 0}'
         'article{border-top:1px solid #ccc;margin:22px 0}details{margin:8px 0}a{color:#1263a3}'
         'section{margin:10px 0}.meta{font-size:13px;color:#555;margin:2px 0}.id{font-family:monospace}'
         '.segment{border-top:4px solid #555;padding-top:8px;margin-top:40px}.derived{border-left:4px solid #b26b00;'
         'padding-left:10px}.harness{color:#555}.record{border-left:4px solid #1263a3;padding-left:10px}'
         '.file{border-left:4px solid #5b8a3c;padding-left:10px}table{border-collapse:collapse}'
         'td,th{border:1px solid #ccc;padding:3px 8px;text-align:left}</style>')
COLLAPSE_CHARS = 3000   # longer results and records start collapsed (full text is always present)
LABELS = {"reasoning": "Reasoning (returned; untrusted self-report)", "text": "Visible text",
          "summary": "Compaction summary (derived self-report written by the agent's model)",
          "tool_call": "Tool call", "tool_result": "Tool result", "conference": "Conference record",
          "file_version": "Private file version", "runtime": "Harness input"}

esc = html.escape


def anchor(item_id):
    """Link to an item anchor (IDs may contain brackets, slashes and @)."""
    return f'<a class="id" href="#{quote(item_id, safe="")}">{esc(item_id)}</a>'


def _meta_line(item, extra=""):
    when = item.get("time") or (" to ".join(t or "?" for t in item["time_bounds"]) if item.get("time_bounds")
                                else "time unknown")
    channel = item.get("channel")
    return (f'<p class="meta">{anchor(item["id"])} · {esc(LABELS.get(item["kind"], item["kind"]))} · '
            f'channel {esc(str(channel))} · {esc(when)}{extra}</p>')


def _body(item, collapse_label=None, open_=False):
    text = item.get("text") or ""
    block = f'<pre>{esc(text)}</pre>'
    if collapse_label is None:
        return block
    return f'<details{" open" if open_ else ""}><summary>{esc(collapse_label)} ({len(text)} characters)</summary>{block}</details>'


def _part(item):
    """One turn part (reasoning, visible text, summary or tool call) as a section."""
    kind = item["kind"]
    if kind == "reasoning":
        body = _body(item, "Reasoning")
    elif kind == "summary":
        body = _body(item, "Compaction summary", open_=False)
    else:
        body = _body(item)
    css = ' class="derived"' if item["meta"].get("derived") else ""
    extra = ""
    if kind == "tool_call" and item["meta"].get("conference"):
        join = item["meta"]["conference"]
        extra = f' · conference {esc(join["status"])}' + (f' seq {join["seq"]}' if join.get("seq") else "")
    return f'<section id="{esc(item["id"])}"{css}>{_meta_line(item, extra)}{body}</section>'


def _result_article(item, number):
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
    body = _body(item, "Result", open_=False) if len(text) > COLLAPSE_CHARS else _body(item)
    message_id = meta.get("tool_message_id")
    small = f'<small>Message ID: {esc(str(message_id))}</small>' if message_id else ""
    prefix, _, call_number = item["id"].rpartition(".o")
    return (f'<article id="message-{number}"><h3>Result of {esc(prefix)}.c{esc(call_number)}</h3>{small}'
            f'<section id="{esc(item["id"])}">{_meta_line(item, extra)}{body}</section></article>')


def _record_article(item):
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
    body = _body(item, "Record", open_=False) if len(text) > COLLAPSE_CHARS else _body(item)
    return f'<article class="record"><section id="{esc(item["id"])}">{_meta_line(item, extra)}{body}</section></article>'


def _file_article(item):
    meta = item["meta"]
    extra = (f' · version {meta.get("version")} · {meta.get("bytes")} bytes · runtime line {meta.get("runtime_line")}'
             + (f' · integrity {esc(meta["integrity"])}' if meta.get("integrity") != "ok" else "")
             + (f' · same content as {esc(meta["duplicate_of"])}' if meta.get("duplicate_of") else ""))
    return (f'<article class="file"><section id="{esc(item["id"])}">{_meta_line(item, extra)}'
            f'{_body(item, meta.get("path") or "File")}</section></article>')


def _harness_article(item, number):
    meta = item["meta"]
    repeats = meta.get("repeat_turns") or []
    extra = f' · {esc(str(meta.get("role")))} · {esc(str(meta.get("harness_kind")))}'
    if repeats:
        extra += f' · repeated before {len(repeats)} later turn(s)'
    small = f'<small>Message ID: {esc(str(meta["message_id"]))}</small>' if meta.get("message_id") else ""
    label = "Harness input (token counters removed)"
    return (f'<article id="message-{number}" class="harness">{small}<section id="{esc(item["id"])}">'
            f'{_meta_line(item, extra)}{_body(item, label)}</section></article>')


def render_agent(history, run_id):
    """(HTML page, number of message articles) for one AgentHistory."""
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
                out.append(_record_article(item))
            elif item["kind"] == "file_version":
                out.append(_file_article(item))
            else:
                out.append(f'<article><section id="{esc(item["id"])}">{_meta_line(item)}{_body(item)}</section></article>')
            continue
        seg = members[0]["meta"].get("segment")
        if seg is not None and seg != segment:
            segment = seg
            info = segments.get(seg, {})
            out.append(f'<h2 class="segment" id="segment-{seg}">Segment {seg} · turns '
                       f'{info.get("first_turn", "?")}–{info.get("last_turn", "?")}</h2>')
        while harness and harness[0]["turn"] <= turn:
            number += 1
            out.append(_harness_article(harness.pop(0), number))
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
                       + ''.join(_part(i) for i in parts) + '</article>')
        for item in members:
            if item["kind"] == "tool_result":
                number += 1
                out.append(_result_article(item, number))
    for item in harness:
        number += 1
        out.append(_harness_article(item, number))

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
    table = '<table>' + ''.join(f'<tr><th>{esc(k)}</th><td>{esc(str(v))}</td></tr>' for k, v in rows) + '</table>'
    toc = ''.join(f'<li><a href="#segment-{s["segment"]}">Segment {s["segment"]}</a>: turns {s["first_turn"]}–'
                  f'{s["last_turn"]}' + (f' (summary {anchor("%s.t%04d.s" % (history.code, s["summary_turn"]))})'
                                         if s.get("summary_turn") is not None else '') + '</li>'
                  for s in history.segments)
    page = ('<!doctype html><meta charset="utf-8"><title>' + esc(history.agent_id) + '</title>' + STYLE +
            '<p><a href="index.html">All agents</a> · <a href="../conference-view/index.html">Conference</a></p>'
            f'<h1>{esc(history.agent_id)} ({esc(history.code)})</h1>'
            f'<p>Run {esc(run_id)}. Private researcher export: do not provide to experimental agents. Full '
            'reconstructed history across every compaction segment, shared with the transcript judge '
            '(research/analysis/judge/history.py). IDs are the judge\'s citation anchors. Returned reasoning '
            'is untrusted self-report; compaction summaries are derived from earlier turns. Tool-result times '
            'are the joined conference event time or the bounds between the issuing turn and the next request. '
            'Private-file times are capture times, not edit times. Times are UTC. Raw Inspect logs and '
            'conference records remain authoritative.</p>' + table + (f'<ul>{toc}</ul>' if toc else '') +
            ''.join(out))
    return page, number


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
    links, manifest = [], []
    for agent, record in sorted(built.agents.items()):
        page, messages = render_agent(record, built.run_id)
        name = agent + '.html'
        (out / name).write_text(page, encoding='utf-8')
        stats = record.stats
        links.append(f'<li><a href="{name}">{esc(agent)}</a> — {stats["turns"]} model turns, '
                     f'{stats["segments"]} segment(s), {stats["items"]} items, about {stats["tokens_total"]:,} tokens</li>')
        manifest.append({'agent_id': agent, 'messages': messages, 'source': record.source, 'output': name,
                         'inspect_error': bool(stats.get('inspect_error')), 'history_schema': judge_history.SCHEMA,
                         'turns': stats['turns'], 'summary_turns': stats['summary_turns'],
                         'segments': stats['segments'], 'items': stats['items'],
                         'items_by_kind': stats['items_by_kind'], 'tokens_estimate': stats['tokens_total'],
                         'harness_items': stats['harness_items'], 'exposure_entries': stats['exposure_entries'],
                         'missing_tool_results': len(stats['missing_tool_results'])})
    (out / 'index.html').write_text(
        '<!doctype html><meta charset="utf-8"><title>Agent transcripts</title>' + STYLE +
        '<h1>GDM Math Swarm agent transcripts</h1><p>Run ' + esc(built.run_id) + '. Private researcher exports; do not '
        'publish or provide to experimental agents. Each page shows the full reconstructed history across '
        'compactions (reasoning, tool calls and results, summaries, own conference records, private file '
        'versions), with judge citation IDs as anchors.</p><ul>' + ''.join(links) + '</ul>', encoding='utf-8')
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return {'agents': len(manifest), 'messages': sum(x['messages'] for x in manifest),
            'items': sum(x['items'] for x in manifest), 'index': str(out / 'index.html')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, help='default: RUN/transcripts')
    parser.add_argument('--agent', action='append', help='export only this agent (repeatable)')
    options = parser.parse_args()
    print(json.dumps(export(options.run_dir, options.output_dir, agents=options.agent)))
