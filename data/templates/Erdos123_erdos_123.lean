import FormalConjectures.Util.ProblemImports

open Filter
open Submonoid
open scoped Pointwise

namespace Erdos123

def IsDComplete (A : Set ℕ) : Prop :=
  ∀ᶠ n in atTop, ∃ s : Finset ℕ,

    (s : Set ℕ) ⊆ A ∧

    IsAntichain (· ∣ ·) (s : Set ℕ) ∧

    s.sum id = n

def IsSnug (ε : ℝ) (A : Finset ℕ) : Prop :=
  ∃ hA : A.Nonempty, ∀ a ∈ A, a < (1 + ε) * A.min' hA

def PairwiseCoprime (a b c : ℕ) : Prop := Pairwise (Nat.Coprime.onFun ![a, b, c])

-- EDITABLE START
-- EDITABLE END

theorem erdos_123 : answer(default) ↔ ∀ a > 1, ∀ b > 1, ∀ c > 1, PairwiseCoprime a b c →
    IsDComplete (↑(powers a) * ↑(powers b) * ↑(powers c)) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos123
