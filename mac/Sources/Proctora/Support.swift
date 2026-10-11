import AppKit

enum Allowlist {
    /// macOS security / permission dialogs. Never fight these for focus.
    private static let systemDialogs: Set<String> = [
        "com.apple.SecurityAgent", "com.apple.SecurityAgentHelper", "com.apple.security.agent",
        "com.apple.UserNotificationCenter", "com.apple.systempreferences",
        "com.apple.coreautha.autha", "com.apple.loginwindow"
    ]

    private static let system: Set<String> = [
        "com.apple.dock", "com.apple.systemuiserver", "com.apple.controlcenter",
        "com.apple.notificationcenterui", "com.apple.WindowManager",
        "com.apple.universalaccessd", "com.apple.finder"
    ]

    /// Teacher-allowed apps for this exam (bundle IDs).
    static var allowed: Set<String> = []

    static func isSystemDialog(_ app: NSRunningApplication) -> Bool {
        if let id = app.bundleIdentifier, systemDialogs.contains(id) { return true }
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

/// Integrity events. Written to ~/Library/Application Support/Proctora/events.log
/// and forwarded to the exam page (which sends them to the server).
final class EventLog {
    static let shared = EventLog()

    enum Event {
        case lockStarted, lockEnded, tapUnavailable, safetyUnlock, secureInputOn
        case blockedShortcut(String)
        case otherAppForeground(String)
        case otherAppLaunched(String)
        case spaceChanged
        case extraDisplay(Int)
        case blockedNavigation(String)
        case emergencyExitAccepted(String), emergencyExitRejected
        case externalFinished(String)

        /// (type, detail) as sent to the server.
        var pair: (String, String) {
            switch self {
            case .lockStarted: return ("lock_started", "")
            case .lockEnded: return ("lock_ended", "")
            case .tapUnavailable: return ("tap_unavailable", "accessibility not granted")
            case .safetyUnlock: return ("safety_unlock", "dev safety timer")
            case .secureInputOn: return ("secure_input_on", "another app holds secure input")
            case .blockedShortcut(let s): return ("blocked_shortcut", s)
            case .otherAppForeground(let s): return ("other_app_foreground", s)
            case .otherAppLaunched(let s): return ("other_app_launched", s)
            case .spaceChanged: return ("space_changed", "")
            case .extraDisplay(let n): return ("extra_display", "count=\(n)")
            case .blockedNavigation(let s): return ("blocked_navigation", s)
            case .emergencyExitAccepted(let how): return ("emergency_exit", "invigilator code accepted (\(how))")
            case .emergencyExitRejected: return ("emergency_exit_rejected", "wrong code")
            case .externalFinished(let reason): return ("external_finished", reason)
            }
        }
    }

    /// Called on the main thread for every event.
    var onEvent: ((_ type: String, _ detail: String, _ time: String) -> Void)?

    private let url: URL
    private let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private let queue = DispatchQueue(label: "proctora.eventlog")

    private init() {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        let folder = dir.appendingPathComponent("FairPlay", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        url = folder.appendingPathComponent("events.log")
    }

    /// Thread-safe: the event tap callback runs off the main thread.
    func record(_ e: Event) {
        let (type, detail) = e.pair
        let now = iso.string(from: Date())
        let text = "\(now) \(type) \(detail)\n"
        NSLog("[FairPlay] \(type) \(detail)")
        queue.async { [url] in
            guard let data = text.data(using: .utf8) else { return }
            if let h = try? FileHandle(forWritingTo: url) {
                h.seekToEndOfFile(); h.write(data); try? h.close()
            } else {
                try? data.write(to: url)
            }
        }
        DispatchQueue.main.async { [weak self] in self?.onEvent?(type, detail, now) }
    }

    var logPath: String { url.path }
}
