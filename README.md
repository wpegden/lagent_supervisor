# Lean formalization worker/reviewer supervisor

This package implements a two-agent loop for long unattended Lean formalization runs.

It now supports either:

- a single proof-formalization phase, which matches the original workflow, or
- a multi-phase workflow:
  - `paper_check`
  - `planning`
  - `theorem_stating`
  - `proof_formalization`
  - `proof_complete_style_cleanup`

The worker edits the repo and maintains shared workflow files such as `TASKS.md`, `PAPERNOTES.md`, `PLAN.md`, `PaperDefinitions.lean`, and `PaperTheorems.lean` as the current phase requires.

The reviewer reads the worker handoff JSON, the latest terminal output, and the supervisor's validation summary, then returns a decision such as `CONTINUE`, `ADVANCE_PHASE`, `NEED_INPUT`, `STUCK`, or `DONE`. When the reviewer returns `STUCK`, the supervisor now asks for up to ten distinct stuck-recovery suggestions before finally stopping the run as stuck. Inside a branch run, that branch-local stuck-recovery budget is shorter: four failed recovery attempts cause the branch to be pruned. In proof formalization, the supervisor can also open a branch episode when the reviewer has identified a genuine route split, such as continuing the current path versus a major rewrite. After proof formalization reaches a complete clean proof state, the workflow advances into `proof_complete_style_cleanup`, where the goal is optional warning cleanup and moderate refactors while preserving a fully buildable proof state after every cycle.

It supports:

- Claude Code
- Codex CLI
- Gemini CLI
- mixed worker/reviewer pairs, such as Codex worker + Claude reviewer

## What changed in this version

This package now uses **real `tmux` windows for each burst**.

Each cycle works like this:

1. the supervisor writes a prompt file;
2. it launches the worker in a new `tmux` window using the provider's **non-interactive one-shot mode**;
3. you can attach to the `tmux` session and watch the worker's native terminal output live while it runs;
4. when the worker process exits, the supervisor reads `supervisor/worker_handoff.json` and captures the terminal output from the pane;
5. the supervisor launches the reviewer the same way;
6. the reviewer writes `supervisor/review_decision.json`;
7. if the decision is `CONTINUE`, the supervisor launches the next worker burst;
8. if the decision is `STUCK`, the supervisor asks the reviewer for a creative stuck-recovery strategy and injects that guidance into the next worker burst, up to ten consecutive times before finally stopping as stuck; branch-local runs instead use a four-attempt budget and are pruned automatically if they exhaust it.
9. if the reviewer identifies a genuine strategic fork, the supervisor can spawn up to `branching.max_current_branches` child runs from the same git commit, let them run to the initial branch-selection checkpoint given by `branching.evaluation_cycle_budget`, and then ask which branch seems more likely to eventually succeed at formalizing the whole paper; if the selector says `CONTINUE_BRANCHING`, later checkpoints use shorter recheck increments (default `+5`, reused thereafter), and after the initial checkpoint continuing both branches is treated as a higher-bar choice that should be reserved for genuinely close calls.

So the agents are visible in real TTY windows while running, but the supervisor still gets a clean file-based handoff when they finish.
If the supervisor itself exits mid-cycle, rerunning it resumes the failed stage of the current cycle rather than always starting a fresh worker cycle.
If a provider CLI process exits nonzero, the supervisor automatically retries the same burst after 1 hour, then 2 hours, then 3 hours before finally surfacing the error.
If the latest Codex weekly budget drops below the configured policy threshold, the supervisor pauses before launching any new Codex burst and periodically rechecks until the budget recovers; it stays in-process rather than exiting, so resuming does not require a restart.
If a branch episode is active, the parent supervisor pauses its own mainline, monitors the child branch runs, automatically prunes child branches that exhaust their branch-local stuck-recovery budget, and after selection leaves the winning child supervisor running in its own worktree.
If a child branch later finds a compelling replacement split while the frontier is already at the branch cap, it can still propose that split upward. The parent supervisor then decides whether to keep the current frontier or replace it by selecting that branch and immediately branching it again.

## High-level architecture

Every role gets its own persistent scope directory under:

```text
<repo>/.agent-supervisor/scopes/<provider>-<role>/
```

Inside each scope directory there are two symlinks:

```text
repo/        -> your actual Lean repo
supervisor/  -> <repo>/.agent-supervisor
```

The agents therefore operate from their own per-role project roots, while still editing the same underlying repository via `repo/`.

This gives the worker and reviewer separate contexts for providers that scope sessions by project directory.

