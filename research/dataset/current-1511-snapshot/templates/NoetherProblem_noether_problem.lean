import FormalConjectures.Util.ProblemImports

namespace NoetherProblem

class IsRationalExtension (K L ι : Type*)
    [Field K] [Field L] [Algebra K L] where
  pure_transcendental :
    Nonempty (L ≃ₐ[K] ((FractionRing (MvPolynomial ι K))))

theorem rationalExtension_empty_index (K L ι : Type*) [Field K] [Field L] [Algebra K L] [IsEmpty ι]
    [IsRationalExtension K L ι] :
    Nonempty (L ≃ₐ[K] K) := by
  set a : L ≃ₐ[K] (FractionRing (MvPolynomial ι K)) :=
    Classical.choice IsRationalExtension.pure_transcendental
  set b : (MvPolynomial ι K) ≃ₐ[K] K := MvPolynomial.isEmptyAlgEquiv K ι
  set c : FractionRing (MvPolynomial ι K) ≃ₐ[K] K :=
    IsFractionRing.fieldEquivOfAlgEquiv K (FractionRing (MvPolynomial ι K)) K b
  apply Nonempty.intro (a.trans c)

def HasNoetherProperty (K L ι : Type) [Field K] [Field L] [Fintype ι]
    [Algebra K L] [IsRationalExtension K L ι] : Prop :=
  ∀ H : Subgroup (L ≃ₐ[K] L), Finite H → ∃ ι' : Type,
    IsRationalExtension K (IntermediateField.fixedField H) ι'

-- EDITABLE START
-- EDITABLE END

theorem noether_problem : answer(False) ↔ ∀ (K L ι G : Type)
    [Field K] [Field L] [Fintype ι] [Algebra K L] [IsRationalExtension K L ι],
    HasNoetherProperty K L ι := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end NoetherProblem
