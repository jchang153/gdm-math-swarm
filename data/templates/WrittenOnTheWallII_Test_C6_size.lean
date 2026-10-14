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

theorem house_indep : α(HouseGraph) = 2 := by
  rw [indep_num_eq_computable]; decide +native

theorem house_dom : dominationNumber HouseGraph = 2 := by
  rw [dom_num_eq_computable]; decide +native

theorem house_avg_dist : averageDistance HouseGraph = 7/5 := by
  rw [avg_dist_eq_computable, show computable_avg_dist HouseGraph = (7 / 5 : ℚ) from by decide +native]
  norm_num

theorem house_order : n HouseGraph = 5 := by simp [n]

theorem house_size : HouseGraph.edgeFinset.card = 6 := by
  decide +native

theorem house_szeged : szegedIndex HouseGraph = 24 := by
  rw [szeged_eq_computable]; decide +native

theorem house_wiener : wienerIndex HouseGraph = 14 := by
  rw [wiener_eq_computable]; decide +native

theorem house_min_deg : HouseGraph.minDegree = 2 := by
  decide +native

theorem house_max_deg : HouseGraph.maxDegree = 3 := by
  decide +native

theorem house_avg_deg : averageDegree HouseGraph = 12/5 := by
  unfold averageDegree; simp [Fintype.card_fin]; decide +native

theorem house_residue : residue HouseGraph = 0 := by
  unfold residue; decide +native

theorem house_annihilation : annihilationNumber HouseGraph = 3 := by
  decide +native

theorem K4_indep : α(K4) = 1 := by
  rw [indep_num_eq_computable]; decide +native

theorem K4_dom : dominationNumber K4 = 1 := by
  rw [dom_num_eq_computable]; decide +native

theorem K4_avg_dist : averageDistance K4 = 1 := by
  rw [avg_dist_eq_computable, show computable_avg_dist K4 = (1 : ℚ) from by decide +native]
  norm_num

theorem K4_order : n K4 = 4 := by simp [n]

theorem K4_size : K4.edgeFinset.card = 6 := by
  decide +native

theorem K4_szeged : szegedIndex K4 = 6 := by
  rw [szeged_eq_computable]; decide +native

theorem K4_wiener : wienerIndex K4 = 6 := by
  rw [wiener_eq_computable]; decide +native

theorem K4_min_deg : K4.minDegree = 3 := by
  decide +native

theorem K4_max_deg : K4.maxDegree = 3 := by
  decide +native

theorem K4_avg_deg : averageDegree K4 = 3 := by
  unfold averageDegree; simp [Fintype.card_fin]

theorem K4_residue : residue K4 = 0 := by
  unfold residue; decide +native

theorem K4_annihilation : annihilationNumber K4 = 2 := by
  decide +native

theorem petersen_indep : α(PetersenGraph) = 4 := by
  rw [indep_num_eq_computable]; decide +native

theorem petersen_dom : dominationNumber PetersenGraph = 3 := by
  rw [dom_num_eq_computable]; decide +native

theorem petersen_avg_dist : averageDistance PetersenGraph = 5/3 := by
  rw [avg_dist_eq_computable, show computable_avg_dist PetersenGraph = (5 / 3 : ℚ) from by decide +native]
  norm_num

theorem petersen_order : n PetersenGraph = 10 := by simp [n]

theorem petersen_size : PetersenGraph.edgeFinset.card = 15 := by
  decide +native

theorem petersen_szeged : szegedIndex PetersenGraph = 135 := by
  rw [szeged_eq_computable]; decide +native

theorem petersen_wiener : wienerIndex PetersenGraph = 75 := by
  rw [wiener_eq_computable]; decide +native

theorem petersen_min_deg : PetersenGraph.minDegree = 3 := by
  decide +native

theorem petersen_max_deg : PetersenGraph.maxDegree = 3 := by
  decide +native

theorem petersen_avg_deg : averageDegree PetersenGraph = 3 := by
  unfold averageDegree; simp [Fintype.card_fin]; decide +native

theorem petersen_residue : residue PetersenGraph = 0 := by
  unfold residue; decide +native

theorem petersen_annihilation : annihilationNumber PetersenGraph = 5 := by
  decide +native

theorem C6_indep : α(C6) = 3 := by
  rw [indep_num_eq_computable]; decide +native

theorem C6_dom : dominationNumber C6 = 2 := by
  rw [dom_num_eq_computable]; decide +native

theorem C6_avg_dist : averageDistance C6 = 9/5 := by
  rw [avg_dist_eq_computable, show computable_avg_dist C6 = (9 / 5 : ℚ) from by decide +native]
  norm_num

theorem C6_order : n C6 = 6 := by simp [n]

-- EDITABLE START
-- EDITABLE END

theorem C6_size : C6.edgeFinset.card = 6 := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end WrittenOnTheWallII.Test
