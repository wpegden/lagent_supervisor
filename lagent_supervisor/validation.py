from __future__ import annotations

from lagent_supervisor.shared import *
from lagent_supervisor.storage import JsonFile, append_jsonl
from lagent_supervisor.frontier import (
    load_validated_paper_main_results_manifest,
    normalize_repo_relative_path,
    normalize_repo_relative_path_list,
)

EXCLUDED_REPO_DIRS = {".git", ".lake", "build", "lake-packages", ".agent-supervisor"}

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


def capture_lean_tree_snapshot(config: Config) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    for path in repo_lean_files(config):
        rel = normalize_repo_relative_path(
            str(path.relative_to(config.repo_path)),
            label="Lean snapshot file",
            required_suffix=".lean",
        )
        snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def changed_lean_files_since_cycle_baseline(
    config: Config,
    cycle_baseline: Optional[Dict[str, Any]],
) -> List[str]:
    if not isinstance(cycle_baseline, dict):
        return []
    raw_files = cycle_baseline.get("files")
    if not isinstance(raw_files, dict):
        return []
    baseline_files: Dict[str, str] = {}
    for raw_path, raw_hash in raw_files.items():
        if not str(raw_path).strip() or not isinstance(raw_hash, str):
            continue
        rel = normalize_repo_relative_path(
            raw_path,
            label="cycle lean baseline file",
            required_suffix=".lean",
        )
        baseline_files[rel] = raw_hash
    current_files = capture_lean_tree_snapshot(config)
    changed = [
        path
        for path in sorted(set(baseline_files) | set(current_files))
        if baseline_files.get(path) != current_files.get(path)
    ]
    return changed


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
        for path in (
            git_summary.get("cycle_changed_lean_files")
            if isinstance(git_summary.get("cycle_changed_lean_files"), list)
            else (git_summary.get("changed_lean_files") or [])
        )
        if str(path).strip()
    ]
    allowed = normalize_repo_relative_path_list(
        worker_update.get("allowed_edit_paths"),
        label="theorem frontier worker update allowed_edit_paths",
        required_suffix=".lean",
        allow_empty=True,
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
        try:
            json.dumps(detail, ensure_ascii=False)
            entry["detail"] = detail
        except (TypeError, ValueError):
            entry["detail"] = repr(detail)
    print(f"WARNING [{category}] cycle {cycle}: {message}")
    try:
        append_jsonl(supervisor_warnings_log_path(config), entry)
    except (TypeError, ValueError, OSError) as exc:
        print(f"WARNING: Could not write warning log entry: {exc}")


def repo_lean_files(config: Config) -> List[Path]:
    results: List[Path] = []
    for current_root, dirnames, filenames in os.walk(config.repo_path):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDED_REPO_DIRS]
        for filename in filenames:
            if filename.endswith(".lean"):
                results.append(Path(current_root) / filename)
    return sorted(results)


def run_command(command: Sequence[str], cwd: Path) -> Dict[str, Any]:
    command_list = [str(part) for part in command]
    try:
        proc = subprocess.run(
            command_list,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        executable = command_list[0] if command_list else "<empty>"
        return {
            "command": command_list,
            "ok": False,
            "returncode": None,
            "output": f"Executable not found: {executable}",
        }
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "command": command_list,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": output,
    }


def has_lake_project(config: Config) -> bool:
    return (config.repo_path / "lakefile.toml").exists() or (config.repo_path / "lakefile.lean").exists()


def _lake_available() -> bool:
    return shutil.which("lake") is not None


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
    if not _lake_available():
        return {
            "path": relative_repo_label(config, path),
            "exists": True,
            "ok": False,
            "returncode": None,
            "output": "Executable not found: lake",
        }
    result = run_command(["lake", "env", "lean", str(path.relative_to(config.repo_path))], cwd=config.repo_path)
    return {
        "path": relative_repo_label(config, path),
        "exists": True,
        "ok": result["ok"],
        "returncode": result["returncode"],
        "output": result["output"],
    }


