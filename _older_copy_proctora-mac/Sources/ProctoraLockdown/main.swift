import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    var overlay: OverlayWindowController?
    private var permissionPoll: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.regular)

        let overlay = OverlayWindowController()
        self.overlay = overlay
        LockdownController.shared.overlay = overlay
        overlay.present()
        NSApp.activate(ignoringOtherApps: true)

        print("""
        ------------------------------------------------------------
        Proctora lockdown 0.2
        Exit code: \(ExitCode.current)   Safety auto-unlock: \(Int(LockdownController.shared.safetyUnlockAfter)) s
        Log: \(EventLog.shared.logPath)
        ------------------------------------------------------------
        """)

        // Lock immediately if Accessibility is already granted; otherwise show
        // the permission screen and poll until it is.
        if LockdownController.shared.lock() { return }
        EventTap.requestTrust()
        permissionPoll = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] t in
            if LockdownController.shared.lock() {
                t.invalidate(); self?.permissionPoll = nil
            } else {
                self?.overlay?.setPermissionStatus("Waiting for Accessibility permission… (checked \(Date().formatted(date: .omitted, time: .standard)))")
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        LockdownController.shared.isLocked ? .terminateCancel : .terminateNow
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        overlay?.takeFocus(); return true
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
