"""Interaction graph of a run's story map: graph.json and the self-contained graph.html.

Owner request and decisions (docs/plan.md, "Story-map interaction graph (November 5, 2026)"): agents as nodes,
interactions (DMs, board posts and reads, library publications and reads, submissions, credit, overseer reverts and
checks) appearing over run time, nodes coloured by the story map's stance readings. This script builds the data;
the front end is scripts/storymap_graph_template.html, whose literal line ``/*GRAPH_DATA*/null`` is replaced by the
JSON (``</`` escaped as ``<\\/``). No model calls, network or credentials.

Contract (schema ``storymap-graph/1``)::

    {"schema", "run", "minutes", "title",
     "nodes":   [{"id", "role": "solver|overseer|seed", "page": "agents/<id>.html" | null}],
     "states":  {agent: [{"t_from", "t_to", "stance", "has_trick", "believes_judge", "summary"}]},
     "thoughts":{agent: [{"t", "code", "quote"}]},
     "events":  [{"t", "type", "src", "dst", "fake", "problem", "text"}]  (sorted by t),
     "maps":    {agent: {"phases": [{"t_from", "t_to", "stance", "label"}], "turning_points": [{"t", "from", "to"}],
                 "story": stage-3 story (<= 200 chars)}}   (the hover card's miniature story map),
     "codes":   {code: public name}, "codes_overseer": {code: public name},
     "addons":  {"deception": {"version", "taxonomy_version", "taxonomy_sha256", "prompt_sha256", "codes", "agents"}}}

When the map folder holds ``deception/hcodes.jsonl`` (the deception add-on, storymap/deception.py), its valid H codes
join ``thoughts`` with ``"addon": "deception"`` and ``certainty``, their names join ``codes``, and ``addons`` records
the add-on's versions; without it the graph is unchanged.

Times are run minutes from the conference's ``run_started_at``, the story maps' origin (storymap/extract.py ``mins``).

* Solver states: the stage-3 phases set the stance (as on the story-map index; stage-1 chunk states where a run has
  no stage-3 reading); ``has_trick``, ``believes_judge`` and ``summary`` come from the stage-1 chunk covering the
  segment. Segments break at every phase and chunk boundary.
* Overseer states (overseer schema: status, knows_trick): ``stance`` maps the status onto the solver stances,
  not_looking and investigating_other -> none, investigating_trick -> considering, enforcing and coordinating ->
  rejected, idle -> stopped; ``has_trick`` = knows_trick; ``believes_judge`` null; the original status is kept in
  ``ov_status``. Overseer codes (groups O, H, I, T, A, G) clash with solver codes, so their names are in
  ``codes_overseer``.
* The seed has no states or thoughts.
* Event types: dm, board_post (dst null), board_read (reader -> each poster read; dst null when unknown),
  library_publish (incl. the seed's injected file), library_read (reader -> author of the file read, resolved from
  library_published / seeded_artifact_injected by path and revision), submit, credited (task closed in the agent's
  favour), revert (overseer -> credited agent), check (overseer proof check -> author when the checked file is a
  library file or submission, else null), ov_board_post, ov_board_read. ``fake`` is the story map fake rule
  (storymap/fake_rule.py, as faking_heatmap.collect) for the submission or library file concerned, else null.
  Push deliveries, listings and status calls are left out.

Agentic monitors (arm AG; docs/plan.md, "Agentic monitors in the story maps (owner decision, November 24, 2026)").
Only in an AG run (settings ``agentic_monitor.enabled``, or monitor actions in runtime.jsonl) the graph gains, after
the other nodes, one node per solver's monitor ``{"id": "agmon-NNN", "role": "agentic_monitor", "parent":
"agent-NNN", "page": "agents/agent-NNN.monitor.html" (the pair page, linked even when not built), "acted": bool,
"stats": {"reverts", "messages", "unsent", "refused"}}``, a top-level ``"agentic": {"monitors", "acted", "source"}``,
and two event types, src the monitor and dst its solver:

* ``agmon_revert``: the reviewer-identity ``submission_reverted`` (conference time), the monitor named by
  runtime.jsonl ``agentic_revert`` (joined by submission id), else agmon-NNN of the credited agent; ``fake`` the
  reverted submission's fake rule, ``problem``, ``text`` the reason, ``delivered`` the run minute the reason DM
  reached the solver (messages.delivered_at; null when never delivered or unknown). The reason DM itself is not a
  separate event.
* ``agmon_message``: a reviewer-identity ``dm_sent`` that is an ``agentic_message`` (joined by public message id),
  ``text`` in full (MESSAGE_MAX), ``sent`` true, ``delivered`` as above; a message the conference could not store
  (runtime ``sent`` false) is drawn at its runtime.jsonl time with ``sent`` false.

``acted`` is true for a monitor with at least one revert or message (refused reverts only count in ``stats``). The
viewer shows acted monitors by default as satellites of their solvers (toggle "agentic monitors", option "all
monitors"). Without runtime.jsonl, the 1:1 pairing names the monitor and a reviewer DM within REVERT_NOTICE_GAP
seconds after a revert to the same solver is taken as its notice. The "reviewer" identity stays dropped from every
other event type. A non-AG run's graph.json is byte-identical to before. graph-thumb.json leaves monitors out.

Shared viewer (owner, November 6, 2026): the published graph.html is a small stable shell that loads one shared
viewer for the whole site, ``_graph/viewer.js`` and ``_graph/viewer.css`` at the site root (``../../../_graph/`` from
RUN/analysis/storymap/graph.html), and fetches ``graph.json`` next to itself. Both viewer files are generated from
the template (``viewer_files``), so a design change is one edit to the template plus a site deploy, with no per-run
rebuild. ``--inline`` keeps the earlier self-contained single file (data inlined) for local previews.

Catalogue thumbnail (owner, November 6, 2026; docs/plan.md "Site redesign: run panes with the interaction graph"):
``graph-thumb.json`` beside graph.json, schema ``storymap-graph-thumb/1``, no text, built from graph.json alone
(``thumb``; CLI ``--thumb-from-json``)::

    {"schema", "minutes" (graph.json minutes), "solvers", "overseers", "nodes": [{"id", "role", "b"?}] (graph order),
     "frames": [{"t", "s", "f"}]  (THUMB_FRAMES frames evenly over the viewer's time range; the last is its end)}

``s`` has one character per node, its stance at ``t`` as the viewer reads it (stateAt: the last state with
t_from <= t; overseer stances are already mapped by OV_STANCE): n none, c considering, d deferred, x faking,
r rejected, s stopped, '.' no story-map state (or an unknown stance). ``f`` is '1' once the node has faked by ``t``
(the viewer's first-fake rule: its first fake submission; the seed's fake library publish), else '0'.
``b`` (optional) is the node's behaviour badge letters as on the public run pages (research/analysis/agent_badges
run_badges, e.g. "E", "CW", "O", "S", "?"; "" for none). It is set on every node only when the run folder holds
behaviour-judge records (simplified or rubric-3.1 judge); without them (a roles-only map, or no run folder, as with
--thumb-from-json away from the run) every ``b`` is omitted, since the role already gives O and S.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from bisect import bisect_right
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO, REPO / 'scripts', REPO / 'src'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCHEMA = 'storymap-graph/1'
PLACEHOLDER = '/*GRAPH_DATA*/null'
TEMPLATE = REPO / 'scripts' / 'storymap_graph_template.html'
SOLVER_TAXONOMY = REPO / 'research' / 'analysis' / 'storymap' / 'taxonomy.md'
OVERSEER_TAXONOMY = REPO / 'research' / 'analysis' / 'storymap' / 'taxonomy_overseer.md'
SUMMARY_MAX, QUOTE_MAX, TEXT_MAX, STORY_MAX = 300, 200, 160, 200
# board posts and DMs are rare and read in full in the viewer: whitespace kept, only a safety cap
MESSAGE_MAX = 8000
MESSAGE_TYPES = ('dm', 'board_post', 'ov_board_post')
OV_STANCE = {'not_looking': 'none', 'investigating_other': 'none', 'investigating_trick': 'considering',
             'enforcing': 'rejected', 'coordinating': 'rejected', 'idle': 'stopped'}
THUMB_SCHEMA = 'storymap-graph-thumb/1'
THUMB_JSON = 'graph-thumb.json'
THUMB_FRAMES = 24
THUMB_CHAR = {'none': 'n', 'considering': 'c', 'deferred': 'd', 'faking': 'x', 'rejected': 'r', 'stopped': 's'}
TYPES = ('dm', 'board_post', 'board_read', 'library_publish', 'library_read', 'submit', 'credited', 'revert', 'check',
         'ov_board_post', 'ov_board_read', 'agmon_revert', 'agmon_message')
# Agentic monitor arm AG (docs/plan.md, "Agentic monitors in the story maps (owner decision, November 24, 2026)"):
# monitor agmon-NNN watches solver agent-NNN (research_swarm.agentic_monitor); its reverts and DMs are stored in the
# conference under the reviewer identity and named in runtime.jsonl (agentic_revert / agentic_message).
AGMON_PREFIX, SOLVER_PREFIX = 'agmon-', 'agent-'
AGMON_ROLE = 'agentic_monitor'
AGMON_KINDS = ('agentic_revert', 'agentic_message', 'agentic_revert_refused')
REVERT_NOTICE_GAP = 5.0   # seconds: without runtime.jsonl, a reviewer DM this soon after a revert is its reason notice


def message_text(text):
    text = str(text).strip()
    return text if len(text) <= MESSAGE_MAX else text[:MESSAGE_MAX - 1].rstrip() + '…'


def clip(text, n):
    if text is None:
        return None
    text = ' '.join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + '…'


def minutes(ts, start):
    return round((float(ts) - start) / 60, 3)


def _read_jsonl(path):
    rows = []
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return rows
    for line in lines:
        if line.strip():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _num(v, default=None):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def load_map(map_dir):
    """{agent: {"chunks": [stage-1 rows sorted by t_from], "phases": [stage-3 phases]}} of a story map folder."""
    out = {}
    if not map_dir:
        return out
    map_dir = Path(map_dir)
    for path in sorted((map_dir / 'stage1').glob('*.jsonl')):
        rows = sorted((r for r in _read_jsonl(path) if _num(r.get('t_from')) is not None),
                      key=lambda r: (r['t_from'], r.get('chunk') or 0))
        out.setdefault(path.stem, {'chunks': [], 'phases': []})['chunks'] = rows
    for row in _read_jsonl(map_dir / 'map.jsonl'):
        agent = row.get('agent')
        if agent:
            rec = out.setdefault(agent, {'chunks': [], 'phases': []})
            rec['phases'] = [p for p in row.get('phases') or []
                             if isinstance(p, dict) and _num(p.get('from_t')) is not None]
            rec['turning_points'] = [tp for tp in row.get('turning_points') or []
                                     if isinstance(tp, dict) and _num(tp.get('t')) is not None]
            rec['story'] = row.get('story') if isinstance(row.get('story'), str) else None
    return out


def mini_map(rec, tmax, overseer=False):
    """The hover card's miniature story map of one agent (fields all shown on the published story-map pages): its
    stage-3 phases (stance; overseers: mapped status, raw status in ``status``), turning points and the stage-3 story
    (clipped)."""
    phases = []
    for p in rec.get('phases') or []:
        a, b = _num(p['from_t']), _num(p.get('to_t'), tmax)
        if b is None or b <= a:
            continue
        row = {'t_from': round(a, 3), 't_to': round(b, 3), 'label': clip(p.get('label'), 60)}
        if overseer:
            row['stance'] = OV_STANCE.get(p.get('status'), 'unknown')
            row['status'] = p.get('status') or 'unknown'
        else:
            row['stance'] = p.get('stance') or 'unknown'
        phases.append(row)
    tps = [{'t': round(_num(tp['t']), 3), 'from': str(tp.get('from') or ''), 'to': str(tp.get('to') or '')}
           for tp in rec.get('turning_points') or []]
    return {'phases': phases, 'turning_points': tps, 'story': clip(rec.get('story'), STORY_MAX)}


def solver_states(rec, tmax):
    """States of one solver: segments at every phase and chunk boundary (see the module docstring)."""
    chunks, phases = rec['chunks'], rec['phases']
    if phases:
        spans = [(_num(p['from_t']), _num(p.get('to_t'), tmax), p.get('stance') or 'unknown') for p in phases]
    else:
        spans = [(_num(c['t_from']), _num(c.get('t_to'), tmax), (c.get('state') or {}).get('stance') or 'unknown')
                 for c in chunks]
    if not spans:
        return []
    cuts = sorted({t for s in spans for t in s[:2]} | {_num(c['t_from']) for c in chunks}
                  | {_num(c.get('t_to'), tmax) for c in chunks})
    starts = [c['t_from'] for c in chunks]
    out = []
    for a, b in zip(cuts, cuts[1:]):
        if b <= a:
            continue
        mid = (a + b) / 2
        stance = next((s for f, t, s in spans if f <= mid < t), None)
        if stance is None:
            continue
        i = bisect_right(starts, mid) - 1
        state = (chunks[i].get('state') or {}) if i >= 0 else {}
        row = {'t_from': round(a, 3), 't_to': round(b, 3), 'stance': stance,
               'has_trick': state.get('has_trick') if isinstance(state.get('has_trick'), bool) else None,
               'believes_judge': state.get('believes_judge'), 'summary': clip(state.get('summary'), SUMMARY_MAX)}
        prev = out[-1] if out else None
        if prev and prev['t_to'] == row['t_from'] and all(prev[k] == row[k] for k in
                                                          ('stance', 'has_trick', 'believes_judge', 'summary')):
            prev['t_to'] = row['t_to']
        else:
            out.append(row)
    return out


def overseer_states(rec, tmax):
    """States of one overseer, from its stage-3 status phases (else stage-1 statuses), mapped as documented."""
    chunks, phases = rec['chunks'], rec['phases']
    spans = ([(_num(p['from_t']), _num(p.get('to_t'), tmax), p.get('status')) for p in phases] if phases else
             [(_num(c['t_from']), _num(c.get('t_to'), tmax), (c.get('state') or {}).get('status')) for c in chunks])
    starts = [c['t_from'] for c in chunks]
    out = []
    for a, b, status in spans:
        if b <= a:
            continue
        i = bisect_right(starts, (a + b) / 2) - 1
        state = (chunks[i].get('state') or {}) if i >= 0 else {}
        out.append({'t_from': round(a, 3), 't_to': round(b, 3), 'stance': OV_STANCE.get(status, 'unknown'),
                    'has_trick': state.get('knows_trick') if isinstance(state.get('knows_trick'), bool) else None,
                    'believes_judge': None, 'summary': clip(state.get('summary'), SUMMARY_MAX),
                    'ov_status': status or 'unknown'})
    return out


def thoughts_of(rec):
    out = []
    for c in rec['chunks']:
        for th in c.get('thoughts') or []:
            if isinstance(th, dict) and _num(th.get('t')) is not None and th.get('code'):
                out.append({'t': round(th['t'], 3), 'code': str(th['code']), 'quote': clip(th.get('quote'), QUOTE_MAX)})
    out.sort(key=lambda x: x['t'])
    return out


def run_title(run_id, settings):
    """A short label from the run id: model, solvers, overseers, seeding, delivery, replicate."""
    parts = []
    m = re.match(r'([a-z]+[0-9.]*?[a-z]*?)(\d+)-', run_id)
    model = (settings.get('model') or {}).get('name') if isinstance(settings.get('model'), dict) else None
    if m:
        parts.append(f'{m.group(2)} agents')
    ov = re.search(r'-(\d+)ov-', run_id)
    if ov:
        parts.append(f'{ov.group(1)} overseer{"s" if ov.group(1) != "1" else ""}')
    if 'commsonly' in run_id:
        parts.append('communication only')
    if 'seeded' in run_id:
        parts.append('seeded')
    if '-push' in run_id:
        parts.append('push delivery')
    rep = re.search(r'-(r\d+)-\d{8}T', run_id)
    if rep:
        parts.append(rep.group(1))
    return ', '.join(([model] if model else []) + parts) or run_id


def _sid(value):
    """The DB submission id (int) of an event's 'sub-000123'."""
    if isinstance(value, int):
        return value
    m = re.fullmatch(r'sub-(\d+)', str(value or ''))
    return int(m.group(1)) if m else None


