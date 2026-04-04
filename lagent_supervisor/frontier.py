from __future__ import annotations

import re

from lagent_supervisor.shared import *
from lagent_supervisor.storage import JsonFile, append_jsonl


_BACKTICK_REF_RE = re.compile(r"`([^`]+)`")
_PAPER_LABEL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]*:[A-Za-z0-9_.-]+\b")


def _reject_pipe(text: str, *, label: str) -> None:
    if "|" in text:
        raise SupervisorError(f"{label} may not contain '|': {text!r}")


def normalize_frontier_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def normalize_frontier_enum(value: Any, allowed: Sequence[str], *, label: str) -> str:
    normalized = normalize_frontier_text(value)
    if not normalized:
        raise SupervisorError(f"{label} must be non-empty.")
    normalized = normalized.upper() if all(item.isupper() for item in allowed) else normalized.lower()
    if normalized not in allowed:
        raise SupervisorError(f"Invalid {label} {value!r}. Expected one of {list(allowed)}.")
    return normalized


def normalize_frontier_text_list(value: Any, *, label: str, allow_empty: bool = True) -> List[str]:
    if value in (None, ""):
        if allow_empty:
            return []
        raise SupervisorError(f"{label} must be a non-empty list.")
    if not isinstance(value, list):
        raise SupervisorError(f"{label} must be a list.")
    cleaned: List[str] = []
    for item in value:
        text = normalize_frontier_text(item)
        if not text:
            raise SupervisorError(f"{label} must not contain empty entries.")
        cleaned.append(text)
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned and not allow_empty:
        raise SupervisorError(f"{label} must be a non-empty list.")
    return cleaned


def normalize_repo_relative_path(value: Any, *, label: str, required_suffix: Optional[str] = None) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise SupervisorError(f"{label} must be non-empty.")
    pure = PurePosixPath(text)
    if pure.is_absolute() or text.startswith("/") or ":" in pure.parts[0]:
        raise SupervisorError(f"{label} must be a repo-relative path, not {value!r}.")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise SupervisorError(f"{label} must not contain empty, '.' or '..' path segments.")
    normalized = pure.as_posix()
    if required_suffix and not normalized.endswith(required_suffix):
        raise SupervisorError(f"{label} must end with {required_suffix!r}.")
    return normalized


def normalize_repo_relative_path_list(
    value: Any,
    *,
    label: str,
    required_suffix: Optional[str] = None,
    allow_empty: bool = False,
) -> List[str]:
    if value in (None, ""):
        if allow_empty:
            return []
        raise SupervisorError(f"{label} must be a non-empty list.")
    if not isinstance(value, list):
        raise SupervisorError(f"{label} must be a list.")
    cleaned: List[str] = []
    for idx, item in enumerate(value):
        cleaned.append(
            normalize_repo_relative_path(
                item,
                label=f"{label}[{idx}]",
                required_suffix=required_suffix,
            )
        )
    cleaned = list(dict.fromkeys(cleaned))
    if not cleaned and not allow_empty:
        raise SupervisorError(f"{label} must be a non-empty list.")
    return cleaned


def validate_theorem_frontier_action(value: Any) -> str:
    action = normalize_frontier_text(value).upper()
    if action not in THEOREM_FRONTIER_ACTIONS:
        raise SupervisorError(f"Invalid theorem frontier action {value!r}")
    return action


def validate_theorem_frontier_outcome(value: Any) -> str:
    outcome = normalize_frontier_text(value).upper()
    if outcome not in THEOREM_FRONTIER_OUTCOMES:
        raise SupervisorError(f"Invalid theorem frontier outcome {value!r}")
    return outcome


def theorem_frontier_node_kind(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_NODE_KINDS, label="theorem frontier node kind")


def theorem_frontier_node_status(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_NODE_STATUSES, label="theorem frontier node status")


def theorem_frontier_paper_decision(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_PAPER_DECISIONS, label="paper verifier decision")


def theorem_frontier_paper_classification(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_PAPER_CLASSIFICATIONS, label="paper verifier classification")


def theorem_frontier_cone_purity(value: Any) -> str:
    return normalize_frontier_enum(value, THEOREM_FRONTIER_CONE_PURITY_LEVELS, label="theorem frontier cone purity")


