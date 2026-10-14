import FormalConjectures.Util.ProblemImports

namespace Erdos567

open SimpleGraph
open scoped Finset

def Q3 : SimpleGraph (Fin 3 → Bool) where
  Adj u v := #{i | u i ≠ v i} = 1
  symm _ _ := by simp [eq_comm]
  loopless _ := by simp

def K33 : SimpleGraph (Fin 3 ⊕ Fin 3) := completeBipartiteGraph (Fin 3) (Fin 3)

def H5 : SimpleGraph (Fin 5) :=
  .cycleGraph 5 ⊔ .edge 0 2 ⊔ .edge 1 3

-- EDITABLE START
-- EDITABLE END

theorem erdos_567.parts.ii : answer(default) ↔ IsRamseySizeLinear K33 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos567
