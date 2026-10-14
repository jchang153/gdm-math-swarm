"""Trusted comparator container entry (researcher-only; never in agent images).

stdin: one JSON object {"mode": "verify" | "upstream" | "probe",
"challenge": pristine Lean source whose proof is an admission,
"solution": submitted Lean source, "theorem_name": "Namespace.name",
"step_timeout": seconds, "deadline_seconds": seconds}. stdout: exactly one JSON
record, written only by this process, carrying the SHA-256 of the raw request
(``request_sha256``) so the host can bind it to what it sent. Every other
process in the container is untrusted output-wise.

Trust boundary (docs/evaluator.md):
* Steps follow leanprover/comparator 14fda6a `compareIt`: sandboxed
  `lake build Challenge`, sandboxed `lean4export Challenge -- targets`, then the
  same for `Solution`, with `buildLandrunArgs`' exact landrun argument layout.
* `Solution.lean` is written only after the Challenge export is captured.
* Untrusted processes never inherit this process's stdout/stderr; their output
  goes to capture files that are never parsed for a verdict.
* After each sandboxed step every other process in the container is SIGKILLed.
* Landlock V5 (landrun c91b41a) has no signal scoping, so sandboxed code can
  still send signals to this process. The entry must be PID 1 (preflight) and
  sets every catchable asynchronous signal to SIG_IGN; the kernel never
  delivers SIGKILL/SIGSTOP to a namespace init from inside its namespace. Any
  other failure still ends in one fail-closed record (BaseException is caught).
* A step that exceeds its time limit reports ``timeout``, never a build failure.
* The verdict comes only from `gdm_verify_exports`, which reproduces
  `verifyMatch`/`runKernel` and runs no untrusted code.
* Refuses to run (``sandbox_unavailable``) as root, when not PID 1, without
  Landlock ABI >= 5 (landrun's --best-effort would otherwise silently degrade,
  possibly to no sandbox), or when AF_UNIX sockets are not denied by the
  seccomp profile.
* Every record carries ``entry_sha256``, the SHA-256 of this file as baked into
  the image; validation records and evaluations bind it.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import socket
import subprocess
import sys
import time

FROZEN = Path('/opt/lean-project')
WORK = Path(os.environ.get('GDM_WORK', '/work'))
CHECK = WORK / 'check'
TRUSTED = WORK / 'trusted'
COMPARATOR = Path('/opt/comparator/.lake/build/bin/comparator')
VERIFIER = Path('/opt/comparator/.lake/build/bin/gdm_verify_exports')
TOOLCHAIN_BIN = Path('/opt/elan/toolchains/leanprover--lean4---v4.27.0/bin')
PERMITTED_AXIOMS = ['propext', 'Quot.sound', 'Classical.choice']
# comparator 14fda6a `primitiveTargets` (also exported, as upstream does).
PRIMITIVES = ['Nat.add', 'Nat.sub', 'Nat.mul', 'Nat.pow', 'Nat.gcd', 'Nat.div', 'Nat.mod',
              'Nat.beq', 'Nat.ble', 'Nat.land', 'Nat.lor', 'Nat.xor', 'Nat.shiftLeft',
              'Nat.shiftRight', 'String.ofList']
MISMATCH_PREFIXES = ('Challenge and solution theorem statement do not match:',
                     'Const does not match between challenge and target ',
                     "Challenge and solution constant kind don't match:")
MIN_LANDLOCK_ABI = 5  # landrun c91b41a requests Landlock V5 (best effort).
MAX_SOURCE_BYTES = 1 << 20
TAIL = 6000
SYS_LANDLOCK_CREATE_RULESET = 444  # same number on aarch64 and x86_64
ENTRY_VERSION = 'gdm-comparator-entry/3'
# Catchable asynchronous signals ignored by PID 1 (never SIGCHLD or fault signals).
IGNORED_SIGNALS = tuple(getattr(signal, name) for name in (
    'SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT', 'SIGUSR1', 'SIGUSR2', 'SIGALRM', 'SIGVTALRM',
    'SIGPROF', 'SIGTSTP', 'SIGTTIN', 'SIGTTOU', 'SIGXCPU', 'SIGPIPE') if hasattr(signal, name))
# Probe items: these must be allowed; signal/exec items are informational
# (Landlock V5 has no signal scoping; exec is not a required denial); every
# other item must be denied.
PROBE_ALLOWED = frozenset({'write_dot_lake', 'read_frozen_project'})
PROBE_INFORMATIONAL_PREFIXES = ('signal_pid1_', 'exec_')
PROBE_REQUIRED = frozenset({'write_dot_lake', 'write_project_root', 'write_trusted_dir', 'write_pid1_stdout',
                            'unix_socket', 'unix_socketpair', 'unix_socketcall_i386', 'tcp_connect',
                            'ptrace_attach_pid1', 'read_frozen_project'})
# Required denials that only exist on some hosts (i386 ABI: x86_64 only).
PROBE_MAY_NOT_APPLY = frozenset({'unix_socketcall_i386'})


def entry_digest():
    """SHA-256 of this entry file as it exists in the image (None if unreadable)."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return None


