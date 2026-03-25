#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_DIR))

import supervisor


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def validation_by_cycle(state_dir: Path) -> Dict[int, Dict[str, Any]]:
    rows = load_jsonl(state_dir / "validation_log.jsonl")
    return {
        int(row.get("cycle", 0) or 0): row
        for row in rows
        if isinstance(row, dict) and int(row.get("cycle", 0) or 0) > 0
    }


def review_by_cycle(state_dir: Path) -> Dict[int, Dict[str, Any]]:
    rows = load_jsonl(state_dir / "review_log.jsonl")
    return {
        int(row.get("cycle", 0) or 0): row
        for row in rows
        if isinstance(row, dict) and int(row.get("cycle", 0) or 0) > 0
    }


def worker_output_for_cycle(state_dir: Path, cycle: int) -> str:
    path = state_dir / "logs" / f"worker-cycle-{cycle:04d}.ansi.log"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def worker_handoff_for_cycle(state_dir: Path, cycle: int) -> Dict[str, Any]:
    output = worker_output_for_cycle(state_dir, cycle)
    if not output:
        return {}
    try:
        return supervisor.extract_json_object(output, required_key="status")
    except supervisor.SupervisorError:
        return {}


def rebase_repo_path(old_repo: Path, new_repo: Path, path: Optional[Path]) -> Optional[Path]:
    if path is None:
        return None
    try:
        rel = path.relative_to(old_repo)
    except ValueError:
        return path
    return new_repo / rel


def historical_config(base: supervisor.Config, repo_path: Path) -> supervisor.Config:
    return supervisor.Config(
        repo_path=repo_path,
        goal_file=rebase_repo_path(base.repo_path, repo_path, base.goal_file) or base.goal_file,
        state_dir=base.state_dir,
        worker=base.worker,
        reviewer=base.reviewer,
        tmux=base.tmux,
        workflow=supervisor.WorkflowConfig(
            start_phase=base.workflow.start_phase,
            sorry_mode=base.workflow.sorry_mode,
            paper_tex_path=rebase_repo_path(base.repo_path, repo_path, base.workflow.paper_tex_path),
            approved_axioms_path=rebase_repo_path(base.repo_path, repo_path, base.workflow.approved_axioms_path)
            or base.workflow.approved_axioms_path,
            human_input_path=rebase_repo_path(base.repo_path, repo_path, base.workflow.human_input_path)
            or base.workflow.human_input_path,
            input_request_path=rebase_repo_path(base.repo_path, repo_path, base.workflow.input_request_path)
            or base.workflow.input_request_path,
        ),
        chat=base.chat,
        git=base.git,
        max_cycles=base.max_cycles,
        sleep_seconds=base.sleep_seconds,
        startup_timeout_seconds=base.startup_timeout_seconds,
        burst_timeout_seconds=base.burst_timeout_seconds,
        branching=base.branching,
    )


@contextmanager
def detached_historical_worktree(config: supervisor.Config, head: Optional[str], enabled: bool) -> Iterator[supervisor.Config]:
    if not enabled or not head or not shutil.which("git"):
        yield config
        return

    with tempfile.TemporaryDirectory(prefix="lagent-branch-replay-") as tmpdir:
        worktree_path = Path(tmpdir) / "repo"
        subprocess.run(
            ["git", "-C", str(config.repo_path), "worktree", "add", "--detach", str(worktree_path), head],
            check=True,
            text=True,
            capture_output=True,
        )
        try:
            yield historical_config(config, worktree_path)
        finally:
            subprocess.run(
                ["git", "-C", str(config.repo_path), "worktree", "remove", "--force", str(worktree_path)],
                check=False,
                text=True,
                capture_output=True,
            )


