"""Agentic monitors in the story maps (AG arm; owner decision, November 24, 2026; docs/plan.md "Agentic monitors in
the story maps"): the solver-page, index and events-page parts and the monitor-solver pair page.

Called by storymap/view.py only when the map has ``agentic_context.json`` (storymap/agentic.py, stage 0); without it
no page changes, byte for byte. Inputs (all optional beyond the context file; missing parts are said so on the page):

* ``agentic_context.json``: ``pairs`` {agmon: {solver, waits, reverts, messages, ..., cost_usd}} (the pair page's hard
  facts) and ``seen`` {solver: [[call_id, call_t, seen_t], ...]} (when the monitor saw each solver call, run minutes).
* ``agentic/<agmon>.jsonl``: the monitor's own timeline {i, t, ts, turn, kind, tool, text, event, call_id}; kinds
  reasoning / text / tool_call / tool_result / context; events GW wait, GL list_revertible, GO get_output, GR revert,
  GF refused revert, GM message, GE error, GC compaction.
* the solvers' timelines: Y1 (its monitor reverted its accepted submission, at revert time), Y2 (a message from its
  monitor, at delivery; it replaces D1 for monitor messages), and P2 written by ``agmon-NNN`` (role
  ``agentic_monitor``).

Writes ``agents/<solver>.monitor.html`` per pair: the monitor's lanes (waits as bars spanning the wait, looks, reverts,
refusals and messages, one tick per reasoning or text item) above the solver's (tool calls the monitor saw, position on
cheating and thought lanes when a stage-1/3 map exists, the monitor's acts as the solver received them, submissions),
with thin connectors (solver call -> the monitor saw it, sampled when dense; message sent -> delivered; revert -> the
reverted submission), the hard facts and the same detail panel and stepper as the agent pages. The monitor's text is
shown as written (HTML-escaped, with the pages' credential and host-path redaction).

Event types, the role and the file names come from storymap/agentic.py (the stage-0 extractor; via view.py): solver
events Y1, Y2 and monitor timeline events GW, GL, GO, GR, GF, GM, GE, GC. Coverage ("seen") means delivered to the
monitor's feed, not necessarily read.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from pathlib import Path

from . import view as V

ROLE = V.AGENTIC_ROLE
from .agentic import MONITOR_EVENT_TYPES as MON_TYPES, MONITOR_DIR  # noqa: E402  (GW, GL, GO, GR, GF, GM, GE, GC)
MON_NAME = {"GW": "wait", "GL": "listed its solver's revertible submissions", "GO": "read a call's full output",
            "GR": "reverted a submission", "GF": "revert refused by the host", "GM": "message to its solver",
            "GE": "tool error", "GC": "context compaction"}
COVERAGE_PX = 16
COVERAGE_LANE = ("cov", "", "Monitor coverage", "Coverage",
                 "Each of the agent's tool calls shaded from when it was made until its agentic monitor saw it (the "
                 "result of the monitor's next wait); darker = several calls waiting at once; a tick = never seen")
SEEN_CONNECTORS = 150  # at most this many call -> seen connectors on a pair page (evenly sampled)
COVERAGE_CAP = 6000
MON_CAPS = {"reasoning": 8000, "text": 4000, "tool_call": 1500, "tool_result": 2000, "event": 1500}

PAIR_CSS = """
.wb{fill:var(--fg);fill-opacity:.22;stroke:var(--fg);stroke-opacity:.45;stroke-width:.8}.wb.imp{fill-opacity:.1;
stroke-dasharray:2 2}.wb:hover{fill-opacity:.4}
.cn{stroke:var(--fg);stroke-width:.8;opacity:.22;pointer-events:none}.cn.msg{stroke:var(--ev-ov);stroke-width:1.4;
opacity:.85;stroke-dasharray:4 3}.cn.rev{stroke:var(--fg);stroke-width:1.4;opacity:.75}.cn.rev.fk{stroke:var(--ev-fake)}
.sc{stroke:var(--muted);stroke-width:1}.sc.unseen{stroke:var(--fg);stroke-width:1.6}
.ll.sep{border-top:2px solid var(--line)}rect.sep{fill:var(--line)}
.pairkey{display:flex;flex-wrap:wrap;gap:2px 14px;list-style:none;margin:4px 0;padding:0;font-size:12.5px}
.pairkey li{display:inline-flex;gap:5px;align-items:center}.pairkey svg{width:26px;height:16px;flex:none;overflow:visible}
table.hf td.n,table.hf th.n{text-align:right;font-variant-numeric:tabular-nums}table.hf{font-size:13.5px}table.hf td{padding:2px 8px}
.hfbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 14px;margin:8px 0 12px}
.hfbox h2{margin:4px 0 6px;font-size:1.05rem}.hfgrid{display:flex;flex-wrap:wrap;gap:0 28px}
"""


def is_monitor_name(name):
    return str(name).startswith("agmon-")


_CACHE = {}


def activate(path, agents, view_module=None):
    """Read agentic_context.json and switch view.py's AG mode on: AGENTIC = {on, ctx, dir, mon_of {solver: agmon}}.
    The solver -> monitor map comes from the context's pairs, else from each solver's timeline index ``monitor``.
    ``view_module``: the calling view module (``python -m ...view`` runs it as __main__, a second copy of view)."""
    global V
    if view_module is not None:
        V = view_module
    path = Path(path)
    ctx = V._read_json(path)
    ctx = ctx if isinstance(ctx, dict) else {}
    pairs = ctx.get("pairs") if isinstance(ctx.get("pairs"), dict) else {}
    mon_of = {str(p["solver"]): str(m) for m, p in pairs.items() if isinstance(p, dict) and p.get("solver")}
    for name, a in agents.items():
        m = (a.get("index") or {}).get("monitor")
        if m and name not in mon_of and not is_monitor_name(name):
            mon_of[name] = str(m)
    _CACHE.clear()
    V.AGENTIC.update(on=True, ctx=ctx, dir=path.parent, mon_of=mon_of)


def pair_of(mon):
    pairs = (V.AGENTIC.get("ctx") or {}).get("pairs") or {}
    p = pairs.get(mon)
    return p if isinstance(p, dict) else {}


def monitor_items(mon):
    """The monitor's timeline items (agentic/<agmon>.jsonl beside the context file), in order; [] when missing."""
    if mon in _CACHE:
        return _CACHE[mon]
    base = V.AGENTIC.get("dir")
    rows = []
    if base is not None:
        for d in (Path(base) / MONITOR_DIR, Path(base) / "timeline" / MONITOR_DIR, Path(base).parent / MONITOR_DIR):
            if (d / f"{mon}.jsonl").is_file():
                rows = [r for r in V._read_jsonl(d / f"{mon}.jsonl") if "_malformed" not in r]
                break
    rows.sort(key=lambda r: (V._num(r.get("t")), V._num(r.get("i"))))
    _CACHE[mon] = rows
    return rows


