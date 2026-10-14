import FormalConjectures.Util.ProblemImports

namespace DedekindNumber

open Finset

instance piFinBoolDecidableLE {n : ℕ} :
    DecidableRel (fun (a b : Fin n → Bool) => a ≤ b) :=
  fun a b => show Decidable (a ≤ b) from by
    rw [Pi.le_def]
    exact Fintype.decidableForallFintype

def M (n : ℕ) : ℕ :=
  Fintype.card {f : (Fin n → Bool) → Bool // Monotone f}

def IsSperner {n : ℕ} (A : Finset (Finset (Fin n))) : Prop :=
  IsAntichain (fun s t => s ⊆ t) (A : Set (Finset (Fin n)))

instance isSpernerDecidable {n : ℕ} :
    DecidablePred (fun A : Finset (Finset (Fin n)) => IsSperner A) :=
  fun A => by
    unfold IsSperner IsAntichain Set.Pairwise
    simp only [Finset.mem_coe, Pi.compl_apply, compl_iff_not]
    exact inferInstance

def M' (n : ℕ) : ℕ :=
  Fintype.card {A : Finset (Finset (Fin n)) // IsSperner A}

-- EDITABLE START
-- EDITABLE END

theorem M'_three : M' 3 = 20 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end DedekindNumber
