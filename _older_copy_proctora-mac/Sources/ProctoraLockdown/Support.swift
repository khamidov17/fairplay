import AppKit

enum Allowlist {
    /// macOS security / permission dialogs. We never fight these for focus:
    /// fighting them is what made build 0.1 feel frozen.
    private static let systemDialogs: Set<String> = [
        "com.apple.SecurityAgent",
        "com.apple.SecurityAgentHelper",
        "com.apple.security.agent",
        "com.apple.UserNotificationCenter",
        "com.apple.systempreferences",      // only reachable while we ask for Accessibility
        "com.apple.coreautha.autha",
        "com.apple.loginwindow"
    ]

    /// OS agents we must leave alone.
    private static let system: Set<String> = [
        "com.apple.dock", "com.apple.systemuiserver", "com.apple.controlcenter",
        "com.apple.notificationcenterui", "com.apple.WindowManager",
        "com.apple.universalaccessd", "com.apple.finder"
    ]

    /// Teacher-allowed apps for this exam (bundle IDs). Empty in the test build.
    static var allowed: Set<String> = []

    static func isSystemDialog(_ app: NSRunningApplication) -> Bool {
        guard let id = app.bundleIdentifier else { return false }
        if systemDialogs.contains(id) { return true }
        if let name = app.localizedName, name.contains("SecurityAgent") { return true }
        return false
    }

    static func permits(_ app: NSRunningApplication) -> Bool {
        guard let id = app.bundleIdentifier else { return app.activationPolicy != .regular }
        if id == Bundle.main.bundleIdentifier { return true }
        if system.contains(id) || systemDialogs.contains(id) || allowed.contains(id) { return true }
        return app.activationPolicy != .regular
    }
}

/// Code the invigilator types to end lockdown. Fixed for the test build; the
/// real app receives a per-session code from the server.
enum ExitCode {
    static let current = "1234"
}

/// Append-only log at ~/Library/Application Support/ProctoraLockdown/events.log
final class EventLog {
    static let shared = EventLog()

    enum Event {
        case lockStarted, lockEnded, tapUnavailable, safetyUnlock
        case blockedShortcut(String)
        case otherAppForeground(String)
        case otherAppLaunched(String)
        case spaceChanged
        case extraDisplay(Int)
        case unlockAccepted, unlockRejected

        var line: String {
            switch self {
            case .lockStarted: return "lock_started"
            case .lockEnded: return "lock_ended"
            case .tapUnavailable: return "tap_unavailable_accessibility_not_granted"
            case .safetyUnlock: return "safety_unlock_timer"
            case .blockedShortcut(let s): return "blocked_shortcut \(s)"
            case .otherAppForeground(let s): return "other_app_foreground \(s)"
            case .otherAppLaunched(let s): return "other_app_launched_and_closed \(s)"
            case .spaceChanged: return "space_changed_snapped_back"
            case .extraDisplay(let n): return "extra_display count=\(n)"
            case .unlockAccepted: return "unlock_accepted"
            case .unlockRejected: return "unlock_rejected"
            }
        }
    }

    private let url: URL
    private let iso = ISO8601DateFormatter()
    private let queue = DispatchQueue(label: "proctora.eventlog")

    private init() {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        let folder = dir.appendingPathComponent("ProctoraLockdown", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        url = folder.appendingPathComponent("events.log")
    }

    /// Thread-safe: the event tap callback runs on the tap's run loop.
    func record(_ e: Event) {
        let text = "\(iso.string(from: Date())) \(e.line)\n"
        NSLog("[Proctora] \(e.line)")
        queue.async { [url] in
            guard let data = text.data(using: .utf8) else { return }
            if let h = try? FileHandle(forWritingTo: url) {
                h.seekToEndOfFile(); h.write(data); try? h.close()
            } else {
                try? data.write(to: url)
            }
        }
    }

    var logPath: String { url.path }
}
