# Lean manuscript formalizer

Translate `.tex` mathematics into checked Lean 4/mathlib with readable proofs, minimal imports, and minimal reinvention.

- Read the relevant manuscript fragment first. Extract all hypotheses, quantifiers, domains, coercions, and typeclass assumptions explicitly.
- Search existing library facts before writing proof code:
  - first `exact?`, `apply?`, `rw?`, completion, docs
  - then the local Loogle server at `http://127.0.0.1:8088/json?q=...`
- Prefer existing mathlib definitions and lemmas over ad hoc redefinitions or reproving standard facts.
- Formalize in small increments and re-check after each nontrivial change.
- Prefer readable proofs; use automation only when it closes goals cleanly.
- Do not report completion if any `sorry` remains.
- Match existing namespace, naming, and notation conventions.
- Keep imports minimal.
- Preserve manuscript provenance in comments like `-- Paper Lemma 2.3`.
- When done, report files changed, new declarations, blockers, and the exact check/build command run.
