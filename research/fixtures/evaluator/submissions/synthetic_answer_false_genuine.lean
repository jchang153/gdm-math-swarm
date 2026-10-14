import FormalConjectures.Util.ProblemImports

/-! Researcher-only synthetic answer-based template whose answer is False. -/

namespace SyntheticAnswer

-- EDITABLE START
-- EDITABLE END

theorem two_add_two_five : answer(False) ↔ (2 : ℕ) + 2 = 5 := by
-- EDITABLE START
  norm_num
-- EDITABLE END

end SyntheticAnswer
