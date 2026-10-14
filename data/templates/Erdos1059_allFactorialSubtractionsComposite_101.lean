import FormalConjectures.Util.ProblemImports

namespace Erdos1059

def IsFactorial (d : ℕ) : Prop :=
  d ∈ Set.range Nat.factorial

def factorialsLessThanN (n : ℕ) : Set ℕ :=
  { d | d < n ∧ IsFactorial d }

def AllFactorialSubtractionsComposite (n : ℕ) : Prop :=
  ∀d ∈ factorialsLessThanN n, (n - d).Composite

abbrev DecidableIsFactorial (d : ℕ) : Prop :=
  ((Finset.Icc 0 d).filter (λ k => Nat.factorial k = d)).Nonempty

def decidableFactorialsLessThanN (n : ℕ) : Finset ℕ :=
  (Finset.range n).filter DecidableIsFactorial

def DecidableAllFactorialSubtractionsComposite (n : ℕ) : Prop :=
  ∀ d ∈ decidableFactorialsLessThanN n, (n - d).Composite

lemma isFactorial_equivalent (d : ℕ) :
  IsFactorial d ↔ DecidableIsFactorial d := by
  unfold IsFactorial DecidableIsFactorial
  simp
  constructor
  · rintro ⟨k, hk⟩
    use k
    rw [Finset.mem_filter]
    constructor
    · have hk : k <= d := by
        rw [← hk]
        apply Nat.self_le_factorial
      rw [Finset.mem_Icc]
      exact ⟨Nat.zero_le k, hk⟩
    · exact hk
  · rintro ⟨k, hk⟩
    use k
    rw [Finset.mem_filter] at hk
    exact hk.2

lemma factorialsLessThanN_equivalent (n : ℕ) :
  factorialsLessThanN n = ↑(decidableFactorialsLessThanN n) := by
  ext d
  unfold factorialsLessThanN decidableFactorialsLessThanN
  simp
  exact λ _ => isFactorial_equivalent d

lemma allFactorialSubtractionsComposite_equivalent (d : ℕ) :
    DecidableAllFactorialSubtractionsComposite d ↔ AllFactorialSubtractionsComposite d := by
  unfold AllFactorialSubtractionsComposite DecidableAllFactorialSubtractionsComposite
  rw [factorialsLessThanN_equivalent d]
  simp

-- EDITABLE START
-- EDITABLE END

theorem allFactorialSubtractionsComposite_101 : AllFactorialSubtractionsComposite 101 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos1059
