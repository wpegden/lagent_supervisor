#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import supervisor


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ask a live supervisor to stop cleanly after the next completed cycle boundary."
    )
    parser.add_argument("--config", required=True, help="Path to supervisor config JSON")
    parser.add_argument("--reason", default="", help="Optional note recorded with the restart request")
    args = parser.parse_args()

    config = supervisor.load_config(Path(args.config).expanduser().resolve())
    payload = supervisor.request_cycle_boundary_restart(config, reason=args.reason)
    print(
        "Requested cycle-boundary restart via "
        f"{supervisor.cycle_boundary_restart_request_path(config)}"
    )
    if payload.get("reason"):
        print(f"Reason: {payload['reason']}")
    print(f"Requested at: {payload.get('requested_at') or '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
