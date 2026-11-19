"""``publish_cloudflare.py dataset-rename`` (owner decision, November 19, 2026: the private HF logs dataset moved to
the owner's account with its commits unchanged): only the dataset part of its links changes, on every page, and
the log viewer follows. Offline: placeholders, no login, network or deploy."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import log_viewer  # noqa: E402
import publish_cloudflare as cf  # noqa: E402
import test_log_viewer as tl  # noqa: E402

OLD, NEW = 'org/logs', 'user/logs'
COMMIT = 'c' * 40


def renamed_site():
    files = {n: d.replace(tl.DATASET.encode(), OLD.encode()) for n, d in tl.site().items()}
    files[f'{tl.RUN}/results.html'] += (f'<a href="https://huggingface.co/datasets/{OLD}/blob/{COMMIT}/{tl.RUN}/'
                                        f'raw/x.jsonl">raw</a><a href="https://huggingface.co/datasets/{OLD}">d</a>'
                                        ).encode()
    escaped = 'https://huggingface.co/datasets/' + OLD + '/tree'  # JSON with escaped slashes (PHP-style encoders)
    files[f'{tl.RUN}/results.json'] = (json.dumps(
        {'records': f'https://huggingface.co/datasets/{OLD}/tree/{COMMIT}/{tl.RUN}'})[:-1]
        + ', "escaped": "' + escaped.replace('/', '\\/') + '"}').encode()
    files[f'{tl.RUN}/figure.png'] = b'\x89PNG\r\n\x1a\n\x00' + f'huggingface.co/datasets/{OLD}/'.encode()
    files[f'{tl.RUN}/latin1.txt'] = 'caf\xe9'.encode('latin-1')
    files[f'{tl.RUN}/about.html'] = b'<p>logs-dataset notes: org/logsy and https://huggingface.co/datasets/org/logs-2/</p>'
    return files


class Plan(unittest.TestCase):
    def test_only_dataset_links_change(self):
        base = renamed_site()
        files, plan = cf.plan_dataset_rename(base, OLD, NEW)
        self.assertEqual(plan['source'], {'kind': 'dataset-rename', 'from': OLD, 'to': NEW})
        changed = sorted(r['path'] for r in plan['replaced_pages'])
        self.assertEqual(changed, sorted([f'{tl.RUN}/inspect-view/index.html', f'{tl.RUN}/results.html',
                                          f'{tl.RUN}/results.json']))
        self.assertEqual(plan['renamed_files'], 3)
        self.assertEqual(plan['inspect_pages_renamed'], 1)
        self.assertEqual(plan['skipped_binary_files'], 2)
        for record in plan['replaced_pages']:
            self.assertEqual(record['old'], cf.digest(base[record['path']]))
            self.assertEqual(record['new'], cf.digest(files[record['path']]))
        results = files[f'{tl.RUN}/results.html'].decode()
        self.assertIn(f'https://huggingface.co/datasets/{NEW}/blob/{COMMIT}/', results)
        self.assertIn(f'href="https://huggingface.co/datasets/{NEW}"', results)
        record = json.loads(files[f'{tl.RUN}/results.json'])
        self.assertEqual(record['records'], f'https://huggingface.co/datasets/{NEW}/tree/{COMMIT}/{tl.RUN}')
        self.assertEqual(record['escaped'], f'https://huggingface.co/datasets/{NEW}/tree')
        self.assertIn(b'datasets\\/user\\/logs\\/tree', files[f'{tl.RUN}/results.json'])
        for name in (f'{tl.RUN}/figure.png', f'{tl.RUN}/latin1.txt', f'{tl.RUN}/about.html'):
            self.assertEqual(files[name], base[name])  # binary, not UTF-8, other datasets: untouched
        self.assertEqual(log_viewer.viewer_runs(files), {tl.RUN: (NEW, tl.COMMIT, f'{tl.RUN}/inspect')})
        self.assertEqual(log_viewer.viewer_runs(base)[tl.RUN][0], OLD)
        again, plan = cf.plan_dataset_rename(files, OLD, NEW)  # nothing left to rename: a plan of zero
        self.assertEqual((again, plan['renamed_files']), (files, 0))

    def test_viewer_follows(self):
        files, _ = cf.plan_dataset_rename(renamed_site(), OLD, NEW)
        files, _ = cf.apply_viewer(files)
        viewer, config = log_viewer.build(files, cf.catalogue(files), 'client-id-1234')
        self.assertEqual(config['runs'][tl.RUN]['dataset'], NEW)
        self.assertIn(f'data-dataset="{NEW}"', viewer[f'{tl.RUN}/index.html'].decode())
        self.assertIn('data-org-ids="" data-org-name=""', viewer['signin.html'].decode())

    def test_refusals(self):
        base = renamed_site()
        for old, new in ((OLD, OLD), ('org', NEW), (OLD, 'user/logs/x'), (OLD, '../logs'), ('', NEW)):
            with self.assertRaises(ValueError):
                cf.plan_dataset_rename(base, old, new)
        for leftover in (f'http://huggingface.co/datasets/{OLD}/x', f'//huggingface.co/datasets/{OLD}/x'):
            other = dict(base)
            other['experiments.html'] += leftover.encode()  # a link the rename does not cover: refused
            with self.assertRaises(ValueError):
                cf.plan_dataset_rename(other, OLD, NEW)


class Command(unittest.TestCase):
    def test_plan_only(self):
        base = renamed_site()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cf, 'deployments', return_value=[]), \
                patch.object(cf, 'load_site', return_value=({}, base)), \
                patch.object(cf, 'finish', side_effect=lambda f, *a, **k: (f.pop() if k.get('consume') else f, {})), \
                patch.object(cf, 'served_viewer_config', return_value={'schema': log_viewer.SCHEMA}), \
                patch.object(cf, 'deploy_viewer') as deploy_viewer, patch.object(cf, 'deploy') as deploy, \
                patch('builtins.print') as printed:
            receipt = Path(tmp) / 'receipt.json'
            self.assertEqual(cf.main(['dataset-rename', '--from', OLD, '--to', NEW, '--workdir', tmp,
                                      '--receipt', str(receipt)]), 0)
            out = json.loads(receipt.read_text())
        deploy.assert_not_called()
        deploy_viewer.assert_not_called()
        self.assertFalse(out['committed'])
        self.assertEqual(out['renamed_files'], 3)
        self.assertEqual(out['viewer']['runs'], 1)
        self.assertEqual(json.loads(printed.call_args.args[0])['source']['to'], NEW)

    def test_arguments(self):
        for argv in (['dataset-rename', '--from', OLD, '--to', OLD], ['dataset-rename', '--from', 'x', '--to', NEW],
                     ['dataset-rename', '--to', NEW]):
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                cf.main(argv)


if __name__ == '__main__':
    unittest.main()
