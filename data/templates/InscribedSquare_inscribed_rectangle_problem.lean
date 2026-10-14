import FormalConjectures.Util.ProblemImports

open Topology ContDiff Manifold
open scoped EuclideanGeometry

namespace InscribedSquare

structure IsRectangle (a b c d : ℝ²) (ratio : ℝ) : Prop where
  diagonal_midpoints_eq : a + c = b + d
  diagonal_lengths_eq : dist a c = dist b d
  a_ne_b : a ≠ b
  b_ne_c : b ≠ c
  has_ratio : (dist a b) / (dist b c) = ratio

-- EDITABLE START
-- EDITABLE END

theorem inscribed_rectangle_problem :
    answer(default) ↔ ∀ (γ : Circle → ℝ²) (hγ : IsEmbedding γ) (r : ℝ) (hr : r > 0),
      ∃ t₁ t₂ t₃ t₄, IsRectangle (γ t₁) (γ t₂) (γ t₃) (γ t₄) r := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end InscribedSquare