def theorem_frontier_payload(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = state.get("theorem_frontier")
    if isinstance(payload, dict):
        return payload
    return None


def theorem_frontier_current(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return None
    current = payload.get("current")
    if isinstance(current, dict):
        return current
    return payload


def theorem_frontier_active_node_id(state: Dict[str, Any]) -> str:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return ""
    return normalize_frontier_text(payload.get("active_node_id"))


def _relationship_sets(
    nodes: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    parents: Dict[str, Set[str]] = {node_id: set() for node_id in nodes}
    children: Dict[str, Set[str]] = {node_id: set() for node_id in nodes}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        parent = normalize_frontier_text(edge.get("parent"))
        child = normalize_frontier_text(edge.get("child"))
        if not parent or not child:
            continue
        if parent in children:
            children[parent].add(child)
        if child in parents:
            parents[child].add(parent)
    return parents, children


def _assert_relationship_consistency(nodes: Dict[str, Dict[str, Any]], edges: Sequence[Dict[str, Any]]) -> None:
    expected_parents, expected_children = _relationship_sets(nodes, edges)
    for node_id, node in nodes.items():
        actual_parents = set(node.get("parent_ids") or [])
        actual_children = set(node.get("child_ids") or [])
        if actual_parents != expected_parents[node_id]:
            raise SupervisorError(
                f"Theorem-frontier node {node_id!r} parent_ids do not match the declared edges: "
                f"expected {sorted(expected_parents[node_id])!r}, got {sorted(actual_parents)!r}."
            )
        if actual_children != expected_children[node_id]:
            raise SupervisorError(
                f"Theorem-frontier node {node_id!r} child_ids do not match the declared edges: "
                f"expected {sorted(expected_children[node_id])!r}, got {sorted(actual_children)!r}."
            )


def _assert_acyclic_dependency_graph(nodes: Dict[str, Dict[str, Any]], edges: Sequence[Dict[str, Any]]) -> None:
    adjacency: Dict[str, List[str]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        parent = normalize_frontier_text(edge.get("parent"))
        child = normalize_frontier_text(edge.get("child"))
        if not parent or not child:
            continue
        adjacency.setdefault(parent, []).append(child)

    visiting: Set[str] = set()
    visited: Set[str] = set()
    trail: List[str] = []

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            cycle = trail[trail.index(node_id):] + [node_id]
            raise SupervisorError(f"Theorem-frontier dependency graph must be acyclic; found cycle {cycle!r}.")
        visiting.add(node_id)
        trail.append(node_id)
        for child_id in adjacency.get(node_id, []):
            if child_id not in nodes:
                raise SupervisorError(
                    f"Theorem-frontier dependency graph references missing child {child_id!r} from {node_id!r}."
                )
            visit(child_id)
        trail.pop()
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in nodes:
        visit(node_id)

def assert_theorem_frontier_review_matches_node(review: Dict[str, Any], node: Dict[str, Any]) -> None:
    review_id = normalize_frontier_text(review.get("active_node_id"))
    node_id = normalize_frontier_text(node.get("node_id"))
    if review_id != node_id:
        raise SupervisorError(
            f"Theorem-frontier review active_node_id {review_id!r} does not match "
            f"the authoritative active node {node_id!r}."
        )


def theorem_frontier_node_children(
    nodes: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
    node_id: str,
) -> List[str]:
    if node_id not in nodes:
        return []
    return sorted(
        normalize_frontier_text(edge.get("child"))
        for edge in edges
        if isinstance(edge, dict) and normalize_frontier_text(edge.get("parent")) == node_id
    )


def theorem_frontier_node_parents(
    nodes: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
    node_id: str,
) -> List[str]:
    if node_id not in nodes:
        return []
    return sorted(
        normalize_frontier_text(edge.get("parent"))
        for edge in edges
        if isinstance(edge, dict) and normalize_frontier_text(edge.get("child")) == node_id
    )


def _proof_backticked_refs(text: str) -> Set[str]:
    return {
        normalize_frontier_text(match.group(1))
        for match in _BACKTICK_REF_RE.finditer(str(text or ""))
        if normalize_frontier_text(match.group(1))
    }


def _paper_label_refs(text: str) -> Set[str]:
    return {match.group(0).lower() for match in _PAPER_LABEL_RE.finditer(str(text or ""))}


def _assert_local_node_proof(
    nodes: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
    node_id: str,
    *,
    context_label: str,
) -> None:
    node = nodes[node_id]
    children = theorem_frontier_node_children(nodes, edges, node_id)
    proof = str(node.get("natural_language_proof") or "")
    proof_refs = _proof_backticked_refs(proof)
    allowed_node_refs = {node_id, *children}
    extraneous_node_refs = sorted(
        ref for ref in proof_refs if ref in nodes and ref not in allowed_node_refs
    )
    if extraneous_node_refs:
        raise SupervisorError(
            f"{context_label} node {node_id!r} natural_language_proof may cite only itself and its current children; "
            f"found out-of-scope node refs {extraneous_node_refs!r}."
        )
    missing_child_refs = sorted(child_id for child_id in children if child_id not in proof_refs)
    if missing_child_refs:
        raise SupervisorError(
            f"{context_label} node {node_id!r} natural_language_proof must explicitly cite every current child node id "
            f"in backticks; missing {missing_child_refs!r}."
        )
    allowed_labels = set(_paper_label_refs(node.get("paper_provenance")))
    for child_id in children:
        allowed_labels.update(_paper_label_refs(nodes[child_id].get("paper_provenance")))
    extra_labels = sorted(label for label in _paper_label_refs(proof) if label not in allowed_labels)
    if extra_labels:
        raise SupervisorError(
            f"{context_label} node {node_id!r} natural_language_proof cites paper labels not represented by the node "
            f"or its current children: {extra_labels!r}. Add those dependencies as child nodes or refactor the proof."
        )


def assert_local_node_proofs(
    nodes: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
    *,
    context_label: str,
) -> None:
    for node_id in sorted(nodes):
        _assert_local_node_proof(nodes, edges, node_id, context_label=context_label)


def theorem_frontier_effective_node_status(
    nodes: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
    node_id: str,
    memo: Optional[Dict[str, str]] = None,
    visiting: Optional[Set[str]] = None,
) -> str:
    if memo is None:
        memo = {}
    if visiting is None:
        visiting = set()
    if node_id in memo:
        return memo[node_id]
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        raise SupervisorError(f"Unknown theorem-frontier node {node_id!r}.")
    raw_status = str(node.get("status", "open"))
    if raw_status in {"refuted", "replaced"}:
        memo[node_id] = raw_status
        return raw_status
    if node_id in visiting:
        memo[node_id] = raw_status if raw_status in {"active", "frozen", "proposed"} else "open"
        return memo[node_id]
    visiting.add(node_id)
    children = theorem_frontier_node_children(nodes, edges, node_id)
    all_children_closed = all(
        theorem_frontier_effective_node_status(nodes, edges, child_id, memo, visiting) == "closed"
        for child_id in children
    )
    visiting.remove(node_id)
    if raw_status == "closed" and all_children_closed:
        memo[node_id] = "closed"
    elif raw_status in {"active", "frozen", "proposed"}:
        memo[node_id] = raw_status
    else:
        memo[node_id] = "open"
    return memo[node_id]


def theorem_frontier_node_closure_check(
    nodes: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
    node_id: str,
) -> Tuple[bool, str]:
    if node_id not in nodes:
        raise SupervisorError(f"Unknown theorem-frontier node {node_id!r}.")
    children = theorem_frontier_node_children(nodes, edges, node_id)
    unresolved = [
        child_id
        for child_id in children
        if theorem_frontier_effective_node_status(nodes, edges, child_id) != "closed"
    ]
    if unresolved:
        return False, f"required child nodes are not closed: {unresolved!r}"
    return True, ""


def repair_theorem_frontier_closed_nodes(
    nodes: Dict[str, Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
) -> List[str]:
    reopened: List[str] = []
    while True:
        changed = False
        for node_id, node in nodes.items():
            if not isinstance(node, dict) or node.get("status") != "closed":
                continue
            closable, _reason = theorem_frontier_node_closure_check(nodes, edges, node_id)
            if closable:
                continue
            node["status"] = "open"
            node["updated_at"] = timestamp_now()
            reopened.append(node_id)
            changed = True
        if not changed:
            break
    return reopened


def sync_theorem_frontier_metrics(payload: Dict[str, Any]) -> None:
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        return
    edges = payload.get("edges")
    if not isinstance(edges, list):
        return
    metrics = payload.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        return
    effective_statuses = {
        node_id: theorem_frontier_effective_node_status(nodes, edges, node_id)
        for node_id in nodes
    }
    metrics["closed_nodes_count"] = sum(
        1
        for node_id in nodes
        if effective_statuses.get(node_id) == "closed"
    )
    metrics["refuted_nodes_count"] = sum(
        1
        for node in nodes.values()
        if isinstance(node, dict) and node.get("status") in {"refuted", "replaced"}
    )
    metrics["paper_nodes_closed"] = sum(
        1
        for node_id, node in nodes.items()
        if isinstance(node, dict)
        and effective_statuses.get(node_id) == "closed"
        and node.get("kind") in {"paper", "paper_faithful_reformulation"}
    )


def validate_theorem_frontier_node(
    node: Dict[str, Any],
    *,
    require_relationships: bool,
    require_status: bool,
) -> Dict[str, Any]:
    required_keys = {
        "node_id",
        "kind",
        "natural_language_statement",
        "natural_language_proof",
        "lean_statement",
        "lean_anchor",
        "paper_provenance",
        "blocker_cluster",
        "acceptance_evidence",
        "notes",
    }
    if require_relationships:
        required_keys.update({"parent_ids", "child_ids"})
    if require_status:
        required_keys.add("status")
    missing = required_keys.difference(node)
    if missing:
        raise SupervisorError(f"Theorem-frontier node missing keys: {sorted(missing)}")
    validated = dict(node)
    validated["node_id"] = normalize_frontier_text(validated.get("node_id"))
    _reject_pipe(validated["node_id"], label="Theorem-frontier node_id")
    validated["kind"] = theorem_frontier_node_kind(validated.get("kind"))
    validated["natural_language_statement"] = normalize_frontier_text(validated.get("natural_language_statement"))
    validated["natural_language_proof"] = normalize_frontier_text(validated.get("natural_language_proof"))
    validated["lean_statement"] = normalize_frontier_text(validated.get("lean_statement"))
    validated["lean_anchor"] = normalize_frontier_text(validated.get("lean_anchor"))
    validated["paper_provenance"] = normalize_frontier_text(validated.get("paper_provenance"))
    validated["blocker_cluster"] = normalize_frontier_text(validated.get("blocker_cluster"))
    validated["acceptance_evidence"] = normalize_frontier_text(validated.get("acceptance_evidence"))
    validated["notes"] = normalize_frontier_text(validated.get("notes"))
    display_label = normalize_frontier_text(validated.get("display_label"))
    if display_label:
        validated["display_label"] = display_label
    else:
        validated.pop("display_label", None)
    if require_status:
        validated["status"] = theorem_frontier_node_status(validated.get("status"))
    if require_relationships:
        validated["parent_ids"] = normalize_frontier_text_list(validated.get("parent_ids"), label="node.parent_ids")
        validated["child_ids"] = normalize_frontier_text_list(validated.get("child_ids"), label="node.child_ids")
        for parent_id in validated["parent_ids"]:
            _reject_pipe(parent_id, label="Theorem-frontier parent_ids entry")
        for child_id in validated["child_ids"]:
            _reject_pipe(child_id, label="Theorem-frontier child_ids entry")
        if len(validated["parent_ids"]) != len(set(validated["parent_ids"])):
            raise SupervisorError(f"Theorem-frontier node {validated['node_id']!r} has duplicate parent_ids.")
        if len(validated["child_ids"]) != len(set(validated["child_ids"])):
            raise SupervisorError(f"Theorem-frontier node {validated['node_id']!r} has duplicate child_ids.")
        if validated["node_id"] in set(validated["child_ids"]):
            raise SupervisorError(f"Theorem-frontier node {validated['node_id']!r} may not be its own child.")
        if validated["node_id"] in set(validated["parent_ids"]):
            raise SupervisorError(f"Theorem-frontier node {validated['node_id']!r} may not be its own parent.")
    for key in (
        "node_id",
        "natural_language_statement",
        "natural_language_proof",
        "lean_statement",
        "lean_anchor",
        "paper_provenance",
    ):
        if not validated[key]:
            raise SupervisorError(f"Theorem-frontier node field {key} must be non-empty.")
    return validated


def validate_theorem_frontier_edge(edge: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {"parent", "child"}
    missing = required_keys.difference(edge)
    if missing:
        raise SupervisorError(f"Theorem-frontier edge missing keys: {sorted(missing)}")
    validated = {
        "parent": normalize_frontier_text(edge.get("parent")),
        "child": normalize_frontier_text(edge.get("child")),
    }
    _reject_pipe(validated["parent"], label="Theorem-frontier edge parent")
    _reject_pipe(validated["child"], label="Theorem-frontier edge child")
    if not validated["parent"] or not validated["child"]:
        raise SupervisorError("Theorem-frontier edge fields parent and child must be non-empty.")
    if validated["parent"] == validated["child"]:
        raise SupervisorError("Theorem-frontier edges may not be self-edges.")
    return validated


def theorem_frontier_node_record(
    node: Dict[str, Any],
    *,
    status: str,
    parent_ids: Optional[Sequence[str]] = None,
    child_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    record = dict(node)
    record["status"] = theorem_frontier_node_status(status)
    record["parent_ids"] = list(dict.fromkeys(parent_ids or []))
    record["child_ids"] = list(dict.fromkeys(child_ids or []))
    record["updated_at"] = timestamp_now()
    return validate_theorem_frontier_node(record, require_relationships=True, require_status=True)


def upsert_theorem_frontier_node(
    nodes: Dict[str, Dict[str, Any]],
    node: Dict[str, Any],
    *,
    default_status: str,
) -> Dict[str, Any]:
    existing = nodes.get(node["node_id"])
    parent_ids = existing.get("parent_ids", []) if isinstance(existing, dict) else []
    child_ids = existing.get("child_ids", []) if isinstance(existing, dict) else []
    status = existing.get("status", default_status) if isinstance(existing, dict) else default_status
    record = theorem_frontier_node_record(node, status=status, parent_ids=parent_ids, child_ids=child_ids)
    nodes[record["node_id"]] = record
    return record


def add_theorem_frontier_edge(payload: Dict[str, Any], edge: Dict[str, Any]) -> None:
    nodes = payload.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        raise SupervisorError("Theorem-frontier payload nodes must be a mapping.")
    edges = payload.setdefault("edges", [])
    if not isinstance(edges, list):
        raise SupervisorError("Theorem-frontier payload edges must be a list.")
    edge_record = validate_theorem_frontier_edge(edge)
    if edge_record["parent"] not in nodes or edge_record["child"] not in nodes:
        raise SupervisorError(
            "Cannot add a theorem-frontier edge unless both endpoints are already present in the authoritative DAG: "
            f"{edge_record['parent']!r} -> {edge_record['child']!r}."
        )
    for existing in edges:
        if (
            isinstance(existing, dict)
            and normalize_frontier_text(existing.get("parent")) == edge_record["parent"]
            and normalize_frontier_text(existing.get("child")) == edge_record["child"]
        ):
            return
    edge_record["updated_at"] = timestamp_now()
    edges.append(edge_record)
    parent = nodes.get(edge_record["parent"])
    child = nodes.get(edge_record["child"])
    if isinstance(parent, dict):
        parent["child_ids"] = list(dict.fromkeys([*parent.get("child_ids", []), edge_record["child"]]))
    if isinstance(child, dict):
        child["parent_ids"] = list(dict.fromkeys([*child.get("parent_ids", []), edge_record["parent"]]))


def _recompute_relationships(payload: Dict[str, Any]) -> None:
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        raise SupervisorError("Theorem-frontier payload must contain node and edge collections.")
    expected_parents, expected_children = _relationship_sets(nodes, edges)
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        node["parent_ids"] = sorted(expected_parents[node_id])
        node["child_ids"] = sorted(expected_children[node_id])
        node["updated_at"] = node.get("updated_at") or timestamp_now()


def assert_relationship_consistency(nodes: Dict[str, Dict[str, Any]], edges: Sequence[Dict[str, Any]]) -> None:
    _assert_relationship_consistency(nodes, edges)


def assert_acyclic_dependency_graph(nodes: Dict[str, Dict[str, Any]], edges: Sequence[Dict[str, Any]]) -> None:
    _assert_acyclic_dependency_graph(nodes, edges)


def recompute_relationships(payload: Dict[str, Any]) -> None:
    _recompute_relationships(payload)


def default_theorem_frontier_payload(mode: str) -> Dict[str, Any]:
    return {
        "mode": "full",
        "active_node_id": None,
        "current_action": None,
        "nodes": {},
        "edges": [],
        "metrics": {
            "active_node_age": 0,
            "blocker_cluster_age": 0,
            "closed_nodes_count": 0,
            "refuted_nodes_count": 0,
            "paper_nodes_closed": 0,
            "failed_close_attempts": 0,
            "low_cone_purity_streak": 0,
            "cone_purity": None,
            "structural_churn": 0,
        },
        "escalation": {
            "required": False,
            "reasons": [],
        },
        "paper_verifier_history": [],
        "nl_proof_verifier_history": [],
        "current": None,
    }


def validate_loaded_theorem_frontier_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise SupervisorError("Theorem-frontier payload must be a JSON object.")
    mode = normalize_frontier_text(payload.get("mode")).lower()
    if mode != "full":
        raise SupervisorError(f"Unknown theorem-frontier payload mode: {payload.get('mode')!r}")

    raw_nodes = payload.get("nodes", {})
    if not isinstance(raw_nodes, dict):
        raise SupervisorError("Full theorem-frontier payload nodes must be a mapping.")
    nodes: Dict[str, Dict[str, Any]] = {}
    for node_id, raw_node in raw_nodes.items():
        if not isinstance(raw_node, dict):
            raise SupervisorError(f"Theorem-frontier node {node_id!r} must be an object.")
        validated = validate_theorem_frontier_node(raw_node, require_relationships=True, require_status=True)
        if validated["node_id"] != str(node_id):
            raise SupervisorError(
                f"Theorem-frontier node key {node_id!r} does not match embedded node_id {validated['node_id']!r}."
            )
        nodes[validated["node_id"]] = validated

    raw_edges = payload.get("edges", [])
    if not isinstance(raw_edges, list):
        raise SupervisorError("Full theorem-frontier payload edges must be a list.")
    edges: List[Dict[str, Any]] = []
    seen_pairs: Set[Tuple[str, str]] = set()
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            raise SupervisorError("Theorem-frontier edges must be objects.")
        validated_edge = validate_theorem_frontier_edge(raw_edge)
        if validated_edge["parent"] not in nodes or validated_edge["child"] not in nodes:
            raise SupervisorError(
                "Theorem-frontier edge references a missing node: "
                f"{validated_edge['parent']!r} -> {validated_edge['child']!r}"
            )
        key = (validated_edge["parent"], validated_edge["child"])
        if key in seen_pairs:
            raise SupervisorError(f"Duplicate theorem-frontier edge {key!r}.")
        seen_pairs.add(key)
        edges.append(validated_edge)

    _assert_relationship_consistency(nodes, edges)
    _assert_acyclic_dependency_graph(nodes, edges)
    repair_theorem_frontier_closed_nodes(nodes, edges)
    assert_local_node_proofs(
        nodes,
        edges,
        context_label="Theorem-frontier payload",
    )

    active_node_id = normalize_frontier_text(payload.get("active_node_id"))
    if active_node_id:
        if active_node_id not in nodes:
            raise SupervisorError(f"Theorem-frontier active_node_id {active_node_id!r} is not present in nodes.")
        if nodes[active_node_id]["status"] not in {"open", "active"}:
            raise SupervisorError(
                f"Theorem-frontier active_node_id {active_node_id!r} must name an open/active node, "
                f"not {nodes[active_node_id]['status']!r}."
            )
    active_nodes = sorted(node_id for node_id, node in nodes.items() if node.get("status") == "active")
    if active_node_id:
        if active_nodes not in ([active_node_id], []):
            raise SupervisorError(
                "Full theorem-frontier payload must have exactly one active node matching active_node_id when any node is marked active; "
                f"found active nodes {active_nodes!r} with active_node_id={active_node_id!r}."
            )
    elif active_nodes:
        raise SupervisorError(
            "Full theorem-frontier payload has active nodes but no active_node_id: "
            f"{active_nodes!r}."
        )

    raw_metrics = payload.get("metrics", {})
    if not isinstance(raw_metrics, dict):
        raise SupervisorError("Full theorem-frontier metrics must be a mapping.")
    metrics = {
        "active_node_age": int(raw_metrics.get("active_node_age", 0) or 0),
        "blocker_cluster_age": int(raw_metrics.get("blocker_cluster_age", 0) or 0),
        "closed_nodes_count": int(raw_metrics.get("closed_nodes_count", 0) or 0),
        "refuted_nodes_count": int(raw_metrics.get("refuted_nodes_count", 0) or 0),
        "paper_nodes_closed": int(raw_metrics.get("paper_nodes_closed", 0) or 0),
        "failed_close_attempts": int(raw_metrics.get("failed_close_attempts", 0) or 0),
        "low_cone_purity_streak": int(raw_metrics.get("low_cone_purity_streak", 0) or 0),
        "cone_purity": None,
        "structural_churn": int(raw_metrics.get("structural_churn", 0) or 0),
    }
    cone_purity = raw_metrics.get("cone_purity")
    if cone_purity not in (None, ""):
        metrics["cone_purity"] = theorem_frontier_cone_purity(cone_purity)

    raw_escalation = payload.get("escalation", {"required": False, "reasons": []})
    if not isinstance(raw_escalation, dict):
        raise SupervisorError("Full theorem-frontier escalation payload must be a mapping.")
    escalation = {
        "required": bool(raw_escalation.get("required")),
        "reasons": normalize_frontier_text_list(
            raw_escalation.get("reasons"),
            label="theorem_frontier.escalation.reasons",
        ),
    }

    current_action = payload.get("current_action")
    normalized_current_action = None
    if current_action not in (None, ""):
        normalized_current_action = validate_theorem_frontier_action(current_action)

    history = payload.get("paper_verifier_history", [])
    if not isinstance(history, list):
        raise SupervisorError("Full theorem-frontier paper_verifier_history must be a list.")
    normalized_history: List[Dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            raise SupervisorError("paper_verifier_history entries must be objects.")
        item_cycle = item.get("cycle")
        try:
            expected_cycle = int(item_cycle)
        except (TypeError, ValueError):
            raise SupervisorError(f"paper_verifier_history entry cycle must be an integer, got {item_cycle!r}.")
        normalized_history.append(
            validate_theorem_frontier_paper_verifier_review("proof_formalization", expected_cycle, item)
        )
    nl_history = payload.get("nl_proof_verifier_history", [])
    if not isinstance(nl_history, list):
        raise SupervisorError("Full theorem-frontier nl_proof_verifier_history must be a list.")
    normalized_nl_history: List[Dict[str, Any]] = []
    for item in nl_history:
        if not isinstance(item, dict):
            raise SupervisorError("nl_proof_verifier_history entries must be objects.")
        item_cycle = item.get("cycle")
        try:
            expected_cycle = int(item_cycle)
        except (TypeError, ValueError):
            raise SupervisorError(f"nl_proof_verifier_history entry cycle must be an integer, got {item_cycle!r}.")
        normalized_nl_history.append(
            validate_theorem_frontier_nl_proof_verifier_review("proof_formalization", expected_cycle, item)
        )

    current = payload.get("current")
    if current is not None and not isinstance(current, dict):
        raise SupervisorError("Full theorem-frontier current payload must be an object or null.")

    result = {
        "mode": "full",
        "active_node_id": active_node_id or None,
        "current_action": normalized_current_action,
        "nodes": nodes,
        "edges": edges,
        "metrics": metrics,
        "escalation": escalation,
        "paper_verifier_history": normalized_history,
        "nl_proof_verifier_history": normalized_nl_history,
        "current": dict(current) if isinstance(current, dict) else None,
    }
    sync_theorem_frontier_metrics(result)
    return result


def theorem_frontier_branch_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict) or normalize_frontier_text(payload.get("mode")).lower() != "full":
        return {}
    active_node_id = normalize_frontier_text(payload.get("active_node_id"))
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    active_node = nodes.get(active_node_id) if active_node_id else None
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    escalation = payload.get("escalation") if isinstance(payload.get("escalation"), dict) else {}
    open_hypotheses = [
        str(item).strip()
        for item in current.get("open_hypotheses", [])
        if str(item).strip()
    ] if isinstance(current.get("open_hypotheses"), list) else []
    open_children = [
        child_id
        for child_id in theorem_frontier_node_children(nodes, edges, active_node_id)
        if theorem_frontier_effective_node_status(nodes, edges, child_id) != "closed"
    ] if active_node_id else []
    return {
        "active_node_id": active_node_id or None,
        "active_node_kind": active_node.get("kind") if isinstance(active_node, dict) else None,
        "active_node_anchor": active_node.get("lean_anchor") if isinstance(active_node, dict) else None,
        "active_node_nl_statement": active_node.get("natural_language_statement") if isinstance(active_node, dict) else None,
        "active_node_lean_statement": active_node.get("lean_statement") if isinstance(active_node, dict) else None,
        "blocker_cluster": (
            str(active_node.get("blocker_cluster", "")).strip()
            if isinstance(active_node, dict)
            else str(current.get("blocker_cluster", "")).strip() or None
        ),
        "current_action": normalize_frontier_text(payload.get("current_action")) or None,
        "assessed_action": normalize_frontier_text(current.get("assessed_action")) or None,
        "open_hypotheses": open_hypotheses,
        "open_hypotheses_count": len(open_hypotheses),
        "open_children": open_children,
        "open_children_count": len(open_children),
        "active_node_age": int(metrics.get("active_node_age", 0) or 0),
        "blocker_cluster_age": int(metrics.get("blocker_cluster_age", 0) or 0),
        "failed_close_attempts": int(metrics.get("failed_close_attempts", 0) or 0),
        "cone_purity": metrics.get("cone_purity") if metrics.get("cone_purity") not in ("", None) else None,
        "escalation_required": bool(escalation.get("required")),
        "escalation_reasons": [
            str(item).strip()
            for item in escalation.get("reasons", [])
            if str(item).strip()
        ] if isinstance(escalation.get("reasons"), list) else [],
    }


def branch_selection_question_for_state(state: Dict[str, Any]) -> str:
    summary = theorem_frontier_branch_summary(state)
    active_node_id = str(summary.get("active_node_id") or "").strip()
    if not active_node_id:
        return "Which branch seems more likely to eventually succeed at formalizing the whole paper?"
    anchor = str(summary.get("active_node_anchor") or "").strip()
    blocker = str(summary.get("blocker_cluster") or "").strip()
    detail_bits = []
    if anchor:
        detail_bits.append(f"at `{anchor}`")
    if blocker:
        detail_bits.append(f"(current blocker cluster: {blocker})")
    detail_suffix = f" {' '.join(detail_bits)}" if detail_bits else ""
    return (
        f"Which branch seems more likely to close theorem-frontier node `{active_node_id}`{detail_suffix} "
        "and then finish formalizing the whole paper?"
    )


def reset_child_branch_theorem_frontier_runtime_state(state: Dict[str, Any]) -> None:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    metrics["active_node_age"] = 0
    metrics["blocker_cluster_age"] = 0
    metrics["failed_close_attempts"] = 0
    metrics["low_cone_purity_streak"] = 0
    metrics["cone_purity"] = None
    metrics["structural_churn"] = 0
    payload["metrics"] = metrics
    payload["current_action"] = None
    payload["current"] = None
    payload["escalation"] = {"required": False, "reasons": []}
    state["last_theorem_frontier_worker_update"] = None
    state["last_theorem_frontier_review"] = None
    state["last_theorem_frontier_paper_review"] = None
    state["last_theorem_frontier_nl_proof_review"] = None


def write_theorem_frontier_state_file_if_present(state_dir: Path, state: Dict[str, Any]) -> None:
    payload = theorem_frontier_payload(state)
    if isinstance(payload, dict):
        JsonFile.dump(state_dir / "theorem_frontier.json", payload)


def theorem_frontier_context_text(config: Config, state: Dict[str, Any], provider: str) -> str:
    phase = current_phase(config, state)
    if not theorem_frontier_enabled(config, phase):
        return ""
    payload = theorem_frontier_payload(state) or default_theorem_frontier_payload("full")
    active_node_id = normalize_frontier_text(payload.get("active_node_id"))
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    active_node = nodes.get(active_node_id) if active_node_id else None
    worker_artifact = supervisor_prompt_label(config, provider, theorem_frontier_worker_update_path(config))
    paper_artifact = supervisor_prompt_label(config, provider, theorem_frontier_paper_verifier_path(config))
    nl_proof_artifact = supervisor_prompt_label(config, provider, theorem_frontier_nl_proof_verifier_path(config))
    review_artifact = supervisor_prompt_label(config, provider, theorem_frontier_review_path(config))
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    escalation = payload.get("escalation") if isinstance(payload.get("escalation"), dict) else {}
    lines = [
        "Theorem-frontier DAG discipline:",
        "- Proof formalization is controlled by an authoritative theorem-frontier DAG.",
        "- Each theorem node stores its current child decomposition and a fully rigorous natural-language proof from those children.",
        "- Working on a node may happen anywhere in the DAG; closure still requires its current children to be closed.",
        f"- The worker must write the theorem-frontier worker artifact to `{worker_artifact}`.",
        f"- Structural DAG edits are reviewed through `{paper_artifact}` and `{nl_proof_artifact}` before they enter the DAG.",
        f"- The reviewer must write the theorem-frontier review artifact to `{review_artifact}`.",
        "- Each burst must act on one active theorem node via `CLOSE`, `EXPAND`, or `REFUTE_REPLACE`.",
        "- `EXPAND` means insert new nodes between the active node and its current children only.",
        "- `REFUTE_REPLACE` means replace the active node's current decomposition with a different one.",
        "- When choosing the next active node, prioritize leverage: prefer nodes whose clarification is most likely to force upstream refactors/restatements if the route is wrong. This usually means lower nodes or locally tricky/doubtful nodes, not routine wrappers.",
        "- Work outside the active node's local cone does not count as theorem-frontier progress.",
    ]
    if isinstance(active_node, dict):
        children = theorem_frontier_node_children(nodes, edges, active_node_id)
        open_children = [
            child_id
            for child_id in children
            if theorem_frontier_effective_node_status(nodes, edges, child_id) != "closed"
        ]
        lines.extend(
            [
                "Current authoritative frontier state:",
                f"- Active theorem node: {active_node_id}",
                f"- Kind: {active_node.get('kind') or '(none)'}",
                f"- Anchor: {active_node.get('lean_anchor') or '(none)'}",
                f"- Blocker cluster: {active_node.get('blocker_cluster') or '(none)'}",
                f"- Immediate children: {', '.join(children) if children else '(none)'}",
                f"- Open children: {', '.join(open_children) if open_children else '(none)'}",
                f"- Active-node age: {int(metrics.get('active_node_age', 0) or 0)}",
                f"- Blocker-cluster age: {int(metrics.get('blocker_cluster_age', 0) or 0)}",
                f"- Failed close attempts on this blocker: {int(metrics.get('failed_close_attempts', 0) or 0)}",
                f"- Closed nodes: {int(metrics.get('closed_nodes_count', 0) or 0)}",
                f"- Latest cone purity: {metrics.get('cone_purity') or '(none)'}",
            ]
        )
        if escalation.get("required"):
            reasons = escalation.get("reasons") if isinstance(escalation.get("reasons"), list) else []
            lines.append(f"- Escalation required: {', '.join(str(reason) for reason in reasons) if reasons else 'yes'}")
    else:
        lines.append("- No active theorem node exists yet; the first burst must choose one exactly.")
    return "\n".join(lines)


def validate_paper_main_results_manifest(phase: str, manifest: Any) -> Dict[str, Any]:
    if not isinstance(manifest, dict):
        raise SupervisorError("Paper coarse-DAG manifest must be a JSON object.")
    required_keys = {"phase", "nodes", "edges", "initial_active_node_id"}
    missing = required_keys.difference(manifest)
    if missing:
        raise SupervisorError(f"Paper coarse-DAG manifest missing keys: {sorted(missing)}")
    validated = dict(manifest)
    if str(validated.get("phase")).strip().lower() != phase:
        raise SupervisorError(
            f"Paper coarse-DAG manifest phase mismatch: expected {phase}, got {validated.get('phase')}"
        )
    raw_nodes = validated.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise SupervisorError("Paper coarse-DAG manifest must contain a non-empty `nodes` list.")
    raw_edges = validated.get("edges")
    if not isinstance(raw_edges, list):
        raise SupervisorError("Paper coarse-DAG manifest field `edges` must be a list.")
    nodes: List[Dict[str, Any]] = []
    node_ids: Set[str] = set()
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise SupervisorError("Every entry in `nodes` must be a JSON object.")
        node = validate_theorem_frontier_node(raw_node, require_relationships=False, require_status=False)
        if node["kind"] not in {"paper", "paper_faithful_reformulation"}:
            raise SupervisorError(
                "Paper coarse-DAG manifest may only contain `paper` or `paper_faithful_reformulation` nodes, "
                f"got {node['kind']!r} for {node['node_id']!r}."
            )
        if node["node_id"] in node_ids:
            raise SupervisorError(f"Duplicate paper coarse-DAG node id: {node['node_id']!r}")
        node_ids.add(node["node_id"])
        nodes.append(node)
    edges: List[Dict[str, Any]] = []
    edge_pairs: Set[Tuple[str, str]] = set()
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            raise SupervisorError("Every entry in `edges` must be a JSON object.")
        edge = validate_theorem_frontier_edge(raw_edge)
        key = (edge["parent"], edge["child"])
        if key in edge_pairs:
            raise SupervisorError(f"Duplicate paper coarse-DAG edge: {key!r}.")
        if edge["parent"] not in node_ids or edge["child"] not in node_ids:
            raise SupervisorError(
                "Paper coarse-DAG edges must stay within the declared node set: "
                f"{edge['parent']!r} -> {edge['child']!r}."
            )
        edge_pairs.add(key)
        edges.append(edge)
    nodes_by_id = {node["node_id"]: dict(node, parent_ids=[], child_ids=[]) for node in nodes}
    expected_parents, expected_children = _relationship_sets(nodes_by_id, edges)
    for node in nodes:
        node["parent_ids"] = sorted(expected_parents[node["node_id"]])
        node["child_ids"] = sorted(expected_children[node["node_id"]])
    _assert_acyclic_dependency_graph(nodes_by_id, edges)
    assert_local_node_proofs(
        nodes_by_id,
        edges,
        context_label="Paper coarse-DAG manifest",
    )
    validated["nodes"] = nodes
    validated["edges"] = edges
    validated["initial_active_node_id"] = normalize_frontier_text(validated.get("initial_active_node_id"))
    if not validated["initial_active_node_id"]:
        raise SupervisorError("Paper coarse-DAG manifest field `initial_active_node_id` must be non-empty.")
    if validated["initial_active_node_id"] not in node_ids:
        raise SupervisorError(
            "Paper coarse-DAG manifest `initial_active_node_id` must name one of the declared nodes, "
            f"got {validated['initial_active_node_id']!r}."
        )
    return validated


def load_validated_paper_main_results_manifest(config: Config) -> Dict[str, Any]:
    path = paper_main_results_manifest_path(config)
    if not path.exists():
        raise SupervisorError(
            "Cannot enter proof_formalization without a paper coarse-DAG manifest at "
            f"{path}."
        )
    return validate_paper_main_results_manifest("theorem_stating", JsonFile.load(path, None))


def validate_theorem_frontier_worker_update_full(phase: str, cycle: int, update: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "cycle",
        "active_node_id",
        "requested_action",
        "cone_scope",
        "allowed_edit_paths",
        "result_summary",
        "proposed_nodes",
        "proposed_edges",
        "next_candidate_node_ids",
        "structural_change_reason",
    }
    missing = required_keys.difference(update)
    if missing:
        raise SupervisorError(f"Theorem-frontier worker update missing keys: {sorted(missing)}")
    validated = dict(update)
    validated = validate_phase_and_cycle_fields(
        "Theorem-frontier worker update",
        validated,
        phase=phase,
        cycle=cycle,
    )
    validated["active_node_id"] = normalize_frontier_text(validated.get("active_node_id"))
    if not validated["active_node_id"]:
        raise SupervisorError("Theorem-frontier worker update field active_node_id must be non-empty.")
    raw_active_after = validated.get("active_node_after")
    if raw_active_after in ("", None):
        validated["active_node_after"] = None
    elif not isinstance(raw_active_after, dict):
        raise SupervisorError("Theorem-frontier worker update field active_node_after must be an object when present.")
    else:
        validated["active_node_after"] = validate_theorem_frontier_node(
            dict(raw_active_after),
            require_relationships=False,
            require_status=False,
        )
        if validated["active_node_after"]["node_id"] != validated["active_node_id"]:
            raise SupervisorError(
                "Theorem-frontier worker update active_node_after.node_id must match active_node_id."
            )
    validated["requested_action"] = validate_theorem_frontier_action(validated.get("requested_action"))
    validated["cone_scope"] = normalize_frontier_text(validated.get("cone_scope"))
    validated["allowed_edit_paths"] = normalize_repo_relative_path_list(
        validated.get("allowed_edit_paths"),
        label="theorem frontier worker update allowed_edit_paths",
        required_suffix=".lean",
        allow_empty=True,
    )
    validated["result_summary"] = normalize_frontier_text(validated.get("result_summary"))
    validated["proposed_nodes"] = [
        validate_theorem_frontier_node(dict(node), require_relationships=False, require_status=False)
        for node in (validated.get("proposed_nodes") or [])
    ]
    if not isinstance(validated.get("proposed_edges"), list):
        raise SupervisorError("Theorem-frontier worker update field proposed_edges must be a list.")
    validated["proposed_edges"] = [
        validate_theorem_frontier_edge(dict(edge))
        for edge in validated.get("proposed_edges", [])
    ]
    validated["next_candidate_node_ids"] = normalize_frontier_text_list(
        validated.get("next_candidate_node_ids"),
        label="theorem_frontier.next_candidate_node_ids",
        allow_empty=True,
    )
    validated["structural_change_reason"] = normalize_frontier_text(validated.get("structural_change_reason"))
    if not validated["cone_scope"] or not validated["result_summary"]:
        raise SupervisorError("Theorem-frontier worker update fields cone_scope and result_summary must be non-empty.")
    proposed_ids = [node["node_id"] for node in validated["proposed_nodes"]]
    if len(proposed_ids) != len(set(proposed_ids)):
        raise SupervisorError("Theorem-frontier worker update proposed_nodes must have unique node_id values.")
    proposed_edge_pairs = [(edge["parent"], edge["child"]) for edge in validated["proposed_edges"]]
    if len(proposed_edge_pairs) != len(set(proposed_edge_pairs)):
        raise SupervisorError("Theorem-frontier worker update proposed_edges must have unique parent/child pairs.")
    if validated["requested_action"] == "CLOSE" and (validated["proposed_nodes"] or validated["proposed_edges"]):
        raise SupervisorError("Theorem-frontier CLOSE cycles may not propose structural node or edge changes.")
    if validated["requested_action"] == "CLOSE" and validated["active_node_after"] is not None:
        raise SupervisorError(
            "Theorem-frontier CLOSE cycles may not rewrite the active node. "
            "Change the node only through EXPAND or REFUTE_REPLACE."
        )
    if validated["requested_action"] in {"EXPAND", "REFUTE_REPLACE"} and not isinstance(validated["active_node_after"], dict):
        raise SupervisorError(
            "Theorem-frontier worker update must include active_node_after when requested_action is EXPAND or REFUTE_REPLACE."
        )
    return validated


def validate_theorem_frontier_review_full(phase: str, cycle: int, review: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "cycle",
        "active_node_id",
        "assessed_action",
        "blocker_cluster",
        "outcome",
        "next_active_node_id",
        "cone_purity",
        "open_hypotheses",
        "justification",
    }
    missing = required_keys.difference(review)
    if missing:
        raise SupervisorError(f"Theorem-frontier review missing keys: {sorted(missing)}")
    validated = dict(review)
    validated = validate_phase_and_cycle_fields(
        "Theorem-frontier review",
        validated,
        phase=phase,
        cycle=cycle,
    )
    validated["active_node_id"] = normalize_frontier_text(validated.get("active_node_id"))
    if not validated["active_node_id"]:
        raise SupervisorError("Theorem-frontier review field active_node_id must be non-empty.")
    validated["assessed_action"] = validate_theorem_frontier_action(validated.get("assessed_action"))
    validated["blocker_cluster"] = normalize_frontier_text(validated.get("blocker_cluster"))
    validated["outcome"] = validate_theorem_frontier_outcome(validated.get("outcome"))
    validated["next_active_node_id"] = normalize_frontier_text(validated.get("next_active_node_id"))
    validated["cone_purity"] = theorem_frontier_cone_purity(validated.get("cone_purity"))
    validated["open_hypotheses"] = normalize_frontier_text_list(
        validated.get("open_hypotheses"),
        label="theorem_frontier.open_hypotheses",
    )
    validated["justification"] = normalize_frontier_text(validated.get("justification"))
    if not validated["justification"]:
        raise SupervisorError("Theorem-frontier review field justification must be non-empty.")
    if not validated.get("blocker_cluster"):
        validated["blocker_cluster"] = ""
    return validated


def validate_theorem_frontier_approved_edge_refs(
    entries: Any,
    *,
    label: str,
) -> List[Dict[str, str]]:
    if not isinstance(entries, list):
        raise SupervisorError(f"{label} must be a list.")
    approved_edges: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SupervisorError(f"{label} entries must be objects.")
        edge = validate_theorem_frontier_edge(entry)
        key = (edge["parent"], edge["child"])
        if key in seen:
            raise SupervisorError(f"{label} contains duplicate edge {key!r}.")
        seen.add(key)
        approved_edges.append(edge)
    return approved_edges


def validate_theorem_frontier_paper_verifier_review(phase: str, cycle: int, review: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "cycle",
        "parent_node_id",
        "change_kind",
        "decision",
        "classification",
        "approved_node_ids",
        "approved_edges",
        "justification",
        "caveat",
    }
    missing = required_keys.difference(review)
    if missing:
        raise SupervisorError(f"Theorem-frontier paper-verifier review missing keys: {sorted(missing)}")
    validated = dict(review)
    validated = validate_phase_and_cycle_fields(
        "Theorem-frontier paper-verifier review",
        validated,
        phase=phase,
        cycle=cycle,
    )
    validated["parent_node_id"] = normalize_frontier_text(validated.get("parent_node_id"))
    validated["change_kind"] = normalize_frontier_enum(
        validated.get("change_kind"),
        ("EXPAND", "REFUTE_REPLACE"),
        label="paper verifier change kind",
    )
    validated["decision"] = theorem_frontier_paper_decision(validated.get("decision"))
    validated["classification"] = theorem_frontier_paper_classification(validated.get("classification"))
    validated["approved_node_ids"] = normalize_frontier_text_list(
        validated.get("approved_node_ids"),
        label="paper_verifier.approved_node_ids",
    )
    validated["approved_edges"] = validate_theorem_frontier_approved_edge_refs(
        validated.get("approved_edges"),
        label="paper_verifier.approved_edges",
    )
    validated["justification"] = normalize_frontier_text(validated.get("justification"))
    validated["caveat"] = normalize_frontier_text(validated.get("caveat"))
    if not validated["parent_node_id"] or not validated["justification"]:
        raise SupervisorError("paper_verifier.parent_node_id and paper_verifier.justification must be non-empty.")
    return validated


def validate_theorem_frontier_nl_proof_verifier_review(phase: str, cycle: int, review: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {
        "phase",
        "cycle",
        "parent_node_id",
        "change_kind",
        "decision",
        "approved_node_ids",
        "justification",
        "caveat",
    }
    missing = required_keys.difference(review)
    if missing:
        raise SupervisorError(f"Theorem-frontier NL-proof verifier review missing keys: {sorted(missing)}")
    validated = dict(review)
    validated = validate_phase_and_cycle_fields(
        "Theorem-frontier NL-proof verifier review",
        validated,
        phase=phase,
        cycle=cycle,
    )
    validated["parent_node_id"] = normalize_frontier_text(validated.get("parent_node_id"))
    validated["change_kind"] = normalize_frontier_enum(
        validated.get("change_kind"),
        ("EXPAND", "REFUTE_REPLACE"),
        label="NL-proof verifier change kind",
    )
    validated["decision"] = theorem_frontier_paper_decision(validated.get("decision"))
    validated["approved_node_ids"] = normalize_frontier_text_list(
        validated.get("approved_node_ids"),
        label="nl_proof_verifier.approved_node_ids",
    )
    validated["justification"] = normalize_frontier_text(validated.get("justification"))
    validated["caveat"] = normalize_frontier_text(validated.get("caveat"))
    if not validated["parent_node_id"] or not validated["justification"]:
        raise SupervisorError("nl_proof_verifier.parent_node_id and nl_proof_verifier.justification must be non-empty.")
    return validated


def theorem_frontier_requires_paper_verifier(worker_update: Dict[str, Any]) -> bool:
    return worker_update["requested_action"] in {"EXPAND", "REFUTE_REPLACE"}


def seed_theorem_frontier_from_main_results_manifest(
    config: Config,
    state: Dict[str, Any],
    manifest: Dict[str, Any],
    *,
    cycle: int,
) -> Dict[str, Any]:
    payload = default_theorem_frontier_payload("full")
    nodes: Dict[str, Dict[str, Any]] = {}
    for node in manifest["nodes"]:
        nodes[node["node_id"]] = theorem_frontier_node_record(node, status="open")
    payload["nodes"] = nodes
    payload["current_action"] = None
    payload["current"] = None
    payload["paper_verifier_history"] = []
    payload["nl_proof_verifier_history"] = []
    for edge in manifest["edges"]:
        add_theorem_frontier_edge(payload, edge)
    active_node_id = manifest["initial_active_node_id"]
    if active_node_id not in payload["nodes"]:
        raise SupervisorError(f"Initial active theorem node {active_node_id!r} is not present in the seeded DAG.")
    payload["nodes"][active_node_id]["status"] = "active"
    payload["active_node_id"] = active_node_id
    _recompute_relationships(payload)
    validated_payload = validate_loaded_theorem_frontier_payload(payload)
    state["theorem_frontier"] = validated_payload
    state["last_theorem_frontier_worker_update"] = None
    state["last_theorem_frontier_review"] = None
    state["last_theorem_frontier_paper_review"] = None
    state["last_theorem_frontier_nl_proof_review"] = None
    JsonFile.dump(theorem_frontier_state_path(config), validated_payload)
    append_jsonl(
        theorem_frontier_history_path(config),
        {
            "cycle": cycle,
            "mode": "full",
            "event": "seed",
            "active_node_id": validated_payload.get("active_node_id"),
            "seed_node_ids": [node["node_id"] for node in manifest["nodes"]],
            "seed_edge_count": len(manifest["edges"]),
            "updated_at": timestamp_now(),
        },
    )
    return validated_payload
