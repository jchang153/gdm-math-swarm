import FormalConjectures.Util.ProblemImports

namespace Erdos10

abbrev sumPrimeAndTwoPows (k : ℕ) : Set ℕ :=
  { p + (pows.map (2 ^ ·)).sum | (p : ℕ) (pows : Multiset ℕ) (_ : p.Prime)
    (_ : pows.card ≤ k)}

-- EDITABLE START
-- EDITABLE END

theorem erdos_10.variants.granville_soundararajan_odd :
    {n : ℕ | Odd n ∧ 1 < n} ⊆ sumPrimeAndTwoPows 3 ∧
      {n : ℕ | Even n ∧ n ≠ 0} ⊆ sumPrimeAndTwoPows 4 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos10
