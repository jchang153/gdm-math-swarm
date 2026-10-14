import FormalConjectures.Util.ProblemImports

namespace Erdos195

-- EDITABLE START
/-- Synthetic exploit fixture: a higher-priority order instance changes what
`≥` on `ℕ` means in the fixed statement below. -/
instance (priority := high) trivialLE : LE ℕ := ⟨fun _ _ => True⟩
-- EDITABLE END

theorem erdos_195.variants.leq_5_bound :
    5 ≥ sSup {k : ℕ | ∀ f : ℤ ≃ ℤ, HasMonotoneAP f k} := by
-- EDITABLE START
  exact True.intro
-- EDITABLE END

end Erdos195
