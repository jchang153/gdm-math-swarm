"""Story map links on the catalogue (owner, November 2, 2026: "the story maps should also be accessible from the
main page"): build_experiment_index.with_storymap_link, the note above the run list, and
publish_cloudflare.py catalogue (only index.html and experiments.json change). Offline; no network or deploy."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_experiment_index as index  # noqa: E402
import publish_cloudflare as cf  # noqa: E402

MAPPED = 'mapped-run-20261028T194542Z'
PLAIN = 'plain-run-20261027T000000Z'


def entry(run, hour):
    return {'run': run, 'date': '2026-10-28 10:00 PDT', 'start_utc': f'2026-10-28T{hour:02d}:00:00+00:00',
            'track': 'spontaneous', 'agents': 10, 'model': 'vendor/model', 'provider_route': 'route/us',
            'elapsed_seconds': 3600, 'pilot_spent_usd': '1.00', 'distinct_verified_tasks': 1, 'task_count': 14,
            'agent_exits': {'harness_completed': 10},
            'links': {'Report': run + '/results.html', 'Inspect log 1': run + '/inspect-view/index.html#x'}}


def site():
    with tempfile.TemporaryDirectory() as tmp:
        index.write_index(tmp, [entry(MAPPED, 10), entry(PLAIN, 9)], hosted=True)
        files = {n: (Path(tmp) / n).read_bytes() for n in ('index.html', 'experiments.json')}
    for run in (MAPPED, PLAIN):
        files[f'{run}/results.html'] = b'<html></html>'
    files[f'{MAPPED}/{index.STORYMAP_TARGET}'] = b'<html>map</html>'
    return files


class StorymapLinkTests(unittest.TestCase):
    def test_link_placed_before_inspect_logs_and_removable(self):
        links = index.with_storymap_link(entry(MAPPED, 1)['links'], MAPPED)
        self.assertEqual(list(links), ['Report', 'Story map', 'Inspect log 1'])
        self.assertEqual(links['Story map'], f'{MAPPED}/analysis/storymap/index.html')
        self.assertEqual(index.with_storymap_link(links, MAPPED), links)
        self.assertNotIn('Story map', index.with_storymap_link(links, MAPPED, present=False))

    def test_catalogue_subcommand_changes_only_the_catalogue(self):
        base = site()
        files, plan = cf.plan_catalogue(base, unlisted=())
        self.assertEqual(sorted(n for n in files if files[n] != base.get(n)), ['experiments.json', 'index.html'])
        self.assertEqual(plan['storymap_links'], [MAPPED])
        entries = {e['run']: e for e in json.loads(files['experiments.json'])['experiments']}
        self.assertIn('Story map', entries[MAPPED]['links'])
        self.assertNotIn('Story map', entries[PLAIN]['links'])
        page = files['index.html'].decode()
        self.assertIn(f'href="{MAPPED}/analysis/storymap/index.html"', page)
        self.assertIn('1 run has an agent story map', page)
        again, plan = cf.plan_catalogue(files, unlisted=())  # idempotent
        self.assertEqual(plan['changed_files'], [])
        cf.finish(files, {}, plan)  # the site gate accepts the result

    def test_link_dropped_when_story_map_leaves_the_site(self):
        files, _ = cf.plan_catalogue(site(), unlisted=())
        del files[f'{MAPPED}/{index.STORYMAP_TARGET}']
        files, _ = cf.plan_catalogue(files, unlisted=())
        self.assertNotIn(b'Story map', files['experiments.json'])

    def test_local_build_links_runs_with_a_story_map(self):
        page = index.write_index  # the note only appears when some entry links a story map
        with tempfile.TemporaryDirectory() as tmp:
            page(tmp, [entry(PLAIN, 9)], hosted=True)
            self.assertNotIn('agent story map', (Path(tmp) / 'index.html').read_text())


if __name__ == '__main__':
    unittest.main()
