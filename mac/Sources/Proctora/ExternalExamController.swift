import AppKit
import WebKit

/// Exams held on another website (CONTRACT.md slice 3, `openExternal`).
///
/// While active: a light native strip sits above the web view (title, countdown,
/// "Finish exam"); navigation is limited to the exam server plus the teacher's
/// allowed sites; because our own JS page is gone, this class sends heartbeats and
/// integrity events to the server itself. Finishing loads
/// <server>/student/external-done/<attempt>, whose page submits and unlocks.
///
/// Audit 2026-10-12 #10: allowed sites are matched EXACTLY ("canvas.dku.edu" does not
/// open "x.canvas.dku.edu"; "*.dku.edu" does), with an optional path prefix
/// ("docs.google.com/forms/d/e/1FAIpQL/"). Only https in release builds.
final class ExternalExamController {

    /// One allowed-site entry: [*.]host[/path-prefix].
    struct SiteRule: Equatable {
        let wildcard: Bool
        let host: String
        let pathPrefix: String

        init?(_ raw: String) {
            var s = raw.trimmingCharacters(in: .whitespaces)
            if let r = s.range(of: "://") { s = String(s[r.upperBound...]) }
            if let q = s.firstIndex(where: { $0 == "?" || $0 == "#" }) { s = String(s[..<q]) }
            var host = s
            var path = ""
            if let slash = s.firstIndex(of: "/") {
                host = String(s[..<slash])
                path = String(s[slash...])
            }
            var wild = false
            if host.hasPrefix("*.") { wild = true; host = String(host.dropFirst(2)) }
            if let colon = host.firstIndex(of: ":") { host = String(host[..<colon]) }
            host = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
            guard !host.isEmpty, !host.contains("*") else { return nil }
            wildcard = wild
            self.host = host
            pathPrefix = path == "/" ? "" : path
        }

        func matches(host h: String, path p: String) -> Bool {
            let hostOK = h == host || (wildcard && h.hasSuffix("." + host))
            guard hostOK else { return false }
            if pathPrefix.isEmpty { return true }
            // Audit 2026-10-13: whole path segments only: "/forms/abc" allows "/forms/abc"
            // and "/forms/abc/..." but not "/forms/abcdef".
            let path = p.isEmpty ? "/" : p
            if pathPrefix.hasSuffix("/") { return path.hasPrefix(pathPrefix) }
            return path == pathPrefix || path.hasPrefix(pathPrefix + "/")
        }
    }

    struct Session {
        let attemptId: String
        let startURL: URL
        let rules: [SiteRule]
        let finishContains: String?
        var deadline: Date
        let clockOffset: TimeInterval      // server time minus local time
        let title: String
        let token: String
        let appSession: String
    }

    private(set) var session: Session?
    var isActive: Bool { session != nil }

    private let server: URL
    private weak var webView: WKWebView?
    private weak var container: NSView?
    private weak var window: NSWindow?

    private var strip: NSView?
    private var titleLabel: NSTextField?
    private var timeLabel: NSTextField?
    private var countdownTimer: Timer?
    private var heartbeatTimer: Timer?
    private var urlObservation: NSKeyValueObservation?
    private var pendingEvents: [[String: Any]] = []
    private var sendingEvents = false
    private var finishing = false

    static let stripHeight: CGFloat = 52

    private static let isoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let isoPlain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func parseDate(_ s: String?) -> Date? {
        guard let s, !s.isEmpty else { return nil }
        return isoFrac.date(from: s) ?? isoPlain.date(from: s)
    }

    init(server: URL) {
        self.server = server
    }

    func attach(webView: WKWebView, container: NSView, window: NSWindow?) {
        self.webView = webView
        self.container = container
        self.window = window
    }

    private static func schemeOK(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased() else { return false }
        if scheme == "https" { return true }
        // Plain http only in dev builds (local test sites).
        return Config.isDevBuild && scheme == "http"
    }

    // MARK: - Start

