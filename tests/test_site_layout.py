"""The dense layout (scripts/site_layout.py) and its guarded rollout (publish_cloudflare.py restyle)."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import site_layout  # noqa: E402
from site_style import themed  # noqa: E402

RESULTS = themed(
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>r</title><style>body{}</style></head><body>'
    '<h1>run</h1><section class="run-intro" aria-label="About this run"><style>.run-intro{}</style><p>Intro</p>'
    '</section><!--/run-intro--><section class="card" aria-label="Run summary"><div class="tile">t</div></section>'
    '<nav class="tabs"></nav><ul class="tabhelp"><li>help</li></ul><main id="report">'
    '<h2>Run</h2><table><tr><td>Start</td><td>+0:00</td></tr></table>'
    '<h2>Condition</h2><table><tr><td>Model</td><td>m</td></tr></table>'
    '<h2>Agents</h2><div class="scroll"><table><tr><th>Agent</th></tr><tr><td>agent-001</td></tr></table></div>'
    '<h2 id="statuses">What the statuses mean</h2><h3>Stop reason</h3><dl class="gloss"><dt>a</dt><dd>b</dd></dl>'
    '<h3>Agent exits</h3><dl class="gloss"><dt>c</dt><dd>d</dd></dl><h2>Records</h2><p>x</p></main></body></html>')


def text(document):
    """Visible text and link targets, without the layout's own style block."""
    document = re.sub(r'<style id="site-layout">.*?</style>', '', document, flags=re.S)
    return re.sub(r'<(?!a )[^>]*>', '', document), re.findall(r'href="([^"]*)"', document)


class LayoutTest(unittest.TestCase):
    def test_results_page_wraps_sections_and_keeps_content(self):
        page = site_layout.apply('RUN/results.html', RESULTS)
        self.assertEqual(page.count(site_layout.MARKER), 1)
        self.assertIn('<div class="lay-lead"><section class="run-intro"', page)
        self.assertIn('<div class="lay-cols lay-facts"><section><h2>Run</h2>', page)
        self.assertIn('<div class="lay-cols lay-gloss"><section><h3>Stop reason</h3>', page)
        self.assertEqual(text(page), text(RESULTS))
        self.assertEqual(site_layout.apply('RUN/results.html', page), page)  # idempotent
        # The layout style follows the page's own head styles and precedes <body>.
        self.assertLess(page.index('<style>body{}</style>'), page.index(site_layout.MARKER))
        self.assertLess(page.index(site_layout.MARKER), page.index('<body'))

    def test_style_block_is_replaced_not_duplicated(self):
        old = RESULTS.replace('</head>', '<style id="site-layout">.old{}</style></head>')
        page = site_layout.apply('results.html', old)
        self.assertEqual(page.count(site_layout.MARKER), 1)
        self.assertNotIn('.old{}', page)

    def test_other_kinds_and_unknown_pages(self):
        conference = themed('<!doctype html><html><meta charset="utf-8"><title>c</title><style>p{}</style>'
                            '<main id="records"></main></html>')
        page = site_layout.apply('RUN/conference-view/index.html', conference)
        self.assertIn('#records{display:grid', page)
        self.assertEqual(site_layout.kind('RUN/analysis/simple_judge/report.html'), 'simple_judge')
        for name in ('index.html', 'RUN/analysis/incident/report.html', 'RUN/conference-view/files.html',
                     'RUN/inspect-view/index.html'):
            self.assertIsNone(site_layout.kind(name))
            self.assertEqual(site_layout.apply(name, conference), conference)

    def test_css_is_tokens_only_and_phone_safe(self):
        for css in (site_layout.RESULTS_CSS, site_layout.CONFERENCE_CSS, site_layout.SIMPLE_JUDGE_CSS):
            self.assertNotRegex(css, r'url\(|@import|https?:')  # no external resources
            self.assertIn('70ch', css)
            self.assertIn('max-width:1120px', css)  # one centred column
        # Grids never force a column wider than the phone viewport.
        self.assertIn('minmax(min(440px,100%)', site_layout.RESULTS_CSS)
        self.assertIn('minmax(min(330px,100%)', site_layout.CONFERENCE_CSS)


