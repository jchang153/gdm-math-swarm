#!/usr/bin/env python3
"""Researcher-only validation of the independent evaluator on real Lean (Phase B).

Everything runs through the production runner (research_swarm.evaluator:
ComparatorRunner with its default image, seccomp profile and resources), one
comparator container at a time, by the image ID resolved at start:
1. the sandbox probe (required; nothing else runs if it fails);
2. every fixture in research/fixtures/evaluator/cases.json in verify mode (the
   submissions/ entry is a symlink to the host-only private/research/fixtures/
   evaluator/submissions/, git-ignored),
   judged against research_swarm.evaluator.VALIDATION_EXPECTATIONS;
3. upstream cross-checks (unmodified comparator exit code; advisory);
4. with --pilot-self-compare, each of the 14 frozen pilot templates against
   itself (pristine, plus the helper-answer bridge for answer-based tasks),
   expecting illegal_axiom (sorryAx) -- never mismatch or challenge_failed.
   --task-set all71 self-compares the 71-task parent pool's templates instead
   (docs/plan.md, "Gemini 3.1 Pro collaboration run authorized"), and
   --only-tasks ID,ID,... restricts the self-comparisons to the named tasks (the
   evaluator certifies only templates with a passing self-comparison).
The record (data/validation/evaluator-<UTC>.json, interface I6) is rewritten
atomically after every case, so a crash loses nothing; --resume continues it.
Full comparator records (with untrusted output tails) go only to the
git-ignored runs/validation-evaluator/<stamp>/. Fixture text is never stored.
Run only with nothing else using Docker (docs/evaluator.md, "Resources"): it
refuses to start while judge or agent containers run (--force-while-live
overrides, loudly and on the record). --cases subsets never write a gate record.

  PYTHONPATH=src .venv/bin/python scripts/validate_evaluator.py --offline-plan
  PYTHONPATH=src .venv/bin/python scripts/validate_evaluator.py --pilot-self-compare
  PYTHONPATH=src .venv/bin/python scripts/validate_evaluator.py --resume data/validation/evaluator-<UTC>.json
"""
from __future__ import annotations
import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from research_swarm import evaluator as ev  # noqa: E402
from research_swarm.dataset import PILOT_TASK_SET, TASK_SETS, FrozenDataset  # noqa: E402
from research_swarm.judge import TaskTemplate, immutable_segments  # noqa: E402
from research_swarm.keyword_filter import check_keywords  # noqa: E402

DEBUG_DIR = ROOT / 'runs' / 'validation-evaluator'


def stamp(moment=None):
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(moment))


def iso(moment=None):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(moment))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--image', default=ev.DEFAULT_IMAGE, help='comparator tag; runs use its resolved image ID')
    parser.add_argument('--seccomp', type=Path, default=ev.DEFAULT_SECCOMP)
    parser.add_argument('--pilot-self-compare', action='store_true',
                        help='also self-compare the 14 pilot templates (pristine and answer bridge)')
    parser.add_argument('--task-set', default=PILOT_TASK_SET, choices=sorted(TASK_SETS),
                        help='templates to self-compare (default the 14-task pilot)')
    parser.add_argument('--only-tasks', help='comma-separated task ids: self-compare only these templates')
    parser.add_argument('--skip-fixtures', action='store_true', help='with --resume: only add self-comparisons')
    parser.add_argument('--no-upstream', action='store_true', help='skip the advisory upstream cross-checks')
    parser.add_argument('--cases', help='comma-separated fixture names (debug; never a gate record)')
    parser.add_argument('--resume', type=Path, help='continue an existing record (same image, pins, resources)')
    parser.add_argument('--rerun-failed', action='store_true', help='with --resume: run failed entries again')
    parser.add_argument('--out', type=Path, help='record path (default data/validation/evaluator-<UTC>.json)')
    parser.add_argument('--offline-plan', action='store_true', help='static checks and plan only; no Docker')
    parser.add_argument('--force-while-live', action='store_true',
                        help='start comparator containers even while judge/agent containers run (recorded; '
                             'can change a live run\'s acceptances)')
    return parser.parse_args(argv)


def static_checks(template, source):
    """The operational judge's static stages; fixtures must be operationally admissible."""
    keywords = list(check_keywords(source).matched_keywords)
    try:
        intact = immutable_segments(template.source, template.answer_based) == immutable_segments(
            source, template.answer_based)
    except ValueError:
        intact = False
    return {'keywords': keywords, 'immutable_segments_intact': intact, 'passed': not keywords and intact}


