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


if __name__ == '__main__':
    unittest.main()
