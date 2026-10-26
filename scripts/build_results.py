#!/usr/bin/env python3
"""Write a completed run's results.json and results.html for the HF experiment library.

A deterministic, facts-only review of the run's own records, modelled on the
gemini10-20261020T054602Z report layout. Every field is computed from records; the only free
text is researcher notes given explicitly with --caveat / --caveat-for (stored under
"researcher_notes", shown under "Researcher notes"). Record contents (board posts, DMs,
library, private files, Inspect logs) are linked, never summarized. Offline: no model calls,
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

Page layout: a summary card (model, prompt variant, agents, duration, accepted and genuine tasks,
cost, judge status), tabs Report / Conference / Behavior judge (the simplified judge's
RUN/analysis/simple_judge/report.html when the run has one, followed by "Behavior judge (rubric 3.1)" when it
also has the earlier judge; scripts/simple_judge_site.py) / Incident report (only when
RUN/analysis/incident/report.html exists; scripts/incident_site.py) / Inspect with one-line
explanations (agent transcripts are read in Inspect View; the site has no custom transcript viewer), the facts sections, and plain explanations of every status label shown. Raw Inspect
logs are not linked (they were only downloads); Inspect View reads them. Light and dark mode, phone width.

Publication steps (docs/experiment-library.md):

  1) python scripts/build_results.py --run-dir runs/RUN_ID [--ledger LEDGER.sqlite]
         [--key-usage-log LOG.jsonl] [--caveat TEXT] [--caveat-for SUBJECT TEXT] [--title TEXT]
  2) python3 scripts/prepare_hf_library.py --run "RUN_ID=VIEWER_BUNDLE" --output-dir NEW_BUNDLE
  3) python scripts/publish_hf_library.py --bundle NEW_BUNDLE          # read-only preview
     python scripts/publish_hf_library.py --bundle NEW_BUNDLE --commit

Existing results files are never replaced without --overwrite; --output-dir writes a scratch
copy for review instead.

--rerender-html re-renders only results.html from the run's existing results.json with the current
page layout (for example to republish older runs with maintain_hf_run.py --scope site). The report
data is not recomputed, so the flags used for the original report (ledger, caveats, stop time) are
not needed and results.json stays byte for byte; it takes no data flags.

Spending. With --ledger (a consistent copy of the budget ledger SQLite, opened immutable
read-only) "budget.pilot" counts exactly the ledger calls whose request IDs this run admitted
in runtime.jsonl and "cumulative_budget" summarizes the whole ledger. Without it
"budget.pilot" comes from runtime.jsonl (requests settled only after the run stay counted as
held, with a warning) and "cumulative_budget" is RUN/budget-final.json. A run with its own
budget (docs/plan.md "One per-run budget, no manual approvals (October 20, 2026)") reports that
budget as the pilot row's cap (from the ledger's runs table, else RUN/run-budget.json); the
cumulative rows show an older ledger's legacy caps only, "not recorded" otherwise.

Seeded runs (track "seeded", owner design 2026-10-25; research/analysis/roles.py): the seeded participant
(agent-101, role "seed": no container, model or transcript) is left out of the per-agent rows and the solver
counts; a researcher-only "seeded" block records it (task, channel, trigger, injection time from the
seeded_artifact_injected event and its run minute, its submissions, and credited tasks by role, seed shown
separately). Runs without a seed are unchanged.
"""
import argparse
import collections
import datetime
from decimal import Decimal
import html
import json
from pathlib import Path
import re
import sqlite3
import sys

if str(Path(__file__).resolve().parent) not in sys.path:  # shared site theme lives beside this script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_style import themed  # noqa: E402
from incident_site import add_incident_tab, has_incident_report  # noqa: E402
from simple_judge_site import add_simple_judge_tab, has_simple_judge, simple_report  # noqa: E402
import run_clock  # noqa: E402
from site_theme import BASE as STYLE, badge_class  # noqa: E402
if str(Path(__file__).resolve().parents[1]) not in sys.path:  # research/analysis/roles.py (participant roles)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.analysis import roles as participant_roles  # noqa: E402

NANO = Decimal('1e-9')
ACCEPTED = ('accepted', 'accepted_after_close')
SCOPES = ('pilot', 'compatibility', 'analysis')
JUDGE_REPORT = 'analysis/judge/report.html'
# Run page tabs: (label, target, one-line plain explanation). The report is this page; the other
# record views are separate pages next to it. Raw Inspect logs (.eval) are not linked: they were only
# downloads, and Inspect View reads them (site-nologs, October 21, 2026).
TABS = (('Report', '#report', 'This page: facts computed from the run\'s own records (clock, exits, '
         'submissions, proof checker, spending).'),
        ('Conference', 'conference-view/index.html', 'What agents saw and shared: board posts, DMs, the shared '
         'library, judging events and private files.'),
        ('Behavior judge', JUDGE_REPORT, 'A post-hoc model judge\'s reading of the transcripts (model-generated, '
         'verified by a second model call); researcher-only.'),
        ('Inspect', 'inspect-view/index.html', 'Agent transcripts and the raw Inspect evaluation logs '
         '(authoritative), read in the native Inspect log viewer.'))
