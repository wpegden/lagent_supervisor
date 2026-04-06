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
import tempfile
import textwrap
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"
CHAT_VIEWER_DIR = PROJECT_ROOT / "chat_viewer"
PROVIDER_CONTEXT_DIR = PROJECT_ROOT / "provider_context"
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
THEOREM_FRONTIER_PHASES: Tuple[str, ...] = ("off", "full")
THEOREM_FRONTIER_ACTIONS: Tuple[str, ...] = ("CLOSE", "REFACTOR", "EXPAND", "REFUTE_REPLACE")
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
THEOREM_FRONTIER_LEAN_PROOF_STATUSES: Tuple[str, ...] = (
    "unproved",
    "proved",
)
GENERATED_FRONTIER_DIRNAME = "GeneratedFrontier"
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
SUPERVISOR_SHARED_STATE_DIR_MODE = 0o2775
SUPERVISOR_SHARED_STATE_FILE_MODE = 0o640
SUPERVISOR_SHARED_MULTIWRITER_FILE_MODE = 0o664
SUPERVISOR_SHARED_STATE_LOG_MODE = SUPERVISOR_SHARED_MULTIWRITER_FILE_MODE
SUPERVISOR_CHECKPOINT_DIR_MODE = 0o2755
SUPERVISOR_CHECKPOINT_FILE_MODE = 0o644
SUPERVISOR_SHARED_REPO_DIR_MODE = 0o2775
SUPERVISOR_SHARED_REPO_FILE_MODE = 0o664


class SupervisorError(RuntimeError):
    pass


def validate_phase_and_cycle_fields(
    label: str,
    payload: Dict[str, Any],
    *,
    phase: str,
    cycle: int,
) -> Dict[str, Any]:
    actual_phase = str(payload.get("phase", "")).strip().lower()
    if actual_phase != phase:
        raise SupervisorError(f"{label} phase mismatch: expected {phase}, got {payload.get('phase')}")
    if "cycle" not in payload:
        raise SupervisorError(f"{label} missing key 'cycle'.")
    raw_cycle = payload.get("cycle")
    try:
        actual_cycle = int(raw_cycle)
    except (TypeError, ValueError):
        raise SupervisorError(f"{label} cycle must be an integer, got {raw_cycle!r}.")
    if actual_cycle != cycle:
        raise SupervisorError(f"{label} cycle mismatch: expected {cycle}, got {raw_cycle!r}.")
    payload["phase"] = phase
    payload["cycle"] = cycle
    return payload


@dataclass
class ProviderConfig:
    provider: str
    model: Optional[str]
    extra_args: List[str]
    fallback_model: Optional[str] = None


@dataclass
class TmuxConfig:
    session_name: str
    dashboard_window_name: str
    kill_windows_after_capture: bool
    burst_user: Optional[str] = None
    burst_group: Optional[str] = None
    burst_home: Optional[Path] = None


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
    source_path: Optional[Path] = None


DAG_VIEWER_DIR = PROJECT_ROOT / "dag_viewer"

from lagent_supervisor.storage import JsonFile

GEMINI_RATE_LIMIT_OR_CAPACITY_PATTERNS: Tuple[str, ...] = (
    "model_capacity_exhausted",
    "rateLimitExceeded",
    "rate limit exceeded",
    "resource_exhausted",
    "too many requests",
    "status 429",
    '"code": 429',
    "no capacity available for model",
)

BUDGET_ERROR_RETRY_DELAY_SECONDS = 15 * 60
PRODUCTIVE_LOCAL_FAILURE_MAX_RETRY_DELAY_SECONDS = 5 * 60

BUDGET_ERROR_PATTERNS: Tuple[str, ...] = (
    "model_capacity_exhausted",
    "ratelimitexceeded",
    "rate limit exceeded",
    "too many requests",
    "resource_exhausted",
    "retryablequotaerror",
    "quota exceeded",
    "usage limit",
    "credit balance is too low",
    "overloaded_error",
    "status 429",
    '"code": 429',
    "no capacity available for model",
    "you've hit your limit",
    "you have hit your limit",
    "hit your limit",
)

