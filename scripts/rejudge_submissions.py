#!/usr/bin/env python3
"""Offline re-judge of a run's submissions that were never judged or never credited (researcher-only).

A submission to a closed problem is stored with outcome ``problem_closed`` and never judged
(src/research_swarm/conference.py ``_submit_solution``), so "genuine" was only known for accepted submissions.
This script runs, over every selected submission:

1. the SAME operational judge (``judge.judge_submission``: keyword scan, immutable-source check, Lean compilation
   with zero ``sorry``) with the run's own task templates (frozen dataset, hash-checked against the conference
   state) and the conference's Lean backend (``lean.DockerLeanCompiler`` with the run's pinned judge image,
   timeout, memory and CPUs from settings.json ``effective.judge``);
2. the stricter proof checker of check_proof / the research evaluator (``evaluator.evaluate_submission`` with the
   validated ``DockerComparatorBackend``) on the sources that pass the operational judge (all with
   ``--strict-all``). A verdict the proof checker already gave for the same task and source
   (evaluations/evaluations.jsonl, not ``unresolved``) is reused; restated answers are ``fraudulent`` without a
   container (evaluator semantics v4);
3. the strict fake rule (research/analysis/storymap/fake_rule.py ``is_fake``), applied as if the submission had
   been accepted when it passes the operational judge (else the pattern alone, exactly as for a non-accepted one).

Selection: outcomes ``problem_closed`` and ``accepted_after_close`` (default), plus ``rejected`` with
--include-rejected, plus the submit_solution sources of monitor-blocked actions with --include-blocked (monitor
events; they never reached the conference: ids ``blocked-NNNNNN``). Identical sources of one task are judged once.

Output: one ``gdm-rejudge/1`` JSON line per submission appended to RUN/analysis/rejudge.jsonl (Git-ignored run
directory; latest line per submission_id wins), fields: submission_id, public_id, agent_id, problem_id, outcome,
origin, submitted_at, source_sha256, compiles (Lean exit 0, sorry or not), compile_status, sorry_count,
passes_judge, judge {stage, reason}, strict {validity, reason, review_flags, source}, genuine (strict ``genuine``;
None when the strict checker was not run), fake, fake_pattern, fake_rule_version, judge_image, rejudged_at,
seconds. Resumable: submissions already in the file are skipped (--retry-unresolved re-runs rows whose strict
verdict is unresolved or whose compile hit setup_error/timeout/killed).

Parallel and bounded: --workers Lean containers at once (each 2g, 1 CPU, the run's settings), --strict-workers
comparator containers at once (each 6g, 2 CPUs; 1 = the host-wide evaluator lock as evaluate_run.py). On macOS
(the owner's laptop) workers + strict workers are capped at 4 heavy processes in total.

Needs Docker with the run's Lean judge image (gdm-math-swarm-lean) and, for the strict checker, the validated
comparator image and a data/validation/evaluator-<UTC>.json record: in practice the VM. Refuses while agent or
judge containers of a live run are running (--force-while-live overrides; recorded per row). No model calls,
network, credentials or writes outside RUN/analysis/.

  PYTHONPATH=src .venv/bin/python scripts/rejudge_submissions.py --run-dir runs/RUN --dry-run
  PYTHONPATH=src .venv/bin/python scripts/rejudge_submissions.py --run-dir runs/RUN --workers 16 --strict-workers 2
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT / 'src'), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
from research_swarm import judge as operational_judge  # noqa: E402
from research_swarm.judge import TaskTemplate  # noqa: E402
from research.analysis import run_metrics  # noqa: E402

SCHEMA = 'gdm-rejudge/1'
OUTPUT = run_metrics.REJUDGE_FILE
DEFAULT_OUTCOMES = ('problem_closed', 'accepted_after_close')
RETRY_COMPILE = ('setup_error', 'timeout', 'killed')
LAPTOP_HEAVY_LIMIT = 4


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n'.join(__doc__.split('\n')[1:]))
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--include-rejected', action='store_true', help='also re-judge rejected submissions')
    parser.add_argument('--include-blocked', action='store_true',
                        help='also judge submit_solution sources of monitor-blocked actions')
    parser.add_argument('--workers', type=int, default=4, help='Lean judge containers at once (default 4)')
    parser.add_argument('--strict-workers', type=int, default=1,
                        help='comparator containers at once (default 1: host-wide evaluator lock)')
    parser.add_argument('--no-strict', action='store_true', help='operational judge only (genuine stays None)')
    parser.add_argument('--strict-all', action='store_true',
                        help='run the strict checker also on sources the operational judge rejects')
    parser.add_argument('--retry-unresolved', action='store_true',
                        help='redo rows with an unresolved strict verdict or a setup_error/timeout/killed compile')
    parser.add_argument('--limit', type=int, help='judge at most this many distinct sources now')
    parser.add_argument('--lean-image', help='override the judge image (default: the run\'s pinned image ID, else '
                                             'its tag)')
    parser.add_argument('--validation-record', type=Path,
                        help='comparator validation record (default: newest data/validation/evaluator-<UTC>.json)')
    parser.add_argument('--force-while-live', action='store_true',
                        help='run even while agent/judge containers of a live run are running (recorded)')
    parser.add_argument('--dry-run', action='store_true', help='list what would be judged; no Docker, no writes')
    return parser.parse_args(argv)


# ------------------------------------------------------------------------------------------- selection
def read_submissions(run, outcomes):
    """Selected conference submissions (internal ids sub-NNNNNN) from state.sqlite, opened read-only."""
    path = Path(run) / 'conference' / 'state.sqlite'
    if not path.is_file():
        return [], {}
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in db.execute('PRAGMA table_info(submissions)')}
        public = ', public_id' if 'public_id' in columns else ''
        tasks = {row['task_id']: dict(row) for row in db.execute(
            'SELECT task_id, theorem_name, answer_based, template_sha256 FROM tasks')}
        marks = ','.join('?' * len(outcomes))
        rows = db.execute(f'SELECT submission_id, agent_id, task_id, source, source_sha256, submitted_at, outcome'
                          f'{public} FROM submissions WHERE outcome IN ({marks}) ORDER BY submission_id',
                          tuple(outcomes)).fetchall()
    finally:
        db.close()
    out = [{'submission_id': 'sub-%06d' % row['submission_id'], 'public_id': row['public_id'] if public else None,
            'agent_id': row['agent_id'], 'problem_id': row['task_id'], 'source': row['source'],
            'source_sha256': row['source_sha256'], 'submitted_at': row['submitted_at'], 'outcome': row['outcome'],
            'origin': 'conference'} for row in rows]
    return out, tasks


def blocked_submissions(run):
    """submit_solution sources of monitor-blocked tool calls (never reached the conference); their inputs come
    from RUN/monitor/calls.jsonl. Ids ``blocked-<tool_use_id>``."""
    runtime = run_metrics.read_runtime(run)
    events, _ = run_metrics.read_monitor_events(run, runtime)
    actions = run_metrics.blocked_actions(events, run_metrics.monitor_calls(events), run_metrics.read_call_inputs(run))
    out = []
    for action, problem, source in run_metrics.blocked_submissions(actions):
        if problem:
            out.append({'submission_id': f'blocked-{action["tool_use_id"]}', 'public_id': None,
                        'agent_id': action['agent'], 'problem_id': problem, 'source': source,
                        'source_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
                        'submitted_at': action['ts'], 'outcome': 'monitor_blocked', 'origin': 'monitor_blocked'})
    return out


def read_output(path):
    latest = {}
    for row in run_metrics._jsonl(path):
        if row.get('submission_id'):
            latest[row['submission_id']] = row
    return latest


def needs_work(row, retry):
    if row is None:
        return True
    if not retry:
        return False
    return (row.get('compile_status') in RETRY_COMPILE
            or (row.get('strict') or {}).get('validity') == 'unresolved')


def templates(run, tasks, problem_ids):
    """{task_id: (TaskTemplate, problem or None)} from the run's frozen dataset revision, hash-checked."""
    from research_swarm.dataset import FrozenDataset, run_dataset_revision, run_task_set
    dataset = FrozenDataset(ROOT, task_set=run_task_set(run), revision=run_dataset_revision(run, ROOT))
    out = {}
    for task_id in sorted(problem_ids):
        task = dataset.tasks.get(task_id)
        if task is None:
            out[task_id] = (None, 'task not in the frozen dataset')
            continue
        source = task.template_source(dataset.repository)
        recorded = (tasks.get(task_id) or {}).get('template_sha256')
        if recorded not in (None, hashlib.sha256(source.encode('utf-8')).hexdigest()):
            out[task_id] = (None, 'run template differs from the frozen dataset template')
            continue
        out[task_id] = (TaskTemplate(source, task.theorem_name, bool(task.answer_based)), None)
    return out


