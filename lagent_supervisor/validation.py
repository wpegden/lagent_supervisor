from __future__ import annotations

from lagent_supervisor.shared import *
from lagent_supervisor.storage import JsonFile, append_jsonl
from lagent_supervisor.frontier import (
    load_validated_paper_main_results_manifest,
    normalize_frontier_text,
    normalize_repo_relative_path,
    normalize_repo_relative_path_list,
    theorem_frontier_payload,
    theorem_frontier_node_children,
)

EXCLUDED_REPO_DIRS = {".git", ".lake", "build", "lake-packages", ".agent-supervisor"}


class ValidationRoutingError(SupervisorError):
    retry_role = "fatal"


class WorkerFixableValidationError(ValidationRoutingError):
    retry_role = "worker"


class ReviewerFixableValidationError(ValidationRoutingError):
    retry_role = "reviewer"

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


def validate_theorem_frontier_generated_edit_policy(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_update: Optional[Dict[str, Any]],
    validation_summary: Dict[str, Any],
) -> None:
    if not theorem_frontier_full_enabled(config, phase) or not isinstance(worker_update, dict):
        return
    action = str(worker_update.get("requested_action") or "").strip().upper()
    if action not in {"CLOSE", "REFACTOR"}:
        return
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return
    active_node_id = normalize_frontier_text(worker_update.get("active_node_id") or payload.get("active_node_id"))
    if not active_node_id:
        return
    proof_rel = repo_relative_path(config, theorem_frontier_generated_proof_path(config, active_node_id))
    statements_rel = repo_relative_path(config, theorem_frontier_generated_statements_path(config))
    generated_proofs_prefix = theorem_frontier_generated_proofs_dir(config).relative_to(config.repo_path).as_posix() + "/"
    allowed = normalize_repo_relative_path_list(
        worker_update.get("allowed_edit_paths"),
        label="theorem frontier worker update allowed_edit_paths",
        required_suffix=".lean",
        allow_empty=True,
    )
    cone = validation_summary.get("theorem_frontier_cone_files")
    changed = cone.get("changed_lean_files") if isinstance(cone, dict) and isinstance(cone.get("changed_lean_files"), list) else []

    if action == "CLOSE":
        if allowed != [proof_rel]:
            raise WorkerFixableValidationError(
                f"Theorem-frontier CLOSE cycles may edit only `{proof_rel}`. "
                f"Set allowed_edit_paths to exactly [{proof_rel!r}] and keep all other Lean files frozen."
            )
        unexpected = [path for path in changed if path != proof_rel]
        if unexpected:
            raise WorkerFixableValidationError(
                f"Theorem-frontier CLOSE cycles may change only `{proof_rel}`; found additional Lean edits {unexpected!r}."
            )
        return

    if proof_rel not in allowed:
        raise WorkerFixableValidationError(
            f"Theorem-frontier REFACTOR cycles must include the active generated proof file `{proof_rel}` in allowed_edit_paths."
        )
    forbidden = [
        path
        for path in changed
        if path == statements_rel or (path.startswith(generated_proofs_prefix) and path != proof_rel)
    ]
    if forbidden:
        raise WorkerFixableValidationError(
            "Theorem-frontier REFACTOR cycles must keep generated statements frozen and may edit only the active node's "
            f"generated proof file inside `{generated_proofs_prefix}`; found forbidden Lean edits {forbidden!r}."
        )


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


def normalize_lean_source_text(text: str) -> str:
    return " ".join(str(text or "").split())


def lean_module_name_for_path(config: Config, path: Path) -> str:
    rel = path.relative_to(config.repo_path).with_suffix("")
    return ".".join(rel.parts)


