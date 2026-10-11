import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var main: MainWindowController?

    func applicationDidFinishLaunching(_ note: Notification) {
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
        NotificationCenter.default.addObserver(forName: .proctoraEmergencyExit, object: nil, queue: .main) { [weak wc] _ in
            wc?.promptEmergencyExit()
        }

        wc.start()
        NSApp.activate(ignoringOtherApps: true)

        print("""
        ------------------------------------------------------------
        FairPlay \(Config.version)  server: \(Config.serverURL.absoluteString)
        Safety auto-unlock: \(Config.safetyUnlockMinutes > 0 ? "\(Int(Config.safetyUnlockMinutes)) min" : "off")
        Invigilator exit while locked: Ctrl-Opt-Cmd-E, code \(Config.invigilatorCode)
        Log: \(EventLog.shared.logPath)
        ------------------------------------------------------------
        """)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        LockdownController.shared.isLocked ? .terminateCancel : .terminateNow
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        main?.takeFocus(); return true
    }

    @objc func reloadPage(_ sender: Any?) { main?.reload() }

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
