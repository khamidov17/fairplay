import AppKit

// `--watchdog <pid>`: the helper copy started while locked (Watchdog.swift).
if CommandLine.arguments.count >= 3, CommandLine.arguments[1] == "--watchdog",
   let pid = Int32(CommandLine.arguments[2]) {
    Watchdog.run(parent: pid)
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var main: MainWindowController?

    func applicationDidFinishLaunching(_ note: Notification) {
        DebugLog.installHooks()
        NSApp.setActivationPolicy(.regular)
        buildMenu()

        let wc = MainWindowController()
        main = wc
        let lock = LockdownController.shared
        lock.surface = wc
        lock.onUnlocked = { [weak wc] in
            wc?.endExternalIfActive()
            wc?.send(["type": "unlocked"])
        }
        EventLog.shared.onEvent = { [weak wc] type, detail, time in
            wc?.sendEvent(type: type, detail: detail, time: time)
        }
        AppSession.shared.onStatus = { [weak wc] status in
            wc?.handleServerStatus(status)
        }
        NotificationCenter.default.addObserver(forName: .proctoraEmergencyExit, object: nil, queue: .main) { [weak wc] _ in
            wc?.promptEmergencyExit()
        }
        if Watchdog.cleanUpAtLaunch() {
            // The last run ended while locked (killed / crashed). The page forwards this
            // once the student is back in the exam; the server also sees the beat gap.
            EventLog.shared.record(.previousRunKilled)
            DebugLog.log("app", "last run ended while LOCKED (killed, crashed or Mac restarted)")
        }

        wc.start()
        NSApp.activate(ignoringOtherApps: true)

        print("""
        ------------------------------------------------------------
        FairPlay \(Config.version) (\(Config.isDevBuild ? "dev" : "release") build)  server: \(Config.serverURL.absoluteString)
        Safety auto-unlock: \(Config.safetyUnlockMinutes > 0 ? "\(Int(Config.safetyUnlockMinutes)) min" : "off")
        Invigilator exit while locked: Ctrl-Opt-Cmd-E, code from the teacher's live room
        No server / error: "Offline code..." in that dialog\(Config.isDevBuild ? " (dev builds also accept 0000)" : "")
        TLS pins: \(Pinning.enabled ? "\(Config.pins.count)" : "none")   device: \(AppSession.shared.deviceId)
        Log: \(EventLog.shared.logPath)
        ------------------------------------------------------------
        """)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let locked = LockdownController.shared.isLocked
        DebugLog.log("app", locked ? "quit requested while locked: refused" : "quitting")
        return locked ? .terminateCancel : .terminateNow
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        main?.takeFocus(); return true
    }

    @objc func reloadPage(_ sender: Any?) {
        // Audit M-18: a reload during the exam drops page state; not while locked.
        guard !LockdownController.shared.isLocked else {
            DebugLog.log("app", "View > Reload ignored while locked")
            return
        }
        DebugLog.log("app", "View > Reload")
        main?.reload()
    }

    private func buildMenu() {
        let bar = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About FairPlay",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit FairPlay", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        bar.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        bar.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "View")
        let reload = NSMenuItem(title: "Reload", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        reload.target = self
        view.addItem(reload)
        viewItem.submenu = view
        bar.addItem(viewItem)

        NSApp.mainMenu = bar
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
