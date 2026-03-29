# Theorem Frontier Upgrade Testing Strategy

## Goal

Upgrade the supervisor from a free-text proof frontier to a theorem-frontier workflow without depending on expensive real-paper trial runs for intermediate phases.

The strategy is to test the upgrade as a control-plane system:

- prompt contracts,
- artifact contracts,
- state transitions,
- frontier aging/escalation,
- branch behavior,
- and replay behavior on historical logs.

Real-paper runs should be reserved for final confidence, not for discovering basic control-flow bugs.

## Principles

1. Test each rollout phase independently before making it the default.
2. Prefer deterministic synthetic fixtures over live provider runs.
3. Make every new frontier artifact machine-validated.
4. Use replay tests from historical projects to catch wrapper-churn regressions.
5. Keep a strict backward-compatibility lane while the feature is gated off.

## Test Layers

### 1. Schema and validation tests

These are the cheapest and highest-value tests.

They should cover:

- config parsing and defaults for theorem-frontier mode switches;
- worker frontier artifact validation;
- reviewer frontier artifact validation;
- theorem-frontier state-file validation;
- node/edge schema validation once the DAG arrives;
- refusal of malformed or incomplete active-node statements.

These tests should intentionally include malformed cases:

- missing Lean statement,
- missing theorem id,
- invalid action enum,
- invalid outcome enum,
- empty blocker cluster,
- phase mismatch,
- structurally invalid child lists,
- bad closure mode,
- invalid branch-point reuse.

### 2. Prompt contract tests

The new workflow is prompt-driven, so prompt text is part of the interface.

Tests should assert:

- legacy prompts stay unchanged when theorem-frontier mode is off;
- Phase 0 proof prompts require exactly one active theorem and one action;
- reviewer prompts require theorem-frontier classification rather than generic progress judgment;
- artifact paths are mentioned correctly;
- prompt text distinguishes `CLOSE`, `EXPAND`, and `REFUTE/REPLACE`;
- prompt text forbids side growth outside the active cone once that phase is introduced.

These tests should be string-level regression tests, similar to the existing cleanup-phase prompt tests.

### 3. Pure state-transition tests

This is the core substitute for expensive paper runs.

Create direct tests for supervisor-owned frontier state updates:

- first active theorem initializes frontier state;
- same theorem id increments active-node age;
- same blocker cluster with renamed theorem still increments blocker-cluster age;
- `CLOSED` resets the active node;
- `EXPANDED` keeps the blocker lineage but changes the active node correctly;
- `REFUTED_REPLACED` records the failed route and replacement summary;
- rejected frontier reviews do not count as progress;
- escalation flags are raised when thresholds are crossed.

These tests should operate entirely on in-memory state dictionaries.

### 4. Loop-level supervisor tests

Use the existing mocked `main()`-style tests to verify:

- Phase 0 proof cycles load the frontier artifacts;
- missing frontier artifacts fail the cycle cleanly when the mode is on;
- frontier review output is written into state and exported artifacts;
- theorem-frontier state survives restart/recovery;
- branch episodes preserve the theorem-frontier state in child configs/state;
- cleanup phase does not accidentally demand theorem-frontier artifacts if the mode is not supposed to apply there.

These are the tests that catch glue bugs between prompt generation, artifact loading, validation, and state persistence.

### 5. Replay tests from historical runs

This is the most important non-live testing layer.

Build small replay fixtures from past projects, especially `twobites`, and feed the supervisor historical frontier situations without calling providers.

The fixtures should encode cycles where:

- a wrapper target was later discovered impossible;
- the same blocker cluster was re-expressed under different wrappers;
- a branch split represented two genuinely different replacements;
- the system looked “nearly done” at the outer theorem file while the inner theorem remained open.

For replay, the test oracle is not “prove the paper”.
The oracle is control behavior:

- would blocker-cluster age have increased?
- would escalation have triggered?
- would the reviewer have been forced to call `EXPAND` or `REFUTE/REPLACE`?
- would wrapper-only outer-shell progress have failed the active-cone rule?

### 6. Mutation-style regression tests

Add adversarial tests that intentionally simulate the known failure mode:

- same blocker, new wrapper name;
- same blocker, extra quantifier shell;
- same blocker, one more extraction theorem above it;
- same blocker, support lemma outside the active cone.