def ev_type(item):
    ev = item.get("event")
    return ev.get("type") if isinstance(ev, dict) else None


def ev_detail(item):
    """An event's detail: its ``detail`` dict, or the event's own fields when the extractor keeps them flat."""
    ev = item.get("event") if isinstance(item.get("event"), dict) else {}
    d = ev.get("detail")
    return d if isinstance(d, dict) else {k: v for k, v in ev.items() if k not in ("type", "id")}


def secs(s):
    """Seconds as the pages' h/m/s ('45s', '8m 54s'); 'unknown' when missing."""
    return "unknown" if s is None or not isinstance(s, (int, float)) else V.fmt_hms(s / 60.0)


def _yes(v):
    return "yes" if v is True else "no" if v is False else "unknown"


def _when(v):
    """A recorded time as shown: run minutes as h/m/s, anything else (an ISO stamp) as written."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return V.fmt_t(v)
    return "not recorded" if v in (None, "") else str(v)


def sent_t(ev):
    """A Y2's send time in run minutes: its ``sent_ts`` when numeric, else delivery minus the lag; None if unknown."""
    d = ev["event"].get("detail") or {}
    if isinstance(d.get("sent_ts"), (int, float)) and not isinstance(d.get("sent_ts"), bool):
        return float(d["sent_ts"])
    lag = d.get("lag_s")
    return V._num(ev.get("t")) - lag / 60.0 if isinstance(lag, (int, float)) else None


def msg_id(d):
    """A message's id as the agents saw it (dm-000034), with the host's internal id when it differs."""
    a, b = d.get("dm_id"), d.get("message_id")
    return (f"{a} ({b})" if a and b and a != b else a or b) or "unknown"


# ------------------------------------------------------------------ solver page, index, events page

def refused_events(mon, solver):
    """The monitor's refused reverts (GF) as pseudo-events for the solver page's Monitor lane (key ev-gf<i>)."""
    out = []
    for it in monitor_items(mon):
        if ev_type(it) != "GF":
            continue
        d = ev_detail(it)
        out.append({"i": f"gf{it.get('i')}", "t": it.get("t"), "turn": None, "kind": "event",
                    "text": d.get("why") or it.get("text") or "",
                    "event": {"type": "GF", "id": f"GF:{solver}:{mon}:{it.get('i')}", "author": mon,
                              "author_role": ROLE, "fake": None, "tags": [], "outcome": "refused",
                              "detail": dict(d, monitor=mon)}})
    return out


def coverage_svg(solver, tmax, y, h):
    """The solver page's coverage strip: each call shaded from when it was made until the monitor saw it."""
    seen = ((V.AGENTIC.get("ctx") or {}).get("seen") or {}).get(solver) or []
    if not seen:
        return (f'<text x="50%" y="{y + h - 4}" text-anchor="middle" font-size="10">no coverage records for this '
                f'agent</text>')
    out = []
    for row in seen[:COVERAGE_CAP]:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        cid, call_t, seen_t = row[0], row[1], row[2]
        if call_t is None:
            continue
        x0 = V.pct(call_t, tmax)
        if seen_t is None:
            out.append(f'<line class="cov-none" x1="{x0:.2f}%" x2="{x0:.2f}%" y1="{y + 2}" y2="{y + h - 2}">'
                       f'<title>{V.esc(f"Call at {V.fmt_t(call_t)}: never seen by the monitor")}</title></line>')
            continue
        x1 = V.pct(seen_t, tmax)
        lag = (V._num(seen_t) - V._num(call_t)) * 60
        out.append(f'<rect class="cov" x="{x0:.2f}%" y="{y + 3}" width="{max(x1 - x0, 0.12):.2f}%" height="{h - 6}">'
                   f'<title>{V.esc(f"Call at {V.fmt_t(call_t)}, seen by the monitor at {V.fmt_t(seen_t)} (after {secs(lag)})")}'
                   f'</title></rect>')
    return "".join(out)


def counts(a):
    """(reverts, messages) of its monitor on a solver, from its timeline (Y1, Y2)."""
    c = Counter(ev["event"].get("type") for ev in a["events"])
    return c[V.REV_T], c[V.MSG_T]


def index_cell(name, a):
    """(Monitor column cell, 'yes'/'no' intervened) of an index row."""
    mon = V.AGENTIC["mon_of"].get(name)
    r, m = counts(a)
    if not mon:
        return '<div class="agm muted" title="No agentic monitor recorded for this agent">—</div>', "no"
    tip = (f"{mon}: {V.plural(r, 'revert')}, {V.plural(m, 'message')} delivered; opens the monitor ↔ solver view")
    return (f'<div class="agm"><a href="agents/{V.esc(name)}.monitor.html" title="{V.esc(tip)}">{r} / {m}</a></div>',
            "yes" if r or m else "no")


