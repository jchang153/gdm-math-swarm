#!/usr/bin/env python3
"""Researcher-only Lean validation of the frozen templates. Never export outputs to agents.

Lean runs only through the production DockerLeanCompiler's measured variant (same
networkless, read-only container, source via stdin, no host mounts, same in-container
`timeout -s KILL` wrapper) at the operational judge limits unless overridden. Never
writes data/manifest.json, data/pilot_subset.json or data/templates/ (frozen identity).

Modes (default task set: the 14 pilot tasks; --all for the 71-task parent pool)
  declarations  Offline structure checks (judge parser accepts the template, keyword
                clean, single placeholder, answer field outside editable regions,
                namespace-qualified theorem name equals the manifest name), then the
                criterion: the UNMODIFIED template fails with exactly one error, the
                placeholder's `Provide a complete proof.`, and no admission warnings.
                Lean elaborates the whole statement before running the placeholder
                tactic, so this shows the statement and support code elaborate.
                Unless --template-only: a researcher-only strict variant (autoImplicit
                and relaxedAutoImplicit off, proof `sorry`, appended name probe) must
                elaborate with exactly one admission warning on the target and resolve
                the manifest name to it (`lake env lean` does not apply the lakefile's
                autoImplicit=false, so the strict variant rules out auto-bound names).
                Elaboration evidence only; never evidence of a genuine proof.
  timing        Lean seconds, max RSS and cgroup memory peak under the judge limits:
                each workload once ("first"; cold only if the Docker VM page cache is
                cold, which is recorded), an optional warm-up compile with a larger
                memory cap, then --repeats warm runs. Optional --concurrency N burst.
  reuse         Dependency-reuse check (informational; Mathlib reuse is permitted).
                For each target, inside Lean with the pinned imports: (1) constants
                from the imported environment whose type is alpha-equivalent (`==`)
                to the target's elaborated type; (2) for `answer(_) ↔ P` targets,
                imported constants of type exactly P or ¬P; (3) imported constants
                with the same last name component; (4) template-local constants the
                statement uses (a direct dependency match is impossible when any
                exist). Plus a name/shape grep of the pinned sources (--no-grep skips).
                Not checked: defeq/unification matches, generalizations, multi-step
                proofs. No `exact?` search.

Before declarations/reuse, a warm-up compile with a larger memory cap (default 6g,
2 CPUs, 600 s) loads the Mathlib .olean files into the Docker VM page cache: a cold
cache under the 2g judge cap can exceed 120 s (observed 2026-10-13). --no-warm-up
skips it; the result and VM state are recorded either way.

Judge limits default to the run config (--config, default configs/pilot.json:
compilation_timeout_seconds, judge_memory, judge_cpus) and are recorded as `limits`.
Only a complete run at those limits can set all_required_passed: every pilot task
(declarations, reuse) with the strict variant (declarations), or every default
workload with at least the default repeats (timing). --tasks subsets, --template-only,
fewer workloads/repeats or --timeout/--memory/--cpus overrides record
`complete: false` or `limits_match_config: false` and are never launch evidence.

Output: data/validation/<mode>-<UTC YYYYMMDDTHHMMSSZ>.json (or --output). Example:
  PYTHONPATH=src .venv/bin/python scripts/validate_dataset.py declarations
"""
from __future__ import annotations
import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import platform
import re
import shlex
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from research_swarm.dataset import FrozenDataset  # noqa: E402
from research_swarm.judge import answer_span, editable_spans, immutable_segments  # noqa: E402
from research_swarm.keyword_filter import check_keywords  # noqa: E402
from research_swarm.lean import (DockerLeanCompiler, JUDGE_LABEL, SORRY_WARNING, image_id,  # noqa: E402
                                 lean_shell, parse_diagnostics)

PLACEHOLDER = '  fail "Provide a complete proof."'
EXPECTED_ERROR = 'Provide a complete proof.'
MAX_JOBS = 2  # shared Docker host: at most two Lean containers at once
LEAN_IMAGE = 'gdm-math-swarm-lean:local'
DEPS_IMAGE = 'gdm-math-swarm-deps:local'
SCHEMAS = dict(declarations='gdm-declarations-validation/1', timing='gdm-compile-timing/1',
               reuse='gdm-dependency-reuse/1')
STRICT_OPTIONS = ('set_option autoImplicit false', 'set_option relaxedAutoImplicit false')
DEFAULT_CONFIG = ROOT / 'configs' / 'pilot.json'
DEFAULT_WORKLOADS = ('template:Green32_hasCosetHole_empty', 'genuine:Green32_hasCosetHole_empty',
                     'genuine:Erdos23_erdos_23_variants_n1_tight')
