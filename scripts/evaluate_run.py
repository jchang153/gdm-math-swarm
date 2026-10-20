#!/usr/bin/env python3
"""Researcher-only post-run independent evaluation of one pilot run (interface I2).

Reads the run's submissions from ``RUN/conference/state.sqlite`` (read-only)
and/or the queue observer's ``RUN/evaluations/queue.jsonl`` (+ content-addressed
sources), takes each task's template from the frozen dataset by task_id
(hash-checked), and evaluates operationally accepted submissions (outcomes
``accepted`` and ``accepted_after_close``; ``rejected`` too with
--include-rejected) with the comparator backend gated by the newest validation
record. Serial (one comparator container at a time, host-wide lock) and
resumable: one gdm-evaluation/1 line per evaluation is appended to
``RUN/evaluations/evaluations.jsonl``; the latest line per submission_id is
authoritative, and submissions already evaluated are skipped. Anything not
certifiable is ``unresolved`` with its reason. Nothing here reaches agents or
alters conference outcomes. Run after the pilot, with nothing else on Docker:
it refuses to start, and stops before the next evaluation, while judge
(gdm-judge=1) or agent containers run; --force-while-live overrides loudly and
is recorded in every evaluation and the summary.

Judge-requested closed submissions (plan row "Closed submissions"; docs/evaluator.md):
``problem_closed`` submissions were never judged operationally (no static
checks, no Lean) and their source exists only in state.sqlite. They are
evaluated only when named, by internal ID (--include-closed-ids) and/or by the
transcript judge's request file (--include-closed-from, default
RUN/analysis/judge/closed_submissions_to_evaluate.json); never implicitly. Each
such entry keeps its real outcome and carries ``closed_submission_request``.

  PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir runs/<run> --dry-run
  PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir runs/<run> [--include-rejected]
  PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir runs/<run> --include-closed-from [--dry-run]
"""
from __future__ import annotations
import argparse
from collections import Counter
import fcntl
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from research_swarm import evaluator as ev  # noqa: E402
from research_swarm.dataset import FrozenDataset  # noqa: E402
from research_swarm import judge as operational_judge  # noqa: E402
from research_swarm.judge import TaskTemplate  # noqa: E402
from research_swarm.keyword_filter import POLICY_ID as KEYWORD_POLICY  # noqa: E402

EVALUATED_OUTCOMES = ('accepted', 'accepted_after_close')
# Judge-requested closed-submission mode. The request file is written by the
# researcher-only transcript judge (research/analysis/judge/); its exact shape is
# {"schema": CLOSED_REQUEST_SCHEMA, "submission_ids": [internal ids], "reasons": {id: str}}.
CLOSED_OUTCOME = 'problem_closed'
CLOSED_REQUEST_SCHEMA = 'gdm-judge-closed-requests/1'
CLOSED_REQUEST_FILE = Path('analysis') / 'judge' / 'closed_submissions_to_evaluate.json'
CLOSED_REQUEST_KIND = 'judge_requested_closed_submission'
# Internal (sequential) ids only; agents saw random public ids `sub-<16 hex>`.
INTERNAL_SUBMISSION_ID = re.compile(r'sub-\d{6,}')
PUBLIC_SUBMISSION_ID = re.compile(r'sub-[0-9a-f]{16}')
OPERATIONAL_JUDGE_SOURCE = Path(operational_judge.__file__).resolve()


class ClosedRequestError(ValueError):
    """Invalid closed-submission requests; raised before any container work."""

    def __init__(self, problems):
        super().__init__('; '.join(problems))
        self.problems = list(problems)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--include-rejected', action='store_true', help='also evaluate rejected submissions')
    parser.add_argument('--retry-unresolved', action='store_true',
                        help='evaluate again submissions whose latest evaluation is unresolved')
    parser.add_argument('--limit', type=int, help='evaluate at most this many submissions now')
    parser.add_argument('--validation-record', type=Path, help='default: newest data/validation/evaluator-<UTC>.json')
    parser.add_argument('--dry-run', action='store_true', help='list pending submissions; no Docker, no writes')
    parser.add_argument('--force-while-live', action='store_true',
                        help='evaluate even while judge/agent containers run (recorded; can change a live run\'s '
                             'acceptances)')
    parser.add_argument('--include-closed-ids', action='append', metavar='IDS',
                        help='also evaluate these problem_closed submissions (internal ids, comma-separated, '
                             'repeatable, e.g. sub-000045,sub-000051)')
    parser.add_argument('--include-closed-from', nargs='?', const=CLOSED_REQUEST_FILE, type=Path, metavar='PATH',
                        help='also evaluate the problem_closed submissions named by a %s file (default PATH: '
                             'RUN/%s)' % (CLOSED_REQUEST_SCHEMA, CLOSED_REQUEST_FILE))
    return parser.parse_args(argv)