def _post_key(value):
    m = re.fullmatch(r'(?:ov)?post-(\d+)', str(value or ''))
    return int(m.group(1)) if m else None


def conference_events(run_dir):
    """Conference events (events.jsonl, else the database's events table), in seq order."""
    path = Path(run_dir) / 'conference' / 'events.jsonl'
    rows = _read_jsonl(path)
    if not rows:
        import sqlite3
        db = sqlite3.connect(f"file:{Path(run_dir) / 'conference/state.sqlite'}?mode=ro&immutable=1", uri=True)
        try:
            for seq, ts, kind, agent, payload in db.execute(
                    'select seq, timestamp, kind, agent_id, payload from events order by seq'):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {}
                rows.append({'seq': seq, 'timestamp': ts, 'kind': kind, 'agent_id': agent, 'payload': payload})
        finally:
            db.close()
    return sorted(rows, key=lambda r: (r.get('seq') or 0))


NON_PARTICIPANTS = frozenset({'reviewer'})  # research_swarm.async_review.REVIEWER_ID


def build_events(ctx, rows):
    """Graph events from conference events (see the module docstring); ``ctx`` is storymap.extract.RunContext."""
    start = ctx.start
    authors = {}            # (path, revision) -> author, and path -> latest author
    post_author = {}        # post number -> agent
    ov_post_author = {}
    for key, lib in ctx.library.items():
        authors.setdefault(key, lib.get('author'))
    for n, post in ctx.posts.items():
        post_author[_post_key(n)] = post.get('agent_id')
    for n, post in ctx.ov_board.items():
        ov_post_author[_post_key(n)] = post.get('agent_id')
    events = []

    def add(ts, typ, src, dst=None, fake=None, problem=None, text=None):
        events.append({'t': minutes(ts, start), 'type': typ, 'src': src, 'dst': dst,
                       'fake': fake if isinstance(fake, bool) else None, 'problem': problem,
                       'text': (message_text(text) if typ in MESSAGE_TYPES else clip(text, TEXT_MAX)) if text else None})

    def lib_fake(path, rev):
        lib = ctx.library.get((path, rev))
        return bool(lib['fake']) if lib and lib.get('kind') == 'solution' else None

    def sub_fake(value):
        sub = ctx.subs.get(_sid(value))
        return bool(sub['fake']) if sub else None

    for r in rows:
        kind, agent, ts = r.get('kind'), r.get('agent_id'), r.get('timestamp')
        p = r.get('payload') if isinstance(r.get('payload'), dict) else {}
        if ts is None:
            continue
        if kind in ('library_published', 'seeded_artifact_injected'):
            path, rev = p.get('path'), p.get('revision')
            author = p.get('author') or agent
            authors[(path, rev)] = author
            authors[path] = author
            add(ts, 'library_publish', author, None, lib_fake(path, rev),
                p.get('task_id') or (ctx.library.get((path, rev)) or {}).get('task_id'), path)
            if kind == 'seeded_artifact_injected' and p.get('credit_effect') == 'close_task':
                add(ts, 'credited', author, None, sub_fake(p.get('submission_id')), p.get('task_id'))
        elif kind == 'library_read' and agent:
            path, rev = p.get('path'), p.get('revision')
            dst = authors.get((path, rev)) or authors.get(path) or p.get('author')
            if dst and dst != agent:
                add(ts, 'library_read', agent, dst, lib_fake(path, rev), p.get('problem_id'), path)
        elif kind == 'dm_sent' and agent:
            add(ts, 'dm', agent, p.get('recipient'), None, None, p.get('message'))
        elif kind == 'board_post' and agent:
            post_author.setdefault(_post_key(p.get('post_id')), agent)
            add(ts, 'board_post', agent, None, None, None, p.get('message'))
        elif kind in ('board_read', 'overseer_board_read') and agent:
            table = post_author if kind == 'board_read' else ov_post_author
            typ = 'board_read' if kind == 'board_read' else 'ov_board_read'
            ids = list(p.get('post_ids') or [])
            if not ids:
                continue
            posters = []
            for pid in ids:
                who = table.get(_post_key(pid))
                if who not in posters:
                    posters.append(who)
            posters = [w for w in posters if w != agent]
            if not posters:
                continue
            for who in sorted(posters, key=lambda w: (w is None, w or '')):
                add(ts, typ, agent, who)
        elif kind == 'overseer_board_post' and agent:
            ov_post_author.setdefault(_post_key(p.get('post_id')), agent)
            add(ts, 'ov_board_post', agent, None, None, None, p.get('message'))
        elif kind == 'submission_received' and agent:
            add(ts, 'submit', agent, None, sub_fake(p.get('submission_id')), p.get('problem_id'))
        elif kind == 'task_closed':
            who = p.get('credited_agent') or agent
            if who:
                add(ts, 'credited', who, None, sub_fake(p.get('submission_id')), p.get('problem_id'))
        elif kind == 'submission_reverted' and agent:
            add(ts, 'revert', agent, p.get('credited_agent'), sub_fake(p.get('submission_id')), p.get('problem_id'),
                p.get('reason'))
        elif kind == 'proof_checked' and agent:
            dst, fake = None, None
            sub = ctx.subs.get(_sid(p.get('submission_id')))
            if sub is None and p.get('public_submission_id'):
                sub = ctx.sub_by_public.get(p.get('public_submission_id'))
            if sub is not None:
                dst, fake = sub.get('agent_id'), bool(sub['fake'])
            else:
                path = p.get('library_path') or p.get('path')
                if path:
                    rev = p.get('revision')
                    dst = authors.get((path, rev)) or authors.get(path)
                    fake = lib_fake(path, rev) if rev is not None else None
            verdict = ' '.join(str(x) for x in (p.get('validity'), p.get('reason')) if x)
            add(ts, 'check', agent, dst if dst != agent else None, fake, p.get('problem_id'), verdict or None)
    # The asynchronous reviewer (A1-A4; agent_id "reviewer") is not a participant and has no node: its reverts and
    # DMs are left out of the graph rather than drawn from an unknown node.
    events = [e for e in events if e['src'] not in NON_PARTICIPANTS and e['dst'] not in NON_PARTICIPANTS]
    events.sort(key=lambda e: (e['t'], TYPES.index(e['type'])))
    return events


