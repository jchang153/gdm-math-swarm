import FormalConjectures.Util.ProblemImports

namespace WrittenOnTheWallII.GraphConjecture1

open SimpleGraph

-- EDITABLE START
-- EDITABLE END

theorem conjecture1 {α : Type*} [Fintype α] [DecidableEq α] [Nontrivial α]
    (G : SimpleGraph α) [DecidableRel G.Adj] (h_conn : G.Connected) :
    n G + 1 - 2 * m G ≤ Ls G := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end WrittenOnTheWallII.GraphConjecture1
