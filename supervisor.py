#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from typing import TextIO

from lagent_supervisor.shared import *
from lagent_supervisor.storage import JsonFile, append_jsonl, write_jsonl
from lagent_supervisor.web import update_chat_manifest

PUBLIC_WEB_JSON_MODE = 0o644
from lagent_supervisor.providers import *
from lagent_supervisor.frontier import *
from lagent_supervisor.validation import *







































































































































































































































def install_chat_viewer_assets(root_dir: Path) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = root_dir / "_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    version_sources = [
        CHAT_VIEWER_DIR / "index.html",
        CHAT_VIEWER_DIR / "app.js",
        CHAT_VIEWER_DIR / "markdown-viewer.html",
        CHAT_VIEWER_DIR / "markdown-viewer.js",
        CHAT_VIEWER_DIR / "styles.css",
    ]
    digest = hashlib.sha1()
    for source in version_sources:
        digest.update(source.name.encode("utf-8"))
        digest.update(source.read_bytes())
    viewer_version = digest.hexdigest()[:12]
    asset_targets = {
        CHAT_VIEWER_DIR / "index.html": root_dir / "index.html",
        CHAT_VIEWER_DIR / "app.js": assets_dir / "app.js",
        CHAT_VIEWER_DIR / "markdown-viewer.html": assets_dir / "markdown-viewer.html",
        CHAT_VIEWER_DIR / "markdown-viewer.js": assets_dir / "markdown-viewer.js",
        CHAT_VIEWER_DIR / "styles.css": assets_dir / "styles.css",
    }
    for source, target in asset_targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.name in {"index.html", "markdown-viewer.html"}:
            rendered = source.read_text(encoding="utf-8").replace(CHAT_VIEWER_VERSION_PLACEHOLDER, viewer_version)
            target.write_text(rendered, encoding="utf-8")
        else:
            shutil.copyfile(source, target)
    JsonFile.dump(
        assets_dir / "viewer-version.json",
        {
            "version": viewer_version,
            "generated_at": timestamp_now(),
        },
        mode=PUBLIC_WEB_JSON_MODE,
    )
    if not (root_dir / "repos.json").exists():
        JsonFile.dump(root_dir / "repos.json", {"repos": []}, mode=PUBLIC_WEB_JSON_MODE)


def chat_codex_budget_payload() -> Dict[str, Any]:
    status = latest_codex_weekly_budget_status()
    payload: Dict[str, Any] = {
        "available": status is not None,
        "checked_at": timestamp_now(),
    }
    if status is None:
        payload.update(
            {
                "timestamp": None,
                "source_path": None,
                "plan_type": None,
                "used_percent": None,
                "percent_left": None,
                "window_minutes": None,
                "resets_at": None,
            }
        )
        return payload
    payload.update(status)
    return payload


def refresh_chat_codex_budget_status(config: Config) -> Dict[str, Any]:
    payload = chat_codex_budget_payload()
    JsonFile.dump(chat_codex_budget_path(config), payload, mode=PUBLIC_WEB_JSON_MODE)
    return payload


def refresh_dag_codex_budget_status(config: Config) -> Dict[str, Any]:
    payload = chat_codex_budget_payload()
    JsonFile.dump(dag_codex_budget_path(config), payload, mode=PUBLIC_WEB_JSON_MODE)
    JsonFile.dump(dag_codex_budget_web_path(config), payload, mode=PUBLIC_WEB_JSON_MODE)
    return payload