DEFAULT_REPEATS = 3
FIXTURES = ROOT / 'research' / 'fixtures' / 'judge'
GENUINE = {  # researcher-only genuine proofs (timing workloads)
    'Green32_hasCosetHole_empty': (None, 'Green32_hasCosetHole_empty.proof.lean'),
    'Erdos23_erdos_23_variants_n1_tight': ('Erdos23_erdos_23_variants_n1_tight.preamble.lean',
                                           'Erdos23_erdos_23_variants_n1_tight.proof.lean'),
}

_print_lock = threading.Lock()


def log(*parts):
    with _print_lock:
        print(*parts, flush=True)


def utc_now():
    return datetime.now(timezone.utc)


def iso(moment) -> str:
    return moment.isoformat(timespec='seconds').replace('+00:00', 'Z')


def evidence_path(prefix: str, moment) -> Path:
    return ROOT / 'data' / 'validation' / f'{prefix}-{moment.strftime("%Y%m%dT%H%M%SZ")}.json'


def judge_limits(timeout, memory, cpus) -> dict:
    """Normalized judge-path compile limits, recorded as `limits` (launch gates compare
    them with config compilation_timeout_seconds, judge_memory and judge_cpus)."""
    cpus = float(cpus)
    return dict(timeout_seconds=int(timeout), memory=str(memory), cpus=int(cpus) if cpus.is_integer() else cpus)


def config_limits(path) -> dict:
    config = json.loads(Path(path).read_text())
    return judge_limits(config.get('compilation_timeout_seconds', 120), config.get('judge_memory', '2g'),
                        config.get('judge_cpus', 1))


# ---------------------------------------------------------------- source helpers
def placeholder_line(source: str) -> int:
    hits = [i + 1 for i, line in enumerate(source.split('\n')) if line == PLACEHOLDER]
    if len(hits) != 1:
        raise ValueError(f'expected exactly one proof placeholder, found {len(hits)}')
    return hits[0]


def theorem_line(source: str) -> int:
    """1-based line of the `theorem`/`lemma` keyword owning the proof placeholder."""
    lines = source.split('\n')
    for index in range(placeholder_line(source) - 1, -1, -1):
        if lines[index].startswith(('theorem ', 'lemma ')):
            return index + 1
    raise ValueError('no theorem keyword before the proof placeholder')


_SCOPE = re.compile(r'^(?:(namespace)\s+(\S+)|(?:noncomputable\s+)?(section)(?:\s+(\S+))?|(end)(?:\s+(\S+))?)\s*$')


def qualified_theorem_name(source: str) -> str:
    """Namespace-qualified name of the target theorem (static, no Lean)."""
    lines = source.split('\n')
    target = theorem_line(source)
    stack = []
    for line in lines[:target - 1]:
        match = _SCOPE.match(line)
        if not match:
            continue
        if match.group(1):
            stack.append(('namespace', match.group(2).split('.')))
        elif match.group(3):
            stack.append(('section', []))
        elif match.group(5):
            if not stack:
                raise ValueError('unbalanced end')
            stack.pop()
    name = re.match(r'(?:theorem|lemma)\s+([^\s:({\[]+)', lines[target - 1]).group(1)
    if name.startswith('_root_.'):
        return name[len('_root_.'):]
    return '.'.join([part for _, parts in stack for part in parts] + [name])


def static_checks(task, source) -> dict:
    """Offline checks with the judge's own parser; fatal problems skip Lean."""
    problems, fatal = [], False
    info = {}
    try:
        info['placeholder_line'] = placeholder_line(source)
        info['theorem_line'] = theorem_line(source)
        info['qualified_name'] = qualified_theorem_name(source)
        if info['qualified_name'] != task.theorem_name:
            problems.append(f"qualified theorem name {info['qualified_name']} != manifest {task.theorem_name}")
    except (ValueError, AttributeError) as exc:
        problems.append(f'template structure: {exc}')
        fatal = True
    try:
        spans = editable_spans(source)
        immutable_segments(source, task.answer_based)
        if task.answer_based:
            a, b = answer_span(source)
            info['answer_field'] = source[a:b]
            if any(x <= a and b <= y for x, y in spans):
                problems.append('answer field lies inside an editable region')
            if len(re.findall(r'\banswer\s*\(', source)) != 1:
                problems.append('answer-based template must contain exactly one answer( occurrence')
            if spans[0][1] > a:
                problems.append('answer field precedes the end of the preamble region')
    except ValueError as exc:
        problems.append(f'judge parser: {exc}')
        fatal = True
    matched = check_keywords(source).matched_keywords
    if matched:
        problems.append(f'template contains forbidden keywords {list(matched)}')
    return dict(info, problems=problems, fatal=fatal)


def strict_admitted_variant(source: str) -> tuple[str, int]:
    """Researcher-only: implicit-binding options after the imports, proof `sorry`.

    Returns the source and the line offset introduced by the inserted options.
    """
    lines = source.split('\n')
    last_import = max(i for i, line in enumerate(lines) if line.startswith('import '))
    lines[last_import + 1:last_import + 1] = list(STRICT_OPTIONS)
    return '\n'.join(lines).replace(PLACEHOLDER, '  sorry', 1), len(STRICT_OPTIONS)


