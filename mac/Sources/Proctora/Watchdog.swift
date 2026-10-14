import Foundation
import Darwin

/// Makes killing FairPlay during an exam loud (audit 2026-10-12 #6).
///
/// While locked, a second copy of our own executable runs as `--watchdog <pid>`. It
/// ignores SIGTERM/SIGHUP/SIGINT (so a plain `killall` misses it) and, if the app dies
/// while the lock marker file still exists, reports `watchdog_app_killed` to the server
/// and reopens FairPlay. The app itself also ignores SIGTERM while locked. `kill -9` on
/// both still works -- then the server flags the gap in the app's signed beats.
enum Watchdog {

    private static var child: Process?

    private static var folder: URL {
        let fm = FileManager.default
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first ?? fm.temporaryDirectory
        return base.appendingPathComponent("FairPlay", isDirectory: true)
    }

    /// Exists exactly while the app is locked: "<attempt>\n<token>\n<session>" (may be empty lines).
    static var markerURL: URL { folder.appendingPathComponent("locked.marker") }

    // MARK: - In the app

    static func lockStarted() {
        signal(SIGTERM, SIG_IGN)
        signal(SIGHUP, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        writeMarker(AppSession.shared.exam)
        spawn()
    }

    static func lockEnded() {
        try? FileManager.default.removeItem(at: markerURL)
        signal(SIGTERM, SIG_DFL)
        signal(SIGHUP, SIG_DFL)
        signal(SIGINT, SIG_DFL)
        // The watchdog sees the marker gone and exits by itself.
        child = nil
    }

    /// Called when the exam session becomes known/changes while locked.
    static func update(exam: AppSession.Exam?) {
        guard FileManager.default.fileExists(atPath: markerURL.path) else { return }
        writeMarker(exam)
    }

    /// At launch: a marker left behind means the last run died while locked.
    static func cleanUpAtLaunch() -> Bool {
        let existed = FileManager.default.fileExists(atPath: markerURL.path)
        try? FileManager.default.removeItem(at: markerURL)
        return existed
    }

    private static func writeMarker(_ exam: AppSession.Exam?) {
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let text = [exam?.attemptId ?? "", exam?.token ?? "", exam?.session ?? ""].joined(separator: "\n")
        // Audit 2026-10-13: the marker holds the token, so it is 0600 from the first byte
        // (was: written with default permissions, then chmodded). Temp file + rename keeps
        // the update atomic for the watchdog reading it.
        let tmp = folder.appendingPathComponent("locked.marker.\(getpid()).tmp")
        try? FileManager.default.removeItem(at: tmp)
        let fd = open(tmp.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
        guard fd >= 0 else { return }
        let bytes = Array(text.utf8)
        let wrote = bytes.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        close(fd)
        guard wrote == bytes.count else { try? FileManager.default.removeItem(at: tmp); return }
        if rename(tmp.path, markerURL.path) != 0 { try? FileManager.default.removeItem(at: tmp) }
    }

    /// While locked: true when the watchdog runs. If it died (killed), it is started
    /// again and false is returned once so the lock can report it.
    @discardableResult
    static func ensureRunning() -> Bool {
        guard FileManager.default.fileExists(atPath: markerURL.path) else { return true }
        if let c = child, c.isRunning { return true }
        let hadOne = child != nil
        spawn()
        return !hadOne
    }

    /// Review 2026-10-14: the watchdog runs as Contents/MacOS/FairPlayGuard (a copy of the
    /// app's executable made by build.sh), so `killall Proctora` doesn't end it too.
    static var guardExecutable: URL? {
        Bundle.main.url(forAuxiliaryExecutable: "FairPlayGuard") ?? Bundle.main.executableURL
    }

    private static func spawn() {
        if let c = child, c.isRunning { return }
        guard let exe = guardExecutable else { return }
        let p = Process()
        p.executableURL = exe
        p.arguments = ["--watchdog", String(ProcessInfo.processInfo.processIdentifier)]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run(); child = p; DebugLog.log("lock", "watchdog started pid \(p.processIdentifier)") }
        catch { NSLog("[FairPlay] watchdog not started: \(error)"); DebugLog.log("error", "watchdog not started: \(error)") }
    }

    // MARK: - In the watchdog process

    /// Entry point for `--watchdog <pid>`; never returns.
    static func run(parent: pid_t) -> Never {
        signal(SIGTERM, SIG_IGN)
        signal(SIGHUP, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        while true {
            sleep(1)
            let markerExists = FileManager.default.fileExists(atPath: markerURL.path)
            if !markerExists { exit(0) }                       // lock ended normally
            if kill(parent, 0) != 0 && errno == ESRCH {         // app gone while locked
                if let text = try? String(contentsOf: markerURL, encoding: .utf8),
                   let aid = text.components(separatedBy: "\n").first,
                   AppSession.isSafeToken(aid, min: 3, max: 32) {
                    resumeAttemptId = aid
                }
                report()
                try? FileManager.default.removeItem(at: markerURL)
                reopen()
                exit(0)
            }
        }
    }

    private static func report() {
        guard let text = try? String(contentsOf: markerURL, encoding: .utf8) else { return }
        let parts = text.components(separatedBy: "\n")
        guard parts.count >= 3, !parts[0].isEmpty, !parts[1].isEmpty else { return }
        let iso = ISO8601DateFormatter()
        // Audit M-17: a crash and a kill look the same from here: say what is known.
        let body: [String: Any] = ["events": [["type": "watchdog_app_killed",
                                               "detail": "FairPlay ended while locked (force-quit or crash)",
                                               "client_time": iso.string(from: Date())]]]
        var req = URLRequest(url: Config.serverURL.appendingPathComponent("api/student/attempts/\(parts[0])/events"),
                             timeoutInterval: 8)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(parts[1])", forHTTPHeaderField: "Authorization")
        if !parts[2].isEmpty { req.setValue(parts[2], forHTTPHeaderField: "X-FairPlay-Session") }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        let done = DispatchSemaphore(value: 0)
        Net.session.dataTask(with: req) { _, _, _ in done.signal() }.resume()
        _ = done.wait(timeout: .now() + 10)
    }

    /// Audit M-17: reopen straight into the attempt (the page resumes it and locks again)
    /// instead of the login page.
    private static func reopen() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        var args = [Bundle.main.bundleURL.path]
        if let aid = resumeAttemptId { args += ["--args", "--resume-attempt", aid] }
        p.arguments = args
        try? p.run()
    }

    /// The attempt id from the marker (read before the marker is removed).
    private static var resumeAttemptId: String?
}
