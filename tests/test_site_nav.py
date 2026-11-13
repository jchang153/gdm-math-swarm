"""The math site's root pages and top bar (owner, November 10, 2026: "make the website more user-facing"; a separate
About page): index.html is the Explore page, experiments.html the catalogue, about.html the explanations, and every
generated page carries Explore | Experiments | About (scripts/site_nav.py). Offline; no network or deploy."""
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_experiment_index as index  # noqa: E402
import publish_cloudflare as cf  # noqa: E402
import site_layout  # noqa: E402
import site_nav  # noqa: E402
from site_style import themed  # noqa: E402

RUN = 'nav-run-20261110T000000Z'
OTHER = 'nav-other-20261109T000000Z'


def entry(run, hour):
    return {'run': run, 'date': '2026-11-10 10:00 PST', 'start_utc': f'2026-11-10T{hour:02d}:00:00+00:00',
            'track': 'spontaneous', 'agents': 10, 'model': 'vendor/model', 'provider_route': 'route/us',
            'elapsed_seconds': 3600, 'pilot_spent_usd': '1.00', 'distinct_verified_tasks': 1, 'task_count': 14,
            'agent_exits': {'harness_completed': 10}, 'links': {'Report': run + '/results.html'}}


# A run page as published before November 10, 2026: themed, with the old library links.
OLD_RESULTS = themed('<!doctype html><html lang="en"><head><style>body{margin:0}</style></head><body>'
                     '<p class="crumbs"><a href="../index.html">All experiments</a></p><h1>Run</h1>'
                     '<p>Terms: the <a href="../index.html#glossary">library glossary</a>.</p></body></html>')
OLD_CONFERENCE = themed('<!doctype html><html lang="en"><head><style>body{margin:0}</style></head><body>'
                        '<p><a href="../../index.html">Experiment library</a> · <a href="../results.html">Run report'
                        '</a></p><h1>Inside the conference</h1></body></html>')


def site():
    with tempfile.TemporaryDirectory() as tmp:
        index.write_index(tmp, [entry(RUN, 10), entry(OTHER, 9)], hosted=True)
        files = {n: (Path(tmp) / n).read_bytes() for n in ('index.html', 'experiments.json')}
    for run in (RUN, OTHER):
        files[f'{run}/results.html'] = OLD_RESULTS.encode()
        files[f'{run}/conference-view/index.html'] = OLD_CONFERENCE.encode()
    return files


def nav_parts(page):
    """The top bar's style block and markup (current-page mark removed) of a page."""
    style = re.findall(r'<style id="site-nav-style">.*?</style>', page, re.S)
    bar = re.findall(r'<nav id="site-nav".*?</nav>', page, re.S)
    return style, [b.replace(' aria-current="page"', '') for b in bar]


class SameBarEverywhere(unittest.TestCase):
    """Owner, November 11, 2026: clicking "Experiments" moved the whole top bar. Explore, Experiments and About (and
    the run pages) carry one identical bar: the same style block and the same markup."""

    def test_explore_experiments_and_about_share_one_bar(self):
        import explore_page
        files = site()
        pages = {'explore': explore_page.render_explore([entry(RUN, 10)], files),
                 'experiments': files['index.html'].decode('utf-8'),
                 'about': explore_page.render_about(),
                 'results': site_layout.apply(f'{RUN}/results.html', OLD_RESULTS)}
        expected = ([site_nav.style()], [site_nav.bar(None)])
        for key, page in pages.items():
            style, bar = nav_parts(page)
            self.assertEqual(style, expected[0], key)
            if key == 'results':
                self.assertEqual(bar, [site_nav.bar(None, '../')], key)
            else:
                self.assertEqual(bar, expected[1], key)
                self.assertIn(f'href="{dict((k, t) for k, t, _ in site_nav.PAGES)[key]}" aria-current="page"', page)
            # No second, page-specific bar.
            self.assertNotIn('sk-nav', page, key)
            self.assertNotIn('class="topnav"', page, key)

    def test_bar_never_wraps_and_ignores_the_page_column(self):
        css = site_nav.CSS
        self.assertIn('scrollbar-gutter:stable', css)
        self.assertIn('width:100vw', css)
        self.assertIn('margin:0 calc(50% - 50vw)', css)
        self.assertIn('white-space:nowrap', css.split('.sn-brand{')[1].split('}')[0])