def ignore_signals():
    for number in IGNORED_SIGNALS:
        signal.signal(number, signal.SIG_IGN)


def signals_ignored():
    return all(signal.getsignal(number) == signal.SIG_IGN for number in IGNORED_SIGNALS)


def _reset_signals():
    """Child processes start with default dispositions (SIG_IGN survives exec)."""
    for number in IGNORED_SIGNALS:
        signal.signal(number, signal.SIG_DFL)


def landlock_abi():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return int(libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1)))


def af_unix_denied():
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
    except OSError:
        return True
    return False


def preflight():
    checks = {'uid': os.getuid(), 'pid': os.getpid(), 'landlock_abi': landlock_abi(),
              'af_unix_denied': af_unix_denied(), 'landrun': shutil.which('landrun'),
              'signals_ignored': signals_ignored()}
    problems = []
    if checks['uid'] == 0:
        problems.append('running as root')
    if checks['pid'] != 1:
        problems.append('entry is not PID 1')
    if not checks['signals_ignored']:
        problems.append('PID 1 signal dispositions not ignored')
    if checks['landlock_abi'] < MIN_LANDLOCK_ABI:
        problems.append('Landlock ABI below %d' % MIN_LANDLOCK_ABI)
    if not checks['af_unix_denied']:
        problems.append('AF_UNIX sockets are not denied (seccomp profile missing)')
    if not checks['landrun']:
        problems.append('landrun missing')
    return checks, problems


def landrun_command(cmd, args, env_pass, readable, writable, executable):
    """Exact layout of comparator 14fda6a `buildLandrunArgs`."""
    out = ['landrun', '--best-effort', '--ro', '/', '--rw', '/dev', '-ldd', '-add-exec']
    for name in env_pass:
        out += ['--env', name]
    for path in readable:
        out += ['--ro', str(path)]
    for path in writable:
        out += ['--rwx', str(path)]
    for path in executable:
        out += ['--rox', str(path)]
    return out + [cmd] + list(args)


def kill_others():
    """SIGKILL every other process we may signal, then reap. Returns survivors."""
    try:
        os.kill(-1, signal.SIGKILL)  # excludes the caller and PID 1
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 60  # D-state processes under I/O pressure die slowly
    while time.monotonic() < deadline:
        try:
            while os.waitpid(-1, os.WNOHANG)[0] > 0:
                pass
        except ChildProcessError:
            pass
        others = [p for p in os.listdir('/proc') if p.isdigit() and int(p) not in (1, os.getpid())]
        others = [p for p in others if _state(p) not in ('Z', None)]
        if not others:
            return []
        time.sleep(0.05)
    return others


def _state(pid):
    try:
        return Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def _limits():
    _reset_signals()
    resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 30, 1 << 30))
    os.setsid()


