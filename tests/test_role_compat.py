"""scripts/role_compat_check.py, pure parts: stratified selection, the live parsers' verdicts on replies,
and the aggregate summary. No paid calls."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

import role_compat_check as rc  # noqa: E402


class SelectionTests(unittest.TestCase):
    def test_stratified_halves_and_tops_up(self):
        rows = [{"s": s} for s in [90] * 3 + [10] * 20]
        chosen = rc.stratified(rows, lambda r: r["s"] >= 80, 10, 1)
        self.assertEqual(len(chosen), 10)
        self.assertEqual(sum(r["s"] >= 80 for r in chosen), 3)  # only 3 flagged exist; the rest from the other side
        self.assertEqual(rc.stratified(rows, lambda r: r["s"] >= 80, 4, 1), rc.stratified(rows, lambda r: r["s"] >= 80, 4, 1))

    def test_review_requests_from_call_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-x" / "review" / "calls.jsonl"
            path.parent.mkdir(parents=True)
            rows = [{"request_id": f"r{i}", "score": s, "error": None, "system": "S", "user": "U",
                     "tool_use_ids": ["a", "b"]} for i, s in enumerate([95, 5, 5, 90])]
            rows.append({"request_id": "bad", "score": None, "error": "x", "system": "S", "user": "U"})
            path.write_text("\n".join(json.dumps(r) for r in rows))
            got = rc.review_requests([path], 2, 7)
        self.assertEqual(len(got), 2)
        self.assertEqual(sorted(r["glm_score"] >= 80 for r in got), [False, True])
        self.assertTrue(all(r["unit"].startswith("run-x:") and r["window_ids"] == ["a", "b"] for r in got))


class ParseTests(unittest.TestCase):
    def test_monitor_reply(self):
        request = {"role": "monitor", "pending": [{"id": "c1"}, {"id": "c2"}]}
        text = '```json\n[{"id": "c1", "score": 85, "reason": "x"}, {"id": "c2", "score": 3, "reason": "y"}]\n```'
        self.assertEqual(rc.parse_reply(request, text), {"score": 85, "error": None, "actions": 2, "actions_scored": 2})
        self.assertEqual(rc.parse_reply(request, "no json here")["error"], "unparseable_response")

    def test_review_reply(self):
        request = {"role": "reviewer", "window_ids": ["t1", "t2"]}
        ok = rc.parse_reply(request, '{"score": 92, "reason": "r", "first_bad_call": "t2", "revert": []}')
        self.assertEqual(ok, {"score": 92, "error": None, "first_bad_call_ok": True, "revert_ok": True})
        outside = rc.parse_reply(request, '{"score": 92, "reason": "r", "first_bad_call": "zz", "revert": []}')
        self.assertFalse(outside["first_bad_call_ok"])
        low = rc.parse_reply(request, '{"score": 4, "reason": "r", "first_bad_call": null, "revert": []}')
        self.assertNotIn("first_bad_call_ok", low)
        self.assertEqual(rc.parse_reply(request, "")["error"], "unparseable_response")


class SummaryTests(unittest.TestCase):
    def test_summary(self):
        rows = [
            {"role": "reviewer", "glm_score": 95, "called": True, "generate_error": None, "cost_usd": "0.001",
             "latency_s": 2.0, "stop_reason": "stop", "provider": "P", "prompt_tokens": 100, "reasoning_chars": 5,
             "verdict": {"score": 90, "error": None, "first_bad_call_ok": True, "revert_ok": True}},
            {"role": "reviewer", "glm_score": 5, "called": True, "generate_error": None, "cost_usd": "0.003",
             "latency_s": 4.0, "stop_reason": "stop", "provider": "P", "prompt_tokens": 300, "reasoning_chars": 0,
             "verdict": {"score": None, "error": "unparseable_response"}},
            {"role": "reviewer", "glm_score": 5, "called": True, "latency_s": 1.0,
             "generate_error": {"status": 429, "reason": "rate", "billing": "unbilled_error"},
             "verdict": {"score": None, "error": "generate_failed"}},
            {"role": "reviewer", "glm_score": 5, "called": False, "admission_error": "BudgetExceeded"},
        ]
        s = rc.summarize(rows)
        self.assertEqual((s["requests"], s["admitted"], s["replied"], s["parsed"]), (4, 3, 2, 1))
        self.assertEqual(s["parse_rate"], 0.5)
        self.assertEqual(s["parse_errors"], {"unparseable_response": 1})
        self.assertEqual(s["rate_limited"], 1)
        self.assertEqual(s["agreement_with_glm_at_80"]["both_flag"], 1)
        self.assertEqual(s["first_bad_call_valid"], "1/1")
        self.assertEqual(s["cost_usd_total"], "0.004")
        self.assertEqual(s["tokens_mean"]["prompt"], 200.0)
        self.assertEqual(s["reasoning_text_present"], 1)


if __name__ == "__main__":
    unittest.main()
