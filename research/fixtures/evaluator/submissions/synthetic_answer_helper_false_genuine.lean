import FormalConjectures.Util.ProblemImports

/-! Researcher-only synthetic answer-based template whose answer is False. -/

namespace SyntheticAnswer

-- EDITABLE START
/-- Synthetic helper answer whose value is provably `False`. -/
def FiveAnswer : Prop := 1 = 0
-- EDITABLE END

theorem two_add_two_five : answer(FiveAnswer) ↔ (2 : ℕ) + 2 = 5 := by
-- EDITABLE START
  constructor
  · intro h
    exact absurd (show FiveAnswer from h) (by unfold FiveAnswer; decide)
  · intro h
    exact absurd h (by decide)
-- EDITABLE END

end SyntheticAnswer
