#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import supervisor


PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "claude": {
        "provider": "claude",
        "model": "opus",
        "extra_args": ["--effort", "max"],
    },
    "codex": {
        "provider": "codex",
        "model": "gpt-5.4",
        "extra_args": ["--config", 'model_reasoning_effort="xhigh"'],
    },
    "gemini": {
        "provider": "gemini",
        "model": "gemini-3.1-pro-preview",
        "extra_args": [],
    },
}

DEFAULT_INIT_MAX_CYCLES = 150
ARXIV_EPRINT_BASE_URL = "https://export.arxiv.org/e-print"
ARXIV_NEW_STYLE_RE = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")
ARXIV_OLD_STYLE_RE = re.compile(r"[A-Za-z.-]+/\d{7}(?:v\d+)?")


class InitError(RuntimeError):
    pass


@dataclass
class InitSpec:
    repo_path: Path
    remote_url: Optional[str]
    paper_source: Optional[Path]
    paper_arxiv_id: Optional[str]
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


def prompt_choice(label: str, choices: Sequence[str], default: str) -> str:
    allowed = [choice.strip().lower() for choice in choices]
    default_text = default.strip().lower()
    while True:
        value = prompt_text(label, default_text).strip().lower()
        if value in allowed:
            return value
        print(f"Please choose one of: {', '.join(allowed)}")


def run_checked(args: Sequence[str], *, cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, check=True)


def run_capture(args: Sequence[str], *, cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def normalize_arxiv_id(text: str) -> Optional[str]:
    candidate = text.strip()
    if not candidate:
        return None
    if candidate.lower().startswith("arxiv:"):
        candidate = candidate[6:].strip()
    if ARXIV_NEW_STYLE_RE.fullmatch(candidate):
        return candidate
    if ARXIV_OLD_STYLE_RE.fullmatch(candidate):
        return candidate
    return None


def arxiv_source_stem(arxiv_id: str) -> str:
    return f"arxiv-{supervisor.sanitize_repo_name(arxiv_id.replace('/', '-'))}"


def source_label(paper_source: Optional[Path], paper_arxiv_id: Optional[str]) -> str:
    if paper_source is not None:
        return str(paper_source)
    if paper_arxiv_id is not None:
        return f"arXiv:{paper_arxiv_id}"
    raise InitError("Either paper_source or paper_arxiv_id must be set.")


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


def lake_command(toolchain: Optional[str] = None) -> list[str]:
    command = ["lake"]
    if toolchain:
        command.append(f"+{toolchain}")
    return command


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
    toolchain = detect_explicit_lean_toolchain()
    run_checked([*lake_command(toolchain), "init", package_name, "math"], cwd=repo_path)


def ensure_build_only_ci_workflow(repo_path: Path) -> Path:
    workflow_path = repo_path / ".github" / "workflows" / "lean_action_ci.yml"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(
        textwrap.dedent(
            """\
            name: Lean Action CI

            on:
              push:
              pull_request:
              workflow_dispatch:

            jobs:
              build:
                runs-on: ubuntu-latest

                steps:
                  - uses: actions/checkout@v5
                  - uses: leanprover/lean-action@v1
            """
        ),
        encoding="utf-8",
    )
    return workflow_path


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


def decode_source_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_tar_bytes(data: bytes, destination_dir: Path) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
            if not members:
                return False
            root = destination_dir.resolve()
            for member in members:
                target = (destination_dir / member.name).resolve()
                try:
                    target.relative_to(root)
                except ValueError as exc:
                    raise InitError(f"Refusing to extract suspicious arXiv source member: {member.name}") from exc
                ensure_parent(target)
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                target.write_bytes(extracted.read())
            return True
    except tarfile.TarError:
        return False


def materialize_downloaded_source(raw_bytes: bytes, destination_dir: Path) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)
    if extract_tar_bytes(raw_bytes, destination_dir):
        return
    try:
        decompressed = gzip.decompress(raw_bytes)
    except OSError:
        decompressed = None
    if decompressed is not None and extract_tar_bytes(decompressed, destination_dir):
        return
    payload = decompressed if decompressed is not None else raw_bytes
    (destination_dir / "source.tex").write_text(decode_source_bytes(payload), encoding="utf-8")


