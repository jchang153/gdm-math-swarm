"""Agentic monitors (arm AG) in the story-map interaction graph (scripts/storymap_graph.py and its template).

docs/plan.md, "Agentic monitors in the story maps (owner decision, November 24, 2026)", item (4): monitors as
satellite nodes of their solvers (role agentic_monitor, ``parent``), shown by default only when they acted, with a
toggle; monitor-to-solver ``agmon_revert`` / ``agmon_message`` events attributed from runtime.jsonl + the conference
(the reviewer identity they act under); non-AG graphs unchanged; graph-thumb.json without monitors.

    PYTHONPATH=src .venv/bin/python -m unittest research.analysis.tests.test_storymap_graph_agentic
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for _p in (REPO, REPO / 'scripts', REPO / 'src'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import storymap_graph as sg  # noqa: E402

FIX = HERE / 'fixtures' / 'storymap'
EVENT_KEYS = {'t', 'type', 'src', 'dst', 'fake', 'problem', 'text'}
REASON = 'The answer restates the goal.'
NOTICE = 'Your submission for t2 was reverted by the reviewer: ' + REASON
FREE = 'Please stop restating the theorem; prove it.'


def _fixture():
    spec = importlib.util.spec_from_file_location('storymap_build_fixture_agmon', FIX / 'build_fixture.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_run(root, name, agentic=True, runtime=True, settings_flag=False):
    """The shared fixture run; with ``agentic`` the reviewer identity reverts agent-001's fake t2 submission and DMs
    it (the revert's notice, then a free message), with the monitors' runtime.jsonl records when ``runtime``."""
    fx = _fixture()
    run = fx.build_run(Path(root) / name)
    at = fx.at
    events = [
        {'kind': 'run_start_marked', 'timestamp': fx.T0, 'payload': {'run_started_at': fx.T0}},
        {'kind': 'submission_received', 'timestamp': at(8.0), 'agent_id': 'agent-001',
         'payload': {'submission_id': 'sub-000002', 'problem_id': 't2'}},
        {'kind': 'dm_sent', 'timestamp': at(18.0), 'agent_id': 'agent-003',
         'payload': {'recipient': 'agent-001', 'message': 'hello', 'message_id': 'dm-000002',
                     'public_message_id': 'dm-aa02'}},
    ]
    if agentic:
        events += [
            {'kind': 'submission_reverted', 'timestamp': at(20.0), 'agent_id': 'reviewer',
             'payload': {'submission_id': 'sub-000002', 'public_submission_id': 'sub-pub2', 'problem_id': 't2',
                         'credited_agent': 'agent-001', 'reason': REASON}},
            {'kind': 'dm_sent', 'timestamp': at(20.02), 'agent_id': 'reviewer',
             'payload': {'recipient': 'agent-001', 'message': NOTICE, 'message_id': 'dm-000003',
                         'public_message_id': 'dm-rv01'}},
            {'kind': 'dm_sent', 'timestamp': at(24.0), 'agent_id': 'reviewer',
             'payload': {'recipient': 'agent-001', 'message': FREE, 'message_id': 'dm-000004',
                         'public_message_id': 'dm-rv02'}},
        ]
        db = sqlite3.connect(run / 'conference' / 'state.sqlite')
        db.executemany('insert into messages values (?,?,?,?,?,?,?)',
                       [(3, 'reviewer', 'agent-001', NOTICE, at(20.02), at(22.0), 'dm-rv01'),
                        (4, 'reviewer', 'agent-001', FREE, at(24.0), at(26.5), 'dm-rv02')])
        db.commit()
        db.close()
    with (run / 'conference' / 'events.jsonl').open('w') as f:
        for seq, e in enumerate(events, 1):
            f.write(json.dumps(dict(e, seq=seq)) + '\n')
    if agentic and runtime:
        rows = [
            {'kind': 'agentic_monitor_start', 'timestamp': at(0.5), 'agent_id': 'agmon-001',
             'payload': {'monitor': 'agmon-001', 'solver': 'agent-001'}},
            {'kind': 'agentic_revert_refused', 'timestamp': at(15.0), 'agent_id': 'agmon-002',
             'payload': {'monitor': 'agmon-002', 'solver': 'agent-002', 'problem_id': 't3',
                         'why': 'no_accepted_submission'}},
            {'kind': 'agentic_revert', 'timestamp': at(20.03), 'agent_id': 'agmon-001',
             'payload': {'monitor': 'agmon-001', 'solver': 'agent-001', 'problem_id': 't2',
                         'submission_id': 'sub-000002', 'public_submission_id': 'sub-pub2', 'reason': REASON,
                         'dm_sent': True, 'message_id': 'dm-rv01'}},
            {'kind': 'agentic_message', 'timestamp': at(24.01), 'agent_id': 'agmon-001',
             'payload': {'monitor': 'agmon-001', 'solver': 'agent-001', 'message_id': 'dm-rv02', 'text': FREE,
                         'sent': True}},
            {'kind': 'agentic_message', 'timestamp': at(30.0), 'agent_id': 'agmon-003',
             'payload': {'monitor': 'agmon-003', 'solver': 'agent-003', 'message_id': None, 'text': 'lost',
                         'sent': False}},
        ]
        (run / 'runtime.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    if settings_flag:
        settings = json.loads((run / 'settings.json').read_text())
        settings['effective']['agentic_monitor'] = {'enabled': True}
        (run / 'settings.json').write_text(json.dumps(settings, indent=1) + '\n')
    return run


class AgenticGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.ag = sg.build_graph(write_run(root, 'ag', agentic=True), None, None)
        cls.fallback = sg.build_graph(write_run(root, 'fb', agentic=True, runtime=False, settings_flag=True),
                                      None, None)
        cls.plain = sg.build_graph(write_run(root, 'plain', agentic=False), None, None)
        # a reviewer-arm run (A1-A4): reviewer reverts and DMs, no agentic monitors
        cls.reviewer = sg.build_graph(write_run(root, 'rev', agentic=True, runtime=False), None, None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def mons(self, graph):
        return [n for n in graph['nodes'] if n['role'] == sg.AGMON_ROLE]

    def of(self, graph, typ):
        return [e for e in graph['events'] if e['type'] == typ]

    def test_monitor_nodes_are_satellites_after_the_others(self):
        nodes = self.ag['nodes']
        mons = self.mons(self.ag)
        self.assertEqual([m['id'] for m in mons], ['agmon-001', 'agmon-002', 'agmon-003'])
        self.assertEqual([m['parent'] for m in mons], ['agent-001', 'agent-002', 'agent-003'])
        self.assertEqual([m['page'] for m in mons], [f'agents/agent-00{k}.monitor.html' for k in (1, 2, 3)])
        self.assertTrue(all(n['role'] != sg.AGMON_ROLE for n in nodes[:len(nodes) - 3]))
        self.assertNotIn('reviewer', [n['id'] for n in nodes])
        by = {m['id']: m for m in mons}
        self.assertTrue(by['agmon-001']['acted'])
        self.assertFalse(by['agmon-002']['acted'])          # a refused revert is not an action
        self.assertEqual(by['agmon-002']['stats']['refused'], 1)
        self.assertTrue(by['agmon-003']['acted'])           # an undelivered message is
        self.assertEqual(by['agmon-001']['stats'], {'reverts': 1, 'messages': 1, 'unsent': 0, 'refused': 0})
        self.assertEqual(self.ag['agentic'], {'monitors': 3, 'acted': 2, 'source': 'runtime.jsonl + conference'})

    def test_revert_event(self):
        (rev,) = self.of(self.ag, 'agmon_revert')
        self.assertEqual((rev['src'], rev['dst'], rev['problem'], rev['text']), ('agmon-001', 'agent-001', 't2', REASON))
        self.assertIs(rev['fake'], True)
        self.assertEqual((rev['t'], rev['delivered']), (20.0, 22.0))

    def test_messages_and_the_notice(self):
        msgs = self.of(self.ag, 'agmon_message')
        self.assertEqual([(m['src'], m['dst'], m['text'], m['sent'], m['delivered']) for m in msgs],
                         [('agmon-001', 'agent-001', FREE, True, 26.5), ('agmon-003', 'agent-003', 'lost', False, None)])
        self.assertEqual(msgs[0]['t'], 24.0)
        self.assertFalse(any(e['text'] == NOTICE for e in self.ag['events']))   # the notice rides on the revert
        self.assertFalse(any('reviewer' in (e['src'], e['dst']) for e in self.ag['events']))
        self.assertEqual(self.ag['events'], sorted(self.ag['events'], key=lambda e: (e['t'], sg.TYPES.index(e['type']))))

    def test_fallback_without_runtime(self):
        g = self.fallback
        self.assertEqual(g['agentic']['source'], 'conference (1:1 pairing)')
        self.assertEqual([(e['type'], e['src'], e['dst']) for e in g['events'] if e['type'].startswith('agmon')],
                         [('agmon_revert', 'agmon-001', 'agent-001'), ('agmon_message', 'agmon-001', 'agent-001')])
        self.assertIsNone(self.of(g, 'agmon_revert')[0]['delivered'])   # the notice is unknown without runtime
        self.assertEqual(self.of(g, 'agmon_message')[0]['delivered'], 26.5)
        self.assertEqual(sum(m['acted'] for m in self.mons(g)), 1)

    def test_non_ag_runs_unchanged(self):
        for g in (self.plain, self.reviewer):
            self.assertNotIn('agentic', g)
            self.assertFalse(self.mons(g))
            self.assertTrue(all(set(e) == EVENT_KEYS for e in g['events']))
            self.assertFalse(any(e['type'].startswith('agmon') for e in g['events']))
            self.assertFalse(any('reviewer' in (e['src'], e['dst']) for e in g['events']))
        # the reviewer's records never enter a non-AG graph: it equals the graph of the run without them
        strip = lambda g: sg.dumps(dict(g, run=None, title=None))  # noqa: E731
        self.assertEqual(strip(self.plain), strip(self.reviewer))

    def test_thumb_leaves_monitors_out(self):
        th = sg.thumb(self.ag)
        self.assertFalse(any(n['role'] == sg.AGMON_ROLE for n in th['nodes']))
        self.assertEqual(len(th['frames'][0]['s']), len(th['nodes']))
        self.assertEqual(sg.dumps(th), sg.dumps(sg.thumb(dict(self.ag, nodes=[n for n in self.ag['nodes']
                                                                          if n['role'] != sg.AGMON_ROLE]))))


class AgenticViewerTest(unittest.TestCase):
    def test_template_handles_monitors(self):
        text = sg.TEMPLATE.read_text(encoding='utf-8')
        for needle in ('agentic_monitor', 'agmon_revert', 'agmon_message', 'id="agmonToggles"', 'tgMonAll',
                       '["mon", "Monitor"]', '--e-agmon', 'Monitor–solver pair page'):
            self.assertIn(needle, text)
        self.assertEqual(text.count('--e-agmon:'), 3)   # light, system dark, forced dark
        js = sg.viewer_files(text)['viewer.js'].decode()
        self.assertIn('function placeMons', js)


if __name__ == '__main__':
    unittest.main()
