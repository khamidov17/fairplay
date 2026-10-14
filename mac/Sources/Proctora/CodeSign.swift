import Foundation
import Security

/// Review 2026-10-14: who a running process really is, from its code signature.
///
/// A bundle id or an app name is whatever the app's own Info.plist says, so a home-made
/// app could call itself "com.apple.x" or "com.cisco.anyconnect.notes". Every place that
/// grants trust by name (synthetic input from Apple helpers, system dialogs, VPN clients,
/// teacher-allowed apps) also asks this:
///  - `isApple`: signed by Apple itself ("anchor apple").
///  - `isDeveloperSigned`: signed with an Apple-issued certificate (App Store or Developer
///    ID, "anchor apple generic"). An ad-hoc or self-signed student build fails it.
/// Results are cached per pid for a short time (the event tap asks on every synthetic
/// event); the cache is safe to use from any thread.
enum CodeSign {
    private static let lock = NSLock()
    private static var cache: [Key: (ok: Bool, at: Date)] = [:]
    private static let ttl: TimeInterval = 30

    private struct Key: Hashable { let pid: pid_t; let requirement: String }

    static func isApple(pid: pid_t) -> Bool { satisfies(pid: pid, requirement: "anchor apple") }
    static func isDeveloperSigned(pid: pid_t) -> Bool { satisfies(pid: pid, requirement: "anchor apple generic") }

    static func satisfies(pid: pid_t, requirement: String) -> Bool {
        guard pid > 0 else { return false }
        let key = Key(pid: pid, requirement: requirement)
        let now = Date()
        lock.lock()
        if let hit = cache[key], now.timeIntervalSince(hit.at) < ttl { lock.unlock(); return hit.ok }
        lock.unlock()
        let ok = check(pid: pid, requirement: requirement)
        lock.lock()
        if cache.count > 512 { cache.removeAll() }
        cache[key] = (ok, now)
        lock.unlock()
        return ok
    }

    private static func check(pid: pid_t, requirement: String) -> Bool {
        var code: SecCode?
        let attrs = [kSecGuestAttributePid: NSNumber(value: pid)] as CFDictionary
        guard SecCodeCopyGuestWithAttributes(nil, attrs, [], &code) == errSecSuccess, let code else { return false }
        var req: SecRequirement?
        guard SecRequirementCreateWithString(requirement as CFString, [], &req) == errSecSuccess, let req else {
            return false
        }
        return SecCodeCheckValidity(code, [], req) == errSecSuccess
    }
}
