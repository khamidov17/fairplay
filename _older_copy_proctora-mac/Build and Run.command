#!/bin/bash
cd "$(dirname "$0")"
LOG="$HOME/Library/Application Support/ProctoraLockdown/events.log"
./build.sh || { echo; echo "BUILD FAILED - send the errors above to Claude."; read -n 1 -s -r -p "Press any key to close"; exit 1; }
echo
echo "Launching. Exit code 1234. Safety auto-unlock after 3 minutes."
echo "First time: grant Accessibility when asked, then come back to the app."
: > "$LOG" 2>/dev/null || true
open -W ./ProctoraLockdown.app
echo; echo "===== EVENT LOG (send this to Claude) ====="; cat "$LOG" 2>/dev/null
echo; read -n 1 -s -r -p "Press any key to close"
