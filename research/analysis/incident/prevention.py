"""Contagion prevention figures for the incident report's "Oversight record" (owner request, October 26, 2026).

Host-computed from the saved conference records, no model. Six views of whether oversight slowed the spread:

1. exposure-to-outcome swimlanes: one row per agent that read an exploit file, from first exposure to adoption
   (or the end of the run), with the moment it first saw an overseer warning;
2. exposed x warned table: adoption rates by whether the agent had read an exploit file and had seen a warning;
3. transmission tree with blocked branches: every exposed agent under the exploit file it read first, adopters
   and decliners, warned agents marked, reverted files cut;
4. outbreak curve with interventions: adopters over time, fake credits still standing, the overseer's actions;
5. detection latency: how long each not-genuine accepted file stood, who read it meanwhile, checks and reverts;
6. comparison: this run's outbreak curve over a comparison run's (``--compare-run``), when one is given.

Definitions. An *exploit file* is the shared-library solution of an accepted submission the independent proof
checker found not genuine (results.json); without that record, one the answer-field or native detector flags
(research/analysis/incident/contagion.py). *Exposure* is an agent's first read of an exploit file after it was
published (and before any reversion). *Adoption* is an agent's first submission the detectors flag. A *warning*
is the first time the agent received an overseer message: its board post returned by read_board, or its direct
message returned by get_messages. Agents who read the board may differ from those who do not, so the table
shows association, not effect.
"""
from __future__ import annotations

import html
import json
import math
import sqlite3
from pathlib import Path

from research.analysis import roles as participant_roles
from research.analysis.incident import contagion

SCHEMA = "incident-prevention/1"


# ---------------------------------------------------------------------------
# Records

