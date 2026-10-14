import FormalConjectures.Util.ProblemImports

open Polynomial

namespace Sendov

open Sendov

def Polynomial.IsSendov (f : ℂ[X]) : Prop :=
  2 ≤ f.natDegree ∧ (f.rootSet ℂ ⊆ Metric.closedBall 0 1)

def Nat.SatisfiesSendovConjecture (n : ℕ) : Prop :=
  ∀ (f : ℂ[X]), f.IsSendov → f.natDegree = n →
    ∀ z, z ∈ f.rootSet ℂ → Metric.infDist z (f.derivative.rootSet ℂ) ≤ 1

-- EDITABLE START
-- EDITABLE END

theorem sendov_conjecture (n : ℕ) (hn : 2 ≤ n) : n.SatisfiesSendovConjecture := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Sendov