## Files the workflow may maintain

Always:

- `repo/TASKS.md` — short-term task list with checked-off items

From `paper_check` onward:
- `repo/PAPERNOTES.md` — proof corrections, hidden assumptions, and mathematical notes

From `planning` onward:
- `repo/PLAN.md` — high-level formalization roadmap

From `theorem_stating` onward:
- `repo/PaperDefinitions.lean`
- `repo/PaperTheorems.lean`

At the end of every worker burst it must also write:

- `supervisor/worker_handoff.json`

with this shape:

```json
{
  "phase": "planning",
  "status": "NOT_STUCK",
  "summary_of_changes": "brief summary",
  "current_frontier": "what it is working on now",
  "likely_next_step": "best next step",
  "input_request": ""
}
```

The reviewer writes:

- `supervisor/review_decision.json`

with this shape:

```json
{
  "phase": "planning",
  "decision": "CONTINUE",
  "confidence": 0.81,
  "reason": "brief reason",
  "next_prompt": "short prompt for the worker"
}
```

The supervisor also writes:

- `supervisor/validation_summary.json`

with build status, syntax checks, sorry counts, and axiom enforcement results for the current cycle.
If a git remote is configured, the validation summary also includes git branch, remote, head, and worktree cleanliness information.

## Monitoring model

The important distinction is:

- **human monitoring** happens through `tmux`
- **supervisor handoff** happens through JSON files plus captured pane text

This means you get the best available TTY fidelity while the agent is running, instead of only seeing redirected logs.

## Prerequisites

You need:

- Python 3.10+
- `tmux`
- whichever CLIs you want to use, already installed and authenticated:
  - `claude`
  - `codex`
  - `gemini`

## Minimal setup

### Interactive initializer

If you want the repo, `GOAL.md`, copied paper, and supervisor config created for you, run:

```bash
python3 scripts/init_formalization_project.py
```

It prompts for:

- the source `.tex` file or an arXiv identifier
- the working Lean repo path
- the optional git remote URL
- the config path to write
- the worker and reviewer providers

If you provide an arXiv identifier, the initializer downloads the latest arXiv source bundle, chooses the main `.tex` file, and flattens included `.tex` and available `.bbl` content into a single reference `.tex` before copying it into the repo.
Generated tmux session names are normalized to tmux-safe characters, so identifiers such as `1702.07325` become session names such as `arxiv-1702_07325-agents`.

By default it creates a `paper_check` workflow using a Codex worker and Claude reviewer, writes a config under `configs/`, sets `max_cycles` to `150`, invokes `lake init` with an explicit installed Lean release when available to avoid the transient `stable` warning, rewrites the default Lean GitHub CI workflow to a build-only check, and keeps finished tmux burst windows around for inspection.

### 1. Create a goal file in your Lean repo

Put something like this in `GOAL.md`:

```md
# Goal

Formalize the target theorem from the paper.

## Acceptance condition
- The final Lean theorem statement is present.
- The proof is completed with no placeholders.
- Supporting lemmas needed for the result are formalized.
```

A starter template is included at `examples/GOAL.example.md`.

### 2. Copy and edit a config file

Pick one of the example configs in `configs/` and update:

- `repo_path`
- `goal_file`
- optional `git.remote_url`
- optional `git.remote_name`
- optional `git.branch`
- optional `git.author_name` / `git.author_email`
- optional `workflow.start_phase`
- optional `workflow.paper_tex_path`
- optional `workflow.sorry_mode`
- optional model names
- optional timeout settings:
  - `startup_timeout_seconds` for launch failures before the burst script starts
  - `burst_timeout_seconds` for a single worker/reviewer burst that never exits
- optional `tmux.session_name`
- optional `policy_path`
- optional branching settings:
  - `branching.max_current_branches` to cap concurrent strategic branches; default `2`
  - `branching.evaluation_cycle_budget` seeds the initial branch-selection checkpoint in the live policy file; default `20`
  - `branching.selection_recheck_increments_reviews` controls later checkpoint increments after a `CONTINUE_BRANCHING` decision; default `[5]`, with the last value reused for any further rechecks
  - `branching.poll_seconds` seeds the default branch-monitor poll interval in the live policy file; default `300`

The supervisor also supports a shared hot-reloadable policy file. By default it lives next to the config as `<config>.policy.json`, or you can set `policy_path` explicitly. Child branch configs inherit the same `policy_path`, so one edit affects the whole project frontier on the next loop/poll boundary without restarting the supervisors.

Phase-1 live policy settings are:

