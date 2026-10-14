import FormalConjectures.Util.ProblemImports

open Filter

open scoped Asymptotics Finset

namespace Erdos539

def IsCofactorLowerBound (n m : ℕ) : Prop := ∀ A : Finset ℕ, #A = n →
  m ≤ #((A ×ˢ A).image fun (a, b) ↦ a / a.gcd b)

noncomputable def cofactorThreshold (n : ℕ) : ℕ :=
  sSup {m | IsCofactorLowerBound n m}

-- EDITABLE START
-- EDITABLE END

theorem erdos_539.variants.sq_isBigO :
    (fun n : ℕ ↦ √n) =O[atTop] fun n ↦ (cofactorThreshold n : ℝ) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos539
