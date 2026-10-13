import Foundation
import CryptoKit

/// The app's own link to the server for the attempt it is locked for
/// (audit 2026-10-12 #1, #2, #5, #6, #11; CONTRACT.md "App session").
///
/// - signs start challenges ("start|<nonce>|<device_id>") so the server knows the
///   attempt is started from FairPlay, not a browser;
/// - sends a signed beat every 5 s with the REAL lock state and check results; the
///   server takes "locked" only from these and flags gaps (app killed / Mac asleep);
/// - checks with the server before unlocking (the page alone cannot unlock) and for the
///   invigilator exit code (never stored in the app).
final class AppSession {

    static let shared = AppSession()

    struct Exam {
        let attemptId: String
        let token: String
        let session: String
    }

    private(set) var exam: Exam?
    /// Key for the offline invigilator exit (OfflineExit.swift), fetched by the app itself
    /// right after the exam session is known. Per attempt + session, from the server
    /// secret; never in the binary, never seen by the page. Memory only.
    private(set) var offlineKey: Data?
    private var offlineKeyTries = 0
    let deviceId: String
    /// Short check summary for beats, e.g. "vm=0,disp=1,mir=0,tap=1,inp=1,proc=0".
    var checksProvider: (() -> String)?
    /// Called on the main thread with the attempt status from each beat ("submitted"...).
    var onStatus: ((String) -> Void)?

    private var beatTimer: Timer?
    private var lastTs: Int64 = 0

    private init() {
        deviceId = AppSession.loadDeviceId()
    }

    // MARK: - Identity + signing

    /// Random id per Mac user account, kept in Application Support/FairPlay/device-id.
    private static func loadDeviceId() -> String {
        let fm = FileManager.default
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first ?? fm.temporaryDirectory
        let folder = base.appendingPathComponent("FairPlay", isDirectory: true)
        try? fm.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent("device-id")
        if let s = try? String(contentsOf: url, encoding: .utf8) {
            let id = s.trimmingCharacters(in: .whitespacesAndNewlines)
            if AppSession.isSafeToken(id, min: 8, max: 64) { return id }
        }
        let id = UUID().uuidString.lowercased()
        try? id.write(to: url, atomically: true, encoding: .utf8)
        return id
    }

    /// ASCII letters, digits, "-" and "_" only (no "|" so signed messages stay unambiguous).
    static func isSafeToken(_ s: String, min: Int, max: Int) -> Bool {
        guard s.count >= min, s.count <= max else { return false }
        return s.unicodeScalars.allSatisfy {
            ($0.value >= 48 && $0.value <= 57) || ($0.value >= 65 && $0.value <= 90)
                || ($0.value >= 97 && $0.value <= 122) || $0 == "-" || $0 == "_"
        }
    }

    /// Lowercase hex HMAC-SHA256 with the compiled-in app key (same as Python hmac.hexdigest()).
    static func sign(_ message: String) -> String {
        let key = SymmetricKey(data: Data(Config.appKey.utf8))
        let mac = HMAC<SHA256>.authenticationCode(for: Data(message.utf8), using: key)
        return mac.map { String(format: "%02x", $0) }.joined()
    }

    /// Bridge reply to {"action": "attest", "nonce": ...}.
    func attestation(nonce: String) -> [String: Any] {
        guard AppSession.isSafeToken(nonce, min: 8, max: 128) else {
            return ["type": "attestationFailed", "nonce": nonce, "reason": "bad_nonce"]
        }
        return ["type": "attestation", "nonce": nonce, "device_id": deviceId,
                "signature": AppSession.sign("start|\(nonce)|\(deviceId)"),
                "version": Config.version, "build": Config.isDevBuild ? "dev" : "release"]
    }

    // MARK: - Exam session + beats

