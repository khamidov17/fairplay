import Foundation

/// Settings. Override with e.g.
///   defaults write ai.proctora.student ServerURL http://192.168.1.20:8000
///   defaults write ai.proctora.student SafetyUnlockMinutes -int 0
enum Config {
    static let version = "0.4"

    static var serverURL: URL {
        let env = ProcessInfo.processInfo.environment["PROCTORA_SERVER"]
        let raw = env ?? UserDefaults.standard.string(forKey: "ServerURL") ?? "http://127.0.0.1:8000"
        return URL(string: raw.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))) ?? URL(string: "http://127.0.0.1:8000")!
    }

    static var startURL: URL { serverURL.appendingPathComponent("student/login") }

    /// Dev-build safety net: unlock by itself after this long. 0 = never (production).
    static var safetyUnlockMinutes: Double {
        let v = UserDefaults.standard.object(forKey: "SafetyUnlockMinutes") as? Double
        return v ?? 180
    }

    /// Invigilator emergency exit (Ctrl-Opt-Cmd-E while locked). Real builds get it per exam from the server.
    static var invigilatorCode: String {
        UserDefaults.standard.string(forKey: "InvigilatorCode") ?? "1234"
    }
}
