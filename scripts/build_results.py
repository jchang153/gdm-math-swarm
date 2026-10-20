#!/usr/bin/env python3
"""Write a completed run's results.json and results.html for the HF experiment library.

A deterministic, facts-only review of the run's own records, modelled on the
gemini10-20261020T054602Z report layout. Every field is computed from records; the only free
text is researcher notes given explicitly with --caveat / --caveat-for (stored under
"researcher_notes", shown under "Researcher notes"). Record contents (board posts, DMs,
library, private files, transcripts) are linked, never summarized. Offline: no model calls,
network, Docker, credentials, keys or .env.

Inputs (read-only): RUN/settings.json (condition), RUN/runtime.jsonl (clock, stop reason,
per-agent requests, exits and restarts, settlement and token usage),
RUN/conference/state.sqlite (opened read-only: every submission with its time and outcome),
RUN/budget-final.json (ledger snapshot), and, only when present, RUN/evaluations/
(proof checker) and the existence of RUN/analysis/judge/report.json, plus the files named by
--ledger and --key-usage-log. Without evaluator records the report states "Proof checker: not
run" and every accepted submission is "not_evaluated". Behavior-judge findings and fields stay
out of results.*: they live only in the judge's own pages (analysis/judge/report.html and its
evidence browser, linked separately from the catalogue); the report carries only a status and
link line ("Behavior judge: see analysis/judge/report.html" or "Behavior judge: not run").

Publication steps (docs/experiment-library.md):

  1) python scripts/build_results.py --run-dir runs/RUN_ID [--ledger LEDGER.sqlite]
         [--key-usage-log LOG.jsonl] [--caveat TEXT] [--caveat-for SUBJECT TEXT] [--title TEXT]
  2) python3 scripts/prepare_hf_library.py --run "RUN_ID=VIEWER_BUNDLE" --output-dir NEW_BUNDLE
  3) python scripts/publish_hf_library.py --bundle NEW_BUNDLE          # read-only preview
     python scripts/publish_hf_library.py --bundle NEW_BUNDLE --commit

Existing results files are never replaced without --overwrite; --output-dir writes a scratch
copy for review instead.

Spending. With --ledger (a consistent copy of the budget ledger SQLite, opened immutable
read-only) "budget.pilot" counts exactly the ledger calls whose request IDs this run admitted
in runtime.jsonl and "cumulative_budget" summarizes the whole ledger. Without it
"budget.pilot" comes from runtime.jsonl (requests settled only after the run stay counted as
held, with a warning) and "cumulative_budget" is RUN/budget-final.json.
"""
import argparse
import collections
import datetime
from decimal import Decimal
import html
import json
from pathlib import Path
import sqlite3
import sys

NANO = Decimal('1e-9')
ACCEPTED = ('accepted', 'accepted_after_close')
SCOPES = ('pilot', 'compatibility', 'analysis')
JUDGE_REPORT = 'analysis/judge/report.html'
STYLE = ('body{max-width:1100px;margin:40px auto;padding:0 22px;font:16px system-ui;line-height:1.55}'
         'table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:9px;border-bottom:1px solid #ddd;'
         'overflow-wrap:anywhere}a{color:#155eaa}small{color:#596579}')
LINKS = (('Agent transcripts', 'transcripts/index.html'),
         ('Conference: board, DMs, library, private files and judging', 'conference-view/index.html'),
         ('Inspect View', 'inspect-view/index.html'), ('Inspect logs', 'inspect/'),
         ('Timeline', 'analysis/timeline.md'), ('Settings', 'settings.json'))
esc = html.escape


def usd(nano):
    """Ledger integers are nano-dollars."""
    return format((Decimal(nano) * NANO).normalize(), 'f') if nano else '0'  # never exponent notation


def dec(value):
    return format(value.normalize(), 'f') if value else '0'


def utc(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


def hhmm(timestamp):
    return utc(timestamp).strftime('%H:%M UTC')


def regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'{path} must be a regular file')
    return path


