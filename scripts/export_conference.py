#!/usr/bin/env python3
"""Render saved conference records and captured private files, without inference.

The activity timeline (RUN/analysis/timeline.jsonl) is not embedded (owner, October 23, 2026): it is kept
only in the private logs dataset, and the view links it there. ``strip_timeline`` turns a published
conference page that still embeds it into the page this exporter writes now, for
``maintain_hf_run.py --scope conference``.
"""
import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import gzip
import html
import json
from pathlib import Path
import re
import sqlite3
import sys

if str(Path(__file__).resolve().parent) not in sys.path:  # shared site theme lives beside this script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_style import themed  # noqa: E402
import run_clock  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # research/analysis/roles.py (participant roles)
    sys.path.insert(0, str(ROOT))
from research.analysis import roles as participant_roles  # noqa: E402
from research.analysis import agent_badges  # noqa: E402
STYLE = '''
:root{font-family:system-ui,sans-serif;color:var(--fg);background:var(--bg);line-height:1.55}
body{max-width:1280px;margin:32px auto;padding:0 24px}a{color:var(--accent)}h1{margin-bottom:8px}
.muted,small{color:var(--muted)}.banner{background:var(--banner);border-left:4px solid var(--banner-line);padding:12px 18px}
nav,.filters{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}button,input,select{font:inherit;padding:9px 12px;border:1px solid var(--input-line);border-radius:7px;background:var(--card);color:inherit}
button{cursor:pointer}button[aria-selected=true]{background:var(--sel-bg);color:var(--sel-fg);border-color:var(--sel-bg)}
input{flex:1;min-width:200px}article{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin:14px 0}
h2,h3{margin:0 0 8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--pre);padding:14px;border-radius:6px;font:13px/1.6 ui-monospace,monospace}
.text{white-space:pre-wrap;overflow-wrap:anywhere}details{margin:12px 0}.badge{display:inline-block;background:var(--chip);padding:2px 8px;border-radius:4px;font-size:12px}
.row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.empty{padding:32px;color:var(--muted);border:1px dashed var(--input-line);background:var(--card);border-radius:10px}
footer{margin:30px 0;color:var(--muted)}[hidden]{display:none!important}
'''


# Captured private files (October 26, 2026): HF limits a repository to 20,000 files, and one page per captured
# file (file-<hash>.html, 1,500-2,100 per 100-agent run) filled the Space. Every captured version of a run is now
# packed into at most MAX_PACKS data files RUN/conference-view/files/pack-NN.json (gzip, base64 in JSON: text, so
# HF never stores them as LFS and the credential screen can decode them) that one viewer page,
# RUN/conference-view/files.html, loads on demand; files.html#file=<id> deep-links one captured file
# (&version=N one capture). Pages exported earlier still have the per-file pages (layout "pages").
FILES_PAGE = 'files.html'
PACK_DIR = 'files'
PACK_SCHEMA = 'gdm-conference-files/1'
FILES_INDEX_SCHEMA = 'gdm-conference-files-index/1'
PACKED_LAYOUT = 'packed'
PACK_NAME = re.compile(r'pack-\d{2}\.json')
CAPTURE_PAGE = re.compile(r'file-[0-9a-f]{20}\.html')
PACK_TARGET_BYTES = 1_500_000  # per pack; raised when a run would need more than MAX_PACKS packs
PACK_LIMIT_BYTES = 7_000_000  # below the site's 8 MB page limit and HF's ~10 MB LFS threshold
MAX_PACKS = 10
# Optional manifest fields of a packed export (absent from manifests written before October 26, 2026).
MANIFEST_OPTIONAL_KEYS = ('captured_file_layout', 'captured_file_packs')
TIMELINE = 'analysis/timeline.jsonl'
SCHEMA = 'gdm-conference-view/1'
DATA_KEYS = ('run', 'records', 'counts', 'warnings', 'conference_events', 'timeline_entries', 'captured_files')
# Static HTML (outside the packed records), so that publication can point the link at the dataset file
# (dataset_logs.repoint_links) or at the run page's offline notice (dataset_logs.offline_links).
TIMELINE_NOTE = ('<p id="timeline-note" class="muted">Full activity timeline: stored in the private logs dataset '
                 '(<a href="../analysis/timeline.jsonl">timeline.jsonl</a>; sign in to huggingface.co with read '
                 'access). This view does not embed it.</p>')
TIMELINE_NOTE_OFFLINE = ('<p id="timeline-note" class="muted">Full activity timeline: kept offline by the '
                         'researchers, not in this Space or the logs dataset (<a href="../analysis/timeline.jsonl">'
                         'timeline.jsonl</a>). This view does not embed it.</p>')
TIMELINE_LOCATIONS = ('dataset', 'offline')
# Optional data field (October 25, 2026): the run start (epoch seconds, run_clock.run_start), so every time in
# the view reads "+12:34 (05:17:40 UTC)" (run_clock.CLOCK_JS). Pages exported earlier lack it.
# Optional data field (multi-overseer runs, October 26, 2026): the overseers' ids when a run has several,
# so the Oversight intro names every overseer. Single-overseer and earlier pages lack it.
OPTIONAL_KEYS = ('run_start', 'overseers')
# Section of the private overseer board (multi-overseer runs with the board); inserted into the page script
# only when the run has it, so every other page is unchanged.
OVERSEER_BOARD_SECTION = ("overseer_board:['Overseer board','Posts on the private overseer board, in posting order. "
                          "Only the overseers could post to or read this board; the solvers never saw it.',"
                          "'No overseer-board posts were recorded.'],")
MULTI_OVERSIGHT_INTRO = ("'Credits the overseers reverted (with the stated reason and the reverting overseer) and the "
                         "proof checks they ran. A reverted credit removed the credit and the library entry and "
                         "reopened the problem; the original submission stays in the records.'")
