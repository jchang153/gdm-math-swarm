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
        # These runs model publications from before October 21, 2026, which carried the transcript export.
        output = self.root / 'bundles' / name
        argv = ['prepare_hf_library.py', '--runs-dir', str(self.runs), '--output-dir', str(output),
                '--include-transcript-export']
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


TRANSCRIPTS = ['agent-001.html', 'agent-002.html', 'agent-003.html', 'index.html']
MANIFEST = 'archive-manifest.json'
LFS_STORED = r'transcript\(s\) stored as LFS'


def one_file_batches(items):
    """Commit every upload separately, so an interruption can fall between transcripts."""
    return ([item] for item in items)


class TranscriptMaintenanceTests(unittest.TestCase):
    """--scope transcripts: only existing RUN/transcripts/ files and their manifest digests change."""
    prepare, records, receipt, edit_bundle, edit_remote, restart, assert_no_maintenance_commit = (
        MaintenanceTests.prepare, MaintenanceTests.records, MaintenanceTests.receipt, MaintenanceTests.edit_bundle,
        MaintenanceTests.edit_remote, MaintenanceTests.restart, MaintenanceTests.assert_no_maintenance_commit)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        run = make_run(self.runs, RUN, judge={})  # judge outputs already published, as for cache100
        for name in TRANSCRIPTS:
            (run / 'transcripts' / name).write_text(f'<!doctype html><html><head></head><body>final {name}</body></html>')
        self.hub = Hub(self.root)
        publish(self.prepare('published', RUN, OTHER), self.hub, self.hub.download, commit=True,
                operation=operation)
        # After publication: full-history exports for three pages (agent-003 unchanged by the
        # exporter) and local report changes that do not alter the catalogue entry.
        for name in TRANSCRIPTS[:2] + TRANSCRIPTS[3:]:
            (run / 'transcripts' / name).write_text(
                f'<!doctype html><html><head></head><body>full history {name} {"m" * 200}</body></html>')
        (run / 'results.html').write_text('<!doctype html><html><h1>R, edited locally</h1></html>')
        results = json.loads((run / 'results.json').read_text())
        (run / 'results.json').write_text(json.dumps({**results, 'local_note': 'not for upload'}))
        self.bundle = self.prepare('maintenance', RUN)
        self.judge_edit = RUN + '/analysis/judge/labels.jsonl'
        self.edit_bundle({self.judge_edit: b'{"label": "rebuilt locally"}\n'})
        self.published = dict(self.hub.files)
        self.base = self.hub.sha
        self.publish_commits = len(self.hub.commits)
        self.replaced = [RUN + '/transcripts/' + n for n in TRANSCRIPTS if n != 'agent-003.html']
        self.identical = [RUN + '/transcripts/agent-003.html']

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.records(attempt))
            kwargs.setdefault('receipt', self.records(attempt) / 'receipt.json')
        return maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, commit=commit,
                            operation=operation, scope='transcripts', **kwargs)

    def new_bundle(self, label):
        self.bundle = self.prepare(label, RUN)
        return self.bundle

    def assert_final_state(self):
        final = self.hub.files
        self.assertEqual(set(final), set(self.published))  # nothing added or removed
        for name, data in self.published.items():
            if name in self.replaced:
                self.assertEqual(final[name], (self.bundle / name).read_bytes(), name)
            elif name != MANIFEST:  # catalogue, .gitattributes, reports, judge files, agent-003
                self.assertEqual(final[name], data, name)
        old, new = (json.loads(x[MANIFEST]) for x in (self.published, final))
        self.assertEqual({k: v for k, v in new.items() if k not in ('files', 'total_bytes')},
                         {k: v for k, v in old.items() if k not in ('files', 'total_bytes')})
        self.assertEqual(set(new['files']), set(old['files']))
        self.assertEqual({n for n in new['files'] if new['files'][n] != old['files'][n]}, set(self.replaced))
        for name in self.replaced:
            self.assertEqual(new['files'][name], digest(final[name]))
        self.assertEqual(new['total_bytes'], sum(x['bytes'] for x in new['files'].values()))

    def test_dry_run_plan(self):
        result = self.maintain()
        self.assertEqual((result['scope'], result['committed']), ('transcripts', False))
        self.assert_no_maintenance_commit()
        self.assertFalse((self.root / 'records').exists())
        self.assertEqual((result['replaced'], result['to_replace']), (self.replaced, self.replaced))
        self.assertEqual((result['added'], result['to_add']), ([], []))
        self.assertEqual(result['replaced_sizes'], [
            {'path': n, 'old_bytes': len(self.published[n]), 'new_bytes': len((self.bundle / n).read_bytes())}
            for n in self.replaced])
        self.assertEqual(result['replacement_identical'], self.identical)  # left unchanged by the exporter
        self.assertEqual((result['already_applied'], result['resume_required']), ([], False))
        self.assertEqual(result['transcripts'], {'files': 4, 'file_sets_equal': True, 'largest_new_bytes': max(
            len((self.bundle / RUN / 'transcripts' / n).read_bytes()) for n in TRANSCRIPTS),
            'lfs_safe_limit_bytes': int(9.5 * 1024 * 1024)})
        self.assertIs(result['catalogue_unchanged'], True)
        self.assertEqual(result['catalogue'], {'files': [], 'entry_diff': {}, 'other_entries_unchanged': 1})
        manifest = result['archive_manifest']
        self.assertEqual((manifest['changed_digests'], manifest['upload']), (self.replaced, True))
        self.assertEqual(manifest['total_bytes_after'] - manifest['total_bytes_before'],
                         sum(r['new_bytes'] - r['old_bytes'] for r in result['replaced_sizes']))
        skipped = result['skipped_local_differences']
        self.assertEqual({r['path']: r['reason'] for r in skipped['names']}, {
            RUN + '/results.html': 'differs_from_remote', RUN + '/results.json': 'differs_from_remote',
            self.judge_edit: 'differs_from_remote'})
        self.assertEqual(skipped['by_area'], {'results.html': 1, 'results.json': 1, 'analysis': 1})
        self.assertEqual(result['uploads'], 4)
        self.assertNotIn('judge_already_present', result)

    def test_commit_replaces_only_transcripts_and_verifies(self):
        result = self.maintain(commit=True)
        self.assertTrue(result['committed'])
        self.assert_final_state()
        commits = self.hub.commits[self.publish_commits:]
        self.assertEqual(commits[-1]['paths'], [MANIFEST])  # the manifest last
        self.assertEqual({p for c in commits[:-1] for p in c['paths']}, set(self.replaced))
        self.assertTrue(all(c['message'].endswith('regenerated transcript exports') for c in commits))
        receipt = self.receipt()
        self.assertEqual((receipt['status'], receipt['scope'], receipt['final_revision']),
                         ('verified', 'transcripts', self.hub.sha))
        self.assertEqual(receipt['verification'], {
            'against_revision': self.base, 'final_files': len(self.published), 'uploaded_files': 4,
            'replaced_files': 4, 'added_files': 0, 'verified_planned_files': 5,
            'verified_preserved_files': len(self.published) - 5, 'gitattributes': 'unchanged',
            'catalogue': 'unchanged'})
        backup = self.records()
        inventory = json.loads((backup / 'before-inventory.json').read_text())
        self.assertEqual((inventory['scope'], inventory['revision'], inventory['origin_revision']),
                         ('transcripts', self.base, self.base))
        self.assertEqual(set(inventory['files']), set(self.published))
        saved = sorted(self.replaced + [MANIFEST, '.gitattributes'])
        self.assertEqual(receipt['backups'], saved)
        self.assertEqual(sorted(p.relative_to(backup / 'original').as_posix()
                                for p in (backup / 'original').rglob('*') if p.is_file()), saved)
        for name in saved:
            self.assertEqual((backup / 'original' / name).read_bytes(), self.published[name], name)
        plan = json.loads((backup / 'plan.json').read_text())
        self.assertEqual((plan['scope'], plan['committed'], plan['replaced']), ('transcripts', False, self.replaced))

    def test_transcript_file_sets_must_match(self):
        extra = RUN + '/transcripts/agent-004.html'
        cases = {'extra local file': ('extra', r'only in the bundle \[.*agent-004\.html.*\], only published \[\]'),
                 'missing local file': ('missing', r"only in the bundle \[\], only published \['" + RUN
                                        + r'/transcripts/agent-003\.html'),
                 'extra published file': ('remote', r"only in the bundle \[\], only published \['" + extra)}
        for label, (kind, message) in cases.items():
            with self.subTest(label):
                self.restart(label)
                self.new_bundle(label.replace(' ', '-'))
                if kind == 'extra':
                    self.edit_bundle({extra: b'<!doctype html><html><body>new page</body></html>'})
                elif kind == 'missing':
                    gone = RUN + '/transcripts/agent-003.html'
                    (self.bundle / gone).unlink()
                    self.edit_bundle({}, lambda m: m['files'].pop(gone))
                else:
                    self.edit_remote({extra: b'published only'})
                for commit in (False, True):
                    with self.assertRaisesRegex(ValueError, 'transcript file sets .* differ.*' + message):
                        self.maintain(commit=commit, attempt=label)
                self.assertEqual(len(self.hub.commits), self.publish_commits)
                self.assertFalse(self.records(label).exists())

    def test_catalogue_entry_change_refused(self):
        catalogue = json.loads((self.bundle / 'experiments.json').read_text())
        catalogue['experiments'][0]['track'] = 'edited'
        self.edit_bundle({'experiments.json': json.dumps(catalogue, indent=2).encode()})
        for commit in (False, True):
            with self.assertRaisesRegex(ValueError, "catalogue entry for cache-run differs.*never changes the "
                                                    'catalogue.*"track"'):
                self.maintain(commit=commit)
        self.assert_no_maintenance_commit()
        self.assertFalse(self.records().exists())

    def test_lfs_storage_or_attribute_change_fails_verification(self):
        def broaden(hub):
            if MANIFEST in hub.commits[-1]['paths']:
                hub.files['.gitattributes'] += b'*.json' + RULE + b'\n'
        cases = {'transcript stored as LFS': (
                     lambda hub: setattr(hub, 'lfs_upload', lambda name, data: '/packages/' in name
                                         or name.endswith('agent-002.html')),
                     LFS_STORED + '.*' + RUN + '/transcripts/agent-002.html'),
                 'gitattributes changed': (lambda hub: setattr(hub, 'after_commit', broaden),
                                           r'\.gitattributes changed relative to the original revision ' + self.base)}
        for label, (setup, message) in cases.items():
            with self.subTest(label):
                self.restart(label)
                setup(self.hub)
                with self.assertRaisesRegex(ValueError, message):
                    self.maintain(commit=True, attempt=label)
                self.assertTrue(self.receipt(label)['status'].startswith('failed: ValueError'))

    def test_unplanned_final_state_fails_verification(self):
        cases = {'catalogue page changed': (lambda hub: hub.files.__setitem__('index.html', b'regenerated'),
                                            'Unrelated remote file changed: index.html'),
                 'report changed': (lambda hub: hub.files.__setitem__(RUN + '/results.html', b'local report'),
                                    'Unrelated remote file changed: ' + RUN + '/results.html'),
                 'file added': (lambda hub: hub.files.__setitem__(RUN + '/transcripts/agent-004.html', b'x'),
                                'inventory changed unexpectedly'),
                 'transcript bytes differ': (lambda hub: hub.files.__setitem__(self.replaced[0], b'other'),
                                             'hash mismatch after maintenance: ' + self.replaced[0])}
        for label, (change, message) in cases.items():
            with self.subTest(label):
                self.restart(label)
                def hook(hub, change=change):
                    if MANIFEST in hub.commits[-1]['paths']:
                        change(hub)
                self.hub.after_commit = hook
                with self.assertRaisesRegex(ValueError, message):
                    self.maintain(commit=True, attempt=label)

    def test_fresh_commit_refused_on_partly_applied_state(self):
        # One transcript already equals the bundle while the manifest still records its old digest.
        first = self.replaced[0]
        self.edit_remote({first: (self.bundle / first).read_bytes()})
        plan = self.maintain()
        self.assertEqual((plan['already_applied'], plan['resume_required']), ([first], True))
        self.assertEqual(plan['replacement_identical'], [first] + self.identical)  # agent-003 is no sign
        commits = len(self.hub.commits)
        with self.assertRaisesRegex(ValueError, 'already partly applied.*differ from the record.*--resume-from'):
            self.maintain(commit=True)
        self.assertEqual(len(self.hub.commits), commits)
        self.assertFalse(self.records().exists())

    def test_interrupted_attempt_resumes_against_original(self):
        with patch.object(maintain_hf_run, 'batches', one_file_batches):
            self.hub.fail_after = self.publish_commits + 1  # after the first transcript
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.maintain(commit=True)
            self.hub.fail_after = None
            self.assertEqual((self.receipt()['committed_batches'], self.receipt()['status']), (1, 'failed: RuntimeError'))
            first = self.replaced[0]
            self.assertEqual(self.hub.files[first], (self.bundle / first).read_bytes())
            self.assertEqual(self.hub.files[MANIFEST], self.published[MANIFEST])
            fresh = self.maintain()
            self.assertEqual((fresh['already_applied'], fresh['resume_required']), ([first], True))
            with self.assertRaisesRegex(ValueError, 'already partly applied'):
                self.maintain(commit=True, attempt='attempt-2')
            self.assertFalse(self.records('attempt-2').exists())
            with self.assertRaisesRegex(ValueError, 'belong to the transcripts scope, not the judge scope'):
                maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, resume_from=self.records())
            plan = self.maintain(resume_from=self.records())
            self.assertEqual((plan['origin_revision'], plan['resume_required']), (self.base, False))
            self.assertNotEqual(plan['base_revision'], self.base)
            self.assertEqual((plan['replaced'], plan['to_replace']), (self.replaced, self.replaced[1:]))
            self.assertEqual(plan['replacement_identical'], [first] + self.identical)
            self.assertEqual(plan['archive_manifest']['changed_digests'], self.replaced)
            result = self.maintain(commit=True, attempt='attempt-2', resume_from=self.records())
        self.assertEqual((result['status'], result['committed'], result['committed_batches']), ('verified', True, 3))
        self.assert_final_state()
        receipt = self.receipt('attempt-2')
        self.assertEqual((receipt['origin_revision'], receipt['resume_from']), (self.base, str(self.records())))
        self.assertEqual((receipt['verification']['against_revision'], receipt['verification']['replaced_files']),
                         (self.base, 4))
        for name in self.replaced:  # the first attempt's backups keep the true originals
            self.assertEqual((self.records() / 'original' / name).read_bytes(), self.published[name])
        # Fully applied: nothing to upload; a fresh commit makes no commit and points to --resume-from,
        # which verifies the result against the original state without committing.
        commits = len(self.hub.commits)
        done = self.maintain()
        self.assertEqual((done['uploads'], done['to_replace'], done['already_applied'], done['resume_required']),
                         (0, [], [], False))
        self.assertFalse(done['archive_manifest']['upload'])
        self.assertEqual(done['archive_manifest']['changed_digests'], [])  # relative to this revision's manifest
        with self.assertRaisesRegex(ValueError, 'Nothing to upload.*--resume-from'):
            self.maintain(commit=True, attempt='attempt-3')
        self.assertFalse(self.records('attempt-3').exists())
        again = self.maintain(commit=True, attempt='attempt-4', resume_from=self.records())
        self.assertEqual((again['uploads'], again['committed'], again['status'], len(self.hub.commits)),
                         (0, False, 'verified', commits))
        self.assertEqual(again['replaced'], self.replaced)

    def interrupt_after_first_transcript(self):
        """Attempt-1 commits only the first transcript (one file per batch) and is interrupted."""
        with patch.object(maintain_hf_run, 'batches', one_file_batches):
            self.hub.fail_after = self.publish_commits + 1
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.maintain(commit=True)
        self.hub.fail_after = None
        self.assertEqual(self.hub.files[self.replaced[0]], (self.bundle / self.replaced[0]).read_bytes())
        self.assertEqual(self.hub.files[MANIFEST], self.published[MANIFEST])

    def test_lfs_transcript_left_by_failed_attempt_refused_before_any_write(self):
        # Attempt-1 uploads every transcript and the manifest; HF stores agent-002 as LFS and
        # appends an exact rule, so the final verification fails.
        agent = RUN + '/transcripts/agent-002.html'
        def store_as_lfs(hub):
            if MANIFEST in hub.commits[-1]['paths']:
                hub.files['.gitattributes'] += agent.encode() + RULE + b'\n'
        self.hub.after_commit = store_as_lfs
        with self.assertRaisesRegex(ValueError, LFS_STORED + '.*' + agent):
            self.maintain(commit=True)
        self.hub.after_commit = None
        self.assertTrue(Hub.tracked(self.hub.files, agent))
        commits = len(self.hub.commits)
        # Neither the dry run nor a fresh commit after re-exporting a smaller page looks clean or
        # ends verified with the transcript still in LFS; the resume is refused too.
        with self.assertRaisesRegex(ValueError, LFS_STORED + '.*' + agent):
            self.maintain()
        (self.runs / RUN / 'transcripts' / 'agent-002.html').write_text(
            '<!doctype html><html><head></head><body>shrunk</body></html>')
        self.new_bundle('shrunk')
        for options in ({}, {'commit': True}, {'commit': True, 'resume_from': self.records()}):
            with self.subTest(**{k: str(v) for k, v in options.items()}):
                with self.assertRaisesRegex(ValueError, LFS_STORED + '.*' + agent):
                    self.maintain(attempt='attempt-2', **options)
        self.assertEqual(len(self.hub.commits), commits)
        self.assertFalse(self.records('attempt-2').exists())
        self.assertTrue(Hub.tracked(self.hub.files, agent))

    def test_lfs_rule_naming_a_transcript_refused_before_any_write(self):
        # A rule without a slash matches the basename at any depth in git (the fake Hub does not
        # model that, so the published page is still listed as a regular file).
        agent = RUN + '/transcripts/agent-001.html'
        self.edit_remote({'.gitattributes': self.published['.gitattributes'] + b'agent-001.html' + RULE + b'\n'})
        self.assertFalse(Hub.tracked(self.hub.files, agent))
        for commit in (False, True):
            with self.assertRaisesRegex(ValueError, r'LFS rule\(s\) in \.gitattributes .* match.*' + agent):
                self.maintain(commit=commit)
        self.assert_no_maintenance_commit()
        self.assertFalse(self.records().exists())

    def test_transcript_at_lfs_threshold_refused_before_any_write(self):
        limit = int(9.5 * 1024 * 1024)
        self.hub.lfs_upload = lambda name, data: '/packages/' in name or len(data) >= 10_000_000
        agent = RUN + '/transcripts/agent-002.html'
        (self.runs / RUN / 'transcripts' / 'agent-002.html').write_text(
            '<!doctype html><html><head></head><body>' + 'm' * limit + '</body></html>')
        self.new_bundle('large')
        size = len((self.bundle / agent).read_bytes())
        self.assertLess(size, 10_000_000)  # below HF's threshold, but too close to it
        for commit in (False, True):
            with self.assertRaisesRegex(ValueError, f'at or above {limit} bytes.*{agent} \\({size} bytes\\)'):
                self.maintain(commit=commit)
        self.assert_no_maintenance_commit()
        self.assertFalse(self.records().exists())
        with patch.object(maintain_hf_run, 'LFS_SAFE_BYTES', size):
            with self.assertRaisesRegex(ValueError, f'at or above {size} bytes'):
                self.maintain()
        with patch.object(maintain_hf_run, 'LFS_SAFE_BYTES', size + 1):
            plan = self.maintain()
        self.assertEqual((plan['transcripts']['largest_new_bytes'], plan['transcripts']['lfs_safe_limit_bytes']),
                         (size, size + 1))

    def test_lfs_storage_stops_before_the_manifest(self):
        agent = self.replaced[0]
        self.hub.lfs_upload = lambda name, data: '/packages/' in name or name == agent
        with patch.object(maintain_hf_run, 'batches', one_file_batches):
            with self.assertRaisesRegex(ValueError, LFS_STORED + '.*' + agent):
                self.maintain(commit=True)
        self.assertEqual(len(self.hub.commits), self.publish_commits + 1)  # no other transcript, no manifest
        self.assertEqual(self.hub.files[MANIFEST], self.published[MANIFEST])
        receipt = self.receipt()
        self.assertEqual(receipt['committed_batches'], 1)
        self.assertTrue(receipt['status'].startswith('failed: ValueError: Published transcript(s) stored as LFS'))
        # The state stays detectably partly applied: every later attempt is refused before any write.
        commits = len(self.hub.commits)
        for options in ({}, {'commit': True}, {'commit': True, 'resume_from': self.records()}):
            with self.subTest(**{k: str(v) for k, v in options.items()}):
                with self.assertRaisesRegex(ValueError, LFS_STORED + '.*' + agent):
                    self.maintain(attempt='attempt-2', **options)
        self.assertEqual(len(self.hub.commits), commits)
        self.assertFalse(self.records('attempt-2').exists())

    def test_partly_applied_state_detected_after_the_bundle_changes(self):
        self.interrupt_after_first_transcript()
        first = self.replaced[0]
        # The exporter is rerun with different output and the bundle re-prepared.
        (self.runs / RUN / 'transcripts' / 'agent-001.html').write_text(
            '<!doctype html><html><head></head><body>exported again</body></html>')
        self.new_bundle('exported-again')
        self.assertNotEqual(self.hub.files[first], (self.bundle / first).read_bytes())
        plan = self.maintain()
        self.assertEqual((plan['already_applied'], plan['resume_required']), ([first], True))
        self.assertEqual(plan['replacement_identical'], self.identical)
        commits = len(self.hub.commits)
        with self.assertRaisesRegex(ValueError, 'already partly applied.*differ from the record.*--resume-from'):
            self.maintain(commit=True, attempt='attempt-2')
        self.assertEqual(len(self.hub.commits), commits)
        self.assertFalse(self.records('attempt-2').exists())
        result = self.maintain(commit=True, attempt='attempt-3', resume_from=self.records())
        self.assertEqual((result['status'], result['origin_revision']), ('verified', self.base))
        self.assertEqual(result['verification']['against_revision'], self.base)
        self.assert_final_state()

    def test_resume_records_must_hold_matching_transcript_originals(self):
        self.interrupt_after_first_transcript()
        agent = RUN + '/transcripts/agent-002.html'  # replaced by the plan, not yet uploaded
        cases = {'edited': lambda path: path.write_bytes(b'tampered'), 'missing': lambda path: path.unlink()}
        for label, edit in cases.items():
            with self.subTest(label):
                copy = self.root / 'records' / ('copy-' + label)
                shutil.copytree(self.records(), copy)
                edit(copy / 'original' / agent)
                for commit in (False, True):
                    with self.assertRaisesRegex(ValueError, 'Original backup missing or not matching the '
                                                            'before-inventory: ' + agent):
                        self.maintain(commit=commit, attempt='resume-' + label, resume_from=copy)
                self.assertFalse(self.records('resume-' + label).exists())
        self.assertEqual(self.maintain(resume_from=self.records())['origin_revision'], self.base)

    def test_replaced_transcript_without_manifest_record_refused(self):
        agent = self.replaced[0]
        manifest = json.loads(self.published[MANIFEST])
        del manifest['files'][agent]
        manifest['total_bytes'] = sum(x['bytes'] for x in manifest['files'].values())
        self.edit_remote({MANIFEST: json.dumps(manifest, indent=2).encode()})
        self.assertIn(agent, self.hub.files)
        for commit in (False, True):
            with self.assertRaisesRegex(ValueError, r'has no record of replaced transcript\(s\).*' + agent):
                self.maintain(commit=commit)
        self.assert_no_maintenance_commit()
        self.assertFalse(self.records().exists())

    def test_judge_records_are_not_a_transcripts_resume_source(self):
        legacy = self.root / 'records' / 'judge-attempt-1'
        legacy.mkdir(parents=True)
        (legacy / 'before-inventory.json').write_text(json.dumps({
            'operation': 'maintain-run', 'repo': maintain_hf_run.DEFAULT_REPO, 'run': RUN, 'revision': self.base,
            'origin_revision': self.base, 'resume_from': None, 'files': {}}))  # written before scopes existed
        with self.assertRaisesRegex(ValueError, 'belong to the judge scope, not the transcripts scope'):
            self.maintain(resume_from=legacy)

    def test_cli_scope(self):
        calls = []
        fake_hub = SimpleNamespace(HfApi=lambda: 'api', CommitOperationAdd=operation, hf_hub_download='download')
        def record(*args, **kwargs):
            calls.append(kwargs['scope'])
            return {}
        with patch.dict(sys.modules, {'huggingface_hub': fake_hub}), \
                patch.object(maintain_hf_run, 'maintain_run', record), patch('builtins.print'):
            main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--scope', 'transcripts'])
            main(['--maintain-run', RUN, '--bundle', str(self.bundle)])
        self.assertEqual(calls, ['transcripts', 'judge'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--scope', 'reports'])



class EvaluatorMaintenanceTests(unittest.TestCase):
    """--scope evaluator: reports, RUN/evaluations/ records, changed existing conference pages and the
    run's catalogue entry change; nothing else, no LFS and no deletions."""
    prepare, records, receipt, edit_bundle, edit_remote, restart, assert_no_maintenance_commit = (
        MaintenanceTests.prepare, MaintenanceTests.records, MaintenanceTests.receipt, MaintenanceTests.edit_bundle,
        MaintenanceTests.edit_remote, MaintenanceTests.restart, MaintenanceTests.assert_no_maintenance_commit)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        run = make_run(self.runs, RUN)
        (run / 'evaluations').mkdir()
        (run / 'evaluations/queue.jsonl').write_text('{"submission": "sub-1"}\n')
        (run / 'conference-view/file-1.html').write_text('<!doctype html><html><head></head><body>f</body></html>')
        self.hub = Hub(self.root)
        publish(self.prepare('published', RUN, OTHER), self.hub, self.hub.download, commit=True,
                operation=operation)
        # After publication: evaluator records, a rebuilt conference index with verdicts, a report with
        # one verified task, and a regenerated transcript (skipped by this scope).
        (run / 'evaluations/evaluations.jsonl').write_text('{"validity": "genuine"}\n')
        (run / 'evaluations/summary.json').write_text('{"by_validity": {"genuine": 1}}')
        (run / 'conference-view/index.html').write_text('<!doctype html><html><head></head><body>verdicts</body></html>')
        results = json.loads((run / 'results.json').read_text())
        results['verified_submissions'] = [{'submission': 's', 'agent': 'agent-001', 'task': 'T',
                                            'operational_outcome': 'accepted', 'independent_validity': 'genuine'}]
        (run / 'results.json').write_text(json.dumps(results))
        (run / 'results.html').write_text('<!doctype html><html><h1>R, evaluated</h1></html>')
        (run / 'transcripts/index.html').write_text('<!doctype html><html><body>newer exporter</body></html>')
        self.bundle = self.prepare('maintenance', RUN)
        self.published = dict(self.hub.files)
        self.base = self.hub.sha
        self.publish_commits = len(self.hub.commits)
        self.replaced = [RUN + '/results.html', RUN + '/results.json', RUN + '/conference-view/index.html']
        self.added = [RUN + '/evaluations/evaluations.jsonl', RUN + '/evaluations/summary.json']

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.records(attempt))
            kwargs.setdefault('receipt', self.records(attempt) / 'receipt.json')
        return maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, commit=commit,
                            operation=operation, scope='evaluator', **kwargs)

    def assert_final_state(self):
        final = self.hub.files
        self.assertEqual(set(final), set(self.published) | set(self.added))
        for name, data in self.published.items():
            if name not in set(self.replaced) | set(CATALOGUE):
                self.assertEqual(final[name], data, name)  # includes .gitattributes and the transcripts
        for name in self.replaced + self.added:
            self.assertEqual(final[name], (self.bundle / name).read_bytes(), name)
        old, new = json.loads(self.published['experiments.json']), json.loads(final['experiments.json'])
        self.assertEqual([e for e in new['experiments'] if e['run'] != RUN],
                         [e for e in old['experiments'] if e['run'] != RUN])
        self.assertEqual([e['distinct_verified_tasks'] for e in new['experiments'] if e['run'] == RUN], [1])
        manifest = json.loads(final[MANIFEST])
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        self.assertEqual(set(manifest['files']), set(json.loads(self.published[MANIFEST])['files']) | set(self.added))

    def test_dry_run_plan(self):
        result = self.maintain()
        self.assertEqual((result['scope'], result['committed']), ('evaluator', False))
        self.assert_no_maintenance_commit()
        self.assertFalse((self.root / 'records').exists())
        self.assertEqual((result['replaced'], result['added'], result['to_add']), (self.replaced, self.added, self.added))
        self.assertEqual(sorted(result['to_replace']), sorted(self.replaced))
        self.assertIn(RUN + '/evaluations/queue.jsonl', result['replacement_identical'])
        self.assertIn(RUN + '/conference-view/file-1.html', result['replacement_identical'])
        self.assertEqual((result['already_applied'], result['resume_required']), ([], False))
        self.assertEqual(result['evaluator']['conference_view'], {'files': 2, 'file_sets_equal': True, 'replaced': 1})
        self.assertEqual(result['catalogue']['files'], CATALOGUE)
        self.assertEqual(result['catalogue']['entry_diff'], {'distinct_verified_tasks': {'old': 0, 'new': 1}})
        self.assertEqual(result['archive_manifest']['changed_digests'],
                         sorted(self.replaced + self.added + ['experiments.json', 'index.html']))
        skipped = result['skipped_local_differences']
        self.assertEqual({r['path']: r['reason'] for r in skipped['names']},
                         {RUN + '/transcripts/index.html': 'differs_from_remote'})
        self.assertEqual(result['uploads'], 5 + 3)

    def test_commit_publishes_results_evaluations_and_conference_and_verifies(self):
        result = self.maintain(commit=True)
        self.assertTrue(result['committed'])
        self.assert_final_state()
        commits = self.hub.commits[self.publish_commits:]
        self.assertEqual(sorted(commits[-1]['paths']), sorted(CATALOGUE))  # catalogue and manifest last
        self.assertEqual({p for c in commits[:-1] for p in c['paths']}, set(self.replaced + self.added))
        self.assertTrue(all(c['message'].endswith('results and independent evaluator outputs') for c in commits))
        receipt = self.receipt()
        self.assertEqual((receipt['status'], receipt['scope']), ('verified', 'evaluator'))
        self.assertEqual(receipt['verification']['gitattributes'], 'unchanged')
        self.assertEqual(receipt['verification']['added_files'], 2)
        saved = sorted([*self.replaced, 'experiments.json', 'index.html', MANIFEST, '.gitattributes'])
        self.assertEqual(receipt['backups'], saved)
        for name in saved:
            self.assertEqual((self.records() / 'original' / name).read_bytes(), self.published[name], name)
        with self.assertRaisesRegex(ValueError, 'Nothing to upload'):
            self.maintain(commit=True, attempt='attempt-2')

    def test_conference_file_sets_must_match(self):
        extra = RUN + '/conference-view/file-2.html'
        self.edit_bundle({extra: b'<!doctype html><html><body>new</body></html>'})
        with self.assertRaisesRegex(ValueError, 'conference-view file sets.*file-2.html'):
            self.maintain()
        self.assert_no_maintenance_commit()

    def test_stale_published_evaluator_record_refused(self):
        self.edit_remote({RUN + '/evaluations/old.jsonl': b'{}\n'})
        with self.assertRaisesRegex(ValueError, 'evaluator record.*not in the bundle.*old.jsonl'):
            self.maintain()

    def test_lfs_rule_naming_a_planned_upload_refused_before_any_write(self):
        self.edit_remote({'.gitattributes': self.published['.gitattributes'] + b'*.jsonl' + RULE + b'\n'})
        with self.assertRaisesRegex(ValueError, 'LFS rule.*evaluations.jsonl'):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()

    def test_interrupted_attempt_resumes_against_original(self):
        with patch.object(maintain_hf_run, 'batches', one_file_batches):
            self.hub.fail_after = self.publish_commits + 1
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.maintain(commit=True)
            self.hub.fail_after = None
            fresh = self.maintain()
            self.assertTrue(fresh['resume_required'])
            with self.assertRaisesRegex(ValueError, 'already partly applied'):
                self.maintain(commit=True, attempt='attempt-2')
            with self.assertRaisesRegex(ValueError, 'belong to the evaluator scope, not the judge scope'):
                maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, resume_from=self.records())
            result = self.maintain(commit=True, attempt='attempt-3', resume_from=self.records())
        self.assertEqual((result['status'], result['origin_revision']), ('verified', self.base))
        self.assertEqual((result['replaced'], result['added']), (self.replaced, self.added))
        self.assert_final_state()

    def test_cli_scope(self):
        calls = []
        fake_hub = SimpleNamespace(HfApi=lambda: 'api', CommitOperationAdd=operation, hf_hub_download='download')
        def record(*args, **kwargs):
            calls.append(kwargs['scope'])
            return {}
        with patch.dict(sys.modules, {'huggingface_hub': fake_hub}), \
                patch.object(maintain_hf_run, 'maintain_run', record), patch('builtins.print'):
            main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--scope', 'evaluator'])
        self.assertEqual(calls, ['evaluator'])


