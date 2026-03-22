# Agent Notes

This repository exists only to smoke-test `lagent_supervisor`.

## Primary task
- Replace the `sorry` in `SmokeTest/Basic.lean`.
- Keep the edit small.
- Use `lake build` if you want to verify the result.

## Local context
- A local Loogle server is available at `http://127.0.0.1:8088/`.
- Prefer the local Loogle server over external search if you want to look up lemmas.
- Do not block on any extra setup beyond the local server already running.
- Prefer straightforward Lean tactics over broad refactors.
