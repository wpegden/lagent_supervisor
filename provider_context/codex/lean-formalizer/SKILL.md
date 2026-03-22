---
name: lean-formalizer
description: Formalize LaTeX manuscript fragments into Lean 4/mathlib. Use for theorem translation, proof planning, mathlib lemma search, naming, and checked .lean edits.
---

# Lean manuscript formalizer

## Goal
Translate `.tex` mathematics into checked Lean 4/mathlib with readable proofs, minimal imports, and minimal reinvention.

## Workflow
1. Read the relevant manuscript fragment first. Extract all hypotheses, quantifiers, domains, coercions, and typeclass assumptions explicitly.
2. Search existing library facts before writing proof code.
   - First use Lean-side search (`exact?`, `apply?`, `rw?`, completion, docs).
   - Then query the local Loogle server at `http://127.0.0.1:8088/json?q=...` with URL-encoded queries.
3. Prefer existing mathlib definitions and lemmas over ad hoc redefinitions or reproving standard facts.
4. Formalize in small increments:
   - statement
   - helper lemmas
   - final proof
   Re-check after each nontrivial change.
5. Prefer readable proofs over brittle search-heavy proofs. Use automation (`simp`, `aesop`, `linarith`, `nlinarith`, `ring`, `omega`) when it closes goals cleanly; otherwise write the proof structure explicitly.
6. Do not leave search tactics as the final proof when they are only being used for discovery. Inline the suggested lemma/tactic.
7. Do not report completion if any `sorry` remains. If blocked, isolate the exact missing lemma and state it precisely.

## Search guidance
- Use Loogle when you know the shape/types of the target theorem better than its name.
- Use symbol-heavy or type-shape queries, not only English.
- Treat Loogle JSON as a search aid, not a stable API.
- Do not invent theorem names; verify them.

## Formalization conventions
- Match existing namespace, theorem naming, and notation conventions in the repo/mathlib.
- Keep imports minimal.
- Preserve manuscript provenance in comments, e.g. `-- Paper Lemma 2.3`.
- Prefer canonical mathlib abstractions over paper-local encodings.

## Output contract
When finishing a task, report:
- files changed
- new definitions/lemmas/theorems added
- remaining blockers, if any
- exact check/build command run and whether it passed
