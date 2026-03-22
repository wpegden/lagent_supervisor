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
REVIEW_LOG="$REPO_PATH/.agent-supervisor/review_log.jsonl"

mkdir -p "$LOG_DIR"

echo "Watching logs in $LOG_DIR"
echo "Following live aggregate logs plus reviewer decisions"
echo

tail -n 80 -F \
  "$LOG_DIR/worker.all.ansi.log" \
  "$LOG_DIR/reviewer.all.ansi.log" \
  "$REVIEW_LOG"
