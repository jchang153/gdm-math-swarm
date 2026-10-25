"""Bounded Lean execution in a disposable, networkless compiler container.

One `compile(source)` = one fresh `docker run --rm` of the pinned compiler image:
no network, read-only root filesystem, no host mounts, source over stdin into a
private tmpfs. The Lean process is limited *inside* the container by coreutils
`timeout -v -s KILL <timeout>`; a host-side safety timer (timeout + startup
allowance) covers container creation/teardown. An optional per-instance FIFO
gate bounds concurrent containers; time spent waiting there is reported as
`queue_seconds` and never counts against the Lean limit.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import json
import logging
import re
import shlex
import subprocess
import threading
import time
import uuid

LOG = logging.getLogger(__name__)

SUBMISSION_PATH = '/tmp/Submission.lean'
PROJECT_DIR = '/opt/lean-project'
# Every compiler container carries this label so leftovers can be swept/checked:
#   docker ps -a --filter label=gdm-judge=1
JUDGE_LABEL = 'gdm-judge=1'
NAME_PREFIX = 'gdm-lean-'
# Host-side allowance over the in-container Lean limit for container creation,
# startup and teardown. Exceeding it is also reported as status 'timeout'.
STARTUP_ALLOWANCE_SECONDS = 90
CLEANUP_TIMEOUT_SECONDS = 30
DEFAULT_OUTPUT_LIMIT_BYTES = 65536
_READ_CHUNK = 65536


def _seconds(value) -> str:
    """Duration argument for coreutils `timeout` (positive, plain decimal)."""
    value = float(value)
    if not value > 0:
        raise ValueError('timeout must be positive')
    return str(int(value)) if value == int(value) else repr(value)


def lean_shell(timeout) -> str:
    """The operational judge command.

    With `-s KILL` coreutils `timeout` exits 137 exactly like an OOM kill, so
    `-v` is what distinguishes the two: it prints `timeout: sending signal KILL
    to command 'lake'` to stderr only when the limit fires.
    """
    return (f'cat > {SUBMISSION_PATH} && cd {PROJECT_DIR} && '
            f'timeout -v -s KILL {_seconds(timeout)} lake env lean {SUBMISSION_PATH}')


# Researcher-side measurement variant (validation only). Identical Lean
# invocation, timeout wrapper and container limits; a python3 parent records
# wall time, the peak RSS of the largest waited-for descendant (the Lean
# process) and the container cgroup's peak memory (which also counts mapped
# .olean page cache charged to this container).
METRICS_MARK = '@@GDM_METRICS '
_MEASURE_PY = (
    'import json,resource,subprocess,sys,time\n'
    't=time.monotonic()\n'
    'rc=subprocess.call(["timeout","-v","-s","KILL",sys.argv[1],"lake","env","lean",'
    '"' + SUBMISSION_PATH + '"])\n'
    'e=time.monotonic()-t\n'
    'u=resource.getrusage(resource.RUSAGE_CHILDREN)\n'
    'try:\n'
    '    peak=int(open("/sys/fs/cgroup/memory.peak").read())\n'
    'except Exception:\n'
    '    peak=None\n'
    'sys.stdout.flush()\n'
    'sys.stderr.write("\\n' + METRICS_MARK + '"+json.dumps(dict(lean_rc=rc,lean_seconds=e,'
    'max_rss_bytes=u.ru_maxrss*1024,user_seconds=u.ru_utime,system_seconds=u.ru_stime,'
    'cgroup_memory_peak_bytes=peak))+"\\n")\n'
    'sys.exit(rc if rc>=0 else 128-rc)\n'
)


def measured_lean_shell(timeout) -> str:
    return (f'cat > {SUBMISSION_PATH} && cd {PROJECT_DIR} && exec python3 -c '
            + shlex.quote(_MEASURE_PY) + ' ' + _seconds(timeout))


# Lean 4 wording: one warning per declaration whose value contains an explicit
# admission (`sorry` or the `admit` tactic). Real-Lean confirmation of the 4.27
# wording is recorded by scripts/validate_judge.py (data/validation/judge-*.json).
SORRY_WARNING = re.compile(r"declaration uses ['‘]sorry['’]")
_SORRY_BYTES = re.compile(rb"declaration uses (?:'|\xe2\x80\x98)sorry(?:'|\xe2\x80\x99)")
# Lean diagnostics for the submission itself (`/tmp/Submission.lean:L:C: ...`).
_DIAGNOSTIC_BYTES = re.compile(rb'\n' + re.escape(SUBMISSION_PATH.encode()) + rb':\d{1,9}:\d{1,9}: ')
# Printed by `timeout -v` only when the in-container Lean limit fires.
_TIMEOUT_NOTICE_BYTES = re.compile(rb'\ntimeout: sending signal KILL to command ')
_PATTERN_WINDOW = 64  # >= longest match of the streaming patterns above
_STREAM_PATTERNS = dict(sorry=_SORRY_BYTES, diagnostic=_DIAGNOSTIC_BYTES)
_STDERR_PATTERNS = dict(_STREAM_PATTERNS, timeout_notice=_TIMEOUT_NOTICE_BYTES)

# 125: docker could not create/run the container (incl. missing image with
# --pull never); 126/127: command not runnable.
SETUP_EXIT_CODES = (125, 126, 127)
# 137 = SIGKILL. With the timeout notice it is the in-container Lean limit;
# without it, a memory-cgroup OOM kill or an external kill. Not a Lean verdict.
KILLED_EXIT_CODES = (137,)
# `timeout` exits 137 after -s KILL (observed, coreutils 9.4 in the pinned
# image); 124 is its documented status for other signals. Either counts as the
# Lean limit only together with the timeout notice.
TIMEOUT_EXIT_CODES = (124, 137)
# Positive evidence that the toolchain/environment (not the submission) failed:
# Lake/elan/shell error lines without a file location, or filesystem/toolchain
# failures. Only consulted for a nonzero exit with no submission diagnostic.
_ENV_FAILURE = re.compile(
    r'^(?:error|uncaught exception|lake|elan|sh|/bin/sh|timeout|cat|python3?): '
    r'|Read-only file system|dubious ownership|URL has changed|is not installed'
    r'|No such file or directory|Permission denied|No space left on device', re.M)

_DIAGNOSTIC = re.compile(
    r'^(?P<file>[^\n:]+):(?P<line>\d+):(?P<column>\d+): (?P<severity>error|warning|info)'
    r'(?:\((?P<code>[^)\n]*)\))?: ', re.M)


@dataclass(frozen=True)
class CompileResult:
    status: str  # success | error | timeout | killed | setup_error
    exit_code: int | None = None
    stdout: str = ''
    stderr: str = ''
    sorry_count: int | None = None
    elapsed_seconds: float | None = None  # docker run wall time, excluding queue wait
    metrics: dict | None = None
    queue_seconds: float | None = None  # wait at the concurrency gate (None: no gate)
    output_truncated: bool = False  # head+tail kept; counts still cover the full output
    timeout_source: str | None = None  # 'container' (Lean limit) | 'host' (safety timer)

    @property
    def succeeded(self):
        return self.status == 'success' and self.exit_code == 0 and self.sorry_count == 0


def parse_diagnostics(output: str) -> list[dict]:
    """Split Lean's `file:line:col: severity: text` output into messages."""
    matches = list(_DIAGNOSTIC.finditer(output))
    messages = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        messages.append(dict(line=int(match['line']), column=int(match['column']),
                             severity=match['severity'], code=match['code'],
                             text=output[match.end():end].rstrip('\n')))
    return messages


