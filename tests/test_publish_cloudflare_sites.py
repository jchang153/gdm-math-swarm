"""Separate ImpossibleBench library (owner, November 3, 2026: "don't publish the ib results to the existing gdm page;
please create a new page in the same exact style"): publish_cloudflare.py --site, its domain guard, the IB catalogue
texts (catalogue_intro_ib.py) and fetch_run.py's site choice. Offline: no login, network or deploy."""
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
import build_experiment_index as index  # noqa: E402
import catalogue_intro  # noqa: E402
import catalogue_page  # noqa: E402
import catalogue_intro_ib  # noqa: E402
import fetch_run  # noqa: E402
import publish_cloudflare as cf  # noqa: E402
import run_profile  # noqa: E402

MATH = 'math-run-20261101T000000Z'
CODE = 'ib20-seeded-control-20261103T000000Z'
CODE_SETTINGS = json.dumps({'config': {'domain': 'code'}, 'effective': {'domain': 'code'}}).encode()
MATH_SETTINGS = json.dumps({'config': {'track': 'spontaneous'}, 'effective': {}}).encode()


def entry(run, hour):
    return {'run': run, 'date': '2026-11-03 10:00 PST', 'start_utc': f'2026-11-03T{hour:02d}:00:00+00:00',
            'track': 'seeded', 'agents': 20, 'model': 'vendor/model', 'provider_route': 'route/us',
            'elapsed_seconds': 3600, 'pilot_spent_usd': '1.00', 'distinct_verified_tasks': 0, 'task_count': 20,
            'agent_exits': {'harness_completed': 20}, 'links': {'Report': run + '/results.html'}}


def run_files(run, settings):
    return {f'{run}/results.html': b'<html><a href="results.json">r</a></html>', f'{run}/results.json': b'{}',
            f'{run}/settings.json': settings}


class RegistryTests(unittest.TestCase):
    def test_math_site_is_the_unchanged_default(self):
        gdm = cf.SITES[cf.DEFAULT_SITE]
        self.assertEqual(cf.DEFAULT_SITE, 'gdm')
        self.assertEqual((gdm.project, gdm.url, gdm.domain), (cf.PROJECT, cf.SITE_URL, 'lean'))
        self.assertEqual(gdm.project, 'gdm-math-swarm-experiments')
        self.assertEqual(gdm.cache, cf.CACHE)
        self.assertEqual(gdm.receipts, ROOT / 'runs' / 'phase-b' / 'cloudflare' / 'receipts')
        self.assertIs(cf.site_of(None), gdm)

    def test_ib_site_is_separate(self):
        ib = cf.SITES['ib']
        self.assertEqual(ib.project, 'gdm-impossiblebench-experiments')
        self.assertEqual(ib.url, 'https://gdm-impossiblebench-experiments.pages.dev')
        self.assertEqual(ib.domain, 'code')
        self.assertEqual(ib.receipts, ROOT / 'runs' / 'phase-b' / 'cloudflare-ib' / 'receipts')
        self.assertNotEqual(ib.cache, cf.CACHE)
        self.assertFalse(ib.mirror_space)
        self.assertIs(cf.site_of('ib'), ib)

    def test_settings_domain(self):
        self.assertEqual(cf.settings_domain(None), 'lean')
        self.assertEqual(cf.settings_domain(b'not json'), 'lean')
        self.assertEqual(cf.settings_domain(MATH_SETTINGS), 'lean')
        self.assertEqual(cf.settings_domain(CODE_SETTINGS), 'code')
        self.assertEqual(cf.settings_domain(json.dumps({'config': {'domain': 'code'}}).encode()), 'code')
        self.assertEqual(cf.settings_domain(json.dumps({'effective': {'domain': 'code'}, 'config': {}}).encode()),
                         'code')


