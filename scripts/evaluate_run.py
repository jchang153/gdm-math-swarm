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

  PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir runs/<run> --dry-run
  PYTHONPATH=src .venv/bin/python scripts/evaluate_run.py --run-dir runs/<run> [--include-rejected]
"""
from __future__ import annotations
import argparse
from collections import Counter
import fcntl
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from research_swarm import evaluator as ev  # noqa: E402
from research_swarm.dataset import FrozenDataset  # noqa: E402
from research_swarm.judge import TaskTemplate  # noqa: E402

EVALUATED_OUTCOMES = ('accepted', 'accepted_after_close')


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


def collect_submissions(run_dir, include_rejected=False):
    """Merge SQLite and queue records by submission_id. Returns (submissions, tasks, notes)."""
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
    return ev.evaluation_entry(task_id, theorem, answer_based, template_source, source, result, _metadata(item),
                               describe, started, clock())


def summarize(latest, notes):
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
            'sources': notes}


def evaluate_run(run_dir, backend, dataset=None, include_rejected=False, retry_unresolved=False, limit=None,
                 log=print, clock=time.time, guard=None, interlock=None):
    """Evaluate pending submissions serially. Returns the summary dict.

    ``guard()`` (optional) runs before each evaluation and returns a reason to
    stop (e.g. a judge or agent container started) or None. ``interlock``: the
    status from ``ev.enforce_interlock``; a forced one is recorded per entry.
    """
    dataset = dataset or FrozenDataset(ROOT)
    out_dir = ev.run_evaluations_dir(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / 'evaluations.jsonl'
    submissions, tasks, notes = collect_submissions(run_dir, include_rejected)
    todo = pending(submissions, latest_evaluations(output), retry_unresolved)
    if limit is not None:
        todo = todo[:limit]
    describe = backend.describe() if hasattr(backend, 'describe') else type(backend).__name__
    forced = {k: interlock.get(k) for k in ('checked_at', 'problem')} if interlock and interlock.get('forced') else None
    stopped = None
    log('%d selected submissions, %d to evaluate' % (len(submissions), len(todo)))
    for number, item in enumerate(todo, 1):
        stopped = guard() if guard is not None else None
        if stopped:
            log('Stopped before %s: %s. Resume later; nothing pending is lost.' % (item['submission_id'], stopped))
            break
        entry = evaluate_one(item, dataset, tasks, backend, describe, clock)
        if forced:
            entry['evaluated_while_live'] = forced
        ev.append_jsonl(output, entry)
        log('[%d/%d] %s %s %s: %s (%.0f s)' % (number, len(todo), item['submission_id'], item['problem_id'],
                                              item.get('outcome'), entry['validity'], entry['seconds']))
    latest, malformed = read_evaluations(output)
    summary = summarize(latest, dict(notes, evaluations_malformed=malformed))
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
    if args.dry_run:
        submissions, _, notes = collect_submissions(run_dir, args.include_rejected)
        todo = pending(submissions, latest_evaluations(ev.run_evaluations_dir(run_dir) / 'evaluations.jsonl'),
                       args.retry_unresolved)
        print(json.dumps(notes))
        for item in todo:
            print(item['submission_id'], item['problem_id'], item.get('outcome'), ','.join(item['origins']))
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
                               interlock=interlock)
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
