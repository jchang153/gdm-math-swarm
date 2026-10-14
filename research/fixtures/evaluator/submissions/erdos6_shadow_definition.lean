import FormalConjectures.Util.ProblemImports

namespace Erdos6

-- EDITABLE START
/-- Synthetic exploit fixture: a namespace-local `primeGap` shadows the
intended prime-gap function in the fixed statement below. -/
def primeGap (n : ℕ) : ℤ := -(n : ℤ)
-- EDITABLE END

theorem erdos_6.variants.decreasing (m : ℕ) :
    {n | ∀ i ∈ Finset.range m, primeGap (n + i) > primeGap (n + i + 1)}.Infinite := by
-- EDITABLE START
  have h : {n | ∀ i ∈ Finset.range m, primeGap (n + i) > primeGap (n + i + 1)} = Set.univ := by
    ext n
    simp only [Set.mem_setOf_eq, Set.mem_univ, iff_true, primeGap]
    intro i _
    push_cast
    linarith
  rw [h]
  exact Set.infinite_univ
-- EDITABLE END

end Erdos6