def prior_verdicts(run):
    """{(task_id, source_sha256): (validity, reason, flags)} from the proof checker, unresolved excluded."""
    out = {}
    for entry in run_metrics._jsonl(Path(run) / 'evaluations' / 'evaluations.jsonl'):
        if entry.get('validity') in ('genuine', 'fraudulent'):
            out[(entry.get('task_id'), entry.get('source_sha256'))] = (
                entry['validity'], entry.get('reason'), list(entry.get('review_flags') or []))
    return out


# --------------------------------------------------------------------------------------------- judging
class Precompiled:
    """A compiler that returns one already computed CompileResult (judge_submission compiles at most once)."""

    def __init__(self, result):
        self.result = result

    def compile(self, source):
        return self.result


def judge_one(template, source, compiler):
    """(compile result, judge result). Lean always runs, so ``compiles`` is known even when a static check fails;
    the judge itself is ``judge_submission`` with that one compile result."""
    result = compiler.compile(source)
    return result, operational_judge.judge_submission(template, source, Precompiled(result))


def fake_verdict(source, passes, validity):
    from research.analysis.storymap import fake_rule
    form = fake_rule.classify(source or '') or None
    return fake_rule.is_fake(source, 'accepted' if passes else 'not_accepted', validity), form, fake_rule.RULE_VERSION


