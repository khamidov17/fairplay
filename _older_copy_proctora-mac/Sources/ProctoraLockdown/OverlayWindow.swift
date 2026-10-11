import AppKit

final class LockWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

/// One full-screen window with two looks:
///  - "permission" look: normal window level, asks for Accessibility. System
///    dialogs must be able to appear above it.
///  - "locked" look: maximum level, covers all screens, excluded from capture.
final class OverlayWindowController: NSWindowController {

    private var exitField: NSSecureTextField?
    private var statusLabel: NSTextField?
    private var permissionStack: NSStackView?
    private var lockedStack: NSStackView?
    private var blockingView: NSView?
    private var blockingLabel: NSTextField?

    convenience init() {
        let win = LockWindow(
            contentRect: NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1440, height: 900),
            styleMask: [.borderless], backing: .buffered, defer: false)
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        win.sharingType = .none                       // not in screenshots / screen share
        win.isOpaque = true
        win.backgroundColor = NSColor(calibratedRed: 0.08, green: 0.09, blue: 0.12, alpha: 1)
        win.hidesOnDeactivate = false
        win.isReleasedWhenClosed = false
        win.level = .normal
        self.init(window: win)
        buildPermissionLook()
        buildLockedLook()
        showPermissionLook()
    }

    // MARK: - Geometry / focus

    func coverAllScreens() {
        guard let win = window else { return }
        let union = NSScreen.screens.reduce(NSRect.zero) { $0.union($1.frame) }
        win.setFrame(union.isEmpty ? (NSScreen.main?.frame ?? win.frame) : union, display: true)
    }

    func present() { coverAllScreens(); showWindow(nil); takeFocus() }

    func takeFocus() {
        window?.makeKeyAndOrderFront(nil)
        window?.orderFrontRegardless()
        if let f = exitField, window?.firstResponder !== f.currentEditor() {
            window?.makeFirstResponder(f)
        }
    }

    // MARK: - Looks

    func showPermissionLook() {
        window?.level = .normal
        permissionStack?.isHidden = false
        lockedStack?.isHidden = true
    }

    func enterLockedLook() {
        window?.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.maximumWindow)))
        permissionStack?.isHidden = true
        lockedStack?.isHidden = false
        coverAllScreens()
    }

    func setPermissionStatus(_ text: String) { statusLabel?.stringValue = text }

    private func buildPermissionLook() {
        guard let content = window?.contentView else { return }
        let title = label("One permission before the exam can lock", size: 28, weight: .bold)
        let body = label("""
        Proctora needs Accessibility access to block system shortcuts \
        (Spotlight, screenshots, Mission Control) during the exam.

        1. In the window that opened, find "ProctoraLockdown" and turn it ON.
        2. If it isn't listed, click + and choose ~/code/proctora-mac/ProctoraLockdown.app
        3. Come back here. The exam locks automatically within a second.
        """, size: 15, weight: .regular, color: NSColor(white: 0.85, alpha: 1))
        body.alignment = .left
        body.preferredMaxLayoutWidth = 620

        let status = label("Waiting for Accessibility permission…", size: 14, weight: .semibold,
                           color: NSColor(calibratedRed: 1, green: 0.78, blue: 0.4, alpha: 1))
        statusLabel = status

        let open = NSButton(title: "Open Accessibility settings", target: self, action: #selector(openAccessibility))
        open.bezelStyle = .rounded

        let stack = NSStackView(views: [title, body, status, open])
        stack.orientation = .vertical; stack.spacing = 20; stack.alignment = .centerX
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: content.centerYAnchor)
        ])
        permissionStack = stack
    }

    private func buildLockedLook() {
        guard let content = window?.contentView else { return }
        let badge = label("LOCKED", size: 16, weight: .semibold, color: .white)
        badge.wantsLayer = true
        badge.layer?.backgroundColor = NSColor(calibratedRed: 0.18, green: 0.29, blue: 0.85, alpha: 1).cgColor
        badge.layer?.cornerRadius = 10

        let title = label("Exam in progress", size: 30, weight: .bold)
        let sub = label("Lockdown test build 0.2 - try to escape. Everything you try is logged.",
                        size: 15, weight: .regular, color: NSColor(white: 0.8, alpha: 1))

        let field = NSSecureTextField()
        field.placeholderString = "Invigilator exit code"
        field.alignment = .center
        field.font = .systemFont(ofSize: 15)
        field.target = self; field.action = #selector(tryUnlock)
        field.translatesAutoresizingMaskIntoConstraints = false
        exitField = field

        let end = NSButton(title: "End lockdown", target: self, action: #selector(tryUnlock))
        end.bezelStyle = .rounded

        let stack = NSStackView(views: [badge, title, sub, field, end])
        stack.orientation = .vertical; stack.spacing = 18; stack.alignment = .centerX
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: content.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: content.centerYAnchor),
            field.widthAnchor.constraint(equalToConstant: 260),
            badge.widthAnchor.constraint(equalToConstant: 140),
            badge.heightAnchor.constraint(equalToConstant: 34)
        ])
        lockedStack = stack
    }

    // MARK: - Actions

    @objc private func openAccessibility() {
        EventTap.requestTrust()
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func tryUnlock() {
        let entered = exitField?.stringValue ?? ""
        if entered == ExitCode.current {
            EventLog.shared.record(.unlockAccepted)
            LockdownController.shared.unlock()
            NSApp.terminate(nil)
        } else {
            EventLog.shared.record(.unlockRejected)
            exitField?.stringValue = ""
            NSSound.beep()
        }
    }

    // MARK: - Blocking banner

    func showBlocking(reason: String) {
        if blockingView != nil { blockingLabel?.stringValue = reason; return }
        guard let content = window?.contentView else { return }
        let v = NSView(frame: content.bounds)
        v.autoresizingMask = [.width, .height]
        v.wantsLayer = true
        v.layer?.backgroundColor = NSColor(calibratedRed: 0.55, green: 0.15, blue: 0.05, alpha: 0.97).cgColor
        let l = label(reason, size: 22, weight: .semibold)
        l.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(l)
        content.addSubview(v)
        NSLayoutConstraint.activate([
            l.centerXAnchor.constraint(equalTo: v.centerXAnchor),
            l.centerYAnchor.constraint(equalTo: v.centerYAnchor)
        ])
        blockingView = v; blockingLabel = l
    }

    func hideBlocking() {
        blockingView?.removeFromSuperview()
        blockingView = nil; blockingLabel = nil
    }

    private func label(_ text: String, size: CGFloat, weight: NSFont.Weight,
                       color: NSColor = .white) -> NSTextField {
        let l = NSTextField(labelWithString: text)
        l.font = .systemFont(ofSize: size, weight: weight)
        l.textColor = color
        l.alignment = .center
        l.maximumNumberOfLines = 0
        l.translatesAutoresizingMaskIntoConstraints = false
        return l
    }
}