- `stuck_recovery.mainline_max_attempts`
- `stuck_recovery.branch_max_attempts`
- `branching.evaluation_cycle_budget`
- `branching.selection_recheck_increments_reviews`
- `branching.poll_seconds`
- `branching.proposal_cooldown_reviews`
- `branching.replacement_min_confidence`
- `timing.sleep_seconds`
- `timing.agent_retry_delays_seconds`
- `codex_budget_pause.weekly_percent_left_threshold`
- `codex_budget_pause.poll_seconds`
- `prompt_notes.worker`
- `prompt_notes.reviewer`
- `prompt_notes.branching`

If the policy file is invalid, the supervisor keeps using the last known good policy and prints a warning instead of crashing mid-run.

When the frontier is already at `branching.max_current_branches`, the current implementation only allows a replacement split if the proposing branch offers a full new capped frontier by itself. In the default two-branch setup, that means one active branch can propose a two-way split that fully replaces the current frontier if the parent reviewer judges it clearly superior.

If `git.remote_url` is set, the supervisor will:

- initialize `repo_path` as a git repository if needed
- configure the requested remote
- configure local `user.name` / `user.email` if they are missing
- add a supervisor-managed `.gitignore` block for the state directory
- refuse to proceed if the configured remote branch already exists but the local repo has no commits

When a git remote is configured, the worker is instructed to commit and push after every productive burst.

### 3. Run the supervisor

Foreground:

```bash
python3 supervisor.py --config configs/codex_worker_claude_reviewer.json
```

Or inside GNU Screen:

```bash
./scripts/start_in_screen.sh configs/codex_worker_claude_reviewer.json lean-supervisor
```

Or inside its own `tmux` session:

```bash
./scripts/start_in_tmux.sh configs/codex_worker_claude_reviewer.json lean-supervisor
```

### 4. Attach to the agent windows

The worker/reviewer bursts run in the `tmux` session from the config, for example:

```bash
tmux attach -t lean-agents
```

or:

```bash
./scripts/attach_agents_tmux.sh lean-agents
```

### 5. Tail the logs if desired

```bash
./scripts/watch_logs.sh /path/to/your/repo
```

Logs are under:

```text
<repo>/.agent-supervisor/logs/
```

Important files:

- `worker.latest.ansi.log`
- `reviewer.latest.ansi.log`
- `worker.all.ansi.log`
- `reviewer.all.ansi.log`
- `review_log.jsonl`

## Web transcript browser

The supervisor can also export a web-safe transcript stream of the supervisor/agent conversation:

- worker prompts
- worker handoff JSON
- supervisor validation summaries
- reviewer prompts
- reviewer decision JSON
- links to the current exported workflow markdown files such as `GOAL.md`, `TASKS.md`, `PAPERNOTES.md`, `PLAN.md`, `HUMAN_INPUT.md`, and `INPUT_REQUEST.md` when they exist, opened through a lightweight web markdown viewer

It does **not** publish the raw terminal capture used for local debugging.

By default the web files live under:

```text
~/lagent-chats/<repo_name>/
```

with shared viewer assets in:

```text
~/lagent-chats/index.html
~/lagent-chats/_assets/
~/lagent-chats/repos.json
```

You can initialize the viewer assets without running the supervisor:

```bash
python3 scripts/install_lagent_chats_user_files.py
```

Optional config block:

```json
"chat": {
  "root_dir": "~/lagent-chats",
  "repo_name": "my-repo",
  "public_base_url": "https://packer.math.cmu.edu/lagent-chats/"
}
```

To serve the viewer with nginx, render the site config:

```bash
python3 scripts/render_lagent_chats_nginx_conf.py
```

Typical setup on Ubuntu:

```bash
python3 scripts/render_lagent_chats_nginx_conf.py | sudo tee /etc/nginx/sites-available/lagent-chats >/dev/null
sudo ln -sf /etc/nginx/sites-available/lagent-chats /etc/nginx/sites-enabled/lagent-chats
sudo setfacl -m u:www-data:rx /home/$USER
sudo setfacl -R -m u:www-data:rx ~/lagent-chats
sudo setfacl -d -m u:www-data:rx ~/lagent-chats
sudo nginx -t
sudo systemctl enable --now nginx
sudo certbot --nginx -d packer.math.cmu.edu
```



## Default model/effort settings in the example configs

The shipped example configs now default to the strongest documented settings for unattended runs:

- **Claude Code**: `--model opus --effort max`
- **Codex CLI**: `--model gpt-5.4 --config model_reasoning_effort="xhigh"`
- **Gemini CLI**: `--model gemini-3.1-pro-preview`