def split_latex_comment(line: str) -> tuple[str, str]:
    for index, char in enumerate(line):
        if char != "%":
            continue
        slash_count = 0
        cursor = index - 1
        while cursor >= 0 and line[cursor] == "\\":
            slash_count += 1
            cursor -= 1
        if slash_count % 2 == 0:
            return line[:index], line[index:]
    return line, ""


def read_latex_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def choose_main_tex_file(source_dir: Path) -> Path:
    tex_files = sorted(path for path in source_dir.rglob("*.tex") if path.is_file())
    if not tex_files:
        raise InitError(f"Downloaded arXiv source in {source_dir} does not contain any .tex files.")

    def score(path: Path) -> tuple[int, int, int, int, str]:
        text = read_latex_text(path)
        return (
            1 if "\\documentclass" in text else 0,
            1 if "\\begin{document}" in text else 0,
            1 if path.name.lower() in {"main.tex", "paper.tex", "ms.tex"} else 0,
            -len(path.relative_to(source_dir).parts),
            str(path),
        )

    return max(tex_files, key=score)


def resolve_include_path(current_file: Path, source_dir: Path, target: str) -> Optional[Path]:
    candidate = target.strip()
    if not candidate:
        return None
    candidate_path = Path(candidate)
    candidates = [candidate_path]
    if candidate_path.suffix == "":
        candidates.extend(
            [
                candidate_path.with_suffix(".tex"),
                candidate_path.with_suffix(".bbl"),
            ]
        )
    search_roots = [current_file.parent, source_dir]
    seen: set[Path] = set()
    for root in search_roots:
        for rel in candidates:
            resolved = (root / rel).resolve()
            if resolved in seen or not resolved.exists() or not resolved.is_file():
                continue
            seen.add(resolved)
            return resolved
    return None


def find_bbl_file(current_file: Path, source_dir: Path, main_tex: Path, bibliography_targets: Sequence[str]) -> Optional[Path]:
    explicit_targets = [item.strip() for item in bibliography_targets if item.strip()]
    for target in explicit_targets:
        resolved = resolve_include_path(current_file, source_dir, f"{target}.bbl")
        if resolved is not None:
            return resolved
    same_stem = main_tex.with_suffix(".bbl")
    if same_stem.exists():
        return same_stem
    bbl_files = sorted(path for path in source_dir.rglob("*.bbl") if path.is_file())
    if len(bbl_files) == 1:
        return bbl_files[0]
    return None


def flatten_latex_file(path: Path, source_dir: Path, main_tex: Path, stack: Optional[list[Path]] = None) -> str:
    stack = list(stack or [])
    resolved = path.resolve()
    if resolved in stack:
        cycle = " -> ".join(item.name for item in [*stack, resolved])
        raise InitError(f"Encountered recursive LaTeX include cycle: {cycle}")
    stack.append(resolved)

    input_re = re.compile(r"\\(input|include)\s*\{([^}]+)\}")
    bibliography_re = re.compile(r"\\bibliography\s*\{([^}]+)\}")
    lines: list[str] = []

    for line in read_latex_text(resolved).splitlines(keepends=True):
        code, comment = split_latex_comment(line)

        def include_repl(match: re.Match[str]) -> str:
            target = match.group(2).strip()
            include_path = resolve_include_path(resolved, source_dir, target)
            if include_path is None:
                return match.group(0)
            rel = include_path.relative_to(source_dir).as_posix()
            included_text = flatten_latex_file(include_path, source_dir, main_tex, stack)
            if not included_text.endswith("\n"):
                included_text += "\n"
            return f"\n% >>> begin included file: {rel}\n{included_text}% <<< end included file: {rel}\n"

        def bibliography_repl(match: re.Match[str]) -> str:
            targets = [item.strip() for item in match.group(1).split(",")]
            bbl_path = find_bbl_file(resolved, source_dir, main_tex, targets)
            if bbl_path is None:
                return match.group(0)
            rel = bbl_path.relative_to(source_dir).as_posix()
            bbl_text = read_latex_text(bbl_path)
            if not bbl_text.endswith("\n"):
                bbl_text += "\n"
            return f"\n% >>> begin bibliography from: {rel}\n{bbl_text}% <<< end bibliography from: {rel}\n"

        code = input_re.sub(include_repl, code)
        code = bibliography_re.sub(bibliography_repl, code)
        lines.append(code + comment)

    return "".join(lines)


