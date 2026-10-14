import FormalConjectures.Util.ProblemImports

namespace SumOfThreeCubes

variable {R : Type*} [Ring R]

def IsSumOfThreeCubes (n : R) : Prop :=
  ∃ x y z : R, n = x^3 + y^3 + z^3

theorem isSumOfThreeCubes_2 : IsSumOfThreeCubes (2 : ℤ) :=
  ⟨1, 1, 0, by norm_num⟩

theorem isSumOfThreeCubes_33 : IsSumOfThreeCubes (33 : ℤ) :=
  ⟨8866128975287528, -8778405442862239, -2736111468807040, by norm_num⟩

theorem isSumOfThreeCubes_42 : IsSumOfThreeCubes (42 : ℤ) :=
  ⟨-80538738812075974, 80435758145817515, 12602123297335631, by norm_num⟩

theorem mod_9_of_isSumOfThreeCubes (n : ℤ) (hn : IsSumOfThreeCubes n) :
    ¬(n ≡ 4 [ZMOD 9] ∨ n ≡ 5 [ZMOD 9]) := by
  rw [show (9 : ℤ) = (9 : ℕ) from rfl, ← ZMod.intCast_eq_intCast_iff _ _ 9,
    ← ZMod.intCast_eq_intCast_iff _ _ 9]
  obtain ⟨x, y, z, hn⟩ := hn
  replace hn := congrArg (fun x : ℤ ↦ (x : ZMod 9)) hn
  simp only [Int.cast_add, Int.cast_pow] at hn
  generalize (n : ZMod 9) = n at *
  generalize (x : ZMod 9) = x at *
  generalize (y : ZMod 9) = y at *
  generalize (z : ZMod 9) = z at *
  decide +revert

-- EDITABLE START
-- EDITABLE END

theorem isSumOfThreeCubes_iff_mod_9 :
    answer(default) ↔ ∀ n : ℤ, IsSumOfThreeCubes n ↔ ¬(n ≡ 4 [ZMOD 9] ∨ n ≡ 5 [ZMOD 9]) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end SumOfThreeCubes
