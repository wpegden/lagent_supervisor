#!/usr/bin/env python3
"""Export a self-contained retrospective bundle for a supervisor-managed run."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_CHAT_ROOT = Path("/home/leanagent/lagent-chats")
RELEVANT_EVENT_KINDS = {
    "worker_prompt",
    "worker_handoff",
    "validation_summary",
    "reviewer_prompt",
    "reviewer_decision",
    "branch_strategy_prompt",
    "branch_strategy_decision",
}
THEOREM_DECL_RE = re.compile(
    r"^[+-]\s*(?:theorem|lemma|def|example|abbrev|structure|class|inductive)\s+([A-Za-z0-9_'.]+)"
)


def timestamp_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def load_json(path: Path) -> Any:
    try:
        return json.loads(safe_read_text(path))
    except Exception:
        return None


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    for raw_line in safe_read_text(path).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def run_git(repo_path: Path, args: Sequence[str], check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout


def git_show_file(repo_path: Path, head: str, relative_path: str) -> Optional[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "show", f"{head}:{relative_path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def git_rev_parents(repo_path: Path, head: str) -> List[str]:
    output = run_git(repo_path, ["rev-list", "--parents", "-n", "1", head], check=False).strip()
    if not output:
        return []
    parts = output.split()
    return parts[1:]


def git_commit_metadata(repo_path: Path, head: str) -> Dict[str, str]:
    fmt = "%H%x1f%P%x1f%cI%x1f%s"
    output = run_git(repo_path, ["show", "-s", f"--format={fmt}", head]).strip()
    full_head, parents, committed_at, subject = (output.split("\x1f", 3) + ["", "", "", ""])[:4]
    return {
        "head": full_head,
        "parents": parents,
        "committed_at": committed_at,
        "subject": subject,
    }


def git_changed_files(repo_path: Path, head: str) -> List[Dict[str, str]]:
    output = run_git(repo_path, ["diff-tree", "--no-commit-id", "--name-status", "-r", head], check=False)
    changed: List[Dict[str, str]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changed.append({"status": parts[0], "path": parts[-1]})
    return changed


def parse_numstat(output: str) -> Dict[str, int]:
    lean_added = 0
    lean_removed = 0
    lean_files = 0
    total_added = 0
    total_removed = 0
    total_files = 0
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        add_text, remove_text, path_text = parts
        if not add_text.isdigit() or not remove_text.isdigit():
            continue
        added = int(add_text)
        removed = int(remove_text)
        total_added += added
        total_removed += removed
        total_files += 1
        if path_text.endswith(".lean"):
            lean_added += added
            lean_removed += removed
            lean_files += 1
    return {
        "lean_added": lean_added,
        "lean_removed": lean_removed,
        "lean_files_touched": lean_files,
        "total_added": total_added,
        "total_removed": total_removed,
        "total_files_touched": total_files,
    }


def git_numstat(repo_path: Path, previous_head: str, head: str) -> Dict[str, int]:
    if not previous_head or previous_head == head:
        return {
            "lean_added": 0,
            "lean_removed": 0,
            "lean_files_touched": 0,
            "total_added": 0,
            "total_removed": 0,
            "total_files_touched": 0,
        }
    output = run_git(repo_path, ["diff", "--numstat", previous_head, head], check=False)
    return parse_numstat(output)


def git_patch(repo_path: Path, previous_head: str, head: str) -> str:
    if not previous_head or previous_head == head:
        return ""
    return run_git(repo_path, ["diff", "--binary", previous_head, head], check=False)


def git_zero_context_lean_diff(repo_path: Path, previous_head: str, head: str) -> str:
    if not previous_head or previous_head == head:
        return ""
    return run_git(repo_path, ["diff", "-U0", previous_head, head, "--", "*.lean"], check=False)


def parse_decl_changes(diff_text: str) -> Dict[str, List[str]]:
    added: List[str] = []
    removed: List[str] = []
    for line in diff_text.splitlines():
        match = THEOREM_DECL_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if line.startswith("+"):
            added.append(name)
        elif line.startswith("-"):
            removed.append(name)
    return {"added": sorted(set(added)), "removed": sorted(set(removed))}


def summarize_text_diff(previous_text: str, current_text: str, from_label: str, to_label: str, max_lines: int = 80) -> str:
    diff = list(
        difflib.unified_diff(
            previous_text.splitlines(),
            current_text.splitlines(),
            fromfile=from_label,
            tofile=to_label,
            lineterm="",
        )
    )
    if not diff:
        return "(no change)\n"
    clipped = diff[:max_lines]
    if len(diff) > max_lines:
        clipped.append("... (diff truncated)")
    return "\n".join(clipped) + "\n"


def find_chat_export_dir(repo_path: Path, chat_root: Path) -> Optional[Path]:
    for child in sorted(chat_root.iterdir()):
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        if not meta_path.exists():
            continue
        meta = load_json(meta_path)
        if isinstance(meta, dict) and str(meta.get("repo_path", "")).strip() == str(repo_path):
            return child
    return None


def collect_project_exports(chat_root: Path, project_name: str) -> List[Dict[str, Any]]:
    exports: List[Dict[str, Any]] = []
    for child in sorted(chat_root.iterdir()):
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        if not meta_path.exists():
            continue
        meta = load_json(meta_path)
        if not isinstance(meta, dict):
            continue
        if str(meta.get("project_name", "")).strip() != project_name:
            continue
        repo_path_text = str(meta.get("repo_path", "")).strip()
        exports.append(
            {
                "export_dir": child,
                "meta": meta,
                "repo_name": str(meta.get("repo_name", child.name) or child.name),
                "repo_path": Path(repo_path_text) if repo_path_text else None,
                "is_branch": bool(meta.get("is_branch", False)),
            }
        )
    return exports


def select_root_export(project_exports: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for entry in project_exports:
        if not entry.get("is_branch", False):
            return entry
    return project_exports[0] if project_exports else None


def build_branch_name_to_repo_name(project_exports: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    root_export = select_root_export(project_exports)
    if root_export is not None:
        mapping["mainline"] = str(root_export.get("repo_name", "mainline"))
    for entry in project_exports:
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        branch_overview = meta.get("branch_overview") if isinstance(meta.get("branch_overview"), dict) else {}
        for episode in branch_overview.get("episodes", []) or []:
            if not isinstance(episode, dict):
                continue
            for branch in episode.get("branches", []) or []:
                if not isinstance(branch, dict):
                    continue
                name = str(branch.get("name", "")).strip()
                repo_name = str(branch.get("repo_name", "")).strip()
                if name and repo_name:
                    mapping[name] = repo_name
    return mapping


def selected_lineage_repo_names(selected_meta: Dict[str, Any], project_exports: Sequence[Dict[str, Any]]) -> List[str]:
    branch_overview = selected_meta.get("branch_overview") if isinstance(selected_meta.get("branch_overview"), dict) else {}
    path_names = list(reversed(branch_overview.get("current_path_newest_to_oldest") or []))
    name_map = build_branch_name_to_repo_name(project_exports)
    repo_names: List[str] = []
    for name in path_names:
        repo_name = name_map.get(str(name), "")
        if repo_name:
            repo_names.append(repo_name)
    if not repo_names:
        repo_names.append(str(selected_meta.get("repo_name", "")))
    return repo_names


def collect_supervisor_artifacts(project_name: str, root_repo_path: Optional[Path], supervisor_root: Path) -> List[Dict[str, str]]:
    artifacts: List[Dict[str, str]] = []

    def add(src: Path, bundle_path: str, kind: str, description: str) -> None:
        if not src.exists():
            return
        artifacts.append(
            {
                "source_path": str(src),
                "bundle_path": bundle_path,
                "kind": kind,
                "description": description,
            }
        )

    add(
        supervisor_root / "supervisor.py",
        "SUPERVISOR_CODE/supervisor.py",
        "supervisor_code",
        "Main supervisor state machine: phases, prompts, validation gates, branching, and cleanup logic.",
    )
    add(
        supervisor_root / "README.md",
        "SUPERVISOR_CODE/README.md",
        "supervisor_docs",
        "Human-facing documentation for how the supervisor works and what its phases and policies mean.",
    )
    add(
        supervisor_root / "tests" / "test_supervisor.py",
        "SUPERVISOR_CODE/tests/test_supervisor.py",
        "supervisor_tests",
        "Regression tests for supervisor behavior. Useful for understanding intended semantics of branching, validation, and cleanup.",
    )

    root_config = supervisor_root / "configs" / f"{project_name}.json"
    policy_path: Optional[Path] = None
    if root_config.exists():
        add(
            root_config,
            f"SUPERVISOR_POLICY/root_config/{root_config.name}",
            "root_config",
            "Root run config used to start the project. Contains repo identity and startup-only settings such as max cycles.",
        )
        root_config_data = load_json(root_config)
        if isinstance(root_config_data, dict):
            policy_text = str(root_config_data.get("policy_path", "")).strip()
            if policy_text:
                policy_path = Path(policy_text)
    if policy_path is None:
        candidate_policy = supervisor_root / "configs" / f"{project_name}.policy.json"
        if candidate_policy.exists():
            policy_path = candidate_policy
    if policy_path is not None and policy_path.exists():
        add(
            policy_path,
            f"SUPERVISOR_POLICY/shared_policy/{policy_path.name}",
            "shared_policy",
            "Hot-reloadable policy file shared across the project frontier. Governs branch-review cadence, stuck limits, budget pauses, and prompt notes.",
        )

    if root_repo_path is not None:
        branch_root = root_repo_path / ".agent-supervisor" / "branches"
        if branch_root.exists():
            for config_path in sorted(branch_root.glob("*/*.json")):
                rel = config_path.relative_to(branch_root)
                add(
                    config_path,
                    f"SUPERVISOR_POLICY/branch_configs/{rel}",
                    "branch_config",
                    "Spawned branch config for a child run. Captures the repo path and policy path used for that branch episode.",
                )
    return artifacts


def collect_events_by_cycle(events_path: Path) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, List[Dict[str, Any]]]]]:
    relevant_events: List[Dict[str, Any]] = []
    by_cycle: DefaultDict[int, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for event in iter_jsonl(events_path):
        kind = str(event.get("kind", "")).strip()
        if kind not in RELEVANT_EVENT_KINDS:
            continue
        relevant_events.append(event)
        cycle = int(event.get("cycle", 0) or 0)
        if cycle > 0:
            by_cycle[cycle][kind].append(event)
    return relevant_events, by_cycle


def latest_event_content(events: Dict[str, List[Dict[str, Any]]], kind: str) -> Any:
    items = events.get(kind) or []
    if not items:
        return None
    return items[-1].get("content")


def latest_event_summary(events: Dict[str, List[Dict[str, Any]]], kind: str) -> str:
    items = events.get(kind) or []
    if not items:
        return ""
    return str(items[-1].get("summary", "")).strip()


def copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree_filtered(src: Path, dst: Path, predicate) -> None:
    if not src.exists():
        return
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if not predicate(path):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def render_autoprover_readme(
    repo_path: Path,
    meta: Optional[Dict[str, Any]],
    selected_lineage_cycle_count: int,
    project_cycle_count: int,
    project_repo_count: int,
    paper_files: List[Path],
    supervisor_artifacts: Sequence[Dict[str, str]],
) -> str:
    project_name = str((meta or {}).get("project_name", repo_path.name))
    export_name = str((meta or {}).get("repo_name", repo_path.name))
    paper_lines = "\n".join(f"- `paper/{path.name}`" for path in paper_files) if paper_files else "- none found"
    if supervisor_artifacts:
        supervisor_lines = "\n".join(
            f"- `{item['bundle_path']}`\n  source: `{item['source_path']}`\n  purpose: {item['description']}"
            for item in supervisor_artifacts
        )
    else:
        supervisor_lines = "- none copied"
    return f"""# Retrospective Bundle