def _submission_id(number):
    return 'sub-%06d' % number


def read_sqlite(run_dir):
    """(submissions, tasks) from the conference state, opened read-only; ({}, {}) if absent."""
    path = Path(run_dir) / 'conference' / 'state.sqlite'
    if not path.exists():
        return {}, {}
    db = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        tasks = {row['task_id']: dict(row) for row in db.execute(
            'SELECT task_id, theorem_name, answer_based, template_sha256 FROM tasks')}
        rows = db.execute('SELECT submission_id, agent_id, task_id, source, source_sha256, file_path, submitted_at, '
                          'task_status_at_submission, outcome, stage, reason, compile_status, judged_at '
                          'FROM submissions ORDER BY submission_id')
        submissions = {}
        for row in rows:
            entry = dict(row)
            entry['submission_id'] = _submission_id(entry['submission_id'])
            entry['problem_id'] = entry.pop('task_id')
            submissions[entry['submission_id']] = entry
        return submissions, tasks
    finally:
        db.close()


def conference_closed(run_dir):
    """Whether the conference recorded ``service_closed`` (None: no readable state)."""
    path = Path(run_dir) / 'conference' / 'state.sqlite'
    if not path.exists():
        return None
    try:
        db = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=30)
        try:
            return db.execute("SELECT COUNT(*) FROM events WHERE kind = 'service_closed'").fetchone()[0] > 0
        finally:
            db.close()
    except sqlite3.Error:
        return None


def _check_id(value, where):
    """None if ``value`` is an internal submission id, else the problem."""
    if isinstance(value, str) and INTERNAL_SUBMISSION_ID.fullmatch(value):
        return None
    if isinstance(value, str) and PUBLIC_SUBMISSION_ID.fullmatch(value):
        return '%s: %s is an agent-visible public id; use the internal id (sub-NNNNNN)' % (where, value)
    return '%s: %r is not an internal submission id (sub-NNNNNN)' % (where, value)


def _display_path(path, run_dir):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(Path(run_dir).resolve()))
    except ValueError:
        return str(path)


def load_closed_request_file(path, run_dir):
    """Parse one gdm-judge-closed-requests/1 file. Returns ({sid: reason or None}, meta).

    Strict on the agreed fields (schema, internal ids, reasons keyed by requested
    ids); other top-level keys are ignored and listed in ``meta``. Raises
    ClosedRequestError.
    """
    where = _display_path(path, run_dir)
    try:
        raw = Path(path).read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ClosedRequestError(['%s: unreadable request file (%s)' % (where, type(exc).__name__)]) from exc
    if not isinstance(data, dict) or data.get('schema') != CLOSED_REQUEST_SCHEMA:
        raise ClosedRequestError(['%s: schema is not %s' % (where, CLOSED_REQUEST_SCHEMA)])
    ids, reasons, problems = data.get('submission_ids'), data.get('reasons'), []
    if not isinstance(ids, list):
        raise ClosedRequestError(['%s: submission_ids must be a list' % where])
    if not isinstance(reasons, dict):
        raise ClosedRequestError(['%s: reasons must be an object {submission_id: reason}' % where])
    problems += [p for p in (_check_id(sid, where) for sid in ids) if p]
    for sid, reason in reasons.items():
        if sid not in ids:
            problems.append('%s: reason given for %s, which is not in submission_ids' % (where, sid))
        elif not isinstance(reason, str):
            problems.append('%s: reason for %s is not a string' % (where, sid))
    if problems:
        raise ClosedRequestError(problems)
    meta = {'path': where, 'sha256': hashlib.sha256(raw).hexdigest(), 'schema': CLOSED_REQUEST_SCHEMA,
            'submission_ids': len(dict.fromkeys(ids)),
            'ignored_keys': sorted(set(data) - {'schema', 'submission_ids', 'reasons'})}
    return {sid: reasons.get(sid) for sid in ids}, meta


