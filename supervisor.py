#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
PROMPT_TOKEN = "__PROMPT__"


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
class Config:
    repo_path: Path
    goal_file: Path
    state_dir: Path
    worker: ProviderConfig
    reviewer: ProviderConfig
    tmux: TmuxConfig
    max_cycles: int
    sleep_seconds: float


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


def load_config(path: Path) -> Config:
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
        session_name=str(tmux_block.get("session_name", "lean-agents")),
        dashboard_window_name=str(tmux_block.get("dashboard_window_name", "dashboard")),
        kill_windows_after_capture=bool(tmux_block.get("kill_windows_after_capture", True)),
    )

    return Config(
        repo_path=repo_path,
        goal_file=goal_file,
        state_dir=state_dir,
        worker=provider_cfg("worker"),
        reviewer=provider_cfg("reviewer"),
        tmux=tmux_cfg,
        max_cycles=int(raw.get("max_cycles", 0)),
        sleep_seconds=float(raw.get("sleep_seconds", 1.0)),
    )


def check_dependencies() -> None:
    for exe in ("tmux",):
        if subprocess.run(["bash", "-lc", f"command -v {shlex.quote(exe)} >/dev/null 2>&1"], check=False).returncode != 0:
            raise SupervisorError(f"Required executable not found on PATH: {exe}")


def ensure_repo_files(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "runtime", "prompts", "scopes"):
        (config.state_dir / name).mkdir(parents=True, exist_ok=True)

    if not config.goal_file.exists():
        raise SupervisorError(
            f"Goal file not found: {config.goal_file}. Create it before running the supervisor."
        )

    plan_path = config.repo_path / "PLAN.md"
    if not plan_path.exists():
        plan_path.write_text(
            textwrap.dedent(
                """\
                # High-Level Plan

                ## Global objective
                - Fill this in from `GOAL.md`.

                ## Strategy
                - [ ] Inspect the theorem target, imports, and surrounding files.
                - [ ] Identify the next dependency chain of intermediate lemmas.
                - [ ] Work the dependency chain from the top down.
                - [ ] Close the main theorem and clean up.

                ## Notes
                - Keep this file high-level and durable across the run.
                """
            ),
            encoding="utf-8",
        )

    tasks_path = config.repo_path / "TASKS.md"
    if not tasks_path.exists():
        tasks_path.write_text(
            textwrap.dedent(
                """\
                # Tasks

                - [ ] Review `GOAL.md`, `PLAN.md`, and the current Lean files.
                - [ ] Create the first concrete subgoals.

                ## Completed
                - [ ] Move completed items here or check them off in place.
                """
            ),
            encoding="utf-8",
        )


def role_scope_dir(config: Config, provider: str, role: str) -> Path:
    scope = config.state_dir / "scopes" / f"{provider}-{role}"
    scope.mkdir(parents=True, exist_ok=True)
    links = {
        "repo": config.repo_path,
        "supervisor": config.state_dir,
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
    return scope


def tmux_cmd(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], text=True, capture_output=True, check=check)


def ensure_tmux_session(config: Config) -> None:
    session = config.tmux.session_name
    if tmux_cmd("has-session", "-t", session, check=False).returncode == 0:
        return
    dashboard_cmd = (
        f"bash -lc {shlex.quote(f'cd {config.repo_path} && echo Agent tmux session ready: {session} && exec bash')}"
    )
    tmux_cmd("new-session", "-d", "-s", session, "-n", config.tmux.dashboard_window_name, dashboard_cmd)


def read_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8", errors="replace")


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n\n...[truncated]...\n\n" + text[-half:]


def render_template(name: str, **kwargs: Any) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").format(**kwargs)


