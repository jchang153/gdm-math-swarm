"""Offline tests of the narrow maintenance update of one published run (maintain_hf_run.py).

An in-memory private Space with per-revision snapshots and HF-like automatic LFS rules stands
in for the Hub. No HF login, network, model calls, credentials or real SDK.
"""
import contextlib
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import maintain_hf_run  # noqa: E402
from maintain_hf_run import main, maintain_run  # noqa: E402
from publish_hf_library import digest, publish  # noqa: E402
import prepare_hf_library  # noqa: E402
from test_judge_publication import make_run, make_viewer, write_judge  # noqa: E402

RUN, OTHER = 'cache-run', 'a-old'
RULE = b' filter=lfs diff=lfs merge=lfs -text'
CATALOGUE = ['index.html', 'experiments.json', 'archive-manifest.json']


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def operation(**kwargs):
    return SimpleNamespace(**kwargs)


class Hub:
    """Private Space fake: every commit is a new revision snapshot; reads may name any revision."""

    def __init__(self, root):
        self.root = root
        self.history = {'0': {'.gitattributes': b'*.eval' + RULE + b'\n'}}
        self.revision = 0
        self.private = True
        self.commits = []
        self.concurrent = False
        self.fail_after = None
        self.after_commit = None
        self.lfs_upload = lambda name, data: '/packages/' in name  # HF decides LFS uploads

    @property
    def sha(self):
        return str(self.revision)

    @property
    def files(self):
        return self.history[self.sha]

    def push(self, files):
        """An out-of-band commit (another collaborator or a test edit)."""
        self.revision += 1
        self.history[self.sha] = dict(files)

    def repo_info(self, repo, *, repo_type, files_metadata=False):
        if self.concurrent and not files_metadata:
            self.push(self.files)
        return SimpleNamespace(private=self.private, sha=self.sha)

    @staticmethod
    def tracked(snapshot, name):
        return any(line.endswith(RULE) and fnmatch.fnmatchcase(name, line[:-len(RULE)].decode())
                   for line in snapshot.get('.gitattributes', b'').splitlines())

    def list_repo_tree(self, repo, *, repo_type, revision, recursive):
        assert recursive and revision in self.history
        snapshot = self.history[revision]
        yield SimpleNamespace(path='a-folder', tree_id='folder')
        for name, data in sorted(snapshot.items()):
            lfs = None
            if self.tracked(snapshot, name):
                lfs = SimpleNamespace(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
                pointer = b'oid sha256:' + lfs.sha256.encode() + b'\nsize ' + str(len(data)).encode()
            yield SimpleNamespace(path=name, size=len(data), lfs=lfs, blob_id=blob(pointer if lfs else data))

    def download(self, repo, name, *, repo_type, revision):
        path = self.root / 'hub-cache' / revision / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.history[revision][name])
        return str(path)

    def create_commit(self, *, repo_id, repo_type, parent_commit, commit_message, operations):
        assert parent_commit == self.sha
        if self.fail_after is not None and len(self.commits) >= self.fail_after:
            raise RuntimeError('simulated upload interruption')
        files = dict(self.files)
        for op in operations:
            data = op.path_or_fileobj
            files[op.path_in_repo] = Path(data).read_bytes() if isinstance(data, str) else data
            if self.lfs_upload(op.path_in_repo, files[op.path_in_repo]) and not self.tracked(files, op.path_in_repo):
                files['.gitattributes'] = files.get('.gitattributes', b'') + op.path_in_repo.encode() + RULE + b'\n'
        self.commits.append({'message': commit_message, 'paths': [op.path_in_repo for op in operations]})
        self.push(files)
        oid = self.sha
        if self.after_commit:
            self.after_commit(self)
        return SimpleNamespace(oid=oid)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        make_run(self.runs, RUN)
        self.hub = Hub(self.root)
        publish(self.prepare('published', RUN, OTHER), self.hub, self.hub.download, commit=True,
                operation=operation)
        # After publication: judge outputs and report sections added, transcripts regenerated.
        run = self.runs / RUN
        write_judge(run)
        results = json.loads((run / 'results.json').read_text())
        (run / 'results.json').write_text(json.dumps({**results, 'behavior_judge': {'mode': 'paid'}}))
        (run / 'transcripts/index.html').write_text('<!doctype html><html><body>newer exporter</body></html>')
        (run / 'transcripts/agent-001.html').write_text('<!doctype html><html><body>full history</body></html>')
        self.bundle = self.prepare('maintenance', RUN)
        self.published = dict(self.hub.files)
        self.base = self.hub.sha
        self.publish_commits = len(self.hub.commits)
        files = json.loads((self.bundle / 'archive-manifest.json').read_text())['files']
        self.judge = sorted(n for n in files if n.startswith(RUN + '/analysis/judge/'))
        self.replaced = [RUN + '/results.html', RUN + '/results.json']

    def prepare(self, name, *runs):
        output = self.root / 'bundles' / name
        argv = ['prepare_hf_library.py', '--runs-dir', str(self.runs), '--output-dir', str(output)]
        for run in runs:
            argv += ['--run', f'{run}={self.viewer}']
        with patch.object(sys, 'argv', argv), patch('builtins.print'):
            prepare_hf_library.main()
        return output

    def records(self, attempt='attempt-1'):
        return self.root / 'records' / attempt

    def maintain(self, commit=False, attempt='attempt-1', bundle=None, download=None, **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.records(attempt))
            kwargs.setdefault('receipt', self.records(attempt) / 'receipt.json')
        return maintain_run(bundle or self.bundle, self.hub, download or self.hub.download, run=RUN,
                            commit=commit, operation=operation, **kwargs)

    def restart(self, label):
        """A fresh fake Space at the published revision, for independent sub-cases."""
        hub = Hub(self.root / 'hubs' / label)
        hub.history, hub.revision = {self.base: dict(self.published)}, int(self.base)
        hub.commits = [None] * self.publish_commits
        self.hub = hub

    def on_last_commit(self, change):
        """Apply change to the Space right after the catalogue (last) batch is committed."""
        def hook(hub):
            if 'experiments.json' in hub.commits[-1]['paths']:
                change(hub)
        self.hub.after_commit = hook

    def edit_bundle(self, changes, edit_manifest=None):
        """Change bundle files and keep the bundle's own manifest digests consistent."""
        manifest = json.loads((self.bundle / 'archive-manifest.json').read_text())
        for name, data in changes.items():
            (self.bundle / name).parent.mkdir(parents=True, exist_ok=True)
            (self.bundle / name).write_bytes(data)
            manifest['files'][name] = digest(data)
        if edit_manifest:
            edit_manifest(manifest)
        (self.bundle / 'archive-manifest.json').write_text(json.dumps(manifest))

    def assert_resume_refused(self, message):
        """After a failed first attempt: a commit without --resume-from is refused as partly
        applied, and a resume refuses the earlier attempt's unverified change before any write."""
        commits, revision = len(self.hub.commits), self.hub.sha
        self.assertTrue(self.maintain()['resume_required'])
        with self.assertRaisesRegex(ValueError, 'already partly applied'):
            self.maintain(commit=True, attempt='attempt-2')
        with self.assertRaisesRegex(ValueError, message):
            self.maintain(resume_from=self.records())
        with self.assertRaisesRegex(ValueError, message):
            self.maintain(commit=True, attempt='attempt-3', resume_from=self.records())
        self.assertEqual((len(self.hub.commits), self.hub.sha), (commits, revision))
        self.assertFalse(self.records('attempt-2').exists() or self.records('attempt-3').exists())

    def receipt(self, attempt='attempt-1'):
        return json.loads((self.records(attempt) / 'receipt.json').read_text())

    def edit_remote(self, changes):
        self.hub.push({**self.published, **changes})

    def assert_no_maintenance_commit(self):
        self.assertEqual(len(self.hub.commits), self.publish_commits)

    def assert_final_state(self):
        final = self.hub.files
        self.assertEqual(set(final), set(self.published) | set(self.judge))
        for name, data in self.published.items():
            if name not in set(self.replaced) | set(CATALOGUE) | {'.gitattributes'}:
                self.assertEqual(final[name], data, name)
        for name in self.replaced + self.judge:
            self.assertEqual(final[name], (self.bundle / name).read_bytes(), name)
        old, new = json.loads(self.published['experiments.json']), json.loads(final['experiments.json'])
        self.assertEqual([e for e in new['experiments'] if e['run'] != RUN],
                         [e for e in old['experiments'] if e['run'] != RUN])
        bundle_entry = json.loads((self.bundle / 'experiments.json').read_text())['experiments']
        self.assertEqual([e for e in new['experiments'] if e['run'] == RUN], bundle_entry)
        old_manifest, manifest = (json.loads(x['archive-manifest.json']) for x in (self.published, final))
        self.assertEqual({k: v for k, v in manifest.items() if k not in ('files', 'total_bytes')},
                         {k: v for k, v in old_manifest.items() if k not in ('files', 'total_bytes')})
        self.assertNotIn('behavior_judge', manifest)
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        self.assertEqual(set(manifest['files']), set(old_manifest['files']) | set(self.judge))
        self.assertEqual(manifest['total_bytes'], sum(x['bytes'] for x in manifest['files'].values()))
        self.assertEqual(final['.gitattributes'], self.published['.gitattributes']
                         + (RUN + '/analysis/judge/packages/agent-001.json').encode() + RULE + b'\n')

    def test_dry_run_plan_is_read_only(self):
        result = self.maintain()
        self.assertFalse(result['committed'])
        self.assert_no_maintenance_commit()
        self.assertEqual(self.hub.sha, self.base)
        self.assertFalse((self.root / 'records').exists())
        self.assertEqual((result['base_revision'], result['origin_revision'], result['resume_from']),
                         (self.base, self.base, None))
        self.assertEqual((result['replaced'], result['to_replace']), (self.replaced, self.replaced))
        self.assertEqual((result['added'], result['to_add']), (self.judge, self.judge))
        self.assertEqual((result['already_applied'], result['resume_required']), ([], False))
        self.assertIn(RUN + '/analysis/judge/review/agents/agent-001.html', self.judge)
        self.assertFalse(any('/raw/' in n or '/units/' in n or 'ledger' in n for n in self.judge))
        self.assertEqual(result['judge_already_present'], [])
        self.assertEqual(result['catalogue']['files'], CATALOGUE)
        self.assertEqual(result['catalogue']['entry_diff'], {'links': {
            'added': {'Behavior judge': RUN + '/analysis/judge/report.html'}, 'removed': {}, 'changed': {}}})
        self.assertEqual(result['catalogue']['other_entries_unchanged'], 1)
        self.assertEqual(result['archive_manifest']['changed_digests'],
                         sorted(self.replaced + self.judge + ['experiments.json', 'index.html']))
        self.assertGreater(result['local_judge_exclusions'], 0)
        self.assertEqual(result['uploads'], len(self.judge) + 2 + 3)

    def test_differing_transcripts_are_reported_and_never_uploaded(self):
        skipped = self.maintain()['skipped_local_differences']
        self.assertEqual({r['path']: r['reason'] for r in skipped['names']}, {
            RUN + '/transcripts/agent-001.html': 'absent_remotely',
            RUN + '/transcripts/index.html': 'differs_from_remote'})
        self.assertEqual((skipped['count'], skipped['differs_from_remote'], skipped['absent_remotely']), (2, 1, 1))
        self.assertEqual(skipped['by_area'], {'transcripts': 2})
        self.maintain(commit=True)
        self.assertEqual(self.hub.files[RUN + '/transcripts/index.html'], self.published[RUN + '/transcripts/index.html'])
        self.assertNotIn(RUN + '/transcripts/agent-001.html', self.hub.files)
        uploaded = {p for c in self.hub.commits[self.publish_commits:] for p in c['paths']}
        self.assertEqual(uploaded, set(self.replaced) | set(self.judge) | set(CATALOGUE))

    def test_commit_replaces_only_allowed_files_and_verifies(self):
        result = self.maintain(commit=True)
        self.assertTrue(result['committed'])
        self.assert_final_state()
        commits = self.hub.commits[self.publish_commits:]
        self.assertEqual(sorted(commits[-1]['paths']), sorted(CATALOGUE))  # catalogue last
        self.assertTrue(all(p not in CATALOGUE for c in commits[:-1] for p in c['paths']))
        receipt = self.receipt()
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual((receipt['base_revision'], receipt['final_revision']), (self.base, self.hub.sha))
        self.assertEqual((receipt['replaced'], receipt['added']), (self.replaced, self.judge))
        self.assertEqual(receipt['skipped_local_differences']['count'], 2)
        self.assertEqual((receipt['committed'], receipt['committed_batches']), (True, len(commits)))
        self.assertEqual(receipt['verification'], {
            'against_revision': self.base, 'final_files': len(self.hub.files),
            'uploaded_files': len(self.judge) + 5, 'replaced_files': 5, 'added_files': len(self.judge),
            'verified_planned_files': len(self.judge) + 5, 'verified_preserved_files': len(self.published) - 6,
            'gitattributes': 'exact LFS additions for newly added judge files'})

    def test_backups_and_before_inventory_written_before_upload(self):
        seen = {}
        def check(hub):
            seen.setdefault('inventory', (self.records() / 'before-inventory.json').is_file())
            seen.setdefault('receipt', json.loads((self.records() / 'receipt.json').read_text())['status'])
        self.hub.after_commit = check
        self.maintain(commit=True)
        self.assertEqual(seen, {'inventory': True, 'receipt': 'backups written; nothing uploaded'})
        backup = self.records()
        inventory = json.loads((backup / 'before-inventory.json').read_text())
        self.assertEqual((inventory['operation'], inventory['run'], inventory['revision'], inventory['origin_revision'],
                          inventory['resume_from']), ('maintain-run', RUN, self.base, self.base, None))
        self.assertEqual(set(inventory['files']), set(self.published))
        name = RUN + '/results.html'
        self.assertEqual(inventory['files'][name], {'blob_id': blob(self.published[name]), 'lfs_sha256': None,
                                                    'size': len(self.published[name])})
        for name in self.replaced + CATALOGUE + ['.gitattributes']:
            self.assertEqual((backup / 'original' / name).read_bytes(), self.published[name], name)
        plan = json.loads((backup / 'plan.json').read_text())
        self.assertEqual((plan['committed'], plan['added']), (False, self.judge))

    def test_other_catalogue_entry_change_refused(self):
        catalogue = json.loads(self.published['experiments.json'])
        duplicate = {**catalogue, 'experiments': [catalogue['experiments'][0], *catalogue['experiments']]}
        cases = {'reformatted catalogue': {'experiments.json': json.dumps(catalogue, indent=1).encode()},
                 'edited row of another run': {'index.html': self.published['index.html'].replace(
                     OTHER.encode(), OTHER.encode() + b' (edited)', 1)},
                 'duplicated entry': {'experiments.json': json.dumps(duplicate, indent=2).encode()}}
        for label, change in cases.items():
            with self.subTest(label):
                self.edit_remote(change)
                with self.assertRaisesRegex(ValueError, 'catalogue'):
                    self.maintain(commit=True, attempt=label)
                self.assert_no_maintenance_commit()

    def test_unrelated_remote_file_change_refused_at_verification(self):
        def tamper(hub):
            hub.files[OTHER + '/results.html'] = b'changed outside the plan'
        self.hub.after_commit = tamper
        with self.assertRaisesRegex(ValueError, 'Unrelated remote file changed: ' + OTHER):
            self.maintain(commit=True)
        self.assertTrue(self.receipt()['status'].startswith('failed: ValueError: Unrelated'))

    def test_broader_lfs_attribute_change_refused(self):
        def broaden(hub):
            hub.files['.gitattributes'] += b'*.json' + RULE + b'\n'
        self.hub.after_commit = broaden
        with self.assertRaisesRegex(ValueError, 'LFS attributes'):
            self.maintain(commit=True)

    def test_existing_judge_file_difference_refused(self):
        self.edit_remote({RUN + '/analysis/judge/report.html': b'another judge build'})
        with self.assertRaisesRegex(ValueError, 'Published judge file differs'):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()

    def test_public_space_refused(self):
        self.hub.private = False
        with self.assertRaisesRegex(ValueError, 'private Space'):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()

    def test_concurrent_revision_refused_before_first_write(self):
        self.hub.concurrent = True
        with self.assertRaisesRegex(ValueError, 'concurrently'):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()

    def test_concurrent_revision_between_batches_refused(self):
        def collaborator(hub):
            hub.after_commit = None
            hub.push(hub.files)
        self.hub.after_commit = collaborator
        with self.assertRaisesRegex(ValueError, 'concurrently'):
            self.maintain(commit=True)
        self.assertEqual(len(self.hub.commits), self.publish_commits + 1)
        self.assertEqual(self.hub.files['experiments.json'], self.published['experiments.json'])
        receipt = self.receipt()
        self.assertEqual(receipt['committed_batches'], 1)
        self.assertTrue(receipt['status'].startswith('failed: ValueError: Space changed concurrently'))

    def test_space_turning_public_between_batches_refused(self):
        def publicize(hub):
            hub.private = False
        self.hub.after_commit = publicize
        with self.assertRaisesRegex(ValueError, 'no longer private'):
            self.maintain(commit=True)
        self.assertEqual(len(self.hub.commits), self.publish_commits + 1)

    def test_unpublished_run_refused(self):
        catalogue = json.loads(self.published['experiments.json'])
        without = {**catalogue, 'experiments': [e for e in catalogue['experiments'] if e['run'] != RUN]}
        manifest = json.loads(self.published['archive-manifest.json'])
        cases = {'catalogue lacks the run': ({'experiments.json': json.dumps(without, indent=2).encode()},
                                             'catalogue must list the run'),
                 'manifest lacks the run': ({'archive-manifest.json': json.dumps(
                     {**manifest, 'runs': [OTHER]}).encode()}, 'manifest does not list the run'),
                 'catalogue warnings': ({'experiments.json': json.dumps(
                     {**catalogue, 'warnings': ['x']}, indent=2).encode()}, 'warnings'),
                 'manifest warnings': ({'archive-manifest.json': json.dumps(
                     {**manifest, 'warnings': ['x']}).encode()}, 'Remote catalogue or manifest has warnings')}
        for label, (change, message) in cases.items():
            with self.subTest(label):
                self.edit_remote(change)
                with self.assertRaisesRegex(ValueError, message):
                    self.maintain(commit=True, attempt=label)
                self.assert_no_maintenance_commit()
        self.hub = Hub(self.root / 'other-hub')
        publish(self.prepare('other-only', OTHER), self.hub, self.hub.download, commit=True, operation=operation)
        commits = len(self.hub.commits)
        with self.assertRaisesRegex(ValueError, 'not published'):
            self.maintain(commit=True, attempt='unpublished')
        self.assertEqual(len(self.hub.commits), commits)

    def test_multi_run_bundle_refused(self):
        with self.assertRaisesRegex(ValueError, 'exactly one run'):
            self.maintain(commit=True, bundle=self.prepare('both', RUN, OTHER))
        self.assert_no_maintenance_commit()

    def test_bundle_judge_file_outside_allowlist_refused(self):
        self.edit_bundle({RUN + '/analysis/judge/raw/request-0001.json': b'{}'})
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            self.maintain()

    def test_bundle_judge_files_must_match_preparation_record(self):
        cases = {'count differs': lambda m: m['behavior_judge'][RUN].update(included=len(self.judge) + 1),
                 'record missing': lambda m: m.pop('behavior_judge')}
        for label, edit in cases.items():
            with self.subTest(label):
                self.edit_bundle({}, edit)
                with self.assertRaisesRegex(ValueError, 'Bundle judge files do not match the preparation record'):
                    self.maintain()

    def test_catalogue_link_to_skipped_bundle_only_file_refused(self):
        timeline = RUN + '/analysis/timeline.md'
        catalogue = json.loads((self.bundle / 'experiments.json').read_text())
        catalogue['experiments'][0]['links']['Timeline'] = timeline
        self.edit_bundle({timeline: b'# Timeline\n', 'experiments.json': json.dumps(catalogue, indent=2).encode()})
        with self.assertRaisesRegex(ValueError, 'Catalogue link Timeline would point to a missing file: ' + timeline):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()

    def test_download_not_matching_listed_metadata_refused(self):
        def download(repo, name, **kwargs):
            path = Path(self.hub.download(repo, name, **kwargs))
            if name == 'experiments.json':
                path.write_bytes(path.read_bytes() + b' ')
            return str(path)
        with self.assertRaisesRegex(ValueError, 'Remote metadata mismatch: experiments.json'):
            self.maintain(commit=True, download=download)
        self.assert_no_maintenance_commit()

    def test_missing_required_remote_file_refused(self):
        self.hub.push({k: v for k, v in self.published.items() if k != RUN + '/results.json'})
        with self.assertRaisesRegex(ValueError, 'Remote file required for maintenance is missing: ' + RUN + '/results.json'):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()

    def test_published_judge_file_missing_from_bundle_refused(self):
        stale = RUN + '/analysis/judge/packages/agent-999.json'
        self.edit_remote({stale: b'{}'})
        for commit in (False, True):
            with self.assertRaisesRegex(ValueError, r'1 published judge file\(s\) are not in the bundle.*' + stale):
                self.maintain(commit=commit)
        self.assert_no_maintenance_commit()
        self.assertEqual(self.hub.files[stale], b'{}')  # never deleted automatically

    def test_verification_refuses_unplanned_final_state(self):
        cases = {
            'extra file': (lambda hub: hub.files.__setitem__(RUN + '/transcripts/agent-001.html', b'not planned'),
                           'inventory changed unexpectedly'),
            'dropped file': (lambda hub: hub.files.pop(OTHER + '/results.html'), 'inventory changed unexpectedly'),
            'uploaded bytes differ': (lambda hub: hub.files.__setitem__(RUN + '/results.html', b'other bytes'),
                                      'hash mismatch after maintenance: ' + RUN + '/results.html'),
            'catalogue bytes differ': (lambda hub: hub.files.__setitem__('archive-manifest.json', b'{}'),
                                       'hash mismatch after maintenance: archive-manifest.json'),
            'revision moved after last batch': (lambda hub: hub.push(hub.files), 'changed before verification'),
            'turned public after last batch': (lambda hub: setattr(hub, 'private', False),
                                               'changed before verification')}
        for label, (change, message) in cases.items():
            with self.subTest(label):
                self.restart(label)
                self.on_last_commit(change)
                with self.assertRaisesRegex(ValueError, message):
                    self.maintain(commit=True, attempt=label)
                self.assertTrue(self.receipt(label)['status'].startswith('failed: ValueError'))

    def test_lfs_rule_for_replaced_report_refused(self):
        self.hub.lfs_upload = lambda name, data: '/packages/' in name or name == RUN + '/results.json'
        with self.assertRaisesRegex(ValueError, 'LFS attributes addition is not a newly uploaded run file'):
            self.maintain(commit=True)
        self.assertIn((RUN + '/results.json').encode() + RULE, self.hub.files['.gitattributes'])

    def test_idempotent_resume_after_partial_upload(self):
        self.hub.fail_after = self.publish_commits + 1  # the catalogue batch is interrupted
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.maintain(commit=True)
        self.assertEqual(self.receipt()['committed_batches'], 1)
        self.assertEqual(self.receipt()['status'], 'failed: RuntimeError')
        self.assertEqual(self.hub.files['experiments.json'], self.published['experiments.json'])
        self.hub.fail_after = None
        plan = self.maintain(resume_from=self.records())
        self.assertEqual((plan['to_add'], plan['to_replace']), ([], []))
        self.assertEqual((plan['added'], plan['replaced']), (self.judge, self.replaced))  # relative to the original
        self.assertEqual((plan['judge_already_present'], plan['replacement_identical']), (self.judge, self.replaced))
        self.assertEqual((plan['origin_revision'], plan['resume_required']), (self.base, False))
        self.assertNotEqual(plan['base_revision'], self.base)
        self.assertEqual(plan['catalogue']['files'], CATALOGUE)
        self.assertEqual(plan['archive_manifest']['changed_digests'],
                         sorted(self.replaced + self.judge + ['experiments.json', 'index.html']))
        # A second, resumed attempt is interrupted too: its own records start mid-maintenance.
        self.hub.fail_after = self.publish_commits + 1
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.maintain(commit=True, attempt='attempt-2', resume_from=self.records())
        self.hub.fail_after = None
        with self.assertRaisesRegex(ValueError, "first attempt's backup directory.*" + str(self.records())):
            self.maintain(commit=True, attempt='attempt-3', resume_from=self.records('attempt-2'))
        result = self.maintain(commit=True, attempt='attempt-3', resume_from=self.records())
        self.assertEqual((result['status'], result['committed'], result['committed_batches']), ('verified', True, 1))
        self.assert_final_state()
        receipt = self.receipt('attempt-3')
        self.assertEqual((receipt['origin_revision'], receipt['resume_from']), (self.base, str(self.records())))
        self.assertEqual((receipt['replaced'], receipt['added']), (self.replaced, self.judge))
        self.assertEqual(receipt['verification']['against_revision'], self.base)
        self.assertEqual(receipt['verification']['replaced_files'], 5)
        # The first attempt's backups keep the true originals; later attempts have their own.
        self.assertEqual((self.records() / 'original' / self.replaced[0]).read_bytes(), self.published[self.replaced[0]])
        commits = len(self.hub.commits)
        with self.assertRaisesRegex(ValueError, 'already partly applied'):
            self.maintain(commit=True, attempt='attempt-4')
        again = self.maintain(commit=True, attempt='attempt-5', resume_from=self.records())
        self.assertEqual((again['uploads'], again['committed'], again['status'], len(self.hub.commits)),
                         (0, False, 'verified', commits))

    def test_resume_refuses_unrelated_change_left_by_failed_attempt(self):
        self.hub.after_commit = lambda hub: hub.files.__setitem__(OTHER + '/results.html', b'changed outside the plan')
        with self.assertRaisesRegex(ValueError, 'Unrelated remote file changed: ' + OTHER):
            self.maintain(commit=True)
        self.hub.after_commit = None
        self.assert_resume_refused('Unrelated remote file changed: ' + OTHER + '/results.html')

    def test_resume_refuses_broader_lfs_rule_left_by_failed_attempt(self):
        def broaden(hub):
            hub.files['.gitattributes'] += b'*.json' + RULE + b'\n'
            hub.after_commit = None
        self.hub.after_commit = broaden
        with self.assertRaisesRegex(ValueError, 'LFS attributes'):
            self.maintain(commit=True)
        self.assert_resume_refused('LFS attributes addition is not a newly uploaded run file')

    def test_resume_refuses_change_made_during_an_interrupted_attempt(self):
        def tamper(hub):
            hub.files[OTHER + '/results.html'] = b'changed during the judge batch'
            hub.after_commit = None
        self.hub.after_commit = tamper
        self.hub.fail_after = self.publish_commits + 1
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.maintain(commit=True)
        self.hub.fail_after = None
        self.assert_resume_refused('Unrelated remote file changed: ' + OTHER + '/results.html')

    def test_resume_refuses_other_catalogue_or_manifest_change_since_original(self):
        self.hub.fail_after = self.publish_commits + 1
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.maintain(commit=True)
        self.hub.fail_after = None
        partial = dict(self.hub.files)
        catalogue = json.loads(partial['experiments.json'])
        edited = [{**e, 'track': 'edited'} if e['run'] == OTHER else e for e in catalogue['experiments']]
        manifest = json.loads(partial['archive-manifest.json'])
        # A consistently regenerated catalogue (both files), so only the since-original check can refuse it.
        cases = {'another catalogue entry': (maintain_hf_run.regenerate(edited),
                                             'Another catalogue entry changed since the original revision ' + self.base),
                 'another manifest record': ({'archive-manifest.json': json.dumps(
                     {**manifest, 'note': 'x'}, indent=2).encode()}, 'archive manifest changed outside the maintenance')}
        for label, (change, message) in cases.items():
            with self.subTest(label):
                self.hub.push({**partial, **change})
                with self.assertRaisesRegex(ValueError, message):
                    self.maintain(commit=True, attempt=label, resume_from=self.records())

    def test_resume_after_failed_verification_verifies_against_original(self):
        listing, state = self.hub.list_repo_tree, {'fail': True}
        def flaky(repo, **kwargs):
            if state['fail'] and len(self.hub.commits) > self.publish_commits:
                raise ConnectionError('transient listing failure')
            return listing(repo, **kwargs)
        self.hub.list_repo_tree = flaky
        with self.assertRaises(ConnectionError):
            self.maintain(commit=True)
        self.assertEqual(self.receipt()['status'], 'failed: ConnectionError')
        state['fail'] = False
        with self.assertRaisesRegex(ValueError, 'already partly applied'):
            self.maintain(commit=True, attempt='attempt-2')
        commits = len(self.hub.commits)
        self.maintain(commit=True, attempt='attempt-3', resume_from=self.records())
        self.assertEqual(len(self.hub.commits), commits)
        receipt = self.receipt('attempt-3')
        self.assertEqual((receipt['status'], receipt['committed'], receipt['committed_batches'], receipt['uploads']),
                         ('verified', False, 0, 0))
        self.assertEqual((receipt['origin_revision'], receipt['replaced'], receipt['added']),
                         (self.base, self.replaced, self.judge))
        self.assertEqual((receipt['verification']['against_revision'], receipt['verification']['replaced_files'],
                          receipt['verification']['added_files']), (self.base, 5, len(self.judge)))
        self.assert_final_state()

    def test_resume_records_must_match_their_inventory(self):
        self.hub.fail_after = self.publish_commits + 1
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.maintain(commit=True)
        self.hub.fail_after = None
        def edit_original(path):
            (path / 'original' / 'experiments.json').write_bytes(b'{}')
        def drop_attributes(path):
            (path / 'original' / '.gitattributes').unlink()
        def other_run(path):
            record = json.loads((path / 'before-inventory.json').read_text())
            (path / 'before-inventory.json').write_text(json.dumps({**record, 'run': OTHER}))
        cases = {'edited original': (edit_original, 'Original backup missing or not matching the before-inventory: '
                                                    'experiments.json'),
                 'missing original': (drop_attributes, 'Original backup missing.*: .gitattributes'),
                 'another run': (other_run, 'another operation, repository or run')}
        for label, (edit, message) in cases.items():
            with self.subTest(label):
                copy = self.root / 'records' / ('copy-' + label.replace(' ', '-'))
                shutil.copytree(self.records(), copy)
                edit(copy)
                with self.assertRaisesRegex(ValueError, message):
                    self.maintain(resume_from=copy)

    def test_output_paths_and_existing_records_refused(self):
        self.records().mkdir(parents=True)
        (self.records() / 'kept.json').write_text('{}')
        backup = self.records('attempt-2')
        cases = [({'backup_dir': self.records()}, 'new or empty'),
                 ({'receipt': self.records() / 'kept.json'}, 'Receipt already exists'),
                 ({'receipt': self.bundle / 'receipt.json'}, 'outside the immutable'),
                 ({'backup_dir': self.bundle / 'backups'}, 'outside the immutable'),
                 ({'backup_dir': None}, 'requires a receipt and a backup directory'),
                 ({'receipt': backup}, 'must not replace the backup directory'),
                 ({'receipt': backup / 'before-inventory.json'}, 'must not replace the backup directory'),
                 ({'receipt': backup / 'plan.json'}, 'must not replace the backup directory'),
                 ({'receipt': backup / 'original' / 'experiments.json'}, 'must not replace the backup directory'),
                 ({'resume_from': self.root / 'missing'}, 'resume-from must be'),
                 ({'resume_from': self.records(), 'receipt': self.records() / 'new.json'},
                  "outside the resumed attempt's records"),
                 ({'resume_from': self.records(), 'backup_dir': self.records() / 'attempt-2'},
                  "outside the resumed attempt's records")]
        for options, message in cases:
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, message):
                    self.maintain(commit=True, attempt='attempt-2', **options)
        self.assert_no_maintenance_commit()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--commit'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--commit',
                  '--backup-dir', str(self.bundle / 'b'), '--receipt', str(self.root / 'r.json')])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root ignores directory permissions')
    def test_unwritable_receipt_fails_before_any_upload(self):
        locked = self.root / 'locked'
        locked.mkdir()
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        with self.assertRaises(PermissionError):
            self.maintain(commit=True, receipt=locked / 'receipt.json')
        self.assert_no_maintenance_commit()
        self.assertFalse((self.records() / 'before-inventory.json').exists())

    def test_receipt_failure_does_not_hide_the_original_error(self):
        write = maintain_hf_run.write_json
        def failing(path, data):
            if str(data.get('status', '')).startswith('failed'):
                raise OSError('receipt volume unavailable')
            write(path, data)
        self.hub.after_commit = lambda hub: hub.files.__setitem__(OTHER + '/results.html', b'changed outside the plan')
        with patch.object(maintain_hf_run, 'write_json', failing), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaisesRegex(ValueError, 'Unrelated remote file changed'):
                self.maintain(commit=True)
        self.assertIn('receipt volume unavailable', stderr.getvalue())
        self.assertEqual(self.receipt()['status'], 'uploading; catalogue may be pending')


if __name__ == '__main__':
    unittest.main()
