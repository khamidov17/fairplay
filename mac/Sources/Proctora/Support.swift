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

    /// macOS itself: kept running (never in front of the exam).
    private static let system: Set<String> = [
        "com.apple.dock", "com.apple.systemuiserver", "com.apple.controlcenter",
        "com.apple.notificationcenterui", "com.apple.WindowManager",
        "com.apple.universalaccessd"
    ]
    /// Finder: macOS relaunches it at once when quit, so it is hidden and kept behind
    /// the exam instead (the focus guard never lets it in front).
    static let finder = "com.apple.finder"

    /// Dev builds only: terminals (local server) and browsers (teacher dashboard) stay
    /// running while you test the lock on one Mac. They stay hidden behind the locked
    /// window. Release builds close them like any other app the teacher didn't allow.
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

    /// Never allowed, whatever the exam says: terminals (a script left running there can
    /// kill FairPlay) and web browsers (the whole internet). Lowercase bundle-id prefixes.
    static let neverAllowed: [String] = [
        "com.apple.terminal", "com.googlecode.iterm2", "dev.warp", "io.alacritty", "net.kovidgoyal.kitty",
        "com.github.wez.wezterm", "co.zeit.hyper", "com.mitchellh.ghostty",
        "com.apple.safari", "com.google.chrome", "org.mozilla.firefox", "company.thebrowser",
        "com.microsoft.edgemac", "com.brave.browser", "com.operasoftware", "com.vivaldi",
        "org.chromium", "com.kagi.kagimacos", "com.sigmaos", "app.zen-browser"
    ]

    /// 2026-10-14: bundle ids the teacher allowed for this exam (the page passes the
    /// exam's "allowed_apps"; the server only offers a curated catalog). Lowercase.
    private(set) static var teacherAllowed: Set<String> = []

    /// From the bridge ("lock" / "session" / "openExternal"): keeps only well-formed
    /// macOS bundle ids (Windows process names have no dot and are skipped), never a
    /// terminal or a browser, at most 40.
    /// Set before the lock; once locked the list only changes if it was empty (an offline
    /// start that learns its exam later), so a page can't widen it mid-exam.
    static func updateTeacherAllowed(_ raw: Any?) {
        guard !LockdownController.shared.isLocked || teacherAllowed.isEmpty else { return }
        setTeacherAllowed(raw)
    }

    static func setTeacherAllowed(_ raw: Any?) {
        teacherAllowed = parseTeacherAllowed(raw)
        DebugLog.log("lock", "teacher-allowed apps: \(teacherAllowed.sorted().joined(separator: ", "))")
    }

    static func parseTeacherAllowed(_ raw: Any?) -> Set<String> {
        let ids = ((raw as? [Any]) ?? []).compactMap { $0 as? String }
        var out: Set<String> = []
        for id in ids.prefix(40) {
            let lower = id.lowercased()
            guard looksLikeBundleId(lower), !matches(lower, neverAllowed) else { continue }
            out.insert(lower)
        }
        return out
    }

    static func looksLikeBundleId(_ s: String) -> Bool {
        guard (3...200).contains(s.count), s.contains("."), !s.hasPrefix("."), !s.hasSuffix(".") else { return false }
        return s.unicodeScalars.allSatisfy {
            ($0.value >= 48 && $0.value <= 57) || ($0.value >= 97 && $0.value <= 122) || $0 == "." || $0 == "-" || $0 == "_"
        }
    }

    /// Review 2026-10-14: an id matches a listed one exactly or as a sub-id
    /// ("com.cisco.anyconnect.gui"), never by plain prefix ("com.fortinetnotes").
    static func matches(_ lowerId: String, _ list: [String]) -> Bool {
        list.contains { let p = $0.lowercased(); return lowerId == p || lowerId.hasPrefix(p + ".") }
    }

    /// The name test alone (unit-testable). VPN prefixes, dev tools and teacher-allowed apps.
    static func keepsId(_ id: String) -> Bool {
        let lower = id.lowercased()
        guard !lower.isEmpty else { return false }
        return matches(lower, Array(devKeep)) || matches(lower, alwaysPrefixes)
    }

    /// Kept running regardless of the deny lists: VPN clients (and dev-build tools).
    /// Review 2026-10-14: a VPN name only counts when the app is really signed with an
    /// Apple-issued certificate, so a home-made "com.cisco.anyconnect.x" gets no pass.
    static func keeps(_ app: NSRunningApplication) -> Bool {
        guard let id = app.bundleIdentifier, keepsId(id) else { return false }
        if matches(id.lowercased(), Array(devKeep)) { return true }          // dev builds only
        return CodeSign.isDeveloperSigned(pid: app.processIdentifier)
    }

    static func isTeacherAllowed(_ app: NSRunningApplication) -> Bool {
        guard let id = app.bundleIdentifier?.lowercased(), !teacherAllowed.isEmpty else { return false }
        guard teacherAllowed.contains(where: { id == $0 || id.hasPrefix($0 + ".") }) else { return false }
        return CodeSign.isDeveloperSigned(pid: app.processIdentifier)
    }

    /// A real Apple security dialog (by bundle id AND Apple's signature, never by name).
    static func isSystemDialog(_ app: NSRunningApplication) -> Bool {
        guard let id = app.bundleIdentifier, systemDialogs.contains(id) else { return false }
        return CodeSign.isApple(pid: app.processIdentifier)
    }

    /// May this app keep running while locked? Windowed (.regular) apps: macOS itself,
    /// VPN clients and the apps the teacher allowed for this exam. Everything else is
    /// closed (2026-10-14). Background/menu-bar apps unless on the deny list
    /// (SystemChecks.deniedBundlePrefixes, checked separately).
    static func permits(_ app: NSRunningApplication) -> Bool {
        guard let id = app.bundleIdentifier else { return app.activationPolicy != .regular }
        if id == Bundle.main.bundleIdentifier { return true }
        if system.contains(id) || systemDialogs.contains(id) { return CodeSign.isApple(pid: app.processIdentifier) }
        if keeps(app) || isTeacherAllowed(app) { return true }
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
        /// 2026-10-13: a windowed app left running behind the exam (noticed, not quit).
        case appInBackground(String)
        /// Review 2026-10-14: a server reply that should be signed was not (man in the
        /// middle, or a server too old for this app). Not a TLS pin failure.
        case serverReplyUnsigned(String)
        /// Review 2026-10-14: the watchdog stopped while locked (started again).
        case watchdogStopped

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
            case .appInBackground(let s): return ("app_in_background", s)
            case .serverReplyUnsigned(let s): return ("server_reply_unsigned", s)
            case .watchdogStopped: return ("watchdog_stopped", "the FairPlay watchdog stopped while locked; started again")
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

    /// Review 2026-10-14: the same event repeating (a key mashed, an app reactivating in a
    /// loop) is sent once per `repeatWindow`, and at most `maxPerType` of one type per run,
    /// so the server's per-attempt cap (5000) can't be used up to bury real evidence.
    static let repeatWindow: TimeInterval = 10
    static let maxPerType = 300
    private var lastSent: [String: Date] = [:]
    private var perType: [String: Int] = [:]
    private var capped: Set<String> = []
    private static let maxLogBytes: UInt64 = 5_000_000

    /// Decides (under `queue`) whether an event goes out. Pure state, testable.
    /// Only these noisy kinds are throttled; lock / exit / watchdog events always go out.
    static let throttled: Set<String> = [
        "other_app_foreground", "other_app_launched", "blocked_shortcut", "synthetic_input",
        "blocked_navigation", "space_changed", "process_blocked", "app_in_background",
    ]

    static func shouldSend(type: String, detail: String, at now: Date, lastSent: inout [String: Date],
                           perType: inout [String: Int]) -> Bool {
        guard throttled.contains(type) else { return true }
        let key = type + "|" + detail
        if let t = lastSent[key], now.timeIntervalSince(t) < repeatWindow { return false }
        lastSent[key] = now
        if lastSent.count > 2000 { lastSent.removeAll() }
        let n = (perType[type] ?? 0) + 1
        perType[type] = n
        return n <= maxPerType
    }

    /// Thread-safe and non-blocking: the event tap callback calls this on its own thread,
    /// so all formatting and file work happens on `queue` (review 2026-10-14: it used to
    /// wait there behind file writes).
    func record(_ e: Event) {
        let (type, detail) = e.pair
        let at = Date()
        queue.async { [weak self] in
            guard let self else { return }
            let now = self.iso.string(from: at)
            self.append("\(now) \(type) \(detail)\n")
            DebugLog.log("event", "\(type) \(detail)")
            var out: [(String, String)] = []
            if EventLog.shouldSend(type: type, detail: detail, at: at, lastSent: &self.lastSent, perType: &self.perType) {
                out.append((type, detail))
            } else if (self.perType[type] ?? 0) > EventLog.maxPerType, !self.capped.contains(type) {
                self.capped.insert(type)
                out.append(("events_capped", "more than \(EventLog.maxPerType) \(type) events; later ones only in the local log"))
            }
            guard !out.isEmpty else { return }
            DispatchQueue.main.async { [weak self] in for (t, d) in out { self?.onEvent?(t, d, now) } }
        }
    }

    /// On `queue`. The local log rolls over at 5 MB (one old copy kept).
    private func append(_ text: String) {
        guard let data = text.data(using: .utf8) else { return }
        if let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber)?.uint64Value,
           size > EventLog.maxLogBytes {
            let old = url.appendingPathExtension("1")
            try? FileManager.default.removeItem(at: old)
            try? FileManager.default.moveItem(at: url, to: old)
        }
        if let h = try? FileHandle(forWritingTo: url) {
            h.seekToEndOfFile(); h.write(data); try? h.close()
        } else {
            try? data.write(to: url)
        }
    }

    var logPath: String { url.path }
}
