import FormalConjectures.Util.ProblemImports

namespace RiemannHypothesis

end RiemannHypothesis

namespace GRH

def trivialZeros {q : ℕ} (χ : DirichletCharacter ℂ q) : Set ℤ :=
  if q = 1 then {-2 * (n + 1) | (n : ℕ) } else
    if χ.Odd then { -2 * n - 1 | (n : ℕ) } else
      { - 2 * n | (n : ℕ) }

-- EDITABLE START
-- EDITABLE END

theorem implies_riemannHypothesis :
    (∀ (hχ : (1 : DirichletCharacter ℂ 1).IsPrimitive) (s : ℂ), (1 : DirichletCharacter ℂ 1).LFunction s = 0 → s ∉ Int.cast '' trivialZeros (1 : DirichletCharacter ℂ 1) → s.re = 1 / 2) ↔ RiemannHypothesis := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end GRH
