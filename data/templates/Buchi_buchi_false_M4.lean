import FormalConjectures.Util.ProblemImports

namespace Buchi

def IsBuchi (M : ℕ) : Prop :=
  ∀ x a : ℤ, (∀ n ∈ Finset.range M, IsSquare ((x + (n : ℤ)) ^ 2 + a)) → a = 0

theorem buchi_false_M0 : ¬ IsBuchi 0 := by
  simp only [IsBuchi, Finset.range_zero, not_forall, exists_prop]
  exact ⟨0, 1, by norm_num⟩

theorem buchi_false_M1 : ¬ IsBuchi 1 := by
  simp only [IsBuchi, not_forall]
  refine ⟨0, 4, ?_, by norm_num⟩
  intro n hn
  simp only [Finset.range_one, Finset.mem_singleton] at hn; subst hn
  exact ⟨2, by norm_num⟩

theorem buchi_false_M2 : ¬ IsBuchi 2 := by
  simp only [IsBuchi, not_forall]
  refine ⟨7, -48, ?_, by norm_num⟩
  intro n hn
  simp [Finset.mem_range] at hn
  interval_cases n
  · exact ⟨1, by norm_num⟩
  · exact ⟨4, by norm_num⟩

theorem buchi_false_M3 : ¬ IsBuchi 3 := by
  simp only [IsBuchi, not_forall]
  refine ⟨24, -576, ?_, by norm_num⟩
  intro n hn
  simp [Finset.mem_range] at hn
  interval_cases n
  · exact ⟨0, by norm_num⟩
  · exact ⟨7, by norm_num⟩
  · exact ⟨10, by norm_num⟩

-- EDITABLE START
-- EDITABLE END

theorem buchi_false_M4 : ¬ IsBuchi 4 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Buchi
