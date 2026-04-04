#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import supervisor


def tmux_has_session(name: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", name],
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0


def tmux_pane_field(name: str, field: str) -> str:
    proc = subprocess.run(
        ["tmux", "list-panes", "-t", name, "-F", field],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""


def tmux_current_command(name: str) -> str:
    pane_command = tmux_pane_field(name, "#{pane_current_command}")
    pane_pid_text = tmux_pane_field(name, "#{pane_pid}")
    if pane_command not in {"bash", "sh"} or not pane_pid_text:
        return pane_command
    try:
        pane_pid = int(pane_pid_text)
    except ValueError:
        return pane_command
    proc = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm="],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return pane_command
    children: dict[int, list[tuple[int, str]]] = {}
    for line in proc.stdout.splitlines():
        try:
            pid_s, ppid_s, comm = line.strip().split(None, 2)
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        children.setdefault(ppid, []).append((pid, comm))
    stack = [pane_pid]
    seen: set[int] = set()
    effective = pane_command
    shell_names = {"bash", "sh"}
    while stack:
        parent = stack.pop()
        if parent in seen:
            continue
        seen.add(parent)
        for child_pid, comm in children.get(parent, []):
            if comm not in shell_names:
                effective = comm
            stack.append(child_pid)
    return effective


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.open("a", encoding="utf-8").write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def format_age_seconds(value: float) -> str:
    if not math.isfinite(value):
        return "inf"
    return str(int(value))


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def tail_text(path: Path, max_lines: int = 80) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])


def latest_cycle_log(state_dir: Path, prefix: str, cycle: int) -> Path | None:
    if cycle <= 0:
        return None
    candidate = state_dir / "logs" / f"{prefix}-cycle-{cycle:04d}.ansi.log"
    return candidate if candidate.exists() else None


def detect_budget_issue(text: str) -> bool:
    hay = text.lower()
    return any(pattern in hay for pattern in supervisor.BUDGET_ERROR_PATTERNS)


def detect_handoff_issue(text: str) -> bool:
    hay = text.lower()
    patterns = (
        "artifact missing required keys",
        "could not parse json artifact",
        "could not parse json object",
        "missing worker handoff",
        "missing validation summary",
        "handoff parse issue",
    )
    return any(pattern in hay for pattern in patterns)


def detect_build_issue(text: str) -> bool:
    hay = text.lower()
    patterns = (
        "`lake build` is failing",
        "build failure",
        "validation error detected",
        "cannot complete proof_formalization while `lake build` is failing",
        "cannot advance from theorem_stating while `lake build` is failing",
        "theorem-frontier cone file guard failed",
        "syntax checks failed",
    )
    return any(pattern in hay for pattern in patterns)


