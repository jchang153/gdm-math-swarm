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
import re
import sys
from urllib.parse import quote

if str(Path(__file__).resolve().parent) not in sys.path:  # shared site theme lives beside this script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_style import head as theme_head  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

STYLE = theme_head() + ('<style>body{max-width:1100px;margin:36px auto;padding:0 20px;font:16px system-ui;line-height:1.5}'
         'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--pre);padding:12px;margin:6px 0}'
         'article{border-top:1px solid var(--line);margin:22px 0}details{margin:8px 0}a{color:var(--accent)}'
         'section{margin:10px 0}.meta{font-size:13px;color:var(--muted);margin:2px 0}.id{font-family:monospace}'
         '.segment{border-top:4px solid var(--segment);padding-top:8px;margin-top:40px}.derived{border-left:4px solid var(--derived);'
         'padding-left:10px}.harness{color:var(--muted)}.record{border-left:4px solid var(--record);padding-left:10px}'
         '.file{border-left:4px solid var(--file);padding-left:10px}table{border-collapse:collapse;max-width:100%}'
         'td,th{border:1px solid var(--line);padding:3px 8px;text-align:left;overflow-wrap:anywhere}</style>')
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


# ----------------------------------------------------------------------------- agentic monitors (arm AG)
# docs/plan.md, "Agentic monitor arm AG (owner decisions, November 23, 2026)": one monitor agmon-NNN per solver
# agent-NNN. Its transcript is rendered from its Inspect sample's messages (else from Claude Code session JSONL
# files under RUN/agentic_monitor/ or RUN/agentic-monitor/) on its own page, linked from its solver's page.
# Runs without monitors are exported exactly as before.
MONITOR_PREFIX = "agmon-"
MONITOR_DIRS = ("agentic_monitor", "agentic-monitor")
MONITOR_NAME = re.compile(r"agmon-\d+(?!\d)")  # a session file or folder named after its monitor


def solver_of(monitor_id):
    """agmon-007 -> agent-007 (None for anything else)."""
    text = str(monitor_id)
    if not text.startswith(MONITOR_PREFIX) or not text[len(MONITOR_PREFIX):].isdigit():
        return None
    return "agent-" + text[len(MONITOR_PREFIX):]


def monitor_of(agent_id):
    """agent-007 -> agmon-007 (None for anything else)."""
    text = str(agent_id)
    if not text.startswith("agent-") or not text[len("agent-"):].isdigit():
        return None
    return MONITOR_PREFIX + text[len("agent-"):]


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _arguments_text(arguments):
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        return str(arguments)


def normalize_message(message):
    """One chat message (an Inspect ChatMessage or its dict form) as {role, text, reasoning, tool_calls,
    tool_call_id, function, error}."""
    content = _get(message, "content")
    texts, reasoning, calls = [], [], []
    if isinstance(content, str):
        texts.append(content)
    else:
        for part in content or []:
            kind = _get(part, "type")
            if kind == "text":
                texts.append(str(_get(part, "text") or ""))
            elif kind == "reasoning":
                reasoning.append(str(_get(part, "reasoning") or _get(part, "summary") or
                                     ("(redacted)" if _get(part, "redacted") else "")))
            elif kind == "tool_use":
                calls.append({"id": _get(part, "id"), "function": _get(part, "name"),
                              "arguments": _arguments_text(_get(part, "arguments"))})
            else:
                texts.append(f"[{kind} content]")
    for call in _get(message, "tool_calls") or []:
        calls.append({"id": _get(call, "id"), "function": _get(call, "function"),
                      "arguments": _arguments_text(_get(call, "arguments"))})
    error = _get(message, "error")
    if error is not None and not isinstance(error, (str, dict)):
        error = {"type": _get(error, "type"), "message": _get(error, "message")}
    return {"role": str(_get(message, "role") or "unknown"), "text": "\n".join(t for t in texts if t),
            "reasoning": "\n".join(r for r in reasoning if r), "tool_calls": calls,
            "tool_call_id": _get(message, "tool_call_id"), "function": _get(message, "function"), "error": error}


def session_messages(path):
    """Normalized messages of a Claude Code session JSONL file (user/assistant records; tool_use and tool_result
    blocks), skipping unreadable lines and other record types."""
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if record.get("isCompactSummary"):
            out.append({"role": "summary", "text": content if isinstance(content, str) else
                        "\n".join(str(b.get("text") or "") for b in content or [] if isinstance(b, dict)),
                        "reasoning": "", "tool_calls": [], "tool_call_id": None, "function": None, "error": None})
            continue
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else \
            [b for b in content or [] if isinstance(b, dict)]
        role = str(message.get("role") or record.get("type") or "unknown")
        pending = {"role": role, "text": "", "reasoning": "", "tool_calls": [], "tool_call_id": None,
                   "function": None, "error": None}
        texts, thoughts = [], []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text") or ""))
            elif kind == "thinking":
                thoughts.append(str(block.get("thinking") or ""))
            elif kind == "tool_use":
                pending["tool_calls"].append({"id": block.get("id"), "function": block.get("name"),
                                              "arguments": _arguments_text(block.get("input"))})
            elif kind == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    result = "\n".join(str(b.get("text") or "") for b in result if isinstance(b, dict))
                out.append({"role": "tool", "text": str(result or ""), "reasoning": "", "tool_calls": [],
                            "tool_call_id": block.get("tool_use_id"), "function": None,
                            "error": "tool error" if block.get("is_error") else None})
        pending.update(text="\n".join(t for t in texts if t), reasoning="\n".join(t for t in thoughts if t))
        if pending["text"] or pending["reasoning"] or pending["tool_calls"]:
            out.append(pending)
    return out