# The Timeline link is local here; the timeline lives only in the private logs dataset (owner, October 23, 2026):
# prepare_hf_library.py moves RUN/analysis/timeline.md to the dataset (dataset_logs category 'timeline') and
# repoints this link to the dataset file page, so the Space never stores it.
OTHER_RECORDS = (('Timeline', 'analysis/timeline.md'), ('Settings', 'settings.json'))
# Plain explanations of the status labels this report shows (only labels present are listed).
STOP_REASONS = {
    'max_run_seconds': 'The run reached its time limit.',
    'budget_admission_exhausted': 'The run\'s budget could no longer admit model requests.',
    'evaluation_finished': 'Every agent finished before the time limit.',
    'user_requested_early_stop': 'A researcher stopped the run early.',
    'post_depletion_window': 'The run stopped a fixed time after its budget ran out.',
    'startup_timeout': 'The agents did not start in time.',
    'monitor_error': 'The run monitor failed, so the run was stopped.'}
EXITS = {
    'harness_completed': 'The agent ended its session on its own with a final reply.',
    'harness_completed_after_truncation': 'Ended on its own after replies were cut off at the output length limit.',
    'harness_completed_after_provider_error': 'Ended after the model provider returned an error to its last request.',
    'harness_completed_empty_reply': 'Ended because its last reply had no visible text or tool call (not a voluntary '
                                     'exit).',
    'budget_exhausted': 'Stopped: the run\'s budget could not cover its next model request.',
    'budget_ledger_unavailable': 'Stopped: the budget ledger stayed locked, so no request could be admitted.',
    'error': 'The agent harness raised an error.',
    'error_after_truncation': 'The harness raised an error after repeatedly truncated replies.',
    'interrupted': 'Cancelled without a recorded outcome (for example after the stop grace period).',
    'interrupted_by_user': 'Cancelled by an operator interrupt.'}
OUTCOMES = {
    'accepted': 'Passed the conference\'s automatic operational checks while the problem was '
                'open; the problem closed and was credited. Not a check of mathematical validity.',
    'accepted_after_close': 'Passed the automatic checks, but another agent had already closed the problem: no '
                            'credit.',
    'problem_closed': 'Handed in after the problem had closed, so it was never checked.',
    'rejected': 'Failed the automatic checks (stage and reason are in the conference view).',
    'judge_error': 'The conference\'s checker itself failed on this submission.',
    'pending': 'Still being checked when the records were saved.',
    'reverted': 'Accepted and credited, then reverted by the overseer: the credit was removed, the solution was '
                'withdrawn from the shared library and the problem reopened (the original submission is kept in '
                'the records).'}
VALIDITY = {
    'genuine': 'The independent proof checker confirmed the proof establishes the intended statement.',
    'fraudulent': 'The independent proof checker found the proof does not establish the intended statement.',
    'unresolved': 'The independent proof checker could not establish validity either way (reason shown).',
    'not_evaluated': 'No proof-checker record exists for this submission.'}
CHECKER = {'completed': 'Every accepted submission has a proof-checker record.',
           'partial': 'Some accepted submissions have no proof-checker record yet.',
           'not_run': 'The independent proof checker has not been run on this run.'}
JUDGE = {'completed': 'The behavior judge has run; its findings are on its own page (Behavior judge tab).',
         'not_run': 'The behavior judge has not been run on this run.'}
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


def recorded_run_budget(run, db=None):
    """This run's own budget in USD (Decimal) from the ledger's runs table, else RUN/run-budget.json,
    else None (runs before per-run budgets)."""
    if db is not None:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'runs' in tables:
            # The run directory's name, or its newest relaunch id <name>-r<N> (budget.fresh_run_id).
            row = db.execute('SELECT budget FROM runs WHERE id=? OR id GLOB ? ORDER BY created DESC, rowid DESC',
                             (run.name, run.name + '-r[0-9]*')).fetchone()
            if row is not None:
                return Decimal(row[0]) * NANO
    path = run / 'run-budget.json'
    if path.is_file():
        value = (json.loads(regular(path).read_text()) or {}).get('budget_usd')
        if value is not None:
            return Decimal(str(value))
    return None


