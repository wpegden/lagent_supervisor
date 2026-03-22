# Goal

Smoke-test the supervisor on a tiny Lean 4 project.

## Acceptance condition
- Replace the single `sorry` in `SmokeTest/Basic.lean`.
- Keep the theorem statement `addZeroRight (n : Nat) : n + 0 = n`.
- Leave the repository in a state where `lake build` succeeds.
- Maintain `PLAN.md` and `TASKS.md` during the run.

## Notes
- This is a short interactive smoke test, not a long unattended run.
- Keep changes minimal and localized.
- If useful, consult the local Loogle server at `http://127.0.0.1:8088/`.
