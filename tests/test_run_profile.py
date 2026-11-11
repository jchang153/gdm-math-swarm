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
import build_experiment_index  # noqa: E402
from build_experiment_index import short_title, write_index  # noqa: E402
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
        p = run_profile.profile(results(), SETTINGS)
        card, more = run_profile.card_inner(p, {'minutes': 60, 'fakes': 4}), run_profile.card_more(p)
        self.assertIn('<svg class="spark" viewBox="0 0 240 40"', card)
        self.assertIn('class="sp-seed"', card)
        self.assertIn('<div title="Fakes accepted"><dt>Fakes</dt><dd class="bad">2</dd><dd class="sub">1 reverted</dd>', card)
        self.assertIn('<div title="Fakes standing at the end"><dt>Standing</dt><dd class="bad">1</dd>', card)
        self.assertIn('<div title="Fake-minutes"><dt>Fake-min</dt><dd>47</dd>', card)
        # The condition sentence and notes are secondary: under the card's Details.
        self.assertIn('<p class="rc-cond">100 GLM 5.3 Flash solvers, 2 DeepSeek', more)
        self.assertIn('1 accepted submission unresolved', more)
        self.assertIn('planted seed reverted at +10 min', more)

    def test_series_of_run_ids(self):
        self.assertEqual(run_profile.series_of('glmflash100-seeded-reminder5m-120m-push-r2-20261104T042744Z')[0], 'push120')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-10ov-glm-120m-push-r1-20261104T044246Z')[0], 'push120')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-1ov-glm-120m-push-commsonly-r1-20261104T220000Z')[0], 'push120comms')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-10ov-glm-120m-push-commsonly-r3-20261104T220000Z')[0], 'push120comms')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-control-120m-push-r1-20261104T042405Z')[0], 'push120')
        for arm in ('actions-silent', 'actions-reason', 'full-silent', 'full-reason'):
            self.assertEqual(run_profile.series_of(f'glmflash100-seeded-monitor-{arm}-120m-push-r2-20261110T200000Z')[0],
                             'monitor120')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-1ov-glm-180m-notices-commsonly-r1-20261030T060000Z')[0], 'conditions180')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-5ov-ds-60m-notices-20261027T220000Z')[0], 'notices60')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-2ov-glm-180m-notices-r2-20261028T200000Z')[0], 'notices180')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-5ov-ds-60m-20261027T072259Z')[0], 'oversight60')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-control-60m-20261027T054747Z')[0], 'oversight60')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-1ov-ds-30m-20261026T201120Z')[0], 'oversight30')
        self.assertEqual(run_profile.series_of('glm100-seeded-control-30m-20261025T230654Z')[0], 'seeded30')
        self.assertEqual(run_profile.series_of('glm100-repeat2-20261022T055342Z')[0], 'baseline100')
        self.assertEqual(run_profile.series_of('glm-rule6-20261021T224441Z')[0], 'small')
        self.assertEqual(run_profile.series_of('owner-opus55-20261021T183459Z')[0], 'models')
        self.assertEqual(run_profile.series_of('anything-else')[0], 'early')

    def test_sweep25_series(self):
        for slug in ('grok47', 'kimik31pro', 'dsv41flash', 'gpt6luna', 'gemini4argon'):
            self.assertEqual(run_profile.series_of(f'{slug}-25-seeded-control-60m-push-20261105T200000Z')[0], 'sweep25')
        self.assertEqual(run_profile.SERIES[0][0], 'sweep25')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-control-120m-push-r1-20261104T042405Z')[0], 'push120')
        self.assertEqual(run_profile.series_of('glmflash100-seeded-1ov-glm-120m-push-commsonly-r1-20261104T220000Z')[0],
                         'push120comms')
        for model, name in (('x-ai/grok-4.7', 'Grok 4.7'), ('moonshotai/kimi-k3.1-pro', 'Kimi K3.1 Pro'),
                            ('openai/gpt-6-luna', 'GPT 6 Luna'), ('google/gemini-4-argon', 'Gemini 4 Argon'),
                            ('deepseek/deepseek-v4.1-flash-1017', 'DeepSeek V4.1 Flash')):
            self.assertEqual(run_profile.model_name(model), name)

    def test_intro_names_the_runs_seed_agent(self):
        r = results(run='grok47-25-seeded-control-60m-push-20261105T200000Z',
                    condition={'model': 'x-ai/grok-4.7', 'solvers': 25, 'agents': 25},
                    seeded={'seed_agent': 'agent-026', 'task': 'infinite_fermat_primes', 'injected_minute': 3.01},
                    accepted_submissions=[{'agent': 'agent-026', 'submitted_utc': at(3),
                                           'independent_validity': 'fraudulent', 'reverted': False}],
                    reverted_submissions=[])
        text = run_profile.intro_html(r, {'effective': {'unread_notices': True}})
        self.assertIn('the silent seed agent-026 is credited', text)
        self.assertNotIn('agent-101', text)
        self.assertIn('60-minute 25-agent model sweep', text)
        self.assertIn('25 Grok 4.7 solvers', text)
        self.assertIn('the silent seed agent-101 is credited', run_profile.intro_html(results(), SETTINGS))

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_javascript_twin_matches_python(self):
        cases = [(results(), SETTINGS), (results(), None), (results(independent_evaluation={'status': 'not_run'}), None)]
        spont = results(condition={'model': 'google/gemini-4-argon', 'agents': 5})
        del spont['seeded']
        cases.append((spont, None))
        for r, s in cases:
            scale = {'minutes': 90, 'fakes': 3}
            script = (run_profile.CARD_JS + ';const [r,s,sc]=JSON.parse(require("fs").readFileSync(0,"utf8"));'
                      'const p=RP.profile(r,s);process.stdout.write(JSON.stringify([p,RP.inner(p,sc),RP.condition(p),RP.more(p)]));')
            out = subprocess.run(['node', '-e', script], input=json.dumps([r, s, scale]), capture_output=True, text=True,
                                 check=True).stdout
            p, card, condition, more = json.loads(out)
            expected = run_profile.profile(r, s)
            self.assertEqual(p, expected)
            self.assertEqual(card, run_profile.card_inner(expected, scale))
            self.assertEqual(condition, run_profile.condition_text(expected))
            self.assertEqual(more, run_profile.card_more(expected))


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
        self.assertIn(f'<h1>{catalogue_intro.HEADLINE}</h1>', page)
        self.assertIn(f'href="{catalogue_intro.PAPER_URL}" target="_blank" rel="noopener noreferrer">'
                      f'<cite>{catalogue_intro.PAPER_TITLE}</cite></a>', page)
        self.assertIn('Paglieri et al.', page)
        for anchor in ('id="about"', 'href="about.html"', 'id="cards-view"', 'id="table-view"',
                       'id="view-cards"', 'id="view-table"', 'href="#series-small"'):
            self.assertIn(anchor, page)
        # The explanations moved to about.html (owner, November 10, 2026); the catalogue links it.
        about = build_experiment_index.about_page()
        for anchor in ('id="experiment"', 'id="how"', 'id="read"', 'id="glossary"', 'Paglieri et al.',
                       '<a href="about.html" aria-current="page">About</a>'):
            self.assertIn(anchor, about)
        for term in ('Fake-minutes', 'Seed readers', 'Standing fake', 'Overseer', 'Revert', 'Genuine'):
            self.assertIn(f'<dt>{term}</dt>', about)
            self.assertNotIn(f'<dt>{term}</dt>', page)
        # The existing table and filters are kept; the compare view is gone (owner, November 6, 2026).
        for control in ('id="runs"', 'id="filter"'):
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
        # Short titles from the run ID; the full condition sentence sits in the card's details.
        # Math catalogue: run panes whose title links the run report (owner, November 6, 2026).
        for run, title in ((profiled['run'], '2 DeepSeek overseers'), (bare['run'], '10 DeepSeek overseers'),
                           (other['run'], 'GLM 5.3 Flash')):  # model comparison: the model's name
            self.assertIn(f'<h3 class="rc-title"><a href="{run}/results.html">{title}</a></h3>', cards)
        self.assertIn('<div class="rc-prof"><p class="rc-cond">100 GLM 5.3 Flash solvers, 2 DeepSeek', cards)
        self.assertIn('<div class="rc-body" data-profile="1"><figure class="rc-spark">', cards)
        self.assertIn('<div class="rc-body" data-profile="0"><p class="rc-empty">', cards)
        self.assertIn('<span class="chip k-model" title="Solver model × solvers"><b>GLM 5.3 Flash</b> × 100</span>'
                      '<span class="chip k-ov" title="Overseers"><b>2</b> DeepSeek V4.1 Flash overseers</span>', cards)
        self.assertIn('<dl class="pane-nums" data-static=1><div title="Accepted submissions', cards)
        # No compare feature on the math catalogue (owner, November 6, 2026).
        for gone in ('class="card-pick"', 'id="compare"', 'id="compare-view"', 'Tick 2 to 4', 'td class="pick"'):
            self.assertNotIn(gone, page)
        self.assertIn('const RP=(()=>{', page)
        self.assertIn('"deepseek/deepseek-v4.1-flash-1017": "DeepSeek V4.1 Flash"', page)

    def test_short_titles_and_replicate_order(self):
        for run, title in [('glmflash100-seeded-control-180m-notices-r1-20261028T195741Z', 'Control · r1'),
                           ('glmflash100-seeded-2ov-glm-180m-notices-r2-20261028T194424Z', '2 GLM overseers · r2'),
                           ('glmflash100-seeded-1ov-ds-60m-20261027T055433Z', '1 DeepSeek overseer'),
                           ('glm100-repeat2-20261022T055342Z', 'Repeat 2'),
                           ('glm100-baseline-90m-20261022T183812Z', 'Baseline, 90 min'),
                           ('glm-rule6-20261021T224441Z', 'With rule 6')]:
            self.assertEqual(short_title({'run': run, 'model': 'z-ai/glm-5.3'}), title)
        self.assertEqual(short_title({'run': 'gemini10-20261020T054602Z', 'model': 'google/gemini-3.1-pro-preview'}),
                         'Gemini 3.1 Pro')
        # Replicates in order within an overseer count, whatever their start times.
        r1 = entry('glmflash100-seeded-control-180m-notices-r1-20261028T195741Z', 9)
        r2 = entry('glmflash100-seeded-control-180m-notices-r2-20261028T194617Z', 8)
        page, _ = self.write([r1, r2])
        self.assertLess(page.index('data-run="' + r1['run']), page.index('data-run="' + r2['run']))

    def test_rendering_is_deterministic(self):
        es = [entry('glmflash100-seeded-2ov-ds-60m-20261027T055654Z', 5,
                    profile=run_profile.profile(results(), SETTINGS)), entry('owner-opus55-20261021T183459Z', 1)]
        self.assertEqual(self.write(es), self.write(list(reversed(es))))


