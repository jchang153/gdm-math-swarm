#!/usr/bin/env python3
"""Build the private local experiment library from saved results.json reports.

No model calls, Docker, network, credentials, or changes to raw experiment records.
Run after exporting a completed experiment: python scripts/build_experiment_index.py
"""
import argparse
import datetime
from decimal import Decimal
import html
import json
from pathlib import Path
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo
from space_navigation import private_space_navigation

ROOT = Path(__file__).resolve().parents[1]
# Runs the owner removed from the library lists (docs/plan.md, October 20, 2026). Their files
# stay where they are; only the catalogue rows are hidden. Delete a record to list the run again.
UNLISTED = ROOT / 'data' / 'library-unlisted.json'
UNLISTED_SCHEMA = 'gdm-library-unlisted/1'
RUN_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*')
INSPECT_LOGS = 'Inspect logs'


def inspect_log_links(run, names):
    """Catalogue links to a run's raw Inspect logs (file names under RUN/inspect/).

    A static host cannot list a directory (the hosted Space redirects RUN/inspect/ off-site), so
    each .eval file is linked directly: 'Inspect logs' for a single log, else one numbered link
    per log in name order.
    """
    logs = sorted(n for n in names if n.endswith('.eval') and '/' not in n)
    targets = [quote(run, safe='') + '/inspect/' + quote(n, safe='') for n in logs]
    if len(targets) == 1:
        return {INSPECT_LOGS: targets[0]}
    return {f'Inspect log {i}': t for i, t in enumerate(targets, 1)}


def unlisted_runs(path=UNLISTED):
    """Run ID -> record ({run, date, reason}) of the tracked unlisted-runs file; empty if absent."""
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('schema') != UNLISTED_SCHEMA or not isinstance(data.get('runs'), list):
        raise ValueError(f'{path} is not a {UNLISTED_SCHEMA} file')
    records = {}
    for row in data['runs']:
        if (not isinstance(row, dict) or not isinstance(row.get('run'), str) or not RUN_NAME.fullmatch(row['run'])
                or not isinstance(row.get('date'), str) or not isinstance(row.get('reason'), str)):
            raise ValueError(f'{path}: every record needs a valid run, a date and a reason')
        if row['run'] in records:
            raise ValueError(f'{path}: duplicate record for {row["run"]}')
        records[row['run']] = row
    return records


def build(runs, hosted=False, unlisted=None):
    """unlisted: run IDs left out of the catalogue (default: the tracked unlisted-runs file)."""
    runs = Path(runs).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    unlisted = set(unlisted_runs() if unlisted is None else unlisted)
    entries, warnings, hidden = [], [], []
    for path in sorted(runs.glob('*/results.json')):
        if path.is_symlink() or path.parent.is_symlink():
            warnings.append(f'Skipped linked run: {path.parent.name}')
            continue
        if path.parent.name in unlisted:
            hidden.append(path.parent.name)  # files kept; only the catalogue row is left out
            continue
        try:
            data = json.loads(path.read_text())
            config_path = path.parent / 'settings.json'
            config = json.loads(config_path.read_text()).get('config', {}) if config_path.exists() else {}
            # Explicit allowlist: never publish the complete config or provider metadata.
            proof_rows = data.get('verified_submissions', [])
            distinct = {p['task'] for p in proof_rows if p.get('independent_validity') == 'genuine'}
            spent = Decimal(data['budget']['pilot']['settled_usd'])
            elapsed = float(data['elapsed_seconds'])
            stamp = datetime.datetime.fromisoformat(data['start_utc'])
            date = stamp.astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M %Z')
            # All entries are completed reports; incomplete runs remain in their folders.
            entry = {'run': path.parent.name, 'date': date, 'start_utc': data['start_utc'],
                     'track': config.get('track', 'unknown'), 'agents': sum(data['agent_exits'].values()),
                     'model': config.get('candidate_model_id', 'unknown'),
                     'provider_route': config.get('provider_route', 'unknown'),
                     'elapsed_seconds': elapsed, 'pilot_spent_usd': str(spent),
                     'distinct_verified_tasks': len(distinct),
                     'task_count': config.get('task_count'), 'agent_exits': data['agent_exits'],
                     'links': {}}
            for name, target in [('Report', 'results.html'), ('Transcripts', 'transcripts/index.html'),
                                 ('Conference', 'conference-view/index.html'),
                                 ('Inspect View', 'inspect-view/index.html'),
                                 ('Timeline', 'analysis/timeline.md'),
                                 # Researcher-only post-hoc transcript judge (docs/plan.md, Oct 17).
                                 ('Behavior judge', 'analysis/judge/report.html')]:
                if (path.parent / target).exists():
                    entry['links'][name] = quote(path.parent.name, safe='') + '/' + target
            logs = path.parent / 'inspect'
            if logs.is_dir():
                entry['links'].update(inspect_log_links(path.parent.name, [
                    p.name for p in logs.iterdir() if p.is_file() and not p.is_symlink()]))
            entries.append(entry)
        except (ValueError, KeyError, TypeError, OSError) as error:
            warnings.append(f'{path.parent.name}: {type(error).__name__}')
    return {**write_index(runs, entries, warnings, hosted=hosted), 'unlisted': hidden}


