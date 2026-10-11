import AppKit
import ApplicationServices
import Carbon.HIToolbox

extension Notification.Name {
    static let proctoraEmergencyExit = Notification.Name("ProctoraEmergencyExit")
}

/// Session-wide keyboard tap (same core as lockdown 0.2, which passed escape testing).
/// Sees every key before macOS acts on it; returning nil swallows it, so Spotlight,
/// screenshots, Cmd-Tab, Mission Control and app shortcuts never fire.
/// Needs the Accessibility permission; without it start() returns false.
final class EventTap {

    static let shared = EventTap()

    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private(set) var isRunning = false

    static var isTrusted: Bool { AXIsProcessTrusted() }

    static func requestTrust() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
    }

    func start() -> Bool {
        guard !isRunning else { return true }
        guard EventTap.isTrusted else { return false }

        let mask: CGEventMask =
            (1 << CGEventType.keyDown.rawValue) |
            (1 << CGEventType.keyUp.rawValue) |
            (1 << CGEventType.flagsChanged.rawValue)

        let callback: CGEventTapCallBack = { _, type, event, _ in
            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let t = EventTap.shared.tap { CGEvent.tapEnable(tap: t, enable: true) }
                return Unmanaged.passUnretained(event)
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
                // One log line per real press: skip key-up and auto-repeat.
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
        source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        isRunning = true
        return true
    }

    func stop() {
        guard isRunning, let tap else { return }
        CGEvent.tapEnable(tap: tap, enable: false)
        if let source { CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes) }
        self.tap = nil; self.source = nil
        isRunning = false
    }

    /// Ctrl-Opt-Cmd-E: invigilator emergency exit (asks for the invigilator code).
    static func isEmergencyCombo(_ event: CGEvent, type: CGEventType) -> Bool {
        guard type == .keyDown || type == .keyUp else { return false }
        let f = event.flags
        let key = Int(event.getIntegerValueField(.keyboardEventKeycode))
        return key == kVK_ANSI_E && f.contains(.maskCommand) && f.contains(.maskControl) && f.contains(.maskAlternate)
    }

    static func shouldSwallow(_ event: CGEvent, type: CGEventType) -> Bool {
        if type == .flagsChanged { return false }   // bare modifier presses must pass
        let flags = event.flags
        let key = Int(event.getIntegerValueField(.keyboardEventKeycode))
        let cmd = flags.contains(.maskCommand)
        let ctrl = flags.contains(.maskControl)
        let opt = flags.contains(.maskAlternate)
        let fn = flags.contains(.maskSecondaryFn)

        if cmd { return true }
        // Ctrl-Space stays allowed: it switches input source (pinyin etc.).
        if ctrl && [kVK_UpArrow, kVK_DownArrow, kVK_LeftArrow, kVK_RightArrow].contains(key) { return true }
        if ctrl && opt { return true }
        if [kVK_F3, kVK_F4, kVK_F11, kVK_F12].contains(key) { return true }
        if fn && [kVK_ANSI_E, kVK_F3, kVK_F4].contains(key) { return true }
        if key == kVK_Escape && opt { return true }
        return false
    }

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
