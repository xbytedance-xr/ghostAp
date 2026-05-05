#!/usr/bin/env bash
# check_shim_deadline.sh — CI check for deprecated shim file cleanup
#
# Purpose: Ensures deprecated shim modules are removed after their deadline.
# Fails (exit 1) if the current UTC date >= 2026-06-01 AND any shim file still exists.
#
# Usage:
#   bash scripts/check_shim_deadline.sh
#
# CI Integration:
#   Add as a lint step in your CI pipeline:
#     - name: Check deprecated shim cleanup
#       run: bash scripts/check_shim_deadline.sh

set -euo pipefail

DEADLINE="2026-06-01"
TODAY=$(date -u +%Y-%m-%d)

# Date comparison (lexicographic works for ISO 8601 dates)
if [[ "$TODAY" < "$DEADLINE" ]]; then
    echo "✅ Shim deadline ($DEADLINE) not yet reached (today: $TODAY). Skipping check."
    exit 0
fi

echo "⏰ Shim deadline reached ($DEADLINE). Checking for stale shim files..."

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHIM_FILES=(
    "src/card/session_config.py"
    "src/card/session_factory.py"
    "src/card/session_rotator.py"
    "src/card/static_session.py"
    "src/card/delivery_tracker.py"
    "src/card/action_dispatch.py"
    "src/card/action_ids.py"
    "src/card/action_router.py"
    "src/card/timer_manager.py"
    "src/card/timer_scheduler.py"
    "src/card/_session_ttl.py"
)

FOUND=0
for shim in "${SHIM_FILES[@]}"; do
    if [[ -f "$REPO_ROOT/$shim" ]]; then
        echo "  ❌ STALE: $shim (should have been removed by $DEADLINE)"
        FOUND=$((FOUND + 1))
    fi
done

if [[ $FOUND -gt 0 ]]; then
    echo ""
    echo "ERROR: $FOUND deprecated shim file(s) still exist after deadline $DEADLINE."
    echo "ACTION: Delete these files and migrate all callers to canonical import paths."
    echo "        See .Memory/Backlog.md (B001) for details."
    exit 1
fi

echo "✅ All deprecated shim files have been removed. Clean!"
exit 0