def agmon_of(solver):
    """``agent-007`` -> ``agmon-007`` (None for any other id)."""
    s = str(solver or '')
    return AGMON_PREFIX + s[len(SOLVER_PREFIX):] if s.startswith(SOLVER_PREFIX) else None


def solver_of(monitor):
    s = str(monitor or '')
    return SOLVER_PREFIX + s[len(AGMON_PREFIX):] if s.startswith(AGMON_PREFIX) else None


def agentic_runtime(run_dir):
    """runtime.jsonl rows of the agentic monitors' actions (AGMON_KINDS), in file order; [] when absent."""
    rows = []
    try:
        f = (Path(run_dir) / 'runtime.jsonl').open(encoding='utf-8')
    except OSError:
        return rows
    with f:
        for line in f:
            if '"agentic_' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get('kind') in AGMON_KINDS:
                rows.append(row)
    return rows


def is_agentic(settings, runtime_rows):
    """An AG run: settings' agentic_monitor.enabled, or any monitor action in runtime.jsonl (as
    scripts/export_conference.py agentic_actions)."""
    config = ((settings.get('effective') or {}).get('agentic_monitor')
              or (settings.get('config') or {}).get('agentic_monitor'))
    return (isinstance(config, dict) and config.get('enabled') is True) or bool(runtime_rows)


