"""Log viewer on a separate origin with HF sign-in (owner approval, November 12, 2026): scripts/log_viewer.py and
its wiring in scripts/publish_cloudflare.py. Offline: fake getter and runner; no login, network or deploy."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import dataset_logs  # noqa: E402
import log_viewer  # noqa: E402
import publish_cloudflare as cf  # noqa: E402

RUN = 'r1-20261027T000000Z'
OFFLINE = 'r2-20261028T000000Z'
COMMIT = 'b' * 40
DATASET = 'org-x/logs-x'
RECORDS = f'https://huggingface.co/datasets/{DATASET}/tree/{COMMIT}/{RUN}/inspect'
THEME = '"use strict";(()=>{document.documentElement.dataset.t="x"})();'
INSPECT = ('<!doctype html><html><head><meta charset="utf-8"><link rel="icon" href="./assets/favicon.svg" />'
           f'<script>{THEME}</script><script type="module">import "./assets/index.js";</script>'
           '<link rel="stylesheet" crossorigin href="./assets/index.css">'
           '<script id="log_dir_context" type="application/json">{"log_dir": "logs"}</script>'
           '</head><body><div id="app"></div></body></html>')


def inspect_files(run, records):
    page = cf.inspect_page(INSPECT, records=records)
    return {f'{run}/inspect-view/index.html': page.encode(),
            f'{run}/inspect-view/assets/index.js': b'console.log(1)',
            f'{run}/inspect-view/assets/index.css': b'body{}',
            f'{run}/inspect-view/assets/favicon.svg': b'<svg/>',
            f'{run}/inspect-view/logs/listing.json': json.dumps({'2026-x.eval': {}}).encode()}


def site():
    files = {'experiments.json': json.dumps({'experiments': [{'run': RUN, 'model': 'm'}, {'run': OFFLINE}],
                                             'warnings': []}).encode()}
    files.update(inspect_files(RUN, RECORDS))
    files.update(inspect_files(OFFLINE, ''))
    for run in (RUN, OFFLINE):
        files[f'{run}/results.html'] = (b'<html><a href="inspect-view/index.html">Inspect</a>'
                                        b'<a href="results.json">r</a></html>')
        files[f'{run}/analysis/incident/report.html'] = (
            b'<a href="../../inspect-view/index.html#/logs/2026-x.eval/samples/sample/a/1">t</a>'
            b'<a href="../../inspect-view/">v</a><a class="x" href=\'../../inspect-view/index.html?q=1#/z\'>q</a>')
    files['experiments.html'] = (f'<a href="{RUN}/inspect-view/index.html">i</a>'
                                 f'<a href="{OFFLINE}/inspect-view/index.html">o</a>'
                                 '<img src="r1-20261027T000000Z/inspect-view/index.html">').encode()
    return files


class Settings(unittest.TestCase):
    def test_records_and_shims(self):
        self.assertEqual(log_viewer.settings_from_records(RECORDS), (DATASET, COMMIT, f'{RUN}/inspect'))
        for bad in ('', f'https://huggingface.co/datasets/{DATASET}/tree/main/{RUN}/inspect',
                    f'https://evil.example/datasets/{DATASET}/tree/{COMMIT}/x', RECORDS + '/../../y'):
            self.assertIsNone(log_viewer.settings_from_records(bad))
        hf = dataset_logs.shim_script(path=f'{RUN}/inspect', commit=COMMIT)
        self.assertEqual(log_viewer.page_settings(f'<html><head>{hf}</head></html>')[1:], (COMMIT, f'{RUN}/inspect'))
        self.assertEqual(log_viewer.viewer_runs(site()), {RUN: (DATASET, COMMIT, f'{RUN}/inspect')})


class Links(unittest.TestCase):
    def test_rewrite(self):
        files, changed = cf.apply_viewer(site())
        url = log_viewer.run_url(RUN)
        self.assertIn(f'href="{url}"'.encode(), files[f'{RUN}/results.html'])
        report = files[f'{RUN}/analysis/incident/report.html'].decode()
        self.assertIn(f'href="{url}#/logs/2026-x.eval/samples/sample/a/1"', report)
        self.assertIn(f'href="{url}"', report)
        self.assertIn(f"href='{url}#/z'", report)
        catalogue = files['experiments.html'].decode()
        self.assertIn(f'href="{url}"', catalogue)
        self.assertIn(f'href="{OFFLINE}/inspect-view/index.html"', catalogue)  # logs kept offline: unchanged
        self.assertIn(f'src="{RUN}/inspect-view/index.html"', catalogue)  # only <a href>
        self.assertIn(b'inspect-view/index.html', files[f'{OFFLINE}/results.html'])
        # the fallback Inspect View page links the viewer; the offline run's does not
        self.assertIn(f'data-viewer="{url}"'.encode(), files[f'{RUN}/inspect-view/index.html'])
        self.assertNotIn(b' data-viewer="', files[f'{OFFLINE}/inspect-view/index.html'])
        self.assertEqual(changed, 4)
        again, changed = cf.apply_viewer(files)  # idempotent
        self.assertEqual((again, changed), (files, 0))

    def test_notice_refuses_foreign_viewer(self):
        with self.assertRaises(ValueError):
            cf.notice_shim(RECORDS, 'https://evil.example/r/')


class Build(unittest.TestCase):
    def test_viewer_files(self):
        files, _ = cf.apply_viewer(site())
        viewer, config = log_viewer.build(files, cf.catalogue(files), 'client-id-1234')
        self.assertEqual(sorted(config['runs']), [RUN])
        page = viewer[f'{RUN}/index.html'].decode()
        self.assertNotIn('private-logs', page)
        self.assertNotIn('<script>', page)  # every executable inline script moved to a file
        self.assertIn('src="../auth-fetch.js" data-dataset="org-x/logs-x"', page)
        self.assertIn(f'data-commit="{COMMIT}" data-path="{RUN}/inspect" data-signin="/signin"', page)
        self.assertIn('<script id="log_dir_context" type="application/json">', page)
        self.assertIn('href="../assets/index.css"', page)
        moved = [n for n in viewer if n.startswith('assets/inline-')]
        self.assertEqual(len(moved), 2)
        self.assertIn(b'import "./index.js"', b''.join(viewer[n] for n in moved))
        self.assertIn(THEME.encode(), b''.join(viewer[n] for n in moved))
        self.assertEqual(viewer['assets/index.js'], b'console.log(1)')
        self.assertIn(f'{RUN}/logs/listing.json', viewer)
        self.assertFalse(any(n.startswith(OFFLINE) for n in viewer))
        self.assertFalse(any(n.endswith('.eval') for n in viewer))
        headers = viewer['_headers'].decode()
        for part in ("frame-ancestors 'none'", "script-src 'self' 'unsafe-eval';", 'Referrer-Policy: no-referrer',
                     "connect-src 'self' https://huggingface.co", "base-uri 'none'"):
            self.assertIn(part, headers)
        signin = viewer['signin.html'].decode()
        self.assertIn('data-client-id="client-id-1234"', signin)
        self.assertIn(f'data-redirect-uri="{log_viewer.URL}/signin"', signin)
        self.assertIn(f'data-org-ids="{dataset_logs.ORG_IDS}" data-org-name="org-x"', signin)
        self.assertNotIn('<script>', signin)
        self.assertIn(f'href="{RUN}/"', viewer['index.html'].decode())
        self.assertEqual(json.loads(viewer[log_viewer.CONFIG])['client_id'], 'client-id-1234')

    def test_without_client_id_sign_in_is_disabled(self):
        viewer, config = log_viewer.build(site(), [], None)
        self.assertIn('data-client-id=""', viewer['signin.html'].decode())
        self.assertIsNone(config['client_id'])

    def test_refusals(self):
        files = site()
        with self.assertRaises(ValueError):
            log_viewer.build(files, [], 'bad id with spaces')
        other = dict(files)
        other.update(inspect_files('r3-20261029T000000Z', RECORDS.replace(RUN, 'r3-20261029T000000Z')))
        other['r3-20261029T000000Z/inspect-view/assets/index.js'] = b'different'
        with self.assertRaises(ValueError):
            log_viewer.build(other, [], None)
        external = dict(files)
        external[f'{RUN}/inspect-view/index.html'] = external[f'{RUN}/inspect-view/index.html'].replace(
            b'</head>', b'<script src="https://cdn.example/x.js"></script></head>')
        with self.assertRaises(ValueError):
            log_viewer.build(external, [], None)
        for name, data in (('x/a.eval', b'x'), ('x/l.zip', b'PK\x03\x04'), ('_worker.js', b'x'),
                           ('a.js', b'hf_' + b'a' * 34)):
            with self.assertRaises(ValueError):
                cf.viewer_gate(name, data)
        self.assertEqual(cf.viewer_gate('_headers', b'/*\n'), b'/*\n')

    def test_client_id_source(self):
        with patch.dict(os.environ, {log_viewer.CLIENT_ENV: 'env-client-1234'}):
            self.assertEqual(log_viewer.client_id_from({'client_id': 'served-client'}), 'env-client-1234')
        with patch.dict(os.environ, {log_viewer.CLIENT_ENV: ''}):
            self.assertEqual(log_viewer.client_id_from({'client_id': 'served-client'}), 'served-client')
            self.assertIsNone(log_viewer.client_id_from(None))
            served = {'schema': log_viewer.SCHEMA, 'client_id': 'served-client-1'}
            viewer, summary = cf.plan_viewer(site(), getter=lambda url: (200, {}, json.dumps(served).encode()))
            self.assertTrue(summary['sign_in_configured'])
            self.assertIn(log_viewer.NOT_FOUND, viewer)

            def down(url):
                raise OSError('no route')
            self.assertFalse(cf.plan_viewer(site(), getter=down)[1]['sign_in_configured'])


class Verify(unittest.TestCase):
    def served(self, viewer, headers_ok=True, leak=False):
        def getter(url):
            name = url[len(log_viewer.URL) + 1:]
            headers = {'Content-Security-Policy': log_viewer.CSP, 'Referrer-Policy': 'no-referrer'} if headers_ok else {}
            if leak and name.endswith('.eval'):
                return 200, {}, b'PK\x03\x04'
            for candidate in (name, name + 'index.html'):
                if candidate in viewer:
                    return 200, headers, viewer[candidate]
            return 404, headers, b''
        return getter

    def test_verify(self):
        viewer, _ = log_viewer.build(site(), [], 'client-id-1234')
        self.assertTrue(cf.verify_viewer(getter=self.served(viewer))['ok'])
        result = cf.verify_viewer(getter=self.served(viewer, headers_ok=False))
        self.assertFalse(result['ok'])
        self.assertFalse(cf.verify_viewer(getter=self.served(viewer, leak=True))['ok'])


class Commit(unittest.TestCase):
    def test_math_commit_deploys_viewer_first(self):
        calls = []
        files, _ = cf.apply_viewer(site())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cf, 'deployments', return_value=[]), \
                patch.object(cf, 'load_site', return_value=({}, {})), \
                patch.object(cf, 'plan_catalogue', return_value=(files, {'source': {'kind': 'catalogue'}})), \
                patch.object(cf, 'finish', side_effect=lambda f, *a, **k: (f.pop() if k.get('consume') else f, {})), \
                patch.object(cf, 'served_viewer_config', return_value={'schema': log_viewer.SCHEMA}), \
                patch.object(cf, 'remember'), \
                patch.object(cf, 'deploy_viewer', side_effect=lambda *a: calls.append('viewer') or {'id': 'v', 'url': 'https://v.x'}), \
                patch.object(cf, 'verify_viewer', return_value={'ok': True}), \
                patch.object(cf, 'deploy', side_effect=lambda *a, **k: calls.append('site') or ({'id': 'n', 'url': 'https://n.x'}, '')), \
                patch.object(cf, 'verify', return_value={'ok': True}), \
                patch.object(cf.deploy_lock, 'held'), patch('builtins.print') as printed:
            self.assertEqual(cf.main(['catalogue', '--commit', '--workdir', tmp]), 0)
        self.assertEqual(calls, ['viewer', 'site'])
        out = json.loads(printed.call_args.args[0])
        self.assertEqual(out['viewer']['runs'], 1)

    def test_no_viewer_until_created(self):
        # Before the ``viewer`` command created it, other commands neither deploy it nor link it.
        calls = []
        files, _ = cf.apply_viewer(site())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cf, 'deployments', return_value=[]), \
                patch.object(cf, 'load_site', return_value=({}, {})), \
                patch.object(cf, 'plan_catalogue', return_value=(site(), {'source': {'kind': 'catalogue'}})), \
                patch.object(cf, 'served_viewer_config', return_value=None), \
                patch.object(cf, 'deploy_viewer', side_effect=lambda *a: calls.append('viewer')), \
                patch('builtins.print') as printed:
            self.assertEqual(cf.main(['catalogue', '--workdir', tmp]), 0)
        self.assertEqual(calls, [])
        self.assertNotIn('viewer', json.loads(printed.call_args.args[0]))

    def test_viewer_failure_stops_the_site_deploy(self):
        files, _ = cf.apply_viewer(site())
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cf, 'deployments', return_value=[]), \
                patch.object(cf, 'load_site', return_value=({}, files)), \
                patch.object(cf, 'finish', side_effect=lambda f, *a, **k: (f.pop() if k.get('consume') else f, {})), \
                patch.object(cf, 'served_viewer_config', return_value=None), \
                patch.object(cf, 'remember'), \
                patch.object(cf, 'deploy_viewer', return_value={'id': 'v', 'url': 'https://v.x'}), \
                patch.object(cf, 'verify_viewer', return_value={'ok': False}), \
                patch.object(cf, 'deploy') as deploy, patch.object(cf.deploy_lock, 'held'), patch('builtins.print'):
            with self.assertRaises(RuntimeError):
                cf.main(['viewer', '--commit', '--workdir', tmp])
        deploy.assert_not_called()

    def test_project_created_once(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            class R:
                returncode = 0
                stdout = '[]'
                stderr = ''
            if 'deployment' in command and len([c for c in calls if 'deployment' in c]) == 1:
                R.returncode, R.stdout, R.stderr = 1, '', 'Project not found. [code: 8000007]'
            return R
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cf.ensure_viewer_project(tmp, runner), [])
        self.assertTrue(any('create' in c and log_viewer.PROJECT in c for c in calls))


if __name__ == '__main__':
    unittest.main()
