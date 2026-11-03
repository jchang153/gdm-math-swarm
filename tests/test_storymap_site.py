"""Story maps on the public site (owner, November 2, 2026): scripts/storymap_site.py, the prepare_hf_library.py
allowlist and publish_cloudflare.py storymap. Offline; no network or deploy."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import prepare_hf_library  # noqa: E402
import publish_cloudflare as cf  # noqa: E402
import storymap_site as sm  # noqa: E402

RUN = 'r1-20261027T000000Z'
OTHER = 'r0-20261020T000000Z'
RESULTS = ('<html><nav class="tabs" aria-label="Run records"><a href="#report">Report</a>'
           '<a href="inspect-view/index.html">Inspect</a></nav><ul class="tabhelp"><li><strong>Report</strong>: x</li>'
           '<li><strong>Inspect</strong>: y</li></ul></html>')
INDEX = ('<nav class="top"><a href="index.html">All agents</a><a href="events.html">Events</a>'
         '<a href="validation.html">Validation</a></nav><a href="agents/agent-001.html">agent-001</a>'
         '<span>thought A: codes</span>')
AGENT = '<a href="../index.html">All</a><a href="../events.html">Events</a><p>cd /home/researcher/work</p>'
VALIDATION_MD = ('# Story map validation\n\nMap: `runs/x/analysis/storymap`  \n'
                 'Reference: `private/research/analysis/storymap/reference/x.jsonl`  \n')
VALIDATION_HTML = ('<a href="index.html">All</a><p>Reference: <code>private/research/analysis/storymap/reference/'
                   'x.jsonl</code><br>Time</p>')


def storymap_folder(root, **extra):
    files = {'index.html': INDEX, 'events.html': '<a href="index.html">i</a>', 'validation.md': VALIDATION_MD,
             'validation.html': VALIDATION_HTML, 'agents/agent-001.html': AGENT, 'map.jsonl': '{}',
             'pipeline.json': '{}', 'validation.json': '{}', 'run-budgets.json': '{}',
             'timeline/agent-001.jsonl': '{}', 'stage1/a.json': '{}', 'units/u.json': '{}', **extra}
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


class AllowlistTests(unittest.TestCase):
    def test_only_pages_and_validation_are_published(self):
        keep = ['analysis/storymap/index.html', 'analysis/storymap/events.html', 'analysis/storymap/validation.md',
                'analysis/storymap/validation.html', 'analysis/storymap/taxonomy.html',
                'analysis/storymap/agents/agent-001.html']
        drop = ['analysis/storymap/map.jsonl', 'analysis/storymap/pipeline.json', 'analysis/storymap/validation.json',
                'analysis/storymap/run-budgets.json', 'analysis/storymap/timeline/agent-001.jsonl',
                'analysis/storymap/stage2/agent-001.json', 'analysis/storymap/raw/x.json',
                'analysis/storymap/units/u.json', 'analysis/storymap/crosscheck/index.html',
                'analysis/storymap/bakeoff/index.html', 'analysis/storymap/agents/sub/x.html']
        for path in keep:
            self.assertFalse(prepare_hf_library.run_excluded(path), path)
        for path in drop:
            self.assertTrue(prepare_hf_library.run_excluded(path), path)
        self.assertFalse(prepare_hf_library.run_excluded('analysis/summary.json'))

    def test_public_files_screens_and_strips_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = sm.public_files(storymap_folder(Path(tmp)))
            self.assertEqual(sorted(files), ['agents/agent-001.html', 'events.html', 'index.html',
                                             'validation.html', 'validation.md'])
            for name in ('validation.md', 'validation.html'):
                self.assertNotIn(b'private/', files[name])
                self.assertNotIn(b'Reference', files[name])
            self.assertIn(b'Map: `runs/x/analysis/storymap`', files['validation.md'])
            self.assertIn(b'/home/researcher', files['agents/agent-001.html'])  # the agents' container home

    def test_public_files_refuses_problems(self):
        cases = {'agents/agent-001.html': AGENT + '<p>/Users/someone/x</p>',
                 'events.html': '<p>see private/research/analysis/storymap/prompts.md</p>',
                 'index.html': INDEX + '<a href="timeline/agent-001.jsonl">t</a>',
                 'taxonomy.html': '<p>key sk-ant-' + 'a' * 30 + '</p>'}
        for name, text in cases.items():
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError, msg=name):
                sm.public_files(storymap_folder(Path(tmp), **{name: text}))
        # The taxonomy is public (owner, November 2, 2026): names next to their codes and taxonomy.html are allowed.
        with tempfile.TemporaryDirectory() as tmp:
            files = sm.public_files(storymap_folder(Path(tmp), **{
                'events.html': '<span>thought C1 &middot; Calls it cheating</span><a href="taxonomy.html#code-C1">c</a>',
                'taxonomy.html': '<h3 id="code-C1">C1 - Calls it cheating</h3><a href="index.html">i</a>'}),
                taxonomy={'C1': 'Calls it cheating'})
            self.assertIn('taxonomy.html', files)

    def test_prompt_text_is_refused(self):
        line = 'Return one JSON object with exactly these keys and nothing else around it.'
        self.assertEqual(sm.page_problems('index.html', f'<p>{line}</p>'.encode(), {'index.html'}, prompts={line}),
                         ['text of the private stage prompts'])
        escaped = 'Quote the agent\'s words "verbatim" and never paraphrase anything at all here.'
        import html as _html
        page = f'<p>{_html.escape(escaped)}</p>'.encode()
        self.assertTrue(sm.page_problems('index.html', page, {'index.html'}, prompts={escaped}))
        self.assertEqual(sm.page_problems('index.html', b'<p>ok</p>', {'index.html'}, prompts={line}), [])
        with tempfile.TemporaryDirectory() as tmp:  # taxonomy text that the prompts also carry is not prompt text
            tmp = Path(tmp)
            (tmp / 'p.md').write_text('# Prompt\n' + line + '\nThe agent cites others\' use or acceptance as a reason.\n')
            (tmp / 't.md').write_text('The agent cites others\' use or acceptance as a reason.\n')
            self.assertEqual(sm.prompt_lines(tmp / 'p.md', tmp / 't.md'), frozenset({line}))
            self.assertEqual(sm.prompt_lines(tmp / 'missing.md', tmp / 't.md'), frozenset())

    def test_tab_before_inspect_and_idempotent(self):
        page = sm.add_storymap_tab(RESULTS)
        self.assertLess(page.index(sm.TAB), page.index('href="inspect-view/'))
        self.assertLess(page.index(sm.HELP), page.index('<li><strong>Inspect</strong>'))
        self.assertEqual(sm.add_storymap_tab(page), page)
        with self.assertRaises(ValueError):
            sm.add_storymap_tab('<html></html>')


class PublishTests(unittest.TestCase):
    def site(self):
        files = {'index.html': b'<html>catalogue</html>', 'experiments.json': b'{"experiments": [], "warnings": []}'}
        for run in (OTHER, RUN):
            files[f'{run}/results.html'] = RESULTS.encode()
            files[f'{run}/results.json'] = b'{}'
        return files

    def test_adds_only_storymap_pages_and_tab(self):
        base = self.site()
        with tempfile.TemporaryDirectory() as tmp:
            files, plan = cf.plan_storymap(base, (), {RUN: storymap_folder(Path(tmp))})
        changed = sorted(n for n in files if base.get(n) != files[n])
        self.assertEqual(changed, sorted([f'{RUN}/results.html'] + [
            f'{RUN}/analysis/storymap/{n}' for n in ('index.html', 'events.html', 'validation.md',
                                                      'validation.html', 'agents/agent-001.html')]))
        self.assertEqual(plan['added_files'], 5)
        self.assertEqual([x['path'] for x in plan['replaced_pages']], [f'{RUN}/results.html'])
        self.assertIn(sm.TAB.encode(), files[f'{RUN}/results.html'])
        self.assertEqual(files[f'{OTHER}/results.html'], base[f'{OTHER}/results.html'])
        files, manifest = cf.finish(files, {}, plan)  # the site gate accepts every file
        self.assertIn(f'{RUN}/analysis/storymap/index.html', manifest['files'])
        json.dumps(plan)

    def test_refresh_replaces_and_removes_stale_pages(self):
        base = self.site()
        base[f'{RUN}/analysis/storymap/agents/agent-999.html'] = b'old'
        base[f'{RUN}/analysis/storymap/index.html'] = b'old'
        with tempfile.TemporaryDirectory() as tmp:
            files, plan = cf.plan_storymap(base, (), {RUN: storymap_folder(Path(tmp))})
        self.assertNotIn(f'{RUN}/analysis/storymap/agents/agent-999.html', files)
        self.assertEqual(plan['removed_files'], [f'{RUN}/analysis/storymap/agents/agent-999.html'])
        self.assertIn(f'{RUN}/analysis/storymap/index.html', [x['path'] for x in plan['replaced_pages']])

    def test_refuses_unknown_or_unlisted_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = storymap_folder(Path(tmp))
            with self.assertRaises(ValueError):
                cf.plan_storymap(self.site(), (), {'absent-run': folder})
            with self.assertRaises(ValueError):
                cf.plan_storymap(self.site(), (RUN,), {RUN: folder})
            with self.assertRaises(ValueError):
                cf.plan_storymap(self.site(), (), {})


if __name__ == '__main__':
    unittest.main()