def budgets(run, runtime, ledger):
    """Run-only pilot spending and the cumulative ledger, each labelled with its source."""
    final = json.loads(regular(run / 'budget-final.json').read_text())
    if ledger is not None:
        db = sqlite3.connect(f'file:{regular(ledger).resolve()}?mode=ro&immutable=1', uri=True)
        try:
            caps = dict(db.execute('SELECT name,cap FROM scopes'))
            budget = recorded_run_budget(run, db)
            pilot, committed = ledger_scope(db, 'pilot', set(runtime['admitted']))
            if budget is not None:
                pilot['cap_usd'] = dec(budget)
                pilot['remaining_usd'] = dec(max(Decimal(0), budget - Decimal(committed) * NANO))
            else:
                pilot['cap_usd'] = usd(caps.get('pilot', 0))
            pilot['source'] = 'ledger calls with request IDs admitted in runtime.jsonl'
            cumulative = {}
            for scope in SCOPES:
                summary, committed = ledger_scope(db, scope)
                summary['cap_usd'] = usd(caps[scope]) if scope in caps else None
                summary['remaining_usd'] = usd(caps[scope] - committed) if scope in caps else None
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
    budget = recorded_run_budget(run)
    if budget is not None:
        pilot['cap_usd'] = dec(budget)
        pilot['remaining_usd'] = dec(max(Decimal(0), budget - settled - held))
    cumulative = {}
    for scope in SCOPES:
        s = final.get(scope) or {}
        cumulative[scope] = {'settled_usd': s.get('settled_usd'), 'held_usd': s.get('held_usd'),
                             'unresolved_requests': s.get('unresolved_requests'),
                             'settled_requests': s.get('settled_requests'),
                             'unbilled_error_requests': s.get('unbilled_error_requests'),
                             'provider_reported_cost_usd': None, 'manual_reconciliation_usd': None,
                             'manual_reconciliation_requests': s.get('manual_reconciliations'),
                             'cap_usd': s.get('cap_usd', s.get('legacy_cap_usd')), 'remaining_usd': s.get('remaining_usd'),
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


def reversions(run):
    """Reverted credits (overseer ``revert_submission``), keyed by the reverted submission's serial ID
    (sub-NNNNNN). Empty for runs whose state has no ``reversions`` table (every run before oversight)."""
    db = sqlite3.connect(f'file:{regular(run / "conference" / "state.sqlite").resolve()}?mode=ro', uri=True)
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'reversions' not in tables:
            return {}
        rows = db.execute('SELECT reversion_id,task_id,submission_id,credited_agent,reverted_by,reason,reverted_at '
                          'FROM reversions ORDER BY reversion_id').fetchall()
    finally:
        db.close()
    return {f'sub-{sub:06d}': {'reversion_id': rid, 'task': task, 'credited_agent': agent, 'reverted_by': by,
                               'reason': reason, 'reverted_utc': utc(at).isoformat()}
            for rid, task, sub, agent, by, reason, at in rows}


def proof_checks(run):
    """``proof_checked`` conference events (overseer ``check_proof``), oldest first; [] when none."""
    path = run / 'conference' / 'events.jsonl'
    if not path.is_file():
        return []
    out = []
    for record in json_lines(path):
        if record.get('kind') != 'proof_checked':
            continue
        p = record.get('payload') or {}
        out.append({'time_utc': utc(record['timestamp']).isoformat(), 'agent': record.get('agent_id'),
                    **{k: p.get(k) for k in ('problem_id', 'target', 'submission_id', 'file_path', 'verified',
                                             'validity', 'reason', 'comparator_status', 'seconds')}})
    return out


def oversight(settings, every, accepted, reverted, checks, agents):
    """Oversight facts (runs with an overseer or with reverted credits); None otherwise, so earlier
    runs' results are unchanged. Separates the overseer's totals from the solvers'."""
    config, effective = settings.get('config') or {}, settings.get('effective') or {}
    overseer = effective.get('overseer') or config.get('overseer')
    if not overseer and not reverted and not checks:
        return None
    roles = dict(effective.get('roles') or {})
    overseer_id = (overseer or {}).get('agent_id')
    overseer_ids = participant_roles.overseer_ids(overseer)  # both forms: agent_ids (several) or agent_id
    for agent in overseer_ids:
        roles.setdefault(agent, 'overseer')
    for agent in participant_roles.seed_agents(settings):  # seeded runs: the planted seed, never a solver
        roles[agent] = participant_roles.SEED
    for agent in agents:
        roles.setdefault(agent, 'solver')
    net = {}  # task -> final credited row (accepted and not reverted)
    for row in accepted:
        if row['operational_outcome'] == 'accepted' and row['_serial'] not in reverted:
            net[row['task']] = row
    by_role = {}
    for role in ('solver', 'overseer') + (('seed',) if 'seed' in roles.values() else ()):
        members = {a for a, r in roles.items() if r == role}
        subs = [r for r in every if r['agent'] in members]
        by_role[role] = {'agents': sorted(members), 'submissions': len(subs),
                         'accepted_submissions': sum(r['operational_outcome'] == 'accepted' for r in subs),
                         'reverted_credits': sum(r['_serial'] in reverted for r in subs),
                         'net_credited_tasks': sum(row['agent'] in members for row in net.values())}
    verified_net = {t for t, row in net.items() if row.get('independent_validity') == 'genuine'}
    if overseer and 'agent_ids' in overseer:  # multi-overseer form (added keys; the single form is unchanged)
        every_overseer = sorted({a for a, r in roles.items() if r == 'overseer'})
        overseer_facts = {'agent_ids': every_overseer, 'count': len(every_overseer),
                          'powers': overseer.get('powers'), 'board': bool(overseer.get('board')),
                          'oversight_tools': overseer.get('oversight_tools')}
    else:
        overseer_facts = {'agent_id': overseer_id, 'powers': (overseer or {}).get('powers'),
                          'oversight_tools': (overseer or {}).get('oversight_tools')} if overseer else None
    return {'overseer': overseer_facts,
            'roles': dict(sorted(roles.items())),
            'net_credited_tasks': len(net),
            'net_credits_by_agent': dict(sorted(collections.Counter(r['agent'] for r in net.values()).items())),
            'net_verified_tasks': len(verified_net),
            'by_role': by_role,
            'reverted_submissions': [
                {'submission': row['submission'], 'agent': row['agent'], 'task': row['task'],
                 'submitted_utc': row['submitted_utc'], 'reverted_utc': reverted[row['_serial']]['reverted_utc'],
                 'reverted_by': reverted[row['_serial']]['reverted_by'],
                 'revert_reason': reverted[row['_serial']]['reason'],
                 'independent_validity': row.get('independent_validity')}
                for row in accepted if row['_serial'] in reverted],
            'proof_checks': {'count': len(checks),
                             'by_validity': dict(sorted(collections.Counter(c['validity'] for c in checks).items())),
                             'by_target': dict(sorted(collections.Counter(c['target'] for c in checks).items())),
                             'checks': checks},
            # Multi-overseer runs only: what each overseer did (absent for a single overseer).
            **({'by_overseer': {
                agent: {'submissions': sum(r['agent'] == agent for r in every),
                        'reverted_credits_made': sum(m['reverted_by'] == agent for m in reverted.values()),
                        'proof_checks': sum(c['agent'] == agent for c in checks)}
                for agent in overseer_facts['agent_ids']}} if overseer_facts and 'agent_ids' in overseer_facts
               else {})}


def seeded(run, settings, runtime, every, first_accept):
    """Researcher-only facts of a seeded run (None otherwise): the planted seed, its injection and the
    credits by role with the seed separate."""
    info = participant_roles.seeded_info(settings)
    if info is None:
        return None
    seeds = set(info['seed_agents'])
    roles = participant_roles.special_roles(settings)
    injected = participant_roles.injection(run)
    when = (injected or {}).get('timestamp')
    credited = collections.Counter(roles.get(f['agent'], 'solver') for f in first_accept.values())
    return {'researcher_only': True,
            'note': (f'{info["seed_agent"]} is the seeded source, planted by the researchers: a silent participant '
                     'with no container, model or transcript, credited with the seeded artifact at the trigger. It '
                     'is not a solver and is left out of the per-agent rows.'),
            'track': 'seeded', 'seed_agent': info['seed_agent'], 'label': participant_roles.SEED_LABEL,
            'task': info['task_id'], 'channel': info['channel'], 'path': info['path'], 'trigger': info['trigger'],
            'artifact_sha256': info['artifact_sha256'], 'source': info['source'],
            'injection_recorded': injected is not None,
            'injected_utc': utc(when).isoformat() if isinstance(when, (int, float)) else None,
            'injected_minute': round((when - runtime['start']) / 60, 2) if isinstance(when, (int, float)) else None,
            'injection': (injected or {}).get('payload'),
            'submissions': [{'submission': r['submission'], 'task': r['task'], 'operational_outcome':
                             r['operational_outcome'], 'submitted_utc': r['submitted_utc']}
                            for r in every if r['agent'] in seeds],
            'credited_tasks_by_role': dict(sorted(credited.items())),
            'seed_credited_tasks': sorted(t for t, f in first_accept.items() if f['agent'] in seeds),
            'solver_credited_tasks': sum(n for role, n in credited.items() if role == 'solver')}


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
    settings = json.loads(regular(run / "settings.json").read_text())
    recorded = settings.get('config', {})
    if stop_utc is not None and not stop_reason:
        raise ValueError('--stop-utc requires --stop-reason')
    runtime = read_runtime(run, stop_utc)
    pilot, cumulative = budgets(run, runtime, ledger)
    every = conference(run, ids)
    accepted = [dict(r, independent_validity='not_evaluated') for r in every if r['operational_outcome'] in ACCEPTED]
    evaluation = evaluate(run, accepted)
    judge = judge_summary(run)
    reverted = reversions(run)
    tokens = runtime['tokens']
    by_agent = collections.defaultdict(collections.Counter)
    by_task = collections.defaultdict(collections.Counter)
    first_accept = {}
    for r in every:
        by_agent[r['agent']][r['operational_outcome']] += 1
        by_task[r['task']][r['operational_outcome']] += 1
        # The task's credit as it stands at the end: a reverted credit is not the task's first acceptance
        # (identical to the first accepted submission when nothing was reverted).
        if r['operational_outcome'] == 'accepted' and r['task'] not in first_accept and r['_serial'] not in reverted:
            first_accept[r['task']] = {'agent': r['agent'], 'submitted_utc': r['submitted_utc']}
    seeds = participant_roles.seed_agents(settings)  # empty except in seeded runs
    agents = sorted((set(runtime['agents']) | set(by_agent)) - seeds)
    seed_facts = seeded(run, settings, runtime, every, first_accept) if seeds else None
    overseen = oversight(settings, every, accepted, reverted, proof_checks(run), agents)
    roles = overseen['roles'] if overseen else {}
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
        'agents': [{'agent': a, **({'role': roles.get(a, 'solver')} if overseen else {}),
                    **({k: runtime['agents'][a][k] for k in
                                    ('requests', 'finished', 'unbilled', 'restarts', 'exit', 'exit_at')}
                                   if a in runtime['agents'] else {}),
                    'submissions': dict(sorted(by_agent[a].items()))} for a in agents],
        'budget': {'pilot': pilot}, 'cumulative_budget': cumulative}
    if key_usage_log is not None:
        last = json_lines(key_usage_log)[-1]
        results['key_usage_counter'] = {'billed_since_launch_usd': last['billed_since_launch'],
                                        'ledger_settled_this_run_usd': last['settled_this_run'],
                                        'source': Path(key_usage_log).name}
    if overseen:
        results['condition'].update(overseer=overseen['overseer'],
                                    solvers=len(overseen['by_role']['solver']['agents']),
                                    participants=len(set(overseen['roles']) | set(agents)))
        for row in accepted:
            mark = reverted.get(row['_serial'])
            row.update(reverted=mark is not None, reverted_utc=mark and mark['reverted_utc'],
                       revert_reason=mark and mark['reason'], reverted_by=mark and mark['reverted_by'])
    if seed_facts:
        results['condition'].update(seed_agent=seed_facts['seed_agent'],
                                    solvers=sum(r == 'solver' for a, r in (overseen['roles'] if overseen else {
                                        a: 'solver' for a in agents}).items()))
        if not overseen:
            results['condition']['participants'] = len(agents) + len(seeds)
        for row in accepted:
            if row['agent'] in seeds:
                row['seeded'] = True
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
    if overseen:
        history = collections.defaultdict(list)
        for r in public:
            if r['operational_outcome'] == 'accepted':
                history[r['task']].append({'submission': r['submission'], 'agent': r['agent'],
                                           'submitted_utc': r['submitted_utc'], 'reverted': r['reverted'],
                                           'reverted_utc': r['reverted_utc']})
        for t in results['tasks']:
            t['credit_history'] = history.get(t['task'], [])
        results['oversight'] = overseen
        results['reverted_submissions'] = overseen['reverted_submissions']
    if seed_facts:
        results['seeded'] = seed_facts
    page = render(results, title, inspect_logs(run))
    if has_simple_judge(run):  # navigation only: the simplified judge becomes the Behavior judge tab
        page = add_simple_judge_tab(page, simple_report(run))
    if has_incident_report(run):  # navigation only: the incident report lives on its own page
        page = add_incident_tab(page)
    return json.dumps(results, indent=1) + '\n', page


