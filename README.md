# LAgent Supervisor

`lagent_supervisor` is a control plane for long-running Lean formalization projects. It runs worker and reviewer bursts in `tmux`, validates the repository after each burst, tracks project state under `.agent-supervisor/`, exports static web views, and can manage branching when a proof route genuinely splits.

The current system is built around a **node-centric theorem frontier**:

- each frontier node is a theorem statement
- each node stores a rigorous natural-language proof from its current children
- edges are only structural parent-child links in the current decomposition
- a node is closed only when it has a Lean proof from its current children and all of those children are closed

This README describes the project as it exists now.

## High-Level Workflow

The supervisor moves a project through five phases:

1. `paper_check`
2. `planning`
3. `theorem_stating`
4. `proof_formalization`
5. `proof_complete_style_cleanup`

### `paper_check`

The worker reads the paper and records:

- proof hazards
- notation mismatches
- hidden assumptions
- likely Lean pain points

The main shared artifact is `PAPERNOTES.md`.

### `planning`

The worker turns the paper into a concrete Lean work plan in `PLAN.md` and updates `TASKS.md`.

### `theorem_stating`

The worker writes the statement layer, typically in:

- `PaperDefinitions.lean`
- `PaperTheorems.lean`

In full theorem-frontier mode, this phase must also produce `.agent-supervisor/paper_main_results.json`. That file is the coarse starting proof DAG for the project:

- theorem nodes on the chosen proof route
- exact Lean and natural-language statements for each node
- structural parent-child links between those nodes
- a rigorous natural-language proof on each node from its current children
- an `initial_active_node_id`

### `proof_formalization`

This is the main proving phase. The supervisor maintains an authoritative theorem frontier and each cycle works on **one active node**.

Meaningful progress is one of:

- `CLOSE`: prove the active node from its current children
- `EXPAND`: insert new intermediate nodes between the active node and its current children, then rewrite the active node’s natural-language proof to use the refined child set
- `REFUTE_REPLACE`: replace the active node’s current decomposition with a different one

The system does not require bottom-up work. A cycle can work on any node in the DAG. Closure is still dependency-based: a node only becomes closed when its children are closed too.

When choosing the next active node, the supervisor and reviewer should prefer **high-leverage nodes** over routine wrappers. In practice this means preferring lower nodes, or nodes whose local proof looks tricky or doubtful, when progress there is likely to force upstream refactors/restatements or reveal that the current route is wrong.

### `proof_complete_style_cleanup`

Cleanup starts only after proof formalization reaches a complete clean proof state. This phase is for:

- warning cleanup
- modest refactors
- packaging and style improvements

The supervisor preserves a last known good complete-proof commit and rolls cleanup back if cleanup breaks proof completeness.

## Core Architecture

The repository has one orchestration module and several smaller support modules:

- [supervisor.py](/home/leanagent/src/lagent_supervisor/supervisor.py): main orchestration loop, phase logic, tmux management, branching, exports
- [lagent_supervisor/storage.py](/home/leanagent/src/lagent_supervisor/lagent_supervisor/storage.py): atomic JSON writes and file locking
- [lagent_supervisor/validation.py](/home/leanagent/src/lagent_supervisor/lagent_supervisor/validation.py): repo validation, Lean scanning, theorem-stating edit policy
- [lagent_supervisor/frontier.py](/home/leanagent/src/lagent_supervisor/lagent_supervisor/frontier.py): theorem-frontier schemas, invariants, status derivation, seeding, updates
- [lagent_supervisor/web.py](/home/leanagent/src/lagent_supervisor/lagent_supervisor/web.py): manifest and export helpers
- [lagent_supervisor/providers.py](/home/leanagent/src/lagent_supervisor/lagent_supervisor/providers.py): provider command assembly and provider-specific behavior
- [lagent_supervisor/shared.py](/home/leanagent/src/lagent_supervisor/lagent_supervisor/shared.py): shared constants, default payloads, helper text

## Roles

### Core roles

- `worker`
- `reviewer`

The worker edits the repo and produces a handoff artifact. The reviewer decides whether the cycle should continue, advance phase, stop, or branch.

### Theorem-frontier verifier roles

In full theorem-frontier mode, structural node changes are dual-gated by:

