import FormalConjectures.Util.ProblemImports

open Filter Asymptotics Real

namespace Erdos18

noncomputable def practicalH (n : ℕ) : ℕ :=
  Finset.sup (Finset.Icc 1 n) fun m =>
    sInf {k | ∃ D : Finset ℕ, D ⊆ n.divisors ∧ D.card = k ∧ m ∈ subsetSums D}

theorem practicalH_one : practicalH 1 = 1 := by
  norm_num [subsetSums, practicalH]

theorem practicalH_two : practicalH 2 = 1 := by
  simp only [practicalH, (by decide : Finset.Icc 1 2 = ({1, 2} : Finset ℕ)),
    (by decide : Nat.divisors 2 = ({1, 2} : Finset ℕ)), Finset.sup_insert, Finset.sup_singleton]
  have h1 : sInf {k | ∃ D : Finset ℕ, D ⊆ {1, 2} ∧ D.card = k ∧ 1 ∈ subsetSums D} = 1 :=
    le_antisymm (Nat.sInf_le ⟨{1}, by simp, rfl, {1}, rfl.subset, by simp⟩)
      (le_csInf ⟨1, {1}, by simp, rfl, {1}, rfl.subset, by simp⟩ fun k ⟨D, _, hD, B, hB, hm⟩ =>
        hD ▸ Finset.one_le_card.mpr ((Finset.nonempty_iff_ne_empty.mpr fun h => by simp [h] at hm).mono hB))
  have h2 : sInf {k | ∃ D : Finset ℕ, D ⊆ {1, 2} ∧ D.card = k ∧ 2 ∈ subsetSums D} = 1 :=
    le_antisymm (Nat.sInf_le ⟨{2}, by simp, rfl, {2}, rfl.subset, by simp⟩)
      (le_csInf ⟨1, {2}, by simp, rfl, {2}, rfl.subset, by simp⟩ fun k ⟨D, _, hD, B, hB, hm⟩ =>
        hD ▸ Finset.one_le_card.mpr ((Finset.nonempty_iff_ne_empty.mpr fun h => by simp [h] at hm).mono hB))
  simp [h1, h2]

theorem practicalH_le_divisors (n : ℕ) (hn : Nat.IsPractical n) :
    practicalH n ≤ n.divisors.card := by
  simp only [practicalH, Finset.sup_le_iff, Finset.mem_Icc]
  exact fun m ⟨_, hm⟩ => Nat.sInf_le ⟨n.divisors, Finset.Subset.refl _, rfl, hn m hm⟩

theorem factorial_isPractical (n : ℕ) : Nat.IsPractical n.factorial := by
    induction n with
  | zero =>
    intro m hm; simp at hm; interval_cases m
    · exact ⟨∅, by simp, by simp⟩
    · exact ⟨{1}, by simp, by simp⟩
  | succ n ih =>
    intro m hm
    by_cases hle : m ≤ n.factorial
    · exact subsetSums_mono (by exact_mod_cast (Nat.divisors_subset_of_dvd
        (Nat.factorial_ne_zero _) (Nat.factorial_dvd_factorial n.le_succ))) (ih m hle)
    · push_neg at hle; rw [Nat.factorial_succ] at hm
      set q := m / (n + 1); set r := m % (n + 1)
      have h_div : m = (n + 1) * q + r := (Nat.div_add_mod m (n + 1)).symm
      have h_r_lt : r < n + 1 := Nat.mod_lt m (Nat.succ_pos n)
      obtain ⟨B, hB_sub, hB_sum⟩ := ih q (Nat.div_le_of_le_mul (by linarith))
      have hdvd : ∀ d ∈ B, d * (n + 1) ∈ (n + 1).factorial.divisors := fun d hd => by
        refine Nat.mem_divisors.mpr ⟨?_, Nat.factorial_ne_zero _⟩
        rw [mul_comm, Nat.factorial_succ]
        exact mul_dvd_mul_left _ (Nat.dvd_of_mem_divisors (by exact_mod_cast hB_sub hd))
      have hB'_sum : (B.image (· * (n + 1))).sum id = (n + 1) * q := by
        rw [Finset.sum_image (fun a _ b _ h => mul_right_cancel₀ (by omega) h)]
        simp [Finset.mul_sum, mul_comm, hB_sum]
      by_cases hr : r = 0
      · rw [show m = (B.image (· * (n + 1))).sum id from by rw [hB'_sum]; omega]
        exact ⟨_, fun x hx => by
          obtain ⟨d, hd, rfl⟩ := Finset.mem_image.mp hx; exact hdvd d hd, rfl⟩
      · have h_disj : Disjoint (B.image (· * (n + 1))) {r} := by
          rw [Finset.disjoint_singleton_right, Finset.mem_image]; rintro ⟨d, hd, hdr⟩
          have : 0 < d := Nat.pos_of_dvd_of_pos
            (Nat.dvd_of_mem_divisors (by exact_mod_cast hB_sub hd)) (Nat.factorial_pos n)
          have := le_mul_of_one_le_left (Nat.zero_le (n + 1)) this
          omega
        rw [show m = (B.image (· * (n + 1)) ∪ {r}).sum id from by
          rw [Finset.sum_union h_disj, Finset.sum_singleton, hB'_sum, id_eq]; exact h_div]
        exact ⟨_, fun x hx => by
          rcases Finset.mem_union.mp hx with h | h
          · obtain ⟨d, hd, rfl⟩ := Finset.mem_image.mp h; exact hdvd d hd
          · rw [Finset.mem_singleton.mp h]; exact Nat.mem_divisors.mpr
              ⟨(Nat.dvd_factorial (by omega) (by omega)).trans
                (Nat.factorial_dvd_factorial n.le_succ), Nat.factorial_ne_zero _⟩, rfl⟩

-- EDITABLE START
-- EDITABLE END

theorem erdos_18a : answer(default) ↔
    ∃ C : ℝ, 0 < C ∧ ∃ᶠ m in atTop, Nat.IsPractical m ∧
      (practicalH m : ℝ) < (log (log m)) ^ C := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos18
