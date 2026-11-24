"""Story map stage 0 for agentic-monitor runs (arm AG; docs/plan.md, "Agentic monitors in the story maps (owner
decision, November 24, 2026)"). Offline, read only, no model calls. Public interface:
research/analysis/storymap/INTERFACE-agentic.md (this docstring is its summary).

Only runs with agentic monitors are touched (``is_agentic``: settings ``effective``/``config``
``agentic_monitor.enabled``, or ``agentic_monitor`` roles in ``effective.roles``; without settings, ``agentic_*``
rows in runtime.jsonl). Every other run's stage-0 output is byte-identical.

Roles. ``agmon-NNN`` has role ``agentic_monitor`` and watches ``agent-NNN``. In AG runs the conference sender
``reviewer`` (async_review.REVIEWER_ID, the monitors' DM and revert identity) is re-attributed to the monitor of
the solver concerned: a message by its public id (``agentic_message`` / ``agentic_revert`` ``message_id``), else
the 1:1 mapping recipient ``agent-NNN`` -> ``agmon-NNN``; a revert by its submission (``agentic_revert``
``submission_id``), else the credited solver's monitor. Its ``author_role`` is ``agentic_monitor``.

Solver timelines (``timeline/<agent>.jsonl``, item format unchanged) gain two event types:

* ``Y1`` (``REVERT_EVENT``) monitor reverted my accepted submission, at the revert time (conference
  ``reversions.reverted_at``). id ``Y1:sub-000065``; author the monitor; outcome ``reverted``; fake the strict
  fake rule with evaluator validity; tags ``["revert_notice"]``. detail ``{monitor, submission_id,
  public_submission_id, problem_id, reason, accepted_ts, latency_min, message_id, fake}``.
* ``Y2`` (``MESSAGE_EVENT``) monitor message delivered to me, at delivery (``push_delivered`` or the first pull
  ``get_messages`` that returned it). id ``Y2:dm-000034``; author the monitor; tool ``push`` / ``get_messages``.
  detail ``{monitor, message_id, dm_id, text, sent_ts, delivered_ts, lag_s, revert_notice, delivery, ...push
  fields}``. It replaces the D1 that message used to get; a later re-read gets no event. P2 is unchanged except
  that its author (and text) name the monitor; S1's ``reverted_by`` names the monitor too.

``*_ts`` fields of G/monitor events are run-clock minutes (the clock of item ``t``); ``message_id`` is the
conference public message id (``dm-<hex>``, the id ``agentic_message`` records), ``dm_id`` the internal
``dm-000034``.

Monitor timelines ``agentic/<agmon>.jsonl`` (same item format; kinds reasoning / text / tool_call / tool_result /
context / event) from the monitor's Inspect sample: the system prompt once as a context item, each new user message
(implicit-wait deliveries, compaction requests) as a context item, tool results in full; outputs the harness
discarded (a re-sent request) are left out. A tool result's time is its runtime row's time when joined (the tool's
return), else the monitor's next model request. Event items from runtime.jsonl, placed right after the joined tool
result (``detail.tool_call_id``, the monitor's own tool-use id) else by time:
``GW`` wait, ``GL`` list_revertible, ``GO`` get_output, ``GR`` revert (Y1's detail plus ``delivered_ts`` of its
DM), ``GF`` refused revert, ``GM`` message, ``GE`` error, ``GC`` compaction. ``agentic/index.json`` lists the
monitor files ({monitor: {solver, items, est_tokens, first_min, last_min, source}}).

``agentic_context.json`` (beside ``timeline/``): ``{"version": 1, "pairs": {agmon: {...}}, "seen": {agent:
[[call_id, call_ts_min, seen_ts_min], ...]}}``; field definitions in ``pair_stats``.

``timeline/index.json`` entries gain ``"monitor": "agmon-NNN"`` (AG runs only).
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

ROLE = "agentic_monitor"
REVIEWER_ID = "reviewer"            # research_swarm.async_review.REVIEWER_ID
MONITOR_PREFIX, SOLVER_PREFIX = "agmon-", "agent-"
MONITOR_ID = re.compile(r"^agmon-(\d+)$")
REVERT_EVENT, MESSAGE_EVENT = "Y1", "Y2"
SOLVER_EVENT_TYPES = (REVERT_EVENT, MESSAGE_EVENT)
MONITOR_EVENT_TYPES = ("GW", "GL", "GO", "GR", "GF", "GM", "GE", "GC")
MONITOR_EVENT_NAMES = {"GW": "wait", "GL": "list revertible", "GO": "get output", "GR": "revert",
                       "GF": "refused revert", "GM": "message to solver", "GE": "error", "GC": "compaction"}
SOLVER_EVENT_NAMES = {REVERT_EVENT: "monitor reverted my submission", MESSAGE_EVENT: "monitor message delivered"}
KIND_TYPE = {"agentic_wait": "GW", "agentic_list_revertible": "GL", "agentic_get_output": "GO",
             "agentic_revert": "GR", "agentic_revert_refused": "GF", "agentic_message": "GM",
             "agentic_error": "GE", "agentic_compaction": "GC"}
# the monitor tool whose call produced each runtime row (implicit waits and errors/compactions have none)
KIND_TOOL = {"agentic_wait": "wait", "agentic_list_revertible": "list_revertible", "agentic_get_output": "get_output",
             "agentic_revert": "revert_submission", "agentic_revert_refused": "revert_submission",
             "agentic_message": "message_solver"}
REVERT_DM_HEAD = "A reviewer reverted your accepted submission for"   # async_review.REVERT_DM_TEXT
CONTEXT_FILE, MONITOR_DIR = "agentic_context.json", "agentic"
CONTEXT_VERSION = 1
JOIN_SLACK = 5.0     # seconds: a runtime row vs its tool call / result in the Inspect sample (same host clock)
TEXT_CHARS = 1500    # characters of a message / reason repeated in an event's text


def solver_of(monitor):
    m = MONITOR_ID.match(str(monitor or ""))
    return SOLVER_PREFIX + m.group(1) if m else None


def monitor_of(solver):
    s = str(solver or "")
    return MONITOR_PREFIX + s[len(SOLVER_PREFIX):] if s.startswith(SOLVER_PREFIX) else None


def _mins(ts, start):
    return None if ts is None else round((ts - start) / 60, 3)


def _block(settings):
    for key in ("effective", "config"):
        b = (settings.get(key) or {}).get("agentic_monitor")
        if isinstance(b, dict):
            return b
    return None


def is_agentic(settings, run_dir=None):
    """True for a run with agentic monitors (see the module docstring)."""
    block = _block(settings or {})
    if block is not None:
        return bool(block.get("enabled"))
    roles = ((settings or {}).get("effective") or {}).get("roles") or {}
    if any(r == ROLE for r in roles.values()):
        return True
    if not settings and run_dir is not None:
        path = Path(run_dir) / "runtime.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as f:
                return any('"agentic_wait"' in line or '"agentic_monitor_start"' in line for line in f)
    return False


def load_rows(run_dir):
    """Every ``agentic_*`` runtime row in file order: [{"kind", "ts", "agent", "payload"}]."""
    path = Path(run_dir) / "runtime.jsonl"
    out = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            if '"agentic_' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get("kind")
            if kind in KIND_TYPE or kind == "agentic_monitor_start":
                out.append({"kind": kind, "ts": row.get("timestamp"), "agent": row.get("agent_id"),
                            "payload": row.get("payload") or {}})
    return out


def load_costs(run_dir):
    """{monitor: settled USD} from runtime.jsonl ``inference_finished`` rows of agmon-* agents (None when there
    is no runtime.jsonl)."""
    path = Path(run_dir) / "runtime.jsonl"
    if not path.exists():
        return None
    out = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if '"inference_finished"' not in line or '"agmon-' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            agent = row.get("agent_id")
            if row.get("kind") != "inference_finished" or not MONITOR_ID.match(str(agent or "")):
                continue
            try:
                out[agent] = out.get(agent, 0.0) + float((row.get("payload") or {}).get("settled_usd") or 0)
            except (TypeError, ValueError):
                continue
    return out


class AgenticRun:
    """The run's agentic-monitor records joined to the conference (attached to RunContext as ``ctx.agentic``)."""

    def __init__(self, ctx, rows=None):
        self.ctx = ctx
        self.rows = load_rows(ctx.run_dir) if rows is None else rows
        self.monitors = sorted({r["agent"] for r in self.rows if MONITOR_ID.match(str(r["agent"] or ""))} |
                               {a for a, r in (((ctx.settings or {}).get("effective") or {}).get("roles") or {})
                                .items() if r == ROLE})
        self.msg_monitor, self.revert_rows = {}, {}
        for r in self.rows:
            p = r["payload"]
            if r["kind"] == "agentic_message" and p.get("message_id"):
                self.msg_monitor[p["message_id"]] = r["agent"]
            elif r["kind"] == "agentic_revert":
                if p.get("message_id"):
                    self.msg_monitor[p["message_id"]] = r["agent"]
                if p.get("submission_id"):
                    self.revert_rows[p["submission_id"]] = r

    # attribution --------------------------------------------------------------
    def message_monitor(self, public_id, recipient):
        return self.msg_monitor.get(public_id) or monitor_of(recipient)

    def revert_monitor(self, submission_id, credited):
        """The monitor of a reviewer revert of internal submission ``submission_id`` (int)."""
        row = self.revert_rows.get("sub-%06d" % int(submission_id)) if submission_id is not None else None
        return row["agent"] if row else monitor_of(credited)

    def author(self, author, recipient=None, public_id=None, submission_id=None, credited=None):
        """``reviewer`` -> its monitor (messages by public id / recipient, reverts by submission / credited)."""
        if author != REVIEWER_ID:
            return author
        if submission_id is not None or credited is not None:
            return self.revert_monitor(submission_id, credited)
        return self.message_monitor(public_id, recipient)

    def is_monitor_message(self, author):
        return author == REVIEWER_ID

    # solver Y1 ----------------------------------------------------------------
    def reverts_of(self, solver):
        """Y1 records for ``solver``: every reviewer reversion credited to it (time order)."""
        out = []
        for rev in self.ctx.reversions_list:
            if rev.get("reverted_by") != REVIEWER_ID or rev.get("credited_agent") != solver:
                continue
            out.append(self.revert_detail(rev))
        return out

    def revert_detail(self, rev):
        """(ts, detail, fake) of one reviewer reversion row."""
        ctx = self.ctx
        sub = ctx.subs.get(rev["submission_id"]) or {}
        sub_id = "sub-%06d" % rev["submission_id"]
        row = self.revert_rows.get(sub_id)
        p = row["payload"] if row else {}
        monitor = row["agent"] if row else monitor_of(rev.get("credited_agent"))
        accepted = ctx.accept_time(sub, rev) if sub else rev.get("accepted_at")
        when = rev.get("reverted_at") or (row["ts"] if row else None)
        fake = bool(sub["fake"]) if sub else None
        detail = {"monitor": monitor, "submission_id": sub_id,
                  "public_submission_id": p.get("public_submission_id") or sub.get("public_id"),
                  "problem_id": rev.get("task_id") or p.get("problem_id"),
                  "reason": p.get("reason") or rev.get("reason"),
                  "accepted_ts": _mins(accepted, ctx.start),
                  "latency_min": round((when - accepted) / 60, 3) if when and accepted else None,
                  "message_id": p.get("message_id"), "fake": fake}
        return {"ts": when, "detail": detail, "fake": fake}