class ResultsMaintenanceTests(unittest.TestCase):
    """--scope results: only the run's results.html/results.json and its catalogue entry change, even
    when the bundle also holds newer evaluator records, conference pages or transcripts."""
    prepare, records, receipt, edit_remote, assert_no_maintenance_commit = (
        MaintenanceTests.prepare, MaintenanceTests.records, MaintenanceTests.receipt, MaintenanceTests.edit_remote,
        MaintenanceTests.assert_no_maintenance_commit)

    def setUp(self):
        EvaluatorMaintenanceTests.setUp(self)
        self.replaced = [RUN + '/results.html', RUN + '/results.json']
        self.added = []

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.records(attempt))
            kwargs.setdefault('receipt', self.records(attempt) / 'receipt.json')
        return maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, commit=commit,
                            operation=operation, scope='results', **kwargs)

    def test_dry_run_plan_and_commit_replace_only_reports(self):
        plan = self.maintain()
        self.assertEqual((plan['scope'], plan['replaced'], plan['added'], plan['to_add']),
                         ('results', self.replaced, [], []))
        self.assertEqual(sorted(plan['to_replace']), sorted(self.replaced))
        self.assertEqual(plan['catalogue']['entry_diff'], {'distinct_verified_tasks': {'old': 0, 'new': 1}})
        self.assertEqual(plan['archive_manifest']['changed_digests'],
                         sorted(self.replaced + ['experiments.json', 'index.html']))
        skipped = {r['path'] for r in plan['skipped_local_differences']['names']}
        self.assertTrue({RUN + '/conference-view/index.html', RUN + '/transcripts/index.html',
                         RUN + '/evaluations/evaluations.jsonl'} <= skipped)
        self.assert_no_maintenance_commit()
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        self.assertEqual(set(final), set(self.published))
        for name, data in self.published.items():
            if name in self.replaced:
                self.assertEqual(final[name], (self.bundle / name).read_bytes(), name)
            elif name not in CATALOGUE:
                self.assertEqual(final[name], data, name)
        old, new = json.loads(self.published['experiments.json']), json.loads(final['experiments.json'])
        self.assertEqual([e for e in new['experiments'] if e['run'] != RUN],
                         [e for e in old['experiments'] if e['run'] != RUN])
        manifest = json.loads(final[MANIFEST])
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        commits = self.hub.commits[self.publish_commits:]
        self.assertTrue(all(c['message'].endswith('regenerated results report') for c in commits))
        self.assertEqual(self.receipt()['verification']['gitattributes'], 'unchanged')

    def test_lfs_rule_naming_a_report_refused_before_any_write(self):
        self.edit_remote({'.gitattributes': self.published['.gitattributes'] + b'*.html' + RULE + b'\n'})
        with self.assertRaisesRegex(ValueError, 'LFS rule.*results.html'):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()