def name_probe(theorem_name: str) -> str:
    return (
        '\n-- researcher-only declaration-identity probe\n'
        'run_cmd do\n'
        f'  let n : Lean.Name := `{theorem_name}\n'
        '  match (← Lean.getEnv).find? n with\n'
        '  | some (.thmInfo _) => Lean.logInfo m!"GDM_NAME_PROBE theorem {n}"\n'
        '  | some _ => Lean.throwError m!"GDM_NAME_PROBE not_a_theorem {n}"\n'
        '  | none => Lean.throwError m!"GDM_NAME_PROBE unresolved {n}"\n'
        f'\n#check @{theorem_name}\n'
        f'#print axioms {theorem_name}\n')


REUSE_MARK = 'GDM_REUSE '


def reuse_probe(theorem_name: str) -> str:
    """Researcher-only Lean probe over the imported environment (see module doc)."""
    return f'''
-- researcher-only dependency-reuse probe
open Lean in
def gdmReuseLast : Name → String
  | .str _ s => s
  | .num p _ => gdmReuseLast p
  | .anonymous => ""

open Lean in
run_cmd do
  let env ← getEnv
  let target : Name := `{theorem_name}
  let some info := env.find? target
    | throwError "GDM_REUSE_UNRESOLVED"
  let ty := info.type
  let localConsts := ty.getUsedConstants.filter fun c => (env.getModuleIdxFor? c).isNone
  let conj : Option Expr := match ty.consumeMData.getAppFnArgs with
    | (``Iff, #[_, p]) => some p
    | _ => none
  let negConj := conj.map mkNot
  let last := gdmReuseLast target
  let step := fun (acc : Array Name × Array Name × Array Name) (n : Name) (c : ConstantInfo) =>
    if (env.getModuleIdxFor? n).isNone then acc
    else
      let a := if c.type == ty then acc.1.push n else acc.1
      let b := if some c.type == conj || some c.type == negConj then acc.2.1.push n else acc.2.1
      let d := if gdmReuseLast n == last then acc.2.2.push n else acc.2.2
      (a, b, d)
  let (sameType, conjecture, sameName) := env.constants.fold step (#[], #[], #[])
  let names := fun (xs : Array Name) => toJson ((xs.toList.take 50).map toString)
  let payload := (Json.mkObj [
    ("target", toJson (toString target)),
    ("template_local_constants", names localConsts),
    ("has_answer_iff", toJson conj.isSome),
    ("exact_type_matches", names sameType),
    ("conjecture_or_negation_matches", names conjecture),
    ("same_last_component", names sameName),
    ("same_last_component_count", toJson sameName.size)]).compress
  logInfo m!"{REUSE_MARK}{{payload}}"
'''


def output_lines(result) -> list[str]:
    """Lean 4.27 prints info messages (#check, #print, logInfo) WITHOUT the
    `file:line:col: info:` prefix that errors and warnings carry (observed), so
    probe results are read from the raw output lines."""
    return (result.stdout + '\n' + result.stderr).split('\n')


def parse_reuse(lines) -> dict | None:
    for line in lines:
        if line.startswith(REUSE_MARK):
            try:
                return json.loads(line[len(REUSE_MARK):])
            except ValueError:
                return None
    return None


def genuine_source(dataset, task_id) -> str:
    preamble, proof = GENUINE[task_id]
    source = dataset.tasks[task_id].template_source(ROOT)
    if preamble:
        mark = '-- EDITABLE START\n'
        source = source.replace(mark, mark + (FIXTURES / preamble).read_text().rstrip('\n') + '\n', 1)
    return source.replace(PLACEHOLDER, (FIXTURES / proof).read_text().rstrip('\n'), 1)


