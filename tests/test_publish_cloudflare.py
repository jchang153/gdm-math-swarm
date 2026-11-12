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


def fake_catalogue(entries, unlisted, files=None, site=None):
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
    def test_republish_compares_pages_with_log_viewer_links(self, _):
        # A run already served with its links pointed at the log viewer (November 12, 2026) republishes unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            bundle = {f'{RUN}/results.html': b'<html><a href="inspect-view/index.html">I</a></html>',
                      f'{RUN}/inspect-view/index.html': HF_INSPECT.replace(
                          COMMIT, dataset_logs.COMMIT_PLACEHOLDER).encode(),
                      f'{RUN}/inspect-view/assets/index.js': b'js'}
            root, loaded = self.bundle(tmp, bundle)
            with patch.object(cf, 'load_bundle', return_value=loaded):
                first, _ = cf.plan_bundle(site(OTHER), root, unlisted=(), dataset_commit=COMMIT)
            served, changed = cf.apply_viewer(first)
            self.assertEqual(changed, 0)  # the plan already serves the viewer links
            self.assertIn(b'gdm-math-swarm-viewer.pages.dev', served[f'{RUN}/results.html'])
            with patch.object(cf, 'load_bundle', return_value=loaded):
                again, plan = cf.plan_bundle(served, root, unlisted=(), dataset_commit=COMMIT)
            self.assertEqual(plan['replaced_pages'], [])
            self.assertEqual(again[f'{RUN}/results.html'], served[f'{RUN}/results.html'])

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_new_run_added_and_existing_runs_immutable(self, _):
        with tempfile.TemporaryDirectory() as tmp:
            root, loaded = self.bundle(tmp, {f'{RUN}/results.html': b'<html>new</html>',
                                             f'{RUN}/inspect-view/index.html': HF_INSPECT.replace(
                                                 COMMIT, dataset_logs.COMMIT_PLACEHOLDER).encode(),
                                             f'{RUN}/inspect-view/assets/index.js': b'js',
                                             'index.html': b'ignored'})
            with patch.object(cf, 'load_bundle', return_value=loaded):
                files, plan = cf.plan_bundle(site(OTHER), root, unlisted=())
            self.assertEqual(plan['added_files'], 3)
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


    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_withdraw_removes_only_recorded_runs(self, _):
        base = site(OTHER, RUN)
        with self.assertRaises(ValueError):
            cf.plan_withdraw(base, unlisted=(), runs=[RUN])  # not recorded as unlisted
        with self.assertRaises(ValueError):
            cf.plan_withdraw(base, unlisted=('absent-run',), runs=['absent-run'])
        files, plan = cf.plan_withdraw(base, unlisted=(RUN,), runs=[RUN])
        self.assertFalse([n for n in files if n.startswith(RUN + '/')])
        self.assertEqual(plan['removed_files'], 2)
        for name in (f'{OTHER}/results.html', f'{OTHER}/results.json'):
            self.assertEqual(files[name], base[name])
        self.assertEqual([e['run'] for e in json.loads(files['experiments.json'])['experiments']], [OTHER])


class FinishTests(unittest.TestCase):
    def test_manifest_generated_pages_and_limits(self):
        files, manifest = cf.finish(site(RUN), {'history': [{'utc': 'old'}]}, {'source': {'kind': 'test'}})
        self.assertEqual(set(files) - set(site(RUN)), {cf.SITE_MANIFEST, cf.NOT_FOUND, cf.PRIVATE_PAGE}
                         | set(cf.site_look.font_assets()))  # the math site's fonts (November 6, 2026)
        recorded = json.loads(files[cf.SITE_MANIFEST])
        self.assertEqual(recorded['schema'], cf.SITE_SCHEMA)
        self.assertEqual(set(recorded['files']), set(files) - {cf.SITE_MANIFEST})
        self.assertEqual(recorded['runs'], [RUN])
        self.assertEqual(len(recorded['history']), 2)
        with patch.object(cf, 'MAX_FILES', 3), self.assertRaises(ValueError):
            cf.finish(site(RUN), {}, {'source': {}})
        with self.assertRaises(ValueError):
            cf.finish({**site(RUN), f'{RUN}/x.jsonl': b'{}'}, {}, {'source': {}})


