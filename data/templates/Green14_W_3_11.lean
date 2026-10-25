import FormalConjectures.Util.ProblemImports

open Filter Set Topology

namespace Green14

def mixedMonoAPGuaranteeSet (k r : ℕ) : Set ℕ :=
  { N | ∀ coloring : Icc 1 N → Fin 2,
    (∃ s : Finset (Icc 1 N), ({(s' : ℕ) | s' ∈ s}).IsAPOfLength k ∧ ∀ x ∈ s, coloring x = 0) ∨
    (∃ s : Finset (Icc 1 N), ({(s' : ℕ) | s' ∈ s}).IsAPOfLength r ∧ ∀ x ∈ s, coloring x = 1) }

noncomputable def W (k r : ℕ) : ℕ := sInf (mixedMonoAPGuaranteeSet k r)

-- EDITABLE START
-- EDITABLE END

theorem W_3_11 : W 3 11 = 114 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Green14
