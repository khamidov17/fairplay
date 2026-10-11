#!/bin/bash
# Double-click: builds Proctora.app and opens it. Start the server first (Run Server.command).
cd "$(dirname "$0")"
bash ./build.sh || { echo; echo "BUILD FAILED - see above"; read -n1 -p "Press a key to close"; exit 1; }
echo "==> Opening FairPlay (Invigilator exit while locked: Ctrl-Opt-Cmd-E, code 1234)"
open -W ./FairPlay.app
echo; echo "---- last events ----"
tail -n 40 "$HOME/Library/Application Support/FairPlay/events.log" 2>/dev/null
read -n1 -p "Press a key to close"