# The capture-time line of a captured-file page, as exported before October 25, 2026 (America/Los_Angeles).
_CAPTURE_LINE = re.compile(r'(<h2>Capture \d+</h2><p>)([A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} [AP]M) '
                           r'(PDT|PST|UTC)( · )')
_ZONE_OFFSETS = {'PDT': -7, 'PST': -8, 'UTC': 0}


def restamp_capture_page(document, start):
    """A captured-file page (conference-view/file-*.html) with each capture time shown as run clock plus UTC
    (run_clock.clock), e.g. "Oct 22, 2026 12:44:41 PM PDT" -> "+1:37 (19:44:41 UTC)". The zone abbreviation
    gives the offset explicitly. Idempotent: lines already in the clock format do not match."""
    def convert(match):
        local = datetime.strptime(match.group(2), '%b %d, %Y %I:%M:%S %p')
        stamp = local.replace(tzinfo=timezone(timedelta(hours=_ZONE_OFFSETS[match.group(3)])))
        return match.group(1) + run_clock.clock(stamp, start) + match.group(4)
    return _CAPTURE_LINE.sub(convert, document)


def json_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def page(title, body):
    return themed('<!doctype html><html lang="en"><meta charset="utf-8">'
                  '<meta name="viewport" content="width=device-width,initial-scale=1">'
                  f'<title>{html.escape(title)}</title><style>{STYLE}</style>{body}</html>')


def compact_browser_page(document):
    """Keep self-contained HTML below HF's large-file attachment threshold.

    Only the embedded records change representation. Browser-side decompression
    preserves the original JSON, links, filters and textContent rendering.
    """
    pattern = r'<script id="data" type="application/json">(.*?)</script><script>(.*?)</script>'
    match = re.search(pattern, document, re.S)
    if match is None:
        raise ValueError('Missing conference data and renderer')
    encoded, script = match.groups()
    json.loads(encoded)  # Refuse malformed records before producing an export.
    prefix = "const data=JSON.parse(document.getElementById('data').textContent);"
    if not script.lstrip().startswith(prefix):
        raise ValueError('Unexpected conference renderer bootstrap')
    packed = base64.b64encode(gzip.compress(encoded.encode('utf-8'), mtime=0)).decode('ascii')
    bootstrap = '''(async()=>{
if(typeof DecompressionStream==='undefined')throw new Error('This viewer needs a browser with gzip decompression support. Please use a current Chrome, Firefox, Edge or Safari.');
document.getElementById('status').textContent='Loading conference records…';
const bytes=Uint8Array.from(atob(document.getElementById('data').textContent),c=>c.charCodeAt(0));
const stream=new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
const data=JSON.parse(await new Response(stream).text());
'''
    renderer = bootstrap + script.lstrip()[len(prefix):] + '''
})().catch(error=>{document.getElementById('status').textContent='Could not load conference: '+error.message;});'''
    replacement = '<script id="data" type="application/gzip+base64">' + packed + '</script><script>' + renderer + '</script>'
    result = document[:match.start()] + replacement + document[match.end():]
    if len(result.encode('utf-8')) >= 8_000_000:
        raise ValueError('Conference HTML needs a split-data export before HF publication')
    return result


