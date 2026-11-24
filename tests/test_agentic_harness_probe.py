"""scripts/agentic_harness_probe.py, pure parts (no Docker, no model)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

import agentic_harness_probe as hp  # noqa: E402

TOOLS = ["mcp__review__wait", "mcp__review__revert_submission", "mcp__review__list_revertible",
         "mcp__review__get_output", "mcp__review__message_solver"]


def run(**over):
    prompt = "PROMPT"
    delivery = hp.filler(40_000, hp.DELIVERY_HEAD, hp.DELIVERY_TAIL)
    big = hp.filler(120_000, hp.BIG_HEAD, hp.BIG_TAIL)
    rows = [{"step": "side", "time": 0, "tools": ["Bash"], "system": "x", "last_tool_text": None},
            {"step": "first", "time": 1, "tools": TOOLS, "system": "header\n" + prompt, "last_tool_text": None},
            {"step": "after_delivery", "time": 2, "tools": TOOLS, "system": prompt, "last_tool_text": delivery},
            {"step": "after_big", "time": 3, "tools": TOOLS, "system": prompt, "last_tool_text": big},
            {"step": "after_long_wait", "time": 1804, "tools": TOOLS, "system": prompt,
             "last_tool_text": hp.WAIT_OK + " after 1800 s"},
            {"step": "final", "time": 1805, "tools": [], "system": "", "last_tool_text": None}]
    for step, patch in over.items():
        next(r for r in rows if r["step"] == step).update(patch)
    return hp.evaluate(rows, wait_seconds=1800, delivery_chars=40_000, big_chars=120_000, prompt=prompt)


class ProbeTests(unittest.TestCase):
    def test_filler_exact(self):
        text = hp.filler(1000, "H", "T")
        self.assertEqual((len(text), text[0], text[-1]), (1000, "H", "T"))
        with self.assertRaises(ValueError):
            hp.filler(1, "HH", "T")

    def test_prompt_replaced_records_other_text(self):
        checks = run(first={"system": "x" * 600 + "\nPROMPT"})
        self.assertFalse(checks["prompt_replaced"]["passed"])
        self.assertEqual(checks["prompt_replaced"]["other_system_chars"], 601)
        self.assertTrue(checks["prompt_replaced"]["other_system_text"].endswith("\n<PROMPT>"))
        self.assertTrue(run(first={"system": "PROMPT"})["prompt_replaced"]["passed"])

    def test_all_pass(self):
        checks = run()
        self.assertTrue(all(v["passed"] for k, v in checks.items() if not v.get("informational")))
        self.assertTrue(checks["oversized_result"]["whole"])
        self.assertIsNone(checks["oversized_result"]["passed"])
        self.assertEqual(checks["long_wait_survives"]["elapsed_s"], 1801)

    def test_builtin_tool_offered_fails(self):
        checks = run(first={"tools": TOOLS + ["Bash"]})
        self.assertFalse(checks["only_monitor_tools"]["passed"])
        self.assertEqual(checks["only_monitor_tools"]["extra"], ["Bash"])

    def test_wait_timeout_and_cut_delivery_fail(self):
        checks = run(after_long_wait={"last_tool_text": "MCP error -32001: Request timed out", "time": 63},
                     after_delivery={"last_tool_text": hp.DELIVERY_HEAD + "short"})
        self.assertFalse(checks["long_wait_survives"]["passed"])
        self.assertFalse(checks["delivery_intact"]["passed"])

    def test_oversized_result_file_reference_recorded(self):
        checks = run(after_big={"last_tool_text": "Output too large; saved to /tmp/x.txt"})
        self.assertFalse(checks["oversized_result"]["whole"])
        self.assertTrue(checks["oversized_result"]["file_reference_hint"])


if __name__ == "__main__":
    unittest.main()
