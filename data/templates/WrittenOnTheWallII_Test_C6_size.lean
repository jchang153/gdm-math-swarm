import FormalConjectures.Util.ProblemImports

open SimpleGraph

namespace WrittenOnTheWallII.Test

open Classical

abbrev HouseGraph : SimpleGraph (Fin 5) :=
  SimpleGraph.fromEdgeSet {
    s(0, 1), s(1, 2), s(2, 3), s(3, 0),
    s(2, 4), s(3, 4)
  }

abbrev K4 : SimpleGraph (Fin 4) := completeGraph (Fin 4)

abbrev PetersenGraph : SimpleGraph (Fin 10) :=
  SimpleGraph.fromEdgeSet {

    s(0, 1), s(1, 2), s(2, 3), s(3, 4), s(4, 0),

    s(0, 5), s(1, 6), s(2, 7), s(3, 8), s(4, 9),

    s(5, 7), s(7, 9), s(9, 6), s(6, 8), s(8, 5)
  }

abbrev C6 : SimpleGraph (Fin 6) := cycleGraph 6

abbrev Star5 : SimpleGraph (Fin 1 ⊕ Fin 5) := completeBipartiteGraph (Fin 1) (Fin 5)

instance : DecidableRel Star5.Adj := by unfold Star5 completeBipartiteGraph; infer_instance

theorem house_order : n HouseGraph = 5 := by simp [n]

theorem K4_order : n K4 = 4 := by simp [n]

theorem K4_avg_deg : averageDegree K4 = 3 := by
  unfold averageDegree; simp [Fintype.card_fin]

theorem petersen_order : n PetersenGraph = 10 := by simp [n]

theorem C6_order : n C6 = 6 := by simp [n]

-- EDITABLE START
-- EDITABLE END

theorem C6_size : C6.edgeFinset.card = 6 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end WrittenOnTheWallII.Test