def render_monitor(monitor_id, messages, run_id, source, solver_page=None):
    """(HTML page, number of message articles) of one agentic monitor's normalized messages."""
    out, names = [], {}
    for number, m in enumerate(messages, 1):
        role = m["role"]
        parts = []
        if m["reasoning"]:
            parts.append(_body({"text": m["reasoning"]}, "Reasoning (returned; untrusted self-report)"))
        if m["text"]:
            text_item = {"text": m["text"]}
            parts.append(_body(text_item, "Text", open_=False) if len(m["text"]) > COLLAPSE_CHARS
                         else _body(text_item))
        for call in m["tool_calls"]:
            names[call.get("id")] = call.get("function")
            parts.append(f'<section class="meta">Tool call <span class="id">{esc(str(call.get("id")))}</span>: '
                         f'<strong>{esc(str(call.get("function")))}</strong></section>'
                         + _body({"text": call.get("arguments") or ""}))
        title = {"system": "System prompt", "user": "Input", "assistant": "Monitor turn", "tool": "Tool result",
                 "summary": "Compaction summary (derived self-report)"}.get(role, role)
        if role == "tool":
            function = m.get("function") or names.get(m.get("tool_call_id"))
            title += f' · {function}' if function else ''
            title += f' · call {m["tool_call_id"]}' if m.get("tool_call_id") else ''
        if m.get("error"):
            title += ' · error ' + (m["error"] if isinstance(m["error"], str) else json.dumps(m["error"], ensure_ascii=False))
        css = ' class="harness"' if role in ("system", "user") else ' class="derived"' if role == "summary" else ''
        out.append(f'<article id="message-{number}"{css}><h3>{esc(title)}</h3>' + ''.join(parts) + '</article>')
    back = (f' · <a href="{esc(solver_page, quote=True)}">Solver: {esc(solver_page[:-5])}</a>' if solver_page else '')
    calls = sum(len(m["tool_calls"]) for m in messages)
    page = ('<!doctype html><meta charset="utf-8"><title>' + esc(monitor_id) + '</title>' + STYLE +
            '<p><a href="index.html">All agents</a>' + back + '</p>'
            f'<h1>{esc(monitor_id)} (agentic monitor)</h1>'
            f'<p>Run {esc(run_id)}. Private researcher export: do not provide to experimental agents. The transcript '
            f'of the agentic monitor of {esc(solver_of(monitor_id) or "?")}, external to the swarm and undisclosed '
            'to the solvers (arm AG): its instructions, the solver activity pushed to it and its tool calls (wait, '
            'revert_submission, list_revertible, get_output, message_solver) with their results. Returned reasoning '
            'is untrusted self-report. Source: ' + esc(str(source)) + '. When the source is an Inspect sample, this '
            'is its final message list; earlier context removed by compaction is in the raw log.</p>'
            f'<table><tr><th>Messages</th><td>{len(messages)}</td></tr><tr><th>Tool calls</th><td>{calls}</td></tr>'
            '</table>' + ''.join(out))
    return page, len(messages)


def monitor_sources(run, history_module=None, wanted=None):
    """{agmon-NNN: ('inspect', (path, sample id, epoch)) | ('session', [jsonl paths])}: Inspect samples first,
    else Claude Code session files under RUN/agentic_monitor/ or RUN/agentic-monitor/."""
    found = {}
    if history_module is not None:
        for monitor, ref in history_module.monitor_sample_index(run).items():
            found[monitor] = ("inspect", ref)
    for name in MONITOR_DIRS:
        folder = Path(run) / name
        if not folder.is_dir() or folder.is_symlink():
            continue
        for path in sorted(folder.rglob("*.jsonl")):
            if path.is_symlink():
                continue
            hits = [MONITOR_NAME.match(part) for part in (path.stem, *path.relative_to(folder).parts[:-1])]
            monitor = next((h[0] for h in hits if h), None)
            if monitor is None:
                continue
            if found.get(monitor, ("session",))[0] == "session":
                found.setdefault(monitor, ("session", []))[1].append(path)
    return {k: v for k, v in sorted(found.items()) if wanted is None or k in wanted}


