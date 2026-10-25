import FormalConjectures.Util.ProblemImports

namespace Erdos1092

open Classical
open SimpleGraph
open Finset
open Asymptotics
open Filter

noncomputable def f (r n : ℕ) : ℕ :=
  sSup {k : ℕ |
    ∀ G : SimpleGraph (Fin n),
      (∀ H : Subgraph G,
        ∃ E : Finset (Sym2 H.verts),
          E.card ≤ k ∧
          chromaticNumber (H.coe.deleteEdges E) ≤ (r + 1 : ℕ∞)) →
      chromaticNumber G ≤ (r + 1 : ℕ∞)}

-- EDITABLE START
-- EDITABLE END

theorem f_asymptotic_2 : answer(default) ↔
    (fun (n : ℕ) => (n : ℝ)) =o[atTop] (fun (n : ℕ) => (f 2 n : ℝ)) := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Erdos1092