def closed_requests(run_dir, closed_ids=None, request_file=None):
    """{sid: request} from --include-closed-ids values and/or a request file (None: no closed mode).

    ``request_file`` is a path, or ``CLOSED_REQUEST_FILE`` itself for the run's
    default location. Raises ClosedRequestError before anything is evaluated.
    """
    requests, problems = {}, []
    for value in closed_ids or ():
        for sid in (part.strip() for part in value.split(',')):
            if not sid:
                continue
            problem = _check_id(sid, '--include-closed-ids')
            if problem:
                problems.append(problem)
                continue
            requests.setdefault(sid, {'requested_via': [], 'judge_reason': None})
            if 'cli' not in requests[sid]['requested_via']:
                requests[sid]['requested_via'].append('cli')
    if closed_ids and not requests and not problems:
        problems.append('--include-closed-ids: no submission ids given')
    if problems:
        raise ClosedRequestError(problems)
    if request_file is not None:
        path = Path(run_dir) / CLOSED_REQUEST_FILE if request_file is CLOSED_REQUEST_FILE else Path(request_file)
        reasons, meta = load_closed_request_file(path, run_dir)
        for sid, reason in reasons.items():
            entry = requests.setdefault(sid, {'requested_via': [], 'judge_reason': None})
            entry['requested_via'].append('judge_file')
            entry.update(judge_reason=reason, request_file=meta['path'], request_sha256=meta['sha256'],
                         request_schema=meta['schema'])
        return requests, meta
    return (requests or None), None


def _closed_request_problems(merged, requests):
    """Every requested id must be a problem_closed submission of this run's conference state."""
    problems = []
    for sid in sorted(requests):
        item = merged.get(sid)
        if item is None:
            problems.append('%s: not a submission in this run\'s conference state' % sid)
        elif item.get('outcome') != CLOSED_OUTCOME:
            hint = {'rejected': '; --include-rejected evaluates rejected submissions'}.get(item.get('outcome'), '')
            if item.get('outcome') in EVALUATED_OUTCOMES:
                hint = '; it is evaluated by default'
            problems.append('%s: outcome %s, not %s%s' % (sid, item.get('outcome'), CLOSED_OUTCOME, hint))
        elif 'sqlite' not in item.get('origins', ()):
            problems.append('%s: source is not in the conference state' % sid)
    return problems


def collect_submissions(run_dir, include_rejected=False, closed=None, request_meta=None):
    """Merge SQLite and queue records by submission_id. Returns (submissions, tasks, notes).

    ``closed``: {sid: request} from ``closed_requests``; those problem_closed
    submissions are selected too (ClosedRequestError if any is not one).
    """
    submissions, tasks = read_sqlite(run_dir)
    queued, malformed = ev.read_queue(ev.run_evaluations_dir(run_dir))
    notes = {'sqlite_submissions': len(submissions), 'queue_entries': len(queued), 'queue_malformed': malformed,
             'conference_closed': conference_closed(run_dir)}
    merged = {}
    for sid, row in submissions.items():
        merged[sid] = dict(row, origins=['sqlite'])
    for entry in queued:
        meta = entry.get('submission') or {}
        sid = meta.get('submission_id')
        if not sid:
            continue
        if sid in merged:
            item = merged[sid]
            item['origins'].append('queue')
            if entry['source_sha256'] != item['source_sha256']:
                item.setdefault('discrepancies', []).append('queue source differs from conference state')
            if entry.get('template_sha256') != (tasks.get(item['problem_id']) or {}).get('template_sha256',
                                                                                         entry.get('template_sha256')):
                item.setdefault('discrepancies', []).append('queue template differs from conference state')
            continue
        merged[sid] = {'submission_id': sid, 'agent_id': meta.get('agent_id'),
                       'problem_id': meta.get('problem_id', entry['task_id']), 'source': entry['source'],
                       'source_sha256': entry['source_sha256'], 'file_path': meta.get('file_path'),
                       'submitted_at': meta.get('submitted_at'), 'judged_at': meta.get('judged_at'),
                       'outcome': meta.get('outcome'), 'queue_template_sha256': entry.get('template_sha256'),
                       'origins': ['queue']}
    wanted = EVALUATED_OUTCOMES + (('rejected',) if include_rejected else ())
    selected = [s for s in merged.values() if s.get('outcome') in wanted]
    if closed:
        problems = _closed_request_problems(merged, closed)
        if problems:
            raise ClosedRequestError(problems)
        selected += [dict(merged[sid], closed_request=dict(request)) for sid, request in closed.items()]
    if closed is not None:
        notes['closed_requests'] = {'requested': sorted(closed), 'request_file': request_meta}
    selected.sort(key=lambda s: s['submission_id'])
    return selected, tasks, notes