class EvaluatorMaintenanceTests(unittest.TestCase):
    """--scope evaluator: reports, RUN/evaluations/ records, changed existing conference pages and the
    run's catalogue entry change; nothing else, no LFS and no deletions."""
    prepare, records, receipt, edit_bundle, edit_remote, restart, assert_no_maintenance_commit = (
        MaintenanceTests.prepare, MaintenanceTests.records, MaintenanceTests.receipt, MaintenanceTests.edit_bundle,
        MaintenanceTests.edit_remote, MaintenanceTests.restart, MaintenanceTests.assert_no_maintenance_commit)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        run = make_run(self.runs, RUN)
        (run / 'evaluations').mkdir()
        (run / 'evaluations/queue.jsonl').write_text('{"submission": "sub-1"}\n')
        (run / 'conference-view/file-1.html').write_text('<!doctype html><html><head></head><body>f</body></html>')
        self.hub = Hub(self.root)
        publish(self.prepare('published', RUN, OTHER), self.hub, self.hub.download, commit=True,
                operation=operation)
        # After publication: evaluator records, a rebuilt conference index with verdicts, a report with
        # one verified task, and a regenerated transcript (skipped by this scope).
        (run / 'evaluations/evaluations.jsonl').write_text('{"validity": "genuine"}\n')
        (run / 'evaluations/summary.json').write_text('{"by_validity": {"genuine": 1}}')
        (run / 'conference-view/index.html').write_text('<!doctype html><html><head></head><body>verdicts</body></html>')
        results = json.loads((run / 'results.json').read_text())
        results['verified_submissions'] = [{'submission': 's', 'agent': 'agent-001', 'task': 'T',
                                            'operational_outcome': 'accepted', 'independent_validity': 'genuine'}]
        (run / 'results.json').write_text(json.dumps(results))
        (run / 'results.html').write_text('<!doctype html><html><h1>R, evaluated</h1></html>')
        (run / 'transcripts/index.html').write_text('<!doctype html><html><body>newer exporter</body></html>')
        self.bundle = self.prepare('maintenance', RUN)
        self.published = dict(self.hub.files)
        self.base = self.hub.sha
        self.publish_commits = len(self.hub.commits)
        self.replaced = [RUN + '/results.html', RUN + '/results.json', RUN + '/conference-view/index.html']
        self.added = [RUN + '/evaluations/evaluations.jsonl', RUN + '/evaluations/summary.json']

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.records(attempt))
            kwargs.setdefault('receipt', self.records(attempt) / 'receipt.json')
        return maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, commit=commit,
                            operation=operation, scope='evaluator', **kwargs)

    def assert_final_state(self):
        final = self.hub.files
        self.assertEqual(set(final), set(self.published) | set(self.added))
        for name, data in self.published.items():
            if name not in set(self.replaced) | set(CATALOGUE):
                self.assertEqual(final[name], data, name)  # includes .gitattributes and the transcripts
        for name in self.replaced + self.added:
            self.assertEqual(final[name], (self.bundle / name).read_bytes(), name)
        old, new = json.loads(self.published['experiments.json']), json.loads(final['experiments.json'])
        self.assertEqual([e for e in new['experiments'] if e['run'] != RUN],
                         [e for e in old['experiments'] if e['run'] != RUN])
        self.assertEqual([e['distinct_verified_tasks'] for e in new['experiments'] if e['run'] == RUN], [1])
        manifest = json.loads(final[MANIFEST])
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        self.assertEqual(set(manifest['files']), set(json.loads(self.published[MANIFEST])['files']) | set(self.added))

    def test_dry_run_plan(self):
        result = self.maintain()
        self.assertEqual((result['scope'], result['committed']), ('evaluator', False))
        self.assert_no_maintenance_commit()
        self.assertFalse((self.root / 'records').exists())
        self.assertEqual((result['replaced'], result['added'], result['to_add']), (self.replaced, self.added, self.added))
        self.assertEqual(sorted(result['to_replace']), sorted(self.replaced))
        self.assertIn(RUN + '/evaluations/queue.jsonl', result['replacement_identical'])
        self.assertIn(RUN + '/conference-view/file-1.html', result['replacement_identical'])
        self.assertEqual((result['already_applied'], result['resume_required']), ([], False))
        self.assertEqual(result['evaluator']['conference_view'], {'files': 2, 'file_sets_equal': True, 'replaced': 1})
        self.assertEqual(result['catalogue']['files'], CATALOGUE)
        self.assertEqual(result['catalogue']['entry_diff'], {'distinct_verified_tasks': {'old': 0, 'new': 1}})
        self.assertEqual(result['archive_manifest']['changed_digests'],
                         sorted(self.replaced + self.added + ['experiments.json', 'index.html']))
        skipped = result['skipped_local_differences']
        self.assertEqual({r['path']: r['reason'] for r in skipped['names']},
                         {RUN + '/transcripts/index.html': 'differs_from_remote'})
        self.assertEqual(result['uploads'], 5 + 3)

    def test_commit_publishes_results_evaluations_and_conference_and_verifies(self):
        result = self.maintain(commit=True)
        self.assertTrue(result['committed'])
        self.assert_final_state()
        commits = self.hub.commits[self.publish_commits:]
        self.assertEqual(sorted(commits[-1]['paths']), sorted(CATALOGUE))  # catalogue and manifest last
        self.assertEqual({p for c in commits[:-1] for p in c['paths']}, set(self.replaced + self.added))
        self.assertTrue(all(c['message'].endswith('results and independent evaluator outputs') for c in commits))
        receipt = self.receipt()
        self.assertEqual((receipt['status'], receipt['scope']), ('verified', 'evaluator'))
        self.assertEqual(receipt['verification']['gitattributes'], 'unchanged')
        self.assertEqual(receipt['verification']['added_files'], 2)
        saved = sorted([*self.replaced, 'experiments.json', 'index.html', MANIFEST, '.gitattributes'])
        self.assertEqual(receipt['backups'], saved)
        for name in saved:
            self.assertEqual((self.records() / 'original' / name).read_bytes(), self.published[name], name)
        with self.assertRaisesRegex(ValueError, 'Nothing to upload'):
            self.maintain(commit=True, attempt='attempt-2')

    def test_conference_file_sets_must_match(self):
        extra = RUN + '/conference-view/file-2.html'
        self.edit_bundle({extra: b'<!doctype html><html><body>new</body></html>'})
        with self.assertRaisesRegex(ValueError, 'conference-view file sets.*file-2.html'):
            self.maintain()
        self.assert_no_maintenance_commit()

    def test_stale_published_evaluator_record_refused(self):
        self.edit_remote({RUN + '/evaluations/old.jsonl': b'{}\n'})
        with self.assertRaisesRegex(ValueError, 'evaluator record.*not in the bundle.*old.jsonl'):
            self.maintain()

    def test_lfs_rule_naming_a_planned_upload_refused_before_any_write(self):
        self.edit_remote({'.gitattributes': self.published['.gitattributes'] + b'*.jsonl' + RULE + b'\n'})
        with self.assertRaisesRegex(ValueError, 'LFS rule.*evaluations.jsonl'):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()

    def test_interrupted_attempt_resumes_against_original(self):
        with patch.object(maintain_hf_run, 'batches', one_file_batches):
            self.hub.fail_after = self.publish_commits + 1
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.maintain(commit=True)
            self.hub.fail_after = None
            fresh = self.maintain()
            self.assertTrue(fresh['resume_required'])
            with self.assertRaisesRegex(ValueError, 'already partly applied'):
                self.maintain(commit=True, attempt='attempt-2')
            with self.assertRaisesRegex(ValueError, 'belong to the evaluator scope, not the judge scope'):
                maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, resume_from=self.records())
            result = self.maintain(commit=True, attempt='attempt-3', resume_from=self.records())
        self.assertEqual((result['status'], result['origin_revision']), ('verified', self.base))
        self.assertEqual((result['replaced'], result['added']), (self.replaced, self.added))
        self.assert_final_state()

    def test_cli_scope(self):
        calls = []
        fake_hub = SimpleNamespace(HfApi=lambda: 'api', CommitOperationAdd=operation, hf_hub_download='download')
        def record(*args, **kwargs):
            calls.append(kwargs['scope'])
            return {}
        with patch.dict(sys.modules, {'huggingface_hub': fake_hub}), \
                patch.object(maintain_hf_run, 'maintain_run', record), patch('builtins.print'):
            main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--scope', 'evaluator'])
        self.assertEqual(calls, ['evaluator'])


