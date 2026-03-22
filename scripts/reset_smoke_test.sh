#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SMOKE_REPO="$ROOT_DIR/smoke_test_repo"

if [[ ! -d "$SMOKE_REPO" ]]; then
  echo "Smoke test repo not found: $SMOKE_REPO" >&2
  exit 1
fi

tmux kill-session -t lagent-smoke-agents 2>/dev/null || true
tmux kill-session -t lagent-smoke-supervisor 2>/dev/null || true

rm -rf \
  "$SMOKE_REPO/.agent-supervisor" \
  "$SMOKE_REPO/.lake" \
  "$SMOKE_REPO/build" \
  "$SMOKE_REPO/lake-packages"

rm -f \
  "$SMOKE_REPO/lake-manifest.json" \
  "$SMOKE_REPO/PLAN.md" \
  "$SMOKE_REPO/TASKS.md"

cat > "$SMOKE_REPO/SmokeTest/Basic.lean" <<'EOF'
namespace SmokeTest

theorem addZeroRight (n : Nat) : n + 0 = n := by
  sorry

theorem zeroAddLeft (n : Nat) : 0 + n = n := by
  simp

end SmokeTest
EOF

echo "Smoke test reset:"
echo "- tmux sessions stopped: lagent-smoke-agents, lagent-smoke-supervisor"
echo "- supervisor state and Lean build artifacts removed"
echo "- SmokeTest/Basic.lean restored to the original one-sorry state"
