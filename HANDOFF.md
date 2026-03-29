# Handoff Notes

## What This Repo Is

`lagent_supervisor` is the control plane for the Lean-paper autoprover workflow. It manages:

- phased runs over a formalization repo:
  - `paper_check`
  - `planning`
  - `theorem_stating`
  - `proof_formalization`
  - `proof_complete_style_cleanup`
- worker/reviewer burst orchestration through provider adapters
- validation (`lake build`, syntax checks, sorry policy, axioms, git state)
- stuck-recovery
- branch episodes and branch selection
- chat/export generation for the website
- sidecar export tasks such as retrospective bundles and Lean line metrics

The codebase center of gravity is still [supervisor.py](/home/leanagent/src/lagent_supervisor/supervisor.py). The main regression suite is [tests/test_supervisor.py](/home/leanagent/src/lagent_supervisor/tests/test_supervisor.py).

## Current High-Level Status

The biggest recent change is that the proof loop is no longer just “worker/reviewer plus free-text frontier”. It now defaults to a **full theorem-frontier DAG workflow** during `proof_formalization`.

Current default behavior:

- `workflow.theorem_frontier_phase` now defaults to `"full"`.
- `proof_formalization` requires machine-validated theorem-frontier artifacts.
- There is an authoritative DAG stored in `.agent-supervisor/theorem_frontier.json`.
- Structural DAG changes go through a separate paper-verifier burst.
- Branching is anchored to the active theorem-frontier node.
- A bounded static cone guard now checks actual edited Lean files against worker-declared allowed paths.
- `theorem_stating` now must also produce a main-results manifest, and `theorem_stating -> proof_formalization` seeds the initial proof DAG from that manifest.

This was implemented to address the exact failure mode exposed by the `twobites` run: long stretches of clean builds and wrapper construction without a supervisor-owned source of truth about which mathematical obligation was actually being closed.

## Most Important Recent Changes

### 1. Proof-Complete Cleanup Phase

A separate final phase now exists:

- phase name: `proof_complete_style_cleanup`
- purpose:
  - warning cleanup
  - moderate refactors that improve reuse/readability
  - preserve a completely buildable proof state after every cycle
- behavior:
  - if cleanup work breaks proof completeness, the supervisor restores the last good proof-complete commit
  - if cleanup stalls or is not worth continuing, the supervisor stops successfully as `DONE`

This logic is implemented in [supervisor.py](/home/leanagent/src/lagent_supervisor/supervisor.py) and documented in [README.md](/home/leanagent/src/lagent_supervisor/README.md). The corresponding tests are already in the main test suite.

### 2. Full Theorem-Frontier DAG Model

The proof-formalization loop now has an authoritative theorem DAG.

Key properties:

- one active theorem leaf at a time
- every proof cycle must do one of:
  - `CLOSE`
  - `EXPAND`
  - `REFUTE_REPLACE`
- nodes require:
  - exact natural-language statement
  - exact Lean statement
  - anchor
  - provenance
  - blocker cluster
  - closure mode
- structural changes are paper-verified before admission
- existing authoritative nodes are immutable
- rejected paper-verifier edits cannot silently enter the DAG
- closed nodes cannot be immediately resurrected

This is all in [supervisor.py](/home/leanagent/src/lagent_supervisor/supervisor.py). The design motivation originally lived in `newplan.md`, but the code now reflects the implemented version. `newplan.md` is being removed per request.

### 3. Branching Integrated With Theorem Frontier

Branching behavior has been tightened substantially:

- branch prompts now anchor on the current active theorem-frontier node
- child branches inherit the authoritative DAG
- child branches do **not** inherit stagnation pressure:
  - active-leaf age resets
  - blocker age resets
  - failed-close counts reset
  - cone-purity streak resets
  - escalation resets
  - last frontier artifacts reset
- branch snapshots now include theorem-frontier summaries:
  - active leaf
  - blocker cluster
  - open hypothesis count/list
  - ages
  - cone purity
  - escalation state
