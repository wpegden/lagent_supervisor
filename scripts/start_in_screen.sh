#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <config.json> [screen-session-name]" >&2
  exit 1
fi

CONFIG_PATH=$(python3 - <<'PY' "$1"
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
)
SESSION_NAME="${2:-lean-supervisor}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

screen -S "$SESSION_NAME" -L -Logfile "$ROOT_DIR/supervisor.screen.log" -dm bash -lc \
  "cd '$ROOT_DIR' && python3 supervisor.py --config '$CONFIG_PATH'; echo; echo '[supervisor exited]'; exec bash"

echo "Started supervisor in GNU Screen session: $SESSION_NAME"
echo "Attach supervisor with: screen -r $SESSION_NAME"
echo "Agent bursts will appear in tmux session configured in your JSON file."
