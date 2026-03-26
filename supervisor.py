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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
CHAT_VIEWER_DIR = PACKAGE_DIR / "chat_viewer"
PROVIDER_CONTEXT_DIR = PACKAGE_DIR / "provider_context"
PROMPT_TOKEN = "__PROMPT__"
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
PHASES: Tuple[str, ...] = (
    "paper_check",
    "planning",
    "theorem_stating",
    "proof_formalization",
)
WORKER_STATUSES: Tuple[str, ...] = ("NOT_STUCK", "STUCK", "DONE", "NEED_INPUT")
REVIEWER_DECISIONS: Tuple[str, ...] = ("CONTINUE", "ADVANCE_PHASE", "STUCK", "NEED_INPUT", "DONE")
BRANCH_STRATEGY_DECISIONS: Tuple[str, ...] = ("NO_BRANCH", "BRANCH")
BRANCH_SELECTION_DECISIONS: Tuple[str, ...] = ("CONTINUE_BRANCHING", "SELECT_BRANCH")
BRANCH_REPLACEMENT_DECISIONS: Tuple[str, ...] = ("KEEP_FRONTIER", "REPLACE_WITH_PROPOSAL")
SORRY_MODES: Tuple[str, ...] = ("default", "allowed")
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
    start_phase = str(workflow_block.get("start_phase", "proof_formalization")).strip().lower()
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


def next_phase(phase: str) -> Optional[str]:
    idx = phase_index(phase)
    if idx + 1 >= len(PHASES):
        return None
    return PHASES[idx + 1]


def phase_uses_paper_notes(phase: str) -> bool:
    return phase in {"paper_check", "planning", "theorem_stating", "proof_formalization"}


def phase_uses_plan(phase: str) -> bool:
    return phase in {"planning", "theorem_stating", "proof_formalization"}


def phase_uses_statement_files(phase: str) -> bool:
    return phase in {"theorem_stating", "proof_formalization"}


def current_phase(config: Config, state: Dict[str, Any]) -> str:
    phase = str(state.get("phase") or config.workflow.start_phase).strip().lower()
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
    return [
        "- [ ] Prove the target statements presented in `PaperTheorems.lean`.",
        "- [ ] Keep reusable proof infrastructure in separate support files when that yields a cleaner project structure.",
        "- [ ] Maintain `TASKS.md` and `PLAN.md` as the proof frontier moves.",
        "- [ ] Keep sorrys within the configured policy.",
        "- [ ] Do not introduce unapproved axioms.",
    ]


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


def chat_repo_dir(config: Config) -> Path:
    return chat_root_dir(config) / config.chat.repo_name


def chat_repo_meta_path(config: Config) -> Path:
    return chat_repo_dir(config) / "meta.json"


def chat_repo_events_path(config: Config) -> Path:
    return chat_repo_dir(config) / "events.jsonl"


def chat_repo_index_path(config: Config) -> Path:
    return chat_repo_dir(config) / "index.html"


def chat_repo_files_dir(config: Config) -> Path:
    return chat_repo_dir(config) / "files"


def chat_repo_url(config: Config) -> str:
    return f"{config.chat.public_base_url}#{config.chat.repo_name}"


def chat_repo_direct_url(config: Config) -> str:
    return f"{config.chat.public_base_url}{config.chat.repo_name}/"


def install_chat_viewer_assets(root_dir: Path) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = root_dir / "_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    asset_targets = {
        CHAT_VIEWER_DIR / "index.html": root_dir / "index.html",
        CHAT_VIEWER_DIR / "app.js": assets_dir / "app.js",
        CHAT_VIEWER_DIR / "markdown-viewer.html": assets_dir / "markdown-viewer.html",
        CHAT_VIEWER_DIR / "markdown-viewer.js": assets_dir / "markdown-viewer.js",
        CHAT_VIEWER_DIR / "styles.css": assets_dir / "styles.css",
    }
    for source, target in asset_targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    if not (root_dir / "repos.json").exists():
        JsonFile.dump(root_dir / "repos.json", {"repos": []})


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
    current_phase(config, state)
    return state


