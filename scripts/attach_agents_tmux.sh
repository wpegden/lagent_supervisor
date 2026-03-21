#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <agent-tmux-session-name>" >&2
  exit 1
fi

tmux attach -t "$1"
