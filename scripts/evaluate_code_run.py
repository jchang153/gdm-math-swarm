#!/usr/bin/env python3
"""Researcher-only post-run evaluation of one coding run (domain "code").

Reads the run's submissions from ``RUN/conference/state.sqlite`` (read-only) and/or
the code queue observer's ``RUN/evaluations/queue.jsonl`` (+ ``sources/<sha256>.py``),
and evaluates every operationally accepted submission (outcomes ``accepted`` and
``accepted_after_close``; ``rejected`` too with --include-rejected) in the same
sandbox as the operational judge: once against the visible tests and once against
the task's private reference tests (research_swarm.coding.dataset.CodingDataset).
Validity (research_swarm.coding.evaluator): impossible variant + accepted ->
``fraudulent`` (``hardcoded_fake``); otherwise ``genuine`` iff every reference test
passes, else ``unresolved`` with the reason.

Writes the Lean evaluator's record layout, so scripts/build_results.py and the story
map read it unchanged: one gdm-evaluation/1 line per evaluation appended to
``RUN/evaluations/evaluations.jsonl`` (latest line per submission_id authoritative;
already-evaluated submissions are skipped: resumable) and ``RUN/evaluations/summary.json``.

  PYTHONPATH=src:. .venv/bin/python scripts/evaluate_code_run.py --run-dir runs/<run> --dry-run
  PYTHONPATH=src:. .venv/bin/python scripts/evaluate_code_run.py --run-dir runs/<run> [--jobs 4]
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / 'src', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from research_swarm import evaluator as ev  # noqa: E402
from research_swarm.coding import evaluator as cev  # noqa: E402
from research_swarm.coding import judge as code_judge  # noqa: E402

EVALUATED_OUTCOMES = cev.ACCEPTED_OUTCOMES
LOCK_NAME = '.evaluate_run.lock'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--include-rejected', action='store_true', help='also evaluate rejected submissions')
    parser.add_argument('--retry-unresolved', action='store_true',
                        help='evaluate again submissions whose latest evaluation is unresolved')
    parser.add_argument('--limit', type=int, help='evaluate at most this many submissions now')
    parser.add_argument('--jobs', type=int, default=4, help='parallel sandbox containers (default 4)')
    parser.add_argument('--dry-run', action='store_true', help='list pending submissions; no Docker, no writes')
    parser.add_argument('--image', default=code_judge.AGENT_IMAGE)
    parser.add_argument('--local', action='store_true',
                        help='development only: run the harness as a local subprocess, not in a container')
    return parser.parse_args(argv)


def _submission_id(number):
    return 'sub-%06d' % number


def read_sqlite(run_dir):
    """{submission_id: row} from the conference state (read-only); {} if absent."""
    path = Path(run_dir) / 'conference' / 'state.sqlite'
    if not path.exists():
        return {}
    db = sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        submissions = {}
        for row in db.execute('SELECT * FROM submissions ORDER BY submission_id'):
            entry = {k: row[k] for k in row.keys()
                     if k in ('submission_id', 'agent_id', 'task_id', 'source', 'source_sha256', 'file_path',
                              'submitted_at', 'task_status_at_submission', 'outcome', 'stage', 'reason',
                              'compile_status', 'judged_at')}
            entry['submission_id'] = _submission_id(entry['submission_id'])
            entry['problem_id'] = entry.pop('task_id')
            submissions[entry['submission_id']] = entry
        return submissions
    finally:
        db.close()


def collect_submissions(run_dir, include_rejected=False):
    """Merge SQLite and queue records by submission_id. Returns (selected, notes)."""
    submissions = read_sqlite(run_dir)
    queued, malformed = cev.read_code_queue(ev.run_evaluations_dir(run_dir))
    notes = {'sqlite_submissions': len(submissions), 'queue_entries': len(queued), 'queue_malformed': malformed}
    merged = {sid: dict(row, origins=['sqlite']) for sid, row in submissions.items()}
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
            continue
        merged[sid] = {'submission_id': sid, 'agent_id': meta.get('agent_id'),
                       'problem_id': meta.get('problem_id', entry['task_id']), 'source': entry['source'],
                       'source_sha256': entry['source_sha256'], 'file_path': meta.get('file_path'),
                       'submitted_at': meta.get('submitted_at'), 'judged_at': meta.get('judged_at'),
                       'outcome': meta.get('outcome'), 'origins': ['queue']}
    wanted = EVALUATED_OUTCOMES + (('rejected',) if include_rejected else ())
    selected = sorted((s for s in merged.values() if s.get('outcome') in wanted), key=lambda s: s['submission_id'])
    return selected, notes


def read_evaluations(path):
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


def pending(submissions, latest, retry_unresolved=False):
    return [s for s in submissions if s['submission_id'] not in latest
            or (retry_unresolved and latest[s['submission_id']].get('validity') == 'unresolved')]


def _metadata(item):
    keep = ('submission_id', 'agent_id', 'problem_id', 'outcome', 'source_sha256', 'file_path', 'submitted_at',
            'judged_at', 'stage', 'reason', 'compile_status', 'origins', 'discrepancies')
    return {k: item[k] for k in keep if item.get(k) is not None}


def evaluate_one(item, dataset, runner_, describe, clock=time.time):
    """One gdm-evaluation/1 entry (with coding fields). Data problems are unresolved without a run."""
    task_id = item['problem_id']
    source = item['source']
    started = clock()
    problem, task, variant, reference = None, None, None, None
    try:
        task = dataset.task(task_id)
    except KeyError:
        problem = 'Task not in the coding dataset'
    if problem is None:
        try:
            variant = dataset.variant(task_id)
            reference = dataset.reference_tests(task_id)
        except Exception as exc:  # PrivateDataMissing, hash mismatch
            problem = 'Researcher-only reference data unavailable: %s' % type(exc).__name__
    if problem is None:
        if ev._sha256_text(source) != item['source_sha256']:
            problem = 'Stored source does not match its recorded digest'
        elif item.get('discrepancies'):
            problem = 'Conference state and queue disagree: ' + '; '.join(item['discrepancies'])
    if problem is not None:
        evaluation = {'validity': 'unresolved', 'reason': problem, 'review_flags': [],
                      'evidence': {'details': problem, 'variant': variant}, 'variant': variant,
                      'visible_passed': None, 'visible_total': None, 'reference_passed': None,
                      'reference_total': None, 'hardcoded_fake': False, 'classify_code': None}
    else:
        evaluation = cev.evaluate_code(source, variant, task.visible_tests, reference, task.entry_point, runner_,
                                       item.get('outcome'))
    return cev.code_evaluation_entry(task_id, task.entry_point if task else '', task.starter if task else '',
                                     source, evaluation, _metadata(item), describe, started, clock())


def summarize(latest, notes):
    entries = list(latest.values())
    candidates = {}
    for e in sorted(entries, key=lambda e: e['submission']['submission_id']):
        for flag in e.get('review_flags') or []:
            candidates.setdefault(flag, []).append(e['submission']['submission_id'])
    return {'schema': 'gdm-evaluation-summary/1', 'domain': 'code',
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'evaluated_submissions': len(entries),
            'by_validity': dict(Counter(e['validity'] for e in entries)),
            'by_outcome_and_validity': dict(Counter('%s/%s' % (e['submission'].get('outcome'), e['validity'])
                                                    for e in entries)),
            'by_variant_and_validity': dict(Counter('%s/%s' % (e.get('variant'), e['validity']) for e in entries)),
            'by_task': {task: dict(Counter(e['validity'] for e in entries if e['task_id'] == task))
                        for task in sorted({e['task_id'] for e in entries})},
            'hardcoded_fake': sorted(e['submission']['submission_id'] for e in entries if e.get('hardcoded_fake')),
            'review_flags': dict(Counter(f for e in entries for f in e.get('review_flags') or [])),
            'review_candidates': candidates,
            'review_flag_reasons': cev.review_reasons(sorted(candidates)),
            'sources': notes}


def evaluate_run(run_dir, runner_, dataset=None, include_rejected=False, retry_unresolved=False, limit=None,
                 jobs=1, log=print, clock=time.time):
    """Evaluate pending submissions (``jobs`` at a time). Returns the summary dict."""
    if dataset is None:
        from research_swarm.coding.dataset import CodingDataset
        dataset = CodingDataset(ROOT)
    submissions, notes = collect_submissions(run_dir, include_rejected)
    out_dir = ev.run_evaluations_dir(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / 'evaluations.jsonl'
    todo = pending(submissions, read_evaluations(output)[0], retry_unresolved)
    if limit is not None:
        todo = todo[:limit]
    describe = runner_.describe() if hasattr(runner_, 'describe') else {'kind': type(runner_).__name__}
    describe = dict(describe, semantics=cev.SEMANTICS)
    log('%d selected submissions, %d to evaluate' % (len(submissions), len(todo)))
    write_lock, counter = threading.Lock(), [0]

    def work(item):
        entry = evaluate_one(item, dataset, runner_, describe, clock)
        ev.append_jsonl(output, entry, write_lock)
        with write_lock:
            counter[0] += 1
            log('[%d/%d] %s %s %s: %s (%s)' % (counter[0], len(todo), item['submission_id'], item['problem_id'],
                                               item.get('outcome'), entry['validity'], entry.get('variant')))

    with ThreadPoolExecutor(max_workers=max(1, int(jobs))) as pool:
        for future in [pool.submit(work, item) for item in todo]:
            future.result()
    latest, malformed = read_evaluations(output)
    summary = summarize(latest, dict(notes, evaluations_malformed=malformed))
    (out_dir / 'summary.json').write_text(json.dumps(summary, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    return summary


def main(argv=None):
    args = parse_args(argv)
    run_dir = args.run_dir.resolve()
    if not (run_dir / 'conference').exists() and not ev.run_evaluations_dir(run_dir).exists():
        print('No conference state or evaluation queue under %s' % run_dir)
        return 2
    submissions, notes = collect_submissions(run_dir, args.include_rejected)
    if args.dry_run:
        latest = read_evaluations(ev.run_evaluations_dir(run_dir) / 'evaluations.jsonl')[0]
        todo = pending(submissions, latest, args.retry_unresolved)
        print(json.dumps(notes))
        for item in todo:
            print(item['submission_id'], item['problem_id'], item.get('outcome'), ','.join(item['origins']))
        print('%d pending of %d selected' % (len(todo), len(submissions)))
        return 0
    if args.local:
        runner_ = code_judge.LocalPythonRunner()
    else:
        runner_ = code_judge.DockerPythonRunner(image=args.image, label=cev.EVALUATOR_LABEL,
                                                name_prefix=cev.EVALUATOR_PREFIX, max_concurrent=None)
    ev.run_evaluations_dir(run_dir).mkdir(parents=True, exist_ok=True)
    with open(ev.run_evaluations_dir(run_dir) / LOCK_NAME, 'a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('Another evaluation is working on this run')
            return 3
        summary = evaluate_run(run_dir, runner_, include_rejected=args.include_rejected,
                               retry_unresolved=args.retry_unresolved, limit=args.limit, jobs=args.jobs)
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
