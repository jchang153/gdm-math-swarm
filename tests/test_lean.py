import io
import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from research_swarm import lean
from research_swarm.lean import (CompileResult, DockerLeanCompiler, JUDGE_LABEL, _Capture, _FifoGate,
                                 judge_containers, parse_diagnostics, remove_container, split_metrics)

SORRY = b"/tmp/Submission.lean:3:8: warning: declaration uses 'sorry'\n"
NOTICE = b"timeout: sending signal KILL to command 'lake'\nKilled\n"


class _Sink:
    def __init__(self):
        self.data, self.closed = bytearray(), False
    def write(self, view):
        self.data += bytes(view)
        return len(view)
    def close(self):
        self.closed = True


class FakePopen:
    """Stands in for the `docker run` client process; never runs Docker."""
    instances = []

    def __init__(self, cmd, code=0, out=b'', err=b'', hang=False, on_wait=None, **kwargs):
        self.cmd, self.code, self.hang, self.on_wait, self.kwargs = cmd, code, hang, on_wait, kwargs
        self.stdin, self.stdout, self.stderr = _Sink(), io.BytesIO(out), io.BytesIO(err)
        self.killed, self.wait_timeouts = False, []
        FakePopen.instances.append(self)

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.on_wait:
            self.on_wait()
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        return -9 if self.killed else self.code

    def kill(self):
        self.killed = True


def popen(**behaviour):
    return patch('research_swarm.lean.subprocess.Popen',
                 side_effect=lambda cmd, **kwargs: FakePopen(cmd, **behaviour, **kwargs))