class Runner:
    def __init__(self, step_timeout, deadline=None):
        self.step_timeout = step_timeout
        self.deadline = deadline  # time.monotonic() bound for the whole request
        self.steps = []
        self.tails = {}

    def limit(self):
        if self.deadline is None:
            return self.step_timeout
        return max(0, min(self.step_timeout, self.deadline - time.monotonic()))

    def failure(self, status, detail):
        """Classify a failed step: exceeding a time limit is never a build failure."""
        if self.steps and self.steps[-1]['status'] == 'timeout':
            return {'status': 'timeout', 'detail': '%s exceeded its time limit' % self.steps[-1]['name']}
        return {'status': status, 'detail': detail}

    def run(self, name, command, env, stdout_path=None, untrusted=False):
        log = TRUSTED / (name + '.log')
        start = time.monotonic()
        status, limit = 'ok', self.limit()
        if limit <= 0:
            self.steps.append({'name': name, 'status': 'timeout', 'exit_code': None, 'seconds': 0.0,
                               'timeout': 0, 'untrusted': untrusted, 'survivors_after_kill': 0})
            return False
        with open(log, 'wb') as err, open(stdout_path or os.devnull, 'wb') as out:
            proc = subprocess.Popen(command, cwd=CHECK, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out if stdout_path else err, stderr=err,
                                    preexec_fn=_limits)
            try:
                rc = proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                status, rc = 'timeout', None
        survivors = kill_others()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.steps.append({'name': name, 'status': status, 'exit_code': rc,
                           'seconds': round(time.monotonic() - start, 3), 'timeout': round(limit, 3),
                           'children_max_rss_bytes': usage.ru_maxrss * 1024,
                           'untrusted': untrusted, 'survivors_after_kill': len(survivors)})
        if survivors:
            raise RuntimeError('Processes survived SIGKILL after ' + name)
        text = log.read_bytes()[-TAIL:].decode('utf-8', 'replace')
        self.tails[name] = text
        return status == 'ok' and rc == 0


def setup_project(challenge):
    for path in (CHECK, TRUSTED):
        path.mkdir(parents=True)
    (WORK / 'home').mkdir(exist_ok=True)
    for name in ('lean-toolchain', 'lake-manifest.json'):
        shutil.copyfile(FROZEN / name, CHECK / name)
    # Root package options are kept so the frozen dependency traces stay valid;
    # Challenge/Solution reset the elaboration-relevant ones to Lean defaults,
    # matching the operational compiler (`lake env lean`, no package options).
    build = (FROZEN / 'lakefile.toml').read_text()
    for lib in ('Challenge', 'Solution'):
        build += ('\n[[lean_lib]]\nname = "%s"\n[lean_lib.leanOptions]\n'
                  'autoImplicit = true\nrelaxedAutoImplicit = true\n' % lib)
    (CHECK / 'lakefile.toml').write_text(build)
    for name in ('FormalConjectures', 'FormalConjecturesForMathlib', 'FormalConjecturesForMathlib.lean'):
        if (FROZEN / name).exists():
            (CHECK / name).symlink_to(FROZEN / name)
    lake = CHECK / '.lake'
    lake.mkdir()
    (lake / 'packages').symlink_to(FROZEN / '.lake/packages', target_is_directory=True)
    # Prebuilt root-package artifacts as a symlink tree: real (writable)
    # directories, read-only files. Lake adds Challenge/Solution outputs beside them.
    subprocess.run(['cp', '-rs', str(FROZEN / '.lake/build'), str(lake / 'build')], check=True)
    (CHECK / 'Challenge.lean').write_text(challenge, encoding='utf-8')


def base_env():
    return {'PATH': '%s:/usr/local/bin:/usr/bin:/bin' % TOOLCHAIN_BIN, 'HOME': str(WORK / 'home'),
            'LANG': 'C.UTF-8', 'LEAN_ABORT_ON_PANIC': '1'}


def trusted_output(command, env):
    return subprocess.run(command, cwd=CHECK, env=env, capture_output=True, text=True,
                          check=True, timeout=120, preexec_fn=_reset_signals).stdout.strip()


