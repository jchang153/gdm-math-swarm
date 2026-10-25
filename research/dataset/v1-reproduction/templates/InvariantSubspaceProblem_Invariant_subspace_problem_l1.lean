import FormalConjectures.Util.ProblemImports

namespace InvariantSubspaceProblem

variable {H : Type*} [NormedAddCommGroup H]

structure ClosedInvariantSubspace [Module ℂ H] (T : H →L[ℂ] H) where
  toSubspace : Submodule ℂ H
  ne_bot : toSubspace ≠ ⊥
  ne_top : toSubspace ≠ ⊤
  is_closed : IsClosed (toSubspace : Set H)
  is_fixed : toSubspace.map T.toLinearMap ≤ toSubspace

lemma TopologicalSpace.nontrivial_of_not_separableSpace {H : Type*} [TopologicalSpace H]
    (h : ¬ TopologicalSpace.SeparableSpace H) : Nontrivial H := by
  rw [← not_subsingleton_iff_nontrivial]
  contrapose! h
  infer_instance

-- EDITABLE START
-- EDITABLE END

theorem Invariant_subspace_problem_l1 :
    ∃ (T : (lp (fun (_ : ℕ) => ℂ) 1) →L[ℂ] (lp (fun (_ : ℕ) => ℂ) 1)),
    IsEmpty (ClosedInvariantSubspace T) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end InvariantSubspaceProblem
