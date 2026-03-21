#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <repo-path>" >&2
  exit 1
fi

REPO_PATH=$(python3 - <<'PY' "$1"
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
)
LOG_DIR="$REPO_PATH/.agent-supervisor/logs"

mkdir -p "$LOG_DIR"

echo "Watching logs in $LOG_DIR"
echo

tail -n 80 -F \
  "$LOG_DIR/worker.latest.ansi.log" \
  "$LOG_DIR/reviewer.latest.ansi.log" \
  "$LOG_DIR/worker.all.ansi.log" \
  "$LOG_DIR/reviewer.all.ansi.log"
