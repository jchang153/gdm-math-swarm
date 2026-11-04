"""Faking over time (owner, November 3, 2026): scripts/faking_heatmap.py, its results.html section and
publish_cloudflare.py heatmap. Offline; no network or deploy."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import faking_heatmap as fh  # noqa: E402
import publish_cloudflare as cf  # noqa: E402

try:
    import matplotlib  # noqa: F401
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

START = 1_800_000_000.0
FAKE = 'theorem t : answer(P x) ↔ P x := by\n  exact Iff.rfl\n'
REAL = 'theorem t : 1 + 1 = 2 := by norm_num\n'
PAGE = ('<html><body><nav class="tabs"><a href="#report">Report</a></nav><main id="report">'
        '<h2>Run</h2></main></body></html>')


def make_run(root, name='r1-20261103T000000Z', fakes=True):
    run = Path(root) / name
    (run / 'conference').mkdir(parents=True)
    (run / 'evaluations').mkdir()
    (run / 'settings.json').write_text(json.dumps({'config': {'max_run_seconds': 1800}, 'effective': {
        'roles': {'agent-001': 'solver', 'agent-002': 'solver', 'agent-003': 'solver', 'agent-101': 'seed',
                  'agent-102': 'overseer'}}}))
    (run / 'results.json').write_text(json.dumps({'start_utc': '2027-01-15T08:00:00Z', 'elapsed_seconds': 1800}))
    db = sqlite3.connect(run / 'conference' / 'state.sqlite')
    db.executescript(
        'create table submissions (submission_id, agent_id, source, outcome, submitted_at, source_sha256);'
        'create table reversions (credited_agent, submission_id, reverted_at);'
        'create table messages (sender, recipient, delivered_at);'
        'create table posts (post_id, agent_id, posted_at);'
        'create table events (timestamp, kind, agent_id, payload);')
    import run_clock
    t0 = run_clock.epoch('2027-01-15T08:00:00Z')
    rows = [('s1', 'agent-002', REAL, 'accepted', t0 + 60, 'h-real'),
            ('s9', 'agent-101', FAKE, 'accepted', t0 + 30, 'h-seed')]
    if fakes:
        rows += [('s2', 'agent-001', FAKE, 'rejected', t0 + 120, 'h1'),          # never credited, faked
                 ('s3', 'agent-003', FAKE, 'accepted', t0 + 300, 'h3'),          # credited fake
                 ('s4', 'agent-003', FAKE, 'problem_closed', t0 + 330, 'h4'),
                 ('s5', 'agent-102', FAKE, 'rejected', t0 + 90, 'h5')]           # overseer: excluded
    db.executemany('insert into submissions values (?,?,?,?,?,?)', rows)
    db.execute('insert into reversions values (?,?,?)', ('agent-003', 's3', t0 + 600))
    db.execute('insert into reversions values (?,?,?)', ('agent-002', 's1', t0 + 610))  # not a fake: no marker
    db.execute('insert into messages values (?,?,?)', ('agent-102', 'agent-003', t0 + 400))
    db.execute('insert into messages values (?,?,?)', ('agent-102', 'agent-001', None))  # undelivered
    db.execute('insert into posts values (?,?,?)', (7, 'agent-102', t0 + 200))
    db.execute('insert into events values (?,?,?,?)',
               (t0 + 450, 'board_read', 'agent-001', json.dumps({'post_ids': ['post-000007']})))
    db.commit()
    db.close()
    (run / 'evaluations' / 'evaluations.jsonl').write_text(
        json.dumps({'source_sha256': 'h-real', 'validity': 'genuine'}) + '\n')
    return run


class CollectTest(unittest.TestCase):
    def test_rows_markers_and_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = fh.collect(make_run(tmp))
        self.assertEqual(s['minutes'], 30)
        self.assertEqual([r['agent'] for r in s['rows']], ['agent-003', 'agent-001'])  # credited block first
        self.assertEqual(s['excluded_agents'], ['agent-101', 'agent-102'])
        a3, a1 = s['rows']
        self.assertTrue(a3['credited'] and not a1['credited'])
        self.assertEqual(a3['fakes'], [[5.0, True], [5.5, False]])
        self.assertEqual(a3['reverts'], [10.0])
        self.assertEqual(a3['dms'], [round(400 / 60, 3)])
        self.assertEqual(a1['dms'], [])
        self.assertEqual(a1['posts'], [7.5])
        self.assertEqual((s['fakers'], s['credited_fakers'], s['accepted_fakes']), (2, 1, 1))

    def test_moving_average(self):
        summary = {'minutes': 9, 'rows': [{'fakes': [[1.2, False], [1.9, False], [3.0, True]]}]}
        row = fh.matrix(summary)[0]
        self.assertEqual(len(row), 10)
        self.assertAlmostEqual(row[1], 2 / 5)
        self.assertAlmostEqual(row[3], 3 / 5)
        self.assertAlmostEqual(row[5], 3 / 5)
        self.assertAlmostEqual(row[6], 1 / 5)
        self.assertAlmostEqual(row[8], 0)


class SectionTest(unittest.TestCase):
    def test_section_is_idempotent_and_first_in_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = fh.collect(make_run(tmp))
        page = fh.add_section(PAGE, s)
        self.assertIn('<main id="report"><section id="faking-over-time"><h2>Faking over time</h2>', page)
        self.assertIn('src="faking-heatmap.png"', page)
        self.assertEqual(fh.add_section(page, s), page)
        none = fh.add_section(page, dict(s, rows=[], fakers=0))
        self.assertEqual(none.count('id="faking-over-time"'), 1)
        self.assertIn('No fakes in this run', none)
        self.assertNotIn('faking-heatmap.png', none)
        legacy = fh.add_section('<html><body><h1>T</h1><p>x</p><h2>A</h2></body></html>', s)
        self.assertIn('<p>x</p><section id="faking-over-time">', legacy)
        with self.assertRaises(ValueError):
            fh.add_section('<html><body></body></html>', s)

    def test_coding_wording(self):
        text = fh.section({'domain': 'code', 'rows': [], 'excluded_agents': []})
        self.assertIn('hard-codes test cases', text)


@unittest.skipUnless(HAVE_MPL, 'matplotlib not installed')
class RenderTest(unittest.TestCase):
    def test_write_png_and_publish_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(tmp)
            out = Path(tmp) / 'out'
            fh.write(run, out)
            png = (out / fh.IMAGE).read_bytes()
            self.assertTrue(png.startswith(b'\x89PNG'))
            base = {f'{run.name}/results.html': PAGE.encode(), 'index.html': b'<html></html>'}
            files, plan = cf.plan_heatmap(base, [], {run.name: str(out)})
            self.assertEqual(files[f'{run.name}/{fh.IMAGE}'], png)
            self.assertIn(b'id="faking-over-time"', files[f'{run.name}/results.html'])
            self.assertEqual(files['index.html'], base['index.html'])
            self.assertEqual(plan['added_files'], 1)
            # a run without fakers: summary only, no PNG, note on the page
            quiet = make_run(Path(tmp) / 'q', name='r2-20261103T000000Z', fakes=False)
            fh.write(quiet, quiet)
            self.assertFalse((quiet / fh.IMAGE).exists())
            files, plan = cf.plan_heatmap({f'{quiet.name}/results.html': PAGE.encode()}, [],
                                          {quiet.name: str(quiet)})
            self.assertIn(b'No fakes in this run', files[f'{quiet.name}/results.html'])
            self.assertEqual(plan['without_fakers'], [quiet.name])
            with self.assertRaises(ValueError):
                cf.plan_heatmap(base, [], {'not-on-site': str(out)})


if __name__ == '__main__':
    unittest.main()