PRODUCTIVE_LOCAL_FAILURE_PATTERNS: Tuple[str, ...] = (
    "type mismatch",
    "unsolved goals",
    "application type mismatch",
    "declaration has type",
    "tactic `",
    "tactic '",
    "building twobites.",
    "error: twobites/",
    "error: repo/",
    "lake build",
    "error loading config.toml",
    "permission denied (os error 13)",
    "operation not permitted (os error 1)",
)


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
                    _persist_state(self.config, state)
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
                    _persist_state(self.config, state)
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
                    _persist_state(self.config, state)
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


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def codex_credit_status_from_rate_limits(rate_limits: Dict[str, Any]) -> Dict[str, Any]:
    credits = rate_limits.get("credits")
    status: Dict[str, Any] = {
        "credits_raw": credits,
        "credits_available": None,
        "credits_remaining": None,
        "credits_used": None,
        "credits_spent": None,
        "credits_limit": None,
    }
    if credits is None:
        return status
    if isinstance(credits, (int, float)) and not isinstance(credits, bool):
        status["credits_available"] = float(credits)
        return status
    if not isinstance(credits, dict):
        return status

    def _pick(*keys: str) -> Optional[float]:
        for key in keys:
            parsed = _optional_float(credits.get(key))
            if parsed is not None:
                return parsed
        return None

    status["credits_available"] = _pick("available", "balance")
    status["credits_remaining"] = _pick("remaining")
    status["credits_used"] = _pick("used", "consumed")
    status["credits_spent"] = _pick("spent", "charges")
    status["credits_limit"] = _pick("limit", "total", "purchased", "purchased_total")
    return status


def codex_budget_status_from_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = record.get("payload")
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
        "timestamp": str(record.get("timestamp") or ""),
        "plan_type": rate_limits.get("plan_type"),
        "used_percent": used_percent,
        "percent_left": percent_left,
        "window_minutes": int(secondary.get("window_minutes") or 0),
        "resets_at": secondary.get("resets_at"),
        "weekly_budget_exhausted": used_percent >= 100.0,
        **codex_credit_status_from_rate_limits(rate_limits),
    }


def codex_token_usage_from_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    total = info.get("total_token_usage")
    last = info.get("last_token_usage")
    if not isinstance(total, dict) or not isinstance(last, dict):
        return None
    budget = codex_budget_status_from_record(record) or {}
    return {
        "input_tokens": int(total.get("input_tokens") or 0),
        "cached_input_tokens": int(total.get("cached_input_tokens") or 0),
        "output_tokens": int(total.get("output_tokens") or 0),
        "reasoning_output_tokens": int(total.get("reasoning_output_tokens") or 0),
        "total_tokens": int(total.get("total_tokens") or 0),
        "last_input_tokens": int(last.get("input_tokens") or 0),
        "last_cached_input_tokens": int(last.get("cached_input_tokens") or 0),
        "last_output_tokens": int(last.get("output_tokens") or 0),
        "last_reasoning_output_tokens": int(last.get("reasoning_output_tokens") or 0),
        "last_total_tokens": int(last.get("total_tokens") or 0),
        "model_context_window": int(info.get("model_context_window") or 0),
        "plan_type": budget.get("plan_type"),
        "weekly_used_percent": budget.get("used_percent"),
        "weekly_percent_left": budget.get("percent_left"),
        "weekly_window_minutes": budget.get("window_minutes"),
        "weekly_resets_at": budget.get("resets_at"),
        "weekly_budget_exhausted": bool(budget.get("weekly_budget_exhausted")),
        "credits_raw": budget.get("credits_raw"),
        "credits_available": budget.get("credits_available"),
        "credits_remaining": budget.get("credits_remaining"),
        "credits_used": budget.get("credits_used"),
        "credits_spent": budget.get("credits_spent"),
        "credits_limit": budget.get("credits_limit"),
    }