def flatten_arxiv_source(source_dir: Path, arxiv_id: str) -> tuple[Path, str]:
    main_tex = choose_main_tex_file(source_dir)
    flattened = flatten_latex_file(main_tex, source_dir, main_tex)
    header = textwrap.dedent(
        f"""\
        % Flattened arXiv source for reference use.
        % arXiv identifier: {arxiv_id}
        % Original main tex file: {main_tex.relative_to(source_dir).as_posix()}

        """
    )
    return main_tex, header + flattened


def download_arxiv_source_bytes(arxiv_id: str) -> bytes:
    url = f"{ARXIV_EPRINT_BASE_URL}/{arxiv_id}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lagent-supervisor/1.0 (+https://github.com/wpegden/lagent_supervisor)",
        },
    )
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == 2:
                break
        time.sleep(1.5 * (attempt + 1))
    raise InitError(f"Failed to download arXiv source for {arxiv_id}: {last_error}")


def download_and_flatten_arxiv_source(arxiv_id: str, workspace_dir: Path) -> Path:
    source_root = workspace_dir / "arxiv-source"
    raw_bytes = download_arxiv_source_bytes(arxiv_id)
    materialize_downloaded_source(raw_bytes, source_root)
    _, flattened = flatten_arxiv_source(source_root, arxiv_id)
    output_path = workspace_dir / f"{arxiv_source_stem(arxiv_id)}.tex"
    output_path.write_text(flattened, encoding="utf-8")
    return output_path


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
            "session_name": supervisor.sanitize_tmux_session_name(spec.session_name),
            "dashboard_window_name": "dashboard",
            "kill_windows_after_capture": spec.kill_windows_after_capture,
        },
        "worker": dict(PROVIDER_PRESETS[spec.worker_provider]),
        "reviewer": dict(PROVIDER_PRESETS[spec.reviewer_provider]),
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


def default_paths(paper_source: Optional[Path], paper_arxiv_id: Optional[str]) -> tuple[Path, Path]:
    if paper_source is not None:
        repo_name = supervisor.sanitize_repo_name(paper_source.stem)
    elif paper_arxiv_id is not None:
        repo_name = arxiv_source_stem(paper_arxiv_id)
    else:
        raise InitError("Either paper_source or paper_arxiv_id must be set.")
    return (
        (Path.home() / "math" / repo_name).resolve(),
        (SCRIPT_DIR.parent / "configs" / f"{repo_name}.json").resolve(),
    )


