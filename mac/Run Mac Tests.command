#!/bin/bash
# Double-click: runs the Mac app's unit tests (audit T-07). Output also in mac/test.log.
# Uses Swift Testing, which ships with the Command Line Tools (no full Xcode needed).
cd "$(dirname "$0")"
# After build.sh has built the app, SwiftPM sometimes forgets where the Swift Testing
# macro plugin lives ("plugin for module 'TestingMacros' not found"), so name it.
EXTRA=()
PLUG="$(xcode-select -p 2>/dev/null)/usr/lib/swift/host/plugins/testing"
[ -d "$PLUG" ] && EXTRA=(-Xswiftc -plugin-path -Xswiftc "$PLUG")
swift test ${EXTRA[@]+"${EXTRA[@]}"} 2>&1 | tee test.log
status=${PIPESTATUS[0]}
if [ "$status" -ne 0 ] && grep -qE "module 'Testing'|module dependency: 'Testing'" test.log; then
  echo
  echo "Swift Testing is missing: update the Command Line Tools (xcode-select --install) or install Xcode."
fi
echo "EXIT $status" >> test.log
read -n1 -p "Press a key to close"
exit "$status"