def collect_repo_lean_declarations(config: Config) -> Dict[str, Dict[str, Any]]:
    namespace_pattern = re.compile(r"^\s*namespace\s+([A-Za-z0-9_'.]+)\s*$")
    section_pattern = re.compile(r"^\s*section(?:\s+([A-Za-z0-9_'.]+))?\s*$")
    end_pattern = re.compile(r"^\s*end(?:\s+([A-Za-z0-9_'.]+))?\s*$")
    decl_pattern = re.compile(
        r"^\s*(?:@[^\n]+\s+)*"
        r"(?:(?:private|protected|noncomputable|unsafe|partial|scoped|mutual)\s+)*"
        r"(def|abbrev|theorem|lemma|axiom|constant|opaque)\s+([A-Za-z0-9_'.]+)"
    )
    declarations: Dict[str, Dict[str, Any]] = {}
    for path in repo_lean_files(config):
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        masked_lines = _mask_lean_comments_and_strings(raw_text).splitlines()
        namespace_parts: List[str] = []
        frames: List[Tuple[str, int]] = []
        for lineno, masked_line in enumerate(masked_lines, start=1):
            namespace_match = namespace_pattern.match(masked_line)
            if namespace_match:
                parts = [part for part in namespace_match.group(1).split(".") if part]
                namespace_parts.extend(parts)
                frames.append(("namespace", len(parts)))
                continue
            if section_pattern.match(masked_line):
                frames.append(("section", 0))
                continue
            end_match = end_pattern.match(masked_line)
            if end_match:
                if frames:
                    kind, count = frames.pop()
                    if kind == "namespace" and count > 0:
                        del namespace_parts[-count:]
                continue
            decl_match = decl_pattern.match(masked_line)
            if not decl_match:
                continue
            kind, raw_name = decl_match.groups()
            if raw_name.startswith("_root_."):
                full_name = raw_name[len("_root_.") :]
            else:
                pieces = [part for part in raw_name.split(".") if part]
                full_name = ".".join([*namespace_parts, *pieces]) if namespace_parts else ".".join(pieces)
            if not full_name:
                continue
            declarations[full_name] = {
                "name": full_name,
                "kind": kind,
                "path": path,
                "module": lean_module_name_for_path(config, path),
                "line": lineno,
            }
    return declarations