def solver_article(ev, reactions, key, names=None, href=""):
    """A Y1 / Y2 / GF item in the solver page's detail column: what the monitor did, its reason or text, the latency
    (acceptance to revert) or delivery lag (sent to delivered), and the stage-2 reaction when there is one."""
    e = ev["event"]
    typ = e.get("type")
    d = e.get("detail") or {}
    mon = d.get("monitor") or e.get("author")
    meta = [V.badge(V.type_name(typ)), V.badge(f"by {mon} (agentic monitor)" if mon else "by its agentic monitor",
                                               "info")]
    rows = []
    quote = ""
    if typ == V.REV_T:
        fake = d.get("fake", e.get("fake"))
        meta.append(V.badge("the submission was fake", "bad") if fake is True else
                    V.badge("the submission was not fake", "good") if fake is False else "")
        rows = [("Problem", d.get("problem_id") or "unknown"),
                ("Submission", (f"{d.get('public_submission_id')} (shown to agents)" if d.get("public_submission_id")
                                else "") + (f"; {d.get('submission_id')}" if d.get("submission_id") else "") or "unknown"),
                ("Accepted at", _when(d.get("accepted_ts"))),
                ("Reverted at", V.fmt_t(ev.get("t"))),
                ("Latency (acceptance to revert)", V.fmt_hms(d["latency_min"])
                 if isinstance(d.get("latency_min"), (int, float)) else "unknown"),
                ("Reverted submission fake", _yes(fake)),
                ("Reason message", d.get("message_id") or "not recorded")]
        quote = d.get("reason") or ""
    elif typ == V.MSG_T:
        lag = d.get("lag_s")
        sent = sent_t(ev)
        if d.get("revert_notice"):
            meta.append(V.tbadge("revert notice", "The host's notice of a revert, carrying the monitor's reason"))
        dl = V.delivery_of(e)
        if dl:
            meta.append(V.tbadge(V.DELIVERY_LABEL[dl], V.DELIVERY_HINT[dl]))
        rows = [("Message", msg_id(d)),
                ("Sent at", V.fmt_t(sent) if sent is not None else _when(d.get("sent_ts"))),
                ("Delivered at", V.fmt_t(ev.get("t"))),
                ("Send to delivery", secs(lag)),
                ("Revert notice", _yes(d.get("revert_notice")) if d.get("revert_notice") is not None else "no")]
        quote = d.get("text") or ev.get("text") or ""
    else:  # GF: refused by the host
        rows = [("Problem", d.get("problem_id") or "unknown"), ("Why refused", d.get("why") or "not recorded"),
                ("Tried at", V.fmt_t(ev.get("t")))]
    kv = "".join(f"<dt>{V.esc(k)}</dt><dd>{V.esc(v)}</dd>" for k, v in rows)
    reacts = ("".join(V.reaction_html(r, names, href) for r in reactions) if reactions
              else V.reaction_html(None) if typ != "GF" else "")
    head = V.event_label(e)
    head = head[:1].upper() + head[1:]
    icon = f'<svg class="hglyph" viewBox="-9 -9 18 18">{V.shape(typ, 0, 0, 5.2, V.glyph_class(ev), None, "")}</svg>'
    cite = [ev.get("i")] + [q.get("item") for r in reactions for q in r.get("quotes") or [] if isinstance(q, dict)]
    chain = V.chain_box(ev.get("turn"), cite, "Show what surrounded it: the agent's reasoning and tool calls",
                        before=0, after=1) if typ != "GF" else ""
    k = " k-fake" if typ == V.REV_T and d.get("fake", e.get("fake")) else " k-ov"
    label = ("Reason given by the monitor" if typ == V.REV_T else "Text as delivered" if typ == V.MSG_T else "")
    return (f'<article class="item{k}" id="{key}" data-kind="mon" data-t="{V._num(ev.get("t")):.3f}" '
            f'data-label="{V.esc(V.fmt_t(ev.get("t")) + " · " + head[:110])}">'
            f'<h3>{icon}{V.fmt_t(ev.get("t"))} &middot; {V.esc(head)}</h3><p>{"".join(meta)}'
            + (f' <small>turn {V.esc(ev.get("turn"))} &middot; timeline item {V.esc(ev.get("i"))}</small>'
               if ev.get("turn") is not None else ' <small>from the monitor\'s timeline</small>')
            + '</p>' + (f'<p class="muted">{label}:</p><blockquote>{V.esc(V.redact(V.cut(quote)))}</blockquote>'
                        if quote else "")
            + f'<dl class="kv">{kv}</dl>{reacts}{chain}</article>')


