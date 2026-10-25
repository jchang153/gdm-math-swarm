import FormalConjectures.Util.ProblemImports
open Finset SimpleGraph
open scoped Nat

namespace Erdos835

variable {n k : ℕ}

def Property (k : ℕ) : Prop :=
  let K := {s : Finset (Fin (2 * k)) // s.card = k}
  ∃ c : K → Fin (k + 1),
    ∀ A : Finset (Fin (2 * k)), A.card = k + 1 →
      (image c {s : K | s.val ⊂ A}) = (univ : Finset (Fin (k+1)))

-- EDITABLE START
-- EDITABLE END

theorem erdos_835 : (∃ k > 2, Property k) ↔ answer(default) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos835
