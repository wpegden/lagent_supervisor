# Improvement Notes

## Problem

Some runs make real progress but spend too many proof-formalization cycles building parallel wrapper layers around the same unresolved local theorem. This is not purely a Lean style issue. Short wrappers are often useful and the system already encourages paper-facing interfaces and support-file extraction. The gap is that the supervisor does not currently enforce a strong enough notion of **frontier closure**.

In practice, this allows the worker to keep landing:

- pointwise graph-pair wrappers
- fixed-embedding lifts
- fixed-set lifts
- extraction shells
- nat/real variants
- manuscript-native and deterministic variants

even when the same small family of graph-dependent hypotheses remains open.

## Diagnosis

The current prompts emphasize:

- keeping paper-facing files clean
- moving reusable support lemmas into separate files
- maintaining `PLAN.md` and `TASKS.md`

Those are good, but they do not answer the more important question:

> Has this cycle reduced the unresolved theorem frontier, or merely rearranged it?

This is why a run can feel "almost done" for many cycles in a row. The outer shells become nearly complete, while one hard local theorem remains unproved. The system recognizes the blocker in prose, but it does not track or score progress relative to that blocker in a structured way.

## Goal

Keep the benefits of wrappers and abstraction, but make the supervisor enforce:

- one canonical proof route at a time
- one explicit frontier theorem at a time
- measurable reduction in open assumptions

The target is not to ban wrappers. The target is to stop rewarding wrappers that do not close the frontier.

## Proposal

### 1. Add a canonical dependency chain in `planning`

Require `PLAN.md` to name, for each major paper theorem:

- the paper-facing theorem
- the internal endgame theorem that directly implies it
- the currently believed hard local theorem(s)
- the preferred proof route

This should be a short dependency chain, not a broad brainstorm. For example:

1. `paperMainStatement`
2. theorem-level extraction shell
3. fixed-set mass bound
4. fixed-embedding graph-pair bound
5. local graph-pair combinatorial theorem

The plan should also name which step is currently believed to be the real bottleneck.

### 2. Add a canonical endpoint discipline in `theorem_stating`

For each major proof block, require one canonical internal endpoint. The worker can introduce helper variants, but the repo should explicitly designate one primary theorem shape.

This prevents uncontrolled growth of parallel routes such as:

- direct `section4F`
- manuscript loss-gap
- deterministic target-gap
- nat-valued versions
- real-valued versions

unless the reviewer explicitly approves a route split or branch.

### 3. Track a structured frontier theorem in state

Add a small structured field to supervisor state and review logs:

```json
{
  "frontier_theorem": "name of the current bottleneck theorem",
  "frontier_layer": "graph-pair | embedding | fixed-set | extraction | paper-facing",
  "open_hypotheses": [
    "hLossLeTarget",
    "hSmallCard",
    "hblueCap",
    "hblueCapWeight",
    "hredCap",
    "hredCapWeight"
  ]
}
```

The reviewer should update this on every proof-formalization cycle.

This gives the supervisor a machine-readable notion of whether progress is closing the loop.

### 4. Redefine proof progress in the reviewer prompt

Require every `CONTINUE` review to classify the cycle into one of:

- **assumption discharge**: at least one open hypothesis was proved or eliminated
- **layer lift**: a proved result moved from graph-pair to embedding, embedding to fixed-set, fixed-set to extraction, etc.
- **route clarification**: the canonical frontier theorem changed because the old one was shown to be the wrong target

If the cycle does none of these, it should not be counted as strong progress.

This is the key change. The reviewer should stop praising wrapper-writing in the abstract. A new theorem matters only if it reduces the frontier or raises a proved bound to the next layer.

### 5. Add a wrapper-inflation warning trigger

If the same frontier theorem survives for many cycles and the open-hypothesis set does not shrink, the supervisor should become less tolerant.

Suggested rule:

- keep a rolling count of proof cycles since the last reduction in `open_hypotheses`
- if that count exceeds a threshold, the reviewer prompt becomes stricter
- if it continues, prefer `STUCK` or branching rather than repeated `CONTINUE`

This is not a ban on wrappers. It is a safeguard against repeated shell-building around the same missing lemma.

### 6. Make branching aware of frontier stagnation

The branch selector should not only ask which branch looks more promising overall. It should also ask:

- which branch has actually reduced its frontier assumptions recently
- which branch is only reorganizing the same unresolved local theorem

This would help the system prune branches that are "productive-looking" but not actually closing the main theorem.

### 7. Preserve useful wrappers, but demote weak ones

A wrapper should count as high-value only if it does at least one of:

- removes assumptions
- eliminates a quantifier layer
- moves a theorem one proof layer upward
- becomes the canonical endpoint used by paper-facing extraction

A wrapper should count as low-value if it only:

- renames an equivalent hypothesis bundle
- duplicates a nearby theorem under a slightly different arithmetic parameterization
- adds a parallel route without reducing the canonical frontier

The supervisor should not forbid low-value wrappers, but it should stop mistaking them for major progress.

## Prompt Changes

### Worker prompt additions

In `proof_formalization`, add instructions like:

- Name the current frontier theorem explicitly in `TASKS.md`.
- If you introduce a wrapper, say whether it removes assumptions, lifts the result to a higher layer, or only reorganizes existing hypotheses.
- Prefer proving the current frontier theorem directly once a stable shell already exists.
- Do not add parallel theorem families unless you can explain why the current canonical route is inadequate.

### Reviewer prompt additions

In `proof_formalization`, add instructions like:

- Identify the current frontier theorem and its remaining open hypotheses.
- Approve `CONTINUE` only if the cycle shrank the open-hypothesis set, lifted a proved bound to the next layer, or clearly changed the canonical route for good reason.
- If the worker is adding wrappers while the same hypothesis set remains open, call that out explicitly.
- Prefer `STUCK` or branching when the same unresolved theorem survives too long without hypothesis reduction.

## Logging and Metrics

Add small structured fields to each review log entry:

```json
{
  "frontier_theorem": "...",
  "frontier_layer": "graph-pair",
  "open_hypothesis_count": 6,
  "progress_kind": "assumption_discharge"
}
```

This would allow:

- detecting stagnation automatically
- showing a real progress signal on the website
- comparing branches by frontier reduction rather than by vague reviewer optimism

## Rollout Plan

1. Add the structured frontier fields without changing decisions yet.
2. Update reviewer prompts to report `progress_kind` and open-hypothesis counts.
3. After observing a few runs, tighten `CONTINUE` so wrapper-only cycles get treated as weak progress.
4. Finally, connect frontier stagnation to earlier branching or `STUCK`.

## What Not To Do

- Do not ban wrappers outright.
- Do not require every cycle to prove a theorem from scratch.
- Do not force cleanup/style concerns into proof-formalization too early.
- Do not let the reviewer treat any new theorem with a long name as proof of progress.

## Bottom Line

The supervisor already enforces good file hygiene and paper-facing structure. The missing piece is **frontier accounting**.

The system should explicitly track:

- what the current bottleneck theorem is
- which assumptions remain open
- whether a cycle actually reduced those assumptions

That is the right place to address wrapper inflation early, without overcorrecting against legitimate Lean abstraction work.
