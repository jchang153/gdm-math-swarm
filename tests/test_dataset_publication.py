"""Inspect logs and raw records go to the private logs dataset, not the Space (owner, October 22, 2026).
Offline: mocked Hub calls only, no HF login, network or model calls."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import dataset_logs  # noqa: E402
import prepare_hf_library  # noqa: E402
from publish_hf_library import DEFAULT_REPO, digest, load_bundle, publish  # noqa: E402
import verify_hosted  # noqa: E402
from test_judge_publication import make_run, make_viewer  # noqa: E402

RUN = 'r1-20261022T000000Z'
LOG = '2026-10-22T00-00-00-00-00_gdm-math-swarm_abc.eval'
ZSTD = patch.dict(sys.modules, {'zipfile_zstd': SimpleNamespace()})


def eval_bytes():
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('header.json', '{"status": "success"}' + ' ' * 200)
    return raw.getvalue()


def git_sha1(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class Repo:
    def __init__(self, kind, private=True):
        self.kind, self.files, self.revision, self.private, self.commits = kind, {}, 0, private, []

    @property
    def sha(self):  # dataset commits are full hex ids (they are pinned into pages)
        return f'{self.revision:040x}' if self.kind == 'dataset' else str(self.revision)


class TwoRepoHub:
    """A Space (public) and a dataset (private), as recorded in scripts/hf_visibility.py; every call names its
    repo_type."""

    def __init__(self, root):
        self.root = root
        self.repos = {'space': Repo('space', private=False), 'dataset': Repo('dataset')}
        self.order = []

    def repo_info(self, repo, repo_type='model', **kwargs):
        r = self.repos[repo_type]
        return SimpleNamespace(private=r.private, sha=r.sha, siblings=[])

    def list_repo_tree(self, repo, *, repo_type, revision, recursive):
        r = self.repos[repo_type]
        assert revision == r.sha
        for n, d in r.files.items():
            big = n.endswith('.eval') or n.endswith('.jsonl')
            yield SimpleNamespace(path=n, size=len(d), blob_id=git_sha1(d),
                                  lfs=SimpleNamespace(sha256=hashlib.sha256(d).hexdigest()) if big else None)

    def download(self, repo, name, repo_type='space', revision=None, **kwargs):
        f = self.root / 'cache' / repo_type / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(self.repos[repo_type].files[name])
        return f

    def create_commit(self, *, repo_id, repo_type, parent_commit, commit_message, operations):
        r = self.repos[repo_type]
        assert parent_commit == r.sha
        for op in operations:
            d = op.path_or_fileobj
            r.files[op.path_in_repo] = Path(d).read_bytes() if isinstance(d, str) else d
        r.revision += 1
        r.commits.append(commit_message)
        self.order.append(repo_type)
        return SimpleNamespace(oid=r.sha)


def make_dataset_run(root):
    run = make_run(root / 'runs', RUN)
    (run / 'inspect').mkdir()
    (run / 'inspect' / LOG).write_bytes(eval_bytes())
    (run / 'runtime.jsonl').write_text('{"event": "common_start"}\n')
    (run / 'conference').mkdir()
    (run / 'conference' / 'events.jsonl').write_text('{"e": 1}\n')
    (run / 'conference' / 'state.sqlite').write_bytes(b'SQLite format 3\0')
    (run / 'conference-view' / 'index.html').write_text(
        '<!doctype html><html><head></head><body><a href="../conference/events.jsonl">Conference events</a> '
        '<a href="../results.html">Run report</a></body></html>')
    (run / 'snapshots' / 'agent-001').mkdir(parents=True)
    (run / 'snapshots' / 'agent-001' / ('a' * 64)).write_text('snapshot')
    (run / 'analysis').mkdir(exist_ok=True)
    (run / 'analysis' / 'timeline.md').write_text('# Timeline\n\n- 00:00 start\n')
    (run / 'results.html').write_text('<!doctype html><html><head></head><body><h1>R</h1><p>'
                                      '<a href="analysis/timeline.md">Timeline</a></p></body></html>')
    viewer = make_viewer(root)
    (viewer / 'logs' / LOG).write_bytes(eval_bytes())
    return run, viewer


def prepare(root, viewer, name='bundle', *extra):
    output = root / name
    argv = ['prepare_hf_library.py', '--runs-dir', str(root / 'runs'), '--output-dir', str(output),
            '--run', f'{RUN}={viewer}', *extra]
    with patch.object(sys, 'argv', argv), patch('builtins.print'), ZSTD:
        prepare_hf_library.main()
    return output


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run, self.viewer = make_dataset_run(self.root)

    def test_dataset_files_leave_the_space_bundle(self):
        output = prepare(self.root, self.viewer)
        data = json.loads((output / 'dataset-files' / 'manifest.json').read_text())
        self.assertEqual(data['schema'], dataset_logs.DATASET_SCHEMA)
        files = data['files']
        log = f'{RUN}/inspect/{LOG}'
        self.assertEqual(sorted(files[log]['sources']), [f'inspect-view/logs/{LOG}', f'inspect/{LOG}'])
        self.assertEqual(files[log]['category'], 'eval')
        for name in (f'{RUN}/runtime.jsonl', f'{RUN}/conference/events.jsonl', f'{RUN}/conference/state.sqlite',
                     f'{RUN}/snapshots.tar.gz', f'{RUN}/snapshots.manifest.json', f'{RUN}/analysis/timeline.md'):
            self.assertIn(name, files)
            self.assertTrue((output / 'dataset-files' / name).is_file(), name)
        manifest = json.loads((output / 'archive-manifest.json').read_text())
        self.assertFalse([n for n in manifest['files'] if dataset_logs.category(n.split('/', 1)[-1])])
        self.assertEqual(manifest['dataset_files'][f'{RUN}/inspect-view/logs/{LOG}']['path'], log)
        self.assertEqual(manifest['dataset_repo'], dataset_logs.DATASET_REPO)
        self.assertFalse((output / RUN / 'inspect').exists())
        # The catalogue entry keeps its (hidden) Inspect log link, as published entries have it.
        entry = json.loads((output / 'experiments.json').read_text())['experiments'][0]
        self.assertIn(f'{RUN}/inspect/{LOG}', entry['links'].values())
        # The timeline lives only in the dataset (owner, October 23, 2026): the run page links the dataset file;
        # the catalogue entry keeps its Timeline link as data, but the catalogue page no longer shows it.
        self.assertEqual(files[f'{RUN}/analysis/timeline.md']['category'], 'timeline')
        self.assertFalse((output / RUN / 'analysis' / 'timeline.md').exists())
        report = (output / RUN / 'results.html').read_text()
        self.assertIn(f'/blob/{dataset_logs.COMMIT_PLACEHOLDER}/{RUN}/analysis/timeline.md"', report)
        self.assertEqual(dataset_logs.moved_links(report, 'results.html', {'analysis/timeline.md'}), [])
        self.assertEqual(entry['links']['Timeline'], f'{RUN}/analysis/timeline.md')
        self.assertNotIn('analysis/timeline.md">', (output / 'index.html').read_text())
        with ZSTD:
            load_bundle(output)

    def test_shim_first_signin_page_and_repointed_links(self):
        output = prepare(self.root, self.viewer)
        page = (output / RUN / 'inspect-view' / 'index.html').read_text()
        head = page.index('<head>') + len('<head>')
        self.assertTrue(page[head:].startswith('<script id="evallog-fetch"'))
        self.assertLess(page.index('id="evallog-fetch"'), page.index('id="private-space-navigation"'))
        self.assertEqual(dataset_logs.shim_settings(page),
                         (dataset_logs.DATASET_REPO, dataset_logs.COMMIT_PLACEHOLDER, f'{RUN}/inspect'))
        signin = (output / 'signin.html').read_text()
        self.assertIn(dataset_logs.ORG_IDS, signin)
        self.assertIn('id="private-space-navigation"', signin)
        conference = (output / RUN / 'conference-view' / 'index.html').read_text()
        self.assertIn(f'/blob/{dataset_logs.COMMIT_PLACEHOLDER}/{RUN}/conference/events.jsonl"', conference)
        self.assertIn('data-dataset-file="conference/events.jsonl"', conference)
        self.assertEqual(dataset_logs.moved_links(conference, 'conference-view/index.html',
                                                  {'conference/events.jsonl'}), [])

    def test_credential_in_a_dataset_bound_record_refused(self):
        (self.run / 'runtime.jsonl').write_text('{"key": "hf_' + 'A' * 32 + '"}\n')  # fabricated fixture
        with self.assertRaisesRegex(ValueError, 'Credential-like'):
            prepare(self.root, self.viewer)

    def test_public_placeholder_stays_exempt_in_logs(self):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as z:
            z.writestr('sample.json', '{"apiKeyHelper": "echo sk-ant-api03-DOq5tyLPrk9M4hPE"}')
        (self.run / 'inspect' / LOG).write_bytes(raw.getvalue())
        (self.viewer / 'logs' / LOG).write_bytes(raw.getvalue())
        output = prepare(self.root, self.viewer)
        self.assertTrue((output / 'dataset-files' / RUN / 'inspect' / LOG).is_file())


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run, self.viewer = make_dataset_run(self.root)
        self.bundle = prepare(self.root, self.viewer)
        self.hub = TwoRepoHub(self.root)
        self.hub.repos['space'].files['README.md'] = b'original Space settings'

    def publish(self, **kwargs):
        with ZSTD:
            return publish(self.bundle, self.hub, self.hub.download, operation=lambda **kw: SimpleNamespace(**kw),
                           unlisted=(), **kwargs)

    def test_dataset_first_then_space_with_pinned_commit(self):
        plan = self.publish()
        self.assertEqual(plan['dataset']['committed'], False)
        self.assertEqual(self.hub.order, [])
        result = self.publish(commit=True)
        self.assertEqual(self.hub.order[0], 'dataset')
        self.assertTrue(set(self.hub.order[1:]) == {'space'})
        dataset = self.hub.repos['dataset'].files
        space = self.hub.repos['space'].files
        self.assertIn(f'{RUN}/inspect/{LOG}', dataset)
        self.assertFalse([n for n in space if n.endswith(('.eval', '.jsonl', '.sqlite', 'timeline.md'))])
        self.assertIn(f'{RUN}/analysis/timeline.md', dataset)
        commit = result['dataset']['revision']
        self.assertIn(f'/blob/{commit}/{RUN}/analysis/timeline.md"', space[f'{RUN}/results.html'].decode())
        page = space[f'{RUN}/inspect-view/index.html'].decode()
        self.assertEqual(dataset_logs.shim_settings(page)[1], commit)
        self.assertNotIn(dataset_logs.COMMIT_PLACEHOLDER, page)
        self.assertIn(f'/blob/{commit}/{RUN}/conference/events.jsonl', space[f'{RUN}/conference-view/index.html'].decode())
        self.assertIn('signin.html', space)
        self.assertEqual(space['README.md'], b'original Space settings')
        manifest = json.loads(space['archive-manifest.json'])
        for name, expected in manifest['files'].items():
            self.assertEqual(digest(space[name]), expected, name)
        self.assertEqual(manifest['dataset_files'][f'{RUN}/runtime.jsonl']['commit'], commit)

    def test_eval_bound_for_the_space_refused(self):
        target = self.bundle / RUN / 'inspect' / LOG
        target.parent.mkdir(parents=True)
        target.write_bytes(eval_bytes())
        manifest = json.loads((self.bundle / 'archive-manifest.json').read_text())
        manifest['files'][f'{RUN}/inspect/{LOG}'] = digest(target.read_bytes())
        (self.bundle / 'archive-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'bound for the logs dataset'):
            self.publish(commit=True)
        self.assertEqual(self.hub.order, [])

    def test_public_dataset_refused_before_any_write(self):
        self.hub.repos['dataset'].private = False
        with self.assertRaisesRegex(ValueError, 'dataset .* is public, but its recorded visibility is private'):
            self.publish(commit=True)
        self.assertEqual(self.hub.order, [])

    def test_private_space_refused_before_any_write(self):
        self.hub.repos['space'].private = True
        with self.assertRaisesRegex(ValueError, 'space .* is private, but its recorded visibility is public'):
            self.publish(commit=True)
        self.assertEqual(self.hub.order, [])

    def test_legacy_layout_bundle_refused_unless_requested(self):
        legacy = prepare(self.root, self.viewer, 'legacy', '--records-in-space')
        self.assertTrue((legacy / RUN / 'runtime.jsonl').is_file())
        with ZSTD, self.assertRaisesRegex(ValueError, 'Legacy records-in-space'):
            publish(legacy, self.hub, self.hub.download, operation=lambda **kw: SimpleNamespace(**kw), unlisted=())

    def test_tampered_dataset_file_refused(self):
        (self.bundle / 'dataset-files' / RUN / 'runtime.jsonl').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Dataset file checksum mismatch'):
            self.publish(commit=True)


class Response:
    def __init__(self, status, content=b'', headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


class VerifyTests(unittest.TestCase):
    COMMIT = 'c' * 40

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = eval_bytes()
        shim = dataset_logs.with_log_shim('<!doctype html><html><head></head><body></body></html>',
                                          path=f'{RUN}/inspect', commit=self.COMMIT)
        events = dataset_logs.file_url(f'{RUN}/conference/events.jsonl', self.COMMIT)
        self.space = {
            'index.html': f'<a href="{RUN}/results.html">r</a>'.encode(),
            f'{RUN}/results.html': b'<a href="conference-view/index.html">c</a><a href="../index.html">all</a>',
            f'{RUN}/conference-view/index.html': (
                f'<a href="{events}" target="_blank" rel="noopener noreferrer" '
                f'data-dataset-file="conference/events.jsonl">e</a>').encode(),
            f'{RUN}/inspect-view/index.html': shim.encode(),
            f'{RUN}/inspect-view/logs/listing.json': json.dumps({LOG: {}}).encode(),
            f'{RUN}/inspect/{LOG}': self.log,
            'archive-manifest.json': json.dumps({'dataset_files': {
                f'{RUN}/conference/events.jsonl': {}, f'{RUN}/inspect/{LOG}': {}}}).encode(),
        }
        self.dataset = {f'{RUN}/inspect/{LOG}': self.log, f'{RUN}/conference/events.jsonl': b'{}\n'}
        self.dataset_private = True

    def api(self):
        test = self

        class Api:
            def space_info(self, repo):
                return SimpleNamespace(sha='rev1', private=False, host='https://space.hf.space')

            def repo_info(self, repo, repo_type):
                assert repo_type == 'dataset'
                return SimpleNamespace(private=test.dataset_private, sha=test.COMMIT)

            def list_repo_tree(self, repo, repo_type, revision, recursive, path_in_repo=None):
                assert repo_type == 'space'
                return [SimpleNamespace(path=n, blob_id='x') for n in test.space
                        if path_in_repo is None or n.startswith(path_in_repo + '/')]

            def get_paths_info(self, repo, paths, repo_type, revision):
                store = test.space if repo_type == 'space' else test.dataset
                return [SimpleNamespace(path=p, size=len(store[p]), lfs=None) for p in paths if p in store]

            def hf_hub_download(self, repo, name, repo_type, revision, cache_dir=None):
                path = test.root / 'dl' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(test.space[name])
                return str(path)
        return Api()

    def session(self):
        test = self

        class Session:
            def get(self, url, headers, timeout, follow_redirects):
                if url.startswith('https://space.hf.space/'):
                    data = test.space[url.split('https://space.hf.space/', 1)[1]]
                else:
                    path = url.split(f'/resolve/{test.COMMIT}/', 1)[1]
                    data = test.dataset[path]
                if headers.get('Range'):
                    start, end = map(int, headers['Range'][6:].split('-'))
                    return Response(206, data[start:end + 1])
                injected = (b'<script>window.huggingface={variables:{"OAUTH_CLIENT_ID":"458e2fee-1789",'
                            b'"OAUTH_SCOPES":"openid profile read-repos","OPENID_PROVIDER_URL":"https://huggingface.co",'
                            b'"SPACE_CREATOR_USER_ID":"683762ddd47e"}};</script>')
                return Response(200, injected + data, {'content-type': 'text/html'})

            def request(self, method, url, timeout, headers):
                return Response(204, headers={'access-control-allow-origin': headers['Origin'],
                                              'access-control-allow-headers': 'authorization,range'})
        return Session()

    def verify(self, **kwargs):
        return verify_hosted.verify(self.api(), self.session(), {}, DEFAULT_REPO, RUN, 'rev1', **kwargs)

    def failed(self, record):
        return sorted({(c['check'], c.get('path')) for c in record['checks'] if not c['ok']})

    def test_creator_script_with_oauth_variables_is_stripped(self):
        script = (b'<script>window.huggingface={variables:{"OAUTH_CLIENT_ID":"x-1","OAUTH_SCOPES":"openid profile '
                  b'read-repos","OPENID_PROVIDER_URL":"https://huggingface.co","SPACE_CREATOR_USER_ID":"0a"}};</script>')
        self.assertEqual(verify_hosted.CREATOR_SCRIPT.sub(b'', script + b'<p>x</p>', count=1), b'<p>x</p>')
        old = b'<script>window.huggingface={variables:{"SPACE_CREATOR_USER_ID":"0a1b"}};</script>'
        self.assertEqual(verify_hosted.CREATOR_SCRIPT.sub(b'', old), b'')
        social = (b'<style>x</style><meta property="og:title" content="GDM Math Swarm Experiments">'
                  b'<meta property="og:image" content="https://cdn-thumbnails.huggingface.co/social-thumbnails/a.png">'
                  b'<meta name="twitter:card" content="summary_large_image">'
                  b'<meta name="twitter:image" content="https://cdn-thumbnails.huggingface.co/social-thumbnails/a.png"></head><body>')
        self.assertEqual(verify_hosted.SOCIAL_META.sub(b'', social, count=1), b'<style>x</style></head><body>')
        headless = social[len(b'<style>x</style>'):].replace(b'</head><body>', b'<meta name="referrer" content="no-referrer">')
        self.assertEqual(verify_hosted.SOCIAL_META.sub(b'', headless, count=1), b'<meta name="referrer" content="no-referrer">')
        own = b'<meta property="og:title" content="mine"><p>body</p>'  # no HF thumbnail: kept
        self.assertEqual(verify_hosted.SOCIAL_META.sub(b'', own, count=1), own)

    def test_all_dataset_checks_pass(self):
        record = self.verify(dataset_commit='ccc')
        self.assertTrue(record['ok'], self.failed(record))
        names = {c['check'] for c in record['checks']}
        self.assertTrue({'dataset_private', 'inspect_shim', 'range_206', 'preflight', 'no_moved_links',
                         'no_broken_links', 'dataset_links_resolve'} <= names)

    def test_wrong_commit_public_dataset_and_space_logs(self):
        self.dataset_private = False
        record = self.verify(dataset_commit='ddd', expect_no_space_logs=True)
        failed = {c for c, _ in self.failed(record)}
        self.assertEqual(failed, {'dataset_private', 'inspect_shim', 'no_space_logs'})

    def test_direct_link_to_moved_file_and_broken_link(self):
        self.space[f'{RUN}/results.html'] = b'<a href="conference/events.jsonl">e</a><a href="missing.html">m</a>'
        record = self.verify()
        self.assertEqual(self.failed(record), [('no_broken_links', f'{RUN}/results.html'),
                                               ('no_moved_links', f'{RUN}/results.html')])

    def test_repointed_link_missing_from_dataset(self):
        del self.dataset[f'{RUN}/conference/events.jsonl']
        record = self.verify()
        self.assertEqual({c for c, _ in self.failed(record)}, {'dataset_links_resolve'})

    def test_range_mismatch_fails(self):
        self.dataset[f'{RUN}/inspect/{LOG}'] = b'Y' * len(self.log)
        record = self.verify()
        self.assertIn(('range_206', f'{RUN}/inspect/{LOG}'), self.failed(record))

    def timeline_link(self):
        url = dataset_logs.file_url(f'{RUN}/analysis/timeline.md', self.COMMIT)
        return (f'<a href="{url}" target="_blank" rel="noopener noreferrer" data-dataset-file="analysis/timeline.md" '
                f'title="Stored in the private logs dataset (sign in to huggingface.co with read access)">Timeline</a>')

    def test_timeline_in_the_dataset_passes(self):
        self.space[f'{RUN}/results.html'] += self.timeline_link().encode()
        self.dataset[f'{RUN}/analysis/timeline.md'] = b'# t\n'
        record = self.verify()
        self.assertTrue(record['ok'], self.failed(record))
        check = next(c for c in record['checks'] if c['check'] == 'timeline_link')
        self.assertEqual((check['links'], check['dataset']), (1, 1))

    def test_timeline_left_in_the_space_or_linked_directly_fails(self):
        self.space[f'{RUN}/analysis/timeline.md'] = b'# t\n'
        self.space[f'{RUN}/results.html'] += b'<a href="analysis/timeline.md">Timeline</a>'
        record = self.verify()
        self.assertEqual({c for c, _ in self.failed(record)}, {'no_space_timeline', 'timeline_link'})

    def test_timeline_link_missing_from_the_dataset_fails(self):
        self.space[f'{RUN}/results.html'] += self.timeline_link().encode()
        record = self.verify()
        self.assertEqual({c for c, _ in self.failed(record)}, {'dataset_links_resolve'})

    def test_timeline_kept_offline_links_the_notice(self):
        self.space[f'{RUN}/results.html'] += (b'<aside id="raw-records-offline"></aside><a href="results.html#raw-records-'
                                             b'offline" data-offline-file="analysis/timeline.md">Timeline</a>')
        record = self.verify()
        self.assertTrue(record['ok'], self.failed(record))


if __name__ == '__main__':
    unittest.main()
