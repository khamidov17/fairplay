import AppKit

enum Allowlist {
    /// macOS security / permission dialogs. Never fight these for focus.
    /// Audit M-01: System Settings is no longer here -- it is needed only BEFORE the lock
    /// (Accessibility permission), and while locked its file pickers open anything.
    private static let systemDialogs: Set<String> = [
        "com.apple.SecurityAgent", "com.apple.SecurityAgentHelper", "com.apple.security.agent",
        "com.apple.UserNotificationCenter",
        "com.apple.coreautha.autha", "com.apple.loginwindow"
    ]

    /// Audit M-01: Finder is no longer kept while locked (it opens any file or app). It is
    /// quit like other apps; if macOS keeps relaunching it, LockdownController stops
    /// fighting and only keeps it from the front (see finderRelaunches).
    private static let system: Set<String> = [
        "com.apple.dock", "com.apple.systemuiserver", "com.apple.controlcenter",
        "com.apple.notificationcenterui", "com.apple.WindowManager",
        "com.apple.universalaccessd"
    ]
    static let finder = "com.apple.finder"

    /// Dev builds only: terminals (local server) and browsers (teacher dashboard) stay
    /// running while you test the lock on one Mac. They stay hidden behind the locked
    /// window. Release builds quit them like any other app.
    #if FAIRPLAY_DEV
    static let devKeep: Set<String> = [
        "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable",
        "com.apple.Safari", "com.google.Chrome", "org.mozilla.firefox", "company.thebrowser.Browser",
        "com.anthropic.claudefordesktop"
    ]
    #else
    static let devKeep: Set<String> = []
    #endif

    /// Always allowed in every build: VPN clients (campus networks need them, and quitting
    /// one drops the connection to the exam server).
    static let alwaysPrefixes: [String] = [
        "com.cisco.anyconnect", "com.cisco.secureclient", "com.cisco.Cisco-AnyConnect",
        "com.paloaltonetworks.GlobalProtect", "com.fortinet", "net.openvpn", "com.wireguard"
    ]

    /// Kept running regardless of the deny lists (VPN, and dev-build tools).
    /// Prefix match, so an app's helpers stay too ("com.anthropic.claudefordesktop.helper"
    /// etc.): killing only the helpers made the parent respawn them every few seconds,
    /// which kept the "apps must be closed" screen up for good.
    static func keeps(_ id: String) -> Bool {
        let lower = id.lowercased()
        guard !lower.isEmpty else { return false }
        return devKeep.contains { lower == $0.lowercased() || lower.hasPrefix($0.lowercased() + ".") }
            || alwaysPrefixes.contains { lower.hasPrefix($0.lowercased()) }
    }

    static func isSystemDialog(_ app: NSRunningApplication) -> Bool {
        if let id = app.bundleIdentifier, systemDialogs.contains(id) { return true }
        if let name = app.localizedName, name.contains("SecurityAgent") { return true }
        return false
    }

    /// May this app keep running while locked? Windowed (.regular) apps only when they are
    /// macOS itself (audit M-05: the never-wired "teacher-allowed apps" list is gone);
    /// background/menu-bar apps unless on the deny list
    /// (SystemChecks.deniedBundlePrefixes, checked separately).
    static func permits(_ app: NSRunningApplication) -> Bool {
        guard let id = app.bundleIdentifier else { return app.activationPolicy != .regular }
        if id == Bundle.main.bundleIdentifier { return true }
        if system.contains(id) || systemDialogs.contains(id) || keeps(id) { return true }
        return app.activationPolicy != .regular
    }
}

/// Integrity events. Written to ~/Library/Application Support/FairPlay/events.log
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
        case externalFinished(String)
        // audit 2026-10-12
        case processBlocked(String)
        case orphanScriptEnded(String)
        case vmDetected(String)
        case mirroredDisplay(Int)
        case remoteSession
        case tapLost
        case inputSourceBlocked(String)
        case syntheticInput(String)
        case pinMismatch(String)
        case previousRunKilled
        case offlineExit
        case offlineExitRejected

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
            case .externalFinished(let reason): return ("external_finished", reason)
            case .processBlocked(let s): return ("process_blocked", s)
            case .orphanScriptEnded(let s): return ("process_blocked", "background script ended: \(s)")
            case .vmDetected(let s): return ("vm_detected", s)
            case .mirroredDisplay(let n): return ("mirrored_display", "online displays=\(n)")
            case .remoteSession: return ("remote_session", "this login is not on the Mac's own screen")
            case .tapLost: return ("tap_lost", "keyboard lock was disabled")
            case .inputSourceBlocked(let s): return ("input_source_blocked", s)
            case .syntheticInput(let s): return ("synthetic_input", s)
            case .pinMismatch(let s): return ("tls_pin_mismatch", s)
            case .previousRunKilled: return ("previous_run_killed", "FairPlay ended while locked last time")
            // Own types: the server stores client "emergency_exit" as "client_emergency_exit" (audit S-06).
            case .offlineExit: return ("offline_exit", "offline invigilator code accepted")
            case .offlineExitRejected: return ("offline_exit_rejected", "wrong offline invigilator code")
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

    /// Thread-safe: the event tap callback and URLSession delegates run off the main thread.
    func record(_ e: Event) {
        let (type, detail) = e.pair
        let now = queue.sync { iso.string(from: Date()) }
        let text = "\(now) \(type) \(detail)\n"
        NSLog("[FairPlay] \(type) \(detail)")
        DebugLog.log("event", "\(type) \(detail)")
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