def export(run, out=None):
    """Export the conference view of a saved run into RUN/conference-view/, or into ``out`` (a prepared
    publication bundle, prepare_hf_library.py: the run's records are then only read, and the run may live in
    another clone's runs directory)."""
    run = Path(run).resolve()
    if out is None and (not run.is_relative_to(ROOT / 'runs') or run == ROOT / 'runs' or run.is_symlink()):
        raise ValueError('Use a saved run under this repository runs directory')
    if run.is_symlink() or not run.is_dir():
        raise ValueError('Not a saved run directory: ' + str(run))
    out = run / 'conference-view' if out is None else Path(out)
    if out != run / 'conference-view' and out.resolve().is_relative_to(run):
        raise ValueError('An export outside the run must not write into its records')
    if out.is_symlink():
        raise ValueError('Conference output must not be a symlink')
    out.mkdir(parents=True, exist_ok=True)
    events = json_lines(run / 'conference/events.jsonl')
    badge_map = agent_badges.run_badges(run)  # behavior judge labels next to agent ids (owner, 2026-10-26)
    start = run_clock.run_start(run)  # results.json start_utc (the runtime's common_start)
    runtime = json_lines(run / 'runtime.jsonl')
    db = sqlite3.connect((run / 'conference/state.sqlite').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    tables = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name)]
              for name in ('posts', 'messages', 'feedback', 'submissions', 'library', 'events')}
    # Reverted credits (overseer runs); runs before oversight have no reversions table.
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    reversions = ([dict(row) for row in db.execute('SELECT * FROM reversions ORDER BY reversion_id')]
                  if 'reversions' in present else [])
    # Private overseer board (multi-overseer runs with the board); absent in every other run.
    overseer_posts = ([dict(row) for row in db.execute('SELECT * FROM overseer_board ORDER BY post_id')]
                      if 'overseer_board' in present else [])
    db.close()
    settings_path = run / 'settings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    overseer = ((settings.get('effective') or {}).get('overseer') or (settings.get('config') or {}).get('overseer'))
    overseer_ids = participant_roles.overseer_ids(overseer)  # every overseer (agent_ids, or the legacy agent_id)
    board_on = participant_roles.board_enabled(settings) or 'overseer_board' in present
    roles = participant_roles.special_roles(settings)  # {} unless the run has an overseer or a seed
    seeds = {a for a, r in roles.items() if r == participant_roles.SEED}
    reverted_submissions = {row['submission_id']: row for row in reversions}
    withdrawn_artifacts = {row['artifact_id']: row for row in reversions if row.get('artifact_id') is not None}
    proof_checks = [e for e in events if e['kind'] == 'proof_checked']
    if [x['seq'] for x in tables['events']] != [x['seq'] for x in events]:
        raise ValueError('Database/event-log sequence mismatch')
    for row, event in zip(tables['events'], events):
        if row['kind'] != event['kind'] or json.loads(row['payload']) != event['payload']:
            raise ValueError('Database/event-log content mismatch')

    # No per-record transcript links: agent transcripts are read in Inspect View (owner, October 21,
    # 2026); the custom transcript pages (scripts/export_transcripts.py) are local-only and unpublished.
    def record(category, identity, title, text, timestamp, agents=(), task='', detail=None, links=None):
        if seeds & set(agents):  # seeded runs: records of the planted seed say so
            title = f'{title} · {participant_roles.SEED_LABEL}'
        return dict(category=category, id=identity, title=title, text=text or '', timestamp=timestamp,
                    agents=[a for a in agents if a], task=task or '', detail=detail,
                    links=links or [])

    records = []
    for row in tables['posts']:
        records.append(record('board', 'post-' + str(row['post_id']), row['intent_type'], row['message'], row['posted_at'],
                       [row['agent_id']], detail={'tags': json.loads(row['tags'])}))
    # The board stores posts, not a reply graph. Do not invent reply threads.
    conversations = defaultdict(list)
    for row in tables['messages']:
        conversations[tuple(sorted((row['sender'], row['recipient'])))].append(row)
    for peers, rows in sorted(conversations.items()):
        rows.sort(key=lambda r: (r['sent_at'], r['message_id']))
        records.append(record('dms', 'dm-' + '-'.join(peers), ' ↔ '.join(peers), '', rows[0]['sent_at'], peers,
                       detail={'messages': rows}))
    for row in tables['feedback']:
        records.append(record('feedback', 'feedback-' + str(row['feedback_id']), 'Organizer feedback', row['message'],
                       row['submitted_at'], [row['agent_id']]))
    for row in tables['library']:
        if hashlib.sha256(row['content'].encode()).hexdigest() != row['sha256']:
            raise ValueError('Library source hash mismatch')
        detail = {k: v for k, v in row.items() if k != 'content'}
        withdrawn = withdrawn_artifacts.get(row['artifact_id'])
        if withdrawn:
            detail['withdrawn'] = {'by': withdrawn['reverted_by'], 'reason': withdrawn['reason'],
                                   'at': withdrawn['reverted_at'], 'reversion_id': withdrawn['reversion_id']}
        records.append(record('library', 'library-' + str(row['artifact_id']),
                              row['path'] + (' · withdrawn' if withdrawn else ''), row['content'], row['published_at'],
                       [row['author']], row['task_id'], detail=detail))
    # A run whose independent evaluation has not run yet (e.g. refused) shows every submission as
    # "not independently evaluated" and records the evaluation source as absent.
    evaluations_path = run / 'evaluations/evaluations.jsonl'
    evaluations = ({x['submission']['submission_id']: x for x in json_lines(evaluations_path)}
                   if evaluations_path.exists() else {})
    for row in tables['submissions']:
        if hashlib.sha256(row['source'].encode()).hexdigest() != row['source_sha256']:
            raise ValueError('Submission source hash mismatch')
        sid = f"sub-{row['submission_id']:06d}"
        evaluation = evaluations.get(sid)
        if evaluation:
            s = evaluation['submission']
            if (s['agent_id'], s['problem_id'], evaluation['source_sha256']) != (row['agent_id'], row['task_id'], row['source_sha256']):
                raise ValueError('Independent evaluation binding mismatch')
        detail = {k: v for k, v in row.items() if k != 'source'}
        detail['independent_evaluation'] = ({'validity': evaluation['validity'], 'reason': evaluation['reason']}
                                            if evaluation else {'validity': 'not independently evaluated'})
        reverted = reverted_submissions.get(row['submission_id'])
        if reverted:
            detail['reverted'] = {'by': reverted['reverted_by'], 'reason': reverted['reason'],
                                  'at': reverted['reverted_at'], 'reversion_id': reverted['reversion_id']}
        records.append(record('submissions', sid, sid + ' · ' + row['outcome'] + (' · credit reverted' if reverted else ''),
                              row['source'], row['submitted_at'],
                       [row['agent_id']], row['task_id'], detail=detail,
                       links=[['Evaluation records', '../evaluations/evaluations.jsonl']]))

    # Oversight (overseer runs): reverted credits and proof checks, with the overseer's stated reasons.
    for row in reversions:
        sid = f"sub-{row['submission_id']:06d}"
        records.append(record('oversight', 'reversion-' + str(row['reversion_id']),
                              f"Credit reverted · {row['task_id']}",
                              f"{row['reverted_by']} reverted {row['credited_agent']}'s credit for {row['task_id']} "
                              f"({sid}). Reason: {row['reason']}", row['reverted_at'],
                              [row['reverted_by'], row['credited_agent']], row['task_id'], detail=row))
    for event in proof_checks:
        p = event.get('payload') or {}
        target = p.get('submission_id') if p.get('target') == 'accepted_submission' else p.get('file_path')
        records.append(record('oversight', 'proof-check-' + str(event['seq']),
                              f"Proof check · {p.get('problem_id')} · {'verified' if p.get('verified') else 'not verified'}",
                              f"{p.get('target')}: {target}. {p.get('reason') or ''}", event['timestamp'],
                              [event.get('agent_id')], p.get('problem_id'), detail=p))

    # Overseer board (overseers only): posts in posting order, with author and time.
    for row in overseer_posts:
        records.append(record('overseer_board', 'ovpost-%06d' % row['post_id'], 'Overseer board post',
                              row['message'], row['posted_at'], [row['agent_id']],
                              detail={'channel': 'overseer board (overseers only)'}))

    # Browse only workspace working files and notes; SDK sessions are in the Inspect logs. Captured versions
    # are packed into a few data files that the viewer page (files.html) loads on demand (October 26, 2026:
    # one page per captured file filled the Space's 20,000-file limit).
    versions = captured_versions(run)
    records.extend(captured_file_records(versions, seeds))

    categories = ('board', 'dms', 'library', 'submissions', 'feedback', 'files')
    if overseer or reversions or proof_checks:  # the Oversight section exists only in overseer runs
        categories = categories[:5] + ('oversight',) + categories[5:]
    if board_on:  # the Overseer board section exists only in runs with the board
        categories = categories[:-1] + ('overseer_board',) + categories[-1:]
    counts = {category: 0 for category in categories}
    counts.update(Counter(r['category'] for r in records))
    warnings = ['Private files are researcher-only and were not automatically shared with peers.',
                'Board records have no reply relationship; posts are shown chronologically.',
                'Missing timestamps stay unknown. Event order does not establish causal influence.',
                'Operational outcomes are separate from independent validity. Closed-task attempts may be unevaluated.']
    if roles:
        warnings.append('Participant roles: ' + '; '.join(participant_roles.label(a, roles) for a in roles) + '.'
                        + (' The seeded participant was planted by the researchers: it has no model, container or '
                           'transcript, and its accepted submission and library file were injected at the trigger.'
                           if seeds else ''))
    if not evaluations_path.exists():
        warnings.append('No independent evaluation records: evaluations/evaluations.jsonl is absent, so no '
                        'submission was independently evaluated.')
    data = dict(run=run.name, records=records, counts=counts, warnings=warnings,
                conference_events=len(events), timeline_entries=0, timeline_location='dataset',
                captured_files=len(versions))
    if start is not None:
        data['run_start'] = start
    if len(overseer_ids) > 1:
        data['overseers'] = overseer_ids
    (out / 'index.html').write_text(agent_badges.decorate_page(render_index(data), 'conference-view/index.html', badge_map))
    packs, index = pack_captures(run.name, versions)
    write_packed_files(out, packs, index, start, badge_map)
    manifest = {'schema': SCHEMA, 'run': run.name, 'counts': counts, 'captured_files': len(versions),
                'conference_events': len(events), 'timeline_entries': 0, 'timeline_location': 'dataset',
                'warnings': warnings, 'captured_file_layout': PACKED_LAYOUT,
                'captured_file_packs': {name: digest(data) for name, data in sorted(packs.items())},
                'sources': {str(p.relative_to(run)): hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else 'absent'
                            for p in [run/'conference/state.sqlite', run/'conference/events.jsonl',run/'runtime.jsonl',run/'evaluations/evaluations.jsonl']}}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


