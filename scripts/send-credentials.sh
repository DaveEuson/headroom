#!/usr/bin/env bash
# Run this on the computer where Claude Code is logged in (NOT on the Pi).
# It copies your Claude Code OAuth credentials to the Pi so the tracker can
# read your usage limits.
#
#   ./scripts/send-credentials.sh pi@raspberrypi.local
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <user@pi-hostname>"
  echo "Example: $0 pi@raspberrypi.local"
  exit 1
fi
TARGET="$1"

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

if [ "$(uname)" = "Darwin" ]; then
  # macOS stores Claude Code credentials in the Keychain.
  if ! security find-generic-password -s "Claude Code-credentials" -w > "$TMP" 2>/dev/null; then
    echo "Couldn't read Claude Code credentials from the macOS Keychain."
    echo "Make sure you're logged in to Claude Code on this Mac (run: claude)."
    exit 1
  fi
elif [ -f "$HOME/.claude/.credentials.json" ]; then
  cp "$HOME/.claude/.credentials.json" "$TMP"
else
  echo "Couldn't find ~/.claude/.credentials.json."
  echo "Make sure you're logged in to Claude Code on this machine (run: claude)."
  exit 1
fi

echo "Sending credentials to $TARGET ..."
ssh "$TARGET" "mkdir -p ~/.claude-tracker && chmod 700 ~/.claude-tracker"
scp -q "$TMP" "$TARGET:~/.claude-tracker/credentials.json"
ssh "$TARGET" "chmod 600 ~/.claude-tracker/credentials.json"

echo "Done. The dashboard should show live data within a couple of minutes."
