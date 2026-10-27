"""Cloudflare Pages library (owner, October 27, 2026): scripts/publish_cloudflare.py.
Offline: fake wrangler runner and fake HTTP getter; no login, network or deploy."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import dataset_logs  # noqa: E402
import publish_cloudflare as cf  # noqa: E402

RUN = 'r1-20261027T000000Z'
OTHER = 'r0-20261020T000000Z'
COMMIT = 'a' * 40
HF_INSPECT = ('<!doctype html><html><head>' + dataset_logs.shim_script(path=f'{RUN}/inspect', commit=COMMIT)
              + '<script type="module" src="./assets/index.js"></script></head><body></body></html>')


def entry(run, start):
    return {'run': run, 'start_utc': start, 'links': {'Report': f'{run}/results.html'}}


def fake_catalogue(entries, unlisted):
    entries = [e for e in entries if e['run'] not in unlisted]
    data = json.dumps({'experiments': entries, 'warnings': []}).encode()
    return {'index.html': b'<html>' + b','.join(e['run'].encode() for e in entries) + b'</html>',
            'experiments.json': data}


def site(*runs):
    files = fake_catalogue([entry(r, '2026-10-2%sT00:00:00Z' % i) for i, r in enumerate(runs)], ())
    for run in runs:
        files[f'{run}/results.html'] = b'<html><a href="results.json">r</a></html>'
        files[f'{run}/results.json'] = b'{}'
    return files


class GateTests(unittest.TestCase):
    def test_refuses_private_special_credential_and_large_files(self):
        cf.site_gate(f'{RUN}/results.html', b'<html></html>')
        refused = {
            f'{RUN}/inspect-view/logs/x.eval': b'PK',
            f'{RUN}/raw/events.jsonl': b'{}',
            f'live/{RUN}/inspect/x.eval': b'PK',
            f'{RUN}/snapshots.tar.gz': b'x',
            '_worker.js': b'export default {}',
            f'{RUN}/_headers': b'/*',
            'functions/api.js': b'x',
            '.env': b'X=1',
            f'{RUN}/.hidden': b'x',
            f'{RUN}/notes.html': b'key sk-' + b'A' * 30,
            f'{RUN}/big.json': b'0' * (cf.MAX_FILE_BYTES + 1),
        }
        for name, data in refused.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                cf.site_gate(name, data)

    def test_public_placeholder_key_is_allowed(self):
        cf.site_gate(f'{RUN}/x.html', b'sk-ant-api03-DOq5tyLPrk9M4hPE')


class InspectPageTests(unittest.TestCase):
    def test_hf_shim_replaced_by_notice_linking_the_dataset_folder(self):
        page = cf.inspect_page(HF_INSPECT)
        self.assertNotIn('evallog-fetch', page.split('</script>')[0][:200])
        self.assertNotIn('id="evallog-fetch"', page)
        self.assertIn(cf.SHIM_MARKER, page)
        self.assertIn(f'data-records="https://huggingface.co/datasets/{dataset_logs.DATASET_REPO}/tree/{COMMIT}/'
                      f'{RUN}/inspect"', page)
        self.assertEqual(page, cf.inspect_page(page, records=cf.records_url(HF_INSPECT)))

    def test_unpinned_or_missing_shim_gets_an_offline_notice(self):
        unpinned = HF_INSPECT.replace(COMMIT, dataset_logs.COMMIT_PLACEHOLDER)
        self.assertIn('data-records=""', cf.inspect_page(unpinned))
        bare = '<html><head><title>x</title></head></html>'
        self.assertTrue(cf.inspect_page(bare).startswith('<html><head><script id="private-logs"'))
        with self.assertRaises(ValueError):
            cf.inspect_page('<html>no head</html>')
        with self.assertRaises(ValueError):
            cf.notice_shim('https://example.com/x')

    def test_placeholder_dataset_links_point_to_private_notice(self):
        page = (f'<a href="https://huggingface.co/datasets/o/d/blob/{dataset_logs.COMMIT_PLACEHOLDER}/{RUN}/x.jsonl">'
                'x</a>')
        self.assertEqual(cf.private_links(page, f'{RUN}/sub/results.html'), f'<a href="../../{cf.PRIVATE_PAGE}">x</a>')


class SpaceTests(unittest.TestCase):
    def test_space_files_drop_hf_configuration_and_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in {'README.md': b'---', '.gitattributes': b'*', 'signin.html': b'<html>',
                               '.git/config': b'[core]', 'index.html': b'<html>',
                               f'{RUN}/results.html': b'<html>', f'{RUN}/inspect-view/index.html': HF_INSPECT.encode(),
                               f'live/{RUN}/inspect-view/logs/x.eval': b'PK',
                               f'{RUN}/analysis/timeline.md': b'# t'}.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(data)
            files, private = cf.space_files(root)
            self.assertEqual(set(files), {'index.html', f'{RUN}/results.html', f'{RUN}/inspect-view/index.html'})
            self.assertEqual(private, {'eval': 1, 'timeline': 1})
            self.assertIn(cf.SHIM_MARKER.encode(), files[f'{RUN}/inspect-view/index.html'])
            (root / 'x.json').write_bytes(cf.LFS_POINTER + b'1\noid sha256:' + b'0' * 64)
            with self.assertRaises(ValueError):
                cf.space_files(root)

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_mirror_preserves_site_only_runs_and_lists_changes(self, _):
        base = site(OTHER, RUN)
        space = site(RUN)
        space[f'{RUN}/results.html'] = b'<html>re-rendered</html>'
        files, plan = cf.plan_mirror(base, space, COMMIT, unlisted=())
        self.assertEqual(plan['preserved_site_only_runs'], [OTHER])
        self.assertIn(f'{OTHER}/results.json', files)
        self.assertEqual({e['run'] for e in json.loads(files['experiments.json'])['experiments']}, {OTHER, RUN})
        self.assertEqual(plan['changed_files'], [f'{RUN}/results.html'])
        files, plan = cf.plan_mirror(site(RUN), site(RUN), COMMIT, unlisted=())
        self.assertEqual(files, site(RUN))
        self.assertEqual(plan['preserved_site_only_runs'], [])


class BundleTests(unittest.TestCase):
    def bundle(self, tmp, files):
        root = Path(tmp)
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        manifest = {'runs': [RUN], 'files': {n: {} for n in files}, 'dataset': {}}
        return root, (manifest, [entry(RUN, '2026-10-27T00:00:00Z')])

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_new_run_added_and_existing_runs_immutable(self, _):
        with tempfile.TemporaryDirectory() as tmp:
            root, loaded = self.bundle(tmp, {f'{RUN}/results.html': b'<html>new</html>',
                                             f'{RUN}/inspect-view/index.html': HF_INSPECT.replace(
                                                 COMMIT, dataset_logs.COMMIT_PLACEHOLDER).encode(),
                                             'index.html': b'ignored'})
            with patch.object(cf, 'load_bundle', return_value=loaded):
                files, plan = cf.plan_bundle(site(OTHER), root, unlisted=())
            self.assertEqual(plan['added_files'], 2)
            self.assertIn(b'data-records=""', files[f'{RUN}/inspect-view/index.html'])
            self.assertEqual({e['run'] for e in json.loads(files['experiments.json'])['experiments']}, {OTHER, RUN})
            with patch.object(cf, 'load_bundle', return_value=loaded):
                files, _ = cf.plan_bundle(site(OTHER), root, unlisted=(), dataset_commit=COMMIT)
            self.assertIn(f'tree/{COMMIT}/'.encode(), files[f'{RUN}/inspect-view/index.html'])
            existing = {**site(OTHER), f'{RUN}/results.html': b'<html>old</html>'}
            with patch.object(cf, 'load_bundle', return_value=loaded), self.assertRaises(ValueError):
                cf.plan_bundle(existing, root, unlisted=())
            with patch.object(cf, 'load_bundle', return_value=loaded), self.assertRaises(ValueError):
                cf.plan_bundle({**existing, f'{RUN}/extra.html': b'x'}, root, unlisted=(), replace_pages=True)
            with patch.object(cf, 'load_bundle', return_value=loaded):
                _, plan = cf.plan_bundle(existing, root, unlisted=(), replace_pages=True)
            self.assertEqual([x['path'] for x in plan['replaced_pages']], [f'{RUN}/results.html'])
            with patch.object(cf, 'load_bundle', return_value=loaded), self.assertRaises(ValueError):
                cf.plan_bundle(site(OTHER), root, unlisted=(RUN,))

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_records_are_never_replaced(self, _):
        with tempfile.TemporaryDirectory() as tmp:
            root, loaded = self.bundle(tmp, {f'{RUN}/results.json': b'{"new": 1}'})
            with patch.object(cf, 'load_bundle', return_value=loaded), self.assertRaises(ValueError):
                cf.plan_bundle(site(RUN), root, unlisted=(), replace_pages=True)


class FinishTests(unittest.TestCase):
    def test_manifest_generated_pages_and_limits(self):
        files, manifest = cf.finish(site(RUN), {'history': [{'utc': 'old'}]}, {'source': {'kind': 'test'}})
        self.assertEqual(set(files) - set(site(RUN)), {cf.SITE_MANIFEST, cf.NOT_FOUND, cf.PRIVATE_PAGE})
        recorded = json.loads(files[cf.SITE_MANIFEST])
        self.assertEqual(recorded['schema'], cf.SITE_SCHEMA)
        self.assertEqual(set(recorded['files']), set(files) - {cf.SITE_MANIFEST})
        self.assertEqual(recorded['runs'], [RUN])
        self.assertEqual(len(recorded['history']), 2)
        with patch.object(cf, 'MAX_FILES', 3), self.assertRaises(ValueError):
            cf.finish(site(RUN), {}, {'source': {}})
        with self.assertRaises(ValueError):
            cf.finish({**site(RUN), f'{RUN}/x.jsonl': b'{}'}, {}, {'source': {}})


class FakeWrangler:
    def __init__(self, before, after):
        self.lists = [before, after]
        self.calls = []

    def __call__(self, command, cwd, **kwargs):
        self.calls.append(command)
        if 'list' in command:
            records = self.lists.pop(0) if len(self.lists) > 1 else self.lists[0]
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                [{'Id': i, 'Deployment': f'https://{i}.p.pages.dev', 'Status': 'Success'} for i in records]),
                stderr='')
        return SimpleNamespace(returncode=0, stdout='Deployment complete!', stderr='')


class DeployTests(unittest.TestCase):
    def test_guards_and_wrangler_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeWrangler(['b'], ['n', 'b'])
            deployment, _ = cf.deploy({'index.html': b'x'}, {'id': 'b'}, 'm', tmp, runner)
            self.assertEqual(deployment['id'], 'n')
            deploy_call = next(c for c in runner.calls if 'deploy' in c)
            self.assertIn('--project-name', deploy_call)
            self.assertFalse((Path(tmp) / 'site').exists())
            with self.assertRaises(ValueError):  # production moved since planning
                cf.deploy({'index.html': b'x'}, {'id': 'b'}, 'm', tmp, FakeWrangler(['c', 'b'], ['n', 'c']))
            with self.assertRaises(ValueError):  # another deployment interleaved
                cf.deploy({'index.html': b'x'}, {'id': 'b'}, 'm', tmp, FakeWrangler(['b'], ['n', 'x', 'b']))
            first = FakeWrangler([], ['n'])
            self.assertEqual(cf.deploy({'index.html': b'x'}, None, 'm', tmp, first)[0]['id'], 'n')
        with self.assertRaises(ValueError):
            cf.run_wrangler(['whoami'], cf.ROOT, FakeWrangler([], []))
        with self.assertRaises(ValueError):
            cf.run_wrangler(['whoami'], cf.ROOT / 'runs', FakeWrangler([], []))
        failing = lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='boom')  # noqa: E731
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError):
            cf.run_wrangler(['whoami'], tmp, failing)


class Served:
    """A fake Pages host serving ``files``; 404 otherwise."""
    def __init__(self, files, overrides=None):
        self.files, self.overrides = files, overrides or {}

    def __call__(self, url):
        name = url.split('pages.dev/', 1)[1]
        if name in self.overrides:
            return self.overrides[name]
        if name not in self.files:
            return 404, {'Content-Type': 'text/html'}, b'not found'
        kind = 'text/html' if name.endswith('.html') else 'application/json'
        return 200, {'Content-Type': kind}, self.files[name]


class LoadAndVerifyTests(unittest.TestCase):
    def site(self):
        files = {**site(RUN), f'{RUN}/inspect-view/index.html': cf.inspect_page(HF_INSPECT).encode(),
                 f'{RUN}/inspect-view/assets/index.js': b'js',
                 f'{RUN}/inspect-view/logs/listing.json': b'{"x.eval": {}}'}
        files, _ = cf.finish(files, {}, {'source': {}})
        return files

    def test_load_site_checks_digests_and_caches(self):
        files = self.site()
        with tempfile.TemporaryDirectory() as cache:
            manifest, loaded = cf.load_site('https://d.p.pages.dev', Served(files), cache)
            self.assertEqual(loaded, {n: d for n, d in files.items() if n != cf.SITE_MANIFEST})
            offline = lambda url: (200, {}, files[cf.SITE_MANIFEST]) if url.endswith(cf.SITE_MANIFEST) \
                else (500, {}, b'')  # noqa: E731
            self.assertEqual(cf.load_site('https://d.p.pages.dev', offline, cache)[1], loaded)  # from the cache
        with tempfile.TemporaryDirectory() as cache, self.assertRaises(ValueError):
            cf.load_site('https://d.p.pages.dev', Served(files, {'index.html': (200, {}, b'tampered')}), cache)
        self.assertEqual(cf.load_site(None), ({}, {}))

    def test_verify_passes_and_flags_problems(self):
        files = self.site()
        result = cf.verify('https://d.p.pages.dev', getter=Served(files))
        self.assertTrue(result['ok'], result)
        bad = {f'{RUN}/results.html': (200, {'Content-Type': 'text/html'}, b'changed'),
               f'{RUN}/inspect-view/logs/x.eval': (200, {}, b'PK\x03\x04')}
        result = cf.verify('https://d.p.pages.dev', getter=Served(files, bad))
        failed = {c['check'] for c in result['checks'] if not c['ok']}
        self.assertEqual(failed, {'served_bytes', 'no_private_logs_served'})
        broken = dict(files)
        broken[f'{RUN}/results.html'] = b'<a href="missing.html">x</a><a href="inspect/x.eval">l</a>'
        broken, _ = cf.finish({n: d for n, d in broken.items()}, {}, {'source': {}})
        result = cf.verify('https://d.p.pages.dev', getter=Served(broken))
        self.assertIn('no_broken_links', {c['check'] for c in result['checks'] if not c['ok']})
        spa = Served(files)
        fallback = lambda url: spa(url) if 'no-such-page' not in url else (200, {}, files['index.html'])  # noqa
        result = cf.verify('https://d.p.pages.dev', getter=fallback)
        self.assertIn('not_found_404', {c['check'] for c in result['checks'] if not c['ok']})


if __name__ == '__main__':
    unittest.main()
