import FormalConjectures.Util.ProblemImports

open Filter Set Real
open scoped Topology

namespace Erdos697

noncomputable def δ (m : ℕ) (α : ℝ) : ℝ := (density_exists m α).choose

-- EDITABLE START
-- EDITABLE END

theorem erdos_697.parts.ii {α : ℝ} (hα : α < 1 / log (2 : ℝ)) : Tendsto (δ · α) atTop (𝓝 1) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos697
