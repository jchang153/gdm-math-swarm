import FormalConjectures.Util.ProblemImports

open Combinatorics Filter
open scoped Topology

namespace Erdos812

local notation "R" => hypergraphRamsey 2

-- EDITABLE START
-- EDITABLE END

theorem erdos_812.parts.i :
    answer(default) ↔ ∃ c > 0, ∀ᶠ n in atTop, (R (n + 1) : ℝ) / (R n : ℝ) ≥ 1 + c := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos812
