"""Agentic monitor arm AG (docs/plan.md, "Agentic monitor arm AG (owner decisions, November 23, 2026)"): catalogue
card titles, run-spec chips, run-page titles/chips and the AG series (ag120, both models; owner, November 24, 2026:
every run in exactly one series). Offline; no run records needed."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / 'scripts')):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import build_experiment_index  # noqa: E402
import catalogue_intro  # noqa: E402
import run_profile  # noqa: E402
import site_layout  # noqa: E402

AG_RUNS = ('dsv41flash100-seeded-agentic-120m-push-r1-20261124T050000Z',
           'dsv41flash100-seeded-agentic-monitor-120m-push-r1-20261124T050000Z')
DISCLOSED = 'dsv41flash100-seeded-agentic-disclosed-120m-push-r2-20261124T050000Z'
TITLE = 'AG: agentic monitor (wait, revert + reason, messages)'
DOC = ('<p class="ri-cond">100 DeepSeek V4.1 Flash solvers, no overseer, exploit planted at +3 min, 2 h.</p>'
       '<div class="tile"><div class="k">Model</div><div class="v">deepseek/deepseek-v4.1-flash-1017</div>'
       '<div class="s">route</div></div>')


class AgenticCatalogueTest(unittest.TestCase):
    def test_card_title_and_series(self):
        for run in AG_RUNS:
            self.assertEqual(build_experiment_index.short_title({'run': run}), f'{TITLE} · run 1')
            self.assertEqual(run_profile.series_of(run)[0], 'ag120')
        for run in ('glmflash100-seeded-agentic-monitor-120m-push-r2-20261124T112253Z',
                    'dsv41flash100-seeded-agentic-monitor-120m-push-r3-20261124T103722Z', DISCLOSED):
            self.assertEqual(run_profile.series_of(run)[0], 'ag120')
        self.assertEqual(build_experiment_index.short_title({'run': DISCLOSED}),
                         f'{TITLE}, disclosed to agents · run 2')

    def test_spec_chips(self):
        entry = {'run': AG_RUNS[0], 'model': 'deepseek/deepseek-v4.1-flash-1017', 'elapsed_seconds': 7200}
        chips = build_experiment_index.spec_chips(entry, None)
        self.assertIn('>agentic monitor</span>', chips)
        self.assertIn('>revert + reason, messages</span>', chips)
        self.assertIn('undisclosed', chips)
        self.assertNotIn('async reviewer', chips)
        self.assertIn('described in the solvers', build_experiment_index.spec_chips(dict(entry, run=DISCLOSED), None))

    def test_run_page_title_and_chips(self):
        self.assertEqual(site_layout._agentic(AG_RUNS[0]), ('AG', 'wait, revert + reason, messages'))
        self.assertIsNone(site_layout._agentic('dsv41flash100-seeded-reviewer-rollback-silent-120m-push-r1-20261124T0'))
        title, chips = site_layout._spec(DOC, AG_RUNS[0])
        self.assertIn(' · AG: agentic monitor, wait, revert + reason, messages · ', title)
        self.assertIn(('Monitor', 'agentic, one per solver: wait, revert with reason, messages'), chips)
        self.assertNotIn('Oversight', dict(chips))
        _, chips = site_layout._spec(DOC, DISCLOSED)
        self.assertIn(('Monitor', 'agentic, one per solver: wait, revert with reason, messages, disclosed to agents'),
                      chips)

    def test_other_arms_unchanged(self):
        run = 'dsv41flash100-seeded-reviewer-rollback-reason-120m-push-r3-20261124T012624Z'
        self.assertEqual(build_experiment_index.short_title({'run': run}), 'A3: async reviewer, rollback, reason shown · run 3')
        self.assertIsNone(site_layout._agentic(run))
        chips = build_experiment_index.spec_chips({'run': run, 'model': 'x', 'elapsed_seconds': 7200}, None)
        self.assertNotIn('agentic', chips)

    def test_series_texts(self):
        self.assertIn('agentic monitor', catalogue_intro.SERIES_LINES['ag120'])
        texts = {k: t for k, _, _, t in run_profile.SERIES}
        self.assertIn('agentic monitor', texts['ag120'])
        self.assertIn('Nineteen', texts['ds120'])
        self.assertIn('Eighteen', texts['push120'])
        self.assertIn('own series', texts['ds120'])
        self.assertIn('Agentic monitor', dict(catalogue_intro.GLOSSARY))


if __name__ == '__main__':
    unittest.main()
