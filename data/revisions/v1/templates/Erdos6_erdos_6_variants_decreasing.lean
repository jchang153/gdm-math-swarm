import FormalConjectures.Util.ProblemImports

namespace Erdos6

-- EDITABLE START
-- EDITABLE END

theorem erdos_6.variants.decreasing (m : ℕ) :
    {n | ∀ i ∈ Finset.range m, primeGap (n + i) > primeGap (n + i + 1)}.Infinite := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos6