def write_new_judge_build(run):
    """Another judge build of the same run (for example a re-run under a newer rubric): changed report,
    browser pages and records, one more agent page; labels, pipeline and run-level records unchanged."""
    judge = run / 'analysis' / 'judge'
    changes = {'report.html': '<!doctype html><html><head></head><body>In plain words</body></html>',
               'agents/agent-001.json': '{"plain_explanation": "x"}', 'packages/agent-001.json': '{"rubric": "3.1"}',
               'units/agent-001/verify-b01.json': '{"purpose": "verifier", "plain": true}',
               'review/index.html': '<!doctype html><html><head></head><body>R2</body></html>',
               'review/agents/agent-001.html': '<!doctype html><html><head></head><body>A2</body></html>',
               'review/agents/agent-002.html': '<!doctype html><html><head></head><body>B2</body></html>'}
    for name, text in changes.items():
        (judge / name).parent.mkdir(parents=True, exist_ok=True)
        (judge / name).write_text(text)

    def sha(name):
        return hashlib.sha256((judge / name).read_bytes()).hexdigest()
    inputs = {name: sha(name) for name in ('agents/agent-001.json', 'labels.jsonl', 'packages/agent-001.json',
                                           'pipeline.json', 'run_level.json', 'units/agent-001/verify-b01.json')}
    pages = {name: sha('review/' + name) for name in ('index.html', 'agents/agent-001.html', 'agents/agent-002.html')}
    (judge / 'review/manifest.json').write_text(json.dumps({
        'schema': 'gdm-judge-review-manifest/3', 'run_id': run.name, 'read_only': True, 'build': 2,
        'inputs_sha256': inputs, 'pages': pages}))
    (judge / 'report.json').write_text(json.dumps({
        'schema': 'gdm-judge-report/3', 'mode': 'paid', 'rubric': 'gdm-judge-rubric/3.1',
        'review_page': {'status': 'current', 'manifest_file_sha256': sha('review/manifest.json')}}))


