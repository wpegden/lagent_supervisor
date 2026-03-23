#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
CHAT_VIEWER_DIR = PACKAGE_DIR / "chat_viewer"
PROVIDER_CONTEXT_DIR = PACKAGE_DIR / "provider_context"
PROMPT_TOKEN = "__PROMPT__"
DEFAULT_CHAT_BASE_URL = "https://packer.math.cmu.edu/lagent-chats/"
PHASES: Tuple[str, ...] = (
    "paper_check",
    "planning",
    "theorem_stating",
    "proof_formalization",
)
WORKER_STATUSES: Tuple[str, ...] = ("NOT_STUCK", "STUCK", "DONE", "NEED_INPUT")
REVIEWER_DECISIONS: Tuple[str, ...] = ("CONTINUE", "ADVANCE_PHASE", "STUCK", "NEED_INPUT", "DONE")
SORRY_MODES: Tuple[str, ...] = ("default", "allowed")
SUPERVISOR_TASKS_START = "<!-- SUPERVISOR_TASKS:START -->"
SUPERVISOR_TASKS_END = "<!-- SUPERVISOR_TASKS:END -->"
SUPERVISOR_GITIGNORE_START = "# >>> lagent-supervisor >>>"
SUPERVISOR_GITIGNORE_END = "# <<< lagent-supervisor <<<"


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
    public_base_url: str


@dataclass
class GitConfig:
    remote_url: Optional[str]
    remote_name: str
    branch: str
    author_name: str
    author_email: str


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
    )


def check_dependencies(config: Config) -> None:
    required = ["tmux"]
    if config.git.remote_url:
        required.append("git")
    for exe in required:
        if subprocess.run(["bash", "-lc", f"command -v {shlex.quote(exe)} >/dev/null 2>&1"], check=False).returncode != 0:
            raise SupervisorError(f"Required executable not found on PATH: {exe}")


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
    }


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
    if not meta_path.exists():
        meta = default_chat_meta(config)
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
    state.setdefault("phase_history", [])
    state.setdefault("awaiting_human_input", False)
    current_phase(config, state)
    return state


def save_state(config: Config, state: Dict[str, Any]) -> None:
    JsonFile.dump(config.state_dir / "state.json", state)


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


def format_json_enum(values: Sequence[str]) -> str:
    return " | ".join(json.dumps(value) for value in values)


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


def phase_context_text(config: Config, state: Dict[str, Any], phase: str) -> str:
    parts = [
        f"Current phase: {phase}",
        f"Sorry mode: {config.workflow.sorry_mode}",
        f"Goal file: {relative_repo_label(config, config.goal_file)}",
        "Supervisor-managed files:",
        "- `repo/TASKS.md` always exists and is shared with the supervisor.",
    ]
    if config.workflow.paper_tex_path is not None:
        parts.append(f"- Paper tex: `{relative_repo_label(config, config.workflow.paper_tex_path)}`")
    if phase_uses_paper_notes(phase):
        parts.append("- `repo/PAPERNOTES.md` is where paper corrections and clarifications belong.")
    if phase_uses_plan(phase):
        parts.append("- `repo/PLAN.md` is the durable formalization roadmap.")
    if phase_uses_statement_files(phase):
        parts.append("- `repo/PaperDefinitions.lean` and `repo/PaperTheorems.lean` are the target statement files.")
    parts.append(f"- Approved axioms file: `{relative_repo_label(config, config.workflow.approved_axioms_path)}`")
    if git_is_enabled(config):
        parts.append(
            f"- Git remote: `{config.git.remote_name}` -> `{config.git.remote_url}` on branch `{current_git_branch(config)}`."
        )
        parts.append(f"- Push command when you made progress: `{git_push_command(config)}`")
    parts.append(f"- Validation summary file: `supervisor/{validation_summary_path(config).name}`")
    latest_validation = state.get("last_validation")
    if latest_validation:
        parts.append("Latest supervisor validation summary:")
        parts.append(trim_text(json.dumps(latest_validation, indent=2, ensure_ascii=False), 12000))
    else:
        parts.append("Latest supervisor validation summary: none yet.")
    human_input_text = trim_text(read_text(config.workflow.human_input_path).strip(), 6000)
    if human_input_text:
        parts.append(f"Latest human input from `{relative_repo_label(config, config.workflow.human_input_path)}`:")
        parts.append(human_input_text)
    approved = approved_axioms(config)
    parts.append(f"Approved axioms: {approved if approved else '[]'}")
    return "\n".join(parts)


