  refine ⟨gdmC5, ?_, ?_⟩
  · intro t ht
    rw [SimpleGraph.is3Clique_iff] at ht
    obtain ⟨a, b, c, hab, hac, hbc, -⟩ := ht
    simp only [gdmC5] at hab hac hbc
    have := a.isLt
    have := b.isLt
    have := c.isLt
    omega
  · intro H hle hbip
    by_contra hlt
    have hcard : (gdmC5.edgeFinset \ H.edgeFinset).card = 0 := by omega
    have hGH : gdmC5 ≤ H := by
      intro a b hab
      by_contra hn
      have hmem : s(a, b) ∈ gdmC5.edgeFinset \ H.edgeFinset := by
        simp only [Finset.mem_sdiff, SimpleGraph.mem_edgeFinset, SimpleGraph.mem_edgeSet]
        exact ⟨hab, hn⟩
      rw [Finset.card_eq_zero] at hcard
      rw [hcard] at hmem
      simp at hmem
    obtain ⟨col⟩ := SimpleGraph.Colorable.mono_left hGH hbip
    have h01 := col.valid (show gdmC5.Adj 0 1 from Or.inl (by decide))
    have h12 := col.valid (show gdmC5.Adj 1 2 from Or.inl (by decide))
    have h23 := col.valid (show gdmC5.Adj 2 3 from Or.inl (by decide))
    have h34 := col.valid (show gdmC5.Adj 3 4 from Or.inl (by decide))
    have h40 := col.valid (show gdmC5.Adj 4 0 from Or.inl (by decide))
    generalize col 0 = x0 at *
    generalize col 1 = x1 at *
    generalize col 2 = x2 at *
    generalize col 3 = x3 at *
    generalize col 4 = x4 at *
    revert x0 x1 x2 x3 x4
    decide
