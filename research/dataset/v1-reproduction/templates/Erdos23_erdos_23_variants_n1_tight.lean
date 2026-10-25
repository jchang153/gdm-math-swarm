import FormalConjectures.Util.ProblemImports

open SimpleGraph BigOperators Classical

namespace Erdos23

-- EDITABLE START
-- EDITABLE END

theorem erdos_23.variants.n1_tight :
    ∃ (G : SimpleGraph (Fin 5)), G.CliqueFree 3 ∧ ∀ (H : SimpleGraph (Fin 5)),
        H ≤ G → H.IsBipartite → 1 ≤ (G.edgeFinset \ H.edgeFinset).card := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos23