# ---------------------------------------------------------------- reporting helpers
def _clip(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[:limit] + f'... [{len(text) - limit} more chars]'


def _round(value):
    return None if value is None else round(value, 2)


def summarize(result) -> dict:
    diagnostics = parse_diagnostics(result.stdout + '\n' + result.stderr)
    metrics = result.metrics or {}
    row = dict(
        status=result.status, exit_code=result.exit_code, sorry_warnings=result.sorry_count,
        elapsed_seconds=_round(result.elapsed_seconds), queue_seconds=_round(result.queue_seconds),
        lean_seconds=_round(metrics.get('lean_seconds')),
        lean_cpu_seconds=(round(metrics['user_seconds'] + metrics['system_seconds'], 2)
                          if metrics.get('user_seconds') is not None else None),
        max_rss_bytes=metrics.get('max_rss_bytes'), cgroup_memory_peak_bytes=metrics.get('cgroup_memory_peak_bytes'),
        output_truncated=result.output_truncated, timeout_source=result.timeout_source,
        errors=[dict(line=d['line'], code=d['code'], text=_clip(d['text'])) for d in diagnostics if d['severity'] == 'error'],
        warnings=[dict(line=d['line'], code=d['code'], text=_clip(d['text'], 300)) for d in diagnostics if d['severity'] == 'warning'],
    )
    if result.status in ('timeout', 'setup_error', 'killed') or (not diagnostics and result.exit_code):
        row['raw_tail'] = _clip((result.stdout + '\n' + result.stderr)[-2000:], 2000)
    return row


def vm_state(image: str) -> dict | None:
    """Docker VM uptime and page cache, read from a trivial container (no Lean)."""
    try:
        out = subprocess.run(['docker', 'run', '--rm', '--pull', 'never', '--log-driver', 'none',
                              '--network', 'none', image, 'sh', '-c', 'cat /proc/uptime /proc/meminfo'],
                             capture_output=True, encoding='utf-8', timeout=60, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = out.splitlines()
    if not lines:
        return None
    info = {'vm_uptime_seconds': float(lines[0].split()[0])}
    for line in lines[1:]:
        key, _, value = line.partition(':')
        if key in ('MemTotal', 'MemAvailable', 'Cached', 'Active(file)', 'Inactive(file)'):
            info[key + '_kib'] = int(value.split()[0])
    return info


def environment(args) -> dict:
    try:
        docker = subprocess.run(['docker', 'info', '--format', '{{.NCPU}} {{.MemTotal}} {{.ServerVersion}}'],
                                capture_output=True, encoding='utf-8', timeout=30, check=False).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        docker = []
    return dict(
        host=dict(platform=platform.platform(), python=platform.python_version()),
        docker=dict(zip(('ncpu', 'mem_total_bytes', 'server_version'), docker)),
        lean_image=dict(tag=args.image, id=image_id(args.image)),
        dependency_image=dict(tag=DEPS_IMAGE, id=image_id(DEPS_IMAGE)),
        container=dict(network='none', read_only=True, cap_drop='ALL', no_new_privileges=True, pids_limit=128,
                       memory=args.memory, cpus=args.cpus, timeout_seconds=args.timeout, label=JUDGE_LABEL,
                       source_via='stdin', host_mounts=None, tmpfs='/tmp size=128m',
                       production_shell=lean_shell(args.timeout),
                       measured_variant='same container and Lean call under a python3 parent recording metrics'),
        jobs=args.jobs)


def select_tasks(dataset, args):
    pilot = dataset.subset['task_ids']
    if args.tasks:
        ids = args.tasks
    elif args.all:
        ids = pilot + sorted(t for t in dataset.tasks if t not in pilot)
    else:
        ids = pilot
    unknown = [t for t in ids if t not in dataset.tasks]
    if unknown:
        raise SystemExit(f'unknown task ids: {unknown}')
    return [dataset.tasks[t] for t in ids]


def run_parallel(function, items, jobs):
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(function, items))


def warm_up(args, dataset, label='warm_up') -> dict:
    """Load the Mathlib .olean files into the Docker VM page cache with a larger
    memory cap (a cold cache under the 2g judge cap can exceed 120 s)."""
    warm = DockerLeanCompiler(args.image, timeout=args.warm_timeout, memory=args.warm_memory, cpus=2)
    row = summarize(warm.compile_measured(dataset.tasks['Green32_hasCosetHole_empty'].template_source(ROOT)))
    row.update(memory=args.warm_memory, cpus=2, timeout_seconds=args.warm_timeout)
    log(label, row['status'], row['lean_seconds'])
    return row


def report_base(args, mode, created, extra_images=()) -> dict:
    images = {args.image: image_id(args.image)}
    for tag in extra_images:
        images[tag] = image_id(tag)
    limits = judge_limits(args.timeout, args.memory, args.cpus)
    return dict(schema=SCHEMAS[mode], created_at=iso(created), all_required_passed=False,
                image_ids={k: v for k, v in images.items() if v},
                complete=False, incomplete_reasons=[], limits=limits,
                config=dict(path=str(args.config), limits=args.config_limits),
                limits_match_config=limits == args.config_limits)


def gate_fields(report, incomplete, passed) -> dict:
    """X5 fields: only a complete run at the configured judge limits can pass."""
    complete = not incomplete
    return dict(complete=complete, incomplete_reasons=incomplete,
                all_required_passed=complete and report['limits_match_config'] and passed)


def missing_pilot_tasks(dataset, task_ids) -> list:
    ran = set(task_ids)
    return [t for t in dataset.subset['task_ids'] if t not in ran]


# ---------------------------------------------------------------- declarations
def validate_declaration(compiler, task, source, strict=True) -> dict:
    row = dict(task_id=task.task_id, theorem_name=task.theorem_name, category=task.category,
               answer_based=task.answer_based, template_sha256=task.sha256)
    static = static_checks(task, source)
    problems = list(static.pop('problems'))
    row['static'] = static
    if static['fatal']:
        row.update(status='defect', problems=problems)
        return row

    template = summarize(compiler.compile_measured(source))
    errors = template['errors']
    template['expected_failure_only'] = (
        template['status'] == 'error' and len(errors) == 1 and errors[0]['line'] == static['placeholder_line']
        and errors[0]['text'].startswith(EXPECTED_ERROR) and template['sorry_warnings'] == 0)
    if not template['expected_failure_only']:
        problems.append('template: did not fail only with the expected placeholder error')
    row['template_check'] = template
    statuses = {template['status']}

    if strict:
        variant, offset = strict_admitted_variant(source)
        decl_line = static['theorem_line'] + offset
        result = compiler.compile_measured(variant + name_probe(task.theorem_name))
        admitted, lines = summarize(result), output_lines(result)
        sorry_lines = [w['line'] for w in admitted['warnings'] if SORRY_WARNING.search(w['text'])]
        admitted.update(admission_warning_lines=sorry_lines, target_line=decl_line,
                        exactly_one_admission_on_target=(admitted['status'] == 'success' and not admitted['errors']
                                                         and admitted['sorry_warnings'] == 1 and sorry_lines == [decl_line]))
        if not admitted['exactly_one_admission_on_target']:
            problems.append('strict admitted variant: errors (e.g. auto-bound names) or not exactly one '
                            'admission warning on the target')
        # The probe logs this line only when `find?` resolved the manifest name to a theorem.
        resolved = any(line.startswith('GDM_NAME_PROBE theorem ') for line in lines)
        axioms_text = next((line for line in lines if 'depends on axioms' in line), None)
        row['name_probe'] = dict(resolved_theorem=resolved,
                                 check_output=next((_clip(line, 1500) for line in lines
                                                    if line.startswith(task.theorem_name + ' :')), None),
                                 axioms_output=axioms_text,
                                 target_carries_admission=bool(axioms_text and 'sorryAx' in axioms_text))
        if not (resolved and row['name_probe']['target_carries_admission']):
            problems.append('name probe: manifest theorem_name did not resolve to the admitted theorem')
        row['strict_admitted_check'] = admitted
        statuses.add(admitted['status'])

    if 'timeout' in statuses:
        row['status'] = 'timeout'
    elif statuses & {'setup_error', 'killed'}:
        row['status'] = 'infrastructure_error'
    else:
        row['status'] = 'validated' if not problems else 'defect'
    row['problems'] = problems
    return row


def mode_declarations(args, dataset, compiler, created):
    pilot = set(dataset.subset['task_ids'])

    def one(task):
        row = validate_declaration(compiler, task, task.template_source(ROOT), strict=not args.template_only)
        row['in_pilot'] = task.task_id in pilot
        log(task.task_id, row['status'], (row.get('template_check') or {}).get('lean_seconds'),
            (row.get('strict_admitted_check') or {}).get('lean_seconds'), '; '.join(row['problems']))
        return row

    vm_before = vm_state(args.image)
    rows = run_parallel(one, select_tasks(dataset, args), args.jobs)
    counts = {}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    report = report_base(args, 'declarations', created, (DEPS_IMAGE,))
    missing = missing_pilot_tasks(dataset, [r['task_id'] for r in rows])
    incomplete = ([f'pilot tasks not run: {", ".join(missing)}'] if missing else []) + (
        ['strict variant skipped (--template-only)'] if args.template_only else [])
    report.update(
        **gate_fields(report, incomplete, bool(rows) and all(r['status'] == 'validated' for r in rows)),
        strict_variant=not args.template_only, missing_pilot_tasks=missing,
        description=('Criterion: the unmodified template fails with exactly one error, the placeholder '
                     f'"{EXPECTED_ERROR}", and no admission warnings (the statement and support code elaborate). '
                     + ('Strict variant (autoImplicit off, proof sorry, name probe) also required. '
                        if not args.template_only else 'Strict variant skipped (--template-only). ')
                     + 'Researcher-only variants are never published. Elaboration evidence only.'),
        upstream_revision=dataset.manifest['upstream_revision'],
        manifest_sha256=dataset.subset['parent_manifest_sha256'],
        pilot_task_ids=dataset.subset['task_ids'],
        task_scope='all' if args.all else ('custom' if args.tasks else 'pilot'),
        environment=environment(args), docker_vm=dict(before=vm_before, after=vm_state(args.image)),
        summary=dict(counts=counts,
                     validated_pilot=sum(1 for r in rows if r['in_pilot'] and r['status'] == 'validated'),
                     non_validated=[r['task_id'] for r in rows if r['status'] != 'validated'],
                     lean_seconds_template=_stats([(r.get('template_check') or {}).get('lean_seconds') for r in rows])),
        results=rows)
    return report


# ---------------------------------------------------------------- timing
def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return dict(count=len(values), min=min(values), median=round(statistics.median(values), 2), max=max(values))


def mode_timing(args, dataset, compiler, created):
    workloads = []
    for spec in args.workloads:
        kind, _, task_id = spec.partition(':')
        task = dataset.tasks[task_id]
        if kind == 'template':
            source = task.template_source(ROOT)
            expect = dict(status='error', placeholder_line=placeholder_line(source))
        elif kind == 'genuine' and task_id in GENUINE:
            source, expect = genuine_source(dataset, task_id), dict(status='success', sorry_warnings=0)
        else:
            raise SystemExit(f'unknown workload {spec} (template:<task> or genuine:<{"|".join(GENUINE)}>)')
        workloads.append(dict(id=spec, task_id=task_id, kind=kind, sha256_of_template=task.sha256,
                              source=source, expect=expect))
    runs = []

    def record(label, workload, result, concurrency=1):
        row = summarize(result)
        row.pop('warnings', None)
        expect = workload['expect']
        ok = row['status'] == expect['status'] and (
            expect['status'] != 'error' or (len(row['errors']) == 1 and row['errors'][0]['line'] == expect['placeholder_line']
                                            and row['errors'][0]['text'].startswith(EXPECTED_ERROR)))
        if expect['status'] == 'success':
            ok = ok and row['sorry_warnings'] == 0
        row.update(label=label, workload=workload['id'], concurrency=concurrency, expected_outcome=ok,
                   errors=len(row['errors']))
        runs.append(row)
        log(label, workload['id'], row['status'], row['lean_seconds'], row['max_rss_bytes'],
            row['cgroup_memory_peak_bytes'])

    vm = dict(before=vm_state(args.image))
    for workload in workloads:
        record('first', workload, compiler.compile_measured(workload['source']))
    warm_up_row = None if args.no_warm_up else warm_up(args, dataset)
    vm['after_warm_up'] = vm_state(args.image)
    for i in range(args.repeats):
        for workload in workloads:
            record('warm', workload, compiler.compile_measured(workload['source']))
    if args.concurrency:
        burst = DockerLeanCompiler(args.image, timeout=args.timeout, memory=args.memory, cpus=args.cpus,
                                   max_concurrent=args.concurrency)
        source = workloads[0]['source']
        for result in run_parallel(lambda _: burst.compile_measured(source), range(args.concurrency), args.concurrency):
            record('concurrent', workloads[0], result, args.concurrency)
    vm['after'] = vm_state(args.image)

    summary = {}
    for workload in workloads:
        mine = [r for r in runs if r['workload'] == workload['id']]
        first = next((r for r in mine if r['label'] == 'first'), None)
        warm = [r for r in mine if r['label'] == 'warm']
        summary[workload['id']] = dict(
            first=dict(status=first['status'], lean_seconds=first['lean_seconds'],
                       cgroup_memory_peak_bytes=first['cgroup_memory_peak_bytes']) if first else None,
            warm_lean_seconds=_stats([r['lean_seconds'] for r in warm]),
            warm_elapsed_seconds=_stats([r['elapsed_seconds'] for r in warm]),
            warm_max_rss_bytes=max((r['max_rss_bytes'] or 0 for r in warm), default=None),
            warm_cgroup_peak_bytes=max((r['cgroup_memory_peak_bytes'] or 0 for r in warm), default=None),
            warm_all_expected=bool(warm) and all(r['expected_outcome'] for r in warm),
            concurrent=_stats([r['lean_seconds'] for r in mine if r['label'] == 'concurrent']))
    report = report_base(args, 'timing', created)
    ran = {workload['id'] for workload in workloads}
    missing = [w for w in DEFAULT_WORKLOADS if w not in ran]
    incomplete = ([f'default workloads not run: {", ".join(missing)}'] if missing else []) + (
        [f'--repeats {args.repeats} < {DEFAULT_REPEATS}'] if args.repeats < DEFAULT_REPEATS else [])
    report.update(
        # Required: every warm run reached its expected outcome inside the limit. First
        # (possibly cold) runs and concurrent bursts are measurements, not requirements.
        **gate_fields(report, incomplete, bool(workloads) and all(s['warm_all_expected'] for s in summary.values())),
        missing_default_workloads=missing, repeats=args.repeats,
        note=('"first" runs are cold only if docker_vm.before shows a recently started VM with little page cache; '
              'the script never drops caches. The warm-up uses a larger memory cap so the Mathlib .olean files '
              'become resident; later runs at the judge cap then measure the warm case. elapsed_seconds includes '
              'container start/stop; lean_seconds covers `timeout ... lake env lean` only.'),
        operator_asserted_cold_start=args.cold_start, environment=environment(args), docker_vm=vm,
        warm_up=warm_up_row, workloads=[{k: v for k, v in w.items() if k != 'source'} for w in workloads],
        summary=summary, runs=runs)
    return report


# ---------------------------------------------------------------- dependency reuse
# Name/shape search terms for pinned dependency sources (regex, grep -E syntax).
# Template-local definitions cannot occur in dependencies; terms target the
# dependency-level concepts each statement is built from.
REUSE_TERMS = {
    'erdos_18a': ['IsPractical', 'practicalH', 'subsetSums'],
    'Erdos6_erdos_6_variants_decreasing': ['primeGap'],
    'MagicSquares_exists_semi_magic_square_cubes': ['[Mm]agic', 'Injective2'],
    'erdos_400_part_i': ['erdos_400', 'IsEquivalent.*log'],
    'Erdos195_erdos_195_variants_leq_5_bound': ['HasMonotoneAP'],
    'all_fermat_squarefree': ['fermatNumber', 'Squarefree.*fermat|fermat.*Squarefree'],
    'Erdos1092_f_asymptotic_2': ['deleteEdges.*chromaticNumber|chromaticNumber.*deleteEdges'],
    'EulerBrick_cuboidThree': ['[Cc]uboid', 'EulerBrick'],
    'greens14': ['IsAPOfLength', '[Vv]an ?[Dd]er ?[Ww]aerden'],
    'Green32_hasCosetHole_empty': ['CosetHole'],
    'Erdos1085_erdos_1085_variants_upper_d3': ['unitDistNum', '[Uu]nit[- ]?[Dd]istance'],
    "DedekindNumber_M'_three": ['[Dd]edekind[ _]?[Nn]umber', 'IsSperner|Sperner'],
    'Erdos23_erdos_23_variants_n1_tight': ['cycleGraph.*(CliqueFree|IsBipartite|Colorable)',
                                           '(CliqueFree|IsBipartite).*cycleGraph'],
    'SumOfThreeCubes_isSumOfThreeCubes_iff_mod_9': ['[Tt]hree ?[Cc]ubes', 'SumOfThreeCubes'],
}
SEARCH_ROOTS = ['.lake/packages/mathlib/Mathlib', 'FormalConjecturesForMathlib']
DECL = r'^\s*(@\[[^]]*\]\s*)?(private |protected |noncomputable )*(theorem|lemma|def|abbrev|structure|class|instance|notation)\b'


def source_search(args, tasks):
    """grep inside a networkless container of the lean image; no host mounts, no Lean."""
    script = ['cd /opt/lean-project']
    for task in tasks:
        for term in REUSE_TERMS.get(task.task_id, []):
            marker = json.dumps(dict(task=task.task_id, term=term))
            script.append(f"echo '@@TERM {marker}'")
            script.append('grep -rnE --include=*.lean ' + shlex.quote(term) + ' ' + ' '.join(SEARCH_ROOTS) + ' || true')
    cmd = ['docker', 'run', '--rm', '--pull', 'never', '--log-driver', 'none', '--label', JUDGE_LABEL,
           '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
           '--memory', '1g', '--cpus', '1', '-i', args.image, 'sh', '-c', '\n'.join(script)]
    out = subprocess.run(cmd, capture_output=True, encoding='utf-8', errors='replace', timeout=600, check=True).stdout
    results, current = {}, None
    for line in out.splitlines():
        if line.startswith('@@TERM '):
            current = json.loads(line[7:])
            results.setdefault(current['task'], []).append(dict(term=current['term'], hits=0, declaration_hits=[]))
            continue
        if current is None or not line.strip():
            continue
        entry = results[current['task']][-1]
        entry['hits'] += 1
        path, _, rest = line.partition(':')
        lineno, _, text = rest.partition(':')
        if re.match(DECL, text) and len(entry['declaration_hits']) < 12:
            entry['declaration_hits'].append(f'{path}:{lineno}: {text.strip()[:220]}')
    return results


def mode_reuse(args, dataset, compiler, created):
    tasks = select_tasks(dataset, args)
    search = None
    if not args.no_grep:
        log('source search ...')
        search = source_search(args, tasks)

    def one(task):
        source = task.template_source(ROOT).replace(PLACEHOLDER, '  sorry', 1)
        result = compiler.compile_measured(source + reuse_probe(task.theorem_name))
        row, probe = summarize(result), parse_reuse(output_lines(result))
        row.pop('warnings', None)
        log(task.task_id, row['status'], row['lean_seconds'], None if probe is None else
            (len(probe['exact_type_matches']), len(probe['conjecture_or_negation_matches'])))
        return task, row, probe

    results = []
    for task, row, probe in run_parallel(one, tasks, args.jobs):
        results.append(dict(
            task_id=task.task_id, theorem_name=task.theorem_name, category=task.category,
            answer_based=task.answer_based, probe_completed=probe is not None,
            directly_matched_in_dependencies=(None if probe is None else
                                              bool(probe['exact_type_matches'] or probe['conjecture_or_negation_matches'])),
            probe=probe, compile=row, source_search=None if search is None else search.get(task.task_id, [])))
    report = report_base(args, 'reuse', created)
    missing = missing_pilot_tasks(dataset, [r['task_id'] for r in results])
    report.update(
        **gate_fields(report, [f'pilot tasks not run: {", ".join(missing)}'] if missing else [],
                      bool(results) and all(r['probe_completed'] for r in results)),
        missing_pilot_tasks=missing,
        description=('Informational dependency-reuse check; standard Mathlib reuse is permitted. Exact '
                     'alpha-equivalent type matches against every imported constant; for answer(_) ↔ P targets, '
                     'imported constants of type exactly P or ¬P; same last name component; template-local '
                     'constants used by the statement (a direct dependency match is impossible when any exist). '
                     'Does not detect matches up to definitional unfolding, unification or generalization, nor '
                     'multi-step proofs; absence of a match is not evidence that a target is hard. '
                     'Grep hits are name/shape hints only.'),
        search_roots=SEARCH_ROOTS, environment=environment(args), results=results)
    return report


def live_container_blockers():
    """Running comparator, leftover judge or other experiment agent containers
    (runtime.launch_interlock, read-only). They would distort the compile timings
    recorded as launch evidence, so this script refuses to start beside them."""
    from research_swarm.runtime import launch_interlock
    return launch_interlock()['blockers']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=['declarations', 'timing', 'reuse'])
    parser.add_argument('--output', type=Path, help='default: data/validation/<mode>-<UTC stamp>.json')
    parser.add_argument('--image', default=LEAN_IMAGE)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG,
                        help='run config supplying the judge limits (default: configs/pilot.json)')
    parser.add_argument('--all', action='store_true', help='all 71 parent tasks (default: pilot 14)')
    parser.add_argument('--tasks', nargs='*', help='task subset (never launch evidence unless it covers the pilot)')
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--timeout', type=int, help='default: config compilation_timeout_seconds')
    parser.add_argument('--memory', help='default: config judge_memory')
    parser.add_argument('--cpus', help='default: config judge_cpus')
    parser.add_argument('--template-only', action='store_true',
                        help='declarations: skip the strict variant (never launch evidence)')
    parser.add_argument('--workloads', nargs='*', default=list(DEFAULT_WORKLOADS))
    parser.add_argument('--repeats', type=int, default=DEFAULT_REPEATS)
    parser.add_argument('--no-warm-up', action='store_true')
    parser.add_argument('--warm-memory', default='6g')
    parser.add_argument('--warm-timeout', type=int, default=600)
    parser.add_argument('--concurrency', type=int, default=0, help='timing: optional N-way burst (heavy)')
    parser.add_argument('--cold-start', action='store_true',
                        help='timing: operator asserts Docker was restarted just before this run')
    parser.add_argument('--no-grep', action='store_true', help='reuse: skip the source grep')
    args = parser.parse_args()
    if not 1 <= args.jobs <= MAX_JOBS:
        parser.error(f'--jobs must be between 1 and {MAX_JOBS} on the shared Docker host')
    if not 0 <= args.concurrency <= 8:
        parser.error('--concurrency must be between 0 and 8')
    try:
        args.config_limits = config_limits(args.config)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f'cannot read judge limits from {args.config}: {exc}')
    for attr, key in (('timeout', 'timeout_seconds'), ('memory', 'memory'), ('cpus', 'cpus')):
        if getattr(args, attr) is None:
            setattr(args, attr, args.config_limits[key])
    try:
        judge_limits(args.timeout, args.memory, args.cpus)
    except ValueError as exc:
        parser.error(f'invalid judge limits: {exc}')
    if image_id(args.image) is None:
        parser.error(f'Docker image {args.image} is not available')
    blockers = live_container_blockers()
    if blockers:
        print('Refusing to start: ' + '; '.join(blockers) + '. Nothing was started.', file=sys.stderr)
        return 2
    created = utc_now()
    output = args.output or evidence_path(args.mode, created)
    dataset = FrozenDataset(ROOT)
    compiler = DockerLeanCompiler(args.image, timeout=args.timeout, memory=args.memory, cpus=args.cpus,
                                  max_concurrent=args.jobs)
    started = time.monotonic()
    # timing orders its own warm-up after the first (possibly cold) runs.
    warm_up_row = None if args.mode == 'timing' or args.no_warm_up else warm_up(args, dataset)
    report = dict(declarations=mode_declarations, timing=mode_timing, reuse=mode_reuse)[args.mode](
        args, dataset, compiler, created)
    if args.mode != 'timing':
        report['warm_up'] = warm_up_row
    report['duration_seconds'] = round(time.monotonic() - started, 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    if not report['complete'] or not report['limits_match_config']:
        log('not launch evidence:', '; '.join(report['incomplete_reasons']
                                               + ([] if report['limits_match_config'] else
                                                  [f"limits {report['limits']} differ from config {args.config_limits}"])))
    log('wrote', output, 'all_required_passed =', report['all_required_passed'])
    return 0 if report['all_required_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