class BarTests(unittest.TestCase):
    def test_bar_marks_the_current_page_and_is_idempotent(self):
        page = '<!doctype html><html><head><title>x</title></head><body><p>hi</p></body></html>'
        once = site_nav.apply(page, 'about', '../')
        self.assertIn('<nav id="site-nav" aria-label="Site">', once)
        self.assertIn('<a href="../about.html" aria-current="page">About</a>', once)
        self.assertIn('<a href="../experiments.html">Experiments</a>', once)
        self.assertIn('<a href="../index.html">Explore</a>', once)
        self.assertLess(once.index('<body>'), once.index('<nav id="site-nav"'))
        self.assertLess(once.index(site_nav.STYLE_MARKER), once.index('</head>'))
        again = site_nav.apply(once, 'explore', '')
        self.assertEqual(again.count('<nav id="site-nav"'), 1)
        self.assertEqual(again.count(site_nav.STYLE_MARKER), 1)
        self.assertIn('<a href="index.html" aria-current="page">Explore</a>', again)
        self.assertEqual(site_nav.apply(again, 'explore', ''), again)

    def test_page_without_body_gets_the_bar_before_its_content(self):
        # The published conference views have no <body>: head styles run straight into the first paragraph.
        page = ('<meta charset="utf-8"><script>var a="<p>";</script><style>p{}</style><p><a href="../../index.html">'
                'Experiment library</a></p><h1>Inside the conference</h1>')
        out = site_layout.apply(f'{RUN}/conference-view/index.html', page)
        self.assertLess(out.index(site_nav.STYLE_MARKER), out.index('<nav id="site-nav"'))
        self.assertLess(out.index('<style>p{}</style>'), out.index('<nav id="site-nav"'))
        self.assertLess(out.index('<nav id="site-nav"'), out.index('<p><a href="../../experiments.html">'))
        self.assertIn('<a href="../../about.html">About</a>', out)
        self.assertEqual(site_layout.apply(f'{RUN}/conference-view/index.html', out), out)

    def test_prefix_from_the_run_folder(self):
        self.assertEqual(site_nav.prefix_for('results.html'), '../')
        self.assertEqual(site_nav.prefix_for('conference-view/index.html'), '../../')
        self.assertEqual(site_nav.prefix_for('analysis/simple_judge/report.html'), '../../../')

    def test_old_library_links_point_at_experiments_and_about(self):
        page = site_nav.back_links(OLD_RESULTS + OLD_CONFERENCE)
        self.assertIn('<a href="../experiments.html">All experiments</a>', page)
        self.assertIn('<a href="../about.html#glossary">library glossary</a>', page)
        self.assertIn('<a href="../../experiments.html">Experiment library</a>', page)
        self.assertIn('<a href="../results.html">Run report</a>', page)  # other links unchanged

    def test_hf_space_pages_keep_the_old_library_links(self):
        page = site_layout.apply(f'{RUN}/results.html', OLD_RESULTS)
        hf = site_nav.for_hf_space(page)
        self.assertNotIn('site-nav', hf)
        self.assertIn('<a href="../index.html">All experiments</a>', hf)
        self.assertIn('<a href="../index.html#glossary">library glossary</a>', hf)

    def test_forwarder_once(self):
        page = site_nav.with_forwarder(site_nav.with_forwarder('<html><body><p>x</p></body></html>'))
        self.assertEqual(page.count('id="catalogue-forward"'), 1)
        self.assertLess(page.index('catalogue-forward'), page.index('</body>'))


class RunPageTests(unittest.TestCase):
    def test_layout_adds_the_bar_with_relative_paths(self):
        for name, prefix in (('results.html', '../'), (f'{RUN}/results.html', '../'),
                             (f'{RUN}/conference-view/index.html', '../../'),
                             (f'{RUN}/analysis/simple_judge/report.html', '../../../'),
                             (f'{RUN}/analysis/incident/report.html', '../../../'),
                             (f'{RUN}/analysis/storymap/index.html', '../../../')):
            page = site_layout.apply(name, OLD_RESULTS)
            self.assertIn(f'<a href="{prefix}experiments.html">Experiments</a>', page, name)
            self.assertIn(f'<a class="sn-brand" href="{prefix}index.html" aria-label="GDM Math Swarm">', page, name)
            self.assertEqual(site_layout.apply(name, page), page, name)  # idempotent

    def test_imposible_bench_pages_get_no_bar(self):
        page = site_layout.apply('results.html', OLD_RESULTS, overview=False)
        self.assertNotIn('site-nav', page)
        self.assertIn('href="../index.html">All experiments', page)


