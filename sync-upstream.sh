#!/usr/bin/env bash
# OpenBiliClaw upstream sync
set -euo pipefail
cd /home/node/.openclaw/workspace/repos/OpenBiliClaw_litedocker

echo "=== fetch upstream ==="
git fetch upstream 2>&1 | tail -3

LOCAL=$(git rev-parse HEAD)
UPSTREAM=$(git rev-parse upstream/main)
if [ "$LOCAL" = "$UPSTREAM" ]; then
  echo "Already up to date."
  exit 0
fi

BEFORE=$(git log --oneline -1 HEAD)
echo "LOCAL: $BEFORE"
echo "UPSTREAM: $(git log --oneline -1 upstream/main)"

echo "=== merge upstream/main ==="
if git merge upstream/main --no-edit 2>&1; then
  echo "=== merge succeeded, push ==="
  git push origin main 2>&1 | tail -3
  echo "SYNC_OK"
else
  echo "=== MERGE CONFLICT ==="
  echo "--- conflicting files ---"
  git diff --name-only --diff-filter=U
  echo "--- first conflict markers ---"
  git diff --check 2>&1 || true
  echo "--- diff stats ---"
  git diff --stat
  echo ""
  echo "SYNC_CONFLICT: conflicts left in working tree for manual resolution"
  echo "Run: git merge --abort  to cancel, or resolve conflicts then git commit"
  exit 1
fi
