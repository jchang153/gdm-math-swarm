"""Offline tests of the near-live mirror: no VM, gcloud, HF login, network or model calls.

The VM program runs locally against a fabricated run (a real Inspect realtime buffer and conference
database), through a fake gcloud runner that executes the SSH command with the local shell.
"""
import datetime
import hashlib
import io
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / 'scripts', ROOT / 'src', ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import build_experiment_index  # noqa: E402
import export_conference  # noqa: E402
import live_mirror  # noqa: E402

RUN = 'live-20990101T000000Z'


def blob_id(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class FakeRunner:
    """Stands in for subprocess.run of gcloud: ``describe`` answers a status, ``ssh`` runs the
    command it was given with the local shell (the VM program then reads the fabricated VM)."""

    def __init__(self, status='RUNNING'):
        self.status = status
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[1:3] == ['compute', 'instances']:
            return SimpleNamespace(returncode=0, stdout=self.status + '\n', stderr='')
        command = argv[argv.index('--command') + 1]
        # The VM's python3 is the interpreter running these tests (stdlib only on the VM side).
        path = str(Path(sys.executable).parent) + ':/usr/bin:/bin'
        done = subprocess.run(['/bin/sh', '-c', command], capture_output=True, timeout=300, env={'PATH': path})
        kwargs['stdout'].write(done.stdout)
        return SimpleNamespace(returncode=done.returncode, stdout=None, stderr=done.stderr)


class Hub:
    """A private Space in memory (the HfApi calls the mirror makes)."""

    def __init__(self, private=True):
        self.files, self.revision, self.private, self.commits = {}, 0, private, []
        self.last_modified = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        self.cache = Path(tempfile.mkdtemp(prefix='hub-cache-'))

    def repo_info(self, repo, repo_type):
        return SimpleNamespace(private=self.private, sha=str(self.revision), last_modified=self.last_modified)

    def list_repo_tree(self, repo, repo_type, revision, recursive=False, path_in_repo=None):
        assert revision == str(self.revision)
        names = [n for n in self.files if (path_in_repo is None or n.startswith(path_in_repo + '/'))
                 and (recursive or '/' not in n)]
        if path_in_repo and not names:
            raise type('EntryNotFoundError', (Exception,), {})('404')
        return [SimpleNamespace(path=n, blob_id=blob_id(self.files[n]), lfs=None) for n in names]

    def download(self, repo, name, repo_type, revision):
        path = self.cache / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.files[name])
        return str(path)

    def create_commit(self, *, repo_id, repo_type, parent_commit, commit_message, operations):
        assert parent_commit == str(self.revision)
        for op in operations:
            if op.kind == 'add':
                self.files[op.path_in_repo] = op.path_or_fileobj
            else:
                del self.files[op.path_in_repo]
        self.commits.append((commit_message, operations))
        self.last_modified = datetime.datetime.now(datetime.timezone.utc)
        self.revision += 1
        return SimpleNamespace(oid=str(self.revision))


def add(path_in_repo, path_or_fileobj):
    return SimpleNamespace(kind='add', path_in_repo=path_in_repo, path_or_fileobj=path_or_fileobj)


def delete(path_in_repo):
    return SimpleNamespace(kind='delete', path_in_repo=path_in_repo)


def conference_db(path, *, closed=1):
    from research_swarm import conference
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(conference._SCHEMA)
    db.execute("INSERT INTO tasks VALUES ('t1', 1, 'T1', 'thm1', 0, 'x', ?, NULL, NULL, NULL, NULL)",
               ('closed' if closed else 'open',))
    db.execute("INSERT INTO tasks VALUES ('t2', 2, 'T2', 'thm2', 0, 'x', 'open', NULL, NULL, NULL, NULL)")
    db.execute("INSERT INTO posts (agent_id, intent_type, message, tags, posted_at) VALUES "
               "('agent-001', 'claim', 'Working on t1', '[]', 4102444810.0)")
    db.execute("INSERT INTO events (timestamp, kind, agent_id, tool, call_id, payload) VALUES "
               "(4102444810.0, 'board_post', 'agent-001', 'post_intent', 'c1', ?)", (json.dumps({'post_id': 'post-1'}),))
    source = 'theorem thm1 : True := trivial'
    db.execute("INSERT INTO submissions (agent_id, task_id, source, source_sha256, submitted_at, "
               "task_status_at_submission, outcome) VALUES ('agent-001', 't1', ?, ?, 4102444820.0, 'open', 'accepted')",
               (source, hashlib.sha256(source.encode()).hexdigest()))
    db.commit()
    return db


