#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import supervisor


DEFAULT_WORKER = {
    "provider": "codex",
    "model": "gpt-5.4",
    "extra_args": ["--config", 'model_reasoning_effort="xhigh"'],
}

DEFAULT_REVIEWER = {
    "provider": "claude",
    "model": "opus",
    "extra_args": ["--effort", "max"],
}


class InitError(RuntimeError):
    pass


@dataclass
class InitSpec:
    repo_path: Path
    remote_url: Optional[str]
    paper_source: Path
    paper_dest_rel: Path
    config_path: Path
    package_name: str
    goal_file_name: str
    branch: str
    author_name: str
    author_email: str
    max_cycles: int
    session_name: str
    kill_windows_after_capture: bool
    worker_provider: str
    reviewer_provider: str


def prompt_text(label: str, default: Optional[str] = None, *, required: bool = True) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""
        print("A value is required.")


def prompt_yes_no(label: str, default: bool) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        value = input(f"{label}{suffix}: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.")


def run_checked(args: Sequence[str], *, cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, check=True)


def run_capture(args: Sequence[str], *, cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def repo_name_to_package_name(repo_name: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", repo_name) if part]
    if not parts:
        return "PaperFormalization"
    return "".join(part[:1].upper() + part[1:] for part in parts)


def is_explicit_release_toolchain(text: str) -> bool:
    return bool(re.fullmatch(r"leanprover/lean4:v\d+\.\d+\.\d+(?:[-A-Za-z0-9.]*)?", text.strip()))


def parse_active_release_toolchain(elan_show_output: str) -> Optional[str]:
    active = False
    for line in elan_show_output.splitlines():
        stripped = line.strip()
        if stripped == "active toolchain":
            active = True
            continue
        if not active or not stripped:
            continue
        token = stripped.split()[0]
        if is_explicit_release_toolchain(token):
            return token
    return None


def detect_explicit_lean_toolchain() -> Optional[str]:
    show = run_capture(["elan", "show"])
    if show.returncode == 0:
        parsed = parse_active_release_toolchain(show.stdout)
        if parsed:
            return parsed
    listed = run_capture(["elan", "toolchain", "list"])
    if listed.returncode == 0:
        for line in listed.stdout.splitlines():
            token = line.strip().split()[0] if line.strip() else ""
            if is_explicit_release_toolchain(token):
                return token
    return None


def ensure_repo_git(repo_path: Path, branch: str, author_name: str, author_email: str) -> None:
    if run_capture(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_path).returncode != 0:
        run_checked(["git", "init", "-b", branch], cwd=repo_path)
    if not run_capture(["git", "config", "--get", "user.name"], cwd=repo_path).stdout.strip():
        run_checked(["git", "config", "user.name", author_name], cwd=repo_path)
    if not run_capture(["git", "config", "--get", "user.email"], cwd=repo_path).stdout.strip():
        run_checked(["git", "config", "user.email", author_email], cwd=repo_path)


def ensure_remote(repo_path: Path, remote_name: str, remote_url: Optional[str]) -> None:
    if not remote_url:
        return
    existing = run_capture(["git", "remote", "get-url", remote_name], cwd=repo_path)
    if existing.returncode != 0:
        run_checked(["git", "remote", "add", remote_name, remote_url], cwd=repo_path)
        return
    current = existing.stdout.strip()
    if current != remote_url:
        raise InitError(f"Remote {remote_name!r} already points to {current!r}, expected {remote_url!r}.")


def has_lake_project(repo_path: Path) -> bool:
    return any((repo_path / name).exists() for name in ("lakefile.lean", "lakefile.toml", "lake-manifest.json"))


def ensure_lake_project(repo_path: Path, package_name: str) -> None:
    if has_lake_project(repo_path):
        return
    run_checked(["lake", "init", package_name, "math"], cwd=repo_path)


def ensure_explicit_repo_toolchain(repo_path: Path) -> Optional[str]:
    toolchain = detect_explicit_lean_toolchain()
    if not toolchain:
        return None
    path = repo_path / "lean-toolchain"
    current = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if current == toolchain:
        return toolchain
    if current and is_explicit_release_toolchain(current):
        return current
    path.write_text(toolchain + "\n", encoding="utf-8")
    run_checked(["lake", "update"], cwd=repo_path)
    return toolchain


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def copy_paper_into_repo(repo_path: Path, paper_source: Path, paper_dest_rel: Path) -> Path:
    destination = (repo_path / paper_dest_rel).resolve()
    destination.relative_to(repo_path)
    ensure_parent(destination)
    shutil.copyfile(paper_source, destination)
    return destination


def write_goal_file(path: Path, paper_rel: Path) -> None:
    text = textwrap.dedent(
        f"""\
        Formalize the paper in `{paper_rel.as_posix()}`.

        Follow the supervisor workflow:
        1. check the paper mathematically and record issues in `PAPERNOTES.md`;
        2. create a comprehensive `PLAN.md`;
        3. write `PaperDefinitions.lean` and `PaperTheorems.lean` so they match the paper closely;
        4. prove the statements in Lean with no unapproved axioms.
        """
    )
    path.write_text(text, encoding="utf-8")


def build_config_json(spec: InitSpec) -> dict[str, Any]:
    return {
        "repo_path": str(spec.repo_path),
        "goal_file": spec.goal_file_name,
        "state_dir": ".agent-supervisor",
        "max_cycles": spec.max_cycles,
        "sleep_seconds": 1.0,
        "startup_timeout_seconds": 30.0,
        "burst_timeout_seconds": 7200.0,
        "workflow": {
            "start_phase": "paper_check",
            "paper_tex_path": spec.paper_dest_rel.as_posix(),
            "sorry_mode": "default",
            "approved_axioms_path": "APPROVED_AXIOMS.json",
            "human_input_path": "HUMAN_INPUT.md",
            "input_request_path": "INPUT_REQUEST.md",
        },
        "git": {
            "remote_url": spec.remote_url,
            "remote_name": "origin",
            "branch": spec.branch,
            "author_name": spec.author_name,
            "author_email": spec.author_email,
        },
        "tmux": {
            "session_name": spec.session_name,
            "dashboard_window_name": "dashboard",
            "kill_windows_after_capture": spec.kill_windows_after_capture,
        },
        "worker": dict(DEFAULT_WORKER),
        "reviewer": dict(DEFAULT_REVIEWER),
    }


def write_config(path: Path, data: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git_has_changes(repo_path: Path) -> bool:
    return bool(run_capture(["git", "status", "--short"], cwd=repo_path).stdout.strip())


def git_has_commits(repo_path: Path) -> bool:
    return run_capture(["git", "rev-parse", "--verify", "HEAD"], cwd=repo_path).returncode == 0


def maybe_create_initial_commit(repo_path: Path, branch: str, remote_url: Optional[str]) -> Optional[str]:
    if not git_has_changes(repo_path) and git_has_commits(repo_path):
        return None
    run_checked(["git", "add", "."], cwd=repo_path)
    commit = run_capture(["git", "commit", "-m", "Initial scaffold for paper formalization"], cwd=repo_path)
    if commit.returncode != 0:
        output = (commit.stdout + commit.stderr).strip()
        if "nothing to commit" not in output.lower():
            raise InitError(f"Initial commit failed:\n{output}")
    if remote_url:
        push = run_capture(["git", "push", "-u", "origin", branch], cwd=repo_path)
        if push.returncode != 0:
            output = (push.stdout + push.stderr).strip()
            return output or "git push failed"
    return None


def default_paths(paper_source: Path) -> tuple[Path, Path]:
    repo_name = supervisor.sanitize_repo_name(paper_source.stem)
    return (
        (Path.home() / "math" / repo_name).resolve(),
        (SCRIPT_DIR.parent / "configs" / f"{repo_name}.json").resolve(),
    )


def gather_spec(args: argparse.Namespace) -> InitSpec:
    paper_default = args.paper_source or ""
    paper_source_text = prompt_text("Paper .tex path", paper_default)
    paper_source = Path(paper_source_text).expanduser().resolve()
    if not paper_source.exists():
        raise InitError(f"Paper source not found: {paper_source}")

    repo_default, config_default = default_paths(paper_source)
    repo_path = Path(prompt_text("Working repo path", args.repo_path or str(repo_default))).expanduser().resolve()
    config_path = Path(prompt_text("Supervisor config path", args.config_path or str(config_default))).expanduser().resolve()
    remote_url = prompt_text("Git remote URL (leave blank for none)", args.remote_url or "", required=False).strip() or None

    repo_name = supervisor.sanitize_repo_name(repo_path.name)
    package_default = repo_name_to_package_name(repo_name)
    package_name = prompt_text("Lean package name", args.package_name or package_default)
    paper_dest_default = f"paper/{paper_source.name}"
    paper_dest_rel = Path(prompt_text("Paper path inside repo", args.paper_dest or paper_dest_default))
    goal_file_name = prompt_text("Goal file name", args.goal_file or "GOAL.md")
    max_cycles = int(prompt_text("Initial max_cycles", str(args.max_cycles or 3)))
    session_default = f"{repo_name}-agents"
    session_name = prompt_text("Agent tmux session name", args.session_name or session_default)

    return InitSpec(
        repo_path=repo_path,
        remote_url=remote_url,
        paper_source=paper_source,
        paper_dest_rel=paper_dest_rel,
        config_path=config_path,
        package_name=package_name,
        goal_file_name=goal_file_name,
        branch="main",
        author_name=args.author_name or "leanagent",
        author_email=args.author_email or "leanagent@packer.math.cmu.edu",
        max_cycles=max_cycles,
        session_name=session_name,
        kill_windows_after_capture=False if args.kill_windows_after_capture is None else args.kill_windows_after_capture,
        worker_provider="codex",
        reviewer_provider="claude",
    )


def bootstrap_project(spec: InitSpec, *, create_commit: bool) -> dict[str, Any]:
    spec.repo_path.mkdir(parents=True, exist_ok=True)
    ensure_repo_git(spec.repo_path, spec.branch, spec.author_name, spec.author_email)
    ensure_remote(spec.repo_path, "origin", spec.remote_url)
    ensure_lake_project(spec.repo_path, spec.package_name)
    pinned_toolchain = ensure_explicit_repo_toolchain(spec.repo_path)
    paper_dest = copy_paper_into_repo(spec.repo_path, spec.paper_source, spec.paper_dest_rel)

    goal_path = spec.repo_path / spec.goal_file_name
    write_goal_file(goal_path, spec.paper_dest_rel)

    config = build_config_json(spec)
    write_config(spec.config_path, config)

    push_error = maybe_create_initial_commit(spec.repo_path, spec.branch, spec.remote_url) if create_commit else None
    return {
        "goal_path": goal_path,
        "paper_dest": paper_dest,
        "config_path": spec.config_path,
        "repo_path": spec.repo_path,
        "pinned_toolchain": pinned_toolchain,
        "push_error": push_error,
        "agent_session": spec.session_name,
        "supervisor_session": f"{supervisor.sanitize_repo_name(spec.repo_path.name)}-supervisor",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactively initialize a Lean paper-formalization project.")
    parser.add_argument("--paper-source", help="Source .tex file to copy into the repo")
    parser.add_argument("--repo-path", help="Working repo path to create or reuse")
    parser.add_argument("--remote-url", help="Git remote URL to configure")
    parser.add_argument("--config-path", help="Supervisor config path to write")
    parser.add_argument("--package-name", help="Lean package name for lake init")
    parser.add_argument("--paper-dest", help="Paper path inside the repo, relative to repo_path")
    parser.add_argument("--goal-file", help="Goal file name relative to repo_path")
    parser.add_argument("--session-name", help="tmux session name for agent bursts")
    parser.add_argument("--author-name", help="Local git author name")
    parser.add_argument("--author-email", help="Local git author email")
    parser.add_argument("--max-cycles", type=int, help="Initial max_cycles to write into the config")
    parser.add_argument(
        "--kill-windows-after-capture",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Whether to kill finished burst windows after capture in the generated config",
    )
    parser.add_argument(
        "--no-initial-commit",
        action="store_true",
        help="Skip the initial git add/commit/push step.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        spec = gather_spec(args)
        print()
        print("Summary")
        print(f"- Repo: {spec.repo_path}")
        print(f"- Paper source: {spec.paper_source}")
        print(f"- Paper destination: {spec.paper_dest_rel}")
        print(f"- Config: {spec.config_path}")
        if spec.remote_url:
            print(f"- Git remote: {spec.remote_url}")
        print(f"- Worker/reviewer: {spec.worker_provider} / {spec.reviewer_provider}")
        print()
        if not prompt_yes_no("Proceed with initialization", True):
            print("Cancelled.")
            return 1

        result = bootstrap_project(spec, create_commit=not args.no_initial_commit)
    except (InitError, supervisor.SupervisorError, subprocess.CalledProcessError) as exc:
        print(f"Initialization failed: {exc}", file=sys.stderr)
        return 1

    print()
    print("Created")
    print(f"- Repo: {result['repo_path']}")
    print(f"- Goal file: {result['goal_path']}")
    print(f"- Paper copy: {result['paper_dest']}")
    print(f"- Config: {result['config_path']}")
    if result["pinned_toolchain"]:
        print(f"- Lean toolchain: {result['pinned_toolchain']}")
    print()
    if result["push_error"]:
        print("Git push did not complete cleanly:")
        print(result["push_error"])
        print()
    print("Next commands")
    print(f"cd {SCRIPT_DIR.parent}")
    print(
        f"./scripts/start_in_tmux.sh {shlex_quote(str(result['config_path']))} "
        f"{shlex_quote(str(result['supervisor_session']))}"
    )
    print(f"tmux attach -t {shlex_quote(str(result['agent_session']))}")
    return 0


def shlex_quote(text: str) -> str:
    return shlex.quote(text)


if __name__ == "__main__":
    raise SystemExit(main())