class PaneTests(unittest.TestCase):
    """Run panes of the math catalogue (owner, November 6, 2026)."""

    PAGE = ('<p class="ab-legend" id="ab-legend"><strong>Agent labels</strong> (from the simplified behavior judge; '
            'hover a letter for the basis): ...</p>'
            + ''.join(f'<span class="ab-id">{a}<span class="ab-set">'
                      + ''.join(f'<span class="ab ab-x" title="t">{k}</span>' for k in marks) + '</span></span> '
                      for a, marks in [('agent-001', 'E'), ('agent-002', 'CA'), ('agent-002', 'CA'), ('agent-003', 'TW'),
                                       ('agent-004', 'U'), ('agent-005', '?'), ('agent-006', 'OTW'), ('agent-007', 'S')]))

    def test_agent_label_counts_once_per_agent(self):
        from build_experiment_index import agent_label_counts
        counts = agent_label_counts(self.PAGE)
        self.assertEqual(counts, {'source': 'simple', 'agents': 7, 'E': 1, 'C': 1, 'T': 1, 'U': 1, 'Q': 1, 'O': 1,
                                  'S': 1, 'W': 2})
        self.assertEqual(agent_label_counts(self.PAGE.encode())['agents'], 7)
        self.assertIsNone(agent_label_counts('<p>no labels</p>'))
        roles_only = self.PAGE.replace('simplified behavior judge', 'run roles only (no behavior judge)')
        self.assertIsNone(agent_label_counts(roles_only))

    def test_labels_block_and_pane_parts(self):
        run = 'glmflash100-seeded-1ov-glm-120m-push-r1-20261104T042820Z'
        e = entry(run, 5, profile=run_profile.profile(results(), SETTINGS))
        e['links']['Story map'] = run + '/analysis/storymap/index.html'
        e['links']['Simple behavior judge'] = run + '/analysis/simple_judge/report.html'
        with tempfile.TemporaryDirectory() as tmp:
            write_index(tmp, [e], hosted=True, labels={run: {'E': 1, 'source': 'simple'}, 'other': {'E': 9}})
            page = (Path(tmp) / 'index.html').read_text()
            experiments = json.loads((Path(tmp) / 'experiments.json').read_text())['experiments']
        self.assertIn('<script type="application/json" id="catalogue-labels">{"' + run + '":{"E":1,"source":"simple"}}'
                      '</script>', page)  # listed runs only
        self.assertNotIn('labels', json.dumps(experiments[0]))  # entries unchanged
        # Pane links open the run page's in-page tabs (owner, November 6, 2026).
        self.assertIn(f'data-thumb="{run}/analysis/storymap/graph-thumb.json" data-href="{run}/results.html#overview"',
                      page)
        self.assertNotIn(' href="' + run + '/results.html#overview"', page)  # set only once the thumb loads
        self.assertIn('<p class="pane-btns"><a href="' + run + '/results.html#judge">Behavior judge</a>'
                      '<a href="' + run + '/results.html#storymap">Story map</a></p>', page)
        self.assertIn('<a href="' + run + '/results.html#report">Report</a>', page)
        self.assertIn('<span class="chip k-push" title="Board posts and DMs are pushed into the next tool result">'
                      'push delivery</span>', page)
        self.assertIn('class="legend lg-graph"', page)
        self.assertNotIn('id="per-row"', page)  # the slider was removed again (owner, November 6, 2026)
        # Series sidebar by the series titles; the one-line summary under the heading and as the hover title.
        line = catalogue_intro.SERIES_LINES['push120']
        self.assertIn(f'<a href="#series-push120" data-series="push120" title="{line}"><span class="sn-t">'
                      '120-minute runs with pushed messages</span><span class="n">1</span></a>', page)
        self.assertIn(f'<span class="count">1 run</span></h2><p class="series-line">{line}</p><p>', page)