def _mask_lean_comments_and_strings(text: str) -> str:
    NORMAL = 0
    STRING = 1
    LINE_COMMENT = 2
    BLOCK_COMMENT = 3

    chars = list(text)
    masked = list(text)
    state = NORMAL
    block_depth = 0
    i = 0
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if state == NORMAL:
            if ch == '"':
                masked[i] = " "
                state = STRING
                i += 1
                continue
            if ch == "-" and nxt == "-":
                masked[i] = " "
                if i + 1 < len(masked):
                    masked[i + 1] = " "
                state = LINE_COMMENT
                i += 2
                continue
            if ch == "/" and nxt == "-":
                masked[i] = " "
                if i + 1 < len(masked):
                    masked[i + 1] = " "
                state = BLOCK_COMMENT
                block_depth = 1
                i += 2
                continue
            i += 1
            continue

        if state == STRING:
            if ch == "\\":
                masked[i] = " "
                if i + 1 < len(masked):
                    masked[i + 1] = "\n" if chars[i + 1] == "\n" else " "
                i += 2
                continue
            masked[i] = "\n" if ch == "\n" else " "
            if ch == '"':
                state = NORMAL
            i += 1
            continue

        if state == LINE_COMMENT:
            if ch == "\n":
                state = NORMAL
            else:
                masked[i] = " "
            i += 1
            continue

        if state == BLOCK_COMMENT:
            if ch == "/" and nxt == "-":
                masked[i] = " "
                if i + 1 < len(masked):
                    masked[i + 1] = " "
                block_depth += 1
                i += 2
                continue
            if ch == "-" and nxt == "/":
                masked[i] = " "
                if i + 1 < len(masked):
                    masked[i + 1] = " "
                block_depth -= 1
                i += 2
                if block_depth <= 0:
                    state = NORMAL
                continue
            masked[i] = "\n" if ch == "\n" else " "
            i += 1
            continue

    return "".join(masked)


def collect_sorries(config: Config) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    by_file: Dict[str, int] = {}
    pattern = re.compile(r"\bsorry\b")
    for path in repo_lean_files(config):
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        masked_text = _mask_lean_comments_and_strings(raw_text)
        rel = relative_repo_label(config, path)
        raw_lines = raw_text.splitlines()
        masked_lines = masked_text.splitlines()
        for lineno, (raw_line, masked_line) in enumerate(zip(raw_lines, masked_lines), start=1):
            if pattern.search(masked_line):
                entries.append({"path": rel, "line": lineno, "text": raw_line.strip()})
                by_file[rel] = by_file.get(rel, 0) + 1
    return {
        "count": len(entries),
        "entries": entries,
        "by_file": [{"path": path, "count": count} for path, count in sorted(by_file.items())],
    }


