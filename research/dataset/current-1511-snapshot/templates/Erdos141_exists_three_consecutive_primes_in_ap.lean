import FormalConjectures.Util.ProblemImports

namespace Erdos141

def Set.IsPrimeProgressionOfLength (s : Set ℕ) (l : ℕ∞) : Prop :=
    ∃ a, ENat.card s = l ∧ s = {(a + n).nth Nat.Prime | (n : ℕ) (_ : n < l)}

open Nat Erdos141

theorem first_three_odd_primes : ({3, 5, 7} : Set ℕ).IsPrimeProgressionOfLength 3 := by
  use 1
  constructor
  · aesop
  · norm_num [exists_lt_succ_right, or_assoc, eq_comm, Set.insert_def,
    show (2).nth Nat.Prime = 5 from nth_count prime_five,
    show (3).nth Nat.Prime = 7 from Nat.nth_count (by decide : (7).Prime)]

def Set.IsAPAndPrimeProgressionOfLength (s : Set ℕ) (l : ℕ) :=
   s.IsAPOfLength l ∧ s.IsPrimeProgressionOfLength l

-- EDITABLE START
-- EDITABLE END

theorem exists_three_consecutive_primes_in_ap : ∃ (s : Set ℕ), s.IsAPAndPrimeProgressionOfLength 3 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos141