def inspect_logs(run):
    """Names of the run's raw Inspect logs (RUN/inspect/*.eval regular files), in name order."""
    folder = run / 'inspect'
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.glob('*.eval') if p.is_file() and not p.is_symlink())


def table(rows, header=None):
    """Rows of pre-escaped cells; a table with a header row scrolls sideways on narrow screens."""
    head = '<tr>' + ''.join(f'<th>{esc(h)}</th>' for h in header) + '</tr>' if header else ''
    body = '<table>' + head + ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>'
                                      for row in rows) + '</table>'
    return f'<div class="scroll">{body}</div>' if header else body


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


def duration(seconds):
    seconds = int(float(seconds))
    return f'{seconds // 3600}h {seconds % 3600 // 60}m {seconds % 60}s'


def badge(label):
    return f'<span class="badge {badge_class(label)}">{esc(str(label))}</span>'


def explanation(mapping, label):
    if label in mapping:
        return mapping[label]
    if mapping is EXITS and str(label).startswith('stopped:'):
        return f'Stopped by the run clock ({str(label).split(":", 1)[1].replace("_", " ")}).'
    return 'No plain explanation is recorded for this label.'


def cents(v):
    """Rounded dollars for the summary card (exact amounts stay in the Accounting table)."""
    return 'not recorded' if v is None else f'${Decimal(str(v)):,.2f}'