def write_index(runs, entries, warnings=(), *, hosted=False):
    """Render catalogue entries, including previously published remote runs."""
    runs = Path(runs)
    runs.mkdir(parents=True, exist_ok=True)
    entries = list(entries)
    entries.sort(key=lambda e: (datetime.datetime.fromisoformat(
        e['start_utc'].replace('Z', '+00:00')).timestamp(), e['run']))
    esc = html.escape
    rows = []
    for e in entries:
        links = ' · '.join(f'<a href="{esc(url, quote=True)}">{esc(name)}</a>' for name, url in e['links'].items())
        duration = f'{int(e["elapsed_seconds"]) // 60}m {int(e["elapsed_seconds"]) % 60}s'
        tasks = f'{e["distinct_verified_tasks"]} / {e["task_count"] or "?"}'
        errors = e['agent_exits'].get('error', 0)
        rows.append('<tr><td><strong>' + esc(e['date']) + '</strong><br><small>' + esc(e['run']) + '</small></td>'
                    '<td>' + esc(e['track']) + '<br><small>' + esc(e['model']) + '<br>' + esc(e['provider_route']) + '</small></td>'
                    f'<td>{e["agents"]}</td><td>{duration}</td><td>${Decimal(e["pilot_spent_usd"]):.2f}</td>'
                    f'<td>{tasks}</td><td>{errors}</td><td>{links}</td></tr>')
    body = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>GDM Math Swarm experiments</title><style>'
            'body{max-width:1450px;margin:40px auto;padding:0 24px;font:16px system-ui;line-height:1.5;color:#18212b;background:#fafbfc}'
            'h1{margin-bottom:8px}p{max-width:950px;color:#475569}input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #b8c2cf;border-radius:7px;margin:18px 0;background:white;font:inherit}'
            '.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:white}td,th{padding:14px 12px;border-bottom:1px solid #dde3ea;text-align:left;vertical-align:top}th{font-size:13px;color:#475569}small{font-size:12px;color:#596579}a{color:#155eaa}td:last-child{min-width:210px}'
            '</style><h1>GDM Math Swarm experiments</h1>'
            '<p>A private library of saved runs: results, full agent transcripts, independent evaluations and timelines.</p>'
            f'<p><strong>{len(entries)} saved experiment(s)</strong>. Verified tasks count distinct intended statements, so duplicate accepted proofs count once.</p>'
            '<label for="filter">Find an experiment</label><input id="filter" type="search" placeholder="Search date, run, model or condition…">'
            '<div class="scroll"><table id="runs"><thead><tr><th>Run</th><th>Condition / model</th><th>Agents</th><th>Duration</th><th>Pilot model cost</th><th>Verified tasks</th><th>Agent errors</th><th>Open</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div>'
            + ('<p>This private archive contains saved experiment records and derived views. Access requires permission to this Hugging Face Space. Preserve the original records when moving a run.</p>' if hosted else
               '<p>These files are stored on this computer. Raw runs and transcripts are excluded from Git; the code repository alone is not a backup or a shared results library. Keep the full experiment folder when backing up or moving a run.</p>') +
            '<script>document.getElementById("filter").addEventListener("input",function(){const q=this.value.toLowerCase();document.querySelectorAll("#runs tbody tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});</script></html>')
    (runs / 'index.html').write_text(private_space_navigation(body) if hosted else body)
    (runs / 'experiments.json').write_text(json.dumps({'schema': 'gdm-experiment-index/1', 'experiments': entries, 'warnings': warnings}, indent=2))
    return {'index': str(runs / 'index.html'), 'experiments': len(entries), 'warnings': warnings}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--runs-dir', type=Path, default=ROOT / 'runs')
    parser.add_argument('--hosted', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(args.runs_dir, hosted=args.hosted)))
