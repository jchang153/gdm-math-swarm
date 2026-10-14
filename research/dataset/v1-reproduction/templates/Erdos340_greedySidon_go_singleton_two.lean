import FormalConjectures.Util.ProblemImports

open Filter Finset
open scoped Real Pointwise

namespace Erdos340

private def greedySidon.go (A : Finset ℕ) (hA : IsSidon (A : Set ℕ)) (m : ℕ) :
    {m' : ℕ // m' ≥ m ∧ m' ∉ A ∧ IsSidon (↑(A ∪ {m'}) : Set ℕ)} :=
  if h : A.Nonempty then
    haveI : ∃ m', m' ≥ m ∧ m' ∉ A ∧ IsSidon (↑(A ∪ {m'}) : Set ℕ) := by
      simpa [and_assoc] using hA.exists_insert_ge h m
    ⟨Nat.find this, Nat.find_spec this⟩
  else ⟨m, by simp_all [IsSidon]⟩

-- EDITABLE START
-- EDITABLE END

theorem greedySidon_go_singleton_two : (greedySidon.go {1} (by simp [IsSidon]) 2).val = 2 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos340
