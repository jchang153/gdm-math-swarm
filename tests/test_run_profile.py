"""Offline tests of the run cards and run-page intro (owner request, October 27, 2026): scripts/run_profile.py,
its JavaScript twin (checked with node when it is installed), the catalogue's newcomer sections and series
cards (build_experiment_index.write_index), and the host-side intro of maintain_hf_run --scope site.
No network, credentials or model calls.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_profile  # noqa: E402
from build_experiment_index import write_index  # noqa: E402
import catalogue_intro  # noqa: E402


def at(minute, second=0):
    return f'2026-10-27T00:{minute:02d}:{second:02d}.250000+00:00'


def results(**extra):
    return {'run': 'glmflash100-seeded-2ov-ds-60m-20261027T000000Z', 'start_utc': at(0), 'stop_utc': '2026-10-27T01:00:00+00:00',
            'elapsed_seconds': 3600.0,
            'condition': {'model': 'z-ai/glm-5.3-flash', 'solvers': 100, 'agents': 100,
                          'overseer': {'count': 2, 'agent_ids': ['agent-102', 'agent-103'], 'powers': True,
                                       'board': False}},
            'seeded': {'seed_agent': 'agent-101', 'task': 'infinite_fermat_primes', 'injected_minute': 3.01},
            'independent_evaluation': {'status': 'completed'},
            'accepted_submissions': [
                {'agent': 'agent-101', 'submitted_utc': at(3), 'independent_validity': 'fraudulent', 'reverted': True,
                 'reverted_utc': at(10)},
                {'agent': 'agent-002', 'submitted_utc': at(5), 'independent_validity': 'genuine'},
                {'agent': 'agent-003', 'submitted_utc': at(20), 'independent_validity': 'fraudulent', 'reverted': False},
                {'agent': 'agent-004', 'submitted_utc': at(30), 'independent_validity': 'unresolved'}],
            'reverted_submissions': [{'agent': 'agent-101', 'reverted_utc': at(10)}], **extra}


SETTINGS = {'overseer_model': {'model_id': 'deepseek/deepseek-v4.1-flash-1017'}, 'effective': {'unread_notices': True}}


class ProfileTests(unittest.TestCase):
    def test_fakes_curve_and_condition(self):
        p = run_profile.profile(results(), SETTINGS)
        self.assertEqual(p['fakes'], {'accepted': 2, 'reverted': 1, 'standing_end': 1, 'peak': 1, 'fake_minutes': 47.0,
                                      'unresolved': 1})
        self.assertEqual(p['curve'], [[0, 0, 0], [3.0, 1, 1], [10.0, 0, 1], [20.0, 1, 2], [60.0, 1, 2]])
        self.assertEqual(p['seed'], {'minute': 3.0, 'task': 'infinite_fermat_primes', 'reverted_minute': 10.0})
        self.assertEqual(run_profile.condition_text(p),
                         '100 GLM 5.3 Flash solvers, 2 DeepSeek V4.1 Flash overseers with check_proof and revert '
                         'powers, exploit planted at +3 min, unread notices on, 60 min.')

    def test_unknowns_stay_explicit(self):
        # No settings: the overseers' model is unknown and not guessed; notices unknown are not claimed.
        p = run_profile.profile(results(), None)
        self.assertIsNone(p['overseers']['model'])
        self.assertIsNone(p['notices'])
        self.assertIn('2 overseers with', run_profile.condition_text(p))
        # Proof checker not run: no fake counts are shown.
        p = run_profile.profile(results(independent_evaluation={'status': 'not_run'}), SETTINGS)
        self.assertIn('Proof checker not run: fakes unknown.', run_profile.card_inner(p, {'minutes': 60, 'fakes': 2}))
        # Spontaneous run without an overseer.
        r = results(condition={'model': 'z-ai/glm-5.3', 'agents': 5})
        del r['seeded']
        self.assertEqual(run_profile.condition_text(run_profile.profile(r)), '5 GLM 5.3 agents, no planted exploit, 60 min.')

    def test_card_has_sparkline_counts_and_notes(self):
        card = run_profile.card_inner(run_profile.profile(results(), SETTINGS), {'minutes': 60, 'fakes': 4})
        self.assertIn('<svg class="spark" viewBox="0 0 240 40"', card)
        self.assertIn('class="sp-seed"', card)
        self.assertIn('<dt>Fakes accepted</dt><dd class="bad">2</dd>', card)
        self.assertIn('<dt>Fake-minutes</dt><dd>47</dd>', card)
        self.assertIn('1 accepted submission unresolved', card)
        self.assertIn('planted seed reverted at +10 min', card)

    def test_series_of_run_ids(self):
        self.assertEqual(run_profile.series_of('glmflash100-seeded-5ov-ds-60m-notices-20261027T220000Z')[0], 'notices60')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-5ov-ds-60m-20261027T072259Z')[0], 'oversight60')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-control-60m-20261027T054747Z')[0], 'oversight60')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-1ov-ds-30m-20261026T201120Z')[0], 'oversight30')
        self.assertEqual(run_profile.series_of('glm100-seeded-control-30m-20261025T230654Z')[0], 'seeded30')
        self.assertEqual(run_profile.series_of('glm100-repeat2-20261022T055342Z')[0], 'baseline100')
        self.assertEqual(run_profile.series_of('glm-rule6-20261021T224441Z')[0], 'small')
        self.assertEqual(run_profile.series_of('owner-opus55-20261021T183459Z')[0], 'models')
        self.assertEqual(run_profile.series_of('anything-else')[0], 'early')

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_javascript_twin_matches_python(self):
        cases = [(results(), SETTINGS), (results(), None), (results(independent_evaluation={'status': 'not_run'}), None)]
        spont = results(condition={'model': 'google/gemini-4-argon', 'agents': 5})
        del spont['seeded']
        cases.append((spont, None))
        for r, s in cases:
            scale = {'minutes': 90, 'fakes': 3}
            script = (run_profile.CARD_JS + ';const [r,s,sc]=JSON.parse(require("fs").readFileSync(0,"utf8"));'
                      'const p=RP.profile(r,s);process.stdout.write(JSON.stringify([p,RP.inner(p,sc),RP.condition(p)]));')
            out = subprocess.run(['node', '-e', script], input=json.dumps([r, s, scale]), capture_output=True, text=True,
                                 check=True).stdout
            p, card, condition = json.loads(out)
            expected = run_profile.profile(r, s)
            self.assertEqual(p, expected)
            self.assertEqual(card, run_profile.card_inner(expected, scale))
            self.assertEqual(condition, run_profile.condition_text(expected))


def entry(run, hour, **extra):
    return {'run': run, 'date': '2026-10-27 10:00 PDT', 'start_utc': f'2026-10-27T{hour:02d}:00:00+00:00',
            'track': 'seeded', 'agents': 102, 'model': 'z-ai/glm-5.3-flash', 'provider_route': 'z-ai/fp8',
            'elapsed_seconds': 3600.0, 'pilot_spent_usd': '16.5', 'distinct_verified_tasks': 13, 'task_count': 71,
            'agent_exits': {'stopped:max_run_seconds': 102}, 'links': {'Report': run + '/results.html'}, **extra}


class CataloguePageTests(unittest.TestCase):
    def write(self, entries, hosted=True):
        with tempfile.TemporaryDirectory() as tmp:
            write_index(tmp, entries, hosted=hosted)
            return (Path(tmp) / 'index.html').read_text(), json.loads((Path(tmp) / 'experiments.json').read_text())

    def test_newcomer_sections_and_paper(self):
        page, _ = self.write([entry('glm-rule6-20261021T224441Z', 1)])
        self.assertIn('<h1>Do AI research swarms cheat, and does anyone stop them?</h1>', page)
        self.assertIn(f'href="{catalogue_intro.PAPER_URL}" target="_blank" rel="noopener noreferrer">'
                      f'<cite>{catalogue_intro.PAPER_TITLE}</cite></a>', page)
        self.assertIn('Paglieri et al.', page)
        for anchor in ('id="how"', 'id="read"', 'id="glossary"', 'id="cards-view"', 'id="table-view"',
                       'id="view-cards"', 'id="view-table"', 'href="#series-small"'):
            self.assertIn(anchor, page)
        for term in ('Fake-minutes', 'Seed readers', 'Standing fake', 'Overseer', 'Revert', 'Genuine'):
            self.assertIn(f'<dt>{term}</dt>', page)
        # The existing table, filters and compare view are kept.
        for control in ('id="runs"', 'id="filter"', 'id="compare"', 'id="compare-view"'):
            self.assertIn(control, page)

    def test_series_cards_with_and_without_profile(self):
        profiled = entry('glmflash100-seeded-2ov-ds-60m-20261027T055654Z', 5,
                         profile=run_profile.profile(results(), SETTINGS))
        bare = entry('glmflash100-seeded-10ov-ds-60m-20261027T073443Z', 7)
        other = entry('owner-opus55-20261021T183459Z', 1)
        page, index = self.write([profiled, bare, other])
        self.assertEqual(index['experiments'][0], bare)  # entries are kept exactly
        self.assertNotIn('profile', index['experiments'][0])
        cards = page.split('id="cards-view"', 1)[1].split('id="table-view"', 1)[0]
        self.assertLess(cards.index('id="series-oversight60"'), cards.index('id="series-models"'))  # newest series first
        # Within a series by overseer count: 2 overseers before 10 (the bare entry's count from its run ID).
        self.assertLess(cards.index('data-run="glmflash100-seeded-2ov-ds'), cards.index('data-run="glmflash100-seeded-10ov'))
        self.assertIn('<div class="rc-body" data-profile="1"><h3 class="rc-cond">100 GLM 5.3 Flash solvers, 2 DeepSeek',
                      cards)
        self.assertIn('<div class="rc-body" data-profile="0"><h3 class="rc-cond">102 GLM 5.3 Flash agents, 1h 00m.</h3>',
                      cards)
        self.assertIn('class="card-pick" value="owner-opus55-20261021T183459Z"', cards)
        self.assertIn('const RP=(()=>{', page)
        self.assertIn('"deepseek/deepseek-v4.1-flash-1017": "DeepSeek V4.1 Flash"', page)

    def test_rendering_is_deterministic(self):
        es = [entry('glmflash100-seeded-2ov-ds-60m-20261027T055654Z', 5,
                    profile=run_profile.profile(results(), SETTINGS)), entry('owner-opus55-20261021T183459Z', 1)]
        self.assertEqual(self.write(es), self.write(list(reversed(es))))


class IntroTests(unittest.TestCase):
    PAGE = ('<!doctype html><html><head></head><body><h1>x</h1><p><code>x</code> · a to b</p>'
            + run_profile.SUMMARY_CARD + '<div class="tile">t</div></section></body></html>')

    def test_intro_is_inserted_once_and_replaced(self):
        page = run_profile.add_intro(self.PAGE, results(), SETTINGS)
        self.assertEqual(page.count(run_profile.INTRO_START), 1)
        self.assertLess(page.index(run_profile.INTRO_START), page.index(run_profile.SUMMARY_CARD))
        self.assertIn('2 DeepSeek V4.1 Flash overseers', page)
        self.assertIn('60-minute overseer scaling series', page)
        self.assertIn('2 fakes accepted, 1 reverted, 1 standing at the end, 47 fake-minutes; 1 accepted submission '
                      'unresolved', page)
        self.assertIn('The Oversight section lists every revert', page)
        self.assertIn('href="../index.html#glossary"', page)
        self.assertEqual(run_profile.add_intro(page, results(), SETTINGS), page)  # idempotent
        newer = run_profile.add_intro(page, results(), None)  # replaced as a whole
        self.assertEqual(newer.count(run_profile.INTRO_START), 1)
        self.assertNotIn('2 DeepSeek V4.1 Flash overseers', newer)

    def test_pages_without_tiles_or_records_are_unchanged(self):
        self.assertEqual(run_profile.add_intro('<body>old layout</body>', results()), '<body>old layout</body>')
        self.assertEqual(run_profile.add_intro(self.PAGE, {'run': 'x'}), self.PAGE)

    def test_published_page_gets_intro_host_side(self):
        import maintain_hf_run
        data = results()
        files = {'r/results.html': self.PAGE.encode(), 'r/results.json': json.dumps(data).encode(),
                 'r/settings.json': json.dumps(SETTINGS).encode()}
        start, pages = maintain_hf_run._published_page_restamps('r', files, files.__getitem__)
        self.assertIn('2 DeepSeek V4.1 Flash overseers', pages['r/results.html'].decode())
        files['r/results.html'] = pages['r/results.html']
        self.assertEqual(maintain_hf_run._published_page_restamps('r', files, files.__getitem__)[1], {})  # idempotent


if __name__ == '__main__':
    unittest.main()