def digest(data):
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def captured_versions(run, read=None):
    """{(agent, path): [captured version]} of a saved run's workspace working files and notes, from
    RUN/runtime.jsonl file_snapshot entries and their content-addressed blobs RUN/snapshots/AGENT/SHA256, each
    checked against its SHA-256. ``read(path) -> bytes`` reads a local file (default Path.read_bytes)."""
    run = Path(run)
    read = read or (lambda path: Path(path).read_bytes())
    versions = defaultdict(list)
    lines = read(run / 'runtime.jsonl').decode('utf-8').splitlines()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        entry = json.loads(line)
        p = entry.get('payload', {})
        path = p.get('path', '')
        if entry['kind'] != 'file_snapshot' or not path.startswith('/workspace/'):
            continue
        relative = Path(path).relative_to('/workspace')
        if relative.parts[0] == 'problems' or relative.name in {'lakefile.toml', 'lake-manifest.json'}:
            continue
        agent = entry['agent_id']
        blob_sha = p.get('blob_sha256')
        if not re.fullmatch(r'agent-\d+', agent) or not blob_sha or not re.fullmatch(r'[0-9a-f]{64}', blob_sha):
            raise ValueError('Unsafe snapshot identity')
        blob = run / 'snapshots' / agent / blob_sha
        if blob.is_symlink() or not blob.is_file():
            raise ValueError('Missing or linked snapshot blob')
        content = read(blob)
        if hashlib.sha256(content).hexdigest() != blob_sha:
            raise ValueError('Snapshot blob hash mismatch')
        text = content.decode('utf-8', errors='replace')
        versions[agent, path].append(dict(timestamp=entry['timestamp'], sha256=p.get('sha256'),
                                          blob_sha256=blob_sha, bytes=p.get('bytes'), mode=p.get('mode'),
                                          content=text, runtime_line=line_number))
    for rows in versions.values():
        rows.sort(key=lambda x: (x['timestamp'], x['runtime_line']))
    return dict(sorted(versions.items()))


def file_identity(agent, path):
    """The stable identifier of one captured file (agent, workspace path): 'file-' + 20 hex digits."""
    return 'file-' + hashlib.sha256((agent + ':' + path).encode()).hexdigest()[:20]


def file_link(identity):
    """The viewer deep link of a captured file, relative to RUN/conference-view/."""
    return FILES_PAGE + '#file=' + identity


def captured_file_records(versions, seeds=()):
    """The conference view's 'files' records (one per captured file) linking the packed viewer."""
    records = []
    for (agent, path), rows in versions.items():
        identity = file_identity(agent, path)
        empty = all(not r['content'] for r in rows)
        note = 'All captured versions are empty.' if empty else 'Open to inspect captured contents and versions.'
        title = path + (f' · {participant_roles.SEED_LABEL}' if agent in set(seeds) else '')
        records.append(dict(category='files', id=identity, title=title,
                            text=f'{len(rows)} captured version(s). {note}', timestamp=rows[-1]['timestamp'],
                            agents=[agent], task='', detail={'captures': len(rows), 'empty': empty,
                                                             'capture_modes': dict(Counter(r['mode'] for r in rows))},
                            links=[['View captured versions', file_link(identity)]]))
    return records