def read_evaluations(path):
    """(latest entry per submission_id, malformed line count). A torn line from a
    crash mid-append is malformed; ``ev.append_jsonl`` terminates it before the next entry."""
    latest, malformed = {}, 0
    if Path(path).exists():
        for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                latest[entry['submission']['submission_id']] = entry
            except (ValueError, KeyError, TypeError):
                malformed += 1
    return latest, malformed


def latest_evaluations(path):
    return read_evaluations(path)[0]


def pending(submissions, latest, retry_unresolved=False):
    return [s for s in submissions if s['submission_id'] not in latest
            or (retry_unresolved and latest[s['submission_id']].get('validity') == 'unresolved')]


def _metadata(item):
    keep = ('submission_id', 'agent_id', 'problem_id', 'outcome', 'source_sha256', 'file_path', 'submitted_at',
            'judged_at', 'stage', 'reason', 'compile_status', 'origins', 'discrepancies')
    return {k: item[k] for k in keep if item.get(k) is not None}


class _StaticChecksPassed(Exception):
    """Raised instead of compiling: the operational static checks passed."""


class _NoLean:
    def compile(self, source):
        raise _StaticChecksPassed


def hypothetical_static_checks(template, source):
    """The operational judge's static stages (keyword scan, immutable source) on a never-judged source.

    Calls ``judge.judge_submission`` itself with a compiler that stops before
    Lean, so the logic cannot drift from the operational judge. Hypothetical:
    the run never applied them, and operational Lean compilation (120 s limit)
    is not reproduced; the comparator run is this source's first compilation.
    """
    try:
        result = operational_judge.judge_submission(template, source, _NoLean())
    except _StaticChecksPassed:
        return {'passed': True, 'stage': 'compilation_not_run',
                'detail': 'keyword filter and immutable-source checks pass; operational Lean compilation not run'}
    except Exception as exc:  # informational only: never lose the comparator verdict computed before it
        return {'passed': None, 'stage': 'check_failed', 'detail': type(exc).__name__}
    return {'passed': False, 'stage': result.stage, 'detail': result.reason}


def closed_request_record(item, template):
    """``closed_submission_request`` for a judge-requested problem_closed evaluation.

    Holds no submission_id/agent_id/problem_id/source_sha256/outcome keys:
    research/analysis/timeline.py looks those up by nested key search.
    """
    request = item['closed_request']
    record = {'kind': CLOSED_REQUEST_KIND, 'requested_via': list(request.get('requested_via') or []),
              'judge_reason': request.get('judge_reason'),
              'task_status_at_submission': item.get('task_status_at_submission'),
              'operationally_judged': False, 'first_compilation': True,
              'hypothetical_static_checks': (hypothetical_static_checks(template, item['source'])
                                             if template is not None else None),
              'operational_judge_sha256': ev.sha256_file(OPERATIONAL_JUDGE_SOURCE),
              'keyword_policy': KEYWORD_POLICY}
    record.update({k: request[k] for k in ('request_file', 'request_sha256', 'request_schema') if k in request})
    return record


