import FormalConjectures.Util.ProblemImports

open Asymptotics Filter
open scoped Pointwise

namespace Green32

def HasGap {p : ℕ} (A : Finset (ZMod p)) (L : ℕ) : Prop :=
  ∃ x : ZMod p, ∀ (i : ℕ), i < L → x + (i : ZMod p) ∉ A

theorem hasGap_zero {p : ℕ} (A : Finset (ZMod p)) :
    HasGap A 0 := by
  exact ⟨0, fun _ h => absurd h (by omega)⟩

theorem hasGap_empty {p : ℕ} (L : ℕ) :
    HasGap (∅ : Finset (ZMod p)) L := by
  exact ⟨0, fun _ _ => by simp⟩

theorem not_hasGap_univ {p : ℕ} [NeZero p] :
    ¬ HasGap (Finset.univ : Finset (ZMod p)) 1 := by
  rintro ⟨x, hx⟩
  have := hx 0 (by omega)
  simp at this

theorem hasGap_concrete :
    HasGap ({(0 : ZMod 5)} : Finset (ZMod 5)) 4 := by
  refine ⟨1, fun i hi => ?_⟩
  interval_cases i <;> decide

def HasLargeGapDilate (ω : ℕ → ℝ) : Prop :=
  ∀ᶠ p in atTop, p.Prime →
    100 < ω p ∧ ω p < p ∧
    ∀ A : Finset (ZMod p), A.card = ⌊ω p⌋₊ →
    ∃ c : (ZMod p)ˣ, HasGap (c • A) ⌊100 * (p : ℝ) / ω p⌋₊

def HasCosetHole {n : ℕ} (A : Finset (Fin n → ZMod 2)) (L : ℕ) : Prop :=
  ∃ W : Submodule (ZMod 2) (Fin n → ZMod 2), ∃ v : Fin n → ZMod 2,
    L ≤ Nat.card W ∧ ∀ w : W, v + (w : Fin n → ZMod 2) ∉ A

-- EDITABLE START
opaque genuineHelper (n : ℕ) :
    HasCosetHole (∅ : Finset (Fin n → ZMod 2)) 0 := by
  exact ⟨⊥, 0, Nat.zero_le _, fun w => by simp⟩
-- EDITABLE END

theorem hasCosetHole_empty (n : ℕ) :
    HasCosetHole (∅ : Finset (Fin n → ZMod 2)) 0 := by
-- EDITABLE START
  exact genuineHelper n
-- EDITABLE END

end Green32