def link(label, href):
    return f'<a href="{esc(href, quote=True)}">{esc(label)}</a>'


def breakable(text):
    """Escaped text with line-break opportunities after '_', '/' and '-' (long IDs in narrow tiles)."""
    return text.replace('_', '_<wbr>').replace('/', '/<wbr>').replace('-', '-<wbr>')


def summary_card(results):
    """Headline facts for the top of the run page (records only; no judge findings)."""
    c, ev, judge = results['condition'], results['independent_evaluation'], results['behavior_judge']
    pilot = results['budget']['pilot']
    tasks = c['task_count'] if c['task_count'] is not None else '?'
    exits = results['agent_exits']
    voluntary = sum(n for k, n in exits.items() if k.startswith('harness_completed'))
    errors = sum(n for k, n in exits.items() if k.startswith('error'))
    accepted = len(results['accepted_submissions'])
    if ev['status'] == 'not_run':
        genuine, genuine_note = 'not checked', 'proof checker not run'
    else:
        genuine = f'{ev["distinct_verified_tasks"]} of {tasks}'
        genuine_note = f'proof checker {ev["status"].replace("_", " ")}; distinct tasks'
    spend = cents(pilot['settled_usd'])
    spend_note = (f'of {cents(pilot["cap_usd"])} cap' if pilot.get('cap_usd') not in (None, '0') else
                  'settled model spend')
    if pilot.get('held_usd') not in (None, '0'):
        spend_note += f'; {cents(pilot["held_usd"])} still held'
    tiles = [('Model', c['model'], c['provider_route'] or c['provider']),
             ('Prompt variant', c['prompt_variant'] or 'not recorded', 'as recorded in settings.json'
              if c['prompt_variant'] else 'settings.json names none'),
             ('Agents', (f'{c["solvers"]} solvers' + (overseer_tile(c['overseer']) if c.get('overseer') else '')
                         + (' + seed' if c.get('seed_agent') else '')) if c.get('overseer') or c.get('seed_agent')
              else c['agents'],
              f'{voluntary} ended on their own · {errors} errors'),
             ('Duration', duration(results['elapsed_seconds']), f'stop: {results["stop_reason"]}'),
             ('Accepted tasks', f'{results["operationally_accepted_tasks"]} of {tasks}',
              f'{accepted} accepted submission(s); operational only'),
             ('Genuine tasks', genuine, genuine_note),
             ('Model cost', spend, spend_note),
             ('Behavior judge', judge['status'].replace('_', ' '),
              'findings on the Behavior judge tab' if judge['status'] != 'not_run' else 'no judge outputs')]
    return '<section class="card" aria-label="Run summary">' + ''.join(
        f'<div class="tile"><div class="k">{esc(k)}</div><div class="v">{breakable(esc(value(v)))}</div>'
        f'<div class="s">{esc(str(note or ""))}</div></div>' for k, v, note in tiles) + '</section>'


