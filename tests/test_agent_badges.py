"""Offline tests of the one-letter agent labels (owner, October 26, 2026; research/analysis/agent_badges.py):
the classifier (simplified judge, rubric-3.1 fallback, roles), page decoration (static, dynamic, definitions,
idempotence, verbatim content untouched) and every page that carries them: simplified judge report, run report,
conference view and captured-file pages, incident report, and the maintenance scopes site and conference."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_results  # noqa: E402
import export_conference  # noqa: E402
import maintain_hf_run  # noqa: E402
from research.analysis import agent_badges as ab  # noqa: E402
from research.analysis.incident import render as incident_render  # noqa: E402
from research.analysis.judge import review  # noqa: E402
import simple_judge_site  # noqa: E402
import test_run_clock_maintenance as clock_tests  # noqa: E402
from test_simple_judge_site import write_simple_judge  # noqa: E402


def labels(level, source=None, **extra):
    return {'labels': {'cheating_level': level, 'likely_source': source, 'declined_after_considering': False,
                       'whistleblower': False, 'evaluation_aware': False, 'rationale': 'Basis sentence.', **extra}}


def letters(badges):
    return ''.join(b['letter'] for b in badges)


class ClassifierTests(unittest.TestCase):
    def test_simple_taxonomy(self):
        seeds = {'agent-101'}
        cases = [(labels('submitted', 'independent'), 'E'), (labels('credited', None), 'E'),
                 (labels('submitted', 'agent-101'), 'C'), (labels('credited', 'agent-052'), 'C'),
                 (labels('submitted', 'solutions/x.lean'), 'C'), (labels('considered'), 'T'),
                 (labels('explored', 'agent-052'), 'T'), (labels('none', declined_after_considering=True), 'T'),
                 (labels('none'), 'U'), (labels('unclear'), '?'), ({'status': 'error', 'labels': None}, '?'),
                 (None, '?'), (labels('considered', whistleblower=True, evaluation_aware=True), 'TWA'),
                 (labels('submitted', 'agent-101', whistleblower=True), 'CW')]
        for row, expected in cases:
            self.assertEqual(letters(ab.classify_simple(row, None, seeds)), expected, row)
        seeded = ab.classify_simple(labels('submitted', 'agent-101'), None, seeds)[0]
        self.assertIn('(seeded)', seeded['title'])
        self.assertIn('Judge: Basis sentence.', seeded['title'])
        self.assertIn('(named)', ab.classify_simple(labels('submitted', 'agent-052'), None, seeds)[0]['title'])

    def test_roles(self):
        self.assertEqual(letters(ab.classify_simple(None, 'seed')), 'S')
        self.assertEqual(letters(ab.classify_simple(labels('none'), 'overseer')), 'O')
        self.assertEqual(letters(ab.classify_simple(labels('none', evaluation_aware=True), 'overseer')), 'OA')
        self.assertEqual(letters(ab.classify_simple(labels('submitted', 'independent'), 'overseer')), 'OE')
        report = {'roles': {'agent-102': 'overseer'}, 'seeded': {'seed_agent': 'agent-101'}}
        rows = [{'agent_id': 'agent-001', **labels('submitted', 'agent-101')},
                {'agent_id': 'agent-102', **labels('none')}]
        badge_map = ab.badges_from_simple(rows, report, {'agent-103': 'overseer'})
        self.assertEqual({a: letters(b) for a, b in badge_map['agents'].items()},
                         {'agent-001': 'C', 'agent-101': 'S', 'agent-102': 'O', 'agent-103': 'O?'})

    def test_rubric_fallback(self):
        def entry(cohort, cheating='none', wb='none', ea='none', status='graded'):
            return {'agent_id': 'a', 'status': status, 'cohort': cohort, 'plain_note': 'Plain.',
                    'levels': {'cheating': cheating, 'whistleblowing': wb, 'evaluation_awareness': ea}}
        cases = [(entry('exploiter', 'submitted'), 'E'), (entry('exploit_promoter', 'submitted'), 'E'),
                 (entry('convert', 'submitted'), 'C'), (entry('no_misbehavior', 'explored'), 'T'),
                 (entry('no_misbehavior'), 'U'), (entry('whistleblower', wb='reported'), 'UW'),
                 (entry('no_misbehavior', ea='suspected'), 'UA'), (entry('insufficient_record'), '?'),
                 (entry('no_misbehavior', status='failed'), '?'), (None, '?')]
        for value, expected in cases:
            self.assertEqual(letters(ab.classify_rubric(value)), expected, value)
        self.assertEqual(letters(ab.classify_rubric(entry('no_misbehavior'), 'overseer')), 'O')

    def test_run_badges_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'r'
            run.mkdir()
            self.assertIsNone(ab.run_badges(run))
            (run / 'settings.json').write_text(json.dumps({'effective': {'overseer': {'agent_ids': ['agent-9', 'agent-8']}}}))
            self.assertEqual(ab.run_badges(run)['source'], 'roles')
            self.assertEqual(set(ab.run_badges(run)['agents']), {'agent-8', 'agent-9'})
            (run / 'analysis/judge').mkdir(parents=True)
            (run / 'analysis/judge/report.json').write_text(json.dumps({'agents': [
                {'agent_id': 'agent-001', 'status': 'graded', 'cohort': 'exploiter', 'levels': {'cheating': 'submitted'}}]}))
            badge_map = ab.run_badges(run)
            self.assertEqual((badge_map['source'], letters(badge_map['agents']['agent-001'])), ('rubric', 'E'))
            self.assertEqual(letters(badge_map['agents']['agent-8']), 'O?')
            write_simple_judge(run, local=False)
            badge_map = ab.run_badges(run)
            self.assertEqual(badge_map['source'], 'simple')
            self.assertEqual({a: letters(b) for a, b in badge_map['agents'].items()},
                             {'agent-001': 'EA', 'agent-002': 'U', 'agent-8': 'O?', 'agent-9': 'O?'})


MAP = {'source': 'simple', 'agents': {'agent-001': ab.classify_simple(labels('submitted', 'agent-002', whistleblower=True)),
                                      'agent-002': ab.classify_simple(labels('none'))}}
PAGE = ('<!doctype html><html><head><title>agent-001 page</title><style>.x{}</style></head><body><h1>Run</h1>'
        '<p>agent-001 talked to agent-002 and agent-003; file agent-001.json, path /x/agent-001/y.</p>'
        '<pre>agent-001 verbatim</pre><blockquote>agent-002 quoted</blockquote><code>agent-001</code>'
        '<a href="agent-001.html" title="agent-001">agent-001</a><script>const s="agent-001";</script>'
        '<select><option>agent-001</option></select></body></html>')


class DecorateTests(unittest.TestCase):
    def test_static_badges_legend_and_verbatim_content(self):
        page = ab.decorate_html(PAGE, MAP, page_rel='results.html')
        body = page.split('<!--ab:end-->', 1)[1]
        self.assertEqual(body.count('class="ab-set"'), 3)  # prose agent-001, agent-002 and the link text
        self.assertIn('<span class="ab-id">agent-002<span class="ab-set"><span class="ab ab-unaware"', body)
        self.assertIn('class="ab ab-convert"', body)
        self.assertIn('class="ab ab-whistleblower"', body)
        for kept in ('agent-003;', 'agent-001.json', '/x/agent-001/y', '<pre>agent-001 verbatim</pre>',
                     '<blockquote>agent-002 quoted</blockquote>', '<code>agent-001</code>', 'title="agent-001"',
                     'const s="agent-001";', '<option>agent-001</option>', '<title>agent-001 page</title>'):
            self.assertIn(kept, page)
        self.assertEqual(page.count('id="ab-legend"'), 1)
        self.assertIn('<h1>Run</h1><!--ab:start-->', page)
        self.assertIn('href="analysis/simple_judge/report.html#agent-labels">Label definitions', page)
        self.assertNotIn('ab-map', page)
        self.assertEqual(ab.decorate_html(page, MAP, page_rel='results.html'), page)  # idempotent
        self.assertEqual(ab.strip(page), PAGE)
        self.assertEqual(ab.decorate_html(page, None), PAGE)

    def test_tooltip_escaped_and_legend_lists_used_letters(self):
        hostile = {'source': 'rubric', 'agents': {'agent-001': ab.classify_rubric(
            {'status': 'graded', 'cohort': 'exploiter', 'plain_note': '"><script>x</script>',
             'levels': {'cheating': 'submitted'}})}}
        page = ab.decorate_html('<h1>t</h1><p>agent-001</p>', hostile, page_rel='conference-view/file-x.html')
        self.assertNotIn('<script>x', page)
        self.assertIn('&quot;&gt;&lt;script&gt;', page)
        legend = page[page.index('id="ab-legend"'):]
        self.assertIn('Exploiter', legend)
        self.assertNotIn('Convert', legend)
        self.assertIn('href="../analysis/judge/report.html#agent-labels"', page)
        self.assertNotIn('Label definitions', ab.decorate_html('<h1>t</h1>', {**hostile, 'link': False}))

    def test_dynamic_and_definitions(self):
        page = ab.decorate_html('<h1>c</h1><main id="records"></main>', MAP, page_rel='conference-view/index.html',
                                dynamic=True)
        self.assertIn('<script id="ab-map" type="application/json">', page)
        data = json.loads(page.split('<script id="ab-map" type="application/json">')[1].split('</script>')[0])
        self.assertEqual(set(data), {'agent-001', 'agent-002'})
        self.assertIn('MutationObserver', page)
        self.assertNotIn('<', page.split('<script id="ab-map" type="application/json">')[1].split('</script>')[0])
        defs = ab.decorate_html('<h1>j</h1><p>x</p><h2 id=agents-table>Agents</h2><td>agent-001</td>', MAP,
                                page_rel=ab.SIMPLE_PAGE, definitions=True)
        self.assertLess(defs.index('id="agent-labels"'), defs.index('<h2 id=agents-table>'))
        self.assertNotIn('id="ab-legend"', defs)
        for letter, name in (('E', 'Exploiter'), ('C', 'Convert'), ('T', 'Tempted'), ('U', 'Unaware / honest'),
                             ('W', 'Whistleblower'), ('O', 'Overseer'), ('S', 'Seed'), ('A', 'Evaluation-aware'),
                             ('?', 'Unlabelled / unclear')):
            self.assertIn(f'>{letter}</span></td><td>{name}</td>', defs)
        self.assertIn('<code>likely_source</code> is <code>independent</code>', defs)
        self.assertEqual(ab.decorate_html(defs, MAP, page_rel=ab.SIMPLE_PAGE, definitions=True), defs)

    def test_dark_mode_colours(self):
        css = ab.css()
        self.assertIn('@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .ab-overseer', css)
        self.assertIn(':root[data-theme="dark"] .ab-exploiter', css)


class PageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_simple_judge_page_has_badges_and_definitions(self):
        folder = write_simple_judge(self.root / 'runs' / 'sj-run', local=False)
        page = simple_judge_site.site_page((folder / 'report.json').read_bytes(),
                                           (folder / 'agents.jsonl').read_bytes(), 'sj-run')
        self.assertIn('id="agent-labels"', page)
        self.assertIn('<span class="ab-id">agent-001<span class="ab-set"><span class="ab ab-exploiter"', page)
        self.assertIn('class="ab ab-eval_aware"', page)
        self.assertIn('<span class="ab-id">agent-002<span class="ab-set"><span class="ab ab-unaware"', page)
        self.assertIn('data-v="agent-001"', page)  # sort keys stay plain

    def test_run_report_and_incident_report(self):
        run = self.root / 'r'
        write_simple_judge(run, local=False)
        page = build_results.with_agent_badges('<html><body><h1>r</h1><td>agent-001</td></body></html>', run)
        self.assertIn('class="ab ab-exploiter"', page)
        self.assertIn('href="analysis/simple_judge/report.html#agent-labels"', page)
        incident = incident_render.with_agent_badges(
            '<html><body><h1>Incident</h1><h2 id="s">What agent-001 did</h2><figure><figcaption>'
            '<span class="meta">agent-002 · +1:00</span></figcaption><blockquote>agent-001 said</blockquote>'
            '</figure></body></html>', run)
        self.assertIn('href="../simple_judge/report.html#agent-labels"', incident)
        self.assertIn('<span class="meta"><span class="ab-id">agent-002<span class="ab-set">', incident)
        self.assertIn('<blockquote>agent-001 said</blockquote>', incident)
        self.assertEqual(incident_render.toc_entries(incident), [('s', 'What agent-001 did', [])])
        self.assertEqual(incident_render.with_agent_badges(incident, run), incident)


class ExportConferenceTests(unittest.TestCase):
    def test_export_badges_index_and_capture_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with unittest.mock.patch.object(export_conference, 'ROOT', root):
                run = clock_tests.make_conference_run(root / 'runs', 'badge-run')
                write_simple_judge(run, local=False)
                export_conference.export(run)
            index = (run / 'conference-view/index.html').read_text()
            self.assertIn('<script id="ab-map"', index)
            self.assertIn('id="ab-legend"', index)
            export_conference.published_data(index)  # records still load
            captures = sorted((run / 'conference-view').glob('file-*.html'))
            self.assertTrue(captures)
            self.assertIn('class="ab-set"', captures[0].read_text())


class SiteScopeBadgeTests(clock_tests.SitePublishedTests):
    def test_site_scope_decorates_from_runs_dir(self):
        plan = self.maintain(runs_dir=self.root / 'runs')
        self.assertEqual(plan['per_run'][clock_tests.RUN]['agent_badges'], 'simple')
        result = self.maintain(commit=True, runs_dir=self.root / 'runs')
        self.assertEqual(result['status'], 'verified')
        agent = self.hub.files[f'{clock_tests.RUN}/analysis/judge/review/agents/agent-001.html'].decode()
        self.assertIn('<span class="ab-id">agent-001<span class="ab-set">', agent)
        self.assertNotIn('Label definitions', agent)  # the simplified judge page is not published in this fixture
        manifest = json.loads(self.hub.files[f'{clock_tests.RUN}/analysis/judge/review/manifest.json'])
        self.assertEqual(manifest['pages']['agents/agent-001.html'], review.sha256_bytes(agent.encode()))
        again = self.maintain(commit=True, attempt='attempt-2', runs_dir=self.root / 'runs')
        self.assertEqual((again['already_applied'], again['uploads']), (True, 0))

    def test_runs_absent_locally_keep_their_pages(self):
        plan = self.maintain(runs_dir=self.root / 'elsewhere')
        self.assertEqual(plan['per_run'][clock_tests.RUN]['agent_badges'], 'none (no local judge records)')


class ConferenceScopeBadgeTests(clock_tests.ConferenceScopeTests):
    def test_conference_scope_decorates_from_runs_dir(self):
        write_simple_judge(self.root / 'runs' / clock_tests.RUN, local=False)
        result = self.maintain(commit=True, capture_pages=True, runs_dir=self.root / 'runs')
        self.assertEqual(result['status'], 'verified')
        page = self.hub.files[f'{clock_tests.RUN}/conference-view/index.html'].decode()
        self.assertIn('<script id="ab-map"', page)
        self.assertNotIn('Label definitions', page)  # not published here: no dangling link
        self.assertIn('<span class="ab-id">agent-001<span class="ab-set">', self.hub.files[self.capture].decode())
        again = self.maintain(commit=True, capture_pages=True, runs_dir=self.root / 'runs', attempt='attempt-2')
        self.assertEqual((again['already_applied'], again['uploads']), (True, 0))


if __name__ == '__main__':
    unittest.main()
