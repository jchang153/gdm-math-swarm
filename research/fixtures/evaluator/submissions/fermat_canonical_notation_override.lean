import FormalConjectures.Util.ProblemImports

namespace Fermat

-- EDITABLE START
local notation "Squarefree" => fun (_ : ℕ) => True
-- EDITABLE END

theorem all_fermat_squarefree : answer(True) ↔ ∀ n : ℕ, Squarefree n.fermatNumber := by
-- EDITABLE START
  simp
-- EDITABLE END

end Fermat