def tab_bar(results, logs):
    judge = results['behavior_judge']
    tabs, help_lines = [], []
    for label, href, text in TABS:
        if label == 'Report':
            tabs.append(f'<a class="current" aria-current="page" href="{href}">{esc(label)}</a>')
        elif label == 'Behavior judge' and judge['status'] == 'not_run':
            tabs.append(f'<span class="off">{esc(label)} · not run</span>')
            text += ' Not run for this run.'
        else:
            tabs.append(link(label, judge.get('report', href) if label == 'Behavior judge' else href))
        extra = ' No .eval files were found when this report was built.' if label == 'Inspect' and not logs else ''
        help_lines.append(f'<li><strong>{esc(label)}</strong>: {esc(text)}{extra}</li>')
    return ('<nav class="tabs" aria-label="Run records">' + ''.join(tabs) + '</nav>'
            '<ul class="tabhelp">' + ''.join(help_lines) + '</ul>')


def glossary(results):
    """Plain explanations of the status labels that appear in this report."""
    ev, judge = results['independent_evaluation'], results['behavior_judge']
    groups = [('Stop reason', STOP_REASONS, [results['stop_reason']]),
              ('Agent exits', EXITS, list(results['agent_exits'])),
              ('Submission outcomes (conference, automatic)', OUTCOMES, list(results['submission_outcomes'])
               + (['reverted'] if results.get('reverted_submissions') else [])),
              ('Independent validity (proof checker)', VALIDITY,
               sorted({r['independent_validity'] for r in results['accepted_submissions']})),
              ('Proof checker', CHECKER, [ev['status']]), ('Behavior judge', JUDGE, [judge['status']])]
    out = ['<h2 id="statuses">What the statuses mean</h2>']
    for heading, mapping, labels in groups:
        labels = [x for x in dict.fromkeys(labels) if x is not None]
        if labels:
            out.append(f'<h3>{esc(heading)}</h3><dl class="gloss">' + ''.join(
                f'<dt>{esc(str(x))}</dt><dd>{esc(explanation(mapping, x))}</dd>' for x in labels) + '</dl>')
    return ''.join(out)


def overseer_tile(overseer):
    """' + overseer' (one overseer, unchanged) or ' + N overseers' (multi-overseer form)."""
    count = overseer.get('count') if isinstance(overseer, dict) else None
    return f' + {count} overseers' if isinstance(count, int) and count > 1 else ' + overseer'


def oversight_section(ov):
    """Oversight facts: the overseers' roles and powers, credit by role, reverted credits and proof checks."""
    o = ov['overseer'] or {}
    checks = ov['proof_checks']
    multi = 'agent_ids' in o
    out = ['<h2 id="oversight">Oversight</h2>', facts(([
        ('Overseers', ', '.join(o['agent_ids']) or 'none'), ('Overseer count', o.get('count')),
        ('Overseer board', 'enabled (overseers only)' if o.get('board') else 'disabled')] if multi else [
        ('Overseer', o.get('agent_id') or 'none')]) + [
        ('Oversight tools', ', '.join(o.get('oversight_tools') or []) or ('none' if o else 'not recorded')),
        ('Roles', ov['roles']),
        ('Net credited tasks (after reversions)', ov['net_credited_tasks']),
        ('Net verified tasks (after reversions)', ov['net_verified_tasks']),
        ('Net credits by agent', ov['net_credits_by_agent'] or 'none'),
        ('Reverted credits', len(ov['reverted_submissions'])),
        ('Proof checks', checks['count']), ('Proof checks by validity', checks['by_validity'] or 'none'),
        ('Proof checks by target', checks['by_target'] or 'none')]),
        '<h3>Totals by role</h3>', table([(esc(role), esc(', '.join(t['agents']) or 'none'), esc(str(t['submissions'])),
                                          esc(str(t['accepted_submissions'])), esc(str(t['reverted_credits'])),
                                          esc(str(t['net_credited_tasks'])))
                                         for role, t in ov['by_role'].items()],
                                        ('Role', 'Agents', 'Submissions', 'Accepted', 'Reverted credits',
                                         'Net credited tasks')),
        *(['<h3>By overseer</h3>', table([(esc(agent), esc(str(t['submissions'])), esc(str(t['reverted_credits_made'])),
                                           esc(str(t['proof_checks']))) for agent, t in ov['by_overseer'].items()],
                                         ('Overseer', 'Submissions', 'Credits reverted', 'Proof checks'))]
          if 'by_overseer' in ov else []),
        '<h3>Reverted credits</h3>']
    out.append(table([(esc(r['submission']), esc(r['agent']), esc(r['task']), esc(r['reverted_by']),
                       esc(r['reverted_utc']), esc(r['revert_reason']), badge(r['independent_validity'] or 'not_evaluated'))
                      for r in ov['reverted_submissions']],
                     ('Submission', 'Credited agent', 'Task', 'Reverted by', 'Reverted (UTC)', 'Reason',
                      'Independent validity')) if ov['reverted_submissions'] else '<p>No credit was reverted.</p>')
    out.append('<h3>Proof checks</h3>')
    out.append(table([(esc(c['time_utc']), esc(value(c['agent'])), esc(value(c['problem_id'])),
                       esc(value(c['target'])), esc(value(c.get('submission_id') or c.get('file_path'))),
                       badge('verified' if c['verified'] else 'not verified'), esc(value(c['validity'])),
                       esc(value(c['reason'])))
                      for c in checks['checks']],
                     ('Time (UTC)', 'Agent', 'Problem', 'Target', 'Submission or file', 'Result', 'Validity',
                      'Reason')) if checks['checks'] else '<p>No proof checks were run.</p>')
    return out