For Gemini, the current Gemini 3 docs say the default thinking level is already `high`, so the package does not add a separate effort flag for Gemini 3.1 Pro.

## Example configs

### Claude worker + Claude reviewer

```bash
python3 supervisor.py --config configs/claude_worker_claude_reviewer.json
```

### Codex worker + Claude reviewer

```bash
python3 supervisor.py --config configs/codex_worker_claude_reviewer.json
```

### Claude worker + Codex reviewer

```bash
python3 supervisor.py --config configs/claude_worker_codex_reviewer.json
```

### Gemini worker + Claude reviewer

```bash
python3 supervisor.py --config configs/gemini_worker_claude_reviewer.json
```

### Gemini worker + Gemini reviewer

```bash
python3 supervisor.py --config configs/gemini_worker_gemini_reviewer.json
```

### Full multi-phase workflow

```bash
python3 supervisor.py --config configs/full_workflow_example.json
```

## Provider notes

### Claude Code

The supervisor uses Claude in print mode and resumes the latest conversation in the role scope directory on later bursts. The example configs pin Claude to `opus` and add `--effort max`.

The repo now carries a packaged Claude skill at `provider_context/claude/lean-formalizer/SKILL.md`.
On startup, the supervisor installs it to the current user's `~/.claude/skills/lean-formalizer/` and also into each Claude role scope under `.claude/skills/lean-formalizer/`.

### Codex

The supervisor uses `codex exec` for initial bursts and `codex exec resume --last` for later bursts, again scoped to the role directory. The example configs pin Codex to `gpt-5.4` and set `model_reasoning_effort="xhigh"`.

Because the role scope directory itself is not the Git repo root, the package passes `--skip-git-repo-check` and instructs the agent to work under `repo/`.

The repo now carries a packaged Codex skill at `provider_context/codex/lean-formalizer/SKILL.md`.
On startup, the supervisor installs it to the current user's `~/.codex/skills/lean-formalizer/` and also into each Codex role scope under `.agents/skills/lean-formalizer/`.

### Gemini CLI

The supervisor uses `--prompt` for initial bursts and `--resume latest --prompt` for later bursts, with `--approval-mode=yolo`. The example configs pin Gemini to `gemini-3.1-pro-preview`; Gemini 3 models already default to high thinking, so no extra effort flag is added.

The repo now carries a packaged Gemini context file at `provider_context/gemini/GEMINI.md`.
On startup, the supervisor installs it to the current user's `~/.gemini/GEMINI.md` and into each Gemini role scope as `GEMINI.md`.

You can also seed a user home explicitly with:

```bash
python3 scripts/install_provider_context_files.py --home-dir /home/leanagent
```

## Loop details

Each cycle:

1. worker burst runs in a real `tmux` window;
2. worker updates the current phase artifacts such as `repo/TASKS.md`, `repo/PAPERNOTES.md`, `repo/PLAN.md`, `repo/PaperDefinitions.lean`, and `repo/PaperTheorems.lean`;
   If a git remote is configured and the burst made real progress, the worker should commit and push before ending the burst.
3. worker writes `supervisor/worker_handoff.json`;
4. supervisor captures the worker pane output and runs its own validation checks;
5. reviewer burst runs in a real `tmux` window;
6. reviewer reads the files and decides `CONTINUE`, `ADVANCE_PHASE`, `NEED_INPUT`, `DONE`, or `STUCK` as appropriate for the current phase;
7. reviewer writes `supervisor/review_decision.json`;
8. if `ADVANCE_PHASE`, the supervisor moves to the next workflow phase;
9. if `NEED_INPUT`, the supervisor writes `INPUT_REQUEST.md` and pauses until `HUMAN_INPUT.md` is provided;
10. if `CONTINUE`, the supervisor launches the next worker burst;
11. if `STUCK`, the supervisor runs a stuck-recovery pass and gives the worker up to ten distinct creative recovery attempts before finally stopping as stuck; branch-local runs instead get four recovery attempts before that branch is pruned.

## First recommendation

Start with either:

- Codex worker + Claude reviewer
- Claude worker + Claude reviewer

Those are the combinations most likely to work with the least local tweaking.

## Expected rough edges

This is still a v1 package.

It does **not** yet do any of the following:

- run `lake build`
- count `sorry`
- detect repeated identical failure modes
- enforce budget ceilings
- distinguish "soft pause" from real mathematical stuckness using objective metrics

It is intentionally the simple vibes-only version.