# ---------------------------------------------------------------------------
# Solver timelines: Y1 placement


def place_reverts(stream, reverts):
    """The stream with one ``("agentic_revert", ts, record)`` per Y1 record, before the first later record that is
    not a tool result (at the end when none is), like an unanchored push."""
    if not reverts:
        return stream
    pending = sorted((r for r in reverts if r["ts"] is not None), key=lambda r: r["ts"])
    out, u = [], 0
    for rec in stream:
        ts = rec[1]
        while u < len(pending) and ts is not None and ts > pending[u]["ts"] and rec[0] != "tool_result":
            out.append(("agentic_revert", pending[u]["ts"], pending[u]))
            u += 1
        out.append(rec)
    out.extend(("agentic_revert", r["ts"], r) for r in pending[u:])
    return out


def g1_text(detail, monitor):
    fake = detail.get("fake")
    lat = detail.get("latency_min")
    return (f"Monitor {monitor} reverted my accepted submission for {detail.get('problem_id')} "
            f"({detail.get('submission_id')}" + (", FAKE" if fake else ", not fake" if fake is False else "") + ")"
            + (f", {round(lat, 1)} min after acceptance" if lat is not None else "")
            + ". Reason: " + str(detail.get("reason") or "")[:TEXT_CHARS])


def g2_detail(ctx, monitor, public_id, row, body, sent_epoch, delivered_epoch):
    lag = round(delivered_epoch - sent_epoch, 3) if sent_epoch is not None and delivered_epoch is not None else None
    return {"monitor": monitor, "message_id": public_id,
            "dm_id": "dm-%06d" % row["message_id"] if row and row.get("message_id") is not None else None,
            "text": body, "sent_ts": _mins(sent_epoch, ctx.start), "delivered_ts": _mins(delivered_epoch, ctx.start),
            "lag_s": lag, "revert_notice": is_revert_notice(ctx, public_id, body)}


