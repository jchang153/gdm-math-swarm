import FormalConjectures.Util.ProblemImports

/-! Researcher-only synthetic answer-based template (evaluator validation).
Mirrors the frozen answer-template layout; never exported to agents. -/

namespace SyntheticAnswer

-- EDITABLE START
-- EDITABLE END

theorem all_fermat_odd : answer(default) ↔ ∀ n : ℕ, Odd n.fermatNumber := by
-- EDITABLE START
  -- The placeholder is left unchanged: `True.intro` checks only if the Prop
  -- default unfolds to `True`.
  refine ⟨fun _ n => ?_, fun _ => True.intro⟩
  unfold Nat.fermatNumber
  exact (Nat.even_pow.mpr ⟨even_two, (pow_pos two_pos n).ne'⟩).add_one
-- EDITABLE END

end SyntheticAnswer
