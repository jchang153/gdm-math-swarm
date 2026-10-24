"""Offline tests of the simplified behavior judge on the static site (scripts/simple_judge_site.py): where its
files go (Space, private dataset, local only), the run-page Behavior judge tab, the catalogue link, the publish
screens and maintain_hf_run.py --scope simple-judge (in-memory Space and dataset fakes)."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_experiment_index  # noqa: E402
import build_results  # noqa: E402
import dataset_logs  # noqa: E402
import maintain_hf_run  # noqa: E402
from maintain_hf_run import maintain_simple_judge  # noqa: E402
from publish_hf_library import digest, load_bundle, publish  # noqa: E402
from research.analysis.simple_judge.pipeline import write_outputs  # noqa: E402
import simple_judge_site as site  # noqa: E402
from test_hf_maintenance import RULE, Hub, blob, operation  # noqa: E402
from test_incident_site import PAGE, prepare  # noqa: E402
from test_judge_publication import make_run, make_viewer, write_judge  # noqa: E402

RUN, OTHER = 'sj-run', 'a-old'
LOCAL_ONLY = ('cases.jsonl', 'raw/agent-001-abc/request.json', 'raw/agent-001-abc/response.json',
              'run-budgets.json', 'adjudication.json', 'rehearsal-ledger.sqlite')
SJ = 'analysis/simple_judge/'
JUDGED_PAGE = PAGE.replace('<span class="off">Behavior judge · not run</span>',
                           '<a href="analysis/judge/report.html">Behavior judge</a>').replace(
    '<li><strong>Inspect</strong>', '<li><strong>Behavior judge</strong>: old judge text.</li>'
                                    '<li><strong>Inspect</strong>')


def row(agent, level='credited', aware=True):
    return {'agent_id': agent, 'model': 'google/gemini-3.8-flash', 'fallback_used': False, 'status': 'ok',
            'reason': None, 'calls': [{'model': 'm', 'status': 'ok', 'cost_usd': '0.02'}],
            'facts': {'flagged_submissions': 1, 'flagged_accepted': 1},
            'labels': {'cheating_level': level, 'technique': 'Iff.rfl', 'declined_after_considering': False,
                       'whistleblower': False, 'whistleblower_channel': None, 'evaluation_aware': aware,
                       'likely_source': 'independent', 'rationale': 'Defined default as the target.',
                       'evidence': [{'record': 'A01.t3.r', 'quote': 'just use Iff.rfl', 'verified': True},
                                    {'record': 'A01.t4.r', 'quote': 'made up', 'verified': False,
                                     'reason': 'not found'}]},
            'checks': {'quotes': 2, 'quotes_verified': 1, 'downgrades': [], 'flags': []}}


def write_simple_judge(run, mode='paid', run_id=None, local=True):
    folder = Path(run) / 'analysis' / 'simple_judge'
    write_outputs(folder, run_id or Path(run).name, [row('agent-001'), row('agent-002', 'none', False)],
                  meta={'mode': mode, 'model': 'google/gemini-3.8-flash', 'fallback_model': 'openai/gpt-5.6-terra',
                        'wall_seconds': 90})
    if local:
        for name in LOCAL_ONLY:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text('{"full": "case text"}\n')
        (folder / '.simple-judge.lock').write_text('')
    return folder


class HelperTests(unittest.TestCase):
    def test_allowlist_and_dataset_routing(self):
        for name in ('report.html', 'report.json', 'agents.jsonl'):
            self.assertFalse(site.simple_judge_excluded(SJ + name), name)
        for name in LOCAL_ONLY:
            self.assertTrue(site.simple_judge_excluded(SJ + name), name)
        self.assertTrue(site.simple_judge_excluded(SJ + 'raw/report.html'))
        self.assertFalse(site.simple_judge_excluded('analysis/judge/cases.jsonl'))
        self.assertEqual(dataset_logs.category(SJ + 'agents.jsonl'), 'jsonl')
        for name in ('cases.jsonl', 'report.html', 'report.json', 'raw/a/request.jsonl'):
            self.assertIsNone(dataset_logs.category(SJ + name), name)

    def test_tab_replaces_not_run_placeholder(self):
        page = site.add_simple_judge_tab(PAGE)
        self.assertIn('<a href="conference-view/index.html">Conference</a>' + site.TAB +
                      '<a href="inspect-view/', page)
        self.assertNotIn('Behavior judge · not run', page)
        self.assertNotIn(site.OLD_TAB, page)
        self.assertIn(site.HELP, page)
        self.assertLess(page.index(site.HELP), page.index('<li><strong>Inspect</strong>'))
        self.assertIn('not comparable', page)
        self.assertEqual(site.add_simple_judge_tab(page), page)
        with self.assertRaisesRegex(ValueError, 'tab bar'):
            site.add_simple_judge_tab('<html><h1>old page</h1></html>')

    def test_tab_keeps_rubric_3_1_judge_reachable(self):
        page = site.add_simple_judge_tab(JUDGED_PAGE)
        self.assertIn(site.TAB + site.OLD_TAB + '<a href="inspect-view/', page)
        self.assertIn(site.HELP + site.OLD_HELP + '<li><strong>Inspect</strong>', page)
        self.assertNotIn('old judge text', page)

    def test_summary_tile_names_the_simplified_judge(self):
        tile = ('<div class="tile"><div class="k">Behavior judge</div><div class="v">not run</div>'
                '<div class="s">no judge outputs</div></div>')
        page = site.add_simple_judge_tab(PAGE.replace('<nav', tile + '<nav'))
        self.assertIn(site.TILE, page)
        self.assertNotIn('no judge outputs', page)

    def test_catalogue_link_after_behavior_judge(self):
        links = {'Report': 'r/results.html', 'Behavior judge': 'r/analysis/judge/report.html',
                 'Incident report': 'r/analysis/incident/report.html', 'Inspect logs': 'r/inspect/a.eval'}
        once = site.with_simple_judge_link(links, 'r')
        self.assertEqual(list(once), ['Report', 'Behavior judge', site.CATALOGUE_LABEL, 'Incident report',
                                      'Inspect logs'])
        self.assertEqual(once[site.CATALOGUE_LABEL], 'r/analysis/simple_judge/report.html')
        self.assertEqual(site.with_simple_judge_link(once, 'r'), once)
        self.assertEqual(list(site.with_simple_judge_link({'Report': 'x', 'Inspect log 1': 'y'}, 'r')),
                         ['Report', site.CATALOGUE_LABEL, 'Inspect log 1'])

    def test_site_page_has_counts_labels_quotes_and_the_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = write_simple_judge(Path(tmp) / RUN, local=False)
            data = {n: (folder / n).read_bytes() for n in ('report.json', 'agents.jsonl')}
            page = site.site_page(data['report.json'], data['agents.jsonl'], RUN)
            linked = site.site_page(data['report.json'], data['agents.jsonl'], RUN, old_judge=True)
            for text in ('id=sj-comparability', 'not comparable', 'evaluation-aware', 'credited',
                         'just use Iff.rfl', '&#10003;', '&#10007;', 'data-theme="dark"'):
                self.assertIn(text, page)
            self.assertNotIn('<script', page)
            self.assertNotIn('../judge/report.html', page)
            self.assertIn('href="../judge/report.html"', linked)
            with self.assertRaisesRegex(ValueError, 'belongs to run'):
                site.site_page(data['report.json'], data['agents.jsonl'], 'other-run')
            write_simple_judge(Path(tmp) / RUN, mode='mock', local=False)
            with self.assertRaisesRegex(ValueError, 'mode'):
                site.site_page((folder / 'report.json').read_bytes(), data['agents.jsonl'], RUN)

    def test_incomplete_judge_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / RUN / 'analysis' / 'simple_judge'
            (folder / 'raw').mkdir(parents=True)
            (folder / 'cases.jsonl').write_text('{}\n')
            self.assertEqual(site.simple_judge_files(Path(tmp) / RUN), [])


class BuildResultsTabTests(unittest.TestCase):
    def test_build_and_rerender_add_the_tab(self):
        from test_build_results import generate, make_run as make_full_run
        with tempfile.TemporaryDirectory() as tmp:
            run = make_full_run(tmp)
            write_simple_judge(run, local=False)
            _, page = generate(run)
            self.assertIn(site.TAB, page)
            self.assertNotIn(site.OLD_TAB, page)
            with contextlib.redirect_stdout(io.StringIO()):
                build_results.main(['--run-dir', str(run), '--rerender-html', '--overwrite'])
            self.assertEqual((run / 'results.html').read_text().count(site.TAB_MARKER), 2)  # tab and its help line

    def test_run_with_both_judges_shows_both_tabs(self):
        from test_build_results import generate, make_run as make_full_run
        with tempfile.TemporaryDirectory() as tmp:
            run = make_full_run(tmp, judge=True)
            write_simple_judge(run, local=False)
            _, page = generate(run)
            self.assertIn(site.TAB + site.OLD_TAB, page)


class PrepareAndPublishTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        make_run(self.runs, RUN, judge={})
        (self.runs / RUN / 'results.html').write_text(JUDGED_PAGE)
        write_simple_judge(self.runs / RUN)
        unlisted = patch.object(build_experiment_index, 'unlisted_runs', return_value={})
        unlisted.start()
        self.addCleanup(unlisted.stop)
        self.bundle = prepare(self.runs, self.root / 'bundle', RUN, OTHER, viewer=self.viewer)

    def test_space_gets_report_only_dataset_gets_agents_rest_stays_local(self):
        manifest, entries = load_bundle(self.bundle)
        space = sorted(n for n in manifest['files'] if '/analysis/simple_judge/' in n)
        self.assertEqual(space, [f'{RUN}/{SJ}report.html', f'{RUN}/{SJ}report.json'])
        self.assertEqual(sorted(n for n in manifest['dataset'] if '/simple_judge/' in n), [f'{RUN}/{SJ}agents.jsonl'])
        everything = [p.relative_to(self.bundle).as_posix() for p in self.bundle.rglob('*') if p.is_file()]
        for name in (*LOCAL_ONLY, '.simple-judge.lock'):
            self.assertFalse([n for n in everything if n.endswith(SJ + name)], name)
        self.assertEqual(manifest['simple_judge'], {RUN: {
            'space': [SJ + 'report.html', SJ + 'report.json'], 'dataset': [SJ + 'agents.jsonl'],
            'excluded': len(LOCAL_ONLY) + 1}})  # and the lock file
        page = (self.bundle / RUN / 'results.html').read_text()
        self.assertIn(site.TAB + site.OLD_TAB, page)
        self.assertNotIn(site.TAB_MARKER, (self.bundle / OTHER / 'results.html').read_text())
        report = (self.bundle / RUN / SJ / 'report.html').read_text()
        self.assertIn('id="site-theme"', report)
        self.assertIn('id=sj-comparability', report)
        self.assertIn('href="../judge/report.html"', report)
        links = {e['run']: e['links'] for e in entries}
        self.assertEqual(links[RUN][site.CATALOGUE_LABEL], f'{RUN}/{SJ}report.html')
        self.assertNotIn(site.CATALOGUE_LABEL, links[OTHER])

    def add_space_file(self, name, data=b'{"full": "case text"}\n'):
        path = self.bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest = json.loads((self.bundle / 'archive-manifest.json').read_text())
        manifest['files'][name] = digest(data)
        (self.bundle / 'archive-manifest.json').write_text(json.dumps(manifest))

    def test_publish_screen_refuses_cases_in_the_space(self):
        self.add_space_file(f'{RUN}/{SJ}cases.jsonl')
        with self.assertRaisesRegex(ValueError, 'not allowed in the Space'):
            load_bundle(self.bundle)

    def test_publish_screen_refuses_raw_in_the_space(self):
        self.add_space_file(f'{RUN}/{SJ}raw/agent-001-abc/request.json')
        with self.assertRaisesRegex(ValueError, 'not allowed in the Space'):
            load_bundle(self.bundle)

    def test_publish_screen_refuses_agents_in_the_space(self):
        self.add_space_file(f'{RUN}/{SJ}agents.jsonl', b'{}\n')
        with self.assertRaisesRegex(ValueError, 'not allowed in the Space'):
            load_bundle(self.bundle)

    def test_dataset_screen_refuses_cases(self):
        root = self.bundle / 'dataset-files'
        name = f'{RUN}/{SJ}cases.jsonl'
        (root / name).write_bytes(b'{}\n')
        manifest = json.loads((root / 'manifest.json').read_text())
        manifest['files'][name] = {**digest(b'{}\n'), 'category': 'jsonl', 'run': RUN, 'sources': [SJ + 'cases.jsonl']}
        (root / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'outside the selected runs or categories'):
            load_bundle(self.bundle)


class DualHub(Hub):
    """The maintenance Space fake plus a private logs dataset (repo_type='dataset')."""

    def __init__(self, root):
        super().__init__(root)
        self.dataset, self.dataset_revision, self.dataset_commits = {}, 0, []

    @property
    def dataset_sha(self):
        return f'{self.dataset_revision:040x}'

    def repo_info(self, repo, *, repo_type, files_metadata=False):
        if repo_type == 'dataset':
            return SimpleNamespace(private=True, sha=self.dataset_sha)
        return super().repo_info(repo, repo_type=repo_type, files_metadata=files_metadata)

    def list_repo_tree(self, repo, *, repo_type, revision, recursive):
        if repo_type == 'dataset':
            assert revision == self.dataset_sha
            return iter([SimpleNamespace(path=n, size=len(d), lfs=None, blob_id=blob(d))
                         for n, d in sorted(self.dataset.items())])
        return super().list_repo_tree(repo, repo_type=repo_type, revision=revision, recursive=recursive)

    def get_paths_info(self, repo, paths, *, repo_type, revision):
        assert repo_type == 'dataset' and revision == self.dataset_sha
        return [SimpleNamespace(path=p, size=len(self.dataset[p]), lfs=None, blob_id=blob(self.dataset[p]))
                for p in paths if p in self.dataset]

    def create_commit(self, *, repo_id, repo_type, parent_commit, commit_message, operations):
        if repo_type == 'dataset':
            assert parent_commit == self.dataset_sha
            for op in operations:
                data = op.path_or_fileobj
                self.dataset[op.path_in_repo] = Path(data).read_bytes() if isinstance(data, (str, Path)) else data
            self.dataset_revision += 1
            self.dataset_commits.append(commit_message)
            return SimpleNamespace(oid=self.dataset_sha)
        return super().create_commit(repo_id=repo_id, repo_type=repo_type, parent_commit=parent_commit,
                                     commit_message=commit_message, operations=operations)


class SimpleJudgeScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / 'runs'
        self.viewer = make_viewer(self.root)
        make_run(self.runs, OTHER)
        make_run(self.runs, RUN)
        write_judge(self.runs / RUN)
        (self.runs / RUN / 'results.html').write_text(JUDGED_PAGE)
        self.hub = DualHub(self.root)
        for module in (maintain_hf_run, build_experiment_index):
            unlisted = patch.object(module, 'unlisted_runs', return_value={})
            unlisted.start()
            self.addCleanup(unlisted.stop)
        publish(prepare(self.runs, self.root / 'published', RUN, OTHER, viewer=self.viewer),
                self.hub, self.hub.download, commit=True, operation=operation)
        # After publication: the simplified judge ran; the local run page drifted (it must not be published).
        write_simple_judge(self.runs / RUN)
        (self.runs / RUN / 'results.html').write_text(JUDGED_PAGE.replace('facts', 'regenerated facts'))
        self.published, self.base, self.commits = dict(self.hub.files), self.hub.sha, len(self.hub.commits)
        self.dataset_before = dict(self.hub.dataset)  # the rubric-3.1 judge's labels.jsonl from the publication
        self.space = [f'{RUN}/{SJ}report.html', f'{RUN}/{SJ}report.json']
        self.read = []

    def reader(self, path):
        self.read.append(Path(path))
        return Path(path).read_bytes()

    def maintain(self, commit=False, attempt='attempt-1', **kwargs):
        records = self.root / 'records' / attempt
        if commit:
            kwargs.setdefault('backup_dir', records)
            kwargs.setdefault('receipt', records / 'receipt.json')
        return maintain_simple_judge(self.hub, self.hub.download, run=RUN, runs_dir=self.runs, commit=commit,
                                     operation=operation, reader=self.reader, **kwargs)

    def test_preview_is_read_only_and_reads_only_the_judge_files(self):
        result = self.maintain()
        self.assertEqual((result['committed'], self.hub.sha, len(self.hub.commits), self.hub.dataset),
                         (False, self.base, self.commits, self.dataset_before))
        self.assertEqual(result['to_add'], self.space)
        self.assertEqual(result['to_replace'], sorted([f'{RUN}/results.html', 'archive-manifest.json',
                                                       'experiments.json', 'index.html']))
        self.assertEqual(result['dataset']['path'], f'{RUN}/{SJ}agents.jsonl')
        self.assertEqual(result['dataset']['state'], 'to add')
        self.assertTrue(result['rubric_3_1_tab'])
        self.assertEqual(result['catalogue']['entry_diff'], {'links': {
            'added': {site.CATALOGUE_LABEL: f'{RUN}/{SJ}report.html'}, 'removed': {}, 'changed': {}}})
        self.assertEqual(result['local_only']['files'], len(LOCAL_ONLY) + 1)  # and the lock file
        self.assertEqual(sorted(p.name for p in self.read), ['agents.jsonl', 'report.json'])
        self.assertTrue(all(p.parent == self.runs / RUN / 'analysis' / 'simple_judge' for p in self.read))
        self.assertFalse((self.root / 'records').exists())

    def test_commit_adds_exactly_the_planned_files(self):
        result = self.maintain(commit=True)
        dataset_commits = len(self.hub.dataset_commits)
        self.assertEqual((result['status'], len(self.hub.commits)), ('verified', self.commits + 1))
        self.assertEqual(set(self.hub.dataset) - set(self.dataset_before), {f'{RUN}/{SJ}agents.jsonl'})
        self.assertEqual(self.hub.dataset[f'{RUN}/{SJ}agents.jsonl'],
                         (self.runs / RUN / SJ / 'agents.jsonl').read_bytes())
        final = self.hub.files
        self.assertEqual(set(final), set(self.published) | set(self.space))
        changed = {n for n in self.published if final[n] != self.published[n]}
        self.assertEqual(changed, {f'{RUN}/results.html', 'index.html', 'experiments.json', 'archive-manifest.json'})
        self.assertFalse([n for n in final if '/simple_judge/' in n and n not in self.space])
        page = final[f'{RUN}/results.html'].decode()
        self.assertEqual(page, site.add_simple_judge_tab(self.published[f'{RUN}/results.html'].decode()))
        self.assertIn(site.TAB + site.OLD_TAB, page)
        self.assertNotIn('regenerated facts', page)
        report = final[f'{RUN}/{SJ}report.html'].decode()
        self.assertIn('id="site-theme"', report)
        self.assertIn('id=sj-comparability', report)
        self.assertIn('href="../judge/report.html"', report)
        manifest = json.loads(final['archive-manifest.json'])
        for name, info in manifest['files'].items():
            self.assertEqual(digest(final[name]), info, name)
        self.assertEqual(manifest['dataset_files'][f'{RUN}/{SJ}agents.jsonl']['path'], f'{RUN}/{SJ}agents.jsonl')
        self.assertEqual(manifest['simple_judge'][RUN]['dataset'], [SJ + 'agents.jsonl'])
        entry = next(e for e in json.loads(final['experiments.json'])['experiments'] if e['run'] == RUN)
        self.assertEqual(entry['links'][site.CATALOGUE_LABEL], f'{RUN}/{SJ}report.html')
        self.assertEqual(final['.gitattributes'], self.published['.gitattributes'])
        receipt = json.loads((self.root / 'records/attempt-1/receipt.json').read_text())
        self.assertEqual(receipt['status'], 'verified')
        again = self.maintain(commit=True, attempt='attempt-2')
        self.assertEqual((again['already_applied'], again['uploads'], len(self.hub.commits),
                          len(self.hub.dataset_commits)), (True, 0, self.commits + 1, dataset_commits))

    def test_refusals(self):
        self.hub.push({**self.published, f'{RUN}/{SJ}cases.jsonl': b'{}'})
        with self.assertRaisesRegex(ValueError, 'not allowed there'):
            self.maintain()
        self.hub.push({**self.published, f'{RUN}/{SJ}raw/a/request.json': b'{}'})
        with self.assertRaisesRegex(ValueError, 'not allowed there'):
            self.maintain()
        self.hub.push({**self.published, '.gitattributes': self.published['.gitattributes']
                       + f'{RUN}/{SJ}*'.encode() + RULE + b'\n'})
        with self.assertRaisesRegex(ValueError, 'LFS'):
            self.maintain()
        self.hub.push(self.published)
        self.hub.dataset[f'{RUN}/{SJ}agents.jsonl'] = b'{"older": true}\n'
        with self.assertRaisesRegex(ValueError, 'different bytes'):
            self.maintain()
        self.hub.dataset = dict(self.dataset_before)
        self.hub.concurrent = True
        with self.assertRaisesRegex(ValueError, 'changed concurrently'):
            self.maintain(commit=True)
        self.hub.concurrent = False
        self.hub.push({k: v for k, v in self.published.items() if not k.startswith(RUN + '/')})
        with self.assertRaisesRegex(ValueError, 'not published'):
            self.maintain()

    def test_run_without_a_completed_judge_is_refused(self):
        for name in ('report.json', 'agents.jsonl'):
            (self.runs / RUN / SJ / name).unlink()
        with self.assertRaisesRegex(ValueError, 'no completed simplified judge'):
            self.maintain()

    def test_cli_arguments(self):
        for extra in (['--bundle', str(self.root)], ['--resume-from', str(self.root)], ['--commit']):
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                maintain_hf_run.main(['--maintain-run', RUN, '--scope', 'simple-judge', *extra])




class SpaceReportJsonTest(unittest.TestCase):
    def test_host_paths_are_reduced_for_the_public_space(self):
        import json as _json
        import simple_judge_site
        report = {"run_id": "r", "budget": {"prior": {"judge_dir": "/Users/x/repo/runs/r/analysis/simple_judge",
                                                       "ledgers": ["/Users/x/repo/state/budget.sqlite"]}},
                  "route_record": "data/validation/x.json"}
        out = _json.loads(simple_judge_site.space_report_json(_json.dumps(report).encode()))
        self.assertEqual(out["budget"]["prior"]["judge_dir"], "<host>/r/analysis/simple_judge")
        self.assertEqual(out["budget"]["prior"]["ledgers"], ["<host>/budget.sqlite"])
        self.assertEqual(out["route_record"], "data/validation/x.json")
        self.assertNotIn("/Users/", simple_judge_site.space_report_json(_json.dumps(report).encode()).decode())


if __name__ == '__main__':
    unittest.main()
