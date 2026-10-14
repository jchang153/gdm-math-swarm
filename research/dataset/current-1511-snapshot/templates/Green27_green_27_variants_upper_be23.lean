import FormalConjectures.Util.ProblemImports

open Asymptotics Filter

namespace Green27

noncomputable def m (p : ℕ) : ℝ :=
  (sInf { (A.card) | (A : Finset (ZMod p)) (_ : 2 ≤ A.card) (_ : HasNoUniqueRepresentation A) } : ℝ)

def primesAtTop : Filter ℕ := atTop ⊓ 𝓟 {p : ℕ | p.Prime}

noncomputable def lowerBest (p : ℕ) : ℝ :=
  (Real.sqrt (Real.log (Real.log (Real.log (p : ℝ)))) /
   Real.log (Real.log (Real.log (Real.log (p : ℝ))))) * Real.log (p : ℝ)

noncomputable def upperBest (p : ℕ) : ℝ := (Real.log (p : ℝ)) ^ 2

-- EDITABLE START
-- EDITABLE END

theorem green_27.variants.upper_be23 :
  m =O[primesAtTop] upperBest := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Green27