def agent_files(run, variant=b''):
    return {f'{run}/instructions/agent-001-aa.json': b'{"text": "caf\xc3\xa9"}' + variant,
            f'{run}/instructions/received/agent-001-bb.json': b'{"n": 1}',
            f'{run}/prompts/agent-001.txt': b'You are agent-001.\n',
            f'{run}/prompts/agent-002.txt': b'\xff\xfe binary \x00'}


def more_files(run):
    return {f'{run}/evaluations/sources/{"a" * 64}.lean': b'theorem x : True := trivial\n',
            f'{run}/analysis/storymap/agents/agent-001.html': '<html><a href="../index.html">map</a> caf\u00e9</html>'
            .encode(),
            f'{run}/analysis/judge/agents/agent-001.json': b'{"label": "x"}',
            f'{run}/analysis/judge/packages/agent-001.json': b'{"p": 1}',
            f'{run}/analysis/judge/review/agents/agent-001.html': b'<html>review</html>'}


class PackTests(unittest.TestCase):
    def test_pack_is_lossless_and_keeps_old_urls(self):
        files = {**site(RUN, OTHER), **agent_files(RUN), **agent_files(OTHER)}
        out, manifest = cf.finish(files, {}, {'source': {'kind': 'test'}})
        self.assertFalse([n for n in out if cf.packable(n)])
        for run in (RUN, OTHER):
            self.assertEqual(cf.unpack_run(run, out), agent_files(run))
            for name in (f'{run}/packed/index.html', f'{run}/instructions/404.html', f'{run}/prompts/404.html'):
                self.assertIn(f'data-run="{run}"'.encode(), out[name])
            index = json.loads(out[f'{run}/packed/index.json'])['files']
            self.assertEqual(set(index), {n[len(run) + 1:] for n in agent_files(run)})
        self.assertNotIn('_redirects', out)
        self.assertEqual(set(manifest['files']), set(out) - {cf.SITE_MANIFEST})
        for name in site(RUN, OTHER):  # nothing else changes
            if name not in ('index.html', 'experiments.json'):
                self.assertEqual(out[name], files[name])
        again, _ = cf.finish({n: d for n, d in out.items() if n != cf.SITE_MANIFEST}, {}, {'source': {}})
        self.assertEqual({n: d for n, d in again.items() if n != cf.SITE_MANIFEST},
                         {n: d for n, d in out.items() if n != cf.SITE_MANIFEST})  # repack is idempotent
        with patch.object(cf, 'PACK_PART_BYTES', 10):
            parts, _ = cf.finish(files, {}, {'source': {}})
            self.assertGreater(sum(cf.is_pack_file(n) for n in parts), 4)
            self.assertEqual(cf.unpack_run(RUN, parts), agent_files(RUN))

    def test_gate_and_tampering(self):
        with self.assertRaises(ValueError):
            cf.site_gate('_redirects', b'/* https://evil.example 302\n')
        out, _ = cf.finish({**site(RUN), **agent_files(RUN)}, {}, {'source': {}})
        name = next(n for n in out if cf.is_pack_file(n))
        pack = json.loads(out[name])
        first = next(iter(pack['files']))
        pack['files'][first]['content'] += 'x'
        with self.assertRaises(ValueError):
            cf.unpack_run(RUN, {name: json.dumps(pack).encode()})
        secret = {f'{RUN}/prompts/agent-009.txt': b'\xff' + b'hf_' + b'A' * 34}
        with patch.object(cf, 'SECRET', __import__('re').compile(rb'hf_[A-Za-z]{30,}')), \
                self.assertRaises(ValueError):
            cf.finish({**site(RUN), **secret}, {}, {'source': {}})

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_bundle_against_packed_site(self, _):
        base, _ = cf.finish({**site(RUN), **agent_files(RUN)}, {}, {'source': {}})
        base = {n: d for n, d in base.items() if n not in cf.GENERATED}
        with tempfile.TemporaryDirectory() as tmp:
            root, loaded = BundleTests.bundle(None, tmp, {**{n: d for n, d in site(RUN).items() if '/' in n},
                                                          **agent_files(RUN)})
            loaded = (loaded[0], json.loads(site(RUN)['experiments.json'])['experiments'])
            with patch.object(cf, 'load_bundle', return_value=loaded):
                files, _ = cf.plan_bundle(base, root, unlisted=(), replace_pages=True)
            out, _ = cf.finish(files, {}, {'source': {}})
            self.assertEqual(cf.unpack_run(RUN, out), agent_files(RUN))
        with tempfile.TemporaryDirectory() as tmp:
            root, loaded = BundleTests.bundle(None, tmp, {**{n: d for n, d in site(RUN).items() if '/' in n},
                                                          **agent_files(RUN, b' ')})
            loaded = (loaded[0], json.loads(site(RUN)['experiments.json'])['experiments'])
            with patch.object(cf, 'load_bundle', return_value=loaded), self.assertRaises(ValueError):
                cf.plan_bundle(base, root, unlisted=(), replace_pages=True)

    def test_verify_checks_old_urls(self):
        files, _ = cf.finish({**LoadAndVerifyTests.site(None), **agent_files(RUN), **more_files(RUN)}, {},
                             {'source': {}})
        result = cf.verify('https://d.p.pages.dev', getter=Served(files, nested_404=True))
        self.assertTrue(result['ok'], result)
        old = next(c for c in result['checks'] if c['check'] == 'packed_old_urls')
        self.assertIn('7 group(s)', old['detail'])
        result = cf.verify('https://d.p.pages.dev', getter=Served(files))
        self.assertEqual({c['check'] for c in result['checks'] if not c['ok']}, {'packed_old_urls'})
        originals = {**agent_files(RUN), **more_files(RUN)}  # an edge copy of the original bytes also counts
        cached = Served(files, {n: (200, {}, d) for n, d in originals.items()})
        self.assertTrue(cf.verify('https://d.p.pages.dev', getter=cached)['ok'])
        wrong = Served(files, {n: (200, {}, d + b'x') for n, d in originals.items()})
        self.assertFalse(cf.verify('https://d.p.pages.dev', getter=wrong)['ok'])
        full = cf.verify('https://d.p.pages.dev', full=True, getter=Served(files, nested_404=True))
        self.assertTrue(full['ok'], full)
        self.assertIn('packed_members', {c['check'] for c in full['checks']})
        name = next(n for n in files if n.endswith('analysis-storymap-agents-01.json'))
        pack = json.loads(files[name])
        pack['files'][next(iter(pack['files']))]['content'] += 'x'
        tampered = Served(files, {name: (200, {}, json.dumps(pack).encode())}, nested_404=True)
        result = cf.verify('https://d.p.pages.dev', full=True, getter=tampered)
        self.assertFalse(result['ok'])

    def test_more_groups_are_packed_and_links_into_packs_resolve(self):
        files = {**site(RUN), **agent_files(RUN), **more_files(RUN)}
        files[f'{RUN}/analysis/storymap/index.html'] = (b'<html><a href="agents/agent-001.html">a</a>'
                                                        b'<a href="../../results.html">r</a></html>')
        out, manifest = cf.finish(files, {}, {'source': {}})
        self.assertFalse([n for n in out if cf.packable(n)])
        self.assertEqual(cf.unpack_run(RUN, out), {**agent_files(RUN), **more_files(RUN)})
        for group in cf.PACK_GROUPS:
            self.assertIn(f'{RUN}/{group}/404.html', out)
        self.assertIn(f'{RUN}/packed/analysis-storymap-agents-01.json', out)
        self.assertIn(f'{RUN}/packed/evaluations-sources-01.json', out)
        self.assertEqual(out[f'{RUN}/analysis/storymap/index.html'], files[f'{RUN}/analysis/storymap/index.html'])
        # the site's state round-trips: unpacking the served files gives back every original
        self.assertEqual(cf.unpack_site({n: d for n, d in out.items() if n not in cf.GENERATED}),
                         {n: d for n, d in out.items() if n not in cf.GENERATED and not cf.is_pack_file(n)
                          and '/packed/' not in n and not n.endswith('/404.html')} | agent_files(RUN)
                         | more_files(RUN))
        result = cf.verify('https://d.p.pages.dev', getter=Served(out, nested_404=True))
        self.assertTrue(result['ok'], result)
        broken = dict(out)
        broken[f'{RUN}/analysis/storymap/index.html'] = b'<html><a href="agents/agent-999.html">a</a></html>'
        broken, _ = cf.finish({n: d for n, d in broken.items() if n not in cf.GENERATED}, {}, {'source': {}})
        result = cf.verify('https://d.p.pages.dev', getter=Served(broken, nested_404=True))
        self.assertEqual({c['check'] for c in result['checks'] if not c['ok']}, {'no_broken_links'})

    def test_page_groups_use_smaller_parts(self):
        pages = {f'{RUN}/analysis/storymap/agents/agent-{i:03d}.html': b'<html>' + b'x' * 50 + b'</html>'
                 for i in range(10)}
        with patch.object(cf, 'PAGE_PART_BYTES', 300):
            out = cf.pack_run(RUN, pages)
        self.assertGreater(sum(cf.is_pack_file(n) for n in out), 3)
        self.assertEqual(cf.unpack_run(RUN, out), pages)

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_storymap_on_a_packed_site(self, _):
        old = {f'{RUN}/analysis/storymap/agents/agent-00{i}.html': b'<html>old</html>' for i in (1, 2)}
        page = (b'<html><nav class="tabs"><a href="inspect-view/index.html">Inspect</a></nav>'
                b'<a href="results.json">r</a></html>')
        served, _ = cf.finish({**site(RUN), f'{RUN}/results.html': page, **old,
                               f'{RUN}/analysis/storymap/index.html': b'<html></html>'}, {}, {'source': {}})
        base = cf.unpack_site({n: d for n, d in served.items() if n not in cf.GENERATED})
        self.assertEqual({n: base[n] for n in old}, old)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'agents').mkdir()
            Path(tmp, 'index.html').write_text('<a href="agents/agent-001.html">a</a>')
            Path(tmp, 'agents', 'agent-001.html').write_text('<html>new</html>')
            with patch.object(cf.storymap_site, 'prompt_lines', return_value=frozenset()):
                files, plan = cf.plan_storymap(base, (), {RUN: tmp})
        out, _ = cf.finish(files, {}, plan)
        restored = cf.unpack_run(RUN, out)
        self.assertEqual(restored, {f'{RUN}/analysis/storymap/agents/agent-001.html': b'<html>new</html>'})
        self.assertNotIn(f'{RUN}/analysis/storymap/agents/agent-002.html', out)

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_storymap_graph_shell_gets_the_shared_viewer(self, _):
        """Owner, November 6, 2026: graph.html is a shell + graph.json per run; the site serves one shared viewer
        (_graph/), which is not a run, is refreshed on deployment and satisfies the shell's links."""
        import storymap_graph as sg
        page = (b'<html><nav class="tabs"><a href="inspect-view/index.html">Inspect</a></nav>'
                b'<a href="results.json">r</a></html>')
        base = {**site(RUN), f'{RUN}/results.html': page}
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'index.html').write_text('<a href="graph.html">g</a>')
            Path(tmp, 'graph.html').write_text(sg.shell())
            Path(tmp, 'graph.json').write_text('{"schema": "storymap-graph/1", "nodes": []}')
            with patch.object(cf.storymap_site, 'prompt_lines', return_value=frozenset()):
                files, plan = cf.plan_storymap(base, (), {RUN: tmp})
        self.assertIn(f'{RUN}/analysis/storymap/graph.json', files)
        out, manifest = cf.finish(files, {}, plan)
        for name in ('_graph/viewer.js', '_graph/viewer.css'):
            self.assertIn(name, out)
            self.assertIn(name, manifest['files'])
        self.assertEqual(manifest['runs'], [RUN])  # _graph is not a run
        self.assertEqual(cf.broken_links(cf.unpack_site(out), [f'{RUN}/analysis/storymap/graph.html']), [])
        stale = {**out, '_graph/viewer.js': b'old'}
        again, _ = cf.finish({n: d for n, d in stale.items() if n not in cf.GENERATED}, {}, {'source': {}})
        self.assertEqual(again['_graph/viewer.js'], out['_graph/viewer.js'])  # refreshed from the template
        plain, _ = cf.finish(site(RUN), {}, {'source': {}})
        self.assertFalse(any(n.startswith('_graph/') for n in plain))  # no graph on the site: no viewer

    def test_unpack_refuses_conflicting_copies(self):
        out, _ = cf.finish({**site(RUN), **agent_files(RUN)}, {}, {'source': {}})
        name = next(iter(agent_files(RUN)))
        with self.assertRaises(ValueError):
            cf.unpack_site({**out, name: b'different'})