class PaneGraphTests(unittest.TestCase):
    """Owner, November 6, 2026: runs known to have a graph snapshot render the graph placeholder from the start (no
    sparkline flash on reload); others render the sparkline directly."""

    def render(self, graphs):
        run = 'glmflash100-seeded-1ov-glm-120m-push-r1-20261104T042820Z'
        e = entry(run, 5, profile=run_profile.profile(results(), SETTINGS))
        e['links']['Story map'] = run + '/analysis/storymap/index.html'
        with tempfile.TemporaryDirectory() as tmp:
            write_index(tmp, [e], hosted=True, graphs=graphs)
            return run, (Path(tmp) / 'index.html').read_text()

    def test_known_graph(self):
        run, page = self.render({run_key := 'glmflash100-seeded-1ov-glm-120m-push-r1-20261104T042820Z'})
        self.assertIn(f'<a class="pane-graph known" href="{run}/results.html#overview" data-thumb=', page)
        self.assertIn('<div class="rc-body" hidden data-profile="1">', page)  # the sparkline only as the fallback
        self.assertIn(f'<p class="pane-btns"><a class="b-graph" href="{run}/results.html#overview">'
                      'Interaction graph</a>', page)
        self.assertIn('class="legend lg-graph"', page)
        self.assertNotIn('<p class="legend"><span><i class="lg-area">', page)
        self.assertEqual(run, run_key)

    def test_known_without_graph_and_unknown(self):
        run, page = self.render(set())
        self.assertNotIn('class="pane-graph', page)
        self.assertIn('<div class="rc-body" data-profile="1">', page)
        self.assertNotIn('class="b-graph"', page)
        run, page = self.render(None)  # not known: found in the browser, as before
        self.assertIn('<a class="pane-graph" hidden data-thumb=', page)


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
        self.assertIn('href="../about.html#glossary"', page)
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