def _encode_pack(run_name, number, entries):
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    packed = base64.b64encode(gzip.compress(payload, compresslevel=9, mtime=0)).decode('ascii')
    document = {'schema': PACK_SCHEMA, 'run': run_name, 'pack': 'pack-%02d.json' % number, 'files': len(entries),
                'content_sha256': hashlib.sha256(payload).hexdigest(), 'encoding': 'gzip+base64', 'data': packed}
    return (json.dumps(document, indent=0) + '\n').encode('ascii')


def pack_captures(run_name, versions, target=PACK_TARGET_BYTES, limit=PACK_LIMIT_BYTES, max_packs=MAX_PACKS):
    """({pack name: bytes}, files index) of a run's captured versions. Every captured file goes whole into one
    pack, in identity order; packs are filled up to ``target`` bytes (raised until at most ``max_packs`` are
    needed). Deterministic: the same records give the same bytes. The index maps every identity to
    [pack name, agent, path, captures]."""
    entries = {}
    for (agent, path), rows in versions.items():
        entries[file_identity(agent, path)] = {'agent': agent, 'path': path, 'versions': [
            {k: row[k] for k in ('timestamp', 'sha256', 'blob_sha256', 'bytes', 'mode', 'runtime_line', 'content')}
            for row in rows]}
    if len(entries) != len(versions):
        raise ValueError('Captured-file identity collision')
    ordered = sorted(entries)
    sizes = {i: len(base64.b64encode(gzip.compress(json.dumps(entries[i], ensure_ascii=False).encode(), 6, mtime=0)))
             for i in ordered}
    if ordered:
        target = max(target, -(-sum(sizes.values()) * 11 // (10 * max_packs)))
    while True:
        groups, current, size = [], [], 0
        for i in ordered:
            if current and size + sizes[i] > target:
                groups.append(current)
                current, size = [], 0
            current.append(i)
            size += sizes[i]
        if current:
            groups.append(current)
        if len(groups) <= max_packs or target >= limit:
            break
        target = min(limit, int(target * 1.25))
    packs, index = {}, {}
    for number, group in enumerate(groups, 1):
        name = 'pack-%02d.json' % number
        packs[name] = _encode_pack(run_name, number, {i: entries[i] for i in group})
        if len(packs[name]) > limit:
            raise ValueError(f'Captured-file pack {name} is {len(packs[name])} bytes, above {limit}')
        for i in group:
            index[i] = [name, entries[i]['agent'], entries[i]['path'], len(entries[i]['versions'])]
    if len(packs) > max_packs:
        raise ValueError(f'{len(packs)} captured-file packs needed, more than {max_packs}')
    return packs, {'schema': FILES_INDEX_SCHEMA, 'run': run_name,
                   'packs': {name: {**digest(data), 'files': sum(1 for v in index.values() if v[0] == name)}
                             for name, data in packs.items()},
                   'files': index}


def read_pack(data):
    """The {identity: {agent, path, versions}} of one pack file, checked (schema, encoding, content digest)."""
    document = json.loads(data)
    if not isinstance(document, dict) or document.get('schema') != PACK_SCHEMA \
            or document.get('encoding') != 'gzip+base64':
        raise ValueError('Not a captured-file pack')
    payload = gzip.decompress(base64.b64decode(document['data'], validate=True))
    if hashlib.sha256(payload).hexdigest() != document.get('content_sha256'):
        raise ValueError('Captured-file pack content digest mismatch')
    entries = json.loads(payload)
    if not isinstance(entries, dict) or len(entries) != document.get('files'):
        raise ValueError('Captured-file pack entry count mismatch')
    return entries


def pack_text(data):
    """The decoded contents of a pack (for the credential screen)."""
    return gzip.decompress(base64.b64decode(json.loads(data)['data'], validate=True))


def is_pack_path(name):
    """True for RUN/conference-view/files/pack-NN.json (or the run-relative form)."""
    parts = name.split('/')
    return len(parts) >= 3 and parts[-3:-1] == ['conference-view', PACK_DIR] and bool(PACK_NAME.fullmatch(parts[-1]))


FILES_STYLE = ('#files-list{list-style:none;padding:0;margin:0}#files-list li{padding:6px 0;border-bottom:1px solid '
               'var(--line)}.vnav{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:16px 0}'
               '.vnav select{min-width:0;max-width:100%}article h2 a{color:inherit;text-decoration:none}')


def render_files_page(index, run_start=None):
    """The captured-file viewer RUN/conference-view/files.html: the files index embedded, the captured
    versions loaded from the packs on demand. #file=<id>[&version=N] opens one captured file (every version
    stacked, as the former per-file pages showed them); no hash lists every captured file."""
    if index.get('schema') != FILES_INDEX_SCHEMA:
        raise ValueError('Unexpected captured-file index')
    meta = {**index, 'run_start': run_start}
    encoded = json.dumps(meta, ensure_ascii=True, sort_keys=True).replace('<', '\\u003c').replace('>', '\\u003e') \
        .replace('&', '\\u0026')
    body = '''<p><a href="index.html#files">← Captured files</a> · <a href="index.html">Conference view</a></p>
<h1 id="title">Captured private files</h1><p id="run" class="muted"></p>
<p class="banner">Private researcher record. Captures are observations, not a complete edit history. A partial capture may omit content. Recorded times are capture times, not necessarily edit times.</p>
<div id="list" hidden><div class="filters"><input id="search" aria-label="Search captured files" placeholder="Search agents and paths…"></div><ul id="files-list"></ul></div>
<div class="vnav" id="vnav" hidden><button id="prev-version">Previous capture</button><select id="jump" aria-label="Jump to capture"></select><button id="next-version">Next capture</button></div>
<p id="status" aria-live="polite"></p><main id="captures"></main>
<footer>Captured versions are packed in <code>files/pack-NN.json</code> (gzip, base64) and loaded when a file is opened. <a href="manifest.json">Export manifest</a></footer>
'''
    script = r'''
const meta=JSON.parse(document.getElementById('files-index').textContent);
const el=id=>document.getElementById(id);const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
''' + run_clock.CLOCK_JS + r'''
const cache={};let shown=null,versionCount=0;
el('run').textContent=meta.run+' · '+Object.keys(meta.files).length+' captured files'+(meta.run_start!=null?' · times are run clock (from the run start) and UTC':'');
function pack(name){if(!cache[name])cache[name]=(async()=>{
 if(typeof DecompressionStream==='undefined')throw new Error('This viewer needs a browser with gzip decompression support. Please use a current Chrome, Firefox, Edge or Safari.');
 const r=await fetch('files/'+name);if(!r.ok)throw new Error('HTTP '+r.status+' for files/'+name);
 const j=await r.json();if(j.schema!=='gdm-conference-files/1'||j.encoding!=='gzip+base64')throw new Error('Unexpected data file files/'+name);
 const bytes=Uint8Array.from(atob(j.data),c=>c.charCodeAt(0));
 return JSON.parse(await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text());
})().catch(e=>{delete cache[name];throw e;});return cache[name];}
function params(){return new URLSearchParams(location.hash.slice(1));}
function listing(){el('list').hidden=false;el('vnav').hidden=true;el('captures').replaceChildren();el('title').textContent='Captured private files';document.title='Captured files · '+meta.run;
 const q=el('search').value.toLowerCase(),ul=el('files-list');ul.replaceChildren();let n=0;
 const rows=Object.entries(meta.files).sort((a,b)=>(a[1][1]+a[1][2]).localeCompare(b[1][1]+b[1][2]));
 for(const [id,[,agent,path,count]] of rows){if(q&&!(agent+' '+path).toLowerCase().includes(q))continue;n++;const li=node('li');const a=node('a',agent+' · '+path);a.href='#file='+id;li.append(a,node('span',' · '+count+' captured version(s)','muted'));ul.append(li);}
 el('status').textContent=n+' captured files';}
function go(i){if(!shown)return;i=Math.max(1,Math.min(versionCount,i));el('jump').value=String(i);history.replaceState(null,'','#file='+shown+'&version='+i);const t=el('version-'+i);if(t)t.scrollIntoView();el('prev-version').disabled=i<=1;el('next-version').disabled=i>=versionCount;}
async function route(){const p=params(),id=p.get('file');
 if(!id){shown=null;listing();return;}
 const entry=meta.files[id];el('list').hidden=true;
 if(!entry){shown=null;el('captures').replaceChildren();el('vnav').hidden=true;el('title').textContent='Captured file not found';el('status').textContent='No captured file '+id+' in this run. Open the list of captured files.';const a=node('a','All captured files');a.href='#';el('captures').append(a);return;}
 const [name,agent,path]=entry;el('title').textContent=agent+' · '+path;document.title=agent+' · '+path;
 if(shown!==id){el('captures').replaceChildren();el('vnav').hidden=true;el('status').textContent='Loading captured versions…';
  let data;try{data=(await pack(name))[id];}catch(e){el('status').textContent='Could not load captured versions: '+e.message;return;}
  if(!data){el('status').textContent='Captured versions missing from files/'+name;return;}
  shown=id;versionCount=data.versions.length;const main=el('captures'),jump=el('jump');jump.replaceChildren();
  data.versions.forEach((v,k)=>{const i=k+1,card=node('article');card.id='version-'+i;const h=node('h2');const a=node('a','Capture '+i);a.href='#file='+id+'&version='+i;h.append(a);
   const code=node('code',v.blob_sha256),line=node('p','Runtime line '+v.runtime_line+' · blob SHA-256 ');line.append(code);
   card.append(h,node('p',runClock(v.timestamp,meta.run_start)+' · '+v.mode+' capture · '+v.bytes+' recorded bytes'),line,node('pre',v.content));main.append(card);
   jump.append(new Option('Capture '+i+' of '+versionCount+' · '+runClock(v.timestamp,meta.run_start),String(i)));});
  el('status').textContent=versionCount+' captured version(s)';el('vnav').hidden=versionCount<2;}
 const v=parseInt(p.get('version')||'',10);if(v)go(v);else{el('prev-version').disabled=true;el('next-version').disabled=versionCount<2;el('jump').value='1';}}
el('search').addEventListener('input',()=>{if(!shown)listing();});
el('jump').addEventListener('change',()=>go(parseInt(el('jump').value,10)));
el('prev-version').onclick=()=>go(parseInt(el('jump').value,10)-1);el('next-version').onclick=()=>go(parseInt(el('jump').value,10)+1);
window.addEventListener('hashchange',route);route();
'''
    return page('Captured files · ' + index['run'], body + '<style>' + FILES_STYLE
                + '</style><script id="files-index" type="application/json">' + encoded
                + '</script><script>' + script + '</script>')


def files_page_index(document):
    """The files index embedded in a files.html viewer page."""
    match = re.search(r'<script id="files-index" type="application/json">(.*?)</script>', document, re.S)
    if match is None:
        raise ValueError('Missing captured-file index')
    index = json.loads(match[1])
    if not isinstance(index, dict) or index.get('schema') != FILES_INDEX_SCHEMA:
        raise ValueError('Unexpected captured-file index')
    return index


def write_packed_files(out, packs, index, run_start, badge_map):
    """Write files.html and files/pack-NN.json into a conference-view directory, removing per-file pages
    (file-<hash>.html) and packs of an earlier export there."""
    out = Path(out)
    for stale in out.glob('file-*.html'):
        if CAPTURE_PAGE.fullmatch(stale.name) and not stale.is_symlink():
            stale.unlink()
    folder = out / PACK_DIR
    if folder.is_symlink():
        raise ValueError('Captured-file folder must not be a symlink')
    folder.mkdir(exist_ok=True)
    for stale in folder.glob('pack-*.json'):
        if stale.name not in packs:
            stale.unlink()
    for name, data in packs.items():
        (folder / name).write_bytes(data)
    (out / FILES_PAGE).write_text(agent_badges.decorate_page(render_files_page(index, run_start),
                                                             'conference-view/' + FILES_PAGE, badge_map))


def render_index(data):
    """The compact, self-contained conference page for the records in ``data``."""
    if set(data) - set(OPTIONAL_KEYS) != {*DATA_KEYS, 'timeline_location'} \
            or data['timeline_location'] not in TIMELINE_LOCATIONS \
            or not isinstance(data.get('run_start', 0), (int, float)) or isinstance(data.get('run_start'), bool):
        raise ValueError('Unexpected conference data fields')
    if data['timeline_entries'] != 0 or 'activity' in data['counts'] \
            or any(r['category'] == 'activity' for r in data['records']):
        raise ValueError('The conference view no longer embeds the activity timeline')
    # Script-safe JSON; agent-controlled strings are rendered only with textContent.
    encoded = json.dumps(data, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    body = '''<p><a href="../../index.html">Experiment library</a> · <a href="../results.html">Run report</a> · <a href="../inspect-view/index.html">Agent transcripts (Inspect View)</a></p>
<h1>Inside the conference</h1><p id="run" class="muted"></p>
<p class="banner">Researcher view: private conversations and captured private files appear here alongside public conference activity. Agents did not have access to this combined view.</p>
<nav id="tabs" aria-label="Conference sections"></nav><p id="intro"></p>
<div class="filters"><input id="search" aria-label="Search records" placeholder="Search messages, tasks, sources…"><select id="agent" aria-label="Filter by agent"><option value="">All agents</option></select><select id="task" aria-label="Filter by task"><option value="">All tasks</option></select></div>
<div class="row"><p id="status" aria-live="polite"></p><div><button id="previous">Previous</button> <button id="next">Next</button></div></div>
<main id="records"></main><footer>Derived from the saved conference database, event log and runtime snapshots. <a href="../conference/events.jsonl">Conference events</a> · <a href="manifest.json">Export manifest</a></footer>
'''.replace('<nav id="tabs"', (TIMELINE_NOTE if data['timeline_location'] == 'dataset' else TIMELINE_NOTE_OFFLINE)
           + '\n<nav id="tabs"', 1)
    script = r'''
const data=JSON.parse(document.getElementById('data').textContent);
const sections={board:['Bulletin board','Posts shown in posting order. The conference did not store a reply-thread relationship.','No bulletin-board posts were recorded.'],dms:['Direct messages','Private conversations grouped by the two participants.','No direct messages were saved in this run. Inbox checks appear only in the full activity timeline, which this view does not embed.'],library:['Shared library','Published artifacts and revisions. Complete accepted proofs were published automatically; private notes required deliberate publication.','No shared artifacts were recorded.'],submissions:['Submissions & judging','Conference outcomes and independently bound evaluations. “problem_closed” does not mean a proof was mathematically rejected.','No submissions were recorded.'],feedback:['Organizer feedback','Private feedback submitted to the conference organizers.','No organizer feedback was recorded.'],oversight:['Oversight','Credits the overseer reverted (with its stated reason) and the proof checks it ran. A reverted credit removed the credit and the library entry and reopened the problem; the original submission stays in the records.','No credit was reverted and no proof check was run.'],files:['Private notes & files','Captured versions of RESEARCH.md and private workspace working files. These are observations, not a complete edit history.','No private workspace file captures were recorded.']};
let selected=sections[location.hash.slice(1)]&&(location.hash.slice(1) in data.counts)?location.hash.slice(1):'board',pageNo=0;const size=40;
const el=id=>document.getElementById(id);const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
el('run').textContent=data.run+' · '+data.conference_events+' conference events'+(data.run_start!=null?' · times are run clock (from the run start, '+new Date(Math.floor(data.run_start)*1000).toISOString().replace('.000Z','Z')+') and UTC':'');
'''+run_clock.CLOCK_JS+r'''
function timestamp(value){return value==null?'Time unknown':runClock(value,data.run_start);}
for(const [id,[label]] of Object.entries(sections)){if(!(id in data.counts))continue;const b=node('button',label+' ('+(data.counts[id]||0)+')');b.onclick=()=>{selected=id;location.hash=id;pageNo=0;options();render();};b.dataset.section=id;el('tabs').append(b);}
function options(){const rows=data.records.filter(x=>x.category===selected);for(const [id,values,label] of [['agent',rows.flatMap(x=>x.agents),'All agents'],['task',rows.map(x=>x.task).filter(Boolean),'All tasks']]){const old=el(id).value;el(id).replaceChildren(new Option(label,''));for(const v of [...new Set(values)].sort())el(id).append(new Option(v,v));if([...el(id).options].some(o=>o.value===old))el(id).value=old;}el('task').hidden=el('task').options.length===1;}
function render(){document.querySelectorAll('[data-section]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.section===selected)));el('intro').textContent=sections[selected][1];const q=el('search').value.toLowerCase(),agent=el('agent').value,task=el('task').value;
let rows=data.records.filter(x=>x.category===selected&&(!agent||x.agents.includes(agent))&&(!task||x.task===task)&&(!q||JSON.stringify(x).toLowerCase().includes(q)));
rows.sort((a,b)=>(a.timestamp??Infinity)-(b.timestamp??Infinity));
pageNo=Math.min(pageNo,Math.max(0,Math.ceil(rows.length/size)-1));el('status').textContent=rows.length+' records · page '+(pageNo+1)+' of '+Math.max(1,Math.ceil(rows.length/size));el('previous').disabled=pageNo===0;el('next').disabled=(pageNo+1)*size>=rows.length;
const main=el('records');main.replaceChildren();if(!rows.length)main.append(node('p',(data.counts[selected]||0)?'No records match these filters.':sections[selected][2],'empty'));
for(const r of rows.slice(pageNo*size,(pageNo+1)*size)){const card=node('article');card.id=r.id;card.append(node('h2',r.title),node('p',[timestamp(r.timestamp),...r.agents,r.task].filter(Boolean).join(' · '),'muted'));
if(r.text){if(['library','submissions'].includes(selected)){const d=node('details');d.append(node('summary','Complete source'),node('pre',r.text));card.append(d);}else card.append(node('div',r.text,'text'));}
if(selected==='dms'&&r.detail){for(const m of r.detail.messages){const d=node('article');d.append(node('strong',m.sender+' → '+m.recipient),node('p',timestamp(m.sent_at),'muted'),node('div',m.body,'text'));card.append(d);}}
if(selected==='submissions'){card.append(node('p','Conference judge: '+(r.detail.reason||r.detail.outcome)));if(r.detail.reverted)card.append(node('p','Credit reverted by '+r.detail.reverted.by+': '+r.detail.reverted.reason,'badge'));if(r.detail.compile_status)card.append(node('p','Compilation: '+r.detail.compile_status));const v=r.detail.independent_evaluation;card.append(node('p','Independent validity: '+v.validity,'badge'));if(v.reason)card.append(node('p',v.reason));}
const links=node('p');for(const [label,url] of r.links){if(!url)continue;const a=node('a',label);a.href=url;links.append(a,document.createTextNode(' · '));}card.append(links);
if(r.detail){const d=node('details');d.append(node('summary',selected==='submissions'?'Judge feedback & evidence':'Record details & provenance'),node('pre',JSON.stringify(r.detail,null,2)));card.append(d);}main.append(card);}}
for(const id of ['search','agent','task'])el(id).addEventListener(id==='search'?'input':'change',()=>{pageNo=0;render();});el('previous').onclick=()=>{pageNo--;render();};el('next').onclick=()=>{pageNo++;render();};window.addEventListener('hashchange',()=>{if(sections[location.hash.slice(1)]&&selected!==location.hash.slice(1)){selected=location.hash.slice(1);pageNo=0;options();render();}});options();render();
'''
    if 'overseer_board' in data['counts']:
        script = script.replace("files:['Private notes", OVERSEER_BOARD_SECTION + "files:['Private notes", 1)
    if len(data.get('overseers') or ()) > 1:
        script = script.replace("'Credits the overseer reverted (with its stated reason) and the proof checks it ran. "
                                "A reverted credit removed the credit and the library entry and reopened the problem; "
                                "the original submission stays in the records.'", MULTI_OVERSIGHT_INTRO, 1)
    return compact_browser_page(page('Conference · ' + data['run'], body + '<script id="data" type="application/json">' + encoded + '</script><script>' + script + '</script>'))


def published_data(document):
    """The records embedded in a conference page (compact or plain), checked as JSON."""
    match = re.search(r'<script id="data" type="application/(json|gzip\+base64)">(.*?)</script>', document, re.S)
    if match is None:
        raise ValueError('Missing conference data')
    text = match[2] if match[1] == 'json' else gzip.decompress(base64.b64decode(match[2], validate=True)).decode('utf-8')
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('Conference data is not an object')
    return data


def strip_timeline(document, manifest, location='dataset'):
    """(data, manifest) of a published conference view without the embedded activity timeline: every other
    record, count and field stays exactly as published. ``location`` says where the full timeline is kept
    ('dataset' or 'offline'). Render the page with ``render_index``."""
    if location not in TIMELINE_LOCATIONS:
        raise ValueError('Unknown timeline location: ' + str(location))
    data = published_data(document)
    if set(data) - {'timeline_location', *OPTIONAL_KEYS} != set(DATA_KEYS):
        raise ValueError('Unexpected conference data fields: ' + ', '.join(sorted(set(data) ^ set(DATA_KEYS))))
    if manifest.get('schema') != SCHEMA or manifest.get('run') != data['run']:
        raise ValueError('Conference manifest does not belong to this page')
    activity = [r for r in data['records'] if r['category'] == 'activity']
    if len(activity) != data['timeline_entries'] or data['counts'].get('activity', 0) != len(activity):
        raise ValueError('Embedded timeline does not match its recorded count')
    counts = {k: v for k, v in data['counts'].items() if k != 'activity'}
    if {k: v for k, v in manifest['counts'].items() if k != 'activity'} != counts \
            or manifest.get('timeline_entries') != data['timeline_entries']:
        raise ValueError('Conference manifest counts differ from the page')
    new = dict(run=data['run'], records=[r for r in data['records'] if r['category'] != 'activity'], counts=counts,
               warnings=data['warnings'], conference_events=data['conference_events'], timeline_entries=0,
               timeline_location=location, captured_files=data['captured_files'])
    new.update({k: data[k] for k in OPTIONAL_KEYS if k in data})
    sources = {k: v for k, v in manifest['sources'].items() if k != TIMELINE}
    new_manifest = {'schema': SCHEMA, 'run': manifest['run'], 'counts': counts,
                    'captured_files': manifest['captured_files'], 'conference_events': manifest['conference_events'],
                    'timeline_entries': 0, 'timeline_location': location, 'warnings': manifest['warnings'],
                    'sources': sources}
    new_manifest.update({k: manifest[k] for k in MANIFEST_OPTIONAL_KEYS if k in manifest})
    extra = set(manifest) - set(new_manifest)
    if extra:
        raise ValueError('Unexpected conference manifest fields: ' + ', '.join(sorted(extra)))
    return new, new_manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    print(json.dumps(export(parser.parse_args().run_dir)))
