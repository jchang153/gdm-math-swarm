import FormalConjectures.Util.ProblemImports

namespace OeisA287616

def IsSumOfTriangularAndGeneralizedPolygonal (n : ℕ) : Prop :=
  ∃ x y z : ℕ, n = x * (x + 1) / 2 + y * (3 * y + 1) / 2 + z * (5 * z + 1) / 2

theorem isSumOfTriangularAndGeneralizedPolygonal_0 :
    IsSumOfTriangularAndGeneralizedPolygonal 0 :=
  ⟨0, 0, 0, by norm_num⟩

theorem isSumOfTriangularAndGeneralizedPolygonal_1 :
    IsSumOfTriangularAndGeneralizedPolygonal 1 :=
  ⟨1, 0, 0, by norm_num⟩

theorem isSumOfTriangularAndGeneralizedPolygonal_2 :
    IsSumOfTriangularAndGeneralizedPolygonal 2 :=
  ⟨0, 1, 0, by norm_num⟩

theorem isSumOfTriangularAndGeneralizedPolygonal_4 :
    IsSumOfTriangularAndGeneralizedPolygonal 4 :=
  ⟨1, 0, 1, by norm_num⟩

-- EDITABLE START
-- EDITABLE END

theorem conjecture (n : ℕ) : IsSumOfTriangularAndGeneralizedPolygonal n := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end OeisA287616
