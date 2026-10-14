import FormalConjectures.Util.ProblemImports

namespace Fermat

-- EDITABLE START
def MyAnswer : Prop := ∀ n : ℕ, Squarefree n.fermatNumber
-- EDITABLE END

theorem all_fermat_squarefree : answer(MyAnswer) ↔ ∀ n : ℕ, Squarefree n.fermatNumber := by
-- EDITABLE START
  exact Iff.rfl
-- EDITABLE END

end Fermat
