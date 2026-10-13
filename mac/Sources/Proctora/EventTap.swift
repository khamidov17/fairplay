import AppKit
import ApplicationServices
import Carbon.HIToolbox

extension Notification.Name {
    static let proctoraEmergencyExit = Notification.Name("ProctoraEmergencyExit")
}

/// Session-wide input tap (same core as lockdown 0.2, which passed escape testing).
/// Sees every key before macOS acts on it; returning nil swallows it, so Spotlight,
/// screenshots, Cmd-Tab, Mission Control and app shortcuts never fire.
/// Needs the Accessibility permission; without it start() returns false.
///
/// Audit 2026-10-12 additions: Opt-Space (ChatGPT/Raycast launchers), bare Fn/Globe and
/// Cmd presses (dictation, "Type to Siri"), media/dictation keys (system-defined events),
/// and keyboard/mouse events posted by other programs (remote-control tools, scripts).
final class EventTap {

    static let shared = EventTap()

    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private(set) var isRunning = false
    private static var lastSyntheticLog = Date.distantPast
    /// Audit M-16: the tap runs on its own thread, so a busy main thread (process scan,
    /// a dialog) never delays keys or mouse, and macOS doesn't time the tap out.
    private var thread: Thread?
    private var threadLoop: CFRunLoop?

    // State the tap thread reads. Written on the main thread at lock and by the checks
    // (simple values: a stale read for one event is harmless).
    /// Audit M-03: the user's own enabled system shortcuts (Spotlight, Mission Control,
    /// Shortcuts.app ... rebound to any key), read from com.apple.symbolichotkeys at lock.
    static var userHotkeys: Set<Hotkey> = []
    /// Audit M-09: VoiceOver on -> its Ctrl-Opt chords pass.
    static var voiceOverOn = false
    /// Audit M-08: last input posted by a non-Apple program (pid, app name, time).
    static var lastForeignInput: (pid: Int32, name: String, at: Date)?

    struct Hotkey: Hashable {
        let keyCode: Int
        let modifiers: UInt64       // CGEventFlags bits for Cmd / Ctrl / Opt / Shift / Fn
    }
    static let modifierMask: UInt64 = CGEventFlags.maskCommand.rawValue | CGEventFlags.maskControl.rawValue
        | CGEventFlags.maskAlternate.rawValue | CGEventFlags.maskShift.rawValue | CGEventFlags.maskSecondaryFn.rawValue

    /// AppleSymbolicHotKeys entries look like {"enabled": 1, "value": {"parameters":
    /// [ascii, keycode, modifiers], "type": "standard"}}; the modifier bits are the same
    /// values as CGEventFlags.
    static func loadUserHotkeys() -> Set<Hotkey> {
        guard let all = UserDefaults(suiteName: "com.apple.symbolichotkeys")?
            .dictionary(forKey: "AppleSymbolicHotKeys") else { return [] }
        var out: Set<Hotkey> = []
        for case let entry as [String: Any] in all.values {
            guard (entry["enabled"] as? Bool) == true || (entry["enabled"] as? Int) == 1,
                  let value = entry["value"] as? [String: Any],
                  let params = value["parameters"] as? [Any], params.count >= 3,
                  let code = (params[1] as? NSNumber)?.intValue, code >= 0, code != 65535,
                  let mods = (params[2] as? NSNumber)?.uint64Value else { continue }
            out.insert(Hotkey(keyCode: code, modifiers: mods & modifierMask))
        }
        return out
    }

    static var isTrusted: Bool { AXIsProcessTrusted() }