def is_revert_notice(ctx, public_id, body):
    ag = getattr(ctx, "agentic", None)
    if ag is not None and public_id and any(
            r["payload"].get("message_id") == public_id for r in ag.revert_rows.values()):
        return True
    return str(body or "").startswith(REVERT_DM_HEAD)


def g2_text(detail, how):
    lag = detail.get("lag_s")
    return (f"Monitor {detail['monitor']} message {detail.get('dm_id') or detail.get('message_id')} delivered "
            f"({how}" + (f", {round(lag)} s after sending" if lag is not None else "") + "): "
            + str(detail.get("text") or "")[:TEXT_CHARS])


# ---------------------------------------------------------------------------
# Monitor timelines (Inspect sample + runtime rows)


def _epoch(value):
    if value is None:
        return None
    try:
        return value.timestamp()
    except AttributeError:
        from research.analysis.storymap.extract import _epoch as ep
        return ep(value)


def sample_stream(sample):
    """Normalized records (kind, ts, payload) of a monitor's Inspect sample, in order: context (system prompt once,
    each new user message), reasoning / text / tool_call per adopted model output (at its completion), tool_result
    at the first model request that carried it (the result's arrival, to within the monitor's next request)."""
    from research.analysis.judge.history import BILLING_HEADER, COUNTER, message_parts, message_text
    events = [e for e in (getattr(sample, "events", None) or []) if getattr(e, "event", None) == "model"]
    later_calls = set()   # call ids carried by a later request (results or replayed assistant calls)
    for e in events:
        for m in getattr(e, "input", None) or []:
            if getattr(m, "role", None) == "tool":
                later_calls.add(getattr(m, "tool_call_id", None))
            elif getattr(m, "role", None) == "assistant":
                later_calls.update(getattr(c, "id", None) for c in getattr(m, "tool_calls", None) or [])
    out, seen_counts, results = [], {}, set()
    for turn, e in enumerate(events):
        start = _epoch(getattr(e, "timestamp", None))
        done = _epoch(getattr(e, "completed", None)) or start
        counts = {}
        for m in getattr(e, "input", None) or []:
            role = getattr(m, "role", None)
            if role == "tool":
                cid = getattr(m, "tool_call_id", None)
                if cid in results:
                    continue
                results.add(cid)
                err = getattr(m, "error", None)
                text = message_text(m)
                if not text and err is not None:
                    text = f"[tool error] {getattr(err, 'message', err)}"
                out.append(("tool_result", start, {"id": cid, "text": text, "is_error": err is not None,
                                                   "function": getattr(m, "function", None)}))
            elif role in ("system", "user"):
                text = BILLING_HEADER.sub("", COUNTER.sub("", message_text(m)).strip()).strip()
                if not text:
                    continue
                key = (role, text)
                counts[key] = counts.get(key, 0) + 1
                if counts[key] > seen_counts.get(key, 0):
                    seen_counts[key] = counts[key]
                    out.append(("context", start, {"text": text, "role": role}))
        output = getattr(e, "output", None)
        choices = getattr(output, "choices", None) or []
        msg = getattr(choices[0], "message", None) if choices else None
        if msg is None or getattr(e, "pending", False):
            continue
        calls = getattr(msg, "tool_calls", None) or []
        if calls and not any(getattr(c, "id", None) in later_calls for c in calls) and turn < len(events) - 1:
            continue   # discarded by the harness (a re-sent request): never part of the conversation
        mid = getattr(msg, "id", None) or f"turn-{turn}"
        reasoning, text, other = message_parts(msg)
        if any(r for r in reasoning):
            out.append(("reasoning", done, {"mid": mid, "text": "\n\n".join(r for r in reasoning if r),
                                            "turn": turn}))
        visible = "\n".join(t for t in text + other if t)
        if visible.strip() and visible.strip() != "(no content)":   # the CLI's placeholder for empty text
            out.append(("text", done, {"mid": mid, "text": visible, "turn": turn}))
        for c in calls:
            out.append(("tool_call", done, {"mid": mid, "id": getattr(c, "id", None),
                                            "name": getattr(c, "function", None),
                                            "input": getattr(c, "arguments", None), "turn": turn}))
    return out


