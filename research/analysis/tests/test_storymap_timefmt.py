"""Story-map run times as hours / minutes / seconds (storymap/timefmt.py; owner, November 17, 2026).

    PYTHONPATH=src:. .venv/bin/python -m unittest research.analysis.tests.test_storymap_timefmt
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from research.analysis.storymap import view
from research.analysis.storymap.timefmt import (FMT_JS, fmt_hms, fmt_minutes_in_text, fmt_span, fmt_tick,
                                               fmt_times_in_prose)

REPO = Path(__file__).resolve().parents[3]
CASES = [0, 0.004, 0.5, 0.75, 1, 7.66, 7.7, 12.3, 59.99, 60, 63.2, 145, 179.999, 180, -0.2]
TICKS = [0, 10, 30, 45, 60, 90, 120, 150, 180, 240]


class PythonFormatTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(fmt_hms(7.66), "7m 40s")
        self.assertEqual(fmt_hms(0.75), "45s")
        self.assertEqual(fmt_hms(63.2), "1h 03m 12s")
        self.assertEqual(fmt_hms(60), "1h 00m 00s")
        self.assertEqual(fmt_hms(12.3), "12m 18s")
        self.assertEqual(fmt_hms(0), "0s")
        self.assertEqual(fmt_hms(-0.2), "0s")  # setup items a moment before 0
        self.assertEqual(fmt_hms(-0.2, clamp=False), "−12s")
        self.assertEqual(fmt_hms(None), "0s")
        self.assertEqual(fmt_hms(1 / 120), "1s")  # half a second rounds up, as Math.round

    def test_ticks_and_span(self):
        self.assertEqual([fmt_tick(t) for t in (0, 30, 60, 90, 180)], ["0m", "30m", "1h", "1h 30m", "3h"])
        self.assertEqual(fmt_span(7.66, 63.2), "7m 40s–1h 03m 12s")

    def test_text(self):
        self.assertEqual(fmt_minutes_in_text("reverted, 7.7 min after acceptance"), "reverted, 7m 42s after acceptance")
        self.assertEqual(fmt_minutes_in_text("latencies 7.7, 12.3 minutes"), "latencies 7.7, 12m 18s")
        self.assertEqual(fmt_minutes_in_text("within 2 min; half-life 10 min"), "within 2 min; half-life 10 min")
        self.assertEqual(fmt_minutes_in_text("v1.5 min-width"), "v1.5 min-width")

    def test_prose(self):
        self.assertEqual(fmt_times_in_prose("At 8.913\u201310.231 minutes, agent-084 read it. At 11.969 it read "
                                            "agent-101's fake and by 40.5 min it stopped."),
                         "At 8m 55s\u201310m 14s, agent-084 read it. At 11m 58s it read agent-101's fake and by 40m 30s "
                         "it stopped.")
        self.assertEqual(fmt_times_in_prose("at 3 of 5 tasks, at 0.85 s, 2 hours, item 12.5"),
                         "at 3 of 5 tasks, at 0.85 s, 2 hours, item 12.5")
        rec = view.prose_times({"story": "At 7.39 minutes it read", "quote": "at 7.39 minutes I", "thoughts": [
            {"note": "plan at 14.398", "quote": "wait 14.398 min"}]})
        self.assertEqual(rec, {"story": "At 7m 23s it read", "quote": "at 7.39 minutes I", "thoughts": [
            {"note": "plan at 14m 24s", "quote": "wait 14.398 min"}]})

    def test_view_uses_it(self):
        self.assertEqual(view.fmt_t(7.66), "7m 40s")
        self.assertEqual(view.fmt_span(0, 63.2), "0s–1h 03m 12s")
        self.assertEqual(view.tidy_times("accepted at 12.771"), "accepted at 12m 46s")
        self.assertEqual(view.tidy_times("sub-000001 by agent-101 (seed, FAKE) reverted, 15.5 min after acceptance"),
                         "sub-000001 by agent-101 (seed, FAKE) reverted, 15m 30s after acceptance")
        self.assertIn("FMT_JS", dir(view))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class JsAgreesTest(unittest.TestCase):
    def _run(self, js):
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_page_formatter_matches_python(self):
        got = self._run(FMT_JS + f"console.log(JSON.stringify([{json.dumps(CASES)}.map(m=>fmtHMS(m)),"
                                 f"{json.dumps(TICKS)}.map(fmtTick),fmtHMS(-0.2,true)]))")
        self.assertEqual(got[0], [fmt_hms(m) for m in CASES])
        self.assertEqual(got[1], [fmt_tick(t) for t in TICKS])
        self.assertEqual(got[2], fmt_hms(-0.2, clamp=False))

    def test_graph_viewer_formatter_matches_python(self):
        tpl = (REPO / "scripts" / "storymap_graph_template.html").read_text(encoding="utf-8")
        fn = re.search(r"^function fmtT\(t\) \{.*\}$", tpl, re.M).group(0)
        got = self._run(fn + f"console.log(JSON.stringify({json.dumps(CASES)}.map(fmtT)))")
        self.assertEqual(got, [fmt_hms(m, clamp=False) for m in CASES])
        self.assertNotIn("run min<", tpl)  # the clock shows run time only (comments may say "run minutes")


if __name__ == "__main__":
    unittest.main()
