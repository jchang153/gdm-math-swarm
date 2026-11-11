"""Explore page, About page and site skins (owner, November 10, 2026: an eye-catching Explore landing page with large
graphs, an Experiments page and an About page; three visual directions). Offline; no network or deploy."""
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import explore_page  # noqa: E402
import site_nav  # noqa: E402
import site_skin  # noqa: E402

GRAPH = 'analysis/storymap/graph.html'
HEAT = 'faking-heatmap.png'


def profile(minutes=120, curve=((10, 3, 3), (60, 5, 6)), accepted=6):
    return {'schema': 'gdm-run-profile/1', 'minutes': minutes, 'solvers': 100, 'model': 'z-ai/glm-5.3-flash',
            'overseers': None, 'seed': {'minute': 3.0, 'task': 't', 'reverted_minute': None}, 'notices': True,
            'checked': True, 'fakes': {'accepted': accepted, 'reverted': 0, 'standing_end': 5, 'peak': 5,
                                       'fake_minutes': 1.0, 'unresolved': 0},
            'curve': [[0, 0, 0]] + [list(p) for p in curve] + [[minutes, curve[-1][1], curve[-1][2]]]}


def entry(run, links=('Report',), prof=True):
    names = {'Report': 'results.html', 'Story map': 'analysis/storymap/index.html'}
    e = {'run': run, 'date': '2026-11-04 10:00 PST', 'start_utc': '2026-11-04T18:00:00+00:00', 'track': 'seeded',
         'agents': 102, 'model': 'z-ai/glm-5.3-flash', 'elapsed_seconds': 7200.0, 'pilot_spent_usd': '1.00',
         'links': {k: f'{run}/{names[k]}' for k in links}}
    if prof:
        e['profile'] = profile()
    return e


def highlights():
    return explore_page.load_highlights()


def all_story_runs(data):
    runs = {s['run'] for s in data['stories']}
    for s in data['stories']:
        for f in s.get('figures', []):
            if f.get('run'):
                runs.add(f['run'])
        for link in s.get('links', []):
            runs.add(link['run'])
    for row in data['hero']['chart']['rows']:
        runs.update(row.get('runs', []))
    return sorted(runs)