def frontier_summary_for_meta(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return None
    mode = normalize_frontier_text(payload.get("mode")).lower()
    if mode != "full":
        return None
    nodes = payload.get("nodes") or {}
    if not isinstance(nodes, dict):
        return None
    edges = payload.get("edges") or []
    if not isinstance(edges, list):
        return None
    status_counts: Dict[str, int] = {}
    for node_id, node in nodes.items():
        if isinstance(node, dict):
            s = theorem_frontier_effective_node_status(nodes, edges, node_id)
            status_counts[s] = status_counts.get(s, 0) + 1
    metrics = payload.get("metrics") or {}
    escalation = payload.get("escalation") or {}
    active_node_id = normalize_frontier_text(payload.get("active_node_id"))
    active_node = nodes.get(active_node_id) if active_node_id else None
    return {
        "mode": "full",
        "has_frontier": True,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "status_counts": status_counts,
        "active_node_id": active_node_id or None,
        "active_node_anchor": active_node.get("lean_anchor") if isinstance(active_node, dict) else None,
        "escalation_required": bool(escalation.get("required")),
        "cone_purity": metrics.get("cone_purity"),
        "paper_nodes_closed": int(metrics.get("paper_nodes_closed", 0) or 0),
    }


def _compact_run_status_text(value: Any, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def run_status_for_meta(config: Config, state: Dict[str, Any]) -> Dict[str, Any]:
    phase = current_phase(config, state)
    current_cycle = int(state.get("cycle", 0) or 0)
    resume_cycle, stage = determine_resume_cycle_and_stage(state)
    last_completed_cycle = max(last_review_cycle(state), 0)
    last_review = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    final_decision = str(last_review.get("decision") or "").strip().upper()
    frontier_summary = frontier_summary_for_meta(state) or {}
    active_node_id = normalize_frontier_text(frontier_summary.get("active_node_id"))
    last_transition_error = state.get("last_transition_error") if isinstance(state.get("last_transition_error"), dict) else {}

    task = ""
    if last_transition_error and str(last_transition_error.get("phase") or "").strip() == phase:
        task = f"Fix blocked transition: {last_transition_error.get('error') or 'validation issue'}"
    elif phase == "proof_formalization" and active_node_id:
        if stage == "reviewer":
            task = f"Review node {active_node_id}"
        else:
            task = f"Work node {active_node_id}"
    elif phase == "theorem_stating":
        task = "Audit and align paper-facing Lean statements"
    elif phase == "planning":
        task = "Refine PLAN.md into a complete proof roadmap"
    elif phase == "paper_check":
        task = "Check the paper mathematically and update PAPERNOTES.md"
    elif phase == PHASE_PROOF_COMPLETE_STYLE_CLEANUP:
        task = "Optional style cleanup and polish"

    directive = worker_directive_summary(state)
    if directive:
        task = directive
    full_task = " ".join(str(task or "").split())
    task = _compact_run_status_text(full_task)

    status = "running"
    if last_transition_error and str(last_transition_error.get("phase") or "").strip() == phase:
        status = "blocked"
    elif final_decision == "DONE":
        status = "completed"
        stage = None
        resume_cycle = None
        full_task = "Completed"
        task = "Completed"

    return {
        "status": status,
        "current_stage": stage,
        "current_cycle": current_cycle,
        "resume_cycle": resume_cycle,
        "last_completed_cycle": last_completed_cycle,
        "current_task": task or None,
        "current_task_full": full_task or None,
        "active_node_id": active_node_id or None,
        "agent_token_usage": agent_token_usage_summary(state),
    }


TOKEN_USAGE_TOTAL_FIELDS: Tuple[str, ...] = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _exact_paid_credit_delta(start_usage: Dict[str, Any], end_usage: Dict[str, Any]) -> Tuple[Optional[float], str]:
    for decreasing_field in ("credits_available", "credits_remaining"):
        start_value = start_usage.get(decreasing_field)
        end_value = end_usage.get(decreasing_field)
        if start_value is None or end_value is None:
            continue
        delta = float(start_value) - float(end_value)
        if delta >= 0:
            return delta, decreasing_field
    for increasing_field in ("credits_spent", "credits_used"):
        start_value = start_usage.get(increasing_field)
        end_value = end_usage.get(increasing_field)
        if start_value is None or end_value is None:
            continue
        delta = float(end_value) - float(start_value)
        if delta >= 0:
            return delta, increasing_field
    return None, ""


def _provider_usage_snapshot(adapter: ProviderAdapter) -> Dict[str, Any]:
    if adapter.cfg.provider == "codex":
        usage = latest_codex_token_usage_for_scope(adapter.work_dir())
        return {
            "provider": adapter.cfg.provider,
            "role": adapter.role,
            "available": usage is not None,
            "usage": usage,
        }
    return {
        "provider": adapter.cfg.provider,
        "role": adapter.role,
        "available": False,
        "usage": None,
    }


def _provider_usage_delta(start: Dict[str, Any], end: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    start_usage = start.get("usage") if isinstance(start, dict) else None
    end_usage = end.get("usage") if isinstance(end, dict) else None
    if not isinstance(end_usage, dict):
        return None
    if not isinstance(start_usage, dict):
        return {
            "mode": "end_total",
            **{field: int(end_usage.get(field) or 0) for field in TOKEN_USAGE_TOTAL_FIELDS},
        }
    if str(start_usage.get("source_path") or "") != str(end_usage.get("source_path") or ""):
        return {
            "mode": "end_total_new_session",
            **{field: int(end_usage.get(field) or 0) for field in TOKEN_USAGE_TOTAL_FIELDS},
            "weekly_used_percent_start": start_usage.get("weekly_used_percent"),
            "weekly_used_percent_end": end_usage.get("weekly_used_percent"),
            "weekly_budget_exhausted_start": bool(start_usage.get("weekly_budget_exhausted")),
            "weekly_budget_exhausted_end": bool(end_usage.get("weekly_budget_exhausted")),
            "post_weekly_budget": bool(end_usage.get("weekly_budget_exhausted")),
            "post_weekly_budget_total_tokens_exact": False,
            "post_weekly_budget_total_tokens": 0,
            "paid_credits_used": None,
            "paid_credits_basis": "",
        }
    delta: Dict[str, Any] = {"mode": "delta"}
    for field in TOKEN_USAGE_TOTAL_FIELDS:
        delta[field] = max(0, int(end_usage.get(field) or 0) - int(start_usage.get(field) or 0))
    start_weekly_exhausted = bool(start_usage.get("weekly_budget_exhausted"))
    end_weekly_exhausted = bool(end_usage.get("weekly_budget_exhausted"))
    delta["weekly_used_percent_start"] = start_usage.get("weekly_used_percent")
    delta["weekly_used_percent_end"] = end_usage.get("weekly_used_percent")
    delta["weekly_budget_exhausted_start"] = start_weekly_exhausted
    delta["weekly_budget_exhausted_end"] = end_weekly_exhausted
    delta["post_weekly_budget"] = end_weekly_exhausted
    delta["post_weekly_budget_total_tokens_exact"] = start_weekly_exhausted and end_weekly_exhausted
    delta["post_weekly_budget_total_tokens"] = delta["total_tokens"] if delta["post_weekly_budget_total_tokens_exact"] else 0
    paid_credits_used, paid_credits_basis = _exact_paid_credit_delta(start_usage, end_usage)
    delta["paid_credits_used"] = paid_credits_used
    delta["paid_credits_basis"] = paid_credits_basis
    return delta


def agent_token_usage_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    payload = state.get("agent_token_usage")
    if not isinstance(payload, dict):
        return {"tracked": False, "by_role": {}, "recent_bursts": [], "latest_credit_status": None}
    by_role = payload.get("by_role")
    recent = payload.get("recent_bursts")
    latest_credit_status = payload.get("latest_credit_status")
    return {
        "tracked": bool(by_role),
        "by_role": by_role if isinstance(by_role, dict) else {},
        "recent_bursts": recent[-10:] if isinstance(recent, list) else [],
        "latest_credit_status": latest_credit_status if isinstance(latest_credit_status, dict) else None,
    }


def record_agent_burst_usage(
    config: Config,
    state: Dict[str, Any],
    *,
    cycle: int,
    phase: str,
    adapter: ProviderAdapter,
    stage_label: str,
    attempt: int,
    run: Dict[str, Any],
) -> None:
    usage = run.get("usage")
    if not isinstance(usage, dict):
        return
    end_payload = usage.get("end")
    if not (isinstance(end_payload, dict) and end_payload.get("available")):
        return
    end_usage = end_payload.get("usage")
    if not isinstance(end_usage, dict):
        return
    delta = usage.get("delta")
    if not isinstance(delta, dict):
        return
    totals = state.setdefault("agent_token_usage", {})
    by_role = totals.setdefault("by_role", {})
    recent = totals.setdefault("recent_bursts", [])
    role_key = f"{adapter.cfg.provider}:{adapter.role}"
    role_entry = by_role.setdefault(
        role_key,
        {
            "provider": adapter.cfg.provider,
            "role": adapter.role,
            "burst_count": 0,
            "post_weekly_budget_burst_count": 0,
            "post_weekly_budget_total_tokens_exact": 0,
            "paid_credits_known_burst_count": 0,
            "paid_credits_used_known_total": 0.0,
            **{field: 0 for field in TOKEN_USAGE_TOTAL_FIELDS},
        },
    )
    role_entry["burst_count"] = int(role_entry.get("burst_count") or 0) + 1
    for field in TOKEN_USAGE_TOTAL_FIELDS:
        role_entry[field] = int(role_entry.get(field) or 0) + int(delta.get(field) or 0)
    if bool(delta.get("post_weekly_budget")):
        role_entry["post_weekly_budget_burst_count"] = int(role_entry.get("post_weekly_budget_burst_count") or 0) + 1
    if bool(delta.get("post_weekly_budget_total_tokens_exact")):
        role_entry["post_weekly_budget_total_tokens_exact"] = int(role_entry.get("post_weekly_budget_total_tokens_exact") or 0) + int(delta.get("post_weekly_budget_total_tokens") or 0)
    if delta.get("paid_credits_used") is not None:
        role_entry["paid_credits_known_burst_count"] = int(role_entry.get("paid_credits_known_burst_count") or 0) + 1
        role_entry["paid_credits_used_known_total"] = float(role_entry.get("paid_credits_used_known_total") or 0.0) + float(delta.get("paid_credits_used") or 0.0)
    role_entry["last_timestamp"] = end_usage.get("timestamp")
    role_entry["last_cycle"] = int(cycle)
    role_entry["last_phase"] = phase
    role_entry["last_stage"] = stage_label
    role_entry["source_path"] = end_usage.get("source_path")
    role_entry["last_weekly_used_percent"] = end_usage.get("weekly_used_percent")
    role_entry["last_weekly_budget_exhausted"] = bool(end_usage.get("weekly_budget_exhausted"))
    role_entry["last_credits_available"] = end_usage.get("credits_available")
    role_entry["last_credits_remaining"] = end_usage.get("credits_remaining")
    role_entry["last_credits_used"] = end_usage.get("credits_used")
    role_entry["last_credits_spent"] = end_usage.get("credits_spent")
    role_entry["last_credits_limit"] = end_usage.get("credits_limit")
    latest_credit_status = {
        "timestamp": end_usage.get("timestamp"),
        "source_path": end_usage.get("source_path"),
        "weekly_used_percent": end_usage.get("weekly_used_percent"),
        "weekly_budget_exhausted": bool(end_usage.get("weekly_budget_exhausted")),
        "credits_available": end_usage.get("credits_available"),
        "credits_remaining": end_usage.get("credits_remaining"),
        "credits_used": end_usage.get("credits_used"),
        "credits_spent": end_usage.get("credits_spent"),
        "credits_limit": end_usage.get("credits_limit"),
    }
    totals["latest_credit_status"] = latest_credit_status
    burst_entry = {
        "timestamp": timestamp_now(),
        "cycle": int(cycle),
        "phase": phase,
        "provider": adapter.cfg.provider,
        "role": adapter.role,
        "stage_label": stage_label,
        "attempt": int(attempt),
        "delta": {field: int(delta.get(field) or 0) for field in TOKEN_USAGE_TOTAL_FIELDS},
        "mode": str(delta.get("mode") or ""),
        "source_path": end_usage.get("source_path"),
        "burst_tag": run.get("burst_tag"),
        "weekly_budget": {
            "used_percent_start": delta.get("weekly_used_percent_start"),
            "used_percent_end": delta.get("weekly_used_percent_end"),
            "post_weekly_budget": bool(delta.get("post_weekly_budget")),
            "post_weekly_budget_total_tokens_exact": bool(delta.get("post_weekly_budget_total_tokens_exact")),
            "post_weekly_budget_total_tokens": int(delta.get("post_weekly_budget_total_tokens") or 0),
        },
        "paid_credits": {
            "used": delta.get("paid_credits_used"),
            "basis": str(delta.get("paid_credits_basis") or ""),
            "available_end": end_usage.get("credits_available"),
            "remaining_end": end_usage.get("credits_remaining"),
            "used_end": end_usage.get("credits_used"),
            "spent_end": end_usage.get("credits_spent"),
            "limit_end": end_usage.get("credits_limit"),
        },
    }
    recent.append(burst_entry)
    if len(recent) > 200:
        del recent[:-200]
    save_state(config, state)


def export_dag_frontier_snapshot(config: Config, state: Dict[str, Any]) -> None:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return
    mode = normalize_frontier_text(payload.get("mode")).lower()
    if mode != "full":
        return
    export = dict(payload)
    export["exported_at"] = timestamp_now()
    JsonFile.dump(dag_frontier_path(config), export, mode=PUBLIC_WEB_JSON_MODE)
    JsonFile.dump(dag_frontier_web_path(config), export, mode=PUBLIC_WEB_JSON_MODE)


def theorem_frontier_reviewer_resolution_note(state: Dict[str, Any]) -> str:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return ""
    current = payload.get("current")
    if not isinstance(current, dict):
        return ""
    requested = normalize_frontier_text(current.get("requested_next_active_node_id"))
    resolved = normalize_frontier_text(current.get("next_active_node_id") or payload.get("active_node_id"))
    if not requested or not resolved or requested == resolved:
        return ""
    return (
        f"the reviewer requested `{requested}`, but the authoritative frontier for that cycle "
        f"resolved to `{resolved}` instead; continue from `{resolved}`."
    )


def theorem_frontier_authoritative_next_prompt(
    state: Dict[str, Any],
    *,
    fallback: str = "",
) -> str:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return fallback
    active_node_id = normalize_frontier_text(payload.get("active_node_id"))
    if not active_node_id:
        return fallback
    return f"Continue proof formalization at `{active_node_id}`."


def theorem_frontier_close_validation_note(state: Dict[str, Any], phase: str) -> str:
    payload = state.get("last_theorem_frontier_close_validation_error")
    if not isinstance(payload, dict):
        return ""
    if str(payload.get("phase") or "").strip() != phase:
        return ""
    node_id = normalize_frontier_text(payload.get("active_node_id"))
    error = normalize_frontier_text(payload.get("error"))
    if not error:
        return ""
    node_text = f" for `{node_id}`" if node_id else ""
    return (
        f"deterministic CLOSE validation failed{node_text}: {error} "
        "Edit only the active generated proof file until the generated close-check script succeeds, or use `REFACTOR` first "
        "if supporting proof code must change before the strict `CLOSE` can land."
    )


def _compact_frontier_node(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: node[k]
        for k in (
            "node_id", "kind", "status", "lean_proof_status", "display_label",
            "natural_language_statement",
            "natural_language_proof",
            "lean_statement", "lean_anchor", "paper_provenance",
            "blocker_cluster", "acceptance_evidence",
            "notes", "parent_ids", "child_ids",
        )
        if k in node
    }


def _compact_frontier_edge(edge: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: edge[k]
        for k in ("parent", "child")
        if k in edge
    }


def _compact_frontier_snapshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    nodes = payload.get("nodes") or {}
    return {
        "active_node_id": payload.get("active_node_id"),
        "current_action": payload.get("current_action"),
        "nodes": {
            nid: _compact_frontier_node(node)
            for nid, node in nodes.items()
            if isinstance(node, dict)
        },
        "edges": [
            _compact_frontier_edge(edge)
            for edge in (payload.get("edges") or [])
            if isinstance(edge, dict)
        ],
        "metrics": dict(payload.get("metrics") or {}),
        "escalation": dict(payload.get("escalation") or {}),
    }


def dag_cycle_history_entry_from_state(
    config: Config,
    state: Dict[str, Any],
    *,
    cycle: int,
    timestamp: str,
    entry_type: str,
    completed_phase: Optional[str] = None,
) -> Dict[str, Any]:
    phase = current_phase(config, state)
    last_review = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    run_status = run_status_for_meta(config, state)
    payload = theorem_frontier_payload(state)
    current_payload = payload.get("current") if isinstance(payload, dict) and isinstance(payload.get("current"), dict) else {}
    last_review_cycle = int(last_review.get("cycle", 0) or 0) if isinstance(last_review, dict) else 0
    carry_review = not (entry_type == "live_snapshot" and last_review_cycle < int(cycle))
    entry: Dict[str, Any] = {
        "cycle": int(cycle),
        "type": entry_type,
        "phase": phase,
        "completed_phase": completed_phase or phase,
        "decision": str(last_review.get("decision", "")).strip() if carry_review else "",
        "decision_reason": str(last_review.get("reason", "")).strip() if carry_review else "",
        "current_action": (
            str(current_payload.get("assessed_action") or (payload.get("current_action") if isinstance(payload, dict) else "") or "").strip()
            if carry_review
            else ""
        ),
        "outcome": str(current_payload.get("outcome", "") or "").strip() if carry_review else "",
        "worker_directive": str(run_status.get("current_task_full") or ""),
        "timestamp": timestamp,
        "run_status": run_status,
    }
    if isinstance(payload, dict):
        entry["frontier"] = _compact_frontier_snapshot(payload)
        entry["active_node_id"] = payload.get("active_node_id")
    else:
        entry["frontier"] = None
        entry["active_node_id"] = None
    return entry


def _history_clone_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(state))


def _load_cycle_history_replay_bundle(
    config: Config,
    *,
    phase: str,
    cycle: int,
) -> Optional[Dict[str, Any]]:
    decision_path = reviewer_decision_path(config, cycle)
    if not decision_path.exists():
        return None
    raw_decision = JsonFile.load(decision_path, None)
    if not isinstance(raw_decision, dict):
        return None
    bundle: Dict[str, Any] = {
        "decision": validate_reviewer_decision(phase, cycle, raw_decision),
    }
    if not theorem_frontier_enabled(config, phase):
        return bundle
    worker_path = theorem_frontier_worker_update_path(config, cycle)
    frontier_review_path = theorem_frontier_review_path(config, cycle)
    if not worker_path.exists() or not frontier_review_path.exists():
        return None
    raw_worker = JsonFile.load(worker_path, None)
    raw_frontier_review = JsonFile.load(frontier_review_path, None)
    if not isinstance(raw_worker, dict) or not isinstance(raw_frontier_review, dict):
        return None
    worker_update = validate_theorem_frontier_worker_update_full(phase, cycle, raw_worker)
    frontier_review = validate_theorem_frontier_review_full(phase, cycle, raw_frontier_review)
    bundle["worker_update"] = worker_update
    bundle["frontier_review"] = frontier_review
    if theorem_frontier_requires_paper_verifier(worker_update):
        paper_path = theorem_frontier_paper_verifier_path(config, cycle)
        if paper_path.exists():
            raw_paper = JsonFile.load(paper_path, None)
            if isinstance(raw_paper, dict):
                bundle["paper_review"] = validate_theorem_frontier_paper_verifier_review(phase, cycle, raw_paper)
        nl_path = theorem_frontier_nl_proof_verifier_path(config, cycle)
        if nl_path.exists():
            raw_nl = JsonFile.load(nl_path, None)
            if isinstance(raw_nl, dict):
                bundle["nl_proof_review"] = validate_theorem_frontier_nl_proof_verifier_review(phase, cycle, raw_nl)
    return bundle


def _checkpoint_state_matches_history_bundle(
    checkpoint_state: Dict[str, Any],
    bundle: Dict[str, Any],
    *,
    cycle: int,
) -> bool:
    last_review = checkpoint_state.get("last_review") if isinstance(checkpoint_state.get("last_review"), dict) else {}
    if int(last_review.get("cycle", 0) or 0) != cycle:
        return False
    decision = bundle.get("decision") if isinstance(bundle.get("decision"), dict) else {}
    if str(last_review.get("decision", "")).strip().upper() != str(decision.get("decision", "")).strip().upper():
        return False
    if not isinstance(bundle.get("frontier_review"), dict):
        return True
    payload = theorem_frontier_payload(checkpoint_state)
    if not isinstance(payload, dict):
        return False
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    frontier_review = bundle["frontier_review"]
    if int(current.get("cycle", 0) or 0) != cycle:
        return False
    if normalize_frontier_text(current.get("reviewed_node_id")) != normalize_frontier_text(frontier_review.get("active_node_id")):
        return False
    if str(current.get("current_action", "")).strip().upper() != str(frontier_review.get("assessed_action", "")).strip().upper():
        return False
    if str(current.get("current_outcome", "")).strip().upper() != str(frontier_review.get("outcome", "")).strip().upper():
        return False
    return True


def _rebuild_cycle_history_state_from_bundle(
    config: Config,
    previous_state: Dict[str, Any],
    bundle: Dict[str, Any],
    *,
    cycle: int,
) -> Optional[Dict[str, Any]]:
    state = _history_clone_state(previous_state)
    phase = current_phase(config, state)
    state["cycle"] = int(cycle)
    decision = dict(bundle.get("decision") or {})
    if not isinstance(decision, dict):
        return None
    if theorem_frontier_enabled(config, phase) and isinstance(bundle.get("worker_update"), dict) and isinstance(bundle.get("frontier_review"), dict):
        state["last_theorem_frontier_worker_update"] = dict(bundle["worker_update"])
        if isinstance(bundle.get("paper_review"), dict):
            state["last_theorem_frontier_paper_review"] = dict(bundle["paper_review"])
        if isinstance(bundle.get("nl_proof_review"), dict):
            state["last_theorem_frontier_nl_proof_review"] = dict(bundle["nl_proof_review"])
        state["last_theorem_frontier_review"] = dict(bundle["frontier_review"])
        frontier_payload_before = theorem_frontier_payload(state) or {}
        frontier_current = frontier_payload_before.get("current") if isinstance(frontier_payload_before, dict) else {}
        if int((frontier_current or {}).get("cycle", 0) or 0) != cycle:
            update_theorem_frontier_full_state(
                config,
                state,
                bundle["worker_update"],
                bundle["frontier_review"],
                bundle.get("paper_review") if isinstance(bundle.get("paper_review"), dict) else None,
                bundle.get("nl_proof_review") if isinstance(bundle.get("nl_proof_review"), dict) else None,
                cycle=cycle,
                persist=False,
            )
    decision["cycle"] = cycle
    decision["phase"] = phase
    state["last_review"] = decision
    return state


def _load_internal_theorem_frontier_history(config: Config) -> List[Dict[str, Any]]:
    path = theorem_frontier_history_path(config)
    if not path.exists():
        return []
    entries: List[Dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    entries.sort(key=lambda item: int(item.get("cycle", 0) or 0))
    return entries


def _reset_frontier_for_history_seed(payload: Dict[str, Any], active_node_id: str) -> Dict[str, Any]:
    seed_payload = deep_copy_jsonish(payload)
    nodes = seed_payload.get("nodes")
    if isinstance(nodes, dict):
        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            node["status"] = "open"
            node["lean_proof_status"] = "unproved"
    if active_node_id and isinstance(nodes, dict) and isinstance(nodes.get(active_node_id), dict):
        nodes[active_node_id]["status"] = "active"
    seed_payload["active_node_id"] = active_node_id or seed_payload.get("active_node_id")
    seed_payload["current_action"] = ""
    current = seed_payload.get("current")
    if isinstance(current, dict):
        current["cycle"] = int(seed_payload.get("current", {}).get("cycle", 0) or 0)
        current["assessed_action"] = ""
        current["outcome"] = ""
    return seed_payload


def _fallback_dag_cycle_history_entries_from_frontier_log(
    config: Config,
    state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return []
    internal_history = _load_internal_theorem_frontier_history(config)
    if not internal_history:
        return []

    entries: List[Dict[str, Any]] = []
    deduped_history: List[Dict[str, Any]] = []
    by_cycle: Dict[int, Dict[str, Any]] = {}
    for item in internal_history:
        try:
            cycle = int(item.get("cycle", 0) or 0)
        except (TypeError, ValueError):
            continue
        if cycle <= 0:
            continue
        by_cycle[cycle] = item
    for cycle in sorted(by_cycle):
        deduped_history.append(by_cycle[cycle])

    first_frontier_cycle = int(deduped_history[0].get("cycle", 0) or 0) if deduped_history else 0
    review_log = state.get("review_log") if isinstance(state.get("review_log"), list) else []
    for item in review_log:
        if not isinstance(item, dict):
            continue
        try:
            cycle = int(item.get("cycle", 0) or 0)
        except (TypeError, ValueError):
            continue
        if cycle <= 0 or (first_frontier_cycle > 0 and cycle >= first_frontier_cycle):
            continue
        completed_phase = str(item.get("phase") or "").strip()
        if completed_phase not in PHASES:
            continue
        phase = completed_phase
        if str(item.get("decision") or "").strip().upper() == "ADVANCE_PHASE":
            phase = next_phase(completed_phase) or completed_phase
        synthetic_state = {
            "phase": phase,
            "cycle": cycle,
            "last_review": dict(item),
        }
        entries.append(
            dag_cycle_history_entry_from_state(
                config,
                synthetic_state,
                cycle=cycle,
                timestamp=str(item.get("updated_at") or ""),
                entry_type="cycle_snapshot",
                completed_phase=completed_phase,
            )
        )

    working_payload: Optional[Dict[str, Any]] = None
    last_cycle = 0
    for item in deduped_history:
        cycle = int(item.get("cycle", 0) or 0)
        if cycle <= 0:
            continue
        last_cycle = max(last_cycle, cycle)
        event = str(item.get("event") or "").strip().lower()
        outcome = str(item.get("outcome") or "").strip().upper()
        active_node_id = normalize_frontier_text(item.get("active_node_id"))
        reviewed_node_id = normalize_frontier_text(item.get("reviewed_node_id"))
        next_active_node_id = normalize_frontier_text(item.get("next_active_node_id"))
        timestamp = str(item.get("updated_at") or timestamp_now())
        if event == "seed" or working_payload is None:
            working_payload = _reset_frontier_for_history_seed(payload, active_node_id)
            entries.append(
                dag_cycle_history_entry_from_state(
                    config,
                    {"phase": state.get("phase"), "cycle": cycle, "theorem_frontier": working_payload},
                    cycle=cycle,
                    timestamp=timestamp,
                    entry_type="cycle_snapshot",
                    completed_phase="theorem_stating",
                )
            )
            continue

        working_payload = deep_copy_jsonish(working_payload)
        nodes = working_payload.get("nodes")
        if isinstance(nodes, dict):
            for node_id, node in nodes.items():
                if not isinstance(node, dict):
                    continue
                if node.get("status") == "active":
                    node["status"] = "open"
            if outcome == "CLOSED" and reviewed_node_id and isinstance(nodes.get(reviewed_node_id), dict):
                nodes[reviewed_node_id]["status"] = "closed"
                nodes[reviewed_node_id]["lean_proof_status"] = "proved"
            if next_active_node_id and isinstance(nodes.get(next_active_node_id), dict):
                if str(nodes[next_active_node_id].get("status") or "").strip().lower() != "closed":
                    nodes[next_active_node_id]["status"] = "active"
        working_payload["active_node_id"] = next_active_node_id or active_node_id or working_payload.get("active_node_id")
        working_payload["current_action"] = str(item.get("assessed_action") or "")
        current = working_payload.get("current")
        if not isinstance(current, dict):
            current = {}
            working_payload["current"] = current
        current["cycle"] = cycle
        current["assessed_action"] = str(item.get("assessed_action") or "")
        current["outcome"] = outcome
        working_state = {
            "phase": state.get("phase"),
            "cycle": cycle,
            "theorem_frontier": working_payload,
            "last_review": {
                "cycle": cycle,
                "phase": state.get("phase"),
                "decision": "CONTINUE",
                "reason": str(item.get("justification") or ""),
                "next_prompt": "",
            },
        }
        entries.append(
            dag_cycle_history_entry_from_state(
                config,
                working_state,
                cycle=cycle,
                timestamp=timestamp,
                entry_type="cycle_snapshot",
                completed_phase=str(state.get("phase") or ""),
            )
        )

    current_cycle = int(state.get("cycle", 0) or 0)
    if current_cycle > 0 and current_cycle > last_cycle:
        entries.append(
            dag_cycle_history_entry_from_state(
                config,
                state,
                cycle=current_cycle,
                timestamp=timestamp_now(),
                entry_type="live_snapshot",
            )
        )
    return entries


def build_dag_cycle_history_entries(config: Config, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    checkpoints = sorted(list_cycle_checkpoints(config), key=lambda item: int(item.get("cycle", 0) or 0))
    last_checkpoint_cycle = 0
    current_cycle = int(state.get("cycle", 0) or 0)
    last_review = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    last_completed_cycle = int(last_review.get("cycle", 0) or 0)
    previous_history_state: Optional[Dict[str, Any]] = None
    for checkpoint in checkpoints:
        cycle = int(checkpoint.get("cycle", 0) or 0)
        if last_completed_cycle > 0 and cycle > last_completed_cycle:
            continue
        checkpoint_state_path = Path(str(checkpoint.get("checkpoint_dir", ""))) / "state" / "state.json"
        if cycle <= 0 or not checkpoint_state_path.exists():
            continue
        checkpoint_state = JsonFile.load(checkpoint_state_path, None)
        if not isinstance(checkpoint_state, dict):
            continue
        history_state = checkpoint_state
        if previous_history_state is not None:
            phase = current_phase(config, previous_history_state)
            bundle = _load_cycle_history_replay_bundle(config, phase=phase, cycle=cycle)
            if bundle is not None and not _checkpoint_state_matches_history_bundle(checkpoint_state, bundle, cycle=cycle):
                rebuilt = _rebuild_cycle_history_state_from_bundle(config, previous_history_state, bundle, cycle=cycle)
                if isinstance(rebuilt, dict):
                    history_state = rebuilt
        entries.append(
            dag_cycle_history_entry_from_state(
                config,
                history_state,
                cycle=cycle,
                timestamp=str(checkpoint.get("created_at") or ""),
                entry_type="cycle_snapshot",
                completed_phase=str(checkpoint.get("completed_phase") or ""),
            )
        )
        previous_history_state = _history_clone_state(history_state)
        last_checkpoint_cycle = max(last_checkpoint_cycle, cycle)

    if current_cycle > 0 and current_cycle > last_checkpoint_cycle:
        entries.append(
            dag_cycle_history_entry_from_state(
                config,
                state,
                cycle=current_cycle,
                timestamp=timestamp_now(),
                entry_type="live_snapshot",
            )
        )
    fallback_entries = _fallback_dag_cycle_history_entries_from_frontier_log(config, state)
    if len(fallback_entries) > len(entries):
        return fallback_entries
    return entries


def export_dag_cycle_history(config: Config, state: Dict[str, Any]) -> None:
    entries = build_dag_cycle_history_entries(config, state)
    write_jsonl(dag_frontier_history_path(config), entries, mode=PUBLIC_WEB_JSON_MODE)
    write_jsonl(dag_frontier_history_web_path(config), entries, mode=PUBLIC_WEB_JSON_MODE)


def export_dag_frontier_seed(
    config: Config,
    payload: Dict[str, Any],
    *,
    cycle: int,
) -> None:
    nodes = payload.get("nodes") or {}
    entry = {
        "cycle": cycle,
        "type": "seed",
        "active_node_id": payload.get("active_node_id"),
        "nodes": {
            nid: _compact_frontier_node(n)
            for nid, n in nodes.items()
            if isinstance(n, dict)
        },
        "edges": [
            _compact_frontier_edge(e)
            for e in (payload.get("edges") or [])
            if isinstance(e, dict)
        ],
        "metrics": dict(payload.get("metrics") or {}),
        "timestamp": timestamp_now(),
    }
    append_jsonl(dag_frontier_history_path(config), entry, mode=PUBLIC_WEB_JSON_MODE)
    append_jsonl(dag_frontier_history_web_path(config), entry, mode=PUBLIC_WEB_JSON_MODE)


def export_dag_frontier_cycle(
    config: Config,
    state: Dict[str, Any],
    before_node_ids: Set[str],
    before_edge_ids: Set[str],
    payload: Dict[str, Any],
    *,
    cycle: int,
    outcome: str,
    reviewed_node_id: str,
    worker_directive: str,
) -> None:
    nodes = payload.get("nodes") or {}
    edges = payload.get("edges") or []
    new_node_ids = set(nodes.keys()) - before_node_ids
    before_edge_pairs = {tuple(item.split("->", 1)) for item in before_edge_ids if "->" in item}
    new_edge_pairs = {
        (str(edge.get("parent", "")), str(edge.get("child", "")))
        for edge in edges
        if isinstance(edge, dict)
        and (str(edge.get("parent", "")), str(edge.get("child", ""))) not in before_edge_pairs
    }
    entry: Dict[str, Any] = {
        "cycle": cycle,
        "type": "review",
        "outcome": outcome,
        "active_node_id": payload.get("active_node_id"),
        "reviewed_node_id": reviewed_node_id,
        "worker_directive": worker_directive,
        "nodes_added": {
            nid: _compact_frontier_node(nodes[nid])
            for nid in new_node_ids
            if isinstance(nodes.get(nid), dict)
        },
        "node_statuses": {
            nid: str(n.get("status", ""))
            for nid, n in nodes.items()
            if isinstance(n, dict)
        },
        "node_lean_proof_statuses": {
            nid: str(n.get("lean_proof_status", ""))
            for nid, n in nodes.items()
            if isinstance(n, dict) and n.get("lean_proof_status") not in (None, "")
        },
        "edges": [_compact_frontier_edge(e) for e in edges if isinstance(e, dict)],
        "metrics": dict(payload.get("metrics") or {}),
        "escalation": dict(payload.get("escalation") or {}),
        "timestamp": timestamp_now(),
    }
    if new_edge_pairs:
        entry["edges_added"] = [
            _compact_frontier_edge(e)
            for e in edges
            if isinstance(e, dict) and (str(e.get("parent", "")), str(e.get("child", ""))) in new_edge_pairs
        ]
    append_jsonl(dag_frontier_history_path(config), entry, mode=PUBLIC_WEB_JSON_MODE)
    append_jsonl(dag_frontier_history_web_path(config), entry, mode=PUBLIC_WEB_JSON_MODE)


def worker_directive_summary(state: Dict[str, Any]) -> str:
    last_review = state.get("last_review")
    parts: List[str] = []
    resolution_note = theorem_frontier_reviewer_resolution_note(state)
    if resolution_note:
        parts.append(f"Supervisor note: {resolution_note}")
    if isinstance(last_review, dict):
        next_prompt = theorem_frontier_authoritative_next_prompt(
            state,
            fallback=str(last_review.get("next_prompt", "")).strip(),
        )
        if next_prompt:
            parts.append(next_prompt)
    recovery = state.get("stuck_recovery")
    if isinstance(recovery, dict):
        attempts = recovery.get("attempts")
        if isinstance(attempts, list) and attempts:
            latest = attempts[-1]
            if isinstance(latest, dict):
                creative = str(latest.get("creative_suggestion", "")).strip()
                if creative:
                    parts.append(f"Recovery: {creative}")
    return " | ".join(parts) if parts else ""

def install_dag_viewer_assets(root_dir: Path) -> None:
    root_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = root_dir / "_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    source_files = [
        DAG_VIEWER_DIR / "index.html",
        DAG_VIEWER_DIR / "dag-browser.js",
        DAG_VIEWER_DIR / "dag-browser.css",
        DAG_VIEWER_DIR / "dag-layout-worker.js",
    ]
    digest = hashlib.sha1()
    for source in source_files:
        if source.exists():
            digest.update(source.name.encode("utf-8"))
            digest.update(source.read_bytes())
    viewer_version = digest.hexdigest()[:12]
    asset_targets = {
        DAG_VIEWER_DIR / "index.html": root_dir / "index.html",
        DAG_VIEWER_DIR / "dag-browser.js": assets_dir / "dag-browser.js",
        DAG_VIEWER_DIR / "dag-browser.css": assets_dir / "dag-browser.css",
        DAG_VIEWER_DIR / "dag-layout-worker.js": assets_dir / "dag-layout-worker.js",
    }
    for source, target in asset_targets.items():
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == ".html":
            rendered = source.read_text(encoding="utf-8").replace(
                CHAT_VIEWER_VERSION_PLACEHOLDER, viewer_version,
            )
            target.write_text(rendered, encoding="utf-8")
        else:
            shutil.copyfile(source, target)
    JsonFile.dump(
        assets_dir / "viewer-version.json",
        {"version": viewer_version, "generated_at": timestamp_now()},
        mode=PUBLIC_WEB_JSON_MODE,
    )
    manifest_json = root_dir / "repos.json"
    manifest_txt = root_dir / "repos.txt"
    if not manifest_json.exists():
        JsonFile.dump(manifest_json, {"repos": []}, mode=PUBLIC_WEB_JSON_MODE)
    if not manifest_txt.exists():
        JsonFile.dump(manifest_txt, {"repos": []}, mode=PUBLIC_WEB_JSON_MODE)


def ensure_dag_site(config: Config) -> None:
    root = dag_root_dir(config)
    install_dag_viewer_assets(root)
    refresh_dag_codex_budget_status(config)
    dag_repo_dir(config).mkdir(parents=True, exist_ok=True)


def update_dag_manifest(config: Config, state: Dict[str, Any]) -> None:
    manifest_path = dag_manifest_path(config)
    manifest = JsonFile.load(manifest_path, {"repos": []})
    repos = manifest.get("repos") if isinstance(manifest.get("repos"), list) else []
    summary = frontier_summary_for_meta(state)
    phase = current_phase(config, state)
    cycle = int(state.get("cycle", 0) or 0)
    entry = {
        "repo_name": config.chat.repo_name,
        "project_name": config.chat.project_name,
        "updated_at": timestamp_now(),
        "current_phase": phase,
        "current_cycle": cycle,
        "run_status": run_status_for_meta(config, state),
        "frontier_summary": summary,
        "branch_overview": branch_overview(state),
    }
    found = False
    for i, item in enumerate(repos):
        if isinstance(item, dict) and item.get("repo_name") == config.chat.repo_name:
            repos[i] = entry
            found = True
            break
    if not found:
        repos.append(entry)
    repos.sort(key=lambda r: (r.get("updated_at", ""), r.get("repo_name", "")), reverse=True)
    manifest["repos"] = repos
    JsonFile.dump(manifest_path, manifest, mode=PUBLIC_WEB_JSON_MODE)
    JsonFile.dump(dag_manifest_web_path(config), manifest, mode=PUBLIC_WEB_JSON_MODE)


def export_dag_meta(config: Config, state: Dict[str, Any]) -> None:
    summary = frontier_summary_for_meta(state)
    refresh_dag_codex_budget_status(config)
    meta = {
        "repo_name": config.chat.repo_name,
        "project_name": config.chat.project_name,
        "updated_at": timestamp_now(),
        "current_phase": current_phase(config, state),
        "current_cycle": int(state.get("cycle", 0) or 0),
        "run_status": run_status_for_meta(config, state),
        "agent_token_usage": agent_token_usage_summary(state),
        "frontier_summary": summary,
        "branch_overview": branch_overview(state),
    }
    JsonFile.dump(dag_repo_meta_path(config), meta, mode=PUBLIC_WEB_JSON_MODE)
    JsonFile.dump(dag_repo_meta_web_path(config), meta, mode=PUBLIC_WEB_JSON_MODE)
    export_dag_cycle_history(config, state)
    update_dag_manifest(config, state)


def chat_event_chunk_bounds(cycle: int) -> Tuple[int, int]:
    cycle_num = max(int(cycle or 0), 1)
    start = ((cycle_num - 1) // CHAT_EVENT_CYCLE_CHUNK_SIZE) * CHAT_EVENT_CYCLE_CHUNK_SIZE + 1
    end = start + CHAT_EVENT_CYCLE_CHUNK_SIZE - 1
    return start, end


def chat_event_chunk_relative_path(start_cycle: int, end_cycle: int) -> Path:
    return Path("events") / f"chunk-{start_cycle:04d}-{end_cycle:04d}.jsonl"


def default_chat_events_manifest() -> Dict[str, Any]:
    return {
        "chunk_size_cycles": CHAT_EVENT_CYCLE_CHUNK_SIZE,
        "chunks": [],
    }


def load_chat_events_manifest(config: Config) -> Dict[str, Any]:
    manifest = JsonFile.load(chat_repo_events_manifest_path(config), None)
    default = default_chat_events_manifest()
    if not isinstance(manifest, dict):
        return default
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        chunks = []
    normalized: List[Dict[str, Any]] = []
    for entry in chunks:
        if not isinstance(entry, dict):
            continue
        try:
            start_cycle = int(entry.get("start_cycle", 0) or 0)
            end_cycle = int(entry.get("end_cycle", 0) or 0)
            event_count = int(entry.get("event_count", 0) or 0)
        except (TypeError, ValueError):
            continue
        file_value = str(entry.get("file", "")).strip()
        if not file_value or start_cycle <= 0 or end_cycle < start_cycle:
            continue
        normalized.append(
            {
                "file": file_value,
                "start_cycle": start_cycle,
                "end_cycle": end_cycle,
                "event_count": event_count,
                "updated_at": str(entry.get("updated_at", "")).strip() or None,
            }
        )
    normalized.sort(key=lambda item: (item["start_cycle"], item["end_cycle"]), reverse=True)
    default["chunks"] = normalized
    return default


def write_chat_events_manifest(config: Config, manifest: Dict[str, Any]) -> Dict[str, Any]:
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list):
        chunks = []
    chunks.sort(key=lambda item: (int(item.get("start_cycle", 0) or 0), int(item.get("end_cycle", 0) or 0)), reverse=True)
    payload = {
        "chunk_size_cycles": CHAT_EVENT_CYCLE_CHUNK_SIZE,
        "chunks": chunks,
    }
    JsonFile.dump(chat_repo_events_manifest_path(config), payload)
    return payload


def append_chat_event_chunk(config: Config, event: Dict[str, Any]) -> None:
    start_cycle, end_cycle = chat_event_chunk_bounds(int(event.get("cycle", 0) or 0))
    chunk_rel = chat_event_chunk_relative_path(start_cycle, end_cycle)
    chunk_path = chat_repo_dir(config) / chunk_rel
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(chunk_path, event)

    manifest = load_chat_events_manifest(config)
    chunks = manifest["chunks"]
    chunk_file = chunk_rel.as_posix()
    existing = next((entry for entry in chunks if entry.get("file") == chunk_file), None)
    if existing is None:
        existing = {
            "file": chunk_file,
            "start_cycle": start_cycle,
            "end_cycle": end_cycle,
            "event_count": 0,
            "updated_at": None,
        }
        chunks.append(existing)
    existing["event_count"] = int(existing.get("event_count", 0) or 0) + 1
    existing["updated_at"] = str(event.get("timestamp") or timestamp_now())
    write_chat_events_manifest(config, manifest)


def rebuild_chat_event_chunks_from_legacy_log(config: Config) -> Dict[str, Any]:
    legacy_path = chat_repo_events_path(config)
    manifest = default_chat_events_manifest()
    chunks_dir = chat_repo_events_chunks_dir(config)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    expected: Dict[str, List[Dict[str, Any]]] = {}
    updated_at_by_file: Dict[str, str] = {}
    if legacy_path.exists():
        for line in legacy_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            start_cycle, end_cycle = chat_event_chunk_bounds(int(event.get("cycle", 0) or 0))
            chunk_file = chat_event_chunk_relative_path(start_cycle, end_cycle).as_posix()
            expected.setdefault(chunk_file, []).append(event)
            updated_at_by_file[chunk_file] = str(event.get("timestamp") or updated_at_by_file.get(chunk_file) or timestamp_now())
    for chunk_file, events in expected.items():
        chunk_path = chat_repo_dir(config) / chunk_file
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        chunk_path.write_text(payload, encoding="utf-8")
        start_cycle, end_cycle = chat_event_chunk_bounds(int(events[0].get("cycle", 0) or 0))
        manifest["chunks"].append(
            {
                "file": chunk_file,
                "start_cycle": start_cycle,
                "end_cycle": end_cycle,
                "event_count": len(events),
                "updated_at": updated_at_by_file.get(chunk_file),
            }
        )
    expected_paths = {Path(item["file"]) for item in manifest["chunks"]}
    for path in chunks_dir.rglob("*.jsonl"):
        rel = path.relative_to(chat_repo_dir(config))
        if rel not in expected_paths:
            path.unlink()
    remove_empty_directories(chunks_dir)
    return write_chat_events_manifest(config, manifest)


def ensure_chat_event_chunks(config: Config) -> Dict[str, Any]:
    manifest_path = chat_repo_events_manifest_path(config)
    if manifest_path.exists():
        return load_chat_events_manifest(config)
    return rebuild_chat_event_chunks_from_legacy_log(config)


def default_chat_meta(config: Config) -> Dict[str, Any]:
    return {
        "repo_name": config.chat.repo_name,
        "project_name": config.chat.project_name,
        "is_branch": config.chat.repo_name != config.chat.project_name,
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
        "markdown_files": [],
        "branch_overview": None,
    }


def load_chat_meta(config: Config) -> Dict[str, Any]:
    meta = JsonFile.load(chat_repo_meta_path(config), None)
    defaults = default_chat_meta(config)
    if not isinstance(meta, dict):
        return defaults
    merged = dict(defaults)
    merged.update(meta)
    for key in ("repo_name", "project_name", "is_branch", "repo_display_name", "repo_path", "goal_file", "chat_url", "direct_url"):
        merged[key] = defaults[key]
    return merged


def branch_lineage_entries(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    lineage = state.get("branch_lineage")
    if not isinstance(lineage, list):
        return []
    results: List[Dict[str, Any]] = []
    for entry in lineage:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("branch_name", "")).strip()
        episode_id = str(entry.get("episode_id", "")).strip()
        if not name or not episode_id:
            continue
        results.append(
            {
                "episode_id": episode_id,
                "branch_name": name,
                "summary": str(entry.get("summary", "")).strip(),
                "rewrite_scope": str(entry.get("rewrite_scope", "")).strip(),
            }
        )
    return results


def branch_overview(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    lineage = branch_lineage_entries(state)
    history = state.get("branch_history")
    if not isinstance(history, list):
        history = []
    active = active_branch_episode(state)
    if not lineage and not history and active is None:
        return None

    current_lineage_map = {entry["episode_id"]: entry["branch_name"] for entry in lineage}
    episodes_raw: List[Dict[str, Any]] = [entry for entry in history if isinstance(entry, dict)]
    if active is not None:
        episodes_raw.append(active)

    episodes: List[Dict[str, Any]] = []
    for raw_episode in episodes_raw:
        episode_id = str(raw_episode.get("id", "")).strip()
        if not episode_id:
            continue
        episode_lineage = raw_episode.get("lineage")
        if not isinstance(episode_lineage, list):
            episode_lineage = []
        ancestor_branch_names = [
            str(entry.get("branch_name", "")).strip()
            for entry in episode_lineage
            if isinstance(entry, dict) and str(entry.get("branch_name", "")).strip()
        ]
        selected_branch = str(raw_episode.get("selected_branch", "")).strip()
        status = str(raw_episode.get("status", "")).strip() or "active"
        branches_payload: List[Dict[str, Any]] = []
        for raw_branch in raw_episode.get("branches", []):
            if not isinstance(raw_branch, dict):
                continue
            branch_name = str(raw_branch.get("name", "")).strip()
            if not branch_name:
                continue
            if status == "active":
                branch_status = str(raw_branch.get("status", "")).strip() or "active"
            elif selected_branch and branch_name == selected_branch:
                branch_status = "selected"
            else:
                branch_status = "dead"
            branches_payload.append(
                {
                    "name": branch_name,
                    "repo_name": str(raw_branch.get("chat_repo_name", "")).strip() or None,
                    "summary": str(raw_branch.get("summary", "")).strip(),
                    "rewrite_scope": str(raw_branch.get("rewrite_scope", "")).strip(),
                    "status": branch_status,
                    "is_current_path": current_lineage_map.get(episode_id) == branch_name,
                    "path_newest_to_oldest": [branch_name, *reversed(ancestor_branch_names), "mainline"],
                }
            )
        episodes.append(
            {
                "id": episode_id,
                "phase": raw_episode.get("phase"),
                "trigger_cycle": int(raw_episode.get("trigger_cycle", 0) or 0),
                "status": status,
                "selected_branch": selected_branch or None,
                "selection_question": str(raw_episode.get("selection_question", "")).strip(),
                "lineage_newest_to_oldest": [*reversed(ancestor_branch_names), "mainline"],
                "branches": branches_payload,
            }
        )

    episodes.sort(key=lambda entry: (int(entry.get("trigger_cycle", 0) or 0), str(entry.get("id", ""))), reverse=True)

    current_path_newest_to_oldest = [entry["branch_name"] for entry in reversed(lineage)] + ["mainline"]
    current_path_status = "alive"
    for episode in episodes:
        episode_id = str(episode.get("id", ""))
        current_branch = current_lineage_map.get(episode_id)
        if not current_branch:
            continue
        if episode.get("status") == "selected" and current_branch != episode.get("selected_branch"):
            current_path_status = "dead"
            break

    return {
        "has_branching": bool(episodes),
        "current_path_newest_to_oldest": current_path_newest_to_oldest,
        "current_path_status": current_path_status,
        "episodes": episodes,
    }


def sync_chat_state_metadata(config: Config, state: Dict[str, Any]) -> None:
    meta_path = chat_repo_meta_path(config)
    if not meta_path.exists():
        return
    meta = load_chat_meta(config)
    overview = branch_overview(state)
    if meta.get("branch_overview") != overview:
        meta["branch_overview"] = overview
        JsonFile.dump(meta_path, meta)
        update_chat_manifest(config, meta)

def workflow_markdown_files(config: Config) -> List[Path]:
    candidates = [
        config.goal_file,
        config.repo_path / "TASKS.md",
        config.repo_path / "PAPERNOTES.md",
        config.repo_path / "PLAN.md",
        config.workflow.human_input_path,
        config.workflow.input_request_path,
    ]
    results: List[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            resolved = path.expanduser().resolve()
        if resolved in seen or not path.exists() or path.suffix.lower() != ".md":
            continue
        seen.add(resolved)
        results.append(path)
    return results


def chat_export_relative_path(config: Config, source_path: Path) -> Tuple[str, Path]:
    try:
        rel = source_path.resolve().relative_to(config.repo_path)
        return relative_repo_label(config, source_path), Path("files") / "repo" / rel
    except ValueError:
        digest = hashlib.sha1(str(source_path.resolve()).encode("utf-8")).hexdigest()[:8]
        safe_name = f"{sanitize_repo_name(source_path.stem)}-{digest}{source_path.suffix}"
        return str(source_path), Path("files") / "external" / safe_name


def remove_empty_directories(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue


def sync_chat_markdown_files(config: Config) -> List[Dict[str, Any]]:
    files_dir = chat_repo_files_dir(config)
    files_dir.mkdir(parents=True, exist_ok=True)
    exported: List[Dict[str, Any]] = []
    expected_exports: set[Path] = set()
    for path in workflow_markdown_files(config):
        source_label, export_rel = chat_export_relative_path(config, path)
        target = chat_repo_dir(config) / export_rel
        expected_exports.add(export_rel)
        source_stat = path.stat()
        should_copy = True
        if target.exists():
            target_stat = target.stat()
            should_copy = (
                target_stat.st_size != source_stat.st_size or target_stat.st_mtime_ns != source_stat.st_mtime_ns
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if should_copy:
            shutil.copy2(path, target)
        exported.append(
            {
                "label": path.name,
                "path": source_label,
                "href": f"{config.chat.repo_name}/{export_rel.as_posix()}",
                "updated_at": datetime.fromtimestamp(source_stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )
    for exported_path in files_dir.rglob("*"):
        if not exported_path.is_file():
            continue
        rel = exported_path.relative_to(chat_repo_dir(config))
        if rel not in expected_exports:
            exported_path.unlink()
    remove_empty_directories(files_dir)
    return exported


def refresh_chat_markdown_metadata(config: Config, *, update_manifest: bool) -> List[Dict[str, Any]]:
    repo_dir = chat_repo_dir(config)
    meta_path = chat_repo_meta_path(config)
    if not repo_dir.exists() or not meta_path.exists():
        return []
    meta = load_chat_meta(config)
    markdown_files = sync_chat_markdown_files(config)
    if meta.get("markdown_files") != markdown_files:
        meta["markdown_files"] = markdown_files
        JsonFile.dump(meta_path, meta)
        if update_manifest:
            update_chat_manifest(config, meta)
    return markdown_files


def ensure_chat_site(config: Config) -> None:
    install_chat_viewer_assets(chat_root_dir(config))
    refresh_chat_codex_budget_status(config)
    ensure_chat_event_chunks(config)
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
    meta = load_chat_meta(config)
    JsonFile.dump(meta_path, meta)
    meta["markdown_files"] = refresh_chat_markdown_metadata(config, update_manifest=False)
    if not meta["markdown_files"]:
        meta["markdown_files"] = sync_chat_markdown_files(config)
        JsonFile.dump(meta_path, meta)
    meta["branch_overview"] = branch_overview(load_state(config))
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
    if kind == "stuck_recovery_suggestion" and isinstance(content, dict):
        attempt = content.get("attempt", "?")
        suggestion = str(content.get("creative_suggestion", "")).strip()
        return f"Recovery attempt {attempt}: {suggestion}".strip(": ")
    if kind == "branch_strategy_decision" and isinstance(content, dict):
        decision = str(content.get("branch_decision", "")).strip()
        reason = str(content.get("reason", "")).strip()
        return f"{decision}: {reason}".strip(": ")
    if kind == "branch_selection_decision" and isinstance(content, dict):
        decision = str(content.get("selection_decision", "")).strip()
        reason = str(content.get("reason", "")).strip()
        return f"{decision}: {reason}".strip(": ")
    if kind == "branch_replacement_decision" and isinstance(content, dict):
        decision = str(content.get("replacement_decision", "")).strip()
        reason = str(content.get("reason", "")).strip()
        return f"{decision}: {reason}".strip(": ")
    return kind.replace("_", " ")












GEMINI_RATE_LIMIT_OR_CAPACITY_PATTERNS: Tuple[str, ...] = (
    "model_capacity_exhausted",
    "rateLimitExceeded",
    "rate limit exceeded",
    "resource_exhausted",
    "too many requests",
    "status 429",
    '"code": 429',
    "no capacity available for model",
)

BUDGET_ERROR_RETRY_DELAY_SECONDS = 15 * 60
PRODUCTIVE_LOCAL_FAILURE_MAX_RETRY_DELAY_SECONDS = 5 * 60

BUDGET_ERROR_PATTERNS: Tuple[str, ...] = (
    "model_capacity_exhausted",
    "ratelimitexceeded",
    "rate limit exceeded",
    "too many requests",
    "resource_exhausted",
    "retryablequotaerror",
    "quota exceeded",
    "usage limit",
    "credit balance is too low",
    "overloaded_error",
    "status 429",
    '"code": 429',
    "no capacity available for model",
    "you've hit your limit",
    "you have hit your limit",
    "hit your limit",
)

PRODUCTIVE_LOCAL_FAILURE_PATTERNS: Tuple[str, ...] = (
    "type mismatch",
    "unsolved goals",
    "application type mismatch",
    "declaration has type",
    "tactic `",
    "tactic '",
    "building twobites.",
    "error: twobites/",
    "error: repo/",
    "lake build",
)














def load_state(config: Config) -> Dict[str, Any]:
    state = JsonFile.load(config.state_dir / "state.json", {})
    state.setdefault("cycle", 0)
    state.setdefault("roles", {})
    state.setdefault("review_log", [])
    state.setdefault("phase_history", [])
    state.setdefault("awaiting_human_input", False)
    state.setdefault("stuck_recovery_attempts", [])
    state.setdefault("stuck_recovery_last_trigger_cycle", None)
    state.setdefault("branch_episode_counter", 0)
    state.setdefault("active_branch_episode", None)
    state.setdefault("branch_history", [])
    state.setdefault("branch_context", None)
    state.setdefault("branch_lineage", [])
    state.setdefault("branch_parent_max_current_branches", None)
    state.setdefault("pending_branch_proposal", None)
    state.setdefault("next_branch_proposal_review_count", 0)
    state.setdefault("last_branch_consideration_cycle", 0)
    state.setdefault("codex_budget_pause", None)
    state.setdefault("policy", None)
    state.setdefault("cleanup_last_good_commit", None)
    state.setdefault("last_transition_error", None)
    state.setdefault("theorem_frontier", None)
    state.setdefault("last_theorem_frontier_worker_update", None)
    state.setdefault("last_theorem_frontier_review", None)
    state.setdefault("last_theorem_frontier_paper_review", None)
    state.setdefault("last_theorem_frontier_nl_proof_review", None)
    if state["theorem_frontier"] is None and theorem_frontier_state_path(config).exists():
        raw_frontier = JsonFile.load(theorem_frontier_state_path(config), None)
        state["theorem_frontier"] = validate_loaded_theorem_frontier_payload(raw_frontier)
        if state["theorem_frontier"] != raw_frontier:
            JsonFile.dump(
                theorem_frontier_state_path(config),
                state["theorem_frontier"],
                mode=SUPERVISOR_SHARED_STATE_FILE_MODE,
            )
    elif state["theorem_frontier"] is not None:
        state["theorem_frontier"] = validate_loaded_theorem_frontier_payload(state["theorem_frontier"])
    state["last_review_cycle"] = last_review_cycle(state)
    state["last_validation_cycle"] = last_validation_cycle(state)
    state["theorem_frontier_active_node_id"] = theorem_frontier_active_node_id(state)
    current_phase(config, state)
    return state


def save_state(config: Config, state: Dict[str, Any]) -> None:
    state["last_review_cycle"] = last_review_cycle(state)
    state["last_validation_cycle"] = last_validation_cycle(state)
    state["theorem_frontier_active_node_id"] = theorem_frontier_active_node_id(state)
    JsonFile.dump(config.state_dir / "state.json", state, mode=SUPERVISOR_SHARED_STATE_FILE_MODE)
    normalize_worker_readable_state_permissions(config)
    sync_chat_state_metadata(config, state)


class TeeTextIO:
    def __init__(self, primary: TextIO, mirror: TextIO) -> None:
        self._primary = primary
        self._mirror = mirror

    def write(self, data: str) -> int:
        self._primary.write(data)
        self._mirror.write(data)
        return len(data)

    def flush(self) -> None:
        self._primary.flush()
        self._mirror.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._primary, "isatty", lambda: False)())

    def fileno(self) -> int:
        return int(getattr(self._primary, "fileno")())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


_SUPERVISOR_LOG_HANDLE: Optional[TextIO] = None
_SUPERVISOR_LOG_PATH: Optional[Path] = None
_ORIGINAL_STDOUT: TextIO = sys.stdout
_ORIGINAL_STDERR: TextIO = sys.stderr


def install_supervisor_file_logging(config: Config) -> None:
    global _SUPERVISOR_LOG_HANDLE, _SUPERVISOR_LOG_PATH
    log_path = config.state_dir / "logs" / "supervisor.main.log"
    if _SUPERVISOR_LOG_HANDLE is not None and _SUPERVISOR_LOG_PATH == log_path:
        return
    if _SUPERVISOR_LOG_HANDLE is not None:
        try:
            _SUPERVISOR_LOG_HANDLE.flush()
            _SUPERVISOR_LOG_HANDLE.close()
        except Exception:
            pass
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _SUPERVISOR_LOG_HANDLE = log_path.open("a", encoding="utf-8", buffering=1)
    _SUPERVISOR_LOG_PATH = log_path
    sys.stdout = TeeTextIO(_ORIGINAL_STDOUT, _SUPERVISOR_LOG_HANDLE)
    sys.stderr = TeeTextIO(_ORIGINAL_STDERR, _SUPERVISOR_LOG_HANDLE)




















































































def update_theorem_frontier_full_state(
    config: Config,
    state: Dict[str, Any],
    worker_update: Dict[str, Any],
    review: Dict[str, Any],
    paper_review: Optional[Dict[str, Any]],
    nl_proof_review: Optional[Dict[str, Any]] = None,
    *,
    cycle: int,
    persist: bool = True,
) -> Dict[str, Any]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict) or payload.get("mode") != "full":
        payload = default_theorem_frontier_payload("full")
    nodes = payload.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        raise SupervisorError("Theorem-frontier payload nodes must be a mapping.")
    metrics = payload.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        raise SupervisorError("Theorem-frontier payload metrics must be a mapping.")
    escalation = payload.setdefault("escalation", {"required": False, "reasons": []})
    if not isinstance(escalation, dict):
        raise SupervisorError("Theorem-frontier payload escalation must be a mapping.")
    payload.setdefault("paper_verifier_history", [])
    payload.setdefault("nl_proof_verifier_history", [])

    edges = payload.setdefault("edges", [])
    if not isinstance(edges, list):
        raise SupervisorError("Theorem-frontier payload edges must be a list.")

    previous_active_node_id = normalize_frontier_text(payload.get("active_node_id"))
    previous_active = nodes.get(previous_active_node_id) if previous_active_node_id else None
    previous_blocker = (
        normalize_frontier_text(previous_active.get("blocker_cluster"))
        if isinstance(previous_active, dict)
        else ""
    )
    active_node_id = normalize_frontier_text(worker_update.get("active_node_id"))
    if not active_node_id or active_node_id not in nodes:
        raise SupervisorError(
            f"Theorem-frontier worker update must identify an authoritative active node, got {active_node_id!r}."
        )
    active_record = nodes[active_node_id]
    active_node_after = worker_update.get("active_node_after")

    requested_action = worker_update["requested_action"]
    outcome = review["outcome"]
    paper_decision = paper_review.get("decision") if isinstance(paper_review, dict) else None
    nl_proof_decision = nl_proof_review.get("decision") if isinstance(nl_proof_review, dict) else None
    requires_paper_verifier = theorem_frontier_requires_paper_verifier(worker_update)
    proposed_nodes = list(worker_update.get("proposed_nodes", []))
    proposed_edges = [validate_theorem_frontier_edge(dict(edge)) for edge in (worker_update.get("proposed_edges", []) or [])]
    proposed_node_ids = {node["node_id"] for node in proposed_nodes}
    proposed_edge_pairs = {(edge["parent"], edge["child"]) for edge in proposed_edges}
    paper_approved_node_ids: Set[str] = set()
    paper_approved_edge_pairs: Set[Tuple[str, str]] = set()
    nl_proof_approved_node_ids: Set[str] = set()
    active_node_nl_approved = False
    admitted_node_ids: Set[str] = set()
    admitted_edge_pairs: Set[Tuple[str, str]] = set()

    old_children = theorem_frontier_node_children(nodes, edges, active_node_id)

    def set_active_node(next_node_id: str) -> Optional[Dict[str, Any]]:
        next_node_id = normalize_frontier_text(next_node_id)
        for node in nodes.values():
            if isinstance(node, dict) and node.get("status") == "active":
                node["status"] = "open"
        if not next_node_id:
            payload["active_node_id"] = None
            return None
        if next_node_id not in nodes:
            raise SupervisorError(f"Theorem-frontier active_node_id {next_node_id!r} is not present in nodes.")
        record = nodes[next_node_id]
        if record.get("status") not in {"open", "active"}:
            raise SupervisorError(
                f"Theorem-frontier active_node_id {next_node_id!r} must name an open/active node, "
                f"not {record.get('status')!r}."
            )
        record["status"] = "active"
        record["updated_at"] = timestamp_now()
        payload["active_node_id"] = next_node_id
        return record

    def replace_active_node_after(record: Dict[str, Any], updated_node: Optional[Dict[str, Any]]) -> None:
        if not isinstance(updated_node, dict):
            return
        replacement = theorem_frontier_node_record(
            updated_node,
            status=str(record.get("status") or "open"),
            parent_ids=record.get("parent_ids", []),
            child_ids=record.get("child_ids", []),
            lean_proof_status="unproved",
        )
        record.clear()
        record.update(replacement)

    def remove_direct_children(parent_id: str) -> List[str]:
        removed_children: List[str] = []
        kept_edges: List[Dict[str, Any]] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            if normalize_frontier_text(edge.get("parent")) == parent_id:
                removed_children.append(normalize_frontier_text(edge.get("child")))
                continue
            kept_edges.append(edge)
        edges[:] = kept_edges
        return removed_children

    def prune_detached_subtrees(start_ids: Sequence[str]) -> None:
        queue = list(start_ids)
        while queue:
            node_id = normalize_frontier_text(queue.pop())
            if not node_id or node_id == active_node_id or node_id not in nodes:
                continue
            parent_ids = theorem_frontier_node_parents(nodes, edges, node_id)
            if parent_ids:
                continue
            child_ids = theorem_frontier_node_children(nodes, edges, node_id)
            edges[:] = [
                edge
                for edge in edges
                if isinstance(edge, dict)
                and normalize_frontier_text(edge.get("parent")) != node_id
                and normalize_frontier_text(edge.get("child")) != node_id
            ]
            nodes.pop(node_id, None)
            queue.extend(child_ids)

    def validate_local_structure(
        *,
        change_kind: str,
        approved_nodes: set[str],
        approved_edges: set[Tuple[str, str]],
    ) -> List[Dict[str, Any]]:
        approved_edge_records = [
            edge for edge in proposed_edges if (edge["parent"], edge["child"]) in approved_edges
        ]
        new_node_ids = approved_nodes.difference({active_node_id})
        allowed_parents = {active_node_id, *new_node_ids}
        if change_kind == "EXPAND":
            allowed_children = set(old_children) | new_node_ids
        else:
            allowed_children = set(nodes.keys()).difference({active_node_id}) | new_node_ids
        for edge in approved_edge_records:
            if edge["parent"] not in allowed_parents:
                raise SupervisorError(
                    "Structural theorem-frontier edits may only introduce edges whose parent is the active node or a newly proposed node."
                )
            if edge["child"] not in allowed_children:
                raise SupervisorError(
                    "Structural theorem-frontier edits may only target the active node's current children, newly proposed nodes, "
                    "or (for REFUTE_REPLACE) already-authoritative existing nodes."
                )
        replacement_children = [edge["child"] for edge in approved_edge_records if edge["parent"] == active_node_id]
        if change_kind == "EXPAND" and old_children:
            adjacency: Dict[str, List[str]] = {}
            for edge in approved_edge_records:
                adjacency.setdefault(edge["parent"], []).append(edge["child"])
            seen: Set[str] = set()
            stack = [active_node_id]
            while stack:
                current = stack.pop()
                for child_id in adjacency.get(current, []):
                    if child_id not in seen:
                        seen.add(child_id)
                        if child_id in new_node_ids:
                            stack.append(child_id)
            if not set(old_children).issubset(seen):
                raise SupervisorError(
                    "EXPAND must keep every previous child reachable through the refined local sub-DAG."
                )
        if change_kind == "EXPAND" and not old_children and not replacement_children:
            raise SupervisorError("EXPAND on a leaf node must introduce at least one new child.")
        if change_kind == "REFUTE_REPLACE" and not replacement_children and approved_edge_records:
            raise SupervisorError(
                "REFUTE_REPLACE edges must include at least one outgoing edge from the active node when the new decomposition is non-empty."
            )
        return approved_edge_records

    if isinstance(paper_review, dict):
        paper_approved_node_ids = set(paper_review.get("approved_node_ids", []) or [])
        paper_approved_edge_pairs = {
            (entry["parent"], entry["child"])
            for entry in (paper_review.get("approved_edges", []) or [])
            if isinstance(entry, dict)
        }
        expected_change_kind = "REFUTE_REPLACE" if requested_action == "REFUTE_REPLACE" else "EXPAND"
        if requires_paper_verifier and paper_review.get("change_kind") != expected_change_kind:
            raise SupervisorError(
                "Paper-verifier change_kind does not match the structural theorem-frontier action being applied: "
                f"expected {expected_change_kind!r}, got {paper_review.get('change_kind')!r}."
            )
        allowed_approved_node_ids = {active_node_id} | proposed_node_ids
        if not paper_approved_node_ids.issubset(allowed_approved_node_ids):
            raise SupervisorError(
                "Paper-verifier approved_node_ids must refer only to the active node being changed or worker-proposed nodes."
            )
        if not paper_approved_edge_pairs.issubset(proposed_edge_pairs):
            raise SupervisorError(
                "Paper-verifier approved_edges must be a subset of the worker-proposed theorem-frontier edges."
            )

    if isinstance(nl_proof_review, dict):
        nl_proof_approved_node_ids = set(nl_proof_review.get("approved_node_ids", []) or [])
        expected_change_kind = "REFUTE_REPLACE" if requested_action == "REFUTE_REPLACE" else "EXPAND"
        if requires_paper_verifier and nl_proof_review.get("change_kind") != expected_change_kind:
            raise SupervisorError(
                "NL-proof verifier change_kind does not match the structural theorem-frontier action being applied: "
                f"expected {expected_change_kind!r}, got {nl_proof_review.get('change_kind')!r}."
            )
        allowed_nl_approved_node_ids = set(paper_approved_node_ids)
        if active_node_id:
            allowed_nl_approved_node_ids.add(active_node_id)
        if not nl_proof_approved_node_ids.issubset(allowed_nl_approved_node_ids):
            raise SupervisorError(
                "NL-proof verifier approved_node_ids must be a subset of the paper-verifier-approved node ids, "
                "except that it may additionally approve the unchanged active node."
            )
        active_node_nl_approved = active_node_id in nl_proof_approved_node_ids

    admitted_node_ids = (
        set(paper_approved_node_ids).intersection(nl_proof_approved_node_ids)
        if isinstance(nl_proof_review, dict)
        else set()
    )
    admitted_edge_pairs = set()
    if isinstance(paper_review, dict):
        for edge in proposed_edges:
            pair = (edge["parent"], edge["child"])
            if pair not in paper_approved_edge_pairs:
                continue
            parent_id = edge["parent"]
            child_id = edge["child"]
            parent_ok = parent_id == active_node_id or parent_id in admitted_node_ids
            child_ok = child_id in old_children or child_id in admitted_node_ids or child_id in nodes
            if parent_ok and child_ok:
                admitted_edge_pairs.add(pair)

    full_structural_admission = outcome in {"EXPANDED", "REFUTED_REPLACED"}
    partial_expand_admission = (
        requested_action == "EXPAND"
        and outcome == "STILL_OPEN"
        and isinstance(paper_review, dict)
        and isinstance(nl_proof_review, dict)
        and paper_decision in {"APPROVE", "APPROVE_WITH_CAVEAT"}
        and nl_proof_decision in {"APPROVE", "APPROVE_WITH_CAVEAT"}
        and active_node_nl_approved
    )

    if requires_paper_verifier and full_structural_admission and paper_decision not in {"APPROVE", "APPROVE_WITH_CAVEAT"}:
        raise SupervisorError("Cannot accept a structural theorem-frontier outcome without paper-verifier approval.")
    if requires_paper_verifier and full_structural_admission and nl_proof_decision != "APPROVE":
        raise SupervisorError(
            "Cannot accept a structural theorem-frontier outcome without a clean APPROVE from the NL-proof verifier."
        )

    if paper_decision == "REJECT" and full_structural_admission:
        raise SupervisorError("Paper-verifier rejected the structural change, so the structural outcome cannot be accepted.")
    if nl_proof_decision == "REJECT" and full_structural_admission:
        raise SupervisorError("NL-proof verifier rejected the structural change, so the structural outcome cannot be accepted.")

    assert_theorem_frontier_review_matches_node(review, active_record)
    active_record["status"] = "open"
    active_record["updated_at"] = timestamp_now()

    if isinstance(paper_review, dict):
        history = payload.get("paper_verifier_history")
        if isinstance(history, list):
            history.append(dict(paper_review))
    if isinstance(nl_proof_review, dict):
        history = payload.get("nl_proof_verifier_history")
        if isinstance(history, list):
            history.append(dict(nl_proof_review))

    should_apply_partial_expand = False
    approved_edge_records: List[Dict[str, Any]] = []
    if full_structural_admission:
        if active_node_id not in admitted_node_ids:
            raise SupervisorError(
                "Structural theorem-frontier admission must explicitly approve the changed active node."
            )
        approved_edge_records = validate_local_structure(
            change_kind="REFUTE_REPLACE" if outcome == "REFUTED_REPLACED" else "EXPAND",
            approved_nodes=admitted_node_ids,
            approved_edges=admitted_edge_pairs,
        )
    elif partial_expand_admission:
        try:
            approved_edge_records = validate_local_structure(
                change_kind="EXPAND",
                approved_nodes=admitted_node_ids,
                approved_edges=admitted_edge_pairs,
            )
        except SupervisorError:
            approved_edge_records = []
        else:
            should_apply_partial_expand = bool(
                admitted_node_ids.difference({active_node_id}) or approved_edge_records
            )

    if full_structural_admission or should_apply_partial_expand:
        replace_active_node_after(active_record, active_node_after)
        for node in proposed_nodes:
            if node["node_id"] not in admitted_node_ids:
                continue
            upsert_theorem_frontier_node(nodes, node, default_status="open")
        removed_children = remove_direct_children(active_node_id)
        for edge in approved_edge_records:
            add_theorem_frontier_edge(payload, edge)
        recompute_relationships(payload)
        assert_relationship_consistency(nodes, edges)
        assert_acyclic_dependency_graph(nodes, edges)
        if outcome == "REFUTED_REPLACED":
            prune_detached_subtrees(removed_children)
            recompute_relationships(payload)
        repair_theorem_frontier_closed_nodes(nodes, edges)

    requested_next_active_node_id = normalize_frontier_text(review.get("next_active_node_id"))
    next_active_node_id = ""
    if full_structural_admission or should_apply_partial_expand:
        next_active_node_id = requested_next_active_node_id
    elif outcome == "CLOSED":
        next_active_node_id = requested_next_active_node_id
    elif outcome in {"STILL_OPEN", "NO_FRONTIER_PROGRESS"}:
        next_active_node_id = active_node_id
    if (full_structural_admission or should_apply_partial_expand) and not next_active_node_id and worker_update.get("next_candidate_node_ids"):
        for candidate in worker_update["next_candidate_node_ids"]:
            candidate_id = normalize_frontier_text(candidate)
            if candidate_id:
                next_active_node_id = candidate_id
                break
    if outcome == "CLOSED" and next_active_node_id == active_node_id:
        next_active_node_id = ""

    if outcome == "CLOSED":
        active_record["lean_proof_status"] = "proved"
        proof_anchor = normalize_frontier_text(worker_update.get("lean_proof_anchor"))
        if proof_anchor:
            active_record["lean_proof_anchor"] = proof_anchor
        active_record["updated_at"] = timestamp_now()

    repair_theorem_frontier_closed_nodes(nodes, edges)

    resolved_next_active_node_id = resolve_theorem_frontier_next_active_node_id(
        nodes,
        edges,
        preferred_node_ids=[next_active_node_id],
        anchor_node_ids=(
            [requested_next_active_node_id, next_active_node_id, active_node_id]
            if (full_structural_admission or should_apply_partial_expand or outcome == "CLOSED")
            else [active_node_id]
        ),
    )
    current_node = set_active_node(resolved_next_active_node_id)

    same_node = previous_active_node_id and previous_active_node_id == payload.get("active_node_id")
    same_blocker = bool(previous_blocker) and previous_blocker == review["blocker_cluster"]
    active_node_age = int(metrics.get("active_node_age", 0) or 0) + 1 if same_node and payload.get("active_node_id") else (1 if payload.get("active_node_id") else 0)
    blocker_cluster_age = int(metrics.get("blocker_cluster_age", 0) or 0) + 1 if same_blocker else 1
    failed_close_attempts = (
        int(metrics.get("failed_close_attempts", 0) or 0) + 1
        if review["assessed_action"] == "CLOSE" and outcome != "CLOSED" and same_node
        else (1 if review["assessed_action"] == "CLOSE" and outcome != "CLOSED" else 0)
    )
    low_cone_purity_streak = (
        int(metrics.get("low_cone_purity_streak", 0) or 0) + 1
        if review["cone_purity"] == "LOW"
        else 0
    )
    structural_churn = int(metrics.get("structural_churn", 0) or 0)
    if outcome in {"EXPANDED", "REFUTED_REPLACED"}:
        structural_churn += 1
    elif outcome == "CLOSED":
        structural_churn = 0

    metrics.update(
        {
            "active_node_age": active_node_age,
            "blocker_cluster_age": blocker_cluster_age,
            "failed_close_attempts": failed_close_attempts,
            "low_cone_purity_streak": low_cone_purity_streak,
            "cone_purity": review["cone_purity"],
            "structural_churn": structural_churn,
        }
    )
    reasons: List[str] = []
    if failed_close_attempts >= THEOREM_FRONTIER_FAILED_CLOSE_THRESHOLD:
        reasons.append("same active node failed to close twice; expand or refactor it")
    if blocker_cluster_age >= THEOREM_FRONTIER_BLOCKER_CLUSTER_THRESHOLD:
        reasons.append("same blocker cluster persisted for five reviews; mandatory escalation")
    if low_cone_purity_streak >= THEOREM_FRONTIER_LOW_CONE_PURITY_THRESHOLD:
        reasons.append("low cone purity for two consecutive reviews")
    escalation["required"] = bool(reasons)
    escalation["reasons"] = reasons

    payload["current_action"] = review["assessed_action"]
    payload["current"] = {
        "cycle": cycle,
        "reviewed_node_id": active_node_id,
        "requested_next_active_node_id": requested_next_active_node_id or next_active_node_id or None,
        "next_active_node_id": payload.get("active_node_id"),
        "requested_action": requested_action,
        "assessed_action": review["assessed_action"],
        "outcome": outcome,
        "blocker_cluster": review["blocker_cluster"],
        "cone_purity": review["cone_purity"],
        "open_hypotheses": list(review["open_hypotheses"]),
        "paper_verifier_decision": paper_decision,
        "nl_proof_verifier_decision": nl_proof_decision,
        "justification": review["justification"],
        "structural_change_reason": worker_update.get("structural_change_reason", ""),
        "updated_at": timestamp_now(),
    }
    sync_theorem_frontier_metrics(payload)
    payload = validate_loaded_theorem_frontier_payload(payload)
    state["theorem_frontier"] = payload
    state["last_theorem_frontier_review"] = review
    if persist:
        JsonFile.dump(theorem_frontier_state_path(config), payload, mode=SUPERVISOR_SHARED_STATE_FILE_MODE)
        append_jsonl(
            theorem_frontier_history_path(config),
            {
                "cycle": cycle,
                "mode": "full",
                "active_node_id": payload.get("active_node_id"),
                "reviewed_node_id": active_node_id,
                "requested_next_active_node_id": requested_next_active_node_id or next_active_node_id or None,
                "next_active_node_id": payload.get("active_node_id"),
                "assessed_action": review["assessed_action"],
                "outcome": outcome,
                "blocker_cluster": review["blocker_cluster"],
                "cone_purity": review["cone_purity"],
                "open_hypotheses": list(review["open_hypotheses"]),
                "paper_verifier_decision": paper_decision,
                "nl_proof_verifier_decision": nl_proof_decision,
                "metrics": dict(metrics),
                "escalation": dict(escalation),
                "current_anchor": current_node.get("lean_anchor") if isinstance(current_node, dict) else None,
            },
        )
        update_supervisor_tasks_file(config, current_phase(config, state))
    return payload


def preflight_theorem_frontier_full_state_update(
    config: Config,
    state: Dict[str, Any],
    worker_update: Dict[str, Any],
    review: Dict[str, Any],
    *,
    cycle: int,
) -> None:
    preview_state = deep_copy_jsonish(state)
    preview_payload = update_theorem_frontier_full_state(
        config,
        preview_state,
        worker_update,
        review,
        preview_state.get("last_theorem_frontier_paper_review")
        if isinstance(preview_state.get("last_theorem_frontier_paper_review"), dict)
        else None,
        preview_state.get("last_theorem_frontier_nl_proof_review")
        if isinstance(preview_state.get("last_theorem_frontier_nl_proof_review"), dict)
        else None,
        cycle=cycle,
        persist=False,
    )
    preview_nodes = preview_payload.get("nodes") if isinstance(preview_payload, dict) else None
    preview_edges = preview_payload.get("edges") if isinstance(preview_payload, dict) else None
    if isinstance(preview_nodes, dict) and isinstance(preview_edges, list):
        active_node_id = normalize_frontier_text(review.get("active_node_id"))
        active_node = preview_nodes.get(active_node_id) if active_node_id else None
        if isinstance(active_node, dict):
            if str(review.get("outcome") or "").strip().upper() == "CLOSED":
                sync_theorem_frontier_generated_files(
                    config,
                    preview_state,
                    ensure_active_proof=False,
                    ensure_proof_node_ids=[active_node_id],
                )
                child_nodes = [
                    preview_nodes[child_id]
                    for child_id in theorem_frontier_node_children(preview_nodes, preview_edges, active_node_id)
                    if child_id in preview_nodes and isinstance(preview_nodes[child_id], dict)
                ]
                validate_theorem_frontier_generated_local_proof(
                    config,
                    active_node,
                    child_nodes,
                    label=f"cycle-{cycle:04d}-{active_node_id.replace('.', '-')}",
                )
            else:
                declaration_index = collect_repo_lean_declarations(config)
                if not declaration_index:
                    return
                theorem_frontier_statement_binding(config, active_node, declaration_index)


def theorem_frontier_review_requires_admission_preflight(review: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(review, dict):
        return False
    outcome = str(review.get("outcome") or "").strip().upper()
    return outcome in {"CLOSED", "EXPANDED", "REFUTED_REPLACED"}


def validate_worker_owned_theorem_frontier_update(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    worker_update: Dict[str, Any],
) -> None:
    if not theorem_frontier_full_enabled(config, phase):
        return
    requested_action = str(worker_update.get("requested_action") or "").strip().upper()
    if requested_action not in {"EXPAND", "REFUTE_REPLACE"}:
        return
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return
    declaration_index = collect_repo_lean_declarations(config)
    if not declaration_index:
        raise WorkerFixableValidationError(
            "Cannot validate theorem-frontier structural bindings without discoverable Lean declarations."
        )

    def _check_node(node: Dict[str, Any], *, label: str) -> None:
        try:
            theorem_frontier_statement_binding(config, node, declaration_index)
        except SupervisorError as exc:
            node_id = normalize_frontier_text(node.get("node_id")) or label
            raise WorkerFixableValidationError(
                f"Theorem-frontier structural update for {node_id!r} is not admissible yet: {exc} "
                f"Update the node metadata so lean_statement exactly matches the anchored source declaration "
                f"before resubmitting {requested_action}."
            ) from exc

    active_node_after = worker_update.get("active_node_after")
    if isinstance(active_node_after, dict):
        _check_node(active_node_after, label="active_node_after")
    for proposed in worker_update.get("proposed_nodes", []) or []:
        if isinstance(proposed, dict):
            _check_node(proposed, label="proposed node")


def validate_theorem_frontier_terminal_repo_evidence(config: Config, state: Dict[str, Any]) -> None:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict) or payload.get("mode") != "full":
        return
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        return
    sync_theorem_frontier_generated_files(
        config,
        state,
        ensure_active_proof=True,
        ensure_proof_node_ids=[
            node_id
            for node_id, node in nodes.items()
            if isinstance(node, dict) and str(node.get("lean_proof_status") or "").strip().lower() == "proved"
        ],
    )
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if str(node.get("lean_proof_status") or "").strip().lower() != "proved":
            continue
        child_nodes = [
            nodes[child_id]
            for child_id in theorem_frontier_node_children(nodes, edges, node_id)
            if child_id in nodes and isinstance(nodes[child_id], dict)
        ]
        validate_theorem_frontier_generated_local_proof(
            config,
            node,
            child_nodes,
            label=f"terminal-{node_id.replace('.', '-')}",
        )


def validate_paper_main_results_manifest_repo_evidence(config: Config) -> None:
    manifest = load_validated_paper_main_results_manifest(config)
    declaration_index = collect_repo_lean_declarations(config)
    if not declaration_index:
        raise SupervisorError(
            "Cannot advance from theorem_stating without discoverable Lean declarations for the seeded manifest nodes."
        )
    nodes = manifest.get("nodes")
    if not isinstance(nodes, list):
        raise SupervisorError("Paper coarse-DAG manifest is missing node records.")
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        theorem_frontier_statement_binding(config, raw_node, declaration_index)


def phase_specific_worker_statuses(phase: str) -> Sequence[str]:
    if phase == "planning":
        return WORKER_STATUSES
    return ("NOT_STUCK", "STUCK", "DONE")


def phase_specific_reviewer_decisions(phase: str) -> Sequence[str]:
    if phase == "planning":
        return REVIEWER_DECISIONS
    if phase == "proof_formalization":
        return ("CONTINUE", "ADVANCE_PHASE", "STUCK")
    if is_style_cleanup_phase(phase):
        return ("CONTINUE", "STUCK", "DONE")
    return ("CONTINUE", "ADVANCE_PHASE", "STUCK")


def active_branch_episode(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    episode = state.get("active_branch_episode")
    if isinstance(episode, dict):
        return episode
    return None


def branch_episode_dir(config: Config, episode_id: str) -> Path:
    return config.state_dir / "branches" / episode_id


def branch_strategy_keywords() -> Dict[str, Tuple[str, ...]]:
    return {
        "strong": (
            "pivot",
            "route change",
            "rewrite",
            "counterexample",
            "too weak",
            "too strong",
            "paper-faithful",
            "topological route",
            "combinatorial route",
            "major refactor",
            "mismatch",
        ),
        "general": (
            "route",
            "refactor",
            "blocked",
            "repair",
            "interface bug",
            "direct route",
            "backup route",
            "deleted-spur",
            "containment",
            "same-level continuation",
            "entrance",
        ),
    }


def branch_strategy_signal_tags(decision: Dict[str, Any]) -> List[str]:
    text = " ".join(
        str(decision.get(key, "")).strip().lower()
        for key in ("reason", "next_prompt")
    )
    tags: List[str] = []
    if str(decision.get("decision", "")).strip().upper() == "STUCK":
        tags.append("stuck")
    keywords = branch_strategy_keywords()
    for category, terms in keywords.items():
        for term in terms:
            if term in text:
                tags.append(f"{category}:{term}")
    return tags


def should_consider_branching(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    decision: Dict[str, Any],
) -> bool:
    if not branching_enabled(config) and not can_propose_branch_replacement(state, config):
        return False
    if phase != "proof_formalization":
        return False
    if active_branch_episode(state):
        return False
    if pending_branch_proposal(state):
        return False
    if branch_review_count(state) < next_branch_proposal_review_count(state):
        return False
    cycle = int(decision.get("cycle", state.get("cycle", 0)) or 0)
    if state.get("last_branch_consideration_cycle") == cycle:
        return False
    tags = branch_strategy_signal_tags(decision)
    if "stuck" in tags:
        return True
    strong_tags = [tag for tag in tags if tag.startswith("strong:")]
    if strong_tags:
        return True
    general_tags = {tag for tag in tags if tag.startswith("general:")}
    return len(general_tags) >= 2


def deep_copy_jsonish(data: Any) -> Any:
    return json.loads(json.dumps(data, ensure_ascii=False))


def sanitize_branch_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-.")
    return cleaned or "branch"


def branch_review_count(state: Dict[str, Any]) -> int:
    reviews = state.get("review_log")
    return len(reviews) if isinstance(reviews, list) else 0


def branch_progress_count(branch_state: Dict[str, Any], base_review_count: int) -> int:
    return max(0, branch_review_count(branch_state) - base_review_count)


def branch_episode_preflight_error(config: Config) -> Optional[str]:
    if not shutil.which("git"):
        return "git is not available on PATH"
    if not repo_is_git_repository(config):
        return "branching requires the repo to already be a git worktree"
    if not repo_has_git_commits(config):
        return "branching requires the repo to already have at least one commit"
    status = git_output(config, ["status", "--short"]).strip()
    if status:
        return "branching requires a clean git worktree"
    return None


def format_json_enum(values: Sequence[str]) -> str:
    return " | ".join(json.dumps(value) for value in values)


def stuck_recovery_attempts(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    attempts = state.get("stuck_recovery_attempts")
    if isinstance(attempts, list):
        return attempts
    state["stuck_recovery_attempts"] = []
    return state["stuck_recovery_attempts"]


def clear_stuck_recovery(state: Dict[str, Any]) -> None:
    state["stuck_recovery_attempts"] = []
    state["stuck_recovery_last_trigger_cycle"] = None


def latest_stuck_recovery_attempt(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attempts = stuck_recovery_attempts(state)
    return attempts[-1] if attempts else None


def current_stuck_recovery_attempt_number(state: Dict[str, Any]) -> int:
    return len(stuck_recovery_attempts(state)) + 1


def is_branch_run(state: Dict[str, Any]) -> bool:
    if branch_lineage_entries(state):
        return True
    context = state.get("branch_context")
    return isinstance(context, dict) and bool(context)


def stuck_recovery_attempt_limit(state: Dict[str, Any], policy: Optional[Policy] = None) -> int:
    if policy is not None:
        return (
            policy.stuck_recovery.branch_max_attempts
            if is_branch_run(state)
            else policy.stuck_recovery.mainline_max_attempts
        )
    policy_meta = state.get("policy")
    effective = policy_meta.get("effective") if isinstance(policy_meta, dict) else {}
    if isinstance(effective, dict):
        stuck_block = effective.get("stuck_recovery")
        if isinstance(stuck_block, dict):
            key = "branch_max_attempts" if is_branch_run(state) else "mainline_max_attempts"
            try:
                return max(1, int(stuck_block.get(key)))
            except (TypeError, ValueError):
                pass
    return MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS if is_branch_run(state) else MAX_STUCK_RECOVERY_ATTEMPTS


def branch_strategy_limit(config: Config, state: Dict[str, Any]) -> int:
    if branching_enabled(config):
        return config.branching.max_current_branches
    return parent_branch_capacity(state, config)


def pending_branch_proposal(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    proposal = state.get("pending_branch_proposal")
    return proposal if isinstance(proposal, dict) else None


def clear_pending_branch_proposal(state: Dict[str, Any]) -> None:
    state["pending_branch_proposal"] = None


def store_pending_branch_proposal(
    state: Dict[str, Any],
    proposal: Dict[str, Any],
    *,
    cycle: int,
) -> Dict[str, Any]:
    stored = deep_copy_jsonish(proposal)
    stored["proposal_cycle"] = cycle
    stored["proposal_review_count"] = branch_review_count(state)
    stored["proposal_timestamp"] = timestamp_now()
    state["pending_branch_proposal"] = stored
    return stored


def next_branch_proposal_review_count(state: Dict[str, Any]) -> int:
    value = state.get("next_branch_proposal_review_count", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def last_review_cycle(state: Dict[str, Any]) -> int:
    last_review = state.get("last_review")
    if not isinstance(last_review, dict):
        return 0
    value = last_review.get("cycle", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def last_validation_cycle(state: Dict[str, Any]) -> int:
    last_validation = state.get("last_validation") or {}
    value = last_validation.get("cycle", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def current_cycle_lean_baseline(state: Dict[str, Any], cycle: int) -> Optional[Dict[str, Any]]:
    baseline = state.get("current_cycle_lean_baseline")
    if not isinstance(baseline, dict):
        return None
    try:
        stored_cycle = int(baseline.get("cycle", 0) or 0)
    except (TypeError, ValueError):
        return None
    if stored_cycle != cycle or not isinstance(baseline.get("files"), dict):
        return None
    return baseline


def ensure_current_cycle_lean_baseline(config: Config, state: Dict[str, Any], cycle: int) -> bool:
    if current_cycle_lean_baseline(state, cycle) is not None:
        return False
    state["current_cycle_lean_baseline"] = {
        "cycle": cycle,
        "captured_at": timestamp_now(),
        "files": capture_lean_tree_snapshot(config),
    }
    return True


def determine_resume_cycle_and_stage(state: Dict[str, Any]) -> Tuple[int, str]:
    current_cycle = int(state.get("cycle", 0) or 0)
    if current_cycle <= 0:
        return 1, "worker"

    if last_review_cycle(state) >= current_cycle:
        return current_cycle + 1, "worker"

    last_validation = state.get("last_validation")
    if (
        isinstance(last_validation, dict)
        and last_validation_cycle(state) == current_cycle
        and isinstance(state.get("last_worker_handoff"), dict)
        and "last_worker_output" in state
    ):
        return current_cycle, "reviewer"

    return current_cycle, "worker"


def has_unhandled_stuck_review(state: Dict[str, Any]) -> bool:
    last_review = state.get("last_review") or {}
    review_phase_raw = str(last_review.get("phase", "")).strip()
    review_phase = normalize_phase_name(review_phase_raw) if review_phase_raw else "proof_formalization"
    if review_phase != "proof_formalization":
        return False
    if str(last_review.get("decision", "")).strip().upper() != "STUCK":
        return False
    trigger_cycle = last_review_cycle(state)
    return state.get("stuck_recovery_last_trigger_cycle") != trigger_cycle


def can_attempt_stuck_recovery(state: Dict[str, Any], policy: Optional[Policy] = None) -> bool:
    return has_unhandled_stuck_review(state) and len(stuck_recovery_attempts(state)) < stuck_recovery_attempt_limit(
        state,
        policy=policy,
    )


def stuck_recovery_exhausted(state: Dict[str, Any], policy: Optional[Policy] = None) -> bool:
    return has_unhandled_stuck_review(state) and len(stuck_recovery_attempts(state)) >= stuck_recovery_attempt_limit(
        state,
        policy=policy,
    )


def record_stuck_recovery_attempt(
    state: Dict[str, Any],
    *,
    trigger_cycle: int,
    phase: str,
    suggestion: Dict[str, Any],
) -> Dict[str, Any]:
    attempts = stuck_recovery_attempts(state)
    entry = dict(suggestion)
    entry["phase"] = phase
    entry["attempt"] = len(attempts) + 1
    entry["trigger_cycle"] = trigger_cycle
    attempts.append(entry)
    state["stuck_recovery_last_trigger_cycle"] = trigger_cycle
    return entry








def cleanup_last_good_commit(state: Dict[str, Any]) -> Optional[str]:
    value = state.get("cleanup_last_good_commit")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def git_force_with_lease_required(state: Dict[str, Any], *, branch: Optional[str] = None) -> bool:
    if not bool(state.get("git_force_with_lease_required")):
        return False
    saved_branch = str(state.get("git_force_with_lease_branch") or "").strip()
    if branch and saved_branch and saved_branch != branch:
        return False
    return True


def mark_git_force_with_lease_required(
    state: Dict[str, Any],
    *,
    branch: str,
    reason: str,
) -> None:
    state["git_force_with_lease_required"] = True
    state["git_force_with_lease_branch"] = branch
    state["git_force_with_lease_reason"] = reason


def clear_git_force_with_lease_required(state: Dict[str, Any]) -> None:
    state.pop("git_force_with_lease_required", None)
    state.pop("git_force_with_lease_branch", None)
    state.pop("git_force_with_lease_reason", None)


def update_cleanup_last_good_commit(
    config: Config,
    state: Dict[str, Any],
    validation_summary: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    head: Optional[str] = None
    if isinstance(validation_summary, dict):
        git_summary = validation_summary.get("git")
        if isinstance(git_summary, dict):
            raw_head = git_summary.get("head")
            if isinstance(raw_head, str) and raw_head.strip():
                head = raw_head.strip()
    if head is None:
        head = current_git_head(config)
    if head:
        state["cleanup_last_good_commit"] = head
    return head


def restore_cleanup_last_good_commit(
    config: Config,
    state: Dict[str, Any],
    *,
    cycle: int,
    reason: str,
) -> Dict[str, Any]:
    commit = cleanup_last_good_commit(state)
    if not commit:
        raise SupervisorError("Cleanup rollback requested but no last good commit is recorded.")
    ensure_git_command_ok(config, ["reset", "--hard", commit])
    if git_is_enabled(config):
        current_branch = current_git_branch(config)
        ensure_git_command_ok(
            config,
            ["push", "--force-with-lease", config.git.remote_name, f"HEAD:{current_branch}"],
        )
    restored_validation = run_validation(config, PHASE_PROOF_COMPLETE_STYLE_CLEANUP, cycle)
    state["last_validation"] = restored_validation
    update_cleanup_last_good_commit(config, state, restored_validation)
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
        kind="cleanup_revert",
        actor="supervisor",
        target="workflow",
        content={"reason": reason, "restored_commit": commit},
        content_type="json",
        summary=f"Reverted cleanup worktree to last good commit {commit[:12]}",
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
        kind="validation_summary",
        actor="supervisor",
        target="workflow",
        content=restored_validation,
        content_type="json",
    )
    save_state(config, state)
    return restored_validation


def _cycle_checkpoint_file_specs(config: Config) -> List[Tuple[str, Path]]:
    state_files = [
        config.state_dir / "state.json",
        validation_summary_path(config),
        config.state_dir / "review_log.jsonl",
        config.state_dir / "validation_log.jsonl",
        config.state_dir / "stuck_recovery_log.jsonl",
        theorem_frontier_state_path(config),
        theorem_frontier_history_path(config),
        config.state_dir / "theorem_frontier_paper_verifier_log.jsonl",
        config.state_dir / "theorem_frontier_nl_proof_verifier_log.jsonl",
        paper_main_results_manifest_path(config),
        config.state_dir / "branch_strategy_log.jsonl",
        config.state_dir / "branch_selection_log.jsonl",
        config.state_dir / "branch_replacement_log.jsonl",
    ]
    cycle_artifact_files: List[Path] = []
    if cycle_records_dir(config).exists():
        cycle_artifact_files.extend(path for path in cycle_records_dir(config).rglob("*") if path.is_file())
    state_files.extend(cycle_artifact_files)
    chat_files = [
        chat_repo_meta_path(config),
        chat_repo_events_path(config),
        chat_repo_events_manifest_path(config),
    ]
    dag_files = [
        dag_repo_meta_path(config),
        dag_repo_meta_web_path(config),
        dag_frontier_path(config),
        dag_frontier_web_path(config),
        dag_frontier_history_path(config),
        dag_frontier_history_web_path(config),
    ]
    repo_files = [
        config.workflow.human_input_path,
        config.workflow.input_request_path,
    ]
    specs: List[Tuple[str, Path]] = []
    specs.extend(("state", path) for path in state_files)
    specs.extend(("chat", path) for path in chat_files)
    specs.extend(("dag", path) for path in dag_files)
    specs.extend(("repo", path) for path in repo_files)
    return specs


def _checkpoint_root_for_category(config: Config, category: str) -> Path:
    if category == "state":
        return config.state_dir
    if category == "chat":
        return chat_repo_dir(config)
    if category == "dag":
        return dag_repo_dir(config)
    if category == "repo":
        return config.repo_path
    raise SupervisorError(f"Unknown checkpoint category {category!r}.")


def _checkpoint_relative_path(config: Config, category: str, path: Path) -> Path:
    root = _checkpoint_root_for_category(config, category)
    return path.resolve().relative_to(root.resolve())


def _checkpoint_git_head(config: Config, validation_summary: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if isinstance(validation_summary, dict):
        git_summary = validation_summary.get("git")
        if isinstance(git_summary, dict):
            raw_head = str(git_summary.get("head") or "").strip()
            if raw_head:
                return raw_head
    return current_git_head(config)


def list_cycle_checkpoints(config: Config) -> List[Dict[str, Any]]:
    manifest = JsonFile.load(cycle_checkpoint_manifest_path(config), {"checkpoints": []})
    checkpoints = manifest.get("checkpoints") if isinstance(manifest, dict) else []
    normalized_by_cycle: Dict[int, Dict[str, Any]] = {}
    if isinstance(checkpoints, list):
        for item in checkpoints:
            if not isinstance(item, dict):
                continue
            try:
                cycle = int(item.get("cycle", 0) or 0)
            except (TypeError, ValueError):
                continue
            if cycle <= 0:
                continue
            payload = dict(item)
            payload["cycle"] = cycle
            normalized_by_cycle[cycle] = payload

    checkpoints_dir = cycle_checkpoints_dir(config)
    if checkpoints_dir.exists():
        for path in checkpoints_dir.iterdir():
            if not path.is_dir() or not path.name.startswith("cycle-"):
                continue
            metadata_path = path / "metadata.json"
            metadata: Optional[Dict[str, Any]] = None
            if metadata_path.exists():
                loaded = JsonFile.load(metadata_path, None)
                if isinstance(loaded, dict):
                    metadata = dict(loaded)

            state_path = path / "state" / "state.json"
            inferred_cycle: Optional[int] = None
            if metadata is not None:
                try:
                    inferred_cycle = int(metadata.get("cycle", 0) or 0)
                except (TypeError, ValueError):
                    inferred_cycle = None
            if (inferred_cycle is None or inferred_cycle <= 0) and state_path.exists():
                checkpoint_state = JsonFile.load(state_path, None)
                if isinstance(checkpoint_state, dict):
                    try:
                        inferred_cycle = int(checkpoint_state.get("cycle", 0) or 0)
                    except (TypeError, ValueError):
                        inferred_cycle = None
            if inferred_cycle is None or inferred_cycle <= 0:
                match = re.fullmatch(r"cycle-(\d{4})", path.name)
                if match:
                    inferred_cycle = int(match.group(1))
            if inferred_cycle is None or inferred_cycle <= 0:
                continue

            payload = dict(metadata or {})
            payload["cycle"] = inferred_cycle
            payload.setdefault("checkpoint_dir", str(path))
            if "completed_phase" not in payload and state_path.exists():
                checkpoint_state = JsonFile.load(state_path, None)
                if isinstance(checkpoint_state, dict):
                    last_review = checkpoint_state.get("last_review")
                    if isinstance(last_review, dict) and str(last_review.get("phase") or "").strip():
                        payload["completed_phase"] = str(last_review.get("phase") or "").strip()
                    elif str(checkpoint_state.get("phase") or "").strip():
                        payload["completed_phase"] = str(checkpoint_state.get("phase") or "").strip()
            normalized_by_cycle[inferred_cycle] = payload

    normalized = list(normalized_by_cycle.values())
    normalized.sort(key=lambda entry: int(entry.get("cycle", 0) or 0), reverse=True)
    return normalized


def _ephemeral_cycle_number_from_path(path: Path) -> Optional[int]:
    match = re.search(r"-(\d{4})(?:\.[^.]+)*$", path.name)
    if not match:
        return None
    return int(match.group(1))


def clear_future_cycle_ephemera(config: Config, completed_cycle: int) -> None:
    prompt_dirs = [supervisor_prompts_dir(config), config.state_dir / "prompts"]
    for prompts_dir in prompt_dirs:
        if not prompts_dir.exists():
            continue
        for path in prompts_dir.iterdir():
            if not path.is_file():
                continue
            cycle_num = _ephemeral_cycle_number_from_path(path)
            if cycle_num is not None and cycle_num > completed_cycle:
                path.unlink(missing_ok=True)  # type: ignore[arg-type]

    logs_dir = config.state_dir / "logs"
    if logs_dir.exists():
        for path in logs_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(".latest.ansi.log") or name.endswith(".all.ansi.log"):
                path.unlink(missing_ok=True)  # type: ignore[arg-type]
                continue
            if name.endswith(".ansi.log"):
                cycle_num = _ephemeral_cycle_number_from_path(path)
                if cycle_num is not None and cycle_num > completed_cycle:
                    path.unlink(missing_ok=True)  # type: ignore[arg-type]

    runtime_dir = supervisor_runtime_markers_dir(config)
    if runtime_dir.exists():
        for path in runtime_dir.iterdir():
            if not path.is_file():
                continue
            cycle_num = _ephemeral_cycle_number_from_path(path)
            if cycle_num is not None and cycle_num > completed_cycle:
                path.unlink(missing_ok=True)  # type: ignore[arg-type]

    scripts_dir = supervisor_scripts_dir(config)
    if scripts_dir.exists():
        for path in scripts_dir.iterdir():
            if not path.is_file():
                continue
            cycle_num = _ephemeral_cycle_number_from_path(path)
            if cycle_num is not None and cycle_num > completed_cycle:
                path.unlink(missing_ok=True)  # type: ignore[arg-type]


def clear_future_cycle_checkpoints(config: Config, completed_cycle: int) -> None:
    for checkpoint in list_cycle_checkpoints(config):
        cycle = int(checkpoint.get("cycle", 0) or 0)
        if cycle <= completed_cycle:
            continue
        checkpoint_dir = Path(str(checkpoint.get("checkpoint_dir", "")))
        if checkpoint_dir.exists():
            shutil.rmtree(checkpoint_dir, ignore_errors=True)

    def _trim_manifest(current: Dict[str, Any]) -> Dict[str, Any]:
        checkpoints = current.get("checkpoints") if isinstance(current, dict) else None
        if not isinstance(checkpoints, list):
            checkpoints = []
        kept = [
            item
            for item in checkpoints
            if isinstance(item, dict) and int(item.get("cycle", 0) or 0) <= completed_cycle
        ]
        kept.sort(key=lambda item: int(item.get("cycle", 0) or 0), reverse=True)
        return {"checkpoints": kept}

    JsonFile.update(cycle_checkpoint_manifest_path(config), {"checkpoints": []}, _trim_manifest)


def select_cycle_checkpoint(
    config: Config,
    *,
    cycle: Optional[int] = None,
    after_phase: Optional[str] = None,
) -> Dict[str, Any]:
    checkpoints = list_cycle_checkpoints(config)
    if cycle is not None:
        for entry in checkpoints:
            if int(entry.get("cycle", 0) or 0) == int(cycle):
                return entry
        raise SupervisorError(f"No completed-cycle checkpoint exists for cycle {cycle}.")
    if after_phase is not None:
        target_phase = normalize_phase_name(after_phase)
        for entry in checkpoints:
            if normalize_phase_name(str(entry.get("completed_phase") or "")) == target_phase:
                return entry
        raise SupervisorError(f"No completed-cycle checkpoint exists after phase {target_phase}.")
    raise SupervisorError("Checkpoint selection requires either `cycle` or `after_phase`.")


def write_completed_cycle_checkpoint(
    config: Config,
    state: Dict[str, Any],
    *,
    cycle: int,
    completed_phase: str,
    decision: Dict[str, Any],
    validation_summary: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    checkpoint_dir = cycle_checkpoint_dir(config, cycle)
    if checkpoint_dir.exists():
        shutil.rmtree(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    files: List[Dict[str, Any]] = []
    for category, source in _cycle_checkpoint_file_specs(config):
        if not source.exists() or not source.is_file():
            continue
        rel = _checkpoint_relative_path(config, category, source)
        target = checkpoint_dir / category / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files.append(
            {
                "category": category,
                "relative_path": rel.as_posix(),
            }
        )

    phase_after = current_phase(config, state)
    git_head = _checkpoint_git_head(config, validation_summary)
    entry = {
        "cycle": int(cycle),
        "completed_phase": completed_phase,
        "phase_after": phase_after,
        "decision": str(decision.get("decision", "")).strip(),
        "created_at": timestamp_now(),
        "git_head": git_head,
        "checkpoint_dir": str(checkpoint_dir),
        "file_count": len(files),
        "files": files,
    }
    JsonFile.dump(checkpoint_dir / "metadata.json", entry)
    normalize_checkpoint_tree_permissions(checkpoint_dir)

    def _update_manifest(current: Dict[str, Any]) -> Dict[str, Any]:
        checkpoints = current.get("checkpoints") if isinstance(current, dict) else None
        if not isinstance(checkpoints, list):
            checkpoints = []
        updated = [item for item in checkpoints if not (isinstance(item, dict) and int(item.get("cycle", 0) or 0) == int(cycle))]
        updated.append(entry)
        updated.sort(key=lambda item: int(item.get("cycle", 0) or 0), reverse=True)
        return {"checkpoints": updated}

    JsonFile.update(cycle_checkpoint_manifest_path(config), {"checkpoints": []}, _update_manifest)
    return entry


def request_cycle_boundary_restart(config: Config, *, reason: str = "") -> Dict[str, Any]:
    payload = {
        "requested_at": timestamp_now(),
        "reason": str(reason or "").strip(),
    }
    JsonFile.dump(cycle_boundary_restart_request_path(config), payload)
    return payload


def consume_cycle_boundary_restart_request(config: Config) -> Optional[Dict[str, Any]]:
    path = cycle_boundary_restart_request_path(config)
    if not path.exists():
        return None
    payload = JsonFile.load(path, {})
    path.unlink(missing_ok=True)  # type: ignore[arg-type]
    if not isinstance(payload, dict):
        payload = {}
    return payload


def honor_cycle_boundary_restart_request(
    config: Config,
    state: Dict[str, Any],
    *,
    cycle: int,
    phase: str,
    decision: Dict[str, Any],
    already_stopping: bool = False,
) -> bool:
    request = consume_cycle_boundary_restart_request(config)
    if request is None:
        return False
    content = {
        "cycle": int(cycle),
        "phase": phase,
        "decision": str(decision.get("decision", "")).strip(),
        "requested_at": str(request.get("requested_at", "")).strip(),
        "reason": str(request.get("reason", "")).strip(),
    }
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=current_phase(config, state),
        kind="cycle_boundary_restart",
        actor="supervisor",
        target="workflow",
        content=content,
        content_type="json",
        summary=f"Boundary restart requested after cycle {cycle}",
    )
    if already_stopping:
        print(
            f"Reached requested cycle boundary after cycle {cycle}; "
            "supervisor was already stopping for this cycle."
        )
        return False
    print(
        f"Reached requested cycle boundary after cycle {cycle}; "
        "stopping so the supervisor can be restarted cleanly."
    )
    return True


def restore_cycle_checkpoint(
    config: Config,
    *,
    cycle: Optional[int] = None,
    after_phase: Optional[str] = None,
) -> Dict[str, Any]:
    checkpoint = select_cycle_checkpoint(config, cycle=cycle, after_phase=after_phase)
    checkpoint_dir = Path(str(checkpoint.get("checkpoint_dir", "")))
    if not checkpoint_dir.exists():
        raise SupervisorError(f"Checkpoint directory is missing: {checkpoint_dir}")
    staged_checkpoint_root = Path(tempfile.mkdtemp(prefix="lagent-cycle-checkpoint-"))
    staged_checkpoint_dir = staged_checkpoint_root / checkpoint_dir.name
    shutil.copytree(checkpoint_dir, staged_checkpoint_dir)
    checkpoint_dir = staged_checkpoint_dir

    try:
        git_head = str(checkpoint.get("git_head") or "").strip()
        if git_head:
            ensure_git_command_ok(config, ["reset", "--hard", git_head])
            ensure_git_command_ok(config, ["clean", "-fd"])

        if chat_repo_dir(config).exists():
            shutil.rmtree(chat_repo_dir(config))
        if dag_repo_dir(config).exists():
            shutil.rmtree(dag_repo_dir(config))
        if cycle_records_dir(config).exists():
            shutil.rmtree(cycle_records_dir(config))

        for _, live_path in _cycle_checkpoint_file_specs(config):
            if live_path.exists():
                live_path.unlink()

        files = checkpoint.get("files")
        if not isinstance(files, list):
            raise SupervisorError(f"Checkpoint metadata is missing file entries: {checkpoint_dir / 'metadata.json'}")

        for item in files:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "")).strip()
            rel = Path(str(item.get("relative_path", "")).strip())
            if not category or not str(rel):
                continue
            source = checkpoint_dir / category / rel
            if not source.exists():
                raise SupervisorError(f"Checkpoint file is missing: {source}")
            live_path = _checkpoint_root_for_category(config, category) / rel
            live_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, live_path)

        clear_supervisor_artifacts(
            config,
            worker_handoff_path(config),
            reviewer_decision_path(config),
            theorem_frontier_worker_update_path(config),
            theorem_frontier_review_path(config),
            theorem_frontier_paper_verifier_path(config),
            theorem_frontier_nl_proof_verifier_path(config),
        )
        clear_future_cycle_checkpoints(config, int(checkpoint.get("cycle", 0) or 0))
        def _restore_manifest_entry(current: Dict[str, Any]) -> Dict[str, Any]:
            checkpoints = current.get("checkpoints") if isinstance(current, dict) else None
            if not isinstance(checkpoints, list):
                checkpoints = []
            updated = [
                item
                for item in checkpoints
                if not (isinstance(item, dict) and int(item.get("cycle", 0) or 0) == int(checkpoint.get("cycle", 0) or 0))
            ]
            updated.append(dict(checkpoint))
            updated.sort(key=lambda item: int(item.get("cycle", 0) or 0), reverse=True)
            return {"checkpoints": updated}

        JsonFile.update(cycle_checkpoint_manifest_path(config), {"checkpoints": []}, _restore_manifest_entry)
        clear_future_cycle_ephemera(config, int(checkpoint.get("cycle", 0) or 0))
        shutil.rmtree(scope_root_dir(config), ignore_errors=True)
        shutil.rmtree(supervisor_control_root_dir(config), ignore_errors=True)
        normalize_repo_mutable_permissions(config)
        normalize_worker_readable_state_permissions(config)
        state = load_state(config)
        if git_is_enabled(config):
            mark_git_force_with_lease_required(
                state,
                branch=current_git_branch(config),
                reason=f"restore_cycle_checkpoint:{int(checkpoint.get('cycle', 0) or 0)}",
            )
        else:
            clear_git_force_with_lease_required(state)
        state["roles"] = {}
        save_state(config, state)
        ensure_chat_site(config)
        if chat_repo_events_path(config).exists():
            rebuild_chat_event_chunks_from_legacy_log(config)
        refresh_chat_markdown_metadata(config, update_manifest=True)
        ensure_dag_site(config)
        export_dag_meta(config, state)
        if theorem_frontier_payload(state):
            export_dag_frontier_snapshot(config, state)
        return checkpoint
    finally:
        shutil.rmtree(staged_checkpoint_root, ignore_errors=True)














def stuck_recovery_context_text(state: Dict[str, Any]) -> str:
    latest = latest_stuck_recovery_attempt(state)
    if not latest:
        return ""
    attempt_limit = stuck_recovery_attempt_limit(state)
    return textwrap.dedent(
        f"""\
        Active stuck-recovery guidance:
        - Attempt {latest.get('attempt', '?')} of {attempt_limit} for the current stuck episode.
        - Trigger cycle: {latest.get('trigger_cycle', '?')}
        - Diagnosis: {str(latest.get('diagnosis', '')).strip()}
        - Creative suggestion: {str(latest.get('creative_suggestion', '')).strip()}
        - Why it might work: {str(latest.get('why_this_might_work', '')).strip()}
        - Worker focus prompt: {str(latest.get('worker_prompt', '')).strip()}
        """
    ).strip()


def branch_context_text(state: Dict[str, Any]) -> str:
    context = state.get("branch_context")
    if not isinstance(context, dict):
        return ""
    return textwrap.dedent(
        f"""\
        Active branch strategy:
        - Episode: {context.get('episode_id', '')}
        - Branch: {context.get('branch_name', '')}
        - Summary: {str(context.get('summary', '')).strip()}
        - Frontier anchor node: {str(context.get('frontier_anchor_node_id', '')).strip()}
        - Rewrite scope: {str(context.get('rewrite_scope', '')).strip()}
        - Branch worker prompt: {str(context.get('worker_prompt', '')).strip()}
        - Why this might eventually succeed: {str(context.get('why_this_might_eventually_succeed', '')).strip()}
        """
    ).strip()


def phase_context_text(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    goal_label = repo_prompt_label(config, provider, config.goal_file)
    tasks_label = repo_prompt_label(config, provider, config.repo_path / "TASKS.md")
    parts = [
        f"Current phase: {phase}",
        f"Sorry mode: {config.workflow.sorry_mode}",
        f"Goal file: {goal_label}",
        "Supervisor-managed files:",
        f"- `{tasks_label}` always exists and is shared with the supervisor.",
    ]
    if config.workflow.paper_tex_path is not None:
        parts.append(f"- Paper tex: `{repo_prompt_label(config, provider, config.workflow.paper_tex_path)}`")
    if phase_uses_paper_notes(phase):
        parts.append(
            f"- `{repo_prompt_label(config, provider, config.repo_path / 'PAPERNOTES.md')}` is where paper corrections and clarifications belong."
        )
    if phase_uses_plan(phase):
        parts.append(f"- `{repo_prompt_label(config, provider, config.repo_path / 'PLAN.md')}` is the durable formalization roadmap.")
    if phase_uses_statement_files(phase):
        definitions_label = repo_prompt_label(config, provider, config.repo_path / "PaperDefinitions.lean")
        theorems_label = repo_prompt_label(config, provider, config.repo_path / "PaperTheorems.lean")
        parts.append(f"- `{definitions_label}` and `{theorems_label}` are the target statement files.")
    parts.append(f"- Approved axioms file: `{repo_prompt_label(config, provider, config.workflow.approved_axioms_path)}`")
    if git_is_enabled(config):
        parts.append(
            f"- Git remote: `{config.git.remote_name}` -> `{config.git.remote_url}` on branch `{current_git_branch(config)}`."
        )
        parts.append(f"- Push command when you made progress: `{git_push_command(config)}`")
    parts.append(f"- Validation summary file: `{supervisor_prompt_label(config, provider, validation_summary_path(config))}`")
    latest_validation = state.get("last_validation")
    if latest_validation:
        parts.append("Latest supervisor validation summary:")
        parts.append(trim_text(json.dumps(latest_validation, indent=2, ensure_ascii=False), 12000))
    else:
        parts.append("Latest supervisor validation summary: none yet.")
    human_input_text = trim_text(read_text(config.workflow.human_input_path).strip(), 6000)
    if human_input_text:
        parts.append(f"Latest human input from `{repo_prompt_label(config, provider, config.workflow.human_input_path)}`:")
        parts.append(human_input_text)
    stuck_recovery_text = stuck_recovery_context_text(state)
    if stuck_recovery_text:
        parts.append(stuck_recovery_text)
    branch_text = branch_context_text(state)
    if branch_text:
        parts.append(branch_text)
    frontier_text = theorem_frontier_context_text(config, state, provider)
    if frontier_text:
        parts.append(frontier_text)
    approved = approved_axioms(config)
    parts.append(f"Approved axioms: {approved if approved else '[]'}")
    return "\n".join(parts)


def phase_worker_instructions(config: Config, phase: str, provider: str) -> str:
    paper_label = (
        repo_prompt_label(config, provider, config.workflow.paper_tex_path)
        if config.workflow.paper_tex_path
        else "the paper tex file"
    )
    tasks_label = repo_prompt_label(config, provider, config.repo_path / "TASKS.md")
    papernotes_label = repo_prompt_label(config, provider, config.repo_path / "PAPERNOTES.md")
    plan_label = repo_prompt_label(config, provider, config.repo_path / "PLAN.md")
    definitions_label = repo_prompt_label(config, provider, config.repo_path / "PaperDefinitions.lean")
    theorems_label = repo_prompt_label(config, provider, config.repo_path / "PaperTheorems.lean")
    if phase == "paper_check":
        return textwrap.dedent(
            f"""\
            Phase objective: carefully read `{paper_label}` and mathematically verify the paper's proofs.

            Requirements:
            - Maintain `{tasks_label}`.
            - Maintain `{papernotes_label}` with corrections, hidden assumptions, and proof clarifications.
            - Read the paper carefully enough to catch proof gaps or incorrect statements.
            - Report `STUCK` only if you find a genuine gap or incorrect statement, try to repair it seriously, and still cannot make the argument work.
            - Report `DONE` only when the whole paper has been checked and `{papernotes_label}` is up to date.
            """
        ).strip()
    if phase == "planning":
        return textwrap.dedent(
            f"""\
            Phase objective: create a high-level but comprehensive `{plan_label}` for formalizing the main results of `{paper_label}`.

            Requirements:
            - Maintain `{tasks_label}`.
            - Maintain `{papernotes_label}`.
            - Build `{plan_label}` around statement prerequisites, reusable definitions, mathlib imports, and plausible proof roadmaps.
            - Use `NEED_INPUT` for external results, proposed axioms, or formalization design choices that genuinely need a human decision.
            - Never introduce axioms unless they are explicitly approved by a human and listed in the approved axioms file.
            """
        ).strip()
    if phase == "theorem_stating":
        main_results_label = supervisor_prompt_label(config, provider, paper_main_results_manifest_path(config))
        return textwrap.dedent(
            f"""\
            Phase objective: create Lean files that state the paper's definitions and theorems as close to `{paper_label}` as possible.

            Requirements:
            - Maintain `{tasks_label}`, `{papernotes_label}`, and `{plan_label}`.
            - Create or update `{definitions_label}` and `{theorems_label}`.
            - Write a machine-readable coarse paper-DAG manifest to `{main_results_label}` for the theorem-frontier seeding step.
            - During theorem stating, keep Lean edits inside the statement-file cone: only `PaperDefinitions.lean` / `PaperTheorems.lean` files (including module-layout variants with those exact filenames) should change.
            - Keep the definitions and statements easy for a human to compare against the paper.
            - Make both files syntactically valid Lean.
            - Do not introduce unapproved axioms.
            - If `{main_results_label}` does not exist yet, start from the supervisor-written stub and replace every placeholder field.
            - The manifest must describe the initial coarse theorem-frontier DAG extracted from the paper's proof spine, using exact natural-language statements, exact Lean statements, exact anchors, and only paper-facing / paper-faithful nodes.
            - For every seeded node, `lean_anchor` must name the exact source declaration for that node's statement.
            - For every seeded node, `lean_statement` must be an exact copy of the declaration text at `lean_anchor`, not a paraphrase, summary, or normalized variant.
            - The supervisor will mechanically check those statement anchors before theorem stating may advance.
            - Every seeded node must carry a complete publication-grade paper-derived `natural_language_proof` from its current children.
            - Those proofs should be at least as detailed as the paper's corresponding argument, and often more detailed because they must stand on their own as local proof witnesses for the DAG.
            - Do not use placeholder or sketch prose such as "leaf of the DAG", "direct target", "formalizes", "packages", "routine", or "by the paper" in place of an actual proof.
            - Copying sections of the paper verbatim or near-verbatim is completely acceptable when that text meets these locality and rigor requirements.
            - Keep every proof local to the declared child set: if a proof relies on another named paper lemma/case, represent that dependency as a child node instead of hiding it in prose.
            - The manifest must also choose `initial_active_node_id`, the first theorem node that proof formalization should start on.
            - Choose `initial_active_node_id` for leverage, not convenience: prefer a lower or structurally doubtful node whose clarification is most likely to force upstream refactors/restatements if the current route is wrong, rather than a routine top-level wrapper.
            - The manifest must be a JSON object with exactly these top-level keys:
              `phase`, `nodes`, `edges`, `initial_active_node_id`.
            - Every entry in `nodes` must be an exact theorem-frontier node object for a paper-facing result, lemma, proposition, or faithful reformulation, with kind `paper` or `paper_faithful_reformulation`.
            - `DONE` means the statement files are in place and ready for reviewer comparison against the paper.
            """
        ).strip()
    if is_style_cleanup_phase(phase):
        return textwrap.dedent(
            f"""\
            Phase objective: PROOF COMPLETE - style cleanup.

            Requirements:
            - Treat the proofs as complete already; every burst must end with a fully buildable proof state.
            - Maintain `{tasks_label}` and `{plan_label}`.
            - Focus on warning cleanup, proof/style cleanup, and moderate refactors that improve reuse or readability.
            - Keep `{definitions_label}` and `{theorems_label}` paper-facing and stable.
            - Do not take speculative risks. If a cleanup attempt stops being clearly worthwhile, report `STUCK`.
            - `DONE` means there is no clearly worthwhile remaining cleanup and the polished proof state should be kept as the final result.
            """
        ).strip()
    sorry_policy = (
        f"Default sorry policy: do not move on with extra sorrys anywhere outside `{theorems_label}`."
        if config.workflow.sorry_mode == "default"
        else "Sorrys-allowed mode: temporary extra sorrys are allowed, but you must drive the count down and remove them all by the end."
    )
    return textwrap.dedent(
        f"""\
        Phase objective: prove the target statements presented in `{theorems_label}`.

        Requirements:
        - Maintain `{tasks_label}` and `{plan_label}`.
        - Keep `{definitions_label}` and `{theorems_label}` as the paper-facing interface for definitions and theorem statements.
        - Prefer reusable lemmas, technical definitions, and proof infrastructure in separate support files when that yields a cleaner project structure.
        - It is fine for proofs in `{theorems_label}` to be short wrappers around results proved elsewhere in the repo.
        - Work toward zero sorrys and no unapproved axioms.
        - Keep the proof frontier concrete in `{tasks_label}`.
        """
    ).strip() + "\n- " + sorry_policy + "\n- `DONE` means the full workflow is complete."


def git_worker_instructions(config: Config) -> str:
    if not git_is_enabled(config):
        return ""
    return textwrap.dedent(
        f"""\
        Git requirements:
        - This repo is configured with remote `{config.git.remote_name}` -> `{config.git.remote_url}`.
        - Do not create git commits and do not push from the worker account.
        - Leave the repo edits in place; after the supervisor validates the cycle, it will stage, commit, and push the accepted state itself.
        - Use `summary_of_changes` in your worker handoff to describe the commit-worthy progress clearly.
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
        context_path = supervisor_prompt_label(
            config,
            provider,
            role_scope_dir(config, "gemini", "worker") / "GEMINI.md",
        )
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
    return ""


def refresh_validation_git_summary(config: Config, validation_summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(validation_summary, dict) or not git_is_enabled(config):
        return validation_summary
    git_summary = git_validation_summary(config)
    validation_summary["git"] = git_summary
    validation_summary["git_ok"] = bool(
        git_summary.get("enabled")
        and git_summary.get("repo_ok")
        and git_summary.get("worktree_clean")
        and git_summary.get("remote_matches_config")
    )
    validation_summary["head"] = git_summary.get("head")
    return validation_summary


def build_supervisor_cycle_commit_message(
    phase: str,
    cycle: int,
    decision: Dict[str, Any],
    *,
    worker_handoff: Optional[Dict[str, Any]] = None,
    frontier_review: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    decision_value = str(decision.get("decision") or "").strip().upper() or "CONTINUE"
    active_node_id = ""
    assessed_action = ""
    frontier_outcome = ""
    if isinstance(frontier_review, dict):
        active_node_id = normalize_frontier_text(frontier_review.get("active_node_id"))
        assessed_action = normalize_frontier_text(frontier_review.get("assessed_action"))
        frontier_outcome = normalize_frontier_text(frontier_review.get("outcome"))
    subject_parts = [f"supervisor: {phase} cycle {cycle}"]
    if assessed_action:
        subject_parts.append(assessed_action.lower())
    elif decision_value:
        subject_parts.append(decision_value.lower())
    if active_node_id:
        subject_parts.append(active_node_id)
    subject = trim_text(" ".join(subject_parts), 120)

    summary_of_changes = ""
    current_frontier = ""
    likely_next_step = ""
    if isinstance(worker_handoff, dict):
        summary_of_changes = str(worker_handoff.get("summary_of_changes") or "").strip()
        current_frontier = str(worker_handoff.get("current_frontier") or "").strip()
        likely_next_step = str(worker_handoff.get("likely_next_step") or "").strip()
    lines = [
        f"phase: {phase}",
        f"cycle: {cycle}",
        f"decision: {decision_value}",
    ]
    if assessed_action:
        lines.append(f"frontier_action: {assessed_action}")
    if frontier_outcome:
        lines.append(f"frontier_outcome: {frontier_outcome}")
    if active_node_id:
        lines.append(f"active_node: {active_node_id}")
    if summary_of_changes:
        lines.extend(["", "Worker summary:", summary_of_changes])
    reason = str(decision.get("reason") or "").strip()
    if reason:
        lines.extend(["", "Reviewer/supervisor reason:", reason])
    if current_frontier:
        lines.extend(["", "Current frontier:", current_frontier])
    if likely_next_step:
        lines.extend(["", "Likely next step:", likely_next_step])
    return subject, "\n".join(lines).strip()


def commit_and_push_validated_cycle(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    decision: Dict[str, Any],
    validation_summary: Dict[str, Any],
    *,
    frontier_review: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not git_is_enabled(config):
        return None
    ensure_git_repository(config)
    worker_handoff = state.get("last_worker_handoff") if isinstance(state.get("last_worker_handoff"), dict) else None
    current_branch = current_git_branch(config)
    use_force_with_lease = git_force_with_lease_required(state, branch=current_branch)
    subject, body = build_supervisor_cycle_commit_message(
        phase,
        cycle,
        decision,
        worker_handoff=worker_handoff,
        frontier_review=frontier_review,
    )
    status_before = git_output(config, ["status", "--short"]).strip()
    committed = False
    commit_head: Optional[str] = None
    if status_before:
        ensure_git_command_ok(config, ["add", "-A"])
        staged = git_output(config, ["diff", "--cached", "--name-only"]).strip()
        if staged:
            ensure_git_command_ok(config, ["commit", "-m", subject, "-m", body])
            committed = True
            commit_head = current_git_head(config)
    push_args = ["push", config.git.remote_name, f"HEAD:{current_branch}"]
    if use_force_with_lease:
        push_args = ["push", "--force-with-lease", config.git.remote_name, f"HEAD:{current_branch}"]
    push_output = ensure_git_command_ok(config, push_args)
    clear_git_force_with_lease_required(state)
    refresh_validation_git_summary(config, validation_summary)
    state["last_validation"] = validation_summary
    result = {
        "cycle": cycle,
        "phase": phase,
        "committed": committed,
        "head": current_git_head(config),
        "subject": subject,
        "push_mode": "force-with-lease" if use_force_with_lease else "fast-forward",
        "push_output": trim_text(push_output, 4000),
    }
    if commit_head:
        result["commit_head"] = commit_head
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="git_sync",
        actor="supervisor",
        target="repo",
        content=result,
        content_type="json",
        summary=f"Supervisor git sync for cycle {cycle}",
    )
    return result


def prompt_notes_block(title: str, note: str) -> str:
    cleaned = str(note).strip()
    if not cleaned:
        return ""
    return textwrap.dedent(
        f"""\
        {title}:
        {cleaned}
        """
    ).strip()


def normalize_saved_reviewer_next_prompt(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    replacements = {
        " together with their `repo/Twobites/` mirrors": "",
        " together with their `repo/Twobites/` mirror": "",
        " mirrored by `repo/Twobites/PaperDefinitions.lean` and `repo/Twobites/PaperTheorems.lean`": "",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)
    return " ".join(cleaned.split())


def theorem_frontier_worker_instructions(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    if not theorem_frontier_enabled(config, phase):
        return ""
    cycle = int(state.get("cycle", 0) or 0)
    artifact_label = supervisor_prompt_label(config, provider, theorem_frontier_worker_update_path(config, cycle))
    payload = theorem_frontier_payload(state) or {}
    active_node_id = normalize_frontier_text(payload.get("active_node_id"))
    generated_statements_label = repo_prompt_label(config, provider, theorem_frontier_generated_statements_path(config))
    active_generated_proof_label = (
        repo_prompt_label(config, provider, theorem_frontier_generated_proof_path(config, active_node_id))
        if active_node_id
        else "repo/GeneratedFrontier/Proofs/<active-node>.lean"
    )
    worker_update_check_script = PROJECT_ROOT / "scripts" / "verify_theorem_frontier_worker_update.py"
    if config.source_path is not None:
        worker_update_check_command = (
            f"python3 {shlex.quote(str(worker_update_check_script))} "
            f"--config {shlex.quote(str(config.source_path))} "
            f"--worker-update {shlex.quote(repo_relative_path(config, theorem_frontier_worker_update_path(config, cycle)))}"
        )
    else:
        worker_update_check_command = (
            f"python3 {shlex.quote(str(worker_update_check_script))} "
            f"--repo {shlex.quote(str(config.repo_path))} "
            f"--worker-update {shlex.quote(repo_relative_path(config, theorem_frontier_worker_update_path(config, cycle)))}"
        )
    return textwrap.dedent(
        f"""\
        Theorem-frontier artifact requirements:
        - In addition to the normal worker handoff, write a theorem-frontier JSON to `{artifact_label}`.
        - That JSON must name exactly one active theorem node and one requested action: `CLOSE`, `REFACTOR`, `EXPAND`, or `REFUTE_REPLACE`.
        - The active node is the authoritative proof target for this burst.
        - `CLOSE` means prove the active node from its current children.
        - `REFACTOR` means keep the same active node and child interface, but reorganize proof/support code without changing the frontier.
        - A successful `CLOSE` records the local Lean implication from the current children; the node becomes globally closed later, once those current children are themselves closed.
        - Structural actions (`EXPAND` / `REFUTE_REPLACE`) still use each node's handwritten `lean_anchor` and `lean_statement`.
        - The supervisor generates an immutable frontier statements file at `{generated_statements_label}`.
        - The active node's generated proof file is `{active_generated_proof_label}`.
        - For `CLOSE`, edit only `{active_generated_proof_label}` and set `allowed_edit_paths` to exactly that one file.
        - For `REFACTOR`, keep the frontier/NL artifacts unchanged; you may edit `{active_generated_proof_label}` plus cone-local support `.lean` files, but do not edit `{generated_statements_label}` or any other generated proof file.
        - For `CLOSE` and `REFACTOR`, `lean_proof_anchor` is only optional provenance and is ignored by the deterministic supervisor check.
        - Before handing off any theorem-frontier update, run the exact supervisor worker-update validator yourself on the final artifact: `{worker_update_check_command}`.
        - That script checks the same worker-stage rules the supervisor uses: theorem-frontier JSON schema, allowed node kinds, structural constraints, cone/edit policy, and for `CLOSE` / `REFACTOR` the deterministic generated-frontier checks.
        - Run it only after your last Lean edits and final theorem-frontier JSON rewrite. If it fails, keep fixing and rerun it until it succeeds.
        - Only submit a theorem-frontier update if that script succeeds.
        - `EXPAND` means refine the active node by inserting new nodes between it and its current children only.
        - `REFUTE_REPLACE` means replace the active node's current decomposition with a different route.
        - Prefer the smallest rigorous structural refinement that is likely to be admitted this cycle. Do not batch many delicate new leaves into one `EXPAND` when a smaller admitted subset would move the frontier forward more reliably.
        - For `EXPAND`, it is acceptable if some newly proposed nodes remain too weak to admit this cycle. The supervisor may incrementally admit the paper-approved and NL-approved subset while leaving the active node open.
        - Every node in the DAG carries a complete publication-grade natural-language proof from its current children. Do not leave those proof texts implicit.
        - Write each node proof at least as detailed as the paper's corresponding argument, and often more detailed because the proof must be locally self-contained relative to the current children.
        - Do not submit placeholder or sketch prose such as "leaf of the DAG", "direct target", "formalizes", "packages", "routine", or "by the paper" in place of an actual proof.
        - Copying sections of the paper verbatim or near-verbatim is completely acceptable when that text meets these locality and rigor requirements.
        - Keep every proof local to the declared child set: do not rely on named paper lemmas/cases outside the current node and its children.
        - Work only inside the active cone: the active node, its current children, or mechanically necessary support directly tied to closing that node.
        - When you suggest `next_candidate_node_ids`, prefer nodes whose clarification would be maximally informative about the current proof route: usually lower nodes, or nodes whose local proof looks tricky or doubtful enough that progress there could force knock-on restatement/refactor work upstream.
        - If you propose a structural change, include the exact rewritten active node plus the new local nodes and structural edges. Do not leave them vague.
        - If `requested_action` is `CLOSE` or `REFACTOR`, set `active_node_after` to `null` and leave `proposed_nodes` / `proposed_edges` empty.
        - If `requested_action` is `EXPAND` or `REFUTE_REPLACE`, `active_node_after` must be the exact rewritten active node.

        Your theorem-frontier JSON must have exactly these top-level keys:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "active_node_id": "stable theorem node id",
          "active_node_after": null | {{ exact rewritten active-node object after this burst }},
          "requested_action": "CLOSE" | "REFACTOR" | "EXPAND" | "REFUTE_REPLACE",
          "lean_proof_anchor": "optional provenance note; leave empty for generated-frontier CLOSE/REFACTOR unless useful",
          "cone_scope": "what work is inside the active cone for this burst",
          "allowed_edit_paths": [] | ["repo-relative .lean files allowed inside that cone for this burst"],
          "result_summary": "what changed relative to the active node",
          "proposed_nodes": [{{ exact new node objects }}],
          "proposed_edges": [{{ "parent": "...", "child": "..." }}],
          "next_candidate_node_ids": ["node ids that could become the next active theorem node"],
          "structural_change_reason": "why a structural edit is needed, or empty if none"
        }}
        Use `allowed_edit_paths: []` only if the burst made no Lean file edits at all.
        Any Lean file changed outside `allowed_edit_paths` will fail theorem-frontier cone validation for the cycle.
        """
    ).strip()


def theorem_frontier_reviewer_instructions(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    if not theorem_frontier_enabled(config, phase):
        return ""
    cycle = int(state.get("cycle", 0) or 0)
    artifact_label = supervisor_prompt_label(config, provider, theorem_frontier_review_path(config, cycle))
    return textwrap.dedent(
        f"""\
        Theorem-frontier review requirements:
        - In addition to the normal reviewer decision, write a theorem-frontier review JSON to `{artifact_label}`.
        - Judge the cycle by theorem-frontier standards, not by build cleanliness alone.
        - Confirm whether the requested action really happened on the one active theorem node.
        - `CLOSE` and `REFACTOR` are now supervisor-deterministic actions and should not reach you; focus on structural actions only.
        - Count real progress here only when the burst refined/replaced the active node's local decomposition.
        - Do not accept an `EXPANDED` or `REFUTED_REPLACED` outcome if the rewritten node proof or newly admitted nodes lack complete natural-language proofs.
        - For `EXPAND`, do not treat admission as all-or-nothing. If the geometry is sound but only a subset of the proposed new nodes is ready, approve that subset through the verifier `approved_node_ids` / `approved_edges` and keep the overall outcome `STILL_OPEN`.
        - Use full `EXPANDED` only when the whole proposed local refinement is ready to enter the authoritative DAG now. Otherwise prefer a `STILL_OPEN` outcome with a rigorous approved subset.
        - Treat the overall proof-writing impression as a calibration signal: in a healthy proof-carrying DAG, the aggregate natural-language proof text for the active proof spine should usually feel at least as substantial as the corresponding paper argument, and often longer because each node proof must be locally self-contained.
        - If the node proofs feel much sketchier or shorter than the paper's local arguments, treat that as evidence that the DAG is too coarse or the proofs are not yet rigorous enough to admit.
        - Pay attention to whether the declared children really seem to be the route being used. If some children look unnecessary or the proof seems to rely on an unstated intermediate instead, require a refactor rather than admitting the node.
        - Reject any structural update whose node proofs still appeal to named paper lemmas/cases outside the declared local child set or use placeholder/sketch language instead of a real proof.
        - Do not penalize proofs for copying sections of the paper verbatim or near-verbatim when that wording is actually the best rigorous local proof for the declared children.
        - If the worker mostly added wrappers above the same blocker or drifted outside the active cone, use `NO_FRONTIER_PROGRESS`.
        - If cone purity is low, record that explicitly.
        - Use `next_active_node_id` to name the theorem node that should be active after this review. Use the current active node id if the same node stays active, or leave it empty only if there is no next active node yet.
        - When choosing `next_active_node_id`, prioritize information gain: prefer nodes where real progress is most likely to force knock-on refactor/restatement effects or reveal that the current route is unsound. This usually favors lower nodes, or nodes whose local proof looks tricky or doubtful, over routine wrapper nodes.

        Your theorem-frontier review JSON must have exactly these keys:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "active_node_id": "reviewed theorem node id",
          "assessed_action": "CLOSE" | "EXPAND" | "REFUTE_REPLACE",
          "blocker_cluster": "canonical blocker after review",
          "outcome": "CLOSED" | "EXPANDED" | "REFUTED_REPLACED" | "STILL_OPEN" | "NO_FRONTIER_PROGRESS",
          "next_active_node_id": "next active node id or current id",
          "cone_purity": "HIGH" | "MEDIUM" | "LOW",
          "open_hypotheses": ["remaining assumptions still blocking closure"],
          "justification": "brief theorem-frontier justification"
        }}
        The reviewer should identify authoritative nodes by id and let the supervisor use the canonical DAG statements.
        """
    ).strip()


def theorem_frontier_paper_verifier_instructions(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    if not theorem_frontier_full_enabled(config, phase):
        return ""
    cycle = int(state.get("cycle", 0) or 0)
    return textwrap.dedent(
        f"""\
        Paper-verifier structural-review requirements:
        - You are acting as the dedicated paper-verifier for theorem-frontier structural edits.
        - Review the proposed node/decomposition change only against the paper, `PAPERNOTES.md`, and already approved reformulations.
        - Trigger approval only for structural changes that are paper-exact, paper-faithful reformulations, conservative strengthenings, or explicit exploratory detours.
        - Reject any structural edit that is paper-incompatible, hides a necessary split, or silently changes the proof spine.
        - If you are approving a rewritten active node itself, include that active node id in `approved_node_ids` along with any newly admitted nodes.
        - For `EXPAND`, approve the rigorous paper-faithful subset even when the full worker proposal is not ready for atomic admission. The supervisor may use your approved subset incrementally while leaving the cycle `STILL_OPEN`.

        Your paper-verifier JSON must have exactly these keys:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "parent_node_id": "node whose subtree is changing",
          "change_kind": "EXPAND" | "REFUTE_REPLACE",
          "decision": "APPROVE" | "APPROVE_WITH_CAVEAT" | "REJECT",
          "classification": "paper_exact" | "paper_faithful_reformulation" | "conservative_strengthening" | "exploratory_detour" | "paper_incompatible",
          "approved_node_ids": ["node ids approved by this review"],
          "approved_edges": [{{ "parent": "...", "child": "..." }}],
          "justification": "paper-faithfulness justification",
          "caveat": "leave empty unless APPROVE_WITH_CAVEAT"
        }}
        """
    ).strip()


def theorem_frontier_nl_proof_verifier_instructions(config: Config, state: Dict[str, Any], phase: str, provider: str) -> str:
    if not theorem_frontier_full_enabled(config, phase):
        return ""
    cycle = int(state.get("cycle", 0) or 0)
    return textwrap.dedent(
        f"""\
        NL-proof-verifier requirements:
        - You are acting as the dedicated verifier of natural-language proofs for theorem-frontier structural admissions.
        - Do not judge paper-faithfulness here; the paper-verifier already handled that.
        - Judge only whether the natural-language proofs on the paper-approved rewritten active node and newly admitted nodes are complete, rigorous, and sufficient.
        - Use the paper's own level of detail as a floor, not a ceiling: these local node proofs should usually be at least as detailed as the corresponding paper argument, and often longer because they must be self-contained relative to the declared children.
        - Treat suspiciously short or high-level proofs as a warning sign that the decomposition is too coarse or the proof is still a sketch.
        - Approve only the subset of paper-approved nodes whose natural-language proofs are genuinely rigorous. Reject or withhold approval from anything with gaps, handwaving, missing case analysis, or placeholder/sketch language.
        - For `EXPAND`, use `approved_node_ids` to mark the rigorous subset even when some proposed leaves still need another cycle. The supervisor may incrementally admit that approved subset while keeping the overall cycle `STILL_OPEN`.
        - Copying sections of the paper verbatim or near-verbatim is completely acceptable when the resulting node proof is the right rigorous local proof from the declared children.
        - If you are approving a rewritten active node itself, include that active node id in `approved_node_ids` along with any newly admitted nodes.

        Your NL-proof-verifier JSON must have exactly these keys:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "parent_node_id": "node whose subtree is changing",
          "change_kind": "EXPAND" | "REFUTE_REPLACE",
          "decision": "APPROVE" | "APPROVE_WITH_CAVEAT" | "REJECT",
          "approved_node_ids": ["paper-approved node ids whose NL proofs are rigorous"],
          "justification": "brief rigor judgment",
          "caveat": "leave empty unless APPROVE_WITH_CAVEAT"
        }}
        Full structural admission into the authoritative DAG requires `decision = "APPROVE"` from this verifier. Use `APPROVE_WITH_CAVEAT` when only a subset is ready to admit or when the cycle should continue after admitting the rigorous subset.
        """
    ).strip()


def build_worker_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    is_initial: bool,
    *,
    policy: Optional[Policy] = None,
) -> str:
    cycle = int(state.get("cycle", 0) or 0)
    goal_text = read_text(config.goal_file).strip()
    last_review = state.get("last_review") or {}
    handoff_statuses = format_json_enum(phase_specific_worker_statuses(phase))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    review_guidance = ""
    if not is_initial:
        next_prompt = theorem_frontier_authoritative_next_prompt(
            state,
            fallback=normalize_saved_reviewer_next_prompt(last_review.get("next_prompt") or ""),
        )
        resolution_note = theorem_frontier_reviewer_resolution_note(state)
        resolution_line = f"- Supervisor note: {resolution_note}\n" if resolution_note else ""
        review_guidance = textwrap.dedent(
            f"""\
            Reviewer guidance:
            {resolution_line}\
            - Reason: {(last_review.get("reason") or "No reason supplied.").strip()}
            - Next prompt: {next_prompt or "Continue from the current frontier."}
            """
        )
    transition_blocker_notes = ""
    last_transition_error = state.get("last_transition_error")
    if not is_initial and isinstance(last_transition_error, dict):
        blocked_phase = str(last_transition_error.get("phase", "")).strip()
        if blocked_phase == phase:
            transition_blocker_notes = textwrap.dedent(
                f"""\
                Blocked phase transition:
                - A previous reviewer attempted to advance out of `{phase}`, but the supervisor blocked that transition.
                - Stay in `{phase}` and fix this blocker before attempting another phase advance.
                - Blocking reason: {str(last_transition_error.get('error', '')).strip()}
                """
            )
    stuck_recovery_notes = ""
    latest_recovery = latest_stuck_recovery_attempt(state)
    if latest_recovery:
        attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
        stuck_recovery_notes = textwrap.dedent(
            f"""\
            Supervisor stuck-recovery guidance:
            - This burst is recovery attempt {latest_recovery.get('attempt', '?')} of {attempt_limit} for the current stuck episode.
            - Focus prompt: {str(latest_recovery.get('worker_prompt', '')).strip()}
            - Creative suggestion: {str(latest_recovery.get('creative_suggestion', '')).strip()}
            """
        )
    close_validation_notes = ""
    if not is_initial:
        close_note = theorem_frontier_close_validation_note(state, phase)
        if close_note:
            close_validation_notes = textwrap.dedent(
                f"""\
                Supervisor CLOSE validation note:
                - {close_note}
                """
            )
    protocol_retry_notes = ""
    if not is_initial:
        protocol_retry = state.get("last_worker_protocol_error")
        if isinstance(protocol_retry, dict):
            retry_phase = str(protocol_retry.get("phase", "")).strip()
            retry_cycle = int(protocol_retry.get("cycle", 0) or 0)
            if retry_phase == phase and retry_cycle == cycle:
                protocol_retry_notes = textwrap.dedent(
                    f"""\
                    Supervisor worker-protocol correction:
                    - Your previous burst exited without the required worker handoff JSON.
                    - Fix the exact protocol error below before ending this burst.
                    - Error: {str(protocol_retry.get('error', '')).strip()}
                    """
                )
    provider_notes = provider_context_worker_instructions(config)
    git_notes = git_worker_instructions(config)
    frontier_notes = theorem_frontier_worker_instructions(config, state, phase, config.worker.provider)
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.worker,
    )
    worker_handoff_label = supervisor_prompt_label(config, config.worker.provider, worker_handoff_path(config, cycle))
    return textwrap.dedent(
        f"""\
        You are the main formalization worker.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.worker.provider)}

        {review_guidance}{transition_blocker_notes}{close_validation_notes}{protocol_retry_notes}{stuck_recovery_notes}{provider_notes}
        {phase_worker_instructions(config, phase, config.worker.provider)}
        {frontier_notes}
        {git_notes}
        {policy_notes}

        Before ending this turn:
        - write your handoff JSON to `{worker_handoff_label}`
        - also print the same JSON as the final thing in your terminal output

        Your handoff JSON must have exactly these keys:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
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
        main_results_label = supervisor_prompt_label(config, config.reviewer.provider, paper_main_results_manifest_path(config))
        text = textwrap.dedent(
            f"""\
            Decide whether the worker should continue theorem stating, advance to proof formalization, or stop.
            Compare `PaperDefinitions.lean` and `PaperTheorems.lean` against the paper and insist on changes if they do not correspond.
            Compare the coarse paper-DAG manifest `{main_results_label}` against the paper and the statement files.
            Require that it captures a real coarse proof spine from the paper, uses only paper / paper-faithful nodes, and chooses a reasonable initial active theorem node.
            Require that every seeded node's `lean_anchor` names a real source declaration and that the manifest's `lean_statement` text is an exact source match for that declaration.
            Reject theorem-only skeletons: every node proof must be a genuinely detailed local proof from the declared children, not a proof sketch or placeholder. Use the paper's own detail level as a floor and the overall amount of proof text as a sanity-check on whether the DAG is really proof-carrying. Copying sections of the paper verbatim or near-verbatim is completely acceptable when it meets that local rigor standard.
            Require syntactically valid Lean before advancing.
            """
        ).strip()
        git_note = git_reviewer_instructions(config)
        return text + ("\n" + git_note if git_note else "")
    if is_style_cleanup_phase(phase):
        text = textwrap.dedent(
            """\
            Decide whether cleanup should continue, stop as done, or stop because cleanup has stalled.
            This phase is optional polish, not mission-critical proof development.
            Require that every cycle remain fully buildable with no sorrys and no unapproved axioms.
            Prefer `DONE` once the remaining cleanup is marginal.
            Use `STUCK` when cleanup no longer seems worth the risk or effort; the supervisor will preserve the last good proof-complete commit and finish successfully.
            """
        ).strip()
        git_note = git_reviewer_instructions(config)
        return text + ("\n" + git_note if git_note else "")
    text = textwrap.dedent(
        """\
        Decide whether the worker should continue the proof phase, advance to proof-complete style cleanup, or stop as stuck.
        Use the supervisor validation summary for build status, sorry counts, and axiom enforcement.
        Keep `PaperDefinitions.lean` and `PaperTheorems.lean` paper-facing and easy to compare against the paper.
        If the worker is stuffing reusable infrastructure into those files when separate support files would be cleaner, require refactoring.
        """
    ).strip()
    git_note = git_reviewer_instructions(config)
    return text + ("\n" + git_note if git_note else "")


def build_theorem_frontier_paper_verifier_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_terminal_output: str,
    worker_handoff_text: str,
    worker_frontier_update: Dict[str, Any],
    is_initial: bool,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    recent_reviews = state.get("review_log", [])[-3:]
    paper_notes = trim_text(read_text(config.repo_path / "PAPERNOTES.md").strip(), 16000) or "(none)"
    frontier_payload = theorem_frontier_payload(state) or default_theorem_frontier_payload("full")
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    artifact_label = supervisor_prompt_label(config, config.reviewer.provider, theorem_frontier_paper_verifier_path(config, int(state.get("cycle", 0) or 0)))
    return textwrap.dedent(
        f"""\
        You are the paper-verifier for theorem-frontier structural edits.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Worker theorem-frontier JSON:
        {json.dumps(worker_frontier_update, indent=2, ensure_ascii=False)}

        Current authoritative theorem-frontier payload:
        {trim_text(json.dumps(frontier_payload, indent=2, ensure_ascii=False), 18000)}

        Worker handoff JSON:
        {worker_handoff_text}

        Recent reviewer decisions:
        {json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]"}

        Relevant paper notes from `repo/PAPERNOTES.md`:
        {paper_notes}

        Worker terminal output:
        {trim_text(worker_terminal_output, 18000)}

        {theorem_frontier_paper_verifier_instructions(config, state, phase, config.reviewer.provider)}

        Before ending this turn:
        - write your paper-verifier JSON to `{artifact_label}`
        - also print the same JSON as the final thing in your terminal output
        """
    ).strip()


def build_theorem_frontier_nl_proof_verifier_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_terminal_output: str,
    worker_handoff_text: str,
    worker_frontier_update: Dict[str, Any],
    paper_review: Dict[str, Any],
    is_initial: bool,
) -> str:
    goal_text = read_text(config.goal_file).strip()
    recent_reviews = state.get("review_log", [])[-3:]
    paper_notes = trim_text(read_text(config.repo_path / "PAPERNOTES.md").strip(), 16000) or "(none)"
    frontier_payload = theorem_frontier_payload(state) or default_theorem_frontier_payload("full")
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    artifact_label = supervisor_prompt_label(config, config.reviewer.provider, theorem_frontier_nl_proof_verifier_path(config, int(state.get("cycle", 0) or 0)))
    return textwrap.dedent(
        f"""\
        You are the NL-proof verifier for theorem-frontier structural admissions.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Worker theorem-frontier JSON:
        {json.dumps(worker_frontier_update, indent=2, ensure_ascii=False)}

        Paper-verifier structural review:
        {json.dumps(paper_review, indent=2, ensure_ascii=False)}

        Current authoritative theorem-frontier payload:
        {trim_text(json.dumps(frontier_payload, indent=2, ensure_ascii=False), 18000)}

        Worker handoff JSON:
        {worker_handoff_text}

        Recent reviewer decisions:
        {json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]"}

        Relevant paper notes from `repo/PAPERNOTES.md`:
        {paper_notes}

        Worker terminal output:
        {trim_text(worker_terminal_output, 18000)}

        {theorem_frontier_nl_proof_verifier_instructions(config, state, phase, config.reviewer.provider)}

        Before ending this turn:
        - write your NL-proof-verifier JSON to `{artifact_label}`
        - also print the same JSON as the final thing in your terminal output
        """
    ).strip()


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
    policy: Optional[Policy] = None,
) -> str:
    cycle = int(state.get("cycle", 0) or 0)
    goal_text = read_text(config.goal_file).strip()
    recent_reviews = state.get("review_log", [])[-3:]
    decision_values = format_json_enum(phase_specific_reviewer_decisions(phase))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    terminal_section = (
        trim_text(worker_terminal_output, 18000)
        if include_terminal_output
        else "[omitted from the web transcript; raw terminal output is only kept in local logs]"
    )
    worker_handoff_label = supervisor_prompt_label(config, config.reviewer.provider, worker_handoff_path(config, cycle))
    frontier_update_text = ""
    if theorem_frontier_enabled(config, phase):
        frontier_update = state.get("last_theorem_frontier_worker_update")
        frontier_update_label = supervisor_prompt_label(
            config,
            config.reviewer.provider,
            theorem_frontier_worker_update_path(config, cycle),
        )
        frontier_update_text = textwrap.dedent(
            f"""\

            Worker theorem-frontier JSON from `{frontier_update_label}`:
            {json.dumps(frontier_update, indent=2, ensure_ascii=False) if isinstance(frontier_update, dict) else "{}"}
            """
        )
    paper_verifier_text = ""
    if theorem_frontier_full_enabled(config, phase):
        paper_verifier = state.get("last_theorem_frontier_paper_review")
        paper_verifier_label = supervisor_prompt_label(
            config,
            config.reviewer.provider,
            theorem_frontier_paper_verifier_path(config, cycle),
        )
        paper_verifier_text = textwrap.dedent(
            f"""\

            Paper-verifier structural review from `{paper_verifier_label}`:
            {json.dumps(paper_verifier, indent=2, ensure_ascii=False) if isinstance(paper_verifier, dict) else "{}"}
            """
        )
    nl_proof_verifier_text = ""
    if theorem_frontier_full_enabled(config, phase):
        nl_proof_verifier = state.get("last_theorem_frontier_nl_proof_review")
        nl_proof_verifier_label = supervisor_prompt_label(
            config,
            config.reviewer.provider,
            theorem_frontier_nl_proof_verifier_path(config, cycle),
        )
        nl_proof_verifier_text = textwrap.dedent(
            f"""\n
            NL-proof verifier review from `{nl_proof_verifier_label}`:
            {json.dumps(nl_proof_verifier, indent=2, ensure_ascii=False) if isinstance(nl_proof_verifier, dict) else "{}"}
            """
        )
    validation_label = supervisor_prompt_label(config, config.reviewer.provider, validation_summary_path(config))
    review_decision_label = supervisor_prompt_label(config, config.reviewer.provider, reviewer_decision_path(config, cycle))
    frontier_review_notes = theorem_frontier_reviewer_instructions(config, state, phase, config.reviewer.provider)
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.reviewer,
    )
    return textwrap.dedent(
        f"""\
        You are the review agent supervising the worker.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Recent reviewer decisions:
        {json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]"}

        Worker handoff JSON from `{worker_handoff_label}`:
        {worker_handoff_text}
        {frontier_update_text}
        {paper_verifier_text}
        {nl_proof_verifier_text}

        Supervisor validation summary from `{validation_label}`:
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 16000)}

        Worker's latest terminal output:
        {terminal_section}

        {phase_reviewer_instructions(config, phase)}
        {frontier_review_notes}
        {policy_notes}

        Before ending this turn:
        - write your decision JSON to `{review_decision_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "decision": {decision_values},
          "confidence": 0.0,
          "reason": "brief reason",
          "next_prompt": "short prompt for the worker; empty only if there is no next worker burst"
        }}
        """
    ).strip()


def build_stuck_recovery_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_terminal_output: str,
    worker_handoff_text: str,
    validation_summary: Dict[str, Any],
    last_review: Dict[str, Any],
    is_initial: bool,
    *,
    include_terminal_output: bool = True,
    policy: Optional[Policy] = None,
) -> str:
    cycle = int(last_review.get("cycle", state.get("cycle", 0)) or 0)
    goal_text = read_text(config.goal_file).strip()
    attempts = stuck_recovery_attempts(state)
    attempt_number = len(attempts) + 1
    attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
    terminal_section = (
        trim_text(worker_terminal_output, 18000)
        if include_terminal_output
        else "[omitted from the web transcript; raw terminal output is only kept in local logs]"
    )
    prior_attempts = [
        {
            "attempt": attempt.get("attempt"),
            "diagnosis": attempt.get("diagnosis"),
            "creative_suggestion": attempt.get("creative_suggestion"),
            "why_this_might_work": attempt.get("why_this_might_work"),
            "worker_prompt": attempt.get("worker_prompt"),
        }
        for attempt in attempts
    ]
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    worker_handoff_label = supervisor_prompt_label(config, config.reviewer.provider, worker_handoff_path(config, cycle))
    validation_label = supervisor_prompt_label(config, config.reviewer.provider, validation_summary_path(config))
    stuck_recovery_label = supervisor_prompt_label(config, config.reviewer.provider, stuck_recovery_suggestion_path(config, cycle))
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.reviewer,
    )
    return textwrap.dedent(
        f"""\
        You are temporarily acting as the supervisor's stuck-recovery reviewer.

        {preface}

        The normal reviewer has already concluded that the current workflow is genuinely stuck.
        Your job is not to decide `STUCK` versus `CONTINUE`.
        Instead, review the blocker carefully and propose one creative but concrete recovery strategy for the worker to try next.

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Triggering stuck review:
        {json.dumps(last_review, indent=2, ensure_ascii=False)}

        Prior stuck-recovery attempts for this same stuck episode:
        {json.dumps(prior_attempts, indent=2, ensure_ascii=False) if prior_attempts else "[]"}

        Worker handoff JSON from `{worker_handoff_label}`:
        {worker_handoff_text}

        Supervisor validation summary from `{validation_label}`:
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 16000)}

        Worker's latest terminal output:
        {terminal_section}

        Requirements:
        - Propose a materially different strategy from any prior stuck-recovery attempts listed above.
        - Be creative, but keep the suggestion technically grounded in the actual blocker.
        - Prefer suggestions that could unblock the worker without human input, new axioms, or abandoning the paper-facing interface.
        - Focus on a concrete next experiment, refactor, alternative reduction, counterexample check, or route change the worker can actually try in the next burst.
        - If the best idea is an explicit route change, say so directly and explain why it is different from the failed route.
        {policy_notes}

        Before ending this turn:
        - write your recovery JSON to `{stuck_recovery_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "diagnosis": "brief diagnosis of the blocker",
          "creative_suggestion": "one creative but concrete recovery strategy",
          "why_this_might_work": "brief rationale",
          "worker_prompt": "a short direct prompt telling the worker exactly what to try next"
        }}

        This is recovery attempt {attempt_number} of {attempt_limit} for the current stuck episode.
        """
    ).strip()