- `paper_verifier`
- `nl_proof_verifier`

The separation is deliberate:

- `paper_verifier` checks that the proposed decomposition is faithful to the paper or to an explicitly justified paper-faithful reformulation
- `nl_proof_verifier` checks that every newly admitted or modified node carries a completely rigorous natural-language proof from its current children

### Branching roles

When proof formalization reaches a genuine route split, the supervisor can run branch strategy and branch selection reviews. Branching is anchored to the current active node.

## Theorem Frontier

### What a node means

A theorem-frontier node is the fundamental proof object. It stores:

- `node_id`
- `kind`
- `natural_language_statement`
- `lean_statement`
- `natural_language_proof`
- `lean_anchor`
- `paper_provenance`
- `blocker_cluster`
- `acceptance_evidence`
- `notes`

Its outgoing edges are just its current children.

The intended semantics are:

- the children are the statements currently being used to prove the node
- the node’s `natural_language_proof` is a rigorous proof of the node from exactly those children
- node proofs should be at least as detailed as the corresponding paper argument, and often more detailed because they must be locally self-contained
- node proofs may not appeal to named paper lemmas/cases that are not represented by the current child set
- if the decomposition changes, the node’s proof must be rewritten to match

### What an edge means

An edge is only a structural parent-child link:

- `parent`
- `child`

Edges do not carry proof status. They do not mean “this single child implies the parent.” They only mean “this child is part of the current decomposition of the parent.”

### Closure

A node is effectively closed only when:

- its raw status is `closed`
- it has a Lean proof from its current children
- all current children are effectively closed

A leaf node is just a node with no children. It closes directly when its own Lean proof exists.

### Active work

The active frontier item is always a node:

- `active_node_id`

The worker may:

- try to close that node directly
- expand that node by refining its children
- refactor that node’s decomposition

The worker is not forced to work bottom-up; it may work on any active node the supervisor selects.

### Expansion

Expansion is local:

- it inserts new nodes between the active node and its current children only
- it preserves unaffected parts of the DAG
- it requires a revised rigorous natural-language proof on the active node from the new child set

### Refactor / replace

If the current child set is the wrong route, the worker may replace it. This is the node-level refactor operation.

## Initial Coarse Paper DAG

The initial proof DAG is seeded from `paper_main_results.json` during the `theorem_stating -> proof_formalization` transition.

That manifest contains:

- `phase`
- `nodes`
- `edges`
- `initial_active_node_id`

The seed DAG should be a coarse proof spine extracted from the paper, not a single top theorem with no structure. Every seeded node must already have:

- an exact Lean statement
- a natural-language statement
- a rigorous natural-language proof from its seeded children

Those seed proofs should already be at least as detailed as the corresponding paper arguments, and often more detailed because they must stand on their own as local proof witnesses for the DAG. If the paper proof uses a named intermediate lemma or case, that dependency belongs in the seed DAG rather than being hidden in prose.

This means proof formalization starts from an explicit paper-derived decomposition instead of inventing the whole structure later.

## Validation

Validation runs after each worker burst.

Current validation covers:

- `lake` availability as a structured check rather than a crash
- build success
- `sorry` detection using token-aware scanning instead of regex-only matching
- unapproved `axiom` detection with the same masking logic
- clean git/worktree checks
- theorem-stating edit policy, including the narrow root-import exception needed for Lean package wiring
- proof-formalization cone checks against the current cycle baseline

## Local Permissions Model

The local multi-user setup is intentionally split between a **supervisor user** and a **burst user**.

- `leanagent` runs the Python supervisor, owns the supervisor code, owns the control scripts under `/home/leanagent/.lagent-supervisor-control`, and performs supervisor-side git commit/push.
- `lagentworker` runs worker/reviewer/verifier bursts via `sudo -n -u lagentworker -g leanagent ...`.

The intended rule is:

- supervisor metadata is owned by `leanagent`
- mutable Lean build state is written by `lagentworker`
- workers may read many supervisor artifacts, but they must not be able to rewrite supervisor code or control scripts

### Permission Classes

There are four important filesystem classes.

1. Supervisor control files

