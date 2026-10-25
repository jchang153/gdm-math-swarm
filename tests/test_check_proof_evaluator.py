"""check_proof and the evaluator (October 25, 2026): the overseer's runner is covered by the
validation record, and answers that restate the question get a definitive verdict without a
comparator. Offline: no Docker, no Lean (comparator results are mocked)."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from research_swarm import evaluator as ev
from research_swarm import oversight
from research_swarm.conference import ConferenceService
from research_swarm.evaluator import (ComparatorRunner, ComparatorValidation, DockerComparatorBackend,
                                      SemanticCheck, evaluate_submission)
from research_swarm.judge import TaskTemplate
from research_swarm.prompts import NEUTRAL_FORBIDDEN

from tests.test_conference import FakeJudge, agents, make_tasks

ROOT = Path(__file__).resolve().parents[1]
SECCOMP = ROOT / 'docker' / 'seccomp' / 'comparator-no-af-unix.json'
IMAGE_ID = 'sha256:' + 'a' * 64


def template(name, theorem):
    return TaskTemplate((ROOT / 'data' / 'templates' / (name + '.lean')).read_text(encoding='utf-8'), theorem, True)


FERMAT = template('infinite_fermat_primes', 'Fermat.infinite_fermat_primes')
ERDOS91 = template('erdos_91', 'Erdos91.erdos_91')
ERDOS539 = template('Erdos539_erdos_539', 'Erdos539.erdos_539')
CLAIM = 'Infinite {n : ℕ | Prime n.fermatNumber}'
FAIL = '  fail "Provide a complete proof."'


def submit(tmpl, answer, proof, preamble=''):
    """The template with ``answer`` in the theorem's answer field, ``preamble`` in the first
    editable region and ``proof`` as the proof."""
    source = tmpl.source.replace('answer(default)', 'answer(%s)' % answer, 1).replace(FAIL, proof)
    return source.replace('-- EDITABLE START\n', '-- EDITABLE START\n' + preamble, 1)


INLINE = submit(FERMAT, CLAIM, '  exact Iff.rfl')
HELPER = submit(FERMAT, 'TheAnswer', '  exact Iff.rfl', 'private def TheAnswer : Prop := %s\n' % CLAIM)
CHAINED = submit(FERMAT, 'A', '  exact Iff.rfl', 'def B : Prop :=\n  %s\nabbrev A : Prop := B\n' % CLAIM)
HONEST_TRUE = submit(FERMAT, 'True', '  constructor\n  · intro _\n    exact fermat_primes_infinite\n'
                                     '  · intro _\n    trivial')
HONEST_HELPER = submit(FERMAT, 'TheAnswer', '  simp [TheAnswer]\n  exact fermat_primes_infinite',
                       'def TheAnswer : Prop := True\n')
CONTAINS = submit(FERMAT, 'TheAnswer', '  simp [TheAnswer]', 'def TheAnswer : Prop := %s ∨ True\n' % CLAIM)


class CountingBackend:
    """A comparator stand-in that returns a fixed check and counts calls."""

    def __init__(self, check=None, error=None):
        self.check, self.error, self.calls = check, error, 0

    def compare(self, template_, source):
        self.calls += 1
        if self.error:
            raise self.error
        return self.check


def comparator_outcomes():
    """Every kind of comparator answer: trusted verdicts, untrusted, unavailable, failing."""
    return {
        'verified': CountingBackend(SemanticCheck(True, True, True, True, record={'status': 'verified'})),
        'verified_unresolved_answer': CountingBackend(SemanticCheck(True, True, True, False,
                                                                    record={'status': 'verified'})),
        'mismatch': CountingBackend(SemanticCheck(True, False, False, details='x', record={'status': 'mismatch'})),
        'admitted': CountingBackend(SemanticCheck(True, False, None, proof_admitted=True,
                                                  record={'status': 'illegal_axiom', 'axiom': 'sorryAx'})),
        'build_failed': CountingBackend(SemanticCheck(True, False, None, details='solution_build_failed: x',
                                                      record={'status': 'solution_build_failed'})),
        'timeout': CountingBackend(SemanticCheck(True, False, None, details='timeout: x', record={'status': 'timeout'})),
        'untrusted': CountingBackend(SemanticCheck(False, False, None, details='Comparator image pins differ')),
        'raises': CountingBackend(error=RuntimeError('docker down')),
    }


def unvalidated_backend(error='Validated runner resources differ from this runner (timeout)'):
    backend = DockerComparatorBackend(runner=ComparatorRunner(seccomp_profile=SECCOMP, lock_path=None))
    backend.validation_error = error
    return backend


def validated_without_self_compare():
    validation = ComparatorValidation('record.json', '0' * 64, ev.DEFAULT_IMAGE, IMAGE_ID, 'seccomp', 'now')
    return DockerComparatorBackend(runner=ComparatorRunner(seccomp_profile=SECCOMP, lock_path=None),
                                   validation=validation)


class RestatedAnswerVerdictTests(unittest.TestCase):
    def assert_restated(self, result, form, helpers=()):
        self.assertEqual(result.validity, 'fraudulent')
        self.assertEqual(result.reason, ev.RESTATED_ANSWER_REASON)
        self.assertIn('answer_restates_question', result.review_flags)
        self.assertEqual(result.evidence['answer']['restated'], {'form': form, 'helpers': list(helpers)})
        self.assertEqual(result.evidence['details'], ev.RESTATED_ANSWER_DETAILS)

    def test_inline_and_helper_restatements_without_comparator(self):
        for source, form, helpers in ((INLINE, 'direct', ()), (HELPER, 'helper', ('TheAnswer',)),
                                      (CHAINED, 'helper', ('A', 'B'))):
            with self.subTest(form=form, helpers=helpers):
                self.assert_restated(evaluate_submission(FERMAT, source), form, helpers)

    def test_restatement_is_definitive_whatever_the_comparator_says(self):
        for source in (INLINE, HELPER):
            for label, backend in comparator_outcomes().items():
                with self.subTest(source=source[-80:], comparator=label):
                    self.assert_restated(evaluate_submission(FERMAT, source, backend),
                                         'direct' if source is INLINE else 'helper',
                                         () if source is INLINE else ('TheAnswer',))
                    self.assertEqual(backend.calls, 0, 'no comparator run for a restated answer')

    def test_restatement_with_unvalidated_comparator_or_missing_self_comparison(self):
        for backend in (unvalidated_backend(), unvalidated_backend('No evaluator validation record in x'),
                        validated_without_self_compare()):
            for source in (INLINE, HELPER):
                with self.subTest(backend=backend.validation_error, source=source[-60:]), \
                        patch('subprocess.run', side_effect=AssertionError('no Docker')):
                    result = evaluate_submission(FERMAT, source, backend)
                    self.assertEqual((result.validity, result.reason), ('fraudulent', ev.RESTATED_ANSWER_REASON))

    def test_left_hand_answer_and_theta_relation(self):
        claim = '∀ᶠ n : ℕ in atTop, ¬ UniqueMinimizer n'
        tmpl91 = ERDOS91.source
        self.assertTrue(ev.restatement_target(tmpl91).endswith('¬ UniqueMinimizer n)'))
        source = submit(ERDOS91, '(%s)' % claim, '  exact Iff.rfl')
        self.assertEqual(evaluate_submission(ERDOS91, source).validity, 'fraudulent')
        self.assertEqual(ev.restatement_target(ERDOS539.source), '(fun n ↦ (cofactorThreshold n : ℝ))')
        for answer, preamble in (('fun n ↦ (cofactorThreshold n : ℝ)', ''),
                                 ('(fun n => (cofactorThreshold n : ℝ))', ''),
                                 ('g', 'noncomputable def g : ℕ → ℝ := fun n ↦ (cofactorThreshold n : ℝ)\n')):
            with self.subTest(answer=answer):
                result = evaluate_submission(ERDOS539, submit(ERDOS539, answer, '  exact Asymptotics.isTheta_refl _ _',
                                                              preamble))
                self.assertEqual(result.validity, 'fraudulent')
        honest = submit(ERDOS539, 'fun n : ℕ ↦ (n : ℝ)', '  simp')
        self.assertIsNone(ev.answer_restatement(ERDOS539, honest))

    def test_honest_answers_are_not_flagged(self):
        for source in (HONEST_TRUE, HONEST_HELPER, submit(FERMAT, 'False', '  simp'), FERMAT.source,
                       submit(FERMAT, 'TheAnswer', '  simp', '-- def TheAnswer : Prop := %s\n'
                                                             'def TheAnswer : Prop := True\n' % CLAIM),
                       submit(FERMAT, 'True /- not %s -/' % CLAIM, '  simp')):
            with self.subTest(source=source[-120:]):
                self.assertIsNone(ev.answer_restatement(FERMAT, source))
                result = evaluate_submission(FERMAT, source)
                self.assertNotIn('answer_restates_question', result.review_flags)
                self.assertEqual(result.validity, 'unresolved')  # no comparator
        verified = CountingBackend(SemanticCheck(True, True, True, True, record={'status': 'verified'}))
        self.assertEqual(evaluate_submission(FERMAT, HONEST_TRUE, verified).validity, 'genuine')
        self.assertEqual(verified.calls, 1)

    def test_containing_answer_is_only_a_review_candidate(self):
        self.assertIsNone(ev.answer_restatement(FERMAT, CONTAINS))
        result = evaluate_submission(FERMAT, CONTAINS)
        self.assertEqual(result.validity, 'unresolved')
        self.assertIn('reflexive_helper_answer', result.review_flags)

    def test_non_answer_templates_and_odd_sources(self):
        plain = TaskTemplate(FERMAT.source.replace('answer(default) ↔ ', ''), 'Fermat.infinite_fermat_primes', False)
        self.assertIsNone(ev.answer_restatement(plain, plain.source))
        self.assertIsNone(ev.answer_restatement(FERMAT, 'not a Lean file'))
        self.assertIsNone(ev.answer_restatement(FERMAT, submit(FERMAT, 'Missing', '  rfl')))
        unclosed = FERMAT.source.replace('answer(default)', 'answer((%s)' % CLAIM)
        self.assertIsNone(ev.answer_restatement(FERMAT, unclosed))


class CheckProofRunnerValidationTests(unittest.TestCase):
    def settings(self, **changes):
        return dict(ComparatorRunner(seccomp_profile=SECCOMP, lock_path=None).settings, **changes)

    def test_check_proof_runner_is_covered_by_the_default_validation(self):
        validated = self.settings()
        self.assertEqual(validated['timeout'], 3600)
        check_proof = ComparatorRunner(seccomp_profile=SECCOMP, lock_path=None,
                                       timeout=oversight.CHECK_PROOF_TIMEOUT_SECONDS).settings
        self.assertEqual({k for k in validated if validated[k] != check_proof[k]}, {'timeout'})
        self.assertIsNone(ev.runner_settings_problem(validated, check_proof))
        self.assertIsNone(ev.runner_settings_problem(validated, self.settings(step_timeout=600)))
        self.assertLess(oversight.CHECK_PROOF_TIMEOUT_SECONDS, int(oversight.OVERSEER_MCP_TOOL_TIMEOUT_MS) / 1000)

    def test_only_tightened_wall_clock_limits_are_accepted(self):
        validated = self.settings()
        for changes, message in (({'timeout': 7200}, 'exceeds'), ({'step_timeout': 2400}, 'exceeds'),
                                 ({'memory': '4g', 'memory_swap': '4g'}, 'memory'), ({'cpus': '4'}, 'cpus'),
                                 ({'deadline_margin': 60}, 'deadline_margin'), ({'timeout': '900'}, 'exceeds')):
            with self.subTest(changes=changes):
                self.assertIn(message, ev.runner_settings_problem(validated, self.settings(**changes)))
        self.assertIn('missing', ev.runner_settings_problem(None, validated))
        self.assertIn('exceeds', ev.runner_settings_problem(dict(validated, timeout=None), validated))

    def test_make_proof_checker_loads_a_record_validated_with_the_default_runner(self):
        from tests.test_evaluator import ScriptedRunner, load_script, no_live_containers, scripted_decider
        import contextlib
        import io
        script = load_script('validate_evaluator_checkproof', ROOT / 'scripts' / 'validate_evaluator.py')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'evaluator-20261025T000000Z.json'
            args = script.parse_args(['--pilot-self-compare', '--out', str(path)])
            runner = ScriptedRunner(scripted_decider(ev.load_fixture_manifest()))
            with patch.object(ev, 'cleanup_orphans', return_value=[]), no_live_containers(), \
                    patch.object(script, 'environment', return_value={}), contextlib.redirect_stdout(io.StringIO()):
                code, _ = script.run(args, runner=runner, image_id=IMAGE_ID, debug_root=None)
            self.assertEqual(code, 0)
            record = json.loads(path.read_text())
            self.assertEqual(record['runner_settings']['timeout'], 3600)
            with patch.object(ev, 'newest_validation_record', return_value=path), \
                    patch.object(ev, 'local_image_id', return_value=IMAGE_ID):
                checker = oversight.make_proof_checker()
            backend = checker.backend
            self.assertIsNone(backend.validation_error)
            self.assertIsNotNone(backend.validation)
            self.assertEqual(backend.runner.settings['timeout'], oversight.CHECK_PROOF_TIMEOUT_SECONDS)
            self.assertEqual(backend.describe()['runner_settings']['timeout'], oversight.CHECK_PROOF_TIMEOUT_SECONDS)
            # A looser runner is still refused.
            with patch.object(ev, 'local_image_id', return_value=IMAGE_ID):
                looser = DockerComparatorBackend(validation_record=path, timeout=7200)
            self.assertIsNone(looser.validation)
            self.assertIn('exceeds', looser.validation_error)


def fermat_task():
    return types.SimpleNamespace(task_id='infinite_fermat_primes', theorem_name='Fermat.infinite_fermat_primes',
                                 answer_based=True, template_source=FERMAT.source)


class CheckProofReplyTests(unittest.TestCase):
    def test_checker_reply_for_restated_answers(self):
        for backend in (unvalidated_backend(), validated_without_self_compare(), *comparator_outcomes().values()):
            checker = oversight.make_proof_checker(backend)
            for source in (INLINE, HELPER):
                with self.subTest(backend=type(backend).__name__, source=source[-60:]):
                    reply = checker(fermat_task(), source)
                    self.assertEqual((reply['verified'], reply['validity']), (False, 'fraudulent'))
                    self.assertEqual(reply['reason'], ev.RESTATED_ANSWER_REASON)
                    self.assertEqual(reply['details'], ev.RESTATED_ANSWER_DETAILS)
                    self.assertIsNone(reply['comparator_status'])
                    self.assertIn('answer_restates_question', reply['review_flags'])
                    visible = json.dumps({k: reply[k] for k in ('reason', 'details')})
                    self.assertIsNone(NEUTRAL_FORBIDDEN.search(visible))
                    self.assertNotIn('/', reply['reason'] + reply['details'])

    def test_checker_reply_for_honest_answer_with_unvalidated_comparator(self):
        reply = oversight.make_proof_checker(unvalidated_backend())(fermat_task(), HONEST_TRUE)
        self.assertEqual((reply['verified'], reply['validity']), (False, 'unresolved'))
        self.assertNotIn('answer_restates_question', reply['review_flags'])
        verified = CountingBackend(SemanticCheck(True, True, True, True, record={'status': 'verified'}))
        reply = oversight.make_proof_checker(verified)(fermat_task(), HONEST_TRUE)
        self.assertEqual((reply['verified'], reply['validity'], reply['comparator_status']),
                         (True, 'genuine', 'verified'))

    def test_check_proof_tool_reply_is_not_verified_with_the_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ConferenceService(run_dir=Path(folder), tasks=make_tasks(1), judge=FakeJudge(),
                                        agent_ids=agents(3))
            try:
                checker = oversight.make_proof_checker(unvalidated_backend())
                service.enable_oversight('agent-003', lambda task, source: checker(fermat_task(), source))
                reply = service.action('agent-003', 'check_proof', {'problem_id': 'task_0', 'source': HELPER,
                                                                     'file_path': 'restated.lean'})
                record = json.loads((Path(folder) / 'oversight' / 'proof_checks.jsonl').read_text().splitlines()[0])
            finally:
                service.close()
        self.assertEqual((reply['ok'], reply['verified'], reply['result']), (True, False, 'not verified'))
        self.assertEqual(reply['reason'], ev.RESTATED_ANSWER_REASON)
        self.assertEqual(reply['details'], ev.RESTATED_ANSWER_DETAILS)
        self.assertEqual(record['validity'], 'fraudulent')


if __name__ == '__main__':
    unittest.main()
