import AppKit
import Carbon
import CoreGraphics
import Darwin

/// Checks run at lock and every few seconds while locked (audit 2026-10-12 #7, #8, #9,
/// tap-alive and input-source items). Each returns facts; LockdownController decides.
enum SystemChecks {

    // MARK: - Virtual machine (#8)

    static func isVirtualMachine() -> Bool {
        var flag: Int32 = 0
        var size = MemoryLayout<Int32>.size
        if sysctlbyname("kern.hv_vmm_present", &flag, &size, nil, 0) == 0, flag == 1 { return true }
        let model = sysctlString("hw.model").lowercased()
        return ["virtual", "vmware", "parallels", "qemu", "vbox"].contains { model.contains($0) }
    }

    static func sysctlString(_ name: String) -> String {
        var size = 0
        guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return "" }
        var buf = [CChar](repeating: 0, count: size + 1)
        guard sysctlbyname(name, &buf, &size, nil, 0) == 0 else { return "" }
        return String(cString: buf)
    }

    // MARK: - Displays incl. mirroring / AirPlay / Sidecar (#9)

    struct Displays { let online: Int; let mirrored: Bool }

    /// NSScreen.screens counts a mirror set as one screen; the online display list does not.
    static func displays() -> Displays {
        var count: UInt32 = 0
        guard CGGetOnlineDisplayList(0, nil, &count) == .success, count > 0 else {
            return Displays(online: NSScreen.screens.count, mirrored: false)
        }
        var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
        guard CGGetOnlineDisplayList(count, &ids, &count) == .success else {
            return Displays(online: NSScreen.screens.count, mirrored: false)
        }
        let list = ids.prefix(Int(count))
        let mirrored = list.contains { CGDisplayIsInMirrorSet($0) != 0 }
        return Displays(online: list.count, mirrored: mirrored)
    }

    // MARK: - Remote session

    /// False when this login session is not on the Mac's own screen (remote login).
    static func sessionIsOnConsole() -> Bool {
        guard let dict = CGSessionCopyCurrentDictionary() as? [String: Any] else { return true }
        return (dict["kCGSSessionOnConsoleKey"] as? Bool) ?? true
    }

    // MARK: - Processes (#7)

    struct Proc {
        let pid: pid_t
        let ppid: pid_t
        let uid: uid_t
        let name: String
    }

    static func processes() -> [Proc] {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
        var size = 0
        guard sysctl(&mib, 4, nil, &size, nil, 0) == 0, size > 0 else { return [] }
        let stride = MemoryLayout<kinfo_proc>.stride
        var list = [kinfo_proc](repeating: kinfo_proc(), count: size / stride + 32)
        size = list.count * stride
        guard sysctl(&mib, 4, &list, &size, nil, 0) == 0 else { return [] }
        return list.prefix(size / stride).map { kp in
            var comm = kp.kp_proc.p_comm
            let name = withUnsafeBytes(of: &comm) { raw -> String in
                String(decoding: raw.prefix(while: { $0 != 0 }), as: UTF8.self)
            }
            return Proc(pid: kp.kp_proc.p_pid, ppid: kp.kp_eproc.e_ppid,
                        uid: kp.kp_eproc.e_ucred.cr_uid, name: name)
        }
    }

    /// Remote control, screen sharing, AI assistants/overlays and launchers. Lowercase
    /// bundle-id prefixes; matched against every running app, background ones included.
    static let deniedBundlePrefixes: [String] = [
        "com.teamviewer", "com.anydesk", "com.rustdesk", "com.parsec", "com.google.chromeremotedesktop",
        "com.logmein", "com.realvnc", "com.splashtop", "com.nomachine", "com.microsoft.rdc",
        "com.apple.screensharing", "com.todesk", "com.sunlogin", "com.oray", "com.hnc.discord",
        "us.zoom", "com.openai.chat", "com.anthropic.claude", "com.raycast", "com.runningwithcrayons.alfred",
        "com.cluely", "ai.perplexity", "com.microsoft.copilot", "com.google.gemini", "com.deepseek",
        "com.moonshot", "com.bytedance", "com.alibaba.tongyi", "com.baidu", "com.electron.chatgpt",
    ]

    /// Same idea for processes without a bundle (lowercase "contains" on the process name).
    static let deniedProcessNames: [String] = [
        "teamviewer", "anydesk", "rustdesk", "parsec", "remotedesktop", "screensharingd",
        "screensharingagent", "vncserver", "x11vnc", "todesk", "sunlogin", "sshd",
    ]

    /// Scripts left running by the student (e.g. `sleep 600; killall FairPlay` started
    /// before the exam): user-owned, parent launchd, shell/sleep/osascript. Ended at lock.
    static let killableOrphans: Set<String> = [
        "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "sleep", "osascript", "nohup",
        "expect", "killall", "pkill", "kill", "at", "caffeinate",
    ]
    /// Reported, not ended (could be a real background service).
    static let reportedOrphans: Set<String> = ["python", "python3", "perl", "ruby", "node", "tmux", "screen"]

    struct ProcessFindings {
        var deniedApps: [NSRunningApplication] = []
        var deniedProcs: [Proc] = []
        var orphanScripts: [Proc] = []
        var orphanReported: [Proc] = []
    }

    static func scanProcesses() -> ProcessFindings {
        var f = ProcessFindings()
        let me = ProcessInfo.processInfo.processIdentifier
        for app in NSWorkspace.shared.runningApplications where app.processIdentifier != me {
            guard let id = app.bundleIdentifier?.lowercased() else { continue }
            if Allowlist.keeps(app.bundleIdentifier ?? "") { continue }
            if deniedBundlePrefixes.contains(where: { id.hasPrefix($0) }) { f.deniedApps.append(app) }
        }
        let appPids = Set(f.deniedApps.map { $0.processIdentifier })
        let uid = getuid()
        for p in processes() where p.pid != me && p.pid > 1 {
            let lower = p.name.lowercased()
            if !appPids.contains(p.pid), deniedProcessNames.contains(where: { lower.contains($0) }) {
                f.deniedProcs.append(p)
            } else if p.uid == uid && p.ppid == 1 {
                if killableOrphans.contains(lower) { f.orphanScripts.append(p) }
                else if reportedOrphans.contains(lower) { f.orphanReported.append(p) }
            }
        }
        return f
    }

    // MARK: - Keyboard input source (third-party IMEs with cloud/AI candidates)

    static func currentInputSourceID() -> String? {
        guard let src = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue(),
              let ptr = TISGetInputSourceProperty(src, kTISPropertyInputSourceID) else { return nil }
        return Unmanaged<CFString>.fromOpaque(ptr).takeUnretainedValue() as String
    }

    /// Apple's own keyboards and input methods only (ABC, Russian, Uzbek, Pinyin - Simplified...).
    static func inputSourceAllowed(_ id: String) -> Bool {
        id.hasPrefix("com.apple.")
    }

    /// Switch to an Apple ASCII-capable keyboard; false if none is enabled.
    @discardableResult
    static func selectAppleKeyboard() -> Bool {
        let filter: [String: Any] = [(kTISPropertyInputSourceIsASCIICapable! as String): true,
                                     (kTISPropertyInputSourceCategory! as String): (kTISCategoryKeyboardInputSource! as String)]
        guard let list = TISCreateInputSourceList(filter as CFDictionary, false)?.takeRetainedValue() as? [TISInputSource] else {
            return false
        }
        for src in list {
            guard let ptr = TISGetInputSourceProperty(src, kTISPropertyInputSourceID) else { continue }
            let id = Unmanaged<CFString>.fromOpaque(ptr).takeUnretainedValue() as String
            if inputSourceAllowed(id), TISSelectInputSource(src) == 0 { return true }
        }
        return false
    }
}
