#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <agent-tmux-session-name>" >&2
  exit 1
fi

SESSION_NAME=$(python3 - <<'PY' "$1"
import re
import sys

value = sys.argv[1].strip()
cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value)
cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
print(cleaned or "lean-agents")
PY
)

tmux attach -t "$SESSION_NAME"
