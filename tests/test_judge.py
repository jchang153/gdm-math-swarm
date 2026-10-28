"""Offline judge control-flow and parser checks (fake compilers; not Lean semantics)."""
import unittest
from research_swarm.judge import (ACCEPTED_REASON, ADMISSION_REASON, TaskTemplate, answer_span,
                                  immutable_segments, judge_submission)
from research_swarm.lean import CompileResult
from research.analysis.private_material import skip_decorator as needs_private  # noqa: E402

SOURCE = '''import Mathlib
-- EDITABLE START
-- EDITABLE END
theorem target : True := by
-- EDITABLE START
  trivial
-- EDITABLE END
'''
ANSWER_SOURCE = SOURCE.replace(': True', ': answer(False) ↔ True')


class Compiler:
    def __init__(self, result=CompileResult('success', 0, sorry_count=0)):
        self.result = result
        self.calls = 0

    def compile(self, source):
        self.calls += 1
        return self.result


def preamble(source, text):
    return source.replace('-- EDITABLE START\n', '-- EDITABLE START\n' + text + '\n', 1)


class JudgeTests(unittest.TestCase):
    def test_genuine_requires_compile_and_neutral_acceptance_text(self):
        c = Compiler()
        r = judge_submission(TaskTemplate(SOURCE, 'target'), SOURCE, c)
        self.assertTrue(r.accepted)
        self.assertEqual((r.stage, r.reason), ('accepted', 'Submission accepted.'))
        self.assertEqual(ACCEPTED_REASON, 'Submission accepted.')
        self.assertEqual(c.calls, 1)

    def test_changed_statement(self):
        c = Compiler()
        r = judge_submission(TaskTemplate(SOURCE, 'target'), SOURCE.replace(': True', ': False'), c)
        self.assertEqual((r.stage, r.reason), ('source_integrity', 'Immutable source differs'))
        self.assertEqual(c.calls, 0)

    def test_keywords_reject_before_compilation(self):
        for keyword in ('axiom', 'sorry', 'macro', 'syntax'):
            with self.subTest(keyword=keyword):
                c = Compiler()
                r = judge_submission(TaskTemplate(SOURCE, 'target'), preamble(SOURCE, f'-- {keyword}'), c)
                self.assertEqual((r.accepted, r.stage, r.reason), (False, 'keyword_filter', 'Forbidden text: ' + keyword))
                self.assertEqual(c.calls, 0)

    def test_notation_is_intentionally_not_filtered(self):
        s = preamble(SOURCE, 'local notation "True" => False')
        self.assertTrue(judge_submission(TaskTemplate(SOURCE, 'target'), s, Compiler()).accepted)

    def test_keyword_pass_is_not_acceptance(self):
        reasons = {'error': 'Lean error', 'timeout': 'Lean timeout', 'setup_error': 'Lean setup_error',
                   'killed': 'Lean killed'}
        for result in [CompileResult('error', 1), CompileResult('timeout'), CompileResult('setup_error'),
                       CompileResult('killed', 137), CompileResult('success', 0, sorry_count=1),
                       CompileResult('success', 0)]:
            with self.subTest(result=result):
                r = judge_submission(TaskTemplate(SOURCE, 'target'), SOURCE, Compiler(result))
                self.assertFalse(r.accepted)
                self.assertEqual(r.stage, 'compilation')
                self.assertEqual(r.reason, reasons.get(result.status, ADMISSION_REASON))
                self.assertIs(r.compile_result, result)

    def test_nested_answer_parser_flaw_and_helper(self):
        t = TaskTemplate(ANSWER_SOURCE, 'target', True)
        nested = judge_submission(t, ANSWER_SOURCE.replace('answer(False)', 'answer(f(x))'), Compiler())
        self.assertEqual((nested.accepted, nested.reason), (False, 'Immutable source differs'))
        helper = preamble(ANSWER_SOURCE, 'def MyAns : Prop := True').replace('answer(False)', 'answer(MyAns)')
        self.assertTrue(judge_submission(t, helper, Compiler()).accepted)

    def test_answer_extraction_is_first_match_anywhere(self):
        # The documented side effect: an `answer(` mention in the preamble (even in
        # a comment) becomes the extracted span, so the real answer field is immutable.
        t = TaskTemplate(ANSWER_SOURCE, 'target', True)
        c = Compiler()
        mention = preamble(ANSWER_SOURCE, '-- see answer(n) below').replace('answer(False) ↔', 'answer(True) ↔')
        r = judge_submission(t, mention, c)
        self.assertEqual((r.accepted, r.reason, c.calls), (False, 'Immutable source differs', 0))
        # Even with the answer value unchanged: segment lists then differ in length.
        self.assertFalse(judge_submission(t, preamble(ANSWER_SOURCE, '-- answer(n)'), Compiler()).accepted)
        # A mention after the real field (proof region) is not the first match: harmless.
        later = ANSWER_SOURCE.replace('  trivial', '  -- answer(n)\n  trivial')
        self.assertTrue(judge_submission(t, later, Compiler()).accepted)

    def test_answer_pattern_details(self):
        for text, span in [('answer(x)', 'x'), ('answer (x)', 'x'), ('h.answer(y)', 'y'),
                           ('answer(\n  z)', '\n  z'), ('answer(f (x))', 'f (x')]:
            with self.subTest(text=text):
                a, b = answer_span(text)
                self.assertEqual(text[a:b], span)
        for text in ('my_answer(x)', 'Answer(x)', 'answer x'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                answer_span(text)

    def test_unbalanced_open_paren_passes_integrity(self):
        # First-`)` extraction cannot express nested answers, but an unbalanced `(`
        # inside the span consumes the template's closing parenthesis. Documented
        # flaw; compilation and the independent evaluator decide what it means.
        t = TaskTemplate(ANSWER_SOURCE, 'target', True)
        source = ANSWER_SOURCE.replace('answer(False)', 'answer(f (x)')
        self.assertEqual(immutable_segments(source, True), immutable_segments(ANSWER_SOURCE, True))
        self.assertTrue(judge_submission(t, source, Compiler()).accepted)

    def test_answer_based_template_requires_answer_field(self):
        r = judge_submission(TaskTemplate(SOURCE, 'target', True), SOURCE, Compiler())
        self.assertEqual((r.stage, r.reason), ('source_integrity', 'Missing answer field'))

    def test_markers_cannot_be_removed_or_duplicated(self):
        t = TaskTemplate(SOURCE, 'target')
        self.assertFalse(judge_submission(t, SOURCE.replace('-- EDITABLE END', ''), Compiler()).accepted)
        doubled = preamble(SOURCE, '-- EDITABLE END\n-- EDITABLE START')
        r = judge_submission(t, doubled, Compiler())
        self.assertEqual(r.stage, 'source_integrity')

    def test_alias_markers(self):
        source = (SOURCE.replace('-- EDITABLE START\n-- EDITABLE END', '-- PREAMBLE START\n-- PREAMBLE END', 1)
                  .replace('-- EDITABLE START', '-- PROOF START').replace('-- EDITABLE END', '-- PROOF END'))
        self.assertTrue(judge_submission(TaskTemplate(source, 'target'), source.replace('trivial', 'exact trivial'),
                                         Compiler()).accepted)

    def test_template_default_fields(self):
        self.assertEqual((TaskTemplate('', 'x').answer_based, TaskTemplate('', 'x').answer_value_known), (False, False))


if __name__ == '__main__':
    unittest.main()


# ---------------------------------------------------------------- validation scripts (offline)
import re  # noqa: E402
import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import validate_dataset  # noqa: E402
import validate_judge  # noqa: E402
from research_swarm.dataset import FrozenDataset  # noqa: E402
from research_swarm.keyword_filter import check_keywords  # noqa: E402


class FakeLean:
    """Returns canned CompileResults per source; never runs Docker."""
    def __init__(self, respond):
        self.respond, self.sources = respond, []

    def compile(self, source):
        self.sources.append(('production', source))
        return self.respond(source)

    def compile_measured(self, source):
        self.sources.append(('measured', source))
        return self.respond(source)


def placeholder_error(line):
    return CompileResult('error', 1, f'/tmp/Submission.lean:{line}:2: error: Provide a complete proof.\nn : ℕ\n⊢ goal\n',
                         '', 0, 1.0, dict(lean_seconds=0.5))


@needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
class JudgeValidationScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = validate_judge.build_cases(FrozenDataset(ROOT))
        cls.by_id = {c['id']: c for c in cls.cases}

    def test_evidence_naming(self):
        moment = validate_judge.utc_now()
        self.assertRegex(validate_judge.evidence_path('judge', moment).name, r'^judge-\d{8}T\d{6}Z\.json$')

    def test_case_inventory(self):
        self.assertEqual(len(self.by_id), len(self.cases))
        required = {'unmodified_template', 'genuine_green32', 'genuine_green32_production', 'genuine_erdos23',
                    'keyword_axiom', 'keyword_sorry', 'keyword_macro', 'keyword_syntax', 'answer_nat_nested',
                    'answer_prop_nested', 'answer_nat_helper', 'answer_prop_helper', 'answer_preamble_mention',
                    'admit_sorry_warning', 'timeout', 'sorry_wording_admit'}
        self.assertLessEqual(required, {c['id'] for c in self.cases if c['required']})
        probes = {c['id'] for c in self.cases if c['category'] == 'documentation_probe'}
        self.assertEqual(probes, {'weakness_exit_command', 'weakness_cross_region_block_comment',
                                  'weakness_guard_msgs_drop_warning_admit', 'weakness_process_exit_zero',
                                  'weakness_answer_field_line_comment', 'weakness_answer_field_block_comment',
                                  'weakness_warn_sorry_option_admit'})
        self.assertTrue(all(not self.by_id[p]['required'] for p in probes))
        self.assertEqual(self.by_id['timeout']['path'], 'production')
        self.assertIn('def gdmC5', self.by_id['genuine_erdos23']['source'])  # preamble fixture applied

    def test_keyword_expectations_match_sources(self):
        for case in self.cases:
            with self.subTest(case=case['id']):
                keywords = check_keywords(case['source']).matched_keywords
                if case['expect'].get('stage') == 'keyword_filter':
                    self.assertEqual(keywords, (case['id'].split('_', 1)[1],))
                elif case['kind'] == 'judge':
                    self.assertEqual(keywords, ())

    def test_no_compile_cases_pass_without_lean(self):
        def refuse(source):
            raise AssertionError('compiler must not be invoked')
        for case in self.cases:
            if case['expect'].get('compiler_invoked') is False:
                with self.subTest(case=case['id']):
                    row = validate_judge.run_case(case, FakeLean(refuse))
                    self.assertTrue(row['passed'], row['checks'])

    def test_compile_case_checks(self):
        unmodified = self.by_id['unmodified_template']
        line = unmodified['expect']['placeholder_only']
        self.assertTrue(validate_judge.run_case(unmodified, FakeLean(lambda s: placeholder_error(line)))['passed'])
        wrong = validate_judge.run_case(unmodified, FakeLean(lambda s: placeholder_error(line + 1)))
        self.assertFalse(wrong['checks']['placeholder_only'])
        ok = CompileResult('success', 0, '', '', 0, 1.0, dict(lean_seconds=0.4))
        fake = FakeLean(lambda s: ok)
        row = validate_judge.run_case(self.by_id['genuine_erdos23'], fake)
        self.assertTrue(row['passed'])
        self.assertEqual(row['observed']['reason'], 'Submission accepted.')
        self.assertEqual(fake.sources[0][0], 'measured')
        timed_out = CompileResult('timeout', 137, '', "timeout: sending signal KILL to command 'lake'", None, 120.5,
                                  timeout_source='container')
        fake = FakeLean(lambda s: timed_out)
        with patch.object(validate_judge, 'judge_containers', return_value=[]):
            row = validate_judge.run_case(self.by_id['timeout'], fake)
        self.assertTrue(row['passed'], row['checks'])
        self.assertEqual(fake.sources[0][0], 'production')
        with patch.object(validate_judge, 'judge_containers', return_value=['gdm-lean-x']):
            self.assertFalse(validate_judge.run_case(self.by_id['timeout'], fake)['passed'])
        probe = validate_judge.run_case(self.by_id['weakness_exit_command'], FakeLean(lambda s: ok))
        self.assertIsNone(probe['passed'])
        self.assertTrue(probe['hypothesis_matched'])


class DatasetValidationScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = FrozenDataset(ROOT)

    def test_static_checks_pass_for_all_frozen_templates(self):
        for task in self.dataset.tasks.values():
            with self.subTest(task=task.task_id):
                static = validate_dataset.static_checks(task, task.template_source(ROOT))
                self.assertEqual((static['problems'], static['qualified_name']), ([], task.theorem_name))

    def test_qualified_name_scopes(self):
        source = ('import X\nnamespace A.B\nsection S\nnoncomputable section\nend\nend S\nnamespace C\nend C\n'
                  'theorem t.u : True := by\n  fail "Provide a complete proof."\nend A.B\n')
        self.assertEqual(validate_dataset.qualified_theorem_name(source), 'A.B.t.u')
        self.assertEqual(validate_dataset.qualified_theorem_name(source.replace('theorem t.u', 'theorem _root_.v')), 'v')

    def test_strict_variant_inserts_options_after_imports(self):
        source = self.dataset.tasks['Green32_hasCosetHole_empty'].template_source(ROOT)
        variant, offset = validate_dataset.strict_admitted_variant(source)
        lines = variant.split('\n')
        self.assertEqual(lines[1:1 + offset], list(validate_dataset.STRICT_OPTIONS))
        self.assertTrue(lines[validate_dataset.theorem_line(source) - 1 + offset].startswith('theorem '))
        self.assertNotIn(validate_dataset.PLACEHOLDER, variant)

    def test_validate_declaration_with_fake_lean(self):
        task = self.dataset.tasks['Green32_hasCosetHole_empty']
        source = task.template_source(ROOT)
        fail_line = validate_dataset.placeholder_line(source)
        target = validate_dataset.theorem_line(source) + len(validate_dataset.STRICT_OPTIONS)

        def respond(text):
            if validate_dataset.PLACEHOLDER in text:
                return placeholder_error(fail_line)
            # Lean 4.27 prints info output without a location prefix.
            out = (f"/tmp/Submission.lean:{target}:8: warning: declaration uses 'sorry'\n"
                   f'GDM_NAME_PROBE theorem {task.theorem_name}\n{task.theorem_name} : ∀ (n : ℕ), True\n'
                   f"'{task.theorem_name}' depends on axioms: [sorryAx]\n")
            return CompileResult('success', 0, out, '', 1, 1.0, dict(lean_seconds=0.5))

        row = validate_dataset.validate_declaration(FakeLean(respond), task, source)
        self.assertEqual((row['status'], row['problems']), ('validated', []))
        extra = lambda text: CompileResult('error', 1, placeholder_error(fail_line).stdout
                                           + '/tmp/Submission.lean:9:0: error: unknown identifier\n', '', 0)
        self.assertEqual(validate_dataset.validate_declaration(FakeLean(extra), task, source, strict=False)['status'],
                         'defect')
        timeout = lambda text: CompileResult('timeout', 137, '', '', None, 120.0, timeout_source='container')
        self.assertEqual(validate_dataset.validate_declaration(FakeLean(timeout), task, source)['status'], 'timeout')

    def test_reuse_probe_output_parsing(self):
        probe = validate_dataset.reuse_probe("DedekindNumber.M'_three")
        self.assertIn("`DedekindNumber.M'_three", probe)
        self.assertIn('logInfo m!"GDM_REUSE {payload}"', probe)
        lines = ['noise', 'GDM_REUSE {"exact_type_matches": ["X.y"], "conjecture_or_negation_matches": []}']
        self.assertEqual(validate_dataset.parse_reuse(lines)['exact_type_matches'], ['X.y'])
        self.assertIsNone(validate_dataset.parse_reuse(['GDM_REUSE {broken']))
        self.assertTrue(re.fullmatch(r'reuse-\d{8}T\d{6}Z\.json',
                                     validate_dataset.evidence_path('reuse', validate_dataset.utc_now()).name))

    def test_source_search_markers_survive_a_real_posix_shell(self):
        tasks = self.dataset.pilot_tasks
        task_id = "DedekindNumber_M'_three"
        terms = dict(validate_dataset.REUSE_TERMS)
        terms[task_id] = [*terms[task_id], r'\bSperner\b', r"quote'and\nnewline"]
        real_run = validate_dataset.subprocess.run

        def run_script_without_docker(command, **kwargs):
            # Execute the generated shell protocol, replacing only the image's
            # working directory and filesystem grep with a no-match shell stub.
            script = command[-1].replace('cd /opt/lean-project', 'cd .', 1)
            kwargs['timeout'] = 10
            return real_run(['sh', '-c', 'grep() { return 1; }\n' + script], **kwargs)

        with patch.object(validate_dataset, 'REUSE_TERMS', terms), \
                patch.object(validate_dataset.subprocess, 'run', side_effect=run_script_without_docker):
            result = validate_dataset.source_search(SimpleNamespace(image='unused'), tasks)
        self.assertEqual(set(result), {task.task_id for task in tasks})
        for task in tasks:
            self.assertEqual(result[task.task_id], [
                {'term': term, 'hits': 0, 'declaration_hits': []}
                for term in terms[task.task_id]
            ])


# ---------------------------------------------------------------- documented weaknesses (offline, unchanged)
class DocumentedWeaknessTests(unittest.TestCase):
    """Documented weaknesses of the approved judge stay unchanged (docs/judges.md, plan row
    "Baseline judge weaknesses"). These pin pre-compilation behavior only; Lean outcomes are Phase B."""
    @classmethod
    def setUpClass(cls):
        cls.dataset = FrozenDataset(ROOT)

    def test_answer_field_comment_reaches_compilation_on_answer_based_pilot_tasks(self):
        tasks = [t for t in self.dataset.pilot_tasks if t.answer_based]
        self.assertEqual(len(tasks), 8)
        for task in tasks:
            source = task.template_source(ROOT)
            a, b = answer_span(source)
            for opener, closer in (('--', ''), ('/-', '-/')):
                with self.subTest(task=task.task_id, opener=opener):
                    edited = (source[:a] + 'True ' + opener + source[b:]).replace(
                        validate_dataset.PLACEHOLDER, closer + ') ↔ True := Iff.rfl', 1)
                    c = Compiler()
                    r = judge_submission(TaskTemplate(source, task.theorem_name, True), edited, c)
                    self.assertEqual((r.stage, c.calls), ('accepted', 1))

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_warn_sorry_option_only_literal_forms_are_blocked(self):
        task = self.dataset.tasks['Green32_hasCosetHole_empty']
        source = task.template_source(ROOT)
        template = TaskTemplate(source, task.theorem_name)
        admitted = source.replace(validate_dataset.PLACEHOLDER, '  admit', 1)
        for literal in ('set_option warn.sorry false', 'set_option warn.sorry false in', 'set_option warn.«sorry» false'):
            with self.subTest(literal=literal):
                c = Compiler()
                r = judge_submission(template, preamble(admitted, literal), c)
                self.assertEqual((r.stage, r.reason, c.calls), ('keyword_filter', 'Forbidden text: sorry', 0))
        probe = {c['id']: c for c in validate_judge.build_cases(self.dataset)}['weakness_warn_sorry_option_admit']
        c = Compiler()
        self.assertEqual((judge_submission(template, probe['source'], c).stage, c.calls), ('accepted', 1))

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_answer_field_probes_reach_compilation(self):
        cases = {c['id']: c for c in validate_judge.build_cases(self.dataset)}
        ok = CompileResult('success', 0, '', '', 0, 1.0, dict(lean_seconds=0.4))
        for case_id, opener in (('weakness_answer_field_line_comment', 'answer(True --)'),
                                ('weakness_answer_field_block_comment', 'answer(True /-)')):
            with self.subTest(case=case_id):
                case = cases[case_id]
                self.assertIn(opener, case['source'])
                self.assertTrue(case['template'].answer_based)
                row = validate_judge.run_case(case, FakeLean(lambda s: ok))
                self.assertEqual((row['passed'], row['hypothesis_matched'], row['observed']['compiler_invoked']),
                                 (None, True, True))


# ---------------------------------------------------------------- X5 evidence completeness (offline, mocked Docker)
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402
from research_swarm.lean import DockerLeanCompiler  # noqa: E402

LEAN_ID = 'sha256:' + 'a' * 64


class _ScriptRun:
    """Runs a validation script's main() with Docker mocked and a temporary config/output."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'config.json'
        self.config.write_text(json.dumps(dict(compilation_timeout_seconds=120, judge_memory='2g', judge_cpus=1)))
        self.output = Path(self.tmp.name) / 'out.json'

    def run_main(self, module, argv, **patches):
        argv = [module.__name__] + argv + ['--output', str(self.output), '--config', str(self.config)]
        patches.setdefault('live_container_blockers', lambda: [])
        with patch.object(sys, 'argv', argv), patch.object(module, 'image_id', return_value=LEAN_ID), \
                patch.object(module, 'vm_state', return_value=None):
            stack = [patch.object(module, name, value) for name, value in patches.items()]
            for p in stack:
                p.start()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = module.main()
            finally:
                for p in reversed(stack):
                    p.stop()
        return rc, json.loads(self.output.read_text())


class LiveContainerRefusalTests(_ScriptRun, unittest.TestCase):
    """Judge-side validation refuses to start beside comparator/judge/agent containers."""
    def refused(self, module, argv):
        compiler = MagicMock(side_effect=AssertionError('no compiler may be built'))
        argv = [module.__name__] + argv + ['--output', str(self.output), '--config', str(self.config)]
        with patch.object(sys, 'argv', argv), patch.object(module, 'image_id', return_value=LEAN_ID), \
                patch.object(module, 'live_container_blockers', return_value=['comparator container running']), \
                patch.object(module, 'DockerLeanCompiler', compiler), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = module.main()
        return rc

    def test_validate_judge_refuses_without_writing_evidence(self):
        self.assertEqual(self.refused(validate_judge, ['--no-warm-up']), 2)
        self.assertFalse(self.output.exists())

    def test_validate_dataset_refuses_without_writing_evidence(self):
        self.assertEqual(self.refused(validate_dataset, ['declarations', '--no-warm-up']), 2)
        self.assertFalse(self.output.exists())


class JudgeEvidenceCompletenessTests(_ScriptRun, unittest.TestCase):
    def passing_row(self, case, compiler):
        return dict(id=case['id'], required=case['required'], passed=True if case['required'] else None,
                    observed={}, kind=case['kind'], category=case['category'])

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_full_run_is_complete_and_records_config_limits(self):
        spy = MagicMock(side_effect=DockerLeanCompiler)
        rc, report = self.run_main(validate_judge, ['--no-warm-up'], judge_containers=lambda: [],
                                   run_case=self.passing_row, DockerLeanCompiler=spy)
        self.assertEqual((rc, report['complete'], report['all_required_passed'], report['omitted_cases']),
                         (0, True, True, []))
        self.assertEqual(report['limits'], dict(timeout_seconds=120, memory='2g', cpus=1))
        self.assertEqual(report['config']['limits'], report['limits'])
        self.assertEqual(spy.call_args.kwargs, dict(timeout=120, memory='2g', cpus=1, max_concurrent=1))

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_only_and_skip_timeout_never_pass(self):
        # --only: the keyword case passes without Lean, but the record is partial.
        rc, report = self.run_main(validate_judge, ['--no-warm-up', '--only', 'keyword_axiom'],
                                   judge_containers=lambda: [])
        self.assertEqual((rc, report['complete'], report['all_required_passed']), (1, False, False))
        self.assertTrue(report['summary']['executed_required_passed'])
        self.assertIn('timeout', report['omitted_required_cases'])
        rc, report = self.run_main(validate_judge, ['--no-warm-up', '--skip-timeout'], judge_containers=lambda: [],
                                   run_case=self.passing_row)
        self.assertEqual((rc, report['complete'], report['all_required_passed'], report['omitted_cases']),
                         (1, False, False, ['timeout']))

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_unknown_only_case_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.run_main(validate_judge, ['--no-warm-up', '--only', 'no_such_case'], judge_containers=lambda: [])

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_limits_follow_config(self):
        self.config.write_text(json.dumps(dict(compilation_timeout_seconds=150, judge_memory='3g', judge_cpus='1.5')))
        _, report = self.run_main(validate_judge, ['--no-warm-up'], judge_containers=lambda: [],
                                  run_case=self.passing_row)
        self.assertEqual(report['limits'], dict(timeout_seconds=150, memory='3g', cpus=1.5))
        self.assertEqual(validate_judge.judge_limits('120', '2g', '1.0'), dict(timeout_seconds=120, memory='2g', cpus=1))


class FakeDockerLean:
    """Stands in for DockerLeanCompiler in validate_dataset.main (records limits, no Docker)."""
    made = []

    def __init__(self, image, timeout=120, memory='2g', cpus=1, max_concurrent=None):
        self.image, self.timeout, self.memory, self.cpus = image, timeout, memory, cpus
        FakeDockerLean.made.append(self)

    def compile_measured(self, source):
        if validate_dataset.PLACEHOLDER in source:
            return placeholder_error(validate_dataset.placeholder_line(source))
        if 'researcher-only dependency-reuse probe' in source:
            out = 'GDM_REUSE {"exact_type_matches": [], "conjecture_or_negation_matches": []}\n'
            return CompileResult('success', 0, out, '', 1, 1.0, dict(lean_seconds=0.5))
        return CompileResult('success', 0, '', '', 0, 1.0, dict(lean_seconds=0.5))


class DatasetEvidenceCompletenessTests(_ScriptRun, unittest.TestCase):
    def setUp(self):
        super().setUp()
        FakeDockerLean.made = []
        self.pilot = FrozenDataset(ROOT).subset['task_ids']

    def run_dataset(self, argv):
        validated = lambda compiler, task, source, strict=True: dict(task_id=task.task_id, status='validated',
                                                                     problems=[], strict=strict)
        return self.run_main(validate_dataset, argv + ['--no-warm-up'], DockerLeanCompiler=FakeDockerLean,
                             environment=lambda args: {}, validate_declaration=validated)

    def assert_gate(self, report, rc, complete, passed):
        self.assertEqual((rc, report['complete'], report['all_required_passed']), (0 if passed else 1, complete, passed))

    def test_declarations_full_pilot_passes_at_config_limits(self):
        rc, report = self.run_dataset(['declarations'])
        self.assert_gate(report, rc, True, True)
        self.assertEqual((report['limits'], report['limits_match_config'], report['strict_variant']),
                         (dict(timeout_seconds=120, memory='2g', cpus=1), True, True))
        self.assertEqual({(c.timeout, c.memory, c.cpus) for c in FakeDockerLean.made}, {(120, '2g', 1)})
        rc, report = self.run_dataset(['declarations', '--tasks'] + list(reversed(self.pilot)))
        self.assert_gate(report, rc, True, True)

    def test_declarations_subset_template_only_or_other_limits_never_pass(self):
        rc, report = self.run_dataset(['declarations', '--tasks', 'Green32_hasCosetHole_empty'])
        self.assert_gate(report, rc, False, False)
        self.assertEqual(len(report['missing_pilot_tasks']), len(self.pilot) - 1)
        rc, report = self.run_dataset(['declarations', '--template-only'])
        self.assert_gate(report, rc, False, False)
        self.assertFalse(report['strict_variant'])
        rc, report = self.run_dataset(['declarations', '--timeout', '900', '--memory', '8g'])
        self.assertEqual((rc, report['complete'], report['limits_match_config'], report['all_required_passed']),
                         (1, True, False, False))
        self.assertEqual(report['limits'], dict(timeout_seconds=900, memory='8g', cpus=1))
        self.assertEqual((FakeDockerLean.made[-1].timeout, FakeDockerLean.made[-1].memory), (900, '8g'))

    @needs_private("research/fixtures/judge/Green32_hasCosetHole_empty.proof.lean")
    def test_timing_requires_default_workloads_repeats_and_limits(self):
        rc, report = self.run_dataset(['timing'])
        self.assert_gate(report, rc, True, True)
        rc, report = self.run_dataset(['timing', '--workloads', 'template:Green32_hasCosetHole_empty'])
        self.assert_gate(report, rc, False, False)
        self.assertEqual(len(report['missing_default_workloads']), 2)
        rc, report = self.run_dataset(['timing', '--repeats', '1'])
        self.assert_gate(report, rc, False, False)
        rc, report = self.run_dataset(['timing', '--cpus', '2'])
        self.assertEqual((report['complete'], report['limits_match_config'], report['all_required_passed']),
                         (True, False, False))

    def test_reuse_subset_never_passes(self):
        rc, report = self.run_dataset(['reuse', '--no-grep'])
        self.assert_gate(report, rc, True, True)
        rc, report = self.run_dataset(['reuse', '--no-grep', '--tasks', 'Green32_hasCosetHole_empty'])
        self.assert_gate(report, rc, False, False)
