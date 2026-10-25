import FormalConjectures.Util.ProblemImports

namespace EuclidNumbers

noncomputable def Euclid (n : ℕ) : ℕ := 1 + ∏ i ∈ Finset.range n, i.nth Nat.Prime

-- EDITABLE START
-- EDITABLE END

theorem euclid_numbers_are_square_free : answer(default) ↔ (∀ n, Squarefree (Euclid n)) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end EuclidNumbers
