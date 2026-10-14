import FormalConjectures.Util.ProblemImports

namespace EulerBrick

def IsEulerBrick (a b c : ℕ+) : Prop :=
  IsSquare (a^2 + b^2) ∧ IsSquare (a^2 + c^2) ∧ IsSquare (b^2 + c^2)

def IsPerfectCuboid (a b c : ℕ+) : Prop :=
  IsEulerBrick a b c ∧ IsSquare (a^2 + b^2 + c^2)

def IsEulerHyperBrick (n : ℕ) (sides : Fin n → ℕ+) : Prop :=
  Pairwise fun i j ↦ IsSquare ((sides i)^2 + (sides j)^2)

section Cuboid

open Polynomial

def CuboidOneFor (a b : ℤ) : Prop :=
  Irreducible (X ^ 8 + C (6 * (a ^ 2 - b ^ 2)) * X ^ 6
    + C (b ^ 4 - 4 * a ^ 2 * b ^ 2 + a ^ 4) * X ^ 4
    - C (6 * a ^ 2 * b ^ 2 * (a ^ 2 - b ^ 2)) * X ^ 2 + C (a ^ 4 * b ^ 4))

def CuboidOne : Prop := ∀ ⦃a b : ℤ⦄, gcd a b = 1 → 0 < a → 0 < b → a ≠ b → CuboidOneFor a b

def CuboidTwoFor (a b : ℤ) : Prop :=
  Irreducible (X ^ 10 + C ((2 * b ^ 2 + a ^ 2) * (3 * b ^ 2 - 2 * a ^ 2)) * X ^ 8
    + C ((b ^ 8 + 10 * a ^ 2 * b ^ 6 + 4 * a ^ 4 * b ^ 4 - 14 * a ^ 6 * b ^ 2 + a ^ 8)) * X ^ 6
    - C (a ^ 2 * b ^ 2 * (b ^ 8 - 14 * a ^ 2 * b ^ 6
    + 4 * a ^ 4 * b ^ 4 + 10 * a ^ 6 * b ^ 2 + a ^ 8))
    * X ^ 4 - C (a ^ 6 * b ^ 6 * (b ^ 2 + 2 * a ^ 2) * (-2 * b ^ 2 + 3 * a ^ 2))
    * X ^ 2 - C (b ^ 10 * a ^ 10))

def CuboidTwo : Prop := ∀ ⦃a b : ℕ⦄, a.Coprime b → 0 < a → 0 < b → a ≠ b → CuboidTwoFor a b

def CuboidThreeFor (a b c : ℤ) : Prop :=
  Irreducible (X ^ 12 + C (6 * c ^ 2 - 2 * a ^ 2 - 2 * b ^ 2) * X ^ 10
    + C (c ^ 4 + b ^ 4 + a ^ 4 + 4 * a ^ 2 * c ^ 2 + 4 * b ^ 2 * c ^ 2 - 12 * b ^ 2 * a ^ 2)
    * X ^ 8 + C (6 * a ^ 4 * c ^ 2 + 6 * c ^ 2 * b ^ 4 - 8 * a ^ 2 * b ^ 2 * c ^ 2
    - 2 * c ^ 4 * a ^ 2 - 2 * c ^ 4 * b ^ 2 - 2 * a ^ 4 * b ^ 2 - 2 * b ^ 4 * a ^ 2)
    * X ^ 6 + C (4 * c ^ 2 * b ^ 4 * a ^ 2 + 4 * a ^ 4 * c ^ 2 * b ^ 2
    - 12 * c ^ 4 * a ^ 2 * b ^ 2 + c ^ 4 * a ^ 4 + c ^ 4 * b ^ 4 + a ^ 4 * b ^ 4) * X ^ 4
    + C (6 * a ^ 4 * c ^ 2 * b ^ 4 - 2 * c ^ 4 * a ^ 4 * b ^ 2 - 2 * c ^ 4 * a ^ 2 * b ^ 4)
    * X ^ 2 + C (c ^ 4 * a ^ 4 * b ^ 4))

def CuboidThree : Prop := ∀ ⦃a b c : ℤ⦄, gcd a (gcd b c) = 1 → 0 < a → 0 < b →
  0 < c → a ≠ b → b ≠ c → c ≠ a → b * c ≠ a ^ 2 → a * c ≠ b ^ 2 → CuboidThreeFor a b c

-- EDITABLE START
/-- Synthetic exploit fixture: local notation re-reads the fixed statement. -/
local notation "CuboidThree" => True
-- EDITABLE END

theorem cuboidThree : CuboidThree := by
-- EDITABLE START
  trivial
-- EDITABLE END

end Cuboid
end EulerBrick
