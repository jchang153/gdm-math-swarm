import FormalConjectures.Util.ProblemImports

namespace JacobianConjecture

open Classical

section Prelims

variable {k : Type*} [CommRing k]
variable {σ τ ι : Type*}

variable (k σ τ) in

abbrev RegularFunction := τ → MvPolynomial σ k

namespace RegularFunction

noncomputable def Jacobian (F : RegularFunction k σ τ) :
    Matrix σ τ (MvPolynomial σ k) :=
  Matrix.of fun i j => MvPolynomial.pderiv i (F j)

noncomputable def comp
    (F : RegularFunction k σ τ) (G : RegularFunction k τ ι) :
    RegularFunction k σ ι :=
  fun (i : ι) ↦ MvPolynomial.bind₁ F (G i)

variable (k σ) in
noncomputable def id : RegularFunction k σ σ := MvPolynomial.X

end RegularFunction

end Prelims

variable {k : Type*} [Field k] [CharZero k]

section Conjecture

open RegularFunction

variable {σ : Type*} [Fintype σ]

-- EDITABLE START
-- EDITABLE END

theorem jacobian_conjecture (F : RegularFunction k σ σ)
    (H : IsUnit F.Jacobian.det) :
    ∃ (G : RegularFunction k σ σ), G.comp F = id k σ ∧
    F.comp G = id k σ := by
-- EDITABLE START
  fail "Provide a complete proof."
-- EDITABLE END

end Conjecture
end JacobianConjecture