def classify(verdict):
    status, detail = verdict.get('status'), verdict.get('detail', '')
    if status == 'compare_failed' and detail.startswith(MISMATCH_PREFIXES):
        return {'status': 'mismatch', 'detail': detail}
    if status == 'axioms_failed' and detail.startswith("Illegal axiom detected: '"):
        return {'status': 'illegal_axiom', 'axiom': detail.split("'")[1], 'detail': detail}
    return {'status': status, 'detail': detail}


def deadline_of(request):
    seconds = request.get('deadline_seconds')
    return None if seconds is None else STARTED + float(seconds)


def verify(request, record):
    theorem = request['theorem_name']
    targets = [theorem] + PERMITTED_AXIOMS + PRIMITIVES
    runner = Runner(int(request.get('step_timeout', 600)), deadline_of(request))
    record['steps'], record['untrusted_output_tail'] = runner.steps, runner.tails
    setup_project(request['challenge'])
    env = base_env()
    lean_prefix = trusted_output(['lean', '--print-prefix'], env)
    git = shutil.which('git', path=env['PATH'])
    env['LEAN_PATH'] = trusted_output(['lake', 'env', 'printenv', 'LEAN_PATH'], env)
    build_args = dict(env_pass=['PATH', 'HOME', 'LEAN_ABORT_ON_PANIC'], readable=[CHECK],
                      writable=[CHECK / '.lake'], executable=[lean_prefix, git])
    export_args = dict(env_pass=['PATH', 'HOME', 'LEAN_PATH', 'LEAN_ABORT_ON_PANIC'],
                       readable=[CHECK, CHECK / '.lake'], writable=[], executable=[lean_prefix])
    if not runner.run('build_challenge', landrun_command('lake', ['build', 'Challenge'], **build_args), env):
        return runner.failure('challenge_failed', 'Trusted challenge build failed')
    if not runner.run('export_challenge', landrun_command('lean4export', ['Challenge', '--'] + targets,
                                                          **export_args), env, TRUSTED / 'challenge.export'):
        return runner.failure('challenge_failed', 'Trusted challenge export failed')
    # Only now does untrusted text enter the container's filesystem.
    (CHECK / 'Solution.lean').write_text(request['solution'], encoding='utf-8')
    if not runner.run('build_solution', landrun_command('lake', ['build', 'Solution'], **build_args),
                      env, untrusted=True):
        return runner.failure('solution_build_failed', 'Sandboxed Solution build failed')
    if not runner.run('export_solution', landrun_command('lean4export', ['Solution', '--'] + targets,
                                                         **export_args), env, TRUSTED / 'solution.export',
                      untrusted=True):
        return runner.failure('solution_export_failed', 'Sandboxed Solution export failed')
    config = TRUSTED / 'config.json'
    config.write_text(json.dumps({'theorem_names': [theorem], 'permitted_axioms': PERMITTED_AXIOMS}))
    record['export_bytes'] = {'challenge': (TRUSTED / 'challenge.export').stat().st_size,
                              'solution': (TRUSTED / 'solution.export').stat().st_size}
    out = TRUSTED / 'verdict.json'
    if not runner.run('verify_exports', [str(VERIFIER), str(config), str(TRUSTED / 'challenge.export'),
                                         str(TRUSTED / 'solution.export')], env, out):
        return runner.failure('error', 'Trusted verifier failed')
    lines = out.read_text().strip().splitlines()
    return classify(json.loads(lines[-1]))


def upstream(request, record):
    """Validation cross-check: unmodified comparator binary; exit code only."""
    runner = Runner(int(request.get('step_timeout', 600)) * 4, deadline_of(request))
    record['steps'], record['untrusted_output_tail'] = runner.steps, runner.tails
    setup_project(request['challenge'])
    (CHECK / 'Solution.lean').write_text(request['solution'], encoding='utf-8')
    (CHECK / 'config.json').write_text(json.dumps({
        'challenge_module': 'Challenge', 'solution_module': 'Solution',
        'theorem_names': [request['theorem_name']], 'permitted_axioms': PERMITTED_AXIOMS,
        'enable_nanoda': False}))
    runner.run('upstream_comparator', ['lake', 'env', str(COMPARATOR), 'config.json'], base_env(),
               untrusted=True)
    return {'status': 'upstream_exit', 'exit_code': runner.steps[-1]['exit_code'],
            'detail': 'exit code of unmodified comparator; output not interpreted'}


