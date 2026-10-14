import FormalConjectures.Util.ProblemImports
open scoped Topology
open Filter

namespace Erdos36

def Overlap (A B : Finset ℤ) (k : ℤ) : ℕ := ((A.product B).filter <| fun (a, b) => a - b = k).card

noncomputable def MaxOverlap (A B : Finset ℤ) : ℕ := iSup <| Overlap A B

noncomputable def M (n : ℕ) : ℕ :=
  sInf {MaxOverlap A B | (A : Finset ℤ) (B : Finset ℤ)
    (_disjoint : Disjoint A B)
    (_union : A ∪ B = Finset.Icc (1 : ℤ) (2 * n))
    (_same_card : A.card = B.card)}

noncomputable def MinOverlapQuotient (N : ℕ) := (M N : ℝ) / N

-- EDITABLE START
-- EDITABLE END

theorem minimum_overlap.variants.lower.white_2022 : 0.379005 < atTop.liminf MinOverlapQuotient := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos36