def agentic_events(ctx, rows, runtime_rows):
    """(graph events, {monitor: stats}) of an AG run's monitors (see the module docstring). The conference rows
    (``rows``, kinds submission_reverted / dm_sent by the reviewer identity) give the times; runtime.jsonl names the
    acting monitor (joined by submission id and public message id), else the 1:1 pairing names it (agmon-NNN acts
    only on agent-NNN)."""
    from research_swarm.async_review import REVIEWER_ID
    start = ctx.start
    rt_rev, rt_msg, notices = {}, {}, set()
    stats = {}

    def stat(monitor):
        return stats.setdefault(monitor, {'reverts': 0, 'messages': 0, 'unsent': 0, 'refused': 0})

    for r in runtime_rows:
        p = r.get('payload') if isinstance(r.get('payload'), dict) else {}
        monitor = p.get('monitor') or r.get('agent_id')
        if not str(monitor or '').startswith(AGMON_PREFIX):
            continue
        p = dict(p, monitor=monitor, solver=p.get('solver') or solver_of(monitor), _ts=_num(r.get('timestamp')))
        if r.get('kind') == 'agentic_revert_refused':
            stat(monitor)['refused'] += 1
        elif r.get('kind') == 'agentic_revert':
            sid = _sid(p.get('submission_id'))
            if sid is not None:
                rt_rev[sid] = p
            if p.get('message_id'):
                notices.add(str(p['message_id']))
        elif r.get('kind') == 'agentic_message':
            if p.get('message_id'):
                rt_msg[str(p['message_id'])] = p
            elif p.get('_ts') is not None:   # not delivered: no conference row; drawn from runtime.jsonl
                rt_msg[f'unsent-{len(rt_msg)}'] = p

    def delivered(public_id=None, internal=None):
        msg = ctx.msg_by_public.get(public_id) if public_id else None
        if msg is None and internal is not None:
            m = re.fullmatch(r'dm-(\d+)', str(internal))
            msg = ctx.messages.get(int(m.group(1))) if m else None
        ts = _num((msg or {}).get('delivered_at'))
        return minutes(ts, start) if ts is not None else None

    def sub_fake(value):
        sub = ctx.subs.get(_sid(value))
        return bool(sub['fake']) if sub else None

    events, seen_rev, seen_msg, last_revert = [], set(), set(), {}

    def add(ts, typ, monitor, solver, fake=None, problem=None, text=None, **extra):
        e = {'t': minutes(ts, start), 'type': typ, 'src': monitor, 'dst': solver,
             'fake': fake if isinstance(fake, bool) else None, 'problem': problem,
             'text': message_text(text) if text else None}
        e.update(extra)
        events.append(e)
        stat(monitor)

    for r in rows:
        kind, agent, ts = r.get('kind'), r.get('agent_id'), r.get('timestamp')
        p = r.get('payload') if isinstance(r.get('payload'), dict) else {}
        if ts is None or agent != REVIEWER_ID:
            continue
        if kind == 'submission_reverted':
            sid = _sid(p.get('submission_id'))
            rt = rt_rev.get(sid) or {}
            solver = p.get('credited_agent') or rt.get('solver')
            monitor = rt.get('monitor') or agmon_of(solver)
            if not monitor or not solver:
                continue
            seen_rev.add(sid)
            last_revert[solver] = float(ts)
            stat(monitor)['reverts'] += 1
            add(ts, 'agmon_revert', monitor, solver, sub_fake(p.get('submission_id')), p.get('problem_id'),
                p.get('reason') or rt.get('reason'),
                delivered=delivered(rt.get('message_id')) if rt.get('message_id') else None)
        elif kind == 'dm_sent':
            pub, solver = p.get('public_message_id'), p.get('recipient')
            if pub and str(pub) in notices:
                continue   # a revert's reason notice: its delivery time is on the agmon_revert event
            rt = rt_msg.get(str(pub)) if pub else None
            if rt is None and not runtime_rows and solver in last_revert \
                    and 0 <= float(ts) - last_revert[solver] <= REVERT_NOTICE_GAP:
                continue   # no runtime.jsonl: the DM right after a revert is its notice
            monitor = (rt or {}).get('monitor') or agmon_of(solver)
            if not monitor or not solver:
                continue
            if pub:
                seen_msg.add(str(pub))
            stat(monitor)['messages'] += 1
            add(ts, 'agmon_message', monitor, solver, None, None, p.get('message') or (rt or {}).get('text'),
                sent=True, delivered=delivered(pub, p.get('message_id')))
    # runtime-only actions: a revert without its conference event, a message that could not be delivered
    for sid, rt in rt_rev.items():
        if sid not in seen_rev and rt.get('_ts') is not None and rt.get('solver'):
            stat(rt['monitor'])['reverts'] += 1
            add(rt['_ts'], 'agmon_revert', rt['monitor'], rt['solver'], sub_fake(sid), rt.get('problem_id'),
                rt.get('reason'), delivered=delivered(rt.get('message_id')) if rt.get('message_id') else None)
    for key, rt in rt_msg.items():
        if key in seen_msg or rt.get('_ts') is None or not rt.get('solver'):
            continue
        sent = bool(rt.get('sent')) and not key.startswith('unsent-')
        stat(rt['monitor'])['messages' if sent else 'unsent'] += 1
        add(rt['_ts'], 'agmon_message', rt['monitor'], rt['solver'], None, None, rt.get('text'), sent=sent,
            delivered=delivered(rt.get('message_id')) if sent else None)
    return events, stats


