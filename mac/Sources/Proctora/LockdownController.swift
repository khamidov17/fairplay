import AppKit
import Carbon.HIToolbox

/// What the lock needs from the window that shows the exam.
protocol LockSurface: AnyObject {
    func enterLockedMode()
    func exitLockedMode()
    func takeFocus()
    func coverAllScreens()
    func showBlocking(reason: String)
    func hideBlocking()
}

/// Owns the locked state. Defences, strongest first (same as lockdown 0.2):
///  1. Session-wide CGEventTap swallows every escape shortcut. Lock refuses to start without it.
///  2. Kiosk presentation options: Dock/menu bar hidden, Cmd-Tab, force-quit, hide, logout disabled.
///  3. Focus guard re-asserts our window every 300 ms (never hides or kills other apps,
///     never fights system dialogs). Newly launched apps are closed.
///  4. Space-change, display-change and clipboard watchers.
final class LockdownController {

    static let shared = LockdownController()

    private(set) var isLocked = false
    private var observers: [NSObjectProtocol] = []
    private var focusTimer: Timer?
    private var pasteboardTimer: Timer?
    private var safetyWork: DispatchWorkItem?
    private var lastChangeCount = 0

    weak var surface: LockSurface?
    /// Called after the lock ends for any reason (submit, emergency exit, safety timer).
    var onUnlocked: (() -> Void)?

    /// Returns false (and stays unlocked) when Accessibility is not granted.
    @discardableResult
    func lock() -> Bool {
        guard !isLocked else { return true }
        guard EventTap.shared.start() else {
            EventLog.shared.record(.tapUnavailable)
            return false
        }
        isLocked = true
        EventLog.shared.record(.lockStarted)
        if IsSecureEventInputEnabled() { EventLog.shared.record(.secureInputOn) }

        NSApp.presentationOptions = [
            .hideDock, .hideMenuBar, .disableAppleMenu,
            .disableProcessSwitching, .disableForceQuit,
            .disableSessionTermination, .disableHideApplication
        ]

        surface?.enterLockedMode()
        bringToFront()
        installWorkspaceWatchers()
        installFocusGuard()
        clearAndGuardPasteboard()
        checkDisplays()

        let minutes = Config.safetyUnlockMinutes
        if minutes > 0 {
            let work = DispatchWorkItem { [weak self] in
                guard let self, self.isLocked else { return }
                EventLog.shared.record(.safetyUnlock)
                self.unlock()
            }
            safetyWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + minutes * 60, execute: work)
        }
        return true
    }

    func unlock() {
        guard isLocked else { return }
        isLocked = false
        safetyWork?.cancel(); safetyWork = nil
        EventTap.shared.stop()
        NSApp.presentationOptions = []
        observers.forEach {
            NSWorkspace.shared.notificationCenter.removeObserver($0)
            NotificationCenter.default.removeObserver($0)
        }
        observers.removeAll()
        focusTimer?.invalidate(); focusTimer = nil
        pasteboardTimer?.invalidate(); pasteboardTimer = nil
        surface?.hideBlocking()
        surface?.exitLockedMode()
        EventLog.shared.record(.lockEnded)
        onUnlocked?()
    }

    private func bringToFront() {
        NSApp.activate(ignoringOtherApps: true)
        surface?.takeFocus()
    }

    private func installFocusGuard() {
        let t = Timer(timeInterval: 0.3, repeats: true) { [weak self] _ in
            guard let self, self.isLocked else { return }
            guard let front = NSWorkspace.shared.frontmostApplication else { return }
            if front.processIdentifier == ProcessInfo.processInfo.processIdentifier { return }
            if Allowlist.isSystemDialog(front) { return }
            self.bringToFront()
        }
        RunLoop.main.add(t, forMode: .common)
        focusTimer = t
    }

    private func installWorkspaceWatchers() {
        let ws = NSWorkspace.shared.notificationCenter
        let nc = NotificationCenter.default
        let me = ProcessInfo.processInfo.processIdentifier

        observers.append(ws.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                        object: nil, queue: .main) { [weak self] note in
            guard let self, self.isLocked,
                  let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  app.processIdentifier != me,
                  !Allowlist.isSystemDialog(app) else { return }
            EventLog.shared.record(.otherAppForeground(app.localizedName ?? app.bundleIdentifier ?? "unknown"))
            self.bringToFront()
        })

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

        observers.append(ws.addObserver(forName: NSWorkspace.activeSpaceDidChangeNotification,
                                        object: nil, queue: .main) { [weak self] _ in
            guard let self, self.isLocked else { return }
            EventLog.shared.record(.spaceChanged)
            self.surface?.coverAllScreens()
            self.bringToFront()
        })

        observers.append(nc.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                        object: nil, queue: .main) { [weak self] _ in
            guard let self, self.isLocked else { return }
            self.checkDisplays()
        })
    }

    private func checkDisplays() {
        let count = NSScreen.screens.count
        if count > 1 {
            EventLog.shared.record(.extraDisplay(count))
            surface?.showBlocking(reason: "Only one screen is allowed.\nUnplug the extra display to continue.")
        } else {
            surface?.hideBlocking()
        }
        surface?.coverAllScreens()
    }

    private func clearAndGuardPasteboard() {
        let pb = NSPasteboard.general
        pb.clearContents()
        lastChangeCount = pb.changeCount
        let t = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self, self.isLocked else { return }
            if pb.changeCount != self.lastChangeCount {
                pb.clearContents()
                self.lastChangeCount = pb.changeCount
            }
        }
        RunLoop.main.add(t, forMode: .common)
        pasteboardTimer = t
    }
}