def json_lines(path):
    return [json.loads(line) for line in regular(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def read_runtime(run, stop_utc=None):
    """Single pass over runtime.jsonl."""
    out = {'start': None, 'stop': None, 'reason': None, 'elapsed': None, 'exits': collections.Counter(),
           'budget_exits': [], 'restarts': 0, 'admitted': {}, 'finished': {}, 'unbilled': set(),
           'tokens': collections.Counter(), 'agents': collections.defaultdict(lambda: {
               'requests': 0, 'finished': 0, 'unbilled': 0, 'restarts': 0, 'exit': None, 'exit_at': None})}
    for event in json_lines(run / 'runtime.jsonl'):
        kind, payload, agent = event.get('kind'), event.get('payload') or {}, event.get('agent_id')
        if kind == 'common_start' and out['start'] is None:
            out['start'] = event['timestamp']
        elif kind == 'run_stop':
            out['stop'], out['reason'] = event['timestamp'], payload.get('reason')
            out['elapsed'] = payload.get('elapsed_seconds')
        elif kind == 'agent_configured' and agent:
            out['agents'][agent]
        elif kind == 'agent_exit':
            out['exits'][payload['status']] += 1
            out['agents'][agent].update(exit=payload['status'], exit_at=hhmm(event['timestamp']))
            if payload['status'] == 'budget_exhausted':
                out['budget_exits'].append((agent, event['timestamp']))
        elif kind == 'agent_restart':
            out['restarts'] += 1
            out['agents'][agent]['restarts'] += 1
        elif kind == 'inference_admitted':
            out['admitted'][payload['request_id']] = Decimal(payload.get('reserved_usd') or 0)
            out['agents'][agent]['requests'] += 1
        elif kind == 'inference_unbilled':
            out['unbilled'].add(payload.get('request_id'))
            out['agents'][agent]['unbilled'] += 1
        elif kind == 'inference_finished':
            out['finished'][payload.get('request_id')] = (payload.get('basis'), payload.get('settled_usd'))
            out['agents'][agent]['finished'] += 1
            usage = payload.get('provider_usage') or {}
            tokens = out['tokens']
            tokens['prompt_tokens'] += usage.get('prompt_tokens') or 0
            tokens['cached_tokens'] += (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0
            tokens['completion_tokens'] += usage.get('completion_tokens') or 0
            tokens['reasoning_tokens'] += (usage.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0
    if stop_utc is not None:  # explicit boundary for a run stopped without a run_stop event
        if out['stop'] is not None:
            raise ValueError('runtime.jsonl has a run_stop event; --stop-utc applies only to runs without one')
        out['stop'] = datetime.datetime.fromisoformat(stop_utc.replace('Z', '+00:00')).timestamp()
    if out['start'] is None or out['stop'] is None:
        raise ValueError('runtime.jsonl has no common_start or run_stop: the run is not complete (pass --stop-utc '
                         'and --stop-reason for a run stopped without a run_stop event)')
    if out['elapsed'] is None:
        out['elapsed'] = out['stop'] - out['start']
    return out


def ledger_scope(db, scope, ids=None):
    rows = db.execute('SELECT id,state,basis,actual,reserved FROM calls WHERE scope=?', (scope,)).fetchall()
    if ids is not None:
        rows = [r for r in rows if r[0] in ids]
    settled = [r for r in rows if r[1] == 'settled']
    held = [r for r in rows if r[1] != 'settled']
    summary = {'settled_usd': usd(sum(r[3] or 0 for r in settled)), 'held_usd': usd(sum(r[4] for r in held)),
               'unresolved_requests': len(held), 'settled_requests': len(settled),
               'unbilled_error_requests': sum(r[2] == 'unbilled_error' for r in rows),
               'provider_reported_cost_usd': usd(sum(r[3] or 0 for r in rows if r[2] == 'provider_reported_cost')),
               'manual_reconciliation_usd': usd(sum(r[3] or 0 for r in rows if r[2] == 'manual_reconciliation')),
               'manual_reconciliation_requests': sum(r[2] == 'manual_reconciliation' for r in rows)}
    return summary, sum(r[3] or 0 for r in settled) + sum(r[4] for r in held)


def budgets(run, runtime, ledger):
    """Run-only pilot spending and the cumulative ledger, each labelled with its source."""
    final = json.loads(regular(run / 'budget-final.json').read_text())
    if ledger is not None:
        db = sqlite3.connect(f'file:{regular(ledger).resolve()}?mode=ro&immutable=1', uri=True)
        try:
            caps = dict(db.execute('SELECT name,cap FROM scopes'))
            pilot, _ = ledger_scope(db, 'pilot', set(runtime['admitted']))
            pilot['cap_usd'] = usd(caps.get('pilot', 0))
            pilot['source'] = 'ledger calls with request IDs admitted in runtime.jsonl'
            cumulative = {}
            for scope in SCOPES:
                summary, committed = ledger_scope(db, scope)
                summary['cap_usd'] = usd(caps.get(scope, 0))
                summary['remaining_usd'] = usd(caps.get(scope, 0) - committed)
                summary['source'] = 'ledger'
                cumulative[scope] = summary
        finally:
            db.close()
        return pilot, cumulative
    settled = held = provider = Decimal(0)
    settled_n = unresolved = 0
    for request, reserved in runtime['admitted'].items():
        basis, amount = runtime['finished'].get(request, (None, None))
        if request in runtime['unbilled']:
            settled_n += 1
        elif basis is not None:
            settled_n += 1
            settled += Decimal(amount or 0)
            if basis == 'provider_reported_cost':
                provider += Decimal(amount or 0)
        else:
            unresolved += 1
            held += reserved
    if unresolved:
        print(f'warning: {unresolved} request(s) have no settlement in runtime.jsonl and are reported as held; pass '
              '--ledger with a post-settlement ledger copy for exact run spending', file=sys.stderr)
    pilot = {'settled_usd': dec(settled), 'held_usd': dec(held), 'unresolved_requests': unresolved,
             'settled_requests': settled_n,
             'unbilled_error_requests': len(runtime['unbilled'] & set(runtime['admitted'])),
             'provider_reported_cost_usd': dec(provider), 'manual_reconciliation_usd': '0',
             'manual_reconciliation_requests': 0, 'cap_usd': (final.get('pilot') or {}).get('cap_usd'),
             'source': 'runtime.jsonl'}
    cumulative = {}
    for scope in SCOPES:
        s = final.get(scope) or {}
        cumulative[scope] = {'settled_usd': s.get('settled_usd'), 'held_usd': s.get('held_usd'),
                             'unresolved_requests': s.get('unresolved_requests'),
                             'settled_requests': s.get('settled_requests'),
                             'unbilled_error_requests': s.get('unbilled_error_requests'),
                             'provider_reported_cost_usd': None, 'manual_reconciliation_usd': None,
                             'manual_reconciliation_requests': s.get('manual_reconciliations'),
                             'cap_usd': s.get('cap_usd'), 'remaining_usd': s.get('remaining_usd'),
                             'source': 'budget-final.json'}
    return pilot, cumulative


def conference(run, ids):
    db = sqlite3.connect(f'file:{regular(run / "conference" / "state.sqlite").resolve()}?mode=ro', uri=True)
    try:
        columns = {r[1] for r in db.execute('PRAGMA table_info(submissions)')}
        public = 'public_id' if 'public_id' in columns else 'NULL'
        rows = db.execute(f'SELECT submission_id,{public},agent_id,task_id,outcome,source_sha256,submitted_at '
                          'FROM submissions ORDER BY submission_id').fetchall()
    finally:
        db.close()
    every = [{'submission': public_id if ids == 'public' and public_id else f'sub-{serial:06d}',
              'agent': agent, 'task': task, 'operational_outcome': outcome, 'source_sha256': digest,
              'submitted_utc': utc(at).isoformat(), '_serial': f'sub-{serial:06d}'}
             for serial, public_id, agent, task, outcome, digest, at in rows]
    return every


def evaluate(run, accepted):
    """Bind proof-checker records to accepted submissions (by serial ID, task and source hash)."""
    path = run / 'evaluations' / 'evaluations.jsonl'
    if not path.exists():
        return None
    latest = {}
    for record in json_lines(path):
        latest[record['submission']['submission_id']] = record
    used, unresolved = [], []
    for row in accepted:
        record = latest.get(row['_serial'])
        if record is None:
            continue
        if record['task_id'] != row['task'] or record['source_sha256'] != row['source_sha256']:
            raise ValueError(f'Evaluator record for {row["submission"]} does not match its task or source hash')
        used.append(record)
        details = (record.get('evidence') or {}).get('details')
        row.update(independent_validity=record['validity'], evaluator_reason=record['reason'],
                   evaluator_details=details)
        if record['validity'] != 'genuine':
            unresolved.append({'submission': row['submission'], 'agent': row['agent'], 'task': row['task'],
                               'validity': record['validity'], 'source_sha256': row['source_sha256'],
                               'evaluator_reason': record['reason'], 'evaluator_details': details})
    validations = sorted({((r.get('evaluator') or {}).get('validation_record') or '',
                           (r.get('evaluator') or {}).get('validation_sha256')) for r in used})
    return {'status': 'completed' if len(used) == len(accepted) else 'partial',
            'source': 'evaluations/evaluations.jsonl', 'evaluated_submissions': len(used),
            'accepted_submissions': len(accepted),
            'by_validity': dict(sorted(collections.Counter(r['validity'] for r in used).items())),
            'distinct_verified_tasks': len({r['task'] for r in accepted if r['independent_validity'] == 'genuine'}),
            'not_genuine': unresolved,
            'validation_records': [{'path': ('data/validation/' + p.split('/data/validation/', 1)[1]
                                             if '/data/validation/' in p else p or None), 'sha256': h}
                                   for p, h in validations],
            'records': [p for p in ('evaluations/evaluations.jsonl', 'evaluations/summary.json')
                        if (run / p).exists()]}


def judge_summary(run):
    """Status and link only: judge findings and fields stay in the judge's own pages."""
    path = run / 'analysis' / 'judge' / 'report.json'
    if not path.exists():
        return None
    report = json.loads(regular(path).read_text(encoding='utf-8'))
    if report.get('run_id') not in (None, run.name):
        raise ValueError(f'analysis/judge/report.json belongs to run {report.get("run_id")!r}')
    return {'status': 'completed', 'report': JUDGE_REPORT}


def build(run, *, ledger=None, key_usage_log=None, notes=(), title=None, stop_reason=None, stop_utc=None,
          ids='public'):
    """Return (results_json_text, results_html_text)."""
    recorded = json.loads(regular(run / "settings.json").read_text()).get('config', {})
    if stop_utc is not None and not stop_reason:
        raise ValueError('--stop-utc requires --stop-reason')
    runtime = read_runtime(run, stop_utc)
    pilot, cumulative = budgets(run, runtime, ledger)
    every = conference(run, ids)
    accepted = [dict(r, independent_validity='not_evaluated') for r in every if r['operational_outcome'] in ACCEPTED]
    evaluation = evaluate(run, accepted)
    judge = judge_summary(run)
    tokens = runtime['tokens']
    by_agent = collections.defaultdict(collections.Counter)
    by_task = collections.defaultdict(collections.Counter)
    first_accept = {}
    for r in every:
        by_agent[r['agent']][r['operational_outcome']] += 1
        by_task[r['task']][r['operational_outcome']] += 1
        if r['operational_outcome'] == 'accepted' and r['task'] not in first_accept:
            first_accept[r['task']] = {'agent': r['agent'], 'submitted_utc': r['submitted_utc']}
    agents = sorted(set(runtime['agents']) | set(by_agent))
    results = {
        'run': run.name, 'start_utc': utc(runtime['start']).isoformat(), 'stop_utc': utc(runtime['stop']).isoformat(),
        'elapsed_seconds': runtime['elapsed'], 'stop_reason': stop_reason or runtime['reason'],
        'runtime_stop_reason': runtime['reason'],
        'condition': {'experiment': recorded.get('experiment'), 'track': recorded.get('track'),
                      'agents': recorded.get('agent_count', len(agents)), 'task_count': recorded.get('task_count'),
                      'task_set': recorded.get('task_set'), 'model': recorded.get('candidate_model_id'),
                      'provider': recorded.get('provider'), 'provider_route': recorded.get('provider_route'),
                      'execution': recorded.get('execution'), 'prompt_variant': recorded.get('prompt_variant'),
                      'auto_restart': recorded.get('auto_restart'), 'max_run_seconds': recorded.get('max_run_seconds'),
                      'budget_track': recorded.get('budget_track'),
                      'seeded_contagion_enabled': recorded.get('seeded_contagion_enabled'),
                      'mitigations_enabled': recorded.get('mitigations_enabled')},
        'agent_exits': dict(runtime['exits']), 'auto_restarts': runtime['restarts'],
        'budget_exhausted_agents': [{'agent': a, 'at': hhmm(t)} for a, t in runtime['budget_exits']],
        'agents': [{'agent': a, **({k: runtime['agents'][a][k] for k in
                                    ('requests', 'finished', 'unbilled', 'restarts', 'exit', 'exit_at')}
                                   if a in runtime['agents'] else {}),
                    'submissions': dict(sorted(by_agent[a].items()))} for a in agents],
        'budget': {'pilot': pilot}, 'cumulative_budget': cumulative}
    if key_usage_log is not None:
        last = json_lines(key_usage_log)[-1]
        results['key_usage_counter'] = {'billed_since_launch_usd': last['billed_since_launch'],
                                        'ledger_settled_this_run_usd': last['settled_this_run'],
                                        'source': Path(key_usage_log).name}
    public = [{k: v for k, v in row.items() if not k.startswith('_')} for row in accepted]
    keys = ('submission', 'agent', 'task', 'operational_outcome', 'independent_validity', 'source_sha256')
    results.update({
        'submission_outcomes': dict(sorted(collections.Counter(r['operational_outcome'] for r in every).items())),
        'operationally_accepted_tasks': len({r['task'] for r in public}),
        'tasks': [{'task': t, 'submissions': dict(sorted(c.items())), 'first_accepted': first_accept.get(t)}
                  for t, c in sorted(by_task.items())],
        'verified_submissions': [{k: r[k] for k in keys} for r in public if r['independent_validity'] == 'genuine'],
        'accepted_submissions': public,
        'independent_evaluation': evaluation or {'status': 'not_run'},
        'behavior_judge': judge or {'status': 'not_run'},
        'cache': {**{k: tokens[k] for k in ('prompt_tokens', 'cached_tokens', 'completion_tokens', 'reasoning_tokens')},
                  'input_cache_fraction': (tokens['cached_tokens'] / tokens['prompt_tokens']
                                           if tokens['prompt_tokens'] else None),
                  'source': 'provider_usage of inference_finished runtime events'},
        'researcher_notes': [dict(n) for n in notes]})
    return json.dumps(results, indent=1) + '\n', render(results, title)


def table(rows, header=None):
    head = '<tr>' + ''.join(f'<th>{esc(h)}</th>' for h in header) + '</tr>' if header else ''
    return '<table>' + head + ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>'
                                      for row in rows) + '</table>'


def value(v):
    if v is None:
        return 'not recorded'
    if isinstance(v, bool):
        return 'yes' if v else 'no'
    if isinstance(v, dict):
        return ', '.join(f'{k}: {value(x)}' for k, x in v.items()) or 'none'
    return str(v)


def facts(rows):
    return table([(esc(k), esc(value(v))) for k, v in rows])


def money(v):
    return 'not recorded' if v is None else f'${v}'


def render(results, title):
    c, ev, judge = results['condition'], results['independent_evaluation'], results['behavior_judge']
    pilot = results['budget']['pilot']
    elapsed = int(float(results['elapsed_seconds']))
    body = [f'<h1>{esc(title or results["run"])}</h1>', f'<p><code>{esc(results["run"])}</code></p>',
            '<h2>Run</h2>', facts([
                ('Start (UTC)', results['start_utc']), ('Stop (UTC)', results['stop_utc']),
                ('Elapsed', f'{elapsed // 3600}h {elapsed % 3600 // 60}m {elapsed % 60}s'),
                ('Stop reason', results['stop_reason']), ('Runtime stop reason', results['runtime_stop_reason']),
                ('Agent exits', results['agent_exits']), ('Auto-restarts', results['auto_restarts']),
                ('Budget-exhausted agents', ', '.join(f'{a["agent"]} ({a["at"]})'
                                                      for a in results['budget_exhausted_agents']) or 'none'),
                ('Submission outcomes', results['submission_outcomes']),
                ('Operationally accepted tasks', f'{results["operationally_accepted_tasks"]} of {c["task_count"]}'),
                ('Proof checker', ev['status'].replace('_', ' ')),
                ('Behavior judge', judge['status'].replace('_', ' ')),
                ('Pilot settled (this run)', money(pilot['settled_usd']))]),
            '<h2>Condition</h2>', facts([
                ('Experiment', c['experiment']), ('Track', c['track']), ('Agents', c['agents']),
                ('Execution', c['execution']), ('Tasks', c['task_count']), ('Task set', c['task_set']),
                ('Model', c['model']), ('Provider', c['provider']), ('Provider route', c['provider_route']),
                ('Prompt variant', c['prompt_variant']), ('Auto-restart', c['auto_restart']),
                ('Max run seconds', c['max_run_seconds']), ('Budget track', c['budget_track']),
                ('Seeded contagion', c['seeded_contagion_enabled']), ('Mitigations', c['mitigations_enabled'])]),
            '<h2>Agents</h2>', table([(esc(a['agent']), *(esc(value(a.get(k))) for k in (
                'requests', 'finished', 'unbilled', 'restarts', 'exit', 'exit_at', 'submissions')))
                for a in results['agents']], ('Agent', 'Requests', 'Finished', 'Unbilled', 'Restarts', 'Exit',
                                              'Exit time', 'Submissions')),
            '<h2>Submissions by task</h2>', table([(esc(t['task']), esc(value(t['submissions'])), esc(
                f'{t["first_accepted"]["agent"]} at {t["first_accepted"]["submitted_utc"]}'
                if t['first_accepted'] else 'none')) for t in results['tasks']],
                ('Task', 'Submissions by outcome', 'First accepted')),
            '<h2>Accepted submissions</h2>']

    def validity(row):
        cell = esc(row['independent_validity'])
        if row['independent_validity'] != 'genuine' and row.get('evaluator_reason'):
            cell += '<br><small>' + esc(row['evaluator_reason'] + (': ' + row['evaluator_details']
                                                                    if row.get('evaluator_details') else '')) + '</small>'
        return cell
    body.append(table([(esc(r['submission']), esc(r['agent']), esc(r['task']), esc(r['operational_outcome']),
                        esc(r['submitted_utc']), validity(r)) for r in results['accepted_submissions']],
                      ('Submission', 'Agent', 'Task', 'Conference', 'Submitted (UTC)', 'Independent validity')))
    if ev['status'] == 'not_run':
        body.append('<p>Proof checker: not run</p>')
    else:
        body += [f'<h2>Proof checker <small>(source: {esc(ev["source"])})</small></h2>', facts(
            [('Status', ev['status']), ('Evaluated submissions', f'{ev["evaluated_submissions"]} of '
                                                                f'{ev["accepted_submissions"]}'),
             ('By validity', ev['by_validity']), ('Distinct verified tasks', ev['distinct_verified_tasks'])]
            + [(f'Validation record {i + 1}', f'{v["path"]} (SHA-256 {v["sha256"]})')
               for i, v in enumerate(ev['validation_records'])]),
            '<p>' + ' · '.join(f'<a href="{esc(p, quote=True)}">{esc(p)}</a>' for p in ev['records']) + '</p>']
    body.append('<p>Behavior judge: not run</p>' if judge['status'] == 'not_run' else
                f'<p>Behavior judge: see <a href="{esc(judge["report"], quote=True)}">{esc(judge["report"])}</a></p>')
    body.append('<h2>Accounting</h2>')
    body.append(table([(esc(s), money(b['settled_usd']), money(b['held_usd']), esc(value(b['unresolved_requests'])),
                        esc(value(b['settled_requests'])), money(b['cap_usd']), money(b.get('remaining_usd')),
                        esc(b['source'])) for s, b in [('pilot (this run)', pilot), *results['cumulative_budget'].items()]],
                      ('Scope', 'Settled', 'Held', 'Unresolved requests', 'Settled requests', 'Cap', 'Remaining',
                       'Source')))
    detail = [('Provider-reported (this run)', money(pilot['provider_reported_cost_usd'])),
              ('Manual reconciliation (this run)', f'{money(pilot["manual_reconciliation_usd"])} over '
                                                   f'{pilot["manual_reconciliation_requests"]} requests'),
              ('Unbilled error requests (this run)', pilot['unbilled_error_requests'])]
    if 'key_usage_counter' in results:
        k = results['key_usage_counter']
        detail += [('Key usage counter since launch', money(k['billed_since_launch_usd'])),
                   ('Ledger settled this run (key usage log)', money(k['ledger_settled_this_run_usd']))]
    cache = results['cache']
    fraction = cache['input_cache_fraction']
    detail += [('Input tokens', f'{cache["prompt_tokens"]:,}'), ('Cached input tokens', f'{cache["cached_tokens"]:,}'),
               ('Input cache fraction', 'not recorded' if fraction is None else f'{100 * fraction:.2f}%'),
               ('Completion tokens (including reasoning)', f'{cache["completion_tokens"]:,}'),
               ('Reasoning tokens', f'{cache["reasoning_tokens"]:,}')]
    body.append(facts(detail))
    if results['researcher_notes']:
        body.append('<h2>Researcher notes</h2><ul>' + ''.join(
            '<li>' + (f'<strong>{esc(n["subject"])}</strong>: ' if n.get('subject') else '') + esc(n['text']) + '</li>'
            for n in results['researcher_notes']) + '</ul>')
    body.append('<h2>Records</h2><p>' + ' · '.join(f'<a href="{href}">{label}</a>' for label, href in LINKS) + '</p>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{esc(title or results["run"] + " results")}</title>'
            f'<style>{STYLE}</style>' + ''.join(body) + '</html>\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n'.join(__doc__.split('\n')[1:]))
    parser.add_argument('--run-dir', type=Path, required=True, help='completed run directory, e.g. runs/RUN_ID')
    parser.add_argument('--output-dir', type=Path, help='write results.* here instead of the run directory')
    parser.add_argument('--overwrite', action='store_true', help='replace existing results.json/results.html')
    parser.add_argument('--ledger', type=Path, help='consistent copy of the budget ledger SQLite for exact spending')
    parser.add_argument('--key-usage-log', type=Path,
                        help='settle-by-key-usage JSONL log; its last record gives key_usage_counter')
    parser.add_argument('--caveat', action='append', default=[], metavar='TEXT',
                        help='researcher note (free text, shown under "Researcher notes"); repeatable')
    parser.add_argument('--caveat-for', action='append', default=[], nargs=2, metavar=('SUBJECT', 'TEXT'),
                        help='researcher note about a task or submission; repeatable')
    parser.add_argument('--title', help='page title and heading (default: the run ID)')
    parser.add_argument('--stop-reason', help='reported stop_reason when it differs from the runtime reason')
    parser.add_argument('--stop-utc', help='stop boundary (ISO 8601) for a run without a run_stop event')
    parser.add_argument('--submission-ids', choices=('public', 'serial'), default='public',
                        help='public conference IDs (default) or serial sub-NNNNNN IDs')
    args = parser.parse_args(argv)
    run = args.run_dir.resolve()
    if args.run_dir.is_symlink() or not run.is_dir():
        raise SystemExit(f'Not a run directory: {args.run_dir}')
    notes = ([{'subject': s, 'text': t} for s, t in args.caveat_for]
             + [{'subject': None, 'text': t} for t in args.caveat])
    if any(not n['text'].strip() for n in notes):
        raise SystemExit('Researcher notes must not be empty')
    json_text, page = build(run, ledger=args.ledger, key_usage_log=args.key_usage_log, notes=notes,
                            title=args.title, stop_reason=args.stop_reason, stop_utc=args.stop_utc,
                            ids=args.submission_ids)
    output = (args.output_dir or run).resolve()
    targets = [output / 'results.json', output / 'results.html']
    existing = [str(p) for p in targets if p.exists() or p.is_symlink()]
    if existing and not args.overwrite:
        raise SystemExit('Refusing to replace existing ' + ', '.join(existing) + ' (pass --overwrite or --output-dir)')
    output.mkdir(parents=True, exist_ok=True)
    for path, text in zip(targets, (json_text, page)):
        if path.is_symlink():
            raise SystemExit(f'Refusing to write through a symlink: {path}')
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    data = json.loads(json_text)
    print(json.dumps({'results_json': str(targets[0]), 'results_html': str(targets[1]),
                      'proof_checker': data['independent_evaluation']['status'],
                      'distinct_verified_tasks': data['independent_evaluation'].get('distinct_verified_tasks', 0),
                      'behavior_judge': data['behavior_judge']['status'],
                      'pilot_settled_usd': data['budget']['pilot']['settled_usd']}))


if __name__ == '__main__':
    main()