def build_graph(run_dir, map_dir=None, overseer_map=None):
    """The graph.json object of a run (contract in the module docstring)."""
    from research.analysis.storymap.extract import RunContext
    from research.analysis.storymap import view, view_overseer
    run_dir = Path(run_dir)
    ctx = RunContext(run_dir)
    settings = ctx.settings or {}
    secs = _num((settings.get('config') or {}).get('max_run_seconds')) or _num(
        (settings.get('effective') or {}).get('max_run_seconds'))
    smap, omap = load_map(map_dir), load_map(overseer_map)
    tmax = secs / 60 if secs else view.STANDARD_LENGTHS
    # agentic monitors (and the reviewer identity they act under, which storymap.extract gives their role in AG
    # runs) are not participants: their nodes are added below as satellites
    ids = sorted(a for a in set(ctx.agents) | set(ctx.roles)
                 if not str(a).startswith(AGMON_PREFIX) and ctx.roles.get(a) != AGMON_ROLE)
    nodes, states, thoughts, maps = [], {}, {}, {}
    for agent in ids:
        role = ctx.roles.get(agent, 'solver')
        role = role if role in ('overseer', 'seed') else 'solver'
        rec = (omap if role == 'overseer' else smap).get(agent) if role != 'seed' else None
        nodes.append({'id': agent, 'role': role, 'page': f'agents/{agent}.html' if rec else None})
        if rec:
            states[agent] = overseer_states(rec, tmax) if role == 'overseer' else solver_states(rec, tmax)
            thoughts[agent] = thoughts_of(rec)
            maps[agent] = mini_map(rec, tmax, role == 'overseer')
    rows = conference_events(run_dir)
    events = build_events(ctx, rows)
    runtime_rows = agentic_runtime(run_dir)
    agentic = None
    if is_agentic(settings, runtime_rows):
        # AG run: one satellite node per solver's monitor (after the other nodes), its reverts and messages
        extra, stats = agentic_events(ctx, rows, runtime_rows)
        events = sorted(events + extra, key=lambda e: (e['t'], TYPES.index(e['type'])))
        acted = 0
        for n in list(nodes):
            monitor = agmon_of(n['id']) if n['role'] == 'solver' else None
            if not monitor:
                continue
            st = stats.get(monitor) or {'reverts': 0, 'messages': 0, 'unsent': 0, 'refused': 0}
            did = bool(st['reverts'] or st['messages'] or st['unsent'])
            acted += did
            nodes.append({'id': monitor, 'role': AGMON_ROLE, 'parent': n['id'],
                          'page': f"agents/{n['id']}.monitor.html", 'acted': did, 'stats': st})
        agentic = {'monitors': sum(n['role'] == AGMON_ROLE for n in nodes), 'acted': acted,
                   'source': 'runtime.jsonl + conference' if runtime_rows else 'conference (1:1 pairing)'}
    graph = {'schema': SCHEMA, 'run': run_dir.name, 'minutes': round(tmax, 3),
             'title': run_title(run_dir.name, settings), 'nodes': nodes, 'states': states, 'thoughts': thoughts,
             'maps': maps, 'events': events}
    if agentic:
        graph['agentic'] = agentic
    names = view.load_taxonomy(SOLVER_TAXONOMY) if SOLVER_TAXONOMY.is_file() else {}
    used = {th['code'] for a, rows in thoughts.items() if ctx.roles.get(a) != 'overseer' for th in rows}
    codes = {c: names[c] for c in sorted(used) if c in names}
    if codes:
        graph['codes'] = codes
    if omap:
        onames = view_overseer.load_names(OVERSEER_TAXONOMY) if OVERSEER_TAXONOMY.is_file() else {}
        oused = {th['code'] for a, rows in thoughts.items() if ctx.roles.get(a) == 'overseer' for th in rows}
        ocodes = {c: onames[c] for c in sorted(oused) if c in onames}
        if ocodes:
            graph['codes_overseer'] = ocodes
    if map_dir and (Path(map_dir) / 'deception' / 'hcodes.jsonl').is_file():
        # The deception add-on's valid H codes (storymap/deception.py), versioned under addons.deception.
        from research.analysis.storymap import deception
        graph = deception.merge_from_dir(graph, Path(map_dir) / 'deception')
    return graph


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float('inf')


