#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import supervisor
from lagent_supervisor.frontier import validate_theorem_frontier_worker_update_full
from lagent_supervisor.storage import JsonFile


def load_config_for_args(args: argparse.Namespace) -> supervisor.Config:
    if args.config:
        return supervisor.load_config(Path(args.config))
    repo_path = Path(args.repo or ".").expanduser().resolve()
    config_path = repo_path / ".agent-supervisor" / "config_path.txt"
    if config_path.exists():
        return supervisor.load_config(Path(config_path.read_text(encoding="utf-8").strip()))
    raise supervisor.SupervisorError(
        "Could not determine config path automatically. Pass --config /abs/path/to/config.json."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a theorem-frontier CLOSE with the supervisor's exact checker. "
        "For CLOSE/REFACTOR generally, prefer verify_theorem_frontier_action.py."
    )
    parser.add_argument("--config", help="Path to the supervisor JSON config.")
    parser.add_argument("--repo", help="Repo root; used only to find .agent-supervisor/config_path.txt when --config is omitted.")
    parser.add_argument("--worker-update", required=True, help="Path to the worker theorem_frontier_update.json artifact.")
    args = parser.parse_args()

    try:
        config = load_config_for_args(args)
        state = supervisor.load_state(config)
        phase = supervisor.current_phase(config, state)
        worker_update_path = Path(args.worker_update).expanduser()
        if not worker_update_path.is_absolute():
            worker_update_path = (config.repo_path / worker_update_path).resolve()
        raw_update = JsonFile.load(worker_update_path, None)
        if not isinstance(raw_update, dict):
            raise supervisor.SupervisorError(f"Worker update is missing or invalid: {worker_update_path}")
        cycle = int(raw_update.get("cycle", state.get("cycle", 0)) or 0)
        worker_update = validate_theorem_frontier_worker_update_full(phase, cycle, raw_update)
        if str(worker_update.get("requested_action") or "").strip().upper() != "CLOSE":
            raise supervisor.SupervisorError(
                "verify_theorem_frontier_close.py only accepts requested_action = 'CLOSE'. "
                "Use verify_theorem_frontier_action.py for REFACTOR."
            )
        report = supervisor.verify_theorem_frontier_deterministic_action(
            config,
            state,
            phase,
            cycle,
            worker_update,
        )
        print(json.dumps({"ok": True, **report}, indent=2, ensure_ascii=False))
        return 0
    except supervisor.SupervisorError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