def diagnose_run(
    state_dir: Path,
    state: dict[str, Any],
    session_alive: bool,
    current_command: str,
    state_age: float,
    activity_age: float,
    stall_restart_seconds: int,
) -> dict[str, Any]:
    cycle = int(state.get("cycle", 0) or 0)
    phase = str(state.get("phase", "") or "")
    worker_log_path = latest_cycle_log(state_dir, "worker", cycle)
    last_review = state.get("last_review") if isinstance(state.get("last_review"), dict) else {}
    last_review_cycle = int(last_review.get("cycle", 0) or 0) if isinstance(last_review, dict) else 0
    last_validation = state.get("last_validation") if isinstance(state.get("last_validation"), dict) else {}
    last_validation_cycle = int(last_validation.get("cycle", 0) or 0) if isinstance(last_validation, dict) else 0
    reviewer_cycle = cycle if last_validation_cycle == cycle and last_review_cycle < cycle else max(last_review_cycle, 1)
    reviewer_log_path = latest_cycle_log(state_dir, "reviewer", reviewer_cycle)
    worker_tail = tail_text(worker_log_path, max_lines=60) if worker_log_path else ""
    reviewer_tail = tail_text(reviewer_log_path, max_lines=80) if reviewer_log_path else ""
    last_transition_error = (
        state.get("last_transition_error")
        if isinstance(state.get("last_transition_error"), dict)
        else {}
    )
    reason = str(last_review.get("reason", "") or "")
    next_prompt = str(last_review.get("next_prompt", "") or "")
    review_text = "\n".join(part for part in [reason, next_prompt, reviewer_tail] if part)
    status = "healthy"
    summary = "run is active"
    if not session_alive:
        status = "supervisor_dead"
        summary = "supervisor session is missing"
    elif current_command != "python3":
        status = "supervisor_not_running"
        summary = f"supervisor pane command is {current_command or 'none'}"
    elif cycle <= 0 and not phase:
        status = "starting_up"
        summary = "supervisor is starting and state is not populated yet"
    elif last_transition_error:
        status = "transition_blocked"
        summary = str(last_transition_error.get("error", "") or "phase transition is blocked")
    elif state_age >= stall_restart_seconds and activity_age >= stall_restart_seconds:
        status = "stalled"
        summary = "state and cycle activity are both stale"
    elif detect_budget_issue(worker_tail) or detect_budget_issue(reviewer_tail):
        status = "provider_capacity"
        summary = "provider rate-limit/capacity issue detected"
    elif "paper_main_results.json" in review_text and (
        "missing required keys" in review_text.lower() or "malformed" in review_text.lower()
    ):
        status = "manifest_schema_issue"
        summary = "paper_main_results.json is malformed or missing required theorem-frontier keys"
    elif "theorem-frontier" in review_text.lower() and "missing required" in review_text.lower():
        status = "frontier_schema_issue"
        summary = "theorem-frontier artifact/schema issue detected"
    elif detect_handoff_issue("\n".join(part for part in [reason, next_prompt, worker_tail, reviewer_tail] if part)):
        status = "handoff_issue"
        summary = "worker/reviewer handoff parse issue detected"
    elif detect_build_issue("\n".join(part for part in [reason, next_prompt, worker_tail, reviewer_tail] if part)):
        status = "build_issue"
        summary = "build failure or validation error detected"
    return {
        "status": status,
        "summary": summary,
        "last_review_reason": reason,
        "last_review_next_prompt": next_prompt,
        "last_transition_error": last_transition_error,
        "worker_log_path": str(worker_log_path) if worker_log_path else "",
        "reviewer_log_path": str(reviewer_log_path) if reviewer_log_path else "",
        "worker_log_tail": worker_tail,
        "reviewer_log_tail": reviewer_tail,
    }


def write_debug_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def latest_cycle_activity_mtime(state_dir: Path, cycle: int) -> float:
    candidates: list[Path] = []
    if cycle > 0:
        cycle_tag = f"{cycle:04d}"
        log_dir = state_dir / "logs"
        runtime_dir = state_dir / "runtime"
        prompt_dir = state_dir / "prompts"
        for pattern in (
            f"*cycle-{cycle_tag}*",
            f"*cycle-{cycle}*",
        ):
            candidates.extend(log_dir.glob(pattern))
            candidates.extend(runtime_dir.glob(pattern))
            candidates.extend(prompt_dir.glob(pattern))
    candidates.extend(
        [
            state_dir / "worker_handoff.json",
            state_dir / "review_handoff.json",
            state_dir / "paper_verifier_handoff.json",
            state_dir / "validation_summary.json",
            state_dir / "theorem_frontier.json",
        ]
    )
    mtimes = [path.stat().st_mtime for path in candidates if path.exists()]
    return max(mtimes) if mtimes else 0.0


def is_terminal_state(state: dict[str, Any], max_cycles: int) -> bool:
    last_review = state.get("last_review")
    if isinstance(last_review, dict) and str(last_review.get("decision", "")).strip() == "DONE":
        return True
    cycle = int(state.get("cycle", 0) or 0)
    return max_cycles > 0 and cycle >= max_cycles


