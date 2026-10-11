import AppKit
import Carbon.HIToolbox

/// Owns the locked state. Order of defences, strongest first:
///  1. Session-wide CGEventTap (EventTap) swallows every escape shortcut
///     before macOS acts on it. Lock refuses to start without it.
///  2. Kiosk presentation options hide Dock/menu bar and disable Cmd-Tab,
///     force-quit, hide, logout.
///  3. A focus guard re-asserts our full-screen window every 300 ms if anything
///     else is in front. It never hides or kills other apps (that caused the
///     focus fight in build 0.1); it just puts us back on top and logs.
///  4. Space-change, display-change and clipboard watchers.
final class LockdownController {

    static let shared = LockdownController()

    private(set) var isLocked = false
    private var localMonitor: Any?
    private var observers: [NSObjectProtocol] = []
    private var focusTimer: Timer?
    private var pasteboardTimer: Timer?
    private var lastChangeCount = 0
    private var lockStartedAt = Date()

    weak var overlay: OverlayWindowController?

    /// Test-build safety: auto-unlock after this many seconds. Set to 0 for real exams.
    var safetyUnlockAfter: TimeInterval = 180

    // MARK: - Lock / unlock

    /// Returns false (and does nothing) if the event tap can't start, i.e.
    /// Accessibility isn't granted. The caller shows the permission screen.
    @discardableResult
    func lock() -> Bool {
        guard !isLocked else { return true }
        guard EventTap.shared.start() else {
            EventLog.shared.record(.tapUnavailable)
            return false
        }

        isLocked = true
        lockStartedAt = Date()
        EventLog.shared.record(.lockStarted)

        NSApp.presentationOptions = [
            .hideDock, .hideMenuBar, .disableAppleMenu,
            .disableProcessSwitching, .disableForceQuit,
            .disableSessionTermination, .disableHideApplication
        ]

        overlay?.enterLockedLook()
        bringToFront()

        installLocalMonitor()
        installWorkspaceWatchers()
        installFocusGuard()
        clearAndGuardPasteboard()
        checkDisplays()

        if safetyUnlockAfter > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + safetyUnlockAfter) { [weak self] in
                guard let self, self.isLocked else { return }
                EventLog.shared.record(.safetyUnlock)
                self.unlock()
                NSApp.terminate(nil)
            }
        }
        return true
    }

    func unlock() {
        guard isLocked else { return }
        isLocked = false
        NSApp.presentationOptions = []
        EventTap.shared.stop()
        if let m = localMonitor { NSEvent.removeMonitor(m); localMonitor = nil }
        observers.forEach {
            NSWorkspace.shared.notificationCenter.removeObserver($0)
            NotificationCenter.default.removeObserver($0)
        }
        observers.removeAll()
        focusTimer?.invalidate(); focusTimer = nil
        pasteboardTimer?.invalidate(); pasteboardTimer = nil
        EventLog.shared.record(.lockEnded)
    }

    // MARK: - Focus

    private func bringToFront() {
        NSApp.activate(ignoringOtherApps: true)
        overlay?.takeFocus()
    }

    /// Every 300 ms: if we are not the frontmost app, come back. Exempt system
    /// security dialogs so we never fight a password/permission prompt.
    private func installFocusGuard() {
        focusTimer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { [weak self] _ in
            guard let self, self.isLocked else { return }
            guard let front = NSWorkspace.shared.frontmostApplication else { return }
            if front.processIdentifier == ProcessInfo.processInfo.processIdentifier { return }
            if Allowlist.isSystemDialog(front) { return }
            self.bringToFront()
        }
        RunLoop.main.add(focusTimer!, forMode: .common)
    }

    // MARK: - Local monitor (second layer behind the tap)

    private func installLocalMonitor() {
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
            guard let self, self.isLocked else { return event }
            let f = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            if f.contains(.command) { NSSound.beep(); return nil }
            return event
        }
    }

    // MARK: - Workspace watchers

    private func installWorkspaceWatchers() {
        let ws = NSWorkspace.shared.notificationCenter
        let nc = NotificationCenter.default

        // Something else came to the front: log it; the focus guard brings us back.
        observers.append(ws.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                        object: nil, queue: .main) { [weak self] note in
            guard let self, self.isLocked,
                  let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  app.processIdentifier != ProcessInfo.processInfo.processIdentifier,
                  !Allowlist.isSystemDialog(app) else { return }
            EventLog.shared.record(.otherAppForeground(app.localizedName ?? app.bundleIdentifier ?? "unknown"))
            self.bringToFront()
        })

        // A regular app launched after lock began: close it and log.
        observers.append(ws.addObserver(forName: NSWorkspace.didLaunchApplicationNotification,
                                        object: nil, queue: .main) { [weak self] note in
            guard let self, self.isLocked,
                  let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  app.activationPolicy == .regular,
                  !Allowlist.permits(app) else { return }
            EventLog.shared.record(.otherAppLaunched(app.localizedName ?? app.bundleIdentifier ?? "unknown"))
            app.terminate()
            self.bringToFront()
        })

        // Trackpad swipe to another Space / Mission Control: snap back and log.
        observers.append(ws.addObserver(forName: NSWorkspace.activeSpaceDidChangeNotification,
                                        object: nil, queue: .main) { [weak self] _ in
            guard let self, self.isLocked else { return }
            EventLog.shared.record(.spaceChanged)
            self.overlay?.coverAllScreens()
            self.bringToFront()
        })

        // Display added/removed.
        observers.append(nc.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                        object: nil, queue: .main) { [weak self] _ in
            self?.checkDisplays()
        })
    }

    private func checkDisplays() {
        let count = NSScreen.screens.count
        if count > 1 {
            EventLog.shared.record(.extraDisplay(count))
            overlay?.showBlocking(reason: "Only one screen is allowed.\nUnplug the extra display to continue.")
        } else {
            overlay?.hideBlocking()
        }
        overlay?.coverAllScreens()
    }

    // MARK: - Pasteboard

    private func clearAndGuardPasteboard() {
        let pb = NSPasteboard.general
        pb.clearContents()
        lastChangeCount = pb.changeCount
        pasteboardTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self, self.isLocked else { return }
            if pb.changeCount != self.lastChangeCount {
                pb.clearContents()
                self.lastChangeCount = pb.changeCount
            }
        }
    }
}