    /// Parses the bridge message body. Returns an error text when it is unusable.
    func start(body: [String: Any]) -> String? {
        guard let aid = body["attempt_id"] as? String, !aid.isEmpty,
              let urlText = body["url"] as? String, let url = URL(string: urlText),
              ExternalExamController.schemeOK(url), let startHost = url.host?.lowercased(),
              let token = body["token"] as? String, !token.isEmpty else {
            return "bad_request"
        }
        var rules = ((body["allowed_domains"] as? [Any]) ?? []).compactMap { ($0 as? String).flatMap(SiteRule.init) }
        // The server always sends an entry for the exam address; if a (stale) page did not,
        // allow exactly the start page's host + folder, never the whole domain tree.
        if !rules.contains(where: { $0.matches(host: startHost, path: url.path) }) {
            let folder = url.path.contains("/") ? String(url.path[...url.path.lastIndex(of: "/")!]) : ""
            if let r = SiteRule(startHost + folder) { rules.append(r) }
        }
        let finish = (body["finish_url_contains"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        let serverTime = ExternalExamController.parseDate(body["server_time"] as? String) ?? Date()
        let offset = serverTime.timeIntervalSince(Date())
        let deadline = ExternalExamController.parseDate(body["deadline_at"] as? String)
            ?? Date().addingTimeInterval(3600)
        let title = (body["title"] as? String) ?? "Exam"
        let appSession = (body["session"] as? String) ?? ""

        stopTimers()
        finishing = false
        session = Session(attemptId: aid, startURL: url, rules: rules, finishContains: finish,
                          deadline: deadline, clockOffset: offset, title: title, token: token,
                          appSession: appSession)
        DebugLog.log("external", "website exam start: \(url.host ?? "?") rules=\(rules.count) finish=\(finish ?? "-")")
        showStrip(title: title)
        startTimers()
        observeURL()
        webView?.load(URLRequest(url: url))
        return nil
    }

    // MARK: - Navigation policy

    func allows(_ url: URL) -> Bool {
        guard let session, let host = url.host?.lowercased(), ExternalExamController.schemeOK(url) else {
            return false
        }
        return session.rules.contains { $0.matches(host: host, path: url.path) }
    }

    /// Call for every allowed main-frame navigation and URL change.
    func checkFinishURL(_ url: URL?) {
        guard let session, !finishing, let needle = session.finishContains, let url else { return }
        if url.absoluteString.contains(needle) {
            // Give the site a moment to show its own "submitted" confirmation.
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
                self?.finish(reason: "finish_url")
            }
        }
    }

    // MARK: - Events (the JS page is gone while external)

    func queueEvent(type: String, detail: String, time: String) {
        guard isActive else { return }
        pendingEvents.append(["type": type, "detail": detail, "client_time": time])
        flushEvents()
    }

    private func flushEvents() {
        guard let session, !sendingEvents, !pendingEvents.isEmpty else { return }
        let batch = Array(pendingEvents.prefix(100))
        sendingEvents = true
        post(path: "api/student/attempts/\(session.attemptId)/events", session: session,
             body: ["events": batch]) { [weak self] status, _ in
            guard let self else { return }
            self.sendingEvents = false
            if let status, (200..<300).contains(status) || (400..<500).contains(status) && status != 429 {
                // stored, or rejected for good: drop this batch
                self.pendingEvents.removeFirst(min(batch.count, self.pendingEvents.count))
                if !self.pendingEvents.isEmpty { self.flushEvents() }
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.flushEvents() }
            }
        }
    }

    // MARK: - Heartbeat + countdown

    private func startTimers() {
        let cd = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.add(cd, forMode: .common)
        countdownTimer = cd
        let hb = Timer(timeInterval: 5, repeats: true) { [weak self] _ in self?.heartbeat() }
        RunLoop.main.add(hb, forMode: .common)
        heartbeatTimer = hb
        tick()
        heartbeat()
    }

    private func stopTimers() {
        countdownTimer?.invalidate(); countdownTimer = nil
        heartbeatTimer?.invalidate(); heartbeatTimer = nil
    }

    private func tick() {
        guard let session else { return }
        let now = Date().addingTimeInterval(session.clockOffset)
        let left = max(0, Int(session.deadline.timeIntervalSince(now).rounded(.down)))
        let h = left / 3600, m = (left % 3600) / 60, s = left % 60
        timeLabel?.stringValue = h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%02d:%02d", m, s)
        timeLabel?.textColor = left < 300
            ? NSColor(calibratedRed: 0.71, green: 0.34, blue: 0.04, alpha: 1)
            : NSColor(calibratedRed: 0.11, green: 0.15, blue: 0.20, alpha: 1)
        if left == 0 { finish(reason: "deadline") }
    }

    /// Page-alive heartbeat (the page is gone). The lock state itself goes in the signed
    /// app beats (AppSession), which the server trusts; "locked" here is informational.
    private func heartbeat() {
        guard let session, !finishing else { return }
        let locked = LockdownController.shared.isLocked
        post(path: "api/student/attempts/\(session.attemptId)/heartbeat", session: session,
             body: ["current_question": 0, "locked": locked]) { [weak self] status, json in
            guard let self, status == 200, let json else { return }
            if let d = ExternalExamController.parseDate(json["deadline_at"] as? String) {
                self.session?.deadline = d          // teacher may have added time
            }
            if (json["status"] as? String) == "submitted" {
                self.finish(reason: "closed_by_teacher")
            }
        }
    }

    private func post(path: String, session: Session, body: [String: Any],
                      completion: @escaping (Int?, [String: Any]?) -> Void) {
        Net.request("POST", path: path, token: session.token,
                    appSession: session.appSession.isEmpty ? nil : session.appSession,
                    body: body, completion: completion)
    }

    // MARK: - Finish / teardown

