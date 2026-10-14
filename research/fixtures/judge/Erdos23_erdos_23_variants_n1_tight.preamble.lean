def gdmC5 : SimpleGraph (Fin 5) where
  Adj i j := (i.val + 1) % 5 = j.val ∨ (j.val + 1) % 5 = i.val
  symm _ _ h := h.symm
  loopless i h := by rcases h with h | h <;> omega