    /// From the exam page (bridge "session" / "lock" / "openExternal"). Returns false (and
    /// changes nothing) when malformed -- audit M-06: "lock" then fails instead of locking
    /// with no session, no beats and an unlock nobody checks.
    @discardableResult
    func setExam(attemptId: String?, token: String?, session: String?) -> Bool {
        guard let attemptId, let token, let session,
              AppSession.isSafeToken(attemptId, min: 3, max: 32),
              AppSession.isSafeToken(session, min: 8, max: 64),
              !token.isEmpty, token.count < 4000, !token.contains("\n") else {
            DebugLog.log("exam", "exam session REFUSED (malformed attempt/token/session)")
            return false
        }
        // Audit 2026-10-13: while locked, the lock belongs to the attempt it was taken for.
        // The page can't swap in another attempt (whose hand-in / "gone" would then end
        // this lock); it gets the same attempt with a new session or token only.
        if LockdownController.shared.isLocked, let cur = exam, cur.attemptId != attemptId {
            DebugLog.log("exam", "exam session REFUSED: locked for \(cur.attemptId), page asked for \(attemptId)")
            EventLog.shared.record(.blockedNavigation("another exam while locked"))
            return false
        }
        let moved = exam?.attemptId != attemptId || exam?.session != session
        let changed = moved || exam?.token != token
        exam = Exam(attemptId: attemptId, token: token, session: session)
        if changed {
            DebugLog.log("exam", "exam session set: attempt \(attemptId)\(moved ? "" : " (new token)")")
            Watchdog.update(exam: exam)
            startBeats()
        }
        if moved {
            // Audit 2026-10-13: a token-only refresh keeps the key; asking again would be
            // refused (one key per session) and flagged as "requested twice".
            offlineKey = nil
            offlineKeyTries = 0
            fetchOfflineKey()
        }
        return true
    }

    func clearExam() {
        DebugLog.log("exam", "exam session cleared")
        beatTimer?.invalidate(); beatTimer = nil
        exam = nil
        offlineKey = nil
        Watchdog.update(exam: nil)
    }

    private func startBeats() {
        beatTimer?.invalidate()
        let t = Timer(timeInterval: 5, repeats: true) { [weak self] _ in self?.beat() }
        RunLoop.main.add(t, forMode: .common)
        beatTimer = t
        beat()
    }

    /// Also called right after lock/unlock so the teacher sees the change at once.
    func beat() {
        guard let exam else { return }
        var ts = Int64(Date().timeIntervalSince1970 * 1000)
        if ts <= lastTs { ts = lastTs + 1 }
        lastTs = ts
        let locked = LockdownController.shared.isLocked
        let checks = String((checksProvider?() ?? "").prefix(200))
        let msg = "beat|\(exam.attemptId)|\(exam.session)|\(deviceId)|\(ts)|\(locked ? 1 : 0)|\(checks)"
        let body: [String: Any] = ["session": exam.session, "device_id": deviceId, "ts": NSNumber(value: ts),
                                   "locked": locked, "checks": checks, "signature": AppSession.sign(msg)]
        Net.request("POST", path: "api/student/attempts/\(exam.attemptId)/app-beat", token: exam.token,
                    appSession: exam.session, body: body) { [weak self] status, json in
            guard status == 200, let st = json?["status"] as? String else { return }
            self?.onStatus?(st)
        }
    }

    // MARK: - Offline exit key

