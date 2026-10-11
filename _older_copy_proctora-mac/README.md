# Proctora - macOS lockdown, test build 0.2

## Run
    cd ~/code/proctora-mac
    ./build.sh
    open ProctoraLockdown.app        # or double-click "Build and Run.command"

First launch: the app asks for **Accessibility** permission and shows a
how-to screen. Turn ProctoraLockdown ON in System Settings > Privacy &
Security > Accessibility, come back - it locks by itself within a second.

Exit: type `1234` + Enter. Safety: the test build auto-unlocks after 3 minutes
(`safetyUnlockAfter` in LockdownController.swift; set to 0 for real exams).

## What changed from 0.1
- **System-wide key tap (CGEventTap).** Build 0.1 only saw keys delivered to
  its own window, so Spotlight, screenshots and Cmd-Tab were handled by macOS
  first. The tap now sees every key before the system does and swallows:
  every Cmd-combo, Ctrl-arrows/Ctrl-Space (Spaces, Mission Control), F3/F4/F11/F12,
  Ctrl-Opt-anything, globe/emoji. Lock will NOT start without the tap.
- **Focus bug fixed.** 0.1 hid other apps and fought them (log showed Claude
  coming to the front 8x and a frozen mouse). 0.2 never hides or kills a
  running app; a 300 ms guard just puts our window back on top. System
  security dialogs are exempt so we never fight a permission prompt.
- **Space / Mission Control gestures**: trackpad swipes can't be blocked by any
  app, but a Space change is detected instantly, we snap back, and it's logged.
- Permission screen uses a normal window level so system dialogs show above it;
  the exam screen uses the maximum level.
- Thread-safe log; events from the tap callback no longer race the UI.

## Test checklist (send the log after)
1. Cmd-Space, Cmd-Tab, Cmd-Q, Cmd-H, Cmd-Opt-Esc
2. Cmd-Shift-3, Cmd-Shift-4, Cmd-Shift-5
3. Ctrl-Up, F3, F4; three/four-finger swipe up and sideways
4. Click where the Dock / menu bar would be; hot corners
5. Plug in a second display
6. Wrong code x2, then 1234

Expected log: `blocked_shortcut` for every key combo above, `space_changed_snapped_back`
for a swipe, `extra_display` for the monitor, no `other_app_foreground` lines.

## Known limits (unchanged - need the next layer)
- Trackpad gestures and hot corners are detected + reverted, not prevented.
- Hard power-off, SSH `kill`, or another admin can always end the app. The real
  system catches this with server heartbeats (missing heartbeat = flag).
- Rebuilding with ad-hoc signing resets the Accessibility grant. Create a
  self-signed "Proctora Dev" code-signing cert in Keychain Access once and
  build.sh uses it automatically.
- Full prevention of gestures and force-quit needs Apple's Automatic Assessment
  Configuration entitlement (what Bluebook uses) or MDM. Apply for it.
