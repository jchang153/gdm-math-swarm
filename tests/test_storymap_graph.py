"""Contract tests for scripts/storymap_graph.py (schema storymap-graph/1) on the shared synthetic story-map run.

docs/plan.md, "Story-map interaction graph (November 5, 2026)". The fixture run is
research/analysis/tests/fixtures/storymap/build_fixture.py (seed agent-101, overseer agent-102, solvers
agent-001..003); its conference events are replaced by a hand-written set covering every edge type the contract
names. No network, model calls or browser.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (REPO, REPO / 'scripts', REPO / 'src'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import storymap_graph as sg  # noqa: E402
from research.analysis.storymap import view  # noqa: E402

FIX = REPO / 'research' / 'analysis' / 'tests' / 'fixtures' / 'storymap'
TOP_KEYS = {'schema', 'run', 'minutes', 'title', 'nodes', 'states', 'thoughts', 'events'}  # required; more may follow
EVENT_KEYS = {'t', 'type', 'src', 'dst', 'fake', 'problem', 'text'}
STATE_KEYS = {'t_from', 't_to', 'stance', 'has_trick', 'believes_judge', 'summary'}
HOSTILE = '</script><script>alert(1)</script>'


def _fixture():
    spec = importlib.util.spec_from_file_location('storymap_build_fixture_graph', FIX / 'build_fixture.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_run(root):
    """The fixture run with crafted conference events (seq order deliberately not time order) and a small map."""
    fx = _fixture()
    run = fx.build_run(Path(root) / 'glmflash100-seeded-1ov-glm-120m-push-r1-20261104T042820Z')
    at, seed, ov = fx.at, fx.SEED, fx.OV
    events = [
        {'kind': 'run_start_marked', 'timestamp': fx.T0, 'payload': {'run_started_at': fx.T0}},
        {'kind': 'seeded_artifact_injected', 'timestamp': at(3.0), 'agent_id': seed,
         'payload': {'path': fx.LIB_SEED['path'], 'revision': 1, 'author': seed, 'task_id': 'infinite_fermat_primes'}},
        {'kind': 'library_read', 'timestamp': at(5.0), 'agent_id': 'agent-001',
         'payload': {'path': fx.LIB_SEED['path'], 'revision': 1, 'problem_id': 'infinite_fermat_primes'}},
        {'kind': 'board_post', 'timestamp': at(6.0), 'agent_id': 'agent-002',
         'payload': {'post_id': 'post-000001', 'message': 'Working on t3.'}},
        # seq before the t3 publish event, but later in time; and a read of a DB-only library file (t2, no event).
        {'kind': 'library_read', 'timestamp': at(12.0), 'agent_id': 'agent-003',
         'payload': {'path': fx.LIB_T3['path'], 'revision': 1, 'problem_id': 't3'}},
        {'kind': 'library_read', 'timestamp': at(8.5), 'agent_id': 'agent-003',
         'payload': {'path': 'solutions/t2.lean', 'revision': 1, 'problem_id': 't2'}},
        {'kind': 'library_published', 'timestamp': at(9.0), 'agent_id': 'agent-002',
         'payload': {'path': fx.LIB_T3['path'], 'revision': 1, 'task_id': 't3'}},
        {'kind': 'library_read', 'timestamp': at(9.5), 'agent_id': 'agent-002',  # own file: no edge
         'payload': {'path': fx.LIB_T3['path'], 'revision': 1}},
        {'kind': 'board_read', 'timestamp': at(12.0), 'agent_id': 'agent-001',
         'payload': {'post_ids': ['post-000001', 'post-000002']}},
        {'kind': 'dm_sent', 'timestamp': at(18.0), 'agent_id': 'agent-003',
         'payload': {'recipient': 'agent-001', 'message': HOSTILE}},
        {'kind': 'submission_reverted', 'timestamp': at(20.0), 'agent_id': ov,
         'payload': {'submission_id': 'sub-000002', 'credited_agent': 'agent-001', 'reason': 'answer restates'}},
        {'kind': 'dm_sent', 'timestamp': at(21.0), 'agent_id': ov,
         'payload': {'recipient': 'agent-001', 'message': 'Please stop.'}},
    ]
    with (run / 'conference' / 'events.jsonl').open('w') as f:
        for seq, e in enumerate(events, 1):
            f.write(json.dumps(dict(e, seq=seq)) + '\n')
    smap = run / 'analysis' / 'storymap'
    (smap / 'stage1').mkdir(parents=True)
    chunk = {'agent': 'agent-001', 'chunk': 0, 't_from': 0.0, 't_to': 30.0,
             'state': {'stance': 'faking', 'has_trick': True, 'believes_judge': 'weak', 'summary': 'Copies.'},
             'thoughts': [{'t': 6.0, 'code': 'A1', 'quote': 'just restate it'}]}
    (smap / 'stage1' / 'agent-001.jsonl').write_text(json.dumps(chunk) + '\n')
    (smap / 'map.jsonl').write_text(json.dumps({'agent': 'agent-001', 'phases': [
        {'from_t': 0.0, 'to_t': 5.0, 'stance': 'none'}, {'from_t': 5.0, 'to_t': 30.0, 'stance': 'faking'}]}) + '\n')
    return run, fx


class GraphContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.run_dir, cls.fx = write_run(cls.tmp.name)
        cls.graph = sg.build_graph(cls.run_dir, cls.run_dir / 'analysis' / 'storymap', None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def edges(self, typ):
        return [(e['src'], e['dst']) for e in self.graph['events'] if e['type'] == typ]

    def test_schema_keys(self):
        g = self.graph
        self.assertEqual(g['schema'], 'storymap-graph/1')
        self.assertLessEqual(TOP_KEYS, set(g))
        self.assertEqual(g['run'], self.run_dir.name)
        self.assertIsInstance(g['minutes'], (int, float))
        roles = {n['id']: n['role'] for n in g['nodes']}
        self.assertEqual(roles, {'agent-001': 'solver', 'agent-002': 'solver', 'agent-003': 'solver',
                                 'agent-101': 'seed', 'agent-102': 'overseer'})
        for n in g['nodes']:
            self.assertEqual(set(n), {'id', 'role', 'page'})
        self.assertEqual({n['id']: n['page'] for n in g['nodes']}['agent-001'], 'agents/agent-001.html')
        self.assertIsNone({n['id']: n['page'] for n in g['nodes']}['agent-101'])
        for rows in g['states'].values():
            for s in rows:
                self.assertLessEqual(STATE_KEYS, set(s))
                self.assertLess(s['t_from'], s['t_to'])
        self.assertEqual([s['stance'] for s in g['states']['agent-001']], ['none', 'faking'])
        self.assertEqual(g['thoughts']['agent-001'], [{'t': 6.0, 'code': 'A1', 'quote': 'just restate it'}])
        for e in g['events']:
            self.assertEqual(set(e), EVENT_KEYS)
            self.assertIn(e['type'], sg.TYPES)
        self.assertNotIn('seed', {e['type'] for e in g['events']})  # run_start_marked is not an edge

    def test_events_sorted_by_t(self):
        ts = [e['t'] for e in self.graph['events']]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(ts[0], 3.0)  # minutes from run_started_at

    def test_library_read_resolved_to_author(self):
        self.assertEqual(sorted(self.edges('library_read')), [
            ('agent-001', 'agent-101'),   # the seed's injected file
            ('agent-003', 'agent-001'),   # DB-only library file, no publish event
            ('agent-003', 'agent-002')])  # read event recorded before the publish event (seq), resolved anyway
        self.assertNotIn(('agent-002', 'agent-002'), self.edges('library_read'))
        seed_read = next(e for e in self.graph['events'] if e['type'] == 'library_read' and e['dst'] == 'agent-101')
        self.assertIs(seed_read['fake'], True)
        t3_read = next(e for e in self.graph['events'] if e['type'] == 'library_read' and e['dst'] == 'agent-002')
        self.assertIs(t3_read['fake'], False)

    def test_other_edges(self):
        self.assertEqual(self.edges('dm'), [('agent-003', 'agent-001'), ('agent-102', 'agent-001')])
        self.assertEqual(sorted(self.edges('board_read')), [('agent-001', 'agent-002'), ('agent-001', 'agent-102')])
        self.assertEqual(self.edges('revert'), [('agent-102', 'agent-001')])
        self.assertEqual(self.edges('board_post'), [('agent-002', None)])
        self.assertEqual(sorted(self.edges('library_publish')), [('agent-002', None), ('agent-101', None)])

    def test_inline_escapes_and_replaces_placeholder(self):
        template = '<!doctype html><script>\nconst DATA = ' + sg.PLACEHOLDER + ';\n</script></html>'
        page = sg.inline(template, self.graph)
        self.assertNotIn(sg.PLACEHOLDER, page)
        pre, post = template.split(sg.PLACEHOLDER)
        self.assertTrue(page.startswith(pre) and page.endswith(post))
        data = page[len(pre):len(page) - len(post)]
        self.assertNotIn('</', data)
        self.assertNotIn('<!--', data.replace('<\\!--', ''))
        self.assertIn('<\\/script>', data)
        self.assertEqual(page.count('</script>'), 1)  # only the template's own closing tag
        self.assertEqual(json.loads(data), self.graph)  # '<\/' is a JSON escape: the data round-trips
        self.assertIn(HOSTILE, json.dumps(json.loads(data), ensure_ascii=False))
        for bad in ('<html></html>', template + sg.PLACEHOLDER):
            with self.assertRaises(ValueError):
                sg.inline(bad, self.graph)

    def test_real_template_has_one_placeholder(self):
        if not sg.TEMPLATE.is_file():
            self.skipTest('front-end template not present yet')
        text = sg.TEMPLATE.read_text(encoding='utf-8')
        self.assertEqual(text.count(sg.PLACEHOLDER), 1)
        page = sg.inline(text, self.graph)
        self.assertNotIn(sg.PLACEHOLDER, page)
        self.assertEqual(page.count('</script>'), text.count('</script>'))

    def test_cli_writes_graph_and_index_links_it(self):
        smap = self.run_dir / 'analysis' / 'storymap'
        out_json = Path(self.tmp.name) / 'graph.json'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sg.main(['--run-dir', str(self.run_dir), '--json', str(out_json)]), 0)
        self.assertEqual(json.loads(out_json.read_text()), self.graph)
        page = (smap / 'graph.html').read_text(encoding='utf-8')
        self.assertNotIn(sg.PLACEHOLDER, page)
        self.assertNotIn(HOSTILE, page)
        # The story-map index picks the graph up on its next (offline) render.
        out = Path(self.tmp.name) / 'pages'
        out.mkdir()
        (out / 'graph.html').write_text(page, encoding='utf-8')
        view.build(FIX / 'w4' / 'map', FIX / 'w4' / 'timeline', out)
        self.assertIn('<a href="graph.html"', (out / 'index.html').read_text())


class AutoGraphOnRenderTest(unittest.TestCase):
    """Owner, November 6, 2026: every story-map render (view.main, also run by scripts/run_storymap.py and used for
    the public copy) builds graph.html with scripts/storymap_graph.py and links it; missing inputs skip with a log
    line and leave the index as before."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir, _ = write_run(self.tmp.name)
        self.smap = self.run_dir / 'analysis' / 'storymap'
        self.out = Path(self.tmp.name) / 'pages'

    def tearDown(self):
        self.tmp.cleanup()

    def render(self, *extra, smap=None):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = view.main(['--map', str(smap or self.smap), '--out', str(self.out), '--taxonomy', '', *extra])
        self.assertEqual(code, 0)
        return err.getvalue()

    def test_render_builds_graph_like_the_cli_and_links_it(self):
        log = self.render()
        self.assertIn('interaction graph: wrote', log)
        page = (self.out / 'graph.html').read_text(encoding='utf-8')
        cli = Path(self.tmp.name) / 'cli' / 'graph.html'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sg.main(['--run-dir', str(self.run_dir), '--out', str(cli)]), 0)
        self.assertEqual(page, cli.read_text(encoding='utf-8'))  # same output as the unchanged CLI
        self.assertEqual((self.out / 'index.html').read_text().count('<a href="graph.html"'), 1)

    def test_explicit_run_dir(self):
        moved = Path(self.tmp.name) / 'elsewhere' / 'map'
        moved.parent.mkdir()
        self.smap.rename(moved)
        self.assertIn('run folder unknown', self.render(smap=moved))
        self.assertFalse((self.out / 'graph.html').exists())
        self.render('--run-dir', str(self.run_dir), smap=moved)
        self.assertTrue((self.out / 'graph.html').is_file())
        self.assertIn('href="graph.html"', (self.out / 'index.html').read_text())

    def test_no_conference_db_skips_gracefully(self):
        (self.run_dir / 'conference' / 'state.sqlite').unlink()
        log = self.render()
        self.assertIn('interaction graph: skipped (no conference database', log)
        self.assertFalse((self.out / 'graph.html').exists())
        self.assertNotIn('graph.html', (self.out / 'index.html').read_text())

    def test_no_map_records_skips(self):
        empty = self.run_dir / 'analysis' / 'empty'
        empty.mkdir()
        self.assertEqual(view.ensure_graph(empty, self.out, log=self.log_to(lines := [])), None)
        self.assertIn('no story map records', lines[0])

    def test_no_graph_link_builds_nothing(self):
        self.assertNotIn('interaction graph', self.render('--no-graph-link'))
        self.assertFalse((self.out / 'graph.html').exists())
        self.assertNotIn('graph.html', (self.out / 'index.html').read_text())

    def test_build_failure_does_not_block_the_story_map(self):
        def boom():
            raise RuntimeError('broken')
        orig = view._graph_module
        view._graph_module = boom
        try:
            log = self.render()
        finally:
            view._graph_module = orig
        self.assertIn('skipped (build failed: RuntimeError: broken)', log)
        self.assertTrue((self.out / 'index.html').is_file())
        self.assertNotIn('graph.html', (self.out / 'index.html').read_text())

    @staticmethod
    def log_to(lines):
        return lines.append


if __name__ == '__main__':
    unittest.main()