class ProviderAdapter:
    def __init__(self, cfg: ProviderConfig, role: str, config: Config, state: Dict[str, Any]):
        self.cfg = cfg
        self.role = role
        self.config = config
        self.state = state

    def role_state(self) -> Dict[str, Any]:
        return self.state.setdefault("roles", {}).setdefault(self.role, {})

    def scope_dir(self) -> Path:
        return role_scope_dir(self.config, self.cfg.provider, self.role)

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
    def _common_flags(self) -> List[str]:
        flags: List[str] = [
            "--skip-git-repo-check",
            "--ask-for-approval",
            "never",
            "--sandbox",
            "danger-full-access",
            "--color",
            "always",
        ]
        if self.cfg.model:
            flags += ["--model", self.cfg.model]
        flags += self.cfg.extra_args
        return flags

    def build_initial_command(self) -> List[str]:
        return ["codex", "exec", *self._common_flags(), PROMPT_TOKEN]

    def build_continue_command(self) -> List[str]:
        return ["codex", "exec", "resume", "--last", *self._common_flags(), PROMPT_TOKEN]


class GeminiAdapter(ProviderAdapter):
    def _base(self) -> List[str]:
        cmd = ["gemini", "--approval-mode=yolo"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        cmd += self.cfg.extra_args
        return cmd

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
    return state


def save_state(config: Config, state: Dict[str, Any]) -> None:
    JsonFile.dump(config.state_dir / "state.json", state)


def build_initial_worker_prompt(config: Config) -> str:
    goal_text = read_text(config.goal_file).strip()
    return render_template(
        "worker_initial.txt",
        goal_text=goal_text,
        handoff_path="supervisor/worker_handoff.json",
        plan_path="repo/PLAN.md",
        tasks_path="repo/TASKS.md",
        repo_label="repo/",
    )


def build_continue_worker_prompt(config: Config, state: Dict[str, Any]) -> str:
    last_review = state.get("last_review") or {}
    return render_template(
        "worker_continue.txt",
        review_reason=(last_review.get("reason") or "No reason supplied.").strip(),
        next_prompt=(last_review.get("next_prompt") or "Continue from the current frontier.").strip(),
        handoff_path="supervisor/worker_handoff.json",
        plan_path="repo/PLAN.md",
        tasks_path="repo/TASKS.md",
        repo_label="repo/",
        plan_text=trim_text(read_text(config.repo_path / "PLAN.md"), 12000),
        tasks_text=trim_text(read_text(config.repo_path / "TASKS.md"), 12000),
    )


def build_initial_reviewer_prompt(config: Config, worker_terminal_output: str, worker_handoff_text: str) -> str:
    goal_text = read_text(config.goal_file).strip()
    return render_template(
        "reviewer_initial.txt",
        goal_text=goal_text,
        worker_output=trim_text(worker_terminal_output, 18000),
        worker_handoff_text=worker_handoff_text,
        decision_path="supervisor/review_decision.json",
        worker_handoff_path="supervisor/worker_handoff.json",
        plan_path="repo/PLAN.md",
        tasks_path="repo/TASKS.md",
        plan_text=trim_text(read_text(config.repo_path / "PLAN.md"), 12000),
        tasks_text=trim_text(read_text(config.repo_path / "TASKS.md"), 12000),
    )


def build_continue_reviewer_prompt(config: Config, state: Dict[str, Any], worker_terminal_output: str, worker_handoff_text: str) -> str:
    goal_text = read_text(config.goal_file).strip()
    recent_reviews = state.get("review_log", [])[-3:]
    return render_template(
        "reviewer_continue.txt",
        goal_text=goal_text,
        worker_output=trim_text(worker_terminal_output, 18000),
        worker_handoff_text=worker_handoff_text,
        decision_path="supervisor/review_decision.json",
        worker_handoff_path="supervisor/worker_handoff.json",
        plan_path="repo/PLAN.md",
        tasks_path="repo/TASKS.md",
        plan_text=trim_text(read_text(config.repo_path / "PLAN.md"), 12000),
        tasks_text=trim_text(read_text(config.repo_path / "TASKS.md"), 12000),
        recent_reviews_text=json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]",
    )


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_burst_script(
    adapter: ProviderAdapter,
    cycle: int,
    prompt_file: Path,
    token: str,
    exit_file: Path,
) -> Path:
    runtime_dir = adapter.config.state_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    script_path = runtime_dir / f"{adapter.role}-cycle-{cycle:04d}.sh"
    scope_dir = adapter.scope_dir()

    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        f"EXIT_FILE={shlex.quote(str(exit_file))}",
        f"TOKEN={shlex.quote(token)}",
        f"PROMPT_FILE={shlex.quote(str(prompt_file))}",
        f"SCOPE_DIR={shlex.quote(str(scope_dir))}",
        "cleanup() {",
        "  ec=$?",
        "  printf '%s\n' \"$ec\" > \"$EXIT_FILE\"",
        "  tmux wait-for -S \"$TOKEN\" >/dev/null 2>&1 || true",
        "  exit \"$ec\"",
        "}",
        "trap cleanup EXIT",
        "cd \"$SCOPE_DIR\"",
        "PROMPT_CONTENT=$(cat \"$PROMPT_FILE\")",
        f"echo '[agent-burst] role={adapter.role} provider={adapter.cfg.provider} cwd='\"$PWD\"",
        "echo '[agent-burst] start='$(date -Is)",
        "cmd=(",
    ]
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


