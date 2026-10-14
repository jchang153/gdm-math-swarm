import FormalConjectures.Util.ProblemImports

namespace Erdos6

-- EDITABLE START
#guard_msgs (drop warning) in
opaque hiddenHelper (m : ℕ) :
    {n | ∀ i ∈ Finset.range m, primeGap (n + i) > primeGap (n + i + 1)}.Infinite := by
  admit
-- EDITABLE END

theorem erdos_6.variants.decreasing (m : ℕ) :
    {n | ∀ i ∈ Finset.range m, primeGap (n + i) > primeGap (n + i + 1)}.Infinite := by
-- EDITABLE START
  exact hiddenHelper m
-- EDITABLE END

end Erdos6
