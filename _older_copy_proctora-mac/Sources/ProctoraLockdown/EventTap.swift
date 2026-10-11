import AppKit
import ApplicationServices
import Carbon.HIToolbox

/// A session-wide keyboard tap. Unlike NSEvent local monitors (which only see
/// keys already delivered to our app), a CGEventTap at the session level sees
/// every key before the system acts on it. Returning nil swallows the event,
/// so Spotlight (Cmd-Space), screenshots (Cmd-Shift-3/4/5), Cmd-Tab, Mission
/// Control keys and friends never fire. This is the mechanism SEB uses.
///
/// Requires Accessibility permission (System Settings > Privacy & Security >
/// Accessibility). Without it `start()` returns false and the app must not lock.
final class EventTap {

    static let shared = EventTap()

    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private(set) var isRunning = false

    /// True once the user has granted Accessibility to this binary.
    static var isTrusted: Bool { AXIsProcessTrusted() }

    /// Shows the system prompt that deep-links to the Accessibility pane.
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
            // The tap gets disabled if our callback is ever too slow; re-enable.
            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let t = EventTap.shared.tap { CGEvent.tapEnable(tap: t, enable: true) }
                return Unmanaged.passUnretained(event)
            }
            if EventTap.shouldSwallow(event, type: type) {
                EventLog.shared.record(.blockedShortcut(EventTap.describe(event)))
                return nil
            }
            return Unmanaged.passUnretained(event)
        }

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,          // active: we may swallow events
            eventsOfInterest: mask,
            callback: callback,
            userInfo: nil
        ) else {
            return false
        }

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

    // MARK: - Policy

    /// What to eat. Everything with Command is gone (no exam UI needs it),
    /// plus the control/function keys that reach the window manager.
    static func shouldSwallow(_ event: CGEvent, type: CGEventType) -> Bool {
        let flags = event.flags
        let key = Int(event.getIntegerValueField(.keyboardEventKeycode))
        let cmd = flags.contains(.maskCommand)
        let ctrl = flags.contains(.maskControl)
        let opt = flags.contains(.maskAlternate)
        let fn = flags.contains(.maskSecondaryFn)

        // Modifier press/release alone (flagsChanged) must pass, otherwise the
        // system sees a stuck modifier. We only judge real key events.
        if type == .flagsChanged { return false }

        if cmd { return true }                                   // Cmd-anything
        if ctrl && [kVK_UpArrow, kVK_DownArrow, kVK_LeftArrow,
                    kVK_RightArrow, kVK_Space].contains(key) { return true } // Spaces / Mission Control
        if ctrl && opt { return true }
        if [kVK_F3, kVK_F4, kVK_F11, kVK_F12].contains(key) { return true } // MC, Launchpad, desktop, Dashboard
        if fn && [kVK_ANSI_E, kVK_F3, kVK_F4].contains(key) { return true }   // globe/emoji, fn-F3/F4
        if key == kVK_Escape && opt { return true }              // Opt-Esc dictation on some layouts
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
