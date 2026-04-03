# LAgent Supervisor

`lagent_supervisor` runs long-lived, mostly unattended formalization projects against a Lean repository. It is a control plane for provider CLIs such as Claude Code, Codex, and Gemini: it launches worker and reviewer bursts in `tmux`, validates the repo after every burst, maintains project state under `.agent-supervisor/`, exports web views, and optionally manages strategic branching.

The current system is built around a multi-phase workflow and a proof DAG:

- early phases read and organize the paper
- theorem stating produces paper-facing Lean statements and a coarse paper-derived proof DAG
- proof formalization works edge-by-edge on that DAG
- cleanup runs only after a complete clean proof state is reached

This README describes the project as it exists now.

## What The Supervisor Does

At a high level, the supervisor:

- loads a JSON config and an optional hot-reloadable policy file
- creates a per-project state directory under `<repo>/.agent-supervisor/`
- launches worker/reviewer bursts in real `tmux` windows
- validates the repository after every worker burst
- decides whether to continue, advance phase, stop, or branch
- exports transcript and DAG state to static web directories
- resumes cleanly after interruption

The system is designed for Lean formalization projects driven by a paper, but most of the control-plane logic is provider-agnostic.

## Workflow Phases

The supported workflow phases are:

1. `paper_check`
2. `planning`
3. `theorem_stating`
4. `proof_formalization`
5. `proof_complete_style_cleanup`

### `paper_check`

The worker reads the manuscript and records:

- proof corrections
- hidden assumptions
- notation issues
- likely formalization hazards

The main shared file is `PAPERNOTES.md`.

### `planning`

The worker writes a concrete formalization plan in `PLAN.md` and updates `TASKS.md`. This is where the proof route is mapped into a Lean work plan.

### `theorem_stating`

The worker writes the statement layer:

- `PaperDefinitions.lean`
- `PaperTheorems.lean`

In full theorem-frontier mode, this phase must also produce:

- `.agent-supervisor/paper_main_results.json`

This file is no longer just a “main results list”. It is a **coarse paper-DAG manifest**: a paper-derived proof spine with explicit theorem nodes and explicit dependency edges.

### `proof_formalization`

This is the main proving phase. In the current default system, proof formalization is controlled by an authoritative theorem-frontier DAG. Progress is edge-centric:

- close an existing open edge, or
- expand an existing open edge into a finer local sub-DAG, or
- refute/replace the current route

### `proof_complete_style_cleanup`

This phase only starts after proof formalization has reached a complete clean proof state. Cleanup is optional polishing:

- warning cleanup
- moderate refactors
- tidying theorem packaging

The supervisor preserves a last known good complete proof commit and will roll back cleanup if cleanup breaks proof completeness.

## Roles

The supervisor currently uses these role families.

### Core roles

- `worker`
- `reviewer`

The worker edits the repo and writes a handoff artifact. The reviewer judges the cycle and decides whether to continue, advance phase, stop, or branch.

### Theorem-frontier verifier roles

In full theorem-frontier mode, structural proof-DAG edits are dual-gated by:

- `paper_verifier`
- `nl_proof_verifier`

The separation is intentional:

- `paper_verifier` checks that a proposed structural expansion is faithful to the paper or to an explicitly justified paper-faithful reformulation.
- `nl_proof_verifier` checks that the natural-language proofs carried by the newly admitted edges and new leaf obligations are rigorous enough to justify admission into the authoritative DAG.

### Branching roles

When proof formalization reaches a genuine route split, the supervisor can invoke:

- a branch strategy decision
- branch selection / branch replacement review

Branching is anchored to the current active theorem-frontier obligation edge.

## Theorem-Frontier DAG

The theorem-frontier DAG is the core proof-control structure in full mode.

### Nodes

Nodes are theorem statements. Each node stores:

- `node_id`
- `kind`
- `natural_language_statement`
- `natural_language_proof`
- `lean_statement`
- `lean_anchor`
- `paper_provenance`
- `closure_mode`
- `blocker_cluster`
- `acceptance_evidence`
- `notes`

Important node kinds include:

- `paper`
- `paper_faithful_reformulation`
- `support`

### Edges

Edges are proof obligations or reductions. Each edge has:

- canonical `edge_id = parent|edge_type|child`
- `edge_type`
- `status`
- `justification`
- `natural_language_proof`
- `paper_verifier_status` once admitted

Important edge types include:

- `direct_proof`
- `reduction`
- `case_split`
- `all_of`
- `any_of`
- `equivalence`
- `strengthening`
- `replacement`

### Active frontier

The active frontier item is an **edge**, not a node.

- `active_edge_id` names the current obligation
- the active theorem node is derived from the parent of that edge

This matters operationally: the unit of progress is the obligation edge.

### Proof semantics

The intended semantics are:

- a green edge means the implication/reduction represented by that edge is proved
- a green node means the theorem statement is proved