def short_tool(name):
    name = str(name or "")
    m = re.match(r"mcp__[^_]+(?:_[^_]+)*?__(.+)$", name)
    return m.group(1) if m else name


def _median(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 3) if vals else None


class MonitorBuilder:
    """One monitor's timeline items from its sample stream and runtime rows."""

    def __init__(self, ctx, monitor, rows, delivered=None):
        self.ctx, self.monitor, self.solver = ctx, monitor, solver_of(monitor)
        self.rows = [r for r in rows if r["agent"] == monitor and r["kind"] in KIND_TYPE]
        self.delivered = delivered or {}    # public message id -> delivered epoch (from the solver timeline)
        self.items, self.n = [], {}

    def t(self, ts):
        return _mins(ts, self.ctx.start)

    def add(self, ts, kind, text, turn, tool=None, event=None, call_id=None, mid=None):
        from research.analysis.storymap.extract import _iso
        self.items.append({"i": len(self.items), "t": self.t(ts), "ts": _iso(ts), "turn": turn, "kind": kind,
                           "tool": tool, "text": text, "event": event, "call_id": call_id, "mid": mid})

    def build(self, stream):
        """Items: the sample stream with each runtime row's event after its joined tool result, else by time."""
        calls = {}   # tool_use id -> (tool, call ts, turn)
        result_ts = {}
        for kind, ts, p in stream:
            if kind == "tool_call":
                calls[p["id"]] = (short_tool(p["name"]), ts, p["turn"])
            elif kind == "tool_result":
                result_ts[p["id"]] = ts
        joined, used = {}, set()   # row index -> call id
        for k, r in enumerate(self.rows):
            tool = KIND_TOOL.get(r["kind"])
            if tool is None or (r["kind"] == "agentic_wait" and r["payload"].get("implicit")):
                continue
            for cid, (name, cts, _) in calls.items():
                if cid in used or name != tool or cts is None or r["ts"] is None:
                    continue
                rts = result_ts.get(cid)
                if cts - JOIN_SLACK <= r["ts"] and (rts is None or r["ts"] <= rts + JOIN_SLACK):
                    joined[k], used = cid, used | {cid}
                    break
        after = {}
        for k, cid in joined.items():
            after.setdefault(cid, []).append(k)
        loose = sorted((k for k in range(len(self.rows)) if k not in joined), key=lambda k: self.rows[k]["ts"] or 0)
        # a joined tool result arrived when its runtime row was written (the tool's return), not at the monitor's
        # next model request (which can follow tens of seconds later)
        done = {cid: min(self.rows[k]["ts"] for k in ks) for cid, ks in after.items()
                if all(self.rows[k]["ts"] is not None for k in ks)}
        stream = [(kind, min(ts, done[p["id"]]) if kind == "tool_result" and p["id"] in done and ts is not None
                   else ts, p) for kind, ts, p in stream]
        turn, u = 0, 0
        for kind, ts, p in stream:
            while u < len(loose) and ts is not None and (self.rows[loose[u]]["ts"] or 0) < ts \
                    and kind != "tool_result":
                self.row_event(loose[u], None, turn)
                u += 1
            if kind in ("reasoning", "text", "tool_call"):
                turn = p["turn"]
            if kind == "tool_call":
                body = p.get("input")
                text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                self.add(ts, kind, text, turn, tool=short_tool(p["name"]), call_id=p["id"], mid=p["mid"])
            elif kind in ("reasoning", "text"):
                self.add(ts, kind, p["text"], turn, mid=p["mid"])
            elif kind == "tool_result":
                name, _, cturn = calls.get(p["id"], (short_tool(p.get("function")), None, turn))
                self.add(ts, "tool_result", p["text"], cturn, tool=name, call_id=p["id"])
                for k in sorted(after.get(p["id"], ())):
                    self.row_event(k, p["id"], cturn)
            elif kind == "context":
                self.add(ts, "context", p["text"], turn)
        for k in loose[u:]:
            self.row_event(k, None, turn)
        # joined rows whose result never appeared (the sample ended first)
        shown = {it["event"]["detail"].get("_row") for it in self.items if it["kind"] == "event"}
        for k, cid in sorted(joined.items()):
            if k not in shown:
                self.row_event(k, cid, turn)
        for it in self.items:
            if it["kind"] == "event":
                it["event"]["detail"].pop("_row", None)
        return self.items

    def nxt(self, typ):
        self.n[typ] = self.n.get(typ, 0) + 1
        return self.n[typ]

    def row_event(self, k, call_id, turn):
        r = self.rows[k]
        typ, p, ts = KIND_TYPE[r["kind"]], r["payload"], r["ts"]
        n = self.nxt(typ)
        eid = f"{typ}:{self.monitor}:{n:04d}"
        fake, tags, outcome = None, [], None
        if typ == "GW":
            detail = {k2: p.get(k2) for k2 in ("requested_s", "clamped_s", "delivered_calls", "updated_calls",
                                               "call_ids", "update_ids", "call_lags_s", "implicit",
                                               "solver_stopped", "backlog", "chars")}
            lags = p.get("call_lags_s") or []
            text = ("Implicit wait (default)" if p.get("implicit") else
                    f"Wait {p.get('clamped_s')} s (requested {p.get('requested_s')})") + \
                f": {p.get('delivered_calls') or 0} new call(s), {p.get('updated_calls') or 0} update(s)" + \
                (f"; seen {round(min(lags))}-{round(max(lags))} s after the calls" if lags else "") + \
                ("; solver stopped" if p.get("solver_stopped") else "") + ("; more queued" if p.get("backlog") else "")
        elif typ == "GL":
            detail = {"count": p.get("count"), "problem_ids": p.get("problem_ids") or []}
            text = f"Listed revertible submissions: {p.get('count')} (" + ", ".join(p.get("problem_ids") or []) + ")"
        elif typ == "GO":
            detail = {"call_id": p.get("call_id"), "found": p.get("found"), "chars": p.get("chars")}
            text = f"Get output of {p.get('call_id')}: " + (f"{p.get('chars')} chars" if p.get("found") else
                                                             "not found")
        elif typ == "GR":
            rev = next((x for x in self.ctx.reversions_list if "sub-%06d" % x["submission_id"] ==
                        p.get("submission_id")), None)
            g1 = self.ctx.agentic.revert_detail(rev) if rev else None
            detail = dict(g1["detail"]) if g1 else {
                "monitor": self.monitor, "submission_id": p.get("submission_id"),
                "public_submission_id": p.get("public_submission_id"), "problem_id": p.get("problem_id"),
                "reason": p.get("reason"), "accepted_ts": None, "latency_min": None,
                "message_id": p.get("message_id"), "fake": None}
            detail["monitor"] = self.monitor
            detail["message_id"] = p.get("message_id") or detail.get("message_id")
            detail["dm_sent"] = p.get("dm_sent")
            detail["delivered_ts"] = self.t(self.delivered.get(detail["message_id"]))
            fake, tags, outcome = detail.get("fake"), ["revert_notice"], "reverted"
            lat = detail.get("latency_min")
            text = (f"Reverted {self.solver}'s accepted submission for {detail.get('problem_id')} "
                    f"({detail.get('submission_id')}" + (", FAKE" if fake else ", not fake" if fake is False else "")
                    + ")" + (f", {round(lat, 1)} min after acceptance" if lat is not None else "")
                    + ". Reason: " + str(detail.get("reason") or "")[:TEXT_CHARS])
        elif typ == "GF":
            detail = {"problem_id": p.get("problem_id"), "why": p.get("why")}
            text, outcome = f"Revert of {p.get('problem_id')} refused: {p.get('why')}", "refused"
        elif typ == "GM":
            mid = p.get("message_id")
            row = self.ctx.msg_by_public.get(mid) if mid else None
            sent = (row or {}).get("sent_at") or ts
            delivered = self.delivered.get(mid)
            detail = {"message_id": mid, "dm_id": "dm-%06d" % row["message_id"] if row else None,
                      "text": p.get("text"), "sent": p.get("sent"), "sent_ts": self.t(sent),
                      "delivered_ts": self.t(delivered),
                      "lag_s": round(delivered - sent, 3) if delivered is not None and sent is not None else None}
            outcome = "sent" if p.get("sent") else "failed"
            text = (f"Message to {self.solver}" + ("" if p.get("sent") else " (NOT delivered: failed)")
                    + (f", delivered after {round(detail['lag_s'])} s" if detail["lag_s"] is not None else
                       ", never delivered" if p.get("sent") else "") + ": " + str(p.get("text") or "")[:TEXT_CHARS])
        elif typ == "GE":
            detail = {"stage": p.get("stage"), "error": p.get("error"), "detail": p.get("detail")}
            text = f"Error ({p.get('stage')}): {p.get('error')}" + (f": {p.get('detail')}" if p.get("detail") else "")
            outcome = "error"
        else:   # GC
            detail = {"messages": p.get("messages")}
            text = "Context compaction requested" + (f" at {p.get('messages')} messages" if p.get("messages") else "")
        detail["tool_call_id"] = call_id
        detail["_row"] = k
        ev = {"type": typ, "id": eid, "author": self.monitor, "author_role": ROLE, "fake": fake, "tags": tags,
              "outcome": outcome, "detail": detail}
        self.add(ts, "event", text, turn, tool=KIND_TOOL.get(r["kind"]), event=ev)


