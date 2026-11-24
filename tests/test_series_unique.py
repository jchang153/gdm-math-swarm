"""Every run is in exactly one catalogue series (owner, November 24, 2026: "runs shouldn't be in repeat
categories"): series keys are unique, every series has a one-line summary, and the catalogue's series sections
(build_experiment_index.series_groups / series_sections) hold each run's card exactly once. Runs: one ID per known
arm, plus every run folder of the local runs/ index when present. Offline."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / 'scripts')):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import build_experiment_index  # noqa: E402
import catalogue_intro  # noqa: E402
import run_profile  # noqa: E402

KNOWN = {
    'glmflash100-seeded-agentic-monitor-120m-push-r1-20261124T103755Z': 'ag120',
    'dsv41flash100-seeded-agentic-monitor-120m-push-r1-20261124T103727Z': 'ag120',
    'dsv41flash100-seeded-agentic-disclosed-120m-push-r2-20261124T050000Z': 'ag120',
    'dsv41flash100-seeded-reviewer-rollback-reason-120m-push-r1-20261124T012556Z': 'ds120',
    'dsv41flash100-control-120m-push-r1-20261124T012519Z': 'ds120',
    'glmflash100-seeded-10ov-glm-120m-push-r4-20261116T213543Z': 'push120',
    'glmflash100-seeded-control-120m-push-r1-20261104T042405Z': 'push120',
    'glmflash100-seeded-reviewer-revert-reason-120m-push-r1-20261119T002605Z': 'reviewer120',
    'glmflash100-seeded-monitor-full-silent-120m-push-r1-20261110T200000Z': 'monitor120',
    'glmflash100-seeded-1ov-glm-120m-push-commsonly-r1-20261104T220000Z': 'push120comms',
    'dsv41flash-25-seeded-control-60m-push-20261105T201815Z': 'sweep25',
}


def runs():
    found = dict(KNOWN)
    base = ROOT / 'runs'
    if base.is_dir():
        for path in base.iterdir():
            if path.is_dir() and re.search(r'-\d{8}T\d{6}Z$', path.name):
                found.setdefault(path.name, None)
    return found


def stamp(run):
    m = re.search(r'(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$', run)
    return '{}-{}-{}T{}:{}:{}Z'.format(*m.groups())


class SeriesUniqueTest(unittest.TestCase):
    def test_keys_unique_and_summarised(self):
        keys = [k for k, *_ in run_profile.SERIES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), set(catalogue_intro.SERIES_LINES))

    def test_known_runs(self):
        for run, key in KNOWN.items():
            self.assertEqual(run_profile.series_of(run)[0], key, run)

    def test_each_run_in_exactly_one_section(self):
        entries = [{'run': run, 'start_utc': stamp(run), 'model': 'x', 'agents': 100, 'elapsed_seconds': 7200}
                   for run in runs()]
        groups = build_experiment_index.series_groups(entries)
        placed = [e['run'] for _, _, _, members in groups for e in members]
        self.assertEqual(sorted(placed), sorted(e['run'] for e in entries))
        self.assertEqual(len(placed), len(set(placed)))
        self.assertEqual(len(groups), len({k for k, *_ in groups}))