Node proof status is derived from the proof state of its outgoing dependency edges and the proof status of the downstream child nodes.

The system does not treat “workflow said closed” as enough. In full mode, proof closure is dependency-aware.

### Direct proof edges

Direct theorem proving is represented explicitly by `direct_proof` self-edges. These are ordinary edges in the authoritative DAG. They are not synthesized silently by the supervisor.

If a theorem is to be proved directly at the current level of granularity, that direct-proof edge must exist explicitly and must carry a natural-language proof like any other edge.

## Initial Coarse Paper-DAG

In full mode, the initial proof DAG is seeded from `paper_main_results.json` during the `theorem_stating -> proof_formalization` transition.

That manifest now contains:

- `phase`
- `nodes`
- `edges`
- `initial_active_edge_id`

The seed DAG is intended to be a **coarse paper-derived proof spine**, not a one-node placeholder. It should include:

- the main theorem nodes on the chosen proof route
- important paper lemmas/propositions/cases on that route
- exact Lean and NL statements for every node
- explicit edge-level natural-language proofs explaining the coarse manuscript reductions

This means proof formalization starts from a paper-extracted DAG rather than discovering the whole structure from scratch later.

## Full-Mode Progress Rule

In the current system, a meaningful proof-formalization cycle should do one of:

1. close one or more existing open edges
2. expand one open edge into a finer local sub-DAG
3. refute/replace the current route

Structural expansion is proof-carrying:

- every newly admitted edge must include a rigorous `natural_language_proof`
- every newly admitted leaf node must include a rigorous `natural_language_proof`
- the `paper_verifier` and `nl_proof_verifier` must both approve the admitted subset

Existing authoritative nodes are immutable. If a theorem statement changes, the route should be replaced rather than silently edited in place.

## Branching Model

Branching is available during proof formalization.

The branch anchor is the current active obligation edge:

- branch proposals should be competing ways to resolve the same active obligation
- branch selection compares which branch is more likely to close that obligation and then finish the paper

When a child branch is created, it inherits the current authoritative DAG but resets local runtime pressure:

- active-edge age
- active-node age
- blocker age
- failed-close streak
- cone-purity streak
- escalation state
- last frontier artifacts

This avoids carrying parent stagnation pressure into a fresh child route.

## Runtime Architecture

### `tmux`

Bursts run in real `tmux` windows. This is the primary live-debugging surface.

Typical sessions:

- `<repo>-supervisor`
- `<repo>-agents`

The supervisor launches a worker burst, captures its handoff and terminal output, validates the repo, then launches the reviewer burst.

### Per-role scope directories

Each role gets its own scope directory under:

```text
<repo>/.agent-supervisor/scopes/<provider>-<role>/
```

Each scope contains symlinks:

```text
repo/        -> the real Lean repository
supervisor/  -> <repo>/.agent-supervisor
```

This gives provider CLIs distinct working directories while still editing the same repository.

## Project Files

### Repository files managed during a run

Always:

- `GOAL.md`
- `TASKS.md`

From `paper_check` onward:

- `PAPERNOTES.md`

From `planning` onward:

- `PLAN.md`

From `theorem_stating` onward:

- `PaperDefinitions.lean`
- `PaperTheorems.lean`

### Supervisor state files

Under:

```text
<repo>/.agent-supervisor/
```

Important files include:

- `state.json`
- `validation_summary.json`
- `validation_log.jsonl`
- `review_log.jsonl`
- `theorem_frontier.json`
- `theorem_frontier_history.jsonl`
- `paper_main_results.json`
- `worker_handoff.json`
- `review_decision.json`
- `theorem_frontier_paper_verifier.json`
- `theorem_frontier_nl_proof_verifier.json`
- logs under `logs/`
- runtime scripts under `runtime/`

## Validation

After every worker burst, the supervisor runs validation. Depending on config and phase, this can include:

- build status
- syntax checks
- `sorry` policy
- unapproved axiom checks
- git cleanliness / head / branch / remote status
- theorem-stating file-cone enforcement
- theorem-frontier allowed-edit-path enforcement

If a reviewer asks to advance phase or stop as done but validation blocks that transition, the supervisor records a `last_transition_error` in `state.json` and emits a `transition_blocked` warning.

## Providers

Supported CLIs:

- Claude Code
- Codex
- Gemini CLI

Mixed worker/reviewer pairs are supported.

### Claude

The supervisor uses Claude’s CLI in its role scope directory and resumes the role-local conversation across bursts.

### Codex

The supervisor uses `codex exec` and later `codex exec resume --last`, scoped to the role directory. Codex budget pauses are supported: when the configured threshold is crossed, the supervisor waits and polls rather than exiting.

### Gemini

The supervisor uses Gemini with `--approval-mode=yolo`. Gemini roles can specify a `fallback_model`; on Gemini rate-limit/capacity failures, the supervisor can immediately rerun the burst on the fallback model.