def restart_supervisor(config_path: Path, session_name: str, log_path: Path) -> None:
    if tmux_has_session(session_name):
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)
        log_line(log_path, f"killed stale supervisor session {session_name}")
    subprocess.run(
        [str(ROOT / "scripts" / "start_in_tmux.sh"), str(config_path), session_name],
        cwd=ROOT,
        check=True,
    )
    log_line(log_path, f"started supervisor session {session_name} from {config_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor and restart a supervisor tmux session.")
    parser.add_argument("--config", required=True, help="Path to supervisor config JSON")
    parser.add_argument("--session", required=True, help="Supervisor tmux session name")
    parser.add_argument("--initial-poll-seconds", type=int, default=300)
    parser.add_argument("--steady-poll-seconds", type=int, default=3600)
    parser.add_argument("--early-cycle-threshold", type=int, default=3)
    parser.add_argument("--stall-restart-seconds", type=int, default=14400)
    parser.add_argument("--monitor-hours", type=float, default=24.0)
    args = parser.parse_args()

    config = supervisor.load_config(Path(args.config))
    state_path = config.state_dir / "state.json"
    monitor_log = config.state_dir / "monitor.log"
    status_path = config.state_dir / "monitor_status.json"
    debug_dir = config.state_dir / "monitor_debug"
    deadline = time.time() + args.monitor_hours * 3600.0

    log_line(monitor_log, f"monitor started for session={args.session}")

    while time.time() < deadline:
        state = load_state(state_path)
        cycle = int(state.get("cycle", 0) or 0)
        phase = str(state.get("phase", "")).strip() or "unknown"
        restart_requested = supervisor.cycle_boundary_restart_request_path(config).exists()
        session_alive = tmux_has_session(args.session)
        current_command = tmux_current_command(args.session) if session_alive else ""
        state_age = time.time() - state_path.stat().st_mtime if state_path.exists() else float("inf")
        activity_mtime = latest_cycle_activity_mtime(config.state_dir, cycle)
        activity_age = time.time() - activity_mtime if activity_mtime else float("inf")
        diagnosis = diagnose_run(
            config.state_dir,
            state,
            session_alive,
            current_command,
            state_age,
            activity_age,
            args.stall_restart_seconds,
        )

        payload = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session_name": args.session,
            "session_alive": session_alive,
            "current_command": current_command,
            "phase": phase,
            "cycle": cycle,
            "restart_requested": restart_requested,
            "state_age_seconds": state_age,
            "activity_age_seconds": activity_age,
            "diagnosis": diagnosis,
        }
        status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        write_debug_snapshot(debug_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.json", payload)
        log_line(
            monitor_log,
            f"check phase={phase} cycle={cycle} session_alive={session_alive} "
            f"current_command={current_command or 'none'} state_age={format_age_seconds(state_age)}s "
            f"activity_age={format_age_seconds(activity_age)}s diagnosis={diagnosis['status']}: {diagnosis['summary']}",
        )

        if not session_alive:
            if restart_requested:
                log_line(monitor_log, "cycle-boundary restart requested; supervisor is intentionally stopped")
                time.sleep(args.initial_poll_seconds)
                continue
            if is_terminal_state(state, config.max_cycles):
                log_line(monitor_log, "terminal state detected with no live session; monitor exiting")
                return 0
            restart_supervisor(Path(args.config), args.session, monitor_log)
            time.sleep(args.initial_poll_seconds)
            continue

        if current_command != "python3":
            if restart_requested:
                log_line(
                    monitor_log,
                    f"cycle-boundary restart requested; supervisor pane command is {current_command or 'none'} and will not be auto-restarted",
                )
                time.sleep(args.initial_poll_seconds)
                continue
            if is_terminal_state(state, config.max_cycles):
                log_line(
                    monitor_log,
                    f"terminal state detected with supervisor pane command {current_command or 'none'}; monitor exiting",
                )
                return 0
            log_line(
                monitor_log,
                f"supervisor session alive but command is {current_command or 'none'}; restarting immediately",
            )
            restart_supervisor(Path(args.config), args.session, monitor_log)
            time.sleep(args.initial_poll_seconds)
            continue

        if state_age >= args.stall_restart_seconds and activity_age >= args.stall_restart_seconds:
            log_line(
                monitor_log,
                f"state stale for {int(state_age)}s with no cycle activity for {int(activity_age)}s; restarting",
            )
            restart_supervisor(Path(args.config), args.session, monitor_log)
            time.sleep(args.initial_poll_seconds)
            continue

        poll_seconds = (
            args.initial_poll_seconds
            if cycle < args.early_cycle_threshold
            else args.steady_poll_seconds
        )
        time.sleep(poll_seconds)

    log_line(monitor_log, "monitor deadline reached; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
