import FormalConjectures.Util.ProblemImports

namespace MagicSquares

-- EDITABLE START
-- EDITABLE END

theorem exists_semi_magic_square_cubes :
    answer(default) ↔ ∃ m : Fin 3 → Fin 3 → ℕ, ∃ t : ℕ,
       m.Injective2 ∧
       (∀ i j, ∃ n : ℕ, 0 < n ∧ m i j = n ^ 3) ∧
       (∀ i, ∑ j, m i j = t) ∧
       (∀ j, ∑ i, m i j = t) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end MagicSquares