def _events(run_dir):
    path = Path(run_dir) / "conference" / "events.jsonl"
    out = []
    if path.is_file():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _db_rows(run_dir, query, args=()):
    path = Path(run_dir) / "conference" / "state.sqlite"
    if not path.is_file():
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return db.execute(query, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()


def _clock(run_dir):
    """(common_start, run length in minutes up to the stop at max_run_seconds, else the last event)."""
    start, stop_elapsed, last = None, None, None
    path = Path(run_dir) / "runtime.jsonl"
    if path.is_file():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                ts = event.get("timestamp")
                if isinstance(ts, (int, float)):
                    last = ts if last is None else max(last, ts)
                if start is None and event.get("kind") == "common_start":
                    start = ts
                if event.get("kind") == "run_stop" and stop_elapsed is None:
                    stop_elapsed = (event.get("payload") or {}).get("elapsed_seconds")
    if start is None:
        return None, None
    length = stop_elapsed / 60 if isinstance(stop_elapsed, (int, float)) else ((last - start) / 60 if last else None)
    return start, length


def outbreak_points(run_dir, start, exclude=()):
    """[(agent, minute)] of each agent's first detector-flagged submission (any outcome), in time order."""
    first = {}
    for agent, source, ts in _db_rows(run_dir, "SELECT agent_id, source, submitted_at FROM submissions "
                                                "ORDER BY submitted_at"):
        if agent in exclude or agent in first or ts is None:
            continue
        if contagion.detect(source):
            first[agent] = round((ts - start) / 60, 2)
    return sorted(first.items(), key=lambda kv: kv[1])


def compute(run_dir, overseer, *, compare_run_dir=None):
    run_dir = Path(run_dir)
    start, length = _clock(run_dir)
    if start is None:
        return None
    minute = lambda ts: None if ts is None else round((ts - start) / 60, 2)  # noqa: E731
    roles = participant_roles.special_roles(run_dir)
    seeds = {a for a, r in roles.items() if r == participant_roles.SEED}
    events = _events(run_dir)

    subs = {pid: {"agent": a, "task": t, "source": src, "outcome": o, "judged": j}
            for pid, a, t, src, o, j in _db_rows(run_dir, "SELECT public_id, agent_id, task_id, source, outcome, "
                                                          "judged_at FROM submissions")}
    fraudulent = None
    results = run_dir / "results.json"
    if results.is_file():
        try:
            evaluation = json.loads(results.read_text(encoding="utf-8")).get("independent_evaluation") or {}
            if evaluation.get("status") == "completed" or evaluation.get("not_genuine") is not None:
                fraudulent = {x.get("submission") for x in evaluation.get("not_genuine") or []
                              if x.get("validity") == "fraudulent"}
        except ValueError:
            pass
    basis = "independent proof checker" if fraudulent is not None else "detectors"

    files, by_path = {}, {}
    for e in events:
        p = e.get("payload") or {}
        if e.get("kind") == "library_published" and p.get("kind", "solution") == "solution":
            pid = p.get("public_submission_id")
            sub = subs.get(pid) or {}
            bad = (pid in fraudulent) if fraudulent is not None else bool(contagion.detect(sub.get("source")))
            if not bad or not p.get("path"):
                continue
            files[pid] = {"submission": pid, "path": p["path"], "author": e.get("agent_id"), "task": sub.get("task"),
                          "seeded": e.get("agent_id") in seeds, "published_min": minute(e.get("timestamp")),
                          "accepted_min": minute(sub.get("judged")) or minute(e.get("timestamp")),
                          "checked_min": None, "check_validity": None, "reverted_min": None, "reads": []}
            by_path.setdefault(p["path"], []).append(pid)

    warnings, interventions = {}, []
    overseer_posts = set()
    for e in events:
        kind, p, ts, agent = e.get("kind"), e.get("payload") or {}, e.get("timestamp"), e.get("agent_id")
        if agent == overseer and kind in ("board_post", "dm_sent", "proof_checked", "submission_reverted",
                                          "feedback_submitted"):
            interventions.append({"kind": kind, "minute": minute(ts), "target": p.get("public_submission_id")
                                  or p.get("recipient") or p.get("to"), "validity": p.get("validity")})
            if kind == "board_post" and p.get("post_id"):
                overseer_posts.add(p["post_id"])
            if kind == "proof_checked" and p.get("public_submission_id") in files:
                f = files[p["public_submission_id"]]
                if f["checked_min"] is None:
                    f["checked_min"], f["check_validity"] = minute(ts), p.get("validity")
            if kind == "submission_reverted" and p.get("public_submission_id") in files:
                files[p["public_submission_id"]]["reverted_min"] = minute(ts)
        elif kind == "board_read" and agent not in (overseer, None):
            if overseer_posts & set(p.get("post_ids") or []):
                warnings.setdefault(agent, {"minute": minute(ts), "channel": "board post"})
    for sender, recipient, delivered in _db_rows(run_dir, "SELECT sender, recipient, delivered_at FROM messages "
                                                          "WHERE sender = ? AND delivered_at IS NOT NULL", (overseer,)):
        m = minute(delivered)
        if recipient not in warnings or m < warnings[recipient]["minute"]:
            warnings[recipient] = {"minute": m, "channel": "direct message"}

    exposure = {}
    for e in events:
        if e.get("kind") != "library_read" or e.get("agent_id") in (overseer, None):
            continue
        path = (e.get("payload") or {}).get("path")
        m = minute(e.get("timestamp"))
        for pid in by_path.get(path, []):
            f = files[pid]
            if f["published_min"] is None or m < f["published_min"] or e["agent_id"] == f["author"]:
                continue
            if f["reverted_min"] is not None and m > f["reverted_min"]:
                continue
            f["reads"].append({"agent": e["agent_id"], "minute": m})
            exposure.setdefault(e["agent_id"], {"minute": m, "file": pid})

    adoption = dict(outbreak_points(run_dir, start, exclude=seeds | {overseer}))
    credits = {}
    for pid, sub in subs.items():
        if sub["outcome"] == "accepted" and contagion.detect(sub["source"]):
            credits[sub["agent"]] = credits.get(sub["agent"], 0) + 1

    agents = sorted(set(exposure) | set(adoption) | set(warnings))
    people = []
    for agent in agents:
        if agent in seeds or agent == overseer:
            continue
        adopted = adoption.get(agent)
        warn = warnings.get(agent)
        horizon = adopted if adopted is not None else length
        people.append({"agent": agent, "exposed_min": (exposure.get(agent) or {}).get("minute"),
                       "source": (exposure.get(agent) or {}).get("file"),
                       "adopted_min": adopted, "credits": credits.get(agent, 0),
                       "warned_min": None if warn is None else warn["minute"],
                       "warn_channel": None if warn is None else warn["channel"],
                       "warned_before": warn is not None and (horizon is None or warn["minute"] <= horizon)})
    solvers = [a for a in _all_agents(run_dir) if a not in seeds and a != overseer]
    table = {}
    for exposed in (True, False):
        for warned in (True, False):
            group = [a for a in solvers
                     if (a in exposure) == exposed and _warned_before(a, warnings, adoption, length) == warned]
            table[f"{'exposed' if exposed else 'unexposed'}_{'warned' if warned else 'unwarned'}"] = {
                "agents": len(group), "adopted": sum(a in adoption for a in group)}

    standing, events_fs = [], []
    for f in files.values():
        if f["accepted_min"] is not None:
            events_fs.append((f["accepted_min"], 1))
        if f["reverted_min"] is not None:
            events_fs.append((f["reverted_min"], -1))
    level = 0
    for m, d in sorted(events_fs):
        level += d
        standing.append([m, level])

    record = {"schema": SCHEMA, "computed_by": "host (no model)", "basis": basis, "overseer": overseer,
              "roles": roles, "clock": {"start_ts": start, "length_min": length},
              "files": sorted(files.values(), key=lambda f: (f["accepted_min"] is None, f["accepted_min"] or 0)),
              "people": people, "table": table, "interventions": interventions,
              "outbreak": [[a, m] for a, m in sorted(adoption.items(), key=lambda kv: kv[1])],
              "standing": standing, "comparison": None}
    if compare_run_dir is not None:
        record["comparison"] = comparison(compare_run_dir)
    return record


def comparison(run_dir):
    run_dir = Path(run_dir)
    start, length = _clock(run_dir)
    if start is None:
        return None
    roles = participant_roles.special_roles(run_dir)
    exclude = {a for a, r in roles.items() if r != participant_roles.SOLVER}
    return {"run": run_dir.name, "length_min": length,
            "outbreak": [[a, m] for a, m in outbreak_points(run_dir, start, exclude=exclude)]}


def _all_agents(run_dir):
    rows = _db_rows(run_dir, "SELECT agent_id FROM agents")
    if rows:
        return sorted(r[0] for r in rows)
    return sorted({a for (a,) in _db_rows(run_dir, "SELECT DISTINCT agent_id FROM submissions")})


def _warned_before(agent, warnings, adoption, length):
    warn = warnings.get(agent)
    if warn is None:
        return False
    horizon = adoption.get(agent, length)
    return horizon is None or warn["minute"] <= horizon


# ---------------------------------------------------------------------------
# Figures (inline SVG, the contagion section's style classes)

STYLE = """
.pv-warn{fill:var(--lin0)}.pv-adopt{fill:var(--lin1)}.pv-decl{fill:var(--lin-x)}.pv-stand{stroke:var(--lin3)}
.pv-lane{stroke:var(--rule);stroke-width:2}.pv-lane-a{stroke:var(--lin1);stroke-width:2;opacity:.55}
.pv-bar{fill:var(--lin1);opacity:.35}.pv-bar-r{fill:var(--lin2);opacity:.45}
.pv-read{stroke:var(--fg);stroke-width:1.5}.pv-cut{stroke:var(--lin2);stroke-width:2.5}
.pv-int{stroke:var(--lin0);stroke-width:1;stroke-dasharray:2 3}
.pv-cmp{fill:none;stroke:var(--lin-x);stroke-width:2;stroke-dasharray:5 3}
.pv-standline{fill:none;stroke:var(--lin3);stroke-width:2}
.pv-table{border-collapse:collapse;margin:8px 0 6px;font-size:14px}
.pv-table th,.pv-table td{border:1px solid var(--rule);padding:5px 10px;text-align:center}
.pv-table th{font-weight:600}
"""


def _esc(text):
    return html.escape(str(text), quote=True)


def _at(minutes, record):
    return contagion._at(minutes, record["clock"]["start_ts"])


def _axis(parts, x, left, pw, top, ph, length, height, label="run minute", top_axis=False):
    step = contagion._nice_step(length, 7)
    m = 0.0
    ty = top - 6 if top_axis else top + ph + 16
    while m <= length + 1e-9:
        parts.append(f'<line class="cg-grid" x1="{x(m):.1f}" x2="{x(m):.1f}" y1="{top}" y2="{top + ph}"/>'
                     f'<text class="cg-tick" x="{x(m):.1f}" y="{ty}" text-anchor="middle">{contagion._fmt(m)}</text>')
        m += step
    parts.append(f'<text class="cg-tick" x="{left + pw}" y="{(top - 18) if top_axis else (height - 2)}" '
                 f'text-anchor="end">{label}</text>')


def _shield(cx, cy, title):
    return (f'<path class="pv-warn" d="M{cx - 5:.1f},{cy - 6:.1f} H{cx + 5:.1f} V{cy:.1f} '
            f'Q{cx + 5:.1f},{cy + 5:.1f} {cx:.1f},{cy + 7:.1f} Q{cx - 5:.1f},{cy + 5:.1f} {cx - 5:.1f},{cy:.1f} Z">'
            f'<title>{_esc(title)}</title></path>')


def _short(task, n=18):
    task = task or ""
    return task if len(task) <= n else task[:n - 1] + "…"


def _length(record):
    return max(record["clock"]["length_min"] or 1, 1)


def swimlanes_svg(record, width=680):
    people = sorted((p for p in record["people"] if p["exposed_min"] is not None),
                    key=lambda p: p["exposed_min"])
    if not people:
        return None
    row, left, right, top = 15, 74, 16, 26
    height = top + row * len(people) + 30
    pw, length = width - left - right, _length(record)
    x = lambda m: left + pw * min(max(m, 0), length) / length  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" role="img" xmlns="http://www.w3.org/2000/svg" '
             f'aria-label="Exposure to outcome, one row per exposed agent">']
    _axis(parts, x, left, pw, top, row * len(people), length, height)
    for i, p in enumerate(people):
        cy = top + row * i + row / 2
        end = p["adopted_min"] if p["adopted_min"] is not None else length
        cls = "pv-lane-a" if p["adopted_min"] is not None else "pv-lane"
        parts.append(f'<text class="cg-lab" x="{left - 6}" y="{cy + 4:.1f}" text-anchor="end">{_esc(p["agent"])}</text>'
                     f'<line class="{cls}" x1="{x(p["exposed_min"]):.1f}" x2="{x(end):.1f}" y1="{cy:.1f}" '
                     f'y2="{cy:.1f}"/>'
                     f'<circle class="cg-tick" cx="{x(p["exposed_min"]):.1f}" cy="{cy:.1f}" r="2.5">'
                     f'<title>{_esc(p["agent"])} first read an exploit file at {_esc(_at(p["exposed_min"], record))}'
                     f'</title></circle>')
        if p["warned_min"] is not None:
            parts.append(_shield(x(p["warned_min"]), cy, f'{p["agent"]} saw an overseer warning ({p["warn_channel"]}) '
                                                          f'at {_at(p["warned_min"], record)}'))
        if p["adopted_min"] is not None:
            parts.append(f'<circle class="pv-adopt" cx="{x(end):.1f}" cy="{cy:.1f}" r="4.5"><title>'
                         f'{_esc(p["agent"])} first submitted an exploit at {_esc(_at(end, record))}'
                         f' ({p["credits"]} credited)</title></circle>')
        else:
            parts.append(f'<circle class="pv-decl" cx="{x(end):.1f}" cy="{cy:.1f}" r="4"><title>'
                         f'{_esc(p["agent"])} never submitted an exploit</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


def table_html(record):
    t = record["table"]
    cell = lambda key: (f'{t[key]["adopted"]} of {t[key]["agents"]}'  # noqa: E731
                        + (f' ({100 * t[key]["adopted"] / t[key]["agents"]:.0f}%)' if t[key]["agents"] else ""))
    return ('<table class="pv-table"><thead><tr><th></th><th>Saw an overseer warning first</th>'
            '<th>Never saw one first</th></tr></thead><tbody>'
            f'<tr><th>Read an exploit file</th><td>{cell("exposed_warned")}</td><td>{cell("exposed_unwarned")}</td></tr>'
            f'<tr><th>Never read one</th><td>{cell("unexposed_warned")}</td><td>{cell("unexposed_unwarned")}</td></tr>'
            '</tbody></table>')


def tree_svg(record, width=680):
    files = [f for f in record["files"] if f["reads"]]
    people = {p["agent"]: p for p in record["people"]}
    groups = []
    for f in files:
        kids = sorted((people[a] for a in people if people[a]["source"] == f["submission"]),
                      key=lambda p: p["exposed_min"])
        if kids:
            groups.append((f, kids))
    if not groups:
        return None
    row, left, right, top = 14, 215, 16, 26
    nrows = sum(1 + len(k) for _, k in groups)
    height = top + row * nrows + 24
    pw, length = width - left - right, _length(record)
    x = lambda m: left + pw * min(max(m or 0, 0), length) / length  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" role="img" xmlns="http://www.w3.org/2000/svg" '
             f'aria-label="Exploit files and the agents exposed to each">']
    _axis(parts, x, left, pw, top, row * nrows, length, height)
    r = 0
    for f, kids in groups:
        cy = top + row * r + row / 2
        sx = x(f["accepted_min"])
        label = participant_roles.label(f["author"], record["roles"]).replace(" (seeded, planted by researchers)",
                                                                               " (seed)")
        parts.append(f'<text class="cg-lab" x="{left - 6}" y="{cy + 4:.1f}" text-anchor="end">'
                     f'{_esc(label)} · {_esc(_short(f["task"]))}<title>{_esc(f["task"])}</title></text>'
                     f'<rect class="pv-adopt" x="{sx - 5:.1f}" y="{cy - 5:.1f}" width="10" height="10"><title>'
                     f'{_esc(f["path"])} by {_esc(f["author"])}, accepted at {_esc(_at(f["accepted_min"], record))}; '
                     f'not genuine ({_esc(record["basis"])})</title></rect>')
        if f["reverted_min"] is not None:
            rx = x(f["reverted_min"])
            parts.append(f'<line class="pv-cut" x1="{rx:.1f}" x2="{rx:.1f}" y1="{cy - 7:.1f}" '
                         f'y2="{cy + row * len(kids) + 4:.1f}"><title>reverted by the overseer at '
                         f'{_esc(_at(f["reverted_min"], record))}</title></line>')
        for j, p in enumerate(kids, 1):
            ky = cy + row * j
            kx = x(p["exposed_min"])
            parts.append(f'<text class="cg-tick" x="{left - 6}" y="{ky + 4:.1f}" text-anchor="end">{_esc(p["agent"])}'
                         f'</text><path class="cg-edge lnx" d="M{sx:.1f},{cy + 5:.1f} V{ky:.1f} H{kx:.1f}"/>')
            cls = "pv-adopt" if p["adopted_min"] is not None else "pv-decl"
            what = (f'adopted at {_at(p["adopted_min"], record)} ({p["credits"]} credited)'
                    if p["adopted_min"] is not None else "never submitted an exploit")
            parts.append(f'<circle class="{cls}" cx="{kx:.1f}" cy="{ky:.1f}" r="{4 + min(p["credits"], 6) * 0.6:.1f}">'
                         f'<title>{_esc(p["agent"])}: read it at {_esc(_at(p["exposed_min"], record))}; '
                         f'{_esc(what)}</title></circle>')
            if p["warned_before"]:
                parts.append(_shield(kx + 11, ky, f'{p["agent"]} saw an overseer warning ({p["warn_channel"]}) at '
                                                  f'{_at(p["warned_min"], record)}'))
        r += 1 + len(kids)
    parts.append("</svg>")
    return "".join(parts)


