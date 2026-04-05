from __future__ import annotations

from lagent_supervisor.shared import *

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

    def scope_resume_marker(self) -> Path:
        return self.scope_dir() / ".provider-session-ready"

    def work_dir(self) -> Path:
        return self.scope_dir()

    def burst_env(self) -> Dict[str, str]:
        return {}

    def needs_initial_run(self) -> bool:
        role_state = self.role_state()
        if not bool(role_state.get("initialized")):
            return True
        return not self.scope_resume_marker().exists()

    def mark_initialized(self) -> None:
        role_state = self.role_state()
        role_state["initialized"] = True
        self.scope_resume_marker().touch()

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
        return {
            "GEMINI_CLI_HOME": str(
                prepare_gemini_cli_home(
                    self.scope_dir(),
                    fail_fast_on_rate_limit=bool(str(self.cfg.fallback_model or "").strip()),
                )
            )
        }

    def build_initial_command(self) -> List[str]:
        return self._base() + ["--prompt", PROMPT_TOKEN]

    def build_continue_command(self) -> List[str]:
        return self._base() + ["--resume", "latest", "--prompt", PROMPT_TOKEN]


def burst_log_text(run: Dict[str, Any]) -> str:
    log_text = ""
    per_cycle_log = run.get("per_cycle_log")
    if isinstance(per_cycle_log, Path):
        log_text += read_text(per_cycle_log)
    elif per_cycle_log:
        log_text += read_text(Path(str(per_cycle_log)))
    captured = run.get("captured_output")
    if isinstance(captured, str):
        log_text += "\n" + captured
    return log_text


def gemini_should_fallback_on_run(adapter: ProviderAdapter, run: Dict[str, Any]) -> bool:
    if adapter.cfg.provider != "gemini":
        return False
    fallback_model = str(adapter.cfg.fallback_model or "").strip()
    primary_model = str(adapter.cfg.model or "").strip()
    if not fallback_model or fallback_model == primary_model:
        return False
    if int(run.get("exit_code", 0) or 0) == 0:
        return False
    lowered = burst_log_text(run).lower()
    return any(pattern.lower() in lowered for pattern in GEMINI_RATE_LIMIT_OR_CAPACITY_PATTERNS)


def burst_hit_budget_error(run: Dict[str, Any]) -> bool:
    if int(run.get("exit_code", 0) or 0) == 0:
        return False
    lowered = burst_log_text(run).lower()
    return any(pattern in lowered for pattern in BUDGET_ERROR_PATTERNS)


def burst_hit_productive_local_failure(run: Dict[str, Any]) -> bool:
    if int(run.get("exit_code", 0) or 0) == 0:
        return False
    if burst_hit_budget_error(run):
        return False
    lowered = burst_log_text(run).lower()
    return any(pattern in lowered for pattern in PRODUCTIVE_LOCAL_FAILURE_PATTERNS)


def gemini_fallback_adapter(adapter: ProviderAdapter) -> GeminiAdapter:
    fallback_model = str(adapter.cfg.fallback_model or "").strip()
    if adapter.cfg.provider != "gemini" or not fallback_model:
        raise SupervisorError("Gemini fallback adapter requested without a configured Gemini fallback model.")
    return GeminiAdapter(
        ProviderConfig(
            provider="gemini",
            model=fallback_model,
            extra_args=list(adapter.cfg.extra_args),
            fallback_model=None,
        ),
        adapter.role,
        adapter.config,
        adapter.state,
    )


def make_adapter(role: str, config: Config, state: Dict[str, Any]) -> ProviderAdapter:
    cfg = config.worker if role == "worker" else config.reviewer
    if cfg.provider == "claude":
        return ClaudeAdapter(cfg, role, config, state)
    if cfg.provider == "codex":
        return CodexAdapter(cfg, role, config, state)
    if cfg.provider == "gemini":
        return GeminiAdapter(cfg, role, config, state)
    raise AssertionError(cfg.provider)