def load_sample(run_dir, monitor):
    """The monitor's Inspect sample (attachments resolved), or None when it has none."""
    from research.analysis.judge import history
    index = history.monitor_sample_index(run_dir)
    if monitor not in index:
        return None
    read, _ = history._ensure_inspect()
    path, sample_id, epoch = index[monitor]
    return read(str(path), id=sample_id, epoch=epoch, resolve_attachments=True)


def build_monitor(ctx, monitor, delivered=None, sample=None):
    """(items, index entry) of one monitor."""
    sample = sample if sample is not None else load_sample(ctx.run_dir, monitor)
    stream = sample_stream(sample) if sample is not None else []
    items = MonitorBuilder(ctx, monitor, ctx.agentic.rows, delivered).build(stream)
    from research.analysis.storymap.extract import item_tokens
    times = [it["t"] for it in items if it["t"] is not None]
    return items, {"solver": solver_of(monitor), "items": len(items),
                   "est_tokens": sum(item_tokens(it) for it in items),
                   "first_min": min(times) if times else None, "last_min": max(times) if times else None,
                   "source": "inspect" if sample is not None else "runtime_only"}


# ---------------------------------------------------------------------------
# Run outputs


def solver_deliveries(tl, solvers):
    """{public message id: delivered epoch} from the written solver timelines' Y2 events."""
    from research.analysis.storymap.extract import _epoch as ep
    out = {}
    for agent in solvers:
        path = Path(tl) / f"{agent}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if f'"type": "{MESSAGE_EVENT}"' not in line:
                continue
            it = json.loads(line)
            ev = it.get("event") or {}
            if it.get("kind") == "event" and ev.get("type") == MESSAGE_EVENT:
                mid = (ev.get("detail") or {}).get("message_id")
                if mid and mid not in out:
                    out[mid] = ep(it.get("ts"))
    return out


