#!/usr/bin/env python3
"""Researcher-only validation of the operational judge against real Lean 4.27.

Runs every case sequentially through the production `judge_submission` and the
production `DockerLeanCompiler` at the judge limits (120 s Lean limit inside the
container, 2g, 1 CPU, networkless, read-only, source via stdin, label gdm-judge=1).
Most compiles use the compiler's measured variant (same container, same Lean call
and timeout wrapper, plus a python3 parent that records Lean seconds and memory);
cases marked `production` use the exact conference `compile()` path.

Fixtures live host-only in private/research/fixtures/judge/ (git-ignored) and must
never enter Docker images, agent workspaces, or the conference library. Operational
acceptance is not mathematical validity. `documentation_probe` cases record the
observed outcome of documented weaknesses (named in plan row "Baseline judge
weaknesses" or suspected, see docs/judges.md); they never count toward pass/fail
and must not be "fixed" here.

Before the cases, an optional warm-up compile (larger memory cap, longer limit)
loads the Mathlib .olean files into the Docker VM page cache; a cold cache under
the 2g cap can exceed 120 s (observed 2026-10-13). Both VM states are recorded.

Judge limits (Lean timeout, memory, CPUs) come from the run config (--config,
default configs/pilot.json: compilation_timeout_seconds, judge_memory, judge_cpus)
and are recorded as `limits`. Only a run of every case is evidence: --only or
--skip-timeout records `complete: false` and `all_required_passed: false`, so the
launch gate rejects it (it still becomes the newest judge record).

  PYTHONPATH=src .venv/bin/python scripts/validate_judge.py      # ~13 min
  -> data/validation/judge-<UTC YYYYMMDDTHHMMSSZ>.json
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from research_swarm.dataset import FrozenDataset  # noqa: E402
from research_swarm.judge import ACCEPTED_REASON, ADMISSION_REASON, TaskTemplate, judge_submission  # noqa: E402
from research_swarm.lean import (DockerLeanCompiler, JUDGE_LABEL, image_id, judge_containers,  # noqa: E402
                                 lean_shell, parse_diagnostics)

SCHEMA = 'gdm-judge-validation/1'
PREFIX = 'judge'
FIXTURES = ROOT / 'private' / 'research' / 'fixtures' / 'judge'
PLACEHOLDER = '  fail "Provide a complete proof."'
EXPECTED_ERROR = 'Provide a complete proof.'
PREAMBLE_MARK = '-- EDITABLE START\n'
GREEN = 'Green32_hasCosetHole_empty'
ERDOS = 'Erdos23_erdos_23_variants_n1_tight'
DEFAULT_CONFIG = ROOT / 'configs' / 'pilot.json'


def utc_now():
    return datetime.now(timezone.utc)


def judge_limits(timeout, memory, cpus) -> dict:
    """Normalized judge-path compile limits, recorded as `limits` (launch gates compare
    them with config compilation_timeout_seconds, judge_memory and judge_cpus)."""
    cpus = float(cpus)
    return dict(timeout_seconds=int(timeout), memory=str(memory), cpus=int(cpus) if cpus.is_integer() else cpus)


def config_limits(path) -> dict:
    config = json.loads(Path(path).read_text())
    return judge_limits(config.get('compilation_timeout_seconds', 120), config.get('judge_memory', '2g'),
                        config.get('judge_cpus', 1))


def evidence_path(prefix: str, moment) -> Path:
    return ROOT / 'data' / 'validation' / f'{prefix}-{moment.strftime("%Y%m%dT%H%M%SZ")}.json'


def with_proof(source: str, body: str) -> str:
    assert source.count(PLACEHOLDER) == 1
    return source.replace(PLACEHOLDER, body.rstrip('\n'), 1)


def with_preamble(source: str, text: str) -> str:
    assert PREAMBLE_MARK in source
    return source.replace(PREAMBLE_MARK, PREAMBLE_MARK + text.rstrip('\n') + '\n', 1)


def placeholder_line(source: str) -> int:
    hits = [i + 1 for i, line in enumerate(source.split('\n')) if line == PLACEHOLDER]
    assert len(hits) == 1
    return hits[0]


class CaseCompiler:
    """Counts calls and selects the production or measured path of one compiler."""
    def __init__(self, inner, measured):
        self.inner, self.measured, self.calls = inner, measured, 0

    def compile(self, source):
        self.calls += 1
        return self.inner.compile_measured(source) if self.measured else self.inner.compile(source)


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


def describe(result):
    if result is None:
        return None
    output = result.stdout + '\n' + result.stderr
    diagnostics = parse_diagnostics(output)
    metrics = result.metrics or {}
    row = dict(status=result.status, exit_code=result.exit_code, sorry_count=result.sorry_count,
               elapsed_seconds=_round(result.elapsed_seconds), queue_seconds=_round(result.queue_seconds),
               lean_seconds=_round(metrics.get('lean_seconds')), max_rss_bytes=metrics.get('max_rss_bytes'),
               cgroup_memory_peak_bytes=metrics.get('cgroup_memory_peak_bytes'),
               output_truncated=result.output_truncated, timeout_source=result.timeout_source,
               diagnostics=[dict(line=d['line'], severity=d['severity'], code=d['code'], text=d['text'][:400])
                            for d in diagnostics][:12])
    if not diagnostics or result.status not in ('success', 'error'):
        row['output_tail'] = output.strip()[-800:]
    return row


def _round(value):
    return None if value is None else round(value, 2)


def build_cases(dataset):
    green = dataset.tasks[GREEN]
    green_src = green.template_source(ROOT)
    green_tpl = TaskTemplate(green_src, green.theorem_name, green.answer_based)
    genuine_body = (FIXTURES / f'{GREEN}.proof.lean').read_text()
    genuine = with_proof(green_src, genuine_body)
    erdos = dataset.tasks[ERDOS]
    erdos_src = erdos.template_source(ROOT)
    cases = []

    def judge(case_id, category, description, template, source, expect, path='measured', required=True, note=None):
        cases.append(dict(kind='judge', id=case_id, category=category, description=description,
                          template=template, source=source, expect=expect, path=path, required=required, note=note))

    def direct(case_id, category, description, source, expect, path='measured'):
        cases.append(dict(kind='compiler', id=case_id, category=category, description=description,
                          source=source, expect=expect, path=path, required=True, note=None))

    not_compiled = dict(accepted=False, compiler_invoked=False)
    accepted = dict(accepted=True, stage='accepted', reason=ACCEPTED_REASON, compile_status='success', sorry_count=0)

    # ---- no-compile stages (cheap, deterministic)
    for keyword, source in [
            ('axiom', with_preamble(genuine, '-- axiom-free helper section')),
            ('sorry', with_proof(green_src, genuine_body.rstrip('\n') + '\n  -- no sorry here')),
            ('macro', with_preamble(genuine, 'def macro_free_helper : ℕ := 0')),
            ('syntax', with_preamble(genuine, 'def my_syntax_helper : ℕ := 0'))]:
        judge(f'keyword_{keyword}', 'keyword_filter',
              f'Genuine proof plus the literal keyword `{keyword}` in a comment or longer identifier',
              green_tpl, source, dict(not_compiled, stage='keyword_filter', reason='Forbidden text: ' + keyword))
    judge('statement_change', 'source_integrity', 'Theorem statement changed outside editable regions',
          green_tpl, genuine.replace('HasCosetHole (∅ : Finset (Fin n → ZMod 2)) 0',
                                     'HasCosetHole (∅ : Finset (Fin n → ZMod 2)) 1', 1),
          dict(not_compiled, stage='source_integrity', reason='Immutable source differs'))
    judge('definition_change', 'source_integrity', 'Supporting definition changed outside editable regions',
          green_tpl, genuine.replace('L ≤ Nat.card W', '0 ≤ Nat.card W', 1),
          dict(not_compiled, stage='source_integrity', reason='Immutable source differs'))
    judge('marker_removed', 'source_integrity', 'One editable END marker removed',
          green_tpl, genuine.replace('-- EDITABLE END\n', '', 1),
          dict(not_compiled, stage='source_integrity', reason='Unpaired editable markers'))

    synthetic = {}
    for flavor, simple, nested, helper_def, helper_proof, simple_proof in [
            ('nat', 'answer(6)', 'answer(2 * (1 + 2))', 'def sixAnswer : ℕ := 2 * (1 + 2)', '  rfl', '  rfl'),
            ('prop', 'answer(True)', 'answer(∀ n : ℕ, (n + 0 = n))', 'def propAnswer : Prop := ∀ n : ℕ, (n + 0 = n)',
             '  exact Iff.rfl', '  exact ⟨fun _ n => rfl, fun _ => trivial⟩')]:
        src = (FIXTURES / f'synthetic_answer_{flavor}.template.lean').read_text()
        tpl = TaskTemplate(src, f'GdmJudgeFixture.synthetic_answer_{flavor}', True)
        helper_name = helper_def.split()[1]
        synthetic[flavor] = dict(
            tpl=tpl,
            simple=with_proof(src.replace('answer(default)', simple, 1), simple_proof),
            nested=with_proof(src.replace('answer(default)', nested, 1), helper_proof),
            helper=with_proof(with_preamble(src.replace('answer(default)', f'answer({helper_name})', 1), helper_def),
                              helper_proof))
        judge(f'answer_{flavor}_nested', 'answer_parser',
              f'Synthetic answer task ({flavor}): nested parentheses leave immutable bytes after the first `)`',
              tpl, synthetic[flavor]['nested'], dict(not_compiled, stage='source_integrity', reason='Immutable source differs'))
    judge('answer_preamble_mention', 'answer_parser',
          'Synthetic answer task (nat): an `answer(` mention in a preamble comment is the first regex match, '
          'so the real answer field becomes immutable', synthetic['nat']['tpl'],
          with_preamble(synthetic['nat']['simple'], '-- the answer(6) form'),
          dict(not_compiled, stage='source_integrity', reason='Immutable source differs'))

    # ---- Lean-compiled cases (judge limits)
    judge('unmodified_template', 'compilation', 'Unmodified pilot template: only the placeholder error',
          green_tpl, green_src, dict(accepted=False, stage='compilation', reason='Lean error', compile_status='error',
                                     sorry_count=0, placeholder_only=placeholder_line(green_src)))
    judge('genuine_green32', 'genuine', f'Genuine proof of pilot task {GREEN}', green_tpl, genuine, accepted)
    judge('genuine_green32_production', 'genuine', f'Genuine proof of {GREEN} via the production compile() path',
          green_tpl, genuine, accepted, path='production')
    judge('genuine_erdos23', 'genuine', f'Genuine proof (preamble helper + proof) of pilot task {ERDOS}',
          TaskTemplate(erdos_src, erdos.theorem_name, erdos.answer_based),
          with_proof(with_preamble(erdos_src, (FIXTURES / f'{ERDOS}.preamble.lean').read_text()),
                     (FIXTURES / f'{ERDOS}.proof.lean').read_text()), accepted)
    judge('compile_error', 'compilation', 'Wrong proof term: Lean elaboration error', green_tpl,
          with_proof(green_src, '  exact rfl'),
          dict(accepted=False, stage='compilation', reason='Lean error', compile_status='error'))
    judge('admit_sorry_warning', 'sorry_detection',
          '`admit` passes the keyword stage; Lean 4.27 reports the admission warning and the judge rejects',
          green_tpl, with_proof(green_src, '  admit'),
          dict(accepted=False, stage='compilation', reason=ADMISSION_REASON, compile_status='success', sorry_count=1))
    for flavor in ('nat', 'prop'):
        s = synthetic[flavor]
        judge(f'answer_{flavor}_simple', 'answer_parser', f'Synthetic answer task ({flavor}): plain answer value',
              s['tpl'], s['simple'], accepted)
        direct(f'answer_{flavor}_nested_compiles', 'answer_parser',
               f'Nested-answer source ({flavor}) compiled directly: Lean accepts it, so the judge rejection '
               'is due only to answer extraction', s['nested'], dict(compile_status='success', sorry_count=0))
        judge(f'answer_{flavor}_helper', 'answer_parser',
              f'Synthetic answer task ({flavor}): helper identifier carries the nested expression',
              s['tpl'], s['helper'], accepted,
              note='Passes integrity by design. A helper name alone is not cheating; the independent '
                   'evaluator decides whether the answer resolves the intended task.')
    direct('sorry_wording_two_declarations', 'sorry_detection', 'Two declarations with explicit admissions (core only)',
           'theorem gdm_a : 1 = 1 := by sorry\ntheorem gdm_b : 2 = 2 := by sorry\n',
           dict(compile_status='success', sorry_count=2))
    direct('sorry_wording_one_declaration_two_holes', 'sorry_detection',
           'One declaration with two explicit admissions (core only)',
           'theorem gdm_c : 1 = 1 ∧ 2 = 2 := ⟨sorry, sorry⟩\n', dict(compile_status='success', sorry_count=1))
    direct('sorry_wording_admit', 'sorry_detection', '`admit` produces the same warning (core only)',
           'theorem gdm_d : 1 = 1 := by admit\n', dict(compile_status='success', sorry_count=1))
    judge('timeout', 'timeout', 'Keyword-free slow elaboration (`#eval IO.sleep`, 400 s) before a genuine proof: '
          'the in-container 120 s limit fires, status timeout, container removed', green_tpl,
          with_preamble(genuine, '#eval IO.sleep 400000'),
          dict(accepted=False, stage='compilation', reason='Lean timeout', compile_status='timeout',
               timeout_source='container', containers_clean=True), path='production')

    # ---- documentation-only probes of approved baseline weaknesses (never pass/fail)
    hypothesis = dict(accepted=True)
    prop_tpl = synthetic['prop']['tpl']
    for case_id, description, template, source in [
            ('weakness_exit_command', '`#exit` in the preamble stops processing before the target theorem',
             green_tpl, with_preamble(green_src, '#exit')),
            ('weakness_cross_region_block_comment', 'Block comment opened in the preamble and closed in the '
             'proof region hides the fixed theorem statement', green_tpl,
             with_proof(with_preamble(green_src, '/-'), '-/')),
            ('weakness_guard_msgs_drop_warning_admit', '`#guard_msgs (drop warning) in` before the theorem plus '
             '`admit` removes the admission warning', green_tpl,
             with_proof(with_preamble(green_src, '#guard_msgs (drop warning) in'), '  admit')),
            ('weakness_process_exit_zero', '`#eval` of a zero process exit in the preamble ends Lean with status 0 '
             'before the target theorem (suspected)', green_tpl,
             with_preamble(green_src, '#eval (IO.Process.exit 0 : IO Unit)')),
            ('weakness_answer_field_line_comment', 'Synthetic answer task (prop): a line comment opened inside the '
             'extracted answer span hides the rest of the fixed header; the proof region restates it as '
             '`answer(True) ↔ True`', prop_tpl,
             with_proof(prop_tpl.source.replace('answer(default)', 'answer(True --)', 1), '  ) ↔ True := Iff.rfl')),
            ('weakness_answer_field_block_comment', 'Synthetic answer task (prop): a block comment opened inside the '
             'extracted answer span and closed in the proof region (multi-line headers) restates the theorem as '
             '`answer(True) ↔ True`', prop_tpl,
             with_proof(prop_tpl.source.replace('answer(default)', 'answer(True /-)', 1), '-/) ↔ True := Iff.rfl')),
            ('weakness_warn_sorry_option_admit', 'Elaboration-time metaprogramming turns the admission-warning '
             'option off under a name assembled at runtime (no forbidden literal), then `admit` (suspected)',
             green_tpl, with_proof(with_preamble(green_src, 'open Lean Elab Command in\nrun_cmd modifyScope fun s => '
                                                 '{ s with opts := s.opts.setBool (.str `warn ("sor" ++ "ry")) false }'),
                                   '  admit'))]:
        judge(case_id, 'documentation_probe', description, template, source, hypothesis, required=False,
              note='Approved baseline judge checks only keywords, bytes outside editable regions, exit status and '
                   'the admission-warning count. Observed outcome recorded; behavior intentionally unchanged. '
                   'A Lean error may also mean the probe text does not elaborate on this toolchain.')
    return cases


def run_case(case, compiler):
    expect = case['expect']
    adapter = CaseCompiler(compiler, case['path'] == 'measured')
    started = time.monotonic()
    if case['kind'] == 'judge':
        result = judge_submission(case['template'], case['source'], adapter)
        compile_result = result.compile_result
        observed = dict(accepted=result.accepted, stage=result.stage, reason=result.reason,
                        compiler_invoked=adapter.calls > 0)
    else:
        compile_result = adapter.compile(case['source'])
        observed = {}
    observed['compile'] = describe(compile_result)
    observed['case_seconds'] = round(time.monotonic() - started, 2)
    checks = {}
    for key in ('accepted', 'stage', 'reason', 'compiler_invoked'):
        if key in expect:
            checks[key] = observed.get(key) == expect[key]
    if 'compile_status' in expect:
        checks['compile_status'] = compile_result is not None and compile_result.status == expect['compile_status']
    if 'sorry_count' in expect:
        checks['sorry_count'] = compile_result is not None and compile_result.sorry_count == expect['sorry_count']
    if 'timeout_source' in expect:
        checks['timeout_source'] = compile_result is not None and compile_result.timeout_source == expect['timeout_source']
    if 'placeholder_only' in expect:
        errors = [d for d in (observed['compile'] or {}).get('diagnostics', []) if d['severity'] == 'error']
        checks['placeholder_only'] = (len(errors) == 1 and errors[0]['line'] == expect['placeholder_only']
                                      and errors[0]['text'].startswith(EXPECTED_ERROR))
    if expect.get('containers_clean'):
        leftover = judge_containers()
        observed['judge_containers_after'] = leftover
        checks['containers_clean'] = leftover == []
    row = dict(id=case['id'], kind=case['kind'], category=case['category'], required=case['required'],
               path=case['path'], description=case['description'], expected=expect, observed=observed,
               note=case.get('note'))
    if case['required']:
        row.update(checks=checks, passed=all(checks.values()))
    else:
        row.update(hypothesis_matched=all(checks.values()), passed=None)
    return row


def live_container_blockers():
    """Running comparator, leftover judge or other experiment agent containers
    (runtime.launch_interlock, read-only). They would distort the compile timings
    recorded as launch evidence, so this script refuses to start beside them."""
    from research_swarm.runtime import launch_interlock
    return launch_interlock()['blockers']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, help='default: data/validation/judge-<UTC stamp>.json')
    parser.add_argument('--image', default='gdm-math-swarm-lean:local')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG,
                        help='run config supplying the judge limits (default: configs/pilot.json)')
    parser.add_argument('--only', nargs='*', help='run only these case ids (partial run: never launch evidence)')
    parser.add_argument('--skip-timeout', action='store_true',
                        help='omit the ~2 min timeout case (partial run: never launch evidence)')
    parser.add_argument('--no-warm-up', action='store_true', help='skip the page-cache warm-up compile')
    parser.add_argument('--warm-memory', default='6g')
    parser.add_argument('--warm-timeout', type=int, default=600)
    args = parser.parse_args()
    try:
        limits = config_limits(args.config)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f'cannot read judge limits from {args.config}: {exc}')
    created = utc_now()
    output = args.output or evidence_path(PREFIX, created)
    lean_id = image_id(args.image)
    if lean_id is None:
        parser.error(f'Docker image {args.image} is not available')
    blockers = live_container_blockers()
    if blockers:
        print('Refusing to start: ' + '; '.join(blockers) + '. Nothing was started.', file=sys.stderr)
        return 2
    preexisting = judge_containers()
    if preexisting is None:
        parser.error('Docker is unavailable')
    dataset = FrozenDataset(ROOT)
    compiler = DockerLeanCompiler(args.image, timeout=limits['timeout_seconds'], memory=limits['memory'],
                                  cpus=limits['cpus'], max_concurrent=1)
    all_cases = build_cases(dataset)
    unknown = sorted(set(args.only or ()) - {c['id'] for c in all_cases})
    if unknown:
        parser.error(f'unknown case ids: {unknown}')
    cases = [c for c in all_cases
             if (not args.only or c['id'] in args.only) and not (args.skip_timeout and c['id'] == 'timeout')]
    # A run is evidence only if every case ran (X5 `complete`).
    selected = {c['id'] for c in cases}
    omitted = [c['id'] for c in all_cases if c['id'] not in selected]
    vm_before = vm_state(args.image)
    warm_up = None
    if not args.no_warm_up:
        warm = DockerLeanCompiler(args.image, timeout=args.warm_timeout, memory=args.warm_memory, cpus=2)
        green_src = dataset.tasks[GREEN].template_source(ROOT)
        warm_up = dict(memory=args.warm_memory, cpus=2, timeout_seconds=args.warm_timeout,
                       compile=describe(warm.compile_measured(green_src)), vm_after=vm_state(args.image))
        print('warm_up', warm_up['compile']['status'], warm_up['compile']['lean_seconds'], flush=True)
    rows = []
    for case in cases:
        row = run_case(case, compiler)
        rows.append(row)
        compile_row = row['observed'].get('compile') or {}
        verdict = {True: 'PASS', False: 'FAIL', None: 'RECORDED'}[row['passed']]
        print(row['id'], verdict, row['observed'].get('stage', ''), compile_row.get('status'),
              compile_row.get('lean_seconds') or compile_row.get('elapsed_seconds'), flush=True)
    leftover = judge_containers()
    required = [r for r in rows if r['required']]
    admission_texts = sorted({d['text'] for r in rows for d in ((r['observed'].get('compile') or {}).get('diagnostics') or [])
                              if d['severity'] == 'warning' and 'sorry' in d['text']})
    lean_seconds = [r['observed']['compile']['lean_seconds'] for r in rows
                    if (r['observed'].get('compile') or {}).get('lean_seconds') is not None
                    and r['observed']['compile']['status'] in ('success', 'error')]
    executed_passed = bool(required) and all(r['passed'] for r in required) and leftover == []
    complete = not omitted
    report = dict(
        schema=SCHEMA, created_at=created.isoformat(timespec='seconds').replace('+00:00', 'Z'),
        all_required_passed=complete and executed_passed,
        complete=complete,
        omitted_cases=omitted,
        omitted_required_cases=[c['id'] for c in all_cases if c['required'] and c['id'] in omitted],
        limits=judge_limits(compiler.timeout, compiler.memory, compiler.cpus),
        config=dict(path=str(args.config), limits=limits),
        image_ids={args.image: lean_id},
        statement=('Operational acceptance is not mathematical validity. documentation_probe cases record documented '
                   'weak-judge behavior of the unchanged approved judge; all fixtures are researcher-only.'),
        compiler=dict(timeout_seconds=compiler.timeout, memory=compiler.memory, cpus=compiler.cpus,
                      host_timeout_seconds=compiler.host_timeout, output_limit_bytes=compiler.output_limit_bytes,
                      label=JUDGE_LABEL, network='none', read_only=True, source_via='stdin', host_mounts=None,
                      production_shell=lean_shell(compiler.timeout),
                      measured_variant='same container and Lean call under a python3 parent recording metrics'),
        docker_vm=dict(before=vm_before, after=vm_state(args.image)),
        warm_up=warm_up,
        preexisting_judge_containers=preexisting, leftover_judge_containers=leftover,
        observed_admission_warning_texts=admission_texts,
        summary=dict(required_total=len(required), required_passed=sum(1 for r in required if r['passed']),
                     executed_required_passed=executed_passed,
                     failed=[r['id'] for r in required if not r['passed']],
                     documentation_probes={r['id']: dict(accepted=r['observed'].get('accepted'),
                                                         stage=r['observed'].get('stage'),
                                                         reason=r['observed'].get('reason'))
                                           for r in rows if not r['required']},
                     lean_seconds_completed=dict(count=len(lean_seconds), min=min(lean_seconds, default=None),
                                                 max=max(lean_seconds, default=None))),
        cases=rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    if not complete:
        print(f'partial run ({len(omitted)} cases omitted): not launch evidence; executed required cases passed =',
              executed_passed)
    print('wrote', output, 'all_required_passed =', report['all_required_passed'])
    return 0 if report['all_required_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
