import FormalConjectures.Util.ProblemImports

open Nat Filter Finset
open scoped ArithmeticFunction.omega

namespace Erdos891

-- EDITABLE START
-- EDITABLE END

theorem erdos_891 :
    answer(default) ↔
      ∀ k ≥ 2, ∀ᶠ n in atTop,
      ∃ m ∈ Ico n (n + ∏ i ∈ range k, i.nth Nat.Prime), k < ω m := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos891