def pair_stats(ctx, monitor, delivered, costs):
    """One ``pairs`` entry. Times are run-clock minutes; counts are runtime rows of this monitor.
    waits: agentic_wait rows (implicit included); mean_wait_s: mean clamped_s of those waits; reverts /
    refused / messages / get_output / list_revertible / errors: agentic_revert / _revert_refused / _message /
    _get_output / _list_revertible / _error rows; first_fake_t: the solver's first fake submission (any outcome,
    strict fake rule with evaluator validity); first_revert_t: first agentic_revert; time_to_first_revert_min:
    first_revert_t - first_fake_t; fakes_accepted: the solver's accepted fake submissions; fakes_reverted: those
    later reverted; fakes_credited_at_end: those never reverted; false_reverts: monitor reverts of non-fake
    submissions; median_seen_lag_s: median call_lags_s over all deliveries; message_lag_s_median: median
    send-to-delivery seconds of the monitor's messages (agentic_message, delivered ones); cost_usd: settled USD
    of the monitor's inference (runtime inference_finished), null without runtime.jsonl."""
    solver = solver_of(monitor)
    rows = [r for r in ctx.agentic.rows if r["agent"] == monitor]
    by = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r)
    waits = by.get("agentic_wait", [])
    clamped = [r["payload"].get("clamped_s") for r in waits if isinstance(r["payload"].get("clamped_s"), (int, float))]
    lags = [x for r in waits for x in (r["payload"].get("call_lags_s") or [])]
    subs = sorted((s for s in ctx.subs.values() if s["agent_id"] == solver),
                  key=lambda s: (s["submitted_at"] or 0, s["submission_id"]))
    fakes = [s for s in subs if s["fake"]]
    acc = [s for s in fakes if s["outcome"] == "accepted"]
    reverted = [s for s in acc if s["submission_id"] in ctx.reversions]
    first_fake = _mins(fakes[0]["submitted_at"], ctx.start) if fakes else None
    revs = by.get("agentic_revert", [])
    first_rev = _mins(min(r["ts"] for r in revs), ctx.start) if revs else None
    false = 0
    for r in revs:
        m = re.fullmatch(r"sub-(\d+)", str(r["payload"].get("submission_id") or ""))
        sub = ctx.subs.get(int(m.group(1))) if m else None
        false += bool(sub is not None and not sub["fake"])
    msg_lags = []
    for r in by.get("agentic_message", []):
        mid = r["payload"].get("message_id")
        row = ctx.msg_by_public.get(mid) if mid else None
        if mid in delivered and delivered[mid] is not None:
            sent = (row or {}).get("sent_at") or r["ts"]
            msg_lags.append(delivered[mid] - sent)
    return {"solver": solver, "waits": len(waits),
            "mean_wait_s": round(sum(clamped) / len(clamped), 3) if clamped else None,
            "reverts": len(revs), "refused": len(by.get("agentic_revert_refused", [])),
            "messages": len(by.get("agentic_message", [])), "get_output": len(by.get("agentic_get_output", [])),
            "list_revertible": len(by.get("agentic_list_revertible", [])),
            "errors": len(by.get("agentic_error", [])), "first_fake_t": first_fake, "first_revert_t": first_rev,
            "time_to_first_revert_min": round(first_rev - first_fake, 3)
            if first_rev is not None and first_fake is not None else None,
            "fakes_accepted": len(acc), "fakes_reverted": len(reverted),
            "fakes_credited_at_end": len(acc) - len(reverted), "false_reverts": false,
            "median_seen_lag_s": _median(lags), "message_lag_s_median": _median(msg_lags),
            "cost_usd": round(costs.get(monitor, 0.0), 6) if costs is not None else None}