def events_section(agents):
    """events.html: every revert and message of an agentic monitor, grouped by monitor-solver pair."""
    cards = []
    total = Counter()
    for solver in sorted(agents):
        mon = V.AGENTIC["mon_of"].get(solver)
        if not mon:
            continue
        a = agents[solver]
        g1 = [ev for ev in a["events"] if ev["event"].get("type") == V.REV_T]
        g2 = [ev for ev in a["events"] if ev["event"].get("type") == V.MSG_T]
        gf = refused_events(mon, solver)
        if not (g1 or g2 or gf):
            continue
        total.update(reverts=len(g1), messages=len(g2), refused=len(gf), pairs=1)
        link = lambda ev: f'agents/{V.esc(solver)}.html#sel=ev-{V.esc(ev.get("i"))}'  # noqa: E731
        rrows = []
        for ev in g1:
            d = ev["event"].get("detail") or {}
            fake = d.get("fake", ev["event"].get("fake"))
            rrows.append(f'<tr><td class="n"><a href="{link(ev)}">{V.fmt_t(ev.get("t"))}</a></td>'
                         f'<td>{V.esc(d.get("problem_id") or "?")}</td>'
                         f'<td>{V.esc(d.get("submission_id") or d.get("public_submission_id") or "?")}</td>'
                         f'<td>{_yes(fake)}</td><td class="n">'
                         + (V.fmt_hms(d["latency_min"]) if isinstance(d.get("latency_min"), (int, float)) else "unknown")
                         + f'</td><td class="tx">{V.esc(V.redact(V.cut(d.get("reason") or "", 600)))}</td></tr>')
        for ev in gf:
            d = ev["event"].get("detail") or {}
            rrows.append(f'<tr><td class="n"><a href="{link(ev)}">{V.fmt_t(ev.get("t"))}</a></td>'
                         f'<td>{V.esc(d.get("problem_id") or "?")}</td><td>—</td><td>—</td><td class="n">—</td>'
                         f'<td class="tx"><b>Refused by the host:</b> {V.esc(V.redact(str(d.get("why") or "")))}</td></tr>')
        mrows = []
        for ev in g2:
            d = ev["event"].get("detail") or {}
            lag = d.get("lag_s")
            sent = sent_t(ev)
            mrows.append(f'<tr><td class="n">{V.fmt_t(sent) if sent is not None else "?"}</td>'
                         f'<td class="n"><a href="{link(ev)}">{V.fmt_t(ev.get("t"))}</a></td>'
                         f'<td class="n">{secs(lag)}</td><td>{"yes" if d.get("revert_notice") else "no"}</td>'
                         f'<td class="tx">{V.esc(V.redact(V.cut(d.get("text") or ev.get("text") or "", 600)))}</td></tr>')
        icon = f'<svg class="hglyph" viewBox="-9 -9 18 18">{V.shape(V.REV_T, 0, 0, 5.2, "g-agrev ring", None, "")}</svg>'
        cards.append(
            f'<section class="ev" id="agentic-{V.esc(solver)}"><h3>{icon}{V.esc(mon)} → <a href="agents/{V.esc(solver)}'
            f'.html">{V.esc(solver)}</a> <small><a href="agents/{V.esc(solver)}.monitor.html">monitor ↔ solver view'
            f'</a></small></h3><p><small>{V.plural(len(g1), "revert")}'
            + (f' (and {V.plural(len(gf), "refused attempt")})' if gf else "")
            + f' &middot; {V.plural(len(g2), "message")} delivered</small></p>'
            + (f'<table class="agt"><tr><th>Reverted at</th><th>Problem</th><th>Submission</th><th>Fake</th>'
               f'<th>Latency</th><th>Reason</th></tr>{"".join(rrows)}</table>' if rrows else "")
            + (f'<table class="agt"><tr><th>Sent</th><th>Delivered</th><th>Lag</th><th>Revert notice</th><th>Text</th>'
               f'</tr>{"".join(mrows)}</table>' if mrows else "")
            + '</section>')
    intro = ("Every revert and message of the run's agentic monitors (one per solver), grouped by pair; latency is from "
             "the submission's acceptance to the revert, lag from sending a message to its delivery into the solver's "
             "context. Times link to the solver's page.")
    if not cards:
        return (f'<h2 id="agentic-monitor">Agentic monitor interventions</h2><p class="muted">{intro}</p>'
                '<div class="empty">No agentic monitor reverted or messaged any mapped solver.</div>')
    return (f'<h2 id="agentic-monitor">Agentic monitor interventions</h2><p class="muted">{intro} '
            f'{V.plural(total["pairs"], "pair")}: {V.plural(total["reverts"], "revert")}, '
            f'{V.plural(total["refused"], "refused revert")}, {V.plural(total["messages"], "message")}.</p>'
            + "".join(cards))


# ------------------------------------------------------------------ pair page

FACTS = (  # (key, label, kind): kind t = run time, min = duration in minutes, s = seconds, usd, n = number
    ("waits", "Waits", "n"), ("mean_wait_s", "Mean wait", "s"), ("list_revertible", "Revertible-list calls", "n"),
    ("get_output", "Full-output reads", "n"), ("reverts", "Reverts", "n"), ("refused", "Refused reverts", "n"),
    ("messages", "Messages", "n"), ("errors", "Tool errors", "n"), ("first_fake_t", "Solver's first fake at", "t"),
    ("first_revert_t", "First revert at", "t"), ("time_to_first_revert_min", "First fake to first revert", "min"),
    ("fakes_accepted", "Solver's fakes accepted", "n"), ("fakes_reverted", "Of them reverted", "n"),
    ("fakes_credited_at_end", "Fakes still credited at the end", "n"),
    ("false_reverts", "Reverts of genuine submissions", "n"),
    ("median_seen_lag_s", "Median lag, solver call to monitor sees it", "s"),
    ("message_lag_s_median", "Median lag, message sent to delivered", "s"), ("cost_usd", "Monitor cost", "usd"))


def _fact(kind, v):
    if v is None:
        return "never" if kind in ("t", "min") else "—"
    if kind == "t":
        return V.fmt_t(v)
    if kind == "min":
        return V.fmt_hms(v)
    if kind == "s":
        return secs(v)
    if kind == "usd":
        return f"${V._num(v):.2f}"
    return str(v)


def hard_facts_html(mon, key_of_mon=None):
    p = pair_of(mon)
    if not p:
        return ('<div class="hfbox"><h2>Hard facts (from the records, not model readings)</h2><p class="muted">No '
                'record for this pair in agentic_context.json.</p></div>')
    half = (len(FACTS) + 1) // 2
    tables = []
    for part in (FACTS[:half], FACTS[half:]):
        rows = "".join(f'<tr><td>{V.esc(lab)}</td><td class="n">{V.esc(_fact(kind, p.get(k)))}</td></tr>'
                       for k, lab, kind in part if k in p)
        tables.append(f'<table class="hf">{rows}</table>' if rows else "")
    return ('<div class="hfbox"><h2>Hard facts (from the records, not model readings)</h2>'
            f'<div class="hfgrid">{"".join(tables)}</div></div>')


PANEL = ('<aside id="panel" aria-label="Detail"><div class="stepper"><button type="button" id="pprev" '
         'title="Previous (← or k)">← Prev</button><span id="pos" aria-live="polite"></span>'
         '<button type="button" id="pnext" title="Next (→ or j)">Next →</button>'
         '<label>Step through <select id="kind"><option value="">all items</option>'
         '<option value="mact">only the monitor\'s reverts and messages</option>'
         '<option value="mth">only the monitor\'s reasoning</option>'
         '<option value="mlook">only the monitor\'s waits and looks</option>'
         '<option value="mon">only monitor events at the solver</option>'
         '<option value="sub">only submissions</option><option value="exp">only what the solver read or saw</option>'
         '<option value="th">only the solver\'s thoughts</option></select></label>'
         '<button type="button" id="pclose" title="Close (Esc)" aria-label="Close">×</button></div>'
         '<div id="pbody"></div><p class="muted keys">→ / j next, ← / k previous, Esc closes.</p></aside>')