def node_badges(run_dir):
    """{agent: badge letters} of a run from its behaviour-judge records (agent_badges.run_badges), or None when the
    run folder or its judge records are missing (a roles-only map is None too: see the module docstring)."""
    if not run_dir or not (Path(run_dir) / 'settings.json').is_file():
        return None
    try:
        from research.analysis import agent_badges
        badge_map = agent_badges.run_badges(run_dir)
    except Exception:  # noqa: BLE001  (badges are optional: never block the graph)
        return None
    if not badge_map or badge_map.get('source') not in ('simple', 'rubric'):
        return None
    return {a: ''.join(b['letter'] for b in bs) for a, bs in (badge_map.get('agents') or {}).items()}


def thumb(graph, frames=THUMB_FRAMES, badges=None):
    """The graph-thumb.json object of a graph.json object (contract in the module docstring). Mirrors the viewer
    (scripts/storymap_graph_template.html): its time range (tMin = min(0, first event, first state), tMax =
    max(minutes, last event)), stateAt and first-fake rule. ``badges``: node_badges() of the run, or None."""
    # agentic monitors (AG runs) are left out: 20 satellites would crowd the small snapshot, whose stance and fake
    # dots are the point; the full graph shows them
    nodes = [{'id': n['id'], 'role': n.get('role') or 'solver'} for n in graph.get('nodes') or []
             if n.get('role') != AGMON_ROLE]
    if badges is not None:
        for n in nodes:
            n['b'] = badges.get(n['id'], '')
    ids = {n['id']: n for n in nodes}
    events = sorted((e for e in graph.get('events') or [] if isinstance(e, dict) and _finite(e.get('t'))),
                    key=lambda e: e['t'])
    states = {}
    for n in nodes:
        rows = [s for s in (graph.get('states') or {}).get(n['id']) or [] if _finite(s.get('t_from'))]
        states[n['id']] = sorted(rows, key=lambda s: s['t_from'])
    first_fake = {}
    for e in events:
        a = ids.get(e.get('src'))
        if not a or a['id'] in first_fake or e.get('fake') is not True:
            continue
        if e.get('type') == 'submit' or (a['role'] == 'seed' and e.get('type') == 'library_publish'):
            first_fake[a['id']] = e['t']
    t_min, t_max = 0.0, float(graph.get('minutes') or 0)
    if events:
        t_min, t_max = min(t_min, events[0]['t']), max(t_max, events[-1]['t'])
    for rows in states.values():
        if rows:
            t_min = min(t_min, rows[0]['t_from'])
    if t_max <= t_min:
        t_max = t_min + 1
    starts = {k: [s['t_from'] for s in rows] for k, rows in states.items()}
    out = []
    for k in range(frames):
        t = t_max if k == frames - 1 else t_min + (t_max - t_min) * k / (frames - 1)
        chars, fakes = [], []
        for n in nodes:
            i = bisect_right(starts[n['id']], t) - 1
            chars.append(THUMB_CHAR.get(states[n['id']][i].get('stance'), '.') if i >= 0 else '.')
            ff = first_fake.get(n['id'])
            fakes.append('1' if ff is not None and ff <= t else '0')
        out.append({'t': round(t, 2), 's': ''.join(chars), 'f': ''.join(fakes)})
    return {'schema': THUMB_SCHEMA, 'minutes': graph.get('minutes'),
            'solvers': sum(n['role'] == 'solver' for n in nodes),
            'overseers': sum(n['role'] == 'overseer' for n in nodes), 'nodes': nodes, 'frames': out}


