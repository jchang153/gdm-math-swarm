import FormalConjectures.Util.ProblemImports

namespace Erdos6

-- EDITABLE START
-- Synthetic exploit fixture: declare the target name with a trivial statement,
-- then open a block comment that the proof region closes, so the fixed
-- statement between the regions is never elaborated.
theorem erdos_6.variants.decreasing (_m : ℕ) : True := trivial
/-
-- EDITABLE END

theorem erdos_6.variants.decreasing (m : ℕ) :
    {n | ∀ i ∈ Finset.range m, primeGap (n + i) > primeGap (n + i + 1)}.Infinite := by
-- EDITABLE START
-/
-- EDITABLE END

end Erdos6
