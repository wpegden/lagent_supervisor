#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <config.json> [tmux-session-name]" >&2
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

ACTUAL_SESSION=$(tmux new-session -d -P -F '#{session_name}' -s "$SESSION_NAME" -n supervisor bash -lc \
  "cd '$ROOT_DIR' && python3 supervisor.py --config '$CONFIG_PATH'; echo; echo '[supervisor exited]'; exec bash")

echo "Started supervisor in tmux session: $ACTUAL_SESSION"
echo "Attach with: tmux attach -t $ACTUAL_SESSION"
echo "Agent bursts will appear in the agent tmux session configured in your JSON file."
