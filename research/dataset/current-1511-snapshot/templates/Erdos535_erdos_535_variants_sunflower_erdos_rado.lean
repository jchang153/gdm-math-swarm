import FormalConjectures.Util.ProblemImports

open ArithmeticFunction
open Filter Real
open scoped omega Omega

namespace Erdos535

def NoConstantPairwiseGcdCoprimeSubsets (r : ℕ) (A : Finset ℕ) : Prop :=
  ∀ S ⊆ A, S.card = r →
    ¬ (∃ d, 0 < d ∧ (S : Set ℕ).Pairwise (fun a b => Nat.gcd a b = d) ∧
      ∀ a ∈ S, ∃ b, a = d * b ∧ Nat.gcd b d = 1)

def AllBigOmega (k : ℕ) (A : Finset ℕ) : Prop :=
  ∀ a ∈ A, 1 ≤ a ∧ Ω a = k

noncomputable def f (r N : ℕ) : ℕ :=
  sSup {k : ℕ | ∃ A : Finset ℕ, A ⊆ Finset.Icc 1 N ∧
    (∀ S ⊆ A, S.card = r →
      ¬ (∃ d, (S : Set ℕ).Pairwise fun a b => Nat.gcd a b = d)) ∧
    A.card = k}

-- EDITABLE START
-- EDITABLE END

theorem erdos_535.variants.sunflower_erdos_rado {r : ℕ} (hr : 3 ≤ r) :
    ∃ c_r > (0 : ℝ),
      ∀ k : ℕ, ∀ A : Finset ℕ,
        AllBigOmega k A →
        NoConstantPairwiseGcdCoprimeSubsets r A →
        (A.card : ℝ) ≤ c_r ^ k * (Nat.factorial k) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos535
