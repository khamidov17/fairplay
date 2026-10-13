import AppKit
import Carbon
import CoreGraphics
import Darwin
import IOKit

/// Checks run at lock and every few seconds while locked (audit 2026-10-12 #7, #8, #9,
/// tap-alive and input-source items). Each returns facts; LockdownController decides.
enum SystemChecks {

    // MARK: - Virtual machine (#8)

    static let vmWords = ["virtual", "vmware", "parallels", "qemu", "vbox", "virtualbox", "utm", "apple virtual"]

    /// Audit M-10: three independent signals, any one is enough. A VM that hides one of
    /// them (patched hv_vmm_present, stock hw.model) still shows another.
    static func isVirtualMachine() -> Bool {
        var flag: Int32 = 0
        var size = MemoryLayout<Int32>.size
        if sysctlbyname("kern.hv_vmm_present", &flag, &size, nil, 0) == 0, flag == 1 { return true }
        let model = sysctlString("hw.model").lowercased()
        if vmWords.contains(where: { model.contains($0) }) { return true }
        // Intel: the CPU's "hypervisor present" bit.
        if sysctlString("machdep.cpu.features").uppercased().split(separator: " ").contains("VMM") { return true }
        return platformLooksVirtual()
    }

    /// IOKit platform entry: manufacturer / model strings of the machine itself.
    static func platformLooksVirtual() -> Bool {
        let entry = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
        guard entry != 0 else { return false }
        defer { IOObjectRelease(entry) }
        var text = ""
        for key in ["manufacturer", "model", "product-name"] {
            if let v = IORegistryEntryCreateCFProperty(entry, key as CFString, kCFAllocatorDefault, 0)?
                .takeRetainedValue() as? Data {
                text += String(decoding: v.prefix(while: { $0 != 0 }), as: UTF8.self).lowercased() + " "
            }
        }
        return vmWords.contains { text.contains($0) }
    }

    /// Audit M-07: the Mac's hardware UUID (same after reinstalling FairPlay or copying
    /// its files to another Mac), or nil when IOKit does not give one.
    static func hardwareUUID() -> String? {
        let entry = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
        guard entry != 0 else { return nil }
        defer { IOObjectRelease(entry) }
        let v = IORegistryEntryCreateCFProperty(entry, kIOPlatformUUIDKey as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? String
        return (v?.isEmpty ?? true) ? nil : v
    }

    static func sysctlString(_ name: String) -> String {
        var size = 0
        guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return "" }
        var buf = [CChar](repeating: 0, count: size + 1)
        guard sysctlbyname(name, &buf, &size, nil, 0) == 0 else { return "" }
        return String(cString: buf)
    }

    // MARK: - Displays incl. mirroring / AirPlay / Sidecar (#9)

    struct Displays {
        let online: Int
        let mirrored: Bool
        /// Audit M-10: which screens, so a phantom (DisplayLink, dock) can be named.
        var names: [String] = []
    }

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
        let names = NSScreen.screens.map { $0.localizedName }
        return Displays(online: list.count, mirrored: mirrored, names: names)
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
        /// Audit M-12: when the process started.
        var started: Date = .distantPast
    }

    /// Audit M-13: nil when the list can't be read. Processes started between the size
    /// query and the fetch make the fetch fail with ENOMEM: ask again with more room.
    static func processes() -> [Proc]? {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
        let stride = MemoryLayout<kinfo_proc>.stride
        for attempt in 0..<4 {
            var size = 0
            guard sysctl(&mib, 4, nil, &size, nil, 0) == 0, size > 0 else { return nil }
            var list = [kinfo_proc](repeating: kinfo_proc(), count: size / stride + 64 << attempt)
            size = list.count * stride
            if sysctl(&mib, 4, &list, &size, nil, 0) != 0 {
                if errno == ENOMEM { continue }
                return nil
            }
            return list.prefix(size / stride).map { kp in
                var comm = kp.kp_proc.p_comm
                let name = withUnsafeBytes(of: &comm) { raw -> String in
                    String(decoding: raw.prefix(while: { $0 != 0 }), as: UTF8.self)
                }
                let tv = kp.kp_proc.p_un.__p_starttime
                let started = Date(timeIntervalSince1970: Double(tv.tv_sec) + Double(tv.tv_usec) / 1_000_000)
                return Proc(pid: kp.kp_proc.p_pid, ppid: kp.kp_eproc.e_ppid,
                            uid: kp.kp_eproc.e_ucred.cr_uid, name: name, started: started)
            }
        }
        return nil
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
        /// M-13: the process list could not be read ("proc=?" in the beat).
        var listFailed = false
    }

    /// Audit M-12: a leftover script is ended only if it started shortly before the lock
    /// (or after it); older ones are someone's real background job and are only reported.
    static let orphanWindow: TimeInterval = 10 * 60

    static func scanProcesses(lockedAt: Date?) -> ProcessFindings {
        var f = ProcessFindings()
        let me = ProcessInfo.processInfo.processIdentifier
        for app in NSWorkspace.shared.runningApplications where app.processIdentifier != me {
            guard let id = app.bundleIdentifier?.lowercased() else { continue }
            if Allowlist.keeps(app.bundleIdentifier ?? "") { continue }
            if deniedBundlePrefixes.contains(where: { id.hasPrefix($0) }) { f.deniedApps.append(app) }
        }
        let appPids = Set(f.deniedApps.map { $0.processIdentifier })
        let uid = getuid()
        guard let procs = processes() else {
            f.listFailed = true
            return f
        }
        let since = (lockedAt ?? Date()).addingTimeInterval(-orphanWindow)
        for p in procs where p.pid != me && p.pid > 1 {
            let lower = p.name.lowercased()
            if !appPids.contains(p.pid), deniedProcessNames.contains(where: { lower.contains($0) }) {
                f.deniedProcs.append(p)
            } else if p.uid == uid && p.ppid == 1 {
                if killableOrphans.contains(lower) && p.started >= since { f.orphanScripts.append(p) }
                else if killableOrphans.contains(lower) || reportedOrphans.contains(lower) { f.orphanReported.append(p) }
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