# Sandboxed probe helper (also unit-tested): an AF_UNIX socket through the
# 32-bit `socketcall` multiplexer must be unreachable (docs/evaluator.md, "Seccomp").
PROBE_I386 = r'''
def i386_socketcall_unix():
    # socketcall(SYS_SOCKET, {AF_UNIX, SOCK_STREAM, 0}) through the i386 ABI
    # (int 0x80) in a forked child; the seccomp filter may kill the caller.
    if platform.machine() != 'x86_64':
        return 'not_applicable'
    pid = os.fork()
    if pid == 0:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.mmap.restype = ctypes.c_void_p
            libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_long]
            page = libc.mmap(None, 4096, 7, 0x62, -1, 0)  # RWX; MAP_PRIVATE|MAP_ANONYMOUS|MAP_32BIT
            if not page or page == ctypes.c_void_p(-1).value:
                os._exit(250)
            args = page + 64
            ctypes.memmove(args, struct.pack('<3I', 1, 1, 0), 12)
            # push rbx; mov eax, 102; mov ebx, 1; mov ecx, args; int 0x80; pop rbx; ret
            code = (b'\x53\xb8\x66\x00\x00\x00\xbb\x01\x00\x00\x00\xb9' + struct.pack('<I', args)
                    + b'\xcd\x80\x5b\xc3')
            ctypes.memmove(page, code, len(code))
            rc = ctypes.CFUNCTYPE(ctypes.c_int)(page)()
            os._exit(200 if rc >= 0 else min(-rc, 199))
        except BaseException:
            os._exit(251)
    status = os.waitpid(pid, 0)[1]
    if os.WIFSIGNALED(status):
        return 'denied:signal%d' % os.WTERMSIG(status)
    code = os.WEXITSTATUS(status)
    return {200: 'allowed', 250: 'error:mmap', 251: 'error:exception'}.get(code, 'denied:%d' % code)
'''

PROBE = r'''
import ctypes, json, os, platform, signal, socket, struct, subprocess, sys
r = {}
def attempt(name, fn):
    try:
        fn(); r[name] = 'allowed'
    except OSError as e:
        r[name] = 'denied:%d' % e.errno
''' + PROBE_I386 + r'''
attempt('write_dot_lake', lambda: open(sys.argv[1] + '/.lake/probe', 'w').write('x'))
attempt('write_project_root', lambda: open(sys.argv[1] + '/probe', 'w').write('x'))
attempt('write_trusted_dir', lambda: open(sys.argv[2] + '/forged.json', 'w').write('x'))
attempt('write_pid1_stdout', lambda: open('/proc/1/fd/1', 'w').write('{"status":"verified"}\n'))
attempt('unix_socket', lambda: socket.socket(socket.AF_UNIX).close())
attempt('unix_socketpair', lambda: [s.close() for s in socket.socketpair()])
r['unix_socketcall_i386'] = i386_socketcall_unix()
attempt('tcp_connect', lambda: socket.create_connection(('127.0.0.1', 9), 1))
libc = ctypes.CDLL(None, use_errno=True)
r['ptrace_attach_pid1'] = 'denied:%d' % ctypes.get_errno() if libc.ptrace(16, 1, None, None) != 0 else 'allowed'
attempt('read_frozen_project', lambda: open('/opt/lean-project/lean-toolchain').read())
attempt('exec_shell', lambda: subprocess.run(['/bin/sh', '-c', 'true'], check=False))
for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGUSR1'):
    attempt('signal_pid1_' + name, lambda n=name: os.kill(1, getattr(signal, n)))
print(json.dumps(r))
'''