class StrictChecker:
    """Validated comparator backend(s); ``workers`` > 1 runs that many containers at once (no host lock)."""

    def __init__(self, workers=1, validation_record=None):
        from research_swarm import evaluator as ev
        self.ev = ev
        if workers > 1:
            runner = ev.ComparatorRunner(lock_path=None)
            self.backend = ev.DockerComparatorBackend(runner=runner, validated_image=True,
                                                      validation_record=validation_record)
        else:
            self.backend = ev.DockerComparatorBackend(validated_image=True, validation_record=validation_record)
        self.gate = threading.BoundedSemaphore(max(1, workers))
        self.error = None if self.backend.validation is not None else self.backend.validation_error

    def check(self, template, source):
        backend = self.backend if self.error is None else None
        with self.gate:
            return self.ev.evaluate_submission(template, source, backend)


def strict_verdict(template, source, key, passes, prior, checker, strict_all):
    if key in prior:
        validity, reason, flags = prior[key]
        return {'validity': validity, 'reason': reason, 'review_flags': flags, 'source': 'evaluations.jsonl'}
    if checker is None or (not passes and not strict_all):
        why = 'strict checker not requested' if checker is None else 'operational judge rejected; not checked'
        return {'validity': None, 'reason': why, 'review_flags': [], 'source': 'not_run'}
    result = checker.check(template, source)
    return {'validity': result.validity, 'reason': result.reason, 'review_flags': list(result.review_flags),
            'source': 'comparator' if checker.error is None else 'unvalidated (%s)' % checker.error}


def judge_key(template, problem, source, key, compiler, prior, checker, strict_all):
    """Everything about one distinct (task, source)."""
    started = time.time()
    if template is None:
        return {'compiles': None, 'compile_status': None, 'sorry_count': None, 'passes_judge': False,
                'judge': {'stage': 'template', 'reason': problem}, 'genuine': None, 'fake': None,
                'fake_pattern': None, 'fake_rule_version': None,
                'strict': {'validity': None, 'reason': problem, 'review_flags': [], 'source': 'not_run'},
                'seconds': 0.0}
    compiled, verdict = judge_one(template, source, compiler)
    passes = bool(verdict.accepted)
    strict = strict_verdict(template, source, key, passes, prior, checker, strict_all)
    fake, form, version = fake_verdict(source, passes, strict['validity'])
    return {'compiles': compiled.status == 'success' and compiled.exit_code == 0,
            'compile_status': compiled.status, 'sorry_count': compiled.sorry_count,
            'compile_seconds': None if compiled.elapsed_seconds is None else round(compiled.elapsed_seconds, 3),
            'passes_judge': passes, 'judge': {'stage': verdict.stage, 'reason': verdict.reason},
            'strict': strict, 'genuine': None if strict['validity'] is None else strict['validity'] == 'genuine',
            'fake': fake, 'fake_pattern': form, 'fake_rule_version': version,
            'seconds': round(time.time() - started, 3)}