def split_metrics(stderr: str):
    """Remove the measurement wrapper's trailing metrics line from stderr."""
    index = stderr.rfind(METRICS_MARK)
    if index < 0:
        return stderr, None
    line_end = stderr.find('\n', index)
    line_end = len(stderr) if line_end < 0 else line_end
    try:
        metrics = json.loads(stderr[index + len(METRICS_MARK):line_end])
    except ValueError:
        return stderr, None
    cleaned = stderr[:index].rstrip('\n')
    rest = stderr[line_end + 1:]
    return (cleaned + ('\n' + rest if rest else '')), metrics


class _Capture:
    """Bounded head+tail copy of one output stream.

    Patterns are counted over the *complete* stream (a carry-over window spans
    chunk boundaries), so truncation can never hide an admission warning.
    """
    def __init__(self, limit: int, patterns: dict):
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit
        self.head, self.tail, self.total = bytearray(), bytearray(), 0
        self.patterns = patterns
        self.counts = dict.fromkeys(patterns, 0)
        # A virtual leading newline lets line-anchored patterns match at offset 0.
        self._carry = dict.fromkeys(patterns, b'\n')

    def feed(self, chunk: bytes):
        self.total += len(chunk)
        room = self.head_limit - len(self.head)
        rest = chunk
        if room > 0:
            self.head += chunk[:room]
            rest = chunk[room:]
        if rest:
            self.tail += rest
            if len(self.tail) > self.tail_limit:
                del self.tail[:len(self.tail) - self.tail_limit]
        for name, pattern in self.patterns.items():
            buffer = self._carry[name] + chunk
            end = 0
            for match in pattern.finditer(buffer):
                self.counts[name] += 1
                end = match.end()
            # Keep only bytes after the last counted match: no double counting.
            self._carry[name] = buffer[max(end, len(buffer) - _PATTERN_WINDOW + 1):]

    @property
    def omitted(self) -> int:
        return self.total - len(self.head) - len(self.tail)

    def text(self) -> str:
        if not self.omitted:
            return bytes(self.head + self.tail).decode('utf-8', errors='replace')
        head = bytes(self.head).decode('utf-8', errors='replace')
        tail = bytes(self.tail).decode('utf-8', errors='replace')
        return f'{head}\n[... {self.omitted} bytes of output omitted ...]\n{tail}'


