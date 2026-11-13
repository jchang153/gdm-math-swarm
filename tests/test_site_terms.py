"""Plain words on the public site (owner, November 13, 2026: "things like 'rubric 3.1' or 'simple behavior judge' are
confusing terms ... simplify so these backend side things aren't exposed"; scripts/site_terms.py). Offline."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import site_terms  # noqa: E402


class PlainWordsTest(unittest.TestCase):
    def check(self, old, new):
        self.assertEqual(site_terms.apply(old), new)
        self.assertEqual(site_terms.apply(new), new)  # idempotent

    def test_judge_names(self):
        self.check('<a data-tab="judge-rubric-3-1" href="analysis/judge/report.html">Behavior judge (rubric 3.1)</a>',
                   '<a data-tab="judge-rubric-3-1" href="analysis/judge/report.html">Detailed behavior judge</a>')
        self.check('<title>Simple behavior judge</title><h1>Simple behavior judge</h1>',
                   '<title>Behavior judge</title><h1>Behavior judge</h1>')
        self.check('<p>The simplified behavior judge (default since October 24, 2026): one model call per agent. Its '
                   'counts are not comparable with the rubric-3.1 judge&#x27;s.</p>',
                   '<p>The behavior judge: one model call per agent. Its counts are not comparable with the detailed '
                   'behavior judge&#x27;s.</p>')
        self.check('<p class=muted>run-20261022T050055Z &middot; gdm-simple-judge/1 &middot; model m</p>',
                   '<p class=muted>run-20261022T050055Z &middot; model m</p>')
        self.check('<title>Behavior judge glm100-baseline-30m-20261022T050055Z</title>',
                   '<title>Detailed behavior judge glm100-baseline-30m-20261022T050055Z</title>')

    def test_tooltips_and_attributes(self):
        old = ('<span class="ab ab-unlabelled" data-k="simplified judge" title="Unlabelled / unclear: the simplified '
               'judge gave no label (failed: max_tokens)" aria-label="Unlabelled / unclear: the simplified judge gave '
               'no label (no judge row)">?</span>')
        new = ('<span class="ab ab-unlabelled" data-k="simplified judge" title="Unlabelled / unclear: the behavior '
               'judge gave no label (its reply was cut off)" aria-label="Unlabelled / unclear: the behavior judge gave '
               'no label (not judged)">?</span>')
        self.check(old, new)  # data-* attributes, classes and hrefs never change

    def test_story_map_readings(self):
        self.check('<p>Readers: Claude Haiku 5.5 (stage 1); GPT-6.1 Terra (stage 2); GPT-6.1 Sol (stage 3).</p>',
                   '<p>Readers: Claude Haiku 5.5 (thoughts); GPT-6.1 Terra (reactions); GPT-6.1 Sol (final reading).</p>')
        self.check('<div class="lab">Final reading (stage 3)</div><option value="no stage 3">No final reading</option>',
                   '<div class="lab">Final reading</div><option value="no stage 3">No final reading</option>')
        self.check('<td class="muted">no stage-2 reaction</td>', '<td class="muted">no reaction reading</td>')

    def test_agent_text_and_code_untouched(self):
        for page in ('<blockquote>Now fix stage 2 with det = -4</blockquote>',
                     '<code>simplified judge</code><script>var t="rubric 3.1";</script>',
                     '<p>Stage-1 B1 at 0.0 min is a placeholder</p>'):
            self.assertEqual(site_terms.apply(page), page)

    def test_backend_names(self):
        self.check('<div class="s">as recorded in settings.json</div><div class="s">stop: max_run_seconds</div>',
                   '<div class="s">as recorded in the run settings</div><div class="s">stop: time limit</div>')
        self.check('<p>strict fake rule, answer-echo-v2)) and the weak operational judge</p>',
                   '<p>strict fake rule)) and the weak judge</p>')
        self.check('<td>Role from the run settings (research/analysis/roles.py): an overseer agent.</td>',
                   '<td>Role from the run settings: an overseer agent.</td>')

    def test_covers(self):
        for name in ('RUN/results.html', 'RUN/analysis/judge/report.html', 'RUN/analysis/judge/review/agents/a.html',
                     'RUN/analysis/storymap/agents/agent-001.html', 'RUN/conference-view/index.html'):
            self.assertTrue(site_terms.covers(name), name)
        for name in ('RUN/conference-view/file-0a.html', 'RUN/analysis/storymap/graph.html', 'about.html',
                     'RUN/inspect-view/index.html'):
            self.assertFalse(site_terms.covers(name), name)


if __name__ == '__main__':
    unittest.main()