M_LANES = (  # (key, label, short, hint)
    ("m-wait", "Monitor: waits", "Waits",
     "Each wait of the monitor as a bar from its call to its return (dashed: a turn ended without wait, counted as the "
     "default wait); the dot marks its start"),
    ("m-look", "Monitor: looks, errors", "Looks",
     "Square: listed its solver's revertible submissions; circle: read a call's full output; dot: tool error or "
     "context compaction"),
    ("m-act", "Monitor: reverts, messages", "Acts",
     "X in a ring: revert (pink X: the submission was fake; dashed: refused by the host); diamond in a ring: message "
     "to its solver"),
    ("m-th", "Monitor: reasoning", "Reasoning", "One tick per reasoning (solid) or written text (hollow) item of the "
                                                "monitor; click for its words"),
)
S_CALLS = ("s-calls", "Solver: tool calls", "Calls",
           "The solver's tool calls (thin ticks; bold: never seen by the monitor); grey lines lead to when the "
           "monitor saw a sample of them")
S_LANES = (
    ("s-mon", "Solver: received from the monitor", "Received",
     "As the solver got them: reverts of its accepted submission (X in a ring), messages (diamond in a ring), and "
     "the revert and DM events the host recorded for it"),
    ("s-subs", "Solver: submissions", "Submissions", "Its proof submissions (pink: fake; ✓ accepted, slash: not)"),
)
MON_GLYPH = {"GL": ("L1", "g-hollow"), "GO": ("B1", "g-hollow"), "GE": ("N1", "g-other"), "GC": ("C1", "g-other"),
             "GW": ("N1", "g-other")}


def _mon_glyph(it):
    typ = ev_type(it)
    d = ev_detail(it)
    if typ == "GR":
        return V.REV_T, "g-agrev" + (" g-fill" if d.get("fake") else "") + " ring"
    if typ == "GF":
        return "GF", "g-agrev g-refused ring"
    if typ == "GM":
        return V.MSG_T, "g-hollow ring"
    return MON_GLYPH.get(typ, ("N1", "g-other"))


def _mon_lane(it):
    typ = ev_type(it)
    if typ == "GW":
        return "m-wait"
    if typ in ("GR", "GF", "GM"):
        return "m-act"
    if typ in MON_TYPES:
        return "m-look"
    if it.get("kind") in ("reasoning", "text"):
        return "m-th"
    return None


def _mon_title(it, mon):
    typ = ev_type(it)
    d = ev_detail(it)
    t = V.fmt_t(it.get("t"))
    if typ == "GW":
        return (f"Wait · {t} · asked {secs(d.get('requested_s'))}, waited up to {secs(d.get('clamped_s'))}"
                + (f" · {V.plural(int(V._num(d.get('delivered_calls'))), 'solver call')} delivered"
                   if d.get("delivered_calls") is not None else "")
                + (" · implicit (turn ended without wait)" if d.get("implicit") else ""))
    if typ in MON_TYPES:
        bit = (d.get("reason") or d.get("text") or d.get("why") or "")
        return f"{MON_NAME[typ][:1].upper() + MON_NAME[typ][1:]} · {t}" + (f" · {d.get('problem_id')}" if d.get(
            "problem_id") else "") + (f"\n{V.redact(str(bit))[:200]}" if bit else "")
    kind = "Reasoning" if it.get("kind") == "reasoning" else "Wrote"
    return f"{mon}: {kind} · {t}\n“{V.redact(str(it.get('text') or ''))[:160]}”"


def mon_article(it, key, mon, solver):
    """One monitor timeline item in the detail column: the text as written (escaped, redacted) and, for an event,
    its record."""
    typ = ev_type(it)
    d = ev_detail(it)
    t = it.get("t")
    kind = it.get("kind")
    gtyp, gcls = _mon_glyph(it) if typ else ("TH", "g-th" if kind == "reasoning" else "g-th-low")
    icon = f'<svg class="hglyph" viewBox="-9 -9 18 18">{V.shape(gtyp, 0, 0, 5.2, gcls, None, "")}</svg>'
    rows = []
    if typ == "GW":
        rows = [("Asked to wait", secs(d.get("requested_s"))), ("Waited up to (after the minimum and cap)",
                                                               secs(d.get("clamped_s"))),
                ("Solver calls delivered", d.get("delivered_calls") if d.get("delivered_calls") is not None
                 else "unknown"),
                ("Implicit (ended its turn without wait)", _yes(d.get("implicit"))),
                ("Solver had stopped", _yes(d.get("solver_stopped"))),
                ("Backlog", d.get("backlog") if d.get("backlog") is not None else "unknown")]
        lags = [x for x in d.get("call_lags_s") or [] if isinstance(x, (int, float))]
        if lags:
            rows.append(("Delivered calls' lag (call to delivery)", f"median {secs(statistics.median(lags))}, "
                                                                     f"max {secs(max(lags))}"))
    elif typ == "GR":
        lat = d.get("latency_min")
        rows = [("Problem", d.get("problem_id") or "unknown"),
                ("Submission", (d.get("submission_id") or "") + (f" (shown to agents as {d['public_submission_id']})"
                                                                  if d.get("public_submission_id") else "") or "unknown"),
                ("Latency (acceptance to revert)", V.fmt_hms(lat) if isinstance(lat, (int, float)) else "unknown"),
                ("Submission fake", _yes(d.get("fake")))]
        if d.get("delivered_ts") is not None:
            rows.append(("Reason delivered", _when(d.get("delivered_ts"))))
    elif typ == "GF":
        rows = [("Problem", d.get("problem_id") or "unknown"), ("Why refused", d.get("why") or "not recorded")]
    elif typ == "GM":
        lag = d.get("lag_s")
        rows = [("Message", msg_id(d)), ("Sent at", V.fmt_t(t)),
                ("Delivered at", V.fmt_t(V._num(t) + lag / 60.0) if isinstance(lag, (int, float))
                 else _when(d.get("delivered_ts"))), ("Send to delivery", secs(lag))]
    elif typ == "GO":
        rows = [("Call", d.get("call_id") or it.get("call_id") or "unknown")]
    elif typ:
        rows = [(V.human(k), V._shown(k, v)) for k, v in d.items() if v not in (None, "", [], {})]
    quote = (d.get("reason") if typ == "GR" else d.get("text") if typ == "GM" else None) or it.get("text") or ""
    cap = MON_CAPS.get(kind, 1500)
    body = V.redact(str(quote)[:cap]) + (f"\n[viewer cut {len(str(quote)) - cap} chars]" if len(str(quote)) > cap else "")
    head = (MON_NAME[typ] if typ in MON_NAME else "reasoning" if kind == "reasoning" else "wrote" if kind == "text"
            else kind or "item")
    head = f"{mon}: {head}"
    dk = "mact" if typ in ("GR", "GF", "GM") else "mth" if not typ else "mlook"
    quote_html = ("" if not body else f'<blockquote>{V.esc(body)}</blockquote>' if kind in ("reasoning", "text") or typ
                  in ("GR", "GM") else f'<pre>{V.esc(body)}</pre>')
    kv = "".join(f"<dt>{V.esc(k)}</dt><dd>{V.esc(v)}</dd>" for k, v in rows)
    return (f'<article class="item{" k-ov" if dk == "mact" else " k-th" if dk == "mth" else ""}" id="{key}" '
            f'data-kind="{dk}" data-t="{V._num(t):.3f}" data-label="{V.esc(V.fmt_t(t) + " · " + head[:110])}">'
            f'<h3>{icon}{V.fmt_t(t)} &middot; {V.esc(head)}</h3><p>{V.badge("the monitor", "info")} '
            f'<small>monitor turn {V.esc(it.get("turn"))} &middot; monitor timeline item {V.esc(it.get("i"))}'
            + (f' &middot; tool {V.esc(it.get("tool"))}' if it.get("tool") else "") + '</small></p>'
            + quote_html + (f'<dl class="kv">{kv}</dl>' if kv else "") + '</article>')


