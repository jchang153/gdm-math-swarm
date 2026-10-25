import FormalConjectures.Util.ProblemImports

open EuclideanGeometry

universe u

namespace SquarePacking

def Square (side : ℝ) : Set ℝ² :=
  {p : ℝ² | 0 < p 0 ∧ p 0 < side ∧ 0 < p 1 ∧ p 1 < side}

def UnitSquare : Set ℝ² := Square 1

def Circle (r : ℝ) : Set ℝ² :=
  {p : ℝ² | p 0 ^ 2 + p 1 ^ 2 < r ^ 2}

def UnitCircle : Set ℝ² := Circle 1

structure Packing (n : ℕ) (s : Set ℝ²) (S : Set ℝ²) where

  embeddings : Fin n → (ℝ² ≃ᵢ ℝ²)

  disjoint : Pairwise fun i j => Disjoint (embeddings i '' s) (embeddings j '' s)

  inside : ∀ i : Fin n, embeddings i '' s ⊆ S

-- EDITABLE START
-- EDITABLE END

theorem least_three_square_packing_in_circle :
    IsLeast {r : ℝ | Nonempty (Packing 3 UnitSquare (Circle r))} answer(default) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end SquarePacking