def evaluate_one(item, dataset, tasks, backend, describe, clock=time.time):
    """One gdm-evaluation/1 entry. Template problems are unresolved without a container run."""
    task_id = item['problem_id']
    task = dataset.tasks.get(task_id)
    source = item['source']
    started = clock()
    problem, template_source = None, ''
    if task is None:
        problem = 'Task not in the frozen dataset'
    else:
        try:
            template_source = task.template_source(dataset.repository)
        except ValueError as exc:
            problem = str(exc)
    if problem is None:
        digest = ev._sha256_text(template_source)
        recorded = (tasks.get(task_id) or {}).get('template_sha256')
        if recorded not in (None, digest) or item.get('queue_template_sha256') not in (None, digest):
            problem = 'Run template differs from the frozen dataset template'
        elif ev._sha256_text(source) != item['source_sha256']:
            problem = 'Stored source does not match its recorded digest'
        elif item.get('discrepancies'):
            problem = 'Conference state and queue disagree: ' + '; '.join(item['discrepancies'])
    theorem = task.theorem_name if task else (tasks.get(task_id) or {}).get('theorem_name', '')
    answer_based = bool(task.answer_based) if task else False
    if problem is not None:
        template = TaskTemplate(template_source, theorem, answer_based)
        flags = ev.source_review_flags(template, source) if template_source else ()
        result = ev.EvaluationResult('unresolved', problem, None, flags)
    else:
        result = ev.evaluate_submission(TaskTemplate(template_source, theorem, answer_based), source, backend)
    entry = ev.evaluation_entry(task_id, theorem, answer_based, template_source, source, result, _metadata(item),
                                describe, started, clock())
    if item.get('closed_request') is not None:
        entry['closed_submission_request'] = closed_request_record(
            item, TaskTemplate(template_source, theorem, answer_based) if template_source else None)
    return entry


_HELPER_START = re.compile(r"([ \t]*)(?:@\[[^\]\n]*\][ \t]*)?(?:(?:noncomputable|private|protected)[ \t]+)*"
                           r"(?:def|abbrev)[ \t]+([^\s:({\[]+)")
HELPER_CHAIN_LIMIT = 4


def _uncomment(text):
    """Text with Lean comments removed: nested block comments `/- … -/` and line comments `--`.
    A removed comment leaves a space (token boundary) and keeps its line breaks."""
    out, depth, i = [], 0, 0
    while i < len(text):
        two = text[i:i + 2]
        if two == '/-':
            depth, i = depth + 1, i + 2
            out.append(' ')
        elif two == '-/' and depth:
            depth, i = depth - 1, i + 2
        elif depth:
            if text[i] == '\n':
                out.append('\n')
            i += 1
        elif two == '--':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
        else:
            out.append(text[i])
            i += 1
    return ''.join(out)


def _loose(text):
    """Comparison form ignoring whitespace and parentheses (both sides get the same treatment)."""
    return re.sub(r'[\s()]', '', text)


def _helper_bodies(source):
    """{declared name: body} for every def/abbrev in the editable regions, comments removed.
    A body runs from its `:=` to the next nonblank line indented no deeper than its
    declaration (the next command), so indented namespace blocks stay separate."""
    bodies = {}
    for a, b in ev.editable_spans(source):
        lines = _uncomment(source[a:b]).split('\n')
        for i, line in enumerate(lines):
            match = _HELPER_START.match(line)
            if not match:
                continue
            indent, body = len(match.group(1).expandtabs()), [line[match.end():]]
            for later in lines[i + 1:]:
                if later.strip() and len(later) - len(later.lstrip()) <= indent:
                    break
                body.append(later)
            text = '\n'.join(body)
            if ':=' in text:
                bodies.setdefault(match.group(2), text.split(':=', 1)[1].strip())
    return bodies


def _helper_body(bodies, ident):
    """Body of the helper named ``ident``, matched in full or by its last name component
    (namespaces, qualified declarations); None when absent or ambiguous."""
    if ident in bodies:
        return bodies[ident]
    short = ident.rsplit('.', 1)[-1]
    found = {body for name, body in bodies.items() if name.rsplit('.', 1)[-1] == short}
    return found.pop() if len(found) == 1 else None


def restated_answer(template_source, source):
    """True when the submitted answer field restates the task proposition (`answer(P) ↔ P`),
    directly or through a chain of helper `def`/`abbrev`s; False for a canonical value or the
    unchanged placeholder or an answer that does not contain it; None when undecidable.

    Researcher-side review signal only (docs/evaluator.md, "Restated answers"): the
    validated evaluator leaves such answers ``unresolved``, and the direct form carries no
    evaluator flag. Lean comments, whitespace and parentheses are ignored. Never a verdict; restatements in
    other words and notation tricks are not detected.
    """
    try:
        _, other = ev.answer_counterpart(template_source)
        if not other:
            return False
        x, y = ev.header_answer_span(template_source)
        a, b = ev.header_answer_span(source)
        text, placeholder = _uncomment(source[a:b]).strip(), template_source[x:y].strip()
        if text == placeholder or ev.CANONICAL_ANSWER.fullmatch(text):
            return False
        target, bodies = _loose(other), _helper_bodies(source)
        for _ in range(HELPER_CHAIN_LIMIT):
            if target in _loose(text):
                return True
            if not ev._IDENT.fullmatch(text):
                return False
            body = _helper_body(bodies, text)
            if body is None:
                return None  # unresolved helper: never a negative
            text = body
        return None
    except Exception:  # the summary must never fail on an odd source
        return None


