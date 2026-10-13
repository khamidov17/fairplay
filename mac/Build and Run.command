#!/bin/bash
# Double-click: builds a DEV FairPlay.app and opens it. Start the server first (Run Server.command).
# For exams use a release build: see the top of build.sh.
cd "$(dirname "$0")"
# Output also goes to mac/build.log so build errors can be read from the project folder.
bash ./build.sh 2>&1 | tee build.log
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo; echo "BUILD FAILED - see above (also in mac/build.log)"; read -n1 -p "Press a key to close"; exit 1; }
echo "==> Opening FairPlay (invigilator exit while locked: Ctrl-Opt-Cmd-E, code shown in the teacher's live room; no server/error: \"Offline code...\", dev builds accept 0000)"
open -W ./FairPlay.app
echo; echo "---- last events ----"
tail -n 40 "$HOME/Library/Application Support/FairPlay/events.log" 2>/dev/null
read -n1 -p "Press a key to close"
