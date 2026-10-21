"""Offline tests of unlisting a run from the catalogues without deleting files (docs/plan.md,
October 20, 2026): the tracked data/library-unlisted.json, build_experiment_index.py, publish and
maintain_hf_run.py --scope unlist. In-memory private Space; no HF login, network or credentials.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_experiment_index import build, unlisted_runs  # noqa: E402
import maintain_hf_run  # noqa: E402
from maintain_hf_run import main, maintain_run, unlist_run  # noqa: E402
from publish_hf_library import digest, publish  # noqa: E402
import prepare_hf_library  # noqa: E402
from test_hf_maintenance import CATALOGUE, RULE, Hub, operation  # noqa: E402
from test_judge_publication import make_run, make_viewer  # noqa: E402

RUN, OTHER, LATER = 'b-hidden', 'a-old', 'c-new'
RECORD = {RUN: {'run': RUN, 'date': '2026-10-20', 'reason': 'owner request'}}


class UnlistTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        make_run(self.runs, RUN)
        self.hub = Hub(self.root)
        publish(self.prepare('published', OTHER, RUN), self.hub, self.hub.download, commit=True,
                operation=operation, unlisted=set())
        self.published = dict(self.hub.files)
        self.commits = len(self.hub.commits)

    def prepare(self, name, *runs):
        output = self.root / 'bundles' / name
        argv = ['prepare_hf_library.py', '--runs-dir', str(self.runs), '--output-dir', str(output)]
        for run in runs:
            argv += ['--run', f'{run}={self.viewer}']
        with patch.object(sys, 'argv', argv), patch('builtins.print'):
            prepare_hf_library.main()
        return output

    def unlist(self, commit=False, attempt='attempt-1', **kwargs):
        kwargs.setdefault('unlisted', RECORD)
        if commit:
            kwargs.setdefault('backup_dir', self.root / 'records' / attempt)
            kwargs.setdefault('receipt', self.root / 'records' / attempt / 'receipt.json')
        return unlist_run(self.hub, self.hub.download, run=RUN, commit=commit, operation=operation, **kwargs)

    def listed(self, files=None):
        return [x['run'] for x in json.loads((files or self.hub.files)['experiments.json'])['experiments']]

    def test_dry_run_then_commit_removes_only_the_catalogue_row(self):
        plan = self.unlist()
        self.assertEqual((plan['listed_before'], plan['entries_before'], plan['entries_after'], plan['uploads']),
                         (True, 2, 1, 3))
        self.assertEqual(len(self.hub.commits), self.commits)
        result = self.unlist(commit=True)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(sorted(result['verification']['changed_files']), sorted(CATALOGUE))
        final = self.hub.files
        self.assertEqual(set(final), set(self.published))  # nothing added or deleted
        for name, data in self.published.items():
            if name not in CATALOGUE:
                self.assertEqual(final[name], data, name)
        self.assertTrue(any(n.startswith(RUN + '/') for n in final))
        self.assertEqual(self.listed(), [OTHER])
        self.assertNotIn(RUN, final['index.html'].decode())
        old = json.loads(self.published['experiments.json'])['experiments']
        self.assertEqual(json.loads(final['experiments.json'])['experiments'], [x for x in old if x['run'] == OTHER])
        manifest = json.loads(final['archive-manifest.json'])
        self.assertEqual((manifest['unlisted_runs'], sorted(manifest['runs'])), ([RUN], [OTHER, RUN]))
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        self.assertEqual(json.loads((self.root / 'records/attempt-1/receipt.json').read_text())['status'], 'verified')
        self.assertEqual((self.root / 'records/attempt-1/original/index.html').read_bytes(),
                         self.published['index.html'])
        # A second run finds nothing to do and commits nothing.
        again = self.unlist(commit=True, attempt='attempt-2')
        self.assertTrue(again['already_applied'])
        self.assertEqual(len(self.hub.commits), self.commits + 1)

    def test_later_publication_keeps_the_run_unlisted_and_refuses_republishing_it(self):
        self.unlist(commit=True)
        make_run(self.runs, LATER)
        bundle = self.prepare('later', LATER)
        publish(bundle, self.hub, self.hub.download, commit=True, operation=operation, unlisted=set(RECORD))
        self.assertEqual(self.listed(), [LATER, OTHER])  # newest first
        manifest = json.loads(self.hub.files['archive-manifest.json'])
        self.assertEqual((sorted(manifest['runs']), manifest['unlisted_runs']), ([OTHER, RUN, LATER], [RUN]))
        with self.assertRaisesRegex(ValueError, 'unlisted'):
            publish(self.prepare('again', RUN), self.hub, self.hub.download, operation=operation,
                    unlisted=set(RECORD))

    def test_refusals_before_any_write(self):
        with self.assertRaisesRegex(ValueError, 'library-unlisted.json'):
            self.unlist(commit=True, unlisted={})
        with self.assertRaisesRegex(ValueError, 'not published'):
            unlist_run(self.hub, self.hub.download, run='zz-missing', unlisted={'zz-missing': RECORD[RUN]})
        self.hub.push({**self.published, '.gitattributes': self.published['.gitattributes'] + b'*.html' + RULE + b'\n'})
        with self.assertRaisesRegex(ValueError, 'LFS'):
            self.unlist(commit=True)
        self.hub.push(self.published)
        self.hub.private = False
        with self.assertRaisesRegex(ValueError, 'private'):
            self.unlist()
        self.hub.private = True
        self.hub.concurrent = True  # another collaborator commits between the plan and the upload
        with self.assertRaisesRegex(ValueError, 'concurrently'):
            self.unlist(commit=True, attempt='attempt-3')
        self.assertEqual(len(self.hub.commits), self.commits)
        self.assertEqual(self.listed(), [RUN, OTHER])  # newest first

    def test_maintenance_of_an_unlisted_run_is_refused(self):
        with patch.object(maintain_hf_run, 'unlisted_runs', return_value=RECORD):
            with self.assertRaisesRegex(ValueError, 'unlisted'):
                maintain_run(self.root, self.hub, self.hub.download, run=RUN, scope='results')

    def test_cli_requires_no_bundle_for_unlist(self):
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            main(['--maintain-run', RUN, '--scope', 'unlist', '--bundle', str(self.root)])
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            main(['--maintain-run', RUN, '--scope', 'results'])


class LocalIndexTests(unittest.TestCase):
    def test_build_leaves_unlisted_runs_out_and_keeps_their_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            make_run(runs, OTHER)
            make_run(runs, RUN)
            result = build(runs, unlisted={RUN})
            self.assertEqual((result['experiments'], result['unlisted']), (1, [RUN]))
            self.assertNotIn(RUN, (runs / 'index.html').read_text())
            self.assertTrue((runs / RUN / 'results.json').exists())

    def test_tracked_file_records_baseline100(self):
        records = unlisted_runs()
        self.assertEqual(set(records), {'baseline100-20261015T191545Z'})
        self.assertTrue(records['baseline100-20261015T191545Z']['reason'])

    def test_malformed_file_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'u.json'
            path.write_text(json.dumps({'schema': 'gdm-library-unlisted/1', 'runs': [{'run': 'x'}]}))
            with self.assertRaises(ValueError):
                unlisted_runs(path)
            self.assertEqual(unlisted_runs(Path(tmp) / 'absent.json'), {})


if __name__ == '__main__':
    unittest.main()
