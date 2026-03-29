#!/usr/bin/env python3
"""Export per-cycle Lean-only +/- line counts for chat timelines."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


DEFAULT_CHAT_ROOT = Path("/home/leanagent/lagent-chats")
OUTPUT_FILE = "lean-cycle-stats.json"
EMPTY_TREE_HASH = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def timestamp_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def iter_validation_entries(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def parse_numstat(output: str) -> Dict[str, int]:
    added = 0
    removed = 0
    files_touched = 0
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        add_text, remove_text, path_text = parts
        if ".lean" not in path_text:
            continue
        if not add_text.isdigit() or not remove_text.isdigit():
            continue
        added += int(add_text)
        removed += int(remove_text)
        files_touched += 1
    return {
        "lean_added": added,
        "lean_removed": removed,
        "lean_net": added - removed,
        "lean_files_touched": files_touched,
    }


def git_diff_numstat(repo_path: Path, previous_head: str, head: str) -> Dict[str, int]:
    if not previous_head or not head:
        return {}
    if previous_head == head:
        return {
            "lean_added": 0,
            "lean_removed": 0,
            "lean_net": 0,
            "lean_files_touched": 0,
        }
    result = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--numstat", previous_head, head],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    return parse_numstat(result.stdout)


def git_first_parent_or_empty(repo_path: Path, head: str) -> str:
    if not head:
        return EMPTY_TREE_HASH
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-list", "--parents", "-n", "1", head],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return EMPTY_TREE_HASH
    parts = result.stdout.strip().split()
    if len(parts) >= 2:
        return parts[1]
    return EMPTY_TREE_HASH


def compute_cycle_stats(repo_path: Path, validation_log_path: Path) -> Dict[str, Any]:
    cycles: Dict[str, Dict[str, Any]] = {}
    previous_head: Optional[str] = None
    previous_cycle: Optional[int] = None
    cache: Dict[tuple[str, str], Dict[str, int]] = {}
    first_parent_cache: Dict[str, str] = {}

    for entry in iter_validation_entries(validation_log_path):
        cycle = int(entry.get("cycle", 0) or 0)
        git_info = entry.get("git") if isinstance(entry.get("git"), dict) else {}
        head = str((git_info or {}).get("head", "")).strip()
        if cycle <= 0 or not head:
            continue
        baseline_head = previous_head
        if not baseline_head:
            baseline_head = first_parent_cache.get(head)
            if baseline_head is None:
                baseline_head = git_first_parent_or_empty(repo_path, head)
                first_parent_cache[head] = baseline_head
        key = (baseline_head, head)
        stats = cache.get(key)
        if stats is None:
            stats = git_diff_numstat(repo_path, baseline_head, head)
            cache[key] = stats
        if stats:
            cycles[str(cycle)] = {
                **stats,
                "head": head,
                "previous_head": baseline_head,
                "previous_cycle": previous_cycle,
            }
        previous_head = head
        previous_cycle = cycle

    return {
        "generated_at": timestamp_now(),
        "repo_path": str(repo_path),
        "cycles": cycles,
    }


def export_repo(repo_export_dir: Path) -> None:
    meta = load_json(repo_export_dir / "meta.json")
    payload: Dict[str, Any] = {
        "generated_at": timestamp_now(),
        "repo_name": repo_export_dir.name,
        "available": False,
        "repo_path": None,
        "cycles": {},
    }
    if isinstance(meta, dict):
        repo_path_text = str(meta.get("repo_path", "")).strip()
        if repo_path_text:
            repo_path = Path(repo_path_text)
            validation_log_path = repo_path / ".agent-supervisor" / "validation_log.jsonl"
            if repo_path.exists() and validation_log_path.exists():
                payload.update(
                    {
                        "available": True,
                        "repo_name": str(meta.get("repo_name", repo_export_dir.name) or repo_export_dir.name),
                        **compute_cycle_stats(repo_path, validation_log_path),
                    }
                )
    (repo_export_dir / OUTPUT_FILE).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def iter_export_dirs(chat_root: Path) -> Iterable[Path]:
    for child in sorted(chat_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "_assets" or child.name.startswith("."):
            continue
        if (child / "meta.json").exists():
            yield child


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-root", type=Path, default=DEFAULT_CHAT_ROOT)
    parser.add_argument("--repo", action="append", default=[], help="Specific exported repo name(s) to refresh.")
    args = parser.parse_args()

    targets = set(args.repo or [])
    for repo_export_dir in iter_export_dirs(args.chat_root):
        if targets and repo_export_dir.name not in targets:
            continue
        export_repo(repo_export_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