    /// POST .../offline-key, signed like a beat. The server answers once per session (a
    /// second ask is refused and flagged), so only network failures are retried.
    private func fetchOfflineKey() {
        guard let exam, offlineKey == nil else { return }
        offlineKeyTries += 1
        var ts = Int64(Date().timeIntervalSince1970 * 1000)
        if ts <= lastTs { ts = lastTs + 1 }
        let msg = "offkey|\(exam.attemptId)|\(exam.session)|\(deviceId)|\(ts)"
        let body: [String: Any] = ["session": exam.session, "device_id": deviceId, "ts": NSNumber(value: ts),
                                   "signature": AppSession.sign(msg)]
        Net.request("POST", path: "api/student/attempts/\(exam.attemptId)/offline-key", token: exam.token,
                    appSession: exam.session, body: body) { [weak self] status, json in
            guard let self, let now = self.exam,
                  now.attemptId == exam.attemptId, now.session == exam.session else { return }
            if status == 200, let hex = json?["key"] as? String, let key = AppSession.data(hex: hex), key.count == 32 {
                self.offlineKey = key
                DebugLog.log("exam", "offline exit key received")
            } else if status == nil && self.offlineKeyTries < 30 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 10) { [weak self] in self?.fetchOfflineKey() }
            } else {
                DebugLog.log("exam", "offline exit key NOT available (HTTP \(status.map(String.init) ?? "none"))")
            }
        }
    }

    static func data(hex: String) -> Data? {
        let chars = Array(hex.utf8)
        guard chars.count % 2 == 0 else { return nil }
        var out = Data(capacity: chars.count / 2)
        var i = 0
        while i < chars.count {
            guard let b = UInt8(String(decoding: chars[i..<i + 2], as: UTF8.self), radix: 16) else { return nil }
            out.append(b)
            i += 2
        }
        return out
    }

    // MARK: - Server checks for unlocking

    enum HandIn { case confirmed, notYet, unreachable }

    /// May the lock end? Audit M-04: only on a SIGNED answer from the server
    /// (GET .../handin?nonce=, HMAC with the app key over handin|aid|session|status|nonce),
    /// so a man in the middle without the key can't fake "submitted". Yes for submitted,
    /// gone (no such attempt) or moved (teacher moved it to another computer).
    /// Audit M-06: locked with no exam session -> not yet (was: yes).
    func confirmHandIn(_ done: @escaping (HandIn) -> Void) {
        guard let exam else { done(.notYet); return }
        let nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        // device: on this same Mac a session the page made up is "in_progress", never
        // "moved" (audit 2026-10-13).
        let path = "api/student/attempts/\(exam.attemptId)/handin?nonce=\(nonce)&device=\(deviceId)"
        Net.request("GET", path: path, token: exam.token, appSession: exam.session, body: nil) { status, json in
            guard status == 200, let st = json?["status"] as? String, let proofs = json?["proofs"] as? [String] else {
                DebugLog.log("exam", "hand-in check: HTTP \(status.map(String.init) ?? "none")")
                done(status == nil ? .unreachable : .notYet)
                return
            }
            let expected = AppSession.sign("handin|\(exam.attemptId)|\(exam.session)|\(st)|\(nonce)")
            let genuine = proofs.contains { AppSession.constantTimeEqual($0, expected) }
            DebugLog.log("exam", "hand-in check: \(st), signature \(genuine ? "ok" : "WRONG")")
            guard genuine else {
                EventLog.shared.record(.pinMismatch("hand-in answer not signed by the server"))
                done(.notYet)
                return
            }
            done(["submitted", "gone", "moved"].contains(st) ? .confirmed : .notYet)
        }
    }

    static func exitReplyIsGenuine(proofs: [String], attemptId: String, session: String, nonce: String) -> Bool {
        let expected = AppSession.sign("exit|\(attemptId)|\(session)|ok|\(nonce)")
        return proofs.contains { AppSession.constantTimeEqual($0, expected) }
    }

    static func constantTimeEqual(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        guard x.count == y.count else { return false }
        var diff: UInt8 = 0
        for i in 0..<x.count { diff |= x[i] ^ y[i] }
        return diff == 0
    }

    /// `serverError`: the server answered but not with yes/no (5xx, session moved, ...):
    /// the app then offers the offline code instead of leaving the Mac stuck.
    enum ExitResult: Equatable { case ok, wrong, tooMany, unreachable, noSession, serverError(Int) }

    /// Invigilator exit code, checked (and rate-limited) by the server. Audit 2026-10-13:
    /// "ok" counts only with the server's signature over exit|aid|session|ok|nonce, like
    /// the hand-in status, so a man in the middle can't answer {"ok": true}.
    func checkExitCode(_ code: String, _ done: @escaping (ExitResult) -> Void) {
        guard let exam else { done(.noSession); return }
        let nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        Net.request("POST", path: "api/student/attempts/\(exam.attemptId)/exit-code", token: exam.token,
                    appSession: exam.session,
                    body: ["code": String(code.prefix(16)), "nonce": nonce]) { status, json in
            switch status {
            case 200?:
                guard (json?["ok"] as? Bool) == true else { done(.wrong); return }
                let proofs = (json?["proofs"] as? [String]) ?? []
                guard AppSession.exitReplyIsGenuine(proofs: proofs, attemptId: exam.attemptId,
                                                    session: exam.session, nonce: nonce) else {
                    EventLog.shared.record(.pinMismatch("exit-code answer not signed by the server"))
                    done(.serverError(200))
                    return
                }
                done(.ok)
            case 403?: done(.wrong)
            case 429?: done(.tooMany)
            case nil: done(.unreachable)
            case let other?: done(.serverError(other))
            }
        }
    }
}
