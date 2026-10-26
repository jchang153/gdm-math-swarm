"""results.json / results.html and the conference view of seeded runs (owner design 2026-10-25).

Synthetic seeded run directories (research/analysis/tests/seeded_fixture.py): agent-101 is the planted seed
(no container, model or transcript), agent-102 the overseer in the overseer variant. The seed is left out of
the per-agent rows and the solver counts and recorded in a researcher-only ``seeded`` block; the overseer keeps
its role. Runs without a seed are covered by test_build_results.py and test_oversight_analysis.py.
"""
import base64
import contextlib
import gzip
import io
import json
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / 'scripts', ROOT, ROOT / 'research' / 'analysis' / 'tests'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
import build_results  # noqa: E402
import export_conference  # noqa: E402
from seeded_fixture import OVERSEER, SEED, SEED_PATH, TASK, make_seeded_run  # noqa: E402


class SeededResultsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def generate(self, run):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            build_results.main(['--run-dir', str(run)])
        return json.loads((run / 'results.json').read_text()), (run / 'results.html').read_text()

    def test_seeded_block_and_seed_out_of_agent_rows(self):
        data, page = self.generate(make_seeded_run(self.tmp.name))
        seeded = data['seeded']
        self.assertTrue(seeded['researcher_only'])
        self.assertEqual((seeded['track'], seeded['seed_agent'], seeded['task'], seeded['path']),
                         ('seeded', SEED, TASK, SEED_PATH))
        self.assertEqual(seeded['injected_minute'], 3.0)
        self.assertTrue(seeded['injected_utc'].startswith('2026-'))
        self.assertTrue(seeded['injection_recorded'])
        self.assertEqual([s['operational_outcome'] for s in seeded['submissions']], ['accepted'])
        self.assertEqual(seeded['seed_credited_tasks'], [TASK])
        self.assertEqual(seeded['credited_tasks_by_role'], {'seed': 1, 'solver': 3})
        self.assertEqual(seeded['solver_credited_tasks'], 3)
        agents = [a['agent'] for a in data['agents']]
        self.assertNotIn(SEED, agents)
        self.assertEqual(agents, ['agent-001', 'agent-002', 'agent-003', OVERSEER])
        self.assertEqual({a['agent']: a.get('role') for a in data['agents']}[OVERSEER], 'overseer')
        self.assertEqual(data['condition']['seed_agent'], SEED)
        self.assertEqual(data['condition']['solvers'], 3)
        self.assertEqual(data['condition']['participants'], 5)
        self.assertEqual(data['oversight']['by_role']['seed']['agents'], [SEED])
        self.assertEqual(data['oversight']['by_role']['solver']['agents'], ['agent-001', 'agent-002', 'agent-003'])
        self.assertEqual([r['agent'] for r in data['accepted_submissions'] if r.get('seeded')], [SEED])
        self.assertIn('Seeded source (researcher-only)', page)
        self.assertIn(f'{SEED} (seeded, planted by researchers)', __import__('research.analysis.agent_badges', fromlist=['strip']).strip(page))  # without the agent labels
        self.assertIn('3 solvers + overseer + seed', page)

    def test_control_variant_without_overseer(self):
        data, page = self.generate(make_seeded_run(self.tmp.name, overseer=False))
        self.assertNotIn('oversight', data)
        self.assertNotIn('role', data['agents'][0])
        self.assertEqual([a['agent'] for a in data['agents']], ['agent-001', 'agent-002', 'agent-003'])
        self.assertEqual(data['condition']['solvers'], 3)
        self.assertEqual(data['condition']['participants'], 4)
        self.assertEqual(data['seeded']['seed_agent'], SEED)
        self.assertIn('3 solvers + seed', page)

    def test_conference_view_labels_seed_and_roles(self):
        run = make_seeded_run(Path(self.tmp.name) / 'runs')
        with mock.patch.object(export_conference, 'ROOT', Path(self.tmp.name).resolve()):
            manifest = export_conference.export(run)
        roles = [w for w in manifest['warnings'] if w.startswith('Participant roles')]
        self.assertEqual(len(roles), 1)
        self.assertIn(f'{SEED} (seeded, planted by researchers)', roles[0])
        self.assertIn(f'{OVERSEER} (overseer)', roles[0])
        page = (run / 'conference-view' / 'index.html').read_text()
        packed = re.search(r'<script id="data" type="application/gzip\+base64">(.*?)</script>', page, re.S).group(1)
        records = json.loads(gzip.decompress(base64.b64decode(packed)))['records']
        titles = {r['title'] for r in records if SEED in r['agents']}
        self.assertTrue(titles)
        self.assertTrue(all(t.endswith('seeded (planted by researchers)') for t in titles))
        self.assertFalse([r for r in records if SEED not in r['agents'] and 'planted' in r['title']])


if __name__ == '__main__':
    unittest.main()
