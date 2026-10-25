"""Offline tests for the judge's screening windows (research/analysis/judge/windows.py)."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO / "src", REPO):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from research.analysis.judge import windows  # noqa: E402
from research.analysis.judge.history import estimate_tokens  # noqa: E402


def item(n, chars, agent="agent-007", text=None):
    return {"id": f"A07.t{n:04d}.x", "agent_id": agent, "kind": "text", "text": text if text is not None else "a" * chars}


class WindowTests(unittest.TestCase):
    def test_contiguous_chronological_and_ids(self):
        items = [item(n, 4000) for n in range(10)]          # 1000 tokens each
        made = windows.make_windows(items, target_tokens=3000)
        self.assertEqual([w["id"] for w in made], ["A07.w0001", "A07.w0002", "A07.w0003", "A07.w0004"])
        self.assertEqual([len(w["item_ids"]) for w in made], [3, 3, 3, 1])
        flat = [i for w in made for i in w["item_ids"]]
        self.assertEqual(flat, [i["id"] for i in items])     # every item once, in order
        self.assertEqual([w["tokens"] for w in made], [3000, 3000, 3000, 1000])
        self.assertTrue(all(w["fragments"] == {} for w in made))

    def test_item_up_to_twice_target_is_never_split(self):
        items = [item(0, 400), item(1, 4 * 5000), item(2, 400)]   # 100, 5000, 100 tokens; target 3000
        made = windows.make_windows(items, target_tokens=3000)
        self.assertEqual([w["item_ids"] for w in made], [["A07.t0000.x"], ["A07.t0001.x"], ["A07.t0002.x"]])
        self.assertEqual(made[1]["tokens"], 5000)

    def test_oversized_item_split_into_numbered_fragments(self):
        text = "".join(f"line {n:05d} {'z' * 40}\n" for n in range(2000))   # ~104,000 chars
        items = [item(0, 400), item(1, 0, text=text), item(2, 400)]
        made = windows.make_windows(items, target_tokens=6000)
        pieces = [p for w in made for p in w["item_ids"]]
        fragments = [p for p in pieces if "#f" in p]
        self.assertEqual(fragments, [f"A07.t0001.x#f{n}" for n in range(1, len(fragments) + 1)])
        self.assertGreaterEqual(len(fragments), 5)
        by_id = {i["id"]: i for i in items}
        rebuilt = "".join(t for w in made for pid, _, t in windows.window_pieces(w, by_id) if "#f" in pid)
        self.assertEqual(rebuilt, text)                     # fragments cover the item exactly
        for w in made:
            for pid, span in w["fragments"].items():
                self.assertLessEqual(estimate_tokens(text[span[0]:span[1]]), 6000)
                self.assertTrue(text[span[0]:span[1]].endswith("\n") or span[1] == len(text))
        self.assertEqual(pieces[0], "A07.t0000.x")
        self.assertEqual(pieces[-1], "A07.t0002.x")
        self.assertTrue(all(w["tokens"] <= 6000 for w in made))

    def test_split_text_without_line_breaks_progresses(self):
        ranges = windows.split_text("b" * 10001, 1000)
        self.assertEqual(ranges[0], [0, 4000])
        self.assertEqual(ranges[-1][1], 10001)
        self.assertEqual(sum(b - a for a, b in ranges), 10001)

    def test_empty_and_invalid(self):
        self.assertEqual(windows.make_windows([]), [])
        with self.assertRaises(ValueError):
            windows.make_windows([item(0, 4)], target_tokens=0)
        made = windows.make_windows([{"id": "post-000001", "agent_id": None, "text": "hi"}])
        self.assertEqual(made[0]["id"], "RUN.w0001")


if __name__ == "__main__":
    unittest.main()
