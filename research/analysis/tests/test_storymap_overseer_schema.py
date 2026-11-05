"""Offline tests of the overseer story-map schemas, codebook and prompt loader (INTERFACE.md, "Overseer mode").

Run from the repository root:
    PYTHONPATH=src:. .venv/bin/python -m unittest research.analysis.tests.test_storymap_overseer_schema

The codebook is public and always read; the prompt tests read private/research/analysis/storymap/prompts_overseer.md
and are skipped in a checkout without private/. No model calls, network or credentials.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parents[1]
REPO = ANALYSIS.parents[1]
for path in (str(REPO), str(REPO / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from research.analysis import private_material  # noqa: E402
from research.analysis.storymap import schema, schema_overseer as so  # noqa: E402

ITEMS = [
    {"i": 61, "turn": 9, "t": 8.1, "kind": "reasoning",
     "text": "Tiny file for an open conjecture... maybe the answer just restates the conjecture?"},
    {"i": 87, "turn": 12, "t": 12.4, "kind": "reasoning",
     "text": "The answer is the RHS itself,  so the goal is P ↔ P and Iff.rfl closes it. No mathematics."},
]


def problems(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except so.SchemaError as error:
        return error.problems
    return []


class TaxonomyTests(unittest.TestCase):
    def test_codes_groups_and_events(self):
        info = so.taxonomy_info()
        expected = ([f"O{n}" for n in range(1, 6)] + [f"H{n}" for n in range(1, 7)] + [f"I{n}" for n in range(1, 6)]
                    + ["T1", "T2"] + [f"A{n}" for n in range(1, 11)] + ["G1", "G2"])
        self.assertEqual(info["codes"], expected)
        self.assertEqual(info["thought_codes"], expected)
        self.assertEqual(info["reaction_codes"], [])
        self.assertEqual(sorted(info["groups"]), sorted(so.THOUGHT_GROUPS))
        self.assertEqual(info["groups"]["A"], [f"A{n}" for n in range(1, 11)])
        self.assertEqual(sorted(info["event_types"]), sorted(so.EVENT_TYPES))
        for event in ("K1", "V1", "M1", "M2"):
            self.assertIn(event, info["event_types"])
        self.assertEqual(info["version"], "storymap-overseer-taxonomy/1")
        self.assertEqual(len(info["sha256"]), 64)
        self.assertEqual(info["path"], "research/analysis/storymap/taxonomy_overseer.md")
        self.assertEqual(info["names"]["T1"], "Recognises the trick")
        self.assertEqual(so.taxonomy_codes(), expected)

    def test_viewer_style_sections(self):
        """Every code heading has a definition paragraph and an example (as view.load_definitions expects)."""
        text = so.TAXONOMY_PATH.read_text(encoding="utf-8")
        sections = text.split("\n### ")
        for code in so.taxonomy_info()["codes"]:
            body = next(s for s in sections if s.startswith(f"{code} - "))
            paragraph = body.split("\n", 1)[1].strip()
            self.assertTrue(paragraph, code)
        self.assertEqual(len(text.splitlines()) < 400, True)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(private_material.PrivateMaterialMissing):
                so.taxonomy_info(Path(folder) / "absent.md")


class Stage1Tests(unittest.TestCase):
    def test_example_valid(self):
        out = so.validate_stage1(so.EXAMPLE_STAGE1)
        self.assertEqual(out["state"]["status"], "investigating_trick")
        text = "Reply:\n```json\n" + json.dumps(so.EXAMPLE_STAGE1, ensure_ascii=False) + "\n```"
        self.assertEqual(so.validate_stage1(text), out)

    def test_quotes_verbatim_against_items(self):
        so.validate_stage1(so.EXAMPLE_STAGE1, items=ITEMS)
        obj = copy.deepcopy(so.EXAMPLE_STAGE1)
        obj["thoughts"][1]["quote"] = "the goal is trivially P"
        self.assertTrue(any("verbatim" in p for p in problems(so.validate_stage1, obj, items=ITEMS)))
        soft = []
        so.validate_stage1(obj, items=ITEMS, soft=soft)
        self.assertTrue(any("verbatim" in p for p in soft))
        obj["thoughts"][1]["item"] = 999
        self.assertTrue(any("not among the items" in p for p in problems(so.validate_stage1, obj, items=ITEMS)))

    def test_turn_and_t_fix(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE1)
        obj["thoughts"][1]["t"] = 13.0
        self.assertTrue(problems(so.validate_stage1, obj, items=ITEMS))
        warnings = []
        out = so.validate_stage1(obj, items=ITEMS, fix_item_fields=True, warnings=warnings)
        self.assertEqual(out["thoughts"][1]["t"], 12.4)
        self.assertTrue(warnings)

    def test_codes(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE1)
        for bad in ("B1", "K1", "E1", "F1"):
            obj["thoughts"][0]["code"] = bad
            self.assertTrue(problems(so.validate_stage1, obj), bad)
        obj["thoughts"][0]["code"] = "A10"
        so.validate_stage1(obj)
        self.assertTrue(problems(so.validate_stage1, obj, codes=["T1"]))

    def test_state(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE1)
        obj["state"]["status"] = "faking"
        self.assertTrue(any("state.status" in p for p in problems(so.validate_stage1, obj)))
        for status in so.STATUSES:
            obj["state"]["status"] = status
            so.validate_stage1(obj)
        obj["state"]["suspected"] = ["answer_restatement", "sorry"]
        self.assertTrue(any("suspected[1]" in p for p in problems(so.validate_stage1, obj)))
        obj["state"]["suspected"] = "axioms"
        self.assertTrue(problems(so.validate_stage1, obj))
        obj["state"]["suspected"] = ["axioms", "axioms", "other"]
        self.assertEqual(so.validate_stage1(obj)["state"]["suspected"], ["axioms", "other"])
        obj["state"]["knows_trick"] = "yes"
        self.assertTrue(problems(so.validate_stage1, obj))
        obj = copy.deepcopy(so.EXAMPLE_STAGE1)
        del obj["state"]["knows_trick"]
        obj["state"]["stance"] = "none"
        self.assertTrue(any("knows_trick" in p for p in problems(so.validate_stage1, obj)))

    def test_summary_truncated(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE1)
        obj["state"]["summary"] = "x" * 900
        truncated = []
        out = so.validate_stage1(obj, truncated=truncated)
        self.assertEqual(len(out["state"]["summary"]), so.SUMMARY_MAX_CHARS)
        self.assertEqual(truncated[0]["from_chars"], 900)

    def test_empty_thoughts(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE1)
        obj["thoughts"] = []
        so.validate_stage1(obj)


class Stage2Tests(unittest.TestCase):
    def test_example_valid(self):
        so.validate_stage2(so.EXAMPLE_STAGE2, event_id=so.EXAMPLE_STAGE2["event_id"], items=ITEMS,
                           overseer_expected=True)
        so.validate_stage2(so.EXAMPLE_STAGE2, overseer_expected=False)

    def test_event_id(self):
        self.assertTrue(problems(so.validate_stage2, so.EXAMPLE_STAGE2, event_id="M1:ovpost-000001"))

    def test_response_enum_and_rules(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE2)
        obj["response"] = "punish"
        self.assertTrue(any("response" in p for p in problems(so.validate_stage2, obj)))
        for response in so.RESPONSES[1:]:
            obj["response"] = response
            so.validate_stage2(obj)
        obj["response"] = "none"
        self.assertTrue(any("behaviour" in p for p in problems(so.validate_stage2, obj)))
        obj = copy.deepcopy(so.EXAMPLE_STAGE2)
        obj["registered"] = "no"
        found = problems(so.validate_stage2, obj)
        self.assertTrue(any(p.startswith("change") for p in found))
        self.assertTrue(any(p.startswith("response") for p in found))
        self.assertTrue(any(p.startswith("recognised_trick") for p in found))
        obj.update(change="none", response="none", recognised_trick=None, quotes=[])
        so.validate_stage2(obj)

    def test_recognised_trick_and_quotes(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE2)
        obj["recognised_trick"] = "yes"
        self.assertTrue(problems(so.validate_stage2, obj))
        obj["recognised_trick"] = None
        so.validate_stage2(obj)
        obj["quotes"] = []
        self.assertTrue(any("quote" in p for p in problems(so.validate_stage2, obj)))
        obj = copy.deepcopy(so.EXAMPLE_STAGE2)
        obj["quotes"][0]["quote"] = "invented words"
        self.assertTrue(any("verbatim" in p for p in problems(so.validate_stage2, obj, items=ITEMS)))

    def test_solver_fields_rejected(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE2)
        del obj["response"]
        obj["direction"] = "away"
        self.assertTrue(any("response" in p for p in problems(so.validate_stage2, obj)))


class Stage3Tests(unittest.TestCase):
    def test_example_valid(self):
        ids = {"K1:agent-102:1": 29.5}
        out = so.validate_stage3(so.EXAMPLE_STAGE3, agent="agent-102", event_ids=ids, structure=True)
        self.assertEqual(out["phases"][2]["status"], "enforcing")
        soft = []
        so.validate_stage3(so.EXAMPLE_STAGE3, event_ids=ids, structure=True, soft=soft)
        self.assertEqual(soft, [])

    def test_agent_and_events(self):
        self.assertTrue(problems(so.validate_stage3, so.EXAMPLE_STAGE3, agent="agent-103"))
        self.assertTrue(any("not one of" in p for p in
                            problems(so.validate_stage3, so.EXAMPLE_STAGE3, event_ids=["K1:agent-102:2"])))

    def test_status_enum(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["phases"][0]["status"] = "considering"
        self.assertTrue(any("phases[0].status" in p for p in problems(so.validate_stage3, obj)))
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["phases"][0]["stance"] = obj["phases"][0].pop("status")
        self.assertTrue(problems(so.validate_stage3, obj))
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["turning_points"][0]["to"] = "not_looking"
        self.assertTrue(any("differ" in p for p in problems(so.validate_stage3, obj)))

    def test_final_rules(self):
        cases = [
            ({"detected_trick": True, "detection_t": None}, "detection_t"),
            ({"detected_trick": False}, "detection_t"),
            ({"first_suspected": None}, "first_suspected"),
            ({"first_suspected": "sorry"}, "first_suspected"),
            ({"public_posts_specific": "some"}, "public_posts_specific"),
            ({"public_posts_specific": "none"}, "names_trick_publicly"),
            ({"communicated": False}, "communicated"),
            ({"coordinated": "n/a"}, "coordinated"),
            ({"enforced": None}, "enforced"),
        ]
        for change, field in cases:
            obj = copy.deepcopy(so.EXAMPLE_STAGE3)
            obj["final"].update(change)
            self.assertTrue(any(field in p for p in problems(so.validate_stage3, obj)), change)
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["final"].update(detected_trick=False, detection_t=None, confirmed_on_real_t=None, first_suspicion_t=None,
                            first_suspected=None, enforced=False, communicated=False, coordinated=False,
                            public_posts_specific="none", names_trick_publicly=False, states_consequences=False)
        so.validate_stage3(obj)

    def test_story_length(self):
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["story"] = "y" * 1500
        self.assertTrue(problems(so.validate_stage3, obj))
        soft = []
        self.assertEqual(len(so.validate_stage3(obj, soft=soft)["story"]), so.STORY_MAX_CHARS)
        self.assertTrue(soft)

    def test_structure(self):
        def soft_of(obj, **kwargs):
            soft = []
            so.validate_stage3(obj, structure=True, soft=soft, **kwargs)
            return soft
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["phases"][1]["from_t"] = 7.5                          # gap after phase 0
        self.assertTrue(any("gap" in p for p in soft_of(obj)))
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["turning_points"][1]["t"] = 50.0                      # not at a boundary
        self.assertTrue(any("boundary" in p for p in soft_of(obj)))
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["turning_points"][1]["from"] = "idle"                 # not chained
        self.assertTrue(any("status before it" in p for p in soft_of(obj)))
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        self.assertTrue(any("after the turning point" in p for p in soft_of(obj, event_ids={"K1:agent-102:1": 45.0})))
        obj["final"]["confirmed_on_real_t"] = 10.0                # before detection
        self.assertTrue(any("confirmed_on_real_t" in p for p in soft_of(obj)))
        obj = copy.deepcopy(so.EXAMPLE_STAGE3)
        obj["phases"].insert(1, {"from_t": 6.5, "to_t": 6.5, "status": "idle", "label": "blip"})
        self.assertTrue(any("zero length" in p for p in soft_of(obj)))
        self.assertEqual(problems(so.validate_stage3, so.EXAMPLE_STAGE3), [])   # structure off by default


class AdapterTests(unittest.TestCase):
    def test_pipeline_adapter_validates(self):
        from research.analysis.storymap import pipeline
        adapter = pipeline.SchemaAdapter(so)
        for stage, example, context in ((1, so.EXAMPLE_STAGE1, {"items": ITEMS, "fix_item_fields": True}),
                                        (2, so.EXAMPLE_STAGE2, {"event_id": so.EXAMPLE_STAGE2["event_id"],
                                                                "items": ITEMS, "overseer_expected": None}),
                                        (3, so.EXAMPLE_STAGE3, {"agent": "agent-102", "event_ids": ["K1:agent-102:1"]})):
            out, hard, soft, _ = adapter.validate_noted(stage, json.dumps(example, ensure_ascii=False), **context)
            self.assertIsNotNone(out, hard)
            self.assertEqual((hard, soft), ([], []))

    def test_solver_schema_untouched(self):
        self.assertEqual(schema.SCHEMA_VERSION, "storymap-schema/1")
        self.assertNotEqual(so.SCHEMA_VERSION, schema.SCHEMA_VERSION)


@private_material.skip_decorator(so.PROMPTS_RELATIVE)
class PromptTests(unittest.TestCase):
    def test_prompts_render(self):
        taxonomy = so.taxonomy_info()
        texts = so.prompt_texts()
        self.assertEqual(set(texts), set(so.STAGES))
        shared_prefix = None
        for stage, info in texts.items():
            text = info["text"]
            self.assertNotIn("{{", text)
            self.assertIn(taxonomy["text"].strip("\n"), text)
            self.assertIn(taxonomy["sha256"], text)
            self.assertIn("storymap-overseer-prompts/1", text)
            self.assertIn("```json", text)
            self.assertTrue(text.rstrip().endswith(taxonomy["text"].strip()))
            self.assertEqual(info["taxonomy_sha256"], taxonomy["sha256"])
            self.assertEqual(info["taxonomy_version"], "storymap-overseer-taxonomy/1")
            self.assertEqual(info, so.prompt_info(stage))
            head = text.split("\n\nSTAGE ")[0]
            shared_prefix = shared_prefix or head
            self.assertEqual(head, shared_prefix)
        self.assertEqual(len({info["sha256"] for info in texts.values()}), 3)
        self.assertIn("STAGE 1 - THOUGHTS", texts["stage1"]["text"])
        self.assertIn("STAGE 2 - REACTION", texts["stage2"]["text"])
        self.assertIn("STAGE 3 - THE OVERSEER'S STORY", texts["stage3"]["text"])
        self.assertIn("ONE overseer agent", texts["stage1"]["text"])
        with self.assertRaises(ValueError):
            so.prompt_info("stage4")

    def test_prompt_examples_validate(self):
        texts = so.prompt_texts()
        for stage, validate in (("stage1", so.validate_stage1), ("stage2", so.validate_stage2),
                                ("stage3", so.validate_stage3)):
            block = texts[stage]["text"].split("STAGE ")[1]
            example = block.split("```json\n", 1)[1].split("\n```", 1)[0]
            validate(json.loads(example))
        block = texts["stage3"]["text"].split("STAGE ")[1]
        example = json.loads(block.split("```json\n", 1)[1].split("\n```", 1)[0])
        soft = []
        so.validate_stage3(example, structure=True, soft=soft)
        self.assertEqual(soft, [])

    def test_adapter_record(self):
        from research.analysis.storymap import pipeline
        record = pipeline.SchemaAdapter(so).record()
        self.assertEqual(record["schema_version"], "storymap-overseer-schema/1")
        self.assertTrue(record["stage1"]["prompts_path"].endswith("prompts_overseer.md"))

    def test_missing_prompts(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(private_material.PrivateMaterialMissing):
                so.prompt_info("stage1", prompts_path=Path(folder) / "absent.md")


if __name__ == "__main__":
    unittest.main()
