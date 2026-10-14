import FormalConjectures.Util.ProblemImports

/-! Researcher-only synthetic answer-based template with a numeral answer. -/

namespace SyntheticAnswer

-- EDITABLE START
-- EDITABLE END

theorem least_gt_three : IsLeast {n : ℕ | 3 < n} answer(4) := by
-- EDITABLE START
  exact ⟨by norm_num, fun n hn => hn⟩
-- EDITABLE END

end SyntheticAnswer