This archive is a self-contained retrospective for the supervisor-managed autoprover run on `{project_name}` / `{export_name}`.

## What The Autoprover Is

The autoprover is the workflow implemented by `lagent_supervisor`:

- a worker model runs in repeated bursts and edits the Lean repo;
- a validation step checks build state, sorry count, and related invariants;
- a reviewer model reads the worker handoff and validation output, then decides whether to continue, advance phase, branch, stop as stuck, or finish;
- the supervisor records cycle-by-cycle prompts, handoffs, review decisions, validation checkpoints, and git heads.

The workflow phases are usually:

1. `paper_check`
2. `planning`
3. `theorem_stating`
4. `proof_formalization`
5. optional `proof_complete_style_cleanup`

This bundle is intended for another agent or human reviewer with **no prior context**. It is designed to answer:

- what the paper target was;
- how `PLAN.md` and `TASKS.md` evolved;
- what communication happened in each cycle;
- what code changed in each cycle;
- what seems to have gone right or wrong over time.

## How To Read This Bundle

Start with:

1. `PROJECT_CONTEXT.md`
2. `BRANCH_HISTORY.md`
3. `CYCLE_INDEX.jsonl`
4. `FRONTIER_HISTORY.jsonl`

Then inspect:

- `CYCLE_DOSSIERS/` for per-cycle summaries
- `CYCLE_PATCHES/` for exact code changes
- `PLAN_SNAPSHOTS/` and `TASKS_SNAPSHOTS/` for frontier evolution
- `RAW/` for raw supervisor/chat evidence

