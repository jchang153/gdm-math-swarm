import FormalConjectures.Util.ProblemImports

/-! Researcher-only synthetic answer-based template (evaluator validation).
Mirrors the frozen answer-template layout; never exported to agents. -/

namespace SyntheticAnswer

-- EDITABLE START
-- EDITABLE END

theorem all_fermat_odd : answer(False) ↔ ∀ n : ℕ, Odd n.fermatNumber := by
-- EDITABLE START
  constructor <;> intro h
  · exact absurd h id
  · exact absurd (h 0) (by decide)
-- EDITABLE END

end SyntheticAnswer