def build_branch_strategy_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    worker_terminal_output: str,
    worker_handoff_text: str,
    validation_summary: Dict[str, Any],
    last_review: Dict[str, Any],
    is_initial: bool,
    *,
    include_terminal_output: bool = True,
    policy: Optional[Policy] = None,
) -> str:
    cycle = int(last_review.get("cycle", state.get("cycle", 0)) or 0)
    goal_text = read_text(config.goal_file).strip()
    recent_reviews = state.get("review_log", [])[-6:]
    strategy_limit = branch_strategy_limit(config, state)
    terminal_section = (
        trim_text(worker_terminal_output, 18000)
        if include_terminal_output
        else "[omitted from the web transcript; raw terminal output is only kept in local logs]"
    )
    worker_handoff_label = supervisor_prompt_label(config, config.reviewer.provider, worker_handoff_path(config, cycle))
    validation_label = supervisor_prompt_label(config, config.reviewer.provider, validation_summary_path(config))
    branch_strategy_label = supervisor_prompt_label(config, config.reviewer.provider, branch_strategy_artifact_path(config, cycle))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    parent_control_note = ""
    theorem_branch_note = ""
    active_frontier_anchor = ""
    if not branching_enabled(config) and can_propose_branch_replacement(state, config):
        parent_control_note = textwrap.dedent(
            f"""\
            This run is currently a leaf inside a parent-managed branch frontier.
            If you return `BRANCH`, you are proposing up to {strategy_limit} replacement child strategies for the parent supervisor to evaluate.
            The child branches will not be created immediately in this run; the parent supervisor will decide whether the current frontier should be replaced.
            """
        )
    if theorem_frontier_full_enabled(config, phase):
        frontier_summary = theorem_frontier_branch_summary(state)
        active_node_id = normalize_frontier_text(frontier_summary.get("active_node_id"))
        if active_node_id:
            active_frontier_anchor = active_node_id
            blocker_cluster = str(frontier_summary.get("blocker_cluster") or "").strip()
            theorem_branch_note = textwrap.dedent(
                f"""\
                Theorem-frontier branching rule:
                - Any branch proposal must be a competing replacement route for the active theorem node `{active_node_id or '(unset)'}`.
                - Do not propose branches that widen the frontier above or outside that node's subtree.
                - Branch only when there are genuinely competing next moves for this node: different close routes, materially different expansions, or a real refactor alternative.
                - Do not branch just to keep multiple wrapper-building or bookkeeping variants of the same blocker alive.
                - If the routes still share the same blocker cluster and unresolved hypothesis set, prefer `NO_BRANCH`.
                - Branching is most justified when escalation pressure is building or when there are clearly different ways to cut blocker cluster `{blocker_cluster or '(unset)'}`.
                """
            )
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.branching,
    )
    return textwrap.dedent(
        f"""\
        You are temporarily acting as the supervisor's branching strategist.

        {preface}

        Your job is to decide whether the current run should stay on one route or split into multiple branches with materially different strategies.
        A branch is justified only if there are genuinely different routes to try, such as continuing the current proof path versus a major rewrite or route change.
        Do not branch just because one path is difficult or because two branches would be superficially different.

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Latest reviewer decision:
        {json.dumps(last_review, indent=2, ensure_ascii=False)}

        Recent reviewer decisions:
        {json.dumps(recent_reviews, indent=2, ensure_ascii=False) if recent_reviews else "[]"}

        Worker handoff JSON from `{worker_handoff_label}`:
        {worker_handoff_text}

        Supervisor validation summary from `{validation_label}`:
        {trim_text(json.dumps(validation_summary, indent=2, ensure_ascii=False), 16000)}

        Worker's latest terminal output:
        {terminal_section}

        {parent_control_note}
        {theorem_branch_note}
        {policy_notes}

        Branching policy:
        - At most {strategy_limit} branches may run concurrently in this branch episode or replacement frontier.
        - Branches should be designed to answer the question: which route seems more likely to eventually succeed at formalizing the whole paper?
        - Do not prefer the route that is merely further along today if it appears structurally flawed.
        - Prefer branches whose strategies are materially different: e.g. continue current route, major rewrite, alternate theorem route, alternate abstraction.
        - In theorem-frontier mode, each strategy should represent a genuinely different way to close the anchored node or replace it paper-faithfully; superficial wrapper variants do not justify branching.
        - If no such strategic fork exists yet, return `NO_BRANCH`.

        Before ending this turn:
        - write your branch-strategy JSON to `{branch_strategy_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "branch_decision": "NO_BRANCH" | "BRANCH",
          "frontier_anchor_node_id": "{active_frontier_anchor}",
          "confidence": 0.0,
          "reason": "brief reason",
          "strategies": [
            {{
              "name": "short-branch-name",
              "summary": "one-sentence strategy summary",
              "worker_prompt": "direct branch-specific worker prompt",
              "why_this_might_eventually_succeed": "why this route could still formalize the whole paper",
              "rewrite_scope": "incremental" | "major"
            }}
          ]
        }}

        If `branch_decision` is `BRANCH`, include between 2 and {strategy_limit} strategies.
        If `branch_decision` is `NO_BRANCH`, return an empty `strategies` list.
        """
    ).strip()