## Configuration

Configs live in `configs/`. The initializer script can create one for you.

Important config areas:

- repo path and goal file
- worker and reviewer provider/model settings
- `tmux` session names
- workflow start phase and paper `.tex` path
- theorem-frontier mode
- git settings
- chat export settings
- branching settings
- optional hot-reloadable policy path

The policy file is separate from the main config and is designed to be edited while a run is live.

### Minimal command

```bash
python3 supervisor.py --config configs/codex_worker_claude_reviewer.json
```

### Start in tmux

```bash
./scripts/start_in_tmux.sh configs/codex_worker_claude_reviewer.json lean-supervisor
```

### Start in screen

```bash
./scripts/start_in_screen.sh configs/codex_worker_claude_reviewer.json lean-supervisor
```

## Initializer

To bootstrap a project:

```bash
python3 scripts/init_formalization_project.py
```

The initializer can:

- copy or flatten a paper `.tex`
- set up the repo path
- create `GOAL.md`
- create a config file
- initialize a Lean package when appropriate
- normalize package and session names

## Monitoring And Debugging

### Attach to agent windows

```bash
tmux attach -t <agents-session>
```

or:

```bash
./scripts/attach_agents_tmux.sh <agents-session>
```

### Watch logs

```bash
./scripts/watch_logs.sh /path/to/repo
```

Important log files:

- `worker.latest.ansi.log`
- `reviewer.latest.ansi.log`
- `worker.all.ansi.log`
- `reviewer.all.ansi.log`
- `review_log.jsonl`

### Hourly monitor

The repository includes:

- `scripts/monitor_supervisor_run.py`

This script can monitor a run, classify failures, snapshot diagnostics, and restart the supervisor when that is the configured response.

## Web Interfaces

The project currently has two static web exports.

### 1. Transcript viewer

The transcript viewer lives under the configured chat root, by default:

```text
~/lagent-chats/
```

It exports:

- prompts
- worker/reviewer JSON artifacts
- validation summaries
- shared markdown files through a lightweight web markdown viewer

It does **not** publish the raw terminal capture used for local debugging.

The supervisor manages:

- `~/lagent-chats/index.html`
- `~/lagent-chats/_assets/`
- `~/lagent-chats/repos.json`
- `~/lagent-chats/<repo_name>/...`

Install the viewer assets manually if needed:

```bash
python3 scripts/install_lagent_chats_user_files.py
```

### 2. DAG viewer

The DAG viewer is exported beside the chat root, under:

```text
~/lagent-dags/
```

It displays the authoritative theorem-frontier DAG:

- nodes
- edges
- active obligation
- proof/edge statuses
- cycle-by-cycle frontier deltas

The DAG browser assets live in:

- `dag_viewer/index.html`
- `dag_viewer/dag-browser.js`
- `dag_viewer/dag-layout-worker.js`
- `dag_viewer/dag-browser.css`

## Nginx Helper

To generate an nginx config for the transcript viewer:

```bash
python3 scripts/render_lagent_chats_nginx_conf.py
```

The default generated site serves `/lagent-chats/` from `~/lagent-chats`.

## Supporting Scripts

Notable scripts in `scripts/`:

- `init_formalization_project.py`
- `start_in_tmux.sh`
- `start_in_screen.sh`
- `attach_agents_tmux.sh`
- `watch_logs.sh`
- `watch_worker.sh`
- `monitor_supervisor_run.py`
- `install_lagent_chats_user_files.py`
- `install_provider_context_files.py`
- `render_lagent_chats_nginx_conf.py`
- `export_lean_cycle_stats.py`
- `export_retrospective_bundle.py`
- `replay_branching_candidates.py`
- `reset_smoke_test.sh`

## Provider Context Files

Provider-specific context lives under `provider_context/`. The supervisor can install those files into the appropriate provider home directories or role scopes.

## Testing

The main test suite is:

```bash
python3 -m unittest tests.test_supervisor
```

Useful local checks:

```bash
python3 -m py_compile supervisor.py tests/test_supervisor.py scripts/monitor_supervisor_run.py
node --check dag_viewer/dag-browser.js
node --check dag_viewer/dag-layout-worker.js
```

The more detailed theorem-frontier testing strategy is documented in:

- [theorem_frontier_testing_strategy.md](theorem_frontier_testing_strategy.md)

## Current Defaults And Expectations

The current system assumes:

- multi-phase runs are the normal mode
- full theorem-frontier mode is the default proof-formalization mode
- the initial proof DAG comes from a coarse paper-derived manifest
- proof progress is tracked by obligation edges
- structural expansion is proof-carrying and verifier-gated

This is no longer a “vibes-only” loop. The supervisor is opinionated and stateful, and the theorem-frontier DAG is the authoritative proof-control object during proof formalization.
