"""The deploy lock every publish_cloudflare.py --commit takes itself (scripts/deploy_lock.py; November 6, 2026)."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import deploy_lock  # noqa: E402

HOLD = ('import sys,time;sys.path.insert(0,sys.argv[1]);import deploy_lock\n'
        'with deploy_lock.held(sys.argv[2], poll=0.05):\n'
        '    open(sys.argv[3],"w").close();time.sleep(float(sys.argv[4]))\n')


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / 'cf-deploy.lock'

    def test_acquire_records_owner_and_releases(self):
        with deploy_lock.held(self.lock, poll=0.05):
            owner = json.loads((self.lock / deploy_lock.OWNER).read_text())
            self.assertEqual(owner['pid'], os.getpid())
            with deploy_lock.held(self.lock, poll=0.05):  # same process: runs inside the hold
                self.assertTrue(self.lock.is_dir())
            self.assertTrue(self.lock.is_dir())
        self.assertFalse(self.lock.exists())

    def test_released_on_error(self):
        with self.assertRaises(RuntimeError), deploy_lock.held(self.lock, poll=0.05):
            raise RuntimeError('deploy failed')
        self.assertFalse(self.lock.exists())

    def test_second_command_waits_for_the_first(self):
        ready = Path(self.tmp.name) / 'ready'
        holder = subprocess.Popen([sys.executable, '-c', HOLD, str(ROOT / 'scripts'), str(self.lock), str(ready),
                                   '1.2'], stderr=subprocess.DEVNULL)
        self.addCleanup(holder.wait)
        while not ready.exists():
            time.sleep(0.02)
        start = time.monotonic()
        with deploy_lock.held(self.lock, poll=0.05):
            waited = time.monotonic() - start
            self.assertEqual(json.loads((self.lock / deploy_lock.OWNER).read_text())['pid'], os.getpid())
        self.assertGreater(waited, 0.5)
        self.assertEqual(holder.wait(), 0)

    def test_released_on_sigterm_and_sigint(self):
        """Production, November 6, 2026: a SIGTERM-stopped deploy left owner.json behind."""
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            ready = Path(self.tmp.name) / f'ready-{sig}'
            holder = subprocess.Popen([sys.executable, '-c', HOLD, str(ROOT / 'scripts'), str(self.lock), str(ready),
                                       '30'], stderr=subprocess.PIPE, text=True)
            while not ready.exists():
                time.sleep(0.02)
            holder.send_signal(sig)
            _, err = holder.communicate(timeout=20)
            self.assertNotEqual(holder.returncode, 0)
            self.assertIn('released', err)
            self.assertFalse(self.lock.exists())

    def test_stale_locks_are_taken_over(self):
        self.lock.mkdir()
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        (self.lock / deploy_lock.OWNER).write_text(json.dumps({'pid': dead.pid}))
        with deploy_lock.held(self.lock, poll=0.05):
            self.assertEqual(json.loads((self.lock / deploy_lock.OWNER).read_text())['pid'], os.getpid())
        self.lock.mkdir()  # an ownerless (shell wrapper) lock far older than any deployment
        old = time.time() - deploy_lock.STALE_SECONDS - 60
        os.utime(self.lock, (old, old))
        with deploy_lock.held(self.lock, poll=0.05):
            self.assertTrue((self.lock / deploy_lock.OWNER).is_file())

    def test_inside_a_shell_wrapper_that_holds_it(self):
        """An operator's wrapper (mkdir lock, no owner record) that runs publish_cloudflare: no self-deadlock."""
        wrapper = Path(self.tmp.name) / 'deploy.sh'
        wrapper.write_text(f'#!/bin/bash\nuntil mkdir {self.lock} 2>/dev/null; do sleep 1; done\n'
                           f'trap "rmdir {self.lock}" EXIT\n"$@"\n')
        wrapper.chmod(0o755)
        ready = Path(self.tmp.name) / 'ready'
        done = subprocess.run([str(wrapper), sys.executable, '-c', HOLD, str(ROOT / 'scripts'), str(self.lock),
                               str(ready), '0'], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('held by the deploy wrapper', done.stderr)
        self.assertTrue(ready.exists())
        self.assertFalse(self.lock.exists())

    def test_env_overrides_the_default_path(self):
        with patch.dict(os.environ, {deploy_lock.ENV: str(self.lock)}):
            self.assertEqual(deploy_lock.lock_path(), self.lock)
        with patch.dict(os.environ, {deploy_lock.ENV: ''}):
            self.assertEqual(deploy_lock.lock_path(), deploy_lock.DEFAULT)


class CommitPathsTest(unittest.TestCase):
    """Every --commit command takes the lock before it reads the served site; plans never wait."""

    def test_commit_takes_the_lock_first(self):
        import publish_cloudflare as pc

        class Taken(Exception):
            pass

        class Read(Exception):
            pass

        calls = []

        @contextmanager
        def fake(**kw):
            calls.append(kw)
            raise Taken

            yield  # noqa: unreachable (generator)

        def read(*a, **k):
            raise Read

        commands = (['restyle'], ['catalogue'], ['repack'], ['storymap', '--run', 'R=x'], ['heatmap', '--run', 'R=x'],
                    ['correct', '--run', 'R', '--file', 'a=b'], ['judge-labels', '--run', 'R'],
                    ['withdraw', '--run', 'R'], ['publish', '--bundle', 'b'])
        with patch.object(pc.deploy_lock, 'held', fake), patch.object(pc, 'deployments', read):
            for command in commands:
                calls.clear()
                with self.assertRaises(Taken, msg=command):
                    pc.main(command + ['--commit'])
                self.assertEqual(len(calls), 1)
                with self.assertRaises(Read, msg=command):  # no --commit: straight to reading, no lock
                    pc.main(command)
                self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