def collect_axioms(config: Config) -> Dict[str, Any]:
    approved = set(approved_axioms(config))
    found: List[Dict[str, Any]] = []
    pattern = re.compile(r"^\s*(axiom|constant)\s+([^\s:(]+)")
    for path in repo_lean_files(config):
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        masked_text = _mask_lean_comments_and_strings(raw_text)
        rel = relative_repo_label(config, path)
        raw_lines = raw_text.splitlines()
        masked_lines = masked_text.splitlines()
        for lineno, (raw_line, masked_line) in enumerate(zip(raw_lines, masked_lines), start=1):
            match = pattern.match(masked_line)
            if match:
                kind, name = match.groups()
                found.append(
                    {
                        "path": rel,
                        "line": lineno,
                        "kind": kind,
                        "name": name,
                        "approved": name in approved,
                        "text": raw_line.strip(),
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


def theorem_stating_allowed_sorry_files(config: Config) -> List[str]:
    allowed = {
        relative_repo_label(config, path)
        for path in repo_lean_files(config)
        if path.name == "PaperTheorems.lean"
    }
    allowed.add("repo/PaperTheorems.lean")
    return sorted(allowed)


def theorem_stating_allowed_edit_paths(config: Config) -> List[str]:
    allowed = {
        repo_relative_path(config, path)
        for path in repo_lean_files(config)
        if path.name in {"PaperDefinitions.lean", "PaperTheorems.lean"}
    }
    allowed.update({"PaperDefinitions.lean", "PaperTheorems.lean"})
    return sorted(allowed)


def _lean_import_modules_if_import_only(text: str) -> Optional[List[str]]:
    masked = _mask_lean_comments_and_strings(text)
    imports: List[str] = []
    for original_line, masked_line in zip(text.splitlines(), masked.splitlines()):
        stripped_masked = masked_line.strip()
        if not stripped_masked:
            continue
        if not stripped_masked.startswith("import "):
            return None
        stripped_original = original_line.strip()
        if not stripped_original.startswith("import "):
            return None
        tail = stripped_original[len("import ") :].strip()
        if not tail:
            return None
        parts = [part for part in tail.split() if part]
        if not parts:
            return None
        imports.extend(parts)
    return imports


def _allowed_theorem_stating_import_shim_path(
    config: Config,
    rel_path: str,
    *,
    allowed_statement_paths: Sequence[str],
) -> bool:
    normalized = normalize_repo_relative_path(
        rel_path,
        label="theorem_stating import shim path",
        required_suffix=".lean",
    )
    if "/" in normalized:
        return False
    if normalized in {"PaperDefinitions.lean", "PaperTheorems.lean"}:
        return False
    path = config.repo_path / normalized
    if not path.exists() or not path.is_file():
        return False
    if not (config.repo_path / path.stem).is_dir():
        return False
    imports = _lean_import_modules_if_import_only(read_text(path))
    if not imports:
        return False
    allowed_statement_suffixes = {
        Path(item).stem
        for item in allowed_statement_paths
        if str(item).endswith(("PaperDefinitions.lean", "PaperTheorems.lean"))
    }
    if not any(module.split(".")[-1] in allowed_statement_suffixes for module in imports):
        return False
    return True


def validation_theorem_stating_edit_policy(config: Config, phase: str, git_summary: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "enforced": False,
        "allowed_edit_paths": [],
        "changed_lean_files": [],
        "disallowed_changed_lean_files": [],
        "allowed_infrastructure_edit_paths": [],
    }
    if phase != "theorem_stating":
        return result
    changed = (
        [
            normalize_repo_relative_path(path, label="git.changed_lean_files entry", required_suffix=".lean")
            for path in (
                git_summary.get("cycle_changed_lean_files")
                if isinstance(git_summary.get("cycle_changed_lean_files"), list)
                else (git_summary.get("changed_lean_files") or [])
            )
            if str(path).strip()
        ]
        if isinstance(git_summary.get("cycle_changed_lean_files"), list)
        or isinstance(git_summary.get("changed_lean_files"), list)
        else []
    )
    allowed = theorem_stating_allowed_edit_paths(config)
    allowed_set = set(allowed)
    allowed_infrastructure = [
        path
        for path in changed
        if path not in allowed_set
        and _allowed_theorem_stating_import_shim_path(
            config,
            path,
            allowed_statement_paths=allowed,
        )
    ]
    disallowed = [path for path in changed if path not in allowed_set and path not in set(allowed_infrastructure)]
    return {
        "enforced": True,
        "allowed_edit_paths": allowed,
        "changed_lean_files": changed,
        "disallowed_changed_lean_files": disallowed,
        "allowed_infrastructure_edit_paths": allowed_infrastructure,
    }


def validation_sorry_policy(config: Config, phase: str, sorrys: Dict[str, Any]) -> Dict[str, Any]:
    if config.workflow.sorry_mode == "allowed":
        return {
            "mode": "allowed",
            "allowed_files": ["any"],
            "disallowed_entries": [],
        }
    if phase in {"theorem_stating", "proof_formalization", PHASE_PROOF_COMPLETE_STYLE_CLEANUP}:
        allowed_files = theorem_stating_allowed_sorry_files(config)
    else:
        allowed_files = []
    disallowed = []
    for entry in sorrys["entries"]:
        if not allowed_files or entry["path"] not in set(allowed_files):
            disallowed.append(entry)
    return {
        "mode": "default",
        "allowed_files": allowed_files,
        "disallowed_entries": disallowed,
    }


def run_validation(
    config: Config,
    phase: str,
    cycle: int,
    *,
    previous_validation: Optional[Dict[str, Any]] = None,
    cycle_baseline: Optional[Dict[str, Any]] = None,
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
            "node_count": 0,
            "edge_count": 0,
            "initial_active_node_id": "",
            "error": "",
        }
        if manifest_path.exists():
            try:
                manifest = load_validated_paper_main_results_manifest(config)
                manifest_summary["ok"] = True
                manifest_summary["node_count"] = len(manifest["nodes"])
                manifest_summary["edge_count"] = len(manifest["edges"])
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
        cycle_changed = changed_lean_files_since_cycle_baseline(config, cycle_baseline)
        summary["git"]["cycle_changed_lean_files"] = (
            cycle_changed if cycle_baseline is not None else list(summary["git"]["changed_lean_files"])
        )
        summary["theorem_stating_edit_policy"] = validation_theorem_stating_edit_policy(config, phase, summary["git"])

    git_summary = summary.get("git") if isinstance(summary.get("git"), dict) else {}
    summary["build_ok"] = bool((summary.get("build") or {}).get("ok"))
    summary["git_ok"] = bool(
        git_summary.get("enabled")
        and git_summary.get("repo_ok")
        and git_summary.get("worktree_clean")
        and git_summary.get("remote_matches_config")
    )
    summary["head"] = git_summary.get("head")

    summary["policy_ok"] = (
        summary["all_required_files_present"]
        and (summary["build"]["ok"] or phase in {"paper_check", "planning"})
        and not summary["sorry_policy"]["disallowed_entries"]
        and not summary["axioms"]["unapproved"]
        and all(check["ok"] for check in syntax_checks)
        and not summary.get("theorem_stating_edit_policy", {}).get("disallowed_changed_lean_files")
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
