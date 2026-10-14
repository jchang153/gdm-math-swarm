import FormalConjectures.Util.ProblemImports

open Filter Real
open scoped EuclideanGeometry Topology

namespace Erdos1085
variable {d : ℕ}

noncomputable def f (d n : ℕ) : ℕ := ⨆ (s : Finset (ℝ^ d)) (_ : s.card = n), unitDistNum s

-- EDITABLE START
-- EDITABLE END

theorem erdos_1085.variants.upper_d3 : answer(default) ↔
    (fun n ↦ (f 3 n : ℝ)) =O[atTop] (fun n : ℕ ↦ (n : ℝ) ^ (4/3 : ℝ) * log (log n)) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos1085