def build_branch_selection_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    episode: Dict[str, Any],
    branch_snapshots: List[Dict[str, Any]],
    is_initial: bool,
    *,
    policy: Optional[Policy] = None,
) -> str:
    cycle = int(state.get("cycle", 0) or 0)
    goal_text = read_text(config.goal_file).strip()
    selection_label = supervisor_prompt_label(config, config.reviewer.provider, branch_selection_artifact_path(config, cycle))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    continue_count = branch_selection_continue_count(config, episode, policy)
    initial_budget = branch_review_budget(config, policy)
    question = str(
        episode.get(
            "selection_question",
            "Which branch seems more likely to eventually succeed at formalizing the whole paper?",
        )
    ).strip()
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.branching,
    )
    theorem_branch_note = ""
    active_frontier_anchor = ""
    if theorem_frontier_full_enabled(config, phase):
        frontier_summary = theorem_frontier_branch_summary(state)
        active_node_id = normalize_frontier_text(frontier_summary.get("active_node_id"))
        if active_node_id:
            active_frontier_anchor = active_node_id
            theorem_branch_note = (
                f"Active theorem-frontier branch point: node `{active_node_id or '(unset)'}`. "
                "Prefer the branch that most cleanly closes that subtree, strictly reduces the unresolved hypothesis set, "
                "and leaves the smallest residual cutset."
            )
    post_initial_guidance = ""
    if continue_count > 0:
        post_initial_guidance = textwrap.dedent(
            f"""\
            Additional guidance for this later checkpoint:
            - This branch episode is already past the initial {initial_budget}-review checkpoint.
            - Resource cost now matters more than before.
            - Do not keep a clearly less promising branch alive merely because it is still making local progress.
            - Prefer `SELECT_BRANCH` whenever one branch now looks meaningfully more likely to eventually formalize the whole paper.
            - Return `CONTINUE_BRANCHING` only when the branches still look genuinely close and it remains honestly hard to name a preferred branch.

            """
        )
    return textwrap.dedent(
        f"""\
        You are temporarily acting as the supervisor's branch selector.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Branch episode metadata:
        {json.dumps(episode, indent=2, ensure_ascii=False)}

        Current branch snapshots:
        {json.dumps(branch_snapshots, indent=2, ensure_ascii=False)}

        Decision question:
        {question}

        {policy_notes}
        {theorem_branch_note}
        {post_initial_guidance}

        Requirements:
        - Judge branches by their likelihood of eventually succeeding at formalizing the whole paper.
        - Do not default to the branch that is merely furthest along today.
        - Prefer the branch whose route appears structurally sound and paper-faithful, even if it is temporarily behind.
        - In theorem-frontier mode, compare branches by whether they are actually shrinking the anchored node's unresolved dependency set, blocker age, and escalation pressure.
        - Penalize branches that mainly add wrappers while preserving the same blocker cluster and open hypotheses.
        - Return `CONTINUE_BRANCHING` if the evidence is still too weak and the branches should keep running.
        - Return `SELECT_BRANCH` only if one branch is now clearly the better bet.

        Before ending this turn:
        - write your branch-selection JSON to `{selection_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "selection_decision": "CONTINUE_BRANCHING" | "SELECT_BRANCH",
          "frontier_anchor_node_id": "{active_frontier_anchor}",
          "confidence": 0.0,
          "reason": "brief reason",
          "selected_branch": "branch name or empty string"
        }}
        """
    ).strip()