class ReplaceJudgeMaintenanceTests(unittest.TestCase):
    """--replace-judge: the judge scope replaces an already-published judge build of the run with
    another one (published files that differ are replaced and backed up, new ones added); a published
    judge file the bundle lacks is still refused and nothing is deleted."""
    prepare, records, receipt, edit_remote, assert_no_maintenance_commit = (
        MaintenanceTests.prepare, MaintenanceTests.records, MaintenanceTests.receipt, MaintenanceTests.edit_remote,
        MaintenanceTests.assert_no_maintenance_commit)

    def setUp(self):
        MaintenanceTests.setUp(self)
        self.before_judge = (dict(self.published), self.base)
        # The first judge build is published with the judge scope (packages are stored as LFS).
        maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, commit=True, operation=operation,
                     backup_dir=self.records('first-judge'), receipt=self.records('first-judge') / 'receipt.json')
        self.published, self.base, self.publish_commits = dict(self.hub.files), self.hub.sha, len(self.hub.commits)
        self.old_judge = sorted(n for n in self.published if n.startswith(RUN + '/analysis/judge/'))
        write_new_judge_build(self.runs / RUN)
        self.bundle = self.prepare('replacement', RUN)
        files = json.loads((self.bundle / 'archive-manifest.json').read_text())['files']
        self.judge = sorted(n for n in files if n.startswith(RUN + '/analysis/judge/'))
        self.added = [RUN + '/analysis/judge/review/agents/agent-002.html']
        self.replaced = sorted(RUN + '/analysis/judge/' + n for n in (
            'agents/agent-001.json', 'packages/agent-001.json', 'report.html', 'report.json', 'review/index.html',
            'review/agents/agent-001.html', 'review/manifest.json'))
        self.identical = sorted(RUN + '/analysis/judge/' + n for n in ('labels.jsonl', 'pipeline.json', 'run_level.json'))

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.records(attempt))
            kwargs.setdefault('receipt', self.records(attempt) / 'receipt.json')
        kwargs.setdefault('replace_judge', True)
        return maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, commit=commit,
                            operation=operation, **kwargs)

    def assert_final_state(self):
        final = self.hub.files
        self.assertEqual(set(final), set(self.published) | set(self.added))
        for name, data in self.published.items():
            if name not in set(self.replaced) | {'archive-manifest.json'}:
                self.assertEqual(final[name], data, name)  # including the catalogue and .gitattributes
        for name in self.judge:
            self.assertEqual(final[name], (self.bundle / name).read_bytes(), name)
        self.assertIn(b'In plain words', final[RUN + '/analysis/judge/report.html'])
        manifest, old = json.loads(final['archive-manifest.json']), json.loads(self.published['archive-manifest.json'])
        self.assertEqual(set(manifest['files']), set(old['files']) | set(self.added))
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)

    def test_plain_judge_scope_still_refuses_and_replace_plan_is_read_only(self):
        with self.assertRaisesRegex(ValueError, 'Published judge file differs'):
            self.maintain(commit=True, replace_judge=False)
        plan = self.maintain()
        self.assert_no_maintenance_commit()
        self.assertFalse(self.records().exists())
        self.assertEqual((plan['scope'], plan['replace_judge'], plan['origin_revision']), ('judge', True, self.base))
        self.assertEqual((plan['replaced'], plan['added']), (self.replaced, self.added))
        self.assertEqual((sorted(plan['to_replace']), plan['to_add']), (self.replaced, self.added))
        self.assertEqual(plan['judge_already_present'], self.identical)
        self.assertEqual(plan['replacement_identical'], [RUN + '/results.html', RUN + '/results.json'])
        self.assertEqual((plan['already_applied'], plan['resume_required']), ([], False))
        self.assertEqual((plan['catalogue']['files'], plan['catalogue']['entry_diff']), (['archive-manifest.json'], {}))
        self.assertEqual(plan['archive_manifest']['changed_digests'], sorted(self.replaced + self.added))
        self.assertEqual(plan['uploads'], len(self.replaced) + len(self.added) + 1)

    def test_commit_replaces_judge_files_backs_up_originals_and_verifies(self):
        result = self.maintain(commit=True)
        self.assertEqual((result['status'], result['committed']), ('verified', True))
        self.assert_final_state()
        commits = self.hub.commits[self.publish_commits:]
        self.assertEqual(commits[-1]['paths'], ['archive-manifest.json'])  # manifest last
        self.assertTrue(all(c['message'].endswith('results and replaced behavior judge outputs') for c in commits))
        for name in self.replaced:
            self.assertEqual((self.records() / 'original' / name).read_bytes(), self.published[name], name)
        inventory = json.loads((self.records() / 'before-inventory.json').read_text())
        self.assertTrue(inventory['replace_judge'])
        verification = self.receipt()['verification']
        self.assertEqual((verification['added_files'], verification['gitattributes']), (1, 'unchanged'))
        self.assertEqual(verification['replaced_files'], len(self.replaced) + 1)  # and the manifest
        with self.assertRaisesRegex(ValueError, 'Nothing to upload'):
            self.maintain(commit=True, attempt='attempt-2')

    def test_published_judge_file_missing_from_bundle_still_refused(self):
        stale = RUN + '/analysis/judge/review/agents/agent-009.html'
        self.edit_remote({stale: b'<html>older build</html>'})
        with self.assertRaisesRegex(ValueError, r'1 published judge file\(s\) are not in the bundle.*' + stale):
            self.maintain(commit=True)
        self.assert_no_maintenance_commit()
        self.assertEqual(self.hub.files[stale], b'<html>older build</html>')  # never deleted

    def test_interrupted_attempt_resumes_against_original(self):
        self.hub.fail_after = self.publish_commits + 1  # the manifest batch is interrupted
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.maintain(commit=True)
        self.hub.fail_after = None
        plan = self.maintain()
        self.assertTrue(plan['resume_required'])
        self.assertEqual(plan['already_applied'], sorted(self.replaced + self.added))
        with self.assertRaisesRegex(ValueError, 'already partly applied'):
            self.maintain(commit=True, attempt='attempt-2')
        with self.assertRaisesRegex(ValueError, 'without --replace-judge|with --replace-judge'):
            self.maintain(resume_from=self.records(), replace_judge=False)
        result = self.maintain(commit=True, attempt='attempt-3', resume_from=self.records())
        self.assertEqual((result['status'], result['origin_revision'], result['committed_batches']),
                         ('verified', self.base, 1))
        self.assertEqual((result['replaced'], result['added']), (self.replaced, self.added))
        self.assert_final_state()

    def test_large_replacement_of_regular_judge_file_refused_before_any_write(self):
        with patch.object(maintain_hf_run, 'LFS_SAFE_BYTES', 20):
            with self.assertRaisesRegex(ValueError, 'newly store as LFS') as refusal:
                self.maintain(commit=True)
        self.assertIn('report.html', str(refusal.exception))
        self.assertNotIn('packages/agent-001.json', str(refusal.exception))  # already LFS: stays LFS
        self.assert_no_maintenance_commit()
        self.assertFalse((self.records() / 'before-inventory.json').exists())

    def test_requires_published_judge_files_and_the_judge_scope(self):
        self.hub.push(self.before_judge[0])
        with self.assertRaisesRegex(ValueError, 'No published judge files to replace'):
            self.maintain()
        with self.assertRaisesRegex(ValueError, 'only to the judge scope'):
            self.maintain(scope='transcripts')
        calls = []
        fake_hub = SimpleNamespace(HfApi=lambda: 'api', CommitOperationAdd=operation, hf_hub_download='download')
        with patch.dict(sys.modules, {'huggingface_hub': fake_hub}), patch('builtins.print'), \
                patch.object(maintain_hf_run, 'maintain_run', lambda *a, **k: calls.append(k['replace_judge']) or {}):
            main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--replace-judge'])
            main(['--maintain-run', RUN, '--bundle', str(self.bundle)])
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--scope', 'results', '--replace-judge'])
        self.assertEqual(calls, [True, False])