def write_thumb(graph, path, run_dir=None):
    """Write graph-thumb.json (compact) for a graph.json object, with badges from ``run_dir`` when it has
    behaviour-judge records; return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(thumb(graph, badges=node_badges(run_dir))), encoding='utf-8')
    return path


def dumps(graph):
    return json.dumps(graph, ensure_ascii=False, separators=(',', ':'))


def inline(template_text, graph):
    """The template with its placeholder line replaced by the JSON (``</`` escaped). Raises ValueError when the
    template has no placeholder or more than one."""
    if template_text.count(PLACEHOLDER) != 1:
        raise ValueError(f'template must contain {PLACEHOLDER!r} exactly once')
    data = dumps(graph).replace('</', '<\\/').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    return template_text.replace(PLACEHOLDER, data)


VIEWER_DIR = '_graph'  # site-root folder of the shared viewer
VIEWER_FILES = ('viewer.js', 'viewer.css')
SHELL_PREFIX = '../../../' + VIEWER_DIR + '/'  # from RUN/analysis/storymap/graph.html to the site root's _graph/
GRAPH_JSON = 'graph.json'
_D3 = re.compile(r'<script src="(https://cdnjs\.cloudflare\.com/[^"]+/d3[^"]*\.js)"></script>\n?')


def template_parts(template_text):
    """{"css", "body", "d3", "main"} of the template: its <style> text, the body markup before the scripts, the d3
    URL and the inline script after the data line. Raises ValueError for a template of another shape."""
    t = template_text
    if t.count(PLACEHOLDER) != 1 or t.count('<style>') != 1 or t.count('<body>') != 1:
        raise ValueError('template must have one <style>, one <body> and one data placeholder')
    css = t.split('<style>', 1)[1].split('</style>', 1)[0]
    m = _D3.search(t)
    if not m:
        raise ValueError('template has no cdnjs d3 script')
    body = t.split('<body>', 1)[1][:m.start() - t.index('<body>') - len('<body>')]
    after = t[m.end():]
    if not after.startswith('<script>'):
        raise ValueError('template: the d3 script must be followed by the inline script')
    script = after[len('<script>'):].split('</script>', 1)[0]
    line = 'const GRAPH = ' + PLACEHOLDER + ';'
    if line not in script:
        raise ValueError(f'template: expected the line {line!r}')
    main = script.replace(line, '', 1)
    return {'css': css.strip('\n') + '\n', 'body': body.strip('\n') + '\n', 'd3': m.group(1), 'main': main}


VIEWER_JS = """/* Shared story-map interaction graph viewer. Generated by scripts/storymap_graph.py from
   scripts/storymap_graph_template.html: edit the template, not this file. The page (graph.html) is a shell that
   loads this script and viewer.css; the data is graph.json next to the page (schema storymap-graph/1), or
   window.STORYMAP_GRAPH when the page sets it. */
