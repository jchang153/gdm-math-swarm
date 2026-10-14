import FormalConjectures.Util.ProblemImports

open scoped Real Complex
open IntermediateField

namespace Schanuel

-- EDITABLE START
-- EDITABLE END

theorem schanuel_conjecture (n : ℕ) (z : Fin n → ℂ) (h : LinearIndependent ℚ z) :
    let hinj := algebraMap ℚ (adjoin ℚ (Set.range z ∪ Set.range (cexp ∘ z))) |>.injective
    n ≤ Algebra.trdeg ℚ (adjoin ℚ (Set.range z ∪ Set.range (cexp ∘ z))) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Schanuel
