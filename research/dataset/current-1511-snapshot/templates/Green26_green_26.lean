import FormalConjectures.Util.ProblemImports

open Set
open scoped Pointwise

namespace Green26

abbrev 𝔽 (p n : ℕ) [Fact p.Prime] := Fin n → ZMod p

abbrev 𝔽₃ (n : ℕ) := 𝔽 3 n

def StandardCube {p : ℕ} [Fact p.Prime] (n : ℕ) : Set (𝔽 p n) :=
  {x | ∀ i, x i = 0 ∨ x i = 1}

def IsCube {p n : ℕ} [Fact p.Prime] (A : Set (𝔽 p n)) : Prop :=
  ∃ φ : 𝔽 p n ≃ₗ[ZMod p] 𝔽 p n, A = φ '' StandardCube n

-- EDITABLE START
-- EDITABLE END

theorem green_26 :
    ∀ n : ℕ,
      ∀ A : Fin 100 → Set (𝔽₃ n), (∀ i, IsCube (A i)) →
      ∑ i, A i = univ := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Green26
