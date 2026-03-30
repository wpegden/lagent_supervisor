#!/bin/bash
# Usage: ./scripts/watch_worker.sh <repo_path> [interval_seconds]
# Monitors the active worker burst by tracking file changes and process activity.

REPO="${1:?Usage: $0 <repo_path> [interval_seconds]}"
INTERVAL="${2:-5}"

while true; do
    clear
    echo "=== Worker Monitor: $(basename "$REPO") — $(date +%H:%M:%S) ==="
    echo

    # Find the claude/codex/gemini process under the supervisor scopes
    SCOPE_DIR="$REPO/.agent-supervisor/scopes"
    PIDS=$(pgrep -f "$SCOPE_DIR" 2>/dev/null)
    if [ -n "$PIDS" ]; then
        echo "Agent processes:"
        ps -o pid,pcpu,rss,etime,comm -p $PIDS 2>/dev/null | head -5
    else
        echo "No agent process running."
    fi
    echo

    # Most recently modified files (excluding .lake, .git, scopes)
    echo "Last 5 file changes:"
    find "$REPO" \
        -not -path "*/.lake/*" \
        -not -path "*/.git/*" \
        -not -path "*/.agent-supervisor/scopes/*" \
        -not -path "*/.agent-supervisor/logs/*" \
        -type f -printf '%T+ %p\n' 2>/dev/null \
        | sort -r | head -5 | while read ts path; do
            rel="${path#$REPO/}"
            echo "  $(echo "$ts" | cut -d. -f1)  $rel"
        done
    echo

    # Supervisor state
    STATE="$REPO/.agent-supervisor/state.json"
    if [ -f "$STATE" ]; then
        PHASE=$(python3 -c "import json; s=json.load(open('$STATE')); print(s.get('phase','?'))" 2>/dev/null)
        CYCLE=$(python3 -c "import json; s=json.load(open('$STATE')); print(s.get('cycle','?'))" 2>/dev/null)
        echo "Supervisor: phase=$PHASE cycle=$CYCLE"
    fi

    # Lean file line counts
    echo
    echo "Lean files:"
    find "$REPO" -name "*.lean" \
        -not -path "*/.lake/*" \
        -not -path "*/.agent-supervisor/*" \
        2>/dev/null | sort | while read f; do
            rel="${f#$REPO/}"
            lines=$(wc -l < "$f")
            echo "  $lines  $rel"
        done

    sleep "$INTERVAL"
done