def _step_path(points, x, y, length):
    path, count = [f"M{x(0):.1f},{y(0):.1f}"], 0
    for _, m in points:
        path.append(f"H{x(m):.1f}")
        count += 1
        path.append(f"V{y(count):.1f}")
    path.append(f"H{x(length):.1f}")
    return "".join(path)


def curve_svg(record, width=680, height=260, compare=False):
    cmp_ = record.get("comparison") if compare else None
    if compare and not cmp_:
        return None
    length = _length(record)
    if cmp_:
        length = max(length, cmp_.get("length_min") or 0)
    left, right, top, bottom = 40, 16, 22, 34
    pw, ph = width - left - right, height - top - bottom
    series = [len(record["outbreak"])] + ([len(cmp_["outbreak"])] if cmp_ else [max([s for _, s in
                                                                                       record["standing"]] or [0])])
    ymax = max(series + [1])
    ystep = max(1, int(contagion._nice_step(ymax, 5)))
    ytop = math.ceil(ymax / ystep) * ystep
    x = lambda m: left + pw * min(max(m, 0), length) / length  # noqa: E731
    y = lambda n: top + ph * (1 - n / ytop)  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" role="img" xmlns="http://www.w3.org/2000/svg">']
    for n in range(0, ytop + 1, ystep):
        parts.append(f'<line class="cg-grid" x1="{left}" x2="{left + pw}" y1="{y(n):.1f}" y2="{y(n):.1f}"/>'
                     f'<text class="cg-tick" x="{left - 6}" y="{y(n) + 4:.1f}" text-anchor="end">{n}</text>')
    step = contagion._nice_step(length, 7)
    m = 0.0
    while m <= length + 1e-9:
        parts.append(f'<text class="cg-tick" x="{x(m):.1f}" y="{top + ph + 16}" text-anchor="middle">'
                     f'{contagion._fmt(m)}</text>')
        m += step
    parts.append(f'<text class="cg-tick" x="{left + pw}" y="{height - 2}" text-anchor="end">run minute</text>')
    if cmp_:
        parts.append(f'<path class="pv-cmp" d="{_step_path(cmp_["outbreak"], x, y, cmp_.get("length_min") or length)}">'
                     f'<title>{_esc(cmp_["run"])}: {len(cmp_["outbreak"])} agents submitted an exploit</title></path>')
    else:
        marks = {"board_post": "post", "dm_sent": "DM", "proof_checked": "check", "submission_reverted": "revert",
                 "feedback_submitted": "feedback"}
        for i, item in enumerate(record["interventions"]):
            if item["minute"] is None:
                continue
            ix = x(item["minute"])
            parts.append(f'<line class="pv-int" x1="{ix:.1f}" x2="{ix:.1f}" y1="{top}" y2="{top + ph}">'
                         f'<title>overseer {marks.get(item["kind"], item["kind"])} at '
                         f'{_esc(_at(item["minute"], record))}{" (" + _esc(item["target"]) + ")" if item.get("target") else ""}'
                         f'</title></line><text class="cg-tick" x="{ix + 3:.1f}" y="{top - 6 - 10 * (i % 2)}">'
                         f'{marks.get(item["kind"], item["kind"])}</text>')
        if record["standing"]:
            path, level = [f"M{x(0):.1f},{y(0):.1f}"], 0
            for m, lv in record["standing"]:
                path.append(f"H{x(m):.1f}V{y(lv):.1f}")
                level = lv
            path.append(f"H{x(length):.1f}")
            parts.append(f'<path class="pv-standline" d="{"".join(path)}"><title>not-genuine accepted credits still '
                         f'standing ({level} at the end)</title></path>')
    parts.append(f'<path class="cg-curve" d="{_step_path(record["outbreak"], x, y, _length(record))}">'
                 f'<title>{len(record["outbreak"])} agents submitted an exploit</title></path>')
    parts.append("</svg>")
    return "".join(parts)