class RestyleTest(unittest.TestCase):
    def test_plan_restyle_changes_only_known_themed_pages(self):
        import json
        import publish_cloudflare as pc
        entry_files = {'experiments.json': json.dumps({'experiments': []}).encode()}
        base = {**entry_files,
                'RUN/results.html': RESULTS.encode(),
                'RUN/results.json': b'{"a": 1}',
                'RUN/analysis/incident/report.html': themed('<p>incident</p>').encode(),
                'OLD/results.html': b'<!doctype html><p>legacy page without the theme</p>',
                'OLD/results.json': b'{}'}
        files, plan = pc.plan_restyle(base, set())
        self.assertEqual([r['path'] for r in plan['replaced_pages']], ['RUN/results.html'])
        self.assertEqual(plan['unthemed_pages_left_as_served'], ['OLD/results.html'])
        for name in ('RUN/results.json', 'RUN/analysis/incident/report.html', 'OLD/results.html', 'OLD/results.json'):
            self.assertEqual(files[name], base[name])
        self.assertEqual(set(files) - set(base), {'index.html'})
        self.assertIn(site_layout.MARKER.encode(), files['RUN/results.html'])
        with self.assertRaisesRegex(ValueError, 'not on the site'):
            pc.plan_restyle(base, set(), ['NOPE'])
        only, plan = pc.plan_restyle(base, set(), ['OLD'])
        self.assertEqual(plan['replaced_pages'], [])


def badge(agent, *kinds):
    return (f'<span class="ab-id">{agent}<span class="ab-set">' + ''.join(
        f'<span class="ab ab-{k}" title="{k}">x</span>' for k in kinds) + '</span></span>')


RUN_ID = 'glmflash100-seeded-1ov-glm-120m-push-r1-20261104T042820Z'
# A run page as published (Nov 2026): crumbs, id heading, intro with a stale series, tiles, tab bar, agent labels.
RUN_PAGE = themed(
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>r</title><style>body{}</style></head><body>'
    f'<p class="crumbs"><a href="../index.html">All experiments</a></p><h1>{RUN_ID}</h1>'
    f'<p><code>{RUN_ID}</code> · +0:00 to +2:00:01</p>'
    '<section class="run-intro" aria-label="About this run"><style>.run-intro{}</style>'
    '<p class="ri-cond">100 GLM 5.3 Flash solvers, 1 GLM 5.3 Flash overseer with check_proof and revert powers, '
    'exploit planted at +3 min, 2 h.</p><p><span class="ri-k">Series</span><strong>Earlier pilots.</strong> '
    'DeepSeek text.</p><figure><svg class="spark"></svg></figure></section><!--/run-intro-->'
    '<section class="card" aria-label="Run summary"><div class="tile"><div class="k">Model</div>'
    '<div class="v">z-ai/glm-5.3-flash</div><div class="s">route</div></div><div class="tile"><div class="k">'
    'Prompt variant</div><div class="v">collab</div><div class="s">as recorded</div></div></section>'
    '<nav class="tabs" aria-label="Run records"><a class="current" aria-current="page" href="#report">Report</a>'
    '<a href="conference-view/index.html">Conference</a><a data-tab="simple-judge" '
    'href="analysis/simple_judge/report.html">Behavior judge</a><a data-tab="incident" '
    'href="analysis/incident/report.html">Incident report</a><a data-tab="storymap" '
    'href="analysis/storymap/index.html">Story map</a><a href="inspect-view/index.html">Inspect</a></nav>'
    '<ul class="tabhelp"><li>help</li></ul><main id="report"><h2>Agents</h2><table>'
    + ''.join(f'<tr><td>{b}</td></tr>' for b in (
        badge('agent-001', 'convert'), badge('agent-002', 'exploiter'), badge('agent-003', 'tempted', 'whistleblower'),
        badge('agent-004', 'unaware'), badge('agent-005', 'seed'), badge('agent-006', 'overseer', 'whistleblower')))
    + '</table><h2>Accepted submissions</h2><table><tr><td>' + badge('agent-001', 'convert') + '</td></tr><tr><td>'
    + badge('agent-002', 'exploiter') + '</td></tr></table></main></body></html>')
