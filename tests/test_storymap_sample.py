"""Stratified solver sample for partial story maps (research/analysis/storymap/sample.py) and its wiring in
scripts/run_storymap.py --sample and the index note of view.py (owner decision, November 6, 2026)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src'), str(ROOT / 'scripts')]

from research.analysis.storymap import sample  # noqa: E402

SOLVERS = [f'agent-{i:03d}' for i in range(1, 101)]


class StratumSizes(unittest.TestCase):
    def test_share_rounded_half_up_and_capped(self):
        self.assertEqual(sample.stratum_sizes(20, 100, 25), (5, 15))
        self.assertEqual(sample.stratum_sizes(20, 100, 56), (11, 9))   # 11.2
        self.assertEqual(sample.stratum_sizes(20, 100, 9), (2, 18))    # 1.8
        self.assertEqual(sample.stratum_sizes(20, 100, 2), (0, 20))    # 0.4
        self.assertEqual(sample.stratum_sizes(20, 100, 0), (0, 20))
        self.assertEqual(sample.stratum_sizes(20, 100, 100), (20, 0))
        self.assertEqual(sample.stratum_sizes(20, 22, 21), (19, 1))    # 19.09
        self.assertEqual(sample.stratum_sizes(20, 21, 1), (1, 19))     # 0.95
        self.assertEqual(sample.stratum_sizes(30, 10, 3), (3, 7))      # n capped at the population
        self.assertEqual(sample.stratum_sizes(0, 10, 3), (0, 0))


class Draw(unittest.TestCase):
    def test_deterministic_stratified_and_seed_sensitive(self):
        fakers = SOLVERS[10:35]
        a, fa = sample.draw(SOLVERS, fakers, 20, seed=7)
        b, fb = sample.draw(list(reversed(SOLVERS)), list(reversed(fakers)), 20, seed=7)
        self.assertEqual((a, fa), (b, fb))      # input order does not matter
        self.assertEqual(len(a), 20)
        self.assertEqual(len(set(a)), 20)
        self.assertEqual(len(fa), 5)
        self.assertTrue(set(fa) <= set(fakers))
        self.assertEqual(fa, sorted(x for x in a if x in fakers))
        self.assertEqual(a, sorted(a))
        self.assertNotEqual(sample.draw(SOLVERS, fakers, 20, seed=8)[0], a)

    def test_fakers_outside_population_ignored(self):
        agents, fakers = sample.draw(SOLVERS[:10], ['agent-101', 'agent-001'], 5, seed=1)
        self.assertNotIn('agent-101', agents)
        self.assertEqual(fakers, ['agent-001'] if 'agent-001' in agents else [])


class Record(unittest.TestCase):
    def test_record_load_and_note(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'RUN'
            run.mkdir()
            rec = sample.record(run, 20, 3, population=(SOLVERS, SOLVERS[:9]))
            self.assertEqual(rec['schema'], sample.SCHEMA)
            self.assertEqual((rec['n'], rec['population'], rec['fakers_in_run'], rec['fakers_in_sample']),
                             (20, 100, 9, 2))
            self.assertEqual(rec['seed'], 3)
            self.assertEqual(rec['run'], 'RUN')
            self.assertEqual(rec['fake_rule'], 'answer-echo-v2')
            self.assertIn('stratified', rec['method'])
            (run / sample.FILE).write_text(json.dumps(rec))
            self.assertEqual(sample.load(run), rec)
            self.assertIsNone(sample.load(Path(folder)))
            text = sample.note_text(rec)
            self.assertIn('20 of 100 solvers are mapped', text)
            self.assertIn('seed 3', text)
            self.assertIn('2 of the 20 sampled solvers faked, against 9 of 100', text)


class ViewNote(unittest.TestCase):
    def test_note_only_with_a_record(self):
        from research.analysis.storymap import view
        self.assertEqual(view.sample_note(None), '')
        rec = {'n': 20, 'population': 100, 'seed': 5, 'fakers_in_sample': 1, 'fakers_in_run': 5,
               'fake_rule': 'answer-echo-v2'}
        note = view.sample_note(rec)
        self.assertIn('class="sample-note"', note)
        self.assertIn('20 of 100 solvers are mapped', note)
        page = view.index_page({}, {'run_id': 'RUN'}, 120, sample=rec)
        self.assertIn('20 of 100 solvers are mapped', page)
        self.assertNotIn('sample-note', view.index_page({}, {'run_id': 'RUN'}, 120))


class RunStorymapSample(unittest.TestCase):
    def _steps(self, order, picked):
        def step(name):
            def call(argv):
                order.append((name, list(argv)))
                return 0
            return call
        return {'restore': lambda d, log: order.append(('restore', [])), 'extract': step('extract'),
                'pipeline': step('pipeline'), 'validate': step('validate'), 'view': step('view'),
                'sample': lambda run, out, n, seed, log: picked.append((n, seed)) or ['agent-003', 'agent-007']}

    def test_sample_passes_agents_to_extract_and_pipeline(self):
        import run_storymap
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'RUN'
            run.mkdir()
            order, picked = [], []
            code = run_storymap.main(['--run-dir', str(run), '--sample', '2', '--sample-seed', '9',
                                      '--run-limit', '40', '--no-offload'], steps=self._steps(order, picked))
            self.assertEqual(code, 0)
            self.assertEqual(picked, [(2, 9)])
            names = [n for n, _ in order]
            self.assertEqual(names, ['restore', 'extract', 'pipeline', 'validate', 'view'])
            for name in ('extract', 'pipeline'):
                argv = dict(order)[name]
                self.assertEqual(argv[argv.index('--agents') + 1], 'agent-003,agent-007')
            self.assertIn('--run-limit', dict(order)['pipeline'])

    def test_existing_timeline_reextracted_only_when_sampled_agents_missing(self):
        import run_storymap
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'RUN'
            tl = run / 'analysis' / 'storymap' / 'timeline'
            tl.mkdir(parents=True)
            (tl / 'index.json').write_text('{}')
            (tl / 'agent-003.jsonl').write_text('')
            order, picked = [], []
            run_storymap.main(['--run-dir', str(run), '--sample', '2', '--no-offload'],
                              steps=self._steps(order, picked))
            self.assertIn('extract', [n for n, _ in order])
            (tl / 'agent-007.jsonl').write_text('')
            order.clear()
            run_storymap.main(['--run-dir', str(run), '--sample', '2', '--no-offload'],
                              steps=self._steps(order, picked))
            self.assertNotIn('extract', [n for n, _ in order])
            self.assertEqual(picked[-1], (2, None))

    def test_refuses_with_agents_or_overseer(self):
        import run_storymap
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'RUN'
            run.mkdir()
            for extra in (['--agents', 'agent-001'], ['--overseer'], ['--sample', '0']):
                order, picked = [], []
                argv = ['--run-dir', str(run), '--no-offload', *(['--sample', '2'] if '--sample' not in extra else []),
                        *extra]
                self.assertEqual(run_storymap.main(argv, steps=self._steps(order, picked)), 2)
                self.assertEqual(order, [])

    def test_sample_agents_writes_record(self):
        import run_storymap
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'out'
            fake = lambda run, n, seed: {'n': n, 'population': 100, 'seed': seed, 'fakers_in_sample': 0,  # noqa: E731
                                         'fakers_in_run': 0, 'agents': ['agent-001']}
            got = run_storymap.sample_agents(Path(folder), out, 1, None, lambda m: None, record=fake)
            self.assertEqual(got, ['agent-001'])
            self.assertEqual(json.loads((out / 'sample.json').read_text())['seed'], sample.DEFAULT_SEED)


if __name__ == '__main__':
    unittest.main()
