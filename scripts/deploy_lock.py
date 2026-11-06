"""One Cloudflare deployment at a time: the deploy lock every ``publish_cloudflare.py ... --commit`` takes itself.

Production race, November 6, 2026: a story-map deploy and a restyle interleaved (each read the served site, then
deployed its own change on top of what it had read), so the later one silently reverted the earlier. Every
committing command now holds this lock from reading the base site to verifying the deployment.

The lock is a directory (``mkdir`` is atomic), the same convention as the operators' shell wrappers, at
``$GDM_CF_DEPLOY_LOCK`` (set it to the wrappers' lock to share one lock with them) or else
``runs/phase-b/cloudflare/cf-deploy.lock`` (Git-ignored). The holder writes ``owner.json`` (pid, start, command)
inside it. A waiting command logs one line when it starts waiting and one a minute after, then takes the lock
as soon as it is free; nothing needs a manual step:

- held by this process or one of its ancestors (``owner.json``), or by an ancestor shell wrapper that takes the
  same lock by ``mkdir`` (a lock without ``owner.json`` while an ancestor runs a ``.sh`` script naming its folder
  and name): the command
  runs inside that hold instead of waiting for itself;
- a holder that no longer runs (its pid is gone), or a lock without ``owner.json`` older than STALE_SECONDS:
  removed as stale (logged), then taken.
The lock is released on exit, including on errors.
"""
from contextlib import contextmanager
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ENV = 'GDM_CF_DEPLOY_LOCK'
DEFAULT = Path(__file__).resolve().parents[1] / 'runs' / 'phase-b' / 'cloudflare' / 'cf-deploy.lock'
OWNER = 'owner.json'
STALE_SECONDS = 3 * 3600  # an ownerless lock this old outlives any deployment
LOG_EVERY = 60


def lock_path():
    return Path(os.environ.get(ENV) or DEFAULT)


def _log(text):
    print(f'[deploy lock] {text}', file=sys.stderr, flush=True)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _ps(field, pid):
    try:
        out = subprocess.run(['ps', '-o', f'{field}=', '-p', str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.stdout.strip()


def ancestors():
    """Pids of this process's ancestors, nearest first."""
    out, pid = [], os.getppid()
    while pid > 1 and pid not in out and len(out) < 64:
        out.append(pid)
        try:
            pid = int(_ps('ppid', pid) or 0)
        except ValueError:
            break
    return out


def _wrapper_holds(lock):
    """True when an ancestor runs a script that takes ``lock`` itself (a shell wrapper's ``mkdir`` lock)."""
    for pid in ancestors():
        for word in _ps('command', pid).split():
            script = Path(word)
            try:
                if word.endswith('.sh') and script.is_file() and script.stat().st_size < 1_000_000:
                    text = script.read_text(errors='replace')
                    if 'mkdir' in text and lock.name in text and str(lock.parent) in text:
                        return True
            except OSError:
                continue
    return False


def _owner(lock):
    try:
        return json.loads((lock / OWNER).read_text())
    except (OSError, ValueError):
        return None


def _remove(lock):
    try:
        (lock / OWNER).unlink()
    except FileNotFoundError:
        pass
    try:
        lock.rmdir()
    except FileNotFoundError:
        pass


@contextmanager
def held(lock=None, poll=15, what='deploy'):
    """Hold the deploy lock for the duration of the block (see the module docstring)."""
    lock = Path(lock or lock_path())
    lock.parent.mkdir(parents=True, exist_ok=True)
    started, logged = time.monotonic(), None
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            pass
        owner = _owner(lock)
        if owner is not None:
            pid = owner.get('pid')
            if pid == os.getpid() or pid in ancestors():
                _log(f'{what}: already held by this command\'s process (pid {pid}); continuing inside it')
                yield lock
                return
            if isinstance(pid, int) and not _alive(pid):
                _log(f'{what}: removing a stale lock left by pid {pid} (no longer running): {lock}')
                _remove(lock)
                continue
            holder = f'pid {pid}, since {owner.get("started")}: {owner.get("command", "")[:160]}'
        else:
            if _wrapper_holds(lock):
                _log(f'{what}: held by the deploy wrapper that started this command; continuing inside it')
                yield lock
                return
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > STALE_SECONDS:
                _log(f'{what}: removing an ownerless lock {age / 3600:.1f} h old (stale): {lock}')
                _remove(lock)
                continue
            holder = 'a deploy wrapper (no owner record)'
        now = time.monotonic()
        if logged is None or now - logged >= LOG_EVERY:
            _log(f'{what}: waiting for {lock}, held by {holder} (waited {now - started:.0f} s)')
            logged = now
        time.sleep(poll)
    (lock / OWNER).write_text(json.dumps({
        'pid': os.getpid(), 'started': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'command': ' '.join(sys.argv)}))
    _log(f'{what}: acquired {lock}' + (f' after {time.monotonic() - started:.0f} s' if logged else ''))
    try:
        yield lock
    finally:
        _remove(lock)
        _log(f'{what}: released {lock}')