class HighlightsFile(unittest.TestCase):
    def test_shape(self):
        data = highlights()
        self.assertEqual(data['schema'], 'gdm-explore-highlights/1')
        self.assertTrue(4 <= len(data['stories']) <= 6)
        for s in data['stories']:
            for key in ('id', 'run', 'headline', 'takeaway', 'story', 'numbers', 'figures', 'sources'):
                self.assertIn(key, s, s.get('id'))
            self.assertTrue(all(n.get('value') and n.get('label') for n in s['numbers']))
            self.assertTrue(s['sources'])
        ids = [s['id'] for s in data['stories']] + [u['id'] for u in data['upcoming']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_story_order_and_runs(self):
        """Owner, November 11, 2026: exactly this sequence after the overview chart; the 90-minute spontaneous
        baseline story is gone."""
        stories = highlights()['stories']
        self.assertEqual([s['id'] for s in stories],
                         ['control', 'one-overseer', 'ten-overseers', 'reminders', 'talk-vs-enforce'])
        patterns = [r'^glmflash100-seeded-control-120m-push-r\d', r'^glmflash100-seeded-1ov-glm-120m-push-r\d',
                    r'^glmflash100-seeded-10ov-glm-120m-push-r\d', r'^glmflash100-seeded-reminder5m-120m-push-r\d',
                    r'^glmflash100-seeded-10ov-glm-180m-notices-(?:commsonly|enforceonly)-r\d']
        for s, pattern in zip(stories, patterns):
            self.assertRegex(s['run'], pattern)
        text = json.dumps(highlights())
        self.assertNotIn('baseline-90m', text)
        self.assertNotIn('baseline100', text)
        (talk,) = [s for s in stories if s['id'] == 'talk-vs-enforce']
        self.assertEqual([n['value'] for n in talk['numbers']], ['41.7 vs 88.1', '≈25', '174 / 332 / 647'])
        self.assertTrue(talk['figures'][0]['minus_seed'])

    def test_monitor_slot_has_no_numbers(self):
        (monitor,) = [u for u in highlights()['upcoming'] if u['id'] == 'monitors']
        self.assertEqual(monitor['status'], 'pending')
        self.assertNotIn('numbers', monitor)
        self.assertEqual(monitor['series'], 'monitor120')


class RenderExplore(unittest.TestCase):
    def setUp(self):
        self.data = highlights()
        self.entries = [entry(r, ('Report', 'Story map')) for r in all_story_runs(self.data)]
        self.files = {}
        for e in self.entries:
            self.files[f'{e["run"]}/{GRAPH}'] = b''
            self.files[f'{e["run"]}/{HEAT}'] = b''

    def test_other_sites_have_no_explore_page(self):
        self.assertIsNone(explore_page.render_explore(self.entries, self.files, site='ib'))
        self.assertIsNone(explore_page.render_about(site='ib'))
        self.assertIsNotNone(explore_page.render_explore(self.entries, self.files, site='gdm'))

    def test_deterministic_and_complete(self):
        a = explore_page.render_explore(self.entries, self.files)
        b = explore_page.render_explore(list(self.entries), dict(self.files))
        self.assertEqual(a, b)
        self.assertTrue(a.startswith('<!doctype html>'))
        for s in self.data['stories']:
            self.assertIn(f'id="{s["id"]}"', a)
        self.assertIn('href="experiments.html"', a)
        self.assertIn('href="about.html"', a)
        self.assertIn('aria-current="page">Explore', a)
        self.assertIn('id="site-theme"', a)  # shared theme and toggle

    def test_self_contained_and_no_private_files(self):
        page = explore_page.render_explore(self.entries, self.files)
        self.assertNotRegex(page, r'(?:src|href)="(?:https?:)?//')
        self.assertNotIn('<link', page)
        self.assertNotIn('@import', page)
        for private in ('.eval', 'calls.jsonl', 'timeline', '/inspect/', 'raw/', 'snapshots'):
            self.assertNotIn(private, page)

    def test_graph_iframe_when_published(self):
        page = explore_page.render_explore(self.entries, self.files)
        run = 'glmflash100-seeded-10ov-glm-120m-push-r2-20261104T043647Z'
        self.assertIn(f'<iframe title="Interaction graph of {run}', page)
        self.assertIn(f'data-graph="{run}/{GRAPH}"', page)

    def test_fallback_graph_to_curve(self):
        run = 'glmflash100-seeded-10ov-glm-120m-push-r2-20261104T043647Z'
        files = dict(self.files)
        del files[f'{run}/{GRAPH}']
        page = explore_page.render_explore(self.entries, files)
        self.assertNotIn(f'data-graph="{run}/', page)
        self.assertNotIn(f'{run}/{HEAT}', page)
        self.assertIn('Fake proofs standing over the run', page)

    def test_no_heatmap_anywhere(self):
        """Owner, November 11, 2026: no heat maps on Explore (they stay on the run pages), even when every run has
        one published and a curated figure asks for one."""
        data = json.loads(json.dumps(self.data))
        data['stories'][0]['figures'] = [{'kind': 'heatmap', 'run': data['stories'][0]['run'], 'caption': 'x'}]
        for page in (explore_page.render_explore(self.entries, self.files),
                     explore_page.render_explore(self.entries, self.files, highlights=data),
                     explore_page.render_explore(self.entries)):
            self.assertNotIn(HEAT, page)
            self.assertNotRegex(page.lower(), r'heat[ -]?map')
            self.assertNotIn('<img', page)
        for s in self.data['stories']:
            self.assertFalse([f for f in s['figures'] if f.get('kind') == 'heatmap'], s['id'])

    def test_story_without_figures_is_a_text_card(self):
        data = json.loads(json.dumps(self.data))
        data['stories'][0]['figures'] = []
        page = explore_page.render_explore(self.entries, self.files, highlights=data)
        self.assertIn('class="sk-split sk-textcard"', page)
        self.assertIn('class="sk-num sk-numcard"', page)

    def test_side_by_side_and_one_column_on_phones(self):
        page = explore_page.render_explore(self.entries, self.files)
        self.assertIn('class="sk-split"', page)
        self.assertIn('class="sk-figcol"', page)
        css = site_skin.skin_css()
        self.assertIn('@media (max-width:760px){.sk-split', css)
        self.assertIn('font:16px/1.6', css)

    def test_missing_anchor_run_drops_the_story(self):
        drop = self.data['stories'][0]
        entries = [e for e in self.entries if e['run'] != drop['run']]
        page = explore_page.render_explore(entries, self.files)
        self.assertNotIn(f'id="{drop["id"]}"', page)
        self.assertIn(f'id="{self.data["stories"][1]["id"]}"', page)
        self.assertIsNotNone(explore_page.render_explore([], {}))  # empty site still renders

    def test_charts_from_entries(self):
        page = explore_page.render_explore(self.entries, self.files)
        self.assertIn('class="sk-dots"', page)  # hero chart (curated counts)
        self.assertIn('class="sk-wide"', page)  # lines/bars from profiles, with phone variants
        self.assertIn('class="sk-narrow"', page)

    def test_monitor_slot(self):
        page = explore_page.render_explore(self.entries, self.files)
        self.assertIn('Runs in progress. Results will appear here when they land.', page)
        monitor = entry('glmflash100-seeded-monitor-full-reason-120m-push-r1-20261111T021254Z')
        page = explore_page.render_explore(self.entries + [monitor], self.files)
        self.assertIn('1 run published so far.', page)
        self.assertIn('experiments.html#series-monitor120', page)

    def test_story_order_on_page_and_no_baseline(self):
        page = explore_page.render_explore(self.entries, self.files)
        at = [page.index(f'id="{i}"') for i in ('control', 'one-overseer', 'ten-overseers', 'reminders',
                                                'talk-vs-enforce', 'monitors')]
        self.assertEqual(at, sorted(at))
        self.assertLess(page.index('class="sk-dots"'), at[0])  # the overview chart comes first
        self.assertNotIn('90-minute', page)
        self.assertNotIn('glm100-baseline-90m', page)
        self.assertNotIn('id="copying"', page)

    def test_bars_minus_seed(self):
        spec = {'chart': 'bars', 'title': 't', 'minus_seed': True, 'categories': [
            {'label': 'c', 'bars': [{'label': 'b', 'color': 1, 'match': '^x-'}]}]}
        entries = [dict(entry('x-1'), profile=profile(accepted=26)), dict(entry('x-2'), profile=profile(accepted=24))]
        self.assertIn('average 24</title>', explore_page._bars_chart(spec, entries))
        self.assertIn('average 25</title>', explore_page._bars_chart(dict(spec, minus_seed=False), entries))

    def test_totals(self):
        page = explore_page.render_explore(self.entries, self.files)
        self.assertIn('agent-hours of run time', page)
        self.assertNotIn('agent-hours of work', page)
        self.assertIn(f'<dd>{len(self.entries)}</dd>', page)
        self.assertIn(f'<dd>{102 * len(self.entries):,}</dd>', page)


class Skins(unittest.TestCase):
    def test_every_skin_renders_light_and_dark(self):
        self.assertIn(site_skin.DEFAULT, site_skin.SKINS)
        self.assertEqual(len(site_skin.SKINS), 3)
        for name, skin in site_skin.SKINS.items():
            self.assertEqual(set(skin['light']), set(skin['dark']), name)
            css = site_skin.skin_css(name)
            self.assertIn(':root[data-theme="dark"]', css)
            self.assertIn('prefers-color-scheme:dark', css)
            self.assertIn('@media (max-width:640px)', css)
            self.assertNotRegex(css, r'url\(|@import|https?:')
            tokens = site_skin.skin_css(name, scope='tokens')
            self.assertIn('--bg:var(--k-bg)', tokens)
            page = explore_page.render_explore([], {}, skin=name)
            self.assertIn(f'data-skin="{name}"', page)
            about = explore_page.render_about(skin=name)
            self.assertIn('aria-current="page">About', about)
        self.assertEqual(site_skin.skin_css('nope'), site_skin.skin_css(site_skin.DEFAULT))

    def test_text_contrast_aa(self):
        def lum(hexcolor):
            rgb = [int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
            return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

        def ratio(a, b):
            hi, lo = sorted((lum(a), lum(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)
        pairs = (('ink', 'bg'), ('soft', 'bg'), ('muted', 'bg'), ('ink', 'card'), ('muted', 'card'),
                 ('accent', 'bg'), ('on-accent', 'accent'), ('hero-ink', 'hero'), ('hero-soft', 'hero'),
                 ('block-ink', 'block'))
        for name, skin in site_skin.SKINS.items():
            for mode in ('light', 'dark'):
                t = skin[mode]
                for fg, bg in pairs:
                    self.assertGreaterEqual(ratio(t[fg], t[bg]), 4.5, f'{name} {mode} {fg} on {bg}')
                for c in ('c1', 'c2', 'c3', 'c4', 'c5'):  # chart marks and large numbers: 3:1 (WCAG 1.4.11, large text)
                    self.assertGreaterEqual(ratio(t[c], t['card']), 3.0, f'{name} {mode} {c} on card')

    def test_nav(self):
        bar = site_nav.bar('experiments', prefix='../')
        self.assertIn('href="../experiments.html" aria-current="page"', bar)
        self.assertEqual(len(re.findall('aria-current', bar)), 1)


if __name__ == '__main__':
    unittest.main()