class DomainGuardTests(unittest.TestCase):
    def test_coding_run_refused_on_math_site(self):
        files, manifest = cf.finish(run_files(MATH, MATH_SETTINGS), {}, {'source': {}})
        self.assertEqual(manifest['project'], cf.PROJECT)
        cf.finish({f'{MATH}/results.html': b'<html></html>'}, {}, {'source': {}})  # no settings: a math run
        with self.assertRaisesRegex(ValueError, 'another domain'):
            cf.finish({**run_files(MATH, MATH_SETTINGS), **run_files(CODE, CODE_SETTINGS)}, {}, {'source': {}})

    def test_math_run_refused_on_ib_site(self):
        files, manifest = cf.finish(run_files(CODE, CODE_SETTINGS), {}, {'source': {}}, site='ib')
        self.assertEqual(manifest['project'], 'gdm-impossiblebench-experiments')
        self.assertEqual(json.loads(files[cf.SITE_MANIFEST])['project'], 'gdm-impossiblebench-experiments')
        for bad in (run_files(MATH, MATH_SETTINGS), {f'{MATH}/results.html': b'<html></html>'}):
            with self.subTest(bad=sorted(bad)), self.assertRaisesRegex(ValueError, 'another domain'):
                cf.finish({**run_files(CODE, CODE_SETTINGS), **bad}, {}, {'source': {}}, site='ib')

    def test_project_must_match_site(self):
        with self.assertRaises(ValueError):
            cf.finish(run_files(CODE, CODE_SETTINGS), {}, {'source': {}}, project=cf.PROJECT, site='ib')

    def test_bundle_of_wrong_domain_refused_before_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = run_files(CODE, CODE_SETTINGS)
            for name, data in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(data)
            loaded = ({'runs': [CODE], 'files': {n: {} for n in files}, 'dataset': {}}, [entry(CODE, 1)])
            with patch.object(cf, 'load_bundle', return_value=loaded):
                with self.assertRaisesRegex(ValueError, 'another domain'):
                    cf.plan_bundle({}, root, unlisted=())
                out, plan = cf.plan_bundle({}, root, unlisted=(), site='ib')
            self.assertIn(b'GDM ImpossibleBench Swarm experiments', out['index.html'])
            self.assertEqual(plan['source']['runs'], [CODE])

    def test_mirror_space_is_math_only(self):
        with self.assertRaises(ValueError):
            cf.plan_mirror({}, {}, 'a' * 40, (), site='ib')
        with self.assertRaises(SystemExit), patch.object(cf, 'deployments') as deployments, \
                contextlib.redirect_stderr(io.StringIO()):
            cf.main(['mirror-space', '--site', 'ib'])
        deployments.assert_not_called()


class CatalogueTests(unittest.TestCase):
    def render(self, site):
        return cf.render_catalogue([entry(CODE, 2), entry(CODE + 'x', 1)], (), site=site)

    def test_ib_catalogue_same_style_own_texts(self):
        math, ib = self.render(None), self.render('ib')
        self.assertEqual(math, self.render('gdm'))
        math_page, ib_page = math['index.html'].decode(), ib['index.html'].decode()
        self.assertIn('<title>GDM Math Swarm experiments</title>', math_page)
        self.assertIn('<title>GDM ImpossibleBench Swarm experiments</title>', ib_page)
        self.assertIn(catalogue_intro_ib.HEADLINE, ib_page)
        self.assertIn('Impossible-LiveCodeBench', ib_page)
        self.assertIn(catalogue_intro_ib.PAPER_URL, ib_page)
        for text in (catalogue_intro.HEADLINE, catalogue_intro.PAPER_URL, 'Lean 4'):
            self.assertNotIn(text, ib_page)
        self.assertEqual(json.loads(math['experiments.json']), json.loads(ib['experiments.json']))
        # Same page apart from the texts and the math site's run panes (owner, November 6, 2026): the math style is
        # the IB style plus the pane style; the IB catalogue keeps its cards and plain script.
        style = lambda page: page.split('<style>', 1)[1].split('</style>', 1)[0]  # noqa: E731
        self.assertEqual(style(math_page), style(ib_page) + catalogue_page.PANE_STYLE)
        self.assertIn('class="rc pane"', math_page)
        self.assertNotIn('class="rc pane"', ib_page)
        self.assertNotIn('catalogue-labels', ib_page)
        self.assertNotIn('id="per-row"', ib_page)
        self.assertIn('id="compare-view"', ib_page)  # the IB catalogue keeps its compare view
        self.assertEqual(ib_page.rsplit('<script>', 1)[1],
                         catalogue_page.SCRIPT.replace('/*CARDJS*/', run_profile.CARD_JS) + '</script></body></html>')
        self.assertEqual(math_page.rsplit('<script>', 1)[1], index.pane_script() + '</script></body></html>')

    def test_math_catalogue_reads_agent_labels_from_run_pages(self):
        page = ('<p class="ab-legend" id="ab-legend"><strong>Agent labels</strong> (from the simplified behavior judge; '
                'hover): x</p><span class="ab-id">agent-001<span class="ab-set"><span class="ab ab-exploiter" '
                'title="E">E</span></span></span>').encode()
        files = {f'{MATH}/results.html': page, f'{CODE}/results.html': page}
        math = cf.render_catalogue([entry(MATH, 2)], (), files)['index.html'].decode()
        self.assertIn('id="catalogue-labels">{"' + MATH + '":{"C":0,"E":1,', math)
        ib = cf.render_catalogue([entry(CODE, 2)], (), files, site='ib')['index.html'].decode()
        self.assertNotIn('catalogue-labels', ib)  # the ImpossibleBench catalogue is unchanged

    def test_default_write_index_unchanged_by_intro_argument(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            index.write_index(a, [entry(MATH, 1)], hosted=True)
            index.write_index(b, [entry(MATH, 1)], hosted=True, intro=catalogue_intro)
            self.assertEqual((Path(a) / 'index.html').read_bytes(), (Path(b) / 'index.html').read_bytes())


class CliTests(unittest.TestCase):
    def test_verify_defaults_to_the_site_url(self):
        result = {'ok': True}
        with patch.object(cf, 'verify', return_value=result) as verify, patch('builtins.print'):
            cf.main(['verify', '--site', 'ib'])
            cf.main(['verify'])
        self.assertEqual(verify.call_args_list[0].args[0], 'https://gdm-impossiblebench-experiments.pages.dev')
        self.assertEqual(verify.call_args_list[1].args[0], cf.SITE_URL)

    def plan(self, argv, base_files, planner, result):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cf, 'deployments', return_value=[]) as deployments, \
                patch.object(cf, 'load_site', return_value=({}, base_files)) as load_site, \
                patch.object(cf, planner, return_value=result), patch('builtins.print') as printed:
            code = cf.main([*argv, '--workdir', tmp])
        return code, deployments, load_site, json.loads(printed.call_args.args[0])

    def test_storymap_plans_against_the_ib_project(self):
        files = run_files(CODE, CODE_SETTINGS)
        code, deployments, load_site, out = self.plan(
            ['storymap', '--site', 'ib', '--run', f'{CODE}=/nowhere'], files, 'plan_storymap',
            (files, {'source': {'kind': 'storymap', 'runs': [CODE]}}))
        self.assertEqual(code, 0)
        self.assertEqual(deployments.call_args.kwargs['project'], 'gdm-impossiblebench-experiments')
        self.assertEqual(load_site.call_args.kwargs['cache'], cf.SITES['ib'].cache)
        self.assertEqual(out['project'], 'gdm-impossiblebench-experiments')
        self.assertFalse(out['committed'])

    def test_math_default_and_cross_domain_refusal(self):
        files = run_files(MATH, MATH_SETTINGS)
        _, deployments, load_site, out = self.plan(['catalogue'], files, 'plan_catalogue',
                                                   (files, {'source': {'kind': 'catalogue'}}))
        self.assertEqual(deployments.call_args.kwargs['project'], cf.PROJECT)
        self.assertEqual(load_site.call_args.kwargs['cache'], cf.CACHE)
        self.assertEqual(out['project'], cf.PROJECT)
        with self.assertRaisesRegex(ValueError, 'another domain'):
            self.plan(['catalogue', '--site', 'ib'], files, 'plan_catalogue', (files, {'source': {}}))

    def test_deploy_goes_to_the_ib_project(self):
        files = run_files(CODE, CODE_SETTINGS)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache, \
                patch.object(cf, 'SITES', {**cf.SITES, 'ib': cf.SITES['ib']._replace(cache=Path(cache))}), \
                patch.object(cf, 'deployments', return_value=[]), \
                patch.object(cf, 'load_site', return_value=({}, {})), \
                patch.object(cf, 'plan_catalogue', return_value=(files, {'source': {'kind': 'catalogue'}})), \
                patch.object(cf, 'deploy', return_value=({'id': 'n', 'url': 'https://n.x.pages.dev'}, '')) as deploy, \
                patch.object(cf, 'verify', return_value={'ok': True}) as verify, patch('builtins.print'):
            self.assertEqual(cf.main(['catalogue', '--site', 'ib', '--commit', '--workdir', tmp]), 0)
        self.assertEqual(deploy.call_args.kwargs['project'], 'gdm-impossiblebench-experiments')
        verify.assert_called_once_with('https://n.x.pages.dev')


