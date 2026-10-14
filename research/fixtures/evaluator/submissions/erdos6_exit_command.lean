import FormalConjectures.Util.ProblemImports

namespace Erdos6

-- EDITABLE START
-- Synthetic exploit fixture: stop processing before the fixed theorem, so the
-- file compiles while the target theorem is never declared.
#exit
-- EDITABLE END

theorem erdos_6.variants.decreasing (m : ℕ) :
    {n | ∀ i ∈ Finset.range m, primeGap (n + i) > primeGap (n + i + 1)}.Infinite := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos6