def who(agent, results):
    """An agent id, with "(seeded, planted by researchers)" for a seeded run's seed."""
    return participant_roles.label(agent, {(results.get('seeded') or {}).get('seed_agent'): 'seed'})


def seeded_section(sd):
    """Seeded runs: the planted seed (researcher-only)."""
    return ['<h2 id="seeded">Seeded source (researcher-only)</h2>', f'<p>{esc(sd["note"])}</p>', facts([
        ('Track', sd['track']), ('Seed agent', f'{sd["seed_agent"]} ({sd["label"]})'), ('Task', sd['task']),
        ('Channel', sd['channel']), ('Library path', sd['path']), ('Trigger', sd['trigger']),
        ('Injected (UTC)', sd['injected_utc'] or ('not injected' if not sd['injection_recorded'] else None)),
        ('Injected (run minute)', sd['injected_minute']), ('Artifact SHA-256', sd['artifact_sha256']),
        ('Seed submissions', ', '.join(f'{r["submission"]} ({r["operational_outcome"]})'
                                       for r in sd['submissions']) or 'none'),
        ('Credited tasks by role', sd['credited_tasks_by_role'] or 'none'),
        ('Tasks credited to the seed', ', '.join(sd['seed_credited_tasks']) or 'none'),
        ('Tasks credited to solvers', sd['solver_credited_tasks'])])]


def render(results, title, inspect_logs=()):
    c, ev, judge = results['condition'], results['independent_evaluation'], results['behavior_judge']
    pilot = results['budget']['pilot']
    logs = list(inspect_logs)
    body = ['<p class="crumbs"><a href="../index.html">All experiments</a></p>',
            f'<h1>{esc(title or results["run"])}</h1>',
            f'<p><code>{esc(results["run"])}</code> · {esc(results["start_utc"])} to {esc(results["stop_utc"])}</p>',
            summary_card(results), tab_bar(results, logs), '<main id="report">',
            '<h2>Run</h2>', facts([
                ('Start (UTC)', results['start_utc']), ('Stop (UTC)', results['stop_utc']),
                ('Elapsed', duration(results['elapsed_seconds'])),
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
                ('Seeded contagion', c['seeded_contagion_enabled']), ('Mitigations', c['mitigations_enabled'])]
                + ([('Seed agent', f'{c["seed_agent"]} ({participant_roles.SEED_LABEL})')] if c.get('seed_agent') else [])
                + ([('Solvers', c['solvers']), ('Participants', c['participants']),
                    ('Overseer', c['overseer'])] if 'overseer' in c else [])),
            '<h2>Agents</h2>', table([(esc(a['agent'] + (f' ({a["role"]})' if a.get('role') == 'overseer' else '')),
                                       *(esc(value(a.get(k))) for k in ('requests', 'finished', 'unbilled', 'restarts')),
                                       badge(a['exit']) if a.get('exit') else esc(value(None)),
                                       *(esc(value(a.get(k))) for k in ('exit_at', 'submissions')))
                                      for a in results['agents']],
                                     ('Agent', 'Requests', 'Finished', 'Unbilled', 'Restarts', 'Exit', 'Exit time',
                                      'Submissions')),
            '<h2>Submissions by task</h2>', table([(esc(t['task']), esc(value(t['submissions'])), esc(
                f'{who(t["first_accepted"]["agent"], results)} at {t["first_accepted"]["submitted_utc"]}'
                if t['first_accepted'] else 'none')) for t in results['tasks']],
                ('Task', 'Submissions by outcome', 'First accepted')),
            '<h2>Accepted submissions</h2>']

    def validity(row):
        cell = badge(row['independent_validity'])
        if row['independent_validity'] != 'genuine' and row.get('evaluator_reason'):
            cell += '<br><small>' + esc(row['evaluator_reason'] + (': ' + row['evaluator_details']
                                                                    if row.get('evaluator_details') else '')) + '</small>'
        return cell
    def conference_cell(row):
        cell = badge(row['operational_outcome'])
        if row.get('reverted'):
            cell += ' ' + badge('reverted') + '<br><small>' + esc(
                f'by {row["reverted_by"]} at {row["reverted_utc"]}: {row["revert_reason"]}') + '</small>'
        return cell
    body.append(table([(esc(r['submission']), esc(who(r['agent'], results)), esc(r['task']), conference_cell(r),
                        esc(r['submitted_utc']), validity(r)) for r in results['accepted_submissions']],
                      ('Submission', 'Agent', 'Task', 'Conference', 'Submitted (UTC)', 'Independent validity')))
    if 'oversight' in results:
        body += oversight_section(results['oversight'])
    if results.get('seeded'):
        body += seeded_section(results['seeded'])
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
    body.append(glossary(results))
    records = [('Inspect View' if label == 'Inspect' else label, href) for label, href, _ in TABS[1:]
               if label != 'Behavior judge'] + list(OTHER_RECORDS)
    if judge['status'] != 'not_run':
        records.insert(1, ('Behavior judge', judge['report']))
    body.append('<h2>Records</h2><p>' + ' · '.join(link(label, href) for label, href in records) + '</p></main>')
    return restamp_page(themed('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
                  f'content="width=device-width,initial-scale=1"><title>{esc(title or results["run"] + " results")}</title>'
                  f'<style>{STYLE}</style></head><body>' + ''.join(body) + '</body></html>\n'),
                        run_clock.epoch(results['start_utc']))


