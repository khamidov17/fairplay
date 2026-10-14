import Foundation
import CryptoKit

/// Invigilator exit that works WITHOUT the exam server (crash, Wi-Fi down, server error,
/// exam not yet on the server) -- so a stuck lock never needs a Mac restart.
///
/// Challenge-response:
///   1. The locked Mac shows a random "Mac code", e.g. K7QM-3XPA (new for every lock).
///   2. In the teacher's live room the invigilator picks the student, types the Mac code
///      and gets an 8-digit exit code (the server logs that it gave one).
///   3. exit code = first 4 bytes of HMAC-SHA256(K, "offline-exit|<MAC CODE>"),
///      big-endian, mod 10^8, zero-padded (same in app_client.offline_exit_code).
///      K = this attempt's offline key: derived from the SERVER secret per attempt and app
///      session, fetched by the app right after locking (AppSession.fetchOfflineKey),
///      kept in memory only. It is not in the binary, so extracting the app key with
///      `strings` does not give exit codes (review of 2026-10-12, audit #5).
/// No key (the app never reached the server for this exam): release builds have no
/// offline exit -- restart the Mac, answers are saved on it.
/// 5 wrong codes -> 5 minute wait. Every use is logged and reaches the teacher.
///
/// Dev builds only: without a key they fall back to the app key
/// (server/tools/offline_exit_code.py), and also accept a fixed code (default "0000",
///   defaults write ai.proctora.student DevExitCode 1234).
enum OfflineExit {

    private static let alphabet = Array("ABCDEFGHJKMNPQRSTUVWXYZ23456789")
    private static var current: String?
    private static var wrong = 0
    private static var lockedUntil = Date.distantPast
    static let maxWrong = 5
    static let waitAfterWrong: TimeInterval = 5 * 60

    /// "K7QM3XPA" (no dash). Stable until used or the lock ends.
    static var challenge: String {
        if let c = current { return c }
        var c = ""
        for _ in 0..<8 { c.append(alphabet[Int.random(in: 0..<alphabet.count)]) }
        current = c
        return c
    }

    /// "K7QM-3XPA" for display.
    static var challengeForDisplay: String {
        let c = challenge
        return String(c.prefix(4)) + "-" + String(c.suffix(4))
    }

    /// New challenge next time (after a successful exit or when a new lock starts).
    static func reset() {
        current = nil
        wrong = 0
        lockedUntil = .distantPast
    }

    /// The key exit codes are checked with, or nil when there is none (release, no key).
    private static var keyData: Data? {
        if let k = AppSession.shared.offlineKey { return k }
        #if FAIRPLAY_DEV
        return Data(Config.appKey.utf8)
        #else
        return nil
        #endif
    }

    static var isAvailable: Bool { keyData != nil || Config.isDevBuild }

    static func expectedCode(for challenge: String, key: Data) -> String {
        let mac = Array(HMAC<SHA256>.authenticationCode(for: Data("offline-exit|\(challenge)".utf8),
                                                        using: SymmetricKey(data: key)))
        let n = (UInt32(mac[0]) << 24) | (UInt32(mac[1]) << 16) | (UInt32(mac[2]) << 8) | UInt32(mac[3])
        return String(format: "%08u", n % 100_000_000)
    }

    enum Result { case ok, wrong, tooMany, unavailable }

    static func check(_ typed: String) -> Result {
        let digits = typed.filter { !$0.isWhitespace && $0 != "-" }
        if Date() >= lockedUntil, devCodeMatches(digits) { return .ok }
        return check(typed, key: keyData, now: Date())
    }

    /// The rule itself with the key and the clock passed in (unit-tested): 5 wrong codes
    /// -> wait `waitAfterWrong`, during which even the right code is refused.
    static func check(_ typed: String, key: Data?, now: Date) -> Result {
        if now < lockedUntil { return .tooMany }
        let digits = typed.filter { !$0.isWhitespace && $0 != "-" }
        guard let key else { return .unavailable }
        if AppSession.constantTimeEqual(digits, expectedCode(for: challenge, key: key)) { return .ok }
        wrong += 1
        if wrong >= maxWrong {
            wrong = 0
            lockedUntil = now.addingTimeInterval(waitAfterWrong)
            return .tooMany
        }
        return .wrong
    }

    /// Dev builds only: the fixed test code (default "0000"). Compiled out of release
    /// builds (no FAIRPLAY_DEV flag), where this is always false.
    static func devCodeMatches(_ typed: String) -> Bool {
        #if FAIRPLAY_DEV
        let dev = UserDefaults.standard.string(forKey: "DevExitCode") ?? "0000"
        return !dev.isEmpty && AppSession.constantTimeEqual(typed, dev)
        #else
        return false
        #endif
    }
}