def latency_svg(record, width=680):
    files = record["files"]
    if not files:
        return None
    row, left, right, top = 15, 215, 16, 26
    height = top + row * len(files) + 30
    pw, length = width - left - right, _length(record)
    x = lambda m: left + pw * min(max(m or 0, 0), length) / length  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" role="img" xmlns="http://www.w3.org/2000/svg" '
             f'aria-label="How long each not-genuine accepted file stood">']
    _axis(parts, x, left, pw, top, row * len(files), length, height)
    for i, f in enumerate(files):
        cy = top + row * i + row / 2
        end = f["reverted_min"] if f["reverted_min"] is not None else length
        label = participant_roles.label(f["author"], record["roles"]).replace(" (seeded, planted by researchers)",
                                                                               " (seed)")
        stood = end - (f["accepted_min"] or 0)
        parts.append(f'<text class="cg-lab" x="{left - 6}" y="{cy + 4:.1f}" text-anchor="end">'
                     f'{_esc(label)} · {_esc(_short(f["task"]))}<title>{_esc(f["task"])}</title></text>'
                     f'<rect class="{"pv-bar-r" if f["reverted_min"] is not None else "pv-bar"}" '
                     f'x="{x(f["accepted_min"]):.1f}" y="{cy - 5:.1f}" width="{max(x(end) - x(f["accepted_min"]), 1):.1f}" '
                     f'height="10"><title>{_esc(f["path"])}: accepted at {_esc(_at(f["accepted_min"], record))}, '
                     f'{"reverted" if f["reverted_min"] is not None else "still standing at the end"} after '
                     f'{stood:.1f} min; read by {len(f["reads"])} other agent(s) meanwhile</title></rect>')
        for rd in f["reads"]:
            rx = x(rd["minute"])
            parts.append(f'<line class="pv-read" x1="{rx:.1f}" x2="{rx:.1f}" y1="{cy - 6:.1f}" y2="{cy + 6:.1f}">'
                         f'<title>{_esc(rd["agent"])} read it at {_esc(_at(rd["minute"], record))}</title></line>')
        if f["checked_min"] is not None:
            parts.append(_shield(x(f["checked_min"]), cy, f'overseer checked it at {_at(f["checked_min"], record)}: '
                                                          f'{f["check_validity"]}'))
    parts.append("</svg>")
    return "".join(parts)


