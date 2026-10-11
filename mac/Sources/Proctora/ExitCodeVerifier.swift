import CommonCrypto
import Foundation

/// Phase 1a: per-exam invigilator exit code (replaces the fixed dev code 1234).
///
/// The exam page tells us which attempt is being written (bridge action "examSession", and
/// "openExternal" for website exams): attempt id, the student's token, and `exit_check`, an
/// offline verifier PBKDF2-SHA256(normalised code, salt, iterations).
///
/// A typed code is checked by the server first (POST /api/student/attempts/{id}/exit-code,
/// which logs every try). Only when the server cannot be reached (the network is often what
/// failed) is it compared with `exit_check` locally. A dev/admin override can be set with
///   defaults write ai.proctora.student InvigilatorCode <code>
/// and is used only when no exam session is known at all.
final class ExitCodeVerifier {
    static let shared = ExitCodeVerifier()

    struct Check {
        let salt: Data
        let hash: Data
        let iterations: Int
    }

    enum Result {
        case accepted(how: String)   // "server" | "offline" | "override"
        case rejected
        case tooManyTries
        case unavailable             // nothing to check against
    }

    private(set) var attemptId: String?
    private(set) var token: String?
    private(set) var check: Check?

    /// From the bridge body: {"attempt_id", "token", "exit_check": {alg, iterations, salt, hash}}.
    /// Missing fields keep what we already know (an offline attempt learns its id later).
    func update(from body: [String: Any]) {
        if let aid = body["attempt_id"] as? String, !aid.isEmpty, !aid.hasPrefix("offline:") { attemptId = aid }
        if let t = body["token"] as? String, !t.isEmpty { token = t }
        if let c = body["exit_check"] as? [String: Any],
           let saltText = c["salt"] as? String, let salt = Data(base64Encoded: saltText),
           let hashText = c["hash"] as? String, let hash = Data(base64Encoded: hashText) {
            let iterations = (c["iterations"] as? Int) ?? (c["iterations"] as? NSNumber)?.intValue ?? 100_000
            check = Check(salt: salt, hash: hash, iterations: max(1, iterations))
        }
    }

    /// Forget the session once the computer is unlocked.
    func clear() {
        attemptId = nil; token = nil; check = nil
    }

    static func normalize(_ code: String) -> String {
        String(code.uppercased().unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }
            .map(Character.init))
    }

    /// Completion runs on the main thread.
    func verify(_ typed: String, completion: @escaping (Result) -> Void) {
        let code = ExitCodeVerifier.normalize(typed)
        guard !code.isEmpty else { completion(.rejected); return }
        if let aid = attemptId, let token {
            askServer(code: typed, attemptId: aid, token: token) { [weak self] status in
                switch status {
                case 200: completion(.accepted(how: "server"))
                case 403: completion(.rejected)
                case 429: completion(.tooManyTries)
                default:  // no network / timeout / server trouble: fall back to the local check
                    completion(self?.checkOffline(code) ?? .unavailable)
                }
            }
            return
        }
        completion(checkOffline(code))
    }

    private func checkOffline(_ code: String) -> Result {
        if let check {
            guard let derived = ExitCodeVerifier.pbkdf2(code, salt: check.salt, iterations: check.iterations) else {
                return .unavailable
            }
            return ExitCodeVerifier.constantTimeEqual(derived, check.hash) ? .accepted(how: "offline") : .rejected
        }
        if let override = Config.invigilatorCodeOverride, !override.isEmpty {
            return ExitCodeVerifier.normalize(override) == code ? .accepted(how: "override") : .rejected
        }
        return .unavailable
    }

    private func askServer(code: String, attemptId: String, token: String, completion: @escaping (Int?) -> Void) {
        let url = Config.serverURL.appendingPathComponent("api/student/attempts/\(attemptId)/exit-code")
        var req = URLRequest(url: url, timeoutInterval: 6)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["code": code])
        URLSession.shared.dataTask(with: req) { _, response, _ in
            let status = (response as? HTTPURLResponse)?.statusCode
            DispatchQueue.main.async { completion(status) }
        }.resume()
    }

    static func pbkdf2(_ password: String, salt: Data, iterations: Int, length: Int = 32) -> Data? {
        let pw = Array(password.utf8).map { CChar(bitPattern: $0) }
        var out = [UInt8](repeating: 0, count: length)
        let saltBytes = [UInt8](salt)
        let status = CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2), pw, pw.count,
                                          saltBytes, saltBytes.count,
                                          CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA256), UInt32(iterations),
                                          &out, length)
        return status == Int32(kCCSuccess) ? Data(out) : nil
    }

    static func constantTimeEqual(_ a: Data, _ b: Data) -> Bool {
        guard a.count == b.count else { return false }
        var diff: UInt8 = 0
        for (x, y) in zip(a, b) { diff |= x ^ y }
        return diff == 0
    }
}