- branch replacement proposals are now checked against the current anchored frontier

This was important because otherwise a new branch would begin already “aged” by the parent, and branch selection would still be comparing mostly cycle counts rather than structured frontier closure progress.

### 4. Cone Enforcement

The old “active cone” rule was originally prompt-level only. That was not enough.

Now:

- the worker must declare `allowed_edit_paths` in full theorem-frontier mode
- the supervisor computes actual changed Lean files since the previous validated head
- if a changed Lean file is outside `allowed_edit_paths`, the cycle fails cone validation

This is a **file-level** cone check, not a semantic theorem-dependency checker. That limitation is important:

- it prevents obvious off-cone edits
- it does **not** prove theorem-level semantic locality

This is the intended practical enforcement level for now.

### 5. Theorem-Stating Now Seeds the Initial DAG

This was the last major change in the current session.

Current behavior:

- in full theorem-frontier mode, `theorem_stating` must write `.agent-supervisor/paper_main_results.json`
- that manifest must enumerate all paper main results that should appear as initial DAG nodes
- on the normal `theorem_stating -> proof_formalization` transition:
  - the manifest is validated
  - the initial proof DAG is seeded from it
  - all declared main results appear as authoritative paper nodes
  - `initial_active_node_id` selects the first active proof target

This fixes the previous behavior where proof formalization started with an empty theorem-frontier DAG and had to create its first node ad hoc.

Important caveat:

- this seeding happens on the normal transition out of `theorem_stating`
- a repo started directly at `proof_formalization` will still not get that seeding automatically unless that path is explicitly added later

## Current Testing Status

The regression suite is currently strong by repo standards.

Last verified results:

- `python3 -m py_compile supervisor.py tests/test_supervisor.py`
- `python3 -m unittest tests.test_supervisor`
- result: **155 tests passed**

What is now covered:

- theorem-frontier prompt contracts
- artifact schema validation
- DAG integrity rules
- paper-verifier approval enforcement
- branch/theorem-frontier integration
- replacement-anchor drift rejection
- child branch frontier reset behavior
- cone file guard behavior
- theorem-stating manifest validation and DAG seeding
- cleanup-phase rollback behavior
- loop-level `main()` tests for several workflows

There is also a separate planning document for non-paper test strategy:

- [theorem_frontier_testing_strategy.md](/home/leanagent/src/lagent_supervisor/theorem_frontier_testing_strategy.md)

That file is worth reading before changing the theorem-frontier control logic further.

## Current Repo Working Tree State

There are significant uncommitted changes right now.

Tracked files modified:

- [README.md](/home/leanagent/src/lagent_supervisor/README.md)
- [chat_viewer/app.js](/home/leanagent/src/lagent_supervisor/chat_viewer/app.js)
- [chat_viewer/index.html](/home/leanagent/src/lagent_supervisor/chat_viewer/index.html)
- [chat_viewer/markdown-viewer.html](/home/leanagent/src/lagent_supervisor/chat_viewer/markdown-viewer.html)
- [chat_viewer/styles.css](/home/leanagent/src/lagent_supervisor/chat_viewer/styles.css)
- [supervisor.py](/home/leanagent/src/lagent_supervisor/supervisor.py)
- [tests/test_supervisor.py](/home/leanagent/src/lagent_supervisor/tests/test_supervisor.py)

Untracked files worth knowing about:

- [improvement_notes.md](/home/leanagent/src/lagent_supervisor/improvement_notes.md)
- [theorem_frontier_testing_strategy.md](/home/leanagent/src/lagent_supervisor/theorem_frontier_testing_strategy.md)
- [scripts/export_lean_cycle_stats.py](/home/leanagent/src/lagent_supervisor/scripts/export_lean_cycle_stats.py)
- [scripts/export_retrospective_bundle.py](/home/leanagent/src/lagent_supervisor/scripts/export_retrospective_bundle.py)
- several local run config/policy files under [configs/](/home/leanagent/src/lagent_supervisor/configs)
- several screenshot files used for viewer checks