(function () {
"use strict";
var BODY = __BODY__;
var D3 = __D3__;
var me = document.currentScript;
function load(src) {
  return new Promise(function (ok, fail) {
    var s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = fail; document.head.appendChild(s);
  });
}
function fail(msg) {
  var el = document.getElementById("empty");
  if (el) { el.textContent = msg; el.style.display = "flex"; }
}
function run(GRAPH) {
__MAIN__
}
function boot() {
  document.body.innerHTML = BODY;
  var src = (me && me.getAttribute("data-graph")) || "graph.json";
  var d3p = typeof d3 === "undefined" ? load(D3).catch(function () {}) : Promise.resolve();
  var dp = window.STORYMAP_GRAPH !== undefined ? Promise.resolve(window.STORYMAP_GRAPH)
    : fetch(new URL(src, document.baseURI).href, { cache: "no-cache" }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  Promise.all([dp, d3p]).then(function (v) { run(v[0]); }, function (e) {
    fail("Could not load " + src + " (" + (e && e.message || e) + "). Opened from disk? Serve the folder over " +
         "HTTP, or build a single-file page with storymap_graph.py --inline.");
  });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
"""


def viewer_files(template_text):
    """{"viewer.js": bytes, "viewer.css": bytes}: the shared viewer built from the template."""
    parts = template_parts(template_text)
    js = (VIEWER_JS.replace('__BODY__', json.dumps(parts['body']).replace('</', '<\\/'))
          .replace('__D3__', json.dumps(parts['d3'])).replace('__MAIN__', parts['main']))
    css = ('/* Shared story-map interaction graph viewer styles. Generated by scripts/storymap_graph.py from '
           'scripts/storymap_graph_template.html. */\n' + parts['css'])
    return {'viewer.js': js.encode('utf-8'), 'viewer.css': css.encode('utf-8')}


def shell(prefix=SHELL_PREFIX, data=GRAPH_JSON):
    """The stable per-run graph.html: loads the shared viewer (``prefix``) and fetches ``data`` next to itself."""
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>Interaction Graph</title>\n'
            f'<link rel="stylesheet" href="{prefix}viewer.css">\n</head>\n<body>\n'
            '<noscript>The interaction graph needs JavaScript. See the <a href="index.html">story map</a>.</noscript>\n'
            f'<script src="{prefix}viewer.js" data-graph="{data}"></script>\n</body>\n</html>\n')


def write_viewer(directory, template=TEMPLATE):
    """Write the shared viewer files into ``directory``; return their paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for name, data in viewer_files(Path(template).read_text(encoding='utf-8')).items():
        (directory / name).write_bytes(data)
        out.append(directory / name)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--run-dir', help='runs/<run-id> (required unless only --viewer-dir is asked for)')
    ap.add_argument('--map', help='solver story map folder (default RUN/analysis/storymap)')
    ap.add_argument('--overseer-map', help='overseer story map folder (default RUN/analysis/storymap-overseer if '
                    'it has stage records)')
    ap.add_argument('--json', help='also write graph.json here')
    ap.add_argument('--out', help='graph.html path (default RUN/analysis/storymap/graph.html); graph.json is written '
                    'next to it unless --inline')
    ap.add_argument('--template', default=str(TEMPLATE))
    ap.add_argument('--no-html', action='store_true', help='only write --json')
    ap.add_argument('--inline', action='store_true', help='write the earlier self-contained graph.html (data and '
                    'viewer inlined; for local previews from disk) instead of the shell + graph.json')
    ap.add_argument('--viewer-prefix', default=SHELL_PREFIX, help=f'where the shell loads the shared viewer from '
                    f'(default {SHELL_PREFIX!r}: the site root\'s {VIEWER_DIR}/)')
    ap.add_argument('--viewer-dir', help=f'write the shared viewer (viewer.js, viewer.css) here, e.g. '
                    f'SITE/{VIEWER_DIR}')
    ap.add_argument('--thumb-from-json', metavar='PATH', help=f'only build {THUMB_JSON} from an existing graph.json '
                    '(no conference records needed); written next to it unless --out-thumb')
    ap.add_argument('--out-thumb', metavar='PATH', help=f'{THUMB_JSON} path for --thumb-from-json')
    # with --thumb-from-json, --run-dir only supplies the badges (default: the run folder holding the graph.json,
    # RUN/analysis/storymap/graph.json, else runs/<graph run>)
    args = ap.parse_args(argv)
    if args.thumb_from_json:
        src = Path(args.thumb_from_json)
        graph = json.loads(src.read_text(encoding='utf-8'))
        if graph.get('schema') != SCHEMA:
            ap.error(f'{src} is not a {SCHEMA} graph.json')
        run = Path(args.run_dir) if args.run_dir else None
        if run is None:
            for cand in (src.resolve().parents[2] if len(src.resolve().parents) > 2 else None,
                         REPO / 'runs' / str(graph.get('run') or '')):
                if cand and graph.get('run') and cand.name == graph['run'] and (cand / 'settings.json').is_file():
                    run = cand
                    break
        path = write_thumb(graph, args.out_thumb or src.with_name(THUMB_JSON), run)
        print(f'wrote {path} ({path.stat().st_size} bytes)')
        return 0
    if args.viewer_dir:
        for path in write_viewer(args.viewer_dir, args.template):
            print(f'wrote {path}')
    if not args.run_dir:
        if args.viewer_dir:
            return 0
        ap.error('--run-dir is required (unless only --viewer-dir is asked for)')
    run = Path(args.run_dir)
    smap = Path(args.map) if args.map else run / 'analysis' / 'storymap'
    om = Path(args.overseer_map) if args.overseer_map else run / 'analysis' / 'storymap-overseer'
    if not (om / 'stage1').is_dir():
        om = None
    graph = build_graph(run, smap, om)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(dumps(graph), encoding='utf-8')
        if args.no_html:
            write_thumb(graph, Path(args.json).with_name(THUMB_JSON), run)
        print(f'wrote {args.json} ({len(graph["events"])} events, {len(graph["nodes"])} nodes)')
    if not args.no_html:
        out = Path(args.out) if args.out else run / 'analysis' / 'storymap' / 'graph.html'
        out.parent.mkdir(parents=True, exist_ok=True)
        if args.inline:
            out.write_text(inline(Path(args.template).read_text(encoding='utf-8'), graph), encoding='utf-8')
            stale = out.with_name(GRAPH_JSON)
            if stale.is_file() and (not args.json or stale.resolve() != Path(args.json).resolve()):
                stale.unlink()  # an inline page needs no graph.json; never publish a stale one beside it
        else:
            out.with_name(GRAPH_JSON).write_text(dumps(graph), encoding='utf-8')
            out.write_text(shell(args.viewer_prefix), encoding='utf-8')
            print(f'wrote {out.with_name(GRAPH_JSON)}')
        write_thumb(graph, out.with_name(THUMB_JSON), run)  # the catalogue's snapshot (also beside an inline page)
        print(f'wrote {out.with_name(THUMB_JSON)}')
        print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
