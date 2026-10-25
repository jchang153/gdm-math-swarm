import FormalConjectures.Util.ProblemImports

namespace Erdos680

open Real

-- EDITABLE START
-- EDITABLE END

theorem erdos_680.parts.ii : answer(default) ↔ ∀ ε > 0, ∃ C > 0,
    ¬ ∀ᶠ (n : ℕ) in Filter.atTop, ∃ k ≠ 0,
    Nat.minFac (n + k) > exp ((1 + ε) * √k) + C := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos680
