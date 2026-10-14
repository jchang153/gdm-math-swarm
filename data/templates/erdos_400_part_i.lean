import FormalConjectures.Util.ProblemImports

open Nat Filter Finset
open scoped Asymptotics Topology

namespace Erdos400

noncomputable def g (k n : ℕ) : ℕ :=
  sSup { ((∑ i, a i) - n) | (a : Fin k → ℕ) (_ : (∏ i, (a i) !) ∣ n !) }

-- EDITABLE START
-- EDITABLE END

theorem erdos_400.parts.i :
    answer(default) ↔ ∀ᵉ (k ≥ 2), ∃ c : ℝ,
      (fun x : ℕ ↦ (∑ n ∈ Icc 1 x, (g k n : ℝ))) ~[atTop]
      (fun x : ℕ ↦ c * x * Real.log x) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos400