def theorem_frontier_statement_binding(
    config: Config,
    node: Dict[str, Any],
    declaration_index: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    node_id = str(node.get("node_id") or "").strip() or "<unknown>"
    anchor = normalize_frontier_text(node.get("lean_anchor"))
    if not anchor:
        raise SupervisorError(f"Theorem-frontier node {node_id!r} is missing lean_anchor.")
    declaration = declaration_index.get(anchor)
    if not isinstance(declaration, dict):
        raise SupervisorError(
            f"Theorem-frontier node {node_id!r} names missing Lean statement anchor {anchor!r}."
        )
    source_text = normalize_lean_source_text(read_text(Path(declaration["path"])))
    expected_text = normalize_lean_source_text(node.get("lean_statement"))
    if expected_text not in source_text:
        raise SupervisorError(
            f"Theorem-frontier node {node_id!r} has lean_statement text that does not appear in "
            f"{relative_repo_label(config, Path(declaration['path']))} for anchor {anchor!r}."
        )
    return declaration


def theorem_frontier_proof_check_path(config: Config, label: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label.strip() or "node")
    return config.state_dir / "lean-frontier-checks" / f"{safe}.lean"


_LEAN_DECL_RE = re.compile(
    r"^\s*(?:@[^\n]+\s+)*"
    r"(?:(?:private|protected|noncomputable|unsafe|partial|scoped|mutual)\s+)*"
    r"(def|abbrev|theorem|lemma|axiom|constant|opaque)\s+([A-Za-z0-9_'.]+)"
)
_GENERATED_PROOF_SIGNATURE_RE = re.compile(r"frontier-signature:\s*([0-9a-f]{64})")


def theorem_frontier_generated_statement_info(config: Config, node: Dict[str, Any]) -> Dict[str, str]:
    node_id = normalize_frontier_text(node.get("node_id")) or "<unknown>"
    statement_text = str(node.get("lean_statement") or "").strip()
    match = _LEAN_DECL_RE.search(statement_text)
    if not match:
        raise SupervisorError(
            f"Theorem-frontier node {node_id!r} has lean_statement that is not a standalone Lean declaration."
        )
    declaration_name = match.group(2).split(".")[-1]
    slug = theorem_frontier_generated_node_slug(node_id)
    module_root = theorem_frontier_generated_module_root(config)
    statement_anchor = f"{module_root}.Statements.{slug}.{declaration_name}"
    proof_module = f"{module_root}.Proofs.{slug}"
    proof_anchor = f"{proof_module}.prove_from_children"
    return {
        "node_id": node_id,
        "slug": slug,
        "declaration_name": declaration_name,
        "statement_anchor": statement_anchor,
        "proof_module": proof_module,
        "proof_anchor": proof_anchor,
    }


def _namespace_open_lines(parts: Sequence[str]) -> List[str]:
    return [f"namespace {part}" for part in parts if part]


def _namespace_close_lines(parts: Sequence[str]) -> List[str]:
    return [f"end {part}" for part in reversed([part for part in parts if part])]


def _generated_frontier_import_modules(config: Config) -> List[str]:
    modules: List[str] = []
    generated_root = theorem_frontier_generated_dir(config).resolve()
    for path in repo_lean_files(config):
        try:
            path.resolve().relative_to(generated_root)
            continue
        except ValueError:
            pass
        modules.append(lean_module_name_for_path(config, path))
    return sorted(dict.fromkeys(modules))


def theorem_frontier_generated_proof_signature(
    config: Config,
    node: Dict[str, Any],
    child_nodes: Sequence[Dict[str, Any]],
) -> str:
    info = theorem_frontier_generated_statement_info(config, node)
    child_infos = [theorem_frontier_generated_statement_info(config, child) for child in child_nodes]
    payload = {
        "statement_anchor": info["statement_anchor"],
        "child_statement_anchors": [entry["statement_anchor"] for entry in child_infos],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def theorem_frontier_generated_statements_source(config: Config, nodes: Dict[str, Dict[str, Any]]) -> str:
    imports = _generated_frontier_import_modules(config)
    lines: List[str] = [*(f"import {module}" for module in imports), ""]
    namespace_parts = theorem_frontier_generated_module_root(config).split(".") + ["Statements"]
    lines.extend([*_namespace_open_lines(namespace_parts), ""])
    for node_id in sorted(nodes):
        node = nodes[node_id]
        if not isinstance(node, dict):
            continue
        info = theorem_frontier_generated_statement_info(config, node)
        statement_text = str(node.get("lean_statement") or "").strip()
        lines.extend(
            [
                f"namespace {info['slug']}",
                "",
                statement_text,
                "",
                f"end {info['slug']}",
                "",
            ]
        )
    lines.extend([*_namespace_close_lines(namespace_parts), ""])
    return "\n".join(lines)


def theorem_frontier_generated_proof_scaffold(
    config: Config,
    node: Dict[str, Any],
    child_nodes: Sequence[Dict[str, Any]],
) -> str:
    info = theorem_frontier_generated_statement_info(config, node)
    signature = theorem_frontier_generated_proof_signature(config, node, child_nodes)
    child_infos = [theorem_frontier_generated_statement_info(config, child) for child in child_nodes]
    theorem_lines = ["theorem prove_from_children"]
    for index, child_info in enumerate(child_infos):
        theorem_lines.append(f"  (h{index} : _root_.{child_info['statement_anchor']})")
    theorem_lines.append(f"  : _root_.{info['statement_anchor']} := by")
    theorem_lines.append("  ...")
    theorem_block = "\n".join(theorem_lines)
    namespace_parts = theorem_frontier_generated_module_root(config).split(".") + ["Proofs", info["slug"]]
    return "\n".join(
        [
            f"import {theorem_frontier_generated_module_root(config)}.Statements",
            "",
            *_namespace_open_lines(namespace_parts),
            "",
            f"/- frontier-signature: {signature} -/",
            "/-",
            "Fill in the required theorem below and keep its name and type exact.",
            "",
            theorem_block,
            "-/",
            "",
            *_namespace_close_lines(namespace_parts),
            "",
        ]
    )


def sync_theorem_frontier_generated_files(
    config: Config,
    state: Dict[str, Any],
    *,
    ensure_active_proof: bool = True,
    ensure_proof_node_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return {}
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        return {}
    statements_path = theorem_frontier_generated_statements_path(config)
    statements_path.parent.mkdir(parents=True, exist_ok=True)
    statements_path.write_text(theorem_frontier_generated_statements_source(config, nodes), encoding="utf-8")

    active_node_id = normalize_frontier_text(payload.get("active_node_id"))
    proof_node_ids: List[str] = []
    seen_proof_node_ids: Set[str] = set()

    def _queue_proof_node(node_id: str) -> None:
        normalized = normalize_frontier_text(node_id)
        if not normalized or normalized in seen_proof_node_ids:
            return
        if normalized not in nodes or not isinstance(nodes[normalized], dict):
            return
        seen_proof_node_ids.add(normalized)
        proof_node_ids.append(normalized)

    if ensure_active_proof:
        _queue_proof_node(active_node_id)
    for raw_node_id in ensure_proof_node_ids or ():
        _queue_proof_node(str(raw_node_id))

    proof_paths_by_node_id: Dict[str, str] = {}
    proof_anchors_by_node_id: Dict[str, str] = {}
    for proof_node_id in proof_node_ids:
        proof_node = nodes[proof_node_id]
        child_nodes = [
            nodes[child_id]
            for child_id in theorem_frontier_node_children(nodes, edges, proof_node_id)
            if child_id in nodes and isinstance(nodes[child_id], dict)
        ]
        proof_path = theorem_frontier_generated_proof_path(config, proof_node_id)
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        scaffold = theorem_frontier_generated_proof_scaffold(config, proof_node, child_nodes)
        existing = proof_path.read_text(encoding="utf-8", errors="replace") if proof_path.exists() else ""
        expected_signature = theorem_frontier_generated_proof_signature(config, proof_node, child_nodes)
        match = _GENERATED_PROOF_SIGNATURE_RE.search(existing)
        if match is None or match.group(1) != expected_signature:
            proof_path.write_text(scaffold, encoding="utf-8")
        info = theorem_frontier_generated_statement_info(config, proof_node)
        proof_paths_by_node_id[proof_node_id] = relative_repo_label(config, proof_path)
        proof_anchors_by_node_id[proof_node_id] = info["proof_anchor"]

    active_proof_path = proof_paths_by_node_id.get(active_node_id, "")
    active_proof_anchor = proof_anchors_by_node_id.get(active_node_id, "")
    return {
        "statements_path": relative_repo_label(config, statements_path),
        "active_proof_path": active_proof_path,
        "active_proof_anchor": active_proof_anchor,
        "proof_paths_by_node_id": proof_paths_by_node_id,
    }


def validate_theorem_frontier_generated_local_proof(
    config: Config,
    node: Dict[str, Any],
    child_nodes: Sequence[Dict[str, Any]],
    *,
    label: str,
) -> Dict[str, Any]:
    info = theorem_frontier_generated_statement_info(config, node)
    child_infos = [theorem_frontier_generated_statement_info(config, child) for child in child_nodes]
    proof_path = theorem_frontier_generated_proof_path(config, info["node_id"])
    if not proof_path.exists():
        raise SupervisorError(
            f"Theorem-frontier generated proof file is missing for {info['node_id']!r}: {relative_repo_label(config, proof_path)}"
        )
    imports = [f"{theorem_frontier_generated_module_root(config)}.Statements", info["proof_module"]]
    binders = [f"(h{index} : _root_.{child_info['statement_anchor']})" for index, child_info in enumerate(child_infos)]
    args = " ".join(f"h{index}" for index in range(len(child_infos)))
    exact_line = f"  exact _root_.{info['proof_anchor']}" if not args else f"  exact _root_.{info['proof_anchor']} {args}"
    proof_source = "\n".join(
        [
            *[f"import {module}" for module in imports],
            "",
            f"example {' '.join(binders)} : _root_.{info['statement_anchor']} := by",
            exact_line,
            "",
        ]
    )
    check_path = theorem_frontier_proof_check_path(config, label)
    check_path.parent.mkdir(parents=True, exist_ok=True)
    check_path.write_text(proof_source, encoding="utf-8")
    result = run_command(["lake", "env", "lean", str(check_path.relative_to(config.repo_path))], cwd=config.repo_path)
    if not result["ok"]:
        raise SupervisorError(
            "Theorem-frontier generated proof check failed for "
            f"{info['node_id']!r} via {info['proof_anchor']!r}: {trim_text(result['output'], 4000)}"
        )
    return {
        "node_id": info["node_id"],
        "statement_anchor": info["statement_anchor"],
        "proof_anchor": info["proof_anchor"],
        "imports": imports,
        "proof_path": relative_repo_label(config, proof_path),
        "check_path": relative_repo_label(config, check_path),
        "child_statement_anchors": [entry["statement_anchor"] for entry in child_infos],
    }


def verify_theorem_frontier_close_attempt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    worker_update: Dict[str, Any],
    *,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    if not theorem_frontier_full_enabled(config, phase):
        raise SupervisorError("Theorem-frontier CLOSE verification is only available in full theorem-frontier mode.")
    if str(worker_update.get("requested_action") or "").strip().upper() != "CLOSE":
        raise SupervisorError("Theorem-frontier CLOSE verification requires requested_action = 'CLOSE'.")

    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        raise SupervisorError("Missing theorem-frontier payload in state.")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        raise SupervisorError("Theorem-frontier payload is missing node/edge data.")
    active_node_id = normalize_frontier_text(worker_update.get("active_node_id"))
    generated = sync_theorem_frontier_generated_files(
        config,
        state,
        ensure_active_proof=False,
        ensure_proof_node_ids=[active_node_id],
    )

    active_node = nodes.get(active_node_id) if active_node_id else None
    if not isinstance(active_node, dict):
        raise SupervisorError(
            f"Theorem-frontier CLOSE verification requires an authoritative active node, got {active_node_id!r}."
        )
    child_nodes = [
        nodes[child_id]
        for child_id in theorem_frontier_node_children(nodes, edges, active_node_id)
        if child_id in nodes and isinstance(nodes[child_id], dict)
    ]
    proof_check = validate_theorem_frontier_generated_local_proof(
        config,
        active_node,
        child_nodes,
        label=label or f"cycle-{cycle:04d}-{active_node_id.replace('.', '-')}",
    )
    return {
        "phase": phase,
        "cycle": int(cycle),
        "active_node_id": active_node_id,
        "statement_anchor": proof_check["statement_anchor"],
        "proof_anchor": proof_check["proof_anchor"],
        "proof_module": theorem_frontier_generated_statement_info(config, active_node)["proof_module"],
        "child_node_ids": [str(child.get("node_id") or "") for child in child_nodes],
        "child_statement_anchors": list(proof_check["child_statement_anchors"]),
        "check_path": proof_check["check_path"],
        "imports": list(proof_check["imports"]),
        "statements_path": str(generated.get("statements_path") or ""),
        "proof_path": str(proof_check["proof_path"] or ""),
    }


def verify_theorem_frontier_refactor_attempt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    worker_update: Dict[str, Any],
) -> Dict[str, Any]:
    if not theorem_frontier_full_enabled(config, phase):
        raise SupervisorError("Theorem-frontier REFACTOR verification is only available in full theorem-frontier mode.")
    if str(worker_update.get("requested_action") or "").strip().upper() != "REFACTOR":
        raise SupervisorError("Theorem-frontier REFACTOR verification requires requested_action = 'REFACTOR'.")
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        raise SupervisorError("Missing theorem-frontier payload in state.")
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        raise SupervisorError("Theorem-frontier payload is missing node/edge data.")
    proved_node_ids = sorted(
        node_id
        for node_id, node in nodes.items()
        if isinstance(node, dict) and str(node.get("lean_proof_status") or "").strip().lower() == "proved"
    )
    generated = sync_theorem_frontier_generated_files(
        config,
        state,
        ensure_active_proof=True,
        ensure_proof_node_ids=proved_node_ids,
    )
    for node_id in proved_node_ids:
        child_nodes = [
            nodes[child_id]
            for child_id in theorem_frontier_node_children(nodes, edges, node_id)
            if child_id in nodes and isinstance(nodes[child_id], dict)
        ]
        validate_theorem_frontier_generated_local_proof(
            config,
            nodes[node_id],
            child_nodes,
            label=f"refactor-{cycle:04d}-{node_id.replace('.', '-')}",
        )
    return {
        "phase": phase,
        "cycle": int(cycle),
        "active_node_id": normalize_frontier_text(payload.get("active_node_id")),
        "proved_node_ids": proved_node_ids,
        "statements_path": str(generated.get("statements_path") or ""),
        "active_proof_path": str(generated.get("active_proof_path") or ""),
    }


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