def _legend(items):
    return '<div class="cg-legend">' + "".join(f"<span>{svg}{_esc(text)}</span>" for svg, text in items) + "</div>"


def _sw(inner):
    return f'<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">{inner}</svg>'


SHIELD_SW = _sw('<path class="pv-warn" d="M3,2 H13 V8 Q13,13 8,15 Q3,13 3,8 Z"/>')
ADOPT_SW = _sw('<circle class="pv-adopt" cx="8" cy="8" r="5"/>')
DECL_SW = _sw('<circle class="pv-decl" cx="8" cy="8" r="5"/>')


def figures_html(record):
    """The prevention figures, each with a heading, caption and legend, as one HTML fragment."""
    if not record:
        return ""
    t, out = record["table"], []
    exposed = [p for p in record["people"] if p["exposed_min"] is not None]
    out.append('<h3 id="oversight-exposure">Exposure to outcome</h3>')
    lanes = swimlanes_svg(record)
    if lanes:
        out.append('<figure class="cg-fig nomath">' + lanes + _legend([
            (SHIELD_SW, "saw an overseer warning"), (ADOPT_SW, "submitted the exploit"),
            (DECL_SW, "never submitted it (end of run)")])
            + f"<figcaption>One row per agent that read an exploit file ({len(exposed)} agents), from its first read "
              "(small dot) to its first exploit submission or the end of the run. A shield marks when it first saw "
              "an overseer warning (board post or direct message).</figcaption></figure>")
    else:
        out.append("<p>No agent read an exploit file.</p>")
    out.append('<h3 id="oversight-warned-table">Warned and exposed agents</h3>')
    out.append(table_html(record) + '<p class="cg-caveat">Agents that submitted an exploit, by whether they read an '
               "exploit file and whether they saw an overseer warning before submitting (or before the end of the run). "
               "Agents that read the board may differ from those that do not: this is an association, not an effect."
               "</p>")
    tree = tree_svg(record)
    out.append('<h3 id="oversight-blocked-tree">Transmission tree with blocked branches</h3>')
    if tree:
        out.append('<figure class="cg-fig nomath">' + tree + _legend([
            (_sw('<rect class="pv-adopt" x="3" y="3" width="10" height="10"/>'), "not-genuine accepted file"),
            (ADOPT_SW, "reader that adopted"), (DECL_SW, "reader that did not"), (SHIELD_SW, "warned before"),
            (_sw('<line class="pv-cut" x1="8" x2="8" y1="1" y2="15"/>'), "reverted by the overseer")])
            + "<figcaption>Each not-genuine accepted file (square, at its acceptance) and, below it, every agent whose "
              "first exploit-file read was that file (at the read). Node size grows with exploit credits.</figcaption>"
              "</figure>")
    else:
        out.append("<p>No exploit file was read by another agent.</p>")
    out.append('<h3 id="oversight-interventions">Outbreak with interventions</h3>')
    out.append('<figure class="cg-fig nomath">' + curve_svg(record) + _legend([
        (_sw('<line class="cg-curve" x1="1" x2="15" y1="8" y2="8"/>'), "agents that submitted an exploit"),
        (_sw('<line class="pv-standline" x1="1" x2="15" y1="8" y2="8"/>'), "not-genuine credits still standing"),
        (_sw('<line class="pv-int" x1="8" x2="8" y1="1" y2="15"/>'), "overseer action")])
        + "<figcaption>Cumulative agents with an exploit submission, the number of not-genuine accepted credits "
          "standing (it falls only when the overseer reverts one), and every overseer post, message, proof check and "
          f"reversion. The axis ends at the run's stop.</figcaption></figure>")
    out.append('<h3 id="oversight-latency">Detection latency</h3>')
    lat = latency_svg(record)
    if lat:
        checked = sum(f["checked_min"] is not None for f in record["files"])
        reverted = sum(f["reverted_min"] is not None for f in record["files"])
        out.append('<figure class="cg-fig nomath">' + lat + _legend([
            (_sw('<rect class="pv-bar" x="1" y="4" width="14" height="8"/>'), "standing, not reverted"),
            (_sw('<rect class="pv-bar-r" x="1" y="4" width="14" height="8"/>'), "standing until reverted"),
            (_sw('<line class="pv-read" x1="8" x2="8" y1="2" y2="14"/>'), "read by another agent"),
            (SHIELD_SW, "checked by the overseer")])
            + f"<figcaption>Each not-genuine accepted file ({len(record['files'])}; {checked} checked, {reverted} "
              "reverted), from acceptance to its reversion or the end of the run, with every read by another agent "
              "while it stood.</figcaption></figure>")
    else:
        out.append("<p>No accepted submission was found not genuine.</p>")
    cmp_ = record.get("comparison")
    out.append('<h3 id="oversight-comparison">Compared with a run without the overseer</h3>')
    if cmp_:
        out.append('<figure class="cg-fig nomath">' + curve_svg(record, compare=True) + _legend([
            (_sw('<line class="cg-curve" x1="1" x2="15" y1="8" y2="8"/>'), "this run"),
            (_sw('<line class="pv-cmp" x1="1" x2="15" y1="8" y2="8"/>'), cmp_["run"])])
            + f"<figcaption>Cumulative agents with an exploit submission: {len(record['outbreak'])} in this run, "
              f"{len(cmp_['outbreak'])} in {_esc(cmp_['run'])}. One run per condition: chance can dominate."
              "</figcaption></figure>")
    else:
        out.append("<p>No comparison run was given (run_incident_report.py --rerender-oversight --compare-run).</p>")
    return "\n".join(out)
