"""maintain_hf_run.py --scope migrate-logs (scripts/migrate_dataset.py) and scripts/dataset_logs.py helpers.
Offline: mocked Hub calls only, no HF login, network or model calls."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import dataset_logs  # noqa: E402
import migrate_dataset  # noqa: E402
from test_dataset_publication import TwoRepoHub, ZSTD, eval_bytes  # noqa: E402

RUN = 'r1-20261022T000000Z'
LOG = '2026-10-22T00-00-00-00-00_gdm-math-swarm_abc.eval'


def add(path_in_repo, path_or_fileobj):
    return SimpleNamespace(path_in_repo=path_in_repo, path_or_fileobj=path_or_fileobj, delete=False)


def remove(path_in_repo):
    return SimpleNamespace(path_in_repo=path_in_repo, path_or_fileobj=None, delete=True)


class Hub(TwoRepoHub):
    def __init__(self, root):
        super().__init__(root)
        self.purged = []

    def repo_info(self, repo, repo_type='model', **kwargs):
        info = super().repo_info(repo, repo_type, **kwargs)
        info.used_storage = sum(len(d) for d in self.repos[repo_type].files.values())
        return info

    def create_commit(self, *, repo_id, repo_type, parent_commit, commit_message, operations):
        deletions = [op for op in operations if getattr(op, 'delete', False)]
        for op in deletions:
            del self.repos[repo_type].files[op.path_in_repo]
        return super().create_commit(repo_id=repo_id, repo_type=repo_type, parent_commit=parent_commit,
                                     commit_message=commit_message,
                                     operations=[op for op in operations if not getattr(op, 'delete', False)])

    def list_lfs_files(self, repo, repo_type):
        return [SimpleNamespace(filename=f'{RUN}/runtime.jsonl', file_oid=self.old_sha, size=10)]

    def permanently_delete_lfs_files(self, repo, objects, rewrite_history, repo_type):
        assert rewrite_history is False and repo_type == 'space'
        self.purged += objects


class Session:
    """Answers ranged resolve reads from the fake dataset."""

    def __init__(self, hub):
        self.hub, self.urls = hub, []

    def get(self, url, headers, timeout, follow_redirects):
        self.urls.append(url)
        path = url.split('/resolve/', 1)[1].split('/', 1)[1]
        data = self.hub.repos['dataset'].files[path]
        start, end = (int(x) for x in headers['Range'][6:].split('-'))
        return SimpleNamespace(status_code=206, content=data[start:end + 1])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hub = Hub(self.root)
        log = eval_bytes()
        self.log = log
        space = self.hub.repos['space'].files
        space.update({
            f'{RUN}/inspect/{LOG}': log, f'{RUN}/inspect-view/logs/{LOG}': log,
            f'{RUN}/inspect-view/index.html': b'<html><head></head></html>',
            f'{RUN}/runtime.jsonl': b'{"event": "common_start"}\n',
            f'{RUN}/results.html': b'<html><head></head><body><a href="evaluations/evaluations.jsonl">e</a>'
                                   b'<a href="results.json">r</a></body></html>',
            f'{RUN}/evaluations/evaluations.jsonl': b'{"x": 1}\n',
            f'{RUN}/results.json': b'{}',
            'live/other/inspect-view/logs/x.eval': log,
        })
        space['archive-manifest.json'] = json.dumps({
            'schema': 'gdm-private-space/1', 'runs': [RUN],
            'files': {n: dataset_logs.digest(d) for n, d in space.items() if not n.startswith('live/')}}).encode()
        self.hub.old_sha = hashlib.sha256(space[f'{RUN}/runtime.jsonl']).hexdigest()
        self.runs = self.root / 'runs'
        (self.runs / RUN / 'inspect').mkdir(parents=True)
        (self.runs / RUN / 'inspect' / LOG).write_bytes(log)

    def migrate(self, **kwargs):
        defaults = dict(runs_dir=self.runs, operation=add, delete_operation=remove, session=Session(self.hub),
                        headers={}, runner=lambda *a, **k: None, receipt=self.root / 'out' / 'receipt.json',
                        backup_dir=self.root / 'out' / 'backup')
        with ZSTD:
            return migrate_dataset.migrate(self.hub, self.hub.download, **{**defaults, **kwargs})

    def test_plan_is_read_only_and_deduplicates_the_two_log_copies(self):
        before = dict(self.hub.repos['space'].files)
        result = self.migrate(commit=False)
        self.assertEqual(self.hub.repos['space'].files, before)
        self.assertEqual(self.hub.repos['dataset'].files, {})
        self.assertEqual(list(result['dataset_files']), [f'{RUN}/inspect/{LOG}'])
        self.assertEqual(result['by_category']['eval']['space_files'], 2)
        self.assertEqual(sorted(f['local'] for f in result['files']), ['absent', 'match'])

    def test_copy_eval_verifies_and_leaves_the_space_unchanged(self):
        before = dict(self.hub.repos['space'].files)
        state = self.migrate(commit=True)
        self.assertEqual(state['status'], 'verified')
        self.assertEqual(self.hub.repos['dataset'].files, {f'{RUN}/inspect/{LOG}': self.log})
        self.assertEqual(self.hub.repos['space'].files, before)
        self.assertTrue(state['checks'][f'{RUN}/inspect/{LOG}']['range']['ok'])
        receipt = json.loads((self.root / 'out' / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'verified')
        self.assertTrue((self.root / 'out' / 'backup' / 'before-inventory.json').is_file())
        again = self.migrate(commit=True, receipt=self.root / 'out2' / 'r.json', backup_dir=self.root / 'out2' / 'b')
        self.assertEqual(again['copy']['added'], 0)  # rerunning continues: identical files are skipped

    def test_local_original_that_differs_stops_before_any_write(self):
        (self.runs / RUN / 'inspect' / LOG).write_bytes(b'other')
        with self.assertRaisesRegex(ValueError, 'Local originals differ'):
            self.migrate(commit=True)
        self.assertEqual(self.hub.repos['dataset'].files, {})

    def test_dataset_must_be_private(self):
        self.hub.repos['dataset'].private = False
        with self.assertRaisesRegex(ValueError, 'private'):
            self.migrate(commit=True)

    def test_delete_stage_refuses_eval(self):
        with self.assertRaisesRegex(ValueError, 'never removes Inspect logs'):
            self.migrate(commit=True, delete_moved=True, categories=('eval', 'jsonl'))

    def test_delete_moves_jsonl_repoints_links_and_purges(self):
        state = self.migrate(commit=True, delete_moved=True, categories=('jsonl',))
        space = self.hub.repos['space'].files
        self.assertNotIn(f'{RUN}/runtime.jsonl', space)
        self.assertNotIn(f'{RUN}/evaluations/evaluations.jsonl', space)
        self.assertIn(f'{RUN}/inspect/{LOG}', space)  # Inspect logs stay until the owner approves
        self.assertIn('live/other/inspect-view/logs/x.eval', space)
        page = space[f'{RUN}/results.html'].decode()
        commit = self.hub.repos['dataset'].sha
        self.assertIn(f'datasets/{dataset_logs.DATASET_REPO}/blob/{commit}/{RUN}/evaluations/evaluations.jsonl', page)
        self.assertIn('href="results.json"', page)
        self.assertEqual(dataset_logs.moved_links(page, 'results.html', {'evaluations/evaluations.jsonl'}), [])
        manifest = json.loads(space['archive-manifest.json'])
        self.assertNotIn(f'{RUN}/runtime.jsonl', manifest['files'])
        self.assertEqual(manifest['dataset_files'][f'{RUN}/runtime.jsonl']['path'], f'{RUN}/runtime.jsonl')
        self.assertEqual(manifest['dataset_files'][f'{RUN}/runtime.jsonl']['commit'], commit)
        self.assertEqual(len(self.hub.purged), 1)
        self.assertEqual(state['delete']['deleted'], 2)
        self.assertTrue((self.root / 'out' / 'backup' / 'original' / RUN / 'results.html').is_file())


    def test_offline_files_are_copied_from_local_originals_or_linked_to_the_notice(self):
        space = self.hub.repos['space'].files
        timeline = b'{"t": 1}\n'
        (self.runs / RUN / 'analysis').mkdir()
        (self.runs / RUN / 'analysis' / 'timeline.jsonl').write_bytes(timeline)
        manifest = json.loads(space['archive-manifest.json'])
        manifest['offline_files'] = {
            f'{RUN}/analysis/timeline.jsonl': {**dataset_logs.digest(timeline), 'removed': '2026-10-22'},
            f'{RUN}/conference/events.jsonl': {'bytes': 3, 'sha256': '0' * 64, 'removed': '2026-10-22'}}
        space['archive-manifest.json'] = json.dumps(manifest).encode()
        space[f'{RUN}/conference-view/index.html'] = (b'<html><head></head><body><a href="../analysis/timeline.jsonl">t'
                                                      b'</a><a href="../conference/events.jsonl">e</a></body></html>')
        state = self.migrate(commit=True, delete_moved=True, categories=('jsonl',))
        self.assertEqual(self.hub.repos['dataset'].files[f'{RUN}/analysis/timeline.jsonl'], timeline)
        page = space[f'{RUN}/conference-view/index.html'].decode()
        self.assertIn(f'/blob/{self.hub.repos["dataset"].sha}/{RUN}/analysis/timeline.jsonl', page)
        self.assertIn('href="../results.html#raw-records-offline"', page)
        self.assertEqual(state['delete']['offline_copied'], [f'{RUN}/analysis/timeline.jsonl'])
        self.assertEqual(state['delete']['offline_linked_to_notice'], [f'{RUN}/conference/events.jsonl'])

    def test_deploy_test_adds_only_the_test_area(self):
        before = dict(self.hub.repos['space'].files)
        result = migrate_dataset.deploy_test(self.hub, self.hub.download, run=RUN, dataset_commit='b' * 40,
                                             operation=add)
        space = self.hub.repos['space'].files
        added = sorted(set(space) - set(before))
        self.assertTrue(all(n.startswith('logs-test/') for n in added))
        self.assertTrue(all(space[n] == d for n, d in before.items()))
        self.assertNotIn(f'logs-test/{RUN}/inspect-view/logs/{LOG}', space)
        page = space[f'logs-test/{RUN}/inspect-view/index.html'].decode()
        self.assertEqual(dataset_logs.shim_settings(page), (dataset_logs.DATASET_REPO, 'b' * 40, f'{RUN}/inspect'))
        self.assertIn('logs-test/signin.html', added)
        self.assertTrue(result['verified'])


class HelperTests(unittest.TestCase):
    def test_categories(self):
        c = dataset_logs.category
        self.assertEqual(c('inspect/a.eval'), 'eval')
        self.assertEqual(c('inspect-view/logs/a.eval'), 'eval')
        self.assertEqual(c('analysis/timeline.jsonl'), 'jsonl')
        self.assertEqual(c('conference/state.sqlite-wal'), 'database')
        self.assertEqual(c('recovery/x.db'), 'database')
        self.assertEqual(c('snapshots.tar.gz'), 'snapshots')
        self.assertEqual(c('snapshots/agent-001/abc'), 'snapshots')
        self.assertEqual(c('transcripts/index.html'), 'transcripts')
        for kept in ('results.html', 'results.json', 'conference-view/index.html', 'analysis/judge/report.json',
                     'inspect-view/logs/listing.json', 'analysis/timeline.md'):
            self.assertIsNone(c(kept), kept)
        self.assertEqual(dataset_logs.dataset_path('R', 'inspect-view/logs/a.eval'), 'R/inspect/a.eval')
        self.assertEqual(dataset_logs.dataset_path('R', 'inspect/a.eval', live=True), 'live/R/inspect/a.eval')

    def test_shim_is_first_in_head_and_pinnable(self):
        page = dataset_logs.with_log_shim('<!doctype html><html><head><script id="private-space-navigation">'
                                          '</script></head></html>', path='R/inspect')
        self.assertLess(page.index('evallog-fetch'), page.index('private-space-navigation'))
        pinned = dataset_logs.pin_commit(page.encode(), 'a' * 40).decode()
        self.assertEqual(dataset_logs.shim_settings(pinned), (dataset_logs.DATASET_REPO, 'a' * 40, 'R/inspect'))
        self.assertNotIn('Bearer hf_', pinned)
        with self.assertRaises(ValueError):
            dataset_logs.with_log_shim('<html><head></head></html>', path='../x')

    def test_signin_page_has_no_external_resources(self):
        page = dataset_logs.signin_page('/logs-test/index.html')
        self.assertNotRegex(page, r'<script[^>]+src=|<link[^>]+href=')
        self.assertIn(dataset_logs.ORG_IDS, page)
        self.assertIn('code_challenge_method', page)


if __name__ == '__main__':
    unittest.main()