class FetchRunTests(unittest.TestCase):
    def test_coding_run_published_to_ib_with_separate_receipts(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            (bundle / CODE).mkdir()
            (bundle / CODE / 'settings.json').write_bytes(CODE_SETTINGS)
            self.assertEqual(fetch_run.site_for(CODE, bundle), 'ib')
            self.assertEqual(fetch_run.site_for(MATH, bundle), 'gdm')  # no settings: a math run
            upload = {'outcome': 'uploaded', 'dataset_commit': 'a' * 40}
            result = fetch_run.publish_site(CODE, bundle, upload, publish=True, runner=runner, log=lambda m: None)
        publish, check = calls
        self.assertEqual(publish[publish.index('--site') + 1], 'ib')
        self.assertEqual(Path(publish[publish.index('--receipt') + 1]),
                         fetch_run.ROOT / 'runs' / 'phase-b' / 'cloudflare-ib' / 'receipts' / f'{CODE}.json')
        self.assertEqual(check[-2:], ['--site', 'ib'])
        self.assertEqual(result['site'], 'ib')

    def test_math_run_command_unchanged(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            upload = {'outcome': 'uploaded', 'dataset_commit': 'a' * 40}
            result = fetch_run.publish_site(MATH, Path(tmp), upload, publish=True, runner=runner, log=lambda m: None)
        publish, check = calls
        self.assertNotIn('--site', publish + check)
        self.assertEqual(Path(publish[-1]), fetch_run.ROOT / 'runs' / 'phase-b' / 'cloudflare' / 'receipts'
                         / f'{MATH}.json')
        self.assertEqual(check[-2:], ['--run', MATH])
        self.assertNotIn('site', result)


if __name__ == '__main__':
    unittest.main()