def _wait_spans(items):
    """{GW item i: (start, end, implicit)}: from the wait call to its return (the items sharing its call id), else
    the event's time plus the granted wait."""
    by_call = defaultdict(list)
    for it in items:
        if it.get("call_id"):
            by_call[it["call_id"]].append(V._num(it.get("t")))
    out = {}
    for it in items:
        if ev_type(it) != "GW":
            continue
        d = ev_detail(it)
        t = V._num(it.get("t"))  # the wait event sits at its return (when the solver's calls were delivered)
        ts = by_call.get(it.get("call_id") or d.get("tool_call_id")) or [t]
        a, b = min(ts + [t]), max(ts + [t])
        if b - a < 1e-6 and isinstance(d.get("clamped_s"), (int, float)):
            a = b - d["clamped_s"] / 60.0
        out[it.get("i")] = (a, b, bool(d.get("implicit")))
    return out


def _sample(rows, n):
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(k * step)] for k in range(n)]


def _key_line():
    def g(typ, cls):
        return f'<svg viewBox="-13 -8 26 16">{V.shape(typ, 0, 0, 4.6, cls, None, "")}</svg>'
    items = [
        ('<svg viewBox="0 0 26 16"><rect class="wb" x="1" y="4" width="24" height="8" rx="2"/></svg>', "wait"),
        (g(V.REV_T, "g-agrev ring"), "revert"), (g(V.REV_T, "g-agrev g-fill ring"), "revert of a fake"),
        (g("GF", "g-agrev g-refused ring"), "refused revert"), (g(V.MSG_T, "g-hollow ring"), "message"),
        (g("TH", "g-th"), "reasoning"), (g("TH", "g-th-low"), "written text"),
        ('<svg viewBox="0 0 26 16"><line class="cn" style="opacity:.6" x1="2" y1="14" x2="24" y2="2"/></svg>',
         "solver call → monitor saw it (sample)"),
        ('<svg viewBox="0 0 26 16"><line class="cn msg" x1="2" y1="2" x2="24" y2="14"/></svg>', "message sent → delivered"),
        ('<svg viewBox="0 0 26 16"><line class="cn rev" x1="2" y1="2" x2="24" y2="14"/></svg>',
         "revert → reverted submission")]
    return '<ul class="pairkey">' + "".join(f"<li>{s}{V.esc(t)}</li>" for s, t in items) + "</ul>"


