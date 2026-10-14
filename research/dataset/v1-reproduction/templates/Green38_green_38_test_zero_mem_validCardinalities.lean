import FormalConjectures.Util.ProblemImports

open Filter Set
open scoped Pointwise

namespace Green38

abbrev 𝔽₇ (n : ℕ) := Fin n → ZMod 7

def IntersectsOnlyAtZero {n : ℕ} (A : Finset (𝔽₇ n)) : Prop :=
  ∀ a ∈ A - A, (∀ i, a i ∈ ({-1, 0, 1} : Finset (ZMod 7))) → a = 0

def ValidCardinalities (n : ℕ) : Set ℕ :=
  { (A.card) | (A : Finset (𝔽₇ n)) (_ : IntersectsOnlyAtZero A) }

theorem green_38.test_n1_lower :
    3 ∈ ValidCardinalities 1 := by
  refine ⟨{![0], ![2], ![4]}, ?_, by decide⟩
  intro a ha
  simp at ha ⊢

  cases ha
  · norm_num [← List.ofFn_injective.eq_iff]
    decide
  · revert ‹_›
    use by decide+revert ∘ List.mem_cons.1

noncomputable def LargestAdmissibleCardinality : ℕ → ℝ := fun n ↦
  (sSup (ValidCardinalities n) : ℕ)

noncomputable def C₁ : ℝ := (367 : ℝ) ^ (5⁻¹ : ℝ)

noncomputable def C₂ : ℝ := (7 * Real.cos (Real.pi / 7)) / (1 + Real.cos (Real.pi / 7))

-- EDITABLE START
-- EDITABLE END

theorem green_38.test_zero_mem_validCardinalities {n : ℕ} :
    0 ∈ ValidCardinalities n := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Green38