def build_branch_replacement_prompt(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    episode: Dict[str, Any],
    branch_snapshots: List[Dict[str, Any]],
    proposal_snapshot: Dict[str, Any],
    is_initial: bool,
    *,
    policy: Optional[Policy] = None,
) -> str:
    cycle = int(state.get("cycle", 0) or 0)
    goal_text = read_text(config.goal_file).strip()
    replacement_label = supervisor_prompt_label(config, config.reviewer.provider, branch_replacement_artifact_path(config, cycle))
    preface = "This is the first burst for this role." if is_initial else "Continue from the current session state."
    proposal = proposal_snapshot.get("pending_branch_proposal") if isinstance(proposal_snapshot, dict) else {}
    threshold = branch_replacement_min_confidence(config, policy)
    policy_notes = prompt_notes_block(
        "Supervisor policy note",
        effective_policy(config, state, policy).prompt_notes.branching,
    )
    theorem_branch_note = ""
    if theorem_frontier_full_enabled(config, phase):
        anchor_id = normalize_frontier_text(episode.get("frontier_anchor_node_id"))
        blocker = str(episode.get("frontier_anchor_blocker_cluster") or "").strip()
        if anchor_id:
            theorem_branch_note = (
                f"The active frontier is anchored at theorem node `{anchor_id}`. "
                f"Only replace the current frontier if the proposal offers materially different ways to close or refactor that same subtree, not just new wrapper variants for blocker `{blocker or '(unset)'}`."
            )
    return textwrap.dedent(
        f"""\
        You are temporarily acting as the supervisor's branch-frontier selector.

        {preface}

        Global goal:
        {goal_text}

        {phase_context_text(config, state, phase, config.reviewer.provider)}

        Active branch episode metadata:
        {json.dumps(episode, indent=2, ensure_ascii=False)}

        Current active branch frontier:
        {json.dumps(branch_snapshots, indent=2, ensure_ascii=False)}

        Pending replacement proposal from branch `{proposal_snapshot.get("name", "")}`:
        {json.dumps(proposal, indent=2, ensure_ascii=False)}

        Decision question:
        Should the current branch frontier be replaced by selecting `{proposal_snapshot.get("name", "")}` as the winning route now,
        pruning the other active branches in this episode, and immediately branching that winning route into the proposed child strategies?

        {policy_notes}
        {theorem_branch_note}

        Requirements:
        - Judge routes by their likelihood of eventually succeeding at formalizing the whole paper.
        - This is a high-bar intervention. Return `REPLACE_WITH_PROPOSAL` only if the proposal is clearly stronger than continuing the current capped frontier.
        - The proposed child strategies must be materially different from each other.
        - The proposed child strategies must also be materially different from the surviving current frontier alternatives they would displace.
        - In theorem-frontier mode, the proposal should show a clearer plan for shrinking the anchored node's open hypotheses or refuting/replacing that node paper-faithfully.
        - Do not choose replacement merely because the proposal is newer or more exciting.
        - Prefer `KEEP_FRONTIER` if the evidence is mixed, if the proposal looks like branch churn, or if confidence is below {threshold:.1f}.
        - Return `REPLACE_WITH_PROPOSAL` only if you are confidently endorsing a full frontier replacement now.

        Before ending this turn:
        - write your branch-replacement JSON to `{replacement_label}`
        - also print the same JSON as the final thing in your terminal output

        Return exactly this JSON shape:
        {{
          "phase": "{phase}",
          "cycle": {cycle},
          "replacement_decision": "KEEP_FRONTIER" | "REPLACE_WITH_PROPOSAL",
          "confidence": 0.0,
          "reason": "brief reason"
        }}
        """
    ).strip()

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
    append_chat_event_chunk(config, event)

    meta = load_chat_meta(config)
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
    meta["markdown_files"] = sync_chat_markdown_files(config)
    if kind == "worker_handoff" and isinstance(content, dict):
        meta["last_worker_status"] = content.get("status")
    if kind == "reviewer_decision" and isinstance(content, dict):
        meta["last_reviewer_decision"] = content.get("decision")
    meta["branch_overview"] = branch_overview(state)
    JsonFile.dump(chat_repo_meta_path(config), meta)
    update_chat_manifest(config, meta)
    ensure_dag_site(config)
    export_dag_meta(config, state)
    return event


