def pair_page(a, mon, names, tmax, models="", nav="", href="", all_pairs=()):
    """agents/<solver>.monitor.html: the monitor's lanes above the solver's on one time axis."""
    solver = a["name"]
    items = monitor_items(mon)
    LH, R = V.LANE_PX, 6.5
    phases, phase_src = V.phases_of(a, tmax)
    present = {str(th.get("code") or "?")[:1].upper() for th in a["thoughts"]}
    th_letters = [L for L in V.letters() if L in present]
    seen = ((V.AGENTIC.get("ctx") or {}).get("seen") or {}).get(solver) or []
    lanes = ([(k, "", lab, short, hint) for k, lab, short, hint in M_LANES]
             + [(S_CALLS[0], "", S_CALLS[1], S_CALLS[2], S_CALLS[3])]
             + ([("stance", "", "Solver: " + V.POSITION.lower(), "Position", V.POSITION_HINT)] if phases else [])
             + [(f"th-{L}", L) + V.thought_lane_label(L, names) for L in th_letters]
             + [(k, "", lab, short, hint) for k, lab, short, hint in S_LANES])
    ypos = {ln[0]: V.LANE_TOP + n * LH for n, ln in enumerate(lanes)}
    bottom = V.LANE_TOP + len(lanes) * LH
    svg, labels = [], []
    first_solver = S_CALLS[0]
    for n, (k, letter, label, short, hint) in enumerate(lanes):
        y = ypos[k]
        if k != "stance":
            svg.append(f'<rect class="lane-bg{" alt" if n % 2 else ""}" x="0" y="{y}" width="100%" height="{LH}"/>')
        lt = f"<b>{letter}</b>" if letter else ""
        labels.append(f'<div class="ll{" sep" if k == first_solver else ""}" title="{V.esc((letter + " " if letter else "") + label + ": " + hint)}">'
                      f'{lt}<span class="lf">{V.esc(label)}</span><span class="ls">{V.esc(short)}</span></div>')
    svg.append(V.axis_svg(tmax, V.LANE_TOP, len(lanes) * LH))
    svg.append(f'<rect class="sep" x="0" y="{ypos[first_solver] - 1}" width="100%" height="2"/>')
    mid = {k: y + LH / 2 for k, y in ypos.items()}
    articles, per_lane, connectors = [], defaultdict(list), []

    # Monitor lanes: wait bars, glyphs, reasoning ticks.
    spans = _wait_spans(items)
    key_of_mon = {}
    for it in items:
        lane = _mon_lane(it)
        if lane is None:
            continue
        key = f"m-{it.get('i')}"
        key_of_mon[it.get("i")] = key
        t = V._num(it.get("t"))
        if ev_type(it) == "GW" and it.get("i") in spans:
            s0, s1, imp = spans[it.get("i")]
            x0, x1 = V.pct(s0, tmax), V.pct(s1, tmax)
            svg.append(f'<rect class="wb{" imp" if imp else ""}" x="{x0:.2f}%" y="{ypos["m-wait"] + 9}" '
                       f'width="{max(x1 - x0, 0.12):.2f}%" height="{LH - 18}" rx="2"><title>'
                       f'{V.esc(_mon_title(it, mon) + f" · {V.fmt_span(s0, s1)}")}</title></rect>')
            t = s0
        typ, cls = _mon_glyph(it) if ev_type(it) else ("TH", "g-th" if it.get("kind") == "reasoning" else "g-th-low")
        prio = 0 if ev_type(it) in ("GR", "GM", "GF") else 3 if ev_type(it) == "GW" else 1
        per_lane[lane].append({"t": t, "order": V._num(it.get("i")), "prio": prio, "typ": typ, "cls": cls,
                               "key": key, "title": _mon_title(it, mon)})
        articles.append((t, 0, mon_article(it, key, mon, solver)))

    # Solver lanes: calls (from the coverage records), position, thoughts, the monitor's acts, submissions.
    y0, y1 = ypos["s-calls"] + 6, ypos["s-calls"] + LH - 6
    rows = [r for r in seen if isinstance(r, (list, tuple)) and len(r) >= 3 and r[1] is not None]
    for r in rows[:V_CAP]:
        x = V.pct(r[1], tmax)
        svg.append(f'<line class="sc{" unseen" if r[2] is None else ""}" x1="{x:.2f}%" x2="{x:.2f}%" y1="{y0}" '
                   f'y2="{y1}"/>')
    for r in _sample([r for r in rows if r[2] is not None], SEEN_CONNECTORS):
        connectors.append(f'<line class="cn" x1="{V.pct(r[1], tmax):.2f}%" y1="{y0}" x2="{V.pct(r[2], tmax):.2f}%" '
                          f'y2="{mid["m-wait"]}"/>')
    if not rows:
        svg.append(f'<text x="50%" y="{mid["s-calls"] + 4}" text-anchor="middle">no coverage records for this pair'
                   f'</text>')
    if phases:
        y = ypos["stance"]
        for frm, to, st, label in phases:
            cls = st if st in V.STANCES else "unknown"
            w = max(V.pct(to, tmax) - V.pct(frm, tmax), 0.1)
            svg.append(f'<rect class="ph-{cls}" x="{V.pct(frm, tmax):.2f}%" y="{y + 5}" width="{w:.2f}%" '
                       f'height="{LH - 10}" rx="2"><title>{V.esc(V.stance_title(frm, to, st, label))}</title></rect>')
    sub_t = {}
    for ev in a["events"]:
        e = ev["event"]
        typ = e.get("type")
        if typ == "S1":
            d = e.get("detail") or {}
            for sid in (d.get("submission_id"), d.get("public_id"), str(e.get("id") or "").split(":", 1)[-1]):
                if sid:
                    sub_t.setdefault(str(sid), V._num(ev.get("t")))
    old = V.EVIDENCE["on"]
    V.EVIDENCE["on"] = False  # no embedded solver timeline here: the chain expanders live on the solver page
    try:
        for ev in a["events"]:
            e = ev["event"]
            typ = e.get("type")
            lane = ("s-subs" if typ == "S1" else "s-mon" if typ in V.AGENTIC_TYPES or (
                typ in ("P2", "D1") and (V.is_agentic(e) or e.get("author") == mon)) else None)
            if lane is None:
                continue
            key = f"ev-{ev.get('i')}"
            t = V._num(ev.get("t"))
            per_lane[lane].append({"t": t, "order": V._num(ev.get("i")), "prio": V._event_prio(ev), "typ": typ,
                                   "cls": V.glyph_class(ev), "key": key, "title": V.event_title(ev)})
            articles.append((t, 1, V.event_article(solver, ev, a["by_event"].get(str(e.get("id")), []), key, names,
                                                   href)))
            d = e.get("detail") or {}
            if typ == V.MSG_T and sent_t(ev) is not None:
                connectors.append(f'<line class="cn msg" x1="{V.pct(sent_t(ev), tmax):.2f}%" '
                                  f'y1="{mid["m-act"]}" x2="{V.pct(t, tmax):.2f}%" y2="{mid["s-mon"]}"/>')
            if typ == V.REV_T:
                st = next((sub_t[str(x)] for x in (d.get("submission_id"), d.get("public_submission_id"))
                           if x and str(x) in sub_t), None)
                if st is None and isinstance(d.get("latency_min"), (int, float)):
                    st = t - d["latency_min"]
                fk = " fk" if d.get("fake", e.get("fake")) else ""
                connectors.append(f'<line class="cn rev{fk}" x1="{V.pct(t, tmax):.2f}%" y1="{mid["m-act"]}" '
                                  f'x2="{V.pct(t, tmax):.2f}%" y2="{mid["s-mon"]}"/>')
                if st is not None:
                    connectors.append(f'<line class="cn rev{fk}" x1="{V.pct(t, tmax):.2f}%" y1="{mid["s-mon"]}" '
                                      f'x2="{V.pct(st, tmax):.2f}%" y2="{mid["s-subs"]}"/>')
        items_by_i = {it.get("i"): it for it in a["items"]}
        for n, th in enumerate(a["thoughts"]):
            L = str(th.get("code") or "?")[:1].upper()
            if L not in th_letters:
                continue
            key = f"th-{n}"
            low = th.get("certainty") == "low"
            code = str(th.get("code"))
            per_lane[f"th-{L}"].append(
                {"t": V._num(th.get("t")), "order": V._num(th.get("item")), "typ": "TH", "key": key,
                 "prio": {"high": 0, "medium": 1}.get(th.get("certainty"), 2), "cls": "g-th-low" if low else "g-th",
                 "title": f"Thought {V.code_name(code, names, code)} · {V.fmt_t(th.get('t'))} · "
                          f"“{str(th.get('quote') or '')[:120]}”"})
            articles.append((V._num(th.get("t")), 2, V.thought_article(th, names, [], key, items_by_i, href)))
    finally:
        V.EVIDENCE["on"] = old
    n_stacks = 0
    glyphs = []
    for lane, entries in per_lane.items():
        if lane not in ypos:
            continue
        for group in V.stack(entries, tmax):
            n_stacks += len(group) > 1
            glyphs.append(V.stack_glyph(group, tmax, mid[lane], 7 if group[0]["typ"] == "TH" else R))
    svg += connectors + glyphs
    svg.append(f'<line id="cursor" class="cursor" x1="0" x2="0" y1="{V.LANE_TOP}" y2="{bottom}" visibility="hidden"/>')
    articles.sort(key=lambda x: (x[0], x[1]))

    idx = list(all_pairs)
    pos = idx.index(solver) if solver in idx else -1
    prev_link = (f'<a href="{V.esc(idx[pos - 1])}.monitor.html">&larr; {V.esc(idx[pos - 1])} pair</a>'
                 if pos > 0 else "")
    next_link = (f'<a href="{V.esc(idx[pos + 1])}.monitor.html">{V.esc(idx[pos + 1])} pair &rarr;</a>'
                 if 0 <= pos < len(idx) - 1 else "")
    stage = ("" if a["thoughts"] else '<p class="banner one">Stage 0 only for this solver: no thought reading or final '
             'reading yet, so its position and thought lanes are left out; everything shown comes from the records.'
             '</p>')
    no_mon = ("" if items else f'<p class="banner one">No timeline for {V.esc(mon)} (agentic/{V.esc(mon)}.jsonl): its '
              'lanes are empty.</p>')
    counts_ = Counter(ev_type(it) for it in items)
    nreason = sum(it.get("kind") == "reasoning" for it in items)
    body = (nav.replace("</nav>", f'<span class="sp"></span>{prev_link}{next_link}</nav>')
            + f'<header class="ah"><h1>{V.esc(mon)} ↔ {V.esc(solver)}</h1><small class="muted">agentic monitor and '
            f'its solver</small> <a class="pairlink" href="{V.esc(solver)}.html">{V.esc(solver)}\'s page</a> '
            f'<a class="pairlink" href="../index.html">index</a> <a class="pairlink" href="../events.html#agentic-'
            f'monitor">all interventions</a></header>{V.banner_line(models)}'
            f'{V.legend_bar("agent", set(V.AGENTIC_LANE_TYPES))}{stage}{no_mon}'
            + f'<template id="pempty"><div class="lgbox pkey"><h4>Reading this page</h4>{_key_line()}</div></template>'
            f'<div class="layout"><div class="main">{_key_line()}<div class="tl"><div class="tl-labels">'
            f'{"".join(labels)}</div><div class="tl-plot"><svg class="plot" height="{bottom + 18}">{"".join(svg)}'
            f'</svg></div></div><p class="tlcap">Run time (0–{V.fmt_tick(tmax)}). The monitor above, its solver '
            f'below. '
            + (f"{V.plural(n_stacks, 'spot holds', 'spots hold')} several items (the number on the shape); "
               f"click one to list them. " if n_stacks else "")
            + 'Click a shape, or step with ← →.</p>'
            f'<p class="muted">Monitor: {V.plural(counts_["GW"], "wait")}, {V.plural(counts_["GR"], "revert")}, '
            f'{V.plural(counts_["GF"], "refused revert")}, {V.plural(counts_["GM"], "message")}, '
            f'{V.plural(nreason, "reasoning item")}. Solver: {V.plural(len(a["events"]), "event")}, '
            f'{V.plural(len(a["thoughts"]), "coded thought")}'
            + (f'; position on cheating from the {V.esc(phase_src)}' if phases else "") + '.</p>'
            + hard_facts_html(mon)
            + f'<details class="all"><summary>All {V.plural(len(articles), "item")} in time order</summary>'
            f'<div id="items">{"".join(h for _, _, h in articles) or "<div class=empty>Nothing recorded.</div>"}'
            f'</div></details></div>{PANEL}</div>')
    return V.page(f"Story map {mon} and {solver}", body, V.LEGEND_JS + V.PANEL_JS, "agent", css=PAIR_CSS)


V_CAP = 6000  # solver call ticks drawn per pair page


def pair_pages(agents, names, tmax, models="", nav="", href=""):
    """[(relative path, html)] of every pair whose solver is mapped."""
    pairs = [(s, m) for s, m in sorted(V.AGENTIC["mon_of"].items()) if s in agents]
    order = [s for s, _ in pairs]
    return [(Path("agents") / f"{s}.monitor.html", pair_page(agents[s], m, names, tmax, models, nav, href, order))
            for s, m in pairs]
