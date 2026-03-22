# Smoke Test Repo

This is a tiny Lean 4 project used to smoke-test `lagent_supervisor`.

The intended task is simple:

- inspect the repo,
- maintain `PLAN.md` and `TASKS.md`,
- replace the single `sorry` in `SmokeTest/Basic.lean`,
- leave a handoff JSON for the supervisor.

The project is intentionally small so you can watch one short burst in `tmux`.

Reset it with:

```bash
../scripts/reset_smoke_test.sh
```