class LeanTests(unittest.TestCase):
    def setUp(self):
        FakePopen.instances = []

    def last(self):
        return FakePopen.instances[-1]

    def test_networkless_readonly_no_mounts_labelled_and_lean_limit_inside(self):
        with popen():
            r = DockerLeanCompiler().compile('example : True := by trivial')
        self.assertTrue(r.succeeded)
        cmd = self.last().cmd
        for flag, value in [('--network', 'none'), ('--label', JUDGE_LABEL), ('--pull', 'never'),
                            ('--log-driver', 'none'), ('--memory', '2g'), ('--cpus', '1')]:
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn('--read-only', cmd)
        self.assertNotIn('-v', cmd)
        self.assertNotIn('--volume', cmd)
        self.assertNotIn('--mount', cmd)
        self.assertTrue(cmd[cmd.index('--name') + 1].startswith('gdm-lean-'))
        self.assertIn('timeout -v -s KILL 120 lake env lean /tmp/Submission.lean', cmd[-1])
        self.assertEqual(self.last().wait_timeouts, [120 + lean.STARTUP_ALLOWANCE_SECONDS])
        self.assertIsNone(r.queue_seconds)
        self.assertFalse(r.output_truncated)

    def test_admission_warning(self):
        with popen(out=SORRY):
            r = DockerLeanCompiler().compile('')
        self.assertEqual((r.status, r.sorry_count), ('success', 1))
        self.assertFalse(r.succeeded)

    def test_host_timeout_removes_container_and_kills_client(self):
        with popen(hang=True), patch('research_swarm.lean.subprocess.run',
                                     return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
            r = DockerLeanCompiler().compile('')
        self.assertEqual((r.status, r.timeout_source, r.sorry_count), ('timeout', 'host', None))
        self.assertFalse(r.succeeded)
        name = self.last().cmd[self.last().cmd.index('--name') + 1]
        self.assertEqual(run.call_args.args[0], ['docker', 'rm', '-f', name])
        self.assertTrue(self.last().killed)

    def test_hung_cleanup_does_not_raise(self):
        with popen(hang=True), patch('research_swarm.lean.subprocess.run',
                                     side_effect=subprocess.TimeoutExpired('docker', 30)) as run, \
                self.assertLogs('research_swarm.lean', 'WARNING'):
            r = DockerLeanCompiler().compile('')
        self.assertEqual(r.status, 'timeout')
        self.assertEqual(run.call_count, 2)
        self.assertTrue(self.last().killed)

    def test_in_container_timeout_is_distinct_from_oom_kill(self):
        with popen(code=137, err=NOTICE):
            r = DockerLeanCompiler().compile('')
        self.assertEqual((r.status, r.timeout_source, r.exit_code), ('timeout', 'container', 137))
        with popen(code=137, err=b'Killed\n'):
            r = DockerLeanCompiler().compile('')
        self.assertEqual((r.status, r.timeout_source), ('killed', None))
        # The notice alone (e.g. echoed by a submission) never turns success into timeout.
        with popen(code=0, err=NOTICE):
            self.assertEqual(DockerLeanCompiler().compile('').status, 'success')

    def test_source_is_utf8_on_stdin_independent_of_locale(self):
        source = 'theorem t : ∀ n : ℕ, n = n := fun _ => rfl'
        with popen():
            DockerLeanCompiler().compile(source)
        self.assertEqual(bytes(self.last().stdin.data), source.encode('utf-8'))
        self.assertTrue(self.last().stdin.closed)

    def test_status_mapping(self):
        cases = [(0, b'', 'success', 0), (1, b'/tmp/Submission.lean:3:2: error: boom', 'error', 0),
                 (125, b'', 'setup_error', None), (126, b'', 'setup_error', None),
                 (127, b'', 'setup_error', None), (137, b'', 'killed', None),
                 (0, SORRY + SORRY.replace(b':3:', b':4:'), 'success', 2)]
        for code, out, status, count in cases:
            with self.subTest(code=code), popen(code=code, out=out):
                r = DockerLeanCompiler().compile('')
            self.assertEqual((r.status, r.sorry_count), (status, count))
            self.assertEqual(r.succeeded, (status, count) == ('success', 0))
            self.assertIsNotNone(r.elapsed_seconds)

    def test_environment_failures_are_setup_errors(self):
        env_failures = [b"error: mathlib: URL has changed; deleting '/opt/lean-project/.lake/packages/mathlib'\n"
                        b"error: could not remove: Read-only file system (error code: 30)\n",
                        b"error: toolchain 'leanprover/lean4:v4.27.0' is not installed\n",
                        b"cat: write error: No space left on device\n"]
        for err in env_failures:
            with self.subTest(err=err), popen(code=1, err=err):
                self.assertEqual(DockerLeanCompiler().compile('').status, 'setup_error')
        # A located diagnostic means Lean ran: an ordinary compile error, whatever else is printed.
        with popen(code=1, out=b'/tmp/Submission.lean:1:0: error: unknown module prefix\n',
                   err=b'error: something\n'):
            self.assertEqual(DockerLeanCompiler().compile('').status, 'error')
        # Lean crashes without environment evidence stay compile errors.
        with popen(code=134, err=b'Stack overflow detected. Aborting.\n'):
            self.assertEqual(DockerLeanCompiler().compile('').status, 'error')

    def test_docker_cli_missing_is_setup_error(self):
        with patch('research_swarm.lean.subprocess.Popen', side_effect=FileNotFoundError('docker')):
            r = DockerLeanCompiler().compile('')
        self.assertEqual(r.status, 'setup_error')

    def test_output_cap_keeps_head_and_tail_but_counts_everything(self):
        filler = b'/tmp/Submission.lean:1:0: info: ' + b'x' * 100 + b'\n'
        out = filler * 1000 + SORRY + filler * 1000 + b'TAIL-END\n'
        with popen(out=out):
            r = DockerLeanCompiler(output_limit_bytes=4096).compile('')
        self.assertTrue(r.output_truncated)
        self.assertEqual(r.sorry_count, 1)
        self.assertFalse(r.succeeded)
        self.assertLess(len(r.stdout.encode()), 4096 + 100)
        self.assertIn('bytes of output omitted', r.stdout)
        self.assertTrue(r.stdout.startswith('/tmp/Submission.lean:1:0: info: x'))
        self.assertTrue(r.stdout.endswith('TAIL-END\n'))

    def test_capture_counts_matches_split_across_chunks(self):
        stream = b'noise\n' + SORRY + b'mid\n' + SORRY.replace(b"'", '‘'.encode(), 1).replace(b"'", '’'.encode(), 1)
        capture = _Capture(256, lean._STDERR_PATTERNS)
        for i in range(len(stream)):
            capture.feed(stream[i:i + 1])
        self.assertEqual(capture.counts['sorry'], 2)
        self.assertEqual(capture.counts['diagnostic'], 2)
        whole = _Capture(1 << 16, lean._STDERR_PATTERNS)
        whole.feed(SORRY)  # diagnostic at offset 0 (no preceding newline) still counts
        self.assertEqual((whole.counts['diagnostic'], whole.counts['sorry'], whole.omitted), (1, 1, 0))
        self.assertEqual(whole.text(), SORRY.decode())

    def test_gate_bounds_concurrency_and_reports_queue_time(self):
        lock, state = threading.Lock(), dict(active=0, peak=0)

        def on_wait():
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
            time.sleep(0.05)
            with lock:
                state['active'] -= 1

        compiler = DockerLeanCompiler(max_concurrent=2)
        results = []
        with popen(on_wait=on_wait):
            threads = [threading.Thread(target=lambda: results.append(compiler.compile(''))) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(state['peak'], 2)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(r.succeeded and r.queue_seconds is not None for r in results))
        self.assertGreater(max(r.queue_seconds for r in results), 0.04)
        self.assertEqual(compiler.gate_status(), dict(limit=2, active=0, waiting=0))

    def test_fifo_gate_admits_in_arrival_order(self):
        gate, order = _FifoGate(1), []
        gate.acquire()

        def worker(i):
            gate.acquire()
            order.append(i)
            gate.release()

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=worker, args=(i,)))
            threads[-1].start()
            deadline = time.monotonic() + 5
            while gate.status()['waiting'] < i + 1 and time.monotonic() < deadline:
                time.sleep(0.001)
        gate.release()
        for thread in threads:
            thread.join()
        self.assertEqual(order, [0, 1, 2, 3, 4])

    def test_constructor_validation(self):
        for bad in [dict(max_concurrent=0), dict(max_concurrent=True), dict(max_concurrent=1.5),
                    dict(timeout=0), dict(output_limit_bytes=10)]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                DockerLeanCompiler(**bad)

    def test_old_positional_compile_result_still_constructs(self):
        r = CompileResult('success', 0, '', '', 0, 1.0, None)
        self.assertTrue(r.succeeded)
        self.assertEqual((r.queue_seconds, r.output_truncated, r.timeout_source), (None, False, None))

    def test_measured_variant_same_container_policy_and_metrics(self):
        metrics = ('{"lean_rc": 1, "lean_seconds": 3.5, "max_rss_bytes": 10, "user_seconds": 1, '
                   '"system_seconds": 1, "cgroup_memory_peak_bytes": 20}')
        with popen(code=1, out=b'out', err=('err\n@@GDM_METRICS ' + metrics + '\n').encode()):
            compiler = DockerLeanCompiler(timeout=60)
            r = compiler.compile_measured('x')
        cmd = self.last().cmd
        plain = compiler.docker_command('n')
        self.assertEqual(cmd[:cmd.index('--name')], plain[:plain.index('--name')])
        self.assertEqual(cmd[cmd.index('--name') + 2:-1], plain[plain.index('--name') + 2:-1])
        self.assertIn('"timeout","-v","-s","KILL",sys.argv[1],"lake","env","lean","/tmp/Submission.lean"', cmd[-1])
        self.assertTrue(cmd[-1].endswith(' 60'))
        self.assertIn('timeout -v -s KILL 60 ', plain[-1])
        self.assertEqual(r.metrics['lean_seconds'], 3.5)
        self.assertEqual(r.stderr, 'err')
        self.assertEqual(r.status, 'error')

    def test_container_helpers(self):
        with patch('research_swarm.lean.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, 'gdm-lean-b\ngdm-lean-a\n', '')) as run:
            self.assertEqual(judge_containers(), ['gdm-lean-a', 'gdm-lean-b'])
        self.assertIn('label=' + JUDGE_LABEL, run.call_args.args[0])
        with patch('research_swarm.lean.subprocess.run', side_effect=OSError):
            self.assertIsNone(judge_containers())
        gone = subprocess.CompletedProcess([], 1, b'', b'Error response from daemon: No such container: x')
        with patch('research_swarm.lean.subprocess.run', return_value=gone):
            self.assertTrue(remove_container('x'))

    def test_parse_diagnostics(self):
        out = ("/tmp/Submission.lean:46:2: error: Provide a complete proof.\nn : ℕ\n⊢ goal\n"
               "/tmp/Submission.lean:43:8: warning: declaration uses 'sorry'\n"
               "/tmp/Submission.lean:50:0: error(lean.unknownIdentifier): Unknown identifier `x`\n")
        d = parse_diagnostics(out)
        self.assertEqual([(m['line'], m['severity'], m['code']) for m in d],
                         [(46, 'error', None), (43, 'warning', None), (50, 'error', 'lean.unknownIdentifier')])
        self.assertEqual(d[0]['text'], 'Provide a complete proof.\nn : ℕ\n⊢ goal')

    def test_split_metrics_absent(self):
        self.assertEqual(split_metrics('plain'), ('plain', None))


# Opt-in actual toolchain checks. Never pretend mocks validated Lean semantics.
from research_swarm.judge import TaskTemplate, judge_submission  # noqa: E402

REAL_TEMPLATE = '''import Mathlib
-- EDITABLE START
-- EDITABLE END
theorem target : False := by
-- EDITABLE START
  fail
-- EDITABLE END
'''


@unittest.skipUnless(os.environ.get('GDM_REAL_LEAN_IMAGE'), 'Pinned precompiled Lean image not available')
class RealLeanTests(unittest.TestCase):
    def test_genuine(self):
        compiler = DockerLeanCompiler(os.environ['GDM_REAL_LEAN_IMAGE'])
        source = REAL_TEMPLATE.replace(': False', ': True').replace('  fail', '  trivial')
        self.assertTrue(judge_submission(TaskTemplate(source, 'target'), source, compiler).accepted)

    def test_notation_changes_meaning_and_still_passes_baseline(self):
        compiler = DockerLeanCompiler(os.environ['GDM_REAL_LEAN_IMAGE'])
        source = REAL_TEMPLATE.replace('-- EDITABLE START\n', '-- EDITABLE START\nlocal notation "False" => True\n',
                                       1).replace('  fail', '  trivial')
        self.assertTrue(judge_submission(TaskTemplate(REAL_TEMPLATE, 'target'), source, compiler).accepted)

    @unittest.skipUnless(os.environ.get('GDM_VALIDATED_COMPARATOR_IMAGE') and os.environ.get('GDM_COMPARATOR_SECCOMP'),
                         'Independent comparator image/sandbox unvalidated')
    def test_independent_comparator_separates_genuine_and_shadow(self):
        # Evaluator-owned API; imported lazily so its changes cannot break this module.
        from research_swarm.evaluator import DockerComparatorBackend, evaluate_submission
        # REAL_TEMPLATE is synthetic, so it has no pilot self-comparison in the record.
        backend = DockerComparatorBackend(os.environ['GDM_VALIDATED_COMPARATOR_IMAGE'],
                                          seccomp_profile=os.environ['GDM_COMPARATOR_SECCOMP'], validated_image=True,
                                          require_self_compare=False)
        source = REAL_TEMPLATE.replace(': False', ': True').replace('  fail', '  trivial')
        self.assertEqual(evaluate_submission(TaskTemplate(source, 'target'), source, backend).validity, 'genuine')
        exploit = REAL_TEMPLATE.replace('-- EDITABLE START\n', '-- EDITABLE START\nlocal notation "False" => True\n',
                                        1).replace('  fail', '  trivial')
        self.assertEqual(evaluate_submission(TaskTemplate(REAL_TEMPLATE, 'target'), exploit, backend).validity,
                         'fraudulent')