def gather_spec(args: argparse.Namespace) -> InitSpec:
    if args.paper_source and args.paper_arxiv_id:
        raise InitError("Provide either --paper-source or --paper-arxiv-id, not both.")

    paper_default = args.paper_arxiv_id or args.paper_source or ""
    paper_source_text = prompt_text("Paper .tex path or arXiv id", paper_default)
    paper_arxiv_id = normalize_arxiv_id(paper_source_text)
    paper_source: Optional[Path]
    if paper_arxiv_id is not None:
        paper_source = None
    else:
        paper_source = Path(paper_source_text).expanduser().resolve()
        if not paper_source.exists():
            raise InitError(f"Paper source not found: {paper_source}")

    repo_default, config_default = default_paths(paper_source, paper_arxiv_id)
    repo_path = Path(prompt_text("Working repo path", args.repo_path or str(repo_default))).expanduser().resolve()
    config_path = Path(prompt_text("Supervisor config path", args.config_path or str(config_default))).expanduser().resolve()
    remote_url = prompt_text("Git remote URL (leave blank for none)", args.remote_url or "", required=False).strip() or None

    repo_name = supervisor.sanitize_repo_name(repo_path.name)
    package_default = repo_name_to_package_name(repo_name)
    package_name = prompt_text("Lean package name", args.package_name or package_default)
    paper_dest_default = f"paper/{paper_source.name}" if paper_source is not None else f"paper/{arxiv_source_stem(paper_arxiv_id or 'paper')}.tex"
    paper_dest_rel = Path(prompt_text("Paper path inside repo", args.paper_dest or paper_dest_default))
    goal_file_name = prompt_text("Goal file name", args.goal_file or "GOAL.md")
    max_cycles = int(prompt_text("Initial max_cycles", str(args.max_cycles or DEFAULT_INIT_MAX_CYCLES)))
    session_default = supervisor.sanitize_tmux_session_name(f"{repo_name}-agents")
    session_name = supervisor.sanitize_tmux_session_name(
        prompt_text("Agent tmux session name", args.session_name or session_default)
    )
    worker_provider = prompt_choice("Worker provider", sorted(PROVIDER_PRESETS), args.worker_provider or "codex")
    reviewer_provider = prompt_choice("Reviewer provider", sorted(PROVIDER_PRESETS), args.reviewer_provider or "claude")

    return InitSpec(
        repo_path=repo_path,
        remote_url=remote_url,
        paper_source=paper_source,
        paper_arxiv_id=paper_arxiv_id,
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
        worker_provider=worker_provider,
        reviewer_provider=reviewer_provider,
    )


def bootstrap_project(spec: InitSpec, *, create_commit: bool) -> dict[str, Any]:
    spec.repo_path.mkdir(parents=True, exist_ok=True)
    ensure_repo_git(spec.repo_path, spec.branch, spec.author_name, spec.author_email)
    ensure_remote(spec.repo_path, "origin", spec.remote_url)
    ensure_lake_project(spec.repo_path, spec.package_name)
    ci_workflow_path = ensure_build_only_ci_workflow(spec.repo_path)
    pinned_toolchain = ensure_explicit_repo_toolchain(spec.repo_path)
    with tempfile.TemporaryDirectory(prefix="lagent-paper-source-") as tmpdir:
        temp_root = Path(tmpdir)
        paper_source = spec.paper_source
        if paper_source is None:
            if spec.paper_arxiv_id is None:
                raise InitError("Either paper_source or paper_arxiv_id must be set.")
            paper_source = download_and_flatten_arxiv_source(spec.paper_arxiv_id, temp_root)
        paper_dest = copy_paper_into_repo(spec.repo_path, paper_source, spec.paper_dest_rel)

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
        "ci_workflow_path": ci_workflow_path,
        "pinned_toolchain": pinned_toolchain,
        "push_error": push_error,
        "agent_session": supervisor.sanitize_tmux_session_name(spec.session_name),
        "supervisor_session": supervisor.sanitize_tmux_session_name(
            f"{supervisor.sanitize_repo_name(spec.repo_path.name)}-supervisor"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactively initialize a Lean paper-formalization project.")
    paper_group = parser.add_mutually_exclusive_group()
    paper_group.add_argument("--paper-source", help="Source .tex file to copy into the repo")
    paper_group.add_argument("--paper-arxiv-id", help="arXiv identifier whose latest source should be downloaded and flattened")
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
    parser.add_argument("--worker-provider", choices=sorted(PROVIDER_PRESETS), help="Worker provider for the generated config")
    parser.add_argument("--reviewer-provider", choices=sorted(PROVIDER_PRESETS), help="Reviewer provider for the generated config")
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
        print(f"- Paper source: {source_label(spec.paper_source, spec.paper_arxiv_id)}")
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
    print(f"- CI workflow: {result['ci_workflow_path']}")
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
