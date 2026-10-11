import AppKit
import WebKit

/// Exams held on another website (CONTRACT.md slice 3, `openExternal`).
///
/// While active: a light native strip sits above the web view (title, countdown,
/// "Finish exam"); navigation is limited to the exam server plus the teacher's
/// allowed domains; because our own JS page is gone, this class sends heartbeats and
/// integrity events to the server itself. Finishing loads
/// <server>/student/external-done/<attempt>, whose page submits and unlocks.
final class ExternalExamController {

    struct Session {
        let attemptId: String
        let startURL: URL
        let allowedDomains: [String]
        let finishContains: String?
        var deadline: Date
        let clockOffset: TimeInterval      // server time minus local time
        let title: String
        let token: String
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

    // MARK: - Start

    /// Parses the bridge message body. Returns an error text when it is unusable.
    func start(body: [String: Any]) -> String? {
        guard let aid = body["attempt_id"] as? String, !aid.isEmpty,
              let urlText = body["url"] as? String, let url = URL(string: urlText),
              let scheme = url.scheme?.lowercased(), scheme == "https" || scheme == "http",
              let token = body["token"] as? String, !token.isEmpty else {
            return "bad_request"
        }
        var domains = ((body["allowed_domains"] as? [Any]) ?? []).compactMap { ($0 as? String)?.lowercased() }
        if let host = url.host?.lowercased(), !domains.contains(host) { domains.append(host) }
        let finish = (body["finish_url_contains"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        let serverTime = ExternalExamController.parseDate(body["server_time"] as? String) ?? Date()
        let offset = serverTime.timeIntervalSince(Date())
        let deadline = ExternalExamController.parseDate(body["deadline_at"] as? String)
            ?? Date().addingTimeInterval(3600)
        let title = (body["title"] as? String) ?? "Exam"

        stopTimers()
        finishing = false
        session = Session(attemptId: aid, startURL: url, allowedDomains: domains, finishContains: finish,
                          deadline: deadline, clockOffset: offset, title: title, token: token)
        showStrip(title: title)
        startTimers()
        observeURL()
        webView?.load(URLRequest(url: url))
        return nil
    }

    // MARK: - Navigation policy

    func allows(_ url: URL) -> Bool {
        guard let session, let host = url.host?.lowercased() else { return false }
        guard let scheme = url.scheme?.lowercased(), scheme == "https" || scheme == "http" else { return false }
        for d in session.allowedDomains where host == d || host.hasSuffix("." + d) {
            return true
        }
        return false
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
        post(path: "api/student/attempts/\(session.attemptId)/events", token: session.token,
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

    private func heartbeat() {
        guard let session, !finishing else { return }
        let locked = LockdownController.shared.isLocked
        post(path: "api/student/attempts/\(session.attemptId)/heartbeat", token: session.token,
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

    /// POST JSON with the student's token. Completion runs on the main thread.
    private func post(path: String, token: String, body: [String: Any],
                      completion: @escaping (Int?, [String: Any]?) -> Void) {
        let url = server.appendingPathComponent(path)
        var req = URLRequest(url: url, timeoutInterval: 15)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { data, response, _ in
            let status = (response as? HTTPURLResponse)?.statusCode
            var json: [String: Any]?
            if let data { json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] }
            DispatchQueue.main.async { completion(status, json) }
        }.resume()
    }

    // MARK: - Finish / teardown

    @objc private func finishPressed() {
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

    /// Submit via the web page and leave external mode. The lock stays until JS sends "unlock".
    func finish(reason: String) {
        guard let session, !finishing else { return }
        finishing = true
        // Send what is still queued plus the finish event straight away, with this
        // session's token: after teardown the exam page takes over event forwarding.
        var batch = pendingEvents
        batch.append(["type": "external_finished", "detail": reason,
                      "client_time": ExternalExamController.isoFrac.string(from: Date())])
        post(path: "api/student/attempts/\(session.attemptId)/events", token: session.token,
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