    @objc private func finishPressed() {
        DebugLog.log("external", "Finish exam button pressed")
        guard isActive, !finishing else { return }
        guard let window, window.attachedSheet == nil else { return }
        let alert = NSAlert()
        alert.messageText = "Finish the exam?"
        alert.informativeText = "Make sure you submitted your answers on the website first. "
            + "After this, the exam closes and your computer unlocks."
        alert.addButton(withTitle: "Finish exam")
        alert.addButton(withTitle: "Keep working")
        alert.beginSheetModal(for: window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.finish(reason: "student_finished") }
        }
    }

    /// Submit via the web page and leave external mode. The lock stays until JS sends
    /// "unlock" and the server confirms the hand-in.
    func finish(reason: String) {
        guard let session, !finishing else { return }
        DebugLog.log("external", "finish: \(reason)")
        finishing = true
        // Send what is still queued plus the finish event straight away, with this
        // session's token: after teardown the exam page takes over event forwarding.
        var batch = pendingEvents
        batch.append(["type": "external_finished", "detail": reason,
                      "client_time": ExternalExamController.isoFrac.string(from: Date())])
        post(path: "api/student/attempts/\(session.attemptId)/events", session: session,
             body: ["events": batch]) { _, _ in }
        let aid = session.attemptId
        teardown()
        var comps = URLComponents(url: server.appendingPathComponent("student/external-done/\(aid)"),
                                  resolvingAgainstBaseURL: false)
        comps?.queryItems = [URLQueryItem(name: "reason", value: reason)]
        if let url = comps?.url { webView?.load(URLRequest(url: url)) }
    }

    /// Leave external mode without submitting (emergency exit / safety unlock).
    func abort() {
        guard isActive else { return }
        DebugLog.log("external", "aborted (emergency exit / safety unlock)")
        teardown()
        webView?.load(URLRequest(url: server.appendingPathComponent("student")))
    }

    private func teardown() {
        stopTimers()
        urlObservation?.invalidate(); urlObservation = nil
        hideStrip()
        session = nil
        pendingEvents.removeAll()
    }

    private func observeURL() {
        urlObservation?.invalidate()
        urlObservation = webView?.observe(\.url, options: [.new]) { [weak self] wv, _ in
            let url = wv.url
            DispatchQueue.main.async { self?.checkFinishURL(url) }
        }
    }

    // MARK: - Strip UI (light: white, hairline bottom border)

    private func showStrip(title: String) {
        guard let container, let webView else { return }
        hideStrip()
        let h = ExternalExamController.stripHeight
        let b = container.bounds
        let v = NSView(frame: NSRect(x: 0, y: b.height - h, width: b.width, height: h))
        v.autoresizingMask = [.width, .minYMargin]
        v.wantsLayer = true
        v.layer?.backgroundColor = NSColor.white.cgColor

        let line = NSView(frame: NSRect(x: 0, y: 0, width: b.width, height: 1))
        line.autoresizingMask = [.width, .maxYMargin]
        line.wantsLayer = true
        line.layer?.backgroundColor = NSColor(calibratedRed: 0.886, green: 0.902, blue: 0.925, alpha: 1).cgColor
        v.addSubview(line)

        let lock = NSTextField(labelWithString: "Locked")
        lock.font = .systemFont(ofSize: 12, weight: .semibold)
        lock.textColor = NSColor(calibratedRed: 0.086, green: 0.475, blue: 0.298, alpha: 1)
        lock.wantsLayer = true

        let t = NSTextField(labelWithString: title)
        t.font = .systemFont(ofSize: 14, weight: .semibold)
        t.textColor = NSColor(calibratedRed: 0.11, green: 0.15, blue: 0.20, alpha: 1)
        t.lineBreakMode = .byTruncatingTail
        t.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        titleLabel = t

        let time = NSTextField(labelWithString: "--:--")
        time.font = .monospacedDigitSystemFont(ofSize: 16, weight: .semibold)
        timeLabel = time

        let button = NSButton(title: "Finish exam", target: self, action: #selector(finishPressed))
        button.bezelStyle = .rounded
        button.keyEquivalent = ""

        let spacer = NSView()
        spacer.setContentHuggingPriority(.init(1), for: .horizontal)

        let stack = NSStackView(views: [lock, t, spacer, time, button])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 14
        stack.edgeInsets = NSEdgeInsets(top: 0, left: 20, bottom: 0, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: v.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: v.trailingAnchor),
            stack.topAnchor.constraint(equalTo: v.topAnchor),
            stack.bottomAnchor.constraint(equalTo: v.bottomAnchor, constant: -1)
        ])

        container.addSubview(v, positioned: .above, relativeTo: webView)
        strip = v
        var f = b
        f.size.height = max(0, b.height - h)
        webView.frame = f
    }

    private func hideStrip() {
        strip?.removeFromSuperview()
        strip = nil; titleLabel = nil; timeLabel = nil
        if let container, let webView { webView.frame = container.bounds }
    }
}
