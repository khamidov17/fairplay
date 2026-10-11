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
    private var normalFrame = NSRect(x: 0, y: 0, width: 1280, height: 820)
    private let server = Config.serverURL
    private let external = ExternalExamController(server: Config.serverURL)

    private static let normalStyle: NSWindow.StyleMask = [.titled, .closable, .miniaturizable, .resizable]

    convenience init() {
        let win = ExamWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                             styleMask: MainWindowController.normalStyle, backing: .buffered, defer: false)
        win.title = "FairPlay"
        win.minSize = NSSize(width: 900, height: 600)
        win.sharingType = .none                 // excluded from screenshots / screen recording
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
        external.attach(webView: wv, container: content, window: window)
    }

    func start() {
        showWindow(nil)
        takeFocus()
        webView.load(URLRequest(url: Config.startURL))
        if !EventTap.isTrusted { showPermission() }
    }

    func reload() {
        if webView.url == nil || webView.url?.scheme == "about" {
            webView.load(URLRequest(url: Config.startURL))
        } else {
            webView.reload()
        }
    }

    // MARK: - Bridge (JS <-> native)

    private func isServerURL(_ url: URL?) -> Bool {
        guard let url, let host = url.host else { return false }
        let port = url.port ?? (url.scheme == "https" ? 443 : 80)
        let serverPort = server.port ?? (server.scheme == "https" ? 443 : 80)
        return host == server.host && port == serverPort && url.scheme == server.scheme
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        // Only the exam server's own pages may drive the lock.
        let origin = message.frameInfo.securityOrigin
        let originPort = origin.port == 0 ? (origin.protocol == "https" ? 443 : 80) : origin.port
        let serverPort = server.port ?? (server.scheme == "https" ? 443 : 80)
        guard origin.host == server.host, originPort == serverPort else { return }
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }

        switch action {
        case "hello":
            sendState()
        case "examSession":
            // phase 1a: attempt id, token and exit-code verifier for the invigilator exit
            ExitCodeVerifier.shared.update(from: body)
        case "lock":
            if LockdownController.shared.lock() {
                hidePermission()
                send(["type": "locked"])
            } else {
                showPermission()
                send(["type": "lockFailed", "reason": "accessibility"])
            }
        case "openExternal":
            ExitCodeVerifier.shared.update(from: body)
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
        case "unlock":
            if LockdownController.shared.isLocked {
                LockdownController.shared.unlock()   // onUnlocked sends "unlocked"
            } else {
                send(["type": "unlocked"])
            }
        default:
            break
        }
    }

    func sendState() {
        send(["type": "state",
              "locked": LockdownController.shared.isLocked,
              "trusted": EventTap.isTrusted,
              "version": Config.version])
    }

    func sendEvent(type: String, detail: String, time: String) {
        if external.isActive {
            // Our JS page is gone while the exam website is open: the controller sends events itself.
            external.queueEvent(type: type, detail: detail, time: time)
            return
        }
        send(["type": "event", "event": ["type": type, "detail": detail, "client_time": time]])
    }

    func send(_ detail: [String: Any]) {
        guard let webView,
              let data = try? JSONSerialization.data(withJSONObject: detail),
              let json = String(data: data, encoding: .utf8) else { return }
        let js = "window.dispatchEvent(new CustomEvent('proctora-native', { detail: \(json) }));"
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    // MARK: - Navigation: only the exam server

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "about" || url.scheme == "data" || isServerURL(url) || external.allows(url) {
            if navigationAction.targetFrame?.isMainFrame ?? true { external.checkFinishURL(url) }
            decisionHandler(.allow)
        } else {
            EventLog.shared.record(.blockedNavigation(url.host ?? url.absoluteString))
            decisionHandler(.cancel)
        }
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
        showOffline(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showOffline(error)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
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
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - Accessibility permission panel (shown on top until granted)

    private func showPermission() {
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
        if UserDefaults.standard.string(forKey: "WindowLevel") == "floating" { return .floating }
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
        win.setFrame(union.isEmpty ? (NSScreen.main?.frame ?? win.frame) : union, display: true)
    }

    func showBlocking(reason: String) {
        if blockingView != nil { blockingLabel?.stringValue = reason; return }
        guard let content = window?.contentView else { return }
        let v = NSView(frame: content.bounds)
        v.autoresizingMask = [.width, .height]
        v.wantsLayer = true
        v.layer?.backgroundColor = NSColor(calibratedRed: 0.55, green: 0.15, blue: 0.05, alpha: 0.97).cgColor
        let l = label(reason, size: 22, weight: .semibold)
        v.addSubview(l)
        content.addSubview(v)   // topmost
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

    /// Emergency exit or safety unlock while a website exam is open: leave it without submitting.
    func endExternalIfActive() {
        external.abort()
    }

    // MARK: - Invigilator emergency exit (Ctrl-Opt-Cmd-E while locked)

    func promptEmergencyExit() {
        guard LockdownController.shared.isLocked, let win = window, win.attachedSheet == nil else { return }
        let alert = NSAlert()
        alert.messageText = "Invigilator exit"
        alert.informativeText = "Enter the invigilator code to end locked mode. The teacher will see this."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
        field.placeholderString = "Invigilator code"
        alert.accessoryView = field
        alert.addButton(withTitle: "End locked mode")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        alert.beginSheetModal(for: win) { [weak self] response in
            guard response == .alertFirstButtonReturn else { return }
            // phase 1a: per-exam code, checked by the server (offline verifier as fallback)
            ExitCodeVerifier.shared.verify(field.stringValue) { result in
                switch result {
                case .accepted(let how):
                    EventLog.shared.record(.emergencyExitAccepted(how))
                    LockdownController.shared.unlock()
                case .rejected:
                    EventLog.shared.record(.emergencyExitRejected)
                    NSSound.beep()
                case .tooManyTries:
                    EventLog.shared.record(.emergencyExitRejected)
                    self?.showExitMessage("Too many wrong codes. Wait 5 minutes, then try again.")
                case .unavailable:
                    self?.showExitMessage("No exit code is known for this computer yet. Ask the teacher to check the live room.")
                }
            }
        }
    }

    private func showExitMessage(_ text: String) {
        guard let win = window, win.attachedSheet == nil else { NSSound.beep(); return }
        let alert = NSAlert()
        alert.messageText = "Invigilator exit"
        alert.informativeText = text
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: win, completionHandler: nil)
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