def launch_tmux_burst(adapter: ProviderAdapter, cycle: int, prompt: str) -> Dict[str, Any]:
    state_dir = adapter.config.state_dir
    prompts_dir = state_dir / "prompts"
    logs_dir = state_dir / "logs"
    runtime_dir = state_dir / "runtime"
    prompt_file = prompts_dir / f"{adapter.role}-cycle-{cycle:04d}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    if adapter.role == "worker":
        artifact_path = state_dir / "worker_handoff.json"
    else:
        artifact_path = state_dir / "review_decision.json"
    artifact_path.unlink(missing_ok=True)  # type: ignore[arg-type]

    exit_file = runtime_dir / f"{adapter.role}-cycle-{cycle:04d}.exit"
    exit_file.unlink(missing_ok=True)  # type: ignore[arg-type]

    token = f"lean-agent-{adapter.role}-{cycle:04d}-{int(time.time() * 1000)}"
    script_path = build_burst_script(adapter, cycle, prompt_file, token, exit_file)

    per_cycle_log = logs_dir / f"{adapter.role}-cycle-{cycle:04d}.ansi.log"
    aggregate_log = logs_dir / f"{adapter.role}.all.ansi.log"
    latest_log = logs_dir / f"{adapter.role}.latest.ansi.log"

    header = (
        f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | role={adapter.role} provider={adapter.cfg.provider} "
        f"scope={adapter.scope_dir()} =====\n$ {script_path}\n\n"
    )
    write_log_header(per_cycle_log, header)
    write_log_header(aggregate_log, header)

    session = adapter.config.tmux.session_name
    window_name = f"{adapter.role}-{cycle:04d}"
    proc = tmux_cmd("new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}", "-t", session, "-n", window_name)
    window_id, pane_id = proc.stdout.strip().split()

    tmux_cmd("set-window-option", "-t", window_id, "remain-on-exit", "on")
    pipe_cmd = f"bash -lc {shlex.quote(f'cat | tee -a {aggregate_log} >> {per_cycle_log}') }"
    tmux_cmd("pipe-pane", "-o", "-t", pane_id, pipe_cmd)
    launch_cmd = f"bash -lc {shlex.quote(str(script_path))}; exit"
    tmux_cmd("send-keys", "-t", pane_id, launch_cmd, "C-m")
    tmux_cmd("select-window", "-t", window_id)

    print(f"tmux_session={session} window={window_name} pane={pane_id}")
    print(f"Attach with: tmux attach -t {session}")
    tmux_cmd("wait-for", token)

    # Let tmux flush pane output to pipe-pane target.
    time.sleep(0.3)
    capture = tmux_cmd("capture-pane", "-p", "-t", pane_id, "-S", "-2000", check=False)
    captured_text = capture.stdout if capture.returncode == 0 else ""
    latest_log.write_text(read_text(per_cycle_log), encoding="utf-8")

    exit_code_text = read_text(exit_file).strip()
    if not exit_code_text:
        raise SupervisorError(f"Missing exit code file for {adapter.role}: {exit_file}")
    exit_code = int(exit_code_text)

    if adapter.config.tmux.kill_windows_after_capture:
        tmux_cmd("kill-window", "-t", window_id, check=False)

    return {
        "captured_output": captured_text,
        "artifact_path": artifact_path,
        "per_cycle_log": per_cycle_log,
        "exit_code": exit_code,
        "pane_id": pane_id,
        "window_id": window_id,
    }


