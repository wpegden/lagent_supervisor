#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import supervisor


def _supervisor_session_name(config: supervisor.Config) -> str:
    return supervisor.sanitize_tmux_session_name(f"{config.chat.repo_name}-supervisor")


def _monitor_session_name(config: supervisor.Config) -> str:
    return supervisor.sanitize_tmux_session_name(f"{config.chat.repo_name}-monitor")


def _stop_live_sessions(config: supervisor.Config) -> None:
    for session_name in (
        _supervisor_session_name(config),
        config.tmux.session_name,
        _monitor_session_name(config),
    ):
        supervisor.tmux_cmd("kill-session", "-t", session_name, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a completed-cycle checkpoint for a supervisor run.")
    parser.add_argument("--config", required=True, help="Path to supervisor config JSON")
    parser.add_argument("--cycle", type=int, help="Restore the checkpoint written after this completed cycle")
    parser.add_argument(
        "--after-phase",
        help="Restore the latest checkpoint written after completing this phase, e.g. paper_check",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available completed-cycle checkpoints and exit",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = supervisor.load_config(config_path)

    checkpoints = supervisor.list_cycle_checkpoints(config)
    if args.list:
        if not checkpoints:
            print("No completed-cycle checkpoints found.")
            return 0
        for item in checkpoints:
            print(
                f"cycle={int(item.get('cycle', 0) or 0)} "
                f"completed_phase={item.get('completed_phase') or '?'} "
                f"phase_after={item.get('phase_after') or '?'} "
                f"decision={item.get('decision') or '?'} "
                f"git_head={(str(item.get('git_head') or '')[:12] or 'none')}"
            )
        return 0

    if (args.cycle is None) == (args.after_phase is None):
        parser.error("Provide exactly one of --cycle or --after-phase, unless using --list.")

    _stop_live_sessions(config)
    checkpoint = supervisor.restore_cycle_checkpoint(
        config,
        cycle=args.cycle,
        after_phase=args.after_phase,
    )
    print(
        f"Restored completed-cycle checkpoint cycle={int(checkpoint.get('cycle', 0) or 0)} "
        f"completed_phase={checkpoint.get('completed_phase') or '?'} "
        f"phase_after={checkpoint.get('phase_after') or '?'} "
        f"git_head={(str(checkpoint.get('git_head') or '')[:12] or 'none')}"
    )
    print(
        "Restart the supervisor with:\n"
        f"  python3 supervisor.py --config {config_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
