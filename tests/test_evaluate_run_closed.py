"""Offline tests of evaluate_run.py's judge-requested closed-submission mode (no Docker, no Lean).

``problem_closed`` submissions were never judged operationally; their source
exists only in RUN/conference/state.sqlite. They are evaluated only when named
(--include-closed-ids / --include-closed-from), with the same comparator
backend, into the same evaluations.jsonl (plan row "Closed submissions";
docs/evaluator.md, "Judge-requested closed submissions"). Fake backends only:
real comparator semantics are established by scripts/validate_evaluator.py.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_swarm import evaluator as ev
from research_swarm.evaluator import SemanticCheck
from research.analysis.private_material import skip_decorator as needs_private  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GREEN = 'Green32_hasCosetHole_empty'
ERDOS6 = 'Erdos6_erdos_6_variants_decreasing'


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeBackend:
    """No editable markers -> build failure (unresolved); shadowed `primeGap` -> mismatch
    (fraudulent); anything else verified."""

    def __init__(self):
        self.calls = []

    def compare(self, template, source):
        self.calls.append((template.theorem_name, hashlib.sha256(source.encode()).hexdigest()))
        if 'EDITABLE' not in source:
            return SemanticCheck(True, False, None, details='solution_build_failed: unknown')
        return SemanticCheck(True, True, 'def primeGap' not in source)

    def describe(self):
        return {'backend': 'fake'}


@needs_private("research/fixtures/evaluator/submissions")
class ClosedSubmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from research_swarm.dataset import FrozenDataset
        cls.script = load_script('evaluate_run_closed', ROOT / 'scripts' / 'evaluate_run.py')
        cls.dataset = FrozenDataset(ROOT)
        cls.genuine = (ev.FIXTURE_DIR / 'submissions' / 'green32_genuine.lean').read_text()
        cls.shadow = (ev.FIXTURE_DIR / 'submissions' / 'erdos6_shadow_definition.lean').read_text()

    def make_run(self, run_dir):
        """sub-000001 accepted, -002 closed (genuine), -003 closed (keyword), -004 accepted (shadow),
        -005 closed (shadow), -006 closed (no editable markers), -007 rejected."""
        from research_swarm.conference import ConferenceService, tasks_from_dataset
        from research_swarm.judge import JudgeResult

        def judge(task, source):
            accepted = source != 'rejected'
            return JudgeResult(accepted, 'accepted' if accepted else 'compilation', 'r', None)
        service = ConferenceService(run_dir, tasks_from_dataset(self.dataset), judge,
                                    observer=ev.make_queue_observer(ev.run_evaluations_dir(run_dir)),
                                    agent_ids=['agent-001', 'agent-002', 'agent-003'])
        for agent, problem, source in (('agent-001', GREEN, self.genuine),
                                       ('agent-002', GREEN, self.genuine),
                                       ('agent-003', GREEN, self.genuine + '\n-- sorry\n'),
                                       ('agent-002', ERDOS6, self.shadow),
                                       ('agent-001', ERDOS6, self.shadow),
                                       ('agent-003', ERDOS6, 'x'),
                                       ('agent-003', 'Erdos23_erdos_23_variants_n1_tight', 'rejected')):
            service.action(agent, 'submit_solution', {'problem_id': problem, 'source': source})
        service.close()
        outcomes = self.script.read_sqlite(run_dir)[0]
        self.assertEqual([outcomes[s]['outcome'] for s in sorted(outcomes)],
                         ['accepted', 'problem_closed', 'problem_closed', 'accepted', 'problem_closed',
                          'problem_closed', 'rejected'])
        return service

    def write_request(self, run_dir, ids, reasons=None, **extra):
        path = run_dir / 'analysis' / 'judge' / 'closed_submissions_to_evaluate.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict({'schema': 'gdm-judge-closed-requests/1', 'submission_ids': ids,
                     'reasons': reasons if reasons is not None else {i: 'D1=4 judge_only %s' % i for i in ids}},
                    **extra)
        path.write_text(json.dumps(data))
        return path

    def evaluate(self, run_dir, backend, **kwargs):
        closed, meta = self.script.closed_requests(run_dir, kwargs.pop('ids', None), kwargs.pop('file', None))
        with contextlib.redirect_stdout(io.StringIO()):
            return self.script.evaluate_run(run_dir, backend, dataset=self.dataset, closed=closed,
                                            request_meta=meta, **kwargs)

    @staticmethod
    def lines(run_dir):
        return [json.loads(l) for l in (run_dir / 'evaluations' / 'evaluations.jsonl').read_text().splitlines()]

    def test_default_selection_never_includes_closed_submissions(self):
        fake = FakeBackend()
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            summary = self.evaluate(run_dir, fake, include_rejected=True)
            lines = self.lines(run_dir)
        self.assertEqual(sorted(e['submission']['submission_id'] for e in lines),
                         ['sub-000001', 'sub-000004', 'sub-000007'])
        self.assertFalse(any('closed_submission_request' in e for e in lines))
        self.assertEqual(summary['closed_submission_requests'], {'evaluated': [], 'by_validity': {}})
        self.assertNotIn('closed_requests', summary['sources'], 'no closed mode requested')

    def test_named_ids_are_evaluated_with_real_outcome_and_marker(self):
        fake = FakeBackend()
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            summary = self.evaluate(run_dir, fake, ids=['sub-000002, sub-000005', 'sub-000002'])
            by_id = {e['submission']['submission_id']: e for e in self.lines(run_dir)}
        self.assertEqual(sorted(by_id), ['sub-000001', 'sub-000002', 'sub-000004', 'sub-000005'])
        self.assertEqual(len(fake.calls), 4, 'one comparator call per selected submission')
        closed = by_id['sub-000002']
        self.assertEqual(closed['submission']['outcome'], 'problem_closed')
        self.assertEqual(closed['submission']['origins'], ['sqlite'], 'source only in state.sqlite')
        self.assertNotIn('judged_at', closed['submission'])
        self.assertEqual(closed['validity'], 'genuine')
        self.assertEqual(by_id['sub-000005']['validity'], 'fraudulent')
        request = closed['closed_submission_request']
        self.assertEqual((request['kind'], request['requested_via'], request['judge_reason']),
                         ('judge_requested_closed_submission', ['cli'], None))
        self.assertEqual((request['operationally_judged'], request['first_compilation'],
                          request['task_status_at_submission']), (False, True, 'closed'))
        self.assertTrue(request['hypothetical_static_checks']['passed'])
        self.assertEqual(request['keyword_policy'], 'literal-case-sensitive-whole-source-v1')
        self.assertNotIn('request_file', request)
        self.assertNotIn('closed_submission_request', by_id['sub-000001'])
        self.assertEqual(summary['closed_submission_requests'],
                         {'evaluated': ['sub-000002', 'sub-000005'], 'by_validity': {'genuine': 1, 'fraudulent': 1}})
        self.assertEqual(summary['by_outcome_and_validity']['problem_closed/fraudulent'], 1)
        self.assertEqual(summary['sources']['closed_requests'],
                         {'requested': ['sub-000002', 'sub-000005'], 'request_file': None})

    def test_request_file_is_resumable_and_records_reasons_and_static_checks(self):
        fake = FakeBackend()
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            path = self.write_request(run_dir, ['sub-000003'], generated_at='2026-10-17T00:00:00Z')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            first = self.evaluate(run_dir, fake, file=self.script.CLOSED_REQUEST_FILE)
            self.assertEqual(len(fake.calls), 3)
            again = self.evaluate(run_dir, fake, file=path)
            self.assertEqual(len(fake.calls), 3, 'resumed: nothing pending')
            self.write_request(run_dir, ['sub-000003', 'sub-000006'])
            self.evaluate(run_dir, fake, file=path, ids=['sub-000006'])
            # Never compiled before: the comparator build is its first compilation (fails here).
            self.assertEqual(len(fake.calls), 4, 'same comparator path as accepted submissions')
            lines = self.lines(run_dir)
        self.assertEqual(len(lines), 4)
        self.assertEqual(first['sources']['closed_requests']['request_file'],
                         {'path': 'analysis/judge/closed_submissions_to_evaluate.json', 'sha256': digest,
                          'schema': 'gdm-judge-closed-requests/1', 'submission_ids': 1,
                          'ignored_keys': ['generated_at']})
        self.assertEqual(again['closed_submission_requests']['evaluated'], ['sub-000003'])
        keyword = next(e for e in lines if e['submission']['submission_id'] == 'sub-000003')
        request = keyword['closed_submission_request']
        self.assertEqual((request['judge_reason'], request['request_file'], request['request_sha256'],
                          request['requested_via']),
                         ('D1=4 judge_only sub-000003', 'analysis/judge/closed_submissions_to_evaluate.json',
                          digest, ['judge_file']))
        self.assertEqual(request['hypothetical_static_checks'],
                         {'passed': False, 'stage': 'keyword_filter', 'detail': 'Forbidden text: sorry'})
        markers = next(e for e in lines if e['submission']['submission_id'] == 'sub-000006')
        self.assertEqual((markers['validity'], markers['reason']),
                         ('unresolved', 'Independent proof checking not established'))
        self.assertEqual(markers['closed_submission_request']['requested_via'], ['cli', 'judge_file'])
        self.assertEqual(markers['closed_submission_request']['hypothetical_static_checks']['stage'],
                         'source_integrity')

    def test_retry_unresolved_only_touches_requested_closed_submissions(self):
        class Untrusted(FakeBackend):
            def compare(self, template, source):
                super().compare(template, source)
                return SemanticCheck(False, False, None, details='no record')
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            self.evaluate(run_dir, Untrusted(), ids=['sub-000002'])
            fake = FakeBackend()
            self.evaluate(run_dir, fake, retry_unresolved=True)
            self.assertEqual(len(fake.calls), 2, 'accepted only: the closed one is not retried unless named')
            self.evaluate(run_dir, fake, retry_unresolved=True, ids=['sub-000002'])
            self.assertEqual(len(fake.calls), 3)
            latest = self.script.latest_evaluations(run_dir / 'evaluations' / 'evaluations.jsonl')
        self.assertEqual(latest['sub-000002']['validity'], 'genuine')
        self.assertIn('closed_submission_request', latest['sub-000002'])

    def test_invalid_requests_are_refused_before_any_write(self):
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            refused = {
                'sub-000001': 'outcome accepted, not problem_closed; it is evaluated by default',
                'sub-000007': 'outcome rejected, not problem_closed; --include-rejected',
                'sub-000099': 'not a submission',
                'sub-0123456789abcdef': 'agent-visible public id',
                'post-000001': 'not an internal submission id',
            }
            fake = FakeBackend()
            for sid, message in refused.items():
                with self.assertRaises(self.script.ClosedRequestError) as caught:
                    self.evaluate(run_dir, fake, ids=[sid])
                self.assertIn(message, caught.exception.problems[0])
            with self.assertRaises(self.script.ClosedRequestError):
                self.script.closed_requests(run_dir, [' , '])
            bad_files = ({'schema': 'other', 'submission_ids': [], 'reasons': {}},
                         {'schema': 'gdm-judge-closed-requests/1', 'submission_ids': 'sub-000002', 'reasons': {}},
                         {'schema': 'gdm-judge-closed-requests/1', 'submission_ids': ['sub-000002']},
                         {'schema': 'gdm-judge-closed-requests/1', 'submission_ids': ['sub-000002'],
                          'reasons': {'sub-000003': 'x'}},
                         {'schema': 'gdm-judge-closed-requests/1', 'submission_ids': ['sub-000002'],
                          'reasons': {'sub-000002': 4}})
            path = run_dir / 'analysis' / 'judge' / 'closed_submissions_to_evaluate.json'
            path.parent.mkdir(parents=True)
            for data in bad_files:
                path.write_text(json.dumps(data))
                with self.assertRaises(self.script.ClosedRequestError):
                    self.script.closed_requests(run_dir, None, path)
            path.write_text('{"schema": ')
            with self.assertRaises(self.script.ClosedRequestError):
                self.script.closed_requests(run_dir, None, path)
            self.assertEqual(fake.calls, [])
            self.assertFalse((run_dir / 'evaluations' / 'evaluations.jsonl').exists())

    def test_empty_request_file_selects_nothing_extra(self):
        fake = FakeBackend()
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            self.write_request(run_dir, [])
            summary = self.evaluate(run_dir, fake, file=self.script.CLOSED_REQUEST_FILE)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(summary['sources']['closed_requests']['requested'], [])

    def test_timeline_reads_the_real_outcome(self):
        """research/analysis/timeline.py finds ids by nested key search; the marker must not shadow them."""
        timeline = load_script('timeline_closed', ROOT / 'research' / 'analysis' / 'timeline.py')
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            self.evaluate(run_dir, FakeBackend(), ids=['sub-000005'])
            problems = []
            streams = timeline.evaluator_streams(run_dir, problems)
        verdicts = [e for stream in streams for e in stream if e['kind'] == 'evaluator_verdict']
        closed = next(e for e in verdicts if e['ids']['submission_id'] == 'sub-000005')
        self.assertEqual((closed['ids']['outcome'], closed['ids']['agent_id'], closed['ids']['problem_id'],
                          closed['validity']), ('problem_closed', 'agent-001', ERDOS6, 'fraudulent'))
        self.assertEqual(problems, [])

    # ---------------------------------------------------------------- main()
    def run_main(self, run_dir, argv, live=()):
        fake = FakeBackend()
        fake.validation = object()
        fake.runner = type('Runner', (), {'docker': 'docker', 'lock_path': None})()
        out = io.StringIO()
        with patch.object(ev, 'DockerComparatorBackend', return_value=fake) as backend, \
                patch.object(ev, 'live_experiment_containers', return_value=list(live)), \
                patch.object(ev, 'cleanup_orphans', return_value=[]), \
                patch.object(self.script, 'FrozenDataset', return_value=self.dataset), \
                contextlib.redirect_stdout(out):
            code = self.script.main(['--run-dir', str(run_dir)] + argv)
        fake.constructed = backend.call_count
        return code, fake, out.getvalue()

    def test_default_request_path_flag(self):
        args = self.script.parse_args(['--run-dir', 'r', '--include-closed-from'])
        self.assertIs(args.include_closed_from, self.script.CLOSED_REQUEST_FILE)
        args = self.script.parse_args(['--run-dir', 'r', '--include-closed-from', 'x.json', '--dry-run'])
        self.assertEqual((args.include_closed_from, args.dry_run), (Path('x.json'), True))
        self.assertIsNone(self.script.parse_args(['--run-dir', 'r']).include_closed_from)

    def test_main_dry_run_lists_requests_without_docker_or_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            self.write_request(run_dir, ['sub-000002', 'sub-000005'])
            code, fake, out = self.run_main(run_dir, ['--include-closed-from', '--dry-run'])
            self.assertFalse((run_dir / 'evaluations' / 'evaluations.jsonl').exists())
        self.assertEqual((code, fake.constructed, fake.calls), (0, 0, []))
        self.assertIn('sub-000002 %s problem_closed sqlite judge-requested' % GREEN, out)
        self.assertIn('2 judge-requested problem_closed: 0 already evaluated', out)
        self.assertIn('4 pending of 4 selected', out)

    def test_main_refuses_invalid_requests_before_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            code, fake, out = self.run_main(run_dir, ['--include-closed-ids', 'sub-000004'])
            code_missing, _, out_missing = self.run_main(run_dir, ['--include-closed-from'])
            self.assertFalse((run_dir / 'evaluations' / 'evaluations.jsonl').exists())
        self.assertEqual((code, fake.constructed, fake.calls), (2, 0, []))
        self.assertIn('sub-000004: outcome accepted, not problem_closed', out)
        self.assertEqual(code_missing, 2)
        self.assertIn('analysis/judge/closed_submissions_to_evaluate.json: unreadable request file', out_missing)

    def test_main_respects_the_interlock_and_evaluates_serially(self):
        live = [{'id': 'c1', 'name': 'gdm-judge-1', 'image': 'x', 'kind': 'judge', 'compose_project': None}]
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            self.make_run(run_dir)
            self.write_request(run_dir, ['sub-000005'])
            code, fake, out = self.run_main(run_dir, ['--include-closed-from'], live)
            self.assertEqual((code, fake.calls), (2, []))
            self.assertIn('Refusing to start comparator containers: 1 judge container(s) running', out)
            self.assertFalse((run_dir / 'evaluations' / 'evaluations.jsonl').exists())
            code, fake, out = self.run_main(run_dir, ['--include-closed-from', '--include-closed-ids', 'sub-000002'])
            lines = self.lines(run_dir)
            summary = json.loads((run_dir / 'evaluations' / 'summary.json').read_text())
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 4)
        self.assertIn('4 selected submissions (2 judge-requested problem_closed), 4 to evaluate', out)
        self.assertIn('sub-000005 %s problem_closed (judge-requested): fraudulent' % ERDOS6, out)
        self.assertEqual(summary['closed_submission_requests']['evaluated'], ['sub-000002', 'sub-000005'])
        self.assertFalse(summary['interlock']['forced'])
        self.assertTrue(all('evaluated_while_live' not in e for e in lines))
        via = {e['submission']['submission_id']: e['closed_submission_request']['requested_via']
               for e in lines if 'closed_submission_request' in e}
        self.assertEqual(via, {'sub-000002': ['cli'], 'sub-000005': ['judge_file']})


if __name__ == '__main__':
    unittest.main()
