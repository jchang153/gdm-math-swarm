import FormalConjectures.Util.ProblemImports

open Filter
open scoped Asymptotics

namespace Erdos323

noncomputable def f (k m x : ℕ) : ℕ :=
  { n : ℕ | n ≤ x ∧ ∃ (v : Fin m → ℕ), n = ∑ i, v i ^ k }.ncard

-- EDITABLE START
-- EDITABLE END

theorem erdos_323.variants.k_eq_2 :
    ∃ c > 0, (fun (x : ℕ) ↦ (f 2 2 x : ℝ)) ~[atTop]
      (fun (x : ℕ) ↦ c * (x : ℝ) / Real.sqrt (Real.log (x : ℝ))) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos323