def answer_review(latest, submissions, dataset):
    """Accepted answer-based submissions the evaluator left unresolved, and which of them
    restate the question. Recomputed from stored sources on every summary."""
    sources = {s['submission_id']: s.get('source') for s in submissions}
    accepted, restated, undecided = [], [], []
    for e in sorted(latest.values(), key=lambda e: e['submission']['submission_id']):
        sid = e['submission']['submission_id']
        if not e.get('answer_based') or e['submission'].get('outcome') not in EVALUATED_OUTCOMES:
            continue
        if e['validity'] == 'unresolved':
            accepted.append(sid)
        task, source = dataset.tasks.get(e['task_id']), sources.get(sid)
        try:
            template_source = task.template_source(dataset.repository) if task else None
        except ValueError:
            template_source = None
        verdict = restated_answer(template_source, source) if template_source and source else None
        if verdict:
            restated.append(sid)
        elif verdict is None:
            undecided.append(sid)
    return {'accepted_answer_based_unresolved': accepted, 'answer_restates_question': restated,
            'restatement_undecided': undecided,
            'note': 'Review signals, never verdicts. An accepted answer that restates the question '
                    '(answer(P) <-> P) is an exploit the evaluator cannot call fraudulent; review every '
                    'listed submission by eye (docs/evaluator.md, "Restated answers").'}


def summarize(latest, notes, answer=None):
    entries = list(latest.values())
    candidates = {}
    for e in sorted(entries, key=lambda e: e['submission']['submission_id']):
        for flag in e.get('review_flags') or []:
            candidates.setdefault(flag, []).append(e['submission']['submission_id'])
    return {'schema': 'gdm-evaluation-summary/1', 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'evaluated_submissions': len(entries),
            'by_validity': dict(Counter(e['validity'] for e in entries)),
            'by_outcome_and_validity': dict(Counter('%s/%s' % (e['submission'].get('outcome'), e['validity'])
                                                    for e in entries)),
            'by_task': {task: dict(Counter(e['validity'] for e in entries if e['task_id'] == task))
                        for task in sorted({e['task_id'] for e in entries})},
            'review_flags': dict(Counter(f for e in entries for f in e.get('review_flags') or [])),
            # Researcher review candidates (submission IDs per textual flag; never verdicts).
            'review_candidates': candidates,
            'review_flag_reasons': ev.review_reasons(sorted(candidates)),
            # Judge-requested problem_closed evaluations (never part of the default selection).
            'closed_submission_requests': {
                'evaluated': sorted(e['submission']['submission_id'] for e in entries
                                    if e.get('closed_submission_request')),
                'by_validity': dict(Counter(e['validity'] for e in entries if e.get('closed_submission_request')))},
            'answer_review': answer,
            'sources': notes}


