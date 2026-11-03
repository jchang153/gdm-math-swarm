#!/usr/bin/env python3
"""Run a task's tests against a Python solution and print a per-test report.

Usage: python3 run_tests.py TASK_ID [path/to/solution.py]

The solution defaults to problems/TASK_ID/solution.py; the tests are
problems/TASK_ID/tests.py. Every assert in check(candidate) is one test. All
tests run in order in one Python process (state persists between tests); each
test has its own time limit and the whole run has a total limit. A test is
PASS, FAIL (assertion false), ERROR (an exception) or TIMEOUT. Output printed
by the solution is captured and shown (truncated) after the report.
"""
# Process layout (--sandbox mode): a supervisor reads the length-framed request
# from stdin and forks a zygote, then reads the result key; the zygote forks one
# worker per attempt. The worker (own process group, stdout/stderr captured) loads
# the solution and runs the steps from a start index, reporting over a pipe. The
# supervisor enforces the per-test and total time limits, restarts a worker that
# ended at the next test, and prints one result line tagged with the key.
import ast
import json
import os
import select
import signal
import sys
import time

MARKER = '@@GDM_CODE_RESULT '
PASS, FAIL, ERROR, TIMEOUT = 'PASS', 'FAIL', 'ERROR', 'TIMEOUT'
OUTCOMES = (PASS, FAIL, ERROR, TIMEOUT)
DEFAULT_PER_TEST_TIMEOUT = 10.0
DEFAULT_TOTAL_TIMEOUT = 120.0
DEFAULT_OUTPUT_LIMIT = 4096        # bytes of solution output kept (head)
DETAIL_LIMIT = 300                 # characters per test detail
SOURCE_LIMIT = 300                 # characters of test source per test in results
REPORT_LIMIT = 7000                # characters of the formatted report
KILL_GRACE = 2.0                   # seconds past a deadline before SIGKILL
MAX_PAYLOAD = 64 * 1024 * 1024


# --------------------------------------------------------------- parsing (host)
def _unparse(node):
    if hasattr(ast, 'unparse'):
        return ast.unparse(node)
    raise RuntimeError('Python 3.9 or newer is needed to parse tests')


def _contains_assert(node):
    return any(isinstance(n, ast.Assert) for n in ast.walk(node))


def parse_tests(test_source):
    """{'prelude', 'param', 'steps'} from a tests.py text (never executes it).

    The body of ``def check(candidate)`` is split into its top-level statements.
    A statement that contains an assert is a test (numbered from 1); any other
    statement is setup and runs in order. Module-level statements other than
    ``check`` (imports, helpers) form the prelude. ``assert X == Y[, msg]`` is
    split so the harness can report the actual value of X.
    """
    tree = ast.parse(test_source)
    check, prelude = None, []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'check' and check is None:
            check = node
        else:
            prelude.append(node)
    if check is None:
        raise ValueError('The tests define no check(candidate) function')
    params = check.args.posonlyargs + check.args.args if hasattr(check.args, 'posonlyargs') else check.args.args
    param = params[0].arg if params else 'candidate'
    steps, number = [], 0
    for node in check.body:
        segment = ast.get_source_segment(test_source, node) or _unparse(node)
        step = {'code': _unparse(node), 'source': segment}
        if _contains_assert(node):
            number += 1
            step['index'] = number
            if (isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare)
                    and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)):
                step['lhs'] = _unparse(node.test.left)
                step['rhs'] = _unparse(node.test.comparators[0])
                step['msg'] = _unparse(node.msg) if node.msg is not None else None
        else:
            step['index'] = None
        steps.append(step)
    if not number:
        raise ValueError('The tests contain no assert statements')
    module = ast.Module(body=prelude, type_ignores=[])
    return {'prelude': _unparse(module) if prelude else '', 'param': param, 'steps': steps}


def build_payload(solution_source, parsed, entry_point, per_test_timeout=DEFAULT_PER_TEST_TIMEOUT,
                  total_timeout=DEFAULT_TOTAL_TIMEOUT, output_limit=DEFAULT_OUTPUT_LIMIT, memory_bytes=None):
    return {'solution': solution_source, 'entry_point': entry_point, 'prelude': parsed['prelude'],
            'param': parsed['param'], 'steps': parsed['steps'], 'per_test_timeout': float(per_test_timeout),
            'total_timeout': float(total_timeout), 'output_limit': int(output_limit),
            'memory_bytes': memory_bytes}


