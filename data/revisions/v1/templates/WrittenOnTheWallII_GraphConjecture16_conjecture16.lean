import FormalConjectures.Util.ProblemImports

namespace WrittenOnTheWallII.GraphConjecture16

open Classical SimpleGraph

variable {α : Type*} [Fintype α] [DecidableEq α] [Nontrivial α]

-- EDITABLE START
-- EDITABLE END

theorem conjecture16 (G : SimpleGraph α) (h : G.Connected) :
    letI maxL := (Finset.univ.image (fun v => indepNeighborsCard G v)).max' (by simp)
    letI r := G.radius.toNat
    2 * ((r : ℝ) - 1) + (maxL : ℝ) ≤ b G := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end WrittenOnTheWallII.GraphConjecture16