def phase_worker_instructions(config: Config, phase: str) -> str:
    paper_label = relative_repo_label(config, config.workflow.paper_tex_path) if config.workflow.paper_tex_path else "the paper tex file"
    if phase == "paper_check":
        return textwrap.dedent(
            f"""\
            Phase objective: carefully read `{paper_label}` and mathematically verify the paper's proofs.

            Requirements:
            - Maintain `repo/TASKS.md`.
            - Maintain `repo/PAPERNOTES.md` with corrections, hidden assumptions, and proof clarifications.
            - Read the paper carefully enough to catch proof gaps or incorrect statements.
            - Report `STUCK` only if you find a genuine gap or incorrect statement, try to repair it seriously, and still cannot make the argument work.
            - Report `DONE` only when the whole paper has been checked and `PAPERNOTES.md` is up to date.
            """
        ).strip()
    if phase == "planning":
        return textwrap.dedent(
            f"""\
            Phase objective: create a high-level but comprehensive `repo/PLAN.md` for formalizing the main results of `{paper_label}`.

            Requirements:
            - Maintain `repo/TASKS.md`.
            - Maintain `repo/PAPERNOTES.md`.
            - Build `repo/PLAN.md` around statement prerequisites, reusable definitions, mathlib imports, and plausible proof roadmaps.
            - Use `NEED_INPUT` for external results, proposed axioms, or formalization design choices that genuinely need a human decision.
            - Never introduce axioms unless they are explicitly approved by a human and listed in the approved axioms file.
            """
        ).strip()
    if phase == "theorem_stating":
        return textwrap.dedent(
            f"""\
            Phase objective: create Lean files that state the paper's definitions and theorems as close to `{paper_label}` as possible.

            Requirements:
            - Maintain `repo/TASKS.md`, `repo/PAPERNOTES.md`, and `repo/PLAN.md`.
            - Create or update `repo/PaperDefinitions.lean` and `repo/PaperTheorems.lean`.
            - Keep the definitions and statements easy for a human to compare against the paper.
            - Make both files syntactically valid Lean.
            - Do not introduce unapproved axioms.
            - `DONE` means the statement files are in place and ready for reviewer comparison against the paper.
            """
        ).strip()
    sorry_policy = (
        "Default sorry policy: do not move on with extra sorrys anywhere outside `repo/PaperTheorems.lean`."
        if config.workflow.sorry_mode == "default"
        else "Sorrys-allowed mode: temporary extra sorrys are allowed, but you must drive the count down and remove them all by the end."
    )
    return textwrap.dedent(
        """\
        Phase objective: prove the target statements presented in `repo/PaperTheorems.lean`.

        Requirements:
        - Maintain `repo/TASKS.md` and `repo/PLAN.md`.
        - Keep `repo/PaperDefinitions.lean` and `repo/PaperTheorems.lean` as the paper-facing interface for definitions and theorem statements.
        - Prefer reusable lemmas, technical definitions, and proof infrastructure in separate support files when that yields a cleaner project structure.
        - It is fine for proofs in `repo/PaperTheorems.lean` to be short wrappers around results proved elsewhere in the repo.
        - Work toward zero sorrys and no unapproved axioms.
        - Keep the proof frontier concrete in `TASKS.md`.
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
        context_path = "GEMINI.md"
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


def build_worker_prompt(config: Config, state: Dict[str, Any], phase: str, is_initial: bool) -> str:
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
    provider_notes = provider_context_worker_instructions(config)
    git_notes = git_worker_instructions(config)
    return textwrap.dedent(
        f"""\
        You are the main formalization worker.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase)}

        {review_guidance}{provider_notes}
        {phase_worker_instructions(config, phase)}
        {git_notes}

        Before ending this turn:
        - write your handoff JSON to `supervisor/worker_handoff.json`
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
    return textwrap.dedent(
        f"""\
        You are the review agent supervising the worker.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase)}

        Recent reviewer decisions:
        {json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]"}

        Worker handoff JSON from `supervisor/worker_handoff.json`:
        {worker_handoff_text}

        Supervisor validation summary from `supervisor/{validation_summary_path(config).name}`:
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 16000)}

        Worker's latest terminal output:
        {terminal_section}

        {phase_reviewer_instructions(config, phase)}

        Before ending this turn:
        - write your decision JSON to `supervisor/review_decision.json`
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

    meta = JsonFile.load(chat_repo_meta_path(config), default_chat_meta(config))
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
    if kind == "worker_handoff" and isinstance(content, dict):
        meta["last_worker_status"] = content.get("status")
    if kind == "reviewer_decision" and isinstance(content, dict):
        meta["last_reviewer_decision"] = content.get("decision")
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
) -> Path:
    runtime_dir = adapter.config.state_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    script_path = runtime_dir / f"{adapter.role}-cycle-{cycle:04d}.sh"
    scope_dir = adapter.scope_dir()

    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        f"START_FILE={shlex.quote(str(start_file))}",
        f"EXIT_FILE={shlex.quote(str(exit_file))}",
        f"PROMPT_FILE={shlex.quote(str(prompt_file))}",
        f"SCOPE_DIR={shlex.quote(str(scope_dir))}",
        "cleanup() {",
        "  ec=$?",
        "  printf '%s\n' \"$ec\" > \"$EXIT_FILE\"",
        "  exit \"$ec\"",
        "}",
        "trap cleanup EXIT",
        "cd \"$SCOPE_DIR\"",
        "printf '%s\n' \"$(date -Is)\" > \"$START_FILE\"",
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
) -> None:
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    pane_exit_grace_seconds = 1.0
    while True:
        if path.exists():
            return
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

    start_file = runtime_dir / f"{adapter.role}-cycle-{cycle:04d}.started"
    start_file.unlink(missing_ok=True)  # type: ignore[arg-type]
    exit_file = runtime_dir / f"{adapter.role}-cycle-{cycle:04d}.exit"
    exit_file.unlink(missing_ok=True)  # type: ignore[arg-type]

    script_path = build_burst_script(adapter, cycle, prompt_file, start_file, exit_file)

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
    pipe_inner_cmd = (
        f"cat | tee -a {shlex.quote(str(aggregate_log))} >> {shlex.quote(str(per_cycle_log))}"
    )
    pipe_cmd = shlex.join(["bash", "-lc", pipe_inner_cmd])
    tmux_cmd("pipe-pane", "-o", "-t", pane_id, pipe_cmd)
    launch_cmd = f"{shlex.quote(str(script_path))}; exit"
    tmux_cmd("send-keys", "-t", pane_id, launch_cmd, "C-m")
    tmux_cmd("select-window", "-t", window_id)

    print(f"tmux_session={session} window={window_name} pane={pane_id}")
    print(f"Attach with: tmux attach -t {session}")
    captured_text = ""
    completed = False
    try:
        wait_for_path(
            start_file,
            pane_id,
            adapter.config.startup_timeout_seconds,
            role=adapter.role,
            state_name="startup marker",
            log_path=per_cycle_log,
        )
        wait_for_path(
            exit_file,
            pane_id,
            adapter.config.burst_timeout_seconds,
            role=adapter.role,
            state_name="exit marker",
            log_path=per_cycle_log,
        )
        completed = True
    finally:
        # Let tmux flush pane output to pipe-pane target.
        time.sleep(0.3)
        capture = tmux_cmd("capture-pane", "-p", "-t", pane_id, "-S", "-2000", check=False)
        captured_text = capture.stdout if capture.returncode == 0 else ""
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


def extract_json_object(text: str, required_key: Optional[str] = None) -> Dict[str, Any]:
    candidates = extract_json_objects(text)
    if required_key is not None:
        candidates = [candidate for candidate in candidates if required_key in candidate]
    if candidates:
        return candidates[-1]
    raise SupervisorError("Could not parse JSON object from captured text")


def load_json_artifact_with_fallback(path: Path, captured_text: str, required_key: str) -> Dict[str, Any]:
    errors: List[str] = []
    if path.exists():
        try:
            data = parse_json_object_file(path)
            if required_key in data:
                return data
            errors.append(f"Artifact missing required key {required_key!r}: {path}")
        except SupervisorError as exc:
            errors.append(str(exc))
    try:
        return extract_json_object(captured_text, required_key=required_key)
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
    phase = current_phase(config, state)
    ensure_repo_files(config, phase)
    ensure_chat_site(config)
    ensure_tmux_session(config)

    if not maybe_consume_human_input(config, state):
        print(f"Waiting for human input in: {config.workflow.human_input_path}")
        print(f"Input request written to: {config.workflow.input_request_path}")
        return 0

    if state.get("pending_human_input_event"):
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

    worker = make_adapter("worker", config, state)
    reviewer = make_adapter("reviewer", config, state)

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

    while True:
        phase = current_phase(config, state)
        ensure_repo_files(config, phase)
        state["cycle"] = int(state.get("cycle", 0)) + 1
        cycle = state["cycle"]
        save_state(config, state)

        if config.max_cycles and cycle > config.max_cycles:
            print(f"Reached max_cycles={config.max_cycles}; stopping.")
            break

        print(f"\n===== cycle {cycle}: worker | phase={phase} =====")
        worker_prompt = build_worker_prompt(config, state, phase, worker.needs_initial_run())
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
        worker_run = launch_tmux_burst(worker, cycle, worker_prompt)
        if worker_run["exit_code"] != 0:
            raise SupervisorError(f"Worker process exited with code {worker_run['exit_code']}. See {worker_run['per_cycle_log']}")
        worker.mark_initialized()
        worker_terminal_output = worker_run["captured_output"].strip()
        worker_handoff = load_json_artifact_with_fallback(Path(worker_run["artifact_path"]), worker_terminal_output, "status")
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
        reviewer_run = launch_tmux_burst(reviewer, cycle, reviewer_prompt)
        if reviewer_run["exit_code"] != 0:
            raise SupervisorError(f"Reviewer process exited with code {reviewer_run['exit_code']}. See {reviewer_run['per_cycle_log']}")
        reviewer.mark_initialized()
        reviewer_terminal_output = reviewer_run["captured_output"].strip()
        decision = load_json_artifact_with_fallback(Path(reviewer_run["artifact_path"]), reviewer_terminal_output, "decision")
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
            time.sleep(config.sleep_seconds)
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
        if decision_value in {"DONE", "STUCK"}:
            print(f"Stopping because reviewer returned {decision_value}.")
            break

        time.sleep(config.sleep_seconds)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SupervisorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