def parse_json_object_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SupervisorError(f"Expected JSON artifact not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SupervisorError(f"Could not parse JSON artifact {path}: {exc}") from exc


def extract_json_object(text: str) -> Dict[str, Any]:
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = fenced + re.findall(r"(\{.*\})", text, flags=re.DOTALL)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise SupervisorError("Could not parse JSON object from captured text")


def load_json_artifact_with_fallback(path: Path, captured_text: str, required_key: str) -> Dict[str, Any]:
    if path.exists():
        data = parse_json_object_file(path)
    else:
        data = extract_json_object(captured_text)
    if required_key not in data:
        raise SupervisorError(f"Artifact missing required key {required_key!r}: {path}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Lean formalization worker/reviewer supervisor")
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    config = load_config(Path(args.config).expanduser().resolve())
    check_dependencies()
    ensure_repo_files(config)
    ensure_tmux_session(config)
    state = load_state(config)

    worker = make_adapter("worker", config, state)
    reviewer = make_adapter("reviewer", config, state)

    print(f"repo_path={config.repo_path}")
    print(f"goal_file={config.goal_file}")
    print(f"state_dir={config.state_dir}")
    print(f"worker={config.worker.provider} reviewer={config.reviewer.provider}")
    print(f"tmux_session={config.tmux.session_name}")

    while True:
        state["cycle"] = int(state.get("cycle", 0)) + 1
        cycle = state["cycle"]
        save_state(config, state)

        if config.max_cycles and cycle > config.max_cycles:
            print(f"Reached max_cycles={config.max_cycles}; stopping.")
            break

        print(f"\n===== cycle {cycle}: worker =====")
        worker_prompt = build_initial_worker_prompt(config) if worker.needs_initial_run() else build_continue_worker_prompt(config, state)
        worker_run = launch_tmux_burst(worker, cycle, worker_prompt)
        if worker_run["exit_code"] != 0:
            raise SupervisorError(f"Worker process exited with code {worker_run['exit_code']}. See {worker_run['per_cycle_log']}")
        worker.mark_initialized()
        worker_terminal_output = worker_run["captured_output"].strip()
        worker_handoff = load_json_artifact_with_fallback(Path(worker_run["artifact_path"]), worker_terminal_output, "vibe_check")
        state["last_worker_output"] = worker_terminal_output
        state["last_worker_handoff"] = worker_handoff
        save_state(config, state)

        print(f"\n===== cycle {cycle}: reviewer =====")
        worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
        reviewer_prompt = (
            build_initial_reviewer_prompt(config, worker_terminal_output, worker_handoff_text)
            if reviewer.needs_initial_run()
            else build_continue_reviewer_prompt(config, state, worker_terminal_output, worker_handoff_text)
        )
        reviewer_run = launch_tmux_burst(reviewer, cycle, reviewer_prompt)
        if reviewer_run["exit_code"] != 0:
            raise SupervisorError(f"Reviewer process exited with code {reviewer_run['exit_code']}. See {reviewer_run['per_cycle_log']}")
        reviewer.mark_initialized()
        reviewer_terminal_output = reviewer_run["captured_output"].strip()
        decision = load_json_artifact_with_fallback(Path(reviewer_run["artifact_path"]), reviewer_terminal_output, "decision")
        decision["cycle"] = cycle
        state["last_review"] = decision
        state.setdefault("review_log", []).append(decision)
        save_state(config, state)
        append_jsonl(config.state_dir / "review_log.jsonl", decision)

        print("\n===== reviewer decision =====")
        print(json.dumps(decision, indent=2, ensure_ascii=False))

        decision_value = str(decision.get("decision", "")).strip().upper()
        if decision_value in {"DONE", "STUCK"}:
            print(f"Stopping because reviewer returned {decision_value}.")
            break
        if decision_value != "CONTINUE":
            raise SupervisorError(f"Invalid reviewer decision: {decision_value}")

        time.sleep(config.sleep_seconds)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SupervisorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