## Included Paper Sources

{paper_lines}

## Included Supervisor Code And Policy

These files are **export-time snapshots** of the supervisor implementation and the config/policy files that currently govern this project. They are included so another agent can inspect:

- what code was running the workflow at export time;
- what startup config launched the root project;
- what shared hot-reload policy governed branch review, stuck limits, and budget pauses;
- what child-branch config files were generated for branch episodes.

Use these as the authoritative control-plane context for the retrospective. They are not a historical log of every supervisor revision used earlier in the run; they are the current code/policy snapshot captured when this bundle was generated.

{supervisor_lines}

## Export Facts

- Generated at: `{timestamp_now()}`
- Repo path: `{repo_path}`
- Selected-lineage validated cycles exported: `{selected_lineage_cycle_count}`
- Project-wide validated cycles exported: `{project_cycle_count}`
- Project repos included: `{project_repo_count}`
- This export is read-only and does not modify the running supervisor.
"""


def render_project_context(repo_path: Path, meta: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]], paper_files: List[Path]) -> str:
    meta = meta or {}
    state = state or {}
    lines = [
        "# Project Context",
        "",
        f"- Repo path: `{repo_path}`",
        f"- Export name: `{meta.get('repo_name', repo_path.name)}`",
        f"- Project name: `{meta.get('project_name', repo_path.name)}`",
        f"- Current phase: `{meta.get('current_phase', state.get('phase', 'unknown'))}`",
        f"- Current cycle: `{meta.get('current_cycle', state.get('cycle', 'unknown'))}`",
        f"- Is branch: `{meta.get('is_branch', False)}`",
        "",
        "## Current Root Files",
        "",
    ]
    for name in ["GOAL.md", "PAPERNOTES.md", "PLAN.md", "TASKS.md", "PaperDefinitions.lean", "PaperTheorems.lean"]:
        path = repo_path / name
        lines.append(f"- `{name}`: {'present' if path.exists() else 'missing'}")
    lines.extend(["", "## Paper Sources", ""])
    if paper_files:
        for path in paper_files:
            lines.append(f"- `{path.relative_to(repo_path)}`")
    else:
        lines.append("- no paper tex file discovered")
    return "\n".join(lines) + "\n"


def render_branch_history(
    repo_path: Path,
    meta: Optional[Dict[str, Any]],
    project_exports: Sequence[Dict[str, Any]],
    lineage_repo_names: Sequence[str],
) -> str:
    meta = meta or {}
    lines = ["# Branch History", ""]
    repo_name_to_entry = {str(entry.get("repo_name")): entry for entry in project_exports}
    root_repo_name = None
    for entry in project_exports:
        if not entry.get("is_branch"):
            root_repo_name = str(entry.get("repo_name"))
            break
    selected_path: List[str] = []
    for repo_name in lineage_repo_names:
        entry = repo_name_to_entry.get(str(repo_name))
        entry_meta = entry.get("meta") if isinstance(entry, dict) and isinstance(entry.get("meta"), dict) else {}
        if entry and not entry.get("is_branch"):
            selected_path.append("mainline")
        else:
            branch_label = entry_meta.get("branch_name")
            if not branch_label and isinstance(entry, dict):
                repo_path_text = str(entry.get("repo_path") or "")
                if "--" in repo_path_text:
                    branch_label = repo_path_text.split("--")[-1]
            if not branch_label and root_repo_name and str(repo_name).startswith(f"{root_repo_name}-"):
                branch_label = str(repo_name)[len(root_repo_name) + 1 :]
            selected_path.append(str(branch_label or repo_name))
    if selected_path:
        lines.append("## Selected Lineage")
        lines.append("")
        for item in reversed(selected_path):
            lines.append(f"- `{item}`")
        lines.append("")
    branch_overview = meta.get("branch_overview") if isinstance(meta.get("branch_overview"), dict) else {}
    episodes = branch_overview.get("episodes") or []
    if episodes:
        lines.append("## Episodes")
        lines.append("")
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            lines.append(
                f"- `{episode.get('id')}` trigger_cycle=`{episode.get('trigger_cycle')}` status=`{episode.get('status')}` selected_branch=`{episode.get('selected_branch')}`"
            )
            for branch in episode.get("branches", []) or []:
                if isinstance(branch, dict):
                    lines.append(
                        f"  branch `{branch.get('name')}` repo=`{branch.get('repo_name')}` status=`{branch.get('status')}` scope=`{branch.get('rewrite_scope')}`"
                    )
        lines.append("")
    if project_exports:
        lines.append("## Project Exports")
        lines.append("")
        for entry in sorted(project_exports, key=lambda item: str(item.get("repo_name", ""))):
            entry_meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
            lines.append(
                f"- `{entry.get('repo_name')}` | phase=`{entry_meta.get('current_phase','')}` cycle=`{entry_meta.get('current_cycle','')}` branch=`{entry.get('is_branch', False)}`"
            )
        lines.append("")
    lines.append(
        "This file is intentionally lightweight. Use `RAW/repos.json`, `RAW/<repo_name>/meta.json`, "
        "`PROJECT_CYCLE_INDEX.jsonl`, and the cycle dossiers for the detailed chronology."
    )
    return "\n".join(lines) + "\n"


def relative_path(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-path", type=Path, required=True)
    parser.add_argument("--chat-root", type=Path, default=DEFAULT_CHAT_ROOT)
    parser.add_argument("--chat-export-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--zip-path", type=Path, default=None)
    parser.add_argument("--max-cycle", type=int, default=None, help="Optional upper bound on validated cycle number to export.")
    args = parser.parse_args()

    repo_path = args.repo_path.resolve()
    state_dir = repo_path / ".agent-supervisor"
    if not repo_path.exists():
        raise SystemExit(f"repo does not exist: {repo_path}")
    if not state_dir.exists():
        raise SystemExit(f"missing supervisor state dir: {state_dir}")

    chat_export_dir = args.chat_export_dir
    if chat_export_dir is None:
        chat_export_dir = find_chat_export_dir(repo_path, args.chat_root)
    elif not chat_export_dir.is_absolute():
        chat_export_dir = (args.chat_root / chat_export_dir).resolve()

    output_dir = args.output_dir or (state_dir / "retrospective_bundle")
    zip_path = args.zip_path or (state_dir / "retrospective_bundle.zip")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    meta = load_json(chat_export_dir / "meta.json") if chat_export_dir is not None and (chat_export_dir / "meta.json").exists() else None
    if not isinstance(meta, dict):
        raise SystemExit("could not load selected export meta.json")
    state = load_json(state_dir / "state.json")

    project_name = str(meta.get("project_name", repo_path.name))
    project_exports = collect_project_exports(args.chat_root, project_name)
    if not project_exports and chat_export_dir is not None:
        project_exports = [
            {
                "export_dir": chat_export_dir,
                "meta": meta,
                "repo_name": str(meta.get("repo_name", chat_export_dir.name)),
                "repo_path": repo_path,
                "is_branch": bool(meta.get("is_branch", False)),
            }
        ]
    selected_repo_name = str(meta.get("repo_name", repo_path.name))
    lineage_repo_names = selected_lineage_repo_names(meta, project_exports)
    root_export = select_root_export(project_exports)
    branch_history_meta = root_export.get("meta") if isinstance(root_export, dict) else meta
    root_repo_path = Path(root_export.get("repo_path")) if isinstance(root_export, dict) and root_export.get("repo_path") else repo_path
    supervisor_root = Path(__file__).resolve().parents[1]
    supervisor_artifacts = collect_supervisor_artifacts(project_name, root_repo_path, supervisor_root)

    paper_files = sorted((repo_path / "paper").rglob("*.tex")) if (repo_path / "paper").exists() else []
    if not paper_files:
        paper_files = sorted(repo_path.rglob("*.tex"))

    (output_dir / "README.md").write_text(
        render_autoprover_readme(
            repo_path,
            meta if isinstance(meta, dict) else None,
            0,
            0,
            0,
            paper_files,
            supervisor_artifacts,
        ),
        encoding="utf-8",
    )
    (output_dir / "PROJECT_CONTEXT.md").write_text(
        render_project_context(repo_path, meta if isinstance(meta, dict) else None, state if isinstance(state, dict) else None, paper_files),
        encoding="utf-8",
    )
    (output_dir / "BRANCH_HISTORY.md").write_text(
        render_branch_history(
            repo_path,
            branch_history_meta if isinstance(branch_history_meta, dict) else meta,
            project_exports,
            lineage_repo_names,
        ),
        encoding="utf-8",
    )

    # Copy raw evidence.
    raw_dir = output_dir / "RAW"
    copy_if_exists(args.chat_root / "repos.json", raw_dir / "repos.json")
    for entry in project_exports:
        repo_name = str(entry.get("repo_name"))
        repo_export_dir = entry.get("export_dir")
        repo_state_dir = Path(entry.get("repo_path")) / ".agent-supervisor" if entry.get("repo_path") else None
        repo_raw_dir = raw_dir / repo_name
        if repo_state_dir is not None:
            for filename in ["review_log.jsonl", "validation_log.jsonl", "state.json", "validation_summary.json"]:
                copy_if_exists(repo_state_dir / filename, repo_raw_dir / filename)
            if (repo_state_dir / "runtime").exists():
                copy_tree_filtered(repo_state_dir / "runtime", repo_raw_dir / "runtime_scripts", lambda path: path.suffix == ".sh")
        if isinstance(repo_export_dir, Path):
            for filename in ["events.jsonl", "meta.json"]:
                copy_if_exists(repo_export_dir / filename, repo_raw_dir / filename)

    for artifact in supervisor_artifacts:
        copy_if_exists(Path(artifact["source_path"]), output_dir / artifact["bundle_path"])

    paper_out_dir = output_dir / "PAPER"
    for paper_file in paper_files:
        target = paper_out_dir / paper_file.relative_to(repo_path / "paper") if (repo_path / "paper") in paper_file.parents else paper_out_dir / paper_file.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(paper_file, target)

    for entry in project_exports:
        repo_name = str(entry.get("repo_name"))
        repo_current_dir = output_dir / "CURRENT_FILES" / repo_name
        repo_repo_path = Path(entry.get("repo_path")) if entry.get("repo_path") else None
        if repo_repo_path is None:
            continue
        for name in ["GOAL.md", "PAPERNOTES.md", "PLAN.md", "TASKS.md", "PaperDefinitions.lean", "PaperTheorems.lean", "README.md", "APPROVED_AXIOMS.json"]:
            copy_if_exists(repo_repo_path / name, repo_current_dir / name)

    cycle_rows: List[Dict[str, Any]] = []
    lineage_cycle_rows: List[Dict[str, Any]] = []
    frontier_rows: List[Dict[str, Any]] = []
    communications_rows: List[Dict[str, Any]] = []
    plan_history_rows: List[Dict[str, Any]] = []
    tasks_history_rows: List[Dict[str, Any]] = []
    code_change_rows: List[Dict[str, Any]] = []
    plan_snapshots_dir = output_dir / "PLAN_SNAPSHOTS"
    tasks_snapshots_dir = output_dir / "TASKS_SNAPSHOTS"
    cycle_dossiers_dir = output_dir / "CYCLE_DOSSIERS"
    cycle_patches_dir = output_dir / "CYCLE_PATCHES"

    repo_summary_rows: List[Dict[str, Any]] = []

    for export_entry in project_exports:
        repo_name = str(export_entry.get("repo_name"))
        repo_repo_path = Path(export_entry.get("repo_path")) if export_entry.get("repo_path") else None
        repo_export_dir = export_entry.get("export_dir")
        repo_meta = export_entry.get("meta") if isinstance(export_entry.get("meta"), dict) else {}
        if repo_repo_path is None:
            continue
        repo_state_dir = repo_repo_path / ".agent-supervisor"
        repo_state = load_json(repo_state_dir / "state.json")
        review_entries = list(iter_jsonl(repo_state_dir / "review_log.jsonl"))
        validation_entries = list(iter_jsonl(repo_state_dir / "validation_log.jsonl"))
        if args.max_cycle is not None:
            validation_entries = [row for row in validation_entries if int(row.get("cycle", 0) or 0) <= args.max_cycle]
        validation_entries = [row for row in validation_entries if int(row.get("cycle", 0) or 0) > 0]
        review_by_cycle = {int(row.get("cycle", 0) or 0): row for row in review_entries if int(row.get("cycle", 0) or 0) > 0}

        events_by_cycle: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
        if isinstance(repo_export_dir, Path) and (repo_export_dir / "events.jsonl").exists():
            _, events_by_cycle = collect_events_by_cycle(repo_export_dir / "events.jsonl")

        previous_head: Optional[str] = None
        previous_plan_text = ""
        previous_tasks_text = ""
        last_frontier_text = ""

        for entry in validation_entries:
            cycle = int(entry.get("cycle", 0) or 0)
            git_info = entry.get("git") if isinstance(entry.get("git"), dict) else {}
            head = str((git_info or {}).get("head", "")).strip()
            if cycle <= 0 or not head:
                continue
            if previous_head is None:
                parents = git_rev_parents(repo_repo_path, head)
                previous_head = parents[0] if parents else ""
            commit_meta = git_commit_metadata(repo_repo_path, head)
            changed_files = git_changed_files(repo_repo_path, head)
            numstat = git_numstat(repo_repo_path, previous_head, head)
            lean_diff_text = git_zero_context_lean_diff(repo_repo_path, previous_head, head)
            decl_changes = parse_decl_changes(lean_diff_text)
            patch_text = git_patch(repo_repo_path, previous_head, head)
            patch_path = cycle_patches_dir / repo_name / f"cycle-{cycle:04d}.patch"
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(patch_text, encoding="utf-8")

            plan_text = git_show_file(repo_repo_path, head, "PLAN.md") or ""
            tasks_text = git_show_file(repo_repo_path, head, "TASKS.md") or ""
            plan_snapshot_path = plan_snapshots_dir / repo_name / f"cycle-{cycle:04d}.md"
            tasks_snapshot_path = tasks_snapshots_dir / repo_name / f"cycle-{cycle:04d}.md"
            plan_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            tasks_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            plan_snapshot_path.write_text(plan_text, encoding="utf-8")
            tasks_snapshot_path.write_text(tasks_text, encoding="utf-8")

            plan_history_rows.append(
                {
                    "repo_name": repo_name,
                    "cycle": cycle,
                    "head": head,
                    "snapshot": relative_path(plan_snapshot_path, output_dir),
                    "sha256": sha256_text(plan_text),
                    "changed_from_previous": plan_text != previous_plan_text,
                }
            )
            tasks_history_rows.append(
                {
                    "repo_name": repo_name,
                    "cycle": cycle,
                    "head": head,
                    "snapshot": relative_path(tasks_snapshot_path, output_dir),
                    "sha256": sha256_text(tasks_text),
                    "changed_from_previous": tasks_text != previous_tasks_text,
                }
            )

            cycle_events = events_by_cycle.get(cycle, {})
            worker_prompt = latest_event_content(cycle_events, "worker_prompt")
            worker_handoff = latest_event_content(cycle_events, "worker_handoff")
            reviewer_prompt = latest_event_content(cycle_events, "reviewer_prompt")
            review_entry = review_by_cycle.get(cycle, {})

            frontier_text = ""
            likely_next_step = ""
            worker_summary = ""
            worker_status = ""
            if isinstance(worker_handoff, dict):
                frontier_text = str(worker_handoff.get("current_frontier", "")).strip()
                likely_next_step = str(worker_handoff.get("likely_next_step", "")).strip()
                worker_summary = str(worker_handoff.get("summary_of_changes", "")).strip()
                worker_status = str(worker_handoff.get("status", "")).strip()

            progress_kind = "unknown"
            if frontier_text and frontier_text != last_frontier_text:
                progress_kind = "frontier_shift"
            elif decl_changes["added"] or decl_changes["removed"]:
                progress_kind = "code_change"
            elif plan_text != previous_plan_text or tasks_text != previous_tasks_text:
                progress_kind = "planning_delta"

            frontier_row = {
                "repo_name": repo_name,
                "cycle": cycle,
                "head": head,
                "worker_status": worker_status,
                "worker_summary": worker_summary,
                "current_frontier": frontier_text,
                "likely_next_step": likely_next_step,
                "review_decision": str(review_entry.get("decision", "")).strip(),
                "review_reason": str(review_entry.get("reason", "")).strip(),
                "review_next_prompt": str(review_entry.get("next_prompt", "")).strip(),
                "progress_kind": progress_kind,
            }
            frontier_rows.append(frontier_row)

            cycle_row = {
                "repo_name": repo_name,
                "cycle": cycle,
                "phase": str(review_entry.get("phase", entry.get("phase", ""))).strip(),
                "head": head,
                "commit_subject": commit_meta["subject"],
                "committed_at": commit_meta["committed_at"],
                "review_decision": str(review_entry.get("decision", "")).strip(),
                "review_confidence": review_entry.get("confidence"),
                "worker_status": worker_status,
                "lean_added": numstat["lean_added"],
                "lean_removed": numstat["lean_removed"],
                "lean_files_touched": numstat["lean_files_touched"],
                "changed_files": [item["path"] for item in changed_files],
                "patch": relative_path(patch_path, output_dir),
                "plan_snapshot": relative_path(plan_snapshot_path, output_dir),
                "tasks_snapshot": relative_path(tasks_snapshot_path, output_dir),
                "progress_kind": progress_kind,
                "selected_lineage": repo_name in lineage_repo_names,
            }
            cycle_rows.append(cycle_row)
            if repo_name in lineage_repo_names:
                lineage_cycle_rows.append(cycle_row)

            code_change_rows.append(
                {
                    "repo_name": repo_name,
                    "cycle": cycle,
                    "head": head,
                    "commit_subject": commit_meta["subject"],
                    "changed_files": changed_files,
                    "lean_added": numstat["lean_added"],
                    "lean_removed": numstat["lean_removed"],
                    "lean_files_touched": numstat["lean_files_touched"],
                    "theorems_added": decl_changes["added"],
                    "theorems_removed": decl_changes["removed"],
                    "patch": relative_path(patch_path, output_dir),
                }
            )

            for kind, items in cycle_events.items():
                for event in items:
                    communications_rows.append(
                        {
                            "repo_name": repo_name,
                            "cycle": cycle,
                            "kind": kind,
                            "timestamp": event.get("timestamp"),
                            "summary": event.get("summary"),
                            "content_type": event.get("content_type"),
                            "content": event.get("content"),
                        }
                    )

            plan_diff = summarize_text_diff(previous_plan_text, plan_text, "previous PLAN.md", f"{repo_name} cycle {cycle} PLAN.md")
            tasks_diff = summarize_text_diff(previous_tasks_text, tasks_text, "previous TASKS.md", f"{repo_name} cycle {cycle} TASKS.md")

            dossier_lines = [
                f"# {repo_name} Cycle {cycle:04d}",
                "",
                f"- Repo: `{repo_name}`",
                f"- Phase: `{review_entry.get('phase', entry.get('phase', ''))}`",
                f"- Commit: `{head}`",
                f"- Commit subject: {commit_meta['subject']}",
                f"- Reviewer decision: `{review_entry.get('decision', '')}`",
                f"- Worker status: `{worker_status or 'unknown'}`",
                f"- Lean delta: `+{numstat['lean_added']} / -{numstat['lean_removed']}` across `{numstat['lean_files_touched']}` Lean files",
                f"- Selected lineage: `{repo_name in lineage_repo_names}`",
                "",
                "## Worker",
                "",
                f"- Prompt summary: {latest_event_summary(cycle_events, 'worker_prompt') or '(none)'}",
                f"- Handoff summary: {worker_summary or '(none)'}",
                f"- Current frontier: {frontier_text or '(none)'}",
                f"- Likely next step: {likely_next_step or '(none)'}",
                "",
                "## Reviewer",
                "",
                f"- Reason: {str(review_entry.get('reason', '')).strip() or '(none)'}",
                f"- Next prompt: {str(review_entry.get('next_prompt', '')).strip() or '(none)'}",
                "",
                "## Validation / Commit",
                "",
                f"- Commit time: `{commit_meta['committed_at']}`",
                f"- Changed files: {', '.join(item['path'] for item in changed_files) if changed_files else '(none)'}",
                f"- Added theorem-like declarations: {', '.join(decl_changes['added']) if decl_changes['added'] else '(none)'}",
                f"- Removed theorem-like declarations: {', '.join(decl_changes['removed']) if decl_changes['removed'] else '(none)'}",
                f"- Patch: `{relative_path(patch_path, output_dir)}`",
                "",
                "## PLAN.md Delta",
                "",
                "```diff",
                plan_diff.rstrip(),
                "```",
                "",
                "## TASKS.md Delta",
                "",
                "```diff",
                tasks_diff.rstrip(),
                "```",
                "",
            ]
            worker_prompt_text = worker_prompt if isinstance(worker_prompt, str) else ""
            reviewer_prompt_text = reviewer_prompt if isinstance(reviewer_prompt, str) else ""
            if worker_prompt_text:
                clipped_worker_prompt = worker_prompt_text[:4000] + ("\n... (truncated)" if len(worker_prompt_text) > 4000 else "")
                dossier_lines.extend(["## Worker Prompt Excerpt", "", "```text", clipped_worker_prompt.rstrip(), "```", ""])
            if reviewer_prompt_text:
                clipped_reviewer_prompt = reviewer_prompt_text[:2500] + ("\n... (truncated)" if len(reviewer_prompt_text) > 2500 else "")
                dossier_lines.extend(["## Reviewer Prompt Excerpt", "", "```text", clipped_reviewer_prompt.rstrip(), "```", ""])

            dossier_path = cycle_dossiers_dir / repo_name / f"cycle-{cycle:04d}.md"
            dossier_path.parent.mkdir(parents=True, exist_ok=True)
            dossier_path.write_text("\n".join(dossier_lines), encoding="utf-8")

            previous_head = head
            previous_plan_text = plan_text
            previous_tasks_text = tasks_text
            if frontier_text:
                last_frontier_text = frontier_text

        repo_summary_rows.append(
            {
                "repo_name": repo_name,
                "repo_path": str(repo_repo_path),
                "is_branch": bool(export_entry.get("is_branch", False)),
                "cycle_count": len(validation_entries),
                "current_cycle": repo_meta.get("current_cycle"),
                "current_phase": repo_meta.get("current_phase"),
                "selected_lineage": repo_name in lineage_repo_names,
            }
        )

    cycle_rows.sort(key=lambda row: (str(row.get("repo_name", "")), int(row.get("cycle", 0) or 0)))
    lineage_position = {name: index for index, name in enumerate(lineage_repo_names)}
    lineage_cycle_rows.sort(key=lambda row: (lineage_position.get(str(row.get("repo_name", "")), 9999), int(row.get("cycle", 0) or 0)))

    write_jsonl(output_dir / "PROJECT_CYCLE_INDEX.jsonl", cycle_rows)
    write_jsonl(output_dir / "CYCLE_INDEX.jsonl", lineage_cycle_rows)
    write_jsonl(output_dir / "REPO_SUMMARY.jsonl", repo_summary_rows)
    write_jsonl(output_dir / "COMMUNICATIONS.jsonl", communications_rows)
    write_jsonl(output_dir / "FRONTIER_HISTORY.jsonl", frontier_rows)
    write_jsonl(output_dir / "PLAN_HISTORY.jsonl", plan_history_rows)
    write_jsonl(output_dir / "TASKS_HISTORY.jsonl", tasks_history_rows)
    write_jsonl(output_dir / "CODE_CHANGE_INDEX.jsonl", code_change_rows)

    project_summary = {
        "generated_at": timestamp_now(),
        "repo_path": str(repo_path),
        "chat_export_dir": str(chat_export_dir) if chat_export_dir is not None else None,
        "project_cycle_count": len(cycle_rows),
        "selected_lineage_cycle_count": len(lineage_cycle_rows),
        "first_cycle": lineage_cycle_rows[0]["cycle"] if lineage_cycle_rows else None,
        "last_cycle": lineage_cycle_rows[-1]["cycle"] if lineage_cycle_rows else None,
        "selected_repo_name": selected_repo_name,
        "lineage_repo_names": lineage_repo_names,
        "phase_counts": Counter(str(row.get("phase", "")) for row in cycle_rows),
        "review_decision_counts": Counter(str(row.get("review_decision", "")) for row in cycle_rows),
        "progress_kind_counts": Counter(str(row.get("progress_kind", "")) for row in cycle_rows),
        "paper_files": [str(path.relative_to(repo_path)) for path in paper_files],
    }
    # Counter is not JSON serializable directly once nested in dict.
    project_summary["phase_counts"] = dict(project_summary["phase_counts"])
    project_summary["review_decision_counts"] = dict(project_summary["review_decision_counts"])
    project_summary["progress_kind_counts"] = dict(project_summary["progress_kind_counts"])
    project_summary["supervisor_artifacts"] = supervisor_artifacts
    write_json(output_dir / "bundle_manifest.json", project_summary)

    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=output_dir)

    (output_dir / "README.md").write_text(
        render_autoprover_readme(
            repo_path,
            meta if isinstance(meta, dict) else None,
            len(lineage_cycle_rows),
            len(cycle_rows),
            len(project_exports),
            paper_files,
            supervisor_artifacts,
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "zip_path": str(zip_path),
                "project_cycles": len(cycle_rows),
                "selected_lineage_cycles": len(lineage_cycle_rows),
                "repos": len(project_exports),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