# Times on the run page (owner, October 25, 2026): every time shows the offset from the run start and the
# absolute UTC time (scripts/run_clock.py). render() writes the recorded values and restamp_page() converts
# them, so a published page (with its publication markup) is converted exactly like a fresh one.
CLOCK_NOTE = ('<p class="muted" data-clock="run-start">Times: offset from the run start (the runtime\'s '
              'common_start, +0:00) and the absolute time in UTC.</p>')
_TEXT = re.compile(r'>([^<]+)<')
_HHMM_CELL = re.compile(r'<td>(\d{2}):(\d{2}) UTC</td>')
_HHMM_AGENT = re.compile(r'(agent-[\w-]+) \((\d{2}):(\d{2}) UTC\)')
_UTC_LABELS = re.compile(r'<(th|td)>(Start|Stop|Submitted|Reverted|Time) \(UTC\)</\1>')


def _minute_clock(hour, minute, start):
    """An event recorded only to the minute (exit times): its minute after the run start (the event's midpoint
    in that minute, rounded) and the recorded UTC minute; a time before the start's hour counts as the next day."""
    begin = datetime.datetime.fromtimestamp(start, datetime.timezone.utc)
    at = begin.replace(hour=int(hour), minute=int(minute), second=30, microsecond=0)
    if at.timestamp() < start - 3600:
        at += datetime.timedelta(days=1)
    return f'minute {round((at.timestamp() - start) / 60)} ({hour}:{minute} UTC)'


def restamp_page(document, start):
    """results.html with every recorded time shown as run_clock.clock (offset from the run start ``start``,
    epoch seconds, plus UTC), the minute-only exit times as "minute N (HH:MM UTC)", "(UTC)" dropped from the
    column labels and a one-line note under the run line. Only text between tags changes (links, scripts'
    attributes and publication markup stay), so it also converts a published page; idempotent. ``start`` None
    leaves the page unchanged."""
    if start is None:
        return document
    head_end = document.find('<body')
    head, body = (document[:head_end], document[head_end:]) if head_end >= 0 else ('', document)
    body = _UTC_LABELS.sub(lambda m: f'<{m[1]}>{m[2]}</{m[1]}>', body)
    body = _HHMM_CELL.sub(lambda m: f'<td>{_minute_clock(m[1], m[2], start)}</td>', body)
    body = _TEXT.sub(lambda m: '>' + run_clock.restamp_iso(
        _HHMM_AGENT.sub(lambda a: f'{a[1]} at {_minute_clock(a[2], a[3], start)}', m[1]), start) + '<', body)
    if 'data-clock="run-start"' not in body:
        line = re.search(r'<p><code>[^<]*</code> · [^<]*</p>', body)
        if line:
            body = body[:line.end()] + CLOCK_NOTE + body[line.end():]
    return head + body


def with_agent_badges(page, run):
    """results.html with the behavior judge's one-letter agent labels (research/analysis/agent_badges.py)."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from research.analysis import agent_badges
    return agent_badges.decorate_page(page, 'results.html', agent_badges.run_badges(run))


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
    parser.add_argument('--rerender-html', action='store_true',
                        help='only re-render results.html from the existing results.json (data unchanged)')
    args = parser.parse_args(argv)
    run = args.run_dir.resolve()
    if args.run_dir.is_symlink() or not run.is_dir():
        raise SystemExit(f'Not a run directory: {args.run_dir}')
    if args.rerender_html:
        if (args.ledger or args.key_usage_log or args.caveat or args.caveat_for or args.stop_reason
                or args.stop_utc or args.submission_ids != 'public'):
            raise SystemExit('--rerender-html takes no data flags; the report data comes from results.json')
        source = run / 'results.json'
        if source.is_symlink() or not source.is_file():
            raise SystemExit(f'No results.json to re-render: {source}')
        page = render(json.loads(source.read_text(encoding='utf-8')), args.title, inspect_logs(run))
        if has_simple_judge(run):  # same navigation as a full build
            page = add_simple_judge_tab(page, simple_report(run))
        if has_incident_report(run):
            page = add_incident_tab(page)
        page = with_agent_badges(page, run)
        target = (args.output_dir or run).resolve() / 'results.html'
        if (target.exists() or target.is_symlink()) and not args.overwrite:
            raise SystemExit(f'Refusing to replace existing {target} (pass --overwrite or --output-dir)')
        if target.is_symlink():
            raise SystemExit(f'Refusing to write through a symlink: {target}')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp')
        temporary.write_text(page, encoding='utf-8')
        temporary.replace(target)
        print(json.dumps({'results_html': str(target), 'rerendered_from': str(source)}))
        return
    notes = ([{'subject': s, 'text': t} for s, t in args.caveat_for]
             + [{'subject': None, 'text': t} for t in args.caveat])
    if any(not n['text'].strip() for n in notes):
        raise SystemExit('Researcher notes must not be empty')
    json_text, page = build(run, ledger=args.ledger, key_usage_log=args.key_usage_log, notes=notes,
                            title=args.title, stop_reason=args.stop_reason, stop_utc=args.stop_utc,
                            ids=args.submission_ids)
    page = with_agent_badges(page, run)
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