def codex_session_log_matches_scope(path: Path, scope_dir: Path) -> bool:
    try:
        tail_text = read_text_tail(path)
    except OSError:
        return False
    scope_text = str(scope_dir.resolve())
    for line in reversed(tail_text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        record_type = str(record.get("type") or "")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record_type == "turn_context" and str(payload.get("cwd") or "") == scope_text:
            return True
        if record_type == "session_meta" and str(payload.get("cwd") or "") == scope_text:
            return True
    return False


def latest_codex_token_usage_for_scope(scope_dir: Path, *, limit: int = 20) -> Optional[Dict[str, Any]]:
    latest: Optional[Dict[str, Any]] = None
    for path in recent_codex_session_log_paths(limit=limit):
        if not codex_session_log_matches_scope(path, scope_dir):
            continue
        record = latest_codex_token_count_event_in_file(path)
        if record is None:
            continue
        usage = codex_token_usage_from_record(record)
        if usage is None:
            continue
        candidate = {
            "timestamp": str(record.get("timestamp") or ""),
            "source_path": str(path),
            **usage,
        }
        if latest is None or candidate["timestamp"] > str(latest.get("timestamp") or ""):
            latest = candidate
    return latest


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
    status = codex_budget_status_from_record(latest_record)
    if status is None:
        return None
    return {
        **status,
        "source_path": str(latest_path),
    }


def _supervisor_override(name: str, default: Any) -> Any:
    module = sys.modules.get("supervisor")
    if module is None:
        return default
    return getattr(module, name, default)


def _persist_state(config: Config, state: Dict[str, Any]) -> None:
    save = _supervisor_override("save_state", None)
    if callable(save):
        save(config, state)
        return
    JsonFile.dump(config.state_dir / "state.json", state, mode=SUPERVISOR_SHARED_STATE_FILE_MODE)


def normalize_worker_readable_state_permissions(config: Config) -> None:
    def _normalize_tree(root: Path, *, dir_mode: int, file_mode: int) -> None:
        if not root.exists():
            return
        for path in [root, *root.rglob("*")]:
            try:
                current_mode = path.stat().st_mode & (0o7777 if path.is_dir() else 0o777)
                expected_mode = dir_mode if path.is_dir() else file_mode
                if current_mode != expected_mode:
                    path.chmod(expected_mode)
            except PermissionError:
                pass

    for path in (
        config.state_dir,
        config.state_dir / "cycles",
        config.state_dir / "logs",
        config.state_dir / "runtime",
        config.state_dir / "prompts",
    ):
        _normalize_tree(path, dir_mode=SUPERVISOR_SHARED_STATE_DIR_MODE, file_mode=SUPERVISOR_SHARED_MULTIWRITER_FILE_MODE)
    for path in (
        config.state_dir / "state.json",
        theorem_frontier_state_path(config),
    ):
        if path.exists():
            current_mode = path.stat().st_mode & 0o777
            if current_mode != SUPERVISOR_SHARED_STATE_FILE_MODE:
                try:
                    path.chmod(SUPERVISOR_SHARED_STATE_FILE_MODE)
                except PermissionError:
                    pass
    for path in (
        config.state_dir / "validation_summary.json",
        paper_main_results_manifest_path(config),
        config.state_dir / "validation_log.jsonl",
    ):
        if path.exists():
            current_mode = path.stat().st_mode & 0o777
            if current_mode != SUPERVISOR_SHARED_MULTIWRITER_FILE_MODE:
                try:
                    path.chmod(SUPERVISOR_SHARED_MULTIWRITER_FILE_MODE)
                except PermissionError:
                    pass
    normalize_checkpoint_tree_permissions(cycle_checkpoints_dir(config))


def normalize_checkpoint_tree_permissions(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        try:
            expected_mode = SUPERVISOR_CHECKPOINT_DIR_MODE if path.is_dir() else SUPERVISOR_CHECKPOINT_FILE_MODE
            current_mode = path.stat().st_mode & (0o7777 if path.is_dir() else 0o777)
            if current_mode != expected_mode:
                path.chmod(expected_mode)
        except PermissionError:
            pass


def ensure_shared_repo_dir(path: Path) -> None:
    path.mkdir(mode=SUPERVISOR_SHARED_REPO_DIR_MODE, parents=True, exist_ok=True)
    current_mode = path.stat().st_mode & 0o7777
    if current_mode != SUPERVISOR_SHARED_REPO_DIR_MODE:
        path.chmod(SUPERVISOR_SHARED_REPO_DIR_MODE)


def write_shared_repo_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    ensure_shared_repo_dir(path.parent)
    path.write_text(content, encoding=encoding)
    current_mode = path.stat().st_mode & 0o777
    if current_mode != SUPERVISOR_SHARED_REPO_FILE_MODE:
        path.chmod(SUPERVISOR_SHARED_REPO_FILE_MODE)


def normalize_repo_mutable_permissions(config: Config) -> None:
    mutable_dirs = [
        config.repo_path,
        config.repo_path / ".lake",
        config.repo_path / ".lake" / "packages",
        config.repo_path / "build",
        theorem_frontier_generated_dir(config),
        theorem_frontier_generated_proofs_dir(config),
    ]
    for path in mutable_dirs:
        if path.exists():
            ensure_shared_repo_dir(path)


def normalize_burst_user_codex_permissions(config: Config) -> None:
    burst_home = config.tmux.burst_home
    burst_group = str(config.tmux.burst_group or "").strip()
    if burst_home is None or not burst_group:
        return
    codex_dir = burst_home / ".codex"
    if codex_dir.exists():
        current_mode = codex_dir.stat().st_mode & 0o777
        if current_mode != 0o775:
            codex_dir.chmod(0o775)
    for path in (
        codex_dir / "config.toml",
        codex_dir / "auth.json",
    ):
        if path.exists():
            current_mode = path.stat().st_mode & 0o777
            if current_mode != 0o640:
                path.chmod(0o640)
    sessions_dir = codex_dir / "sessions"
    if sessions_dir.exists():
        current_mode = sessions_dir.stat().st_mode & 0o777
        if current_mode != 0o775:
            sessions_dir.chmod(0o775)
    for mutable_dir in (
        codex_dir / "sessions",
        codex_dir / "tmp",
        codex_dir / "log",
        codex_dir / "shell_snapshots",
        codex_dir / "memories",
    ):
        if not mutable_dir.exists():
            continue
        for path in [mutable_dir, *mutable_dir.rglob("*")]:
            try:
                expected_mode = 0o775 if path.is_dir() else 0o664
                current_mode = path.stat().st_mode & (0o777 if not path.is_dir() else 0o777)
                if current_mode != expected_mode:
                    path.chmod(expected_mode)
            except PermissionError:
                pass


def append_supervisor_jsonl(path: Path, payload: Any, *, mode: int = SUPERVISOR_SHARED_STATE_LOG_MODE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    if not path.exists():
        path.write_text(line, encoding="utf-8")
        path.chmod(mode)
        return
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        current_mode = path.stat().st_mode & 0o777
        if current_mode != mode:
            path.chmod(mode)
        return
    except PermissionError:
        existing = read_text(path)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        temp_path.write_text(existing + line, encoding="utf-8")
        temp_path.chmod(mode)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)  # type: ignore[arg-type]


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
            _persist_state(config, state)
        return

    if current is not None:
        state["codex_budget_pause"] = None
        _persist_state(config, state)


def wait_for_codex_weekly_budget_if_needed(
    config: Config,
    state: Dict[str, Any],
    *,
    phase: str,
    stage_label: str,
) -> None:
    policy_manager = PolicyManager(config)
    announced_pause = False
    latest_status = _supervisor_override("latest_codex_weekly_budget_status", latest_codex_weekly_budget_status)
    sleep_fn = getattr(_supervisor_override("time", time), "sleep", time.sleep)
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

        status = latest_status()
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
        sleep_fn(codex_weekly_budget_pause_poll_seconds(config, policy))


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
            fallback_model=block.get("fallback_model"),
        )

    tmux_block = raw.get("tmux", {})
    burst_user_raw = tmux_block.get("burst_user")
    burst_user = str(burst_user_raw).strip() if burst_user_raw is not None else None
    if burst_user == "":
        burst_user = None
    burst_group_raw = tmux_block.get("burst_group")
    burst_group = str(burst_group_raw).strip() if burst_group_raw is not None else None
    if burst_group == "":
        burst_group = None
    burst_home_raw = tmux_block.get("burst_home")
    burst_home: Optional[Path] = None
    if burst_home_raw is not None:
        burst_home = Path(str(burst_home_raw)).expanduser()
        if not burst_home.is_absolute():
            burst_home = (repo_path / burst_home).resolve()
        else:
            burst_home = burst_home.resolve()
    tmux_cfg = TmuxConfig(
        session_name=sanitize_tmux_session_name(str(tmux_block.get("session_name", "lean-agents"))),
        dashboard_window_name=str(tmux_block.get("dashboard_window_name", "dashboard")),
        kill_windows_after_capture=bool(tmux_block.get("kill_windows_after_capture", True)),
        burst_user=burst_user,
        burst_group=burst_group,
        burst_home=burst_home,
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
        source_path=path,
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
        _persist_state(config, state)


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
    return theorem_frontier_phase(config) == "full" and phase == "proof_formalization"


def theorem_frontier_full_enabled(config: Config, phase: str) -> bool:
    return theorem_frontier_enabled(config, phase)


def theorem_frontier_state_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier.json"


def theorem_frontier_history_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_history.jsonl"


def cycle_records_dir(config: Config) -> Path:
    return config.state_dir / "cycles"


def cycle_record_dir(config: Config, cycle: int) -> Path:
    return cycle_records_dir(config) / f"cycle-{int(cycle):04d}"


def cycle_role_artifacts_dir(config: Config, cycle: int, role: str) -> Path:
    return cycle_record_dir(config, cycle) / role


def cycle_role_artifact_path(config: Config, cycle: int, role: str, filename: str) -> Path:
    return cycle_role_artifacts_dir(config, cycle, role) / filename


def worker_handoff_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "worker_handoff.json"
    return cycle_role_artifact_path(config, cycle, "worker", "worker_handoff.json")


def reviewer_decision_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "review_decision.json"
    return cycle_role_artifact_path(config, cycle, "reviewer", "review_decision.json")


def theorem_frontier_worker_update_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "theorem_frontier_update.json"
    return cycle_role_artifact_path(config, cycle, "worker", "theorem_frontier_update.json")


def theorem_frontier_review_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "theorem_frontier_review.json"
    return cycle_role_artifact_path(config, cycle, "reviewer", "theorem_frontier_review.json")


def theorem_frontier_paper_verifier_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "theorem_frontier_paper_verifier.json"
    return cycle_role_artifact_path(config, cycle, "paper_verifier", "theorem_frontier_paper_verifier.json")


def theorem_frontier_nl_proof_verifier_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "theorem_frontier_nl_proof_verifier.json"
    return cycle_role_artifact_path(config, cycle, "nl_proof_verifier", "theorem_frontier_nl_proof_verifier.json")


def stuck_recovery_suggestion_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "stuck_recovery_suggestion.json"
    return cycle_role_artifact_path(config, cycle, "stuck_recovery", "stuck_recovery_suggestion.json")


def branch_strategy_artifact_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "branch_strategy.json"
    return cycle_role_artifact_path(config, cycle, "branch_strategy", "branch_strategy.json")


def branch_selection_artifact_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "branch_selection.json"
    return cycle_role_artifact_path(config, cycle, "branch_selection", "branch_selection.json")


def branch_replacement_artifact_path(config: Config, cycle: Optional[int] = None) -> Path:
    if cycle is None:
        return config.state_dir / "branch_replacement.json"
    return cycle_role_artifact_path(config, cycle, "branch_replacement", "branch_replacement.json")


def theorem_frontier_worker_update_current_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_update.json"


def theorem_frontier_review_current_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_review.json"


def theorem_frontier_paper_verifier_current_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_paper_verifier.json"


def theorem_frontier_nl_proof_verifier_current_path(config: Config) -> Path:
    return config.state_dir / "theorem_frontier_nl_proof_verifier.json"


def paper_main_results_manifest_path(config: Config) -> Path:
    return config.state_dir / "paper_main_results.json"


def repo_primary_lean_lib_name(config: Config) -> str:
    lakefile_toml = config.repo_path / "lakefile.toml"
    if lakefile_toml.exists():
        try:
            data = tomllib.loads(lakefile_toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        libs = data.get("lean_lib")
        if isinstance(libs, list):
            for entry in libs:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                if name and (config.repo_path / name).is_dir():
                    return name
        package_name = str(data.get("name") or "").strip()
        if package_name and (config.repo_path / package_name).is_dir():
            return package_name
    return ""


def theorem_frontier_generated_module_root(config: Config) -> str:
    lib_name = repo_primary_lean_lib_name(config)
    if lib_name:
        return f"{lib_name}.{GENERATED_FRONTIER_DIRNAME}"
    return GENERATED_FRONTIER_DIRNAME


def theorem_frontier_generated_dir(config: Config) -> Path:
    lib_name = repo_primary_lean_lib_name(config)
    if lib_name:
        return config.repo_path / lib_name / GENERATED_FRONTIER_DIRNAME
    return config.repo_path / GENERATED_FRONTIER_DIRNAME


def theorem_frontier_generated_statements_path(config: Config) -> Path:
    return theorem_frontier_generated_dir(config) / "Statements.lean"


def theorem_frontier_generated_proofs_dir(config: Config) -> Path:
    return theorem_frontier_generated_dir(config) / "Proofs"


def theorem_frontier_generated_node_slug(node_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(node_id or "").strip()).strip("_").lower()
    if not cleaned:
        cleaned = "node"
    if not cleaned[0].isalpha():
        cleaned = f"node_{cleaned}"
    elif not cleaned.startswith("node_"):
        cleaned = f"node_{cleaned}"
    return cleaned


def theorem_frontier_generated_proof_path(config: Config, node_id: str) -> Path:
    return theorem_frontier_generated_proofs_dir(config) / f"{theorem_frontier_generated_node_slug(node_id)}.lean"


def cycle_checkpoints_dir(config: Config) -> Path:
    return config.state_dir / "checkpoints"


def cycle_checkpoint_dir(config: Config, cycle: int) -> Path:
    return cycle_checkpoints_dir(config) / f"cycle-{int(cycle):04d}"


def cycle_checkpoint_manifest_path(config: Config) -> Path:
    return cycle_checkpoints_dir(config) / "manifest.json"


def cycle_boundary_restart_request_path(config: Config) -> Path:
    return config.state_dir / "restart_after_cycle.json"


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


def repo_relative_path(config: Config, path: Path) -> str:
    try:
        return path.resolve().relative_to(config.repo_path).as_posix()
    except ValueError:
        return str(path)


def ensure_approved_axioms_file(config: Config) -> None:
    if config.workflow.approved_axioms_path.exists():
        return
    JsonFile.dump(config.workflow.approved_axioms_path, {"approved_axioms": []})


def paper_main_results_manifest_stub(config: Config) -> Dict[str, Any]:
    return {
        "phase": "theorem_stating",
        "nodes": [
            {
                "node_id": "paper.main",
                "kind": "paper",
                "natural_language_statement": "REPLACE_ME: exact paper statement for the first main result.",
                "natural_language_proof": "REPLACE_ME: complete publication-grade natural-language proof of this node from its current children, at least as detailed as the paper and usually more detailed because it must be locally self-contained.",
                "lean_statement": "def REPLACE_ME_main_statement : Prop := False",
                "lean_anchor": "PaperTheorems.REPLACE_ME_main_statement",
                "paper_provenance": "REPLACE_ME: exact theorem/proposition label from the paper.",
                "blocker_cluster": "REPLACE_ME main-result blocker cluster",
                "acceptance_evidence": "REPLACE_ME: what must be proved for this node to close.",
                "notes": "Replace every REPLACE_ME field before advancing to proof_formalization.",
            },
            {
                "node_id": "paper.main_aux",
                "kind": "paper_faithful_reformulation",
                "natural_language_statement": "REPLACE_ME: exact auxiliary paper lemma/case statement used on the proof spine.",
                "natural_language_proof": "REPLACE_ME: complete publication-grade natural-language proof of this node from its current children, at least as detailed as the paper and usually more detailed because it must be locally self-contained.",
                "lean_statement": "def REPLACE_ME_main_aux_statement : Prop := False",
                "lean_anchor": "PaperTheorems.REPLACE_ME_main_aux_statement",
                "paper_provenance": "REPLACE_ME: exact paper lemma/proposition/case label.",
                "blocker_cluster": "REPLACE_ME auxiliary blocker cluster",
                "acceptance_evidence": "REPLACE_ME: what must be proved for this auxiliary node to close.",
                "notes": "Use `paper` for exact paper statements and `paper_faithful_reformulation` only when Lean needs a faithful reformulation. If a proof relies on another named paper lemma/case, add that dependency as a child node instead of hiding it in prose.",
            },
        ],
        "edges": [
            {
                "parent": "paper.main",
                "child": "paper.main_aux",
            },
        ],
        "initial_active_node_id": "paper.main",
    }


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
        from lagent_supervisor.frontier import normalize_frontier_text

        payload = JsonFile.load(theorem_frontier_state_path(config), {})
        if isinstance(payload, dict) and payload.get("mode") == "full":
            active_node_id = normalize_frontier_text(payload.get("active_node_id"))
            nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
            active_node = nodes.get(active_node_id) if active_node_id else None
            metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
            escalation = payload.get("escalation") if isinstance(payload.get("escalation"), dict) else {}
            tasks.insert(0, "- [ ] The theorem-frontier DAG is authoritative for proof-formalization work.")
            if isinstance(active_node, dict):
                tasks.insert(1, f"- [ ] Active theorem node: `{active_node_id}` at `{active_node.get('lean_anchor')}`.")
                tasks.insert(2, f"- [ ] Blocker cluster: {active_node.get('blocker_cluster')}.")
                tasks.insert(3, f"- [ ] Current action: `{payload.get('current_action') or '(unset)'}`.")
                children = active_node.get("child_ids") if isinstance(active_node.get("child_ids"), list) else []
                tasks.insert(4, f"- [ ] Immediate children: {', '.join(children) if children else '(none)'}")
                tasks.insert(5, f"- [ ] Active-node age: {int(metrics.get('active_node_age', 0) or 0)}; blocker age: {int(metrics.get('blocker_cluster_age', 0) or 0)}.")
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
    normalize_repo_mutable_permissions(config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "runtime", "prompts"):
        (config.state_dir / name).mkdir(parents=True, exist_ok=True)
    if theorem_frontier_enabled(config, phase) and not theorem_frontier_state_path(config).exists():
        from lagent_supervisor.frontier import default_theorem_frontier_payload

        JsonFile.dump(
            theorem_frontier_state_path(config),
            default_theorem_frontier_payload("full"),
            mode=SUPERVISOR_SHARED_STATE_FILE_MODE,
        )

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
    if phase == "theorem_stating" and theorem_frontier_phase(config) == "full":
        manifest_path = paper_main_results_manifest_path(config)
        if not manifest_path.exists():
            JsonFile.dump(
                manifest_path,
                paper_main_results_manifest_stub(config),
                mode=SUPERVISOR_SHARED_MULTIWRITER_FILE_MODE,
            )
    normalize_worker_readable_state_permissions(config)


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


def scope_root_dir(config: Config) -> Path:
    identity = f"{config.repo_path.resolve()}::{config.state_dir.resolve()}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    label = sanitize_repo_name(config.repo_path.name)
    root = Path(tempfile.gettempdir()) / "lagent-supervisor-scopes" / f"{label}-{digest}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def supervisor_control_root_dir(config: Config) -> Path:
    identity = f"{config.repo_path.resolve()}::{config.state_dir.resolve()}::supervisor-control"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    label = sanitize_repo_name(config.repo_path.name)
    root = Path.home() / ".lagent-supervisor-control" / f"{label}-{digest}"
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    root.chmod(0o755)
    return root


def supervisor_prompts_dir(config: Config) -> Path:
    path = supervisor_control_root_dir(config) / "prompts"
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.chmod(0o755)
    return path


def supervisor_scripts_dir(config: Config) -> Path:
    path = supervisor_control_root_dir(config) / "scripts"
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.chmod(0o755)
    return path


def supervisor_git_config_path(config: Config) -> Path:
    path = supervisor_control_root_dir(config) / "gitconfig"
    content = "\n".join(
        [
            "[safe]",
            f"\tdirectory = {config.repo_path}",
            "",
        ]
    )
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    path.chmod(0o644)
    return path


def supervisor_runtime_markers_dir(config: Config) -> Path:
    path = config.state_dir / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def role_scope_dir(config: Config, provider: str, role: str) -> Path:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    scope = scope_root_dir(config) / f"{provider}-{role}"
    scope.mkdir(parents=True, exist_ok=True)
    links = {
        "repo": config.repo_path,
        ".agent-supervisor": config.state_dir,
    }
    legacy_supervisor_link = scope / "supervisor"
    if legacy_supervisor_link.is_symlink():
        legacy_supervisor_link.unlink(missing_ok=True)  # type: ignore[arg-type]
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


def _merge_gemini_scope_settings(gemini_home: Path, *, fail_fast_on_rate_limit: bool = False) -> None:
    settings_path = gemini_home / "settings.json"
    settings: Dict[str, Any] = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}
        if isinstance(loaded, dict):
            settings = loaded
    if fail_fast_on_rate_limit:
        general = settings.get("general")
        if not isinstance(general, dict):
            general = {}
        general["maxAttempts"] = 1
        settings["general"] = general
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_gemini_cli_home(scope_dir: Path, *, fail_fast_on_rate_limit: bool = False) -> Path:
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
    _merge_gemini_scope_settings(gemini_home, fail_fast_on_rate_limit=fail_fast_on_rate_limit)
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


def dag_frontier_web_path(config: Config) -> Path:
    return dag_repo_dir(config) / "frontier.txt"


def dag_frontier_history_path(config: Config) -> Path:
    return dag_repo_dir(config) / "frontier-history.jsonl"


def dag_frontier_history_web_path(config: Config) -> Path:
    return dag_repo_dir(config) / "frontier-history.txt"


def dag_manifest_path(config: Config) -> Path:
    return dag_root_dir(config) / "repos.json"


def dag_manifest_web_path(config: Config) -> Path:
    return dag_root_dir(config) / "repos.txt"


def dag_codex_budget_path(config: Config) -> Path:
    return dag_root_dir(config) / "codex-budget.json"


def dag_codex_budget_web_path(config: Config) -> Path:
    return dag_root_dir(config) / "codex-budget.txt"


def dag_assets_dir(config: Config) -> Path:
    return dag_root_dir(config) / "_assets"


def dag_repo_meta_path(config: Config) -> Path:
    return dag_repo_dir(config) / "meta.json"


def dag_repo_meta_web_path(config: Config) -> Path:
    return dag_repo_dir(config) / "meta.txt"


def chat_repo_url(config: Config) -> str:
    return f"{config.chat.public_base_url}#{config.chat.repo_name}"


def chat_repo_direct_url(config: Config) -> str:
    return f"{config.chat.public_base_url}{config.chat.repo_name}/"


def render_template(name: str, **kwargs: Any) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").format(**kwargs)