class LinkGuardTests(unittest.TestCase):
    """Owner, November 3, 2026: a push run's pages linked analysis/timeline.md and ../analysis/timeline.jsonl, files
    the run never produced (its supervisor died before the timeline step)."""
    PAGE = (b'<html><a href="analysis/timeline.md">timeline</a> <a href="results.json">r</a> '
            b'<a href="https://example.org/x">x</a> <a href="#top">t</a></html>')

    def test_private_links_repointed_to_the_notice(self):
        files = {**site(RUN), f'{RUN}/results.html': self.PAGE,
                 f'{RUN}/conference-view/index.html': b'<a href="../analysis/timeline.jsonl">t</a>'}
        self.assertEqual({(p, u) for p, u, _ in cf.broken_links(files)},
                         {(f'{RUN}/results.html', 'analysis/timeline.md'),
                          (f'{RUN}/conference-view/index.html', '../analysis/timeline.jsonl')})
        fixed, changed = cf.repoint_private_links(files)
        self.assertEqual(len(changed), 2)
        self.assertIn(b'href="../private-records.html"', fixed[f'{RUN}/results.html'])
        self.assertIn(b'href="../../private-records.html"', fixed[f'{RUN}/conference-view/index.html'])
        self.assertEqual(cf.broken_links(fixed), [])
        out, _ = cf.finish(files, {}, {'source': {}})  # every deployment repoints them
        self.assertEqual(cf.broken_links(cf.unpack_site(out)), [])

    @patch.object(cf, 'render_catalogue', side_effect=fake_catalogue)
    def test_bundle_with_a_broken_link_is_refused(self, _):
        for page, refused in ((self.PAGE, False), (b'<html><a href="analysis/missing.html">m</a></html>', True)):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(refused=refused):
                root, loaded = BundleTests.bundle(None, tmp, {f'{RUN}/results.html': page,
                                                              f'{RUN}/results.json': b'{}'})
                with patch.object(cf, 'load_bundle', return_value=loaded):
                    if refused:
                        with self.assertRaises(ValueError):
                            cf.plan_bundle(site(OTHER), root, unlisted=())
                    else:
                        files, plan = cf.plan_bundle(site(OTHER), root, unlisted=())
                        self.assertEqual([x['target'] for x in plan['repointed_private_links']],
                                         [f'{RUN}/analysis/timeline.md'])
                        self.assertIn(b'../private-records.html', files[f'{RUN}/results.html'])


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
    """A fake Pages host serving ``files``; 404 otherwise (``nested_404``: the nearest folder's 404.html, as
    Pages does)."""
    def __init__(self, files, overrides=None, nested_404=False):
        self.files, self.overrides, self.nested_404 = files, overrides or {}, nested_404

    def __call__(self, url):
        name = url.split('pages.dev/', 1)[1]
        if name in self.overrides:
            return self.overrides[name]
        if name not in self.files:
            parts = name.split('/')[:-1]
            while self.nested_404:
                page = '/'.join(parts + ['404.html'])
                if page in self.files:
                    return 404, {'Content-Type': 'text/html'}, self.files[page]
                if not parts:
                    break
                parts.pop()
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