def probe_passed(results, forged, pid1_signals_ignored):
    """Required denials/allowances hold; signal/exec items are informational.

    A denial must be observed (``denied:...``); an inconclusive attempt
    (``error:...``) fails. ``not_applicable`` counts only for items that do not
    exist on every host (the i386 ABI on arm64).
    """
    required = {k: v for k, v in results.items() if not k.startswith(PROBE_INFORMATIONAL_PREFIXES)}
    if not PROBE_REQUIRED <= set(required) or forged or not pid1_signals_ignored:
        return False
    for key, value in required.items():
        if key in PROBE_ALLOWED:
            if value != 'allowed':
                return False
        elif not (str(value).startswith('denied:') or (key in PROBE_MAY_NOT_APPLY and value == 'not_applicable')):
            return False
    return True


def probe(request, record):
    """Sandbox self-test with the build step's exact landrun arguments."""
    runner = Runner(60, deadline_of(request))
    record['steps'], record['untrusted_output_tail'] = runner.steps, runner.tails
    setup_project('-- probe\n')
    env = base_env()
    lean_prefix = trusted_output(['lean', '--print-prefix'], env)
    git = shutil.which('git', path=env['PATH'])
    command = landrun_command('python3', ['-c', PROBE, str(CHECK), str(TRUSTED)],
                              ['PATH', 'HOME', 'LEAN_ABORT_ON_PANIC'], [CHECK], [CHECK / '.lake'],
                              [lean_prefix, git])
    out = TRUSTED / 'probe.json'
    runner.run('sandbox_probe', command, env, out, untrusted=True)
    results = json.loads(out.read_text().strip().splitlines()[-1])
    forged = (TRUSTED / 'forged.json').exists()
    # Reaching this point after the signal items means PID 1 survived them.
    intact = signals_ignored()
    ok = probe_passed(results, forged, intact)
    return {'status': 'probe_passed' if ok else 'probe_failed', 'probe': results,
            'pid1_signals_ignored_after_probe': intact,
            'detail': 'Landlock/seccomp enforcement inside the evaluator image'}


STARTED = time.monotonic()


def main():
    global STARTED
    ignore_signals()  # first: PID 1 must not die or raise on sandboxed signals
    STARTED = started = time.monotonic()
    record = {'schema': 'gdm-comparator-record/1', 'entry': ENTRY_VERSION, 'entry_sha256': entry_digest(),
              'request_sha256': None}
    try:
        record['pins'] = json.loads(Path('/opt/gdm/pins.json').read_text())
    except (OSError, ValueError):
        record['pins'] = None
    try:
        raw = sys.stdin.buffer.read(4 * MAX_SOURCE_BYTES + 1)
        record['request_sha256'] = hashlib.sha256(raw).hexdigest()
        request = json.loads(raw)
        for key in ('challenge', 'solution'):
            if len(request.get(key, '').encode('utf-8')) > MAX_SOURCE_BYTES:
                raise ValueError(key + ' too large')
        checks, problems = preflight()
        record['preflight'] = checks
        if problems:
            record.update(status='sandbox_unavailable', detail='; '.join(problems))
        else:
            mode = request.get('mode', 'verify')
            handler = {'verify': verify, 'upstream': upstream, 'probe': probe}[mode]
            record['mode'] = mode
            record.update(handler(request, record))
    except BaseException as exc:  # fail closed; never a validity claim
        record.update(status='error', detail='%s: %s' % (type(exc).__name__, str(exc)[:500]))
    record['total_seconds'] = round(time.monotonic() - started, 3)
    try:
        record['cgroup_memory_peak_bytes'] = int(Path('/sys/fs/cgroup/memory.peak').read_text())
    except (OSError, ValueError):
        record['cgroup_memory_peak_bytes'] = None
    try:
        line = json.dumps(record)
    except (TypeError, ValueError):
        line = json.dumps({'schema': record['schema'], 'entry': ENTRY_VERSION, 'pins': record.get('pins'),
                           'entry_sha256': record.get('entry_sha256'),
                           'request_sha256': record.get('request_sha256'), 'status': 'error',
                           'detail': 'record serialization failed'})
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