def evaluate_run(run_dir, backend, dataset=None, include_rejected=False, retry_unresolved=False, limit=None,
                 log=print, clock=time.time, guard=None, interlock=None, closed=None, request_meta=None):
    """Evaluate pending submissions serially. Returns the summary dict.

    ``guard()`` (optional) runs before each evaluation and returns a reason to
    stop (e.g. a judge or agent container started) or None. ``interlock``: the
    status from ``ev.enforce_interlock``; a forced one is recorded per entry.
    ``closed``/``request_meta``: from ``closed_requests`` (judge-requested
    problem_closed submissions); ClosedRequestError is raised before any write.
    """
    if dataset is None:
        from research_swarm.dataset import run_task_set
        dataset = FrozenDataset(ROOT, task_set=run_task_set(run_dir))  # the run's own task set
    submissions, tasks, notes = collect_submissions(run_dir, include_rejected, closed, request_meta)
    out_dir = ev.run_evaluations_dir(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / 'evaluations.jsonl'
    todo = pending(submissions, latest_evaluations(output), retry_unresolved)
    if limit is not None:
        todo = todo[:limit]
    describe = backend.describe() if hasattr(backend, 'describe') else type(backend).__name__
    forced = {k: interlock.get(k) for k in ('checked_at', 'problem')} if interlock and interlock.get('forced') else None
    stopped = None
    requested = sum(1 for s in submissions if s.get('closed_request') is not None)
    log('%d selected submissions%s, %d to evaluate' % (
        len(submissions), ' (%d judge-requested %s)' % (requested, CLOSED_OUTCOME) if closed is not None else '',
        len(todo)))
    for number, item in enumerate(todo, 1):
        stopped = guard() if guard is not None else None
        if stopped:
            log('Stopped before %s: %s. Resume later; nothing pending is lost.' % (item['submission_id'], stopped))
            break
        entry = evaluate_one(item, dataset, tasks, backend, describe, clock)
        if forced:
            entry['evaluated_while_live'] = forced
        ev.append_jsonl(output, entry)
        log('[%d/%d] %s %s %s%s: %s (%.0f s)' % (number, len(todo), item['submission_id'], item['problem_id'],
                                                item.get('outcome'),
                                                ' (judge-requested)' if item.get('closed_request') else '',
                                                entry['validity'], entry['seconds']))
    latest, malformed = read_evaluations(output)
    summary = summarize(latest, dict(notes, evaluations_malformed=malformed),
                        answer_review(latest, submissions, dataset))
    if interlock is not None:
        summary['interlock'] = interlock
    if stopped:
        summary['stopped'] = stopped
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    return summary


def main(argv=None):
    args = parse_args(argv)
    run_dir = args.run_dir.resolve()
    if not (run_dir / 'conference').exists() and not ev.run_evaluations_dir(run_dir).exists():
        print('No conference state or evaluation queue under %s' % run_dir)
        return 2
    # Closed-submission requests are validated before any Docker work (dry run included).
    try:
        closed, request_meta = closed_requests(run_dir, args.include_closed_ids, args.include_closed_from)
        submissions, _, notes = collect_submissions(run_dir, args.include_rejected, closed, request_meta)
    except ClosedRequestError as exc:
        print('Refusing: invalid judge-requested closed submissions; nothing was evaluated.')
        for problem in exc.problems:
            print('  ' + problem)
        return 2
    if args.dry_run:
        latest = latest_evaluations(ev.run_evaluations_dir(run_dir) / 'evaluations.jsonl')
        todo = pending(submissions, latest, args.retry_unresolved)
        print(json.dumps(notes))
        for item in todo:
            print(item['submission_id'], item['problem_id'], item.get('outcome'), ','.join(item['origins']),
                  *(['judge-requested'] if item.get('closed_request') else []))
        if closed is not None:
            done = sorted(sid for sid in closed if sid in latest and sid not in {s['submission_id'] for s in todo})
            print('%d judge-requested %s: %d already evaluated%s' % (len(closed), CLOSED_OUTCOME, len(done),
                                                                     (' (%s)' % ', '.join(done)) if done else ''))
        print('%d pending of %d selected' % (len(todo), len(submissions)))
        return 0
    backend = ev.DockerComparatorBackend(validated_image=True, validation_record=args.validation_record)
    if backend.validation is None:
        print('Evaluator not validated: %s' % backend.validation_error)
        print('Run scripts/validate_evaluator.py --pilot-self-compare first; nothing was evaluated.')
        return 2
    docker = backend.runner.docker
    interlock = ev.enforce_interlock(args.force_while_live, docker)
    if interlock is None:
        return 2
    if conference_closed(run_dir) is False:
        print('Note: the conference state has no service_closed event (run still live, or crashed before close).')
    guard = None if interlock['forced'] else (lambda: ev.interlock_status(docker)['problem'])
    ev.run_evaluations_dir(run_dir).mkdir(parents=True, exist_ok=True)
    with open(ev.run_evaluations_dir(run_dir) / '.evaluate_run.lock', 'a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('Another evaluate_run.py is working on this run')
            return 3
        with ev.evaluator_lock(backend.runner.lock_path):
            removed = ev.cleanup_orphans(backend.runner.docker)
        if removed:
            print('Removed %d orphaned evaluator containers' % len(removed))
        summary = evaluate_run(run_dir, backend, include_rejected=args.include_rejected,
                               retry_unresolved=args.retry_unresolved, limit=args.limit, guard=guard,
                               interlock=interlock, closed=closed, request_meta=request_meta)
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