def monitor_messages(run, kind, ref):
    if kind == "inspect":
        path, sample_id, epoch = ref
        from research.analysis.judge import history as judge_history
        read_sample, _ = judge_history._ensure_inspect()
        sample = read_sample(str(path), id=sample_id, epoch=epoch, resolve_attachments=True)
        source = str(Path(path).relative_to(run)) if Path(path).is_relative_to(run) else Path(path).name
        return [normalize_message(m) for m in sample.messages or []], source
    messages = []
    for path in ref:
        messages.extend(session_messages(path))
    return messages, ", ".join(str(p.relative_to(run)) for p in ref)


NAV = '<p><a href="index.html">All agents</a> · <a href="../conference-view/index.html">Conference</a></p>'


def with_monitor_link(page, monitor_id):
    """A solver page with a link to its agentic monitor's page in the top navigation."""
    link = f' · <a href="{esc(monitor_id)}.html">Agentic monitor: {esc(monitor_id)}</a></p>'
    return page.replace(NAV, NAV[:-len('</p>')] + link, 1)


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
    wanted_monitors = None if agents is None else {a for a in agents if solver_of(a)}
    solver_agents = None if agents is None else [a for a in agents if not solver_of(a)]
    built = judge_history.build_run(run, agents=solver_agents)
    monitors = monitor_sources(run, judge_history, wanted_monitors)
    out = Path(out_dir).resolve() if out_dir else run / 'transcripts'
    out.mkdir(parents=True, exist_ok=True)
    links, manifest = [], []
    monitor_links = {}
    for monitor, (kind, ref) in monitors.items():
        messages, source = monitor_messages(run, kind, ref)
        solver = solver_of(monitor)
        page, count = render_monitor(monitor, messages, built.run_id, source,
                                     solver + '.html' if solver in built.agents else None)
        (out / (monitor + '.html')).write_text(page, encoding='utf-8')
        calls = sum(len(m['tool_calls']) for m in messages)
        monitor_links[solver] = (f'<li><a href="{monitor}.html">{esc(monitor)}</a> — agentic monitor of '
                                 f'{esc(solver)}: {count} messages, {calls} tool calls</li>')
        manifest.append({'agent_id': monitor, 'role': 'agentic_monitor', 'solver': solver, 'messages': count,
                         'source': source, 'output': monitor + '.html', 'tool_calls': calls})
    monitor_manifest, manifest = manifest, []
    for agent, record in sorted(built.agents.items()):
        page, messages = render_agent(record, built.run_id)
        if agent in monitor_links:
            page = with_monitor_link(page, monitor_of(agent))
        name = agent + '.html'
        (out / name).write_text(page, encoding='utf-8')
        stats = record.stats
        links.append(f'<li><a href="{name}">{esc(agent)}</a> — {stats["turns"]} model turns, '
                     f'{stats["segments"]} segment(s), {stats["items"]} items, about {stats["tokens_total"]:,} tokens</li>')
        if agent in monitor_links:
            links.append(monitor_links.pop(agent))
        manifest.append({'agent_id': agent, 'messages': messages, 'source': record.source, 'output': name,
                         'inspect_error': bool(stats.get('inspect_error')), 'history_schema': judge_history.SCHEMA,
                         'turns': stats['turns'], 'summary_turns': stats['summary_turns'],
                         'segments': stats['segments'], 'items': stats['items'],
                         'items_by_kind': stats['items_by_kind'], 'tokens_estimate': stats['tokens_total'],
                         'harness_items': stats['harness_items'], 'exposure_entries': stats['exposure_entries'],
                         'missing_tool_results': len(stats['missing_tool_results'])})
    links.extend(monitor_links[k] for k in sorted(monitor_links))  # monitors whose solver was not exported
    manifest.extend(monitor_manifest)
    (out / 'index.html').write_text(
        '<!doctype html><meta charset="utf-8"><title>Agent transcripts</title>' + STYLE +
        '<h1>GDM Math Swarm agent transcripts</h1><p>Run ' + esc(built.run_id) + '. Private researcher exports; do not '
        'publish or provide to experimental agents. Each page shows the full reconstructed history across '
        'compactions (reasoning, tool calls and results, summaries, own conference records, private file '
        'versions), with judge citation IDs as anchors.</p><ul>' + ''.join(links) + '</ul>', encoding='utf-8')
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    solvers = [x for x in manifest if x.get('role') != 'agentic_monitor']
    result = {'agents': len(solvers), 'messages': sum(x['messages'] for x in solvers),
              'items': sum(x['items'] for x in solvers), 'index': str(out / 'index.html')}
    if monitor_manifest:
        result['agentic_monitors'] = len(monitor_manifest)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, help='default: RUN/transcripts')
    parser.add_argument('--agent', action='append', help='export only this agent or agentic monitor agmon-NNN (repeatable)')
    options = parser.parse_args()
    print(json.dumps(export(options.run_dir, options.output_dir, agents=options.agent)))
