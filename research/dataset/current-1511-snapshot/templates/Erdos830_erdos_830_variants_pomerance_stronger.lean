import FormalConjectures.Util.ProblemImports

open scoped ArithmeticFunction.sigma
open Classical Filter Real

namespace Erdos830

noncomputable abbrev A (x : ℝ) : ℝ :=
  Finset.card <| (Finset.Icc 1 ⌊x⌋₊ ×ˢ Finset.Icc 1 ⌊x⌋₊).filter fun (a, b) ↦
    a ≤ b ∧ IsAmicable a b

-- EDITABLE START
-- EDITABLE END

theorem erdos_830.variants.pomerance_stronger :
    ∃ o : ℝ → ℝ, o =o[atTop] (1 : ℝ → ℝ) ∧
    ∀ᶠ x in atTop, A x ≤ x * rexp (- (1/ 2 + o x) * √(x.log * x.log.log)) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos830