def save_state(config: Config, state: Dict[str, Any]) -> None:
    JsonFile.dump(config.state_dir / "state.json", state)
    sync_chat_state_metadata(config, state)


def phase_specific_worker_statuses(phase: str) -> Sequence[str]:
    if phase == "planning":
        return WORKER_STATUSES
    return ("NOT_STUCK", "STUCK", "DONE")


def phase_specific_reviewer_decisions(phase: str) -> Sequence[str]:
    if phase == "planning":
        return REVIEWER_DECISIONS
    if phase == "proof_formalization":
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
        return textwrap.dedent(
            f"""\
            Phase objective: create Lean files that state the paper's definitions and theorems as close to `{paper_label}` as possible.

            Requirements:
            - Maintain `{tasks_label}`, `{papernotes_label}`, and `{plan_label}`.
            - Create or update `{definitions_label}` and `{theorems_label}`.
            - Keep the definitions and statements easy for a human to compare against the paper.
            - Make both files syntactically valid Lean.
            - Do not introduce unapproved axioms.
            - `DONE` means the statement files are in place and ready for reviewer comparison against the paper.
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
        text = textwrap.dedent(
            """\
            Decide whether the worker should continue theorem stating, advance to proof formalization, or stop.
            Compare `PaperDefinitions.lean` and `PaperTheorems.lean` against the paper and insist on changes if they do not correspond.
            Require syntactically valid Lean before advancing.
            """
        ).strip()
        git_note = git_reviewer_instructions(config)
        return text + ("\n" + git_note if git_note else "")
    text = textwrap.dedent(
        """\
        Decide whether the worker should continue the proof phase, stop as stuck, or declare the whole workflow done.
        Use the supervisor validation summary for build status, sorry counts, and axiom enforcement.
        Keep `PaperDefinitions.lean` and `PaperTheorems.lean` paper-facing and easy to compare against the paper.
        If the worker is stuffing reusable infrastructure into those files when separate support files would be cleaner, require refactoring.
        """
    ).strip()
    git_note = git_reviewer_instructions(config)
    return text + ("\n" + git_note if git_note else "")


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
    validation_label = supervisor_prompt_label(config, config.reviewer.provider, validation_summary_path(config))
    review_decision_label = supervisor_prompt_label(config, config.reviewer.provider, config.state_dir / "review_decision.json")
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

        Supervisor validation summary from `{validation_label}`:
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 16000)}

        Worker's latest terminal output:
        {terminal_section}

        {phase_reviewer_instructions(config, phase)}
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
    if not branching_enabled(config) and can_propose_branch_replacement(state, config):
        parent_control_note = textwrap.dedent(
            f"""\
            This run is currently a leaf inside a parent-managed branch frontier.
            If you return `BRANCH`, you are proposing up to {strategy_limit} replacement child strategies for the parent supervisor to evaluate.
            The child branches will not be created immediately in this run; the parent supervisor will decide whether the current frontier should be replaced.
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
        {policy_notes}

        Branching policy:
        - At most {strategy_limit} branches may run concurrently in this branch episode or replacement frontier.
        - Branches should be designed to answer the question: which route seems more likely to eventually succeed at formalizing the whole paper?
        - Do not prefer the route that is merely further along today if it appears structurally flawed.
        - Prefer branches whose strategies are materially different: e.g. continue current route, major rewrite, alternate theorem route, alternate abstraction.
        - If no such strategic fork exists yet, return `NO_BRANCH`.

        Before ending this turn:
        - write your branch-strategy JSON to `{branch_strategy_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "branch_decision": "NO_BRANCH" | "BRANCH",
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
        {post_initial_guidance}

        Requirements:
        - Judge branches by their likelihood of eventually succeeding at formalizing the whole paper.
        - Do not default to the branch that is merely furthest along today.
        - Prefer the branch whose route appears structurally sound and paper-faithful, even if it is temporarily behind.
        - Return `CONTINUE_BRANCHING` if the evidence is still too weak and the branches should keep running.
        - Return `SELECT_BRANCH` only if one branch is now clearly the better bet.

        Before ending this turn:
        - write your branch-selection JSON to `{selection_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "selection_decision": "CONTINUE_BRANCHING" | "SELECT_BRANCH",
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

        Requirements:
        - Judge routes by their likelihood of eventually succeeding at formalizing the whole paper.
        - This is a high-bar intervention. Return `REPLACE_WITH_PROPOSAL` only if the proposal is clearly stronger than continuing the current capped frontier.
        - The proposed child strategies must be materially different from each other.
        - The proposed child strategies must also be materially different from the surviving current frontier alternatives they would displace.
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
    return files


def validation_sorry_policy(config: Config, phase: str, sorrys: Dict[str, Any]) -> Dict[str, Any]:
    if config.workflow.sorry_mode == "allowed":
        return {
            "mode": "allowed",
            "allowed_files": ["any"],
            "disallowed_entries": [],
        }
    if phase in {"theorem_stating", "proof_formalization"}:
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


def run_validation(config: Config, phase: str, cycle: int) -> Dict[str, Any]:
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

    sorrys = collect_sorries(config)
    summary["sorries"] = sorrys
    summary["sorry_policy"] = validation_sorry_policy(config, phase, sorrys)

    axioms = collect_axioms(config)
    summary["axioms"] = axioms
    summary["git"] = git_validation_summary(config)

    summary["policy_ok"] = (
        summary["all_required_files_present"]
        and (summary["build"]["ok"] or phase in {"paper_check", "planning"})
        and not summary["sorry_policy"]["disallowed_entries"]
        and not summary["axioms"]["unapproved"]
        and all(check["ok"] for check in syntax_checks)
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
        except Exception as exc:
            message = str(exc)
            if message != self.last_warning:
                print(f"[chat-export] warning: could not refresh markdown files: {message}", file=sys.stderr)
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

    if state is not None and phase is not None:
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


def validate_worker_handoff(phase: str, handoff: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {"phase", "status", "summary_of_changes", "current_frontier", "likely_next_step", "input_request"}
    missing = required_keys.difference(handoff)
    if missing:
        raise SupervisorError(f"Worker handoff missing keys: {sorted(missing)}")
    if str(handoff.get("phase")).strip().lower() != phase:
        raise SupervisorError(f"Worker handoff phase mismatch: expected {phase}, got {handoff.get('phase')}")
    status = str(handoff.get("status", "")).strip().upper()
    allowed = set(phase_specific_worker_statuses(phase))
    if status not in allowed:
        raise SupervisorError(f"Invalid worker status {status!r} for phase {phase}")
    handoff["status"] = status
    return handoff


def validate_reviewer_decision(phase: str, decision: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {"phase", "decision", "confidence", "reason", "next_prompt"}
    missing = required_keys.difference(decision)
    if missing:
        raise SupervisorError(f"Reviewer decision missing keys: {sorted(missing)}")
    if str(decision.get("phase")).strip().lower() != phase:
        raise SupervisorError(f"Reviewer decision phase mismatch: expected {phase}, got {decision.get('phase')}")
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
    decision["branch_decision"] = branch_decision
    decision["strategies"] = strategies
    return decision


def validate_branch_selection_decision(
    phase: str,
    decision: Dict[str, Any],
    allowed_branches: Sequence[str],
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
    decision["selection_decision"] = selection_decision
    decision["selected_branch"] = selected_branch
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

    validation_summary = run_validation(config, phase, cycle)
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

    run = launch_tmux_burst_with_retries(
        reviewer,
        trigger_cycle,
        prompt,
        state=state,
        phase=phase,
        stage_label="reviewer stuck-recovery burst",
        artifact_name=stuck_recovery_suggestion_path(config).name,
        burst_tag=burst_tag,
        policy=policy,
    )
    reviewer.mark_initialized()
    recovery_terminal_output = run["captured_output"].strip()
    suggestion = load_json_artifact_with_fallback(
        Path(run["artifact_path"]),
        recovery_terminal_output,
        ("phase", "diagnosis", "creative_suggestion", "why_this_might_work", "worker_prompt"),
        fallback_paths=legacy_supervisor_artifact_paths(config, Path(run["artifact_path"])),
    )
    suggestion = validate_stuck_recovery_suggestion(phase, suggestion)
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


def config_to_raw_dict(config: Config, *, policy: Optional[Policy] = None) -> Dict[str, Any]:
    effective = effective_policy(config, policy=policy)
    workflow: Dict[str, Any] = {
        "start_phase": config.workflow.start_phase,
        "sorry_mode": config.workflow.sorry_mode,
        "approved_axioms_path": str(config.workflow.approved_axioms_path),
        "human_input_path": str(config.workflow.human_input_path),
        "input_request_path": str(config.workflow.input_request_path),
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
        snapshots.append(
            {
                "name": branch.get("name"),
                "branch_status": branch_status,
                "summary": branch.get("summary"),
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
    }
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
            strategy={**strategy, "name": label},
            parent_max_current_branches=config.branching.max_current_branches,
        )
        JsonFile.dump(worktree_path / ".agent-supervisor" / "state.json", child_state)
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
        "selection_question": "Which branch seems more likely to eventually succeed at formalizing the whole paper?",
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
    selection = validate_branch_selection_decision(phase, selection, allowed)
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
    selection = {
        "phase": phase,
        "selection_decision": "SELECT_BRANCH",
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
    return len(strategies) == config.branching.max_current_branches


def launch_nested_branch_episode_from_snapshot(
    proposal_snapshot: Dict[str, Any],
    *,
    phase: str,
    proposal: Dict[str, Any],
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
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
            if not proposal_snapshot_can_replace_frontier(config, snapshots, proposal_snapshot, policy=policy):
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
    if phase == "proof_formalization" and decision_value == "DONE":
        if not validation_summary["build"]["ok"]:
            raise SupervisorError("Cannot finish proof_formalization while `lake build` is failing.")
        if validation_summary["sorries"]["count"] != 0:
            raise SupervisorError("Cannot finish proof_formalization while any `sorry` remains.")
        if validation_summary["axioms"]["unapproved"]:
            raise SupervisorError("Cannot finish proof_formalization with unapproved axioms present.")


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

    if not has_active_branch_episode and can_attempt_stuck_recovery(state, policy):
        suggestion = run_stuck_recovery_review(config, state, reviewer, phase, policy=policy)
        attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
        print(
            f"Prepared stuck-recovery attempt {suggestion['attempt']}/{attempt_limit} "
            f"from prior STUCK review."
        )
    elif not has_active_branch_episode and has_unhandled_stuck_review(state):
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
            worker_run = launch_tmux_burst_with_retries(
                worker,
                cycle,
                worker_prompt,
                state=state,
                phase=phase,
                stage_label="worker burst",
                policy=policy,
                reuse_existing_window=not is_new_cycle,
            )
            worker.mark_initialized()
            worker_terminal_output = worker_run["captured_output"].strip()
            worker_handoff = load_json_artifact_with_fallback(
                Path(worker_run["artifact_path"]),
                worker_terminal_output,
                ("phase", "status", "summary_of_changes", "current_frontier", "likely_next_step", "input_request"),
                fallback_paths=legacy_supervisor_artifact_paths(config, Path(worker_run["artifact_path"])),
            )
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

            validation_summary = run_validation(config, phase, cycle)
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
        reviewer_run = launch_tmux_burst_with_retries(
            reviewer,
            cycle,
            reviewer_prompt,
            state=state,
            phase=phase,
            stage_label="reviewer burst",
            policy=policy,
            reuse_existing_window=not is_new_cycle,
        )
        reviewer.mark_initialized()
        reviewer_terminal_output = reviewer_run["captured_output"].strip()
        decision = load_json_artifact_with_fallback(
            Path(reviewer_run["artifact_path"]),
            reviewer_terminal_output,
            ("phase", "decision", "confidence", "reason", "next_prompt"),
            fallback_paths=legacy_supervisor_artifact_paths(config, Path(reviewer_run["artifact_path"])),
        )
        decision = validate_reviewer_decision(phase, decision)
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
        if decision_value != "STUCK" and stuck_recovery_attempts(state):
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