The config files under `configs/` are mostly local run artifacts and should be treated carefully before any commit.

## Current State of the Web Interface

The next session is expected to focus on the website, so this section is intentionally detailed.

### What the Website Currently Does

The viewer lives under [chat_viewer/](/home/leanagent/src/lagent_supervisor/chat_viewer) and is exported into `/home/leanagent/lagent-chats`.

Current features:

- **manual refresh only**
  - auto-refresh was removed
  - the header has a `Refresh now` button
- **viewer-version checking**
  - there is a `_assets/viewer-version.json`
  - the page compares versions to avoid stale JS/CSS after deploys
  - one infinite-reload bug from placeholder versions was fixed
- **weekly Codex budget indicator**
  - shown in the header under the refresh controls
  - label is `weekly budget left`
  - fed by exported `codex-budget.json`
- **paused status support**
  - projects/runs can display as paused
  - paused badges appear in sidebar/header/branch cards
- **branch UI**
  - branched projects use branch panels / branch-board columns
  - branch columns are clickable and select the transcript shown below
  - duplicate lower transcript rendering for branched projects was fixed
- **Lean delta stats**
  - per-cycle validated cards show Lean `+/-` deltas
  - `+` is green, `-` is red
  - branch cards also show branch-local cumulative totals
  - the “transcript shown below” header shows lineage totals across the selected branch chain
- **mobile/browser verification work**
  - several screenshot-driven checks were done
  - some fixes specifically targeted phone-sized branch rendering

### Important Current Web-Interface Behavior

The viewer currently does **not** use chunked history loading anymore.

That history went:

1. chunked loading was added to reduce large refresh cost
2. it caused stale-manifest issues and confusion around live branch histories
3. it was removed again
4. the viewer now goes back to full `events.jsonl` loading

So:

- there is **no** `Load older history` feature anymore
- large projects still fetch full transcript history
- bandwidth/performance on slow connections may still be a problem

### Current Web Fragility / Likely Next Targets

This is the most important section for the next session.

The web viewer is better than it was, but it is still the weakest subsystem compared to the supervisor core.

Known issues / risks:

1. **Branch rendering complexity is still high**
   - `app.js` still carries a lot of logic around branch episodes, branch boards, selected timelines, and transcript rendering.
   - Even after recent simplifications/fixes, branched project rendering is still more fragile than single-thread transcript rendering.

2. **Full-history loading is back**
   - chunking is gone
   - this improves correctness
   - but performance for very large transcripts is still not ideal

3. **Browser-cache sensitivity still matters**
   - viewer-version logic helps, but stale tabs and stale assets were a recurring source of confusion
   - mobile browsers are especially likely to surface this

4. **The website is now feature-rich but operationally messy**
   - paused indicators
   - budget widget
   - branch board
   - Lean delta lines
   - lineage totals
   - version checks
   - files/download links
   These all work, but the viewer code is growing large and should probably be simplified rather than only patched further.

### Files Most Likely To Matter for Web Work

- [chat_viewer/app.js](/home/leanagent/src/lagent_supervisor/chat_viewer/app.js)
  - primary behavior/UI logic
  - currently large and the main source of branching-view fragility
- [chat_viewer/index.html](/home/leanagent/src/lagent_supervisor/chat_viewer/index.html)
  - top-level layout and header controls
- [chat_viewer/styles.css](/home/leanagent/src/lagent_supervisor/chat_viewer/styles.css)
  - branch board styles, paused badge styling, Lean delta styling
- [supervisor.py](/home/leanagent/src/lagent_supervisor/supervisor.py)
  - export-side generation for website data
  - viewer-version export
  - codex budget export
  - chat site refresh logic

### Web Features Added Recently That the Next Session Should Be Aware Of

- `weekly budget left` header indicator
- paused run indicator
- branch-card Lean totals
- selected-lineage Lean totals outside the branch card
- colored per-cycle Lean `+/-`
- simple browsable `files/` directory on the HTTP server for retrospective zip downloads
- manual refresh only