def build_burst_script(
    adapter: ProviderAdapter,
    cycle: int,
    prompt_file: Path,
    start_file: Path,
    exit_file: Path,
    *,
    script_tag: Optional[str] = None,
) -> Path:
    scripts_dir = supervisor_scripts_dir(adapter.config)
    safe_script_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", script_tag).strip("-") if script_tag else None
    script_stem = f"{adapter.role}-{safe_script_tag}" if safe_script_tag else f"{adapter.role}-cycle-{cycle:04d}"
    script_path = scripts_dir / f"{script_stem}.sh"
    work_dir = adapter.work_dir()
    env_vars = adapter.burst_env()
    script_home = adapter.config.tmux.burst_home
    script_path_env = os.environ.get("PATH", "")
    git_config_path = supervisor_git_config_path(adapter.config)

    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        "umask 0002",
        f"START_FILE={shlex.quote(str(start_file))}",
        f"EXIT_FILE={shlex.quote(str(exit_file))}",
        f"PROMPT_FILE={shlex.quote(str(prompt_file))}",
        f"WORK_DIR={shlex.quote(str(work_dir))}",
        "cleanup() {",
        "  ec=$?",
        "  printf '%s\n' \"$ec\" > \"$EXIT_FILE\"",
        "  exit \"$ec\"",
        "}",
        "trap cleanup EXIT",
        f"export HOME={shlex.quote(str(script_home))}" if script_home is not None else "",
        f"export PATH={shlex.quote(script_path_env)}" if script_path_env else "",
        f"export GIT_CONFIG_GLOBAL={shlex.quote(str(git_config_path))}",
        "cd \"$WORK_DIR\"",
        "printf '%s\n' \"$(date -Is)\" > \"$START_FILE\"",
        "PROMPT_CONTENT=$(cat \"$PROMPT_FILE\")",
        f"echo '[agent-burst] role={adapter.role} provider={adapter.cfg.provider} cwd='\"$PWD\"",
        "echo '[agent-burst] start='$(date -Is)",
    ]
    for key, value in env_vars.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    lines.append("cmd=(")
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
    rendered_lines = [line for line in lines if line]
    script_path.write_text("\n".join(rendered_lines) + "\n", encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def _burst_launch_prefix(config: Config) -> List[str]:
    burst_user = config.tmux.burst_user
    if not burst_user:
        return []
    command = ["sudo", "-n", "-u", burst_user]
    if config.tmux.burst_group:
        command.extend(["-g", config.tmux.burst_group])
    return command


def write_log_header(path: Path, header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(header)


def burst_captured_output(log_path: Path, pane_capture: str) -> str:
    log_text = read_text(log_path)
    if log_text.strip():
        return log_text
    return pane_capture


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
    poll_callback: Optional[Callable[[], None]] = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    pane_exit_grace_seconds = 1.0
    while True:
        if path.exists():
            return
        if poll_callback is not None:
            poll_callback()
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


def find_live_tmux_burst_pane(session: str, window_name: str) -> Optional[Dict[str, str]]:
    proc = tmux_cmd(
        "list-panes",
        "-t",
        session,
        "-F",
        "#{window_id}\t#{window_name}\t#{pane_id}\t#{pane_dead}",
        check=False,
    )
    if proc.returncode != 0:
        return None
    matches: List[Dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        window_id, listed_window_name, pane_id, pane_dead = parts
        if listed_window_name != window_name or pane_dead != "0":
            continue
        matches.append({"window_id": window_id, "pane_id": pane_id})
    if len(matches) > 1:
        raise SupervisorError(
            f"Found multiple live tmux panes for {session}:{window_name}; refusing to resume ambiguously."
        )
    return matches[0] if matches else None


def wait_for_tmux_burst_completion(
    adapter: ProviderAdapter,
    *,
    pane_id: str,
    window_id: str,
    prompt_stem: str,
    artifact_path: Path,
    per_cycle_log: Path,
    latest_log: Path,
    start_file: Path,
    exit_file: Path,
    session: str,
    window_name: str,
) -> Dict[str, Any]:
    print(f"tmux_session={session} window={window_name} pane={pane_id}")
    print(f"Attach with: tmux attach -t {session}")
    captured_text = ""
    completed = False
    chat_markdown_refresher = ChatMarkdownRefresher(adapter.config)
    try:
        wait_for_path(
            start_file,
            pane_id,
            adapter.config.startup_timeout_seconds,
            role=adapter.role,
            state_name="startup marker",
            log_path=per_cycle_log,
            poll_callback=chat_markdown_refresher.maybe_refresh,
        )
        wait_for_path(
            exit_file,
            pane_id,
            adapter.config.burst_timeout_seconds,
            role=adapter.role,
            state_name="exit marker",
            log_path=per_cycle_log,
            poll_callback=chat_markdown_refresher.maybe_refresh,
        )
        completed = True
    finally:
        time.sleep(0.3)
        chat_markdown_refresher.maybe_refresh(force=True)
        capture = tmux_cmd("capture-pane", "-p", "-t", pane_id, "-S", "-2000", check=False)
        pane_capture = capture.stdout if capture.returncode == 0 else ""
        captured_text = burst_captured_output(per_cycle_log, pane_capture)
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


class ChatMarkdownRefresher:
    def __init__(self, config: Config, *, interval_seconds: float = 2.0):
        self.config = config
        self.interval_seconds = interval_seconds
        self.next_refresh_at = 0.0
        self.last_warning: Optional[str] = None

    def maybe_refresh(self, *, force: bool = False) -> None:
        if not chat_repo_meta_path(self.config).exists():
            return
        now = time.monotonic()
        if not force and now < self.next_refresh_at:
            return
        try:
            refresh_chat_markdown_metadata(self.config, update_manifest=False)
            refresh_chat_codex_budget_status(self.config)
            refresh_dag_codex_budget_status(self.config)
        except Exception as exc:
            message = str(exc)
            if message != self.last_warning:
                print(f"[chat-export] warning: could not refresh chat exports: {message}", file=sys.stderr)
                self.last_warning = message
            self.next_refresh_at = now + self.interval_seconds
            return
        self.last_warning = None
        self.next_refresh_at = now + self.interval_seconds


def launch_tmux_burst(
    adapter: ProviderAdapter,
    cycle: int,
    prompt: str,
    *,
    state: Optional[Dict[str, Any]] = None,
    phase: Optional[str] = None,
    artifact_path: Optional[Path] = None,
    artifact_name: Optional[str] = None,
    clear_paths: Optional[Sequence[Path]] = None,
    burst_tag: Optional[str] = None,
    reuse_existing_window: bool = False,
) -> Dict[str, Any]:
    normalize_burst_user_codex_permissions(adapter.config)
    state_dir = adapter.config.state_dir
    prompts_dir = supervisor_prompts_dir(adapter.config)
    logs_dir = state_dir / "logs"
    runtime_dir = supervisor_runtime_markers_dir(adapter.config)
    safe_burst_tag = re.sub(r"[^A-Za-z0-9._-]+", "-", burst_tag).strip("-") if burst_tag else None
    prompt_stem = f"{adapter.role}-{safe_burst_tag}" if safe_burst_tag else f"{adapter.role}-cycle-{cycle:04d}"
    prompt_file = prompts_dir / f"{prompt_stem}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    prompt_file.chmod(0o644)

    if artifact_path is not None:
        artifact_path = Path(artifact_path)
    elif artifact_name is not None:
        artifact_path = state_dir / artifact_name
    elif adapter.role == "worker":
        artifact_path = worker_handoff_path(adapter.config, cycle)
    else:
        artifact_path = reviewer_decision_path(adapter.config, cycle)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    start_file = runtime_dir / f"{prompt_stem}.started"
    exit_file = runtime_dir / f"{prompt_stem}.exit"

    per_cycle_log = logs_dir / f"{prompt_stem}.ansi.log"
    aggregate_log = logs_dir / f"{adapter.role}.all.ansi.log"
    latest_log = logs_dir / f"{adapter.role}.latest.ansi.log"
    session = adapter.config.tmux.session_name
    window_name = f"{adapter.role}-{cycle:04d}" if safe_burst_tag is None else f"{adapter.role}-{safe_burst_tag}"
    usage_start = _provider_usage_snapshot(adapter)

    if reuse_existing_window:
        existing = find_live_tmux_burst_pane(session, window_name)
        if existing is not None:
            result = wait_for_tmux_burst_completion(
                adapter,
                pane_id=existing["pane_id"],
                window_id=existing["window_id"],
                prompt_stem=prompt_stem,
                artifact_path=artifact_path,
                per_cycle_log=per_cycle_log,
                latest_log=latest_log,
                start_file=start_file,
                exit_file=exit_file,
                session=session,
                window_name=window_name,
            )
            usage_end = _provider_usage_snapshot(adapter)
            result["usage"] = {
                "start": usage_start,
                "end": usage_end,
                "delta": _provider_usage_delta(usage_start, usage_end),
            }
            result["burst_tag"] = safe_burst_tag
            return result

    if state is not None and phase is not None and adapter.cfg.provider == "codex":
        wait_for_codex_weekly_budget_if_needed(
            adapter.config,
            state,
            phase=phase,
            stage_label=f"{adapter.role} burst",
        )

    paths_to_clear = (
        list(clear_paths)
        if clear_paths is not None
        else role_cycle_artifact_paths(adapter.config, adapter.role, cycle, main_artifact_path=artifact_path)
    )
    clear_supervisor_artifacts(adapter.config, *paths_to_clear)
    start_file.unlink(missing_ok=True)  # type: ignore[arg-type]
    exit_file.unlink(missing_ok=True)  # type: ignore[arg-type]

    script_path = build_burst_script(adapter, cycle, prompt_file, start_file, exit_file, script_tag=safe_burst_tag)

    header = (
        f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} | role={adapter.role} provider={adapter.cfg.provider} "
        f"scope={adapter.scope_dir()} =====\n$ {script_path}\n\n"
    )
    write_log_header(per_cycle_log, header)
    write_log_header(aggregate_log, header)

    proc = tmux_cmd("new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}", "-t", session, "-n", window_name)
    window_id, pane_id = proc.stdout.strip().split()

    tmux_cmd("set-window-option", "-t", window_id, "remain-on-exit", "on")
    pipe_inner_cmd = (
        f"cat | tee -a {shlex.quote(str(aggregate_log))} >> {shlex.quote(str(per_cycle_log))}"
    )
    pipe_cmd = shlex.join(["bash", "-lc", pipe_inner_cmd])
    tmux_cmd("pipe-pane", "-o", "-t", pane_id, pipe_cmd)
    launch_parts = _burst_launch_prefix(adapter.config) + [str(script_path)]
    launch_cmd = f"{shlex.join(launch_parts)}; exit"
    tmux_cmd("send-keys", "-t", pane_id, launch_cmd, "C-m")
    tmux_cmd("select-window", "-t", window_id)

    result = wait_for_tmux_burst_completion(
        adapter,
        pane_id=pane_id,
        window_id=window_id,
        prompt_stem=prompt_stem,
        artifact_path=artifact_path,
        per_cycle_log=per_cycle_log,
        latest_log=latest_log,
        start_file=start_file,
        exit_file=exit_file,
        session=session,
        window_name=window_name,
    )
    usage_end = _provider_usage_snapshot(adapter)
    result["usage"] = {
        "start": usage_start,
        "end": usage_end,
        "delta": _provider_usage_delta(usage_start, usage_end),
    }
    result["burst_tag"] = safe_burst_tag
    return result


def role_cycle_artifact_paths(
    config: Config,
    role: str,
    cycle: int,
    *,
    main_artifact_path: Optional[Path] = None,
) -> List[Path]:
    paths: List[Path] = []
    if main_artifact_path is not None:
        paths.extend([main_artifact_path])
    if role == "worker":
        paths.extend(
            [
                worker_handoff_path(config),
                theorem_frontier_worker_update_path(config, cycle),
                theorem_frontier_worker_update_path(config),
            ]
        )
        return paths
    if role == "reviewer":
        paths.extend(
            [
                reviewer_decision_path(config),
                theorem_frontier_review_path(config, cycle),
                theorem_frontier_review_path(config),
                branch_strategy_artifact_path(config, cycle),
                branch_strategy_artifact_path(config),
                branch_selection_artifact_path(config, cycle),
                branch_selection_artifact_path(config),
                branch_replacement_artifact_path(config, cycle),
                branch_replacement_artifact_path(config),
            ]
        )
        return paths
    if role == "paper_verifier":
        paths.extend(
            [
                theorem_frontier_paper_verifier_path(config, cycle),
                theorem_frontier_paper_verifier_path(config),
            ]
        )
        return paths
    if role == "nl_proof_verifier":
        paths.extend(
            [
                theorem_frontier_nl_proof_verifier_path(config, cycle),
                theorem_frontier_nl_proof_verifier_path(config),
            ]
        )
        return paths
    return paths


def clear_supervisor_artifacts(config: Config, *paths: Path) -> None:
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        path.unlink(missing_ok=True)  # type: ignore[arg-type]
        for legacy_path in legacy_supervisor_artifact_paths(config, path):
            legacy_path.unlink(missing_ok=True)  # type: ignore[arg-type]


def launch_tmux_burst_with_retries(
    adapter: ProviderAdapter,
    cycle: int,
    prompt: str,
    *,
    state: Optional[Dict[str, Any]] = None,
    phase: Optional[str] = None,
    stage_label: str,
    artifact_path: Optional[Path] = None,
    artifact_name: Optional[str] = None,
    clear_paths: Optional[Sequence[Path]] = None,
    burst_tag: Optional[str] = None,
    policy: Optional[Policy] = None,
    reuse_existing_window: bool = False,
) -> Dict[str, Any]:
    retry_delays = agent_retry_delays_seconds(adapter.config, policy)
    max_attempts = len(retry_delays) + 1
    attempt = 1
    while True:
        run = launch_tmux_burst(
            adapter,
            cycle,
            prompt,
            state=state,
            phase=phase,
            artifact_path=artifact_path,
            artifact_name=artifact_name,
            clear_paths=clear_paths,
            burst_tag=burst_tag,
            reuse_existing_window=reuse_existing_window and attempt == 1,
        )
        if run["exit_code"] == 0:
            return run

        if gemini_should_fallback_on_run(adapter, run):
            fallback_adapter = gemini_fallback_adapter(adapter)
            fallback_model = str(fallback_adapter.cfg.model or "").strip()
            primary_model = str(adapter.cfg.model or "").strip() or "(unspecified)"
            print(
                f"{stage_label.capitalize()} hit a Gemini rate-limit/capacity failure on model {primary_model}. "
                f"Retrying the same burst immediately with fallback model {fallback_model}. See {run['per_cycle_log']}"
            )
            fallback_tag = f"{burst_tag}-gemini-fallback" if burst_tag else f"cycle-{cycle:04d}-gemini-fallback"
            fallback_run = launch_tmux_burst(
                fallback_adapter,
                cycle,
                prompt,
                state=state,
                phase=phase,
                artifact_name=artifact_name,
                burst_tag=fallback_tag,
                reuse_existing_window=False,
            )
            if fallback_run["exit_code"] == 0:
                return fallback_run
            run = fallback_run

        if burst_hit_budget_error(run):
            print(
                f"{stage_label.capitalize()} hit a budget/rate-limit/capacity error. "
                f"Retrying the same burst in {BUDGET_ERROR_RETRY_DELAY_SECONDS // 60} minute(s). "
                f"See {run['per_cycle_log']}"
            )
            time.sleep(BUDGET_ERROR_RETRY_DELAY_SECONDS)
            continue

        if attempt > len(retry_delays):
            raise SupervisorError(
                f"{stage_label.capitalize()} process exited with code {run['exit_code']} after "
                f"{len(retry_delays)} retry attempts. See {run['per_cycle_log']}"
            )

        delay_seconds = retry_delays[attempt - 1]
        if burst_hit_productive_local_failure(run):
            delay_seconds = min(delay_seconds, PRODUCTIVE_LOCAL_FAILURE_MAX_RETRY_DELAY_SECONDS)
            delay_minutes = int(delay_seconds // 60)
            print(
                f"{stage_label.capitalize()} ended after a productive local proof/build failure. "
                f"Retrying the same burst in {delay_minutes} minute(s). See {run['per_cycle_log']}"
            )
        else:
            delay_hours = int(delay_seconds // 3600)
            print(
                f"{stage_label.capitalize()} process exited with code {run['exit_code']}. "
                f"Retrying the same burst in {delay_hours} hour(s). See {run['per_cycle_log']}"
            )
        time.sleep(delay_seconds)
        attempt += 1

    raise AssertionError("unreachable")


DEFAULT_VALIDATION_RETRY_LIMIT = 1


def run_burst_with_validation(
    adapter: ProviderAdapter,
    cycle: int,
    prompt: str,
    *,
    config: Optional[Config] = None,
    state: Optional[Dict[str, Any]] = None,
    phase: Optional[str] = None,
    stage_label: str,
    policy: Optional[Policy] = None,
    artifact_name: Optional[str] = None,
    artifact_path: Optional[Path] = None,
    clear_paths: Optional[Sequence[Path]] = None,
    burst_tag: Optional[str] = None,
    reuse_existing_window: bool = False,
    validate: Callable[[Dict[str, Any]], Any],
    validation_retry_limit: int = DEFAULT_VALIDATION_RETRY_LIMIT,
) -> Tuple[Dict[str, Any], Any]:
    """Launch a burst, then validate the result.

    If validation raises SupervisorError, re-launch the agent with a correction
    prompt appended, up to *validation_retry_limit* times.  Returns
    ``(burst_run_dict, validated_result)``.
    """
    current_prompt = prompt
    last_error: Optional[str] = None
    for attempt in range(1, validation_retry_limit + 2):
        run = launch_tmux_burst_with_retries(
            adapter,
            cycle,
            current_prompt,
            state=state,
            phase=phase,
            stage_label=stage_label,
            artifact_path=artifact_path,
            artifact_name=artifact_name,
            clear_paths=clear_paths,
            burst_tag=burst_tag,
            policy=policy,
            reuse_existing_window=reuse_existing_window and attempt == 1,
        )
        if config is not None and state is not None and phase is not None:
            record_agent_burst_usage(
                config,
                state,
                cycle=cycle,
                phase=phase,
                adapter=adapter,
                stage_label=stage_label,
                attempt=attempt,
                run=run,
            )
        try:
            result = validate(run)
            return run, result
        except SupervisorError as exc:
            last_error = str(exc)
            if isinstance(exc, ValidationRoutingError) and exc.retry_role != adapter.role:
                raise
            if attempt > validation_retry_limit:
                raise
            if config is not None:
                log_supervisor_warning(
                    config,
                    cycle=cycle,
                    phase=phase or "",
                    category="validation_retry",
                    message=f"{stage_label} attempt {attempt}/{validation_retry_limit + 1}: {last_error}",
                    detail={"artifact_path": str(run.get("artifact_path", ""))},
                )
            else:
                print(
                    f"WARNING [validation_retry] {stage_label} attempt {attempt}/{validation_retry_limit + 1}: "
                    f"{last_error}"
                )
            print(f"Re-launching {stage_label} with correction prompt.")
            correction = (
                f"\n\n"
                f"IMPORTANT CORRECTION — your previous output failed the supervisor's "
                f"artifact validation with this error:\n\n"
                f"  {last_error}\n\n"
                f"Please fix this exact error. Rewrite the required artifact file(s) with "
                f"the correct schema and try again. All other instructions from the "
                f"original prompt still apply."
            )
            current_prompt = prompt + correction
    raise SupervisorError(last_error or "validation failed")


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


def normalize_required_keys(required_key: Optional[Union[str, Sequence[str]]]) -> List[str]:
    if required_key is None:
        return []
    if isinstance(required_key, str):
        return [required_key]
    return [str(key) for key in required_key]


def extract_json_object(text: str, required_key: Optional[Union[str, Sequence[str]]] = None) -> Dict[str, Any]:
    candidates = extract_json_objects(text)
    required_keys = normalize_required_keys(required_key)
    if required_keys:
        candidates = [candidate for candidate in candidates if all(key in candidate for key in required_keys)]
    if candidates:
        return candidates[-1]
    raise SupervisorError("Could not parse JSON object from captured text")


def load_json_artifact_with_fallback(
    path: Path,
    captured_text: str,
    required_key: Union[str, Sequence[str]],
    *,
    fallback_paths: Sequence[Path] = (),
) -> Dict[str, Any]:
    required_keys = normalize_required_keys(required_key)
    errors: List[str] = []
    for candidate in [path, *fallback_paths]:
        if not candidate.exists():
            continue
        try:
            data = parse_json_object_file(candidate)
            if all(key in data for key in required_keys):
                return data
            errors.append(f"Artifact missing required keys {required_keys!r}: {candidate}")
        except SupervisorError as exc:
            errors.append(str(exc))
    try:
        return extract_json_object(captured_text, required_key=required_keys)
    except SupervisorError as exc:
        errors.append(str(exc))
    raise SupervisorError(" | ".join(errors))


def artifact_fallback_paths(
    config: Config,
    primary_path: Path,
    *extra_paths: Path,
) -> List[Path]:
    seen: set[Path] = set()
    ordered: List[Path] = []
    candidate_groups: List[Path] = []
    for extra_path in extra_paths:
        candidate_groups.append(extra_path)
        candidate_groups.extend(legacy_supervisor_artifact_paths(config, extra_path))
    candidate_groups.extend(legacy_supervisor_artifact_paths(config, primary_path))
    for path in candidate_groups:
        resolved = path.resolve()
        if resolved == primary_path.resolve() or resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(path)
    return ordered


def persist_supervisor_artifact(payload: Dict[str, Any], primary_path: Path, mirror_path: Optional[Path] = None) -> None:
    primary_path.parent.mkdir(parents=True, exist_ok=True)
    JsonFile.dump(primary_path, payload)
    if mirror_path is not None:
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        JsonFile.dump(mirror_path, payload)


WORKER_HANDOFF_KEY_ALIASES: Dict[str, List[str]] = {
    "summary_of_changes": ["summary", "changes", "change_summary"],
    "current_frontier": ["frontier", "current_focus", "focus"],
    "likely_next_step": ["next_step", "next_steps", "next"],
    "input_request": ["input", "request"],
}


def _normalize_worker_handoff_keys(handoff: Dict[str, Any]) -> Dict[str, Any]:
    for canonical, aliases in WORKER_HANDOFF_KEY_ALIASES.items():
        if canonical not in handoff:
            for alias in aliases:
                if alias in handoff:
                    handoff[canonical] = handoff.pop(alias)
                    break
    return handoff


def validate_worker_handoff(phase: str, cycle: int, handoff: Dict[str, Any]) -> Dict[str, Any]:
    handoff = _normalize_worker_handoff_keys(handoff)
    hard_required = {"cycle", "status"}
    missing_hard = hard_required.difference(handoff)
    if missing_hard:
        raise SupervisorError(f"Worker handoff missing critical keys: {sorted(missing_hard)}")
    soft_keys = {"summary_of_changes", "current_frontier", "likely_next_step", "input_request"}
    for key in soft_keys:
        if key not in handoff:
            handoff[key] = ""
    handoff = validate_phase_and_cycle_fields("Worker handoff", handoff, phase=phase, cycle=cycle)
    status = str(handoff.get("status", "")).strip().upper()
    allowed = set(phase_specific_worker_statuses(phase))
    if status not in allowed:
        raise SupervisorError(f"Invalid worker status {status!r} for phase {phase}")
    handoff["status"] = status
    return handoff


def load_validated_theorem_frontier_worker_update(
    config: Config,
    phase: str,
    cycle: int,
    worker_terminal_output: str,
) -> Dict[str, Any]:
    cycle_path = theorem_frontier_worker_update_path(config, cycle)
    frontier_update = load_json_artifact_with_fallback(
        cycle_path,
        worker_terminal_output,
        ("phase", "cycle", "requested_action"),
        fallback_paths=artifact_fallback_paths(
            config,
            cycle_path,
            theorem_frontier_worker_update_path(config),
        ),
    )
    frontier_update = validate_theorem_frontier_worker_update_full(phase, cycle, frontier_update)
    persist_supervisor_artifact(frontier_update, cycle_path, theorem_frontier_worker_update_path(config))
    return frontier_update


def validate_worker_cycle_artifacts(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    worker_terminal_output: str,
    worker_handoff: Dict[str, Any],
) -> Dict[str, Any]:
    previous_validation = state.get("last_validation") if isinstance(state.get("last_validation"), dict) else None
    cycle_baseline = current_cycle_lean_baseline(state, cycle)
    frontier_update: Optional[Dict[str, Any]] = None
    validation_summary: Dict[str, Any]
    if theorem_frontier_enabled(config, phase):
        frontier_update = load_validated_theorem_frontier_worker_update(
            config,
            phase,
            cycle,
            worker_terminal_output,
        )
        worker_update_report = verify_theorem_frontier_worker_update_artifact(
            config,
            state,
            phase,
            cycle,
            frontier_update,
            previous_validation=previous_validation,
            cycle_baseline=cycle_baseline,
        )
        validation_summary = worker_update_report["validation_summary"]
    else:
        validation_summary = run_validation(
            config,
            phase,
            cycle,
            previous_validation=previous_validation,
            cycle_baseline=cycle_baseline,
        )
    return {
        "worker_handoff": worker_handoff,
        "frontier_update": frontier_update,
        "validation_summary": validation_summary,
    }


def verify_theorem_frontier_worker_update_artifact(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    worker_update: Dict[str, Any],
    *,
    previous_validation: Optional[Dict[str, Any]] = None,
    cycle_baseline: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    validate_worker_owned_theorem_frontier_update(
        config,
        state,
        phase,
        cycle,
        worker_update,
    )
    validation_summary = run_validation(
        config,
        phase,
        cycle,
        previous_validation=previous_validation,
        cycle_baseline=cycle_baseline,
    )
    validation_summary["theorem_frontier_cone_files"] = apply_theorem_frontier_cone_file_guard(
        config,
        phase,
        validation_summary,
        worker_update,
    )
    validate_theorem_frontier_generated_edit_policy(
        config,
        state,
        phase,
        worker_update,
        validation_summary,
    )
    deterministic_report: Optional[Dict[str, Any]] = None
    requested_action = str(worker_update.get("requested_action") or "").strip().upper()
    if requested_action in {"CLOSE", "REFACTOR"}:
        try:
            deterministic_report = verify_theorem_frontier_deterministic_action(
                config,
                state,
                phase,
                cycle,
                worker_update,
                validation_summary=validation_summary,
            )
        except SupervisorError as exc:
            if requested_action == "REFACTOR":
                raise WorkerFixableValidationError(
                    f"Theorem-frontier REFACTOR is not admissible yet: {str(exc).strip()} "
                    "Keep the frozen generated frontier statements unchanged and restore every already-proved generated proof file."
                ) from exc
            raise
    return {
        "phase": phase,
        "cycle": int(cycle),
        "requested_action": requested_action,
        "active_node_id": normalize_frontier_text(worker_update.get("active_node_id")),
        "validation_summary": validation_summary,
        "deterministic_report": deterministic_report,
    }


def verify_theorem_frontier_deterministic_action(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    worker_update: Dict[str, Any],
    *,
    validation_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not theorem_frontier_full_enabled(config, phase):
        raise SupervisorError("Theorem-frontier deterministic worker validation is only available in full theorem-frontier mode.")
    requested_action = str(worker_update.get("requested_action") or "").strip().upper()
    if requested_action not in {"CLOSE", "REFACTOR"}:
        raise SupervisorError(
            f"Theorem-frontier deterministic worker validation only supports CLOSE/REFACTOR, got {requested_action or '<empty>'!r}."
        )
    validate_worker_owned_theorem_frontier_update(
        config,
        state,
        phase,
        cycle,
        worker_update,
    )
    summary = validation_summary
    if not isinstance(summary, dict):
        previous_validation = state.get("last_validation") if isinstance(state.get("last_validation"), dict) else None
        cycle_baseline = current_cycle_lean_baseline(state, cycle)
        summary = run_validation(
            config,
            phase,
            cycle,
            previous_validation=previous_validation,
            cycle_baseline=cycle_baseline,
        )
        summary["theorem_frontier_cone_files"] = apply_theorem_frontier_cone_file_guard(
            config,
            phase,
            summary,
            worker_update,
        )
    validate_theorem_frontier_generated_edit_policy(
        config,
        state,
        phase,
        worker_update,
        summary,
    )
    if requested_action == "CLOSE":
        action_report = verify_theorem_frontier_close_attempt(
            config,
            state,
            phase,
            cycle,
            worker_update,
        )
    else:
        action_report = verify_theorem_frontier_refactor_attempt(
            config,
            state,
            phase,
            cycle,
            worker_update,
        )
    return {
        "phase": phase,
        "cycle": int(cycle),
        "requested_action": requested_action,
        "active_node_id": normalize_frontier_text(worker_update.get("active_node_id")),
        "validation_summary": summary,
        "action_report": action_report,
    }


def validate_reviewer_cycle_artifacts(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    reviewer_terminal_output: str,
    decision: Dict[str, Any],
) -> Dict[str, Any]:
    frontier_review: Optional[Dict[str, Any]] = None
    if theorem_frontier_enabled(config, phase):
        cycle_path = theorem_frontier_review_path(config, cycle)
        frontier_review = load_json_artifact_with_fallback(
            cycle_path,
            reviewer_terminal_output,
            ("phase", "cycle", "outcome"),
            fallback_paths=artifact_fallback_paths(
                config,
                cycle_path,
                theorem_frontier_review_path(config),
            ),
        )
        frontier_review = validate_theorem_frontier_review_full(phase, cycle, frontier_review)
        persist_supervisor_artifact(frontier_review, cycle_path, theorem_frontier_review_path(config))
    return {
        "decision": decision,
        "frontier_review": frontier_review,
    }


def _deterministic_frontier_decision_from_review(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    worker_update: Dict[str, Any],
    frontier_review: Dict[str, Any],
    *,
    reason: str,
) -> Dict[str, Any]:
    preview_state = deep_copy_jsonish(state)
    preview_payload = update_theorem_frontier_full_state(
        config,
        preview_state,
        worker_update,
        frontier_review,
        None,
        None,
        cycle=cycle,
        persist=False,
    )
    preview_active_node_id = normalize_frontier_text(preview_payload.get("active_node_id")) if isinstance(preview_payload, dict) else ""
    next_prompt = (
        f"Continue proof formalization at `{preview_active_node_id}`."
        if preview_active_node_id
        else "All theorem-frontier nodes are locally proved and closed; advance out of proof formalization."
    )
    decision_value = "CONTINUE" if preview_active_node_id else "ADVANCE_PHASE"
    decision = validate_reviewer_decision(
        phase,
        cycle,
        {
            "phase": phase,
            "cycle": cycle,
            "decision": decision_value,
            "confidence": 1.0,
            "reason": reason,
            "next_prompt": next_prompt,
        },
    )
    return {
        "decision": decision,
        "frontier_review": frontier_review,
        "preview_payload": preview_payload,
    }


def synthesize_deterministic_frontier_result(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    cycle: int,
    worker_update: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    payload = theorem_frontier_payload(state)
    if not isinstance(payload, dict):
        return None
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        return None
    requested_action = str(worker_update.get("requested_action") or "").strip().upper()
    active_node_id = normalize_frontier_text(worker_update.get("active_node_id"))
    active_node = nodes.get(active_node_id) if active_node_id else None
    if not isinstance(active_node, dict):
        return None
    blocker_cluster = normalize_frontier_text(active_node.get("blocker_cluster"))

    if requested_action == "CLOSE":
        try:
            deterministic_report = verify_theorem_frontier_deterministic_action(
                config,
                state,
                phase,
                cycle,
                worker_update,
            )
            close_report = deterministic_report["action_report"]
        except SupervisorError as exc:
            frontier_review = validate_theorem_frontier_review_full(
                phase,
                cycle,
                {
                    "phase": phase,
                    "cycle": cycle,
                    "active_node_id": active_node_id,
                    "assessed_action": "CLOSE",
                    "blocker_cluster": blocker_cluster,
                    "outcome": "STILL_OPEN",
                    "next_active_node_id": active_node_id,
                    "cone_purity": "HIGH",
                    "open_hypotheses": [],
                    "justification": f"Deterministic CLOSE validation failed: {str(exc).strip()}",
                },
            )
            result = _deterministic_frontier_decision_from_review(
                config,
                state,
                phase,
                cycle,
                worker_update,
                frontier_review,
                reason=(
                    f"The supervisor mechanically checked the generated CLOSE proof for `{active_node_id}` and it did not "
                    f"build yet: {str(exc).strip()}"
                ),
            )
            result["close_validation_error"] = str(exc).strip()
            return result

        next_candidate_ids = [
            normalize_frontier_text(item)
            for item in (worker_update.get("next_candidate_node_ids") or [])
            if normalize_frontier_text(item)
        ]
        next_active_node_id = next_candidate_ids[0] if next_candidate_ids else ""
        frontier_review = validate_theorem_frontier_review_full(
            phase,
            cycle,
            {
                "phase": phase,
                "cycle": cycle,
                "active_node_id": active_node_id,
                "assessed_action": "CLOSE",
                "blocker_cluster": blocker_cluster,
                "outcome": "CLOSED",
                "next_active_node_id": next_active_node_id,
                "cone_purity": "HIGH",
                "open_hypotheses": [],
                "justification": (
                    "The supervisor mechanically validated the worker-supplied CLOSE by compiling the generated "
                    "frontier proof file against the immutable generated statement layer."
                ),
            },
        )
        return _deterministic_frontier_decision_from_review(
            config,
            state,
            phase,
            cycle,
            worker_update,
            frontier_review,
            reason=(
                f"The supervisor deterministically validated the CLOSE on `{active_node_id}` by compiling "
                "the generated frontier proof file against the frozen local statement interface."
            ),
        )

    if requested_action == "REFACTOR":
        deterministic_report = verify_theorem_frontier_deterministic_action(
            config,
            state,
            phase,
            cycle,
            worker_update,
        )
        report = deterministic_report["action_report"]
        frontier_review = validate_theorem_frontier_review_full(
            phase,
            cycle,
            {
                "phase": phase,
                "cycle": cycle,
                "active_node_id": active_node_id,
                "assessed_action": "REFACTOR",
                "blocker_cluster": blocker_cluster,
                "outcome": "STILL_OPEN",
                "next_active_node_id": active_node_id,
                "cone_purity": "HIGH",
                "open_hypotheses": [],
                "justification": (
                    "The supervisor deterministically validated the REFACTOR by keeping the generated frontier "
                    "statement interface frozen and rechecking all currently proved generated proof files."
                ),
            },
        )
        return _deterministic_frontier_decision_from_review(
            config,
            state,
            phase,
            cycle,
            worker_update,
            frontier_review,
            reason=(
                f"The supervisor deterministically validated REFACTOR on `{active_node_id}` while preserving "
                f"{len(report.get('proved_node_ids') or [])} already-proved node(s) against the frozen frontier statements."
            ),
        )
    return None

def validate_reviewer_decision(phase: str, cycle: int, decision: Dict[str, Any]) -> Dict[str, Any]:
    hard_required = {"cycle", "decision"}
    missing_hard = hard_required.difference(decision)
    if missing_hard:
        raise SupervisorError(f"Reviewer decision missing critical keys: {sorted(missing_hard)}")
    for key in ("confidence", "reason", "next_prompt"):
        if key not in decision:
            decision[key] = "" if key != "confidence" else 0.5
    decision = validate_phase_and_cycle_fields("Reviewer decision", decision, phase=phase, cycle=cycle)
    value = str(decision.get("decision", "")).strip().upper()
    allowed = set(phase_specific_reviewer_decisions(phase))
    if value not in allowed:
        raise SupervisorError(f"Invalid reviewer decision {value!r} for phase {phase}")
    decision["decision"] = value
    return decision


def validate_stuck_recovery_suggestion(phase: str, cycle: int, suggestion: Dict[str, Any]) -> Dict[str, Any]:
    required_keys = {"phase", "cycle", "diagnosis", "creative_suggestion", "why_this_might_work", "worker_prompt"}
    missing = required_keys.difference(suggestion)
    if missing:
        raise SupervisorError(f"Stuck-recovery suggestion missing keys: {sorted(missing)}")
    suggestion = validate_phase_and_cycle_fields(
        "Stuck-recovery suggestion",
        dict(suggestion),
        phase=phase,
        cycle=cycle,
    )
    for key in ("diagnosis", "creative_suggestion", "why_this_might_work", "worker_prompt"):
        suggestion[key] = str(suggestion.get(key, "")).strip()
    if not suggestion["creative_suggestion"]:
        raise SupervisorError("Stuck-recovery suggestion must include a non-empty creative_suggestion.")
    if not suggestion["worker_prompt"]:
        raise SupervisorError("Stuck-recovery suggestion must include a non-empty worker_prompt.")
    return suggestion


def validate_branch_strategy_decision(
    config: Config,
    phase: str,
    cycle: int,
    decision: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    required_keys = {"phase", "cycle", "branch_decision", "confidence", "reason", "strategies"}
    missing = required_keys.difference(decision)
    if missing:
        raise SupervisorError(f"Branch-strategy decision missing keys: {sorted(missing)}")
    decision = validate_phase_and_cycle_fields(
        "Branch-strategy decision",
        dict(decision),
        phase=phase,
        cycle=cycle,
    )
    branch_decision = str(decision.get("branch_decision", "")).strip().upper()
    if branch_decision not in BRANCH_STRATEGY_DECISIONS:
        raise SupervisorError(f"Invalid branch_decision {branch_decision!r}")
    raw_strategies = decision.get("strategies")
    if not isinstance(raw_strategies, list):
        raise SupervisorError("Branch-strategy decision strategies must be a list.")
    strategies: List[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw in raw_strategies:
        if not isinstance(raw, dict):
            raise SupervisorError("Each branch strategy must be an object.")
        for key in ("name", "summary", "worker_prompt", "why_this_might_eventually_succeed", "rewrite_scope"):
            if key not in raw:
                raise SupervisorError(f"Branch strategy missing key {key!r}.")
        name = sanitize_branch_label(str(raw.get("name", "")))
        if not name:
            raise SupervisorError("Branch strategy name cannot be empty.")
        if name in seen_names:
            raise SupervisorError(f"Duplicate branch strategy name: {name}")
        seen_names.add(name)
        rewrite_scope = str(raw.get("rewrite_scope", "")).strip().lower()
        if rewrite_scope not in {"incremental", "major"}:
            raise SupervisorError(f"Invalid rewrite_scope {rewrite_scope!r} for branch strategy {name}")
        strategies.append(
            {
                "name": name,
                "summary": str(raw.get("summary", "")).strip(),
                "worker_prompt": str(raw.get("worker_prompt", "")).strip(),
                "why_this_might_eventually_succeed": str(raw.get("why_this_might_eventually_succeed", "")).strip(),
                "rewrite_scope": rewrite_scope,
            }
        )
    limit = branch_strategy_limit(config, state or {})
    if branch_decision == "NO_BRANCH":
        strategies = []
    elif not (2 <= len(strategies) <= limit):
        raise SupervisorError(
            "Branch-strategy decision must include between 2 and "
            f"{limit} strategies when branching."
        )
    frontier_anchor_node_id = normalize_frontier_text(decision.get("frontier_anchor_node_id"))
    active_node_id = theorem_frontier_active_node_id(state or {})
    if theorem_frontier_full_enabled(config, phase):
        if not frontier_anchor_node_id:
            raise SupervisorError("Branch-strategy decision must include frontier_anchor_node_id.")
        if active_node_id and frontier_anchor_node_id != active_node_id:
            raise SupervisorError(
                "Branch-strategy decision frontier_anchor_node_id must match the active theorem-frontier node "
                f"{active_node_id!r}."
            )
    decision["branch_decision"] = branch_decision
    decision["strategies"] = strategies
    decision["frontier_anchor_node_id"] = active_node_id or frontier_anchor_node_id or ""
    return decision


def validate_branch_selection_decision(
    config: Config,
    phase: str,
    cycle: int,
    decision: Dict[str, Any],
    allowed_branches: Sequence[str],
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    required_keys = {"phase", "cycle", "selection_decision", "confidence", "reason", "selected_branch"}
    missing = required_keys.difference(decision)
    if missing:
        raise SupervisorError(f"Branch-selection decision missing keys: {sorted(missing)}")
    decision = validate_phase_and_cycle_fields(
        "Branch-selection decision",
        dict(decision),
        phase=phase,
        cycle=cycle,
    )
    selection_decision = str(decision.get("selection_decision", "")).strip().upper()
    if selection_decision not in BRANCH_SELECTION_DECISIONS:
        raise SupervisorError(f"Invalid selection_decision {selection_decision!r}")
    selected_branch = sanitize_branch_label(str(decision.get("selected_branch", "")))
    if selection_decision == "SELECT_BRANCH":
        if selected_branch not in set(allowed_branches):
            raise SupervisorError(
                f"Branch-selection decision selected invalid branch {selected_branch!r}; "
                f"allowed: {sorted(set(allowed_branches))}"
            )
    else:
        selected_branch = ""
    frontier_anchor_node_id = normalize_frontier_text(decision.get("frontier_anchor_node_id"))
    active_node_id = theorem_frontier_active_node_id(state or {})
    if theorem_frontier_full_enabled(config, phase):
        if not frontier_anchor_node_id:
            raise SupervisorError("Branch-selection decision must include frontier_anchor_node_id.")
        if active_node_id and frontier_anchor_node_id != active_node_id:
            raise SupervisorError(
                "Branch-selection decision frontier_anchor_node_id must match the active theorem-frontier node "
                f"{active_node_id!r}."
            )
    decision["selection_decision"] = selection_decision
    decision["selected_branch"] = selected_branch
    decision["frontier_anchor_node_id"] = active_node_id or frontier_anchor_node_id or ""
    return decision


def validate_branch_replacement_decision(
    phase: str,
    cycle: int,
    decision: Dict[str, Any],
    *,
    threshold: float = DEFAULT_BRANCH_FRONTIER_REPLACEMENT_MIN_CONFIDENCE,
) -> Dict[str, Any]:
    required_keys = {"phase", "cycle", "replacement_decision", "confidence", "reason"}
    missing = required_keys.difference(decision)
    if missing:
        raise SupervisorError(f"Branch-replacement decision missing keys: {sorted(missing)}")
    decision = validate_phase_and_cycle_fields(
        "Branch-replacement decision",
        dict(decision),
        phase=phase,
        cycle=cycle,
    )
    replacement_decision = str(decision.get("replacement_decision", "")).strip().upper()
    if replacement_decision not in BRANCH_REPLACEMENT_DECISIONS:
        raise SupervisorError(f"Invalid replacement_decision {replacement_decision!r}")
    try:
        confidence = float(decision.get("confidence", 0.0))
    except (TypeError, ValueError):
        raise SupervisorError("Branch-replacement decision confidence must be numeric.")
    if replacement_decision == "REPLACE_WITH_PROPOSAL" and confidence < threshold:
        raise SupervisorError(
            "Branch-replacement decision confidence must be at least "
            f"{threshold:.1f} to replace the frontier."
        )
    decision["replacement_decision"] = replacement_decision
    decision["confidence"] = confidence
    decision["reason"] = str(decision.get("reason", "")).strip()
    return decision


def clear_incomplete_current_cycle_worker_state(config: Config, state: Dict[str, Any], cycle: int) -> None:
    state.pop("last_worker_output", None)
    state.pop("last_worker_output_cycle", None)
    state.pop("last_worker_handoff", None)
    if isinstance(state.get("last_validation"), dict) and last_validation_cycle(state) == cycle:
        state.pop("last_validation", None)
        validation_summary_path(config).unlink(missing_ok=True)  # type: ignore[arg-type]
    state["last_theorem_frontier_worker_update"] = None
    state["last_theorem_frontier_review"] = None
    state["last_theorem_frontier_paper_review"] = None
    state["last_theorem_frontier_nl_proof_review"] = None
    clear_supervisor_artifacts(
        config,
        worker_handoff_path(config, cycle),
        worker_handoff_path(config),
        theorem_frontier_worker_update_path(config),
        theorem_frontier_worker_update_path(config, cycle),
        theorem_frontier_review_path(config),
        theorem_frontier_review_path(config, cycle),
        theorem_frontier_paper_verifier_path(config),
        theorem_frontier_paper_verifier_path(config, cycle),
        theorem_frontier_nl_proof_verifier_path(config),
        theorem_frontier_nl_proof_verifier_path(config, cycle),
        reviewer_decision_path(config, cycle),
        reviewer_decision_path(config),
    )


def cached_current_cycle_worker_handoff(state: Dict[str, Any], cycle: int) -> Optional[Dict[str, Any]]:
    handoff = state.get("last_worker_handoff")
    if not isinstance(handoff, dict):
        return None
    try:
        return handoff if int(handoff.get("cycle", 0) or 0) == cycle else None
    except Exception:
        return None


def cached_current_cycle_worker_output(state: Dict[str, Any], cycle: int) -> str:
    output = str(state.get("last_worker_output") or "").strip()
    if not output:
        return ""
    try:
        output_cycle = int(state.get("last_worker_output_cycle", 0) or 0)
    except Exception:
        output_cycle = 0
    if output_cycle:
        return output if output_cycle == cycle else ""
    if cached_current_cycle_worker_handoff(state, cycle) is not None:
        return output
    validation = state.get("last_validation")
    if isinstance(validation, dict):
        try:
            if int(validation.get("cycle", 0) or 0) == cycle:
                return output
        except Exception:
            pass
    return ""


def recover_interrupted_worker_state(config: Config, state: Dict[str, Any], phase: str) -> bool:
    cycle = int(state.get("cycle", 0) or 0)
    if cycle <= 0 or last_review_cycle(state) >= cycle:
        return False
    current_cycle_handoff = cached_current_cycle_worker_handoff(state, cycle)
    current_cycle_worker_output = cached_current_cycle_worker_output(state, cycle)
    current_cycle_frontier_update = state.get("last_theorem_frontier_worker_update")
    if not (
        isinstance(current_cycle_frontier_update, dict)
        and int(current_cycle_frontier_update.get("cycle", 0) or 0) == cycle
    ):
        current_cycle_frontier_update = None
    worker_state_complete = (
        isinstance(state.get("last_validation"), dict)
        and last_validation_cycle(state) == cycle
        and current_cycle_handoff is not None
        and bool(current_cycle_worker_output)
    )
    frontier_state_complete = (
        not theorem_frontier_enabled(config, phase)
        or current_cycle_frontier_update is not None
    )
    if worker_state_complete and frontier_state_complete:
        return False

    artifact_path = worker_handoff_path(config, cycle)
    fallback_paths = artifact_fallback_paths(config, artifact_path, worker_handoff_path(config))
    log_path = config.state_dir / "logs" / f"worker-cycle-{cycle:04d}.ansi.log"
    worker_terminal_output = current_cycle_worker_output
    if not worker_terminal_output and log_path.exists():
        worker_terminal_output = read_text(log_path).strip()
    if not artifact_path.exists() and not any(path.exists() for path in fallback_paths) and not worker_terminal_output:
        return False

    recovered_any = False

    if not worker_state_complete:
        try:
            worker_handoff = load_json_artifact_with_fallback(
                artifact_path,
                worker_terminal_output,
                ("phase", "cycle", "status", "summary_of_changes", "current_frontier", "likely_next_step", "input_request"),
                fallback_paths=fallback_paths,
            )
            worker_handoff = validate_worker_handoff(phase, cycle, worker_handoff)
        except SupervisorError as handoff_exc:
            detail = f"Recovered worker burst ended without the required handoff JSON: {str(handoff_exc).strip()}"
            if theorem_frontier_enabled(config, phase):
                try:
                    load_validated_theorem_frontier_worker_update(
                        config,
                        phase,
                        cycle,
                        worker_terminal_output,
                    )
                    detail += " A theorem-frontier update artifact survived; the next worker retry may keep or rewrite it, but it must end by writing worker_handoff.json."
                except SupervisorError:
                    pass
            state["last_worker_protocol_error"] = {
                "phase": phase,
                "cycle": cycle,
                "error": detail,
                "recorded_at": timestamp_now(),
            }
            log_supervisor_warning(
                config,
                cycle=cycle,
                phase=phase,
                category="worker_noncompliance_recovery",
                message="Recovered a worker burst that exited without the required handoff; scheduling a corrected worker retry.",
                detail={"artifact_path": str(artifact_path)},
            )
            clear_incomplete_current_cycle_worker_state(config, state, cycle)
            save_state(config, state)
            return False
        state["last_worker_output"] = worker_terminal_output
        state["last_worker_output_cycle"] = cycle
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
        recovered_any = True

    baseline_created = ensure_current_cycle_lean_baseline(config, state, cycle)
    needs_validation = (
        not isinstance(state.get("last_validation"), dict)
        or last_validation_cycle(state) != cycle
        or (theorem_frontier_enabled(config, phase) and not isinstance(state.get("last_theorem_frontier_worker_update"), dict))
    )
    if needs_validation:
        try:
            validated = validate_worker_cycle_artifacts(
                config,
                state,
                phase,
                cycle,
                worker_terminal_output,
                state["last_worker_handoff"],
            )
        except SupervisorError as frontier_exc:
            log_supervisor_warning(
                config,
                cycle=cycle,
                phase=phase,
                category="frontier_recovery",
                message=str(frontier_exc),
                detail={"artifact_path": str(theorem_frontier_worker_update_path(config))},
            )
            clear_incomplete_current_cycle_worker_state(config, state, cycle)
            save_state(config, state)
            return False
        frontier_update = validated.get("frontier_update")
        if isinstance(frontier_update, dict):
            state["last_theorem_frontier_worker_update"] = frontier_update
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="theorem_frontier_update",
                actor="worker",
                target="supervisor",
                content=frontier_update,
                content_type="json",
            )
        state["last_validation"] = validated["validation_summary"]
        record_chat_event(
            config,
            state,
            cycle=cycle,
            phase=phase,
            kind="validation_summary",
            actor="supervisor",
            target="reviewer",
            content=validated["validation_summary"],
            content_type="json",
        )
        recovered_any = True

    if recovered_any or baseline_created:
        save_state(config, state)
    return recovered_any or baseline_created


def recover_interrupted_reviewer_state(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    policy: Policy,
) -> Optional[bool]:
    cycle = int(state.get("cycle", 0) or 0)
    if cycle <= 0 or last_review_cycle(state) >= cycle:
        return None
    validation_summary = state.get("last_validation")
    if not isinstance(validation_summary, dict) or last_validation_cycle(state) != cycle:
        return None
    worker_handoff = state.get("last_worker_handoff")
    if not isinstance(worker_handoff, dict):
        return None

    decision_path = reviewer_decision_path(config, cycle)
    frontier_path = theorem_frontier_review_path(config, cycle)
    if not decision_path.exists() and not frontier_path.exists():
        return None

    reviewer_terminal_output = ""
    log_path = config.state_dir / "logs" / f"reviewer-cycle-{cycle:04d}.ansi.log"
    if log_path.exists():
        reviewer_terminal_output = read_text(log_path).strip()

    decision = load_json_artifact_with_fallback(
        decision_path,
        reviewer_terminal_output,
        ("phase", "cycle", "decision"),
        fallback_paths=artifact_fallback_paths(
            config,
            decision_path,
            reviewer_decision_path(config),
        ),
    )
    decision = validate_reviewer_decision(phase, cycle, decision)
    persist_supervisor_artifact(decision, decision_path, reviewer_decision_path(config))
    reviewer_result = validate_reviewer_cycle_artifacts(
        config,
        state,
        phase,
        cycle,
        reviewer_terminal_output,
        decision,
    )

    print(f"Recovered completed reviewer burst for cycle {cycle}; finalizing cycle state.")
    should_stop = finalize_reviewer_cycle(
        config,
        state,
        reviewer,
        phase,
        cycle,
        reviewer_result,
        validation_summary,
        policy,
    )
    return should_stop


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


def run_stuck_recovery_review(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    last_review = dict(state.get("last_review") or {})
    trigger_cycle = last_review_cycle(state)
    validation_summary = state.get("last_validation") or {}
    worker_terminal_output = str(state.get("last_worker_output") or "").strip()
    worker_handoff = state.get("last_worker_handoff") or {}
    worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
    attempt_number = current_stuck_recovery_attempt_number(state)
    burst_tag = f"stuck-recovery-{trigger_cycle:04d}-{attempt_number:02d}"
    artifact_path = stuck_recovery_suggestion_path(config, trigger_cycle)

    prompt = build_stuck_recovery_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        validation_summary,
        last_review,
        reviewer.needs_initial_run(),
        policy=policy,
    )
    prompt_for_chat = build_stuck_recovery_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        validation_summary,
        last_review,
        reviewer.needs_initial_run(),
        include_terminal_output=False,
        policy=policy,
    )
    record_chat_event(
        config,
        state,
        cycle=trigger_cycle,
        phase=phase,
        kind="stuck_recovery_prompt",
        actor="supervisor",
        target="reviewer",
        content=prompt_for_chat,
        content_type="text",
        summary=f"Supervisor -> stuck-recovery prompt for cycle {trigger_cycle}",
    )

    def _validate_stuck_recovery(run: Dict[str, Any]) -> Dict[str, Any]:
        output = run["captured_output"].strip()
        sug = load_json_artifact_with_fallback(
            artifact_path,
            output,
            ("phase", "cycle", "diagnosis"),
            fallback_paths=artifact_fallback_paths(
                config,
                artifact_path,
                stuck_recovery_suggestion_path(config),
                Path(run["artifact_path"]),
            ),
        )
        sug = validate_stuck_recovery_suggestion(phase, trigger_cycle, sug)
        persist_supervisor_artifact(sug, artifact_path, stuck_recovery_suggestion_path(config))
        return sug

    run, suggestion = run_burst_with_validation(
        reviewer,
        trigger_cycle,
        prompt,
        config=config,
        state=state,
        phase=phase,
        stage_label="reviewer stuck-recovery burst",
        artifact_path=artifact_path,
        clear_paths=[artifact_path, stuck_recovery_suggestion_path(config)],
        policy=policy,
        validate=_validate_stuck_recovery,
    )
    reviewer.mark_initialized()
    suggestion = record_stuck_recovery_attempt(
        state,
        trigger_cycle=trigger_cycle,
        phase=phase,
        suggestion=suggestion,
    )
    record_chat_event(
        config,
        state,
        cycle=trigger_cycle,
        phase=phase,
        kind="stuck_recovery_suggestion",
        actor="reviewer",
        target="supervisor",
        content=suggestion,
        content_type="json",
    )
    save_state(config, state)
    append_supervisor_jsonl(config.state_dir / "stuck_recovery_log.jsonl", suggestion)
    return suggestion


def run_theorem_frontier_paper_verifier_review(
    config: Config,
    state: Dict[str, Any],
    paper_verifier: ProviderAdapter,
    phase: str,
    worker_terminal_output: str,
    worker_handoff: Dict[str, Any],
    worker_frontier_update: Dict[str, Any],
    *,
    cycle: int,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    burst_tag = f"theorem-frontier-paper-{cycle:04d}"
    artifact_path = theorem_frontier_paper_verifier_path(config, cycle)
    worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
    prompt = build_theorem_frontier_paper_verifier_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        worker_frontier_update,
        paper_verifier.needs_initial_run(),
    )
    prompt_for_chat = build_theorem_frontier_paper_verifier_prompt(
        config,
        state,
        phase,
        "[omitted from the web transcript; raw terminal output is only kept in local logs]",
        worker_handoff_text,
        worker_frontier_update,
        paper_verifier.needs_initial_run(),
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="theorem_frontier_paper_verifier_prompt",
        actor="supervisor",
        target="paper_verifier",
        content=prompt_for_chat,
        content_type="text",
        summary=f"Supervisor -> theorem-frontier paper-verifier prompt for cycle {cycle}",
    )
    def _validate_paper_verifier(run: Dict[str, Any]) -> Dict[str, Any]:
        output = run["captured_output"].strip()
        rev = load_json_artifact_with_fallback(
            artifact_path,
            output,
            ("phase", "cycle", "decision"),
            fallback_paths=artifact_fallback_paths(
                config,
                artifact_path,
                theorem_frontier_paper_verifier_path(config),
                Path(run["artifact_path"]),
            ),
        )
        rev = validate_theorem_frontier_paper_verifier_review(phase, cycle, rev)
        persist_supervisor_artifact(rev, artifact_path, theorem_frontier_paper_verifier_path(config))
        return rev

    run, review = run_burst_with_validation(
        paper_verifier,
        cycle,
        prompt,
        config=config,
        state=state,
        phase=phase,
        stage_label="paper-verifier burst",
        artifact_path=artifact_path,
        clear_paths=[artifact_path, theorem_frontier_paper_verifier_path(config)],
        burst_tag=burst_tag,
        policy=policy,
        validate=_validate_paper_verifier,
    )
    paper_verifier.mark_initialized()
    review["cycle"] = cycle
    state["last_theorem_frontier_paper_review"] = review
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="theorem_frontier_paper_verifier_review",
        actor="paper_verifier",
        target="supervisor",
        content=review,
        content_type="json",
    )
    save_state(config, state)
    append_supervisor_jsonl(config.state_dir / "theorem_frontier_paper_verifier_log.jsonl", review)
    return review


def run_theorem_frontier_nl_proof_verifier_review(
    config: Config,
    state: Dict[str, Any],
    nl_proof_verifier: ProviderAdapter,
    phase: str,
    worker_terminal_output: str,
    worker_handoff: Dict[str, Any],
    worker_frontier_update: Dict[str, Any],
    paper_review: Dict[str, Any],
    *,
    cycle: int,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    burst_tag = f"theorem-frontier-nl-proof-{cycle:04d}"
    artifact_path = theorem_frontier_nl_proof_verifier_path(config, cycle)
    worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
    prompt = build_theorem_frontier_nl_proof_verifier_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        worker_frontier_update,
        paper_review,
        nl_proof_verifier.needs_initial_run(),
    )
    prompt_for_chat = build_theorem_frontier_nl_proof_verifier_prompt(
        config,
        state,
        phase,
        "[omitted from the web transcript; raw terminal output is only kept in local logs]",
        worker_handoff_text,
        worker_frontier_update,
        paper_review,
        nl_proof_verifier.needs_initial_run(),
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="theorem_frontier_nl_proof_verifier_prompt",
        actor="supervisor",
        target="nl_proof_verifier",
        content=prompt_for_chat,
        content_type="text",
        summary=f"Supervisor -> theorem-frontier NL-proof verifier prompt for cycle {cycle}",
    )

    def _validate_nl_proof_verifier(run: Dict[str, Any]) -> Dict[str, Any]:
        output = run["captured_output"].strip()
        rev = load_json_artifact_with_fallback(
            artifact_path,
            output,
            ("phase", "cycle", "decision"),
            fallback_paths=artifact_fallback_paths(
                config,
                artifact_path,
                theorem_frontier_nl_proof_verifier_path(config),
                Path(run["artifact_path"]),
            ),
        )
        rev = validate_theorem_frontier_nl_proof_verifier_review(phase, cycle, rev)
        persist_supervisor_artifact(rev, artifact_path, theorem_frontier_nl_proof_verifier_path(config))
        return rev

    run, review = run_burst_with_validation(
        nl_proof_verifier,
        cycle,
        prompt,
        config=config,
        state=state,
        phase=phase,
        stage_label="NL-proof-verifier burst",
        artifact_path=artifact_path,
        clear_paths=[artifact_path, theorem_frontier_nl_proof_verifier_path(config)],
        burst_tag=burst_tag,
        policy=policy,
        validate=_validate_nl_proof_verifier,
    )
    nl_proof_verifier.mark_initialized()
    review["cycle"] = cycle
    state["last_theorem_frontier_nl_proof_review"] = review
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="theorem_frontier_nl_proof_verifier_review",
        actor="nl_proof_verifier",
        target="supervisor",
        content=review,
        content_type="json",
    )
    save_state(config, state)
    append_supervisor_jsonl(config.state_dir / "theorem_frontier_nl_proof_verifier_log.jsonl", review)
    return review


def config_to_raw_dict(config: Config, *, policy: Optional[Policy] = None) -> Dict[str, Any]:
    effective = effective_policy(config, policy=policy)
    workflow: Dict[str, Any] = {
        "start_phase": config.workflow.start_phase,
        "sorry_mode": config.workflow.sorry_mode,
        "approved_axioms_path": str(config.workflow.approved_axioms_path),
        "human_input_path": str(config.workflow.human_input_path),
        "input_request_path": str(config.workflow.input_request_path),
        "theorem_frontier_phase": config.workflow.theorem_frontier_phase,
    }
    if config.workflow.paper_tex_path is not None:
        workflow["paper_tex_path"] = str(config.workflow.paper_tex_path)
    return {
        "repo_path": str(config.repo_path),
        "goal_file": str(config.goal_file),
        "state_dir": str(config.state_dir),
        "worker": {
            "provider": config.worker.provider,
            "model": config.worker.model,
            "extra_args": list(config.worker.extra_args),
        },
        "reviewer": {
            "provider": config.reviewer.provider,
            "model": config.reviewer.model,
            "extra_args": list(config.reviewer.extra_args),
        },
        "tmux": {
            "session_name": config.tmux.session_name,
            "dashboard_window_name": config.tmux.dashboard_window_name,
            "kill_windows_after_capture": config.tmux.kill_windows_after_capture,
        },
        "workflow": workflow,
        "chat": {
            "root_dir": str(config.chat.root_dir),
            "repo_name": config.chat.repo_name,
            "project_name": config.chat.project_name,
            "public_base_url": config.chat.public_base_url,
        },
        "git": {
            "remote_url": config.git.remote_url,
            "remote_name": config.git.remote_name,
            "branch": config.git.branch,
            "author_name": config.git.author_name,
            "author_email": config.git.author_email,
        },
        "max_cycles": config.max_cycles,
        "sleep_seconds": effective.timing.sleep_seconds,
        "startup_timeout_seconds": config.startup_timeout_seconds,
        "burst_timeout_seconds": config.burst_timeout_seconds,
        "policy_path": str(resolved_policy_path(config)),
        "branching": {
            "max_current_branches": config.branching.max_current_branches,
            "evaluation_cycle_budget": effective.branching.evaluation_cycle_budget,
            "poll_seconds": effective.branching.poll_seconds,
        },
    }


def branch_episode_snapshots(episode: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    base_review_count = int(episode.get("base_review_count", 0))
    for branch in episode.get("branches", []):
        if not isinstance(branch, dict):
            continue
        branch_status = str(branch.get("status", "")).strip().lower() or "active"
        config_path = Path(str(branch.get("config_path", "")))
        worktree_path = Path(str(branch.get("worktree_path", "")))
        state_path = worktree_path / ".agent-supervisor" / "state.json"
        state_data = JsonFile.load(state_path, {})
        latest_review = state_data.get("last_review") if isinstance(state_data.get("last_review"), dict) else {}
        latest_handoff = (
            state_data.get("last_worker_handoff") if isinstance(state_data.get("last_worker_handoff"), dict) else {}
        )
        latest_validation = (
            state_data.get("last_validation") if isinstance(state_data.get("last_validation"), dict) else {}
        )
        proposal = pending_branch_proposal(state_data)
        recovery_attempt_limit = stuck_recovery_attempt_limit(state_data)
        recovery_attempt_count = len(stuck_recovery_attempts(state_data))
        frontier_summary = theorem_frontier_branch_summary(state_data)
        snapshots.append(
            {
                "name": branch.get("name"),
                "branch_status": branch_status,
                "summary": branch.get("summary"),
                "frontier_anchor_node_id": normalize_frontier_text(
                    branch.get("frontier_anchor_node_id") or episode.get("frontier_anchor_node_id")
                )
                or None,
                "rewrite_scope": branch.get("rewrite_scope"),
                "worker_prompt": branch.get("worker_prompt"),
                "why_this_might_eventually_succeed": branch.get("why_this_might_eventually_succeed"),
                "worktree_path": str(worktree_path),
                "config_path": str(config_path),
                "supervisor_session": branch.get("supervisor_session"),
                "agent_session": branch.get("agent_session"),
                "review_count": branch_review_count(state_data),
                "progress_reviews": branch_progress_count(state_data, base_review_count),
                "cycle": int(state_data.get("cycle", 0) or 0),
                "phase": state_data.get("phase"),
                "latest_review_decision": latest_review.get("decision"),
                "latest_review_reason": latest_review.get("reason"),
                "latest_worker_status": latest_handoff.get("status"),
                "latest_worker_frontier": latest_handoff.get("current_frontier"),
                "stuck_recovery_attempt_count": recovery_attempt_count,
                "stuck_recovery_attempt_limit": recovery_attempt_limit,
                "stuck_recovery_exhausted": branch_status != "dead" and stuck_recovery_exhausted(state_data),
                "pending_branch_proposal": proposal,
                "pending_branch_proposal_confidence": (
                    proposal.get("confidence") if isinstance(proposal, dict) else None
                ),
                "pending_branch_proposal_strategy_count": (
                    len(proposal.get("strategies", [])) if isinstance(proposal, dict) and isinstance(proposal.get("strategies"), list) else 0
                ),
                "git_head": ((latest_validation.get("git") or {}).get("head") if isinstance(latest_validation, dict) else None),
                "theorem_frontier_active_node_id": frontier_summary.get("active_node_id"),
                "theorem_frontier_active_node_anchor": frontier_summary.get("active_node_anchor"),
                "theorem_frontier_blocker_cluster": frontier_summary.get("blocker_cluster"),
                "theorem_frontier_current_action": frontier_summary.get("current_action"),
                "theorem_frontier_assessed_action": frontier_summary.get("assessed_action"),
                "theorem_frontier_open_hypotheses_count": frontier_summary.get("open_hypotheses_count"),
                "theorem_frontier_open_hypotheses": frontier_summary.get("open_hypotheses"),
                "theorem_frontier_open_children_count": frontier_summary.get("open_children_count"),
                "theorem_frontier_open_children": frontier_summary.get("open_children"),
                "theorem_frontier_active_node_age": frontier_summary.get("active_node_age"),
                "theorem_frontier_blocker_cluster_age": frontier_summary.get("blocker_cluster_age"),
                "theorem_frontier_failed_close_attempts": frontier_summary.get("failed_close_attempts"),
                "theorem_frontier_cone_purity": frontier_summary.get("cone_purity"),
                "theorem_frontier_escalation_required": frontier_summary.get("escalation_required"),
                "theorem_frontier_escalation_reasons": frontier_summary.get("escalation_reasons"),
            }
        )
    return snapshots


def branch_episode_ready_for_selection(
    config: Config,
    episode: Dict[str, Any],
    snapshots: Sequence[Dict[str, Any]],
    policy: Optional[Policy] = None,
) -> bool:
    active_snapshots = [snapshot for snapshot in snapshots if str(snapshot.get("branch_status", "active")).lower() != "dead"]
    if not active_snapshots:
        return False
    if any(snapshot.get("latest_review_decision") == "DONE" for snapshot in active_snapshots):
        return True
    target = int(
        episode.get(
            "next_selection_review_target",
            int(episode.get("base_review_count", 0)) + branch_review_budget(config, policy),
        )
    )
    return all(int(snapshot.get("review_count", 0) or 0) >= target for snapshot in active_snapshots)


def branch_strategy_branch_name(config: Config, episode_id: str, label: str) -> str:
    return f"lagent/{sanitize_repo_name(config.chat.repo_name)}/{episode_id}/{sanitize_branch_label(label)}"


def branch_strategy_worktree_path(config: Config, episode_id: str, label: str) -> Path:
    return config.repo_path.parent / f"{config.repo_path.name}--{episode_id}--{sanitize_branch_label(label)}"


def child_branch_config_payload(
    config: Config,
    *,
    episode_id: str,
    strategy: Dict[str, Any],
    worktree_path: Path,
    config_path: Path,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    child_repo_name = sanitize_repo_name(f"{config.chat.repo_name}-{episode_id}-{strategy['name']}")
    agent_session = sanitize_tmux_session_name(f"{child_repo_name}-agents")
    payload = config_to_raw_dict(config, policy=policy)
    payload["repo_path"] = str(worktree_path)
    payload["goal_file"] = str(worktree_path / config.goal_file.name)
    payload["state_dir"] = str(worktree_path / ".agent-supervisor")
    payload["tmux"]["session_name"] = agent_session
    payload["workflow"]["approved_axioms_path"] = str(worktree_path / config.workflow.approved_axioms_path.name)
    payload["workflow"]["human_input_path"] = str(worktree_path / config.workflow.human_input_path.name)
    payload["workflow"]["input_request_path"] = str(worktree_path / config.workflow.input_request_path.name)
    if config.workflow.paper_tex_path is not None:
        payload["workflow"]["paper_tex_path"] = str(worktree_path / config.workflow.paper_tex_path.relative_to(config.repo_path))
    payload["chat"]["repo_name"] = child_repo_name
    payload["chat"]["project_name"] = config.chat.project_name
    payload["git"]["branch"] = branch_strategy_branch_name(config, episode_id, strategy["name"])
    payload["branching"]["max_current_branches"] = 1
    return payload


def start_supervisor_tmux_session(config_path: Path, supervisor_session: str) -> None:
    tmux_cmd(
        "new-session",
        "-d",
        "-s",
        supervisor_session,
        "-n",
        "supervisor",
        "bash",
        "-lc",
        (
            f"cd {shlex.quote(str(PACKAGE_DIR))} && "
            f"python3 supervisor.py --config {shlex.quote(str(config_path))}; "
            "echo; echo '[supervisor exited]'; exec bash"
        ),
    )


def restart_supervisor_tmux_session(config_path: Path, supervisor_session: str) -> None:
    tmux_cmd("kill-session", "-t", supervisor_session, check=False)
    start_supervisor_tmux_session(config_path, supervisor_session)


def build_child_branch_state(
    state: Dict[str, Any],
    *,
    episode_id: str,
    strategy: Dict[str, Any],
    parent_max_current_branches: int,
) -> Dict[str, Any]:
    child_state = deep_copy_jsonish(state)
    child_state["roles"] = {}
    child_state["active_branch_episode"] = None
    child_state["last_branch_consideration_cycle"] = 0
    child_state["branch_parent_max_current_branches"] = max(1, int(parent_max_current_branches))
    child_state["pending_branch_proposal"] = None
    child_state["next_branch_proposal_review_count"] = 0
    child_state["branch_lineage"] = [
        *branch_lineage_entries(state),
        {
            "episode_id": episode_id,
            "branch_name": strategy["name"],
            "summary": strategy["summary"],
            "rewrite_scope": strategy["rewrite_scope"],
        },
    ]
    child_state["branch_context"] = {
        "episode_id": episode_id,
        "branch_name": strategy["name"],
        "summary": strategy["summary"],
        "worker_prompt": strategy["worker_prompt"],
        "why_this_might_eventually_succeed": strategy["why_this_might_eventually_succeed"],
        "rewrite_scope": strategy["rewrite_scope"],
        "frontier_anchor_node_id": normalize_frontier_text(strategy.get("frontier_anchor_node_id")) or None,
    }
    reset_child_branch_theorem_frontier_runtime_state(child_state)
    return child_state


def create_branch_episode(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    decision: Dict[str, Any],
    branch_strategy: Dict[str, Any],
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    preflight_error = branch_episode_preflight_error(config)
    if preflight_error:
        raise SupervisorError(f"Cannot create branch episode: {preflight_error}.")
    status = git_validation_summary(config) if git_is_enabled(config) else {"head": git_output(config, ["rev-parse", "HEAD"]).strip()}

    state["branch_episode_counter"] = int(state.get("branch_episode_counter", 0) or 0) + 1
    episode_id = f"episode-{state['branch_episode_counter']:03d}"
    episode_dir = branch_episode_dir(config, episode_id)
    episode_dir.mkdir(parents=True, exist_ok=True)
    base_review_count = branch_review_count(state)
    parent_head = status.get("head")
    frontier_summary = theorem_frontier_branch_summary(state)
    active_node_id = normalize_frontier_text(frontier_summary.get("active_node_id")) or None
    requested_anchor_node_id = normalize_frontier_text(branch_strategy.get("frontier_anchor_node_id")) or None
    frontier_anchor_node_id = requested_anchor_node_id or active_node_id
    branches: List[Dict[str, Any]] = []
    for strategy in branch_strategy["strategies"]:
        label = sanitize_branch_label(strategy["name"])
        worktree_path = branch_strategy_worktree_path(config, episode_id, label)
        local_branch = branch_strategy_branch_name(config, episode_id, label)
        if worktree_path.exists():
            raise SupervisorError(f"Refusing to create branch worktree at existing path: {worktree_path}")
        git_run(config, ["worktree", "add", "-b", local_branch, str(worktree_path), "HEAD"])
        child_config_path = episode_dir / f"{label}.json"
        payload = child_branch_config_payload(
            config,
            episode_id=episode_id,
            strategy={**strategy, "name": label},
            worktree_path=worktree_path,
            config_path=child_config_path,
            policy=policy,
        )
        JsonFile.dump(child_config_path, payload)
        child_state = build_child_branch_state(
            state,
            episode_id=episode_id,
            strategy={
                **strategy,
                "name": label,
                "frontier_anchor_node_id": frontier_anchor_node_id,
            },
            parent_max_current_branches=config.branching.max_current_branches,
        )
        child_state_dir = worktree_path / ".agent-supervisor"
        JsonFile.dump(child_state_dir / "state.json", child_state)
        write_theorem_frontier_state_file_if_present(child_state_dir, child_state)
        supervisor_session = sanitize_tmux_session_name(f"{payload['chat']['repo_name']}-supervisor")
        start_supervisor_tmux_session(child_config_path, supervisor_session)
        branches.append(
            {
                "name": label,
                "chat_repo_name": payload["chat"]["repo_name"],
                "summary": strategy["summary"],
                "worker_prompt": strategy["worker_prompt"],
                "why_this_might_eventually_succeed": strategy["why_this_might_eventually_succeed"],
                "rewrite_scope": strategy["rewrite_scope"],
                "frontier_anchor_node_id": frontier_anchor_node_id,
                "status": "active",
                "worktree_path": str(worktree_path),
                "config_path": str(child_config_path),
                "local_branch": local_branch,
                "supervisor_session": supervisor_session,
                "agent_session": payload["tmux"]["session_name"],
            }
        )

    episode = {
        "id": episode_id,
        "phase": phase,
        "trigger_cycle": int(decision.get("cycle", state.get("cycle", 0)) or 0),
        "lineage": branch_lineage_entries(state),
        "base_review_count": base_review_count,
        "next_selection_review_target": base_review_count + branch_review_budget(config, policy),
        "evaluation_cycle_budget": branch_review_budget(config, policy),
        "selection_continue_count": 0,
        "selection_question": branch_selection_question_for_state(state),
        "frontier_anchor_node_id": frontier_anchor_node_id,
        "frontier_anchor_lean_anchor": frontier_summary.get("active_node_anchor"),
        "frontier_anchor_lean_statement": frontier_summary.get("active_node_lean_statement"),
        "frontier_anchor_blocker_cluster": frontier_summary.get("blocker_cluster"),
        "reason": branch_strategy.get("reason", ""),
        "confidence": branch_strategy.get("confidence", 0.0),
        "parent_head": parent_head,
        "branches": branches,
        "status": "active",
    }
    state["active_branch_episode"] = episode
    state["last_branch_consideration_cycle"] = episode["trigger_cycle"]
    save_state(config, state)
    append_supervisor_jsonl(episode_dir / "branch_strategy_log.jsonl", branch_strategy)
    return episode


def run_branch_strategy_review(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    last_review: Dict[str, Any],
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    validation_summary = state.get("last_validation") or {}
    worker_terminal_output = str(state.get("last_worker_output") or "").strip()
    worker_handoff = state.get("last_worker_handoff") or {}
    worker_handoff_text = json.dumps(worker_handoff, indent=2, ensure_ascii=False)
    cycle = int(last_review.get("cycle", state.get("cycle", 0)) or 0)
    artifact_path = branch_strategy_artifact_path(config, cycle)
    prompt = build_branch_strategy_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        validation_summary,
        last_review,
        reviewer.needs_initial_run(),
        policy=policy,
    )
    prompt_for_chat = build_branch_strategy_prompt(
        config,
        state,
        phase,
        worker_terminal_output,
        worker_handoff_text,
        validation_summary,
        last_review,
        reviewer.needs_initial_run(),
        include_terminal_output=False,
        policy=policy,
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_strategy_prompt",
        actor="supervisor",
        target="reviewer",
        content=prompt_for_chat,
        content_type="text",
        summary=f"Supervisor -> branch-strategy prompt for cycle {cycle}",
    )
    def _validate_branch_strategy(run: Dict[str, Any]) -> Dict[str, Any]:
        strategy = load_json_artifact_with_fallback(
            artifact_path,
            run["captured_output"].strip(),
            ("phase", "cycle", "branch_decision", "confidence", "reason", "strategies"),
            fallback_paths=artifact_fallback_paths(
                config,
                artifact_path,
                branch_strategy_artifact_path(config),
                Path(run["artifact_path"]),
            ),
        )
        strategy = validate_branch_strategy_decision(config, phase, cycle, strategy, state)
        persist_supervisor_artifact(strategy, artifact_path, branch_strategy_artifact_path(config))
        return strategy

    run, strategy = run_burst_with_validation(
        reviewer,
        cycle,
        prompt,
        config=config,
        state=state,
        phase=phase,
        stage_label="reviewer branch-strategy burst",
        artifact_path=artifact_path,
        clear_paths=[artifact_path, branch_strategy_artifact_path(config)],
        burst_tag=f"branch-strategy-{cycle:04d}",
        policy=policy,
        validate=_validate_branch_strategy,
    )
    reviewer.mark_initialized()
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_strategy_decision",
        actor="reviewer",
        target="supervisor",
        content=strategy,
        content_type="json",
    )
    append_supervisor_jsonl(config.state_dir / "branch_strategy_log.jsonl", strategy)
    save_state(config, state)
    return strategy


def run_branch_selection_review(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    episode: Dict[str, Any],
    snapshots: List[Dict[str, Any]],
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    artifact_path = branch_selection_artifact_path(config, cycle)
    prompt = build_branch_selection_prompt(
        config,
        state,
        phase,
        episode,
        snapshots,
        reviewer.needs_initial_run(),
        policy=policy,
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_selection_prompt",
        actor="supervisor",
        target="reviewer",
        content=prompt,
        content_type="text",
        summary=f"Supervisor -> branch-selection prompt for cycle {cycle}",
    )
    allowed = [str(snapshot.get("name", "")) for snapshot in snapshots]
    def _validate_branch_selection(run: Dict[str, Any]) -> Dict[str, Any]:
        selection = load_json_artifact_with_fallback(
            artifact_path,
            run["captured_output"].strip(),
            ("phase", "cycle", "selection_decision", "confidence", "reason", "selected_branch"),
            fallback_paths=artifact_fallback_paths(
                config,
                artifact_path,
                branch_selection_artifact_path(config),
                Path(run["artifact_path"]),
            ),
        )
        selection = validate_branch_selection_decision(config, phase, cycle, selection, allowed, state)
        persist_supervisor_artifact(selection, artifact_path, branch_selection_artifact_path(config))
        return selection

    run, selection = run_burst_with_validation(
        reviewer,
        cycle,
        prompt,
        config=config,
        state=state,
        phase=phase,
        stage_label="reviewer branch-selection burst",
        artifact_path=artifact_path,
        clear_paths=[artifact_path, branch_selection_artifact_path(config)],
        burst_tag=f"branch-selection-{cycle:04d}",
        policy=policy,
        validate=_validate_branch_selection,
    )
    reviewer.mark_initialized()
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_selection_decision",
        actor="reviewer",
        target="supervisor",
        content=selection,
        content_type="json",
    )
    append_supervisor_jsonl(config.state_dir / "branch_selection_log.jsonl", selection)
    save_state(config, state)
    return selection


def run_branch_replacement_review(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    episode: Dict[str, Any],
    snapshots: List[Dict[str, Any]],
    proposal_snapshot: Dict[str, Any],
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    artifact_path = branch_replacement_artifact_path(config, cycle)
    prompt = build_branch_replacement_prompt(
        config,
        state,
        phase,
        episode,
        snapshots,
        proposal_snapshot,
        reviewer.needs_initial_run(),
        policy=policy,
    )
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_replacement_prompt",
        actor="supervisor",
        target="reviewer",
        content=prompt,
        content_type="text",
        summary=f"Supervisor -> branch-frontier prompt for cycle {cycle}",
    )
    def _validate_branch_replacement(run: Dict[str, Any]) -> Dict[str, Any]:
        decision = load_json_artifact_with_fallback(
            artifact_path,
            run["captured_output"].strip(),
            ("phase", "cycle", "replacement_decision", "confidence", "reason"),
            fallback_paths=artifact_fallback_paths(
                config,
                artifact_path,
                branch_replacement_artifact_path(config),
                Path(run["artifact_path"]),
            ),
        )
        decision = validate_branch_replacement_decision(
            phase,
            cycle,
            decision,
            threshold=branch_replacement_min_confidence(config, policy),
        )
        persist_supervisor_artifact(decision, artifact_path, branch_replacement_artifact_path(config))
        return decision

    run, decision = run_burst_with_validation(
        reviewer,
        cycle,
        prompt,
        config=config,
        state=state,
        phase=phase,
        stage_label="reviewer branch-frontier burst",
        artifact_path=artifact_path,
        clear_paths=[artifact_path, branch_replacement_artifact_path(config)],
        burst_tag=f"branch-replacement-{cycle:04d}",
        policy=policy,
        validate=_validate_branch_replacement,
    )
    reviewer.mark_initialized()
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_replacement_decision",
        actor="reviewer",
        target="supervisor",
        content=decision,
        content_type="json",
    )
    append_supervisor_jsonl(config.state_dir / "branch_replacement_log.jsonl", decision)
    save_state(config, state)
    return decision


def mark_branch_dead_in_episode(
    config: Config,
    state: Dict[str, Any],
    episode: Dict[str, Any],
    branch_name: str,
    *,
    reason: str,
    cycle: int,
) -> bool:
    updated = False
    for branch in episode.get("branches", []):
        if not isinstance(branch, dict) or str(branch.get("name", "")) != branch_name:
            continue
        if str(branch.get("status", "")).strip().lower() == "dead":
            return False
        branch["status"] = "dead"
        branch["pruned_reason"] = reason
        branch["pruned_cycle"] = cycle
        tmux_cmd("kill-session", "-t", str(branch.get("supervisor_session")), check=False)
        tmux_cmd("kill-session", "-t", str(branch.get("agent_session")), check=False)
        updated = True
        break
    if updated:
        state["active_branch_episode"] = episode
        save_state(config, state)
    return updated


def active_branch_snapshots(snapshots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [snapshot for snapshot in snapshots if str(snapshot.get("branch_status", "active")).strip().lower() != "dead"]


def exhausted_branch_snapshots(snapshots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        snapshot
        for snapshot in active_branch_snapshots(snapshots)
        if bool(snapshot.get("stuck_recovery_exhausted"))
    ]


def pending_branch_proposal_snapshots(snapshots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates = [
        snapshot
        for snapshot in active_branch_snapshots(snapshots)
        if isinstance(snapshot.get("pending_branch_proposal"), dict)
    ]
    candidates.sort(
        key=lambda snapshot: (
            float(snapshot.get("pending_branch_proposal_confidence") or 0.0),
            int(snapshot.get("review_count", 0) or 0),
            int(snapshot.get("cycle", 0) or 0),
        ),
        reverse=True,
    )
    return candidates


def proposal_snapshot_anchor_matches_episode(episode: Dict[str, Any], proposal_snapshot: Dict[str, Any]) -> bool:
    episode_anchor = normalize_frontier_text(episode.get("frontier_anchor_node_id"))
    if not episode_anchor:
        return True
    proposal = proposal_snapshot.get("pending_branch_proposal")
    if not isinstance(proposal, dict):
        return False
    proposal_anchor = normalize_frontier_text(proposal.get("frontier_anchor_node_id"))
    return proposal_anchor == episode_anchor


def record_automatic_branch_selection(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    episode: Dict[str, Any],
    *,
    selected_branch: str,
    reason: str,
) -> Dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    frontier_anchor_node_id = normalize_frontier_text(episode.get("frontier_anchor_node_id")) or None
    selection = {
        "phase": phase,
        "cycle": cycle,
        "selection_decision": "SELECT_BRANCH",
        "frontier_anchor_node_id": frontier_anchor_node_id,
        "confidence": 1.0,
        "reason": reason,
        "selected_branch": selected_branch,
        "automatic": True,
    }
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_selection_decision",
        actor="supervisor",
        target="workflow",
        content=selection,
        content_type="json",
    )
    append_supervisor_jsonl(config.state_dir / "branch_selection_log.jsonl", selection)
    append_supervisor_jsonl(branch_episode_dir(config, str(episode.get("id", ""))) / "branch_selection_log.jsonl", selection)
    save_state(config, state)
    return selection


def record_branch_selection_decision(
    config: Config,
    state: Dict[str, Any],
    phase: str,
    episode: Dict[str, Any],
    selection: Dict[str, Any],
) -> Dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    if "frontier_anchor_node_id" not in selection:
        selection = {
            **selection,
            "frontier_anchor_node_id": normalize_frontier_text(episode.get("frontier_anchor_node_id")) or None,
        }
    if "cycle" not in selection:
        selection = {**selection, "cycle": cycle}
    record_chat_event(
        config,
        state,
        cycle=cycle,
        phase=phase,
        kind="branch_selection_decision",
        actor="reviewer",
        target="supervisor",
        content=selection,
        content_type="json",
    )
    append_supervisor_jsonl(config.state_dir / "branch_selection_log.jsonl", selection)
    append_supervisor_jsonl(branch_episode_dir(config, str(episode.get("id", ""))) / "branch_selection_log.jsonl", selection)
    save_state(config, state)
    return selection


def clear_pending_branch_proposal_in_snapshot(snapshot: Dict[str, Any], *, cooldown_reviews: int = 0) -> None:
    config_path = Path(str(snapshot.get("config_path", "")))
    if not config_path.exists():
        return
    branch_config = load_config(config_path)
    branch_state = load_state(branch_config)
    clear_pending_branch_proposal(branch_state)
    next_review = int(snapshot.get("review_count", 0) or 0) + max(0, cooldown_reviews)
    branch_state["next_branch_proposal_review_count"] = max(next_branch_proposal_review_count(branch_state), next_review)
    save_state(branch_config, branch_state)


def restart_branch_supervisor_from_snapshot(snapshot: Dict[str, Any]) -> None:
    config_path = Path(str(snapshot.get("config_path", "")))
    supervisor_session = str(snapshot.get("supervisor_session", "")).strip()
    if not config_path.exists() or not supervisor_session:
        return
    restart_supervisor_tmux_session(config_path, supervisor_session)


def proposal_snapshot_can_replace_frontier(
    config: Config,
    episode: Dict[str, Any],
    snapshots: Sequence[Dict[str, Any]],
    proposal_snapshot: Dict[str, Any],
    *,
    policy: Optional[Policy] = None,
) -> bool:
    active_count = len(active_branch_snapshots(snapshots))
    if active_count < config.branching.max_current_branches:
        return False
    proposal = proposal_snapshot.get("pending_branch_proposal")
    if not isinstance(proposal, dict):
        return False
    try:
        proposal_confidence = float(proposal_snapshot.get("pending_branch_proposal_confidence") or 0.0)
    except (TypeError, ValueError):
        proposal_confidence = 0.0
    if proposal_confidence < branch_replacement_min_confidence(config, policy):
        return False
    strategies = proposal.get("strategies")
    if not isinstance(strategies, list):
        return False
    if not proposal_snapshot_anchor_matches_episode(episode, proposal_snapshot):
        return False
    return len(strategies) == config.branching.max_current_branches


def launch_nested_branch_episode_from_snapshot(
    episode: Dict[str, Any],
    proposal_snapshot: Dict[str, Any],
    *,
    phase: str,
    proposal: Dict[str, Any],
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    if not proposal_snapshot_anchor_matches_episode(episode, proposal_snapshot):
        raise SupervisorError(
            "Cannot launch nested branch episode: replacement proposal drifted away from the parent episode's theorem-frontier anchor."
        )
    config_path = Path(str(proposal_snapshot.get("config_path", "")))
    if not config_path.exists():
        raise SupervisorError(f"Cannot load proposed winner config for nested branching: {config_path}")
    branch_config = load_config(config_path)
    branch_state = load_state(branch_config)
    clear_pending_branch_proposal(branch_state)
    branch_state["next_branch_proposal_review_count"] = 0
    save_state(branch_config, branch_state)
    decision = branch_state.get("last_review")
    if not isinstance(decision, dict):
        raise SupervisorError("Cannot launch nested branch episode: winning branch is missing last_review state.")
    episode = create_branch_episode(branch_config, branch_state, phase, decision, proposal, policy=policy)
    supervisor_session = str(proposal_snapshot.get("supervisor_session", "")).strip()
    if supervisor_session:
        restart_supervisor_tmux_session(config_path, supervisor_session)
    return episode


def prune_branch_episode(
    config: Config,
    state: Dict[str, Any],
    episode: Dict[str, Any],
    selected_branch: str,
    *,
    policy: Optional[Policy] = None,
) -> Dict[str, Any]:
    winner: Optional[Dict[str, Any]] = None
    completed_episode = deep_copy_jsonish(episode)
    completed_episode["status"] = "selected"
    completed_episode["selected_branch"] = selected_branch
    inherited_history = [entry for entry in state.get("branch_history", []) if isinstance(entry, dict)]
    for branch in episode.get("branches", []):
        if not isinstance(branch, dict):
            continue
        branch_state_path = Path(str(branch.get("worktree_path", ""))) / ".agent-supervisor" / "state.json"
        if branch_state_path.exists():
            branch_state = JsonFile.load(branch_state_path, {})
            branch_state["branch_history"] = [*deep_copy_jsonish(inherited_history), deep_copy_jsonish(completed_episode)]
            branch_state["active_branch_episode"] = None
            JsonFile.dump(branch_state_path, branch_state)
        if branch.get("name") == selected_branch:
            winner = branch
            continue
        tmux_cmd("kill-session", "-t", str(branch.get("supervisor_session")), check=False)
        tmux_cmd("kill-session", "-t", str(branch.get("agent_session")), check=False)
    if winner is None:
        raise SupervisorError(f"Could not find selected branch {selected_branch!r} in active episode.")

    winner_config_path = Path(str(winner.get("config_path", "")))
    winner_config = JsonFile.load(winner_config_path, {})
    winner_branching = winner_config.get("branching", {})
    winner_branching["max_current_branches"] = config.branching.max_current_branches
    winner_branching["evaluation_cycle_budget"] = branch_review_budget(config, policy)
    winner_branching["poll_seconds"] = branch_poll_seconds(config, policy)
    winner_config["branching"] = winner_branching
    winner_config["policy_path"] = str(resolved_policy_path(config))
    JsonFile.dump(winner_config_path, winner_config)

    state["branch_history"].append(deep_copy_jsonish(completed_episode))
    state["active_branch_episode"] = None
    save_state(config, state)
    return winner


def branch_episode_status_lines(
    config: Config,
    episode: Dict[str, Any],
    snapshots: Sequence[Dict[str, Any]],
    policy: Optional[Policy] = None,
) -> List[str]:
    target = int(
        episode.get(
            "next_selection_review_target",
            int(episode.get("base_review_count", 0)) + branch_review_budget(config, policy),
        )
    )
    lines = [
        f"Branch episode {episode.get('id', '')}: trigger_cycle={episode.get('trigger_cycle', '?')} "
        f"branches={len(snapshots)} next_selection_review_target={target}",
        f"Selection question: {str(episode.get('selection_question', '')).strip()}",
    ]
    for snapshot in snapshots:
        head = str(snapshot.get("git_head") or "")[:12]
        branch_status = str(snapshot.get("branch_status") or "active")
        stuck_bits = (
            f"stuck_recovery={int(snapshot.get('stuck_recovery_attempt_count', 0) or 0)}/"
            f"{int(snapshot.get('stuck_recovery_attempt_limit', 0) or 0)}"
        )
        if snapshot.get("stuck_recovery_exhausted"):
            stuck_bits += " exhausted"
        proposal_bits = ""
        if snapshot.get("pending_branch_proposal"):
            proposal_bits = (
                f" pending_proposal={int(snapshot.get('pending_branch_proposal_strategy_count', 0) or 0)}-way"
            )
        frontier_bits = ""
        if snapshot.get("theorem_frontier_active_node_id"):
            frontier_bits = (
                f" frontier_node={snapshot.get('theorem_frontier_active_node_id') or 'none'}"
                f" blocker={snapshot.get('theorem_frontier_blocker_cluster') or 'none'}"
                f" open_hyps={int(snapshot.get('theorem_frontier_open_hypotheses_count', 0) or 0)}"
            )
            if snapshot.get("theorem_frontier_escalation_required"):
                frontier_bits += " escalation=yes"
        lines.append(
            "- "
            f"{snapshot.get('name', '')}: "
            f"branch_status={branch_status} "
            f"phase={snapshot.get('phase') or '?'} "
            f"cycle={int(snapshot.get('cycle', 0) or 0)} "
            f"reviews={int(snapshot.get('review_count', 0) or 0)}/{target} "
            f"progress_reviews={int(snapshot.get('progress_reviews', 0) or 0)} "
            f"latest_review={snapshot.get('latest_review_decision') or 'none'} "
            f"worker_status={snapshot.get('latest_worker_status') or 'none'} "
            f"{stuck_bits} "
            f"{proposal_bits} "
            f"{frontier_bits} "
            f"head={head or 'unknown'}"
        )
    return lines


def monitor_active_branch_episode(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    policy_manager: Optional[PolicyManager] = None,
) -> int:
    if policy_manager is None:
        policy_manager = PolicyManager(config)
    while True:
        policy = policy_manager.reload(state=state, persist=True)
        episode = active_branch_episode(state)
        if episode is None:
            return 0
        normalize_branch_episode_selection_schedule(config, state, episode, policy)

        snapshots = branch_episode_snapshots(episode)
        print(f"\n===== branch episode {episode.get('id', '')}: monitoring =====")
        for line in branch_episode_status_lines(config, episode, snapshots, policy):
            print(line)

        exhausted = exhausted_branch_snapshots(snapshots)
        if exhausted:
            exhausted_names = [str(snapshot.get("name", "")).strip() for snapshot in exhausted if str(snapshot.get("name", "")).strip()]
            print(
                "Auto-pruning branch(es) after exhausted stuck recovery: "
                + ", ".join(exhausted_names)
            )
            for snapshot in exhausted:
                branch_name = str(snapshot.get("name", "")).strip()
                if not branch_name:
                    continue
                cycle = int(snapshot.get("cycle", 0) or 0)
                reason = (
                    f"Pruned automatically after exhausting "
                    f"{int(snapshot.get('stuck_recovery_attempt_limit', 0) or 0)} stuck-recovery attempts."
                )
                mark_branch_dead_in_episode(
                    config,
                    state,
                    episode,
                    branch_name,
                    reason=reason,
                    cycle=cycle,
                )
            snapshots = branch_episode_snapshots(episode)
            survivors = active_branch_snapshots(snapshots)
            if not survivors:
                print("Stopping because every branch in the active episode exhausted stuck recovery and was pruned.")
                state.setdefault("branch_history", []).append(
                    {
                        **deep_copy_jsonish(episode),
                        "status": "exhausted",
                    }
                )
                state["active_branch_episode"] = None
                save_state(config, state)
                return 0
            if len(survivors) == 1:
                survivor_name = str(survivors[0].get("name", "")).strip()
                selection = record_automatic_branch_selection(
                    config,
                    state,
                    phase,
                    episode,
                    selected_branch=survivor_name,
                    reason=(
                        "Selected automatically because all other active branches were pruned after exhausting "
                        "their branch-local stuck-recovery budget."
                    ),
                )
                print("\n===== branch selection decision =====")
                print(json.dumps(selection, indent=2, ensure_ascii=False))
                winner = prune_branch_episode(config, state, episode, survivor_name, policy=policy)
                print(
                    f"Automatically selected surviving branch {winner['name']} "
                    f"({winner['worktree_path']})."
                )
                return 0
            print(
                f"{len(survivors)} active branches remain after automatic pruning; "
                "continuing branch monitoring."
            )
            continue

        proposals = pending_branch_proposal_snapshots(snapshots)
        if proposals:
            proposal_snapshot = proposals[0]
            proposal = proposal_snapshot.get("pending_branch_proposal")
            proposal_name = str(proposal_snapshot.get("name", "")).strip()
            if not proposal_snapshot_anchor_matches_episode(episode, proposal_snapshot):
                print(
                    f"Rejecting pending branch-replacement proposal from {proposal_name or 'unknown'}: "
                    "the proposal no longer targets the active parent episode's theorem-frontier anchor."
                )
                clear_pending_branch_proposal_in_snapshot(
                    proposal_snapshot,
                    cooldown_reviews=branch_proposal_cooldown_reviews(config, policy),
                )
                restart_branch_supervisor_from_snapshot(proposal_snapshot)
                continue

            if not proposal_snapshot_can_replace_frontier(config, episode, snapshots, proposal_snapshot, policy=policy):
                print(
                    f"Rejecting pending branch-replacement proposal from {proposal_name or 'unknown'}: "
                    "this v1 policy only supports full frontier replacement when the proposal exactly fills the branch cap."
                )
                clear_pending_branch_proposal_in_snapshot(
                    proposal_snapshot,
                    cooldown_reviews=branch_proposal_cooldown_reviews(config, policy),
                )
                restart_branch_supervisor_from_snapshot(proposal_snapshot)
                continue

            replacement = run_branch_replacement_review(
                config,
                state,
                reviewer,
                phase,
                episode,
                active_branch_snapshots(snapshots),
                proposal_snapshot,
                policy=policy,
            )
            append_supervisor_jsonl(
                branch_episode_dir(config, str(episode.get("id", ""))) / "branch_replacement_log.jsonl",
                replacement,
            )
            print("\n===== branch frontier decision =====")
            print(json.dumps(replacement, indent=2, ensure_ascii=False))

            if replacement["replacement_decision"] != "REPLACE_WITH_PROPOSAL":
                clear_pending_branch_proposal_in_snapshot(
                    proposal_snapshot,
                    cooldown_reviews=branch_proposal_cooldown_reviews(config, policy),
                )
                restart_branch_supervisor_from_snapshot(proposal_snapshot)
                print(
                    f"Kept the current frontier. The proposal from {proposal_name or 'unknown'} is on cooldown for "
                    f"{branch_proposal_cooldown_reviews(config, policy)} review(s)."
                )
                continue

            if not isinstance(proposal, dict):
                raise SupervisorError("Accepted branch replacement is missing the stored proposal payload.")
            selection = record_branch_selection_decision(
                config,
                state,
                phase,
                episode,
                {
                    "phase": phase,
                    "selection_decision": "SELECT_BRANCH",
                    "confidence": replacement["confidence"],
                    "reason": replacement["reason"],
                    "selected_branch": proposal_name,
                    "replacement": True,
                },
            )
            print("\n===== branch selection decision =====")
            print(json.dumps(selection, indent=2, ensure_ascii=False))
            winner = prune_branch_episode(config, state, episode, proposal_name, policy=policy)
            nested_episode = launch_nested_branch_episode_from_snapshot(
                episode,
                {**proposal_snapshot, **winner},
                phase=phase,
                proposal=proposal,
                policy=policy,
            )
            print(
                f"Replaced the capped frontier by selecting {proposal_name} and opening nested branch episode "
                f"{nested_episode['id']} with {len(nested_episode.get('branches', []))} branch(es)."
            )
            return 0

        active_snapshots = active_branch_snapshots(snapshots)
        if not branch_episode_ready_for_selection(config, episode, active_snapshots, policy):
            print(
                f"Waiting {branch_poll_seconds(config, policy):.0f}s before polling branch progress again."
            )
            time.sleep(branch_poll_seconds(config, policy))
            continue

        selection = run_branch_selection_review(
            config,
            state,
            reviewer,
            phase,
            episode,
            active_snapshots,
            policy=policy,
        )
        append_supervisor_jsonl(branch_episode_dir(config, str(episode.get("id", ""))) / "branch_selection_log.jsonl", selection)
        print("\n===== branch selection decision =====")
        print(json.dumps(selection, indent=2, ensure_ascii=False))

        if selection["selection_decision"] == "CONTINUE_BRANCHING":
            continue_count = branch_selection_continue_count(config, episode, policy)
            episode["selection_continue_count"] = continue_count + 1
            episode["evaluation_cycle_budget"] = branch_review_budget(config, policy)
            episode["next_selection_review_target"] = branch_selection_target_for_continue_count(
                config,
                episode,
                continue_count + 1,
                policy,
            )
            state["active_branch_episode"] = episode
            save_state(config, state)
            print(
                "Reviewer chose to continue branching. "
                f"Next branch-selection checkpoint is review_count >= {episode['next_selection_review_target']} "
                "for every active branch."
            )
            time.sleep(branch_poll_seconds(config, policy))
            continue

        winner = prune_branch_episode(
            config,
            state,
            episode,
            str(selection.get("selected_branch", "")),
            policy=policy,
        )
        print(
            "Selected winning branch "
            f"{winner.get('name')} at {winner.get('worktree_path')}."
        )
        print(
            "The winning branch supervisor remains active in its own worktree/session. "
            f"Use config {winner.get('config_path')} and session {winner.get('supervisor_session')} to keep following it."
        )
        return 0


def enforce_terminal_decision(
    config: Config,
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
        if validation_summary.get("theorem_stating_edit_policy", {}).get("disallowed_changed_lean_files"):
            raise SupervisorError(
                "Cannot advance from theorem_stating after editing Lean files outside the statement-file cone."
            )
        if theorem_frontier_phase(config) == "full":
            validate_paper_main_results_manifest_repo_evidence(config)
    if phase == "proof_formalization" and decision_value in {"ADVANCE_PHASE", "DONE"}:
        if not validation_summary["build"]["ok"]:
            raise SupervisorError("Cannot complete proof_formalization while `lake build` is failing.")
        if validation_summary["sorries"]["count"] != 0:
            raise SupervisorError("Cannot complete proof_formalization while any `sorry` remains.")
        if validation_summary["axioms"]["unapproved"]:
            raise SupervisorError("Cannot complete proof_formalization with unapproved axioms present.")
    if is_style_cleanup_phase(phase) and decision_value == "DONE":
        if not validation_summary["build"]["ok"]:
            raise SupervisorError("Cannot finish cleanup while `lake build` is failing.")
        if validation_summary["sorries"]["count"] != 0:
            raise SupervisorError("Cannot finish cleanup while any `sorry` remains.")
        if validation_summary["axioms"]["unapproved"]:
            raise SupervisorError("Cannot finish cleanup with unapproved axioms present.")


def finalize_reviewer_cycle(
    config: Config,
    state: Dict[str, Any],
    reviewer: ProviderAdapter,
    phase: str,
    cycle: int,
    reviewer_result: Dict[str, Any],
    validation_summary: Dict[str, Any],
    policy: Policy,
) -> bool:
    decision = reviewer_result["decision"]
    frontier_review: Optional[Dict[str, Any]] = reviewer_result.get("frontier_review")
    current_frontier: Optional[Dict[str, Any]] = None
    decision_already_recorded = last_review_cycle(state) >= cycle

    if theorem_frontier_enabled(config, phase):
        state["last_theorem_frontier_review"] = frontier_review
        worker_frontier_update = state.get("last_theorem_frontier_worker_update")
        if not isinstance(worker_frontier_update, dict):
            raise SupervisorError("Missing theorem-frontier worker update while applying frontier review.")
        frontier_payload_before = theorem_frontier_payload(state) or {}
        frontier_current = frontier_payload_before.get("current") if isinstance(frontier_payload_before, dict) else {}
        frontier_already_applied = int((frontier_current or {}).get("cycle", 0) or 0) == cycle
        if not frontier_already_applied:
            _dag_before_node_ids = set((frontier_payload_before.get("nodes") or {}).keys())
            _dag_before_edge_ids = {
                f"{str(edge.get('parent', ''))}->{str(edge.get('child', ''))}"
                for edge in (frontier_payload_before.get("edges") or [])
                if isinstance(edge, dict) and edge.get("parent") and edge.get("child")
            }
            current_frontier = update_theorem_frontier_full_state(
                config,
                state,
                worker_frontier_update,
                frontier_review,
                state.get("last_theorem_frontier_paper_review") if isinstance(state.get("last_theorem_frontier_paper_review"), dict) else None,
                state.get("last_theorem_frontier_nl_proof_review") if isinstance(state.get("last_theorem_frontier_nl_proof_review"), dict) else None,
                cycle=cycle,
            )
            ensure_dag_site(config)
            export_dag_frontier_snapshot(config, state)
            export_dag_frontier_cycle(
                config,
                state,
                _dag_before_node_ids,
                _dag_before_edge_ids,
                current_frontier,
                cycle=cycle,
                outcome=frontier_review.get("outcome", ""),
                reviewed_node_id=frontier_review.get("active_node_id", ""),
                worker_directive=worker_directive_summary(state),
            )
            export_dag_meta(config, state)
            metrics = current_frontier.get("metrics") if isinstance(current_frontier, dict) else {}
            escalation = current_frontier.get("escalation") if isinstance(current_frontier, dict) else {}
            review_event_content: Union[Dict[str, Any], Any] = {
                **frontier_review,
                "active_node_id": current_frontier.get("active_node_id"),
                "active_node_age": (metrics or {}).get("active_node_age"),
                "blocker_cluster_age": (metrics or {}).get("blocker_cluster_age"),
                "cone_purity": frontier_review.get("cone_purity"),
                "escalation_required": bool((escalation or {}).get("required")),
            }
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="theorem_frontier_review",
                actor="reviewer",
                target="supervisor",
                content=review_event_content,
                content_type="json",
            )

    decision["cycle"] = cycle
    decision["phase"] = phase
    state["last_review"] = decision
    review_log = state.setdefault("review_log", [])
    if not isinstance(review_log, list):
        review_log = []
        state["review_log"] = review_log
    if not decision_already_recorded:
        review_log.append(decision)
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
    commit_and_push_validated_cycle(
        config,
        state,
        phase,
        cycle,
        decision,
        validation_summary,
        frontier_review=frontier_review,
    )
    save_state(config, state)
    if not decision_already_recorded:
        append_supervisor_jsonl(config.state_dir / "review_log.jsonl", decision)

    print("\n===== reviewer decision =====")
    print(json.dumps(decision, indent=2, ensure_ascii=False))

    decision_value = decision["decision"]
    if decision_value not in {"ADVANCE_PHASE", "DONE"} and state.get("last_transition_error") is not None:
        state["last_transition_error"] = None
        save_state(config, state)
    if decision_value in {"ADVANCE_PHASE", "DONE"}:
        validate_theorem_frontier_terminal_repo_evidence(config, state)
    try:
        enforce_terminal_decision(config, phase, decision_value, validation_summary)
    except SupervisorError as exc:
        if decision_value in {"ADVANCE_PHASE", "DONE"}:
            state["last_transition_error"] = {
                "cycle": cycle,
                "phase": phase,
                "decision": decision_value,
                "error": str(exc),
                "recorded_at": timestamp_now(),
            }
            save_state(config, state)
            log_supervisor_warning(
                config,
                cycle=cycle,
                phase=phase,
                category="transition_blocked",
                message=str(exc),
                detail={
                    "decision": decision_value,
                    "validation_summary_path": str(validation_summary_path(config)),
                },
            )
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="transition_blocked",
                actor="supervisor",
                target="workflow",
                content=state["last_transition_error"],
                content_type="json",
                summary=f"Blocked {decision_value} from {phase}",
            )
            save_state(config, state)
            print(
                f"Phase transition blocked; staying in {phase} and continuing in the current phase: {exc}"
            )
            write_completed_cycle_checkpoint(
                config,
                state,
                cycle=cycle,
                completed_phase=phase,
                decision=decision,
                validation_summary=validation_summary,
            )
            if honor_cycle_boundary_restart_request(
                config,
                state,
                cycle=cycle,
                phase=current_phase(config, state),
                decision=decision,
            ):
                return True
            time.sleep(supervisor_sleep_seconds(config, policy))
            return False
        raise
    if state.get("last_transition_error") is not None:
        state["last_transition_error"] = None
        save_state(config, state)

    if (not decision_already_recorded) and should_consider_branching(config, state, phase, decision):
        preflight_error = branch_episode_preflight_error(config)
        if preflight_error:
            print(f"Skipping branch consideration for cycle {cycle}: {preflight_error}.")
        else:
            branch_strategy = run_branch_strategy_review(
                config,
                state,
                reviewer,
                phase,
                decision,
                policy=policy,
            )
            state["last_branch_consideration_cycle"] = cycle
            save_state(config, state)
            print("\n===== branch strategy decision =====")
            print(json.dumps(branch_strategy, indent=2, ensure_ascii=False))
            if branch_strategy["branch_decision"] == "BRANCH":
                if branching_enabled(config):
                    episode = create_branch_episode(
                        config,
                        state,
                        phase,
                        decision,
                        branch_strategy,
                        policy=policy,
                    )
                    write_completed_cycle_checkpoint(
                        config,
                        state,
                        cycle=cycle,
                        completed_phase=phase,
                        decision=decision,
                        validation_summary=validation_summary,
                    )
                    if honor_cycle_boundary_restart_request(
                        config,
                        state,
                        cycle=cycle,
                        phase=current_phase(config, state),
                        decision=decision,
                    ):
                        return True
                    print(
                        f"Created branch episode {episode['id']} with {len(episode['branches'])} branch(es). "
                        "Parent supervisor will monitor child branches until selection."
                    )
                    return False
                if can_propose_branch_replacement(state, config):
                    proposal = store_pending_branch_proposal(state, branch_strategy, cycle=cycle)
                    save_state(config, state)
                    write_completed_cycle_checkpoint(
                        config,
                        state,
                        cycle=cycle,
                        completed_phase=phase,
                        decision=decision,
                        validation_summary=validation_summary,
                    )
                    honor_cycle_boundary_restart_request(
                        config,
                        state,
                        cycle=cycle,
                        phase=current_phase(config, state),
                        decision=decision,
                        already_stopping=True,
                    )
                    print(
                        "Queued a parent-coordinated branch replacement proposal with "
                        f"{len(proposal.get('strategies', []))} strategy branch(es); "
                        "stopping this branch supervisor so the parent frontier monitor can evaluate it."
                    )
                    return True

    if phase == "proof_formalization" and decision_value != "STUCK" and stuck_recovery_attempts(state):
        clear_stuck_recovery(state)
        save_state(config, state)
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
            return True
        if (
            phase == "theorem_stating"
            and next_value == "proof_formalization"
            and theorem_frontier_phase(config) == "full"
        ):
            manifest = load_validated_paper_main_results_manifest(config)
            seeded_frontier = seed_theorem_frontier_from_main_results_manifest(
                config,
                state,
                manifest,
                cycle=cycle,
            )
        state["phase"] = next_value
        save_state(config, state)
        if (
            phase == "theorem_stating"
            and next_value == "proof_formalization"
            and theorem_frontier_phase(config) == "full"
        ):
            ensure_dag_site(config)
            export_dag_frontier_snapshot(config, state)
            export_dag_frontier_seed(config, seeded_frontier, cycle=cycle)
            export_dag_meta(config, state)
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=next_value,
                kind="theorem_frontier_seed",
                actor="supervisor",
                target="workflow",
                content={
                    "initial_active_node_id": seeded_frontier.get("active_node_id"),
                    "seed_node_ids": sorted(seeded_frontier.get("nodes", {}).keys()),
                    "seed_edge_count": len(seeded_frontier.get("edges", []) or []),
                    "source": str(paper_main_results_manifest_path(config)),
                },
                content_type="json",
            )
        if is_style_cleanup_phase(next_value):
            update_cleanup_last_good_commit(config, state, validation_summary)
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
        write_completed_cycle_checkpoint(
            config,
            state,
            cycle=cycle,
            completed_phase=phase,
            decision=decision,
            validation_summary=validation_summary,
        )
        if honor_cycle_boundary_restart_request(
            config,
            state,
            cycle=cycle,
            phase=current_phase(config, state),
            decision=decision,
        ):
            return True
        print(f"Advancing workflow phase: {phase} -> {next_value}")
        time.sleep(supervisor_sleep_seconds(config, policy))
        return False
    if decision_value == "NEED_INPUT":
        worker_handoff = state.get("last_worker_handoff")
        if not isinstance(worker_handoff, dict):
            raise SupervisorError("Cannot request human input without a worker handoff in state.")
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
        write_completed_cycle_checkpoint(
            config,
            state,
            cycle=cycle,
            completed_phase=phase,
            decision=decision,
            validation_summary=validation_summary,
        )
        honor_cycle_boundary_restart_request(
            config,
            state,
            cycle=cycle,
            phase=current_phase(config, state),
            decision=decision,
            already_stopping=True,
        )
        print(f"Stopping because reviewer requested human input. See {config.workflow.input_request_path}")
        return True
    if decision_value == "STUCK":
        if is_style_cleanup_phase(phase):
            restore_cleanup_last_good_commit(
                config,
                state,
                cycle=cycle,
                reason="cleanup reviewer decided the optional cleanup phase had stalled",
            )
            write_completed_cycle_checkpoint(
                config,
                state,
                cycle=cycle,
                completed_phase=phase,
                decision=decision,
                validation_summary=state.get("last_validation") if isinstance(state.get("last_validation"), dict) else validation_summary,
            )
            honor_cycle_boundary_restart_request(
                config,
                state,
                cycle=cycle,
                phase=current_phase(config, state),
                decision=decision,
                already_stopping=True,
            )
            print("Cleanup reviewer returned STUCK; restored last good commit and stopping as DONE.")
            return True
        if can_attempt_stuck_recovery(state, policy):
            suggestion = run_stuck_recovery_review(config, state, reviewer, phase, policy=policy)
            attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
            write_completed_cycle_checkpoint(
                config,
                state,
                cycle=cycle,
                completed_phase=phase,
                decision=decision,
                validation_summary=validation_summary,
            )
            if honor_cycle_boundary_restart_request(
                config,
                state,
                cycle=cycle,
                phase=current_phase(config, state),
                decision=decision,
            ):
                return True
            print(
                f"Reviewer returned STUCK; queued stuck-recovery attempt "
                f"{suggestion['attempt']}/{attempt_limit}."
            )
            time.sleep(supervisor_sleep_seconds(config, policy))
            return False
        attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
        write_completed_cycle_checkpoint(
            config,
            state,
            cycle=cycle,
            completed_phase=phase,
            decision=decision,
            validation_summary=validation_summary,
        )
        honor_cycle_boundary_restart_request(
            config,
            state,
            cycle=cycle,
            phase=current_phase(config, state),
            decision=decision,
            already_stopping=True,
        )
        print(
            "Stopping because reviewer returned STUCK after exhausting "
            f"{attempt_limit} stuck-recovery attempts."
        )
        return True
    if decision_value == "DONE":
        if is_style_cleanup_phase(phase):
            update_cleanup_last_good_commit(config, state, validation_summary)
            save_state(config, state)
        write_completed_cycle_checkpoint(
            config,
            state,
            cycle=cycle,
            completed_phase=phase,
            decision=decision,
            validation_summary=state.get("last_validation") if isinstance(state.get("last_validation"), dict) else validation_summary,
        )
        honor_cycle_boundary_restart_request(
            config,
            state,
            cycle=cycle,
            phase=current_phase(config, state),
            decision=decision,
            already_stopping=True,
        )
        print("Stopping because reviewer returned DONE.")
        return True

    write_completed_cycle_checkpoint(
        config,
        state,
        cycle=cycle,
        completed_phase=phase,
        decision=decision,
        validation_summary=validation_summary,
    )
    if honor_cycle_boundary_restart_request(
        config,
        state,
        cycle=cycle,
        phase=current_phase(config, state),
        decision=decision,
    ):
        return True
    time.sleep(supervisor_sleep_seconds(config, policy))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Lean formalization worker/reviewer supervisor")
    parser.add_argument("--config", required=True, help="Path to JSON config")
    args = parser.parse_args()

    config = load_config(Path(args.config).expanduser().resolve())
    if config.source_path is not None:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        (config.state_dir / "config_path.txt").write_text(str(config.source_path) + "\n", encoding="utf-8")
    install_supervisor_file_logging(config)
    check_dependencies(config)
    ensure_git_repository(config)
    installed_provider_context = install_personal_provider_context_files(
        Path.home(),
        [config.worker.provider, config.reviewer.provider],
    )
    state = load_state(config)
    policy_manager = PolicyManager(config)
    policy = policy_manager.reload(state=state, force=True, persist=True)
    phase = current_phase(config, state)
    if is_style_cleanup_phase(phase) and cleanup_last_good_commit(state) is None:
        update_cleanup_last_good_commit(config, state, state.get("last_validation"))
        save_state(config, state)
    ensure_repo_files(config, phase)
    ensure_chat_site(config)
    ensure_tmux_session(config)

    has_active_branch_episode = active_branch_episode(state) is not None
    if not has_active_branch_episode and not maybe_consume_human_input(config, state):
        print(f"Waiting for human input in: {config.workflow.human_input_path}")
        print(f"Input request written to: {config.workflow.input_request_path}")
        return 0

    if not has_active_branch_episode and state.get("pending_human_input_event"):
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

    if not has_active_branch_episode and pending_branch_proposal(state) and not branching_enabled(config):
        print("Waiting for parent supervisor to evaluate the pending branch-replacement proposal.")
        return 0

    worker = make_adapter("worker", config, state)
    reviewer = make_adapter("reviewer", config, state)
    paper_verifier = make_adapter("paper_verifier", config, state)
    nl_proof_verifier = make_adapter("nl_proof_verifier", config, state)

    if phase == "proof_formalization" and not has_active_branch_episode and can_attempt_stuck_recovery(state, policy):
        suggestion = run_stuck_recovery_review(config, state, reviewer, phase, policy=policy)
        attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
        print(
            f"Prepared stuck-recovery attempt {suggestion['attempt']}/{attempt_limit} "
            f"from prior STUCK review."
        )
    elif phase == "proof_formalization" and not has_active_branch_episode and has_unhandled_stuck_review(state):
        attempt_limit = stuck_recovery_attempt_limit(state, policy=policy)
        print(
            "Stopping because the current stuck episode already exhausted all "
            f"{attempt_limit} stuck-recovery attempts."
        )
        return 0

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
    print(
        "branching="
        f"max_current_branches={config.branching.max_current_branches} "
        f"evaluation_cycle_budget={policy.branching.evaluation_cycle_budget} "
        f"poll_seconds={policy.branching.poll_seconds}"
    )
    print(
        "codex_budget_pause="
        f"weekly_percent_left_threshold={policy.codex_budget_pause.weekly_percent_left_threshold} "
        f"poll_seconds={policy.codex_budget_pause.poll_seconds}"
    )
    print(f"policy_path={resolved_policy_path(config)}")

    while True:
        policy = policy_manager.reload(state=state, persist=True)
        phase = current_phase(config, state)
        if is_style_cleanup_phase(phase) and cleanup_last_good_commit(state) is None:
            update_cleanup_last_good_commit(config, state, state.get("last_validation"))
            save_state(config, state)
        if active_branch_episode(state):
            return monitor_active_branch_episode(config, state, reviewer, phase, policy_manager)
        ensure_repo_files(config, phase)
        if recover_interrupted_worker_state(config, state, phase):
            print(f"Recovered completed worker burst for cycle {int(state.get('cycle', 0))}; resuming reviewer stage.")
        reviewer_recovery = recover_interrupted_reviewer_state(config, state, reviewer, phase, policy)
        if reviewer_recovery is not None:
            if reviewer_recovery:
                break
            continue
        cycle, stage = determine_resume_cycle_and_stage(state)
        is_new_cycle = cycle > int(state.get("cycle", 0) or 0)
        if is_new_cycle:
            if config.max_cycles and cycle > config.max_cycles:
                print(f"Reached max_cycles={config.max_cycles}; stopping.")
                break
            state["cycle"] = cycle
        baseline_created = False
        if stage == "worker":
            if theorem_frontier_full_enabled(config, phase):
                sync_theorem_frontier_generated_files(config, state, ensure_active_proof=True)
            baseline_created = ensure_current_cycle_lean_baseline(config, state, cycle)
        if is_new_cycle or baseline_created:
            save_state(config, state)
        elif stage == "worker":
            print(f"Resuming interrupted worker burst for cycle {cycle}.")
        else:
            print(f"Resuming interrupted reviewer burst for cycle {cycle}.")

        if stage == "worker":
            policy = policy_manager.reload(state=state, persist=True)
            print(f"\n===== cycle {cycle}: worker | phase={phase} =====")
            cleanup_start_commit = cleanup_last_good_commit(state) if is_style_cleanup_phase(phase) else None
            worker_prompt = build_worker_prompt(
                config,
                state,
                phase,
                worker.needs_initial_run(),
                policy=policy,
            )
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
            def _validate_worker_burst(run: Dict[str, Any]) -> Dict[str, Any]:
                output = run["captured_output"].strip()
                cycle_handoff_path = worker_handoff_path(config, cycle)
                try:
                    handoff = load_json_artifact_with_fallback(
                        cycle_handoff_path,
                        output,
                        ("phase", "cycle", "status"),
                        fallback_paths=artifact_fallback_paths(
                            config,
                            cycle_handoff_path,
                            worker_handoff_path(config),
                            Path(run["artifact_path"]),
                        ),
                    )
                    handoff = validate_worker_handoff(phase, cycle, handoff)
                except SupervisorError as handoff_exc:
                    frontier_update_present = False
                    if theorem_frontier_enabled(config, phase):
                        try:
                            load_validated_theorem_frontier_worker_update(
                                config,
                                phase,
                                cycle,
                                output,
                            )
                            frontier_update_present = True
                        except SupervisorError:
                            frontier_update_present = False
                    raise SupervisorError(
                        "Worker exited without the required handoff JSON. "
                        f"{str(handoff_exc).strip()} "
                        + (
                            "A theorem-frontier update artifact was present; keep it or rewrite it as needed, but do not end the burst again without writing worker_handoff.json."
                            if frontier_update_present
                            else "Do not end the burst again without writing worker_handoff.json."
                        )
                    ) from handoff_exc
                persist_supervisor_artifact(handoff, cycle_handoff_path, worker_handoff_path(config))
                return validate_worker_cycle_artifacts(
                    config,
                    state,
                    phase,
                    cycle,
                    output,
                    handoff,
                )

            worker_run, worker_result = run_burst_with_validation(
                worker,
                cycle,
                worker_prompt,
                config=config,
                state=state,
                phase=phase,
                stage_label="worker burst",
                artifact_path=worker_handoff_path(config, cycle),
                clear_paths=[
                    worker_handoff_path(config, cycle),
                    worker_handoff_path(config),
                    theorem_frontier_worker_update_path(config, cycle),
                    theorem_frontier_worker_update_path(config),
                ],
                policy=policy,
                reuse_existing_window=not is_new_cycle,
                validate=_validate_worker_burst,
            )
            worker.mark_initialized()
            worker_terminal_output = worker_run["captured_output"].strip()
            worker_handoff = worker_result["worker_handoff"]
            frontier_update = worker_result.get("frontier_update")
            validation_summary = worker_result["validation_summary"]
            state.pop("last_worker_protocol_error", None)
            state["last_worker_output"] = worker_terminal_output
            state["last_worker_output_cycle"] = cycle
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
            if isinstance(frontier_update, dict):
                state["last_theorem_frontier_worker_update"] = frontier_update
                record_chat_event(
                    config,
                    state,
                    cycle=cycle,
                    phase=phase,
                    kind="theorem_frontier_update",
                    actor="worker",
                    target="supervisor",
                    content=frontier_update,
                    content_type="json",
                )
            else:
                state["last_theorem_frontier_worker_update"] = None
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
            if is_style_cleanup_phase(phase):
                if not validation_summary["build"]["ok"] or validation_summary["sorries"]["count"] != 0 or validation_summary["axioms"]["unapproved"]:
                    restore_cleanup_last_good_commit(
                        config,
                        state,
                        cycle=cycle,
                        reason="cleanup cycle ended without a fully valid proof state",
                    )
                    print("Cleanup cycle broke proof completeness; restored last good commit and stopping as DONE.")
                    break
                current_head = update_cleanup_last_good_commit(config, state, validation_summary)
                worker_status = str(worker_handoff.get("status", "")).strip().upper()
                if worker_status == "STUCK":
                    print("Cleanup worker reported STUCK; keeping the last good proof-complete commit and stopping as DONE.")
                    save_state(config, state)
                    break
                if cleanup_start_commit and current_head == cleanup_start_commit:
                    print("Cleanup cycle made no committed progress; keeping the last good proof-complete commit and stopping as DONE.")
                    save_state(config, state)
                    break
            save_state(config, state)
        else:
            worker_terminal_output = str(state.get("last_worker_output") or "").strip()
            worker_handoff = state.get("last_worker_handoff")
            validation_summary = state.get("last_validation")
            if not isinstance(worker_handoff, dict):
                raise SupervisorError(
                    f"Cannot resume reviewer cycle {cycle}: missing worker handoff in supervisor state."
                )
            if not isinstance(validation_summary, dict) or last_validation_cycle(state) != cycle:
                raise SupervisorError(
                    f"Cannot resume reviewer cycle {cycle}: missing validation summary for that cycle."
                )
            if theorem_frontier_full_enabled(config, phase) and not isinstance(state.get("last_theorem_frontier_worker_update"), dict):
                try:
                    frontier_update = load_validated_theorem_frontier_worker_update(
                        config,
                        phase,
                        cycle,
                        worker_terminal_output,
                    )
                except SupervisorError as frontier_exc:
                    raise SupervisorError(
                        f"Cannot resume reviewer cycle {cycle}: missing theorem-frontier worker update in state and "
                        f"could not recover it from {theorem_frontier_worker_update_path(config)}: {frontier_exc}"
                    ) from frontier_exc
                state["last_theorem_frontier_worker_update"] = frontier_update
                record_chat_event(
                    config,
                    state,
                    cycle=cycle,
                    phase=phase,
                    kind="theorem_frontier_update",
                    actor="worker",
                    target="supervisor",
                    content=frontier_update,
                    content_type="json",
                )
                save_state(config, state)

        policy = policy_manager.reload(state=state, persist=True)
        auto_frontier_result: Optional[Dict[str, Any]] = None
        state["last_theorem_frontier_close_validation_error"] = None
        if theorem_frontier_full_enabled(config, phase):
            worker_frontier_update = state.get("last_theorem_frontier_worker_update")
            if isinstance(worker_frontier_update, dict):
                requested_action = str(worker_frontier_update.get("requested_action") or "").strip().upper()
                if requested_action in {"CLOSE", "REFACTOR"}:
                    state["last_theorem_frontier_paper_review"] = None
                    state["last_theorem_frontier_nl_proof_review"] = None
                    auto_frontier_result = synthesize_deterministic_frontier_result(
                        config,
                        state,
                        phase,
                        cycle,
                        worker_frontier_update,
                    )
                    close_error = str(auto_frontier_result.get("close_validation_error") or "").strip() if isinstance(auto_frontier_result, dict) else ""
                    if close_error:
                        state["last_theorem_frontier_close_validation_error"] = {
                            "phase": phase,
                            "cycle": cycle,
                            "active_node_id": normalize_frontier_text(worker_frontier_update.get("active_node_id")),
                            "error": close_error,
                            "recorded_at": timestamp_now(),
                        }
                else:
                    paper_review = state.get("last_theorem_frontier_paper_review")
                    nl_proof_review = state.get("last_theorem_frontier_nl_proof_review")
                    if theorem_frontier_requires_paper_verifier(worker_frontier_update):
                        if not (isinstance(paper_review, dict) and int(paper_review.get("cycle", 0) or 0) == cycle):
                            paper_review = run_theorem_frontier_paper_verifier_review(
                                config,
                                state,
                                paper_verifier,
                                phase,
                                worker_terminal_output,
                                worker_handoff,
                                worker_frontier_update,
                                cycle=cycle,
                                policy=policy,
                            )
                        if paper_review.get("decision") in {"APPROVE", "APPROVE_WITH_CAVEAT"}:
                            if not (isinstance(nl_proof_review, dict) and int(nl_proof_review.get("cycle", 0) or 0) == cycle):
                                nl_proof_review = run_theorem_frontier_nl_proof_verifier_review(
                                    config,
                                    state,
                                    nl_proof_verifier,
                                    phase,
                                    worker_terminal_output,
                                    worker_handoff,
                                    worker_frontier_update,
                                    paper_review,
                                    cycle=cycle,
                                    policy=policy,
                                )
                        else:
                            state["last_theorem_frontier_nl_proof_review"] = None
                    else:
                        state["last_theorem_frontier_paper_review"] = None
                        state["last_theorem_frontier_nl_proof_review"] = None
                save_state(config, state)
        reviewer_terminal_output = ""
        if auto_frontier_result is None:
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
                policy=policy,
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
                policy=policy,
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
            def _validate_reviewer_burst(run: Dict[str, Any]) -> Dict[str, Any]:
                output = run["captured_output"].strip()
                cycle_review_path = reviewer_decision_path(config, cycle)
                dec = load_json_artifact_with_fallback(
                    cycle_review_path,
                    output,
                    ("phase", "cycle", "decision"),
                    fallback_paths=artifact_fallback_paths(
                        config,
                        cycle_review_path,
                        reviewer_decision_path(config),
                        Path(run["artifact_path"]),
                    ),
                )
                dec = validate_reviewer_decision(phase, cycle, dec)
                persist_supervisor_artifact(dec, cycle_review_path, reviewer_decision_path(config))
                reviewer_result = validate_reviewer_cycle_artifacts(
                    config,
                    state,
                    phase,
                    cycle,
                    output,
                    dec,
                )
                if theorem_frontier_full_enabled(config, phase):
                    frontier_review = reviewer_result.get("frontier_review")
                    worker_frontier_update = state.get("last_theorem_frontier_worker_update")
                    if not isinstance(worker_frontier_update, dict):
                        raise SupervisorError("Missing theorem-frontier worker update while validating reviewer application.")
                    if theorem_frontier_review_requires_admission_preflight(frontier_review):
                        preflight_theorem_frontier_full_state_update(
                            config,
                            state,
                            worker_frontier_update,
                            frontier_review,
                            cycle=cycle,
                        )
                return reviewer_result

            reviewer_run, reviewer_result = run_burst_with_validation(
                reviewer,
                cycle,
                reviewer_prompt,
                config=config,
                state=state,
                phase=phase,
                stage_label="reviewer burst",
                artifact_path=reviewer_decision_path(config, cycle),
                clear_paths=[
                    reviewer_decision_path(config, cycle),
                    reviewer_decision_path(config),
                    theorem_frontier_review_path(config, cycle),
                    theorem_frontier_review_path(config),
                ],
                policy=policy,
                reuse_existing_window=not is_new_cycle,
                validate=_validate_reviewer_burst,
            )
            reviewer.mark_initialized()
            reviewer_terminal_output = reviewer_run["captured_output"].strip()
        else:
            reviewer_result = auto_frontier_result
            decision_path = reviewer_decision_path(config, cycle)
            frontier_path = theorem_frontier_review_path(config, cycle)
            persist_supervisor_artifact(reviewer_result["decision"], decision_path, reviewer_decision_path(config))
            persist_supervisor_artifact(reviewer_result["frontier_review"], frontier_path, theorem_frontier_review_path(config))
            auto_action = normalize_frontier_text(reviewer_result["frontier_review"].get("assessed_action"))
            record_chat_event(
                config,
                state,
                cycle=cycle,
                phase=phase,
                kind="reviewer_prompt",
                actor="supervisor",
                target="reviewer",
                content=f"Supervisor auto-resolved this {auto_action or 'theorem-frontier'} cycle deterministically from the generated frontier checks.",
                content_type="text",
                summary=f"Supervisor auto-resolved {auto_action or 'theorem-frontier'} for cycle {cycle}",
            )
        if finalize_reviewer_cycle(
            config,
            state,
            reviewer,
            phase,
            cycle,
            reviewer_result,
            validation_summary,
            policy,
        ):
            break
        continue

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SupervisorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
