#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
CHAT_VIEWER_DIR = PACKAGE_DIR / "chat_viewer"
PROVIDER_CONTEXT_DIR = PACKAGE_DIR / "provider_context"
PROMPT_TOKEN = "__PROMPT__"
CHAT_VIEWER_VERSION_PLACEHOLDER = "__VIEWER_VERSION__"
DEFAULT_CHAT_BASE_URL = "https://packer.math.cmu.edu/lagent-chats/"
DEFAULT_MAINLINE_STUCK_RECOVERY_ATTEMPTS = 10
DEFAULT_BRANCH_STUCK_RECOVERY_ATTEMPTS = 4
DEFAULT_BRANCH_FRONTIER_REPLACEMENT_MIN_CONFIDENCE = 0.8
DEFAULT_BRANCH_PROPOSAL_COOLDOWN_REVIEWS = 5
DEFAULT_BRANCH_SELECTION_RECHECK_INCREMENTS_REVIEWS: Tuple[int, ...] = (5,)
DEFAULT_AGENT_CLI_RETRY_DELAYS_SECONDS: Tuple[float, ...] = (3600.0, 7200.0, 10800.0)
DEFAULT_CODEX_WEEKLY_BUDGET_PAUSE_THRESHOLD_PERCENT_LEFT = 15.0
DEFAULT_CODEX_WEEKLY_BUDGET_PAUSE_POLL_SECONDS = 300.0
MAX_STUCK_RECOVERY_ATTEMPTS = DEFAULT_MAINLINE_STUCK_RECOVERY_ATTEMPTS
MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS = DEFAULT_BRANCH_STUCK_RECOVERY_ATTEMPTS
BRANCH_FRONTIER_REPLACEMENT_MIN_CONFIDENCE = DEFAULT_BRANCH_FRONTIER_REPLACEMENT_MIN_CONFIDENCE
BRANCH_PROPOSAL_COOLDOWN_REVIEWS = DEFAULT_BRANCH_PROPOSAL_COOLDOWN_REVIEWS
AGENT_CLI_RETRY_DELAYS_SECONDS = DEFAULT_AGENT_CLI_RETRY_DELAYS_SECONDS
PHASE_PROOF_COMPLETE_STYLE_CLEANUP = "proof_complete_style_cleanup"
CHAT_EVENT_CYCLE_CHUNK_SIZE = 25
PHASES: Tuple[str, ...] = (
    "paper_check",
    "planning",
    "theorem_stating",
    "proof_formalization",
    PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
)
WORKER_STATUSES: Tuple[str, ...] = ("NOT_STUCK", "STUCK", "DONE", "NEED_INPUT")
REVIEWER_DECISIONS: Tuple[str, ...] = ("CONTINUE", "ADVANCE_PHASE", "STUCK", "NEED_INPUT", "DONE")
BRANCH_STRATEGY_DECISIONS: Tuple[str, ...] = ("NO_BRANCH", "BRANCH")
BRANCH_SELECTION_DECISIONS: Tuple[str, ...] = ("CONTINUE_BRANCHING", "SELECT_BRANCH")
BRANCH_REPLACEMENT_DECISIONS: Tuple[str, ...] = ("KEEP_FRONTIER", "REPLACE_WITH_PROPOSAL")
SORRY_MODES: Tuple[str, ...] = ("default", "allowed")
THEOREM_FRONTIER_PHASES: Tuple[str, ...] = ("off", "phase0", "full")
THEOREM_FRONTIER_ACTIONS: Tuple[str, ...] = ("CLOSE", "EXPAND", "REFUTE_REPLACE")
THEOREM_FRONTIER_OUTCOMES: Tuple[str, ...] = (
    "CLOSED",
    "EXPANDED",
    "REFUTED_REPLACED",
    "STILL_OPEN",
    "NO_FRONTIER_PROGRESS",
)
THEOREM_FRONTIER_NODE_KINDS: Tuple[str, ...] = (
    "paper",
    "paper_faithful_reformulation",
    "support",
    "packaging",
    "exploratory",
)
THEOREM_FRONTIER_NODE_STATUSES: Tuple[str, ...] = (
    "proposed",
    "open",
    "active",
    "closed",
    "refuted",
    "replaced",
    "frozen",
)
THEOREM_FRONTIER_CLOSURE_MODES: Tuple[str, ...] = (
    "leaf",
    "all_children",
    "all_cases",
    "any_child",
)
THEOREM_FRONTIER_EDGE_TYPES: Tuple[str, ...] = (
    "reduction",
    "case_split",
    "all_of",
    "any_of",
    "replacement",
    "equivalence",
    "strengthening",
)
THEOREM_FRONTIER_PAPER_DECISIONS: Tuple[str, ...] = ("APPROVE", "APPROVE_WITH_CAVEAT", "REJECT")
THEOREM_FRONTIER_PAPER_CLASSIFICATIONS: Tuple[str, ...] = (
    "paper_exact",
    "paper_faithful_reformulation",
    "conservative_strengthening",
    "exploratory_detour",
    "paper_incompatible",
)
THEOREM_FRONTIER_CONE_PURITY_LEVELS: Tuple[str, ...] = ("HIGH", "MEDIUM", "LOW")
THEOREM_FRONTIER_FAILED_CLOSE_THRESHOLD = 2
THEOREM_FRONTIER_BLOCKER_CLUSTER_THRESHOLD = 5
THEOREM_FRONTIER_LOW_CONE_PURITY_THRESHOLD = 2
SUPERVISOR_TASKS_START = "<!-- SUPERVISOR_TASKS:START -->"
SUPERVISOR_TASKS_END = "<!-- SUPERVISOR_TASKS:END -->"
SUPERVISOR_GITIGNORE_START = "# >>> lagent-supervisor >>>"
SUPERVISOR_GITIGNORE_END = "# <<< lagent-supervisor <<<"
DEFAULT_BRANCH_EVALUATION_CYCLES = 20
DEFAULT_BRANCH_POLL_SECONDS = 300.0


class SupervisorError(RuntimeError):
    pass


@dataclass
class ProviderConfig:
    provider: str
    model: Optional[str]
    extra_args: List[str]


@dataclass
class TmuxConfig:
    session_name: str
    dashboard_window_name: str
    kill_windows_after_capture: bool


@dataclass
class WorkflowConfig:
    start_phase: str
    sorry_mode: str
    paper_tex_path: Optional[Path]
    approved_axioms_path: Path
    human_input_path: Path
    input_request_path: Path
    theorem_frontier_phase: str


@dataclass
class ChatConfig:
    root_dir: Path
    repo_name: str
    project_name: str
    public_base_url: str


@dataclass
class GitConfig:
    remote_url: Optional[str]
    remote_name: str
    branch: str
    author_name: str
    author_email: str


@dataclass
class BranchingConfig:
    max_current_branches: int = 2
    evaluation_cycle_budget: int = DEFAULT_BRANCH_EVALUATION_CYCLES
    poll_seconds: float = DEFAULT_BRANCH_POLL_SECONDS


@dataclass(frozen=True)
class StuckRecoveryPolicy:
    mainline_max_attempts: int = DEFAULT_MAINLINE_STUCK_RECOVERY_ATTEMPTS
    branch_max_attempts: int = DEFAULT_BRANCH_STUCK_RECOVERY_ATTEMPTS


@dataclass(frozen=True)
class BranchingPolicy:
    evaluation_cycle_budget: int = DEFAULT_BRANCH_EVALUATION_CYCLES
    poll_seconds: float = DEFAULT_BRANCH_POLL_SECONDS
    proposal_cooldown_reviews: int = DEFAULT_BRANCH_PROPOSAL_COOLDOWN_REVIEWS
    replacement_min_confidence: float = DEFAULT_BRANCH_FRONTIER_REPLACEMENT_MIN_CONFIDENCE
    selection_recheck_increments_reviews: Tuple[int, ...] = DEFAULT_BRANCH_SELECTION_RECHECK_INCREMENTS_REVIEWS


@dataclass(frozen=True)
class TimingPolicy:
    sleep_seconds: float = 1.0
    agent_retry_delays_seconds: Tuple[float, ...] = DEFAULT_AGENT_CLI_RETRY_DELAYS_SECONDS


@dataclass(frozen=True)
class CodexBudgetPausePolicy:
    weekly_percent_left_threshold: float = DEFAULT_CODEX_WEEKLY_BUDGET_PAUSE_THRESHOLD_PERCENT_LEFT
    poll_seconds: float = DEFAULT_CODEX_WEEKLY_BUDGET_PAUSE_POLL_SECONDS


@dataclass(frozen=True)
class PromptNotesPolicy:
    worker: str = ""
    reviewer: str = ""
    branching: str = ""


@dataclass(frozen=True)
class Policy:
    stuck_recovery: StuckRecoveryPolicy
    branching: BranchingPolicy
    timing: TimingPolicy
    codex_budget_pause: CodexBudgetPausePolicy
    prompt_notes: PromptNotesPolicy


@dataclass
class Config:
    repo_path: Path
    goal_file: Path
    state_dir: Path
    worker: ProviderConfig
    reviewer: ProviderConfig
    tmux: TmuxConfig
    workflow: WorkflowConfig
    chat: ChatConfig
    git: GitConfig
    max_cycles: int
    sleep_seconds: float
    startup_timeout_seconds: float
    burst_timeout_seconds: float
    branching: BranchingConfig = field(default_factory=BranchingConfig)
    policy_path: Optional[Path] = None


class JsonFile:
    @staticmethod
    def load(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def dump(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)


def coerce_int(value: Any, field_name: str, *, minimum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SupervisorError(f"{field_name} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise SupervisorError(f"{field_name} must be at least {minimum}")
    return parsed


def coerce_float(
    value: Any,
    field_name: str,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    strictly_positive: bool = False,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SupervisorError(f"{field_name} must be numeric") from exc
    if strictly_positive and parsed <= 0:
        raise SupervisorError(f"{field_name} must be positive")
    if minimum is not None and parsed < minimum:
        raise SupervisorError(f"{field_name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise SupervisorError(f"{field_name} must be at most {maximum}")
    return parsed


def normalize_theorem_frontier_phase(value: Any) -> str:
    normalized = str(value or "full").strip().lower()
    if normalized not in THEOREM_FRONTIER_PHASES:
        raise SupervisorError(
            f"Unsupported workflow.theorem_frontier_phase: {value!r}. "
            f"Expected one of {list(THEOREM_FRONTIER_PHASES)}."
        )
    return normalized


def default_policy_for_config(config: Config) -> Policy:
    return Policy(
        stuck_recovery=StuckRecoveryPolicy(),
        branching=BranchingPolicy(
            evaluation_cycle_budget=config.branching.evaluation_cycle_budget,
            poll_seconds=config.branching.poll_seconds,
            proposal_cooldown_reviews=DEFAULT_BRANCH_PROPOSAL_COOLDOWN_REVIEWS,
            replacement_min_confidence=DEFAULT_BRANCH_FRONTIER_REPLACEMENT_MIN_CONFIDENCE,
            selection_recheck_increments_reviews=DEFAULT_BRANCH_SELECTION_RECHECK_INCREMENTS_REVIEWS,
        ),
        timing=TimingPolicy(
            sleep_seconds=config.sleep_seconds,
            agent_retry_delays_seconds=DEFAULT_AGENT_CLI_RETRY_DELAYS_SECONDS,
        ),
        codex_budget_pause=CodexBudgetPausePolicy(),
        prompt_notes=PromptNotesPolicy(),
    )


def policy_to_raw_dict(policy: Policy) -> Dict[str, Any]:
    return {
        "stuck_recovery": {
            "mainline_max_attempts": policy.stuck_recovery.mainline_max_attempts,
            "branch_max_attempts": policy.stuck_recovery.branch_max_attempts,
        },
        "branching": {
            "evaluation_cycle_budget": policy.branching.evaluation_cycle_budget,
            "poll_seconds": policy.branching.poll_seconds,
            "proposal_cooldown_reviews": policy.branching.proposal_cooldown_reviews,
            "replacement_min_confidence": policy.branching.replacement_min_confidence,
            "selection_recheck_increments_reviews": list(policy.branching.selection_recheck_increments_reviews),
        },
        "timing": {
            "sleep_seconds": policy.timing.sleep_seconds,
            "agent_retry_delays_seconds": list(policy.timing.agent_retry_delays_seconds),
        },
        "codex_budget_pause": {
            "weekly_percent_left_threshold": policy.codex_budget_pause.weekly_percent_left_threshold,
            "poll_seconds": policy.codex_budget_pause.poll_seconds,
        },
        "prompt_notes": {
            "worker": policy.prompt_notes.worker,
            "reviewer": policy.prompt_notes.reviewer,
            "branching": policy.prompt_notes.branching,
        },
    }


def parse_policy(raw: Any, defaults: Policy, *, path: Path) -> Policy:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SupervisorError(f"Policy file must contain a JSON object: {path}")

    stuck_block = raw.get("stuck_recovery", {})
    if not isinstance(stuck_block, dict):
        raise SupervisorError(f"Policy field stuck_recovery must be an object: {path}")
    branching_block = raw.get("branching", {})
    if not isinstance(branching_block, dict):
        raise SupervisorError(f"Policy field branching must be an object: {path}")
    timing_block = raw.get("timing", {})
    if not isinstance(timing_block, dict):
        raise SupervisorError(f"Policy field timing must be an object: {path}")
    codex_budget_pause_block = raw.get("codex_budget_pause", {})
    if not isinstance(codex_budget_pause_block, dict):
        raise SupervisorError(f"Policy field codex_budget_pause must be an object: {path}")
    prompt_notes_block = raw.get("prompt_notes", {})
    if not isinstance(prompt_notes_block, dict):
        raise SupervisorError(f"Policy field prompt_notes must be an object: {path}")

    retry_delays_raw = timing_block.get(
        "agent_retry_delays_seconds",
        list(defaults.timing.agent_retry_delays_seconds),
    )
    if not isinstance(retry_delays_raw, list):
        raise SupervisorError(f"Policy field timing.agent_retry_delays_seconds must be a list: {path}")
    retry_delays = tuple(
        coerce_float(delay, "policy.timing.agent_retry_delays_seconds[]", strictly_positive=True)
        for delay in retry_delays_raw
    )
    recheck_increments_raw = branching_block.get(
        "selection_recheck_increments_reviews",
        list(defaults.branching.selection_recheck_increments_reviews),
    )
    if not isinstance(recheck_increments_raw, list):
        raise SupervisorError(f"Policy field branching.selection_recheck_increments_reviews must be a list: {path}")
    recheck_increments = tuple(
        coerce_int(value, "policy.branching.selection_recheck_increments_reviews[]", minimum=1)
        for value in recheck_increments_raw
    )
    if not recheck_increments:
        raise SupervisorError(
            f"Policy field branching.selection_recheck_increments_reviews must contain at least one positive integer: {path}"
        )

    return Policy(
        stuck_recovery=StuckRecoveryPolicy(
            mainline_max_attempts=coerce_int(
                stuck_block.get("mainline_max_attempts", defaults.stuck_recovery.mainline_max_attempts),
                "policy.stuck_recovery.mainline_max_attempts",
                minimum=1,
            ),
            branch_max_attempts=coerce_int(
                stuck_block.get("branch_max_attempts", defaults.stuck_recovery.branch_max_attempts),
                "policy.stuck_recovery.branch_max_attempts",
                minimum=1,
            ),
        ),
        branching=BranchingPolicy(
            evaluation_cycle_budget=coerce_int(
                branching_block.get("evaluation_cycle_budget", defaults.branching.evaluation_cycle_budget),
                "policy.branching.evaluation_cycle_budget",
                minimum=1,
            ),
            poll_seconds=coerce_float(
                branching_block.get("poll_seconds", defaults.branching.poll_seconds),
                "policy.branching.poll_seconds",
                strictly_positive=True,
            ),
            proposal_cooldown_reviews=coerce_int(
                branching_block.get(
                    "proposal_cooldown_reviews",
                    defaults.branching.proposal_cooldown_reviews,
                ),
                "policy.branching.proposal_cooldown_reviews",
                minimum=0,
            ),
            replacement_min_confidence=coerce_float(
                branching_block.get(
                    "replacement_min_confidence",
                    defaults.branching.replacement_min_confidence,
                ),
                "policy.branching.replacement_min_confidence",
                minimum=0.0,
                maximum=1.0,
            ),
            selection_recheck_increments_reviews=recheck_increments,
        ),
        timing=TimingPolicy(
            sleep_seconds=coerce_float(
                timing_block.get("sleep_seconds", defaults.timing.sleep_seconds),
                "policy.timing.sleep_seconds",
                minimum=0.0,
            ),
            agent_retry_delays_seconds=retry_delays,
        ),
        codex_budget_pause=CodexBudgetPausePolicy(
            weekly_percent_left_threshold=coerce_float(
                codex_budget_pause_block.get(
                    "weekly_percent_left_threshold",
                    defaults.codex_budget_pause.weekly_percent_left_threshold,
                ),
                "policy.codex_budget_pause.weekly_percent_left_threshold",
                minimum=0.0,
                maximum=100.0,
            ),
            poll_seconds=coerce_float(
                codex_budget_pause_block.get(
                    "poll_seconds",
                    defaults.codex_budget_pause.poll_seconds,
                ),
                "policy.codex_budget_pause.poll_seconds",
                strictly_positive=True,
            ),
        ),
        prompt_notes=PromptNotesPolicy(
            worker=str(prompt_notes_block.get("worker", defaults.prompt_notes.worker)).strip(),
            reviewer=str(prompt_notes_block.get("reviewer", defaults.prompt_notes.reviewer)).strip(),
            branching=str(prompt_notes_block.get("branching", defaults.prompt_notes.branching)).strip(),
        ),
    )


def effective_policy_from_state(state: Dict[str, Any], defaults: Policy) -> Policy:
    policy_meta = state.get("policy")
    effective = policy_meta.get("effective") if isinstance(policy_meta, dict) else None
    try:
        return parse_policy(effective, defaults, path=Path("<state-policy>"))
    except SupervisorError:
        return defaults


class PolicyManager:
    def __init__(self, config: Config):
        self.config = config
        self.path = resolved_policy_path(config)
        self.defaults = default_policy_for_config(config)
        self._policy: Optional[Policy] = None
        self._mtime_ns: Optional[int] = None
        self._digest: Optional[str] = None
        self._last_warning_key: Optional[Tuple[Optional[int], str]] = None

    def current(self, state: Optional[Dict[str, Any]] = None) -> Policy:
        return self.reload(state=state, force=False)

    def _state_payload(self, policy: Policy, *, warning: str = "") -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "mtime_ns": self._mtime_ns,
            "sha256": self._digest,
            "loaded_at": timestamp_now(),
            "warning": warning,
            "effective": policy_to_raw_dict(policy),
        }

    def reload(self, state: Optional[Dict[str, Any]] = None, *, force: bool = False, persist: bool = False) -> Policy:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            JsonFile.dump(self.path, policy_to_raw_dict(self.defaults))

        stat = self.path.stat()
        if not force and self._policy is not None and self._mtime_ns == stat.st_mtime_ns:
            if state is not None and not isinstance(state.get("policy"), dict):
                state["policy"] = self._state_payload(self._policy)
                if persist:
                    JsonFile.dump(self.config.state_dir / "state.json", state)
            return self._policy

        try:
            raw_text = self.path.read_text(encoding="utf-8")
            raw = json.loads(raw_text)
            policy = parse_policy(raw, self.defaults, path=self.path)
            self._mtime_ns = stat.st_mtime_ns
            self._digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]
            self._policy = policy
            self._last_warning_key = None
            if state is not None:
                state["policy"] = self._state_payload(policy)
                if persist:
                    JsonFile.dump(self.config.state_dir / "state.json", state)
            return policy
        except (SupervisorError, json.JSONDecodeError) as exc:
            if self._policy is None:
                raise SupervisorError(f"Could not load policy file {self.path}: {exc}") from exc
            warning_key = (stat.st_mtime_ns, str(exc))
            if self._last_warning_key != warning_key:
                print(f"WARNING: Could not reload policy file {self.path}: {exc}. Keeping the last known good policy.")
                self._last_warning_key = warning_key
            if state is not None:
                state["policy"] = self._state_payload(self._policy, warning=str(exc))
                if persist:
                    JsonFile.dump(self.config.state_dir / "state.json", state)
            return self._policy


def codex_session_logs_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def recent_codex_session_log_paths(*, limit: int = 10) -> List[Path]:
    root = codex_session_logs_root()
    if not root.exists():
        return []
    files = [path for path in root.rglob("*.jsonl") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    return files[:limit]


def read_text_tail(path: Path, *, max_bytes: int = 1_000_000) -> str:
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(max(0, size - max_bytes))
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def latest_codex_token_count_event_in_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        tail_text = read_text_tail(path)
    except OSError:
        return None
    for line in reversed(tail_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        secondary = rate_limits.get("secondary")
        if not isinstance(secondary, dict):
            continue
        return record
    return None


def latest_codex_weekly_budget_status() -> Optional[Dict[str, Any]]:
    latest_record: Optional[Dict[str, Any]] = None
    latest_path: Optional[Path] = None
    for path in recent_codex_session_log_paths():
        record = latest_codex_token_count_event_in_file(path)
        if record is None:
            continue
        timestamp = str(record.get("timestamp") or "")
        if latest_record is None or timestamp > str(latest_record.get("timestamp") or ""):
            latest_record = record
            latest_path = path
    if latest_record is None or latest_path is None:
        return None
    payload = latest_record.get("payload")
    if not isinstance(payload, dict):
        return None
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None
    secondary = rate_limits.get("secondary")
    if not isinstance(secondary, dict):
        return None
    used_percent = float(secondary.get("used_percent") or 0.0)
    percent_left = max(0.0, 100.0 - used_percent)
    return {
        "timestamp": str(latest_record.get("timestamp") or ""),
        "source_path": str(latest_path),
        "plan_type": rate_limits.get("plan_type"),
        "used_percent": used_percent,
        "percent_left": percent_left,
        "window_minutes": int(secondary.get("window_minutes") or 0),
        "resets_at": secondary.get("resets_at"),
    }


def set_codex_budget_pause_state(
    config: Config,
    state: Dict[str, Any],
    *,
    active: bool,
    phase: str,
    stage_label: str,
    status: Optional[Dict[str, Any]],
    threshold_percent_left: float,
) -> None:
    current = state.get("codex_budget_pause")
    if active:
        payload = {
            "active": True,
            "phase": phase,
            "cycle": int(state.get("cycle", 0) or 0),
            "stage_label": stage_label,
            "threshold_percent_left": threshold_percent_left,
            "percent_left": None if status is None else float(status.get("percent_left") or 0.0),
            "used_percent": None if status is None else float(status.get("used_percent") or 0.0),
            "window_minutes": None if status is None else int(status.get("window_minutes") or 0),
            "resets_at": None if status is None else status.get("resets_at"),
            "source_path": None if status is None else str(status.get("source_path") or ""),
            "checked_at": timestamp_now(),
        }
        if current != payload:
            state["codex_budget_pause"] = payload
            save_state(config, state)
        return

    if current is not None:
        state["codex_budget_pause"] = None
        save_state(config, state)


def wait_for_codex_weekly_budget_if_needed(
    config: Config,
    state: Dict[str, Any],
    *,
    phase: str,
    stage_label: str,
) -> None:
    policy_manager = PolicyManager(config)
    announced_pause = False
    while True:
        policy = policy_manager.reload(state=state, persist=True)
        threshold = codex_weekly_budget_pause_threshold_percent_left(config, policy)
        if threshold <= 0:
            set_codex_budget_pause_state(
                config,
                state,
                active=False,
                phase=phase,
                stage_label=stage_label,
                status=None,
                threshold_percent_left=threshold,
            )
            return

        status = latest_codex_weekly_budget_status()
        if status is None:
            set_codex_budget_pause_state(
                config,
                state,
                active=False,
                phase=phase,
                stage_label=stage_label,
                status=None,
                threshold_percent_left=threshold,
            )
            return

        percent_left = float(status.get("percent_left") or 0.0)
        if percent_left >= threshold:
            if announced_pause:
                print(
                    "Resuming after Codex weekly budget pause: "
                    f"{percent_left:.1f}% left (threshold {threshold:.1f}%)."
                )
            set_codex_budget_pause_state(
                config,
                state,
                active=False,
                phase=phase,
                stage_label=stage_label,
                status=status,
                threshold_percent_left=threshold,
            )
            return

        set_codex_budget_pause_state(
            config,
            state,
            active=True,
            phase=phase,
            stage_label=stage_label,
            status=status,
            threshold_percent_left=threshold,
        )
        if not announced_pause:
            print(
                "Pausing before launching a new Codex burst because weekly budget is low: "
                f"{percent_left:.1f}% left (threshold {threshold:.1f}%)."
            )
            announced_pause = True
        print(
            f"Rechecking Codex weekly budget in "
            f"{codex_weekly_budget_pause_poll_seconds(config, policy):.0f}s "
            f"(source: {status.get('source_path')})."
        )
        time.sleep(codex_weekly_budget_pause_poll_seconds(config, policy))

def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    raw = JsonFile.load(path, None)
    if raw is None:
        raise SupervisorError(f"Config file not found: {path}")

    repo_path = Path(raw["repo_path"]).expanduser().resolve()
    if not repo_path.exists():
        raise SupervisorError(f"repo_path does not exist: {repo_path}")

    goal_file = Path(raw["goal_file"])
    if not goal_file.is_absolute():
        goal_file = (repo_path / goal_file).resolve()

    state_dir = Path(raw.get("state_dir", repo_path / ".agent-supervisor")).expanduser()
    if not state_dir.is_absolute():
        state_dir = (repo_path / state_dir).resolve()

    def provider_cfg(key: str) -> ProviderConfig:
        block = raw[key]
        provider = str(block["provider"]).strip().lower()
        if provider not in {"claude", "codex", "gemini"}:
            raise SupervisorError(f"Unsupported provider for {key}: {provider}")
        return ProviderConfig(
            provider=provider,
            model=block.get("model"),
            extra_args=list(block.get("extra_args", [])),
        )

    tmux_block = raw.get("tmux", {})
    tmux_cfg = TmuxConfig(
        session_name=sanitize_tmux_session_name(str(tmux_block.get("session_name", "lean-agents"))),
        dashboard_window_name=str(tmux_block.get("dashboard_window_name", "dashboard")),
        kill_windows_after_capture=bool(tmux_block.get("kill_windows_after_capture", True)),
    )

    workflow_block = raw.get("workflow", {})
    start_phase = normalize_phase_name(str(workflow_block.get("start_phase", "proof_formalization")))
    if start_phase not in PHASES:
        raise SupervisorError(f"Unsupported workflow.start_phase: {start_phase}")
    sorry_mode = str(workflow_block.get("sorry_mode", "default")).strip().lower()
    if sorry_mode not in SORRY_MODES:
        raise SupervisorError(f"Unsupported workflow.sorry_mode: {sorry_mode}")

    paper_tex_raw = workflow_block.get("paper_tex_path")
    paper_tex_path: Optional[Path] = None
    if paper_tex_raw:
        paper_tex_path = Path(str(paper_tex_raw))
        if not paper_tex_path.is_absolute():
            paper_tex_path = (repo_path / paper_tex_path).resolve()
        else:
            paper_tex_path = paper_tex_path.expanduser().resolve()
        try:
            paper_tex_path.relative_to(repo_path)
        except ValueError as exc:
            raise SupervisorError(f"workflow.paper_tex_path must live under repo_path: {paper_tex_path}") from exc

    if start_phase in {"paper_check", "planning", "theorem_stating"} and paper_tex_path is None:
        raise SupervisorError(f"workflow.paper_tex_path is required when start_phase={start_phase}")

    approved_axioms_path = Path(workflow_block.get("approved_axioms_path", repo_path / "APPROVED_AXIOMS.json")).expanduser()
    if not approved_axioms_path.is_absolute():
        approved_axioms_path = (repo_path / approved_axioms_path).resolve()

    human_input_path = Path(workflow_block.get("human_input_path", repo_path / "HUMAN_INPUT.md")).expanduser()
    if not human_input_path.is_absolute():
        human_input_path = (repo_path / human_input_path).resolve()

    input_request_path = Path(workflow_block.get("input_request_path", repo_path / "INPUT_REQUEST.md")).expanduser()
    if not input_request_path.is_absolute():
        input_request_path = (repo_path / input_request_path).resolve()

    chat_block = raw.get("chat", {})
    chat_root_dir = Path(chat_block.get("root_dir", Path.home() / "lagent-chats")).expanduser()
    if not chat_root_dir.is_absolute():
        chat_root_dir = (repo_path / chat_root_dir).resolve()
    chat_repo_name = sanitize_repo_name(str(chat_block.get("repo_name", repo_path.name)))
    chat_project_name = sanitize_repo_name(str(chat_block.get("project_name", chat_repo_name)))
    chat_public_base_url = str(chat_block.get("public_base_url", DEFAULT_CHAT_BASE_URL)).strip() or DEFAULT_CHAT_BASE_URL
    if not chat_public_base_url.endswith("/"):
        chat_public_base_url += "/"

    git_block = raw.get("git", {})
    git_remote_raw = git_block.get("remote_url")
    git_remote_url = str(git_remote_raw).strip() if git_remote_raw is not None else None
    if git_remote_url == "":
        git_remote_url = None
    git_remote_name = str(git_block.get("remote_name", "origin")).strip() or "origin"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", git_remote_name):
        raise SupervisorError(f"Unsupported git.remote_name: {git_remote_name}")
    git_branch = str(git_block.get("branch", "main")).strip() or "main"
    git_author_name = str(git_block.get("author_name", getpass.getuser())).strip() or getpass.getuser()
    git_author_email = str(git_block.get("author_email", default_git_author_email(git_author_name))).strip()
    if not git_author_email:
        git_author_email = default_git_author_email(git_author_name)

    branching_block = raw.get("branching", {})
    max_current_branches = int(branching_block.get("max_current_branches", 2))
    if max_current_branches < 1:
        raise SupervisorError("branching.max_current_branches must be at least 1")
    evaluation_cycle_budget = int(
        branching_block.get("evaluation_cycle_budget", DEFAULT_BRANCH_EVALUATION_CYCLES)
    )
    if evaluation_cycle_budget < 1:
        raise SupervisorError("branching.evaluation_cycle_budget must be at least 1")
    poll_seconds = float(branching_block.get("poll_seconds", DEFAULT_BRANCH_POLL_SECONDS))
    if poll_seconds <= 0:
        raise SupervisorError("branching.poll_seconds must be positive")

    policy_path = Path(raw.get("policy_path", path.with_suffix(".policy.json"))).expanduser()
    if not policy_path.is_absolute():
        policy_path = (path.parent / policy_path).resolve()

    return Config(
        repo_path=repo_path,
        goal_file=goal_file,
        state_dir=state_dir,
        worker=provider_cfg("worker"),
        reviewer=provider_cfg("reviewer"),
        tmux=tmux_cfg,
        workflow=WorkflowConfig(
            start_phase=start_phase,
            sorry_mode=sorry_mode,
            paper_tex_path=paper_tex_path,
            approved_axioms_path=approved_axioms_path,
            human_input_path=human_input_path,
            input_request_path=input_request_path,
            theorem_frontier_phase=normalize_theorem_frontier_phase(
                workflow_block.get("theorem_frontier_phase", "full")
            ),
        ),
        chat=ChatConfig(
            root_dir=chat_root_dir,
            repo_name=chat_repo_name,
            project_name=chat_project_name,
            public_base_url=chat_public_base_url,
        ),
        git=GitConfig(
            remote_url=git_remote_url,
            remote_name=git_remote_name,
            branch=git_branch,
            author_name=git_author_name,
            author_email=git_author_email,
        ),
        max_cycles=int(raw.get("max_cycles", 0)),
        sleep_seconds=float(raw.get("sleep_seconds", 1.0)),
        startup_timeout_seconds=float(raw.get("startup_timeout_seconds", 15.0)),
        burst_timeout_seconds=float(raw.get("burst_timeout_seconds", 7200.0)),
        branching=BranchingConfig(
            max_current_branches=max_current_branches,
            evaluation_cycle_budget=evaluation_cycle_budget,
            poll_seconds=poll_seconds,
        ),
        policy_path=policy_path,
    )


def resolved_policy_path(config: Config) -> Path:
    if config.policy_path is not None:
        return config.policy_path.expanduser().resolve()
    return (config.state_dir / "policy.json").resolve()


def check_dependencies(config: Config) -> None:
    required = ["tmux"]
    if config.git.remote_url or branching_enabled(config):
        required.append("git")
    for exe in required:
        if subprocess.run(["bash", "-lc", f"command -v {shlex.quote(exe)} >/dev/null 2>&1"], check=False).returncode != 0:
            raise SupervisorError(f"Required executable not found on PATH: {exe}")


def branching_enabled(config: Config) -> bool:
    return config.branching.max_current_branches > 1


def effective_policy(config: Config, state: Optional[Dict[str, Any]] = None, policy: Optional[Policy] = None) -> Policy:
    if policy is not None:
        return policy
    defaults = default_policy_for_config(config)
    if state is not None:
        return effective_policy_from_state(state, defaults)
    return defaults


def parent_branch_capacity(state: Dict[str, Any], config: Optional[Config] = None) -> int:
    value = state.get("branch_parent_max_current_branches")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    if parsed > 1:
        return parsed
    if config is not None:
        return max(1, int(config.branching.max_current_branches))
    return 1


def can_propose_branch_replacement(state: Dict[str, Any], config: Optional[Config] = None) -> bool:
    return parent_branch_capacity(state, config) > 1


def branch_review_budget(config: Config, policy: Optional[Policy] = None) -> int:
    return effective_policy(config, policy=policy).branching.evaluation_cycle_budget


def branch_poll_seconds(config: Config, policy: Optional[Policy] = None) -> float:
    return effective_policy(config, policy=policy).branching.poll_seconds


def branch_proposal_cooldown_reviews(config: Config, policy: Optional[Policy] = None) -> int:
    return effective_policy(config, policy=policy).branching.proposal_cooldown_reviews


def branch_replacement_min_confidence(config: Config, policy: Optional[Policy] = None) -> float:
    return effective_policy(config, policy=policy).branching.replacement_min_confidence


def branch_selection_recheck_increments_reviews(
    config: Config, policy: Optional[Policy] = None
) -> Tuple[int, ...]:
    return effective_policy(config, policy=policy).branching.selection_recheck_increments_reviews


def branch_selection_continue_count(
    config: Config,
    episode: Dict[str, Any],
    policy: Optional[Policy] = None,
) -> int:
    explicit = episode.get("selection_continue_count")
    try:
        if explicit is not None:
            return max(0, int(explicit))
    except (TypeError, ValueError):
        pass
    base_target = int(episode.get("base_review_count", 0) or 0) + branch_review_budget(config, policy)
    current_target = int(episode.get("next_selection_review_target", base_target) or 0)
    extra = current_target - base_target
    if extra <= 0:
        return 0
    budget = branch_review_budget(config, policy)
    if budget > 0 and extra % budget == 0:
        return extra // budget
    increments = branch_selection_recheck_increments_reviews(config, policy)
    total = 0
    count = 0
    while total < extra:
        total += increments[min(count, len(increments) - 1)]
        count += 1
    return count


def branch_selection_target_for_continue_count(
    config: Config,
    episode: Dict[str, Any],
    continue_count: int,
    policy: Optional[Policy] = None,
) -> int:
    target = int(episode.get("base_review_count", 0) or 0) + branch_review_budget(config, policy)
    increments = branch_selection_recheck_increments_reviews(config, policy)
    for idx in range(max(0, continue_count)):
        target += increments[min(idx, len(increments) - 1)]
    return target


def normalize_branch_episode_selection_schedule(
    config: Config,
    state: Dict[str, Any],
    episode: Dict[str, Any],
    policy: Optional[Policy] = None,
) -> None:
    continue_count = branch_selection_continue_count(config, episode, policy)
    normalized_target = branch_selection_target_for_continue_count(config, episode, continue_count, policy)
    changed = False
    if int(episode.get("selection_continue_count", -1) or -1) != continue_count:
        episode["selection_continue_count"] = continue_count
        changed = True
    if int(episode.get("next_selection_review_target", normalized_target) or 0) != normalized_target:
        episode["next_selection_review_target"] = normalized_target
        changed = True
    current_budget = int(episode.get("evaluation_cycle_budget", branch_review_budget(config, policy)) or 0)
    expected_budget = branch_review_budget(config, policy)
    if current_budget != expected_budget:
        episode["evaluation_cycle_budget"] = expected_budget
        changed = True
    if changed:
        state["active_branch_episode"] = episode
        save_state(config, state)


def supervisor_sleep_seconds(config: Config, policy: Optional[Policy] = None) -> float:
    return effective_policy(config, policy=policy).timing.sleep_seconds


def agent_retry_delays_seconds(config: Config, policy: Optional[Policy] = None) -> Tuple[float, ...]:
    return effective_policy(config, policy=policy).timing.agent_retry_delays_seconds


def codex_weekly_budget_pause_threshold_percent_left(
    config: Config, policy: Optional[Policy] = None
) -> float:
    return effective_policy(config, policy=policy).codex_budget_pause.weekly_percent_left_threshold


def codex_weekly_budget_pause_poll_seconds(config: Config, policy: Optional[Policy] = None) -> float:
    return effective_policy(config, policy=policy).codex_budget_pause.poll_seconds


def phase_index(phase: str) -> int:
    return PHASES.index(phase)


def is_style_cleanup_phase(phase: str) -> bool:
    return phase == PHASE_PROOF_COMPLETE_STYLE_CLEANUP


def normalize_phase_name(value: str) -> str:
    phase = value.strip().lower()
    if phase in {"proof complete - style cleanup", "proof complete style cleanup"}:
        return PHASE_PROOF_COMPLETE_STYLE_CLEANUP
    return phase


def next_phase(phase: str) -> Optional[str]:
    idx = phase_index(phase)
    if idx + 1 >= len(PHASES):
        return None
    return PHASES[idx + 1]


def phase_uses_paper_notes(phase: str) -> bool:
    return phase in {"paper_check", "planning", "theorem_stating", "proof_formalization", PHASE_PROOF_COMPLETE_STYLE_CLEANUP}


def phase_uses_plan(phase: str) -> bool:
    return phase in {"planning", "theorem_stating", "proof_formalization", PHASE_PROOF_COMPLETE_STYLE_CLEANUP}


def phase_uses_statement_files(phase: str) -> bool:
    return phase in {"theorem_stating", "proof_formalization", PHASE_PROOF_COMPLETE_STYLE_CLEANUP}


def theorem_frontier_phase(config: Config) -> str:
    return config.workflow.theorem_frontier_phase


def theorem_frontier_enabled(config: Config, phase: str) -> bool:
    return theorem_frontier_phase(config) in {"phase0", "full"} and phase == "proof_formalization"


def theorem_frontier_phase0_enabled(config: Config, phase: str) -> bool:
    return theorem_frontier_phase(config) == "phase0" and phase == "proof_formalization"


def theorem_frontier_full_enabled(config: Config, phase: str) -> bool:
    return theorem_frontier_phase(config) == "full" and phase == "proof_formalization"


def theorem_frontier_state_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier.json"


def theorem_frontier_history_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_history.jsonl"


def theorem_frontier_worker_update_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_update.json"


def theorem_frontier_review_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_review.json"


def theorem_frontier_paper_verifier_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_paper_verifier.json"


def paper_main_results_manifest_path(config: Config) -> Path:
    return config.state_dir / "paper_main_results.json"


def current_phase(config: Config, state: Dict[str, Any]) -> str:
    phase = normalize_phase_name(str(state.get("phase") or config.workflow.start_phase))
    if phase not in PHASES:
        raise SupervisorError(f"Invalid workflow phase in state: {phase}")
    state["phase"] = phase
    return phase


def sanitize_repo_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return cleaned or "repo"


def sanitize_tmux_session_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
    return cleaned or "lean-agents"


def default_git_author_email(author_name: str) -> str:
    local_part = re.sub(r"[^A-Za-z0-9._+-]+", "-", author_name.strip()).strip("-.")
    if not local_part:
        local_part = "leanagent"
    host = socket.getfqdn() or socket.gethostname() or "localhost"
    return f"{local_part}@{host}"


def relative_repo_label(config: Config, path: Path) -> str:
    try:
        return f"repo/{path.resolve().relative_to(config.repo_path).as_posix()}"
    except ValueError:
        return str(path)


def ensure_approved_axioms_file(config: Config) -> None:
    if config.workflow.approved_axioms_path.exists():
        return
    JsonFile.dump(config.workflow.approved_axioms_path, {"approved_axioms": []})


def build_tasks_scaffold() -> str:
    return textwrap.dedent(
        f"""\
        # Tasks

        {SUPERVISOR_TASKS_START}
        {SUPERVISOR_TASKS_END}

        ## Worker Tasks
        - [ ] Add the next concrete task here.

        ## Completed
        - [ ] Move completed items here or check them off in place.
        """
    )


def supervisor_gitignore_entries(config: Config) -> List[str]:
    entries: List[str] = []
    try:
        rel_state = config.state_dir.resolve().relative_to(config.repo_path)
    except ValueError:
        rel_state = None
    if rel_state is not None:
        entries.append(f"/{rel_state.as_posix().rstrip('/')}/")
    return entries


def ensure_supervisor_gitignore(config: Config) -> None:
    if not git_is_enabled(config):
        return
    entries = supervisor_gitignore_entries(config)
    if not entries:
        return
    path = config.repo_path / ".gitignore"
    text = read_text(path)
    block = "\n".join([SUPERVISOR_GITIGNORE_START, *entries, SUPERVISOR_GITIGNORE_END])
    if SUPERVISOR_GITIGNORE_START in text and SUPERVISOR_GITIGNORE_END in text:
        text = re.sub(
            rf"{re.escape(SUPERVISOR_GITIGNORE_START)}.*?{re.escape(SUPERVISOR_GITIGNORE_END)}",
            block,
            text,
            flags=re.DOTALL,
        )
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n" if text else "") + block + "\n"
    path.write_text(text, encoding="utf-8")


def claude_skill_source_dir() -> Path:
    return PROVIDER_CONTEXT_DIR / "claude" / "lean-formalizer"


def codex_skill_source_dir() -> Path:
    return PROVIDER_CONTEXT_DIR / "codex" / "lean-formalizer"


def gemini_context_source_file() -> Path:
    return PROVIDER_CONTEXT_DIR / "gemini" / "GEMINI.md"


def install_tree(source: Path, destination: Path) -> List[Path]:
    if not source.exists():
        return []
    destination.mkdir(parents=True, exist_ok=True)
    installed: List[Path] = []
    for path in source.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(source)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        installed.append(target)
    return installed


def install_file(source: Path, destination: Path) -> Optional[Path]:
    if not source.exists():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def install_personal_provider_context_files(home_dir: Path, providers: Sequence[str]) -> List[Path]:
    installed: List[Path] = []
    seen = {provider.strip().lower() for provider in providers if provider.strip()}
    if "claude" in seen:
        installed.extend(install_tree(claude_skill_source_dir(), home_dir / ".claude" / "skills" / "lean-formalizer"))
    if "codex" in seen:
        installed.extend(install_tree(codex_skill_source_dir(), home_dir / ".codex" / "skills" / "lean-formalizer"))
    if "gemini" in seen:
        path = install_file(gemini_context_source_file(), home_dir / ".gemini" / "GEMINI.md")
        if path is not None:
            installed.append(path)
    return installed


def install_scope_provider_context_files(scope_dir: Path, provider: str) -> List[Path]:
    installed: List[Path] = []
    normalized = provider.strip().lower()
    if normalized == "claude":
        installed.extend(install_tree(claude_skill_source_dir(), scope_dir / ".claude" / "skills" / "lean-formalizer"))
    elif normalized == "codex":
        installed.extend(install_tree(codex_skill_source_dir(), scope_dir / ".agents" / "skills" / "lean-formalizer"))
    elif normalized == "gemini":
        path = install_file(gemini_context_source_file(), scope_dir / "GEMINI.md")
        if path is not None:
            installed.append(path)
    return installed


def supervisor_phase_tasks(config: Config, phase: str) -> List[str]:
    paper_label = relative_repo_label(config, config.workflow.paper_tex_path) if config.workflow.paper_tex_path else "(no paper tex configured)"
    if phase == "paper_check":
        return [
            f"- [ ] Read `{paper_label}` carefully from start to finish.",
            "- [ ] Verify the mathematics of each proof, not just the statements.",
            "- [ ] Record corrections, clarifications, and dependencies in `PAPERNOTES.md`.",
            "- [ ] Report `STUCK` only for a genuine gap or incorrect statement after serious repair attempts.",
        ]
    if phase == "planning":
        return [
            f"- [ ] Use `{paper_label}`, `PAPERNOTES.md`, and the current repo state to build `PLAN.md`.",
            "- [ ] Produce a comprehensive roadmap for definitions, theorem statements, and proof dependencies.",
            "- [ ] Identify what can come from mathlib versus what must be formalized here.",
            "- [ ] Use `NEED_INPUT` for external-result or design-choice questions that need a human decision.",
        ]
    if phase == "theorem_stating":
        return [
            "- [ ] Create `PaperDefinitions.lean` with the definitions needed to state the paper results.",
            "- [ ] Create `PaperTheorems.lean` with theorem statements as close to the paper as Lean allows.",
            "- [ ] Keep the files easy for a human to compare against the paper.",
            "- [ ] Make both files syntactically valid Lean.",
        ]
    if is_style_cleanup_phase(phase):
        return [
            "- [ ] Keep the proofs complete and the repo end-to-end buildable after every burst.",
            "- [ ] Eliminate warnings when that can be done safely.",
            "- [ ] Consider moderate refactors that improve reusability without changing the paper-facing results.",
            "- [ ] Stop rather than forcing cleanup work that is not clearly worthwhile.",
        ]
    tasks = [
        "- [ ] Prove the target statements presented in `PaperTheorems.lean`.",
        "- [ ] Keep reusable proof infrastructure in separate support files when that yields a cleaner project structure.",
        "- [ ] Maintain `TASKS.md` and `PLAN.md` as the proof frontier moves.",
        "- [ ] Keep sorrys within the configured policy.",
        "- [ ] Do not introduce unapproved axioms.",
    ]
    if theorem_frontier_phase(config) == "full" and theorem_frontier_state_path(config).exists():
        payload = JsonFile.load(theorem_frontier_state_path(config), {})
        if isinstance(payload, dict) and payload.get("mode") == "full":
            active_leaf_id = normalize_frontier_text(payload.get("active_leaf_id"))
            nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
            active_node = nodes.get(active_leaf_id) if active_leaf_id else None
            metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
            escalation = payload.get("escalation") if isinstance(payload.get("escalation"), dict) else {}
            tasks.insert(0, "- [ ] The theorem-frontier DAG is authoritative for proof-formalization work.")
            if isinstance(active_node, dict):
                tasks.insert(1, f"- [ ] Active leaf: `{active_leaf_id}` at `{active_node.get('lean_anchor')}`.")
                tasks.insert(2, f"- [ ] Blocker cluster: {active_node.get('blocker_cluster')}.")
                tasks.insert(3, f"- [ ] Current action: `{payload.get('current_action') or '(unset)'}`.")
                children = active_node.get("child_ids") if isinstance(active_node.get("child_ids"), list) else []
                tasks.insert(4, f"- [ ] Immediate children: {', '.join(children) if children else '(none)'}")
                tasks.insert(5, f"- [ ] Active-leaf age: {int(metrics.get('active_leaf_age', 0) or 0)}; blocker age: {int(metrics.get('blocker_cluster_age', 0) or 0)}.")
            if escalation.get("required"):
                reasons = escalation.get("reasons") if isinstance(escalation.get("reasons"), list) else []
                tasks.insert(6, f"- [ ] Escalation required: {', '.join(str(reason) for reason in reasons) if reasons else 'yes'}.")
    return tasks


def update_supervisor_tasks_file(config: Config, phase: str) -> None:
    tasks_path = config.repo_path / "TASKS.md"
    text = read_text(tasks_path, build_tasks_scaffold())
    if not text.strip():
        text = build_tasks_scaffold()
    if SUPERVISOR_TASKS_START not in text or SUPERVISOR_TASKS_END not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n{SUPERVISOR_TASKS_START}\n{SUPERVISOR_TASKS_END}\n"
    block = "\n".join([SUPERVISOR_TASKS_START, "## Supervisor Tasks", *supervisor_phase_tasks(config, phase), SUPERVISOR_TASKS_END])
    text = re.sub(
        rf"{re.escape(SUPERVISOR_TASKS_START)}.*?{re.escape(SUPERVISOR_TASKS_END)}",
        block,
        text,
        flags=re.DOTALL,
    )
    tasks_path.write_text(text.rstrip() + "\n", encoding="utf-8")


def ensure_repo_files(config: Config, phase: str) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "runtime", "prompts", "scopes"):
        (config.state_dir / name).mkdir(parents=True, exist_ok=True)
    if theorem_frontier_enabled(config, phase) and not theorem_frontier_state_path(config).exists():
        mode = "full" if theorem_frontier_full_enabled(config, phase) else "phase0"
        JsonFile.dump(theorem_frontier_state_path(config), default_theorem_frontier_payload(mode))

    if not config.goal_file.exists():
        raise SupervisorError(
            f"Goal file not found: {config.goal_file}. Create it before running the supervisor."
        )
    if config.workflow.paper_tex_path is not None and phase in {"paper_check", "planning", "theorem_stating"}:
        if not config.workflow.paper_tex_path.exists():
            raise SupervisorError(f"Paper tex file not found: {config.workflow.paper_tex_path}")

    tasks_path = config.repo_path / "TASKS.md"
    if not tasks_path.exists():
        tasks_path.write_text(build_tasks_scaffold(), encoding="utf-8")
    update_supervisor_tasks_file(config, phase)
    ensure_approved_axioms_file(config)

    if phase_uses_paper_notes(phase):
        paper_notes_path = config.repo_path / "PAPERNOTES.md"
        if not paper_notes_path.exists():
            paper_notes_path.write_text(
                textwrap.dedent(
                    """\
                    # Paper Notes

                    ## Corrections And Clarifications
                    - Add corrections, hidden assumptions, and proof dependencies here.

                    ## Open Questions
                    - Record genuine gaps or design questions here before declaring `STUCK`.
                    """
                ),
                encoding="utf-8",
            )

    if phase_uses_plan(phase):
        plan_path = config.repo_path / "PLAN.md"
        if not plan_path.exists():
            plan_path.write_text(
                textwrap.dedent(
                    """\
                    # High-Level Plan

                    ## Main results
                    - List the main theorems to formalize.

                    ## Imported dependencies
                    - Record what should come from mathlib.

                    ## New definitions
                    - List the definitions needed to state the theorems.

                    ## Proof roadmap
                    - Give a plausible dependency-driven route to each main theorem.

                    ## Design questions
                    - Record any points that may require human input.
                    """
                ),
                encoding="utf-8",
            )


def git_is_enabled(config: Config) -> bool:
    return bool(config.git.remote_url)


def git_run(
    config: Config,
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or config.repo_path),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def git_output(config: Config, args: Sequence[str], *, cwd: Optional[Path] = None) -> str:
    proc = git_run(config, args, cwd=cwd)
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def ensure_git_command_ok(config: Config, args: Sequence[str], *, cwd: Optional[Path] = None) -> str:
    proc = git_run(config, args, cwd=cwd)
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise SupervisorError(f"Git command failed in {cwd or config.repo_path}: git {' '.join(args)}\n{output}")
    return output


def repo_is_git_repository(config: Config) -> bool:
    proc = git_run(config, ["rev-parse", "--is-inside-work-tree"])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_has_git_commits(config: Config) -> bool:
    return git_run(config, ["rev-parse", "--verify", "HEAD"]).returncode == 0


def current_git_branch(config: Config) -> str:
    if not repo_is_git_repository(config):
        return config.git.branch
    branch = git_output(config, ["branch", "--show-current"]).strip()
    return branch or config.git.branch


def remote_branch_exists(config: Config, branch: str) -> bool:
    if not config.git.remote_url:
        return False
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", config.git.remote_url, branch],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise SupervisorError(
            f"Failed to query configured git remote {config.git.remote_url!r} for branch {branch!r}: {output}"
        )
    return bool(proc.stdout.strip())


def ensure_git_repository(config: Config) -> None:
    if not git_is_enabled(config):
        return

    ensure_supervisor_gitignore(config)

    if not repo_is_git_repository(config):
        init = git_run(config, ["init", "-b", config.git.branch])
        if init.returncode != 0:
            fallback = git_run(config, ["init"])
            if fallback.returncode != 0:
                output = ((init.stdout or "") + (init.stderr or "") + (fallback.stdout or "") + (fallback.stderr or "")).strip()
                raise SupervisorError(f"Failed to initialize git repository in {config.repo_path}: {output}")
            ensure_git_command_ok(config, ["symbolic-ref", "HEAD", f"refs/heads/{config.git.branch}"])

    if not git_output(config, ["config", "--get", "user.name"]).strip():
        ensure_git_command_ok(config, ["config", "user.name", config.git.author_name])
    if not git_output(config, ["config", "--get", "user.email"]).strip():
        ensure_git_command_ok(config, ["config", "user.email", config.git.author_email])

    remote_name = config.git.remote_name
    remote_url = config.git.remote_url or ""
    remote_proc = git_run(config, ["remote", "get-url", remote_name])
    if remote_proc.returncode != 0:
        ensure_git_command_ok(config, ["remote", "add", remote_name, remote_url])
    else:
        existing = remote_proc.stdout.strip()
        if existing != remote_url:
            raise SupervisorError(
                f"Configured git remote {remote_name!r} points to {existing!r}, expected {remote_url!r}."
            )

    if not repo_has_git_commits(config):
        branch = current_git_branch(config)
        if remote_branch_exists(config, branch):
            raise SupervisorError(
                f"Configured git remote {remote_name!r} already has branch {branch!r} while local repo "
                "has no commits. Clone or sync that repo manually before running the supervisor."
            )


def role_scope_dir(config: Config, provider: str, role: str) -> Path:
    scope = config.state_dir / "scopes" / f"{provider}-{role}"
    scope.mkdir(parents=True, exist_ok=True)
    links = {
        "repo": config.repo_path,
        "supervisor": config.state_dir,
        ".agent-supervisor": config.state_dir,
    }
    for name, target in links.items():
        link = scope / name
        if link.is_symlink() or link.exists():
            try:
                resolved = link.resolve()
            except FileNotFoundError:
                resolved = None
            if resolved != target:
                if link.is_dir() and not link.is_symlink():
                    raise SupervisorError(f"Refusing to overwrite non-symlink directory: {link}")
                link.unlink(missing_ok=True)  # type: ignore[arg-type]
        if not link.exists():
            link.symlink_to(target, target_is_directory=True)
    install_scope_provider_context_files(scope, provider)
    return scope


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def gemini_include_directories(config: Config, scope_dir: Path) -> List[Path]:
    candidates = [config.repo_path, config.state_dir]
    if config.workflow.paper_tex_path is not None:
        candidates.append(config.workflow.paper_tex_path.parent)
    candidates.extend(
        [
            config.goal_file.parent,
            config.workflow.approved_axioms_path.parent,
            config.workflow.human_input_path.parent,
            config.workflow.input_request_path.parent,
        ]
    )
    include_dirs: List[Path] = []
    resolved_scope = scope_dir.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved == resolved_scope:
            continue
        if any(path_is_within(resolved, existing) for existing in include_dirs):
            continue
        include_dirs = [existing for existing in include_dirs if not path_is_within(existing, resolved)]
        include_dirs.append(resolved)
        if len(include_dirs) >= 5:
            break
    return include_dirs


def repo_prompt_label(config: Config, provider: str, path: Path) -> str:
    if provider == "gemini":
        try:
            return path.resolve().relative_to(config.repo_path).as_posix()
        except ValueError:
            return str(path.resolve())
    return relative_repo_label(config, path)


def supervisor_prompt_label(config: Config, provider: str, path: Path) -> str:
    try:
        return path.resolve().relative_to(config.repo_path.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def legacy_supervisor_artifact_paths(config: Config, path: Path) -> List[Path]:
    try:
        rel = path.resolve().relative_to(config.state_dir.resolve())
    except ValueError:
        return []
    return [config.repo_path / "supervisor" / rel]


def prepare_gemini_cli_home(scope_dir: Path) -> Path:
    gemini_home = scope_dir / ".gemini"
    gemini_home.mkdir(parents=True, exist_ok=True)
    source_home = Path.home() / ".gemini"
    for name in ("oauth_creds.json", "google_accounts.json", "installation_id", "settings.json", "trustedFolders.json"):
        source = source_home / name
        if not source.exists():
            continue
        target = gemini_home / name
        if target.exists():
            continue
        shutil.copy2(source, target)
    return scope_dir


def tmux_cmd(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], text=True, capture_output=True, check=check)


def ensure_tmux_session(config: Config) -> None:
    session = config.tmux.session_name
    if tmux_cmd("has-session", "-t", session, check=False).returncode == 0:
        return
    dashboard_cmd = f"echo Agent tmux session ready: {shlex.quote(session)}; exec bash"
    tmux_cmd(
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        config.tmux.dashboard_window_name,
        "-c",
        str(config.repo_path),
        dashboard_cmd,
    )


def read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8", errors="replace")


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n\n...[truncated]...\n\n" + text[-half:]


def timestamp_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def chat_root_dir(config: Config) -> Path:
    return config.chat.root_dir


def chat_assets_dir(config: Config) -> Path:
    return chat_root_dir(config) / "_assets"


def chat_manifest_path(config: Config) -> Path:
    return chat_root_dir(config) / "repos.json"


def chat_codex_budget_path(config: Config) -> Path:
    return chat_root_dir(config) / "codex-budget.json"


def chat_repo_dir(config: Config) -> Path:
    return chat_root_dir(config) / config.chat.repo_name


def chat_repo_meta_path(config: Config) -> Path:
    return chat_repo_dir(config) / "meta.json"


def chat_repo_events_path(config: Config) -> Path:
    return chat_repo_dir(config) / "events.jsonl"


def chat_repo_events_chunks_dir(config: Config) -> Path:
    return chat_repo_dir(config) / "events"


def chat_repo_events_manifest_path(config: Config) -> Path:
    return chat_repo_dir(config) / "events-manifest.json"


def chat_repo_index_path(config: Config) -> Path:
    return chat_repo_dir(config) / "index.html"


def chat_repo_files_dir(config: Config) -> Path:
    return chat_repo_dir(config) / "files"


def dag_root_dir(config: Config) -> Path:
    return config.chat.root_dir.parent / "lagent-dags"


def dag_repo_dir(config: Config) -> Path:
    return dag_root_dir(config) / config.chat.repo_name


def dag_frontier_path(config: Config) -> Path:
    return dag_repo_dir(config) / "frontier.json"


def dag_frontier_history_path(config: Config) -> Path:
    return dag_repo_dir(config) / "frontier-history.jsonl"


def dag_manifest_path(config: Config) -> Path:
    return dag_root_dir(config) / "repos.json"


def dag_assets_dir(config: Config) -> Path:
    return dag_root_dir(config) / "_assets"


def dag_repo_meta_path(config: Config) -> Path:
    return dag_repo_dir(config) / "meta.json"


def chat_repo_url(config: Config) -> str:
    return f"{config.chat.public_base_url}#{config.chat.repo_name}"


def chat_repo_direct_url(config: Config) -> str:
    return f"{config.chat.public_base_url}{config.chat.repo_name}/"


def install_chat_viewer_assets(root_dir: Path) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = root_dir / "_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    version_sources = [
        CHAT_VIEWER_DIR / "index.html",
        CHAT_VIEWER_DIR / "app.js",
        CHAT_VIEWER_DIR / "markdown-viewer.html",
        CHAT_VIEWER_DIR / "markdown-viewer.js",
        CHAT_VIEWER_DIR / "styles.css",
    ]
    digest = hashlib.sha1()
    for source in version_sources:
        digest.update(source.name.encode("utf-8"))
        digest.update(source.read_bytes())
    viewer_version = digest.hexdigest()[:12]
    asset_targets = {
        CHAT_VIEWER_DIR / "index.html": root_dir / "index.html",
        CHAT_VIEWER_DIR / "app.js": assets_dir / "app.js",
        CHAT_VIEWER_DIR / "markdown-viewer.html": assets_dir / "markdown-viewer.html",
        CHAT_VIEWER_DIR / "markdown-viewer.js": assets_dir / "markdown-viewer.js",
        CHAT_VIEWER_DIR / "styles.css": assets_dir / "styles.css",
    }
    for source, target in asset_targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.name in {"index.html", "markdown-viewer.html"}:
            rendered = source.read_text(encoding="utf-8").replace(CHAT_VIEWER_VERSION_PLACEHOLDER, viewer_version)
            target.write_text(rendered, encoding="utf-8")
        else:
            shutil.copyfile(source, target)
    JsonFile.dump(
        assets_dir / "viewer-version.json",
        {
            "version": viewer_version,
            "generated_at": timestamp_now(),
        },
    )
    if not (root_dir / "repos.json").exists():
        JsonFile.dump(root_dir / "repos.json", {"repos": []})


def chat_codex_budget_payload() -> Dict[str, Any]:
    status = latest_codex_weekly_budget_status()
    payload: Dict[str, Any] = {
        "available": status is not None,
        "checked_at": timestamp_now(),
    }
    if status is None:
        payload.update(
            {
                "timestamp": None,
                "source_path": None,
                "plan_type": None,
                "used_percent": None,
                "percent_left": None,
                "window_minutes": None,
                "resets_at": None,
            }
        )
        return payload
    payload.update(status)
    return payload


def refresh_chat_codex_budget_status(config: Config) -> Dict[str, Any]:
    payload = chat_codex_budget_payload()
    JsonFile.dump(chat_codex_budget_path(config), payload)
    return payload


def frontier_summary_for_meta(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return None
    mode = normalize_frontier_text(payload.get("mode")).lower()
    if mode != "full":
        return None
    nodes = payload.get("nodes") or {}
    if not isinstance(nodes, dict):
        return None
    status_counts: Dict[str, int] = {}
    for node in nodes.values():
        if isinstance(node, dict):
            s = str(node.get("status", "unknown"))
            status_counts[s] = status_counts.get(s, 0) + 1
    metrics = payload.get("metrics") or {}
    escalation = payload.get("escalation") or {}
    active_leaf_id = normalize_frontier_text(payload.get("active_leaf_id"))
    active_node = nodes.get(active_leaf_id) if active_leaf_id else None
    return {
        "mode": "full",
        "has_frontier": True,
        "total_nodes": len(nodes),
        "status_counts": status_counts,
        "active_leaf_id": active_leaf_id or None,
        "active_leaf_anchor": active_node.get("lean_anchor") if isinstance(active_node, dict) else None,
        "escalation_required": bool(escalation.get("required")),
        "cone_purity": metrics.get("cone_purity"),
        "paper_nodes_closed": int(metrics.get("paper_nodes_closed", 0) or 0),
    }


def export_dag_frontier_snapshot(config: Config, state: Dict[str, Any]) -> None:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return
    mode = normalize_frontier_text(payload.get("mode")).lower()
    if mode != "full":
        return
    export = dict(payload)
    export["exported_at"] = timestamp_now()
    JsonFile.dump(dag_frontier_path(config), export)


def _compact_frontier_node(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: node[k]
        for k in (
            "node_id", "kind", "status", "display_label",
            "natural_language_statement",
            "lean_statement", "lean_anchor", "paper_provenance",
            "closure_mode", "blocker_cluster", "acceptance_evidence",
            "notes", "parent_ids", "child_ids",
        )
        if k in node
    }


def export_dag_frontier_seed(
    config: Config,
    payload: Dict[str, Any],
    *,
    cycle: int,
) -> None:
    nodes = payload.get("nodes") or {}
    entry = {
        "cycle": cycle,
        "type": "seed",
        "active_leaf_id": payload.get("active_leaf_id"),
        "nodes": {
            nid: _compact_frontier_node(n)
            for nid, n in nodes.items()
            if isinstance(n, dict)
        },
        "edges": [
            {k: e[k] for k in ("parent", "child", "edge_type", "justification") if k in e}
            for e in (payload.get("edges") or [])
            if isinstance(e, dict)
        ],
        "metrics": dict(payload.get("metrics") or {}),
        "timestamp": timestamp_now(),
    }
    append_jsonl(dag_frontier_history_path(config), entry)


def export_dag_frontier_cycle(
    config: Config,
    state: Dict[str, Any],
    before_node_ids: Set[str],
    payload: Dict[str, Any],
    *,
    cycle: int,
    outcome: str,
    reviewed_node_id: str,
    worker_directive: str,
) -> None:
    nodes = payload.get("nodes") or {}
    new_node_ids = set(nodes.keys()) - before_node_ids
    edges = payload.get("edges") or []
    entry: Dict[str, Any] = {
        "cycle": cycle,
        "type": "review",
        "outcome": outcome,
        "reviewed_node_id": reviewed_node_id,
        "active_leaf_id": payload.get("active_leaf_id"),
        "worker_directive": worker_directive,
        "nodes_added": {
            nid: _compact_frontier_node(nodes[nid])
            for nid in new_node_ids
            if isinstance(nodes.get(nid), dict)
        },
        "node_statuses": {
            nid: str(n.get("status", ""))
            for nid, n in nodes.items()
            if isinstance(n, dict)
        },
        "metrics": dict(payload.get("metrics") or {}),
        "escalation": dict(payload.get("escalation") or {}),
        "timestamp": timestamp_now(),
    }
    if new_node_ids:
        entry["edges_added"] = [
            {k: e[k] for k in ("parent", "child", "edge_type", "justification") if k in e}
            for e in edges
            if isinstance(e, dict) and (e.get("parent") in new_node_ids or e.get("child") in new_node_ids)
        ]
    append_jsonl(dag_frontier_history_path(config), entry)


def worker_directive_summary(state: Dict[str, Any]) -> str:
    last_review = state.get("last_review")
    parts: List[str] = []
    if isinstance(last_review, dict):
        next_prompt = str(last_review.get("next_prompt", "")).strip()
        if next_prompt:
            parts.append(next_prompt)
    recovery = state.get("stuck_recovery")
    if isinstance(recovery, dict):
        attempts = recovery.get("attempts")
        if isinstance(attempts, list) and attempts:
            latest = attempts[-1]
            if isinstance(latest, dict):
                creative = str(latest.get("creative_suggestion", "")).strip()
                if creative:
                    parts.append(f"Recovery: {creative}")
    return " | ".join(parts) if parts else ""


DAG_VIEWER_DIR = PACKAGE_DIR / "dag_viewer"


def install_dag_viewer_assets(root_dir: Path) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = root_dir / "_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    source_files = [
        DAG_VIEWER_DIR / "index.html",
        DAG_VIEWER_DIR / "dag-browser.js",
        DAG_VIEWER_DIR / "dag-browser.css",
        DAG_VIEWER_DIR / "dag-layout-worker.js",
    ]
    digest = hashlib.sha1()
    for source in source_files:
        if source.exists():
            digest.update(source.name.encode("utf-8"))
            digest.update(source.read_bytes())
    viewer_version = digest.hexdigest()[:12]
    asset_targets = {
        DAG_VIEWER_DIR / "index.html": root_dir / "index.html",
        DAG_VIEWER_DIR / "dag-browser.js": assets_dir / "dag-browser.js",
        DAG_VIEWER_DIR / "dag-browser.css": assets_dir / "dag-browser.css",
        DAG_VIEWER_DIR / "dag-layout-worker.js": assets_dir / "dag-layout-worker.js",
    }
    for source, target in asset_targets.items():
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".html":
            rendered = source.read_text(encoding="utf-8").replace(
                CHAT_VIEWER_VERSION_PLACEHOLDER, viewer_version,
            )
            target.write_text(rendered, encoding="utf-8")
        else:
            shutil.copyfile(source, target)
    JsonFile.dump(
        assets_dir / "viewer-version.json",
        {"version": viewer_version, "generated_at": timestamp_now()},
    )
    if not (root_dir / "repos.json").exists():
        JsonFile.dump(root_dir / "repos.json", {"repos": []})


def ensure_dag_site(config: Config) -> None:
    root = dag_root_dir(config)
    install_dag_viewer_assets(root)
    dag_repo_dir(config).mkdir(parents=True, exist_ok=True)


def update_dag_manifest(config: Config, state: Dict[str, Any]) -> None:
    manifest_path = dag_manifest_path(config)
    manifest = JsonFile.load(manifest_path, {"repos": []})
    repos = manifest.get("repos") if isinstance(manifest.get("repos"), list) else []
    summary = frontier_summary_for_meta(state)
    phase = current_phase(config, state)
    cycle = int(state.get("cycle", 0) or 0)
    entry = {
        "repo_name": config.chat.repo_name,
        "project_name": config.chat.project_name,
        "updated_at": timestamp_now(),
        "current_phase": phase,
        "current_cycle": cycle,
        "frontier_summary": summary,
        "branch_overview": branch_overview(state),
    }
    found = False
    for i, item in enumerate(repos):
        if isinstance(item, dict) and item.get("repo_name") == config.chat.repo_name:
            repos[i] = entry
            found = True
            break
    if not found:
        repos.append(entry)
    repos.sort(key=lambda r: (r.get("updated_at", ""), r.get("repo_name", "")), reverse=True)
    manifest["repos"] = repos
    JsonFile.dump(manifest_path, manifest)


def export_dag_meta(config: Config, state: Dict[str, Any]) -> None:
    summary = frontier_summary_for_meta(state)
    meta = {
        "repo_name": config.chat.repo_name,
        "project_name": config.chat.project_name,
        "updated_at": timestamp_now(),
        "current_phase": current_phase(config, state),
        "current_cycle": int(state.get("cycle", 0) or 0),
        "frontier_summary": summary,
        "branch_overview": branch_overview(state),
    }
    JsonFile.dump(dag_repo_meta_path(config), meta)
    update_dag_manifest(config, state)


def chat_event_chunk_bounds(cycle: int) -> Tuple[int, int]:
    cycle_num = max(int(cycle or 0), 1)
    start = ((cycle_num - 1) // CHAT_EVENT_CYCLE_CHUNK_SIZE) * CHAT_EVENT_CYCLE_CHUNK_SIZE + 1
    end = start + CHAT_EVENT_CYCLE_CHUNK_SIZE - 1
    return start, end


def chat_event_chunk_relative_path(start_cycle: int, end_cycle: int) -> Path:
    return Path("events") / f"chunk-{start_cycle:04d}-{end_cycle:04d}.jsonl"


def default_chat_events_manifest() -> Dict[str, Any]:
    return {
        "chunk_size_cycles": CHAT_EVENT_CYCLE_CHUNK_SIZE,
        "chunks": [],
    }


def load_chat_events_manifest(config: Config) -> Dict[str, Any]:
    manifest = JsonFile.load(chat_repo_events_manifest_path(config), None)
    default = default_chat_events_manifest()
    if not isinstance(manifest, dict):
        return default
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        chunks = []
    normalized: List[Dict[str, Any]] = []
    for entry in chunks:
        if not isinstance(entry, dict):
            continue
        try:
            start_cycle = int(entry.get("start_cycle", 0) or 0)
            end_cycle = int(entry.get("end_cycle", 0) or 0)
            event_count = int(entry.get("event_count", 0) or 0)
        except (TypeError, ValueError):
            continue
        file_value = str(entry.get("file", "")).strip()
        if not file_value or start_cycle <= 0 or end_cycle < start_cycle:
            continue
        normalized.append(
            {
                "file": file_value,
                "start_cycle": start_cycle,
                "end_cycle": end_cycle,
                "event_count": event_count,
                "updated_at": str(entry.get("updated_at", "")).strip() or None,
            }
        )
    normalized.sort(key=lambda item: (item["start_cycle"], item["end_cycle"]), reverse=True)
    default["chunks"] = normalized
    return default


def write_chat_events_manifest(config: Config, manifest: Dict[str, Any]) -> Dict[str, Any]:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        chunks = []
    chunks.sort(key=lambda item: (int(item.get("start_cycle", 0) or 0), int(item.get("end_cycle", 0) or 0)), reverse=True)
    payload = {
        "chunk_size_cycles": CHAT_EVENT_CYCLE_CHUNK_SIZE,
        "chunks": chunks,
    }
    JsonFile.dump(chat_repo_events_manifest_path(config), payload)
    return payload


def append_chat_event_chunk(config: Config, event: Dict[str, Any]) -> None:
    start_cycle, end_cycle = chat_event_chunk_bounds(int(event.get("cycle", 0) or 0))
    chunk_rel = chat_event_chunk_relative_path(start_cycle, end_cycle)
    chunk_path = chat_repo_dir(config) / chunk_rel
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(chunk_path, event)

    manifest = load_chat_events_manifest(config)
    chunks = manifest["chunks"]
    chunk_file = chunk_rel.as_posix()
    existing = next((entry for entry in chunks if entry.get("file") == chunk_file), None)
    if existing is None:
        existing = {
            "file": chunk_file,
            "start_cycle": start_cycle,
            "end_cycle": end_cycle,
            "event_count": 0,
            "updated_at": None,
        }
        chunks.append(existing)
    existing["event_count"] = int(existing.get("event_count", 0) or 0) + 1
    existing["updated_at"] = str(event.get("timestamp") or timestamp_now())
    write_chat_events_manifest(config, manifest)


def rebuild_chat_event_chunks_from_legacy_log(config: Config) -> Dict[str, Any]:
    legacy_path = chat_repo_events_path(config)
    manifest = default_chat_events_manifest()
    chunks_dir = chat_repo_events_chunks_dir(config)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    expected: Dict[str, List[Dict[str, Any]]] = {}
    updated_at_by_file: Dict[str, str] = {}
    if legacy_path.exists():
        for line in legacy_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            start_cycle, end_cycle = chat_event_chunk_bounds(int(event.get("cycle", 0) or 0))
            chunk_file = chat_event_chunk_relative_path(start_cycle, end_cycle).as_posix()
            expected.setdefault(chunk_file, []).append(event)
            updated_at_by_file[chunk_file] = str(event.get("timestamp") or updated_at_by_file.get(chunk_file) or timestamp_now())
    for chunk_file, events in expected.items():
        chunk_path = chat_repo_dir(config) / chunk_file
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        chunk_path.write_text(payload, encoding="utf-8")
        start_cycle, end_cycle = chat_event_chunk_bounds(int(events[0].get("cycle", 0) or 0))
        manifest["chunks"].append(
            {
                "file": chunk_file,
                "start_cycle": start_cycle,
                "end_cycle": end_cycle,
                "event_count": len(events),
                "updated_at": updated_at_by_file.get(chunk_file),
            }
        )
    expected_paths = {Path(item["file"]) for item in manifest["chunks"]}
    for path in chunks_dir.rglob("*.jsonl"):
        rel = path.relative_to(chat_repo_dir(config))
        if rel not in expected_paths:
            path.unlink()
    remove_empty_directories(chunks_dir)
    return write_chat_events_manifest(config, manifest)


def ensure_chat_event_chunks(config: Config) -> Dict[str, Any]:
    manifest_path = chat_repo_events_manifest_path(config)
    if manifest_path.exists():
        return load_chat_events_manifest(config)
    return rebuild_chat_event_chunks_from_legacy_log(config)


def default_chat_meta(config: Config) -> Dict[str, Any]:
    return {
        "repo_name": config.chat.repo_name,
        "project_name": config.chat.project_name,
        "is_branch": config.chat.repo_name != config.chat.project_name,
        "repo_display_name": config.repo_path.name,
        "repo_path": str(config.repo_path),
        "goal_file": relative_repo_label(config, config.goal_file),
        "chat_url": chat_repo_url(config),
        "direct_url": chat_repo_direct_url(config),
        "updated_at": None,
        "current_phase": None,
        "current_cycle": 0,
        "event_count": 0,
        "last_event_kind": None,
        "last_summary": "",
        "last_worker_status": None,
        "last_reviewer_decision": None,
        "awaiting_human_input": False,
        "markdown_files": [],
        "branch_overview": None,
    }


def load_chat_meta(config: Config) -> Dict[str, Any]:
    meta = JsonFile.load(chat_repo_meta_path(config), None)
    defaults = default_chat_meta(config)
    if not isinstance(meta, dict):
        return defaults
    merged = dict(defaults)
    merged.update(meta)
    for key in ("repo_name", "project_name", "is_branch", "repo_display_name", "repo_path", "goal_file", "chat_url", "direct_url"):
        merged[key] = defaults[key]
    return merged


def branch_lineage_entries(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    lineage = state.get("branch_lineage")
    if not isinstance(lineage, list):
        return []
    results: List[Dict[str, Any]] = []
    for entry in lineage:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("branch_name", "")).strip()
        episode_id = str(entry.get("episode_id", "")).strip()
        if not name or not episode_id:
            continue
        results.append(
            {
                "episode_id": episode_id,
                "branch_name": name,
                "summary": str(entry.get("summary", "")).strip(),
                "rewrite_scope": str(entry.get("rewrite_scope", "")).strip(),
            }
        )
    return results


def branch_overview(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    lineage = branch_lineage_entries(state)
    history = state.get("branch_history")
    if not isinstance(history, list):
        history = []
    active = active_branch_episode(state)
    if not lineage and not history and active is None:
        return None

    current_lineage_map = {entry["episode_id"]: entry["branch_name"] for entry in lineage}
    episodes_raw: List[Dict[str, Any]] = [entry for entry in history if isinstance(entry, dict)]
    if active is not None:
        episodes_raw.append(active)

    episodes: List[Dict[str, Any]] = []
    for raw_episode in episodes_raw:
        episode_id = str(raw_episode.get("id", "")).strip()
        if not episode_id:
            continue
        episode_lineage = raw_episode.get("lineage")
        if not isinstance(episode_lineage, list):
            episode_lineage = []
        ancestor_branch_names = [
            str(entry.get("branch_name", "")).strip()
            for entry in episode_lineage
            if isinstance(entry, dict) and str(entry.get("branch_name", "")).strip()
        ]
        selected_branch = str(raw_episode.get("selected_branch", "")).strip()
        status = str(raw_episode.get("status", "")).strip() or "active"
        branches_payload: List[Dict[str, Any]] = []
        for raw_branch in raw_episode.get("branches", []):
            if not isinstance(raw_branch, dict):
                continue
            branch_name = str(raw_branch.get("name", "")).strip()
            if not branch_name:
                continue
            if status == "active":
                branch_status = str(raw_branch.get("status", "")).strip() or "active"
            elif selected_branch and branch_name == selected_branch:
                branch_status = "selected"
            else:
                branch_status = "dead"
            branches_payload.append(
                {
                    "name": branch_name,
                    "repo_name": str(raw_branch.get("chat_repo_name", "")).strip() or None,
                    "summary": str(raw_branch.get("summary", "")).strip(),
                    "rewrite_scope": str(raw_branch.get("rewrite_scope", "")).strip(),
                    "status": branch_status,
                    "is_current_path": current_lineage_map.get(episode_id) == branch_name,
                    "path_newest_to_oldest": [branch_name, *reversed(ancestor_branch_names), "mainline"],
                }
            )
        episodes.append(
            {
                "id": episode_id,
                "phase": raw_episode.get("phase"),
                "trigger_cycle": int(raw_episode.get("trigger_cycle", 0) or 0),
                "status": status,
                "selected_branch": selected_branch or None,
                "selection_question": str(raw_episode.get("selection_question", "")).strip(),
                "lineage_newest_to_oldest": [*reversed(ancestor_branch_names), "mainline"],
                "branches": branches_payload,
            }
        )

    episodes.sort(key=lambda entry: (int(entry.get("trigger_cycle", 0) or 0), str(entry.get("id", ""))), reverse=True)

    current_path_newest_to_oldest = [entry["branch_name"] for entry in reversed(lineage)] + ["mainline"]
    current_path_status = "alive"
    for episode in episodes:
        episode_id = str(episode.get("id", ""))
        current_branch = current_lineage_map.get(episode_id)
        if not current_branch:
            continue
        if episode.get("status") == "selected" and current_branch != episode.get("selected_branch"):
            current_path_status = "dead"
            break

    return {
        "has_branching": bool(episodes),
        "current_path_newest_to_oldest": current_path_newest_to_oldest,
        "current_path_status": current_path_status,
        "episodes": episodes,
    }


def sync_chat_state_metadata(config: Config, state: Dict[str, Any]) -> None:
    meta_path = chat_repo_meta_path(config)
    if not meta_path.exists():
        return
    meta = load_chat_meta(config)
    overview = branch_overview(state)
    if meta.get("branch_overview") != overview:
        meta["branch_overview"] = overview
        JsonFile.dump(meta_path, meta)
        update_chat_manifest(config, meta)


def update_chat_manifest(config: Config, meta: Dict[str, Any]) -> None:
    path = chat_manifest_path(config)
    payload = JsonFile.load(path, {"repos": []})
    repos = payload.get("repos")
    if not isinstance(repos, list):
        repos = []
    filtered = [entry for entry in repos if isinstance(entry, dict) and entry.get("repo_name") != config.chat.repo_name]
    filtered.append(meta)
    filtered.sort(key=lambda entry: (entry.get("updated_at") or "", entry.get("repo_name") or ""), reverse=True)
    JsonFile.dump(path, {"repos": filtered})


def workflow_markdown_files(config: Config) -> List[Path]:
    candidates = [
        config.goal_file,
        config.repo_path / "TASKS.md",
        config.repo_path / "PAPERNOTES.md",
        config.repo_path / "PLAN.md",
        config.workflow.human_input_path,
        config.workflow.input_request_path,
    ]
    results: List[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            resolved = path.expanduser().resolve()
        if resolved in seen or not path.exists() or path.suffix.lower() != ".md":
            continue
        seen.add(resolved)
        results.append(path)
    return results


def chat_export_relative_path(config: Config, source_path: Path) -> Tuple[str, Path]:
    try:
        rel = source_path.resolve().relative_to(config.repo_path)
        return relative_repo_label(config, source_path), Path("files") / "repo" / rel
    except ValueError:
        digest = hashlib.sha1(str(source_path.resolve()).encode("utf-8")).hexdigest()[:8]
        safe_name = f"{sanitize_repo_name(source_path.stem)}-{digest}{source_path.suffix}"
        return str(source_path), Path("files") / "external" / safe_name


def remove_empty_directories(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue


def sync_chat_markdown_files(config: Config) -> List[Dict[str, Any]]:
    files_dir = chat_repo_files_dir(config)
    files_dir.mkdir(parents=True, exist_ok=True)
    exported: List[Dict[str, Any]] = []
    expected_exports: set[Path] = set()
    for path in workflow_markdown_files(config):
        source_label, export_rel = chat_export_relative_path(config, path)
        target = chat_repo_dir(config) / export_rel
        expected_exports.add(export_rel)
        source_stat = path.stat()
        should_copy = True
        if target.exists():
            target_stat = target.stat()
            should_copy = (
                target_stat.st_size != source_stat.st_size or target_stat.st_mtime_ns != source_stat.st_mtime_ns
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if should_copy:
            shutil.copy2(path, target)
        exported.append(
            {
                "label": path.name,
                "path": source_label,
                "href": f"{config.chat.repo_name}/{export_rel.as_posix()}",
                "updated_at": datetime.fromtimestamp(source_stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )
    for exported_path in files_dir.rglob("*"):
        if not exported_path.is_file():
            continue
        rel = exported_path.relative_to(chat_repo_dir(config))
        if rel not in expected_exports:
            exported_path.unlink()
    remove_empty_directories(files_dir)
    return exported


def refresh_chat_markdown_metadata(config: Config, *, update_manifest: bool) -> List[Dict[str, Any]]:
    repo_dir = chat_repo_dir(config)
    meta_path = chat_repo_meta_path(config)
    if not repo_dir.exists() or not meta_path.exists():
        return []
    meta = load_chat_meta(config)
    markdown_files = sync_chat_markdown_files(config)
    if meta.get("markdown_files") != markdown_files:
        meta["markdown_files"] = markdown_files
        JsonFile.dump(meta_path, meta)
        if update_manifest:
            update_chat_manifest(config, meta)
    return markdown_files


def ensure_chat_site(config: Config) -> None:
    install_chat_viewer_assets(chat_root_dir(config))
    refresh_chat_codex_budget_status(config)
    ensure_chat_event_chunks(config)
    repo_dir = chat_repo_dir(config)
    repo_dir.mkdir(parents=True, exist_ok=True)
    redirect = textwrap.dedent(
        f"""\
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta http-equiv="refresh" content="0; url=../#{config.chat.repo_name}">
          <title>{config.repo_path.name} transcript</title>
        </head>
        <body>
          <p>Redirecting to <a href="../#{config.chat.repo_name}">the transcript viewer</a>.</p>
        </body>
        </html>
        """
    )
    chat_repo_index_path(config).write_text(redirect, encoding="utf-8")
    meta_path = chat_repo_meta_path(config)
    meta = load_chat_meta(config)
    JsonFile.dump(meta_path, meta)
    meta["markdown_files"] = refresh_chat_markdown_metadata(config, update_manifest=False)
    if not meta["markdown_files"]:
        meta["markdown_files"] = sync_chat_markdown_files(config)
        JsonFile.dump(meta_path, meta)
    meta["branch_overview"] = branch_overview(load_state(config))
    JsonFile.dump(meta_path, meta)
    update_chat_manifest(config, meta)


def summarize_chat_event(kind: str, content: Any) -> str:
    if kind == "worker_handoff" and isinstance(content, dict):
        status = str(content.get("status", "")).strip()
        summary = str(content.get("summary_of_changes", "")).strip()
        return f"{status}: {summary}".strip(": ")
    if kind == "reviewer_decision" and isinstance(content, dict):
        decision = str(content.get("decision", "")).strip()
        reason = str(content.get("reason", "")).strip()
        return f"{decision}: {reason}".strip(": ")
    if kind == "validation_summary" and isinstance(content, dict):
        build_ok = bool((content.get("build") or {}).get("ok"))
        sorry_count = int((content.get("sorries") or {}).get("count") or 0)
        return f"build={'ok' if build_ok else 'failing'}, sorrys={sorry_count}"
    if kind == "phase_transition" and isinstance(content, dict):
        return f"{content.get('from_phase', '?')} -> {content.get('to_phase', '?')}"
    if kind == "input_request":
        return "Reviewer requested human input."
    if kind == "human_input":
        return "Human input consumed for resume."
    if kind == "stuck_recovery_suggestion" and isinstance(content, dict):
        attempt = content.get("attempt", "?")
        suggestion = str(content.get("creative_suggestion", "")).strip()
        return f"Recovery attempt {attempt}: {suggestion}".strip(": ")
    if kind == "branch_strategy_decision" and isinstance(content, dict):
        decision = str(content.get("branch_decision", "")).strip()
        reason = str(content.get("reason", "")).strip()
        return f"{decision}: {reason}".strip(": ")
    if kind == "branch_selection_decision" and isinstance(content, dict):
        decision = str(content.get("selection_decision", "")).strip()
        reason = str(content.get("reason", "")).strip()
        return f"{decision}: {reason}".strip(": ")
    if kind == "branch_replacement_decision" and isinstance(content, dict):
        decision = str(content.get("replacement_decision", "")).strip()
        reason = str(content.get("reason", "")).strip()
        return f"{decision}: {reason}".strip(": ")
    return kind.replace("_", " ")


def render_template(name: str, **kwargs: Any) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").format(**kwargs)


class ProviderAdapter:
    def __init__(self, cfg: ProviderConfig, role: str, config: Config, state: Dict[str, Any]):
        self.cfg = cfg
        self.role = role
        self.config = config
        self.state = state

    def role_state(self) -> Dict[str, Any]:
        key = f"{self.cfg.provider}:{self.role}"
        role_state = self.state.setdefault("roles", {}).setdefault(key, {})
        role_state.setdefault("provider", self.cfg.provider)
        role_state.setdefault("role", self.role)
        return role_state

    def scope_dir(self) -> Path:
        return role_scope_dir(self.config, self.cfg.provider, self.role)

    def work_dir(self) -> Path:
        return self.scope_dir()

    def burst_env(self) -> Dict[str, str]:
        return {}

    def needs_initial_run(self) -> bool:
        return not bool(self.role_state().get("initialized"))

    def mark_initialized(self) -> None:
        self.role_state()["initialized"] = True

    def build_initial_command(self) -> List[str]:
        raise NotImplementedError

    def build_continue_command(self) -> List[str]:
        raise NotImplementedError

    def current_command(self) -> List[str]:
        return self.build_initial_command() if self.needs_initial_run() else self.build_continue_command()


class ClaudeAdapter(ProviderAdapter):
    def _base(self) -> List[str]:
        cmd = ["claude", "--dangerously-skip-permissions"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        cmd += self.cfg.extra_args
        return cmd

    def build_initial_command(self) -> List[str]:
        return self._base() + ["--print", PROMPT_TOKEN]

    def build_continue_command(self) -> List[str]:
        return self._base() + ["--continue", "--print", PROMPT_TOKEN]


class CodexAdapter(ProviderAdapter):
    def _initial_flags(self) -> List[str]:
        flags: List[str] = [
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--color",
            "always",
        ]
        if self.cfg.model:
            flags += ["--model", self.cfg.model]
        flags += self.cfg.extra_args
        return flags

    def _resume_flags(self) -> List[str]:
        flags: List[str] = [
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if self.cfg.model:
            flags += ["--model", self.cfg.model]
        flags += self.cfg.extra_args
        return flags

    def build_initial_command(self) -> List[str]:
        return ["codex", "exec", *self._initial_flags(), PROMPT_TOKEN]

    def build_continue_command(self) -> List[str]:
        return ["codex", "exec", "resume", "--last", *self._resume_flags(), PROMPT_TOKEN]


class GeminiAdapter(ProviderAdapter):
    def _base(self) -> List[str]:
        cmd = ["gemini", "--approval-mode=yolo"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        include_dirs = gemini_include_directories(self.config, self.work_dir())
        if include_dirs:
            cmd += ["--include-directories", ",".join(str(path) for path in include_dirs)]
        cmd += self.cfg.extra_args
        return cmd

    def work_dir(self) -> Path:
        return self.config.repo_path

    def burst_env(self) -> Dict[str, str]:
        return {"GEMINI_CLI_HOME": str(prepare_gemini_cli_home(self.scope_dir()))}

    def build_initial_command(self) -> List[str]:
        return self._base() + ["--prompt", PROMPT_TOKEN]

    def build_continue_command(self) -> List[str]:
        return self._base() + ["--resume", "latest", "--prompt", PROMPT_TOKEN]


def make_adapter(role: str, config: Config, state: Dict[str, Any]) -> ProviderAdapter:
    cfg = config.worker if role == "worker" else config.reviewer
    if cfg.provider == "claude":
        return ClaudeAdapter(cfg, role, config, state)
    if cfg.provider == "codex":
        return CodexAdapter(cfg, role, config, state)
    if cfg.provider == "gemini":
        return GeminiAdapter(cfg, role, config, state)
    raise AssertionError(cfg.provider)


def load_state(config: Config) -> Dict[str, Any]:
    state = JsonFile.load(config.state_dir / "state.json", {})
    state.setdefault("cycle", 0)
    state.setdefault("roles", {})
    state.setdefault("review_log", [])
    state.setdefault("phase_history", [])
    state.setdefault("awaiting_human_input", False)
    state.setdefault("stuck_recovery_attempts", [])
    state.setdefault("stuck_recovery_last_trigger_cycle", None)
    state.setdefault("branch_episode_counter", 0)
    state.setdefault("active_branch_episode", None)
    state.setdefault("branch_history", [])
    state.setdefault("branch_context", None)
    state.setdefault("branch_lineage", [])
    state.setdefault("branch_parent_max_current_branches", None)
    state.setdefault("pending_branch_proposal", None)
    state.setdefault("next_branch_proposal_review_count", 0)
    state.setdefault("last_branch_consideration_cycle", 0)
    state.setdefault("codex_budget_pause", None)
    state.setdefault("policy", None)
    state.setdefault("cleanup_last_good_commit", None)
    state.setdefault("theorem_frontier", None)
    state.setdefault("last_theorem_frontier_worker_update", None)
    state.setdefault("last_theorem_frontier_review", None)
    state.setdefault("last_theorem_frontier_paper_review", None)
    if state["theorem_frontier"] is None and theorem_frontier_state_path(config).exists():
        state["theorem_frontier"] = validate_loaded_theorem_frontier_payload(
            JsonFile.load(theorem_frontier_state_path(config), None)
        )
    elif state["theorem_frontier"] is not None:
        state["theorem_frontier"] = validate_loaded_theorem_frontier_payload(state["theorem_frontier"])
    current_phase(config, state)
    return state


def save_state(config: Config, state: Dict[str, Any]) -> None:
    JsonFile.dump(config.state_dir / "state.json", state)
    sync_chat_state_metadata(config, state)


def normalize_frontier_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def validate_theorem_frontier_action(value: Any) -> str:
    action = normalize_frontier_text(value).upper()
    if action not in THEOREM_FRONTIER_ACTIONS:
        raise SupervisorError(f"Invalid theorem frontier action {value!r}")
    return action


def validate_theorem_frontier_outcome(value: Any) -> str:
    outcome = normalize_frontier_text(value).upper()
    if outcome not in THEOREM_FRONTIER_OUTCOMES:
        raise SupervisorError(f"Invalid theorem frontier outcome {value!r}")
    return outcome


def theorem_frontier_current(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    current = state.get("theorem_frontier")
    if isinstance(current, dict) and isinstance(current.get("nodes"), dict):
        active_leaf_id = normalize_frontier_text(current.get("active_leaf_id"))
        if active_leaf_id:
            node = current["nodes"].get(active_leaf_id)
            if isinstance(node, dict):
                return node
        current_payload = current.get("current")
        if isinstance(current_payload, dict):
            return current_payload
    if isinstance(current, dict):
        return current
    return None


def theorem_frontier_context_text(config: Config, state: Dict[str, Any], provider: str) -> str:
    phase = current_phase(config, state)
    if not theorem_frontier_enabled(config, phase):
        return ""
    if theorem_frontier_full_enabled(config, phase):
        payload = theorem_frontier_payload(state) or default_theorem_frontier_payload("full")
        active_leaf_id = normalize_frontier_text(payload.get("active_leaf_id"))
        nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
        active_node = nodes.get(active_leaf_id) if active_leaf_id else None
        worker_artifact = supervisor_prompt_label(config, provider, theorem_frontier_worker_update_path(config))
        paper_artifact = supervisor_prompt_label(config, provider, theorem_frontier_paper_verifier_path(config))
        review_artifact = supervisor_prompt_label(config, provider, theorem_frontier_review_path(config))
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        escalation = payload.get("escalation") if isinstance(payload.get("escalation"), dict) else {}
        lines = [
            "Theorem-frontier DAG discipline:",
            "- Proof formalization is controlled by an authoritative theorem-frontier DAG.",
            f"- The worker must write the theorem-frontier worker artifact to `{worker_artifact}`.",
            f"- Structural DAG edits are reviewed through `{paper_artifact}` before they enter the DAG.",
            f"- The reviewer must write the theorem-frontier review artifact to `{review_artifact}`.",
            "- Each burst must act on one active theorem node via `CLOSE`, `EXPAND`, or `REFUTE_REPLACE`.",
            "- Work outside the active cone does not count as theorem-frontier progress.",
        ]
        if isinstance(active_node, dict):
            children = active_node.get("child_ids") if isinstance(active_node.get("child_ids"), list) else []
            lines.extend(
                [
                    "Current authoritative frontier state:",
                    f"- Active leaf id: {active_leaf_id}",
                    f"- Kind: {active_node.get('kind') or '(none)'}",
                    f"- Anchor: {active_node.get('lean_anchor') or '(none)'}",
                    f"- Closure mode: {active_node.get('closure_mode') or '(none)'}",
                    f"- Blocker cluster: {active_node.get('blocker_cluster') or '(none)'}",
                    f"- Immediate children: {', '.join(children) if children else '(none)'}",
                    f"- Active-leaf age: {int(metrics.get('active_leaf_age', 0) or 0)}",
                    f"- Blocker-cluster age: {int(metrics.get('blocker_cluster_age', 0) or 0)}",
                    f"- Failed close attempts on this blocker: {int(metrics.get('failed_close_attempts', 0) or 0)}",
                    f"- Latest cone purity: {metrics.get('cone_purity') or '(none)'}",
                ]
            )
            if escalation.get("required"):
                reasons = escalation.get("reasons") if isinstance(escalation.get("reasons"), list) else []
                lines.append(f"- Escalation required: {', '.join(str(reason) for reason in reasons) if reasons else 'yes'}")
        else:
            lines.append("- No active theorem node exists yet; the first burst must establish one exactly.")
        return "\n".join(lines)
    current = theorem_frontier_current(state)
    worker_artifact = supervisor_prompt_label(config, provider, theorem_frontier_worker_update_path(config))
    review_artifact = supervisor_prompt_label(config, provider, theorem_frontier_review_path(config))
    lines = [
        "Theorem-frontier discipline (Phase 0):",
        "- In this burst, work on exactly one active theorem node.",
        f"- The worker must write the active-theorem artifact to `{worker_artifact}`.",
        f"- The reviewer will evaluate theorem-frontier progress using `{review_artifact}`.",
        "- Valid theorem actions are `CLOSE`, `EXPAND`, and `REFUTE_REPLACE`.",
        "- Use exact natural-language and Lean statements for the active theorem.",
        "- Keep work local to the active theorem and its mechanically necessary support lemmas.",
    ]
    if current:
        lines.extend(
            [
                "Last approved theorem-frontier state:",
                f"- Active theorem id: {current.get('active_theorem_id') or '(none)'}",
                f"- Anchor: {current.get('active_theorem_anchor') or '(none)'}",
                f"- Assessed action: {current.get('assessed_action') or '(none)'}",
                f"- Outcome: {current.get('outcome') or '(none)'}",
                f"- Blocker cluster: {current.get('blocker_cluster') or '(none)'}",
                f"- Active-theorem age: {int(current.get('active_theorem_age', 0) or 0)}",
                f"- Blocker-cluster age: {int(current.get('blocker_cluster_age', 0) or 0)}",
            ]
        )
    else:
        lines.append("- No approved theorem-frontier state exists yet; this burst must nominate one explicitly.")
    return "\n".join(lines)


def normalize_frontier_enum(value: Any, allowed: Sequence[str], *, label: str) -> str:
    normalized = normalize_frontier_text(value)
    if not normalized:
        raise SupervisorError(f"{label} must be non-empty.")
    normalized = normalized.upper() if all(item.isupper() for item in allowed) else normalized.lower()
    if normalized not in allowed:
        raise SupervisorError(f"Invalid {label} {value!r}. Expected one of {list(allowed)}.")
    return normalized


def normalize_frontier_text_list(value: Any, *, label: str, allow_empty: bool = True) -> List[str]:
    if value in (None, ""):
        if allow_empty:
            return []
        raise SupervisorError(f"{label} must be a non-empty list.")
    if not isinstance(value, list):
        raise SupervisorError(f"{label} must be a list.")
    cleaned: List[str] = []
    for item in value:
        text = normalize_frontier_text(item)
        if not text:
            raise SupervisorError(f"{label} must not contain empty entries.")
        cleaned.append(text)
    if not cleaned and not allow_empty:
        raise SupervisorError(f"{label} must be a non-empty list.")
    return cleaned


def normalize_repo_relative_path(value: Any, *, label: str, required_suffix: Optional[str] = None) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise SupervisorError(f"{label} must be non-empty.")
    pure = PurePosixPath(text)
    if pure.is_absolute() or text.startswith("/") or ":" in pure.parts[0]:
        raise SupervisorError(f"{label} must be a repo-relative path, not {value!r}.")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise SupervisorError(f"{label} must not contain empty, '.' or '..' path segments.")
    normalized = pure.as_posix()
    if required_suffix and not normalized.endswith(required_suffix):
        raise SupervisorError(f"{label} must end with {required_suffix!r}.")
    return normalized


def normalize_repo_relative_path_list(
    value: Any,
    *,
    label: str,
    required_suffix: Optional[str] = None,
    allow_empty: bool = False,
) -> List[str]:
    if value in (None, ""):
        if allow_empty:
            return []
        raise SupervisorError(f"{label} must be a non-empty list.")
    if not isinstance(value, list):
        raise SupervisorError(f"{label} must be a list.")
    cleaned: List[str] = []
    for idx, item in enumerate(value):
        cleaned.append(
            normalize_repo_relative_path(
                item,
                label=f"{label}[{idx}]",
                required_suffix=required_suffix,
            )
        )
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned and not allow_empty:
        raise SupervisorError(f"{label} must be a non-empty list.")
    return cleaned


def default_theorem_frontier_payload(mode: str) -> Dict[str, Any]:
    if mode == "phase0":
        return {
            "mode": "phase0",
            "current": None,
            "metrics": {
                "active_theorem_age": 0,
                "blocker_cluster_age": 0,
                "closed_nodes_count": 0,
                "refuted_nodes_count": 0,
            },
        }
    return {
        "mode": "full",
        "active_leaf_id": None,
        "current_action": None,
        "nodes": {},
        "edges": [],
        "metrics": {
            "active_leaf_age": 0,
            "blocker_cluster_age": 0,
            "closed_nodes_count": 0,
            "refuted_nodes_count": 0,
            "paper_nodes_closed": 0,
            "failed_close_attempts": 0,
            "low_cone_purity_streak": 0,
            "cone_purity": None,
            "structural_churn": 0,
        },
        "escalation": {
            "required": False,
            "reasons": [],
        },
        "paper_verifier_history": [],
        "current": None,
    }


def theorem_frontier_payload(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = state.get("theorem_frontier")
    if isinstance(payload, dict):
        return payload
    return None


def theorem_frontier_active_leaf_id(state: Dict[str, Any]) -> str:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return ""
    return normalize_frontier_text(payload.get("active_leaf_id"))


def theorem_frontier_branch_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict) or normalize_frontier_text(payload.get("mode")).lower() != "full":
        return {}
    active_leaf_id = normalize_frontier_text(payload.get("active_leaf_id"))
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    active_node = nodes.get(active_leaf_id) if active_leaf_id else None
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    escalation = payload.get("escalation") if isinstance(payload.get("escalation"), dict) else {}
    raw_open_hypotheses = current.get("open_hypotheses")
    open_hypotheses = [
        str(item).strip()
        for item in raw_open_hypotheses
        if str(item).strip()
    ] if isinstance(raw_open_hypotheses, list) else []
    return {
        "active_leaf_id": active_leaf_id or None,
        "active_leaf_kind": active_node.get("kind") if isinstance(active_node, dict) else None,
        "active_leaf_anchor": active_node.get("lean_anchor") if isinstance(active_node, dict) else None,
        "active_leaf_nl_statement": active_node.get("natural_language_statement") if isinstance(active_node, dict) else None,
        "active_leaf_lean_statement": active_node.get("lean_statement") if isinstance(active_node, dict) else None,
        "blocker_cluster": (
            str(active_node.get("blocker_cluster", "")).strip()
            if isinstance(active_node, dict)
            else str(current.get("blocker_cluster", "")).strip() or None
        ),
        "current_action": normalize_frontier_text(payload.get("current_action")) or None,
        "assessed_action": normalize_frontier_text(current.get("assessed_action")) or None,
        "open_hypotheses": open_hypotheses,
        "open_hypotheses_count": len(open_hypotheses),
        "active_leaf_age": int(metrics.get("active_leaf_age", 0) or 0),
        "blocker_cluster_age": int(metrics.get("blocker_cluster_age", 0) or 0),
        "failed_close_attempts": int(metrics.get("failed_close_attempts", 0) or 0),
        "cone_purity": metrics.get("cone_purity") if metrics.get("cone_purity") not in ("", None) else None,
        "escalation_required": bool(escalation.get("required")),
        "escalation_reasons": [
            str(item).strip()
            for item in escalation.get("reasons", [])
            if str(item).strip()
        ] if isinstance(escalation.get("reasons"), list) else [],
    }


def branch_selection_question_for_state(state: Dict[str, Any]) -> str:
    summary = theorem_frontier_branch_summary(state)
    active_leaf_id = str(summary.get("active_leaf_id") or "").strip()
    if not active_leaf_id:
        return "Which branch seems more likely to eventually succeed at formalizing the whole paper?"
    anchor = str(summary.get("active_leaf_anchor") or "").strip()
    blocker = str(summary.get("blocker_cluster") or "").strip()
    detail_bits = []
    if anchor:
        detail_bits.append(f"at `{anchor}`")
    if blocker:
        detail_bits.append(f"(current blocker cluster: {blocker})")
    detail_suffix = f" {' '.join(detail_bits)}" if detail_bits else ""
    return (
        f"Which branch seems more likely to close theorem-frontier node `{active_leaf_id}`{detail_suffix} "
        "and then finish formalizing the whole paper?"
    )


def reset_child_branch_theorem_frontier_runtime_state(state: Dict[str, Any]) -> None:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return
    mode = normalize_frontier_text(payload.get("mode")).lower()
    if mode == "phase0":
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        metrics["active_theorem_age"] = 0
        metrics["blocker_cluster_age"] = 0
        payload["metrics"] = metrics
        payload["current"] = None
    elif mode == "full":
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        metrics["active_leaf_age"] = 0
        metrics["blocker_cluster_age"] = 0
        metrics["failed_close_attempts"] = 0
        metrics["low_cone_purity_streak"] = 0
        metrics["cone_purity"] = None
        metrics["structural_churn"] = 0
        payload["metrics"] = metrics
        payload["current_action"] = None
        payload["current"] = None
        payload["escalation"] = {"required": False, "reasons": []}
    state["last_theorem_frontier_worker_update"] = None
    state["last_theorem_frontier_review"] = None
    state["last_theorem_frontier_paper_review"] = None


def write_theorem_frontier_state_file_if_present(state_dir: Path, state: Dict[str, Any]) -> None:
    payload = theorem_frontier_payload(state)
    if isinstance(payload, dict):
        JsonFile.dump(state_dir / "theorem_frontier.json", payload)


THEOREM_FRONTIER_NODE_CORE_FIELDS: Tuple[str, ...] = (
    "node_id",
    "kind",
    "natural_language_statement",
    "lean_statement",
    "closure_mode",
)


def theorem_frontier_node_core(node: Dict[str, Any]) -> Dict[str, Any]:
    return {field: node.get(field) for field in THEOREM_FRONTIER_NODE_CORE_FIELDS}


def assert_theorem_frontier_node_matches_authoritative(
    authoritative: Dict[str, Any],
    candidate: Dict[str, Any],
    *,
    label: str,
) -> None:
    mismatched = [
        field
        for field in THEOREM_FRONTIER_NODE_CORE_FIELDS
        if authoritative.get(field) != candidate.get(field)
    ]
    if mismatched:
        raise SupervisorError(
            f"{label} changed authoritative theorem-frontier fields {mismatched}; "
            "existing DAG nodes are immutable and must be replaced rather than silently edited."
        )


def assert_theorem_frontier_review_matches_node(review: Dict[str, Any], node: Dict[str, Any]) -> None:
    # Only check the identity field; NL/Lean statements may be paraphrased
    review_id = normalize_frontier_text(review.get("active_theorem_id"))
    node_id = normalize_frontier_text(node.get("node_id"))
    if review_id != node_id:
        raise SupervisorError(
            f"Theorem-frontier review active_theorem_id {review_id!r} does not match "
            f"the authoritative active node {node_id!r}."
        )


def validate_loaded_theorem_frontier_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise SupervisorError("Theorem-frontier payload must be a JSON object.")
    mode = normalize_frontier_text(payload.get("mode")).lower()
    if mode == "phase0":
        current = payload.get("current")
        normalized_current = None
        if current is not None:
            if not isinstance(current, dict):
                raise SupervisorError("Phase-0 theorem-frontier current payload must be an object or null.")
            normalized_current = dict(current)
        metrics = payload.get("metrics")
        if metrics is None:
            metrics = default_theorem_frontier_payload("phase0")["metrics"]
        if not isinstance(metrics, dict):
            raise SupervisorError("Phase-0 theorem-frontier metrics must be a mapping.")
        normalized_metrics = {
            "active_theorem_age": int(metrics.get("active_theorem_age", 0) or 0),
            "blocker_cluster_age": int(metrics.get("blocker_cluster_age", 0) or 0),
            "closed_nodes_count": int(metrics.get("closed_nodes_count", 0) or 0),
            "refuted_nodes_count": int(metrics.get("refuted_nodes_count", 0) or 0),
        }
        return {
            "mode": "phase0",
            "current": normalized_current,
            "metrics": normalized_metrics,
        }
    if mode != "full":
        raise SupervisorError(f"Unknown theorem-frontier payload mode: {payload.get('mode')!r}")

    raw_nodes = payload.get("nodes", {})
    if not isinstance(raw_nodes, dict):
        raise SupervisorError("Full theorem-frontier payload nodes must be a mapping.")
    nodes: Dict[str, Dict[str, Any]] = {}
    for node_id, raw_node in raw_nodes.items():
        if not isinstance(raw_node, dict):
            raise SupervisorError(f"Theorem-frontier node {node_id!r} must be an object.")
        validated = validate_theorem_frontier_node(raw_node, require_relationships=True, require_status=True)
        if validated["node_id"] != str(node_id):
            raise SupervisorError(
                f"Theorem-frontier node key {node_id!r} does not match embedded node_id {validated['node_id']!r}."
            )
        nodes[validated["node_id"]] = validated

    raw_edges = payload.get("edges", [])
    if not isinstance(raw_edges, list):
        raise SupervisorError("Full theorem-frontier payload edges must be a list.")
    edges: List[Dict[str, Any]] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            raise SupervisorError("Theorem-frontier edges must be objects.")
        validated_edge = validate_theorem_frontier_edge(raw_edge, require_paper_status=True)
        if validated_edge["parent"] not in nodes or validated_edge["child"] not in nodes:
            raise SupervisorError(
                "Theorem-frontier edge references a missing node: "
                f"{validated_edge['parent']!r} -> {validated_edge['child']!r}"
            )
        edges.append(validated_edge)

    for node in nodes.values():
        for parent_id in node.get("parent_ids", []):
            if parent_id not in nodes:
                raise SupervisorError(f"Theorem-frontier node {node['node_id']!r} references missing parent {parent_id!r}.")
        for child_id in node.get("child_ids", []):
            if child_id not in nodes:
                raise SupervisorError(f"Theorem-frontier node {node['node_id']!r} references missing child {child_id!r}.")

    active_leaf_id = normalize_frontier_text(payload.get("active_leaf_id"))
    if active_leaf_id:
        if active_leaf_id not in nodes:
            raise SupervisorError(f"Theorem-frontier active_leaf_id {active_leaf_id!r} is not present in nodes.")
        if nodes[active_leaf_id]["status"] not in {"open", "active"}:
            raise SupervisorError(
                f"Theorem-frontier active_leaf_id {active_leaf_id!r} must name an open/active node, "
                f"not {nodes[active_leaf_id]['status']!r}."
            )
    active_nodes = sorted(node_id for node_id, node in nodes.items() if node.get("status") == "active")
    if active_leaf_id:
        if active_nodes != [active_leaf_id]:
            raise SupervisorError(
                "Full theorem-frontier payload must have exactly one active node matching active_leaf_id; "
                f"found active nodes {active_nodes!r} with active_leaf_id={active_leaf_id!r}."
            )
    elif active_nodes:
        raise SupervisorError(
            "Full theorem-frontier payload has active nodes but no active_leaf_id: "
            f"{active_nodes!r}."
        )

    raw_metrics = payload.get("metrics", {})
    if not isinstance(raw_metrics, dict):
        raise SupervisorError("Full theorem-frontier metrics must be a mapping.")
    metrics = {
        "active_leaf_age": int(raw_metrics.get("active_leaf_age", 0) or 0),
        "blocker_cluster_age": int(raw_metrics.get("blocker_cluster_age", 0) or 0),
        "closed_nodes_count": int(raw_metrics.get("closed_nodes_count", 0) or 0),
        "refuted_nodes_count": int(raw_metrics.get("refuted_nodes_count", 0) or 0),
        "paper_nodes_closed": int(raw_metrics.get("paper_nodes_closed", 0) or 0),
        "failed_close_attempts": int(raw_metrics.get("failed_close_attempts", 0) or 0),
        "low_cone_purity_streak": int(raw_metrics.get("low_cone_purity_streak", 0) or 0),
        "cone_purity": None,
        "structural_churn": int(raw_metrics.get("structural_churn", 0) or 0),
    }
    cone_purity = raw_metrics.get("cone_purity")
    if cone_purity not in (None, ""):
        metrics["cone_purity"] = theorem_frontier_cone_purity(cone_purity)

    raw_escalation = payload.get("escalation", {"required": False, "reasons": []})
    if not isinstance(raw_escalation, dict):
        raise SupervisorError("Full theorem-frontier escalation payload must be a mapping.")
    escalation = {
        "required": bool(raw_escalation.get("required")),
        "reasons": normalize_frontier_text_list(
            raw_escalation.get("reasons"),
            label="theorem_frontier.escalation.reasons",
        ),
    }

    current_action = payload.get("current_action")
    normalized_current_action = None
    if current_action not in (None, ""):
        normalized_current_action = validate_theorem_frontier_action(current_action)

    history = payload.get("paper_verifier_history", [])
    if not isinstance(history, list):
        raise SupervisorError("Full theorem-frontier paper_verifier_history must be a list.")
    normalized_history: List[Dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            raise SupervisorError("paper_verifier_history entries must be objects.")
        normalized_history.append(
            validate_theorem_frontier_paper_verifier_review("proof_formalization", item)
        )

    current = payload.get("current")
    if current is not None and not isinstance(current, dict):
        raise SupervisorError("Full theorem-frontier current payload must be an object or null.")

    return {
        "mode": "full",
        "active_leaf_id": active_leaf_id or None,
        "current_action": normalized_current_action,
        "nodes": nodes,
        "edges": edges,
        "metrics": metrics,
        "escalation": escalation,
        "paper_verifier_history": normalized_history,
        "current": dict(current) if isinstance(current, dict) else None,
    }


def theorem_frontier_node_kind(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_NODE_KINDS, label="theorem frontier node kind")


def theorem_frontier_node_status(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_NODE_STATUSES, label="theorem frontier node status")


def theorem_frontier_closure_mode(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_CLOSURE_MODES, label="theorem frontier closure mode")


THEOREM_FRONTIER_EDGE_TYPE_ALIASES: Dict[str, str] = {
    "depends_on": "reduction",
    "dependency": "reduction",
    "requires": "reduction",
    "implies": "reduction",
    "case": "case_split",
    "split": "case_split",
    "alternative": "any_of",
    "replace": "replacement",
    "equivalent": "equivalence",
    "stronger": "strengthening",
    "generalization": "strengthening",
}


def theorem_frontier_edge_type(value: Any) -> str:
    normalized = normalize_frontier_text(value).lower()
    if normalized in THEOREM_FRONTIER_EDGE_TYPE_ALIASES:
        normalized = THEOREM_FRONTIER_EDGE_TYPE_ALIASES[normalized]
    return normalize_frontier_enum(normalized, THEOREM_FRONTIER_EDGE_TYPES, label="theorem frontier edge type")


def theorem_frontier_paper_decision(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_PAPER_DECISIONS, label="paper verifier decision")


def theorem_frontier_paper_classification(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_PAPER_CLASSIFICATIONS, label="paper verifier classification")


def theorem_frontier_cone_purity(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_CONE_PURITY_LEVELS, label="theorem frontier cone purity")


def validate_theorem_frontier_node(
    node: Dict[str, Any],
    *,
    require_relationships: bool,
    require_status: bool,
) -> Dict[str, Any]:
    required_keys = {
        "node_id",
        "kind",
        "natural_language_statement",
        "lean_statement",
        "lean_anchor",
        "paper_provenance",
        "closure_mode",
        "blocker_cluster",
        "acceptance_evidence",
        "notes",
    }
    if require_relationships:
        required_keys.update({"parent_ids", "child_ids"})
    if require_status:
        required_keys.add("status")
    missing = required_keys.difference(node)
    if missing:
        raise SupervisorError(f"Theorem-frontier node missing keys: {sorted(missing)}")
    validated = dict(node)
    validated["node_id"] = normalize_frontier_text(validated.get("node_id"))
    validated["kind"] = theorem_frontier_node_kind(validated.get("kind"))
    validated["natural_language_statement"] = normalize_frontier_text(validated.get("natural_language_statement"))
    validated["lean_statement"] = normalize_frontier_text(validated.get("lean_statement"))
    validated["lean_anchor"] = normalize_frontier_text(validated.get("lean_anchor"))
    validated["paper_provenance"] = normalize_frontier_text(validated.get("paper_provenance"))
    validated["closure_mode"] = theorem_frontier_closure_mode(validated.get("closure_mode"))
    validated["blocker_cluster"] = normalize_frontier_text(validated.get("blocker_cluster"))
    validated["acceptance_evidence"] = normalize_frontier_text(validated.get("acceptance_evidence"))
    validated["notes"] = normalize_frontier_text(validated.get("notes"))
    if require_status:
        validated["status"] = theorem_frontier_node_status(validated.get("status"))
    if require_relationships:
        validated["parent_ids"] = normalize_frontier_text_list(validated.get("parent_ids"), label="node.parent_ids")
        validated["child_ids"] = normalize_frontier_text_list(validated.get("child_ids"), label="node.child_ids")
    for key in (
        "node_id",
        "natural_language_statement",
        "lean_statement",
        "lean_anchor",
        "paper_provenance",
    ):
        if not validated[key]:
            raise SupervisorError(f"Theorem-frontier node field {key} must be non-empty.")
    for key in ("blocker_cluster", "acceptance_evidence", "notes"):
        if key not in validated or validated[key] is None:
            validated[key] = ""
    return validated


def validate_theorem_frontier_edge(edge: Dict[str, Any], *, require_paper_status: bool) -> Dict[str, Any]:
    required_keys = {"parent", "child", "edge_type", "justification"}
    if require_paper_status:
        required_keys.add("paper_verifier_status")
    missing = required_keys.difference(edge)
    if missing:
        raise SupervisorError(f"Theorem-frontier edge missing keys: {sorted(missing)}")
    validated = dict(edge)
    validated["parent"] = normalize_frontier_text(validated.get("parent"))
    validated["child"] = normalize_frontier_text(validated.get("child"))
    validated["edge_type"] = theorem_frontier_edge_type(validated.get("edge_type"))
    validated["justification"] = normalize_frontier_text(validated.get("justification"))
    if require_paper_status:
        validated["paper_verifier_status"] = theorem_frontier_paper_decision(validated.get("paper_verifier_status"))
    if not validated["parent"] or not validated["child"] or not validated["justification"]:
        raise SupervisorError("Theorem-frontier edge fields parent, child, and justification must be non-empty.")
    return validated


def validate_paper_main_results_manifest(phase: str, manifest: Any) -> Dict[str, Any]:
    if not isinstance(manifest, dict):
        raise SupervisorError("Paper main-results manifest must be a JSON object.")
    required_keys = {"phase", "main_results", "dependency_edges", "initial_active_node_id"}
    missing = required_keys.difference(manifest)
    if missing:
        raise SupervisorError(f"Paper main-results manifest missing keys: {sorted(missing)}")
    validated = dict(manifest)
    if str(validated.get("phase")).strip().lower() != phase:
        raise SupervisorError(
            f"Paper main-results manifest phase mismatch: expected {phase}, got {validated.get('phase')}"
        )
    raw_results = validated.get("main_results")
    if not isinstance(raw_results, list) or not raw_results:
        raise SupervisorError("Paper main-results manifest must contain a non-empty `main_results` list.")
    raw_edges = validated.get("dependency_edges")
    if not isinstance(raw_edges, list):
        raise SupervisorError("Paper main-results manifest field `dependency_edges` must be a list.")
    results: List[Dict[str, Any]] = []
    node_ids: Set[str] = set()
    for raw_node in raw_results:
        if not isinstance(raw_node, dict):
            raise SupervisorError("Every entry in `main_results` must be a JSON object.")
        node = validate_theorem_frontier_node(raw_node, require_relationships=False, require_status=False)
        if node["kind"] not in {"paper", "paper_faithful_reformulation"}:
            raise SupervisorError(
                "Paper main-results manifest may only contain `paper` or `paper_faithful_reformulation` nodes, "
                f"got {node['kind']!r} for {node['node_id']!r}."
            )
        if node["node_id"] in node_ids:
            raise SupervisorError(f"Duplicate paper main-result node id: {node['node_id']!r}")
        node_ids.add(node["node_id"])
        results.append(node)
    edges: List[Dict[str, Any]] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            raise SupervisorError("Every entry in `dependency_edges` must be a JSON object.")
        edge = validate_theorem_frontier_edge(raw_edge, require_paper_status=False)
        if edge["parent"] not in node_ids or edge["child"] not in node_ids:
            raise SupervisorError(
                "Paper main-results dependency edges must stay within the declared main-result node set: "
                f"{edge['parent']!r} -> {edge['child']!r}."
            )
        edges.append(edge)
    validated["main_results"] = results
    validated["dependency_edges"] = edges
    validated["initial_active_node_id"] = normalize_frontier_text(validated.get("initial_active_node_id"))
    if not validated["initial_active_node_id"]:
        raise SupervisorError("Paper main-results manifest field `initial_active_node_id` must be non-empty.")
    if validated["initial_active_node_id"] not in node_ids:
        raise SupervisorError(
            "Paper main-results manifest `initial_active_node_id` must name one of the declared main results, "
            f"got {validated['initial_active_node_id']!r}."
        )
    return validated


def load_validated_paper_main_results_manifest(config: Config) -> Dict[str, Any]:
    path = paper_main_results_manifest_path(config)
    if not path.exists():
        raise SupervisorError(
            "Cannot enter proof_formalization without a paper main-results manifest at "
            f"{path}."
        )
    return validate_paper_main_results_manifest("theorem_stating", JsonFile.load(path, None))


def validate_theorem_frontier_worker_update_full(phase: str, update: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "active_node",
        "requested_action",
        "cone_scope",
        "allowed_edit_paths",
        "result_summary",
        "proposed_nodes",
        "proposed_edges",
        "next_candidate_ids",
        "structural_change_reason",
    }
    missing = required_keys.difference(update)
    if missing:
        raise SupervisorError(f"Theorem-frontier worker update missing keys: {sorted(missing)}")
    if str(update.get("phase", "")).strip().lower() != phase:
        print(f"WARNING: Theorem-frontier worker update phase mismatch: expected {phase}, got {update.get('phase')}; accepting anyway.")
    validated_phase = phase
    if not isinstance(update.get("active_node"), dict):
        raise SupervisorError("Theorem-frontier worker update field active_node must be an object.")
    validated = dict(update)
    validated["active_node"] = validate_theorem_frontier_node(
        dict(validated["active_node"]),
        require_relationships=False,
        require_status=False,
    )
    validated["requested_action"] = validate_theorem_frontier_action(validated.get("requested_action"))
    validated["cone_scope"] = normalize_frontier_text(validated.get("cone_scope"))
    validated["allowed_edit_paths"] = normalize_repo_relative_path_list(
        validated.get("allowed_edit_paths"),
        label="theorem frontier worker update allowed_edit_paths",
        required_suffix=".lean",
        allow_empty=False,
    )
    validated["result_summary"] = normalize_frontier_text(validated.get("result_summary"))
    validated["proposed_nodes"] = [
        validate_theorem_frontier_node(dict(node), require_relationships=False, require_status=False)
        for node in (validated.get("proposed_nodes") or [])
    ]
    if not isinstance(validated.get("proposed_edges"), list):
        raise SupervisorError("Theorem-frontier worker update field proposed_edges must be a list.")
    validated["proposed_edges"] = [
        validate_theorem_frontier_edge(dict(edge), require_paper_status=False)
        for edge in validated.get("proposed_edges", [])
    ]
    validated["next_candidate_ids"] = normalize_frontier_text_list(
        validated.get("next_candidate_ids"),
        label="theorem_frontier.next_candidate_ids",
    )
    validated["structural_change_reason"] = normalize_frontier_text(validated.get("structural_change_reason"))
    if not validated["cone_scope"] or not validated["result_summary"]:
        raise SupervisorError("Theorem-frontier worker update fields cone_scope and result_summary must be non-empty.")
    proposed_ids = [node["node_id"] for node in validated["proposed_nodes"]]
    if len(proposed_ids) != len(set(proposed_ids)):
        raise SupervisorError("Theorem-frontier worker update proposed_nodes must have unique node_id values.")
    # proposed_edges are validated against the authoritative DAG later in
    # update_theorem_frontier_full_state; at this stage we only validate
    # their individual field schemas (done above).
    # next_candidate_ids are suggestions; they will be validated against the
    # authoritative DAG later in update_theorem_frontier_full_state.  At this
    # stage we only require them to be non-empty strings.
    return validated


def validate_theorem_frontier_review_full(phase: str, review: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "active_theorem_id",
        "active_theorem_nl_statement",
        "active_theorem_lean_statement",
        "active_theorem_anchor",
        "assessed_action",
        "blocker_cluster",
        "outcome",
        "next_active_theorem_id",
        "cone_purity",
        "open_hypotheses",
        "justification",
    }
    missing = required_keys.difference(review)
    if missing:
        raise SupervisorError(f"Theorem-frontier review missing keys: {sorted(missing)}")
    if str(review.get("phase", "")).strip().lower() != phase:
        print(f"WARNING: Theorem-frontier review phase mismatch: expected {phase}, got {review.get('phase')}; accepting anyway.")
    validated = dict(review)
    validated["phase"] = phase
    validated["active_theorem_id"] = normalize_frontier_text(validated.get("active_theorem_id"))
    validated["active_theorem_nl_statement"] = normalize_frontier_text(validated.get("active_theorem_nl_statement"))
    validated["active_theorem_lean_statement"] = normalize_frontier_text(validated.get("active_theorem_lean_statement"))
    validated["active_theorem_anchor"] = normalize_frontier_text(validated.get("active_theorem_anchor"))
    validated["assessed_action"] = validate_theorem_frontier_action(validated.get("assessed_action"))
    validated["blocker_cluster"] = normalize_frontier_text(validated.get("blocker_cluster"))
    validated["outcome"] = validate_theorem_frontier_outcome(validated.get("outcome"))
    validated["next_active_theorem_id"] = normalize_frontier_text(validated.get("next_active_theorem_id"))
    validated["cone_purity"] = theorem_frontier_cone_purity(validated.get("cone_purity"))
    validated["open_hypotheses"] = normalize_frontier_text_list(
        validated.get("open_hypotheses"),
        label="theorem_frontier.open_hypotheses",
    )
    validated["justification"] = normalize_frontier_text(validated.get("justification"))
    for key in (
        "active_theorem_id",
        "active_theorem_nl_statement",
        "active_theorem_lean_statement",
        "active_theorem_anchor",
        "justification",
    ):
        if not validated[key]:
            raise SupervisorError(f"Theorem-frontier review field {key} must be non-empty.")
    if not validated.get("blocker_cluster"):
        validated["blocker_cluster"] = ""
    return validated


def validate_theorem_frontier_paper_verifier_review(phase: str, review: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "parent_node_id",
        "change_kind",
        "decision",
        "classification",
        "approved_node_ids",
        "approved_edges",
        "justification",
        "caveat",
    }
    missing = required_keys.difference(review)
    if missing:
        raise SupervisorError(f"Theorem-frontier paper-verifier review missing keys: {sorted(missing)}")
    if str(review.get("phase")).strip().lower() != phase:
        raise SupervisorError(
            f"Theorem-frontier paper-verifier phase mismatch: expected {phase}, got {review.get('phase')}"
        )
    validated = dict(review)
    validated["parent_node_id"] = normalize_frontier_text(validated.get("parent_node_id"))
    validated["change_kind"] = normalize_frontier_enum(
        validated.get("change_kind"),
        ("CREATE_ACTIVE", "EXPAND", "REFUTE_REPLACE"),
        label="paper verifier change kind",
    )
    validated["decision"] = theorem_frontier_paper_decision(validated.get("decision"))
    validated["classification"] = theorem_frontier_paper_classification(validated.get("classification"))
    validated["approved_node_ids"] = normalize_frontier_text_list(
        validated.get("approved_node_ids"),
        label="paper_verifier.approved_node_ids",
    )
    if not isinstance(validated.get("approved_edges"), list):
        raise SupervisorError("paper_verifier.approved_edges must be a list.")
    approved_edges: List[Dict[str, str]] = []
    for entry in validated.get("approved_edges", []):
        if not isinstance(entry, dict):
            raise SupervisorError("paper_verifier.approved_edges entries must be objects.")
        approved_edges.append(
            {
                "parent": normalize_frontier_text(entry.get("parent")),
                "child": normalize_frontier_text(entry.get("child")),
            }
        )
    validated["approved_edges"] = approved_edges
    validated["justification"] = normalize_frontier_text(validated.get("justification"))
    validated["caveat"] = normalize_frontier_text(validated.get("caveat"))
    if not validated["parent_node_id"] or not validated["justification"]:
        raise SupervisorError("paper_verifier.parent_node_id and paper_verifier.justification must be non-empty.")
    return validated


def theorem_frontier_requires_paper_verifier(
    state: Dict[str, Any],
    worker_update: Dict[str, Any],
) -> bool:
    active_node = worker_update["active_node"]
    payload = theorem_frontier_payload(state)
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    active_node_known = isinstance(nodes, dict) and active_node["node_id"] in nodes
    return (
        not active_node_known
        or worker_update["requested_action"] in {"EXPAND", "REFUTE_REPLACE"}
        or bool(worker_update["proposed_nodes"])
        or bool(worker_update["proposed_edges"])
    )


def validate_theorem_frontier_worker_update(phase: str, update: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "active_theorem_id",
        "active_theorem_nl_statement",
        "active_theorem_lean_statement",
        "active_theorem_anchor",
        "requested_action",
        "blocker_cluster",
        "cone_scope",
        "result_summary",
    }
    missing = required_keys.difference(update)
    if missing:
        raise SupervisorError(f"Theorem-frontier worker update missing keys: {sorted(missing)}")
    if str(update.get("phase")).strip().lower() != phase:
        raise SupervisorError(
            f"Theorem-frontier worker update phase mismatch: expected {phase}, got {update.get('phase')}"
        )
    update["active_theorem_id"] = normalize_frontier_text(update.get("active_theorem_id"))
    update["active_theorem_nl_statement"] = normalize_frontier_text(update.get("active_theorem_nl_statement"))
    update["active_theorem_lean_statement"] = normalize_frontier_text(update.get("active_theorem_lean_statement"))
    update["active_theorem_anchor"] = normalize_frontier_text(update.get("active_theorem_anchor"))
    update["requested_action"] = validate_theorem_frontier_action(update.get("requested_action"))
    update["blocker_cluster"] = normalize_frontier_text(update.get("blocker_cluster"))
    update["cone_scope"] = normalize_frontier_text(update.get("cone_scope"))
    update["result_summary"] = normalize_frontier_text(update.get("result_summary"))
    for key in (
        "active_theorem_id",
        "active_theorem_nl_statement",
        "active_theorem_lean_statement",
        "active_theorem_anchor",
        "blocker_cluster",
        "cone_scope",
        "result_summary",
    ):
        if not update[key]:
            raise SupervisorError(f"Theorem-frontier worker update field {key} must be non-empty.")
    return update


def validate_theorem_frontier_worker_update_for_mode(config: Config, phase: str, update: Dict[str, Any]) -> Dict[str, Any]:
    if theorem_frontier_full_enabled(config, phase):
        return validate_theorem_frontier_worker_update_full(phase, update)
    return validate_theorem_frontier_worker_update(phase, update)


def validate_theorem_frontier_review(phase: str, review: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "active_theorem_id",
        "active_theorem_nl_statement",
        "active_theorem_lean_statement",
        "active_theorem_anchor",
        "assessed_action",
        "blocker_cluster",
        "outcome",
        "justification",
    }
    missing = required_keys.difference(review)
    if missing:
        raise SupervisorError(f"Theorem-frontier review missing keys: {sorted(missing)}")
    if str(review.get("phase")).strip().lower() != phase:
        raise SupervisorError(f"Theorem-frontier review phase mismatch: expected {phase}, got {review.get('phase')}")
    review["active_theorem_id"] = normalize_frontier_text(review.get("active_theorem_id"))
    review["active_theorem_nl_statement"] = normalize_frontier_text(review.get("active_theorem_nl_statement"))
    review["active_theorem_lean_statement"] = normalize_frontier_text(review.get("active_theorem_lean_statement"))
    review["active_theorem_anchor"] = normalize_frontier_text(review.get("active_theorem_anchor"))
    review["assessed_action"] = validate_theorem_frontier_action(review.get("assessed_action"))
    review["blocker_cluster"] = normalize_frontier_text(review.get("blocker_cluster"))
    review["outcome"] = validate_theorem_frontier_outcome(review.get("outcome"))
    review["justification"] = normalize_frontier_text(review.get("justification"))
    for key in (
        "active_theorem_id",
        "active_theorem_nl_statement",
        "active_theorem_lean_statement",
        "active_theorem_anchor",
        "blocker_cluster",
        "justification",
    ):
        if not review[key]:
            raise SupervisorError(f"Theorem-frontier review field {key} must be non-empty.")
    return review


def validate_theorem_frontier_review_for_mode(config: Config, phase: str, review: Dict[str, Any]) -> Dict[str, Any]:
    if theorem_frontier_full_enabled(config, phase):
        return validate_theorem_frontier_review_full(phase, review)
    return validate_theorem_frontier_review(phase, review)


def update_theorem_frontier_state(config: Config, state: Dict[str, Any], review: Dict[str, Any], *, cycle: int) -> Dict[str, Any]:
    previous = theorem_frontier_current(state)
    normalized_blocker = normalize_frontier_text(review.get("blocker_cluster"))
    normalized_id = normalize_frontier_text(review.get("active_theorem_id"))
    same_node = (
        isinstance(previous, dict)
        and normalize_frontier_text(previous.get("active_theorem_id")) == normalized_id
    )
    same_blocker = (
        isinstance(previous, dict)
        and normalize_frontier_text(previous.get("blocker_cluster")) == normalized_blocker
    )
    active_age = int(previous.get("active_theorem_age", 0) or 0) + 1 if same_node else 1
    blocker_age = int(previous.get("blocker_cluster_age", 0) or 0) + 1 if same_blocker else 1
    metrics = {
        "active_theorem_age": active_age,
        "blocker_cluster_age": blocker_age,
        "closed_nodes_count": int((previous or {}).get("closed_nodes_count", 0) or 0),
        "refuted_nodes_count": int((previous or {}).get("refuted_nodes_count", 0) or 0),
    }
    if review["outcome"] == "CLOSED":
        metrics["closed_nodes_count"] += 1
    if review["outcome"] == "REFUTED_REPLACED":
        metrics["refuted_nodes_count"] += 1
    current = {
        "mode": "phase0",
        "cycle": cycle,
        "active_theorem_id": review["active_theorem_id"],
        "active_theorem_nl_statement": review["active_theorem_nl_statement"],
        "active_theorem_lean_statement": review["active_theorem_lean_statement"],
        "active_theorem_anchor": review["active_theorem_anchor"],
        "assessed_action": review["assessed_action"],
        "blocker_cluster": review["blocker_cluster"],
        "outcome": review["outcome"],
        "justification": review["justification"],
        "active_theorem_age": active_age,
        "blocker_cluster_age": blocker_age,
        "closed_nodes_count": metrics["closed_nodes_count"],
        "refuted_nodes_count": metrics["refuted_nodes_count"],
        "updated_at": timestamp_now(),
    }
    payload = {
        "mode": "phase0",
        "current": current,
        "metrics": metrics,
    }
    state["theorem_frontier"] = current
    state["last_theorem_frontier_review"] = review
    JsonFile.dump(theorem_frontier_state_path(config), payload)
    append_jsonl(
        theorem_frontier_history_path(config),
        {
            "cycle": cycle,
            **current,
        },
    )
    return current


def theorem_frontier_node_record(
    node: Dict[str, Any],
    *,
    status: str,
    parent_ids: Optional[Sequence[str]] = None,
    child_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    record = dict(node)
    record["status"] = theorem_frontier_node_status(status)
    record["parent_ids"] = list(dict.fromkeys(parent_ids or []))
    record["child_ids"] = list(dict.fromkeys(child_ids or []))
    record["updated_at"] = timestamp_now()
    return validate_theorem_frontier_node(record, require_relationships=True, require_status=True)


def upsert_theorem_frontier_node(
    nodes: Dict[str, Dict[str, Any]],
    node: Dict[str, Any],
    *,
    default_status: str,
) -> Dict[str, Any]:
    existing = nodes.get(node["node_id"])
    parent_ids = existing.get("parent_ids", []) if isinstance(existing, dict) else []
    child_ids = existing.get("child_ids", []) if isinstance(existing, dict) else []
    status = existing.get("status", default_status) if isinstance(existing, dict) else default_status
    record = theorem_frontier_node_record(node, status=status, parent_ids=parent_ids, child_ids=child_ids)
    nodes[record["node_id"]] = record
    return record


def add_theorem_frontier_edge(
    payload: Dict[str, Any],
    edge: Dict[str, Any],
    *,
    paper_status: str,
) -> None:
    nodes = payload.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        raise SupervisorError("Theorem-frontier payload nodes must be a mapping.")
    edges = payload.setdefault("edges", [])
    if not isinstance(edges, list):
        raise SupervisorError("Theorem-frontier payload edges must be a list.")
    edge_record = validate_theorem_frontier_edge(
        {
            **edge,
            "paper_verifier_status": paper_status,
        },
        require_paper_status=True,
    )
    if edge_record["parent"] not in nodes or edge_record["child"] not in nodes:
        raise SupervisorError(
            "Cannot add a theorem-frontier edge unless both endpoints are already present in the authoritative DAG: "
            f"{edge_record['parent']!r} -> {edge_record['child']!r}."
        )
    existing = next(
        (
            item
            for item in edges
            if isinstance(item, dict)
            and item.get("parent") == edge_record["parent"]
            and item.get("child") == edge_record["child"]
            and item.get("edge_type") == edge_record["edge_type"]
        ),
        None,
    )
    if existing is None:
        edges.append(edge_record)
    else:
        existing.update(edge_record)
    parent = nodes.get(edge_record["parent"])
    child = nodes.get(edge_record["child"])
    if isinstance(parent, dict):
        parent["child_ids"] = list(dict.fromkeys([*parent.get("child_ids", []), edge_record["child"]]))
    if isinstance(child, dict):
        child["parent_ids"] = list(dict.fromkeys([*child.get("parent_ids", []), edge_record["parent"]]))


def seed_theorem_frontier_from_main_results_manifest(
    config: Config,
    state: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    cycle: int,
) -> Dict[str, Any]:
    payload = default_theorem_frontier_payload("full")
    nodes: Dict[str, Dict[str, Any]] = {}
    initial_active_node_id = manifest["initial_active_node_id"]
    for node in manifest["main_results"]:
        status = "active" if node["node_id"] == initial_active_node_id else "open"
        nodes[node["node_id"]] = theorem_frontier_node_record(node, status=status)
    payload["nodes"] = nodes
    payload["active_leaf_id"] = initial_active_node_id
    payload["current_action"] = None
    payload["current"] = None
    payload["paper_verifier_history"] = []
    for edge in manifest["dependency_edges"]:
        add_theorem_frontier_edge(payload, edge, paper_status="APPROVE")
    validated_payload = validate_loaded_theorem_frontier_payload(payload)
    state["theorem_frontier"] = validated_payload
    state["last_theorem_frontier_worker_update"] = None
    state["last_theorem_frontier_review"] = None
    state["last_theorem_frontier_paper_review"] = None
    JsonFile.dump(theorem_frontier_state_path(config), validated_payload)
    append_jsonl(
        theorem_frontier_history_path(config),
        {
            "cycle": cycle,
            "mode": "full",
            "event": "seed",
            "active_leaf_id": initial_active_node_id,
            "main_result_node_ids": [node["node_id"] for node in manifest["main_results"]],
            "dependency_edge_count": len(manifest["dependency_edges"]),
            "updated_at": timestamp_now(),
        },
    )
    return validated_payload


def update_theorem_frontier_full_state(
    config: Config,
    state: Dict[str, Any],
    worker_update: Dict[str, Any],
    review: Dict[str, Any],
    paper_review: Optional[Dict[str, Any]],
    *,
    cycle: int,
) -> Dict[str, Any]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict) or payload.get("mode") != "full":
        payload = default_theorem_frontier_payload("full")
    nodes = payload.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        raise SupervisorError("Theorem-frontier payload nodes must be a mapping.")
    metrics = payload.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        raise SupervisorError("Theorem-frontier payload metrics must be a mapping.")
    escalation = payload.setdefault("escalation", {"required": False, "reasons": []})
    if not isinstance(escalation, dict):
        raise SupervisorError("Theorem-frontier payload escalation must be a mapping.")
    payload.setdefault("paper_verifier_history", [])

    previous_active_leaf_id = normalize_frontier_text(payload.get("active_leaf_id"))
    previous_active = nodes.get(previous_active_leaf_id) if previous_active_leaf_id else None
    previous_blocker = (
        normalize_frontier_text(previous_active.get("blocker_cluster"))
        if isinstance(previous_active, dict)
        else ""
    )
    active_node = worker_update["active_node"]
    active_node_id = active_node["node_id"]
    active_node_known = active_node_id in nodes
    requested_action = worker_update["requested_action"]
    outcome = review["outcome"]
    paper_decision = paper_review.get("decision") if isinstance(paper_review, dict) else None
    requires_paper_verifier = theorem_frontier_requires_paper_verifier({"theorem_frontier": payload}, worker_update)
    proposed_nodes = list(worker_update.get("proposed_nodes", []))
    proposed_edges = list(worker_update.get("proposed_edges", []))
    proposed_node_ids = {node["node_id"] for node in proposed_nodes}
    proposed_edge_pairs = {(edge["parent"], edge["child"]) for edge in proposed_edges}
    approved_node_ids: set[str] = set()
    approved_edge_pairs: set[Tuple[str, str]] = set()

    if isinstance(paper_review, dict):
        approved_node_ids = set(paper_review.get("approved_node_ids", []) or [])
        approved_edge_pairs = {
            (entry.get("parent", ""), entry.get("child", ""))
            for entry in (paper_review.get("approved_edges", []) or [])
            if isinstance(entry, dict)
        }
        expected_change_kind = (
            "CREATE_ACTIVE"
            if not active_node_known
            else ("REFUTE_REPLACE" if requested_action == "REFUTE_REPLACE" else "EXPAND")
        )
        if requires_paper_verifier and paper_review.get("change_kind") != expected_change_kind:
            raise SupervisorError(
                "Paper-verifier change_kind does not match the structural theorem-frontier action being applied: "
                f"expected {expected_change_kind!r}, got {paper_review.get('change_kind')!r}."
            )
        allowed_approved_node_ids = {active_node_id} | proposed_node_ids
        if not approved_node_ids.issubset(allowed_approved_node_ids):
            raise SupervisorError(
                "Paper-verifier approved_node_ids must refer only to the active node being created or worker-proposed nodes."
            )
        if not approved_edge_pairs.issubset(proposed_edge_pairs):
            raise SupervisorError(
                "Paper-verifier approved_edges must be a subset of the worker-proposed theorem-frontier edges."
            )
        for parent_id, child_id in approved_edge_pairs:
            if parent_id not in approved_node_ids and parent_id != active_node_id and parent_id not in nodes:
                raise SupervisorError(
                    "Paper-verifier approved_edges may only reference nodes that are already authoritative or explicitly approved."
                )
            if child_id not in approved_node_ids and child_id not in nodes:
                raise SupervisorError(
                    "Paper-verifier approved_edges may only reference nodes that are already authoritative or explicitly approved."
                )

    if not active_node_known and paper_decision not in {"APPROVE", "APPROVE_WITH_CAVEAT"}:
        raise SupervisorError(
            "Cannot create or activate a theorem-frontier node that is not already in the authoritative DAG "
            "without paper-verifier approval."
        )
    if not active_node_known and active_node_id not in approved_node_ids:
        raise SupervisorError(
            "Paper-verifier approval for CREATE_ACTIVE must explicitly include the active node id in approved_node_ids."
        )
    if active_node_known:
        assert_theorem_frontier_node_matches_authoritative(
            nodes[active_node_id],
            active_node,
            label="Worker active_node",
        )
    if requires_paper_verifier and outcome in {"EXPANDED", "REFUTED_REPLACED"} and paper_decision not in {"APPROVE", "APPROVE_WITH_CAVEAT"}:
        raise SupervisorError("Cannot accept a structural theorem-frontier outcome without paper-verifier approval.")

    if paper_decision == "REJECT" and outcome in {"EXPANDED", "REFUTED_REPLACED"}:
        raise SupervisorError("Paper-verifier rejected the structural change, so the structural outcome cannot be accepted.")

    if isinstance(previous_active, dict) and previous_active.get("status") == "active":
        previous_active["status"] = "open"
        previous_active["updated_at"] = timestamp_now()

    active_record = upsert_theorem_frontier_node(nodes, active_node, default_status="open")
    assert_theorem_frontier_review_matches_node(review, active_record)
    active_record["status"] = "active"
    active_record["updated_at"] = timestamp_now()
    nodes[active_node_id] = active_record

    if isinstance(paper_review, dict):
        history = payload.get("paper_verifier_history")
        if isinstance(history, list):
            history.append(dict(paper_review))

    if paper_decision in {"APPROVE", "APPROVE_WITH_CAVEAT"}:
        for node in proposed_nodes:
            if node["node_id"] not in approved_node_ids:
                continue
            upsert_theorem_frontier_node(nodes, node, default_status="open")
        for edge in proposed_edges:
            if (edge["parent"], edge["child"]) not in approved_edge_pairs:
                continue
            add_theorem_frontier_edge(payload, edge, paper_status=paper_decision)

    next_active_leaf_id = review["next_active_theorem_id"] or active_node_id
    if outcome == "CLOSED":
        active_record["status"] = "closed"
        next_active_leaf_id = review["next_active_theorem_id"] or ""
    elif outcome == "REFUTED_REPLACED":
        active_record["status"] = "replaced" if worker_update.get("proposed_nodes") else "refuted"
        if not next_active_leaf_id and worker_update.get("next_candidate_ids"):
            next_active_leaf_id = worker_update["next_candidate_ids"][0]
    elif outcome == "EXPANDED":
        active_record["status"] = "open"
        if not next_active_leaf_id and worker_update.get("next_candidate_ids"):
            next_active_leaf_id = worker_update["next_candidate_ids"][0]
    elif outcome in {"STILL_OPEN", "NO_FRONTIER_PROGRESS"}:
        next_active_leaf_id = review["next_active_theorem_id"] or active_node_id

    if outcome == "CLOSED" and next_active_leaf_id == active_node_id:
        raise SupervisorError("A theorem-frontier node cannot be both CLOSED and the next active leaf in the same review.")
    if next_active_leaf_id:
        if next_active_leaf_id not in nodes:
            raise SupervisorError(f"Next active theorem id {next_active_leaf_id!r} is not present in the theorem-frontier DAG.")
        if nodes[next_active_leaf_id].get("status") in {"closed", "refuted", "replaced"}:
            raise SupervisorError(
                f"Next active theorem id {next_active_leaf_id!r} refers to a non-open node with status "
                f"{nodes[next_active_leaf_id].get('status')!r}."
            )
        nodes[next_active_leaf_id]["status"] = "active"
        nodes[next_active_leaf_id]["updated_at"] = timestamp_now()
        payload["active_leaf_id"] = next_active_leaf_id
        current_node = nodes[next_active_leaf_id]
    else:
        payload["active_leaf_id"] = None
        current_node = None

    same_leaf = previous_active_leaf_id and previous_active_leaf_id == payload.get("active_leaf_id")
    same_blocker = bool(previous_blocker) and previous_blocker == review["blocker_cluster"]
    active_leaf_age = int(metrics.get("active_leaf_age", 0) or 0) + 1 if same_leaf and payload.get("active_leaf_id") else (1 if payload.get("active_leaf_id") else 0)
    blocker_cluster_age = int(metrics.get("blocker_cluster_age", 0) or 0) + 1 if same_blocker else 1
    failed_close_attempts = (
        int(metrics.get("failed_close_attempts", 0) or 0) + 1
        if review["assessed_action"] == "CLOSE" and outcome != "CLOSED" and same_leaf
        else (1 if review["assessed_action"] == "CLOSE" and outcome != "CLOSED" else 0)
    )
    low_cone_purity_streak = (
        int(metrics.get("low_cone_purity_streak", 0) or 0) + 1
        if review["cone_purity"] == "LOW"
        else 0
    )
    structural_churn = int(metrics.get("structural_churn", 0) or 0)
    if outcome == "REFUTED_REPLACED":
        structural_churn += 1
    elif outcome == "CLOSED":
        structural_churn = 0

    metrics.update(
        {
            "active_leaf_age": active_leaf_age,
            "blocker_cluster_age": blocker_cluster_age,
            "closed_nodes_count": int(metrics.get("closed_nodes_count", 0) or 0) + (1 if outcome == "CLOSED" else 0),
            "refuted_nodes_count": int(metrics.get("refuted_nodes_count", 0) or 0) + (1 if outcome == "REFUTED_REPLACED" else 0),
            "paper_nodes_closed": int(metrics.get("paper_nodes_closed", 0) or 0)
            + (
                1
                if outcome == "CLOSED"
                and active_record.get("kind") in {"paper", "paper_faithful_reformulation"}
                else 0
            ),
            "failed_close_attempts": failed_close_attempts,
            "low_cone_purity_streak": low_cone_purity_streak,
            "cone_purity": review["cone_purity"],
            "structural_churn": structural_churn,
        }
    )
    reasons: List[str] = []
    if failed_close_attempts >= THEOREM_FRONTIER_FAILED_CLOSE_THRESHOLD:
        reasons.append("same active leaf failed to close twice; expand or replace it")
    if blocker_cluster_age >= THEOREM_FRONTIER_BLOCKER_CLUSTER_THRESHOLD:
        reasons.append("same blocker cluster persisted for five reviews; mandatory escalation")
    if low_cone_purity_streak >= THEOREM_FRONTIER_LOW_CONE_PURITY_THRESHOLD:
        reasons.append("low cone purity for two consecutive reviews")
    escalation["required"] = bool(reasons)
    escalation["reasons"] = reasons

    payload["current_action"] = review["assessed_action"]
    payload["current"] = {
        "cycle": cycle,
        "reviewed_node_id": active_node_id,
        "next_active_leaf_id": payload.get("active_leaf_id"),
        "requested_action": requested_action,
        "assessed_action": review["assessed_action"],
        "outcome": outcome,
        "blocker_cluster": review["blocker_cluster"],
        "cone_purity": review["cone_purity"],
        "open_hypotheses": list(review["open_hypotheses"]),
        "justification": review["justification"],
        "updated_at": timestamp_now(),
    }
    state["theorem_frontier"] = payload
    state["last_theorem_frontier_review"] = review
    JsonFile.dump(theorem_frontier_state_path(config), payload)
    append_jsonl(
        theorem_frontier_history_path(config),
        {
            "cycle": cycle,
            "mode": "full",
            "reviewed_node_id": active_node_id,
            "next_active_leaf_id": payload.get("active_leaf_id"),
            "assessed_action": review["assessed_action"],
            "outcome": outcome,
            "blocker_cluster": review["blocker_cluster"],
            "cone_purity": review["cone_purity"],
            "open_hypotheses": list(review["open_hypotheses"]),
            "paper_verifier_decision": paper_decision,
            "metrics": dict(metrics),
            "escalation": dict(escalation),
            "current_anchor": current_node.get("lean_anchor") if isinstance(current_node, dict) else None,
        },
    )
    update_supervisor_tasks_file(config, current_phase(config, state))
    return payload


def phase_specific_worker_statuses(phase: str) -> Sequence[str]:
    if phase == "planning":
        return WORKER_STATUSES
    return ("NOT_STUCK", "STUCK", "DONE")


def phase_specific_reviewer_decisions(phase: str) -> Sequence[str]:
    if phase == "planning":
        return REVIEWER_DECISIONS
    if phase == "proof_formalization":
        return ("CONTINUE", "ADVANCE_PHASE", "STUCK")
    if is_style_cleanup_phase(phase):
        return ("CONTINUE", "STUCK", "DONE")
    return ("CONTINUE", "ADVANCE_PHASE", "STUCK")


def active_branch_episode(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    episode = state.get("active_branch_episode")
    if isinstance(episode, dict):
        return episode
    return None


def branch_episode_dir(config: Config, episode_id: str) -> Path:
    return config.state_dir / "branches" / episode_id


def branch_strategy_artifact_path(config: Config) -> Path:
    return config.state_dir / "branch_strategy.json"


def branch_selection_artifact_path(config: Config) -> Path:
    return config.state_dir / "branch_selection.json"


def branch_replacement_artifact_path(config: Config) -> Path:
    return config.state_dir / "branch_replacement.json"


def branch_strategy_keywords() -> Dict[str, Tuple[str, ...]]:
    return {
        "strong": (
            "pivot",
            "route change",
            "rewrite",
            "counterexample",
            "too weak",
            "too strong",
            "paper-faithful",
            "topological route",
            "combinatorial route",
            "major refactor",
            "mismatch",
        ),
        "general": (
            "route",
            "refactor",
            "blocked",
            "repair",
            "interface bug",
            "direct route",
            "backup route",
            "deleted-spur",
            "containment",
            "same-level continuation",
            "entrance",
        ),
    }


def branch_strategy_signal_tags(decision: Dict[str, Any]) -> List[str]:
    text = " ".join(
        str(decision.get(key, "")).strip().lower()
        for key in ("reason", "next_prompt")
    )
    tags: List[str] = []
    if str(decision.get("decision", "")).strip().upper() == "STUCK":
        tags.append("stuck")
    keywords = branch_strategy_keywords()
    for category, terms in keywords.items():
        for term in terms:
            if term in text:
                tags.append(f"{category}:{term}")
    return tags


def should_consider_branching(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    decision: Dict[str, Any],
) -> bool:
    if not branching_enabled(config) and not can_propose_branch_replacement(state, config):
        return False
    if phase != "proof_formalization":
        return False
    if active_branch_episode(state):
        return False
    if pending_branch_proposal(state):
        return False
    if branch_review_count(state) < next_branch_proposal_review_count(state):
        return False
    cycle = int(decision.get("cycle", state.get("cycle", 0)) or 0)
    if state.get("last_branch_consideration_cycle") == cycle:
        return False
    tags = branch_strategy_signal_tags(decision)
    if "stuck" in tags:
        return True
    strong_tags = [tag for tag in tags if tag.startswith("strong:")]
    if strong_tags:
        return True
    general_tags = {tag for tag in tags if tag.startswith("general:")}
    return len(general_tags) >= 2


def deep_copy_jsonish(data: Any) -> Any:
    return json.loads(json.dumps(data, ensure_ascii=False))


def sanitize_branch_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-.")
    return cleaned or "branch"


def branch_review_count(state: Dict[str, Any]) -> int:
    reviews = state.get("review_log")
    return len(reviews) if isinstance(reviews, list) else 0


def branch_progress_count(branch_state: Dict[str, Any], base_review_count: int) -> int:
    return max(0, branch_review_count(branch_state) - base_review_count)


def branch_episode_preflight_error(config: Config) -> Optional[str]:
    if not shutil.which("git"):
        return "git is not available on PATH"
    if not repo_is_git_repository(config):
        return "branching requires the repo to already be a git worktree"
    if not repo_has_git_commits(config):
        return "branching requires the repo to already have at least one commit"
    status = git_output(config, ["status", "--short"]).strip()
    if status:
        return "branching requires a clean git worktree"
    return None


def format_json_enum(values: Sequence[str]) -> str:
    return " | ".join(json.dumps(value) for value in values)


def stuck_recovery_attempts(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    attempts = state.get("stuck_recovery_attempts")
    if isinstance(attempts, list):
        return attempts
    state["stuck_recovery_attempts"] = []
    return state["stuck_recovery_attempts"]


def clear_stuck_recovery(state: Dict[str, Any]) -> None:
    state["stuck_recovery_attempts"] = []
    state["stuck_recovery_last_trigger_cycle"] = None


def latest_stuck_recovery_attempt(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attempts = stuck_recovery_attempts(state)
    return attempts[-1] if attempts else None


def current_stuck_recovery_attempt_number(state: Dict[str, Any]) -> int:
    return len(stuck_recovery_attempts(state)) + 1


def is_branch_run(state: Dict[str, Any]) -> bool:
    if branch_lineage_entries(state):
        return True
    context = state.get("branch_context")
    return isinstance(context, dict) and bool(context)


def stuck_recovery_attempt_limit(state: Dict[str, Any], policy: Optional[Policy] = None) -> int:
    if policy is not None:
        return (
            policy.stuck_recovery.branch_max_attempts
            if is_branch_run(state)
            else policy.stuck_recovery.mainline_max_attempts
        )
    policy_meta = state.get("policy")
    effective = policy_meta.get("effective") if isinstance(policy_meta, dict) else {}
    if isinstance(effective, dict):
        stuck_block = effective.get("stuck_recovery")
        if isinstance(stuck_block, dict):
            key = "branch_max_attempts" if is_branch_run(state) else "mainline_max_attempts"
            try:
                return max(1, int(stuck_block.get(key)))
            except (TypeError, ValueError):
                pass
    return MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS if is_branch_run(state) else MAX_STUCK_RECOVERY_ATTEMPTS


def branch_strategy_limit(config: Config, state: Dict[str, Any]) -> int:
    if branching_enabled(config):
        return config.branching.max_current_branches
    return parent_branch_capacity(state, config)


def pending_branch_proposal(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    proposal = state.get("pending_branch_proposal")
    return proposal if isinstance(proposal, dict) else None


def clear_pending_branch_proposal(state: Dict[str, Any]) -> None:
    state["pending_branch_proposal"] = None


def store_pending_branch_proposal(
    state: Dict[str, Any],
    proposal: Dict[str, Any],
    *,
    cycle: int,
) -> Dict[str, Any]:
    stored = deep_copy_jsonish(proposal)
    stored["proposal_cycle"] = cycle
    stored["proposal_review_count"] = branch_review_count(state)
    stored["proposal_timestamp"] = timestamp_now()
    state["pending_branch_proposal"] = stored
    return stored


def next_branch_proposal_review_count(state: Dict[str, Any]) -> int:
    value = state.get("next_branch_proposal_review_count", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def last_review_cycle(state: Dict[str, Any]) -> int:
    last_review = state.get("last_review")
    if not isinstance(last_review, dict):
        return 0
    value = last_review.get("cycle", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def last_validation_cycle(state: Dict[str, Any]) -> int:
    last_validation = state.get("last_validation") or {}
    value = last_validation.get("cycle", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def determine_resume_cycle_and_stage(state: Dict[str, Any]) -> Tuple[int, str]:
    current_cycle = int(state.get("cycle", 0) or 0)
    if current_cycle <= 0:
        return 1, "worker"

    if last_review_cycle(state) >= current_cycle:
        return current_cycle + 1, "worker"

    last_validation = state.get("last_validation")
    if (
        isinstance(last_validation, dict)
        and last_validation_cycle(state) == current_cycle
        and isinstance(state.get("last_worker_handoff"), dict)
        and "last_worker_output" in state
    ):
        return current_cycle, "reviewer"

    return current_cycle, "worker"


def has_unhandled_stuck_review(state: Dict[str, Any]) -> bool:
    last_review = state.get("last_review") or {}
    review_phase_raw = str(last_review.get("phase", "")).strip()
    review_phase = normalize_phase_name(review_phase_raw) if review_phase_raw else "proof_formalization"
    if review_phase != "proof_formalization":
        return False
    if str(last_review.get("decision", "")).strip().upper() != "STUCK":
        return False
    trigger_cycle = last_review_cycle(state)
    return state.get("stuck_recovery_last_trigger_cycle") != trigger_cycle


def can_attempt_stuck_recovery(state: Dict[str, Any], policy: Optional[Policy] = None) -> bool:
    return has_unhandled_stuck_review(state) and len(stuck_recovery_attempts(state)) < stuck_recovery_attempt_limit(
        state,
        policy=policy,
    )


def stuck_recovery_exhausted(state: Dict[str, Any], policy: Optional[Policy] = None) -> bool:
    return has_unhandled_stuck_review(state) and len(stuck_recovery_attempts(state)) >= stuck_recovery_attempt_limit(
        state,
        policy=policy,
    )


def record_stuck_recovery_attempt(
    state: Dict[str, Any],
    *,
    trigger_cycle: int,
    phase: str,
    suggestion: Dict[str, Any],
) -> Dict[str, Any]:
    attempts = stuck_recovery_attempts(state)
    entry = dict(suggestion)
    entry["phase"] = phase
    entry["attempt"] = len(attempts) + 1
    entry["trigger_cycle"] = trigger_cycle
    attempts.append(entry)
    state["stuck_recovery_last_trigger_cycle"] = trigger_cycle
    return entry


def approved_axioms(config: Config) -> List[str]:
    raw = JsonFile.load(config.workflow.approved_axioms_path, {"approved_axioms": []})
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, dict):
        items = raw.get("approved_axioms", [])
        if isinstance(items, list):
            return [str(item).strip() for item in items if str(item).strip()]
    raise SupervisorError(f"Could not parse approved axioms file: {config.workflow.approved_axioms_path}")


def git_push_command(config: Config) -> str:
    return f"git push {shlex.quote(config.git.remote_name)} HEAD:{shlex.quote(current_git_branch(config))}"


def current_git_head(config: Config) -> Optional[str]:
    if not repo_has_git_commits(config):
        return None
    head = git_output(config, ["rev-parse", "HEAD"]).strip()
    return head or None


def cleanup_last_good_commit(state: Dict[str, Any]) -> Optional[str]:
    value = state.get("cleanup_last_good_commit")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def update_cleanup_last_good_commit(
    config: Config,
    state: Dict[str, Any],
    validation_summary: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    head: Optional[str] = None
    if isinstance(validation_summary, dict):
        git_summary = validation_summary.get("git")
        if isinstance(git_summary, dict):
            raw_head = git_summary.get("head")
            if isinstance(raw_head, str) and raw_head.strip():
                head = raw_head.strip()
    if head is None:
        head = current_git_head(config)
    if head:
        state["cleanup_last_good_commit"] = head
    return head


def restore_cleanup_last_good_commit(
    config: Config,
    state: Dict[str, Any],
    *,
    cycle: int,
    reason: str,
) -> Dict[str, Any]:
    commit = cleanup_last_good_commit(state)
    if not commit:
        raise SupervisorError("Cleanup rollback requested but no last good commit is recorded.")
    ensure_git_command_ok(config, ["reset", "--hard", commit])
    if git_is_enabled(config):
        current_branch = current_git_branch(config)
        ensure_git_command_ok(
            config,
            ["push", "--force-with-lease", config.git.remote_name, f"HEAD:{current_branch}"],
        )
    restored_validation = run_validation(config, PHASE_PROOF_COMPLETE_STYLE_CLEANUP, cycle)
    state["last_validation"] = restored_validation
    update_cleanup_last_good_commit(config, state, restored_validation)
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
        kind="cleanup_revert",
        actor="supervisor",
        target="workflow",
        content={"reason": reason, "restored_commit": commit},
        content_type="json",
        summary=f"Reverted cleanup worktree to last good commit {commit[:12]}",
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
        kind="validation_summary",
        actor="supervisor",
        target="workflow",
        content=restored_validation,
        content_type="json",
    )
    save_state(config, state)
    return restored_validation


def git_validation_summary(config: Config) -> Dict[str, Any]:
    if not git_is_enabled(config):
        return {"enabled": False}

    repo_ok = repo_is_git_repository(config)
    summary: Dict[str, Any] = {
        "enabled": True,
        "repo_ok": repo_ok,
        "remote_name": config.git.remote_name,
        "remote_url": config.git.remote_url,
        "branch": current_git_branch(config),
        "push_command": git_push_command(config),
    }
    if not repo_ok:
        summary.update(
            {
                "remote_matches_config": False,
                "has_commits": False,
                "head": None,
                "worktree_clean": False,
                "status": "Repository is not initialized as a git repo.",
                "upstream": None,
                "author_name": None,
                "author_email": None,
            }
        )
        return summary

    remote_url = git_output(config, ["remote", "get-url", config.git.remote_name]).strip()
    head_proc = git_run(config, ["rev-parse", "HEAD"])
    head = head_proc.stdout.strip() if head_proc.returncode == 0 else None
    status = git_output(config, ["status", "--short"]).strip()
    upstream_proc = git_run(config, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    upstream = upstream_proc.stdout.strip() if upstream_proc.returncode == 0 else None
    summary.update(
        {
            "remote_matches_config": remote_url == (config.git.remote_url or ""),
            "has_commits": repo_has_git_commits(config),
            "head": head,
            "worktree_clean": not bool(status),
            "status": trim_text(status or "clean", 3000),
            "upstream": upstream,
            "author_name": git_output(config, ["config", "--get", "user.name"]).strip() or None,
            "author_email": git_output(config, ["config", "--get", "user.email"]).strip() or None,
        }
    )
    return summary


def validation_git_head(validation_summary: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(validation_summary, dict):
        return None
    git_summary = validation_summary.get("git")
    if not isinstance(git_summary, dict):
        return None
    head = git_summary.get("head")
    if isinstance(head, str) and head.strip():
        return head.strip()
    return None


def git_name_only(config: Config, args: Sequence[str]) -> List[str]:
    proc = git_run(config, list(args))
    if proc.returncode != 0:
        return []
    return [item.strip() for item in proc.stdout.splitlines() if item.strip()]


def changed_lean_files_since_validation(
    config: Config,
    previous_validation: Optional[Dict[str, Any]],
) -> List[str]:
    if not repo_is_git_repository(config):
        return []
    previous_head = validation_git_head(previous_validation)
    current_head = current_git_head(config)
    changed: List[str] = []
    if previous_head and current_head and previous_head != current_head:
        changed.extend(git_name_only(config, ["diff", "--name-only", previous_head, current_head, "--", "*.lean"]))
    if current_head:
        changed.extend(git_name_only(config, ["diff", "--name-only", current_head, "--", "*.lean"]))
        changed.extend(git_name_only(config, ["diff", "--cached", "--name-only", current_head, "--", "*.lean"]))
    else:
        changed.extend(git_name_only(config, ["status", "--short"]))
    normalized: List[str] = []
    for item in changed:
        if item.startswith("?? "):
            item = item[3:].strip()
        elif len(item) > 3 and item[2] == " ":
            item = item[3:].strip()
        if not item.endswith(".lean"):
            continue
        normalized.append(
            normalize_repo_relative_path(
                item,
                label="changed Lean file",
                required_suffix=".lean",
            )
        )
    return sorted(dict.fromkeys(normalized))


def apply_theorem_frontier_cone_file_guard(
    config: Config,
    phase: str,
    validation_summary: Dict[str, Any],
    worker_update: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "enforced": False,
        "allowed_edit_paths": [],
        "changed_lean_files": [],
        "disallowed_changed_lean_files": [],
    }
    if not theorem_frontier_full_enabled(config, phase) or not isinstance(worker_update, dict):
        return result
    git_summary = validation_summary.get("git")
    if not isinstance(git_summary, dict):
        return result
    changed = [
        normalize_repo_relative_path(path, label="git.changed_lean_files entry", required_suffix=".lean")
        for path in (git_summary.get("changed_lean_files") or [])
        if str(path).strip()
    ] if isinstance(git_summary.get("changed_lean_files"), list) else []
    allowed = normalize_repo_relative_path_list(
        worker_update.get("allowed_edit_paths"),
        label="theorem frontier worker update allowed_edit_paths",
        required_suffix=".lean",
        allow_empty=False,
    )
    disallowed = [path for path in changed if path not in set(allowed)]
    result = {
        "enforced": True,
        "allowed_edit_paths": allowed,
        "changed_lean_files": changed,
        "disallowed_changed_lean_files": disallowed,
    }
    if disallowed:
        raise SupervisorError(
            "Theorem-frontier cone file guard failed: changed Lean files outside allowed_edit_paths: "
            f"{disallowed}"
        )
    return result


def validation_summary_path(config: Config) -> Path:
    return config.state_dir / "validation_summary.json"


def stuck_recovery_suggestion_path(config: Config) -> Path:
    return config.state_dir / "stuck_recovery_suggestion.json"


def stuck_recovery_context_text(state: Dict[str, Any]) -> str:
    latest = latest_stuck_recovery_attempt(state)
    if not latest:
        return ""
    attempt_limit = stuck_recovery_attempt_limit(state)
    return textwrap.dedent(
        f"""\
        Active stuck-recovery guidance:
        - Attempt {latest.get('attempt', '?')} of {attempt_limit} for the current stuck episode.
        - Trigger cycle: {latest.get('trigger_cycle', '?')}
        - Diagnosis: {str(latest.get('diagnosis', '')).strip()}
        - Creative suggestion: {str(latest.get('creative_suggestion', '')).strip()}
        - Why it might work: {str(latest.get('why_this_might_work', '')).strip()}
        - Worker focus prompt: {str(latest.get('worker_prompt', '')).strip()}
        """
    ).strip()


def branch_context_text(state: Dict[str, Any]) -> str:
    context = state.get("branch_context")
    if not isinstance(context, dict):
        return ""
    return textwrap.dedent(
        f"""\
        Active branch strategy:
        - Episode: {context.get('episode_id', '')}
        - Branch: {context.get('branch_name', '')}
        - Summary: {str(context.get('summary', '')).strip()}
        - Frontier anchor node: {str(context.get('frontier_anchor_node_id', '')).strip()}
        - Rewrite scope: {str(context.get('rewrite_scope', '')).strip()}
        - Branch worker prompt: {str(context.get('worker_prompt', '')).strip()}
        - Why this might eventually succeed: {str(context.get('why_this_might_eventually_succeed', '')).strip()}
        """
    ).strip()


def phase_context_text(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    goal_label = repo_prompt_label(config, provider, config.goal_file)
    tasks_label = repo_prompt_label(config, provider, config.repo_path / "TASKS.md")
    parts = [
        f"Current phase: {phase}",
        f"Sorry mode: {config.workflow.sorry_mode}",
        f"Goal file: {goal_label}",
        "Supervisor-managed files:",
        f"- `{tasks_label}` always exists and is shared with the supervisor.",
    ]
    if config.workflow.paper_tex_path is not None:
        parts.append(f"- Paper tex: `{repo_prompt_label(config, provider, config.workflow.paper_tex_path)}`")
    if phase_uses_paper_notes(phase):
        parts.append(
            f"- `{repo_prompt_label(config, provider, config.repo_path / 'PAPERNOTES.md')}` is where paper corrections and clarifications belong."
        )
    if phase_uses_plan(phase):
        parts.append(f"- `{repo_prompt_label(config, provider, config.repo_path / 'PLAN.md')}` is the durable formalization roadmap.")
    if phase_uses_statement_files(phase):
        definitions_label = repo_prompt_label(config, provider, config.repo_path / "PaperDefinitions.lean")
        theorems_label = repo_prompt_label(config, provider, config.repo_path / "PaperTheorems.lean")
        parts.append(f"- `{definitions_label}` and `{theorems_label}` are the target statement files.")
    parts.append(f"- Approved axioms file: `{repo_prompt_label(config, provider, config.workflow.approved_axioms_path)}`")
    if git_is_enabled(config):
        parts.append(
            f"- Git remote: `{config.git.remote_name}` -> `{config.git.remote_url}` on branch `{current_git_branch(config)}`."
        )
        parts.append(f"- Push command when you made progress: `{git_push_command(config)}`")
    parts.append(f"- Validation summary file: `{supervisor_prompt_label(config, provider, validation_summary_path(config))}`")
    latest_validation = state.get("last_validation")
    if latest_validation:
        parts.append("Latest supervisor validation summary:")
        parts.append(trim_text(json.dumps(latest_validation, indent=2, ensure_ascii=False), 12000))
    else:
        parts.append("Latest supervisor validation summary: none yet.")
    human_input_text = trim_text(read_text(config.workflow.human_input_path).strip(), 6000)
    if human_input_text:
        parts.append(f"Latest human input from `{repo_prompt_label(config, provider, config.workflow.human_input_path)}`:")
        parts.append(human_input_text)
    stuck_recovery_text = stuck_recovery_context_text(state)
    if stuck_recovery_text:
        parts.append(stuck_recovery_text)
    branch_text = branch_context_text(state)
    if branch_text:
        parts.append(branch_text)
    frontier_text = theorem_frontier_context_text(config, state, provider)
    if frontier_text:
        parts.append(frontier_text)
    approved = approved_axioms(config)
    parts.append(f"Approved axioms: {approved if approved else '[]'}")
    return "\n".join(parts)


def phase_worker_instructions(config: Config, phase: str, provider: str) -> str:
    paper_label = (
        repo_prompt_label(config, provider, config.workflow.paper_tex_path)
        if config.workflow.paper_tex_path
        else "the paper tex file"
    )
    tasks_label = repo_prompt_label(config, provider, config.repo_path / "TASKS.md")
    papernotes_label = repo_prompt_label(config, provider, config.repo_path / "PAPERNOTES.md")
    plan_label = repo_prompt_label(config, provider, config.repo_path / "PLAN.md")
    definitions_label = repo_prompt_label(config, provider, config.repo_path / "PaperDefinitions.lean")
    theorems_label = repo_prompt_label(config, provider, config.repo_path / "PaperTheorems.lean")
    if phase == "paper_check":
        return textwrap.dedent(
            f"""\
            Phase objective: carefully read `{paper_label}` and mathematically verify the paper's proofs.

            Requirements:
            - Maintain `{tasks_label}`.
            - Maintain `{papernotes_label}` with corrections, hidden assumptions, and proof clarifications.
            - Read the paper carefully enough to catch proof gaps or incorrect statements.
            - Report `STUCK` only if you find a genuine gap or incorrect statement, try to repair it seriously, and still cannot make the argument work.
            - Report `DONE` only when the whole paper has been checked and `{papernotes_label}` is up to date.
            """
        ).strip()
    if phase == "planning":
        return textwrap.dedent(
            f"""\
            Phase objective: create a high-level but comprehensive `{plan_label}` for formalizing the main results of `{paper_label}`.

            Requirements:
            - Maintain `{tasks_label}`.
            - Maintain `{papernotes_label}`.
            - Build `{plan_label}` around statement prerequisites, reusable definitions, mathlib imports, and plausible proof roadmaps.
            - Use `NEED_INPUT` for external results, proposed axioms, or formalization design choices that genuinely need a human decision.
            - Never introduce axioms unless they are explicitly approved by a human and listed in the approved axioms file.
            """
        ).strip()
    if phase == "theorem_stating":
        main_results_label = supervisor_prompt_label(config, provider, paper_main_results_manifest_path(config))
        return textwrap.dedent(
            f"""\
            Phase objective: create Lean files that state the paper's definitions and theorems as close to `{paper_label}` as possible.

            Requirements:
            - Maintain `{tasks_label}`, `{papernotes_label}`, and `{plan_label}`.
            - Create or update `{definitions_label}` and `{theorems_label}`.
            - Write a machine-readable main-results manifest to `{main_results_label}` for the theorem-frontier DAG seeding step.
            - Keep the definitions and statements easy for a human to compare against the paper.
            - Make both files syntactically valid Lean.
            - Do not introduce unapproved axioms.
            - The manifest must list every main paper result that should appear as an initial theorem-frontier node in proof formalization, using exact natural-language statements, exact Lean statements, exact anchors, and any dependency edges among those main results.
            - The manifest must also choose `initial_active_node_id`, the paper-facing result that proof formalization should start on first.
            - The manifest must be a JSON object with exactly these top-level keys:
              `phase`, `main_results`, `dependency_edges`, `initial_active_node_id`.
            - Every entry in `main_results` must be an exact theorem-frontier node object for a paper-facing result, with kind `paper` or `paper_faithful_reformulation`.
            - `DONE` means the statement files are in place and ready for reviewer comparison against the paper.
            """
        ).strip()
    if is_style_cleanup_phase(phase):
        return textwrap.dedent(
            f"""\
            Phase objective: PROOF COMPLETE - style cleanup.

            Requirements:
            - Treat the proofs as complete already; every burst must end with a fully buildable proof state.
            - Maintain `{tasks_label}` and `{plan_label}`.
            - Focus on warning cleanup, proof/style cleanup, and moderate refactors that improve reuse or readability.
            - Keep `{definitions_label}` and `{theorems_label}` paper-facing and stable.
            - Do not take speculative risks. If a cleanup attempt stops being clearly worthwhile, report `STUCK`.
            - `DONE` means there is no clearly worthwhile remaining cleanup and the polished proof state should be kept as the final result.
            """
        ).strip()
    sorry_policy = (
        f"Default sorry policy: do not move on with extra sorrys anywhere outside `{theorems_label}`."
        if config.workflow.sorry_mode == "default"
        else "Sorrys-allowed mode: temporary extra sorrys are allowed, but you must drive the count down and remove them all by the end."
    )
    return textwrap.dedent(
        f"""\
        Phase objective: prove the target statements presented in `{theorems_label}`.

        Requirements:
        - Maintain `{tasks_label}` and `{plan_label}`.
        - Keep `{definitions_label}` and `{theorems_label}` as the paper-facing interface for definitions and theorem statements.
        - Prefer reusable lemmas, technical definitions, and proof infrastructure in separate support files when that yields a cleaner project structure.
        - It is fine for proofs in `{theorems_label}` to be short wrappers around results proved elsewhere in the repo.
        - Work toward zero sorrys and no unapproved axioms.
        - Keep the proof frontier concrete in `{tasks_label}`.
        """
    ).strip() + "\n- " + sorry_policy + "\n- `DONE` means the full workflow is complete."


def git_worker_instructions(config: Config) -> str:
    if not git_is_enabled(config):
        return ""
    return textwrap.dedent(
        f"""\
        Git requirements:
        - This repo is configured with remote `{config.git.remote_name}` -> `{config.git.remote_url}`.
        - If you made meaningful progress in this burst, create a non-empty git commit before ending the burst.
        - Push that commit before ending the burst. Use `{git_push_command(config)}` if no upstream is configured.
        - Leave the worktree clean after a productive burst.
        - If you made no meaningful changes, do not create an empty commit.
        """
    ).strip()


def provider_context_worker_instructions(config: Config) -> str:
    provider = config.worker.provider
    if provider == "claude":
        context_path = ".claude/skills/lean-formalizer/SKILL.md"
        context_label = "the installed `lean-formalizer` skill"
    elif provider == "codex":
        context_path = ".agents/skills/lean-formalizer/SKILL.md"
        context_label = "the installed `lean-formalizer` skill"
    elif provider == "gemini":
        context_path = supervisor_prompt_label(
            config,
            provider,
            role_scope_dir(config, "gemini", "worker") / "GEMINI.md",
        )
        context_label = "the installed Lean formalization context file"
    else:
        context_path = "the installed provider context file"
        context_label = "the installed provider context file"
    return textwrap.dedent(
        f"""\
        Provider-context requirements:
        - Before substantive work in this burst, read or reread {context_label} at `{context_path}` if it is present in this scope.
        - Follow the Lean-search, naming, proof-planning, and tool-usage suggestions in that file during this burst.
        """
    ).strip()


def git_reviewer_instructions(config: Config) -> str:
    if not git_is_enabled(config):
        return ""
    return textwrap.dedent(
        """\
        Also check the git summary in the validation output.
        If the worker made progress but left the repo dirty or failed to push the resulting commit, require another burst.
        """
    ).strip()


def prompt_notes_block(title: str, note: str) -> str:
    cleaned = str(note).strip()
    if not cleaned:
        return ""
    return textwrap.dedent(
        f"""\
        {title}:
        {cleaned}
        """
    ).strip()


def theorem_frontier_worker_instructions(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    if not theorem_frontier_enabled(config, phase):
        return ""
    if theorem_frontier_full_enabled(config, phase):
        artifact_label = supervisor_prompt_label(config, provider, theorem_frontier_worker_update_path(config))
        return textwrap.dedent(
            f"""\
            Theorem-frontier artifact requirements:
            - In addition to the normal worker handoff, write a theorem-frontier JSON to `{artifact_label}`.
            - That JSON must name exactly one active theorem node and one requested action: `CLOSE`, `EXPAND`, or `REFUTE_REPLACE`.
            - The active node must have an exact natural-language statement, exact Lean statement, exact anchor, proof provenance, closure mode, blocker cluster, and acceptance evidence.
            - Treat the active node as the authoritative proof target for this burst.
            - Work only inside the active cone: the active node, its current children, or mechanically necessary support directly tied to closing that node.
            - If you propose a structural change, include exact proposed nodes and edges. Do not leave them vague.

            Your theorem-frontier JSON must have exactly these top-level keys:
            {{
              "phase": "{phase}",
              "active_node": {{
                "node_id": "stable theorem node id",
                "kind": "paper|paper_faithful_reformulation|support|packaging|exploratory",
                "natural_language_statement": "exact mathematical statement",
                "lean_statement": "exact Lean statement",
                "lean_anchor": "Lean declaration name or file anchor",
                "paper_provenance": "paper source, correction note, or approved reformulation",
                "closure_mode": "leaf|all_children|all_cases|any_child",
                "blocker_cluster": "canonical underlying blocker",
                "acceptance_evidence": "what will count as closure",
                "notes": "short local rationale"
              }},
              "requested_action": "CLOSE" | "EXPAND" | "REFUTE_REPLACE",
              "cone_scope": "what work is inside the active cone for this burst",
              "allowed_edit_paths": ["repo-relative .lean files allowed inside that cone for this burst"],
              "result_summary": "what changed relative to the active node",
              "proposed_nodes": [{{ exact node objects, if any }}],
              "proposed_edges": [{{ "parent": "...", "child": "...", "edge_type": "...", "justification": "..." }}],
              "next_candidate_ids": ["node ids that could become the next active leaf"],
              "structural_change_reason": "why a structural edit is needed, or empty if none"
            }}
            Any Lean file changed outside `allowed_edit_paths` will fail theorem-frontier cone validation for the cycle.
            """
        ).strip()
    artifact_label = supervisor_prompt_label(config, provider, theorem_frontier_worker_update_path(config))
    return textwrap.dedent(
        f"""\
        Theorem-frontier artifact requirements (Phase 0):
        - In addition to the normal worker handoff, write a theorem-frontier JSON to `{artifact_label}`.
        - That JSON must describe exactly one active theorem node for this burst.
        - The active theorem must have an exact natural-language statement, exact Lean statement, and exact anchor.
        - Valid requested actions are `CLOSE`, `EXPAND`, and `REFUTE_REPLACE`.
        - Keep all substantive mathematical work inside the active theorem's local cone or mechanically necessary support for it.

        Your theorem-frontier JSON must have exactly these keys:
        {{
          "phase": "{phase}",
          "active_theorem_id": "stable theorem id for this burst",
          "active_theorem_nl_statement": "exact natural-language statement",
          "active_theorem_lean_statement": "exact Lean statement",
          "active_theorem_anchor": "Lean declaration name or file anchor",
          "requested_action": "CLOSE" | "EXPAND" | "REFUTE_REPLACE",
          "blocker_cluster": "canonical short description of the underlying blocker",
          "cone_scope": "what local descendants/support lemmas are allowed in this burst",
          "result_summary": "what changed relative to this active theorem"
        }}
        """
    ).strip()


def theorem_frontier_reviewer_instructions(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    if not theorem_frontier_enabled(config, phase):
        return ""
    if theorem_frontier_full_enabled(config, phase):
        artifact_label = supervisor_prompt_label(config, provider, theorem_frontier_review_path(config))
        return textwrap.dedent(
            f"""\
            Theorem-frontier review requirements:
            - In addition to the normal reviewer decision, write a theorem-frontier review JSON to `{artifact_label}`.
            - Judge the cycle by theorem-frontier standards, not by build cleanliness alone.
            - Confirm whether the requested action really happened on one active theorem node.
            - If the worker mostly added wrappers above the same blocker or drifted outside the active cone, use `NO_FRONTIER_PROGRESS`.
            - If cone purity is low, record that explicitly.
            - Use `next_active_theorem_id` to name the leaf that should be active after this review. Use the current active theorem id if the same node stays active, or leave it empty only if there is no next active leaf yet.

            Your theorem-frontier review JSON must have exactly these keys:
            {{
              "phase": "{phase}",
              "active_theorem_id": "reviewed node id",
              "active_theorem_nl_statement": "exact mathematical statement",
              "active_theorem_lean_statement": "exact Lean statement",
              "active_theorem_anchor": "Lean declaration name or file anchor",
              "assessed_action": "CLOSE" | "EXPAND" | "REFUTE_REPLACE",
              "blocker_cluster": "canonical blocker after review",
              "outcome": "CLOSED" | "EXPANDED" | "REFUTED_REPLACED" | "STILL_OPEN" | "NO_FRONTIER_PROGRESS",
              "next_active_theorem_id": "next active leaf id or current id",
              "cone_purity": "HIGH" | "MEDIUM" | "LOW",
              "open_hypotheses": ["remaining assumptions still blocking closure"],
              "justification": "brief theorem-frontier justification"
            }}
            """
        ).strip()
    artifact_label = supervisor_prompt_label(config, provider, theorem_frontier_review_path(config))
    return textwrap.dedent(
        f"""\
        Theorem-frontier review requirements (Phase 0):
        - In addition to the normal reviewer decision, write a theorem-frontier review JSON to `{artifact_label}`.
        - Judge the cycle by theorem-frontier standards, not by build cleanliness alone.
        - Confirm exactly one active theorem node and classify what theorem action actually happened.
        - Valid actions are `CLOSE`, `EXPAND`, and `REFUTE_REPLACE`.
        - Valid outcomes are `CLOSED`, `EXPANDED`, `REFUTED_REPLACED`, `STILL_OPEN`, and `NO_FRONTIER_PROGRESS`.
        - If the burst mostly built wrappers without shrinking the active blocker, use `NO_FRONTIER_PROGRESS`.

        Your theorem-frontier review JSON must have exactly these keys:
        {{
          "phase": "{phase}",
          "active_theorem_id": "approved active theorem id for this cycle",
          "active_theorem_nl_statement": "exact natural-language statement",
          "active_theorem_lean_statement": "exact Lean statement",
          "active_theorem_anchor": "Lean declaration name or file anchor",
          "assessed_action": "CLOSE" | "EXPAND" | "REFUTE_REPLACE",
          "blocker_cluster": "canonical blocker cluster after reviewing the burst",
          "outcome": "CLOSED" | "EXPANDED" | "REFUTED_REPLACED" | "STILL_OPEN" | "NO_FRONTIER_PROGRESS",
          "justification": "brief theorem-frontier justification"
        }}
        """
    ).strip()


def theorem_frontier_paper_verifier_instructions(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    if not theorem_frontier_full_enabled(config, phase):
        return ""
    artifact_label = supervisor_prompt_label(config, provider, theorem_frontier_paper_verifier_path(config))
    return textwrap.dedent(
        f"""\
        Paper-verifier structural-review requirements:
        - You are acting as the dedicated paper-verifier for theorem-frontier structural edits.
        - Review the proposed node/edge change only against the paper, `PAPERNOTES.md`, and already approved reformulations.
        - Trigger approval only for structural changes that are paper-exact, paper-faithful reformulations, conservative strengthenings, or explicit exploratory detours.
        - Reject any structural edit that is paper-incompatible, hides a necessary split, or silently changes the proof spine.

        Your paper-verifier JSON must have exactly these keys:
        {{
          "phase": "{phase}",
          "parent_node_id": "node whose subtree is changing",
          "change_kind": "CREATE_ACTIVE" | "EXPAND" | "REFUTE_REPLACE",
          "decision": "APPROVE" | "APPROVE_WITH_CAVEAT" | "REJECT",
          "classification": "paper_exact" | "paper_faithful_reformulation" | "conservative_strengthening" | "exploratory_detour" | "paper_incompatible",
          "approved_node_ids": ["node ids approved by this review"],
          "approved_edges": [{{ "parent": "...", "child": "..." }}],
          "justification": "paper-faithfulness justification",
          "caveat": "leave empty unless APPROVE_WITH_CAVEAT"
        }}
        """
    ).strip()


def build_worker_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    is_initial: bool,
    *,
    policy: Optional[Policy] = None,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    last_review = state.get("last_review") or {}
    handoff_statuses = format_json_enum(phase_specific_worker_statuses(phase))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    review_guidance = ""
    if not is_initial:
        review_guidance = textwrap.dedent(
            f"""\
            Reviewer guidance:
            - Reason: {(last_review.get("reason") or "No reason supplied.").strip()}
            - Next prompt: {(last_review.get("next_prompt") or "Continue from the current frontier.").strip()}
            """
        )
    stuck_recovery_notes = ""
    latest_recovery = latest_stuck_recovery_attempt(state)
    if latest_recovery:
        attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
        stuck_recovery_notes = textwrap.dedent(
            f"""\
            Supervisor stuck-recovery guidance:
            - This burst is recovery attempt {latest_recovery.get('attempt', '?')} of {attempt_limit} for the current stuck episode.
            - Focus prompt: {str(latest_recovery.get('worker_prompt', '')).strip()}
            - Creative suggestion: {str(latest_recovery.get('creative_suggestion', '')).strip()}
            """
        )
    provider_notes = provider_context_worker_instructions(config)
    git_notes = git_worker_instructions(config)
    frontier_notes = theorem_frontier_worker_instructions(config, state, phase, config.worker.provider)
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.worker,
    )
    worker_handoff_label = supervisor_prompt_label(config, config.worker.provider, config.state_dir / "worker_handoff.json")
    return textwrap.dedent(
        f"""\
        You are the main formalization worker.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.worker.provider)}

        {review_guidance}{stuck_recovery_notes}{provider_notes}
        {phase_worker_instructions(config, phase, config.worker.provider)}
        {frontier_notes}
        {git_notes}
        {policy_notes}

        Before ending this turn:
        - write your handoff JSON to `{worker_handoff_label}`
        - also print the same JSON as the final thing in your terminal output

        Your handoff JSON must have exactly these keys:
        {{
          "phase": "{phase}",
          "status": {handoff_statuses},
          "summary_of_changes": "brief summary",
          "current_frontier": "what you are working on now",
          "likely_next_step": "best immediate next step",
          "input_request": "leave empty unless status is NEED_INPUT"
        }}
        """
    ).strip()


def phase_reviewer_instructions(config: Config, phase: str) -> str:
    if phase == "paper_check":
        text = textwrap.dedent(
            """\
            Decide whether the worker should continue checking the paper, advance to planning, or stop as stuck.
            Use `ADVANCE_PHASE` only when the paper has been checked end-to-end and `PAPERNOTES.md` is in good shape.
            """
        ).strip()
        git_note = git_reviewer_instructions(config)
        return text + ("\n" + git_note if git_note else "")
    if phase == "planning":
        text = textwrap.dedent(
            """\
            Decide whether the worker should continue planning, advance to theorem stating, request human input, or stop.
            Use `NEED_INPUT` for genuine design or external-result questions.
            Use `ADVANCE_PHASE` only when `PLAN.md` is comprehensive enough to guide formalization.
            """
        ).strip()
        git_note = git_reviewer_instructions(config)
        return text + ("\n" + git_note if git_note else "")
    if phase == "theorem_stating":
        main_results_label = supervisor_prompt_label(config, config.reviewer.provider, paper_main_results_manifest_path(config))
        text = textwrap.dedent(
            f"""\
            Decide whether the worker should continue theorem stating, advance to proof formalization, or stop.
            Compare `PaperDefinitions.lean` and `PaperTheorems.lean` against the paper and insist on changes if they do not correspond.
            Compare the main-results manifest `{main_results_label}` against the paper and the statement files.
            Require that it includes all main paper results, excludes support-only statements, and chooses a reasonable initial active paper node.
            Require syntactically valid Lean before advancing.
            """
        ).strip()
        git_note = git_reviewer_instructions(config)
        return text + ("\n" + git_note if git_note else "")
    if is_style_cleanup_phase(phase):
        text = textwrap.dedent(
            """\
            Decide whether cleanup should continue, stop as done, or stop because cleanup has stalled.
            This phase is optional polish, not mission-critical proof development.
            Require that every cycle remain fully buildable with no sorrys and no unapproved axioms.
            Prefer `DONE` once the remaining cleanup is marginal.
            Use `STUCK` when cleanup no longer seems worth the risk or effort; the supervisor will preserve the last good proof-complete commit and finish successfully.
            """
        ).strip()
        git_note = git_reviewer_instructions(config)
        return text + ("\n" + git_note if git_note else "")
    text = textwrap.dedent(
        """\
        Decide whether the worker should continue the proof phase, advance to proof-complete style cleanup, or stop as stuck.
        Use the supervisor validation summary for build status, sorry counts, and axiom enforcement.
        Keep `PaperDefinitions.lean` and `PaperTheorems.lean` paper-facing and easy to compare against the paper.
        If the worker is stuffing reusable infrastructure into those files when separate support files would be cleaner, require refactoring.
        """
    ).strip()
    git_note = git_reviewer_instructions(config)
    return text + ("\n" + git_note if git_note else "")


def build_theorem_frontier_paper_verifier_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_terminal_output: str,
    worker_handoff_text: str,
    worker_frontier_update: Dict[str, Any],
    is_initial: bool,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    recent_reviews = state.get("review_log", [])[-3:]
    paper_notes = trim_text(read_text(config.repo_path / "PAPERNOTES.md").strip(), 16000) or "(none)"
    frontier_payload = theorem_frontier_payload(state) or default_theorem_frontier_payload("full")
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    artifact_label = supervisor_prompt_label(config, config.reviewer.provider, theorem_frontier_paper_verifier_path(config))
    return textwrap.dedent(
        f"""\
        You are the paper-verifier for theorem-frontier structural edits.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Worker theorem-frontier JSON:
        {json.dumps(worker_frontier_update, indent=2, ensure_ascii=False)}

        Current authoritative theorem-frontier payload:
        {trim_text(json.dumps(frontier_payload, indent=2, ensure_ascii=False), 18000)}

        Worker handoff JSON:
        {worker_handoff_text}

        Recent reviewer decisions:
        {json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]"}

        Relevant paper notes from `repo/PAPERNOTES.md`:
        {paper_notes}

        Worker terminal output:
        {trim_text(worker_terminal_output, 18000)}

        {theorem_frontier_paper_verifier_instructions(config, state, phase, config.reviewer.provider)}

        Before ending this turn:
        - write your paper-verifier JSON to `{artifact_label}`
        - also print the same JSON as the final thing in your terminal output
        """
    ).strip()


def build_reviewer_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_terminal_output: str,
    worker_handoff_text: str,
    validation_summary: Dict[str, Any],
    is_initial: bool,
    *,
    include_terminal_output: bool = True,
    policy: Optional[Policy] = None,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    recent_reviews = state.get("review_log", [])[-3:]
    decision_values = format_json_enum(phase_specific_reviewer_decisions(phase))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    terminal_section = (
        trim_text(worker_terminal_output, 18000)
        if include_terminal_output
        else "[omitted from the web transcript; raw terminal output is only kept in local logs]"
    )
    worker_handoff_label = supervisor_prompt_label(config, config.reviewer.provider, config.state_dir / "worker_handoff.json")
    frontier_update_text = ""
    if theorem_frontier_enabled(config, phase):
        frontier_update = state.get("last_theorem_frontier_worker_update")
        frontier_update_label = supervisor_prompt_label(
            config,
            config.reviewer.provider,
            theorem_frontier_worker_update_path(config),
        )
        frontier_update_text = textwrap.dedent(
            f"""\

            Worker theorem-frontier JSON from `{frontier_update_label}`:
            {json.dumps(frontier_update, indent=2, ensure_ascii=False) if isinstance(frontier_update, dict) else "{}"}
            """
        )
    paper_verifier_text = ""
    if theorem_frontier_full_enabled(config, phase):
        paper_verifier = state.get("last_theorem_frontier_paper_review")
        paper_verifier_label = supervisor_prompt_label(
            config,
            config.reviewer.provider,
            theorem_frontier_paper_verifier_path(config),
        )
        paper_verifier_text = textwrap.dedent(
            f"""\

            Paper-verifier structural review from `{paper_verifier_label}`:
            {json.dumps(paper_verifier, indent=2, ensure_ascii=False) if isinstance(paper_verifier, dict) else "{}"}
            """
        )
    validation_label = supervisor_prompt_label(config, config.reviewer.provider, validation_summary_path(config))
    review_decision_label = supervisor_prompt_label(config, config.reviewer.provider, config.state_dir / "review_decision.json")
    frontier_review_notes = theorem_frontier_reviewer_instructions(config, state, phase, config.reviewer.provider)
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.reviewer,
    )
    return textwrap.dedent(
        f"""\
        You are the review agent supervising the worker.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Recent reviewer decisions:
        {json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]"}

        Worker handoff JSON from `{worker_handoff_label}`:
        {worker_handoff_text}
        {frontier_update_text}
        {paper_verifier_text}

        Supervisor validation summary from `{validation_label}`:
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 16000)}

        Worker's latest terminal output:
        {terminal_section}

        {phase_reviewer_instructions(config, phase)}
        {frontier_review_notes}
        {policy_notes}

        Before ending this turn:
        - write your decision JSON to `{review_decision_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "decision": {decision_values},
          "confidence": 0.0,
          "reason": "brief reason",
          "next_prompt": "short prompt for the worker; empty only if there is no next worker burst"
        }}
        """
    ).strip()


def build_stuck_recovery_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_terminal_output: str,
    worker_handoff_text: str,
    validation_summary: Dict[str, Any],
    last_review: Dict[str, Any],
    is_initial: bool,
    *,
    include_terminal_output: bool = True,
    policy: Optional[Policy] = None,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    attempts = stuck_recovery_attempts(state)
    attempt_number = len(attempts) + 1
    attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
    terminal_section = (
        trim_text(worker_terminal_output, 18000)
        if include_terminal_output
        else "[omitted from the web transcript; raw terminal output is only kept in local logs]"
    )
    prior_attempts = [
        {
            "attempt": attempt.get("attempt"),
            "diagnosis": attempt.get("diagnosis"),
            "creative_suggestion": attempt.get("creative_suggestion"),
            "why_this_might_work": attempt.get("why_this_might_work"),
            "worker_prompt": attempt.get("worker_prompt"),
        }
        for attempt in attempts
    ]
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    worker_handoff_label = supervisor_prompt_label(config, config.reviewer.provider, config.state_dir / "worker_handoff.json")
    validation_label = supervisor_prompt_label(config, config.reviewer.provider, validation_summary_path(config))
    stuck_recovery_label = supervisor_prompt_label(config, config.reviewer.provider, stuck_recovery_suggestion_path(config))
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.reviewer,
    )
    return textwrap.dedent(
        f"""\
        You are temporarily acting as the supervisor's stuck-recovery reviewer.

        {preface}

        The normal reviewer has already concluded that the current workflow is genuinely stuck.
        Your job is not to decide `STUCK` versus `CONTINUE`.
        Instead, review the blocker carefully and propose one creative but concrete recovery strategy for the worker to try next.

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Triggering stuck review:
        {json.dumps(last_review, indent=2, ensure_ascii=False)}

        Prior stuck-recovery attempts for this same stuck episode:
        {json.dumps(prior_attempts, indent=2, ensure_ascii=False) if prior_attempts else "[]"}

        Worker handoff JSON from `{worker_handoff_label}`:
        {worker_handoff_text}

        Supervisor validation summary from `{validation_label}`:
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 16000)}

        Worker's latest terminal output:
        {terminal_section}

        Requirements:
        - Propose a materially different strategy from any prior stuck-recovery attempts listed above.
        - Be creative, but keep the suggestion technically grounded in the actual blocker.
        - Prefer suggestions that could unblock the worker without human input, new axioms, or abandoning the paper-facing interface.
        - Focus on a concrete next experiment, refactor, alternative reduction, counterexample check, or route change the worker can actually try in the next burst.
        - If the best idea is an explicit route change, say so directly and explain why it is different from the failed route.
        {policy_notes}

        Before ending this turn:
        - write your recovery JSON to `{stuck_recovery_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "diagnosis": "brief diagnosis of the blocker",
          "creative_suggestion": "one creative but concrete recovery strategy",
          "why_this_might_work": "brief rationale",
          "worker_prompt": "a short direct prompt telling the worker exactly what to try next"
        }}

        This is recovery attempt {attempt_number} of {attempt_limit} for the current stuck episode.
        """
    ).strip()


def build_branch_strategy_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_terminal_output: str,
    worker_handoff_text: str,
    validation_summary: Dict[str, Any],
    last_review: Dict[str, Any],
    is_initial: bool,
    *,
    include_terminal_output: bool = True,
    policy: Optional[Policy] = None,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    recent_reviews = state.get("review_log", [])[-6:]
    strategy_limit = branch_strategy_limit(config, state)
    terminal_section = (
        trim_text(worker_terminal_output, 18000)
        if include_terminal_output
        else "[omitted from the web transcript; raw terminal output is only kept in local logs]"
    )
    worker_handoff_label = supervisor_prompt_label(config, config.reviewer.provider, config.state_dir / "worker_handoff.json")
    validation_label = supervisor_prompt_label(config, config.reviewer.provider, validation_summary_path(config))
    branch_strategy_label = supervisor_prompt_label(config, config.reviewer.provider, branch_strategy_artifact_path(config))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    parent_control_note = ""
    theorem_branch_note = ""
    active_frontier_anchor = ""
    if not branching_enabled(config) and can_propose_branch_replacement(state, config):
        parent_control_note = textwrap.dedent(
            f"""\
            This run is currently a leaf inside a parent-managed branch frontier.
            If you return `BRANCH`, you are proposing up to {strategy_limit} replacement child strategies for the parent supervisor to evaluate.
            The child branches will not be created immediately in this run; the parent supervisor will decide whether the current frontier should be replaced.
            """
        )
    if theorem_frontier_full_enabled(config, phase):
        frontier_summary = theorem_frontier_branch_summary(state)
        active_leaf_id = normalize_frontier_text(frontier_summary.get("active_leaf_id"))
        if active_leaf_id:
            active_frontier_anchor = active_leaf_id
            blocker_cluster = str(frontier_summary.get("blocker_cluster") or "").strip()
            theorem_branch_note = textwrap.dedent(
                f"""\
                Theorem-frontier branching rule:
                - Any branch proposal must be a competing replacement route for the active theorem node `{active_leaf_id}`.
                - Do not propose branches that widen the frontier above or outside that node's subtree.
                - Branch only when there are genuinely competing next moves for this node: different close routes, materially different expansions, or a real refute/replace alternative.
                - Do not branch just to keep multiple wrapper-building or bookkeeping variants of the same blocker alive.
                - If the routes still share the same blocker cluster and unresolved hypothesis set, prefer `NO_BRANCH`.
                - Branching is most justified when escalation pressure is building or when there are clearly different ways to cut blocker cluster `{blocker_cluster or '(unset)'}`.
                """
            )
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.branching,
    )
    return textwrap.dedent(
        f"""\
        You are temporarily acting as the supervisor's branching strategist.

        {preface}

        Your job is to decide whether the current run should stay on one route or split into multiple branches with materially different strategies.
        A branch is justified only if there are genuinely different routes to try, such as continuing the current proof path versus a major rewrite or route change.
        Do not branch just because one path is difficult or because two branches would be superficially different.

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Latest reviewer decision:
        {json.dumps(last_review, indent=2, ensure_ascii=False)}

        Recent reviewer decisions:
        {json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]"}

        Worker handoff JSON from `{worker_handoff_label}`:
        {worker_handoff_text}

        Supervisor validation summary from `{validation_label}`:
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 16000)}

        Worker's latest terminal output:
        {terminal_section}

        {parent_control_note}
        {theorem_branch_note}
        {policy_notes}

        Branching policy:
        - At most {strategy_limit} branches may run concurrently in this branch episode or replacement frontier.
        - Branches should be designed to answer the question: which route seems more likely to eventually succeed at formalizing the whole paper?
        - Do not prefer the route that is merely further along today if it appears structurally flawed.
        - Prefer branches whose strategies are materially different: e.g. continue current route, major rewrite, alternate theorem route, alternate abstraction.
        - In theorem-frontier mode, each strategy should represent a genuinely different way to close the anchored node or replace it paper-faithfully; superficial wrapper variants do not justify branching.
        - If no such strategic fork exists yet, return `NO_BRANCH`.

        Before ending this turn:
        - write your branch-strategy JSON to `{branch_strategy_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "branch_decision": "NO_BRANCH" | "BRANCH",
          "frontier_anchor_node_id": "{active_frontier_anchor}",
          "confidence": 0.0,
          "reason": "brief reason",
          "strategies": [
            {{
              "name": "short-branch-name",
              "summary": "one-sentence strategy summary",
              "worker_prompt": "direct branch-specific worker prompt",
              "why_this_might_eventually_succeed": "why this route could still formalize the whole paper",
              "rewrite_scope": "incremental" | "major"
            }}
          ]
        }}

        If `branch_decision` is `BRANCH`, include between 2 and {strategy_limit} strategies.
        If `branch_decision` is `NO_BRANCH`, return an empty `strategies` list.
        """
    ).strip()


def build_branch_selection_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    episode: Dict[str, Any],
    branch_snapshots: List[Dict[str, Any]],
    is_initial: bool,
    *,
    policy: Optional[Policy] = None,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    selection_label = supervisor_prompt_label(config, config.reviewer.provider, branch_selection_artifact_path(config))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    continue_count = branch_selection_continue_count(config, episode, policy)
    initial_budget = branch_review_budget(config, policy)
    question = str(
        episode.get(
            "selection_question",
            "Which branch seems more likely to eventually succeed at formalizing the whole paper?",
        )
    ).strip()
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.branching,
    )
    theorem_branch_note = ""
    active_frontier_anchor = ""
    if theorem_frontier_full_enabled(config, phase):
        frontier_summary = theorem_frontier_branch_summary(state)
        active_leaf_id = normalize_frontier_text(frontier_summary.get("active_leaf_id"))
        if active_leaf_id:
            active_frontier_anchor = active_leaf_id
            theorem_branch_note = (
                f"Active theorem-frontier branch point: `{active_leaf_id}`. "
                "Prefer the branch that most cleanly closes that subtree, strictly reduces the unresolved hypothesis set, "
                "and leaves the smallest residual cutset."
            )
    post_initial_guidance = ""
    if continue_count > 0:
        post_initial_guidance = textwrap.dedent(
            f"""\
            Additional guidance for this later checkpoint:
            - This branch episode is already past the initial {initial_budget}-review checkpoint.
            - Resource cost now matters more than before.
            - Do not keep a clearly less promising branch alive merely because it is still making local progress.
            - Prefer `SELECT_BRANCH` whenever one branch now looks meaningfully more likely to eventually formalize the whole paper.
            - Return `CONTINUE_BRANCHING` only when the branches still look genuinely close and it remains honestly hard to name a preferred branch.

            """
        )
    return textwrap.dedent(
        f"""\
        You are temporarily acting as the supervisor's branch selector.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Branch episode metadata:
        {json.dumps(episode, indent=2, ensure_ascii=False)}

        Current branch snapshots:
        {json.dumps(branch_snapshots, indent=2, ensure_ascii=False)}

        Decision question:
        {question}

        {policy_notes}
        {theorem_branch_note}
        {post_initial_guidance}

        Requirements:
        - Judge branches by their likelihood of eventually succeeding at formalizing the whole paper.
        - Do not default to the branch that is merely furthest along today.
        - Prefer the branch whose route appears structurally sound and paper-faithful, even if it is temporarily behind.
        - In theorem-frontier mode, compare branches by whether they are actually shrinking the anchored node's unresolved dependency set, blocker age, and escalation pressure.
        - Penalize branches that mainly add wrappers while preserving the same blocker cluster and open hypotheses.
        - Return `CONTINUE_BRANCHING` if the evidence is still too weak and the branches should keep running.
        - Return `SELECT_BRANCH` only if one branch is now clearly the better bet.

        Before ending this turn:
        - write your branch-selection JSON to `{selection_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "selection_decision": "CONTINUE_BRANCHING" | "SELECT_BRANCH",
          "frontier_anchor_node_id": "{active_frontier_anchor}",
          "confidence": 0.0,
          "reason": "brief reason",
          "selected_branch": "branch name or empty string"
        }}
        """
    ).strip()


def build_branch_replacement_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    episode: Dict[str, Any],
    branch_snapshots: List[Dict[str, Any]],
    proposal_snapshot: Dict[str, Any],
    is_initial: bool,
    *,
    policy: Optional[Policy] = None,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    replacement_label = supervisor_prompt_label(config, config.reviewer.provider, branch_replacement_artifact_path(config))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    proposal = proposal_snapshot.get("pending_branch_proposal") if isinstance(proposal_snapshot, dict) else {}
    threshold = branch_replacement_min_confidence(config, policy)
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.branching,
    )
    theorem_branch_note = ""
    if theorem_frontier_full_enabled(config, phase):
        anchor_id = normalize_frontier_text(episode.get("frontier_anchor_node_id"))
        blocker = str(episode.get("frontier_anchor_blocker_cluster") or "").strip()
        if anchor_id:
            theorem_branch_note = (
                f"The active frontier is anchored at theorem node `{anchor_id}`. "
                f"Only replace the current frontier if the proposal offers materially different ways to close or replace that same node/subtree, not just new wrapper variants for blocker `{blocker or '(unset)'}`."
            )
    return textwrap.dedent(
        f"""\
        You are temporarily acting as the supervisor's branch-frontier selector.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Active branch episode metadata:
        {json.dumps(episode, indent=2, ensure_ascii=False)}

        Current active branch frontier:
        {json.dumps(branch_snapshots, indent=2, ensure_ascii=False)}

        Pending replacement proposal from branch `{proposal_snapshot.get("name", "")}`:
        {json.dumps(proposal, indent=2, ensure_ascii=False)}

        Decision question:
        Should the current branch frontier be replaced by selecting `{proposal_snapshot.get("name", "")}` as the winning route now,
        pruning the other active branches in this episode, and immediately branching that winning route into the proposed child strategies?

        {policy_notes}
        {theorem_branch_note}

        Requirements:
        - Judge routes by their likelihood of eventually succeeding at formalizing the whole paper.
        - This is a high-bar intervention. Return `REPLACE_WITH_PROPOSAL` only if the proposal is clearly stronger than continuing the current capped frontier.
        - The proposed child strategies must be materially different from each other.
        - The proposed child strategies must also be materially different from the surviving current frontier alternatives they would displace.
        - In theorem-frontier mode, the proposal should show a clearer plan for shrinking the anchored node's open hypotheses or refuting/replacing that node paper-faithfully.
        - Do not choose replacement merely because the proposal is newer or more exciting.
        - Prefer `KEEP_FRONTIER` if the evidence is mixed, if the proposal looks like branch churn, or if confidence is below {threshold:.1f}.
        - Return `REPLACE_WITH_PROPOSAL` only if you are confidently endorsing a full frontier replacement now.

        Before ending this turn:
        - write your branch-replacement JSON to `{replacement_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "replacement_decision": "KEEP_FRONTIER" | "REPLACE_WITH_PROPOSAL",
          "confidence": 0.0,
          "reason": "brief reason"
        }}
        """
    ).strip()


def supervisor_warnings_log_path(config: Config) -> Path:
    return config.state_dir / "logs" / "supervisor_warnings.jsonl"


def log_supervisor_warning(
    config: Config,
    *,
    cycle: int,
    phase: str,
    category: str,
    message: str,
    detail: Any = None,
) -> None:
    entry: Dict[str, Any] = {
        "timestamp": timestamp_now(),
        "cycle": cycle,
        "phase": phase,
        "category": category,
        "message": message,
    }
    if detail is not None:
        entry["detail"] = detail
    print(f"WARNING [{category}] cycle {cycle}: {message}")
    append_jsonl(supervisor_warnings_log_path(config), entry)


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_chat_event(
    config: Config,
    state: Dict[str, Any],
    *,
    cycle: int,
    phase: str,
    kind: str,
    actor: str,
    target: str,
    content: Any,
    content_type: str,
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_chat_site(config)
    timestamp = timestamp_now()
    event_summary = summary if summary is not None else summarize_chat_event(kind, content)
    event = {
        "timestamp": timestamp,
        "repo_name": config.chat.repo_name,
        "cycle": cycle,
        "phase": phase,
        "kind": kind,
        "actor": actor,
        "target": target,
        "content_type": content_type,
        "summary": event_summary,
        "content": content,
    }
    append_jsonl(chat_repo_events_path(config), event)
    append_chat_event_chunk(config, event)

    meta = load_chat_meta(config)
    meta.update(
        {
            "updated_at": timestamp,
            "current_phase": phase,
            "current_cycle": cycle,
            "event_count": int(meta.get("event_count") or 0) + 1,
            "last_event_kind": kind,
            "last_summary": event_summary,
            "awaiting_human_input": bool(state.get("awaiting_human_input")),
        }
    )
    meta["markdown_files"] = sync_chat_markdown_files(config)
    if kind == "worker_handoff" and isinstance(content, dict):
        meta["last_worker_status"] = content.get("status")
    if kind == "reviewer_decision" and isinstance(content, dict):
        meta["last_reviewer_decision"] = content.get("decision")
    meta["branch_overview"] = branch_overview(state)
    JsonFile.dump(chat_repo_meta_path(config), meta)
    update_chat_manifest(config, meta)
    ensure_dag_site(config)
    export_dag_meta(config, state)
    return event


def repo_lean_files(config: Config) -> List[Path]:
    excluded = {".git", ".lake", "build", "lake-packages", ".agent-supervisor"}
    results: List[Path] = []
    for path in config.repo_path.rglob("*.lean"):
        if any(part in excluded for part in path.parts):
            continue
        results.append(path)
    return sorted(results)


def run_command(command: Sequence[str], cwd: Path) -> Dict[str, Any]:
    proc = subprocess.run(
        list(command),
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "command": list(command),
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": output,
    }


def has_lake_project(config: Config) -> bool:
    return (config.repo_path / "lakefile.toml").exists() or (config.repo_path / "lakefile.lean").exists()


def syntax_check_file(config: Config, path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "path": relative_repo_label(config, path),
            "exists": False,
            "ok": False,
            "returncode": None,
            "output": "File is missing.",
        }
    if not has_lake_project(config):
        return {
            "path": relative_repo_label(config, path),
            "exists": True,
            "ok": False,
            "returncode": None,
            "output": "No lake project found for syntax checking.",
        }
    result = run_command(["lake", "env", "lean", str(path.relative_to(config.repo_path))], cwd=config.repo_path)
    return {
        "path": relative_repo_label(config, path),
        "exists": True,
        "ok": result["ok"],
        "returncode": result["returncode"],
        "output": result["output"],
    }


def collect_sorries(config: Config) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    by_file: Dict[str, int] = {}
    for path in repo_lean_files(config):
        rel = relative_repo_label(config, path)
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            if re.search(r"\bsorry\b", line):
                entries.append({"path": rel, "line": lineno, "text": stripped})
                by_file[rel] = by_file.get(rel, 0) + 1
    return {
        "count": len(entries),
        "entries": entries,
        "by_file": [{"path": path, "count": count} for path, count in sorted(by_file.items())],
    }


def collect_axioms(config: Config) -> Dict[str, Any]:
    approved = set(approved_axioms(config))
    found: List[Dict[str, Any]] = []
    for path in repo_lean_files(config):
        rel = relative_repo_label(config, path)
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            match = re.match(r"^\s*(axiom|constant)\s+([^\s:(]+)", line)
            if match:
                kind, name = match.groups()
                found.append(
                    {
                        "path": rel,
                        "line": lineno,
                        "kind": kind,
                        "name": name,
                        "approved": name in approved,
                        "text": stripped,
                    }
                )
    return {
        "approved_axioms": sorted(approved),
        "found": found,
        "unapproved": [entry for entry in found if not entry["approved"]],
    }


def phase_required_files(config: Config, phase: str) -> Dict[str, bool]:
    files = {
        "TASKS.md": (config.repo_path / "TASKS.md").exists(),
        "GOAL.md": config.goal_file.exists(),
    }
    if config.workflow.paper_tex_path is not None:
        files[relative_repo_label(config, config.workflow.paper_tex_path)] = config.workflow.paper_tex_path.exists()
    if phase_uses_paper_notes(phase):
        files["PAPERNOTES.md"] = (config.repo_path / "PAPERNOTES.md").exists()
    if phase_uses_plan(phase):
        files["PLAN.md"] = (config.repo_path / "PLAN.md").exists()
    if phase_uses_statement_files(phase):
        files["PaperDefinitions.lean"] = (config.repo_path / "PaperDefinitions.lean").exists()
        files["PaperTheorems.lean"] = (config.repo_path / "PaperTheorems.lean").exists()
    if phase == "theorem_stating" and theorem_frontier_phase(config) == "full":
        files[paper_main_results_manifest_path(config).name] = paper_main_results_manifest_path(config).exists()
    return files


def validation_sorry_policy(config: Config, phase: str, sorrys: Dict[str, Any]) -> Dict[str, Any]:
    if config.workflow.sorry_mode == "allowed":
        return {
            "mode": "allowed",
            "allowed_files": ["any"],
            "disallowed_entries": [],
        }
    if phase in {"theorem_stating", "proof_formalization", PHASE_PROOF_COMPLETE_STYLE_CLEANUP}:
        allowed_file = "repo/PaperTheorems.lean"
    else:
        allowed_file = None
    disallowed = []
    for entry in sorrys["entries"]:
        if allowed_file is None or entry["path"] != allowed_file:
            disallowed.append(entry)
    return {
        "mode": "default",
        "allowed_files": [allowed_file] if allowed_file else [],
        "disallowed_entries": disallowed,
    }


def run_validation(
    config: Config,
    phase: str,
    cycle: int,
    *,
    previous_validation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "cycle": cycle,
        "phase": phase,
        "sorry_mode": config.workflow.sorry_mode,
        "required_files": phase_required_files(config, phase),
    }
    summary["all_required_files_present"] = all(summary["required_files"].values())

    build_result: Dict[str, Any]
    if has_lake_project(config):
        build_result = run_command(["lake", "build"], cwd=config.repo_path)
    else:
        build_result = {"command": ["lake", "build"], "ok": False, "returncode": None, "output": "No lake project found."}
    summary["build"] = {
        "ran": has_lake_project(config),
        "ok": build_result["ok"],
        "returncode": build_result["returncode"],
        "output": trim_text(build_result["output"], 12000),
    }

    syntax_checks = []
    if phase_uses_statement_files(phase):
        for path in (config.repo_path / "PaperDefinitions.lean", config.repo_path / "PaperTheorems.lean"):
            check = syntax_check_file(config, path)
            check["output"] = trim_text(str(check["output"]), 4000)
            syntax_checks.append(check)
    summary["syntax_checks"] = syntax_checks
    if phase == "theorem_stating" and theorem_frontier_phase(config) == "full":
        manifest_path = paper_main_results_manifest_path(config)
        manifest_summary: Dict[str, Any] = {
            "path": str(manifest_path),
            "present": manifest_path.exists(),
            "ok": False,
            "main_result_count": 0,
            "initial_active_node_id": "",
            "error": "",
        }
        if manifest_path.exists():
            try:
                manifest = load_validated_paper_main_results_manifest(config)
                manifest_summary["ok"] = True
                manifest_summary["main_result_count"] = len(manifest["main_results"])
                manifest_summary["initial_active_node_id"] = manifest["initial_active_node_id"]
            except SupervisorError as exc:
                manifest_summary["error"] = str(exc)
        summary["paper_main_results_manifest"] = manifest_summary

    sorrys = collect_sorries(config)
    summary["sorries"] = sorrys
    summary["sorry_policy"] = validation_sorry_policy(config, phase, sorrys)

    axioms = collect_axioms(config)
    summary["axioms"] = axioms
    summary["git"] = git_validation_summary(config)
    if isinstance(summary["git"], dict):
        summary["git"]["previous_validation_head"] = validation_git_head(previous_validation)
        summary["git"]["changed_lean_files"] = changed_lean_files_since_validation(config, previous_validation)

    summary["policy_ok"] = (
        summary["all_required_files_present"]
        and (summary["build"]["ok"] or phase in {"paper_check", "planning"})
        and not summary["sorry_policy"]["disallowed_entries"]
        and not summary["axioms"]["unapproved"]
        and all(check["ok"] for check in syntax_checks)
        and (
            phase != "theorem_stating"
            or theorem_frontier_phase(config) != "full"
            or bool(summary.get("paper_main_results_manifest", {}).get("ok"))
        )
    )

    path = validation_summary_path(config)
    JsonFile.dump(path, summary)
    append_jsonl(config.state_dir / "validation_log.jsonl", summary)
    return summary


def build_burst_script(
    adapter: ProviderAdapter,
    cycle: int,
    prompt_file: Path,
    start_file: Path,
    exit_file: Path,
    *,
    script_tag: Optional[str] = None,
) -> Path:
    runtime_dir = adapter.config.state_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    safe_script_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", script_tag).strip("-") if script_tag else None
    script_stem = f"{adapter.role}-{safe_script_tag}" if safe_script_tag else f"{adapter.role}-cycle-{cycle:04d}"
    script_path = runtime_dir / f"{script_stem}.sh"
    work_dir = adapter.work_dir()
    env_vars = adapter.burst_env()

    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        f"START_FILE={shlex.quote(str(start_file))}",
        f"EXIT_FILE={shlex.quote(str(exit_file))}",
        f"PROMPT_FILE={shlex.quote(str(prompt_file))}",
        f"WORK_DIR={shlex.quote(str(work_dir))}",
        "cleanup() {",
        "  ec=$?",
        "  printf '%s\n' \"$ec\" > \"$EXIT_FILE\"",
        "  exit \"$ec\"",
        "}",
        "trap cleanup EXIT",
        "cd \"$WORK_DIR\"",
        "printf '%s\n' \"$(date -Is)\" > \"$START_FILE\"",
        "PROMPT_CONTENT=$(cat \"$PROMPT_FILE\")",
        f"echo '[agent-burst] role={adapter.role} provider={adapter.cfg.provider} cwd='\"$PWD\"",
        "echo '[agent-burst] start='$(date -Is)",
    ]
    for key, value in env_vars.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    lines.append("cmd=(")
    for arg in adapter.current_command():
        lines.append(f"  {shlex.quote(arg)}")
    lines += [
        ")",
        "real_cmd=()",
        "for arg in \"${cmd[@]}\"; do",
        f"  if [[ \"$arg\" == {shlex.quote(PROMPT_TOKEN)} ]]; then",
        "    real_cmd+=(\"$PROMPT_CONTENT\")",
        "  else",
        "    real_cmd+=(\"$arg\")",
        "  fi",
        "done",
        "printf '[agent-burst] command:'",
        "for arg in \"${real_cmd[@]}\"; do printf ' %q' \"$arg\"; done",
        "printf '\n'",
        "\"${real_cmd[@]}\"",
        "ec=$?",
        "echo '[agent-burst] end='$(date -Is) ' exit_code='\"$ec\"",
        "exit \"$ec\"",
    ]
    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def write_log_header(path: Path, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(header)


def burst_captured_output(log_path: Path, pane_capture: str) -> str:
    log_text = read_text(log_path)
    if log_text.strip():
        return log_text
    return pane_capture


def pane_is_dead(pane_id: str) -> bool:
    proc = tmux_cmd("display-message", "-p", "-t", pane_id, "#{pane_dead}", check=False)
    if proc.returncode != 0:
        return True
    return proc.stdout.strip() == "1"


def wait_for_path(
    path: Path,
    pane_id: str,
    timeout_seconds: float,
    *,
    role: str,
    state_name: str,
    log_path: Path,
    poll_callback: Optional[Callable[[], None]] = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    pane_exit_grace_seconds = 1.0
    while True:
        if path.exists():
            return
        if poll_callback is not None:
            poll_callback()
        if pane_is_dead(pane_id):
            grace_deadline = time.monotonic() + pane_exit_grace_seconds
            while time.monotonic() < grace_deadline:
                if path.exists():
                    return
                time.sleep(0.05)
            raise SupervisorError(
                f"{role.capitalize()} pane exited before writing {state_name}: {path}. See {log_path}"
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise SupervisorError(
                f"Timed out after {timeout_seconds:.1f}s waiting for {role} {state_name}. See {log_path}"
            )
        time.sleep(0.2)


def find_live_tmux_burst_pane(session: str, window_name: str) -> Optional[Dict[str, str]]:
    proc = tmux_cmd(
        "list-panes",
        "-t",
        session,
        "-F",
        "#{window_id}\t#{window_name}\t#{pane_id}\t#{pane_dead}",
        check=False,
    )
    if proc.returncode != 0:
        return None
    matches: List[Dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        window_id, listed_window_name, pane_id, pane_dead = parts
        if listed_window_name != window_name or pane_dead != "0":
            continue
        matches.append({"window_id": window_id, "pane_id": pane_id})
    if len(matches) > 1:
        raise SupervisorError(
            f"Found multiple live tmux panes for {session}:{window_name}; refusing to resume ambiguously."
        )
    return matches[0] if matches else None


def wait_for_tmux_burst_completion(
    adapter: ProviderAdapter,
    *,
    pane_id: str,
    window_id: str,
    prompt_stem: str,
    artifact_path: Path,
    per_cycle_log: Path,
    latest_log: Path,
    start_file: Path,
    exit_file: Path,
    session: str,
    window_name: str,
) -> Dict[str, Any]:
    print(f"tmux_session={session} window={window_name} pane={pane_id}")
    print(f"Attach with: tmux attach -t {session}")
    captured_text = ""
    completed = False
    chat_markdown_refresher = ChatMarkdownRefresher(adapter.config)
    try:
        wait_for_path(
            start_file,
            pane_id,
            adapter.config.startup_timeout_seconds,
            role=adapter.role,
            state_name="startup marker",
            log_path=per_cycle_log,
            poll_callback=chat_markdown_refresher.maybe_refresh,
        )
        wait_for_path(
            exit_file,
            pane_id,
            adapter.config.burst_timeout_seconds,
            role=adapter.role,
            state_name="exit marker",
            log_path=per_cycle_log,
            poll_callback=chat_markdown_refresher.maybe_refresh,
        )
        completed = True
    finally:
        time.sleep(0.3)
        chat_markdown_refresher.maybe_refresh(force=True)
        capture = tmux_cmd("capture-pane", "-p", "-t", pane_id, "-S", "-2000", check=False)
        pane_capture = capture.stdout if capture.returncode == 0 else ""
        captured_text = burst_captured_output(per_cycle_log, pane_capture)
        latest_log.write_text(read_text(per_cycle_log), encoding="utf-8")

    exit_code_text = read_text(exit_file).strip()
    if not exit_code_text:
        raise SupervisorError(f"Missing exit code file for {adapter.role}: {exit_file}. See {per_cycle_log}")
    exit_code = int(exit_code_text)

    if completed and adapter.config.tmux.kill_windows_after_capture:
        tmux_cmd("kill-window", "-t", window_id, check=False)

    return {
        "captured_output": captured_text,
        "artifact_path": artifact_path,
        "per_cycle_log": per_cycle_log,
        "exit_code": exit_code,
        "pane_id": pane_id,
        "window_id": window_id,
    }


class ChatMarkdownRefresher:
    def __init__(self, config: Config, *, interval_seconds: float = 2.0):
        self.config = config
        self.interval_seconds = interval_seconds
        self.next_refresh_at = 0.0
        self.last_warning: Optional[str] = None

    def maybe_refresh(self, *, force: bool = False) -> None:
        if not chat_repo_meta_path(self.config).exists():
            return
        now = time.monotonic()
        if not force and now < self.next_refresh_at:
            return
        try:
            refresh_chat_markdown_metadata(self.config, update_manifest=False)
            refresh_chat_codex_budget_status(self.config)
        except Exception as exc:
            message = str(exc)
            if message != self.last_warning:
                print(f"[chat-export] warning: could not refresh chat exports: {message}", file=sys.stderr)
                self.last_warning = message
            self.next_refresh_at = now + self.interval_seconds
            return
        self.last_warning = None
        self.next_refresh_at = now + self.interval_seconds


def launch_tmux_burst(
    adapter: ProviderAdapter,
    cycle: int,
    prompt: str,
    *,
    state: Optional[Dict[str, Any]] = None,
    phase: Optional[str] = None,
    artifact_name: Optional[str] = None,
    burst_tag: Optional[str] = None,
    reuse_existing_window: bool = False,
) -> Dict[str, Any]:
    state_dir = adapter.config.state_dir
    prompts_dir = state_dir / "prompts"
    logs_dir = state_dir / "logs"
    runtime_dir = state_dir / "runtime"
    safe_burst_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", burst_tag).strip("-") if burst_tag else None
    prompt_stem = f"{adapter.role}-{safe_burst_tag}" if safe_burst_tag else f"{adapter.role}-cycle-{cycle:04d}"
    prompt_file = prompts_dir / f"{prompt_stem}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    if artifact_name is not None:
        artifact_path = state_dir / artifact_name
    elif adapter.role == "worker":
        artifact_path = state_dir / "worker_handoff.json"
    else:
        artifact_path = state_dir / "review_decision.json"
    start_file = runtime_dir / f"{prompt_stem}.started"
    exit_file = runtime_dir / f"{prompt_stem}.exit"

    per_cycle_log = logs_dir / f"{prompt_stem}.ansi.log"
    aggregate_log = logs_dir / f"{adapter.role}.all.ansi.log"
    latest_log = logs_dir / f"{adapter.role}.latest.ansi.log"
    session = adapter.config.tmux.session_name
    window_name = f"{adapter.role}-{cycle:04d}" if safe_burst_tag is None else f"{adapter.role}-{safe_burst_tag}"

    if reuse_existing_window:
        existing = find_live_tmux_burst_pane(session, window_name)
        if existing is not None:
            return wait_for_tmux_burst_completion(
                adapter,
                pane_id=existing["pane_id"],
                window_id=existing["window_id"],
                prompt_stem=prompt_stem,
                artifact_path=artifact_path,
                per_cycle_log=per_cycle_log,
                latest_log=latest_log,
                start_file=start_file,
                exit_file=exit_file,
                session=session,
                window_name=window_name,
            )

    if state is not None and phase is not None and adapter.cfg.provider == "codex":
        wait_for_codex_weekly_budget_if_needed(
            adapter.config,
            state,
            phase=phase,
            stage_label=f"{adapter.role} burst",
        )

    artifact_path.unlink(missing_ok=True)  # type: ignore[arg-type]
    start_file.unlink(missing_ok=True)  # type: ignore[arg-type]
    exit_file.unlink(missing_ok=True)  # type: ignore[arg-type]

    script_path = build_burst_script(adapter, cycle, prompt_file, start_file, exit_file, script_tag=safe_burst_tag)

    header = (
        f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | role={adapter.role} provider={adapter.cfg.provider} "
        f"scope={adapter.scope_dir()} =====\n$ {script_path}\n\n"
    )
    write_log_header(per_cycle_log, header)
    write_log_header(aggregate_log, header)

    proc = tmux_cmd("new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}", "-t", session, "-n", window_name)
    window_id, pane_id = proc.stdout.strip().split()

    tmux_cmd("set-window-option", "-t", window_id, "remain-on-exit", "on")
    pipe_inner_cmd = (
        f"cat | tee -a {shlex.quote(str(aggregate_log))} >> {shlex.quote(str(per_cycle_log))}"
    )
    pipe_cmd = shlex.join(["bash", "-lc", pipe_inner_cmd])
    tmux_cmd("pipe-pane", "-o", "-t", pane_id, pipe_cmd)
    launch_cmd = f"{shlex.quote(str(script_path))}; exit"
    tmux_cmd("send-keys", "-t", pane_id, launch_cmd, "C-m")
    tmux_cmd("select-window", "-t", window_id)

    return wait_for_tmux_burst_completion(
        adapter,
        pane_id=pane_id,
        window_id=window_id,
        prompt_stem=prompt_stem,
        artifact_path=artifact_path,
        per_cycle_log=per_cycle_log,
        latest_log=latest_log,
        start_file=start_file,
        exit_file=exit_file,
        session=session,
        window_name=window_name,
    )


def launch_tmux_burst_with_retries(
    adapter: ProviderAdapter,
    cycle: int,
    prompt: str,
    *,
    state: Optional[Dict[str, Any]] = None,
    phase: Optional[str] = None,
    stage_label: str,
    artifact_name: Optional[str] = None,
    burst_tag: Optional[str] = None,
    policy: Optional[Policy] = None,
    reuse_existing_window: bool = False,
) -> Dict[str, Any]:
    retry_delays = agent_retry_delays_seconds(adapter.config, policy)
    max_attempts = len(retry_delays) + 1
    for attempt in range(1, max_attempts + 1):
        run = launch_tmux_burst(
            adapter,
            cycle,
            prompt,
            state=state,
            phase=phase,
            artifact_name=artifact_name,
            burst_tag=burst_tag,
            reuse_existing_window=reuse_existing_window and attempt == 1,
        )
        if run["exit_code"] == 0:
            return run

        if attempt > len(retry_delays):
            raise SupervisorError(
                f"{stage_label.capitalize()} process exited with code {run['exit_code']} after "
                f"{len(retry_delays)} retry attempts. See {run['per_cycle_log']}"
            )

        delay_seconds = retry_delays[attempt - 1]
        delay_hours = int(delay_seconds // 3600)
        print(
            f"{stage_label.capitalize()} process exited with code {run['exit_code']}. "
            f"Retrying the same burst in {delay_hours} hour(s). See {run['per_cycle_log']}"
        )
        time.sleep(delay_seconds)

    raise AssertionError("unreachable")


DEFAULT_VALIDATION_RETRY_LIMIT = 1


def run_burst_with_validation(
    adapter: ProviderAdapter,
    cycle: int,
    prompt: str,
    *,
    config: Optional[Config] = None,
    state: Optional[Dict[str, Any]] = None,
    phase: Optional[str] = None,
    stage_label: str,
    policy: Optional[Policy] = None,
    reuse_existing_window: bool = False,
    validate: Callable[[Dict[str, Any]], Any],
    validation_retry_limit: int = DEFAULT_VALIDATION_RETRY_LIMIT,
) -> Tuple[Dict[str, Any], Any]:
    """Launch a burst, then validate the result.

    If validation raises SupervisorError, re-launch the agent with a correction
    prompt appended, up to *validation_retry_limit* times.  Returns
    ``(burst_run_dict, validated_result)``.
    """
    current_prompt = prompt
    last_error: Optional[str] = None
    for attempt in range(1, validation_retry_limit + 2):
        run = launch_tmux_burst_with_retries(
            adapter,
            cycle,
            current_prompt,
            state=state,
            phase=phase,
            stage_label=stage_label,
            policy=policy,
            reuse_existing_window=reuse_existing_window and attempt == 1,
        )
        try:
            result = validate(run)
            return run, result
        except SupervisorError as exc:
            last_error = str(exc)
            if attempt > validation_retry_limit:
                raise
            if config is not None:
                log_supervisor_warning(
                    config,
                    cycle=cycle,
                    phase=phase or "",
                    category="validation_retry",
                    message=f"{stage_label} attempt {attempt}/{validation_retry_limit + 1}: {last_error}",
                    detail={"artifact_path": run.get("artifact_path")},
                )
            else:
                print(
                    f"WARNING [validation_retry] {stage_label} attempt {attempt}/{validation_retry_limit + 1}: "
                    f"{last_error}"
                )
            print(f"Re-launching {stage_label} with correction prompt.")
            correction = (
                f"\n\n"
                f"IMPORTANT CORRECTION — your previous output failed the supervisor's "
                f"artifact validation with this error:\n\n"
                f"  {last_error}\n\n"
                f"Please fix this exact error. Rewrite the required artifact file(s) with "
                f"the correct schema and try again. All other instructions from the "
                f"original prompt still apply."
            )
            current_prompt = prompt + correction
    raise SupervisorError(last_error or "validation failed")


def parse_json_object_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SupervisorError(f"Expected JSON artifact not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SupervisorError(f"Could not parse JSON artifact {path}: {exc}") from exc


def extract_json_objects(text: str) -> List[Dict[str, Any]]:
    decoder = json.JSONDecoder()
    results: List[Dict[str, Any]] = []
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            results.append(data)
    return results


def normalize_required_keys(required_key: Optional[Union[str, Sequence[str]]]) -> List[str]:
    if required_key is None:
        return []
    if isinstance(required_key, str):
        return [required_key]
    return [str(key) for key in required_key]


def extract_json_object(text: str, required_key: Optional[Union[str, Sequence[str]]] = None) -> Dict[str, Any]:
    candidates = extract_json_objects(text)
    required_keys = normalize_required_keys(required_key)
    if required_keys:
        candidates = [candidate for candidate in candidates if all(key in candidate for key in required_keys)]
    if candidates:
        return candidates[-1]
    raise SupervisorError("Could not parse JSON object from captured text")


def load_json_artifact_with_fallback(
    path: Path,
    captured_text: str,
    required_key: Union[str, Sequence[str]],
    *,
    fallback_paths: Sequence[Path] = (),
) -> Dict[str, Any]:
    required_keys = normalize_required_keys(required_key)
    errors: List[str] = []
    for candidate in [path, *fallback_paths]:
        if not candidate.exists():
            continue
        try:
            data = parse_json_object_file(candidate)
            if all(key in data for key in required_keys):
                return data
            errors.append(f"Artifact missing required keys {required_keys!r}: {candidate}")
        except SupervisorError as exc:
            errors.append(str(exc))
    try:
        return extract_json_object(captured_text, required_key=required_keys)
    except SupervisorError as exc:
        errors.append(str(exc))
    raise SupervisorError(" | ".join(errors))


WORKER_HANDOFF_KEY_ALIASES: Dict[str, List[str]] = {
    "summary_of_changes": ["summary", "changes", "change_summary"],
    "current_frontier": ["frontier", "current_focus", "focus"],
    "likely_next_step": ["next_step", "next_steps", "next"],
    "input_request": ["input", "request"],
}


def _normalize_worker_handoff_keys(handoff: Dict[str, Any]) -> Dict[str, Any]:
    for canonical, aliases in WORKER_HANDOFF_KEY_ALIASES.items():
        if canonical not in handoff:
            for alias in aliases:
                if alias in handoff:
                    handoff[canonical] = handoff.pop(alias)
                    break
    return handoff


def validate_worker_handoff(phase: str, handoff: Dict[str, Any]) -> Dict[str, Any]:
    handoff = _normalize_worker_handoff_keys(handoff)
    hard_required = {"status"}
    missing_hard = hard_required.difference(handoff)
    if missing_hard:
        raise SupervisorError(f"Worker handoff missing critical keys: {sorted(missing_hard)}")
    soft_keys = {"summary_of_changes", "current_frontier", "likely_next_step", "input_request"}
    for key in soft_keys:
        if key not in handoff:
            handoff[key] = ""
    if str(handoff.get("phase", "")).strip().lower() != phase:
        print(f"WARNING: Worker handoff phase mismatch: expected {phase}, got {handoff.get('phase')}; accepting anyway.")
    handoff["phase"] = phase
    status = str(handoff.get("status", "")).strip().upper()
    allowed = set(phase_specific_worker_statuses(phase))
    if status not in allowed:
        raise SupervisorError(f"Invalid worker status {status!r} for phase {phase}")
    handoff["status"] = status
    return handoff


def validate_reviewer_decision(phase: str, decision: Dict[str, Any]) -> Dict[str, Any]:
    hard_required = {"decision"}
    missing_hard = hard_required.difference(decision)
    if missing_hard:
        raise SupervisorError(f"Reviewer decision missing critical keys: {sorted(missing_hard)}")
    for key in ("confidence", "reason", "next_prompt"):
        if key not in decision:
            decision[key] = "" if key != "confidence" else 0.5
    if str(decision.get("phase", "")).strip().lower() != phase:
        print(f"WARNING: Reviewer decision phase mismatch: expected {phase}, got {decision.get('phase')}; accepting anyway.")
    decision["phase"] = phase
    value = str(decision.get("decision", "")).strip().upper()
    allowed = set(phase_specific_reviewer_decisions(phase))
    if value not in allowed:
        raise SupervisorError(f"Invalid reviewer decision {value!r} for phase {phase}")
    decision["decision"] = value
    return decision


def validate_stuck_recovery_suggestion(phase: str, suggestion: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {"phase", "diagnosis", "creative_suggestion", "why_this_might_work", "worker_prompt"}
    missing = required_keys.difference(suggestion)
    if missing:
        raise SupervisorError(f"Stuck-recovery suggestion missing keys: {sorted(missing)}")
    if str(suggestion.get("phase")).strip().lower() != phase:
        raise SupervisorError(
            f"Stuck-recovery suggestion phase mismatch: expected {phase}, got {suggestion.get('phase')}"
        )
    for key in ("diagnosis", "creative_suggestion", "why_this_might_work", "worker_prompt"):
        suggestion[key] = str(suggestion.get(key, "")).strip()
    if not suggestion["creative_suggestion"]:
        raise SupervisorError("Stuck-recovery suggestion must include a non-empty creative_suggestion.")
    if not suggestion["worker_prompt"]:
        raise SupervisorError("Stuck-recovery suggestion must include a non-empty worker_prompt.")
    return suggestion


def validate_branch_strategy_decision(
    config: Config,
    phase: str,
    decision: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    required_keys = {"phase", "branch_decision", "confidence", "reason", "strategies"}
    missing = required_keys.difference(decision)
    if missing:
        raise SupervisorError(f"Branch-strategy decision missing keys: {sorted(missing)}")
    if str(decision.get("phase")).strip().lower() != phase:
        raise SupervisorError(
            f"Branch-strategy decision phase mismatch: expected {phase}, got {decision.get('phase')}"
        )
    branch_decision = str(decision.get("branch_decision", "")).strip().upper()
    if branch_decision not in BRANCH_STRATEGY_DECISIONS:
        raise SupervisorError(f"Invalid branch_decision {branch_decision!r}")
    raw_strategies = decision.get("strategies")
    if not isinstance(raw_strategies, list):
        raise SupervisorError("Branch-strategy decision strategies must be a list.")
    strategies: List[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw in raw_strategies:
        if not isinstance(raw, dict):
            raise SupervisorError("Each branch strategy must be an object.")
        for key in ("name", "summary", "worker_prompt", "why_this_might_eventually_succeed", "rewrite_scope"):
            if key not in raw:
                raise SupervisorError(f"Branch strategy missing key {key!r}.")
        name = sanitize_branch_label(str(raw.get("name", "")))
        if not name:
            raise SupervisorError("Branch strategy name cannot be empty.")
        if name in seen_names:
            raise SupervisorError(f"Duplicate branch strategy name: {name}")
        seen_names.add(name)
        rewrite_scope = str(raw.get("rewrite_scope", "")).strip().lower()
        if rewrite_scope not in {"incremental", "major"}:
            raise SupervisorError(f"Invalid rewrite_scope {rewrite_scope!r} for branch strategy {name}")
        strategies.append(
            {
                "name": name,
                "summary": str(raw.get("summary", "")).strip(),
                "worker_prompt": str(raw.get("worker_prompt", "")).strip(),
                "why_this_might_eventually_succeed": str(raw.get("why_this_might_eventually_succeed", "")).strip(),
                "rewrite_scope": rewrite_scope,
            }
        )
    limit = branch_strategy_limit(config, state or {})
    if branch_decision == "NO_BRANCH":
        strategies = []
    elif not (2 <= len(strategies) <= limit):
        raise SupervisorError(
            "Branch-strategy decision must include between 2 and "
            f"{limit} strategies when branching."
        )
    frontier_anchor_node_id = normalize_frontier_text(decision.get("frontier_anchor_node_id"))
    active_leaf_id = theorem_frontier_active_leaf_id(state or {})
    if theorem_frontier_full_enabled(config, phase) and active_leaf_id:
        if frontier_anchor_node_id != active_leaf_id:
            raise SupervisorError(
                "Branch-strategy decision frontier_anchor_node_id must match the active theorem-frontier leaf "
                f"{active_leaf_id!r}."
            )
    decision["branch_decision"] = branch_decision
    decision["strategies"] = strategies
    decision["frontier_anchor_node_id"] = frontier_anchor_node_id
    return decision


def validate_branch_selection_decision(
    config: Config,
    phase: str,
    decision: Dict[str, Any],
    allowed_branches: Sequence[str],
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    required_keys = {"phase", "selection_decision", "confidence", "reason", "selected_branch"}
    missing = required_keys.difference(decision)
    if missing:
        raise SupervisorError(f"Branch-selection decision missing keys: {sorted(missing)}")
    if str(decision.get("phase")).strip().lower() != phase:
        raise SupervisorError(
            f"Branch-selection decision phase mismatch: expected {phase}, got {decision.get('phase')}"
        )
    selection_decision = str(decision.get("selection_decision", "")).strip().upper()
    if selection_decision not in BRANCH_SELECTION_DECISIONS:
        raise SupervisorError(f"Invalid selection_decision {selection_decision!r}")
    selected_branch = sanitize_branch_label(str(decision.get("selected_branch", "")))
    if selection_decision == "SELECT_BRANCH":
        if selected_branch not in set(allowed_branches):
            raise SupervisorError(
                f"Branch-selection decision selected invalid branch {selected_branch!r}; "
                f"allowed: {sorted(set(allowed_branches))}"
            )
    else:
        selected_branch = ""
    frontier_anchor_node_id = normalize_frontier_text(decision.get("frontier_anchor_node_id"))
    active_leaf_id = theorem_frontier_active_leaf_id(state or {})
    if theorem_frontier_full_enabled(config, phase) and active_leaf_id:
        if frontier_anchor_node_id != active_leaf_id:
            raise SupervisorError(
                "Branch-selection decision frontier_anchor_node_id must match the active theorem-frontier leaf "
                f"{active_leaf_id!r}."
            )
    decision["selection_decision"] = selection_decision
    decision["selected_branch"] = selected_branch
    decision["frontier_anchor_node_id"] = frontier_anchor_node_id
    return decision


def validate_branch_replacement_decision(
    phase: str,
    decision: Dict[str, Any],
    *,
    threshold: float = DEFAULT_BRANCH_FRONTIER_REPLACEMENT_MIN_CONFIDENCE,
) -> Dict[str, Any]:
    required_keys = {"phase", "replacement_decision", "confidence", "reason"}
    missing = required_keys.difference(decision)
    if missing:
        raise SupervisorError(f"Branch-replacement decision missing keys: {sorted(missing)}")
    if str(decision.get("phase")).strip().lower() != phase:
        raise SupervisorError(
            f"Branch-replacement decision phase mismatch: expected {phase}, got {decision.get('phase')}"
        )
    replacement_decision = str(decision.get("replacement_decision", "")).strip().upper()
    if replacement_decision not in BRANCH_REPLACEMENT_DECISIONS:
        raise SupervisorError(f"Invalid replacement_decision {replacement_decision!r}")
    try:
        confidence = float(decision.get("confidence", 0.0))
    except (TypeError, ValueError):
        raise SupervisorError("Branch-replacement decision confidence must be numeric.")
    if replacement_decision == "REPLACE_WITH_PROPOSAL" and confidence < threshold:
        raise SupervisorError(
            "Branch-replacement decision confidence must be at least "
            f"{threshold:.1f} to replace the frontier."
        )
    decision["replacement_decision"] = replacement_decision
    decision["confidence"] = confidence
    decision["reason"] = str(decision.get("reason", "")).strip()
    return decision


def recover_interrupted_worker_state(config: Config, state: Dict[str, Any], phase: str) -> bool:
    cycle = int(state.get("cycle", 0) or 0)
    if cycle <= 0 or last_review_cycle(state) >= cycle:
        return False
    if (
        isinstance(state.get("last_validation"), dict)
        and last_validation_cycle(state) == cycle
        and isinstance(state.get("last_worker_handoff"), dict)
        and "last_worker_output" in state
    ):
        return False

    artifact_path = config.state_dir / "worker_handoff.json"
    fallback_paths = legacy_supervisor_artifact_paths(config, artifact_path)
    log_path = config.state_dir / "logs" / f"worker-cycle-{cycle:04d}.ansi.log"
    worker_terminal_output = read_text(log_path).strip() if log_path.exists() else ""
    if not artifact_path.exists() and not any(path.exists() for path in fallback_paths) and not worker_terminal_output:
        return False

    try:
        worker_handoff = load_json_artifact_with_fallback(
            artifact_path,
            worker_terminal_output,
            ("phase", "status", "summary_of_changes", "current_frontier", "likely_next_step", "input_request"),
            fallback_paths=fallback_paths,
        )
    except SupervisorError:
        return False
    worker_handoff = validate_worker_handoff(phase, worker_handoff)
    state["last_worker_output"] = worker_terminal_output
    state["last_worker_handoff"] = worker_handoff
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="worker_handoff",
        actor="worker",
        target="supervisor",
        content=worker_handoff,
        content_type="json",
    )
    if theorem_frontier_enabled(config, phase):
        try:
            frontier_update = load_json_artifact_with_fallback(
                theorem_frontier_worker_update_path(config),
                worker_terminal_output,
                ("phase", "requested_action"),
                fallback_paths=legacy_supervisor_artifact_paths(config, theorem_frontier_worker_update_path(config)),
            )
            frontier_update = validate_theorem_frontier_worker_update_for_mode(config, phase, frontier_update)
            state["last_theorem_frontier_worker_update"] = frontier_update
        except SupervisorError as frontier_exc:
            log_supervisor_warning(
                config, cycle=cycle, phase=phase,
                category="frontier_recovery",
                message=str(frontier_exc),
                detail={"artifact_path": str(theorem_frontier_worker_update_path(config))},
            )
            state["last_theorem_frontier_worker_update"] = None
        if state.get("last_theorem_frontier_worker_update") is not None:
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="theorem_frontier_update",
                actor="worker",
                target="supervisor",
                content=state["last_theorem_frontier_worker_update"],
                content_type="json",
            )

    previous_validation = state.get("last_validation") if isinstance(state.get("last_validation"), dict) else None
    validation_summary = run_validation(config, phase, cycle, previous_validation=previous_validation)
    validation_summary["theorem_frontier_cone_files"] = apply_theorem_frontier_cone_file_guard(
        config,
        phase,
        validation_summary,
        state.get("last_theorem_frontier_worker_update"),
    )
    state["last_validation"] = validation_summary
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="validation_summary",
        actor="supervisor",
        target="reviewer",
        content=validation_summary,
        content_type="json",
    )
    save_state(config, state)
    return True


def maybe_consume_human_input(config: Config, state: Dict[str, Any]) -> bool:
    if not state.get("awaiting_human_input"):
        return True
    if not config.workflow.human_input_path.exists():
        return False
    if config.workflow.input_request_path.exists():
        input_mtime = config.workflow.human_input_path.stat().st_mtime
        request_mtime = config.workflow.input_request_path.stat().st_mtime
        if input_mtime <= request_mtime:
            return False
    text = read_text(config.workflow.human_input_path).strip()
    if not text:
        return False
    state["awaiting_human_input"] = False
    state["last_human_input"] = text
    state["last_human_input_path"] = str(config.workflow.human_input_path)
    state["pending_human_input_event"] = text
    config.workflow.input_request_path.unlink(missing_ok=True)  # type: ignore[arg-type]
    save_state(config, state)
    return True


def write_input_request(
    config: Config,
    phase: str,
    worker_handoff: Dict[str, Any],
    decision: Dict[str, Any],
    validation_summary: Dict[str, Any],
) -> None:
    body = textwrap.dedent(
        f"""\
        # Input Request

        The workflow paused in phase `{phase}` because the reviewer requested human input.

        ## Reviewer reason
        {decision.get("reason", "").strip()}

        ## Reviewer next prompt
        {decision.get("next_prompt", "").strip()}

        ## Worker frontier
        - Status: {worker_handoff.get("status", "").strip()}
        - Current frontier: {worker_handoff.get("current_frontier", "").strip()}
        - Likely next step: {worker_handoff.get("likely_next_step", "").strip()}
        - Input request: {worker_handoff.get("input_request", "").strip()}

        ## Approved axioms
        Update `{relative_repo_label(config, config.workflow.approved_axioms_path)}` if you are explicitly approving any axioms.

        ## Validation summary
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 12000)}

        ## Resume instructions
        Write your reply to `{relative_repo_label(config, config.workflow.human_input_path)}` and rerun the supervisor.
        """
    )
    config.workflow.input_request_path.write_text(body, encoding="utf-8")


def run_stuck_recovery_review(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    last_review = dict(state.get("last_review") or {})
    trigger_cycle = last_review_cycle(state)
    validation_summary = state.get("last_validation") or {}
    worker_terminal_output = str(state.get("last_worker_output") or "").strip()
    worker_handoff = state.get("last_worker_handoff") or {}
    worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
    attempt_number = current_stuck_recovery_attempt_number(state)
    burst_tag = f"stuck-recovery-{trigger_cycle:04d}-{attempt_number:02d}"

    prompt = build_stuck_recovery_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        validation_summary,
        last_review,
        reviewer.needs_initial_run(),
        policy=policy,
    )
    prompt_for_chat = build_stuck_recovery_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        validation_summary,
        last_review,
        reviewer.needs_initial_run(),
        include_terminal_output=False,
        policy=policy,
    )
    record_chat_event(
        config,
        state,
        cycle=trigger_cycle,
        phase=phase,
        kind="stuck_recovery_prompt",
        actor="supervisor",
        target="reviewer",
        content=prompt_for_chat,
        content_type="text",
        summary=f"Supervisor -> stuck-recovery prompt for cycle {trigger_cycle}",
    )

    def _validate_stuck_recovery(run: Dict[str, Any]) -> Dict[str, Any]:
        output = run["captured_output"].strip()
        sug = load_json_artifact_with_fallback(
            Path(run["artifact_path"]),
            output,
            ("phase", "diagnosis"),
            fallback_paths=legacy_supervisor_artifact_paths(config, Path(run["artifact_path"])),
        )
        return validate_stuck_recovery_suggestion(phase, sug)

    run, suggestion = run_burst_with_validation(
        reviewer,
        trigger_cycle,
        prompt,
        config=config,
        state=state,
        phase=phase,
        stage_label="reviewer stuck-recovery burst",
        policy=policy,
        validate=_validate_stuck_recovery,
    )
    reviewer.mark_initialized()
    suggestion = record_stuck_recovery_attempt(
        state,
        trigger_cycle=trigger_cycle,
        phase=phase,
        suggestion=suggestion,
    )
    record_chat_event(
        config,
        state,
        cycle=trigger_cycle,
        phase=phase,
        kind="stuck_recovery_suggestion",
        actor="reviewer",
        target="supervisor",
        content=suggestion,
        content_type="json",
    )
    save_state(config, state)
    append_jsonl(config.state_dir / "stuck_recovery_log.jsonl", suggestion)
    return suggestion


def run_theorem_frontier_paper_verifier_review(
    config: Config,
    state: Dict[str, Any],
    paper_verifier: ProviderAdapter,
    phase: str,
    worker_terminal_output: str,
    worker_handoff: Dict[str, Any],
    worker_frontier_update: Dict[str, Any],
    *,
    cycle: int,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    burst_tag = f"theorem-frontier-paper-{cycle:04d}"
    worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
    prompt = build_theorem_frontier_paper_verifier_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        worker_frontier_update,
        paper_verifier.needs_initial_run(),
    )
    prompt_for_chat = build_theorem_frontier_paper_verifier_prompt(
        config,
        state,
        phase,
        "[omitted from the web transcript; raw terminal output is only kept in local logs]",
        worker_handoff_text,
        worker_frontier_update,
        paper_verifier.needs_initial_run(),
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="theorem_frontier_paper_verifier_prompt",
        actor="supervisor",
        target="paper_verifier",
        content=prompt_for_chat,
        content_type="text",
        summary=f"Supervisor -> theorem-frontier paper-verifier prompt for cycle {cycle}",
    )
    def _validate_paper_verifier(run: Dict[str, Any]) -> Dict[str, Any]:
        output = run["captured_output"].strip()
        rev = load_json_artifact_with_fallback(
            Path(run["artifact_path"]),
            output,
            ("phase", "decision"),
            fallback_paths=legacy_supervisor_artifact_paths(config, Path(run["artifact_path"])),
        )
        return validate_theorem_frontier_paper_verifier_review(phase, rev)

    run, review = run_burst_with_validation(
        paper_verifier,
        cycle,
        prompt,
        config=config,
        state=state,
        phase=phase,
        stage_label="paper-verifier burst",
        policy=policy,
        validate=_validate_paper_verifier,
    )
    paper_verifier.mark_initialized()
    review["cycle"] = cycle
    state["last_theorem_frontier_paper_review"] = review
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="theorem_frontier_paper_verifier_review",
        actor="paper_verifier",
        target="supervisor",
        content=review,
        content_type="json",
    )
    save_state(config, state)
    append_jsonl(config.state_dir / "theorem_frontier_paper_verifier_log.jsonl", review)
    return review


def config_to_raw_dict(config: Config, *, policy: Optional[Policy] = None) -> Dict[str, Any]:
    effective = effective_policy(config, policy=policy)
    workflow: Dict[str, Any] = {
        "start_phase": config.workflow.start_phase,
        "sorry_mode": config.workflow.sorry_mode,
        "approved_axioms_path": str(config.workflow.approved_axioms_path),
        "human_input_path": str(config.workflow.human_input_path),
        "input_request_path": str(config.workflow.input_request_path),
        "theorem_frontier_phase": config.workflow.theorem_frontier_phase,
    }
    if config.workflow.paper_tex_path is not None:
        workflow["paper_tex_path"] = str(config.workflow.paper_tex_path)
    return {
        "repo_path": str(config.repo_path),
        "goal_file": str(config.goal_file),
        "state_dir": str(config.state_dir),
        "worker": {
            "provider": config.worker.provider,
            "model": config.worker.model,
            "extra_args": list(config.worker.extra_args),
        },
        "reviewer": {
            "provider": config.reviewer.provider,
            "model": config.reviewer.model,
            "extra_args": list(config.reviewer.extra_args),
        },
        "tmux": {
            "session_name": config.tmux.session_name,
            "dashboard_window_name": config.tmux.dashboard_window_name,
            "kill_windows_after_capture": config.tmux.kill_windows_after_capture,
        },
        "workflow": workflow,
        "chat": {
            "root_dir": str(config.chat.root_dir),
            "repo_name": config.chat.repo_name,
            "project_name": config.chat.project_name,
            "public_base_url": config.chat.public_base_url,
        },
        "git": {
            "remote_url": config.git.remote_url,
            "remote_name": config.git.remote_name,
            "branch": config.git.branch,
            "author_name": config.git.author_name,
            "author_email": config.git.author_email,
        },
        "max_cycles": config.max_cycles,
        "sleep_seconds": effective.timing.sleep_seconds,
        "startup_timeout_seconds": config.startup_timeout_seconds,
        "burst_timeout_seconds": config.burst_timeout_seconds,
        "policy_path": str(resolved_policy_path(config)),
        "branching": {
            "max_current_branches": config.branching.max_current_branches,
            "evaluation_cycle_budget": effective.branching.evaluation_cycle_budget,
            "poll_seconds": effective.branching.poll_seconds,
        },
    }


def branch_episode_snapshots(episode: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    base_review_count = int(episode.get("base_review_count", 0))
    for branch in episode.get("branches", []):
        if not isinstance(branch, dict):
            continue
        branch_status = str(branch.get("status", "")).strip().lower() or "active"
        config_path = Path(str(branch.get("config_path", "")))
        worktree_path = Path(str(branch.get("worktree_path", "")))
        state_path = worktree_path / ".agent-supervisor" / "state.json"
        state_data = JsonFile.load(state_path, {})
        latest_review = state_data.get("last_review") if isinstance(state_data.get("last_review"), dict) else {}
        latest_handoff = (
            state_data.get("last_worker_handoff") if isinstance(state_data.get("last_worker_handoff"), dict) else {}
        )
        latest_validation = (
            state_data.get("last_validation") if isinstance(state_data.get("last_validation"), dict) else {}
        )
        proposal = pending_branch_proposal(state_data)
        recovery_attempt_limit = stuck_recovery_attempt_limit(state_data)
        recovery_attempt_count = len(stuck_recovery_attempts(state_data))
        frontier_summary = theorem_frontier_branch_summary(state_data)
        snapshots.append(
            {
                "name": branch.get("name"),
                "branch_status": branch_status,
                "summary": branch.get("summary"),
                "frontier_anchor_node_id": normalize_frontier_text(
                    branch.get("frontier_anchor_node_id") or episode.get("frontier_anchor_node_id")
                )
                or None,
                "rewrite_scope": branch.get("rewrite_scope"),
                "worker_prompt": branch.get("worker_prompt"),
                "why_this_might_eventually_succeed": branch.get("why_this_might_eventually_succeed"),
                "worktree_path": str(worktree_path),
                "config_path": str(config_path),
                "supervisor_session": branch.get("supervisor_session"),
                "agent_session": branch.get("agent_session"),
                "review_count": branch_review_count(state_data),
                "progress_reviews": branch_progress_count(state_data, base_review_count),
                "cycle": int(state_data.get("cycle", 0) or 0),
                "phase": state_data.get("phase"),
                "latest_review_decision": latest_review.get("decision"),
                "latest_review_reason": latest_review.get("reason"),
                "latest_worker_status": latest_handoff.get("status"),
                "latest_worker_frontier": latest_handoff.get("current_frontier"),
                "stuck_recovery_attempt_count": recovery_attempt_count,
                "stuck_recovery_attempt_limit": recovery_attempt_limit,
                "stuck_recovery_exhausted": branch_status != "dead" and stuck_recovery_exhausted(state_data),
                "pending_branch_proposal": proposal,
                "pending_branch_proposal_confidence": (
                    proposal.get("confidence") if isinstance(proposal, dict) else None
                ),
                "pending_branch_proposal_strategy_count": (
                    len(proposal.get("strategies", [])) if isinstance(proposal, dict) and isinstance(proposal.get("strategies"), list) else 0
                ),
                "git_head": ((latest_validation.get("git") or {}).get("head") if isinstance(latest_validation, dict) else None),
                "theorem_frontier_active_leaf_id": frontier_summary.get("active_leaf_id"),
                "theorem_frontier_active_leaf_anchor": frontier_summary.get("active_leaf_anchor"),
                "theorem_frontier_blocker_cluster": frontier_summary.get("blocker_cluster"),
                "theorem_frontier_current_action": frontier_summary.get("current_action"),
                "theorem_frontier_assessed_action": frontier_summary.get("assessed_action"),
                "theorem_frontier_open_hypotheses_count": frontier_summary.get("open_hypotheses_count"),
                "theorem_frontier_open_hypotheses": frontier_summary.get("open_hypotheses"),
                "theorem_frontier_active_leaf_age": frontier_summary.get("active_leaf_age"),
                "theorem_frontier_blocker_cluster_age": frontier_summary.get("blocker_cluster_age"),
                "theorem_frontier_failed_close_attempts": frontier_summary.get("failed_close_attempts"),
                "theorem_frontier_cone_purity": frontier_summary.get("cone_purity"),
                "theorem_frontier_escalation_required": frontier_summary.get("escalation_required"),
                "theorem_frontier_escalation_reasons": frontier_summary.get("escalation_reasons"),
            }
        )
    return snapshots


def branch_episode_ready_for_selection(
    config: Config,
    episode: Dict[str, Any],
    snapshots: Sequence[Dict[str, Any]],
    policy: Optional[Policy] = None,
) -> bool:
    active_snapshots = [snapshot for snapshot in snapshots if str(snapshot.get("branch_status", "active")).lower() != "dead"]
    if not active_snapshots:
        return False
    if any(snapshot.get("latest_review_decision") == "DONE" for snapshot in active_snapshots):
        return True
    target = int(
        episode.get(
            "next_selection_review_target",
            int(episode.get("base_review_count", 0)) + branch_review_budget(config, policy),
        )
    )
    return all(int(snapshot.get("review_count", 0) or 0) >= target for snapshot in active_snapshots)


def branch_strategy_branch_name(config: Config, episode_id: str, label: str) -> str:
    return f"lagent/{sanitize_repo_name(config.chat.repo_name)}/{episode_id}/{sanitize_branch_label(label)}"


def branch_strategy_worktree_path(config: Config, episode_id: str, label: str) -> Path:
    return config.repo_path.parent / f"{config.repo_path.name}--{episode_id}--{sanitize_branch_label(label)}"


def child_branch_config_payload(
    config: Config,
    *,
    episode_id: str,
    strategy: Dict[str, Any],
    worktree_path: Path,
    config_path: Path,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    child_repo_name = sanitize_repo_name(f"{config.chat.repo_name}-{episode_id}-{strategy['name']}")
    agent_session = sanitize_tmux_session_name(f"{child_repo_name}-agents")
    payload = config_to_raw_dict(config, policy=policy)
    payload["repo_path"] = str(worktree_path)
    payload["goal_file"] = str(worktree_path / config.goal_file.name)
    payload["state_dir"] = str(worktree_path / ".agent-supervisor")
    payload["tmux"]["session_name"] = agent_session
    payload["workflow"]["approved_axioms_path"] = str(worktree_path / config.workflow.approved_axioms_path.name)
    payload["workflow"]["human_input_path"] = str(worktree_path / config.workflow.human_input_path.name)
    payload["workflow"]["input_request_path"] = str(worktree_path / config.workflow.input_request_path.name)
    if config.workflow.paper_tex_path is not None:
        payload["workflow"]["paper_tex_path"] = str(worktree_path / config.workflow.paper_tex_path.relative_to(config.repo_path))
    payload["chat"]["repo_name"] = child_repo_name
    payload["chat"]["project_name"] = config.chat.project_name
    payload["git"]["branch"] = branch_strategy_branch_name(config, episode_id, strategy["name"])
    payload["branching"]["max_current_branches"] = 1
    return payload


def start_supervisor_tmux_session(config_path: Path, supervisor_session: str) -> None:
    tmux_cmd(
        "new-session",
        "-d",
        "-s",
        supervisor_session,
        "-n",
        "supervisor",
        "bash",
        "-lc",
        (
            f"cd {shlex.quote(str(PACKAGE_DIR))} && "
            f"python3 supervisor.py --config {shlex.quote(str(config_path))}; "
            "echo; echo '[supervisor exited]'; exec bash"
        ),
    )


def restart_supervisor_tmux_session(config_path: Path, supervisor_session: str) -> None:
    tmux_cmd("kill-session", "-t", supervisor_session, check=False)
    start_supervisor_tmux_session(config_path, supervisor_session)


def build_child_branch_state(
    state: Dict[str, Any],
    *,
    episode_id: str,
    strategy: Dict[str, Any],
    parent_max_current_branches: int,
) -> Dict[str, Any]:
    child_state = deep_copy_jsonish(state)
    child_state["roles"] = {}
    child_state["active_branch_episode"] = None
    child_state["last_branch_consideration_cycle"] = 0
    child_state["branch_parent_max_current_branches"] = max(1, int(parent_max_current_branches))
    child_state["pending_branch_proposal"] = None
    child_state["next_branch_proposal_review_count"] = 0
    child_state["branch_lineage"] = [
        *branch_lineage_entries(state),
        {
            "episode_id": episode_id,
            "branch_name": strategy["name"],
            "summary": strategy["summary"],
            "rewrite_scope": strategy["rewrite_scope"],
        },
    ]
    child_state["branch_context"] = {
        "episode_id": episode_id,
        "branch_name": strategy["name"],
        "summary": strategy["summary"],
        "worker_prompt": strategy["worker_prompt"],
        "why_this_might_eventually_succeed": strategy["why_this_might_eventually_succeed"],
        "rewrite_scope": strategy["rewrite_scope"],
        "frontier_anchor_node_id": normalize_frontier_text(strategy.get("frontier_anchor_node_id")) or None,
    }
    reset_child_branch_theorem_frontier_runtime_state(child_state)
    return child_state


def create_branch_episode(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    decision: Dict[str, Any],
    branch_strategy: Dict[str, Any],
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    preflight_error = branch_episode_preflight_error(config)
    if preflight_error:
        raise SupervisorError(f"Cannot create branch episode: {preflight_error}.")
    status = git_validation_summary(config) if git_is_enabled(config) else {"head": git_output(config, ["rev-parse", "HEAD"]).strip()}

    state["branch_episode_counter"] = int(state.get("branch_episode_counter", 0) or 0) + 1
    episode_id = f"episode-{state['branch_episode_counter']:03d}"
    episode_dir = branch_episode_dir(config, episode_id)
    episode_dir.mkdir(parents=True, exist_ok=True)
    base_review_count = branch_review_count(state)
    parent_head = status.get("head")
    frontier_summary = theorem_frontier_branch_summary(state)
    frontier_anchor_node_id = (
        normalize_frontier_text(branch_strategy.get("frontier_anchor_node_id"))
        or normalize_frontier_text(frontier_summary.get("active_leaf_id"))
        or None
    )
    branches: List[Dict[str, Any]] = []
    for strategy in branch_strategy["strategies"]:
        label = sanitize_branch_label(strategy["name"])
        worktree_path = branch_strategy_worktree_path(config, episode_id, label)
        local_branch = branch_strategy_branch_name(config, episode_id, label)
        if worktree_path.exists():
            raise SupervisorError(f"Refusing to create branch worktree at existing path: {worktree_path}")
        git_run(config, ["worktree", "add", "-b", local_branch, str(worktree_path), "HEAD"])
        child_config_path = episode_dir / f"{label}.json"
        payload = child_branch_config_payload(
            config,
            episode_id=episode_id,
            strategy={**strategy, "name": label},
            worktree_path=worktree_path,
            config_path=child_config_path,
            policy=policy,
        )
        JsonFile.dump(child_config_path, payload)
        child_state = build_child_branch_state(
            state,
            episode_id=episode_id,
            strategy={**strategy, "name": label, "frontier_anchor_node_id": frontier_anchor_node_id},
            parent_max_current_branches=config.branching.max_current_branches,
        )
        child_state_dir = worktree_path / ".agent-supervisor"
        JsonFile.dump(child_state_dir / "state.json", child_state)
        write_theorem_frontier_state_file_if_present(child_state_dir, child_state)
        supervisor_session = sanitize_tmux_session_name(f"{payload['chat']['repo_name']}-supervisor")
        start_supervisor_tmux_session(child_config_path, supervisor_session)
        branches.append(
            {
                "name": label,
                "chat_repo_name": payload["chat"]["repo_name"],
                "summary": strategy["summary"],
                "worker_prompt": strategy["worker_prompt"],
                "why_this_might_eventually_succeed": strategy["why_this_might_eventually_succeed"],
                "rewrite_scope": strategy["rewrite_scope"],
                "frontier_anchor_node_id": frontier_anchor_node_id,
                "status": "active",
                "worktree_path": str(worktree_path),
                "config_path": str(child_config_path),
                "local_branch": local_branch,
                "supervisor_session": supervisor_session,
                "agent_session": payload["tmux"]["session_name"],
            }
        )

    episode = {
        "id": episode_id,
        "phase": phase,
        "trigger_cycle": int(decision.get("cycle", state.get("cycle", 0)) or 0),
        "lineage": branch_lineage_entries(state),
        "base_review_count": base_review_count,
        "next_selection_review_target": base_review_count + branch_review_budget(config, policy),
        "evaluation_cycle_budget": branch_review_budget(config, policy),
        "selection_continue_count": 0,
        "selection_question": branch_selection_question_for_state(state),
        "frontier_anchor_node_id": frontier_anchor_node_id,
        "frontier_anchor_lean_anchor": frontier_summary.get("active_leaf_anchor"),
        "frontier_anchor_lean_statement": frontier_summary.get("active_leaf_lean_statement"),
        "frontier_anchor_blocker_cluster": frontier_summary.get("blocker_cluster"),
        "reason": branch_strategy.get("reason", ""),
        "confidence": branch_strategy.get("confidence", 0.0),
        "parent_head": parent_head,
        "branches": branches,
        "status": "active",
    }
    state["active_branch_episode"] = episode
    state["last_branch_consideration_cycle"] = episode["trigger_cycle"]
    save_state(config, state)
    append_jsonl(episode_dir / "branch_strategy_log.jsonl", branch_strategy)
    return episode


def run_branch_strategy_review(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    last_review: Dict[str, Any],
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    validation_summary = state.get("last_validation") or {}
    worker_terminal_output = str(state.get("last_worker_output") or "").strip()
    worker_handoff = state.get("last_worker_handoff") or {}
    worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
    cycle = int(last_review.get("cycle", state.get("cycle", 0)) or 0)
    prompt = build_branch_strategy_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        validation_summary,
        last_review,
        reviewer.needs_initial_run(),
        policy=policy,
    )
    prompt_for_chat = build_branch_strategy_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        validation_summary,
        last_review,
        reviewer.needs_initial_run(),
        include_terminal_output=False,
        policy=policy,
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_strategy_prompt",
        actor="supervisor",
        target="reviewer",
        content=prompt_for_chat,
        content_type="text",
        summary=f"Supervisor -> branch-strategy prompt for cycle {cycle}",
    )
    run = launch_tmux_burst_with_retries(
        reviewer,
        cycle,
        prompt,
        state=state,
        phase=phase,
        stage_label="reviewer branch-strategy burst",
        artifact_name=branch_strategy_artifact_path(config).name,
        burst_tag=f"branch-strategy-{cycle:04d}",
        policy=policy,
    )
    reviewer.mark_initialized()
    strategy = load_json_artifact_with_fallback(
        Path(run["artifact_path"]),
        run["captured_output"].strip(),
        ("phase", "branch_decision", "confidence", "reason", "strategies"),
        fallback_paths=legacy_supervisor_artifact_paths(config, Path(run["artifact_path"])),
    )
    strategy = validate_branch_strategy_decision(config, phase, strategy, state)
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_strategy_decision",
        actor="reviewer",
        target="supervisor",
        content=strategy,
        content_type="json",
    )
    append_jsonl(config.state_dir / "branch_strategy_log.jsonl", strategy)
    save_state(config, state)
    return strategy


def run_branch_selection_review(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    episode: Dict[str, Any],
    snapshots: List[Dict[str, Any]],
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    prompt = build_branch_selection_prompt(
        config,
        state,
        phase,
        episode,
        snapshots,
        reviewer.needs_initial_run(),
        policy=policy,
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_selection_prompt",
        actor="supervisor",
        target="reviewer",
        content=prompt,
        content_type="text",
        summary=f"Supervisor -> branch-selection prompt for cycle {cycle}",
    )
    run = launch_tmux_burst_with_retries(
        reviewer,
        cycle,
        prompt,
        state=state,
        phase=phase,
        stage_label="reviewer branch-selection burst",
        artifact_name=branch_selection_artifact_path(config).name,
        burst_tag=f"branch-selection-{cycle:04d}",
        policy=policy,
    )
    reviewer.mark_initialized()
    allowed = [str(snapshot.get("name", "")) for snapshot in snapshots]
    selection = load_json_artifact_with_fallback(
        Path(run["artifact_path"]),
        run["captured_output"].strip(),
        ("phase", "selection_decision", "confidence", "reason", "selected_branch"),
        fallback_paths=legacy_supervisor_artifact_paths(config, Path(run["artifact_path"])),
    )
    selection = validate_branch_selection_decision(config, phase, selection, allowed, state)
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_selection_decision",
        actor="reviewer",
        target="supervisor",
        content=selection,
        content_type="json",
    )
    append_jsonl(config.state_dir / "branch_selection_log.jsonl", selection)
    save_state(config, state)
    return selection


def run_branch_replacement_review(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    episode: Dict[str, Any],
    snapshots: List[Dict[str, Any]],
    proposal_snapshot: Dict[str, Any],
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    prompt = build_branch_replacement_prompt(
        config,
        state,
        phase,
        episode,
        snapshots,
        proposal_snapshot,
        reviewer.needs_initial_run(),
        policy=policy,
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_replacement_prompt",
        actor="supervisor",
        target="reviewer",
        content=prompt,
        content_type="text",
        summary=f"Supervisor -> branch-frontier prompt for cycle {cycle}",
    )
    run = launch_tmux_burst_with_retries(
        reviewer,
        cycle,
        prompt,
        state=state,
        phase=phase,
        stage_label="reviewer branch-frontier burst",
        artifact_name=branch_replacement_artifact_path(config).name,
        burst_tag=f"branch-replacement-{cycle:04d}",
        policy=policy,
    )
    reviewer.mark_initialized()
    decision = load_json_artifact_with_fallback(
        Path(run["artifact_path"]),
        run["captured_output"].strip(),
        ("phase", "replacement_decision", "confidence", "reason"),
        fallback_paths=legacy_supervisor_artifact_paths(config, Path(run["artifact_path"])),
    )
    decision = validate_branch_replacement_decision(
        phase,
        decision,
        threshold=branch_replacement_min_confidence(config, policy),
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_replacement_decision",
        actor="reviewer",
        target="supervisor",
        content=decision,
        content_type="json",
    )
    append_jsonl(config.state_dir / "branch_replacement_log.jsonl", decision)
    save_state(config, state)
    return decision


def mark_branch_dead_in_episode(
    config: Config,
    state: Dict[str, Any],
    episode: Dict[str, Any],
    branch_name: str,
    *,
    reason: str,
    cycle: int,
) -> bool:
    updated = False
    for branch in episode.get("branches", []):
        if not isinstance(branch, dict) or str(branch.get("name", "")) != branch_name:
            continue
        if str(branch.get("status", "")).strip().lower() == "dead":
            return False
        branch["status"] = "dead"
        branch["pruned_reason"] = reason
        branch["pruned_cycle"] = cycle
        tmux_cmd("kill-session", "-t", str(branch.get("supervisor_session")), check=False)
        tmux_cmd("kill-session", "-t", str(branch.get("agent_session")), check=False)
        updated = True
        break
    if updated:
        state["active_branch_episode"] = episode
        save_state(config, state)
    return updated


def active_branch_snapshots(snapshots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [snapshot for snapshot in snapshots if str(snapshot.get("branch_status", "active")).strip().lower() != "dead"]


def exhausted_branch_snapshots(snapshots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        snapshot
        for snapshot in active_branch_snapshots(snapshots)
        if bool(snapshot.get("stuck_recovery_exhausted"))
    ]


def pending_branch_proposal_snapshots(snapshots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates = [
        snapshot
        for snapshot in active_branch_snapshots(snapshots)
        if isinstance(snapshot.get("pending_branch_proposal"), dict)
    ]
    candidates.sort(
        key=lambda snapshot: (
            float(snapshot.get("pending_branch_proposal_confidence") or 0.0),
            int(snapshot.get("review_count", 0) or 0),
            int(snapshot.get("cycle", 0) or 0),
        ),
        reverse=True,
    )
    return candidates


def proposal_snapshot_anchor_matches_episode(episode: Dict[str, Any], proposal_snapshot: Dict[str, Any]) -> bool:
    episode_anchor = normalize_frontier_text(episode.get("frontier_anchor_node_id"))
    if not episode_anchor:
        return True
    proposal = proposal_snapshot.get("pending_branch_proposal")
    if not isinstance(proposal, dict):
        return False
    proposal_anchor = normalize_frontier_text(proposal.get("frontier_anchor_node_id"))
    return proposal_anchor == episode_anchor


def record_automatic_branch_selection(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    episode: Dict[str, Any],
    *,
    selected_branch: str,
    reason: str,
) -> Dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    frontier_anchor_node_id = normalize_frontier_text(episode.get("frontier_anchor_node_id")) or None
    selection = {
        "phase": phase,
        "selection_decision": "SELECT_BRANCH",
        "frontier_anchor_node_id": frontier_anchor_node_id,
        "confidence": 1.0,
        "reason": reason,
        "selected_branch": selected_branch,
        "automatic": True,
    }
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_selection_decision",
        actor="supervisor",
        target="workflow",
        content=selection,
        content_type="json",
    )
    append_jsonl(config.state_dir / "branch_selection_log.jsonl", selection)
    append_jsonl(branch_episode_dir(config, str(episode.get("id", ""))) / "branch_selection_log.jsonl", selection)
    save_state(config, state)
    return selection


def record_branch_selection_decision(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    episode: Dict[str, Any],
    selection: Dict[str, Any],
) -> Dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    if "frontier_anchor_node_id" not in selection:
        selection = {
            **selection,
            "frontier_anchor_node_id": normalize_frontier_text(episode.get("frontier_anchor_node_id")) or None,
        }
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_selection_decision",
        actor="reviewer",
        target="supervisor",
        content=selection,
        content_type="json",
    )
    append_jsonl(config.state_dir / "branch_selection_log.jsonl", selection)
    append_jsonl(branch_episode_dir(config, str(episode.get("id", ""))) / "branch_selection_log.jsonl", selection)
    save_state(config, state)
    return selection


def clear_pending_branch_proposal_in_snapshot(snapshot: Dict[str, Any], *, cooldown_reviews: int = 0) -> None:
    config_path = Path(str(snapshot.get("config_path", "")))
    if not config_path.exists():
        return
    branch_config = load_config(config_path)
    branch_state = load_state(branch_config)
    clear_pending_branch_proposal(branch_state)
    next_review = int(snapshot.get("review_count", 0) or 0) + max(0, cooldown_reviews)
    branch_state["next_branch_proposal_review_count"] = max(next_branch_proposal_review_count(branch_state), next_review)
    save_state(branch_config, branch_state)


def restart_branch_supervisor_from_snapshot(snapshot: Dict[str, Any]) -> None:
    config_path = Path(str(snapshot.get("config_path", "")))
    supervisor_session = str(snapshot.get("supervisor_session", "")).strip()
    if not config_path.exists() or not supervisor_session:
        return
    restart_supervisor_tmux_session(config_path, supervisor_session)


def proposal_snapshot_can_replace_frontier(
    config: Config,
    episode: Dict[str, Any],
    snapshots: Sequence[Dict[str, Any]],
    proposal_snapshot: Dict[str, Any],
    *,
    policy: Optional[Policy] = None,
) -> bool:
    active_count = len(active_branch_snapshots(snapshots))
    if active_count < config.branching.max_current_branches:
        return False
    proposal = proposal_snapshot.get("pending_branch_proposal")
    if not isinstance(proposal, dict):
        return False
    try:
        proposal_confidence = float(proposal_snapshot.get("pending_branch_proposal_confidence") or 0.0)
    except (TypeError, ValueError):
        proposal_confidence = 0.0
    if proposal_confidence < branch_replacement_min_confidence(config, policy):
        return False
    strategies = proposal.get("strategies")
    if not isinstance(strategies, list):
        return False
    if not proposal_snapshot_anchor_matches_episode(episode, proposal_snapshot):
        return False
    return len(strategies) == config.branching.max_current_branches


def launch_nested_branch_episode_from_snapshot(
    episode: Dict[str, Any],
    proposal_snapshot: Dict[str, Any],
    *,
    phase: str,
    proposal: Dict[str, Any],
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    if not proposal_snapshot_anchor_matches_episode(episode, proposal_snapshot):
        raise SupervisorError(
            "Cannot launch nested branch episode: replacement proposal drifted away from the parent episode's theorem-frontier anchor."
        )
    config_path = Path(str(proposal_snapshot.get("config_path", "")))
    if not config_path.exists():
        raise SupervisorError(f"Cannot load proposed winner config for nested branching: {config_path}")
    branch_config = load_config(config_path)
    branch_state = load_state(branch_config)
    clear_pending_branch_proposal(branch_state)
    branch_state["next_branch_proposal_review_count"] = 0
    save_state(branch_config, branch_state)
    decision = branch_state.get("last_review")
    if not isinstance(decision, dict):
        raise SupervisorError("Cannot launch nested branch episode: winning branch is missing last_review state.")
    episode = create_branch_episode(branch_config, branch_state, phase, decision, proposal, policy=policy)
    supervisor_session = str(proposal_snapshot.get("supervisor_session", "")).strip()
    if supervisor_session:
        restart_supervisor_tmux_session(config_path, supervisor_session)
    return episode


def prune_branch_episode(
    config: Config,
    state: Dict[str, Any],
    episode: Dict[str, Any],
    selected_branch: str,
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    winner: Optional[Dict[str, Any]] = None
    completed_episode = deep_copy_jsonish(episode)
    completed_episode["status"] = "selected"
    completed_episode["selected_branch"] = selected_branch
    inherited_history = [entry for entry in state.get("branch_history", []) if isinstance(entry, dict)]
    for branch in episode.get("branches", []):
        if not isinstance(branch, dict):
            continue
        branch_state_path = Path(str(branch.get("worktree_path", ""))) / ".agent-supervisor" / "state.json"
        if branch_state_path.exists():
            branch_state = JsonFile.load(branch_state_path, {})
            branch_state["branch_history"] = [*deep_copy_jsonish(inherited_history), deep_copy_jsonish(completed_episode)]
            branch_state["active_branch_episode"] = None
            JsonFile.dump(branch_state_path, branch_state)
        if branch.get("name") == selected_branch:
            winner = branch
            continue
        tmux_cmd("kill-session", "-t", str(branch.get("supervisor_session")), check=False)
        tmux_cmd("kill-session", "-t", str(branch.get("agent_session")), check=False)
    if winner is None:
        raise SupervisorError(f"Could not find selected branch {selected_branch!r} in active episode.")

    winner_config_path = Path(str(winner.get("config_path", "")))
    winner_config = JsonFile.load(winner_config_path, {})
    winner_branching = winner_config.get("branching", {})
    winner_branching["max_current_branches"] = config.branching.max_current_branches
    winner_branching["evaluation_cycle_budget"] = branch_review_budget(config, policy)
    winner_branching["poll_seconds"] = branch_poll_seconds(config, policy)
    winner_config["branching"] = winner_branching
    winner_config["policy_path"] = str(resolved_policy_path(config))
    JsonFile.dump(winner_config_path, winner_config)

    state["branch_history"].append(deep_copy_jsonish(completed_episode))
    state["active_branch_episode"] = None
    save_state(config, state)
    return winner


def branch_episode_status_lines(
    config: Config,
    episode: Dict[str, Any],
    snapshots: Sequence[Dict[str, Any]],
    policy: Optional[Policy] = None,
) -> List[str]:
    target = int(
        episode.get(
            "next_selection_review_target",
            int(episode.get("base_review_count", 0)) + branch_review_budget(config, policy),
        )
    )
    lines = [
        f"Branch episode {episode.get('id', '')}: trigger_cycle={episode.get('trigger_cycle', '?')} "
        f"branches={len(snapshots)} next_selection_review_target={target}",
        f"Selection question: {str(episode.get('selection_question', '')).strip()}",
    ]
    for snapshot in snapshots:
        head = str(snapshot.get("git_head") or "")[:12]
        branch_status = str(snapshot.get("branch_status") or "active")
        stuck_bits = (
            f"stuck_recovery={int(snapshot.get('stuck_recovery_attempt_count', 0) or 0)}/"
            f"{int(snapshot.get('stuck_recovery_attempt_limit', 0) or 0)}"
        )
        if snapshot.get("stuck_recovery_exhausted"):
            stuck_bits += " exhausted"
        proposal_bits = ""
        if snapshot.get("pending_branch_proposal"):
            proposal_bits = (
                f" pending_proposal={int(snapshot.get('pending_branch_proposal_strategy_count', 0) or 0)}-way"
            )
        frontier_bits = ""
        if snapshot.get("theorem_frontier_active_leaf_id"):
            frontier_bits = (
                f" frontier={snapshot.get('theorem_frontier_active_leaf_id')}"
                f" blocker={snapshot.get('theorem_frontier_blocker_cluster') or 'none'}"
                f" open_hyps={int(snapshot.get('theorem_frontier_open_hypotheses_count', 0) or 0)}"
            )
            if snapshot.get("theorem_frontier_escalation_required"):
                frontier_bits += " escalation=yes"
        lines.append(
            "- "
            f"{snapshot.get('name', '')}: "
            f"branch_status={branch_status} "
            f"phase={snapshot.get('phase') or '?'} "
            f"cycle={int(snapshot.get('cycle', 0) or 0)} "
            f"reviews={int(snapshot.get('review_count', 0) or 0)}/{target} "
            f"progress_reviews={int(snapshot.get('progress_reviews', 0) or 0)} "
            f"latest_review={snapshot.get('latest_review_decision') or 'none'} "
            f"worker_status={snapshot.get('latest_worker_status') or 'none'} "
            f"{stuck_bits} "
            f"{proposal_bits} "
            f"{frontier_bits} "
            f"head={head or 'unknown'}"
        )
    return lines


def monitor_active_branch_episode(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    policy_manager: Optional[PolicyManager] = None,
) -> int:
    if policy_manager is None:
        policy_manager = PolicyManager(config)
    while True:
        policy = policy_manager.reload(state=state, persist=True)
        episode = active_branch_episode(state)
        if episode is None:
            return 0
        normalize_branch_episode_selection_schedule(config, state, episode, policy)

        snapshots = branch_episode_snapshots(episode)
        print(f"\n===== branch episode {episode.get('id', '')}: monitoring =====")
        for line in branch_episode_status_lines(config, episode, snapshots, policy):
            print(line)

        exhausted = exhausted_branch_snapshots(snapshots)
        if exhausted:
            exhausted_names = [str(snapshot.get("name", "")).strip() for snapshot in exhausted if str(snapshot.get("name", "")).strip()]
            print(
                "Auto-pruning branch(es) after exhausted stuck recovery: "
                + ", ".join(exhausted_names)
            )
            for snapshot in exhausted:
                branch_name = str(snapshot.get("name", "")).strip()
                if not branch_name:
                    continue
                cycle = int(snapshot.get("cycle", 0) or 0)
                reason = (
                    f"Pruned automatically after exhausting "
                    f"{int(snapshot.get('stuck_recovery_attempt_limit', 0) or 0)} stuck-recovery attempts."
                )
                mark_branch_dead_in_episode(
                    config,
                    state,
                    episode,
                    branch_name,
                    reason=reason,
                    cycle=cycle,
                )
            snapshots = branch_episode_snapshots(episode)
            survivors = active_branch_snapshots(snapshots)
            if not survivors:
                print("Stopping because every branch in the active episode exhausted stuck recovery and was pruned.")
                state.setdefault("branch_history", []).append(
                    {
                        **deep_copy_jsonish(episode),
                        "status": "exhausted",
                    }
                )
                state["active_branch_episode"] = None
                save_state(config, state)
                return 0
            if len(survivors) == 1:
                survivor_name = str(survivors[0].get("name", "")).strip()
                selection = record_automatic_branch_selection(
                    config,
                    state,
                    phase,
                    episode,
                    selected_branch=survivor_name,
                    reason=(
                        "Selected automatically because all other active branches were pruned after exhausting "
                        "their branch-local stuck-recovery budget."
                    ),
                )
                print("\n===== branch selection decision =====")
                print(json.dumps(selection, indent=2, ensure_ascii=False))
                winner = prune_branch_episode(config, state, episode, survivor_name, policy=policy)
                print(
                    f"Automatically selected surviving branch {winner['name']} "
                    f"({winner['worktree_path']})."
                )
                return 0
            print(
                f"{len(survivors)} active branches remain after automatic pruning; "
                "continuing branch monitoring."
            )
            continue

        proposals = pending_branch_proposal_snapshots(snapshots)
        if proposals:
            proposal_snapshot = proposals[0]
            proposal = proposal_snapshot.get("pending_branch_proposal")
            proposal_name = str(proposal_snapshot.get("name", "")).strip()
            if not proposal_snapshot_anchor_matches_episode(episode, proposal_snapshot):
                print(
                    f"Rejecting pending branch-replacement proposal from {proposal_name or 'unknown'}: "
                    "the proposal no longer targets the active parent episode's theorem-frontier anchor."
                )
                clear_pending_branch_proposal_in_snapshot(
                    proposal_snapshot,
                    cooldown_reviews=branch_proposal_cooldown_reviews(config, policy),
                )
                restart_branch_supervisor_from_snapshot(proposal_snapshot)
                continue

            if not proposal_snapshot_can_replace_frontier(config, episode, snapshots, proposal_snapshot, policy=policy):
                print(
                    f"Rejecting pending branch-replacement proposal from {proposal_name or 'unknown'}: "
                    "this v1 policy only supports full frontier replacement when the proposal exactly fills the branch cap."
                )
                clear_pending_branch_proposal_in_snapshot(
                    proposal_snapshot,
                    cooldown_reviews=branch_proposal_cooldown_reviews(config, policy),
                )
                restart_branch_supervisor_from_snapshot(proposal_snapshot)
                continue

            replacement = run_branch_replacement_review(
                config,
                state,
                reviewer,
                phase,
                episode,
                active_branch_snapshots(snapshots),
                proposal_snapshot,
                policy=policy,
            )
            append_jsonl(
                branch_episode_dir(config, str(episode.get("id", ""))) / "branch_replacement_log.jsonl",
                replacement,
            )
            print("\n===== branch frontier decision =====")
            print(json.dumps(replacement, indent=2, ensure_ascii=False))

            if replacement["replacement_decision"] != "REPLACE_WITH_PROPOSAL":
                clear_pending_branch_proposal_in_snapshot(
                    proposal_snapshot,
                    cooldown_reviews=branch_proposal_cooldown_reviews(config, policy),
                )
                restart_branch_supervisor_from_snapshot(proposal_snapshot)
                print(
                    f"Kept the current frontier. The proposal from {proposal_name or 'unknown'} is on cooldown for "
                    f"{branch_proposal_cooldown_reviews(config, policy)} review(s)."
                )
                continue

            if not isinstance(proposal, dict):
                raise SupervisorError("Accepted branch replacement is missing the stored proposal payload.")
            selection = record_branch_selection_decision(
                config,
                state,
                phase,
                episode,
                {
                    "phase": phase,
                    "selection_decision": "SELECT_BRANCH",
                    "confidence": replacement["confidence"],
                    "reason": replacement["reason"],
                    "selected_branch": proposal_name,
                    "replacement": True,
                },
            )
            print("\n===== branch selection decision =====")
            print(json.dumps(selection, indent=2, ensure_ascii=False))
            winner = prune_branch_episode(config, state, episode, proposal_name, policy=policy)
            nested_episode = launch_nested_branch_episode_from_snapshot(
                episode,
                {**proposal_snapshot, **winner},
                phase=phase,
                proposal=proposal,
                policy=policy,
            )
            print(
                f"Replaced the capped frontier by selecting {proposal_name} and opening nested branch episode "
                f"{nested_episode['id']} with {len(nested_episode.get('branches', []))} branch(es)."
            )
            return 0

        active_snapshots = active_branch_snapshots(snapshots)
        if not branch_episode_ready_for_selection(config, episode, active_snapshots, policy):
            print(
                f"Waiting {branch_poll_seconds(config, policy):.0f}s before polling branch progress again."
            )
            time.sleep(branch_poll_seconds(config, policy))
            continue

        selection = run_branch_selection_review(
            config,
            state,
            reviewer,
            phase,
            episode,
            active_snapshots,
            policy=policy,
        )
        append_jsonl(branch_episode_dir(config, str(episode.get("id", ""))) / "branch_selection_log.jsonl", selection)
        print("\n===== branch selection decision =====")
        print(json.dumps(selection, indent=2, ensure_ascii=False))

        if selection["selection_decision"] == "CONTINUE_BRANCHING":
            continue_count = branch_selection_continue_count(config, episode, policy)
            episode["selection_continue_count"] = continue_count + 1
            episode["evaluation_cycle_budget"] = branch_review_budget(config, policy)
            episode["next_selection_review_target"] = branch_selection_target_for_continue_count(
                config,
                episode,
                continue_count + 1,
                policy,
            )
            state["active_branch_episode"] = episode
            save_state(config, state)
            print(
                "Reviewer chose to continue branching. "
                f"Next branch-selection checkpoint is review_count >= {episode['next_selection_review_target']} "
                "for every active branch."
            )
            time.sleep(branch_poll_seconds(config, policy))
            continue

        winner = prune_branch_episode(
            config,
            state,
            episode,
            str(selection.get("selected_branch", "")),
            policy=policy,
        )
        print(
            "Selected winning branch "
            f"{winner.get('name')} at {winner.get('worktree_path')}."
        )
        print(
            "The winning branch supervisor remains active in its own worktree/session. "
            f"Use config {winner.get('config_path')} and session {winner.get('supervisor_session')} to keep following it."
        )
        return 0


def enforce_terminal_decision(
    phase: str,
    decision_value: str,
    validation_summary: Dict[str, Any],
) -> None:
    if phase == "theorem_stating" and decision_value in {"ADVANCE_PHASE", "DONE"}:
        if not validation_summary["build"]["ok"]:
            raise SupervisorError("Cannot advance from theorem_stating while `lake build` is failing.")
        if not all(check["ok"] for check in validation_summary["syntax_checks"]):
            raise SupervisorError("Cannot advance from theorem_stating while statement files fail syntax checks.")
        if validation_summary["axioms"]["unapproved"]:
            raise SupervisorError("Cannot advance from theorem_stating with unapproved axioms present.")
        if validation_summary["sorry_policy"]["disallowed_entries"]:
            raise SupervisorError("Cannot advance from theorem_stating with disallowed sorrys present.")
    if phase == "proof_formalization" and decision_value in {"ADVANCE_PHASE", "DONE"}:
        if not validation_summary["build"]["ok"]:
            raise SupervisorError("Cannot complete proof_formalization while `lake build` is failing.")
        if validation_summary["sorries"]["count"] != 0:
            raise SupervisorError("Cannot complete proof_formalization while any `sorry` remains.")
        if validation_summary["axioms"]["unapproved"]:
            raise SupervisorError("Cannot complete proof_formalization with unapproved axioms present.")
    if is_style_cleanup_phase(phase) and decision_value == "DONE":
        if not validation_summary["build"]["ok"]:
            raise SupervisorError("Cannot finish cleanup while `lake build` is failing.")
        if validation_summary["sorries"]["count"] != 0:
            raise SupervisorError("Cannot finish cleanup while any `sorry` remains.")
        if validation_summary["axioms"]["unapproved"]:
            raise SupervisorError("Cannot finish cleanup with unapproved axioms present.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Lean formalization worker/reviewer supervisor")
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    config = load_config(Path(args.config).expanduser().resolve())
    check_dependencies(config)
    ensure_git_repository(config)
    installed_provider_context = install_personal_provider_context_files(
        Path.home(),
        [config.worker.provider, config.reviewer.provider],
    )
    state = load_state(config)
    policy_manager = PolicyManager(config)
    policy = policy_manager.reload(state=state, force=True, persist=True)
    phase = current_phase(config, state)
    if is_style_cleanup_phase(phase) and cleanup_last_good_commit(state) is None:
        update_cleanup_last_good_commit(config, state, state.get("last_validation"))
        save_state(config, state)
    ensure_repo_files(config, phase)
    ensure_chat_site(config)
    ensure_tmux_session(config)

    has_active_branch_episode = active_branch_episode(state) is not None
    if not has_active_branch_episode and not maybe_consume_human_input(config, state):
        print(f"Waiting for human input in: {config.workflow.human_input_path}")
        print(f"Input request written to: {config.workflow.input_request_path}")
        return 0

    if not has_active_branch_episode and state.get("pending_human_input_event"):
        record_chat_event(
            config,
            state,
            cycle=int(state.get("cycle", 0)),
            phase=phase,
            kind="human_input",
            actor="human",
            target="supervisor",
            content=str(state.pop("pending_human_input_event")),
            content_type="text",
        )
        save_state(config, state)

    if not has_active_branch_episode and pending_branch_proposal(state) and not branching_enabled(config):
        print("Waiting for parent supervisor to evaluate the pending branch-replacement proposal.")
        return 0

    worker = make_adapter("worker", config, state)
    reviewer = make_adapter("reviewer", config, state)
    paper_verifier = make_adapter("paper_verifier", config, state)

    if phase == "proof_formalization" and not has_active_branch_episode and can_attempt_stuck_recovery(state, policy):
        suggestion = run_stuck_recovery_review(config, state, reviewer, phase, policy=policy)
        attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
        print(
            f"Prepared stuck-recovery attempt {suggestion['attempt']}/{attempt_limit} "
            f"from prior STUCK review."
        )
    elif phase == "proof_formalization" and not has_active_branch_episode and has_unhandled_stuck_review(state):
        attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
        print(
            "Stopping because the current stuck episode already exhausted all "
            f"{attempt_limit} stuck-recovery attempts."
        )
        return 0

    print(f"repo_path={config.repo_path}")
    print(f"goal_file={config.goal_file}")
    print(f"state_dir={config.state_dir}")
    print(f"worker={config.worker.provider} reviewer={config.reviewer.provider}")
    print(f"tmux_session={config.tmux.session_name}")
    print(f"phase={phase}")
    print(f"chat_root={chat_root_dir(config)}")
    print(f"chat_url={chat_repo_url(config)}")
    if installed_provider_context:
        print(f"provider_context_installed={len(installed_provider_context)}")
    if config.workflow.paper_tex_path is not None:
        print(f"paper_tex_path={config.workflow.paper_tex_path}")
    if git_is_enabled(config):
        print(f"git_remote={config.git.remote_name}:{config.git.remote_url}")
        print(f"git_branch={current_git_branch(config)}")
    print(f"sorry_mode={config.workflow.sorry_mode}")
    print(
        "branching="
        f"max_current_branches={config.branching.max_current_branches} "
        f"evaluation_cycle_budget={policy.branching.evaluation_cycle_budget} "
        f"poll_seconds={policy.branching.poll_seconds}"
    )
    print(
        "codex_budget_pause="
        f"weekly_percent_left_threshold={policy.codex_budget_pause.weekly_percent_left_threshold} "
        f"poll_seconds={policy.codex_budget_pause.poll_seconds}"
    )
    print(f"policy_path={resolved_policy_path(config)}")

    while True:
        policy = policy_manager.reload(state=state, persist=True)
        phase = current_phase(config, state)
        if is_style_cleanup_phase(phase) and cleanup_last_good_commit(state) is None:
            update_cleanup_last_good_commit(config, state, state.get("last_validation"))
            save_state(config, state)
        if active_branch_episode(state):
            return monitor_active_branch_episode(config, state, reviewer, phase, policy_manager)
        ensure_repo_files(config, phase)
        if recover_interrupted_worker_state(config, state, phase):
            print(f"Recovered completed worker burst for cycle {int(state.get('cycle', 0))}; resuming reviewer stage.")
        cycle, stage = determine_resume_cycle_and_stage(state)
        is_new_cycle = cycle > int(state.get("cycle", 0) or 0)
        if is_new_cycle:
            if config.max_cycles and cycle > config.max_cycles:
                print(f"Reached max_cycles={config.max_cycles}; stopping.")
                break
            state["cycle"] = cycle
            save_state(config, state)
        elif stage == "worker":
            print(f"Resuming interrupted worker burst for cycle {cycle}.")
        else:
            print(f"Resuming interrupted reviewer burst for cycle {cycle}.")

        if stage == "worker":
            policy = policy_manager.reload(state=state, persist=True)
            print(f"\n===== cycle {cycle}: worker | phase={phase} =====")
            cleanup_start_commit = cleanup_last_good_commit(state) if is_style_cleanup_phase(phase) else None
            worker_prompt = build_worker_prompt(
                config,
                state,
                phase,
                worker.needs_initial_run(),
                policy=policy,
            )
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="worker_prompt",
                actor="supervisor",
                target="worker",
                content=worker_prompt,
                content_type="text",
                summary=f"Supervisor -> worker prompt for cycle {cycle}",
            )
            def _validate_worker_burst(run: Dict[str, Any]) -> Dict[str, Any]:
                output = run["captured_output"].strip()
                handoff = load_json_artifact_with_fallback(
                    Path(run["artifact_path"]),
                    output,
                    ("phase", "status"),
                    fallback_paths=legacy_supervisor_artifact_paths(config, Path(run["artifact_path"])),
                )
                return validate_worker_handoff(phase, handoff)

            worker_run, worker_handoff = run_burst_with_validation(
                worker,
                cycle,
                worker_prompt,
                config=config,
                state=state,
                phase=phase,
                stage_label="worker burst",
                policy=policy,
                reuse_existing_window=not is_new_cycle,
                validate=_validate_worker_burst,
            )
            worker.mark_initialized()
            worker_terminal_output = worker_run["captured_output"].strip()
            state["last_worker_output"] = worker_terminal_output
            state["last_worker_handoff"] = worker_handoff
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="worker_handoff",
                actor="worker",
                target="supervisor",
                content=worker_handoff,
                content_type="json",
            )
            if theorem_frontier_enabled(config, phase):
                try:
                    frontier_required_keys: Tuple[str, ...] = ("phase", "requested_action")
                    frontier_update = load_json_artifact_with_fallback(
                        theorem_frontier_worker_update_path(config),
                        worker_terminal_output,
                        frontier_required_keys,
                        fallback_paths=legacy_supervisor_artifact_paths(config, theorem_frontier_worker_update_path(config)),
                    )
                    frontier_update = validate_theorem_frontier_worker_update_for_mode(config, phase, frontier_update)
                    state["last_theorem_frontier_worker_update"] = frontier_update
                    record_chat_event(
                        config,
                        state,
                        cycle=cycle,
                        phase=phase,
                        kind="theorem_frontier_update",
                        actor="worker",
                        target="supervisor",
                        content=frontier_update,
                        content_type="json",
                    )
                except SupervisorError as frontier_exc:
                    log_supervisor_warning(
                        config, cycle=cycle, phase=phase,
                        category="frontier_worker_artifact",
                        message=str(frontier_exc),
                        detail={"artifact_path": str(theorem_frontier_worker_update_path(config))},
                    )
                    state["last_theorem_frontier_worker_update"] = None

            previous_validation = state.get("last_validation") if isinstance(state.get("last_validation"), dict) else None
            validation_summary = run_validation(config, phase, cycle, previous_validation=previous_validation)
            validation_summary["theorem_frontier_cone_files"] = apply_theorem_frontier_cone_file_guard(
                config,
                phase,
                validation_summary,
                state.get("last_theorem_frontier_worker_update"),
            )
            state["last_validation"] = validation_summary
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="validation_summary",
                actor="supervisor",
                target="reviewer",
                content=validation_summary,
                content_type="json",
            )
            if is_style_cleanup_phase(phase):
                if not validation_summary["build"]["ok"] or validation_summary["sorries"]["count"] != 0 or validation_summary["axioms"]["unapproved"]:
                    restore_cleanup_last_good_commit(
                        config,
                        state,
                        cycle=cycle,
                        reason="cleanup cycle ended without a fully valid proof state",
                    )
                    print("Cleanup cycle broke proof completeness; restored last good commit and stopping as DONE.")
                    break
                current_head = update_cleanup_last_good_commit(config, state, validation_summary)
                worker_status = str(worker_handoff.get("status", "")).strip().upper()
                if worker_status == "STUCK":
                    print("Cleanup worker reported STUCK; keeping the last good proof-complete commit and stopping as DONE.")
                    save_state(config, state)
                    break
                if cleanup_start_commit and current_head == cleanup_start_commit:
                    print("Cleanup cycle made no committed progress; keeping the last good proof-complete commit and stopping as DONE.")
                    save_state(config, state)
                    break
            save_state(config, state)
        else:
            worker_terminal_output = str(state.get("last_worker_output") or "").strip()
            worker_handoff = state.get("last_worker_handoff")
            validation_summary = state.get("last_validation")
            if not isinstance(worker_handoff, dict):
                raise SupervisorError(
                    f"Cannot resume reviewer cycle {cycle}: missing worker handoff in supervisor state."
                )
            if not isinstance(validation_summary, dict) or last_validation_cycle(state) != cycle:
                raise SupervisorError(
                    f"Cannot resume reviewer cycle {cycle}: missing validation summary for that cycle."
                )

        policy = policy_manager.reload(state=state, persist=True)
        if theorem_frontier_full_enabled(config, phase):
            worker_frontier_update = state.get("last_theorem_frontier_worker_update")
            if not isinstance(worker_frontier_update, dict):
                log_supervisor_warning(
                    config, cycle=cycle, phase=phase,
                    category="frontier_worker_missing",
                    message="No theorem-frontier worker update in state; proceeding without frontier-gated paper review.",
                )
            paper_review = state.get("last_theorem_frontier_paper_review")
            if isinstance(worker_frontier_update, dict) and theorem_frontier_requires_paper_verifier(state, worker_frontier_update):
                if not (isinstance(paper_review, dict) and int(paper_review.get("cycle", 0) or 0) == cycle):
                    paper_review = run_theorem_frontier_paper_verifier_review(
                        config,
                        state,
                        paper_verifier,
                        phase,
                        worker_terminal_output,
                        worker_handoff,
                        worker_frontier_update,
                        cycle=cycle,
                        policy=policy,
                    )
            else:
                state["last_theorem_frontier_paper_review"] = None
        print(f"\n===== cycle {cycle}: reviewer | phase={phase} =====")
        worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
        reviewer_prompt = build_reviewer_prompt(
            config,
            state,
            phase,
            worker_terminal_output,
            worker_handoff_text,
            validation_summary,
            reviewer.needs_initial_run(),
            policy=policy,
        )
        reviewer_prompt_for_chat = build_reviewer_prompt(
            config,
            state,
            phase,
            worker_terminal_output,
            worker_handoff_text,
            validation_summary,
            reviewer.needs_initial_run(),
            include_terminal_output=False,
            policy=policy,
        )
        record_chat_event(
            config,
            state,
            cycle=cycle,
            phase=phase,
            kind="reviewer_prompt",
            actor="supervisor",
            target="reviewer",
            content=reviewer_prompt_for_chat,
            content_type="text",
            summary=f"Supervisor -> reviewer prompt for cycle {cycle}",
        )
        def _validate_reviewer_burst(run: Dict[str, Any]) -> Dict[str, Any]:
            output = run["captured_output"].strip()
            dec = load_json_artifact_with_fallback(
                Path(run["artifact_path"]),
                output,
                ("phase", "decision"),
                fallback_paths=legacy_supervisor_artifact_paths(config, Path(run["artifact_path"])),
            )
            return validate_reviewer_decision(phase, dec)

        reviewer_run, decision = run_burst_with_validation(
            reviewer,
            cycle,
            reviewer_prompt,
            config=config,
            state=state,
            phase=phase,
            stage_label="reviewer burst",
            policy=policy,
            reuse_existing_window=not is_new_cycle,
            validate=_validate_reviewer_burst,
        )
        reviewer.mark_initialized()
        reviewer_terminal_output = reviewer_run["captured_output"].strip()
        frontier_review: Optional[Dict[str, Any]] = None
        if theorem_frontier_enabled(config, phase):
            try:
                frontier_review = load_json_artifact_with_fallback(
                    theorem_frontier_review_path(config),
                    reviewer_terminal_output,
                    ("phase", "outcome"),
                    fallback_paths=legacy_supervisor_artifact_paths(config, theorem_frontier_review_path(config)),
                )
                frontier_review = validate_theorem_frontier_review_for_mode(config, phase, frontier_review)
                state["last_theorem_frontier_review"] = frontier_review
                review_event_content: Union[Dict[str, Any], Any] = frontier_review
                if theorem_frontier_full_enabled(config, phase):
                    worker_frontier_update = state.get("last_theorem_frontier_worker_update")
                    if not isinstance(worker_frontier_update, dict):
                        raise SupervisorError("Missing theorem-frontier worker update while applying full frontier review.")
                    _dag_before_node_ids = set((theorem_frontier_payload(state) or {}).get("nodes", {}).keys())
                    current_frontier = update_theorem_frontier_full_state(
                        config,
                        state,
                        worker_frontier_update,
                        frontier_review,
                        state.get("last_theorem_frontier_paper_review") if isinstance(state.get("last_theorem_frontier_paper_review"), dict) else None,
                        cycle=cycle,
                    )
                    ensure_dag_site(config)
                    export_dag_frontier_snapshot(config, state)
                    export_dag_frontier_cycle(
                        config,
                        state,
                        _dag_before_node_ids,
                        current_frontier,
                        cycle=cycle,
                        outcome=frontier_review.get("outcome", ""),
                        reviewed_node_id=frontier_review.get("active_theorem_id", ""),
                        worker_directive=worker_directive_summary(state),
                    )
                    export_dag_meta(config, state)
                    metrics = current_frontier.get("metrics") if isinstance(current_frontier, dict) else {}
                    escalation = current_frontier.get("escalation") if isinstance(current_frontier, dict) else {}
                    review_event_content = {
                        **frontier_review,
                        "active_leaf_id": current_frontier.get("active_leaf_id"),
                        "active_leaf_age": (metrics or {}).get("active_leaf_age"),
                        "blocker_cluster_age": (metrics or {}).get("blocker_cluster_age"),
                        "cone_purity": frontier_review.get("cone_purity"),
                        "escalation_required": bool((escalation or {}).get("required")),
                    }
                else:
                    current_frontier = update_theorem_frontier_state(config, state, frontier_review, cycle=cycle)
                    review_event_content = {
                        **frontier_review,
                        "active_theorem_age": current_frontier["active_theorem_age"],
                        "blocker_cluster_age": current_frontier["blocker_cluster_age"],
                    }
                record_chat_event(
                    config,
                    state,
                    cycle=cycle,
                    phase=phase,
                    kind="theorem_frontier_review",
                    actor="reviewer",
                    target="supervisor",
                    content=review_event_content,
                    content_type="json",
                )
            except SupervisorError as frontier_exc:
                log_supervisor_warning(
                    config, cycle=cycle, phase=phase,
                    category="frontier_review_processing",
                    message=str(frontier_exc),
                    detail={
                        "frontier_review": frontier_review,
                        "worker_update_present": isinstance(state.get("last_theorem_frontier_worker_update"), dict),
                    },
                )
        decision["cycle"] = cycle
        decision["phase"] = phase
        state["last_review"] = decision
        state.setdefault("review_log", []).append(decision)
        record_chat_event(
            config,
            state,
            cycle=cycle,
            phase=phase,
            kind="reviewer_decision",
            actor="reviewer",
            target="supervisor",
            content=decision,
            content_type="json",
        )
        save_state(config, state)
        append_jsonl(config.state_dir / "review_log.jsonl", decision)

        print("\n===== reviewer decision =====")
        print(json.dumps(decision, indent=2, ensure_ascii=False))

        decision_value = decision["decision"]
        enforce_terminal_decision(phase, decision_value, validation_summary)
        if should_consider_branching(config, state, phase, decision):
            preflight_error = branch_episode_preflight_error(config)
            if preflight_error:
                print(f"Skipping branch consideration for cycle {cycle}: {preflight_error}.")
            else:
                branch_strategy = run_branch_strategy_review(
                    config,
                    state,
                    reviewer,
                    phase,
                    decision,
                    policy=policy,
                )
                state["last_branch_consideration_cycle"] = cycle
                save_state(config, state)
                print("\n===== branch strategy decision =====")
                print(json.dumps(branch_strategy, indent=2, ensure_ascii=False))
                if branch_strategy["branch_decision"] == "BRANCH":
                    if branching_enabled(config):
                        episode = create_branch_episode(
                            config,
                            state,
                            phase,
                            decision,
                            branch_strategy,
                            policy=policy,
                        )
                        print(
                            f"Created branch episode {episode['id']} with {len(episode['branches'])} branch(es). "
                            "Parent supervisor will monitor child branches until selection."
                        )
                        continue
                    if can_propose_branch_replacement(state, config):
                        proposal = store_pending_branch_proposal(state, branch_strategy, cycle=cycle)
                        save_state(config, state)
                        print(
                            "Queued a parent-coordinated branch replacement proposal with "
                            f"{len(proposal.get('strategies', []))} strategy branch(es); "
                            "stopping this branch supervisor so the parent frontier monitor can evaluate it."
                        )
                        break
        if phase == "proof_formalization" and decision_value != "STUCK" and stuck_recovery_attempts(state):
            clear_stuck_recovery(state)
            save_state(config, state)
        if decision_value == "ADVANCE_PHASE":
            next_value = next_phase(phase)
            state.setdefault("phase_history", []).append(
                {
                    "cycle": cycle,
                    "phase": phase,
                    "decision": decision_value,
                    "reason": decision.get("reason", ""),
                }
            )
            if next_value is None:
                print("Reviewer advanced past the final phase; stopping as DONE.")
                break
            state["phase"] = next_value
            save_state(config, state)
            if (
                phase == "theorem_stating"
                and next_value == "proof_formalization"
                and theorem_frontier_phase(config) == "full"
            ):
                try:
                    manifest = load_validated_paper_main_results_manifest(config)
                    seeded_frontier = seed_theorem_frontier_from_main_results_manifest(
                        config,
                        state,
                        manifest,
                        cycle=cycle,
                    )
                    ensure_dag_site(config)
                    export_dag_frontier_snapshot(config, state)
                    export_dag_frontier_seed(config, seeded_frontier, cycle=cycle)
                    export_dag_meta(config, state)
                    record_chat_event(
                        config,
                        state,
                        cycle=cycle,
                        phase=next_value,
                        kind="theorem_frontier_seed",
                        actor="supervisor",
                        target="workflow",
                        content={
                            "initial_active_node_id": seeded_frontier.get("active_leaf_id"),
                            "main_result_node_ids": sorted(seeded_frontier.get("nodes", {}).keys()),
                            "source": str(paper_main_results_manifest_path(config)),
                        },
                        content_type="json",
                    )
                except SupervisorError as exc:
                    log_supervisor_warning(
                        config, cycle=cycle, phase=next_value,
                        category="frontier_dag_seeding",
                        message=str(exc),
                        detail={"manifest_path": str(paper_main_results_manifest_path(config))},
                    )
            if is_style_cleanup_phase(next_value):
                update_cleanup_last_good_commit(config, state, validation_summary)
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=next_value,
                kind="phase_transition",
                actor="supervisor",
                target="workflow",
                content={
                    "from_phase": phase,
                    "to_phase": next_value,
                    "reason": decision.get("reason", ""),
                },
                content_type="json",
            )
            save_state(config, state)
            print(f"Advancing workflow phase: {phase} -> {next_value}")
            time.sleep(supervisor_sleep_seconds(config, policy))
            continue
        if decision_value == "NEED_INPUT":
            write_input_request(config, phase, worker_handoff, decision, validation_summary)
            state["awaiting_human_input"] = True
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="input_request",
                actor="supervisor",
                target="human",
                content=read_text(config.workflow.input_request_path).strip(),
                content_type="text",
            )
            save_state(config, state)
            print(f"Stopping because reviewer requested human input. See {config.workflow.input_request_path}")
            break
        if decision_value == "STUCK":
            if is_style_cleanup_phase(phase):
                restore_cleanup_last_good_commit(
                    config,
                    state,
                    cycle=cycle,
                    reason="cleanup reviewer decided the optional cleanup phase had stalled",
                )
                print("Cleanup reviewer returned STUCK; restored last good commit and stopping as DONE.")
                break
            if can_attempt_stuck_recovery(state, policy):
                suggestion = run_stuck_recovery_review(config, state, reviewer, phase, policy=policy)
                attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
                print(
                    f"Reviewer returned STUCK; queued stuck-recovery attempt "
                    f"{suggestion['attempt']}/{attempt_limit}."
                )
                time.sleep(supervisor_sleep_seconds(config, policy))
                continue
            attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
            print(
                "Stopping because reviewer returned STUCK after exhausting "
                f"{attempt_limit} stuck-recovery attempts."
            )
            break
        if decision_value == "DONE":
            if is_style_cleanup_phase(phase):
                update_cleanup_last_good_commit(config, state, validation_summary)
                save_state(config, state)
            print("Stopping because reviewer returned DONE.")
            break

        time.sleep(supervisor_sleep_seconds(config, policy))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SupervisorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
