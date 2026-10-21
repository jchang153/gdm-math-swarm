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
from incident_site import has_incident_report, with_incident_link
from catalogue_page import CHEATING, COHORT_GLOSS, COHORT_LABELS, COHORTS, FLAGGED, SCRIPT, STYLE, hidden_link
from site_style import themed
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
    per log in name order. The entries keep these links as data (published entries must survive
    regeneration unchanged), but the pages no longer show them (catalogue_page.hidden_link): they
    were only downloads; read the logs in Inspect View.
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
                     'track': config.get('track', 'unknown'), 'prompt_variant': config.get('prompt_variant', 'baseline'),
                     'agents': sum(data['agent_exits'].values()),
                     'model': config.get('candidate_model_id', 'unknown'),
                     'provider_route': config.get('provider_route', 'unknown'),
                     'elapsed_seconds': elapsed, 'pilot_spent_usd': str(spent),
                     'distinct_verified_tasks': len(distinct),
                     'task_count': config.get('task_count'), 'agent_exits': data['agent_exits'],
                     'links': {}, 'summary': run_summary(path.parent, data)}
            for name, target in [('Report', 'results.html'),
                                 # Transcripts are read in Inspect View (owner, Oct 21, 2026).
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
            # Model-written incident report (docs/plan.md, October 21, 2026); only its allowlisted files are published.
            if has_incident_report(path.parent):
                entry['links'] = with_incident_link(entry['links'], path.parent.name)
            entries.append(entry)
        except (ValueError, KeyError, TypeError, OSError) as error:
            warnings.append(f'{path.parent.name}: {type(error).__name__}')
    return {**write_index(runs, entries, warnings, hosted=hosted), 'unlisted': hidden}


def judge_summary(report):
    """Cohort counts of a behavior judge report.json (gdm-judge-report/3), or None if it has none.

    Only counts already shown in the published judge report: no finding text or evidence.
    """
    counts = report.get('counts') if isinstance(report, dict) else None
    if not isinstance(counts, dict) or not isinstance(counts.get('cohorts'), dict):
        return None
    number = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None  # noqa: E731
    cohorts = {k: number(counts['cohorts'].get(k)) for k in COHORTS if k in counts['cohorts']}
    if any(v is None for v in cohorts.values()) or number(counts.get('agents')) is None:
        return None
    summary = {'mode': report.get('mode') if isinstance(report.get('mode'), str) else None,
               'agents': counts['agents'], 'graded': number(counts.get('graded')),
               'provisional': number(counts.get('provisional')) or 0, 'cohorts': cohorts}
    return summary


def run_summary(run_dir, data):
    """The catalogue's optional per-run summary: prompt variant, accepted tasks, judge cohorts.

    Entries published before October 21, 2026 lack it; the hosted catalogue then reads the same
    fields from the run's published results.json and analysis/judge/report.json in the browser.
    """
    condition = data.get('condition') if isinstance(data.get('condition'), dict) else {}
    variant = condition.get('prompt_variant')  # build_results.py records the launch setting here
    accepted = data.get('operationally_accepted_tasks')
    judge = None
    report = run_dir / 'analysis' / 'judge' / 'report.json'
    if (run_dir / 'analysis' / 'judge' / 'report.html').exists() and report.is_file() and not report.is_symlink():
        try:
            judge = judge_summary(json.loads(report.read_text()))
        except (ValueError, OSError):
            judge = None
    return {'prompt_variant': variant if isinstance(variant, str) else None,
            'accepted_tasks': accepted if isinstance(accepted, int) and not isinstance(accepted, bool) else None,
            'judge': judge}


# "Live now" (owner request, October 21, 2026; scripts/live_mirror.py). The hosted catalogue reads
# live/index.json at browser runtime, so this page stays a pure function of the catalogue entries
# (the maintenance tools reproduce it byte for byte) while the mirror updates only live/. A run that
# is already in the catalogue is never listed as live. Entries are shown with textContent only and
# every link is built here from a validated run ID, never taken from the file.
LIVE_SCRIPT = r'''<section id="live" hidden><h2>Live now</h2><p>Runs still going, mirrored from their VM about every few minutes. These are unfinished snapshots: transcripts so far in Inspect View, conference (operational) outcomes only, no independent evaluation or behavior judge yet. A run's live entry is removed once it is published here.</p><ul id="live-runs"></ul></section>
<script>(()=>{const published=new Set(PUBLISHED);const ok=/^[A-Za-z0-9][A-Za-z0-9_-]*$/;
fetch('live/index.json',{cache:'no-store'}).then(r=>r.ok?r.json():null).then(d=>{if(!d||d.schema!=='gdm-live-mirror/1'||!Array.isArray(d.runs))return;
const list=document.getElementById('live-runs');for(const e of d.runs){if(!e||!ok.test(e.run)||published.has(e.run))continue;
const li=document.createElement('li'),base='live/'+encodeURIComponent(e.run)+'/',strong=document.createElement('strong');strong.textContent=e.run;li.append(strong);
const facts=[e.state_label,e.model,e.agents!=null?e.agents+' agents':null,e.solved!=null?e.solved+' solved (conference judge)':null,e.updated_utc?'updated '+new Date(e.updated_utc).toLocaleString():null].filter(Boolean);
li.append(document.createTextNode(' · '+facts.join(' · ')+' · '));
for(const [i,[label,path]] of [['Live summary','index.html'],['Inspect View','inspect-view/index.html'],['Conference','conference-view/index.html']].entries()){const a=document.createElement('a');a.href=base+path;a.textContent=label;if(i)li.append(document.createTextNode(' · '));li.append(a);}
list.append(li);}document.getElementById('live').hidden=!list.children.length;}).catch(()=>{});})();</script>'''


def live_section(entries):
    """The hosted catalogue's "Live now" block (empty and hidden until live/index.json lists a run)."""
    published = json.dumps(sorted(e['run'] for e in entries)).replace('<', '\\u003c')
    return LIVE_SCRIPT.replace('PUBLISHED', published)


def newest_first(entries):
    """Catalogue order: newest first by actual start time (owner request, October 21, 2026); ties by run ID, reversed."""
    return sorted(entries, key=lambda e: (datetime.datetime.fromisoformat(
        e['start_utc'].replace('Z', '+00:00')).timestamp(), e['run']), reverse=True)


def duration_text(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f'{seconds // 3600}h {seconds % 3600 // 60:02d}m'
    return f'{seconds // 60}m {seconds % 60:02d}s'


def judge_text(entry):
    """(cell HTML, sort value) of the behavior judge column; plain words, no finding text."""
    summary = entry.get('summary')
    judge = summary.get('judge') if isinstance(summary, dict) else None
    if judge:
        parts = [f'{judge["cohorts"][k]} {COHORT_LABELS[k][0 if judge["cohorts"][k] == 1 else 1]}'
                 for k in FLAGGED if judge['cohorts'].get(k)]
        flagged = sum(judge['cohorts'].get(k, 0) for k in CHEATING)
        text = ' · '.join(parts) if parts else 'none flagged'
        graded = judge['graded'] if judge['graded'] is not None else judge['agents']
        extra = f'of {graded} graded' + (f', {judge["provisional"]} provisional' if judge['provisional'] else '')
        if judge.get('mode') not in (None, 'paid'):
            extra += f' ({judge["mode"]})'
        return f'{html.escape(text)}<br><small>{html.escape(extra)}</small>', flagged
    if 'Behavior judge' in entry['links']:
        return ('see report' if isinstance(summary, dict) else '<span class="pending">…</span>'), -1
    return '<span class="muted">not run</span>', -2


def write_index(runs, entries, warnings=(), *, hosted=False):
    """Render catalogue entries, including previously published remote runs."""
    runs = Path(runs)
    runs.mkdir(parents=True, exist_ok=True)
    entries = newest_first(entries)
    esc = html.escape
    rows = []
    for e in entries:
        links = ' · '.join(f'<a href="{esc(url, quote=True)}">{esc(name)}</a>' for name, url in e['links'].items()
                           if not hidden_link(name))
        summary = e.get('summary') if isinstance(e.get('summary'), dict) else None
        pending = '<span class="pending" title="Not in this catalogue entry">…</span>'
        variant = (esc(summary['prompt_variant']) if summary and summary.get('prompt_variant')
                   else '<span class="muted">not recorded</span>' if summary else pending)
        accepted = summary.get('accepted_tasks') if summary else None
        accepted_text = str(accepted) if accepted is not None else ('?' if summary else pending)
        judge, judge_sort = judge_text(e)
        stamp = datetime.datetime.fromisoformat(e['start_utc'].replace('Z', '+00:00')).timestamp()
        spent = Decimal(e['pilot_spent_usd'])
        errors = e['agent_exits'].get('error', 0)
        cells = [('Start', f'{stamp:.3f}', '', f'<strong>{esc(e["date"])}</strong><br><small>{esc(e["run"])}</small>'),
                 ('Model', esc(e['model'], quote=True), '',
                  f'{esc(e["model"])}<br><small>{esc(e["track"])} · {esc(e["provider_route"])}</small>'),
                 ('Prompt variant', None, 'c-variant', variant),
                 ('Agents', str(e['agents']), '', str(e['agents'])
                  + (f'<br><small>{errors} error exit{"s" if errors != 1 else ""}</small>' if errors else '')),
                 ('Duration', f'{float(e["elapsed_seconds"]):.3f}', '', duration_text(e['elapsed_seconds'])),
                 ('Accepted / genuine', str(e['distinct_verified_tasks']), 'c-tasks',
                  f'<span class="c-accepted">{accepted_text}</span> / <strong>{e["distinct_verified_tasks"]}</strong>'
                  f'<br><small>of {esc(str(e["task_count"] or "?"))} tasks</small>'),
                 ('Cost', f'{spent:.6f}', '', f'${spent:.2f}'),
                 ('Behavior judge', str(judge_sort), 'c-judge', judge),
                 ('Open', None, 'open', links)]
        rows.append(f'<tr data-run="{esc(e["run"], quote=True)}"><td class="pick"><input type="checkbox" '
                    f'aria-label="Compare {esc(e["run"], quote=True)}" value="{esc(e["run"], quote=True)}"></td>'
                    + ''.join(f'<td data-label="{label}"' + (f' data-sort="{sort}"' if sort is not None else '')
                              + (f' class="{cls}"' if cls else '') + f'><div class="v">{content}</div></td>'
                              for label, sort, cls, content in cells) + '</tr>')
    # The compare view and the filters read the same entries; '<' is escaped so the JSON
    # can never close its script element.
    blob = json.dumps(entries, separators=(',', ':'), ensure_ascii=False).replace('<', '\\u003c')
    note = ('This private archive contains saved experiment records and derived views. Access requires permission '
            'to this Hugging Face Space. Preserve the original records when moving a run.' if hosted else
            'These files are stored on this computer. Raw runs and transcripts are excluded from Git; the code '
            'repository alone is not a backup or a shared results library. Keep the full experiment folder when '
            'backing up or moving a run.')
    glossary = ''.join(f'<dt>{esc(COHORT_LABELS[k][1].capitalize())}</dt><dd>{esc(COHORT_GLOSS[k])}</dd>'
                       for k in COHORTS)
    headers = [('', None), ('Start', 'start'), ('Model', 'model'), ('Prompt variant', 'variant'),
               ('Agents', 'agents'), ('Duration', 'duration'), ('Accepted / genuine', 'tasks'),
               ('Cost', 'cost'), ('Behavior judge', 'judge'), ('Open', None)]
    head = ''.join(f'<th scope="col" data-key="{k}"' + (' aria-sort="descending"' if k == 'start' else '')
                   + f'><button type="button">{esc(t)}</button></th>' if k else
                   f'<th scope="col">{"<span class=sr>Select</span>" if not t else esc(t)}</th>' for t, k in headers)
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>GDM Math Swarm experiments</title><style>' + STYLE + '</style></head><body>'
            '<h1>GDM Math Swarm experiments</h1>'
            '<p>A private library of saved runs: results, agent transcripts in Inspect View, independent evaluations and timelines.</p>'
            f'<p><strong>{len(entries)} saved experiment(s)</strong>, newest first. Tick 2 to 4 runs to compare them side by side.</p>'
            + (live_section(entries) if hosted else '') +
            '<div class="toolbar"><label class="grow">Find<input id="filter" type="search" placeholder="Date, run, model, condition…"></label>'
            '<label>Model<select id="f-model"><option value="">All</option></select></label>'
            '<label>Prompt variant<select id="f-variant"><option value="">All</option></select></label>'
            '<label>Behavior judge<select id="f-judge"><option value="">All</option><option value="run">Run</option>'
            '<option value="flagged">Cheating found</option><option value="clean">None flagged</option>'
            '<option value="none">Not run</option></select></label>'
            '<label>Sort<select id="sort">' + ''.join(f'<option value="{k}">{esc(t)}</option>' for t, k in headers if k)
            + '</select></label><button type="button" id="direction" aria-label="Sort direction">Newest first ↓</button></div>'
            '<div class="actions"><span id="shown" aria-live="polite"></span>'
            '<button type="button" id="compare" disabled>Compare selected (0)</button>'
            '<button type="button" id="clear" class="quiet">Clear selection</button></div>'
            '<section id="compare-view" hidden aria-label="Compare runs"><div class="cmp-head"><h2>Compare runs</h2>'
            '<button type="button" id="close-compare" class="quiet">Close</button></div><div class="scroll" id="compare-table"></div></section>'
            '<div class="scroll"><table id="runs"><thead><tr>' + head + '</tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div>'
            '<details class="help"><summary>What the columns mean</summary><dl>'
            '<dt>Accepted / genuine</dt><dd>Accepted: distinct tasks the weak operational judge accepted during the run '
            '(acceptance is not mathematical validity). Genuine: distinct intended statements with at least one '
            'submission the independent evaluator found genuine, so duplicate accepted proofs count once.</dd>'
            '<dt>Cost</dt><dd>Settled pilot model spend from the run\'s budget ledger.</dd>'
            '<dt>Behavior judge</dt><dd>The researcher-only post-hoc transcript judge, by cohort. "not run" means no '
            'judge outputs are published for the run; "…" means the entry predates these columns and the run\'s own '
            'records could not be read here.</dd>' + glossary + '</dl></details>'
            f'<p class="note">{esc(note)}</p>'
            f'<script type="application/json" id="catalogue-data">{blob}</script>'
            '<script>' + SCRIPT + '</script></body></html>')
    body = themed(body)
    (runs / 'index.html').write_text(private_space_navigation(body) if hosted else body)
    (runs / 'experiments.json').write_text(json.dumps({'schema': 'gdm-experiment-index/1', 'experiments': entries, 'warnings': warnings}, indent=2))
    return {'index': str(runs / 'index.html'), 'experiments': len(entries), 'warnings': warnings}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--runs-dir', type=Path, default=ROOT / 'runs')
    parser.add_argument('--hosted', action='store_true')
    args = parser.parse_args()
    print(json.dumps(build(args.runs_dir, hosted=args.hosted)))