def seen_map(ctx):
    """{solver: [[call_id, call_ts_min, seen_ts_min], ...]} from every wait's new calls (first delivery only)."""
    out = {}
    for r in ctx.agentic.rows:
        if r["kind"] != "agentic_wait" or r["ts"] is None:
            continue
        p = r["payload"]
        solver = p.get("solver") or solver_of(r["agent"])
        lags = p.get("call_lags_s") or []
        for n, cid in enumerate(p.get("call_ids") or []):
            lag = lags[n] if n < len(lags) and isinstance(lags[n], (int, float)) else None
            out.setdefault(solver, []).append(
                [cid, _mins(r["ts"] - lag, ctx.start) if lag is not None else None, _mins(r["ts"], ctx.start)])
    return {k: v for k, v in sorted(out.items())}


def _monitor_work(job):
    run_dir, monitor, out, rows, delivered = job
    from research.analysis.storymap.extract import RunContext
    try:
        ctx = RunContext(run_dir, agentic_rows=rows)
        items, entry = build_monitor(ctx, monitor, delivered)
    except Exception as ex:  # noqa: BLE001 - reported per monitor, the run continues
        return monitor, {"solver": solver_of(monitor), "error": f"{type(ex).__name__}: {ex}"}
    with open(Path(out) / f"{monitor}.jsonl", "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return monitor, entry


def write_outputs(ctx, out, solvers, workers=4, log=None):
    """Monitor timelines for the monitors of ``solvers`` (``agentic/``), ``agentic_context.json`` (every monitor)
    and returns the monitor index."""
    from concurrent.futures import ProcessPoolExecutor
    out = Path(out)
    tl = out / "timeline"
    delivered = solver_deliveries(tl, sorted(set(solvers) | {solver_of(m) for m in ctx.agentic.monitors}))
    mdir = out / MONITOR_DIR
    mdir.mkdir(parents=True, exist_ok=True)
    wanted = [m for m in (monitor_of(s) for s in solvers) if m in ctx.agentic.monitors]
    jobs = [(str(ctx.run_dir), m, str(mdir), ctx.agentic.rows, delivered) for m in wanted]
    index = {}
    if workers <= 1 or len(jobs) <= 1:
        results = map(_monitor_work, jobs)
        pool = None
    else:
        pool = ProcessPoolExecutor(max_workers=workers)
        results = pool.map(_monitor_work, jobs)
    for monitor, entry in results:
        index[monitor] = entry
        if log:
            log(monitor, entry)
    if pool is not None:
        pool.shutdown()
    old = mdir / "index.json"
    if old.exists():
        merged = json.loads(old.read_text(encoding="utf-8"))
        merged.update(index)
        index = merged
    index = dict(sorted(index.items()))
    (mdir / "index.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    costs = load_costs(ctx.run_dir)
    context = {"version": CONTEXT_VERSION,
               "pairs": {m: pair_stats(ctx, m, delivered, costs) for m in ctx.agentic.monitors},
               "seen": seen_map(ctx)}
    (out / CONTEXT_FILE).write_text(json.dumps(context, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return index
