"""Offline tool-result evidence regressions; no Docker, providers, or credentials."""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from research_swarm.launch_gates import REQUIRED_EVIDENCE_CHECKS

try:
    from inspect_ai.event import ModelEvent
    from inspect_ai.model import ChatMessageTool, ContentText, GenerateConfig, ModelOutput
    from inspect_ai.tool import ToolCallError
    import validate_runtime as vr
    HAVE_INSPECT = True
except ImportError:
    HAVE_INSPECT = False


def event(messages, output=None):
    return ModelEvent(model="mockllm/runtime", input=messages, tools=[], tool_choice="auto",
                      config=GenerateConfig(), output=output or ModelOutput.from_content("mockllm/runtime", "done"))


def sample(messages=(), events=()):
    return types.SimpleNamespace(messages=list(messages), events=list(events), error=None, limit=None)


def result(call_id, content="ok", function=None, error=None):
    return ChatMessageTool(tool_call_id=call_id, content=content, function=function, error=error)


@unittest.skipUnless(HAVE_INSPECT, "optional Inspect runtime dependencies are not installed")
class RuntimeLogSummaryTests(unittest.TestCase):
    def test_compaction_preserves_earlier_tool_results_in_model_events(self):
        # The final compacted conversation retains only the recent result. The
        # required early conference/Lean/denied-tool results remain in history.
        early = [result("a1-post", '{"ok":true}', "mcp__conference__post_intent"),
                 result("a1-lean", "Lean completed", "Bash"),
                 result("a1-webfetch", "No such tool available: WebFetch", "WebFetch",
                        ToolCallError(type="unknown", message="disabled")),
                 result("a1-agent", "No such tool available: Agent", "Agent",
                        ToolCallError(type="unknown", message="disabled"))]
        recent = result("a1-check", "ABSENT", "Bash")
        output = ModelOutput.for_tool_call(model="mockllm/runtime", tool_name="mcp__conference__post_intent",
                                          tool_arguments={}, tool_call_id="a1-post")
        summary = vr.sample_summary(sample([recent], [event([], output), event(early), event([recent])]))
        self.assertEqual(set(summary["tool_results"]), {m.tool_call_id for m in early} | {"a1-check"})
        self.assertEqual(summary["tool_results"]["a1-lean"], "Lean completed")
        self.assertIn("mcp__conference__post_intent", summary["tool_calls_in_model_outputs"])
        self.assertEqual(summary["tool_result_conflicts"], {})
        self.assertEqual(summary["tool_result_metadata"]["a1-webfetch"]["error"],
                         {"type": "unknown", "message": "disabled"})
        self.assertEqual(summary["tool_result_metadata"]["a1-post"]["sources"], ["model_event[1].input[0]"])

    def test_final_message_result_without_followup_request_is_preserved(self):
        summary = vr.sample_summary(sample([result("last", "complete", "Bash")]))
        self.assertEqual(summary["tool_results"], {"last": "complete"})
        self.assertEqual(summary["tool_result_metadata"]["last"]["sources"], ["sample.messages[0]"])

    def test_equivalent_replays_deduplicate_and_keep_all_sources(self):
        # Inspect can render text blocks and plain strings identically, and a
        # replay may omit a known function name. Neither changes the result.
        plain = result("call", "same", "Bash")
        blocks = result("call", [ContentText(text="same")])
        summary = vr.sample_summary(sample([plain], [event([plain]), event([blocks])]))
        self.assertEqual(summary["tool_results"], {"call": "same"})
        metadata = summary["tool_result_metadata"]["call"]
        self.assertEqual(metadata["function"], "Bash")
        self.assertEqual(metadata["sources"], ["model_event[0].input[0]", "model_event[1].input[0]",
                                              "sample.messages[0]"])
        self.assertEqual(summary["tool_result_conflicts"], {})

    def test_conflicting_text_is_visible_and_cannot_satisfy_gate(self):
        summary = vr.sample_summary(sample([result("a1-post", "different")],
                                           [event([result("a1-post", "original")])]))
        self.assertNotIn("a1-post", summary["tool_results"])
        self.assertNotIn("a1-post", summary["tool_result_metadata"])
        self.assertEqual(len(summary["tool_result_conflicts"]["a1-post"]["variants"]), 2)

    def test_conflicting_error_state_is_not_silently_deduplicated(self):
        success = result("call", "same", "Bash")
        failure = result("call", "same", "Bash", ToolCallError(type="unknown", message="failed"))
        summary = vr.sample_summary(sample([success], [event([failure])]))
        self.assertEqual(summary["tool_results"], {})
        errors = [v["error"] for v in summary["tool_result_conflicts"]["call"]["variants"]]
        self.assertIn(None, errors)
        self.assertIn({"type": "unknown", "message": "failed"}, errors)

    def test_conflicting_function_identity_is_not_accepted(self):
        summary = vr.sample_summary(sample([result("call", function="Write")],
                                           [event([result("call", function="Bash")])]))
        self.assertEqual(summary["tool_results"], {})
        self.assertEqual(summary["tool_result_conflicts"]["call"]["functions"], ["Bash", "Write"])

    def test_model_output_alone_does_not_fabricate_a_tool_result(self):
        output = ModelOutput.for_tool_call(model="mockllm/runtime", tool_name="Bash",
                                          tool_arguments={}, tool_call_id="a1-lean")
        summary = vr.sample_summary(sample(events=[event([], output)]))
        self.assertEqual(summary["tool_results"], {})
        self.assertEqual(summary["tool_calls_in_model_outputs"], ["Bash"])

    def test_missing_tool_call_identity_is_flagged(self):
        summary = vr.sample_summary(sample([result("", "unattributable")]))
        self.assertEqual(summary["tool_results"], {})
        self.assertIn("<missing-tool-call-id>", summary["tool_result_conflicts"])

    def test_launch_gate_requires_consistency_even_for_old_evidence(self):
        self.assertIn("inspect_tool_result_replays_consistent", REQUIRED_EVIDENCE_CHECKS["runtime-e2e"])


if __name__ == "__main__":
    unittest.main()