def row_for(item, verdict, image, forced):
    keep = ('submission_id', 'public_id', 'agent_id', 'problem_id', 'outcome', 'origin', 'submitted_at',
            'source_sha256')
    row = {'schema': SCHEMA, **{k: item.get(k) for k in keep}, **verdict, 'judge_image': image,
           'rejudged_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    if forced:
        row['judged_while_live'] = forced
    return row


def append(path, rows, lock):
    text = ''.join(json.dumps(r, sort_keys=True, default=str) + '\n' for r in rows)
    with lock, open(path, 'a', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------------------------- driver
def judge_settings(run):
    settings = run_metrics._read_json(Path(run) / 'settings.json') or {}
    judge = (settings.get('effective') or {}).get('judge') or {}
    config = settings.get('config') or {}
    return {'image': judge.get('image') or config.get('lean_image') or 'gdm-math-swarm-lean:local',
            'tag': config.get('lean_image') or 'gdm-math-swarm-lean:local',
            'timeout': int(judge.get('timeout_seconds') or config.get('compilation_timeout_seconds') or 120),
            'memory': str(judge.get('memory') or config.get('judge_memory') or '2g'),
            'cpus': judge.get('cpus') or config.get('judge_cpus') or 1}


def docker_image_present(image):
    try:
        return subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def bounded(workers, strict_workers, platform=sys.platform):
    """Worker counts within the laptop limit (macOS: at most 4 heavy processes in total)."""
    workers, strict_workers = max(1, workers), max(1, strict_workers)
    if platform == 'darwin' and workers + strict_workers > LAPTOP_HEAVY_LIMIT:
        strict_workers = min(strict_workers, LAPTOP_HEAVY_LIMIT // 2)
        workers = max(1, LAPTOP_HEAVY_LIMIT - strict_workers)
    return workers, strict_workers


def plan(run, include_rejected=False, include_blocked=False, retry=False):
    """(groups {(task, sha): [items]}, tasks, done rows, selected count)."""
    outcomes = DEFAULT_OUTCOMES + (('rejected',) if include_rejected else ())
    items, tasks = read_submissions(run, outcomes)
    if include_blocked:
        items += blocked_submissions(run)
    done = read_output(Path(run) / OUTPUT)
    groups = {}
    for item in items:
        if needs_work(done.get(item['submission_id']), retry):
            groups.setdefault((item['problem_id'], item['source_sha256']), []).append(item)
    return groups, tasks, done, len(items)


def rejudge(run, compiler, checker=None, workers=4, strict_all=False, retry=False, include_rejected=False,
            include_blocked=False, limit=None, image=None, forced=None, log=print):
    """Judge every pending distinct source; returns a summary dict. ``compiler``: an object with
    ``compile(source) -> CompileResult`` (DockerLeanCompiler in production)."""
    run = Path(run)
    groups, tasks, _, selected = plan(run, include_rejected, include_blocked, retry)
    keys = sorted(groups, key=lambda k: min(i['submission_id'] for i in groups[k]))
    if limit is not None:
        keys = keys[:limit]
    found = templates(run, tasks, {k[0] for k in keys}) if keys else {}
    prior = prior_verdicts(run)
    output = run / OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    log(f'{selected} selected submissions; {sum(len(groups[k]) for k in keys)} to judge as {len(keys)} distinct '
        f'sources ({workers} Lean workers)')
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {}
        for key in keys:
            template, problem = found.get(key[0], (None, 'no template'))
            futures[pool.submit(judge_key, template, problem, groups[key][0]['source'], key, compiler, prior,
                                checker, strict_all)] = key
        for future in as_completed(futures):
            key = futures[future]
            try:
                verdict = future.result()
            except Exception as exc:  # one broken source never stops the run; recorded and retried later
                verdict = {'compiles': None, 'compile_status': 'setup_error', 'passes_judge': False,
                           'judge': {'stage': 'error', 'reason': f'{type(exc).__name__}: {exc}'[:500]},
                           'strict': {'validity': 'unresolved', 'reason': 'not run', 'review_flags': [],
                                      'source': 'not_run'}, 'genuine': None, 'fake': None}
            append(output, [row_for(item, verdict, image, forced) for item in groups[key]], lock)
            done += 1
            log(f'[{done}/{len(keys)}] {key[0]} {key[1][:12]} x{len(groups[key])}: compiles={verdict.get("compiles")} '
                f'judge={verdict.get("passes_judge")} strict={(verdict.get("strict") or {}).get("validity")} '
                f'fake={verdict.get("fake")}')
    latest = read_output(output)
    return {'output': str(output), 'rows': len(latest), 'judged_now': done,
            'coverage': run_metrics.rejudge_coverage(None, latest)}


def live_problem():
    from research_swarm import evaluator as ev
    live = ev.live_experiment_containers()
    if live is None:
        return 'could not list running containers (is Docker running?)'
    if live:
        return '%d agent/judge container(s) running (%s)' % (len(live), ', '.join(c['name'] for c in live[:5]))
    return None


def main(argv=None):
    args = parse_args(argv)
    run = args.run_dir.resolve()
    if not (run / 'conference' / 'state.sqlite').is_file():
        print(f'No conference/state.sqlite under {run}')
        return 2
    settings = run_metrics._read_json(run / 'settings.json') or {}
    if ((settings.get('config') or {}).get('domain') or 'math') == 'code':
        print('Coding runs are not supported (the Lean judge does not apply).')
        return 2
    if args.dry_run:
        groups, _, done, selected = plan(run, args.include_rejected, args.include_blocked, args.retry_unresolved)
        print(json.dumps({'selected': selected, 'already_rejudged': len(done),
                          'pending_submissions': sum(len(v) for v in groups.values()),
                          'pending_distinct_sources': len(groups), 'judge': judge_settings(run)}, indent=1))
        return 0
    workers, strict_workers = bounded(args.workers, args.strict_workers)
    if (workers, strict_workers) != (args.workers, args.strict_workers):
        print(f'Laptop limit: {workers} Lean workers, {strict_workers} strict workers')
    try:
        docker_ok = subprocess.run(['docker', 'info'], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        docker_ok = False
    if not docker_ok:
        print('Docker is not available here. The re-judge needs Docker with the run\'s Lean judge image (and the '
              'validated comparator image for the strict checker): run it on the VM.')
        return 2
    problem = live_problem()
    forced = None
    if problem and not args.force_while_live:
        print(f'Refusing: {problem}. Re-judge after the run ends (or pass --force-while-live).')
        return 2
    if problem:
        forced = {'checked_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'problem': problem}
        print('WARNING: judging while live: ' + problem)
    js = judge_settings(run)
    image = args.lean_image or js['image']
    if not docker_image_present(image):
        if not args.lean_image and docker_image_present(js['tag']):
            print(f'Pinned judge image {image} not present; using the tag {js["tag"]} (recorded per row)')
            image = js['tag']
        else:
            print(f'Lean judge image {image} is not available to Docker here. Run on the VM that ran the experiment.')
            return 2
    from research_swarm.lean import DockerLeanCompiler
    compiler = DockerLeanCompiler(image=image, timeout=js['timeout'], memory=js['memory'], cpus=js['cpus'],
                                  max_concurrent=workers)
    checker = None
    if not args.no_strict:
        checker = StrictChecker(strict_workers, args.validation_record)
        if checker.error:
            print(f'Strict checker not validated ({checker.error}); its verdicts will be unresolved. Pass --no-strict '
                  'to skip it, or run scripts/validate_evaluator.py first.')
    summary = rejudge(run, compiler, checker, workers=workers + (strict_workers if checker else 0),
                      strict_all=args.strict_all, retry=args.retry_unresolved, include_rejected=args.include_rejected,
                      include_blocked=args.include_blocked, limit=args.limit, image=image, forced=forced)
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