### My Recommendation for the Next Web Session

If the goal is reliability, the next session should probably bias toward **simplifying the viewer model** rather than adding more UI features.

Concrete likely directions:

- reduce branching-display logic in `app.js`
- separate project selection / branch selection / transcript rendering more cleanly
- make data flow more explicit instead of inferred
- keep export-side changes minimal unless absolutely necessary

The website is currently usable, but it is not yet as robust as the supervisor/tested control-plane logic.

## Sidecar Scripts Added Recently

### Lean Cycle Stats Exporter

- file: [scripts/export_lean_cycle_stats.py](/home/leanagent/src/lagent_supervisor/scripts/export_lean_cycle_stats.py)
- purpose:
  - compute Lean-only line deltas from validated git heads
  - power the per-cycle and per-branch Lean stats shown on the site

### Retrospective Bundle Exporter

- file: [scripts/export_retrospective_bundle.py](/home/leanagent/src/lagent_supervisor/scripts/export_retrospective_bundle.py)
- purpose:
  - generate a detailed zip bundle for postmortem/review by another agent
  - includes:
    - README
    - paper tex
    - cycle dossiers
    - cycle patches
    - plan/task history
    - communications
    - supervisor code snapshot
    - policy snapshot

This was used to create downloadable bundles for `twobites` and `connectivity_threshold_gnp`.

## Real Run State To Remember

The important live-run operational point from earlier:

- `twobites` was paused intentionally to save resources
- the website was updated to indicate paused state

So if someone checks the website and sees `twobites` marked paused, that is expected, not a bug.

## Files That Capture Design Intent

These are useful context files:

- [improvement_notes.md](/home/leanagent/src/lagent_supervisor/improvement_notes.md)
  - notes on what went wrong/right with theorem-frontier planning and wrapper churn
- [theorem_frontier_testing_strategy.md](/home/leanagent/src/lagent_supervisor/theorem_frontier_testing_strategy.md)
  - the non-paper testing strategy for the theorem-frontier rollout
- [README.md](/home/leanagent/src/lagent_supervisor/README.md)
  - now updated to describe the cleanup phase, theorem-frontier mode, branch integration, and theorem-stating main-results manifest

## What Was Removed

- `newplan.md` was the design-plan scratch file for the theorem-frontier DAG transition.
- The code now implements the core of that plan.
- It is being removed so the next session uses the code, README, testing strategy, and this handoff as the primary sources of truth instead of a stale planning document.

## Practical Advice for the Next Session

1. Start by reading:
   - [HANDOFF.md](/home/leanagent/src/lagent_supervisor/HANDOFF.md)
   - [README.md](/home/leanagent/src/lagent_supervisor/README.md)
   - [theorem_frontier_testing_strategy.md](/home/leanagent/src/lagent_supervisor/theorem_frontier_testing_strategy.md)

2. If you work on the website, inspect:
   - [chat_viewer/app.js](/home/leanagent/src/lagent_supervisor/chat_viewer/app.js)
   - [chat_viewer/index.html](/home/leanagent/src/lagent_supervisor/chat_viewer/index.html)
   - [chat_viewer/styles.css](/home/leanagent/src/lagent_supervisor/chat_viewer/styles.css)

3. Before committing anything, check `git status` carefully.
   - There are both real code changes and many local/untracked operational files.

4. Keep using the test suite.
   - The theorem-frontier changes are too invasive to edit casually without rerunning the full regression suite.

5. If changing the proof-control logic again:
   - prefer state-machine integrity and explicit invariants over prompt-only guidance
   - keep branching aligned to the active theorem-frontier anchor
   - be cautious about adding new viewer/export complexity unless it pays for itself in reliability

## Last Known Validation Snapshot for This Session

- `python3 -m py_compile supervisor.py tests/test_supervisor.py`
- `python3 -m unittest tests.test_supervisor`
- result: **155 tests passed**

That is the baseline this handoff assumes.