class FakeVM:
    """A fabricated VM home: ROOT/runs/RUN with a live Inspect buffer, conference and runtime log."""

    def __init__(self, base, *, stopped=False, events=8):
        sys.path.insert(0, str(ROOT))
        from research.analysis.tests import test_judge_live as helpers
        self.base = base
        self.home = base / 'vm'
        self.run_dir, self.sample, runtime = helpers.scenario(self.home)
        assert self.run_dir.name == RUN
        cutoff = helpers.T0 + (1000 if stopped else 60)
        self.runtime = [r for r in runtime if r['timestamp'] <= cutoff]
        self.write_runtime(partial_tail=True)
        self.buffer_dir = base / 'vm-buffer'
        self.log = self.run_dir / 'inspect' / '2099-01-01T00-00-00_gdm-math-swarm_live.eval'
        helpers.write_eval(self.log, [])
        self.buffer = helpers.write_buffer(self.log, self.buffer_dir, self.sample.events[:events])
        self.db = conference_db(self.run_dir / 'conference' / 'state.sqlite')
        session = self.run_dir / 'snapshots' / 'agent-001'
        session.mkdir(parents=True, exist_ok=True)
        data = b'{"session": "jsonl"}\n'
        self.session_blob = hashlib.sha256(data).hexdigest()
        (session / self.session_blob).write_bytes(data)
        self.runtime.append({'kind': 'file_snapshot', 'agent_id': 'agent-001', 'timestamp': helpers.T0 + 1,
                             'payload': {'path': '/home/researcher/.claude/projects/-workspace/s.jsonl',
                                         'sha256': self.session_blob, 'blob_sha256': self.session_blob,
                                         'bytes': len(data), 'mode': 'full'}})
        self.write_runtime(partial_tail=True)

    def write_runtime(self, partial_tail=False):
        text = ''.join(json.dumps(r) + '\n' for r in self.runtime)
        (self.run_dir / 'runtime.jsonl').write_text(text + ('{"kind": "half' if partial_tail else ''))


class RemoteProgramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.vm = FakeVM(self.base)
        self.cloud = live_mirror.Cloud('gcloud', runner=FakeRunner())

    def snapshot(self, **kwargs):
        target = self.base / 'snap.tar.gz'
        args = dict(since_line=0, evals={}, buffer_dir=str(self.vm.buffer_dir))
        args.update(kwargs)
        self.cloud.snapshot('collab-vm-1', str(self.vm.home), RUN, target=target, **args)
        with tarfile.open(target, 'r:gz') as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}
        return target, members, json.loads(members['MANIFEST.json'])

    def test_discovery_reports_running_and_stopped_runs(self):
        self.assertEqual(self.cloud.discover('collab-vm-1', str(self.vm.home), 3600),
                         [{'run': RUN, 'stopped': False, 'age_seconds': 0}])
        self.vm.runtime.append({'kind': 'run_stop', 'timestamp': 1})
        self.vm.write_runtime()
        self.assertTrue(self.cloud.discover('collab-vm-1', str(self.vm.home), 3600)[0]['stopped'])
        self.assertEqual(self.cloud.discover('collab-vm-1', str(self.vm.home), -1), [])

    def test_snapshot_is_consistent_and_leaves_out_session_transcripts(self):
        _, members, manifest = self.snapshot()
        self.assertTrue(members['runtime.jsonl'].endswith(b'\n'))          # the half-written line is left out
        self.assertEqual(manifest['runtime_lines'], len(self.vm.runtime))
        self.assertNotIn(f'snapshots/agent-001/{self.vm.session_blob}', members)
        self.assertTrue(any(n.startswith('snapshots/agent-001/') for n in members))   # workspace file captures
        self.assertIn('inspect/' + self.vm.log.name, members)
        buffers = [n for n in members if n.startswith('samplebuffer/')]
        self.assertEqual(len(buffers), 1)
        copy = self.base / 'buffer-copy.db'
        copy.write_bytes(members[buffers[0]])
        with sqlite3.connect(copy) as db:   # an SQLite online backup: complete while the writer is open
            self.assertGreater(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        conf = self.base / 'conf-copy.db'
        conf.write_bytes(members['conference/state.sqlite'])
        with sqlite3.connect(conf) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256(members[name]).hexdigest(), digest)

    def test_unchanged_logs_and_old_blobs_are_not_sent_again(self):
        _, first, manifest = self.snapshot()
        _, second, again = self.snapshot(since_line=manifest['runtime_lines'], evals=manifest['evals'])
        self.assertNotIn('inspect/' + self.vm.log.name, second)
        self.assertFalse(any(n.startswith('snapshots/') for n in second))
        self.assertTrue(any(n.startswith('samplebuffer/') for n in second))   # the buffer is always current
        _, third, _ = self.snapshot(since_line=10 ** 6)                       # beyond the log: start over
        self.assertTrue(any(n.startswith('snapshots/') for n in third))

    def test_command_carries_no_credentials_and_gcloud_gets_no_tokens(self):
        runner = FakeRunner()
        cloud = live_mirror.Cloud('gcloud', runner=runner)
        environ = {'PATH': '/usr/bin:/bin', 'HF_TOKEN': 'hf_' + 'a' * 34, 'OPENROUTER_API_KEY': 'sk-' + 'b' * 30,
                   'HUGGING_FACE_HUB_TOKEN': 'x', 'SSH_AUTH_SOCK': '/tmp/agent'}
        with mock.patch.dict('os.environ', environ, clear=True):
            cloud.discover('collab-vm-1', str(self.vm.home), 3600)
        argv, kwargs = runner.calls[-1]
        self.assertEqual(argv[:4], ['gcloud', 'compute', 'ssh', 'collab-vm-1'])
        self.assertIn('--tunnel-through-iap', argv)
        self.assertEqual(set(kwargs['env']), {'PATH', 'SSH_AUTH_SOCK', 'CLOUDSDK_PYTHON'})
        command = argv[argv.index('--command') + 1]
        self.assertNotIn('hf_', command)
        self.assertNotIn('sk-', command)
        self.assertTrue(shlex.split(command)[:5] == ['nice', '-n', '10', 'python3', '-c'])

    def test_apply_refuses_unexpected_or_tampered_entries(self):
        target, members, manifest = self.snapshot()
        source = self.base / 'work' / RUN
        source.mkdir(parents=True)
        live_mirror.apply_snapshot(target, source, RUN)
        self.assertEqual((source / 'runtime.jsonl').read_bytes(), members['runtime.jsonl'])
        self.assertEqual(live_mirror.missing_blobs(source), [])
        for name, data in (('../escape', b'x'), ('runtime.jsonl', b'tampered')):
            bad = self.base / 'bad.tar.gz'
            with tarfile.open(bad, 'w:gz') as tar:
                for member, content in [('MANIFEST.json', json.dumps(manifest).encode()), (name, data)]:
                    info = tarfile.TarInfo(member)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
            with self.assertRaises(ValueError):
                live_mirror.apply_snapshot(bad, source, RUN)
        with self.assertRaises(ValueError):
            live_mirror.apply_snapshot(target, source, 'another-run')


