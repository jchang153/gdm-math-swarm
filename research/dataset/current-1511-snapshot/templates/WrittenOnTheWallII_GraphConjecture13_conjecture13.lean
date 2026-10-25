import FormalConjectures.Util.ProblemImports

namespace WrittenOnTheWallII.GraphConjecture13

open Classical SimpleGraph

variable {α : Type*} [Fintype α] [DecidableEq α] [Nontrivial α]

-- EDITABLE START
-- EDITABLE END

theorem conjecture13 (G : SimpleGraph α) (h : G.Connected) :
    letI maxL := (Finset.univ.image (fun v => indepNeighborsCard G v)).max' (by simp)
    (G.diam : ℝ) + (maxL : ℝ) - 1 ≤ b G := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end WrittenOnTheWallII.GraphConjecture13