REPORT = {'counts': {'labelled': 4, 'cheating_any': 2, 'whistleblower': {'True': 2}, 'evaluation_aware': {'True': 0},
                     'cheating_level': {'none': 1, 'considered': 1, 'explored': 0, 'submitted': 1, 'credited': 1,
                                        'unclear': 0}}}


class OverviewTest(unittest.TestCase):
    """Run overview at the top of results.html (owner request and decisions, November 6, 2026)."""

    def read(self, files):
        return lambda path: files.get(path)

    def test_overview_header_tabs_and_judge_card(self):
        import json
        files = {'analysis/simple_judge/report.json': json.dumps(REPORT).encode(),
                 'analysis/storymap/graph.html': b'<!doctype html>'}
        page = site_layout.apply(f'{RUN_ID}/results.html', RUN_PAGE, read=self.read(files))
        self.assertEqual(page.count(site_layout.OV_START), 1)
        self.assertLess(page.index(site_layout.OV_START), page.index(f'<h1>{RUN_ID}</h1>'))
        self.assertIn('100 GLM 5.3 Flash solvers · 1 overseer (check + revert) · 2 h · push · r1', page)
        self.assertIn('<li><b>Delivery</b>pushed messages</li>', page)
        # Tabs: one bar, in the owner's order; every old link kept.
        self.assertEqual(page.count('<nav class="tabs"'), 1)
        labels = re.findall(r'>([^<>]+)</a>', re.search(r'<nav class="tabs".*?</nav>', page, re.S)[0])
        self.assertEqual(labels, ['Overview', 'Behavior judge', 'Story map', 'Conference', 'Incident report',
                                  'Report details', 'Inspect'])
        # Category bar from the page's labels; judge card from report.json; graph embedded.
        self.assertIn('aria-label="Agent labels: Exploiter 1, Convert 1, Tempted 1, Unaware / honest 1"', page)
        self.assertIn('src="analysis/storymap/graph.html?embed=1&amp;t=end"', page)  # opens on the end state
        self.assertIn('#ov-tabs{display:flex;flex-wrap:nowrap;overflow-x:auto', page)  # one swipeable row on phones
        self.assertIn('<span>submitted</span>', page)
        notable = re.findall(r'href="analysis/storymap/agents/(agent-\d+)\.html"', page)
        self.assertEqual(notable, ['agent-002', 'agent-003', 'agent-006', 'agent-001'])
        # Stale series text replaced from run_profile.SERIES; nothing else of the page lost.
        self.assertNotIn('Earlier pilots', page)
        self.assertIn('120-minute runs with pushed messages', page)
        for kept in ('conference-view/index.html', 'inspect-view/index.html', '<h2>Agents</h2>', 'href="#report"'):
            self.assertIn(kept, page)
        self.assertEqual(site_layout.apply(f'{RUN_ID}/results.html', page, read=self.read(files)), page)

    def test_tabs_switch_panels_in_place(self):
        """Owner, November 6, 2026: one page whose tabs switch panels instead of leaving the page."""
        page = site_layout.apply(f'{RUN_ID}/results.html', RUN_PAGE)
        nav = re.search(r'<nav class="tabs" id="ov-tabs"[^>]*>(.*?)</nav>', page, re.S)[1]
        self.assertEqual(re.findall(r'data-ov="([^"]+)"', nav), ['overview', 'report'])  # the page's own panels
        # The other tabs keep their exact markup (simple_judge_site / storymap_site find them by it); the script
        # keys their panels by href prefix.
        self.assertIn('<a data-tab="storymap" href="analysis/storymap/index.html">Story map</a>', nav)
        for prefix in ('judge:"analysis/simple_judge/"', '"judge-rubric":"analysis/judge/"', 'inspect:"inspect-view/"'):
            self.assertIn(prefix, page)
        # Real hrefs kept: without JavaScript (or with a modifier click) the tabs are plain links.
        for href in ('#overview', 'analysis/simple_judge/report.html', 'analysis/storymap/index.html',
                     'conference-view/index.html', 'analysis/incident/report.html', '#report', 'inspect-view/index.html'):
            self.assertIn(f'href="{href}"', nav)
        # Tab bar outside the header card (sticky over the whole page), then the overview row, the panel, the
        # script and the end marker that the panel CSS hides the report body after.
        order = [page.index(x) for x in ('class="ov-head"', 'id="ov-tabs"', 'class="ov-row"', 'id="ov-panel" hidden',
                                         'history.pushState', 'id="ov-end"', site_layout.OV_END)]
        self.assertEqual(order, sorted(order))
        self.assertIn('html[data-ov-tab]:not([data-ov-tab="report"]) #ov-end~*:not(.site-theme-toggle){display:none}',
                      page)
        self.assertNotIn('data-ov-tab', page.split('<body', 1)[1].split('<script>', 1)[0])  # no JS: all visible
        # Only the active tab's iframe shows: the author display rule must not override [hidden] (bug, Nov 6).
        self.assertIn('#ov-frames iframe[hidden]{display:none}', page)
        self.assertNotIn('scrollTo(0,', page)  # a tab switch never moves the page (owner, November 6, 2026)
        self.assertIn('if(window.pageYOffset!==y)window.scrollTo(window.pageXOffset,y);', page)
        self.assertEqual(site_layout.apply(f'{RUN_ID}/results.html', page), page)  # idempotent
        # A rubric-3.1 judge tab gets its own panel next to the behavior judge.
        rubric = RUN_PAGE.replace('Behavior judge</a>', 'Behavior judge</a><a data-tab="judge-rubric-3-1" '
                                  'href="analysis/judge/report.html">Behavior judge (rubric 3.1)</a>')
        self.assertIn('Behavior judge</a><a data-tab="judge-rubric-3-1" href="analysis/judge/report.html">',
                      site_layout.apply('results.html', rubric))

    def test_without_graph_or_judge_and_page_only(self):
        no_map = RUN_PAGE.replace('<a data-tab="storymap" href="analysis/storymap/index.html">Story map</a>', '')
        page = site_layout.apply('results.html', no_map)
        self.assertNotIn('<iframe', page)
        self.assertIn('Standing fakes over the run', page)
        self.assertIn('<svg class="spark">', page.split(site_layout.OV_END)[0])
        self.assertIn('href="analysis/simple_judge/report.html">agent-002', page.replace('<code>', '')
                      .replace('</code>', ''))
        # Story map tab present, no read: the graph follows the tab (every story-mapped run has one).
        self.assertIn('<iframe', site_layout.apply('results.html', RUN_PAGE))

    def test_math_only(self):
        self.assertNotIn(site_layout.OV_START, site_layout.apply('results.html', RUN_PAGE, overview=False))
        coding = RUN_PAGE.replace('<h2>Agents</h2>', '<h2>Reference-test evaluator</h2>')
        page = site_layout.apply('results.html', coding)
        self.assertNotIn(site_layout.OV_START, page)
        self.assertNotIn('.ov-head', page)

    def test_viewer_has_embed_mode(self):
        """The overview iframe loads graph.html?embed=1: the shared viewer hides its header and side panel."""
        template = (ROOT / 'scripts' / 'storymap_graph_template.html').read_text(encoding='utf-8')
        self.assertIn('html.embed header, html.embed aside { display: none; }', template)
        self.assertIn('q.get("embed") === "1"', template)
        self.assertIn('setTime(new URLSearchParams(location.search).get("t") === "end" ? tMax : tMin)', template)
        self.assertIn('if (playing && tNow >= tMax) setTime(tMin);', template)  # Play from the end rewinds

    def test_restyle_reads_run_files_and_respects_site(self):
        import json
        import publish_cloudflare as pc
        base = {'experiments.json': json.dumps({'experiments': []}).encode(),
                f'{RUN_ID}/results.html': RUN_PAGE.encode(),
                f'{RUN_ID}/analysis/simple_judge/report.json': json.dumps(REPORT).encode()}
        files, _ = pc.plan_restyle(base, set())
        self.assertIn(b'<span>credited</span>', files[f'{RUN_ID}/results.html'])
        files, _ = pc.plan_restyle(base, set(), site=pc.SITES['ib'])
        self.assertNotIn(site_layout.OV_START.encode(), files[f'{RUN_ID}/results.html'])


if __name__ == '__main__':
    unittest.main()
