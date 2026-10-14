import Lean
import Comparator
import Lean4Checker.Replay

/-! gdm-math-swarm trusted export verifier (researcher-only).
Uses the pinned Comparator statement comparison and Lean4Checker replay, with
a researcher-side axiom audit that also traverses opaque bodies. Upstream
`runForUsedConsts` omits those bodies; kernel replay alone allows axioms. -/

open Lean

namespace GdmVerify

/-- Verbatim copy of `Comparator.primitiveTargets` at the pinned revision. -/
def primitiveTargets : Array Name := #[
  ``Nat.add, ``Nat.sub, ``Nat.mul, ``Nat.pow, ``Nat.gcd, ``Nat.div, ``Nat.mod,
  ``Nat.beq, ``Nat.ble, ``Nat.land, ``Nat.lor, ``Nat.xor, ``Nat.shiftLeft,
  ``Nat.shiftRight, ``String.ofList]

abbrev V := ExceptT (String × String × String) IO

def stage (status name : String) : Except String α → V α
  | .ok a => pure a
  | .error e => throw (status, name, e)

/-- Keep upstream type, mutual declaration and recursor dependencies, and add
opaque bodies explicitly. Opaqueness affects reduction, not proof validity. -/
def runForProofDependencies [Monad m] (info : ConstantInfo) (f : Name → m Unit) : m Unit := do
  Comparator.runForUsedConsts info f
  if let .opaqueInfo opaqueDecl := info then
    opaqueDecl.value.getUsedConstants.forM f

/-- Check the entire reachable proof closure. Mark before expansion so mutual
and cyclic declarations terminate. Missing dependencies fail the trusted audit. -/
partial def auditProofClosure (solution : Comparator.ExportedEnv) (legal : Std.HashSet Name)
    (pending : Array Name) (checked : Std.HashSet Name) : Except String Unit := do
  if pending.isEmpty then
    return ()
  let target := pending.back!
  let rest := pending.pop
  if checked.contains target then
    auditProofClosure solution legal rest checked
  else
    let some info := solution.constMap[target]?
      | throw s!"Constant not found in solution '{target}'"
    if let .axiomInfo decl := info then
      if !legal.contains decl.name then
        throw s!"Illegal axiom detected: '{target}'"
    let collect : StateM (Array Name) Unit := runForProofDependencies info fun name =>
      modify (fun names => names.push name)
    let (_, next) := collect.run rest
    auditProofClosure solution legal next (checked.insert target)

/-- Roots must be theorems; then inspect their types and proof dependencies,
including recursively nested opaque helpers. The permitted set is unchanged. -/
def checkProofAxioms (solution : Comparator.ExportedEnv) (targets legal : Array Name) :
    Except String Unit := do
  for target in targets do
    let some info := solution.constMap[target]?
      | throw s!"Const not found in solution: '{target}'"
    let .thmInfo _ := info
      | throw s!"Solution constant is not a theorem: '{target}'"
  auditProofClosure solution (Std.HashSet.ofArray legal) targets {}

def verify (theorems legal : Array Name) (challengePath solutionPath : System.FilePath) : V Unit := do
  let challenge ← stage "parse_failed" "parse_challenge" (Comparator.parse (← IO.FS.readFile challengePath))
  let solution ← stage "parse_failed" "parse_solution" (Comparator.parse (← IO.FS.readFile solutionPath))
  stage "compare_failed" "compare" (Comparator.compareAt challenge solution (theorems ++ legal) primitiveTargets)
  stage "axioms_failed" "axioms" (checkProofAxioms solution theorems legal)
  let constMap := solution.constMap.erase ``Quot.mk |>.erase ``Quot.lift |>.erase ``Quot.ind
  let env ← mkEmptyEnvironment
  match ← (env.replay' constMap).toBaseIO with
  | .ok _ => pure ()
  | .error e => throw ("kernel_rejected", "kernel", toString e)

def emit (status name detail : String) : IO Unit :=
  IO.println (Json.mkObj [("status", status), ("stage", name), ("detail", detail)]).compress

end GdmVerify

def main (args : List String) : IO UInt32 := do
  let [configPath, challengePath, solutionPath] := args
    | IO.eprintln "usage: gdm_verify_exports config.json challenge.export solution.export"; return 2
  let config ← IO.ofExcept <| Json.parse (← IO.FS.readFile configPath)
  let theorems ← IO.ofExcept <| config.getObjValAs? (Array String) "theorem_names"
  let legal ← IO.ofExcept <| config.getObjValAs? (Array String) "permitted_axioms"
  match ← (GdmVerify.verify (theorems.map String.toName) (legal.map String.toName)
      challengePath solutionPath).run with
  | .ok () => GdmVerify.emit "verified" "kernel" "Lean default kernel accepts the solution"
  | .error (status, name, detail) => GdmVerify.emit status name detail
  return 0