def rel(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def save(record, path):
    record['updated_at'] = iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.%s.tmp' % path.name)
    temporary.write_text(json.dumps(record, indent=1, sort_keys=True, default=str) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def dump_full(debug_dir, name, payload):
    if debug_dir is None:
        return
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / (name + '.json')).write_text(json.dumps(payload, indent=1, default=str) + '\n', encoding='utf-8')


def environment(docker='docker'):
    info = {'host_platform': platform.platform(), 'python': platform.python_version()}
    try:
        raw = subprocess.run([docker, 'info', '--format', '{{json .}}'], capture_output=True,
                             encoding='utf-8', timeout=60, check=False).stdout
        data = json.loads(raw or '{}')
        info.update({k: data.get(k) for k in ('ServerVersion', 'KernelVersion', 'OperatingSystem',
                                              'Architecture', 'NCPU', 'MemTotal')})
    except (OSError, subprocess.TimeoutExpired, ValueError):
        info['docker_info'] = 'unavailable'
    return info


def peak(records):
    values = [r.get('cgroup_memory_peak_bytes') for r in records if isinstance(r.get('cgroup_memory_peak_bytes'), int)]
    return max(values) if values else None


def run_case(case, runner, image_id, entry_sha256=None):
    """One fixture through evaluate_submission + the production runner. Returns (entry, full)."""
    template, source = case['template_obj'], case['source']
    expectation = ev.VALIDATION_EXPECTATIONS.get(case['category'])
    static = static_checks(template, source)
    before, started = runner.invocations, time.time()
    result = ev.evaluate_submission(template, source, ev.CandidateBackend(runner, image_id, entry_sha256))
    finished = time.time()
    evidence = result.evidence or {}
    attempts, last = evidence.get('attempts') or [], evidence.get('record') or {}
    entry = {
        'name': case['name'], 'category': case['category'], 'submission': case['submission'],
        'template': case.get('task_id') or case.get('template'), 'theorem_name': template.theorem_name,
        'answer_based': template.answer_based, 'submission_sha256': case['submission_sha256'],
        'template_sha256': case['template_sha256'],
        'expected_verdicts': sorted(expectation.verdicts) if expectation else None,
        'expected_statuses': sorted(expectation.statuses) if expectation else None,
        'observed_verdict': result.validity, 'reason': result.reason,
        'statuses': [a.get('status') for a in attempts], 'axiom': last.get('axiom'),
        'docker_runs': runner.invocations - before, 'real_lean': True,
        'static_checks': static, 'static_checks_passed': static['passed'],
        'review_flags': list(result.review_flags), 'answer': evidence.get('answer'),
        'details': str(evidence.get('details', ''))[:1000], 'attempts': attempts,
        'wall_seconds': round(finished - started, 3), 'peak_memory_bytes': peak(attempts),
        'started_at': iso(started), 'finished_at': iso(finished)}
    entry['passed'] = ev.case_passes(entry)
    return entry, {'entry': entry, 'record': last}


def run_upstream(case, runner, image_id, verdict):
    started = time.time()
    record = runner.upstream_exit(case['template_obj'], case['source'], image=image_id)
    code = record.get('exit_code')
    agrees = record.get('status') == 'upstream_exit' and code is not None and (code == 0) == (verdict == 'genuine')
    return {'name': case['name'], 'verify_verdict': verdict, 'status': record.get('status'), 'exit_code': code,
            'agrees': agrees, 'advisory': True, 'real_lean': True, 'host_seconds': record.get('host_seconds'),
            'peak_memory_bytes': record.get('cgroup_memory_peak_bytes'),
            'wall_seconds': round(time.time() - started, 3), 'detail': str(record.get('detail', ''))[:500]}, record


def self_compare_requests(dataset, only=None):
    """(kind, task, template, request) for each pilot template (researcher-only sources);
    ``only``: a set of task ids to restrict to."""
    for task in dataset.pilot_tasks:
        if only is not None and task.task_id not in only:
            continue
        template = TaskTemplate(task.template_source(dataset.repository), task.theorem_name, task.answer_based)
        probe = ev.pristine_probe_source(template)
        yield 'pristine', task, template, ev.verify_request(probe, probe, task.theorem_name)
        if task.answer_based:
            solution = ev.bridge_probe_source(template)
            plan = ev.plan_answer(template, solution)
            challenge, bridged, target = ev.bridge_sources(template, solution, plan, 'True')
            yield 'bridge', task, template, ev.verify_request(challenge, bridged, target)


def run_self_compare(kind, task, template, request, runner, image_id):
    started = time.time()
    record = runner.invoke(request, image_id)
    entry = {'task_id': task.task_id, 'kind': kind, 'theorem_name': request['theorem_name'],
             'template_sha256': ev._sha256_text(template.source), 'status': record.get('status'),
             'axiom': record.get('axiom'), 'detail': str(record.get('detail', ''))[:500], 'real_lean': True,
             'entry_sha256': record.get('entry_sha256'),
             'host_seconds': record.get('host_seconds'), 'peak_memory_bytes': record.get('cgroup_memory_peak_bytes'),
             'steps': ev.attempt_summary(record)['steps'], 'wall_seconds': round(time.time() - started, 3),
             'finished_at': iso()}
    entry['passed'] = ev.self_compare_passes(entry)
    return entry, record


def finalize(record, manifest, subset):
    cases = {c.get('name'): c for c in record['cases']}
    digests = {c['name']: (c['submission_sha256'], c['template_sha256']) for c in manifest}
    fixtures_ok = all(name in cases and ev.case_passes(cases[name])
                      and (cases[name]['submission_sha256'], cases[name]['template_sha256']) == digests[name]
                      for name in digests)
    covered = {c['category'] for c in manifest} >= ev.REQUIRED_VALIDATION_CATEGORIES
    probe_ok = (record.get('probe') or {}).get('status') == 'probe_passed'
    # As in load_validation: the image's entry must be the repository's entry.
    entry_ok = record.get('image_entry_sha256') == ev.sha256_file(ROOT / ev.ENTRY_SOURCE)
    record['all_required_passed'] = bool(probe_ok and entry_ok and fixtures_ok and covered and not subset)
    record['summary'] = {
        'probe_passed': probe_ok, 'image_entry_current': entry_ok,
        'fixtures_passed': sum(1 for c in record['cases'] if ev.case_passes(c)), 'fixtures_total': len(manifest),
        'fixtures_failed': sorted(c['name'] for c in record['cases'] if not ev.case_passes(c)),
        'fixtures_missing': sorted(set(digests) - set(cases)),
        'upstream_agreeing': sum(1 for u in record['upstream'] if u.get('agrees')),
        'upstream_total': len(record['upstream']),
        'self_compare_passed': sum(1 for e in record['self_compare'] if ev.self_compare_passes(e)),
        'self_compare_total': len(record['self_compare']),
        'self_compare_failed': sorted('%s/%s' % (e['task_id'], e['kind']) for e in record['self_compare']
                                      if not ev.self_compare_passes(e)),
        'container_runs': record.get('container_runs', 0)}


def offline_plan(manifest, dataset, args):
    print('Fixture cases (static judge checks, answer plan, minimum container runs):')
    runs = 0
    for case in manifest:
        template, source = case['template_obj'], case['source']
        plan = ev.plan_answer(template, source)
        # Restated answers (semantics v4) and plan problems are decided without a container.
        minimum = 0 if plan.problem or ev.answer_restatement(template, source) else 1
        runs += minimum + (1 if case.get('upstream') and not args.no_upstream else 0)
        print('  %-40s %-32s static=%s plan=%s runs>=%d' % (case['name'], case['category'],
              static_checks(template, source)['passed'], plan.mode, minimum))
    if args.pilot_self_compare:
        only = set(args.only_tasks.split(',')) if getattr(args, 'only_tasks', None) else None
        planned = list(self_compare_requests(dataset, only))
        runs += len(planned)
        print('Self-comparisons: %d (%d pristine, %d bridge)' % (len(planned), sum(p[0] == 'pristine' for p in planned),
                                                               sum(p[0] == 'bridge' for p in planned)))
    print('Planned comparator container runs (lower bound, plus 1 probe): %d' % runs)
    return 0


def run(args, runner=None, image_id=None, debug_root=DEBUG_DIR, log=print):
    """Programmatic entry (tests inject a fake runner). Returns (exit_code, record_path)."""
    manifest = ev.load_fixture_manifest()
    dataset = FrozenDataset(ROOT, task_set=getattr(args, 'task_set', PILOT_TASK_SET))
    only = set(args.only_tasks.split(',')) if getattr(args, 'only_tasks', None) else None
    if only and not only <= {t.task_id for t in dataset.pilot_tasks}:
        raise SystemExit('Unknown task ids for this task set: ' + ', '.join(sorted(only - {t.task_id for t in dataset.pilot_tasks})))
    selected = set(args.cases.split(',')) if args.cases else None
    if selected and not selected <= {c['name'] for c in manifest}:
        raise SystemExit('Unknown fixture names: ' + ', '.join(sorted(selected - {c['name'] for c in manifest})))
    if args.offline_plan:
        return offline_plan(manifest, dataset, args), None
    if (args.skip_fixtures or args.rerun_failed) and not args.resume:
        # A fixture-less new record would become the newest gate record and close the gate.
        raise SystemExit('--skip-fixtures and --rerun-failed require --resume')
    target = args.resume or args.out
    if selected and target is not None and ev.VALIDATION_NAME.fullmatch(target.name):
        # A subset always finalizes all_required_passed=false; on a gate-named
        # record that would silently close (or downgrade) the evaluator gate.
        raise SystemExit('--cases is a debug subset and never writes a gate record (%s); omit --resume/--out '
                         '(subsets go to runs/validation-evaluator/) or use an evaluator-debug-*.json name'
                         % target.name)
    runner = runner or ev.ComparatorRunner(image=args.image, seccomp_profile=args.seccomp)
    interlock = ev.enforce_interlock(args.force_while_live, runner.docker, log)
    if interlock is None:
        return 2, None
    with ev.evaluator_lock(runner.lock_path):
        orphans = ev.cleanup_orphans(runner.docker)
    image_id = image_id or ev.local_image_id(args.image, runner.docker)
    if not image_id:
        log('Comparator image %s is not available' % args.image)
        return 2, None
    seccomp_sha = ev.sha256_file(runner.seccomp_profile)
    if args.resume:
        path = args.resume
        record = json.loads(path.read_text(encoding='utf-8'))
        mismatched = [k for k, v in (('image_ids', {'comparator': image_id}), ('pins', runner.pins),
                                     ('runner_settings', runner.settings), ('semantics', ev.SEMANTICS),
                                     ('seccomp_sha256', seccomp_sha), ('schema', ev.VALIDATION_SCHEMA),
                                     ('evaluator_py_sha256', ev.evaluator_source_sha256()),
                                     ('fixture_manifest_sha256', ev.sha256_file(ev.FIXTURE_MANIFEST)))
                      if record.get(k) != v]
        if mismatched:
            log('Cannot resume: record differs in ' + ', '.join(mismatched))
            return 2, path
        record['complete'] = False
    else:
        moment = time.time()
        default_dir = DEBUG_DIR if selected else ev.VALIDATION_DIR
        prefix = 'evaluator-debug-' if selected else 'evaluator-'
        path = args.out or default_dir / (prefix + stamp(moment) + '.json')
        record = {'schema': ev.VALIDATION_SCHEMA, 'created_at': iso(moment), 'complete': False,
                  'all_required_passed': False, 'image_ids': {'comparator': image_id}, 'image': args.image,
                  'pins': runner.pins, 'runner_settings': runner.settings, 'semantics': ev.SEMANTICS,
                  'seccomp_profile': rel(runner.seccomp_profile), 'seccomp_sha256': seccomp_sha,
                  # Reported by the image's entry (probe record), not hashed from the working tree.
                  'image_entry_sha256': None,
                  'evaluator_py_sha256': ev.evaluator_source_sha256(),
                  'fixture_manifest_sha256': ev.sha256_file(ev.FIXTURE_MANIFEST),
                  'environment': environment(runner.docker), 'cases': [], 'upstream': [], 'self_compare': [],
                  'container_runs': 0, 'notes': 'Researcher-only. Expected verdicts come from code '
                                                '(VALIDATION_EXPECTATIONS); load_validation recomputes passes.'}
    record['orphans_removed'] = orphans
    record.setdefault('interlock', []).append(interlock)
    debug_dir = (debug_root / path.stem) if debug_root else None
    base_runs = runner.invocations

    def checkpoint():
        record['container_runs'] = record.get('container_runs', 0) + runner.invocations - checkpoint.seen
        checkpoint.seen = runner.invocations
        finalize(record, manifest, bool(selected))
        save(record, path)
    checkpoint.seen = base_runs

    log('Record: %s  image: %s' % (rel(path), image_id))
    probe = runner.probe(image_id)
    dump_full(debug_dir, 'probe', probe)
    record['probe'] = {k: probe.get(k) for k in ('status', 'detail', 'probe', 'pid1_signals_ignored_after_probe',
                                                  'preflight', 'host_seconds', 'cgroup_memory_peak_bytes', 'entry',
                                                  'entry_sha256', 'pins')}
    record['probe']['finished_at'] = iso()
    entry_sha256 = probe.get('entry_sha256')
    record['image_entry_sha256'] = entry_sha256
    checkpoint()
    log('probe: %s  entry: %s' % (probe.get('status'), entry_sha256))
    if probe.get('status') != 'probe_passed':
        record['complete'] = True
        checkpoint()
        return 1, path
    if entry_sha256 != ev.sha256_file(ROOT / ev.ENTRY_SOURCE):
        # The gate would reject this record: do not spend hours validating a stale image.
        log('Comparator image entry differs from docker/comparator_entry.py; rebuild the image (docs/evaluator.md, '
            '"Rebuild") and validate again')
        record['complete'] = True
        checkpoint()
        return 1, path

    if any((probe.get('pins') or {}).get(k) != v for k, v in runner.pins.items()):
        log('Comparator image verifier or dependency pins differ; rebuild the image and validate again')
        record['complete'] = True
        checkpoint()
        return 1, path

    current = {c['name']: (c['submission_sha256'], c['template_sha256']) for c in manifest}
    record['cases'] = [c for c in record['cases'] if current.get(c['name']) == (c.get('submission_sha256'),
                                                                                 c.get('template_sha256'))
                       and not (args.rerun_failed and not ev.case_passes(c))]
    done = {c['name'] for c in record['cases']}
    if not args.skip_fixtures:
        for case in manifest:
            if selected and case['name'] not in selected:
                continue
            if case['name'] in done:
                continue
            entry, full = run_case(case, runner, image_id, entry_sha256)
            dump_full(debug_dir, 'case-' + case['name'], full)
            record['cases'].append(entry)
            checkpoint()
            log('case %-40s %-11s statuses=%s passed=%s (%.0f s)' % (
                case['name'], entry['observed_verdict'], entry['statuses'], entry['passed'], entry['wall_seconds']))
        if not args.no_upstream:
            verdicts = {c['name']: c['observed_verdict'] for c in record['cases']}
            finished = {u['name'] for u in record['upstream'] if u.get('status') == 'upstream_exit'
                        and not (args.rerun_failed and not u.get('agrees'))}
            record['upstream'] = [u for u in record['upstream'] if u['name'] in finished]
            for case in manifest:
                if not case.get('upstream') or case['name'] in finished or case['name'] not in verdicts:
                    continue
                if selected and case['name'] not in selected:
                    continue
                entry, full = run_upstream(case, runner, image_id, verdicts[case['name']])
                dump_full(debug_dir, 'upstream-' + case['name'], full)
                record['upstream'].append(entry)
                checkpoint()
                log('upstream %-36s exit=%s agrees=%s' % (case['name'], entry['exit_code'], entry['agrees']))

    if args.pilot_self_compare:
        record['self_compare'] = [e for e in record['self_compare']
                                  if not (args.rerun_failed and not ev.self_compare_passes(e))]
        kept = {(e['task_id'], e['kind'], e['template_sha256']) for e in record['self_compare']}
        record['self_compare_scope'] = {'task_set': getattr(args, 'task_set', PILOT_TASK_SET),
                                        'only_tasks': sorted(only) if only else None}
        for kind, task, template, request in self_compare_requests(dataset, only):
            if (task.task_id, kind, ev._sha256_text(template.source)) in kept:
                continue
            entry, full = run_self_compare(kind, task, template, request, runner, image_id)
            dump_full(debug_dir, 'self-%s-%s' % (kind, task.task_id), {'entry': entry, 'record': full})
            record['self_compare'].append(entry)
            checkpoint()
            log('self-compare %-8s %-48s %s %s passed=%s' % (kind, task.task_id, entry['status'],
                                                            entry['axiom'] or '', entry['passed']))
    record['complete'] = True
    checkpoint()
    summary = record['summary']
    log(json.dumps(summary, indent=1))
    log('all_required_passed=%s  record=%s' % (record['all_required_passed'], rel(path)))
    return (0 if record['all_required_passed'] else 1), path


def main(argv=None):
    code, _ = run(parse_args(argv))
    return code


if __name__ == '__main__':
    sys.exit(main())
