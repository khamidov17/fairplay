import AppKit

/// Dev builds only: a readable trace of what happened, written into the project folder
/// (fairplay/logs/events.log, path compiled in by build.sh) so it can be read without
/// digging in ~/Library. Clicks, button presses, lock/unlock, check results, app-quit
/// attempts, server requests, page navigation, JavaScript errors, crashes.
///
/// Never logs typed text, invigilator/offline codes, tokens or answers.
/// Release builds: BuildConfig.debugLogPath is "" and nothing is written.
enum DebugLog {

    private static let queue = DispatchQueue(label: "fairplay.debuglog")
    private static let maxBytes: UInt64 = 5 * 1024 * 1024
    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = .current
        return f
    }()

    static let url: URL? = {
        #if FAIRPLAY_DEV
        let path = Config.debugLogPath
        guard !path.isEmpty else { return nil }
        let u = URL(fileURLWithPath: path)
        try? FileManager.default.createDirectory(at: u.deletingLastPathComponent(), withIntermediateDirectories: true)
        return u
        #else
        return nil
        #endif
    }()

    static var isOn: Bool { url != nil }

    /// `area`: short tag such as "click", "lock", "net", "web", "js", "check", "quit", "exit".
    static func log(_ area: String, _ message: @autoclosure () -> String) {
        guard let url else { return }
        let text = message().replacingOccurrences(of: "\n", with: " | ")
        let line = "\(queue.sync { iso.string(from: Date()) }) [\(area)] \(text)\n"
        // A crash line must hit the disk before the process dies.
        if area == "crash" { queue.sync { write(line, to: url) } } else { queue.async { write(line, to: url) } }
    }

    private static func write(_ line: String, to url: URL) {
            guard let data = line.data(using: .utf8) else { return }
            let fm = FileManager.default
            if let size = (try? fm.attributesOfItem(atPath: url.path))?[.size] as? UInt64, size > maxBytes {
                let old = url.appendingPathExtension("1")
                try? fm.removeItem(at: old)
                try? fm.moveItem(at: url, to: old)
            }
            if let h = try? FileHandle(forWritingTo: url) {
                h.seekToEndOfFile(); h.write(data); try? h.close()
            } else {
                try? data.write(to: url)
            }
    }

    // MARK: - Hooks installed once at launch

    private static var clickMonitor: Any?

    static func installHooks() {
        guard isOn else { return }
        let info = ProcessInfo.processInfo
        log("app", "---- FairPlay \(Config.version) dev launched, pid \(info.processIdentifier), "
            + "macOS \(info.operatingSystemVersionString), server \(Config.serverURL.absoluteString), "
            + "accessibility \(EventTap.isTrusted ? "on" : "OFF")")

        NSSetUncaughtExceptionHandler { e in
            DebugLog.log("crash", "uncaught exception \(e.name.rawValue): \(e.reason ?? "") "
                         + e.callStackSymbols.prefix(8).joined(separator: " ; "))
        }

        // Every click in our own windows (exam window, alerts, blocking screen).
        clickMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { event in
            DebugLog.logClick(event)
            return event
        }

        let ws = NSWorkspace.shared.notificationCenter
        for (name, label) in [(NSWorkspace.didLaunchApplicationNotification, "launched"),
                              (NSWorkspace.didTerminateApplicationNotification, "ended"),
                              (NSWorkspace.didActivateApplicationNotification, "front")] {
            _ = ws.addObserver(forName: name, object: nil, queue: .main) { note in
                guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                      app.processIdentifier != info.processIdentifier || label != "front" else { return }
                DebugLog.log("apps", "\(label): \(DebugLog.describe(app))")
            }
        }
        _ = ws.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { _ in
            DebugLog.log("app", "Mac going to sleep")
        }
        _ = ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
            DebugLog.log("app", "Mac woke up")
        }
    }

    static func describe(_ app: NSRunningApplication) -> String {
        let policy: String
        switch app.activationPolicy {
        case .regular: policy = "window"
        case .accessory: policy = "menubar"
        default: policy = "background"
        }
        return "\(app.localizedName ?? "?") (\(app.bundleIdentifier ?? "no bundle id"), pid \(app.processIdentifier), \(policy))"
    }

    private static func logClick(_ event: NSEvent) {
        let kind = event.type == .rightMouseDown ? "right-click" : "click"
        guard let win = event.window else {
            log("click", "\(kind) outside our windows at \(NSEvent.mouseLocation)")
            return
        }
        let p = event.locationInWindow
        var target = "window"
        if let content = win.contentView, let hit = content.hitTest(content.convert(p, from: nil)) {
            target = describe(view: hit)
        }
        let title = win.title.isEmpty ? String(describing: type(of: win)) : win.title
        log("click", "\(kind) at (\(Int(p.x)),\(Int(p.y))) in \"\(title)\" on \(target)")
    }

    private static func describe(view: NSView) -> String {
        var v: NSView? = view
        while let cur = v {
            if let b = cur as? NSButton { return "button \"\(b.title)\"" }
            if cur is NSSecureTextField { return "code field" }
            if let t = cur as? NSTextField { return t.isEditable ? "text field" : "label \"\(t.stringValue.prefix(40))\"" }
            if cur.className.contains("WKWebView") || cur is ExamWebView { return "exam page (see [js] click line)" }
            v = cur.superview
        }
        return String(describing: type(of: view))
    }

    /// Injected into the exam page in dev builds: clicks (element, not text typed),
    /// JavaScript errors, unhandled promise rejections, console.error/warn.
    static let pageScript = """
    (function () {
      function post(kind, msg) {
        try { window.webkit.messageHandlers.proctora.postMessage({ action: 'log', kind: kind, msg: String(msg).slice(0, 600) }); } catch (e) {}
      }
      function el(t) {
        if (!t || !t.tagName) return '?';
        var s = t.tagName.toLowerCase();
        if (t.id) s += '#' + t.id;
        if (t.getAttribute && t.getAttribute('aria-label')) s += '[' + t.getAttribute('aria-label') + ']';
        if (t.type === 'password' || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA') return s;
        var txt = (t.innerText || t.value || '').trim().replace(/\\s+/g, ' ').slice(0, 50);
        return txt ? s + ' "' + txt + '"' : s;
      }
      document.addEventListener('click', function (e) {
        var t = e.target && e.target.closest ? (e.target.closest('button,a,input,select,label,[role=button]') || e.target) : e.target;
        post('click', el(t) + ' @ ' + location.pathname);
      }, true);
      window.addEventListener('error', function (e) {
        post('error', (e.message || 'error') + ' at ' + (e.filename || '') + ':' + (e.lineno || 0));
      });
      window.addEventListener('unhandledrejection', function (e) {
        var r = e.reason; post('error', 'unhandled promise: ' + (r && r.message ? r.message : r));
      });
      ['error', 'warn'].forEach(function (k) {
        var orig = console[k];
        console[k] = function () {
          try { post('console.' + k, Array.prototype.map.call(arguments, function (a) {
            return a && a.message ? a.message : (typeof a === 'object' ? JSON.stringify(a) : String(a)); }).join(' ')); } catch (e) {}
          return orig.apply(console, arguments);
        };
      });
      if (window.fetch) {
        var f = window.fetch;
        window.fetch = function (input, init) {
          var m = (init && init.method) || (input && input.method) || 'GET';
          var u = String((input && input.url) || input).split('?')[0];
          return f.apply(this, arguments).then(function (r) {
            if (!r.ok) post('fetch', m + ' ' + u + ' -> HTTP ' + r.status);
            return r;
          }, function (err) {
            post('fetch', m + ' ' + u + ' -> FAILED ' + (err && err.message ? err.message : err));
            throw err;
          });
        };
      }
      post('page', 'loaded ' + location.href.split('?')[0]);
    })();
    """
}