class RootPageTests(unittest.TestCase):
    def test_render_writes_explore_catalogue_and_about(self):
        files = site()
        pages = cf.render_catalogue(cf.catalogue(files), (), files)
        self.assertEqual(sorted(pages), ['about.html', 'experiments.html', 'experiments.json', 'index.html'])
        explore = pages['index.html'].decode()
        self.assertIn('<a href="index.html" aria-current="page">Explore</a>', explore)  # its own bar only
        self.assertEqual(explore.count('aria-label="Site"'), 1)
        self.assertIn('id="catalogue-forward"', explore)
        catalogue = pages['experiments.html'].decode()
        self.assertIn('<a href="experiments.html" aria-current="page">Experiments</a>', catalogue)
        self.assertIn('id="runs"', catalogue)
        self.assertIn('href="about.html"', catalogue)
        self.assertNotIn('<details id="glossary">', catalogue)
        about = pages['about.html'].decode()
        self.assertIn('<a href="about.html" aria-current="page">About</a>', about)
        for anchor in ('id="experiment"', 'id="how"', 'id="read"', 'id="glossary"'):  # the forwarder's targets
            self.assertIn(anchor, about)
        for term in ('Fake-minutes', 'Standing fake', 'Converts (detailed judge)'):  # run pages link about.html#glossary
            self.assertIn(f'<dt>{term}</dt>', about)
        self.assertIn('data-skin="editorial"', catalogue)  # the owner's pick: editorial tokens on the catalogue

    def test_ib_site_keeps_its_catalogue_as_index(self):
        pages = cf.render_catalogue([entry(RUN, 10)], (), {}, site='ib')
        self.assertEqual(sorted(pages), ['experiments.json', 'index.html'])
        self.assertNotIn(b'site-nav', pages['index.html'])
        self.assertIn(b'id="runs"', pages['index.html'])

    def test_restyle_adds_the_bar_and_leaves_no_broken_link(self):
        files, plan = cf.plan_restyle(site(), ())
        for name in ('index.html', 'experiments.html', 'about.html'):
            self.assertIn(name, files)
        page = files[f'{RUN}/results.html'].decode()
        self.assertIn('<a href="../experiments.html">All experiments</a>', page)
        self.assertIn('<a href="../about.html">About</a>', page)
        conference = files[f'{RUN}/conference-view/index.html'].decode()
        self.assertIn('<a href="../../experiments.html">Experiment library</a>', conference)
        self.assertEqual(len(plan['replaced_pages']), 4)
        self.assertEqual(cf.broken_links(files), [])
        again, plan = cf.plan_restyle(files, ())
        self.assertEqual(plan['replaced_pages'], [])
        out, manifest = cf.finish(files, {}, plan)  # the site gate accepts the result
        self.assertIn('href="experiments.html"', out[cf.PRIVATE_PAGE].decode())
        for name in ('index.html', 'experiments.html', 'about.html'):
            self.assertIn(name, manifest['files'])

    def test_catalogue_plan_lists_every_root_page(self):
        files, plan = cf.plan_catalogue(site(), unlisted=())
        self.assertEqual(plan['changed_files'], ['about.html', 'experiments.html', 'index.html'])
        self.assertEqual(cf.plan_catalogue(files, unlisted=())[1]['changed_files'], [])

    def test_local_library_writes_both_names_and_about(self):
        with tempfile.TemporaryDirectory() as tmp:
            index.write_index(tmp, [entry(RUN, 10)])
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(names, ['about.html', 'experiments.html', 'experiments.json', 'index.html'])
            self.assertEqual((Path(tmp) / 'index.html').read_bytes(), (Path(tmp) / 'experiments.html').read_bytes())
        with tempfile.TemporaryDirectory() as tmp:  # hosted: index.html only (render_catalogue names the pages)
            index.write_index(tmp, [entry(RUN, 10)], hosted=True)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['experiments.json', 'index.html'])

    def test_skin_layers_on_top(self):
        fake = types.ModuleType('site_skin')
        fake.skin_css = lambda name, scope='full': f'.skin-{name}-{scope}{{color:red}}'
        with patch.dict(sys.modules, {'site_skin': fake}), tempfile.TemporaryDirectory() as tmp:
            index.write_index(tmp, [entry(RUN, 10)], skin='bold')
            page = (Path(tmp) / 'experiments.html').read_text()
            self.assertIn('<style id="site-skin" data-skin="bold">.skin-bold-tokens{color:red}</style>', page)
            self.assertLess(page.index('id="site-look"'), page.index('id="site-skin"'))
        with tempfile.TemporaryDirectory() as tmp:  # default: catalogue_intro.SKIN, the editorial skin
            index.write_index(tmp, [entry(RUN, 10)])
            self.assertIn('data-skin="editorial"', (Path(tmp) / 'experiments.html').read_text())
            self.assertIn('data-skin="editorial"', (Path(tmp) / 'about.html').read_text())
        with tempfile.TemporaryDirectory() as tmp:  # an empty skin keeps the earlier look
            index.write_index(tmp, [entry(RUN, 10)], skin='')
            self.assertNotIn('site-skin', (Path(tmp) / 'experiments.html').read_text())

if __name__ == '__main__':
    unittest.main()
