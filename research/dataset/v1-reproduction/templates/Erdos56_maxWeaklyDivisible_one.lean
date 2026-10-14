import FormalConjectures.Util.ProblemImports

open scoped Finset

namespace Erdos56

def WeaklyDivisible (k : ℕ) (A : Finset ℕ) : Prop :=
    ∀ s ∈ A.powersetCard (k + 1), ¬ Set.Pairwise s Nat.Coprime

lemma weaklyDivisible_empty (k : ℕ): WeaklyDivisible k {} := by
  simp [WeaklyDivisible]

lemma weaklyDivisible_singleton {k : ℕ} (hk : k ≠ 0) (l : ℕ) : WeaklyDivisible k {l} := by
  simp [WeaklyDivisible, hk]

lemma not_weaklyDivisible_zero {A : _} (h : A.Nonempty) : ¬WeaklyDivisible 0 A := by
  simpa [WeaklyDivisible] using ⟨{_}, by simpa using h.choose_spec⟩

lemma empty_iff_weaklyDivisible_zero {A : _} : WeaklyDivisible 0 A ↔ A = ∅ :=
  ⟨fun h ↦ Finset.not_nonempty_iff_eq_empty.1 <| mt not_weaklyDivisible_zero (not_not.2 h),
    fun h ↦ h ▸ weaklyDivisible_empty _⟩

noncomputable def MaxWeaklyDivisible (N : ℕ) (k : ℕ) : ℕ :=
  sSup {#A | (A : Finset ℕ) (_ : A ⊆ Finset.Icc 1 N) (_ : WeaklyDivisible k A)}

theorem maxWeaklyDivisible_zero : ∀ k : ℕ, MaxWeaklyDivisible 0 k = 0 := by
  intro k
  simp [MaxWeaklyDivisible, Nat.sSup_def]

-- EDITABLE START
-- EDITABLE END

theorem maxWeaklyDivisible_one {k : ℕ} (hk : k ≠ 0) : MaxWeaklyDivisible 1 k = 1 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos56
