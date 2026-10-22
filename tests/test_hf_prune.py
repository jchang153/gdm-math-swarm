"""Offline tests of maintain_hf_run.py --scope prune (owner, October 22, 2026: "Let's start by first just
pruning old runs"; HF's 1 GB storage limit). In-memory private Space; no HF login, network or credentials.
"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from maintain_hf_run import OFFLINE_KEY, OFFLINE_MARKER, prune_run  # noqa: E402
from publish_hf_library import digest, publish  # noqa: E402
from test_hf_maintenance import RULE, Hub, operation  # noqa: E402
import test_hf_snapshots  # noqa: E402
from test_hf_snapshots import TAR_RULE, add_snapshots, delete  # noqa: E402
from test_judge_publication import make_run, make_viewer  # noqa: E402

RUN, OTHER = 'snap-run', 'a-old'


class PurgingHub(Hub):
    """Adds the Hub's LFS object listing and permanent deletion (objects ever pushed under a path)."""

    def __init__(self, root):
        super().__init__(root)
        self.purged = []

    def list_lfs_files(self, repo, *, repo_type):
        seen = {}
        for revision in list(self.history):
            for item in self.list_repo_tree(repo, repo_type=repo_type, revision=revision, recursive=True):
                lfs = getattr(item, 'lfs', None)
                if lfs is not None and lfs.sha256 not in self.purged:
                    seen.setdefault(lfs.sha256, SimpleNamespace(file_oid=lfs.sha256, oid=lfs.sha256,
                                                                filename=item.path, size=lfs.size))
        return list(seen.values())

    def permanently_delete_lfs_files(self, repo, objects, *, rewrite_history, repo_type):
        assert rewrite_history is False
        self.purged += [f.file_oid for f in objects]


class PruneTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        run = make_run(self.runs, RUN)
        add_snapshots(run)
        (run / 'runtime.jsonl').write_text('{"event": "start"}\n' * 50)
        (run / 'analysis').mkdir(exist_ok=True)
        (run / 'analysis/timeline.jsonl').write_text('{"t": 1}\n' * 50)
        self.hub = PurgingHub(self.root)
        self.hub.history['0']['.gitattributes'] += TAR_RULE + b'*.jsonl' + RULE + b'\n'
        bundle = test_hf_snapshots.SnapshotArchiveTests.prepare(SimpleNamespace(root=self.root, runs=self.runs, viewer=viewer),
                                              'published', RUN, OTHER)
        publish(bundle, self.hub, self.hub.download, commit=True, operation=operation, records_in_space=True)
        self.published = dict(self.hub.files)

    def scope(self, commit=False, attempt='attempt-1', **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.root / 'records' / attempt)
            kwargs.setdefault('receipt', self.root / 'records' / attempt / 'receipt.json')
        return prune_run(self.hub, self.hub.download, run=RUN, runs_dir=self.runs, commit=commit,
                         operation=operation, delete_operation=delete, removed='2026-10-22', **kwargs)

    def test_dry_run_then_commit_removes_raw_files_and_adds_notice(self):
        pruned = {RUN + '/runtime.jsonl', RUN + '/analysis/timeline.jsonl', RUN + '/snapshots.tar.gz'}
        plan = self.scope()
        self.assertEqual({x['path'] for x in plan['to_delete']}, pruned)
        self.assertEqual(self.hub.files, self.published)  # dry run writes nothing
        result = self.scope(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        self.assertEqual(set(final), set(self.published) - pruned)
        for name, data in self.published.items():
            if name not in pruned and name not in (RUN + '/results.html', 'archive-manifest.json'):
                self.assertEqual(final[name], data, name)
        page = final[RUN + '/results.html'].decode()
        self.assertIn(OFFLINE_MARKER, page)
        self.assertIn('runtime.jsonl', page)
        manifest = json.loads(final['archive-manifest.json'])
        self.assertEqual(set(manifest[OFFLINE_KEY]), pruned)
        self.assertFalse(pruned & set(manifest['files']))
        self.assertEqual(manifest['files'][RUN + '/results.html'], digest(final[RUN + '/results.html']))
        self.assertEqual(set(self.hub.purged), {manifest[OFFLINE_KEY][n]['sha256'] for n in pruned})
        self.assertTrue((self.runs / RUN / 'runtime.jsonl').exists())  # local copies untouched
        again = self.scope(commit=True, attempt='attempt-2')
        self.assertTrue(again['already_applied'])
        self.assertEqual(self.hub.files, final)

    def test_refuses_when_local_original_differs_or_is_missing(self):
        (self.runs / RUN / 'runtime.jsonl').write_text('changed\n')
        with self.assertRaisesRegex(ValueError, 'differs from the published'):
            self.scope(commit=True)
        (self.runs / RUN / 'runtime.jsonl').unlink()
        with self.assertRaisesRegex(ValueError, 'Local original missing'):
            self.scope()
        self.assertEqual(self.hub.files, self.published)

    def test_eval_kind_and_unknown_kind(self):
        with self.assertRaisesRegex(ValueError, 'Unknown prune kind'):
            self.scope(kinds=('bogus',))


if __name__ == '__main__':
    unittest.main()
