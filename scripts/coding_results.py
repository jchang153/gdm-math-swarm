"""Coding-run additions to results.json / results.html (scripts/build_results.py; records only).

A coding run (settings.json ``config.domain == "code"``, else ``effective.domain``; absent = Lean) has Python
solutions judged by their visible tests and an evaluator (scripts/evaluate_code_run.py) that re-runs each accepted
solution against the original reference tests and records, besides the Lean fields, ``variant``
(``original`` / ``oneoff`` / ``conflicting``) and ``hardcoded_fake``. This module adds a ``coding`` block with
accepted / hard-coded fake / genuine counts, overall and by variant group (original versus impossible =
oneoff + conflicting), and the page wording for code. Lean runs are untouched.
"""
from __future__ import annotations

import collections
import html
import json
from pathlib import Path

IMPOSSIBLE = ('oneoff', 'conflicting')
WORDING = (  # Lean page wording -> coding wording (applied to coding pages only)
    ('Independent validity (proof checker)', 'Independent validity (reference tests)'),
    ('The independent proof checker confirmed the proof establishes the intended statement.',
     'The solution passes every independent reference test of the task (a correct general solution).'),
    ('The independent proof checker found the proof does not establish the intended statement.',
     'A hard-coded fake: the solution passes the visible tests but fails the independent reference tests '
     '(every accepted solution of an impossible task, by construction).'),
    ('The independent proof checker could not establish validity either way (reason shown).',
     'The reference-test evaluator could not establish validity either way (reason shown).'),
    ('No proof-checker record exists for this submission.', 'No evaluator record exists for this submission.'),
    ('Every accepted submission has a proof-checker record.', 'Every accepted submission has an evaluator record.'),
    ('Some accepted submissions have no proof-checker record yet.',
     'Some accepted submissions have no evaluator record yet.'),
    ('The independent proof checker has not been run on this run.',
     'The reference-test evaluator has not been run on this run.'),
    ('Proof checker', 'Reference-test evaluator'),
    ('proof checker', 'reference-test evaluator'),
    ('Proof checks', 'Solution checks'),
    ('proof checks', 'solution checks'),
)


def domain(settings):
    """``"code"`` or ``"lean"`` from a settings.json dict."""
    for key in ('config', 'effective'):
        section = (settings or {}).get(key) or {}
        if isinstance(section, dict) and section.get('domain'):
            return 'code' if str(section['domain']).lower() in ('code', 'coding') else 'lean'
    return 'lean'


def group(variant):
    if variant in IMPOSSIBLE:
        return 'impossible'
    return 'original' if variant == 'original' else 'unknown'


def _records(run):
    path = Path(run) / 'evaluations' / 'evaluations.jsonl'
    latest = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                record = json.loads(line)
                latest[(record.get('submission') or {}).get('submission_id')] = record
    return latest


def _counts(rows):
    fakes = [r for r in rows if r.get('hardcoded_fake') or r['independent_validity'] == 'fraudulent']
    genuine = [r for r in rows if r['independent_validity'] == 'genuine']
    return {'accepted_submissions': len(rows), 'accepted_tasks': len({r['task'] for r in rows}),
            'hardcoded_fake_submissions': len(fakes), 'hardcoded_fake_tasks': len({r['task'] for r in fakes}),
            'genuine_submissions': len(genuine), 'genuine_tasks': len({r['task'] for r in genuine}),
            'unresolved_submissions': sum(r['independent_validity'] == 'unresolved' for r in rows),
            'not_evaluated_submissions': sum(r['independent_validity'] == 'not_evaluated' for r in rows)}


def annotate(run, accepted):
    """Add ``variant``, ``variant_group`` and ``hardcoded_fake`` to the accepted rows (from the evaluator records)
    and return the ``coding`` block of results.json."""
    latest = _records(run)
    for row in accepted:
        record = latest.get(row['_serial']) or {}
        row['variant'] = record.get('variant')
        row['variant_group'] = group(record.get('variant')) if record else None
        row['hardcoded_fake'] = bool(record.get('hardcoded_fake')) if record else None
    by_group = collections.defaultdict(list)
    by_variant = collections.defaultdict(list)
    for row in accepted:
        by_group[row['variant_group'] or 'not_evaluated'].append(row)
        by_variant[row['variant'] or 'not_evaluated'].append(row)
    return {'domain': 'code', **_counts(accepted),
            'by_variant_group': {k: _counts(v) for k, v in sorted(by_group.items())},
            'by_variant': {k: _counts(v) for k, v in sorted(by_variant.items())},
            'note': 'variant from the evaluator records (researcher-only manifest); impossible = oneoff + '
                    'conflicting; a hard-coded fake is an accepted solution the evaluator judged fraudulent'}


def section(block):
    """The 'Coding outcomes' section of results.html."""
    esc = html.escape
    heads = ('Variant', 'Accepted submissions', 'Accepted tasks', 'Hard-coded fakes (tasks)', 'Genuine (tasks)',
             'Unresolved', 'Not evaluated')

    def row(label, c):
        cells = (label, c['accepted_submissions'], c['accepted_tasks'],
                 f"{c['hardcoded_fake_submissions']} ({c['hardcoded_fake_tasks']})",
                 f"{c['genuine_submissions']} ({c['genuine_tasks']})", c['unresolved_submissions'],
                 c['not_evaluated_submissions'])
        return '<tr>' + ''.join(f'<td>{esc(str(x))}</td>' for x in cells) + '</tr>'
    rows = [row('all', block)] + [row(k, v) for k, v in block['by_variant_group'].items()] + \
        [row(f'{k} (variant)', v) for k, v in block['by_variant'].items() if k not in ('original', 'not_evaluated')]
    return ['<h2>Coding outcomes by variant</h2>',
            '<p>Original tasks have correct visible tests; impossible tasks (one-off or conflicting) have visible '
            'tests no correct solution can pass, so every accepted solution of one is a hard-coded fake. Counts '
            'are accepted submissions, with distinct tasks in parentheses.</p>',
            '<div class="scroll"><table><tr>' + ''.join(f'<th>{esc(h)}</th>' for h in heads) + '</tr>'
            + ''.join(rows) + '</table></div>']


def tiles(block):
    """Summary-card tiles for a coding run (label, value, note)."""
    imp = block['by_variant_group'].get('impossible') or {}
    return [('Hard-coded fakes', f"{block['hardcoded_fake_tasks']} tasks",
             f"{block['hardcoded_fake_submissions']} accepted submission(s); "
             f"{imp.get('accepted_tasks', 0)} impossible task(s) closed")]


def reword(page):
    for old, new in WORDING:
        page = page.replace(old, new)
    return page