def historical_state_for_cycle(
    cycle: int,
    reviews: Dict[int, Dict[str, Any]],
    validations: Dict[int, Dict[str, Any]],
    worker_output: str,
    worker_handoff: Dict[str, Any],
) -> Dict[str, Any]:
    truncated_reviews = [reviews[index] for index in sorted(reviews) if index <= cycle]
    return {
        "cycle": cycle,
        "review_log": truncated_reviews,
        "last_review": reviews.get(cycle, {}),
        "last_validation": validations.get(cycle, {}),
        "last_worker_output": worker_output,
        "last_worker_handoff": worker_handoff,
        "last_branch_consideration_cycle": 0,
        "stuck_recovery_attempts": [],
        "branch_context": None,
    }


def choose_cycles(
    requested_cycles: List[int],
    config: supervisor.Config,
    reviews: Dict[int, Dict[str, Any]],
) -> List[int]:
    if requested_cycles:
        return requested_cycles
    triggered: List[int] = []
    for cycle in sorted(reviews):
        decision = reviews[cycle]
        phase = str(decision.get("phase", "")).strip().lower()
        state = {"cycle": cycle, "last_branch_consideration_cycle": 0}
        if supervisor.should_consider_branching(config, state, phase, decision):
            triggered.append(cycle)
    return triggered


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay historical branch-trigger candidates from supervisor logs.")
    parser.add_argument("--config", required=True, help="Path to supervisor JSON config")
    parser.add_argument("--cycles", nargs="*", type=int, default=[], help="Specific historical cycles to inspect")
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Also print the generated branch-strategy prompt preview for each selected cycle",
    )
    parser.add_argument(
        "--max-prompt-chars",
        type=int,
        default=5000,
        help="Maximum number of prompt characters to print per cycle when --show-prompt is used",
    )
    parser.add_argument(
        "--no-historical-head",
        action="store_true",
        help="Do not create detached worktrees at the historical git heads; use the current repo checkout instead",
    )
    args = parser.parse_args()

    config = supervisor.load_config(Path(args.config).expanduser().resolve())
    reviews = review_by_cycle(config.state_dir)
    validations = validation_by_cycle(config.state_dir)
    cycles = choose_cycles(args.cycles, config, reviews)

    if not cycles:
        print("No branch-triggering cycles found in the available review log.")
        return 0

    for cycle in cycles:
        review = reviews.get(cycle)
        if review is None:
            print(f"\n===== cycle {cycle} =====")
            print("No reviewer decision found for this cycle.")
            continue

        phase = str(review.get("phase", "")).strip().lower()
        worker_output = worker_output_for_cycle(config.state_dir, cycle)
        worker_handoff = worker_handoff_for_cycle(config.state_dir, cycle)
        state = historical_state_for_cycle(cycle, reviews, validations, worker_output, worker_handoff)
        should_branch = supervisor.should_consider_branching(config, state, phase, review)
        tags = supervisor.branch_strategy_signal_tags(review)
        validation = validations.get(cycle, {})
        head = ((validation.get("git") or {}).get("head") if isinstance(validation, dict) else None)

        print(f"\n===== cycle {cycle} =====")
        print(f"phase={phase}")
        print(f"should_consider_branching={should_branch}")
        print(f"signal_tags={tags}")
        print(f"git_head={head or 'unknown'}")
        print(f"review_decision={review.get('decision')}")
        print(f"review_reason={str(review.get('reason', '')).strip()}")

        if not args.show_prompt:
            continue

        with detached_historical_worktree(config, head, enabled=not args.no_historical_head) as prompt_config:
            prompt = supervisor.build_branch_strategy_prompt(
                prompt_config,
                state,
                phase,
                worker_output,
                json.dumps(worker_handoff, indent=2, ensure_ascii=False) if worker_handoff else "{}",
                validation if isinstance(validation, dict) else {},
                review,
                False,
                include_terminal_output=False,
            )
        trimmed = prompt[: args.max_prompt_chars]
        print("\n--- branch-strategy prompt preview ---")
        print(trimmed)
        if len(prompt) > len(trimmed):
            print(f"\n[truncated at {args.max_prompt_chars} characters]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