    static func requestTrust() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
    }

    /// NX_SYSDEFINED: media keys, dictation/Siri keys and the like.
    private static let systemDefinedType = CGEventType(rawValue: 14)!
    /// Aux keys that stay usable: volume, mute, brightness, keyboard backlight.
    private static let allowedAuxKeys: Set<Int> = [0, 1, 2, 3, 7, 21, 22, 23]

    func start() -> Bool {
        guard !isRunning else { return true }
        guard EventTap.isTrusted else { return false }

        EventTap.userHotkeys = EventTap.loadUserHotkeys()
        DebugLog.log("lock", "user shortcuts blocked from System Settings: \(EventTap.userHotkeys.count)")
        var mask: CGEventMask =
            (1 << CGEventType.keyDown.rawValue) |
            (1 << CGEventType.keyUp.rawValue) |
            (1 << CGEventType.flagsChanged.rawValue) |
            (1 << EventTap.systemDefinedType.rawValue)
        for t: CGEventType in [.leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp,
                               .otherMouseDown, .otherMouseUp, .scrollWheel] {
            mask |= (1 << t.rawValue)
        }
        // Audit M-03: trackpad gestures (NSEvent gesture/magnify/swipe/smart-magnify = 29-32).
        // Whether swallowing them stops Mission Control / Notification Center swipes is a
        // hand test; scrolling is a different event type and keeps working.
        for raw: UInt32 in EventTap.gestureTypes { mask |= (CGEventMask(1) << CGEventMask(raw)) }

        let callback: CGEventTapCallBack = { _, type, event, _ in
            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let t = EventTap.shared.tap { CGEvent.tapEnable(tap: t, enable: true) }
                return Unmanaged.passUnretained(event)
            }
            if EventTap.isSynthetic(event) {
                // Audit M-08: Apple's own helpers (Accessibility Keyboard, Voice Control,
                // Switch Control, Universal Control) post input too: log, don't swallow.
                // Any other program's input is swallowed and the student is told.
                let pid = Int32(truncatingIfNeeded: event.getIntegerValueField(.eventSourceUnixProcessID))
                let poster = EventTap.posterName(pid)
                let apple = poster.bundleId.hasPrefix("com.apple.")
                let now = Date()
                if now.timeIntervalSince(EventTap.lastSyntheticLog) > 5 {
                    EventTap.lastSyntheticLog = now
                    EventLog.shared.record(.syntheticInput("type=\(type.rawValue) pid=\(pid) \(poster.bundleId.isEmpty ? poster.name : poster.bundleId)\(apple ? " (Apple, allowed)" : "")"))
                }
                if !apple {
                    EventTap.lastForeignInput = (pid, poster.name, now)
                    return nil
                }
            }
            switch type {
            case .leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp,
                 .otherMouseDown, .otherMouseUp, .scrollWheel:
                return Unmanaged.passUnretained(event)
            default:
                break
            }
            if EventTap.gestureTypes.contains(type.rawValue) {
                return nil
            }
            if type == EventTap.systemDefinedType {
                return EventTap.allowSystemDefined(event) ? Unmanaged.passUnretained(event) : nil
            }
            let isRepeat = event.getIntegerValueField(.keyboardEventAutorepeat) != 0
            if EventTap.isEmergencyCombo(event, type: type) {
                if type == .keyDown && !isRepeat {
                    DispatchQueue.main.async {
                        NotificationCenter.default.post(name: .proctoraEmergencyExit, object: nil)
                    }
                }
                return nil
            }
            if EventTap.shouldSwallow(event, type: type) {
                // One log line per real press: skip key-up, modifiers and auto-repeat.
                if type == .keyDown && !isRepeat {
                    EventLog.shared.record(.blockedShortcut(EventTap.describe(event)))
                }
                return nil
            }
            return Unmanaged.passUnretained(event)
        }

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: mask,
            callback: callback,
            userInfo: nil
        ) else { return false }

        self.tap = tap
        guard let src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0) else { return false }
        source = src
        startThread(with: src)
        CGEvent.tapEnable(tap: tap, enable: true)
        isRunning = true
        return true
    }

    func stop() {
        guard isRunning, let tap else { return }
        CGEvent.tapEnable(tap: tap, enable: false)
        if let source, let loop = threadLoop {
            CFRunLoopRemoveSource(loop, source, .commonModes)
            CFRunLoopStop(loop)
        }
        self.tap = nil; self.source = nil
        thread = nil; threadLoop = nil
        isRunning = false
    }

    /// Runs the tap's run loop on a dedicated thread (audit M-16). Returns once it runs.
    private func startThread(with source: CFRunLoopSource) {
        let ready = DispatchSemaphore(value: 0)
        let t = Thread { [weak self] in
            let loop = CFRunLoopGetCurrent()
            self?.threadLoop = loop
            CFRunLoopAddSource(loop, source, .commonModes)
            ready.signal()
            CFRunLoopRun()
        }
        t.name = "fairplay.eventtap"
        t.qualityOfService = .userInteractive
        thread = t
        t.start()
        _ = ready.wait(timeout: .now() + 2)
    }

    static let gestureTypes: Set<UInt32> = [29, 30, 31, 32]

    /// Name and bundle id of the program that posted an event (pid from the event).
    static func posterName(_ pid: Int32) -> (name: String, bundleId: String) {
        if let app = NSRunningApplication(processIdentifier: pid) {
            return (app.localizedName ?? "pid \(pid)", app.bundleIdentifier ?? "")
        }
        var buf = [CChar](repeating: 0, count: 1024)
        if proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 {
            let path = String(cString: buf)
            // Apple system daemons live in /System or /usr/libexec.
            let apple = path.hasPrefix("/System/") || path.hasPrefix("/usr/libexec/") || path.hasPrefix("/usr/sbin/")
            return ((path as NSString).lastPathComponent, apple ? "com.apple.system" : "")
        }
        return ("pid \(pid)", "")
    }

    /// Periodic check (audit: "no check that the tap is still enabled"). Re-enables a tap
    /// macOS switched off; false when it cannot (Accessibility revoked, tap gone).
    func ensureHealthy() -> Bool {
        guard isRunning, let tap, EventTap.isTrusted else { return false }
        if !CGEvent.tapIsEnabled(tap: tap) {
            CGEvent.tapEnable(tap: tap, enable: true)
        }
        return CGEvent.tapIsEnabled(tap: tap)
    }

    /// Posted by another program (CGEventPost / remote-control tools) rather than coming
    /// from the keyboard, mouse or trackpad. TEST BY HAND with every input method you allow.
    static func isSynthetic(_ event: CGEvent) -> Bool {
        let pid = Int32(truncatingIfNeeded: event.getIntegerValueField(.eventSourceUnixProcessID))
        if pid == 0 || pid == ProcessInfo.processInfo.processIdentifier { return false }
        let state = event.getIntegerValueField(.eventSourceStateID)
        return state != Int64(CGEventSourceStateID.hidSystemState.rawValue)
    }

    /// Media / special keys: only volume, mute and brightness pass.
    static func allowSystemDefined(_ event: CGEvent) -> Bool {
        guard let ns = NSEvent(cgEvent: event), ns.type == .systemDefined else { return true }
        guard ns.subtype.rawValue == 8 else { return true }        // 8 = aux control buttons
        let key = Int((ns.data1 & 0xFFFF_0000) >> 16)
        return allowedAuxKeys.contains(key)
    }

    /// Ctrl-Opt-Cmd-E: invigilator emergency exit (asks for the invigilator code).
    static func isEmergencyCombo(_ event: CGEvent, type: CGEventType) -> Bool {
        guard type == .keyDown || type == .keyUp else { return false }
        let f = event.flags
        let key = Int(event.getIntegerValueField(.keyboardEventKeycode))
        return key == kVK_ANSI_E && f.contains(.maskCommand) && f.contains(.maskControl) && f.contains(.maskAlternate)
    }

    static func shouldSwallow(_ event: CGEvent, type: CGEventType) -> Bool {
        let key = Int(event.getIntegerValueField(.keyboardEventKeycode))
        if type == .flagsChanged {
            // Bare modifiers must pass (Shift, Ctrl, Opt for typing) -- except Fn/Globe
            // (dictation, emoji, input menu) and Cmd (double-press "Type to Siri").
            return key == kVK_Function || key == kVK_Command || key == kVK_RightCommand
        }
        let flags = event.flags
        let cmd = flags.contains(.maskCommand)
        let ctrl = flags.contains(.maskControl)
        let opt = flags.contains(.maskAlternate)
        let fn = flags.contains(.maskSecondaryFn)

        if cmd { return true }
        // Audit M-03: any shortcut the user enabled in System Settings (rebound Spotlight,
        // Mission Control, Shortcuts.app automations ...), whatever key it is on.
        let mods = flags.rawValue & modifierMask & ~CGEventFlags.maskSecondaryFn.rawValue
        let modsFn = flags.rawValue & modifierMask
        if userHotkeys.contains(Hotkey(keyCode: key, modifiers: modsFn))
            || userHotkeys.contains(Hotkey(keyCode: key, modifiers: mods)) { return true }
        // Audit M-09: VoiceOver's own Ctrl-Opt chords must work for blind students.
        if ctrl && opt && !voiceOverOn { return true }
        // Every function key (F1-F20: Mission Control, Launchpad, dictation, Ctrl-F2 menu
        // bar focus, Ctrl-F7/F8 ...). Brightness and volume arrive as system-defined media
        // events, not as these, so they still work.
        if functionKeys.contains(key) { return true }
        // Ctrl with anything but a letter (letters are text-editing keys). Ctrl-Space stays:
        // it switches input source (pinyin etc.).
        if ctrl && !(opt && voiceOverOn) && key != kVK_Space && !letterKeys.contains(key) { return true }
        // Fn/Globe with any key: emoji picker, dictation, app shortcuts.
        if fn && !arrowAndEditKeys.contains(key) { return true }
        if opt && key == kVK_Space { return true }       // ChatGPT / Raycast / Alfred launchers
        if key == kVK_Escape && opt { return true }
        return false
    }

    static let functionKeys: Set<Int> = [kVK_F1, kVK_F2, kVK_F3, kVK_F4, kVK_F5, kVK_F6, kVK_F7, kVK_F8,
                                         kVK_F9, kVK_F10, kVK_F11, kVK_F12, kVK_F13, kVK_F14, kVK_F15,
                                         kVK_F16, kVK_F17, kVK_F18, kVK_F19, kVK_F20]
    static let letterKeys: Set<Int> = [kVK_ANSI_A, kVK_ANSI_B, kVK_ANSI_C, kVK_ANSI_D, kVK_ANSI_E, kVK_ANSI_F,
                                       kVK_ANSI_G, kVK_ANSI_H, kVK_ANSI_I, kVK_ANSI_J, kVK_ANSI_K, kVK_ANSI_L,
                                       kVK_ANSI_M, kVK_ANSI_N, kVK_ANSI_O, kVK_ANSI_P, kVK_ANSI_Q, kVK_ANSI_R,
                                       kVK_ANSI_S, kVK_ANSI_T, kVK_ANSI_U, kVK_ANSI_V, kVK_ANSI_W, kVK_ANSI_X,
                                       kVK_ANSI_Y, kVK_ANSI_Z]
    /// On a laptop keyboard Fn + arrows / Delete are Page Up/Down, Home/End, forward delete.
    static let arrowAndEditKeys: Set<Int> = [kVK_UpArrow, kVK_DownArrow, kVK_LeftArrow, kVK_RightArrow,
                                             kVK_Delete, kVK_ForwardDelete, kVK_Return]

    static func describe(_ event: CGEvent) -> String {
        var parts: [String] = []
        let f = event.flags
        if f.contains(.maskCommand) { parts.append("Cmd") }
        if f.contains(.maskControl) { parts.append("Ctrl") }
        if f.contains(.maskAlternate) { parts.append("Opt") }
        if f.contains(.maskShift) { parts.append("Shift") }
        if f.contains(.maskSecondaryFn) { parts.append("fn") }
        parts.append("key\(event.getIntegerValueField(.keyboardEventKeycode))")
        return parts.joined(separator: "-")
    }
}