class PassTests(unittest.TestCase):
    """End to end: fabricated VM -> snapshot -> real transcript and conference rendering -> fake Space."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.vm = FakeVM(self.base)
        self.work = self.base / 'repo' / 'runs' / 'phase-b' / 'live-mirror'
        patcher = mock.patch.object(export_conference, 'ROOT', self.base / 'repo')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hub = Hub()
        self.hub.files['experiments.json'] = json.dumps({'experiments': [{'run': 'older-run'}]}).encode()
        self.hub.files['index.html'] = b'<html>catalogue</html>'
        self.runner = FakeRunner()
        self.cloud = live_mirror.Cloud('gcloud', runner=self.runner)
        self.state = live_mirror.load_state(self.work / 'state.json')

    def run_pass(self, **kwargs):
        return live_mirror.run_pass(self.cloud, [('collab-vm-1', str(self.vm.home))], state=self.state,
                                    work=self.work, api=self.hub, download=self.hub.download, add=add, delete=delete,
                                    buffer_dir=str(self.vm.buffer_dir), quiet_seconds=0, **kwargs)

    def test_pass_publishes_only_live_files_and_renders_the_real_views(self):
        report = self.run_pass()
        self.assertEqual(report['runs'][RUN]['transcripts'], 1, report)
        self.assertEqual(report['upload']['committed'], True, report)
        changed = {op.path_in_repo for op in self.hub.commits[0][1]}
        self.assertTrue(all(name.startswith('live/') for name in changed))
        self.assertEqual(self.hub.files['index.html'], b'<html>catalogue</html>')
        prefix = f'live/{RUN}/'
        for name in ('index.html', 'status.json', 'transcripts/index.html', 'transcripts/agent-001.html',
                     'conference-view/index.html'):
            self.assertIn(prefix + name, self.hub.files)
        page = self.hub.files[prefix + 'transcripts/agent-001.html'].decode()
        self.assertIn('id="private-space-navigation"', page)
        inner = live_mirror.unpack_page(page)
        self.assertIn('A01.t0000', inner)                      # the judge's reconstruction, as published later
        self.assertIn('Live snapshot, not final', inner)
        self.assertIn('id="private-space-navigation"', inner)
        conference = self.hub.files[prefix + 'conference-view/index.html'].decode()
        self.assertIn('href="../../../index.html"', conference)   # the library catalogue
        self.assertNotIn('href="../results.html"', conference)    # not mirrored: plain text
        index = json.loads(self.hub.files['live/index.json'])
        self.assertEqual(index['schema'], 'gdm-live-mirror/1')
        entry = index['runs'][0]
        self.assertEqual((entry['run'], entry['state'], entry['solved'], entry['task_count']), (RUN, 'running', 1, 2))
        self.assertEqual(json.loads(self.hub.files[prefix + 'status.json'])['submissions'], {'accepted': 1})

    def test_own_recent_commit_does_not_defer_the_next_pass(self):
        live_mirror.run_pass(self.cloud, [('collab-vm-1', str(self.vm.home))], state=self.state, work=self.work,
                             api=self.hub, download=self.hub.download, add=add, delete=delete,
                             buffer_dir=str(self.vm.buffer_dir), quiet_seconds=0)
        self.assertEqual(self.state['own_revisions'], ['1'])
        self.vm.db.execute("INSERT INTO posts (agent_id, intent_type, message, tags, posted_at) VALUES "
                           "('agent-001', 'note', 'later', '[]', 4102444812.0)")
        self.vm.db.execute("INSERT INTO events (timestamp, kind, agent_id, tool, call_id, payload) VALUES "
                           "(4102444812.0, 'board_post', 'agent-001', 'post_intent', 'c3', ?)",
                           (json.dumps({'post_id': 'post-3'}),))
        self.vm.db.commit()
        report = live_mirror.run_pass(self.cloud, [('collab-vm-1', str(self.vm.home))], state=self.state,
                                      work=self.work, api=self.hub, download=self.hub.download, add=add,
                                      delete=delete, buffer_dir=str(self.vm.buffer_dir), quiet_seconds=600)
        self.assertTrue(report['upload']['committed'], report)

    def test_unchanged_files_are_not_uploaded_again_and_stale_files_are_removed(self):
        self.run_pass()
        first = {op.path_in_repo for op in self.hub.commits[0][1]}
        self.hub.files[f'live/{RUN}/transcripts/agent-099.html'] = b'left by an earlier snapshot'
        self.hub.revision += 1
        self.run_pass()
        second = self.hub.commits[1][1]
        uploaded = {op.path_in_repo for op in second if op.kind == 'add'}
        self.assertNotIn(f'live/{RUN}/transcripts/agent-001.html', uploaded)   # same events: same bytes
        self.assertLess(len(uploaded), len(first))
        self.assertEqual([op.path_in_repo for op in second if op.kind == 'delete'],
                         [f'live/{RUN}/transcripts/agent-099.html'])

    def test_published_run_is_retired_from_the_live_area(self):
        self.run_pass()
        self.hub.files['experiments.json'] = json.dumps({'experiments': [{'run': 'older-run'}, {'run': RUN}]}).encode()
        self.hub.revision += 1
        report = self.run_pass()
        self.assertEqual(report['upload']['runs_removed'], [RUN])
        self.assertFalse([n for n in self.hub.files if n.startswith(f'live/{RUN}/')])
        self.assertEqual(json.loads(self.hub.files['live/index.json'])['runs'], [])

    def test_stopped_run_gets_one_final_pass_and_a_stopped_vm_is_never_started(self):
        self.run_pass()
        self.vm.runtime.append({'kind': 'run_stop', 'timestamp': self.vm.runtime[-1]['timestamp'] + 1})
        self.vm.write_runtime()
        self.run_pass()
        entry = json.loads(self.hub.files['live/index.json'])['runs'][0]
        self.assertEqual(entry['state'], 'finished')
        self.assertTrue(self.state['runs'][RUN]['final'])
        commits = len(self.hub.commits)
        self.run_pass()                                   # final: not pulled again
        self.assertEqual(len(self.hub.commits), commits)
        self.state['runs'][RUN]['final'] = False
        self.state['runs'][RUN]['entry']['state'] = 'running'
        self.runner.status = 'TERMINATED'
        report = self.run_pass()
        self.assertEqual(report['vms']['collab-vm-1'], 'TERMINATED')
        self.assertFalse([argv for argv, _ in self.runner.calls if 'start' in argv])
        self.assertEqual(json.loads(self.hub.files['live/index.json'])['runs'][0]['state'], 'vm_stopped')

    def test_pass_defers_to_a_recent_publication_and_refuses_a_public_space(self):
        self.hub.revision += 1   # another publisher just committed
        self.hub.last_modified = datetime.datetime.now(datetime.timezone.utc)
        report = live_mirror.run_pass(self.cloud, [('collab-vm-1', str(self.vm.home))], state=self.state,
                                      work=self.work, api=self.hub, download=self.hub.download, add=add,
                                      delete=delete, buffer_dir=str(self.vm.buffer_dir), quiet_seconds=600)
        self.assertIn('deferred', report['upload'])
        self.assertEqual(self.hub.commits, [])
        self.hub.private = False
        report = self.run_pass()
        self.assertIn('private Space', report['upload'])
        self.assertEqual(self.hub.commits, [])

    def test_credential_like_content_is_refused_before_upload(self):
        self.vm.db.execute("INSERT INTO posts (agent_id, intent_type, message, tags, posted_at) VALUES "
                           "('agent-001', 'note', ?, '[]', 4102444811.0)", ('key hf_' + 'Z' * 34,))
        self.vm.db.execute("INSERT INTO events (timestamp, kind, agent_id, tool, call_id, payload) VALUES "
                           "(4102444811.0, 'board_post', 'agent-001', 'post_intent', 'c2', ?)",
                           (json.dumps({'post_id': 'post-2'}),))
        self.vm.db.commit()
        report = self.run_pass()
        self.assertIn('Credential-like content', report['runs'][RUN])
        self.assertFalse([n for n in self.hub.files if n.startswith(f'live/{RUN}/')])


class SyncGuardTests(unittest.TestCase):
    def test_only_live_paths_and_sizes_below_the_lfs_limit(self):
        hub = Hub()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / 'b'
            (bundle / 'transcripts').mkdir(parents=True)
            (bundle / 'transcripts' / 'agent-001.html').write_bytes(b'x' * live_mirror.LFS_SAFE_LIMIT)
            with self.assertRaises(ValueError):
                live_mirror.sync(hub, hub.download, updates={RUN: {'bundle': bundle, 'entry': {'run': RUN}}},
                                 add=add, delete=delete, quiet_seconds=0)
            (bundle / 'transcripts' / 'agent-001.html').write_bytes(b'ok')
            (bundle / '.env').write_bytes(b'')
            with self.assertRaises(ValueError):
                live_mirror.sync(hub, hub.download, updates={RUN: {'bundle': bundle, 'entry': {'run': RUN}}},
                                 add=add, delete=delete, quiet_seconds=0)
        self.assertEqual(hub.commits, [])

    def test_nothing_changed_makes_no_commit(self):
        hub = Hub()
        result = live_mirror.sync(hub, hub.download, updates={}, add=add, delete=delete, quiet_seconds=0)
        self.assertTrue(result['committed'])   # first pass writes an empty live/index.json
        result = live_mirror.sync(hub, hub.download, updates={}, add=add, delete=delete, quiet_seconds=0)
        self.assertEqual((result['committed'], result['uploads'], len(hub.commits)), (False, 0, 1))


class PageTests(unittest.TestCase):
    def test_compressed_page_round_trip(self):
        inner = '<!doctype html><title>x</title><p id="message-1">é &lt;tag&gt;</p>' * 1000
        page = live_mirror.compressed_page(inner, 'agent-001 · live')
        self.assertEqual(live_mirror.unpack_page(page), inner)
        self.assertLess(len(page), len(inner) / 5)
        self.assertNotIn('<p id="message-1">', page)

    def test_links_to_unmirrored_files_become_text(self):
        available = {'conference-view/index.html', 'transcripts/index.html'}
        out = live_mirror.fix_links('<a href="../results.html">Run report</a> <a href="../transcripts/index.html">T</a> '
                                    '<a href="../../index.html">Library</a> <a href="#x">here</a>',
                                    'conference-view/index.html', available)
        self.assertIn('<span class="unavailable" title="Available once the run is published">Run report</span>', out)
        self.assertIn('<a href="../transcripts/index.html">T</a>', out)
        self.assertIn('<a href="../../../index.html">Library</a>', out)
        self.assertIn('<a href="#x">here</a>', out)

    def test_hosted_catalogue_reads_the_live_list_and_skips_published_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {'run': 'a-run', 'start_utc': '2026-10-15T10:00:00+00:00', 'date': 'd', 'track': 't', 'agents': 1,
                     'model': 'm', 'provider_route': 'p', 'elapsed_seconds': 1, 'pilot_spent_usd': '1',
                     'distinct_verified_tasks': 0, 'task_count': 14, 'agent_exits': {}, 'links': {}}
            build_experiment_index.write_index(Path(tmp), [entry], hosted=True)
            hosted = (Path(tmp) / 'index.html').read_text()
            build_experiment_index.write_index(Path(tmp), [entry], hosted=False)
            local = (Path(tmp) / 'index.html').read_text()
        self.assertIn("fetch('live/index.json'", hosted)
        self.assertIn('new Set(["a-run"])', hosted)
        self.assertIn('id="live" hidden', hosted)
        self.assertNotIn('live/index.json', local)


if __name__ == '__main__':
    unittest.main()