- path: `/home/leanagent/.lagent-supervisor-control`
- owner/group: `leanagent:leanagent`
- directories: `755`
- files: `644`
- generated burst scripts: `755`

Workers must be able to execute the generated burst scripts through `sudo`, but they must not be able to rewrite this control tree.

2. Live supervisor state

- path: `<repo>/.agent-supervisor`
- owner/group: typically `leanagent:leanagent`
- mutable artifact directories such as `cycles/`, `logs/`, `runtime/`, `prompts/`: `2775`
- ordinary live artifact files in those trees: `664`
- authoritative live state files:
  - `state.json`
  - `theorem_frontier.json`
  use `640`
- multi-writer summary/log files such as:
  - `validation_summary.json`
  - `paper_main_results.json`
  - `validation_log.jsonl`
  use `664`

This lets the burst user read what it needs and write its own cycle artifacts, while keeping the main authoritative state files non-world-readable and non-world-writable.

3. Checkpoints

- path: `<repo>/.agent-supervisor/checkpoints`
- owner/group: typically `leanagent:leanagent`
- directories: `2755`
- files: `644`

Checkpoints are immutable snapshots. Workers may need to read them for context, but they should not be able to mutate them. A previous bug came from copying live files into checkpoints with `shutil.copy2`, which preserved restrictive `600` modes from source artifacts; checkpoint normalization now fixes this after checkpoint creation.

4. Repo-local Lean state

- main repo source tree: shared between users
- generated frontier files:
  - directories `2775`
  - files `664`
- `.lake` and other repo-local build state:
  - owner should effectively be `lagentworker`
  - group `leanagent`
  - directories `2775`
  - files `664`

The important practical rule is:

- repo-local `lake` commands should run as `lagentworker`
- the supervisor should orchestrate and validate, but it should not be the normal writer for `.lake`

### Codex Runtime Permissions

When using Codex bursts with `HOME=/home/leanagent`, the worker must be able to read shared Codex config and mutate only the runtime subtrees it actually uses.

The intended policy is:

- `/home/leanagent/.codex/config.toml`: `640`
- `/home/leanagent/.codex/auth.json`: `640`
- mutable runtime directories such as:
  - `sessions/`
  - `tmp/`
  - `log/`
  - `shell_snapshots/`
  - `memories/`
  are normalized recursively so the burst user can read/write them
- `/home/leanagent/.codex/history.jsonl` should also be writable by the burst user and is normalized to `664`

The supervisor normalizes these before launching bursts.

### Operational Rule of Thumb

If you are deciding how a new file should be permissioned, ask:

- Is this a supervisor-owned control artifact? Then it should be readable but not writable by `lagentworker`.
- Is this a live burst artifact that the worker must write? Then it belongs in the shared live state class.
- Is this an immutable checkpoint snapshot? Then it should be readable but not writable by `lagentworker`.
- Is this mutable Lean build state? Then it should be writable by `lagentworker`.

Do not rely on ad hoc `chmod` repairs after failures. New code paths that create files in these areas should normalize permissions immediately after writing them.

Lean file discovery is repo-relative, so repositories are not accidentally skipped just because they live under directory names like `build`, `.git`, `.lake`, `lake-packages`, or `.agent-supervisor`.

## Persistence and Safety

Project state lives under `<repo>/.agent-supervisor/`.

Important files include:

- `state.json`
- `validation_summary.json`
- `worker_handoff.json`
- `review_decision.json`
- `paper_main_results.json`
- `theorem_frontier.json`
- logs under `.agent-supervisor/logs/`
- runtime scripts under `.agent-supervisor/runtime/`
- completed-cycle checkpoints under `.agent-supervisor/checkpoints/`

Shared JSON writes use atomic replace plus file locking, so concurrent writers do not corrupt manifests or state files.

### Completed-Cycle Checkpoints

After every completed reviewer cycle, the supervisor now writes a checkpoint bundle keyed by cycle. A checkpoint records:

- the validated git head for that completed cycle
- `state.json`
- validation and review logs
- theorem-frontier state and history, if present
- chat-event/export state needed to keep the web views coherent after a restore

This supports rollback after a supervisor fix. The intended workflow is:

1. decide the last completed cycle the bug could have affected
2. restore that checkpoint
3. restart the supervisor from there

