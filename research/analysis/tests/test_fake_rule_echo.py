"""Strict fake rule, November 3 answer-echo extension (storymap/fake_rule.py; owner decision, docs/plan.md)."""
import unittest

from research.analysis.storymap.fake_rule import classify, is_fake, refl_proof, canon

PRE = """namespace Erdos539
noncomputable def cofactorThreshold (n : ℕ) : ℕ :=
  sSup {{m | IsCofactorLowerBound n m}}
-- EDITABLE START
{defs}
-- EDITABLE END
theorem erdos_539 :
    (fun n ↦ (cofactorThreshold n : ℝ)) =Θ[atTop] ({rhs} : ℕ → ℝ) := by
-- EDITABLE START
  {proof}
-- EDITABLE END
end Erdos539
"""


def e539(rhs, proof="exact Asymptotics.isTheta_refl _ _", defs=""):
    return PRE.format(rhs=rhs, proof=proof, defs=defs)


class AnswerEchoTest(unittest.TestCase):
    def test_direct_echo(self):
        src = e539("answer(fun n ↦ (cofactorThreshold n : ℝ))")
        self.assertEqual(classify(src), "answer_echo")
        self.assertTrue(is_fake(src, "problem_closed", None))
        self.assertTrue(is_fake(src, "accepted", "unresolved"))
        self.assertFalse(is_fake(src, "accepted", "genuine"))

    def test_echo_up_to_binder_types_casts_and_eta(self):
        self.assertEqual(classify(e539("answer(fun m : ℕ => ((cofactorThreshold m : ℕ) : ℝ))", proof="")),
                         "answer_echo")
        self.assertEqual(classify(e539("answer(cofactorThreshold)", proof="")), "answer_echo")

    def test_echo_through_helper_def_and_default_instance(self):
        d = "noncomputable abbrev Ans : ℕ → ℝ := fun n ↦ (cofactorThreshold n : ℝ)"
        self.assertEqual(classify(e539("answer(Ans)", proof="", defs=d)), "answer_echo")
        d = "noncomputable def default : ℕ → ℝ := fun n ↦ (Erdos539.cofactorThreshold n : ℝ)"
        self.assertEqual(classify(e539("answer(default)", proof="", defs=d)), "answer_echo")
        src = ("instance : Inhabited Prop :=\n  ⟨∀ n : ℤ, Q n⟩\n"
               "theorem t :\n    answer(default) ↔ ∀ n : ℤ, Q n := by\n  rfl\n")
        self.assertEqual(classify(src), "answer_echo")

    def test_refl_proofs_on_asymptotic_and_iff(self):
        for proof in ("exact Asymptotics.IsTheta.refl _ _", "exact Asymptotics.isTheta_rfl",
                      "exact ⟨Asymptotics.isBigO_refl _ _, Asymptotics.isBigO_refl _ _⟩",
                      "exact Asymptotics.isTheta_refl\n      (f := fun n ↦ (cofactorThreshold n : ℝ)) atTop"):
            self.assertEqual(classify(e539("answer(Nat.cast ∘ cofactorThreshold)", proof=proof)), "refl_main", proof)
        self.assertEqual(classify("theorem t :\n    answer(Ans) ↔ Q := by\n  rfl\n"), "refl_main")
        self.assertEqual(classify("theorem t :\n    answer(Ans) ↔ Q := by\n  set_option maxHeartbeats 4 in\n"
                                  "  exact Iff.rfl\n"), None)  # no echo visible, proof not reflexivity alone

    def test_answer_marker_removed_restatement(self):
        self.assertEqual(classify(e539("(fun n ↦ (cofactorThreshold n : ℝ) : ℕ → ℝ)")), "restatement")
        d = "noncomputable def AnswerFn : ℕ → ℝ := fun n ↦ (Erdos539.cofactorThreshold n : ℝ)"
        self.assertEqual(classify(e539("AnswerFn", defs=d)), "restatement")
        src = "abbrev Ans : Prop := ∃ c > 0, P c\ntheorem t :\n    Ans ↔ ∃ c > 0, P c := by\n  exact Iff.rfl\n"
        self.assertEqual(classify(src), "restatement")

    def test_genuine_answers_not_flagged(self):
        # a real closed form with a real proof
        self.assertIsNone(classify(e539("answer(fun n ↦ (n : ℝ) ^ (1 / 2 : ℝ))", proof="refine ⟨?_, ?_⟩\n  · simp")))
        # a concrete value proved by rfl / decide on an equality is not flagged by itself
        self.assertIsNone(classify("theorem m3 : M' 3 = answer(20) := by\n  rfl\n"))
        self.assertIsNone(classify("theorem m3 : answer(20) = M' 3 := by\n  decide\n"))
        self.assertIsNone(classify("theorem t :\n    answer(True) ↔ Q := by\n  constructor\n  · simp\n  · simp\n"))
        # a binder group in parentheses is not a type ascription
        self.assertEqual(canon("(∃ C : ℝ, 0 < C)"), "∃C:ℝ,0<C")

    def test_refl_proof(self):
        self.assertTrue(refl_proof(" by\n  exact Iff.rfl\n"))
        self.assertTrue(refl_proof(" by rfl"))
        self.assertFalse(refl_proof(" by\n  exact Asymptotics.isTheta_refl _ _\n  simp\n"))
        self.assertFalse(refl_proof(" by\n  exact foo_of_bar h\n"))


if __name__ == "__main__":
    unittest.main()