class CorrectTests(unittest.TestCase):
    """publish_cloudflare.py correct (owner decision, November 4, 2026): named served records of one run replaced."""

    def test_replaces_named_records_and_entry_and_records_digests(self):
        files = site(RUN, OTHER)
        files[f'{RUN}/conference-view/manifest.json'] = b'{"old": 1}'
        with tempfile.TemporaryDirectory() as tmp, patch.object(cf, 'render_catalogue', fake_catalogue):
            new = Path(tmp) / 'results.json'
            new.write_bytes(b'{"fixed": 1}')
            same = Path(tmp) / 'manifest.json'
            same.write_bytes(b'{"old": 1}')
            fixed = {**entry(RUN, '2026-10-20T00:00:00Z'), 'distinct_verified_tasks': 16}
            out, plan = cf.plan_correct(files, (), RUN, {'results.json': new, 'conference-view/manifest.json': same},
                                        fixed)
        self.assertEqual(out[f'{RUN}/results.json'], b'{"fixed": 1}')
        self.assertEqual([r['path'] for r in plan['replaced_records']], [f'{RUN}/results.json'])
        self.assertEqual(plan['catalogue_entry']['fields'], ['distinct_verified_tasks'])
        rows = json.loads(out['experiments.json'])['experiments']
        self.assertEqual([e.get('distinct_verified_tasks') for e in rows if e['run'] == RUN], [16])
        self.assertEqual({n for n in out if out[n] != files.get(n)},
                         {f'{RUN}/results.json', 'experiments.json', 'index.html'} - (
                             {'index.html'} if out['index.html'] == files['index.html'] else set()))

    def test_refuses_pages_unserved_paths_other_runs_and_foreign_entries(self):
        files = site(RUN)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'x'
            local.write_bytes(b'x')
            for records, fixed in (({'results.html': local}, None), ({'missing.json': local}, None),
                                   ({}, {**entry(OTHER, '2026-10-20T00:00:00Z')}),
                                   ({}, {'run': RUN, 'start_utc': '2026-10-20T00:00:00Z',
                                         'links': {'Report': f'{OTHER}/results.html'}}), ({}, None)):
                with self.subTest(records=records, fixed=fixed), self.assertRaises(ValueError):
                    cf.plan_correct(files, (), RUN, records, fixed)
            with self.assertRaises(ValueError):
                cf.plan_correct(files, (), OTHER, {'results.json': local})

    def test_named_pages_replaced_and_records_removed(self):
        files = site(RUN, OTHER)
        files[f'{RUN}/faking-heatmap.png'] = b'\x89PNG old'
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / 'results.html'
            page.write_bytes(b'<html>fixed</html>')
            out, plan = cf.plan_correct(files, (), RUN, {}, pages={'results.html': page},
                                        remove=['faking-heatmap.png'])
        self.assertEqual(out[f'{RUN}/results.html'], b'<html>fixed</html>')
        self.assertNotIn(f'{RUN}/faking-heatmap.png', out)
        self.assertEqual([r['path'] for r in plan['replaced_records']], [f'{RUN}/results.html'])
        self.assertEqual([r['path'] for r in plan['removed_records']], [f'{RUN}/faking-heatmap.png'])
        self.assertEqual({n for n in set(out) | set(files) if out.get(n) != files.get(n)},
                         {f'{RUN}/results.html', f'{RUN}/faking-heatmap.png'})

    def test_refuses_unserved_or_non_page_pages_and_page_removal(self):
        files = site(RUN)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'x'
            local.write_bytes(b'x')
            for pages, remove in (({'missing.html': local}, ()), ({'results.json': local}, ()),
                                  ({}, ['results.html']), ({}, ['missing.png'])):
                with self.subTest(pages=pages, remove=remove), self.assertRaises(ValueError):
                    cf.plan_correct(files, (), RUN, {}, pages=pages, remove=remove)