def frame_input(payload, nonce):
    """stdin bytes for --sandbox: b'<length>\\n' + payload JSON + nonce line."""
    data = json.dumps(payload).encode('utf-8')
    return str(len(data)).encode() + b'\n' + data + nonce.encode('ascii') + b'\n'


def parse_result(stdout, nonce):
    """The supervisor's result dict from its stdout, or None (last line carrying the nonce)."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', 'replace')
    key = MARKER + nonce + ' '
    index = stdout.rfind(key)
    if index < 0:
        return None
    end = stdout.find('\n', index)
    try:
        result = json.loads(stdout[index + len(key):end if end >= 0 else len(stdout)])
    except ValueError:
        return None
    return result if isinstance(result, dict) and isinstance(result.get('tests'), list) else None


# --------------------------------------------------------------- worker
def _clip(text, limit=DETAIL_LIMIT):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + '...[%d more characters]' % (len(text) - limit)


def _worker(payload, wfd, start, gen):
    """Load the solution, run steps[start:], report each step on ``wfd``. Never returns."""
    _write, _dumps, _repr, _str, _type = os.write, json.dumps, repr, str, type
    _setitimer, _signal, _bool, _compile, _exec, _eval = signal.setitimer, signal.signal, bool, compile, exec, eval
    _exit, _SystemExit, _AssertionError, _BaseException = os._exit, SystemExit, AssertionError, BaseException
    per_test = float(payload['per_test_timeout'])

    class _Timeout(BaseException):
        pass

    def flush():
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except BaseException:
                pass

    def send(message):
        flush()
        message['gen'] = gen
        try:
            data = (_dumps(message) + '\n').encode('utf-8', 'replace')
            while data:
                data = data[_write(wfd, data):]
        except BaseException:
            _exit(70)

    def alarm(signum, frame):
        raise _Timeout()

    def arm():
        _signal(signal.SIGALRM, alarm)
        _setitimer(signal.ITIMER_REAL, per_test, 0.25)  # re-fires if the solution swallows it

    def disarm():
        _setitimer(signal.ITIMER_REAL, 0)

    def describe(exc):
        try:
            name = _type(exc).__name__
            if isinstance(exc, _SystemExit):
                return 'SystemExit(%s): the solution tried to exit' % _clip(_repr(exc.code), 80)
            text = _str(exc)
            return _clip(name + (': ' + text if text else ''))
        except BaseException:
            return 'exception (could not be described)'

    def safe_repr(value):
        try:
            return _clip(_repr(value), 200)
        except _Timeout:
            raise
        except BaseException:
            return '<unprintable value>'

    if payload.get('memory_bytes'):
        try:
            import resource
            limit = int(payload['memory_bytes'])
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:
            pass
    module = {'__name__': '__solution__', '__builtins__': __builtins__}
    try:
        try:
            arm()
            _exec(_compile(payload['solution'], 'solution.py', 'exec'), module)
            candidate = module.get(payload['entry_point'])
            if candidate is None:
                raise NameError("the solution defines no function '%s'" % payload['entry_point'])
            if not callable(candidate):
                raise TypeError("'%s' is not callable" % payload['entry_point'])
            tests = {'__name__': '__tests__', '__builtins__': __builtins__, payload['param']: candidate}
            if payload.get('prelude'):
                _exec(_compile(payload['prelude'], 'tests.py', 'exec'), tests)
        finally:
            disarm()
    except _Timeout:
        send({'t': 'load_error', 'detail': 'loading the solution took longer than %g s' % per_test})
        _exit(0)
    except BaseException as exc:
        send({'t': 'load_error', 'detail': describe(exc)})
        _exit(0)
    send({'t': 'loaded'})
    steps = payload['steps']
    for position in range(start, len(steps)):
        step = steps[position]
        send({'t': 'start', 'pos': position})
        outcome, detail = PASS, None
        try:
            try:
                arm()
                if step.get('lhs') is not None:
                    left = _eval(_compile(step['lhs'], 'tests.py', 'eval'), tests)
                    right = _eval(_compile(step['rhs'], 'tests.py', 'eval'), tests)
                    if not _bool(left == right):
                        outcome = FAIL
                        detail = 'got ' + safe_repr(left)
                        if step.get('msg') is not None:
                            detail += '; message: ' + _clip(_eval(_compile(step['msg'], 'tests.py', 'eval'), tests))
                else:
                    _exec(_compile(step['code'], 'tests.py', 'exec'), tests)
            finally:
                disarm()
        except _Timeout:
            outcome, detail = TIMEOUT, 'exceeded the %g s time limit' % per_test
        except _AssertionError as exc:
            outcome, detail = FAIL, (describe(exc) if _str(exc) else 'assertion failed')
        except BaseException as exc:
            outcome, detail = ERROR, describe(exc)
        send({'t': 'result', 'pos': position, 'outcome': outcome, 'detail': detail})
    send({'t': 'done'})
    _exit(0)


# --------------------------------------------------------------- zygote
def _read_line(fd):
    data = b''
    while not data.endswith(b'\n'):
        chunk = os.read(fd, 1)
        if not chunk:
            return None
        data += chunk
    return data.decode('ascii', 'replace').strip()


def _zygote(payload, cmd_r, res_w, cap_w):
    """Fork one worker per 'spawn START GEN' command; report its pid and exit status."""
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    while True:
        line = _read_line(cmd_r)
        if not line or not line.startswith('spawn '):
            os._exit(0)
        _, start, gen = line.split()
        start, gen = int(start), int(gen)
        pid = os.fork()
        if pid == 0:
            try:
                os.setpgid(0, 0)
                os.close(cmd_r)
                os.dup2(cap_w, 1)
                os.dup2(cap_w, 2)
                os.close(cap_w)
                sys.stdout = open(1, 'w', closefd=False, errors='replace', buffering=1)
                sys.stderr = open(2, 'w', closefd=False, errors='replace', buffering=1)
                _worker(payload, res_w, start, gen)
            finally:
                os._exit(71)
        message = json.dumps({'t': 'spawned', 'gen': gen, 'pid': pid}) + '\n'
        os.write(res_w, message.encode())
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except InterruptedError:
                continue
            except ChildProcessError:
                status = -1
                break
        code = os.waitstatus_to_exitcode(status) if hasattr(os, 'waitstatus_to_exitcode') and status >= 0 else status
        os.write(res_w, (json.dumps({'t': 'exited', 'gen': gen, 'status': code}) + '\n').encode())


# --------------------------------------------------------------- supervisor
def _read_exact(fd, count):
    chunks, left = [], count
    while left:
        chunk = os.read(fd, min(left, 1 << 20))
        if not chunk:
            raise EOFError('payload truncated')
        chunks.append(chunk)
        left -= len(chunk)
    return b''.join(chunks)


def _kill_group(pid):
    for target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        try:
            target()
        except (OSError, ProcessLookupError):
            pass


class _Supervisor:
    def __init__(self, payload):
        self.payload = payload
        self.steps = payload['steps']
        self.per_test = float(payload['per_test_timeout'])
        self.total = float(payload['total_timeout'])
        self.output_limit = int(payload.get('output_limit') or DEFAULT_OUTPUT_LIMIT)
        self.results = {}          # step position -> (outcome, detail)
        self.load_error = None
        self.loaded = False
        self.gen = 0
        self.pid = None
        self.running = None        # (position, started) of the step in progress
        self.spawned_at = None
        self.killed = False
        self.status = 'completed'
        self.output, self.output_bytes = bytearray(), 0
        self.restarts = 0
        self.no_progress = 0

    def remaining(self):
        return [p for p in range(len(self.steps)) if p not in self.results]

    def spawn(self, start):
        self.gen += 1
        self.pid, self.running, self.loaded, self.killed = None, None, False, False
        self.spawned_at = time.monotonic()
        self._progress_mark = len(self.results)
        try:
            os.write(self.cmd_w, ('spawn %d %d\n' % (start, self.gen)).encode())
        except OSError:  # the zygote is gone (e.g. killed by the solution): nothing more can run
            for position in self.remaining():
                self.results[position] = (ERROR, 'not run: the test process ended')
            self.status = 'harness_error'
            self.done = True

    def kill(self):
        if self.pid:
            _kill_group(self.pid)
        self.killed = True

    def on_message(self, message):
        if not isinstance(message, dict) or message.get('gen') != self.gen:
            return
        kind = message.get('t')
        if kind == 'spawned':
            self.pid = message.get('pid')
        elif kind == 'loaded':
            self.loaded = True
        elif kind == 'load_error':
            self.load_error = _clip(message.get('detail') or 'the solution failed to load')
            self.finish_load_error()
        elif kind == 'start':
            position = message.get('pos')
            if isinstance(position, int) and 0 <= position < len(self.steps) and position not in self.results:
                self.running = (position, time.monotonic())
        elif kind == 'result':
            position = message.get('pos')
            if self.running and position == self.running[0] and message.get('outcome') in OUTCOMES:
                detail = message.get('detail')
                self.results[position] = (message['outcome'], _clip(detail) if detail is not None else None)
                self.running = None
        elif kind == 'done':
            self.kill()
            self.done = True
        elif kind == 'exited':
            self.on_exit(message.get('status'))

    def finish_load_error(self):
        for position in self.remaining():
            self.results[position] = (ERROR, 'not run: the solution failed to load')
        self.status = 'load_error'
        self.kill()
        self.done = True

    def on_exit(self, status):
        if self.done:
            return
        if not self.loaded:
            self.load_error = 'the solution process ended while loading (exit status %s)' % status
            self.finish_load_error()
            return
        if self.running:
            position = self.running[0]
            if position not in self.results:
                self.results[position] = (ERROR, 'the test process ended during this test (exit status %s)' % status)
            self.running = None
        left = self.remaining()
        if not left:
            self.done = True
            return
        if len(self.results) == self._progress_mark:
            self.no_progress += 1
            # The process died outside any test: charge the next test so the run always advances.
            self.results[left[0]] = (ERROR, 'the test process ended before this test (exit status %s)' % status)
            left = left[1:]
            if not left:
                self.done = True
                return
        self.restarts += 1
        self.spawn(left[0])

    def check_deadlines(self, now):
        if now > self.started + self.total:
            for position in self.remaining():
                self.results[position] = (TIMEOUT, 'not finished: the total time limit of %g s was reached'
                                          % self.total if not self.running or self.running[0] != position
                                          else 'exceeded the total time limit of %g s' % self.total)
            self.status = 'total_timeout'
            self.kill()
            self.done = True
            return
        if self.killed:
            return
        if self.running and now > self.running[1] + self.per_test + KILL_GRACE:
            self.results[self.running[0]] = (TIMEOUT, 'exceeded the %g s time limit' % self.per_test)
            self.kill()
        elif not self.loaded and self.spawned_at and now > self.spawned_at + self.per_test + KILL_GRACE + 5:
            self.load_error = 'loading the solution took longer than %g s' % self.per_test
            self.finish_load_error()

    def run(self, cmd_w, res_r, cap_r, zygote):
        self.cmd_w, self.done, self.started = cmd_w, False, time.monotonic()
        self.spawn(0)
        buffer = b''
        open_fds = [res_r, cap_r]
        while not self.done:
            now = time.monotonic()
            self.check_deadlines(now)
            if self.done:
                break
            wait = 0.2
            try:
                readable, _, _ = select.select(open_fds, [], [], wait)
            except InterruptedError:
                continue
            for fd in readable:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b''
                if not chunk:
                    open_fds.remove(fd)
                    continue
                if fd == cap_r:
                    self.output_bytes += len(chunk)
                    room = self.output_limit - len(self.output)
                    if room > 0:
                        self.output += chunk[:room]
                    continue
                buffer += chunk
                if len(buffer) > 4 * MAX_PAYLOAD:
                    buffer = b''
                while b'\n' in buffer and not self.done:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        self.on_message(json.loads(line.decode('utf-8', 'replace')))
                    except ValueError:
                        pass
            if res_r not in open_fds and not self.done:
                for position in self.remaining():
                    self.results[position] = (ERROR, 'not run: the test process ended')
                self.status = 'harness_error' if self.status == 'completed' else self.status
                break
        self.kill()
        # Drain what the solution printed so far (bounded, non-blocking).
        end = time.monotonic() + 0.2
        while cap_r in open_fds and time.monotonic() < end:
            readable, _, _ = select.select([cap_r], [], [], 0.05)
            if not readable:
                break
            chunk = os.read(cap_r, 65536)
            if not chunk:
                break
            self.output_bytes += len(chunk)
            room = self.output_limit - len(self.output)
            if room > 0:
                self.output += chunk[:room]
        return self.report()

    def report(self):
        tests, setup = [], []
        for position, step in enumerate(self.steps):
            outcome, detail = self.results.get(position, (ERROR, 'not run'))
            if step.get('index') is None:
                if outcome != PASS:
                    setup.append({'position': position, 'source': _clip(step['source'], SOURCE_LIMIT),
                                  'outcome': outcome, 'detail': detail})
                continue
            tests.append({'index': step['index'], 'source': _clip(step['source'], SOURCE_LIMIT),
                          'outcome': outcome, 'detail': detail})
        return {'status': self.status, 'tests': tests, 'passed': sum(t['outcome'] == PASS for t in tests),
                'total': len(tests), 'load_error': self.load_error, 'setup_errors': setup,
                'output': bytes(self.output).decode('utf-8', 'replace'), 'output_bytes': self.output_bytes,
                'output_truncated': self.output_bytes > len(self.output), 'restarts': self.restarts,
                'elapsed_seconds': round(time.monotonic() - self.started, 3)}


def sandbox_main():
    """--sandbox: stdin = b'<length>\\n' + payload JSON + b'<nonce>\\n'; prints the result line."""
    header = _read_line(0)
    length = int(header or '0')
    if not 0 < length <= MAX_PAYLOAD:
        raise ValueError('bad payload length')
    payload = json.loads(_read_exact(0, length).decode('utf-8'))
    res_r, res_w = os.pipe()
    cap_r, cap_w = os.pipe()
    cmd_r, cmd_w = os.pipe()
    out_fd = os.dup(1)
    zygote = os.fork()
    if zygote == 0:
        try:
            for fd in (res_r, cap_r, cmd_w, out_fd):
                os.close(fd)
            os.dup2(cap_w, 1)
            os.dup2(cap_w, 2)
            _zygote(payload, cmd_r, res_w, cap_w)
        finally:
            os._exit(72)
    for fd in (res_w, cap_w, cmd_r):
        os.close(fd)
    # Read only now, after the fork: no process that runs solution code holds the key.
    nonce = _read_line(0) or ''
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        result = _Supervisor(payload).run(cmd_w, res_r, cap_r, zygote)
    except Exception as exc:  # report, never hang
        result = {'status': 'harness_error', 'tests': [], 'passed': 0, 'total': 0,
                  'load_error': None, 'error': type(exc).__name__ + ': ' + str(exc)}
    try:
        os.close(cmd_w)
    except OSError:
        pass
    _kill_group(zygote)
    data = ('\n' + MARKER + nonce + ' ' + json.dumps(result) + '\n').encode('utf-8')
    while data:
        data = data[os.write(out_fd, data):]
    try:
        os.waitpid(zygote, 0)
    except OSError:
        pass
    return 0


# --------------------------------------------------------------- report
def format_report(result, label=None, limit=REPORT_LIMIT):
    """Human-readable per-test report."""
    tests = result.get('tests') or []
    passed, total = result.get('passed', 0), result.get('total', len(tests))
    head = []
    name = (label + ': ') if label else ''
    status = result.get('status')
    if status == 'load_error':
        head.append('%sthe solution could not be loaded: %s' % (name, result.get('load_error')))
        head.append('0 of %d tests passed (no test was run).' % total)
    else:
        head.append('%s%d of %d tests passed%s.' % (name, passed, total,
                                                   '' if passed == total else ' (%d did not)' % (total - passed)))
        if status == 'total_timeout':
            head.append('The run stopped at the total time limit.')
        elif status == 'harness_error':
            head.append('The test run ended early; tests that were not run count as errors.')
    for item in result.get('setup_errors') or []:
        head.append('Setup statement failed (%s): %s\n    %s' % (item['outcome'], _clip(item['source'], 160),
                                                              item.get('detail')))
    lines = []
    for test in tests:
        source = test['source'].strip().replace('\n', '\n        ')
        line = 'Test %d: %-7s %s' % (test['index'], test['outcome'], source)
        if test.get('detail') and status != 'load_error':
            line += '\n        ' + str(test['detail'])
        lines.append((test['outcome'], line))
    tail = []
    output = result.get('output') or ''
    if output:
        if result.get('output_truncated'):
            tail.append('Solution output (first %d of %d bytes):' % (len(output.encode('utf-8', 'replace')),
                                                                      result.get('output_bytes', 0)))
        else:
            tail.append('Solution output:')
        tail.append(_clip(output, 2000))
    if status == 'load_error':
        lines = []
    text = '\n'.join(head + [l for _, l in lines] + tail)
    if len(text) <= limit:
        return text
    # Too long: keep tests that did not pass first, then as many passing ones as fit.
    budget = limit - len('\n'.join(head + tail)) - 200
    kept, omitted = [], 0
    for outcome, line in lines:
        if outcome != PASS and budget - len(line) - 1 > 0:
            kept.append(line)
            budget -= len(line) + 1
        elif outcome != PASS:
            omitted += 1
    passing = [l for o, l in lines if o == PASS]
    shown_pass = 0
    for line in passing:
        if budget - len(line) - 1 <= 0:
            break
        budget -= len(line) + 1
        shown_pass += 1
    order = {l: i for i, (_, l) in enumerate(lines)}
    body = sorted(kept + passing[:shown_pass], key=order.get)
    note = '[%d passing and %d other test lines omitted for length]' % (len(passing) - shown_pass, omitted)
    return '\n'.join(head + body + [note] + tail)[:limit]


# --------------------------------------------------------------- local runner
def run_local(solution_source, test_source, entry_point, per_test_timeout=DEFAULT_PER_TEST_TIMEOUT,
              total_timeout=DEFAULT_TOTAL_TIMEOUT, output_limit=DEFAULT_OUTPUT_LIMIT, memory_bytes=None,
              python=None, harness_path=None, harness_source=None):
    """Run the harness in a local subprocess (no container). Returns the result dict
    (status 'harness_error' with no tests when the harness gave no result)."""
    import secrets
    import subprocess
    parsed = parse_tests(test_source)
    payload = build_payload(solution_source, parsed, entry_point, per_test_timeout, total_timeout, output_limit,
                            memory_bytes)
    nonce = secrets.token_hex(16)
    if harness_source is not None:
        command = [python or sys.executable, '-I', '-c', harness_source, '--sandbox']
    else:
        command = [python or sys.executable, '-I', harness_path or os.path.abspath(__file__), '--sandbox']
    import tempfile
    with tempfile.TemporaryFile() as out:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.DEVNULL,
                                   cwd=tempfile.gettempdir(), start_new_session=True)
        try:
            process.communicate(frame_input(payload, nonce), timeout=float(total_timeout) + 30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        out.seek(0, 2)
        size = out.tell()
        out.seek(max(0, size - 8 * 1024 * 1024))
        stdout = out.read()
    result = parse_result(stdout, nonce)
    if result is None:
        result = {'status': 'harness_error', 'tests': [], 'passed': 0, 'total': len(
            [s for s in parsed['steps'] if s.get('index') is not None]), 'load_error': None}
    return result


def cli_main(argv):
    import argparse
    parser = argparse.ArgumentParser(prog='run_tests.py', description='Run a task\'s tests against a solution.')
    parser.add_argument('task_id')
    parser.add_argument('solution', nargs='?', help='default: problems/TASK_ID/solution.py')
    parser.add_argument('--problems-dir', default=None, help='default: the problems/ folder next to this script')
    parser.add_argument('--per-test-timeout', type=float, default=DEFAULT_PER_TEST_TIMEOUT)
    parser.add_argument('--total-timeout', type=float, default=DEFAULT_TOTAL_TIMEOUT)
    args = parser.parse_args(argv)
    problems = args.problems_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'problems')
    try:
        with open(os.path.join(problems, 'tasks.json'), encoding='utf-8') as handle:
            listing = {t['task_id']: t for t in json.load(handle)}
    except (OSError, ValueError) as exc:
        print('Cannot read the task list in %s: %s' % (problems, exc))
        return 2
    task = listing.get(args.task_id)
    if task is None:
        print('Unknown task %r' % args.task_id)
        return 2
    folder = os.path.join(problems, task.get('dir') or args.task_id)
    solution_path = args.solution or os.path.join(folder, 'solution.py')
    try:
        with open(solution_path, encoding='utf-8') as handle:
            solution = handle.read()
        with open(os.path.join(folder, 'tests.py'), encoding='utf-8') as handle:
            tests = handle.read()
    except OSError as exc:
        print('Cannot read %s' % exc)
        return 2
    result = run_local(solution, tests, task['entry_point'], args.per_test_timeout, args.total_timeout)
    print(format_report(result, args.task_id))
    return 0 if result.get('total') and result.get('passed') == result.get('total') else 1


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--sandbox':
        sys.exit(sandbox_main())
    sys.exit(cli_main(sys.argv[1:]))