class _FifoGate:
    """At most `limit` concurrent holders, admitted in arrival order."""
    def __init__(self, limit: int):
        self.limit, self.active = limit, 0
        self._waiting = deque()
        self._condition = threading.Condition()

    def acquire(self) -> float:
        started = time.monotonic()
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
            try:
                while self._waiting[0] is not ticket or self.active >= self.limit:
                    self._condition.wait()
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
            self._waiting.popleft()
            self.active += 1
            self._condition.notify_all()
        return time.monotonic() - started

    def release(self):
        with self._condition:
            self.active -= 1
            self._condition.notify_all()

    def status(self) -> dict:
        with self._condition:
            return dict(limit=self.limit, active=self.active, waiting=len(self._waiting))


def _pump(stream, capture):
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            capture.feed(chunk)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _write_all(stream, data: bytes):
    try:
        view = memoryview(data)
        while view:
            written = stream.write(view)
            view = view[written if written is not None else len(view):]
    except (OSError, ValueError):
        pass  # the container exited early; its exit status says why
    finally:
        try:
            stream.close()
        except OSError:
            pass


class DockerLeanCompiler:
    """Image must have a prebuilt pinned project at /opt/lean-project.

    Source arrives over stdin, never via a host directory mount. Container has no
    network, Docker socket, credentials, or conference-service connection.
    `timeout` limits the Lean process inside the container; `max_concurrent`
    (int) bounds this instance's concurrent containers with a FIFO gate;
    `output_limit_bytes` caps each captured stream (head + tail kept).
    """
    def __init__(self, image='gdm-math-swarm-lean:local', timeout=120, memory='2g', cpus=1,
                 max_concurrent=None, output_limit_bytes=DEFAULT_OUTPUT_LIMIT_BYTES,
                 startup_allowance=STARTUP_ALLOWANCE_SECONDS):
        _seconds(timeout)
        if max_concurrent is not None and (isinstance(max_concurrent, bool)
                                           or not isinstance(max_concurrent, int) or max_concurrent < 1):
            raise ValueError('max_concurrent must be None or a positive integer')
        if int(output_limit_bytes) < 256:
            raise ValueError('output_limit_bytes must be at least 256')
        self.image, self.timeout, self.memory, self.cpus = image, timeout, memory, cpus
        self.max_concurrent = max_concurrent
        self.output_limit_bytes = int(output_limit_bytes)
        self.startup_allowance = startup_allowance
        self._gate = _FifoGate(max_concurrent) if max_concurrent else None

    @property
    def host_timeout(self) -> float:
        return float(self.timeout) + float(self.startup_allowance)

    def gate_status(self) -> dict | None:
        return self._gate.status() if self._gate else None

    def docker_command(self, name: str, shell: str | None = None) -> list[str]:
        return ['docker', 'run', '--rm', '--name', name, '--label', JUDGE_LABEL,
                '--pull', 'never', '--log-driver', 'none', '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '128', '--memory', str(self.memory), '--cpus', str(self.cpus),
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m', '-i', self.image,
                'sh', '-c', shell if shell is not None else lean_shell(self.timeout)]

    def compile(self, source: str) -> CompileResult:
        return self._run(source, lean_shell(self.timeout))

    def compile_measured(self, source: str) -> CompileResult:
        """Researcher validation only: same container, Lean call and limit, plus metrics."""
        return self._run(source, measured_lean_shell(self.timeout))

    def _run(self, source: str, shell: str) -> CompileResult:
        queue_seconds = self._gate.acquire() if self._gate else None
        try:
            return self._execute(source, shell, queue_seconds)
        finally:
            if self._gate:
                self._gate.release()

    def _execute(self, source: str, shell: str, queue_seconds) -> CompileResult:
        name = NAME_PREFIX + uuid.uuid4().hex
        out = _Capture(self.output_limit_bytes, _STREAM_PATTERNS)
        err = _Capture(self.output_limit_bytes, _STDERR_PATTERNS)
        start = time.monotonic()
        try:
            process = subprocess.Popen(self.docker_command(name, shell), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            return CompileResult('setup_error', stderr=str(exc), elapsed_seconds=time.monotonic() - start,
                                 queue_seconds=queue_seconds)
        # Explicit UTF-8: Lean sources are Unicode and must not depend on the
        # host locale (an ASCII locale would otherwise crash on `ℕ`).
        threads = [threading.Thread(target=_write_all, args=(process.stdin, source.encode('utf-8')), daemon=True),
                   threading.Thread(target=_pump, args=(process.stdout, out), daemon=True),
                   threading.Thread(target=_pump, args=(process.stderr, err), daemon=True)]
        for thread in threads:
            thread.start()
        try:
            code = process.wait(timeout=self.host_timeout)
        except subprocess.TimeoutExpired:
            # Killing the Docker client alone does not stop its container.
            remove_container(name)
            try:
                process.kill()
                process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                LOG.warning('docker client for %s did not exit after kill', name)
            _join(threads, 10)
            return CompileResult('timeout', None, out.text(), err.text(),
                                 elapsed_seconds=time.monotonic() - start, queue_seconds=queue_seconds,
                                 output_truncated=bool(out.omitted or err.omitted), timeout_source='host')
        _join(threads, CLEANUP_TIMEOUT_SECONDS)
        elapsed = time.monotonic() - start
        stdout = out.text()
        stderr, metrics = split_metrics(err.text())
        common = dict(elapsed_seconds=elapsed, metrics=metrics, queue_seconds=queue_seconds,
                      output_truncated=bool(out.omitted or err.omitted))
        if code in SETUP_EXIT_CODES:
            return CompileResult('setup_error', code, stdout, stderr, **common)
        if code in TIMEOUT_EXIT_CODES and err.counts['timeout_notice']:
            return CompileResult('timeout', code, stdout, stderr, timeout_source='container', **common)
        if code in KILLED_EXIT_CODES:
            return CompileResult('killed', code, stdout, stderr, **common)
        diagnostics = out.counts['diagnostic'] + err.counts['diagnostic']
        if code != 0 and not diagnostics and _ENV_FAILURE.search(stdout + '\n' + stderr):
            return CompileResult('setup_error', code, stdout, stderr, **common)
        return CompileResult('success' if code == 0 else 'error', code, stdout, stderr,
                             out.counts['sorry'] + err.counts['sorry'], **common)


def _join(threads, timeout):
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))


def remove_container(name: str, docker: str = 'docker') -> bool:
    """Force-remove one compiler container; tolerant of a hung Docker CLI.

    Returns False (and logs) when removal could not be confirmed. The
    in-container Lean limit still ends such a container, and `--rm` removes it.
    """
    for _ in range(2):
        try:
            result = subprocess.run([docker, 'rm', '-f', name], capture_output=True,
                                    timeout=CLEANUP_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 or b'No such container' in (result.stderr or b''):
            return True
    LOG.warning('could not confirm removal of compiler container %s (label %s)', name, JUDGE_LABEL)
    return False


def judge_containers(docker: str = 'docker') -> list[str] | None:
    """Names of existing (running or exited) compiler containers; None if Docker is unavailable."""
    try:
        result = subprocess.run([docker, 'ps', '-a', '--filter', 'label=' + JUDGE_LABEL,
                                 '--format', '{{.Names}}'], capture_output=True, encoding='utf-8',
                                timeout=CLEANUP_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return sorted(result.stdout.split()) if result.returncode == 0 else None


def image_id(image: str) -> str | None:
    """Local image ID, or None when Docker or the image is unavailable."""
    try:
        result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                                capture_output=True, encoding='utf-8', timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None