For example, after `paper_check` completes, a new run will have a checkpoint for cycle `1`, so a later control-plane fix can resume from just after that phase instead of rerunning the paper read.

List checkpoints:

```bash
python3 scripts/restore_cycle_checkpoint.py --config configs/your_run.json --list
```

Restore a specific completed cycle:

```bash
python3 scripts/restore_cycle_checkpoint.py --config configs/your_run.json --cycle 1
```

Restore the latest checkpoint written after completing a named phase:

```bash
python3 scripts/restore_cycle_checkpoint.py --config configs/your_run.json --after-phase paper_check
```

The restore script stops the live supervisor, agent, and monitor tmux sessions for that run before restoring files. It does not auto-launch a new supervisor session; after restore, restart normally with:

```bash
python3 supervisor.py --config configs/your_run.json
```

## Web Exports

The supervisor exports two static web views:

- transcript/chat view
- DAG view

The DAG viewer is in:

- [dag_viewer/index.html](/home/leanagent/src/lagent_supervisor/dag_viewer/index.html)
- [dag_viewer/dag-browser.js](/home/leanagent/src/lagent_supervisor/dag_viewer/dag-browser.js)
- [dag_viewer/dag-browser.css](/home/leanagent/src/lagent_supervisor/dag_viewer/dag-browser.css)
- [dag_viewer/dag-layout-worker.js](/home/leanagent/src/lagent_supervisor/dag_viewer/dag-layout-worker.js)

The viewer now mirrors the node-centric semantics:

- green node: effectively closed theorem
- yellow node: currently active theorem
- gray node: open theorem
- red/faded node: refuted or replaced theorem
- edges are structural only; they are drawn green only when they belong to a closed parent decomposition

Full `natural_language_proof` text remains stored in the authoritative frontier/export data. The web UI should treat those proofs as primary content, but collapse long statements and proofs by default and expand them on demand rather than truncating them in storage.

The static viewers avoid unsafe `innerHTML` and unsafe-link sinks from unescaped data.

## Branching

Branching is optional and only for real route splits.

When a child branch is created, it inherits the current theorem frontier but resets local runtime pressure such as:

- active-node age
- blocker age
- failed-close streak
- cone-purity streak
- escalation state
- last frontier artifacts

Branch selection compares routes anchored to the same current node, rather than unrelated side work.

## Configuration

Projects are launched from JSON configs under [configs/](/home/leanagent/src/lagent_supervisor/configs). A config chooses:

- repository path
- paper path
- worker/reviewer providers and models
- chat and DAG export roots
- workflow start phase
- branching policy
- theorem-frontier mode

Policies can be hot-reloaded from separate JSON policy files.

## Scripts

Useful scripts include:

- [scripts/init_formalization_project.py](/home/leanagent/src/lagent_supervisor/scripts/init_formalization_project.py)
- [scripts/monitor_supervisor_run.py](/home/leanagent/src/lagent_supervisor/scripts/monitor_supervisor_run.py)
- [scripts/restore_cycle_checkpoint.py](/home/leanagent/src/lagent_supervisor/scripts/restore_cycle_checkpoint.py)
- [scripts/export_retrospective_bundle.py](/home/leanagent/src/lagent_supervisor/scripts/export_retrospective_bundle.py)
- [scripts/start_in_tmux.sh](/home/leanagent/src/lagent_supervisor/scripts/start_in_tmux.sh)
- [scripts/export_lean_cycle_stats.py](/home/leanagent/src/lagent_supervisor/scripts/export_lean_cycle_stats.py)

## Testing

The main regression suite is [tests/test_supervisor.py](/home/leanagent/src/lagent_supervisor/tests/test_supervisor.py).

The suite covers:

- storage and manifest safety
- validation behavior
- theorem-frontier schemas and invariants
- node-centric proof-state transitions
- branching behavior
- export payloads
- workflow transitions
- retry and recovery behavior

Run it with:

```bash
python3 -m unittest tests.test_supervisor
```

Useful extra checks:

```bash
python3 -m py_compile supervisor.py lagent_supervisor/*.py tests/test_supervisor.py
node --check dag_viewer/dag-browser.js
node --check dag_viewer/dag-layout-worker.js
```