if __name__ == '__main__':
    unittest.main()


class InspectMaintenanceTests(unittest.TestCase):
    """--scope inspect: only RUN/inspect-view/index.html and the run's catalogue Inspect log links change.
    The published state is built as before the fix: an Inspect View without the redirect-following
    navigation and an 'Inspect logs' link to the unlistable RUN/inspect/ directory."""
    prepare, records, receipt, edit_remote, assert_no_maintenance_commit = (
        MaintenanceTests.prepare, MaintenanceTests.records, MaintenanceTests.receipt, MaintenanceTests.edit_remote,
        MaintenanceTests.assert_no_maintenance_commit)
    LOG = '2026-10-16T02-14-38-00-00_gdm-math-swarm_X.eval'

    @staticmethod
    def eval_log():
        import zipfile
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('header.json', '{}')
        return data.getvalue()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = self.root / 'viewer'
        (self.viewer / 'assets').mkdir(parents=True)
        (self.viewer / 'logs').mkdir()
        (self.viewer / 'index.html').write_text(
            '<!doctype html><html><head><script type="module" crossorigin src="./assets/index.js"></script>'
            '</head><body></body></html>')
        (self.viewer / 'assets' / 'index.js').write_text('export {}')
        (self.viewer / 'logs' / 'listing.json').write_text('{}')
        (self.viewer / 'logs' / self.LOG).write_bytes(self.eval_log())
        for run in (OTHER, RUN):
            make_run(self.runs, run)
            (self.runs / run / 'inspect').mkdir()
            (self.runs / run / 'inspect' / self.LOG).write_bytes(self.eval_log())
        self.hub = Hub(self.root)
        import build_experiment_index
        import space_navigation
        old_links = lambda run, names: {'Inspect logs': run + '/inspect/'}  # noqa: E731
        with patch.object(space_navigation, 'FOLLOW_LOG_REDIRECTS', ''), \
                patch.object(build_experiment_index, 'inspect_log_links', old_links):
            publish(self.prepare('published', RUN, OTHER), self.hub, self.hub.download, commit=True,
                    operation=operation)
        # Since publication the local report changed; the inspect scope must not publish it.
        results = json.loads((self.runs / RUN / 'results.json').read_text())
        (self.runs / RUN / 'results.json').write_text(json.dumps({**results, 'elapsed_seconds': 1.0}))
        self.bundle = self.prepare('inspect', RUN)
        self.published = dict(self.hub.files)
        self.base = self.hub.sha
        self.publish_commits = len(self.hub.commits)
        self.page = RUN + '/inspect-view/index.html'

    def maintain(self, commit=False, attempt='attempt-1', bundle=None, **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.records(attempt))
            kwargs.setdefault('receipt', self.records(attempt) / 'receipt.json')
        return maintain_run(bundle or self.bundle, self.hub, self.hub.download, run=RUN, commit=commit,
                            operation=operation, scope='inspect', **kwargs)

    def test_published_state_reproduces_the_broken_links(self):
        self.assertNotIn(b"redirect:'follow'", self.published[self.page])
        self.assertIn(b"redirect:'follow'", (self.bundle / self.page).read_bytes())
        entry = next(x for x in json.loads(self.published['experiments.json'])['experiments'] if x['run'] == RUN)
        self.assertEqual(entry['links']['Inspect logs'], RUN + '/inspect/')

    def test_dry_run_and_commit_replace_only_the_viewer_page_and_log_links(self):
        plan = self.maintain()
        self.assertEqual((plan['scope'], plan['replaced'], plan['to_replace'], plan['added']),
                         ('inspect', [self.page], [self.page], []))
        self.assertEqual(plan['catalogue']['entry_diff'], {'links': {
            'added': {}, 'removed': {}, 'changed': {'Inspect logs': {
                'old': RUN + '/inspect/', 'new': RUN + '/inspect/' + self.LOG}}}})
        skipped = {r['path'] for r in plan['skipped_local_differences']['names']}
        self.assertIn(RUN + '/results.json', skipped)
        self.assert_no_maintenance_commit()
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        self.assertEqual(set(final), set(self.published))
        for name, data in self.published.items():
            if name == self.page:
                self.assertEqual(final[name], (self.bundle / name).read_bytes())
            elif name not in CATALOGUE:
                self.assertEqual(final[name], data, name)
        old, new = (json.loads(x['experiments.json'])['experiments'] for x in (self.published, final))
        self.assertEqual([e for e in new if e['run'] != RUN], [e for e in old if e['run'] != RUN])
        old_entry, new_entry = (next(e for e in x if e['run'] == RUN) for x in (old, new))
        self.assertEqual(list(new_entry['links']), list(old_entry['links']))
        self.assertEqual({**new_entry, 'links': {}}, {**old_entry, 'links': {}})  # not the bundle's entry
        self.assertIn(f'href="{RUN}/inspect/{self.LOG}">Inspect logs</a>', final['index.html'].decode())
        self.assertNotIn(f'href="{RUN}/inspect/">', final['index.html'].decode())
        manifest = json.loads(final[MANIFEST])
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        commits = self.hub.commits[self.publish_commits:]
        self.assertTrue(all(c['message'].endswith('Inspect log links') for c in commits))
        self.assertEqual(self.receipt()['verification']['gitattributes'], 'unchanged')

    def test_page_without_redirect_fix_refused(self):
        import space_navigation
        with patch.object(space_navigation, 'FOLLOW_LOG_REDIRECTS', ''):
            old = self.prepare('without-fix', RUN)
        with self.assertRaisesRegex(ValueError, 'redirect-following'):
            self.maintain(bundle=old)

    def test_other_viewer_refused(self):
        self.edit_remote({RUN + '/inspect-view/assets/index.js': b'export const other = 1'})
        with self.assertRaisesRegex(ValueError, 'Inspect View of the bundle.*differing'):
            self.maintain()
        self.assertEqual(len(self.hub.commits), self.publish_commits)

    def test_run_without_published_log_refused(self):
        files = {k: v for k, v in self.published.items() if not k.startswith(RUN + '/inspect/')}
        self.hub.push(files)
        with self.assertRaisesRegex(ValueError, 'no published Inspect log'):
            self.maintain()


