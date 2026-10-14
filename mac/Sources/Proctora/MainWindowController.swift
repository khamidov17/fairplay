import AppKit
import WebKit

final class ExamWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

/// No context menu (it offers "Look Up", "Search with Google", Services, Share...)
/// and no force-click Look Up.
final class ExamWebView: WKWebView {
    override func willOpenMenu(_ menu: NSMenu, with event: NSEvent) {
        menu.removeAllItems()
        super.willOpenMenu(menu, with: event)
    }
    override func quickLook(with event: NSEvent) {}
}

/// WKUserContentController retains its handlers; this breaks the cycle.
final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        target?.userContentController(c, didReceive: m)
    }
}

/// The one window: student web UI in a WKWebView, plus the native bridge
/// (CONTRACT.md "Native bridge"). Normal window until the exam asks to lock;
/// then borderless, covering every screen, above everything.
final class MainWindowController: NSWindowController, LockSurface,
                                  WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {

    private var webView: ExamWebView!
    private var permissionView: NSView?
    private var permissionStatus: NSTextField?
    private var blockingView: NSView?
    private var blockingLabel: NSTextField?
    private var trustPoll: Timer?
    private var unlockRetry: Timer?
    private var urlWatch: NSKeyValueObservation?
    /// Audit M-18: set only while WE load an about:/data: page (loadOwnHTML); a link or
    /// script on a page can never set it. (WKNavigationAction.sourceFrame is declared
    /// non-optional, so a nil check on it is compiled away and can't be used.)
    private var ownPageLoadPending = false
    /// Audit W-10: per-launch nonce stamped on every message to the page.
    private let bridgeNonce = UUID().uuidString.replacingOccurrences(of: "-", with: "")
    private var normalFrame = NSRect(x: 0, y: 0, width: 1280, height: 820)
    private let server = Config.serverURL
    private let external = ExternalExamController(server: Config.serverURL)

    private static let normalStyle: NSWindow.StyleMask = [.titled, .closable, .miniaturizable, .resizable]

    convenience init() {
        let win = ExamWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                             styleMask: MainWindowController.normalStyle, backing: .buffered, defer: false)
        win.title = "FairPlay"
        win.minSize = NSSize(width: 900, height: 600)
        win.sharingType = .none                 // excluded from screenshots / screen recording (verify on macOS 15+)
        win.isReleasedWhenClosed = false
        win.collectionBehavior = [.fullScreenNone]
        win.center()
        self.init(window: win)
        buildWebView()
    }

    // MARK: - Setup

    private func buildWebView() {
        guard let content = window?.contentView else { return }
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()    // keeps localStorage: offline answers survive a crash
        config.applicationNameForUserAgent = "FairPlay/\(Config.version)"
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        // No Apple Intelligence rewrite/proofread in answers. Set through KVC because some
        // Xcode SDKs don't expose WKWebViewConfiguration.writingToolsBehavior to Swift even
        // though the runtime has it (build failed on a second Mac with "no member").
        if #available(macOS 15.0, *) {
            let sel = NSSelectorFromString("setWritingToolsBehavior:")
            if config.responds(to: sel) {
                config.setValue(NSWritingToolsBehavior.none.rawValue, forKey: "writingToolsBehavior")
            }
        }

        let ucc = WKUserContentController()
        ucc.add(WeakScriptHandler(self), name: "proctora")
        let guardJS = """
        (function () {
          document.addEventListener('contextmenu', function (e) { e.preventDefault(); }, true);
          document.addEventListener('dragstart', function (e) { e.preventDefault(); }, true);
          window.open = function () { return null; };
        })();
        """
        ucc.addUserScript(WKUserScript(source: guardJS, injectionTime: .atDocumentStart, forMainFrameOnly: false))
        // Audit W-10: the page drops 'proctora-native' events that lack this nonce, so page
        // JS can't fake "locked"/"unlocked" to its own UI.
        // Audit 2026-10-13: only the FairPlay server's own pages get it, never a website
        // exam (send() only talks to server pages anyway).
        ucc.addUserScript(WKUserScript(source: MainWindowController.nonceScript(nonce: bridgeNonce, server: server),
                                       injectionTime: .atDocumentStart, forMainFrameOnly: true))
        if DebugLog.isOn {   // dev builds: page clicks + JS errors into logs/events.log
            ucc.addUserScript(WKUserScript(source: DebugLog.pageScript, injectionTime: .atDocumentStart,
                                           forMainFrameOnly: false))
        }
        config.userContentController = ucc

        let wv = ExamWebView(frame: content.bounds, configuration: config)
        wv.autoresizingMask = [.width, .height]
        wv.navigationDelegate = self
        wv.uiDelegate = self
        wv.allowsBackForwardNavigationGestures = false
        wv.allowsMagnification = false
        wv.allowsLinkPreview = false
        content.addSubview(wv)
        webView = wv
        // In-page (pushState) route changes never reach decidePolicyFor, so also watch the
        // URL itself: a teacher page inside the student app goes back to the student login.
        urlWatch = wv.observe(\.url, options: [.new]) { [weak self] view, _ in
            DispatchQueue.main.async {
                guard let self, let u = view.url, self.isServerURL(u) else { return }
                if MainWindowController.isTeacherPath(u.path) {
                    EventLog.shared.record(.blockedNavigation("teacher page \(u.path)"))
                    DebugLog.log("web", "teacher page opened in-page, sent back: \(u.path)")
                    view.load(URLRequest(url: Config.startURL))
                    return
                }
                // Review 2026-10-14: in-page route changes away from the locked attempt
                // (e.g. a "Back to home" link) go back to it.
                let aid = AppSession.shared.exam?.attemptId
                if LockdownController.shared.isLocked,
                   !MainWindowController.allowedWhileLocked(path: u.path, attemptId: aid), let aid {
                    EventLog.shared.record(.blockedNavigation("left the exam: \(u.path)"))
                    view.load(URLRequest(url: Config.serverURL.appendingPathComponent("student/exam/\(aid)")))
                }
            }
        }
        external.attach(webView: wv, container: content, window: window)
    }

    func start() {
        showWindow(nil)
        takeFocus()
        guard Config.serverIsAcceptable else {
            // Release builds never talk to a plain-http server (audit #11).
            loadOwnHTML("""
            <!doctype html><meta charset="utf-8"><body style="font:16px -apple-system;padding:40px">
            <h1>This copy of FairPlay is not set up</h1>
            <p>It has no secure (https) exam server. Ask your school for the correct FairPlay app.</p>
            """)
            return
        }
        webView.load(URLRequest(url: MainWindowController.launchURL()))
        if !EventTap.isTrusted { showPermission() }
    }

    func reload() {
        guard Config.serverIsAcceptable else { return }
        if webView.url == nil || webView.url?.scheme == "about" {
            webView.load(URLRequest(url: Config.startURL))
        } else {
            webView.reload()
        }
    }

    // MARK: - Bridge (JS <-> native)

    private func defaultPort(_ scheme: String?) -> Int { scheme?.lowercased() == "https" ? 443 : 80 }

    /// `window.__fairplayNonce` for documents whose origin is the server's.
    static func nonceScript(nonce: String, server: URL) -> String {
        let scheme = (server.scheme ?? "https").lowercased()
        let host = (server.host ?? "").lowercased()
        let defaultPort = scheme == "https" ? 443 : 80
        let port = server.port.map { $0 == defaultPort ? "" : ":\($0)" } ?? ""
        let origin = "\(scheme)://\(host)\(port)".filter { $0.isLetter || $0.isNumber || ":/.-[]".contains($0) }
        let safeNonce = nonce.filter { $0.isLetter || $0.isNumber || $0 == "-" }
        return "if (location.origin === '\(origin)') { window.__fairplayNonce = '\(safeNonce)'; }"
    }

    private func isServerURL(_ url: URL?) -> Bool { MainWindowController.isServerURL(url, server: server) }

    /// Same scheme, host (any case) and port as the exam server (default ports filled in).
    static func isServerURL(_ url: URL?, server: URL) -> Bool {
        guard let url, let host = url.host else { return false }
        func port(_ u: URL) -> Int { u.port ?? (u.scheme?.lowercased() == "https" ? 443 : 80) }
        return host.lowercased() == (server.host ?? "").lowercased() && port(url) == port(server)
            && url.scheme?.lowercased() == server.scheme?.lowercased()
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        // Dev trace lines from any page (exam page or website exam). Never drive anything.
        if let body = message.body as? [String: Any], body["action"] as? String == "log" {
            let host: String = message.frameInfo.securityOrigin.host
            let kind: String = (body["kind"] as? String) ?? "?"
            let text: String = String(((body["msg"] as? String) ?? "").prefix(600))
            let from: String = host.isEmpty ? "local" : host
            DebugLog.log("js", kind + " [" + from + "] " + text)
            return
        }
        // Only the exam server's own pages (same scheme, host and port) may drive the lock.
        let origin = message.frameInfo.securityOrigin
        let originPort = origin.port == 0 ? defaultPort(origin.protocol) : origin.port
        let serverPort = server.port ?? defaultPort(server.scheme)
        guard message.frameInfo.isMainFrame,
              origin.protocol.lowercased() == server.scheme?.lowercased(),
              origin.host == server.host, originPort == serverPort else { return }
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        DebugLog.log("bridge", "page -> app: \(action)")

        switch action {
        case "hello":
            sendState()
        case "flushEvents":
            flushEventQueue()
        case "attest":
            send(AppSession.shared.attestation(nonce: (body["nonce"] as? String) ?? ""))
        case "session":
            if AppSession.shared.setExam(attemptId: body["attempt_id"] as? String, token: body["token"] as? String,
                                         session: body["session"] as? String) {
                Allowlist.updateTeacherAllowed(body["allowed_apps"])
            }
        case "lock":
            // Audit M-06: lock only for a valid exam session -- or an explicit offline start
            // (no server session yet; then only the invigilator or a hand-in that reaches the
            // server ends the lock).
            let offlineStart = (body["offline"] as? Bool) == true
                && body["attempt_id"] == nil && body["session"] == nil
            if !offlineStart && !AppSession.shared.setExam(attemptId: body["attempt_id"] as? String,
                                                          token: body["token"] as? String,
                                                          session: body["session"] as? String) {
                send(["type": "lockFailed", "reason": "bad_session"])
                return
            }
            if offlineStart { DebugLog.log("lock", "offline start: locking without a server session") }
            Allowlist.updateTeacherAllowed(body["allowed_apps"])
            if LockdownController.shared.lock() {
                hidePermission()
                send(["type": "locked"])
            } else {
                showPermission()
                send(["type": "lockFailed", "reason": "accessibility"])
            }
        case "openExternal":
            guard AppSession.shared.setExam(attemptId: body["attempt_id"] as? String, token: body["token"] as? String,
                                            session: body["session"] as? String) else {
                send(["type": "externalFailed", "reason": "bad_session"])
                return
            }
            Allowlist.updateTeacherAllowed(body["allowed_apps"])
            if !LockdownController.shared.isLocked {
                guard LockdownController.shared.lock() else {
                    showPermission()
                    send(["type": "lockFailed", "reason": "accessibility"])
                    return
                }
                hidePermission()
                send(["type": "locked"])
            }
            if let problem = external.start(body: body) {
                send(["type": "externalFailed", "reason": problem])
            }
        case "preflight":
            // Audit M-11: before locking, list what must be fixed outside the lock.
            send(["type": "preflight", "problems": LockdownController.shared.preflight()])
        case "unlock":
            requestUnlock()
        default:
            break
        }
    }

    /// The page asks to unlock after handing in. Audit #11: the page alone can no longer
    /// unlock -- the server must confirm the attempt is submitted. Retries every 3 s.
    private func requestUnlock() {
        guard LockdownController.shared.isLocked else {
            send(["type": "unlocked"])
            return
        }
        AppSession.shared.confirmHandIn { [weak self] result in
            guard let self, LockdownController.shared.isLocked else { return }
            switch result {
            case .confirmed:
                self.unlockRetry?.invalidate(); self.unlockRetry = nil
                LockdownController.shared.unlock()       // onUnlocked sends "unlocked"
                AppSession.shared.clearExam()   // after unlock: its last beat (locked=0) still needs the session
            case .notYet, .unreachable:
                self.send(["type": "unlockPending",
                           "reason": result == .unreachable ? "offline" : "not_submitted"])
                if self.unlockRetry == nil {
                    let t = Timer(timeInterval: 3, repeats: true) { [weak self] _ in
                        guard LockdownController.shared.isLocked else {
                            self?.unlockRetry?.invalidate(); self?.unlockRetry = nil
                            return
                        }
                        self?.requestUnlockOnce()
                    }
                    RunLoop.main.add(t, forMode: .common)
                    self.unlockRetry = t
                }
            }
        }
    }

    private func requestUnlockOnce() {
        AppSession.shared.confirmHandIn { [weak self] result in
            guard let self, result == .confirmed, LockdownController.shared.isLocked else { return }
            self.unlockRetry?.invalidate(); self.unlockRetry = nil
            LockdownController.shared.unlock()
            AppSession.shared.clearExam()   // after unlock: its last beat (locked=0) still needs the session
        }
    }

    /// Server status from the app's own beats: a teacher close also unlocks.
    func handleServerStatus(_ status: String) {
        if status == "submitted", LockdownController.shared.isLocked, !external.isActive {
            requestUnlockOnce()
        }
    }

    func sendState() {
        send(["type": "state",
              "locked": LockdownController.shared.isLocked,
              "trusted": EventTap.isTrusted,
              "version": Config.version])
    }

    /// Review 2026-10-14: integrity events wait here until a page that forwards them to the
    /// server (the exam page) has asked for them ("flushEvents"), instead of being dropped
    /// while no such page is listening (between loads, the error page, the start page...).
    private var eventsReady = false
    private var eventQueue: [[String: Any]] = []
    private let eventQueueMax = 300

    func sendEvent(type: String, detail: String, time: String) {
        if external.isActive {
            // Our JS page is gone while the exam website is open: the controller sends events itself.
            external.queueEvent(type: type, detail: detail, time: time)
            return
        }
        let event: [String: Any] = ["type": type, "detail": detail, "client_time": time]
        if eventsReady, let webView, isServerURL(webView.url) {
            send(["type": "event", "event": event])
        } else {
            eventQueue.append(event)
            if eventQueue.count > eventQueueMax { eventQueue.removeFirst(eventQueue.count - eventQueueMax) }
        }
    }

    private func flushEventQueue() {
        eventsReady = true
        let queued = eventQueue
        eventQueue.removeAll()
        for e in queued { send(["type": "event", "event": e]) }
    }

    func send(_ detail: [String: Any]) {
        guard let webView, isServerURL(webView.url),
              let data = try? JSONSerialization.data(withJSONObject: detail.merging(["bridge_nonce": bridgeNonce]) { _, b in b }),
              let json = String(data: data, encoding: .utf8) else { return }
        if let t = detail["type"] as? String, t != "event" {
            var extra = ""
            if let r = detail["reason"] { extra = " reason=\(r)" }
            DebugLog.log("bridge", "app -> page: \(t)\(extra)")
        }
        let js = "window.dispatchEvent(new CustomEvent('proctora-native', { detail: \(json) }));"
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    // MARK: - Navigation: only the exam server

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        // Audit M-15: the student app never opens teacher pages or the API docs on the
        // exam server (a teacher login there shows start codes, exit codes, answer keys).
        if isServerURL(url) && MainWindowController.isTeacherPath(url.path) {
            EventLog.shared.record(.blockedNavigation("teacher page \(url.path)"))
            DebugLog.log("web", "navigation BLOCKED (teacher page): \(url.path)")
            decisionHandler(.cancel)
            return
        }
        // Audit M-18: about:/data: pages only when WE load them (loadOwnHTML sets the flag;
        // navigation type "other", main frame), never from a link or script on a page.
        let isLocal = url.scheme == "about" || url.scheme == "data"
        let ownLocalPage = isLocal && ownPageLoadPending && navigationAction.navigationType == .other
            && (navigationAction.targetFrame?.isMainFrame ?? true)
        if ownLocalPage { ownPageLoadPending = false }
        // Review 2026-10-14: while locked, only the locked attempt's own pages.
        if isServerURL(url), (navigationAction.targetFrame?.isMainFrame ?? true), LockdownController.shared.isLocked,
           !MainWindowController.allowedWhileLocked(path: url.path, attemptId: AppSession.shared.exam?.attemptId) {
            EventLog.shared.record(.blockedNavigation("left the exam: \(url.path)"))
            DebugLog.log("web", "navigation BLOCKED while locked: \(url.path)")
            decisionHandler(.cancel)
            return
        }
        if ownLocalPage || isServerURL(url) || external.allows(url) {
            if navigationAction.targetFrame?.isMainFrame ?? true {
                external.checkFinishURL(url)
                eventsReady = false            // the new page asks again when it listens
            }
            decisionHandler(.allow)
        } else {
            EventLog.shared.record(.blockedNavigation(url.host ?? url.absoluteString))
            DebugLog.log("web", "navigation BLOCKED: \(url.scheme ?? "")://\(url.host ?? "")\(url.path)")
            decisionHandler(.cancel)
        }
    }

    /// Audit M-17: relaunched by the watchdog after the app ended while locked -> straight
    /// back into that attempt (the page resumes it and asks to lock again).
    static func launchURL() -> URL {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--resume-attempt"), i + 1 < args.count,
           AppSession.isSafeToken(args[i + 1], min: 3, max: 32) {
            DebugLog.log("app", "relaunched after ending while locked: resuming \(args[i + 1])")
            return Config.serverURL.appendingPathComponent("student/exam/\(args[i + 1])")
        }
        return Config.startURL
    }

    /// Pages a locked computer may show: the locked attempt's exam / website-exam / done
    /// pages (and any attempt's while the attempt is not known yet, i.e. an offline start).
    static func allowedWhileLocked(path: String, attemptId: String?) -> Bool {
        let p = path.lowercased()
        let roots = ["/student/exam/", "/student/external/", "/student/external-done/", "/student/done/"]
        guard let aid = attemptId?.lowercased() else { return roots.contains { p.hasPrefix($0) } }
        let offlineOK = ["/student/exam/offline:", "/student/exam/offline%3a",
                         "/student/done/offline:", "/student/done/offline%3a"].contains { p.hasPrefix($0) }
        return offlineOK || roots.contains { p == $0 + aid || p.hasPrefix($0 + aid + "/") }
    }

    static func isTeacherPath(_ path: String) -> Bool {
        let p = path.lowercased()
        return ["/teacher", "/docs", "/redoc", "/openapi.json"].contains { p == $0 || p.hasPrefix($0 + "/") }
    }

    /// TLS: normal validation plus the optional server pin (audit #11).
    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        Pinning.handle(challenge, completionHandler)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, isServerURL(url) || external.allows(url) {
            webView.load(URLRequest(url: url))   // same-server links open in place
        } else if let url = navigationAction.request.url {
            EventLog.shared.record(.blockedNavigation(url.host ?? url.absoluteString))
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let ns = error as NSError
        DebugLog.log("web", "page load failed: \(ns.domain) \(ns.code) \(ns.localizedDescription)")
        showOffline(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        let ns = error as NSError
        DebugLog.log("web", "page failed: \(ns.domain) \(ns.code) \(ns.localizedDescription)")
        showOffline(error)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if let u = webView.url { DebugLog.log("web", "page shown: \(u.host ?? u.scheme ?? "")\(u.path)") }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        DebugLog.log("crash", "exam page process crashed; reloading")
        reload()
    }

    private func showOffline(_ error: Error) {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        if ns.domain == "WebKitErrorDomain" && ns.code == 102 { return }   // interrupted by policy
        var retry = (ns.userInfo[NSURLErrorFailingURLErrorKey] as? URL) ?? Config.startURL
        if !isServerURL(retry) && !external.allows(retry) { retry = Config.startURL }
        let target = retry.absoluteString
            .replacingOccurrences(of: "\\", with: "")
            .replacingOccurrences(of: "'", with: "")
            .replacingOccurrences(of: "<", with: "")
            .replacingOccurrences(of: "\"", with: "")
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        font:16px -apple-system,system-ui,sans-serif;background:#f4f5f8;color:#111827}
        .card{background:#fff;border-radius:14px;padding:32px 36px;max-width:460px;
        box-shadow:0 2px 12px rgba(0,0,0,.08)}h1{font-size:22px;margin:0 0 10px}
        p{color:#4b5563;line-height:1.5}button{margin-top:12px;font:inherit;font-weight:600;
        padding:10px 18px;border:0;border-radius:10px;background:#2f4bd9;color:#fff}
        </style></head><body><div class="card">
        <h1>Cannot reach the exam server</h1>
        <p>FairPlay is trying again every 3 seconds. Answers you already typed are saved on this computer.</p>
        <p style="font-size:13px">\(server.absoluteString)</p>
        <button onclick="location.href='\(target)'">Try again now</button>
        </div><script>setTimeout(function(){ location.href='\(target)'; }, 3000);</script></body></html>
        """
        loadOwnHTML(html)
    }

    private func loadOwnHTML(_ html: String) {
        ownPageLoadPending = true
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - Accessibility permission panel (shown on top until granted)

    private func showPermission() {
        DebugLog.log("lock", "Accessibility permission screen shown")
        guard permissionView == nil, let content = window?.contentView else {
            startTrustPoll(); return
        }
        let v = NSView(frame: content.bounds)
        v.autoresizingMask = [.width, .height]
        v.wantsLayer = true
        v.layer?.backgroundColor = NSColor(calibratedRed: 0.08, green: 0.09, blue: 0.12, alpha: 1).cgColor

        let title = label("One permission before your exam", size: 28, weight: .bold)
        let body = label("""
        FairPlay locks this Mac during the exam so that no other app, website \
        or shortcut can be used. macOS needs your OK for that, once.

        1. Click "Open settings" below. It opens Privacy & Security > Accessibility
           (on newer macOS it is called "Device Control and Data Access").
        2. Turn ON "FairPlay". If it is already on, turn it off and on again.
        3. Come back here. This screen disappears by itself.
        """, size: 15, weight: .regular, color: NSColor(white: 0.85, alpha: 1))
        body.alignment = .left
        body.preferredMaxLayoutWidth = 600

        let status = label("Waiting for permission...", size: 14, weight: .semibold,
                           color: NSColor(calibratedRed: 1, green: 0.78, blue: 0.4, alpha: 1))
        permissionStatus = status

        let open = NSButton(title: "Open settings", target: self, action: #selector(openAccessibility))
        open.bezelStyle = .rounded

        let stack = NSStackView(views: [title, body, status, open])
        stack.orientation = .vertical; stack.spacing = 20; stack.alignment = .centerX
        stack.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: v.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: v.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: v.widthAnchor, constant: -40)
        ])
        content.addSubview(v, positioned: .above, relativeTo: webView)
        permissionView = v
        startTrustPoll()
    }

    private func startTrustPoll() {
        guard trustPoll == nil else { return }
        let t = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            if EventTap.isTrusted {
                self.hidePermission()
                self.sendState()
            } else {
                let now = Date().formatted(date: .omitted, time: .standard)
                self.permissionStatus?.stringValue = "Waiting for permission... (checked \(now))"
            }
        }
        RunLoop.main.add(t, forMode: .common)
        trustPoll = t
    }

    private func hidePermission() {
        trustPoll?.invalidate(); trustPoll = nil
        permissionView?.removeFromSuperview()
        permissionView = nil; permissionStatus = nil
    }

    @objc private func openAccessibility() {
        EventTap.requestTrust()
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: - LockSurface

    private var lockedLevel: NSWindow.Level {
        if Config.floatingLockedWindow { return .floating }   // dev builds only
        return NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.maximumWindow)))
    }

    func enterLockedMode() {
        guard let win = window else { return }
        normalFrame = win.frame
        win.styleMask = [.borderless]
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        win.level = lockedLevel
        win.hasShadow = false
        coverAllScreens()
        takeFocus()
        win.makeFirstResponder(webView)
    }

    func exitLockedMode() {
        guard let win = window else { return }
        win.level = .normal
        win.styleMask = MainWindowController.normalStyle
        win.collectionBehavior = [.fullScreenNone]
        win.hasShadow = true
        win.title = "FairPlay"
        win.setFrame(normalFrame, display: true)
        win.makeFirstResponder(webView)
    }

    func takeFocus() {
        guard let win = window else { return }
        win.makeKeyAndOrderFront(nil)
        win.orderFrontRegardless()
    }

    func coverAllScreens() {
        guard let win = window, LockdownController.shared.isLocked else { return }
        let union = NSScreen.screens.reduce(NSRect.zero) { $0.union($1.frame) }
        let target = union.isEmpty ? (NSScreen.main?.frame ?? win.frame) : union
        // Called every 3 s: only touch the window when the screens really changed.
        if win.frame != target { win.setFrame(target, display: true) }
    }

    private var blockingAction: (() -> Void)?
    private var blockingButton: NSButton?

    func showBlocking(reason: String, quitAction: (() -> Void)?) {
        blockingAction = quitAction
        if blockingView == nil { DebugLog.log("lock", "red blocking screen shown") }
        if blockingView != nil {
            blockingLabel?.stringValue = reason
            blockingButton?.isHidden = quitAction == nil
            return
        }
        guard let content = window?.contentView else { return }
        let v = NSView(frame: content.bounds)
        v.autoresizingMask = [.width, .height]
        v.wantsLayer = true
        v.layer?.backgroundColor = NSColor(calibratedRed: 0.55, green: 0.15, blue: 0.05, alpha: 0.97).cgColor
        let l = label(reason, size: 22, weight: .semibold)
        l.preferredMaxLayoutWidth = 760
        let b = NSButton(title: "Quit these apps now", target: self, action: #selector(blockingButtonPressed))
        b.bezelStyle = .rounded
        b.controlSize = .large
        b.isHidden = quitAction == nil
        let stack = NSStackView(views: [l, b])
        stack.orientation = .vertical; stack.spacing = 24; stack.alignment = .centerX
        stack.translatesAutoresizingMaskIntoConstraints = false
        v.addSubview(stack)
        content.addSubview(v)   // topmost
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: v.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: v.centerYAnchor),
            stack.widthAnchor.constraint(lessThanOrEqualTo: v.widthAnchor, constant: -80)
        ])
        blockingView = v; blockingLabel = l; blockingButton = b
    }

    @objc private func blockingButtonPressed() {
        blockingAction?()
    }

    func hideBlocking() {
        if blockingView != nil { DebugLog.log("lock", "red blocking screen hidden") }
        blockingView?.removeFromSuperview()
        blockingView = nil; blockingLabel = nil; blockingButton = nil; blockingAction = nil
    }

    /// Emergency exit or safety unlock while a website exam is open: leave it without submitting.
    func endExternalIfActive() {
        external.abort()
    }

    // MARK: - Invigilator emergency exit (Ctrl-Opt-Cmd-E while locked)

    /// Audit #5: the normal code is per exam, lives only on the server (shown in the
    /// teacher's live room) and has an attempt limit there.
    /// 2026-10-12: when the server cannot answer (offline, server error, exam never reached
    /// it) the invigilator uses the offline code instead (OfflineExit.swift), so a stuck
    /// lock never needs a Mac restart.
    func promptEmergencyExit() {
        DebugLog.log("exit", "Ctrl-Opt-Cmd-E pressed (locked=\(LockdownController.shared.isLocked))")
        guard LockdownController.shared.isLocked, let win = window, win.attachedSheet == nil else { return }
        let alert = NSAlert()
        alert.messageText = "Invigilator exit"
        alert.informativeText = "Enter the invigilator code from the teacher's live room to end locked mode. "
            + "The teacher will see this.\n\nNo connection or an error? Choose \"Offline code\"."
            + (Config.isDevBuild ? "\n\nDev build: 0000 also works." : "")
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
        field.placeholderString = "Invigilator code"
        alert.accessoryView = field
        alert.addButton(withTitle: "End locked mode")
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Offline code…")
        alert.window.initialFirstResponder = field
        LockdownController.shared.ownSecureField = true
        alert.beginSheetModal(for: win) { [weak self] response in
            LockdownController.shared.ownSecureField = false
            let choice: String
            if response == .alertFirstButtonReturn { choice = "End locked mode" }
            else if response == .alertThirdButtonReturn { choice = "Offline code" }
            else { choice = "Cancel" }
            DebugLog.log("exit", "exit dialog: " + choice)
            if response == .alertThirdButtonReturn {
                DispatchQueue.main.async { self?.promptOfflineExit(reason: nil) }
                return
            }
            guard response == .alertFirstButtonReturn else { return }
            let typed = field.stringValue
            guard !typed.isEmpty else { return }
            // Dev builds: the fixed test code ("0000") ends locked mode right here, no server
            // and no second dialog. Release builds compile this out (always false).
            if OfflineExit.devCodeMatches(typed.filter { !$0.isWhitespace && $0 != "-" }) {
                DebugLog.log("exit", "dev test code accepted (dev build only)")
                EventLog.shared.record(.offlineExit)
                LockdownController.shared.unlock()
                AppSession.shared.clearExam()   // after unlock: its last beat (locked=0) still needs the session
                return
            }
            AppSession.shared.checkExitCode(typed) { result in
                DebugLog.log("exit", "server exit-code result: \(result)")
                guard LockdownController.shared.isLocked else { return }
                switch result {
                case .ok:
                    LockdownController.shared.unlock()
                    AppSession.shared.clearExam()   // after unlock: its last beat (locked=0) still needs the session
                case .wrong:
                    NSSound.beep()
                    self?.notice("Wrong invigilator code. Press Ctrl-Opt-Cmd-E to try again.")
                case .tooMany:
                    self?.notice("Too many wrong codes. Wait 5 minutes.")   // server: WRONG_EXIT_LOCK_S
                case .unreachable:
                    self?.promptOfflineExit(reason: "The exam server cannot be reached.")
                case .noSession:
                    self?.promptOfflineExit(reason: "This exam has not reached the server yet.")
                case .serverError(let status):
                    self?.promptOfflineExit(reason: "The exam server answered with an error (\(status)).")
                }
            }
        }
    }

    /// Offline exit: show the Mac code, check the invigilator's answer locally.
    func promptOfflineExit(reason: String?) {
        guard LockdownController.shared.isLocked, let win = window, win.attachedSheet == nil else { return }
        guard OfflineExit.isAvailable else {
            DebugLog.log("exit", "offline exit not available: no offline key for this exam")
            notice((reason.map { $0 + "\n\n" } ?? "")
                   + "Offline exit is not available: this Mac never reached the exam server for this exam. "
                   + "If you must end the exam here, restart the Mac. Answers are saved on it.")
            return
        }
        let alert = NSAlert()
        alert.messageText = "Offline invigilator exit"
        var info = ""
        if let reason { info += reason + "\n\n" }
        info += "Mac code:  \(OfflineExit.challengeForDisplay)\n\n"
            + "Invigilator: in the teacher's live room choose \"Mac offline or stuck?\", pick this "
            + "student, type the Mac code and enter the 8-digit code here. "
            + "Answers stay saved on this Mac. This is logged."
        alert.informativeText = info
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
        field.placeholderString = "8-digit offline code"
        alert.accessoryView = field
        alert.addButton(withTitle: "End locked mode")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        LockdownController.shared.ownSecureField = true
        alert.beginSheetModal(for: win) { [weak self] response in
            LockdownController.shared.ownSecureField = false
            guard response == .alertFirstButtonReturn, LockdownController.shared.isLocked else {
                DebugLog.log("exit", "offline dialog cancelled")
                return
            }
            let result = OfflineExit.check(field.stringValue)
            DebugLog.log("exit", "offline code result: \(result) (Mac code \(OfflineExit.challengeForDisplay))")
            switch result {
            case .ok:
                EventLog.shared.record(.offlineExit)
                LockdownController.shared.unlock()
                AppSession.shared.clearExam()   // after unlock: its last beat (locked=0) still needs the session
            case .wrong:
                NSSound.beep()
                EventLog.shared.record(.offlineExitRejected)
                DispatchQueue.main.async { self?.promptOfflineExit(reason: "Wrong code, try again.") }
            case .tooMany:
                EventLog.shared.record(.offlineExitRejected)
                DispatchQueue.main.async { self?.notice("Too many wrong codes. Wait \(Int(OfflineExit.waitAfterWrong / 60)) minutes, then press Ctrl-Opt-Cmd-E.") }
            case .unavailable:
                DispatchQueue.main.async { self?.promptOfflineExit(reason: nil) }   // shows why
            }
        }
    }

    private func notice(_ text: String) {
        guard let win = window, win.attachedSheet == nil else { return }
        let a = NSAlert()
        a.messageText = "Invigilator exit"
        a.informativeText = text
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: win, completionHandler: nil)
    }

    // MARK: - Helpers

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
