import FormalConjectures.Util.ProblemImports

/-! Researcher-only synthetic answer-based template (evaluator validation).
Mirrors the frozen answer-template layout; never exported to agents. -/

namespace SyntheticAnswer

-- EDITABLE START
/-- Synthetic helper answer whose value is provably `True`. -/
def OddAnswer : Prop := 2 + 2 = 4
-- EDITABLE END

theorem all_fermat_odd : answer(OddAnswer) ↔ ∀ n : ℕ, Odd n.fermatNumber := by
-- EDITABLE START
  refine ⟨fun _ n => ?_, fun _ => (rfl : 2 + 2 = 4)⟩
  unfold Nat.fermatNumber
  exact (Nat.even_pow.mpr ⟨even_two, (pow_pos two_pos n).ne'⟩).add_one
-- EDITABLE END

end SyntheticAnswer