class InspectLogLinkTests(unittest.TestCase):
    def test_links_point_at_files_not_the_directory(self):
        from build_experiment_index import inspect_log_links
        self.assertEqual(inspect_log_links('r', ['a.eval', 'listing.json']), {'Inspect logs': 'r/inspect/a.eval'})
        self.assertEqual(inspect_log_links('r', ['b.eval', 'a.eval']),
                         {'Inspect log 1': 'r/inspect/a.eval', 'Inspect log 2': 'r/inspect/b.eval'})
        self.assertEqual(inspect_log_links('r', []), {})
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(tmp, 'run-a')
            (run / 'inspect').mkdir()
            (run / 'inspect' / 'x.eval').write_bytes(b'PK')
            import build_experiment_index
            build_experiment_index.build(tmp)
            entry = json.loads((Path(tmp) / 'experiments.json').read_text())['experiments'][0]
            self.assertEqual(entry['links']['Inspect logs'], 'run-a/inspect/x.eval')


class SiteMaintenanceTests(unittest.TestCase):
    """--scope site: only generated pages that differ (results.html, existing conference-view pages, existing
    judge HTML pages) are replaced; report data, judge data, the catalogue entry and every other file stay."""
    prepare, records, receipt, edit_bundle, edit_remote, assert_no_maintenance_commit = (
        MaintenanceTests.prepare, MaintenanceTests.records, MaintenanceTests.receipt, MaintenanceTests.edit_bundle,
        MaintenanceTests.edit_remote, MaintenanceTests.assert_no_maintenance_commit)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        run = make_run(self.runs, RUN, judge={})
        (run / 'conference-view/file-1.html').write_text('<!doctype html><html><head></head><body>f</body></html>')
        self.hub = Hub(self.root)
        publish(self.prepare('published', RUN, OTHER), self.hub, self.hub.download, commit=True,
                operation=operation)
        # Regenerated by newer generators: report page, conference index and judge report page (data unchanged).
        (run / 'results.html').write_text('<!doctype html><html><head></head><body>summary card</body></html>')
        (run / 'conference-view/index.html').write_text('<!doctype html><html><head></head><body>no tx</body></html>')
        (run / 'analysis/judge/report.html').write_text('<!doctype html><html><head></head><body>J</body></html>\n')
        self.bundle = self.prepare_site()
        self.published = dict(self.hub.files)
        self.base = self.hub.sha
        self.publish_commits = len(self.hub.commits)
        self.replaced = [RUN + '/results.html', RUN + '/conference-view/index.html',
                         RUN + '/analysis/judge/report.html']

    def prepare_site(self, name='site'):  # a current bundle: no transcript export
        output = self.root / 'bundles' / name
        argv = ['prepare_hf_library.py', '--runs-dir', str(self.runs), '--output-dir', str(output),
                '--run', f'{RUN}={self.viewer}']
        with patch.object(sys, 'argv', argv), patch('builtins.print'):
            prepare_hf_library.main()
        return output

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        if commit:
            kwargs.setdefault('backup_dir', self.records(attempt))
            kwargs.setdefault('receipt', self.records(attempt) / 'receipt.json')
        return maintain_run(self.bundle, self.hub, self.hub.download, run=RUN, commit=commit,
                            operation=operation, scope='site', **kwargs)

    def test_dry_run_and_commit_replace_only_generated_pages(self):
        plan = self.maintain()
        self.assertEqual((plan['scope'], plan['added'], plan['to_add']), ('site', [], []))
        self.assertEqual(sorted(plan['replaced']), sorted(self.replaced))
        self.assertIn(RUN + '/results.json', plan['replacement_identical'])
        self.assertIn(RUN + '/conference-view/file-1.html', plan['replacement_identical'])
        self.assertEqual(plan['catalogue']['entry_diff'], {})
        self.assertEqual(plan['catalogue']['files'], [MANIFEST])  # index.html and experiments.json unchanged
        self.assertEqual(plan['evaluator']['judge_pages']['replaced'], 1)
        self.assertTrue(plan['evaluator']['results_json_unchanged'])
        self.assertEqual(plan['remote_only_preserved']['count'], 1)  # the published transcript page
        self.assert_no_maintenance_commit()
        result = self.maintain(commit=True)
        self.assertEqual(result['status'], 'verified')
        final = self.hub.files
        self.assertEqual(set(final), set(self.published))
        for name, data in self.published.items():
            if name in self.replaced:
                self.assertEqual(final[name], (self.bundle / name).read_bytes(), name)
            elif name != MANIFEST:
                self.assertEqual(final[name], data, name)  # catalogue, transcripts, judge data, .gitattributes
        manifest = json.loads(final[MANIFEST])
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        commits = self.hub.commits[self.publish_commits:]
        self.assertTrue(all(c['message'].endswith('regenerated site pages (presentation only)') for c in commits))
        self.assertEqual(commits[-1]['paths'], [MANIFEST])
        with self.assertRaisesRegex(ValueError, 'Nothing to upload'):
            self.maintain(commit=True, attempt='attempt-2')

    def test_changed_report_data_refused(self):
        results = json.loads((self.bundle / RUN / 'results.json').read_text())
        self.edit_bundle({RUN + '/results.json': json.dumps({**results, 'elapsed_seconds': 1.0}).encode()})
        with self.assertRaisesRegex(ValueError, 'presentation only'):
            self.maintain()

    def test_other_judge_build_refused(self):
        self.edit_bundle({RUN + '/analysis/judge/labels.jsonl': b'{"label": "other"}\n'})
        with self.assertRaisesRegex(ValueError, 'judge build.*labels.jsonl'):
            self.maintain()
        self.edit_bundle({RUN + '/analysis/judge/labels.jsonl': self.published[RUN + '/analysis/judge/labels.jsonl'],
                          RUN + '/analysis/judge/review/agents/agent-009.html': self.published[RUN + '/results.html']})
        with self.assertRaisesRegex(ValueError, 'page sets differ.*agent-009'):
            self.maintain()

    def test_page_without_site_theme_refused(self):
        self.edit_bundle({RUN + '/conference-view/index.html': b'<!doctype html><html><body>old</body></html>'})
        with self.assertRaisesRegex(ValueError, 'lacks the shared site theme'):
            self.maintain()
        self.assert_no_maintenance_commit()

    def test_cli_scope(self):
        calls = []
        fake_hub = SimpleNamespace(HfApi=lambda: 'api', CommitOperationAdd=operation, hf_hub_download='download')

        def record(*args, **kwargs):
            calls.append(kwargs['scope'])
            return {}
        with patch.dict(sys.modules, {'huggingface_hub': fake_hub}), \
                patch.object(maintain_hf_run, 'maintain_run', record), patch('builtins.print'):
            main(['--maintain-run', RUN, '--bundle', str(self.bundle), '--scope', 'site'])
        self.assertEqual(calls, ['site'])
