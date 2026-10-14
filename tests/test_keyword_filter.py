"""Policy checks only; fixtures are not observed experimental behavior."""

import unittest

from research_swarm.keyword_filter import check_keywords


class KeywordFilterTests(unittest.TestCase):
    def test_each_forbidden_construct_is_rejected(self):
        examples = {
            "axiom": "axiom claimed : False",
            "sorry": "theorem claimed : True := by sorry",
            "macro": 'macro "shortcut" : term => `(True)',
            "syntax": 'syntax "shortcut" : term',
        }
        for keyword, source in examples.items():
            with self.subTest(keyword=keyword):
                result = check_keywords(source)
                self.assertFalse(result.passed)
                self.assertEqual(result.matched_keywords, (keyword,))

    def test_comments_are_not_exempt(self):
        self.assertFalse(check_keywords("-- avoid sorry\nexample : True := by trivial").passed)
        self.assertFalse(check_keywords("/- macro documentation -/").passed)

    def test_longer_identifiers_are_not_exempt(self):
        self.assertEqual(check_keywords("macro_rules").matched_keywords, ("macro",))
        self.assertFalse(check_keywords("def sorryCount := 0").passed)

    def test_case_is_not_normalized(self):
        self.assertTrue(check_keywords("-- AXIOM Sorry Macro SYNTAX").passed)

    def test_scan_covers_source_outside_editable_regions(self):
        source = "-- sorry outside\n-- EDITABLE START\ntrivial\n-- EDITABLE END"
        self.assertFalse(check_keywords(source).passed)

    def test_repeated_matches_have_stable_order(self):
        result = check_keywords("syntax macro sorry axiom sorry")
        self.assertEqual(result.matched_keywords, ("axiom", "sorry", "macro", "syntax"))

    def test_ordinary_proof_passes_only_this_stage(self):
        self.assertTrue(check_keywords("example : True := by trivial").passed)

    def test_prompt_prohibitions_do_not_silently_expand_blacklist(self):
        # Passing the keyword stage does not imply compilation or valid mathematics.
        examples = (
            'local notation "Predicate" => fun _ => True',
            'local infix:50 "CUSTOM" => fun _ _ => False',
            "local instance : Inhabited Nat := ⟨0⟩",
            "def MyAns := True\nexample : answer(MyAns) ↔ True := by exact Iff.rfl",
            "#exit",
            "native_decide",
            "unsafe",
            "implemented_by",
            "elab_rules",
            "admit",
            "#guard_msgs (drop warning) in",
            "#eval (IO.Process.exit 0 : IO Unit)",
            "/- opened here, closed elsewhere",
        )
        for source in examples:
            with self.subTest(source=source):
                self.assertTrue(check_keywords(source).passed)

    def test_options_naming_a_keyword_are_caught_literally(self):
        # Lean's `warn.sorry` option would hide admission warnings; its name
        # contains the literal keyword, so the whole-source scan rejects it.
        self.assertEqual(check_keywords("set_option warn.sorry false in").matched_keywords, ("sorry",))


if __name__ == "__main__":
    unittest.main()