The expected result should be:

- no theorem-frontier progress credit,
- blocker-cluster age increases,
- escalation threshold moves closer,
- reviewer output classifies the cycle as no real frontier movement.

This is how we make “wrapper churn” a tested anti-property.

### 7. Branch and subtree tests

Once the DAG phases arrive, add tests around branch semantics:

- branches may only diverge below the selected node;
- node ids above the branch point remain shared;
- selection uses descendant closure / residual cutset criteria rather than wrapper count;
- refuted nodes remain visible in history after a branch loses;
- branch replacement under a frontier cap preserves node identity and branch provenance.

These tests can be synthetic; they do not need real Lean repos.

### 8. Export and observability tests

The upgrade only helps if humans and later agents can inspect it.

Add tests for:

- theorem-frontier state exported to JSON cleanly;
- retrospective bundle includes frontier state/history;
- chat export metadata can surface active theorem id, action, and blocker age later if desired;
- paused/restarted runs preserve frontier state.

## Phase-by-phase test plan

### Phase 0: active theorem + action discipline

Target behavior:

- each proof cycle names exactly one active theorem;
- worker classifies its intended action as `CLOSE`, `EXPAND`, or `REFUTE_REPLACE`;
- reviewer confirms what actually happened;
- supervisor tracks active-theorem age and blocker-cluster age.

Required tests:

- config default off / explicit on;
- worker/reviewer prompt contract;
- artifact validation;
- frontier-state persistence;
- age updates;
- restart recovery;
- backward-compatibility with mode off.

### Phase 1: minimal authoritative frontier JSON

Target behavior:

- one current active theorem plus immediate descendants/support scope are stored authoritatively;
- `TASKS.md` becomes a derived view.

Required tests:

- frontier JSON schema;
- derived `TASKS.md` rendering;
- refusal of nodes without exact Lean statements;
- migration from Phase 0 state to Phase 1 frontier file.

### Phase 2: local sub-DAG expansion and explicit refutation

Target behavior:

- active node can be replaced by a small child DAG;
- bad routes become `refuted` rather than just “stuck”.

Required tests:

- 2–5 child expansion validation;
- invalid over-large expansion rejection;
- explicit refutation records;
- replacement preserves parent linkage;
- replay tests for historical impossible routes.

### Phase 3: paper-verifier structural gate

Target behavior:

- structural edits are not admitted until paper-verified.

Required tests:

- structural edit trigger detection;
- approve / approve-with-caveat / reject paths;
- no state mutation on rejected structural changes;
- caveat propagation into frontier metadata.

### Phase 4: active-cone enforcement and branch-at-node policy

Target behavior:

- reviewer rejects theorem-irrelevant side growth;
- branching only occurs at named nodes/subtrees.

Required tests:

- low cone-purity rejection;
- side-wrapper no-credit replay cases;
- branch-at-node lineage preservation;
- branch-selection metrics keyed to descendant closure and blocker stability.

## Suggested fixture families

### Tiny synthetic repos

Use tiny fake repos with:

- one theorem file,
- one support file,
- small mocked review/worker payloads.

These are the base fixtures for loop tests.

### Historical replay fixtures

Store a compact fixture set extracted from:

- `twobites`,
- rental-harmony branches,
- a successful small project like `connectivity_threshold_gnp`.

Each fixture only needs:

- cycle id,
- active theorem label,
- blocker cluster label,
- worker frontier artifact,
- reviewer frontier artifact,
- expected supervisor state update.

### Negative fixtures

Add malformed JSON and misleading wrapper-progress fixtures so the validator and reviewer logic is tested against the exact failure mode we are trying to eliminate.

## Rollout gates

A phase should not become default until:

1. schema tests pass,
2. prompt contract tests pass,
3. loop tests pass,
4. replay fixtures show the intended control behavior,
5. backward-compatibility tests pass with the feature disabled.

## Immediate recommendation

Implement Phase 0 first and do not default it on yet.

Phase 0 gives the highest signal-to-risk ratio because it:

- introduces named active theorem tracking,
- introduces blocker-cluster aging,
- introduces `CLOSE` / `EXPAND` / `REFUTE_REPLACE`,
- but does not yet require a full DAG editor or a paper-verifier gate.

That is enough to test the core control insight from `newplan.md` before paying the cost of the later phases.
