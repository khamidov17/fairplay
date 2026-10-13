import Foundation

/// Settings (audit 2026-10-12 #4).
///
/// Release builds read NOTHING a student can change: the server, app key and pins are
/// compiled in by build.sh, there is no safety auto-unlock, no window-level switch and
/// no local invigilator code (the server checks it).
///
/// Dev builds (build.sh with FAIRPLAY_BUILD=dev, the default) keep the old overrides:
///   defaults write ai.proctora.student ServerURL http://192.168.1.20:8000
///   defaults write ai.proctora.student SafetyUnlockMinutes -int 0
enum Config {
    static let version = "0.5"

    #if FAIRPLAY_DEV
    static let isDevBuild = true
    #else
    static let isDevBuild = false
    #endif

    /// Audit M-14: what build.sh wrote into the signed bundle (falls back to dev defaults).
    private static let built: [String: Any] = {
        guard let url = Bundle.main.url(forResource: "FairPlayBuild", withExtension: "plist"),
              let data = try? Data(contentsOf: url),
              let dict = (try? PropertyListSerialization.propertyList(from: data, format: nil)) as? [String: Any]
        else { return [:] }
        return dict
    }()

    static var appKey: String { (built["AppKey"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? BuildConfig.appKey }
    static var pins: [String] { (built["Pins"] as? [String]) ?? BuildConfig.pins }
    static var debugLogPath: String { (built["DebugLogPath"] as? String) ?? BuildConfig.debugLogPath }

    /// Read once at launch; never changes while the app runs.
    static let serverURL: URL = {
        var raw = (built["ServerURL"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? BuildConfig.serverURL
        #if FAIRPLAY_DEV
        if let env = ProcessInfo.processInfo.environment["PROCTORA_SERVER"], !env.isEmpty {
            raw = env
        } else if let d = UserDefaults.standard.string(forKey: "ServerURL"), !d.isEmpty {
            raw = d
        }
        #endif
        let trimmed = raw.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        return URL(string: trimmed) ?? URL(string: "https://server-not-configured.invalid")!
    }()

    static var startURL: URL { serverURL.appendingPathComponent("student/login") }

    /// Release builds only talk to an https server (audit #11).
    static var serverIsAcceptable: Bool {
        isDevBuild || serverURL.scheme?.lowercased() == "https"
    }

    /// Dev-build safety net: unlock by itself after this long. Always 0 (never) in release.
    static var safetyUnlockMinutes: Double {
        #if FAIRPLAY_DEV
        let v = UserDefaults.standard.object(forKey: "SafetyUnlockMinutes") as? Double
        return v ?? 180
        #else
        return 0
        #endif
    }

    /// Dev only: keep the locked window at .floating so a debugger stays usable.
    static var floatingLockedWindow: Bool {
        #if FAIRPLAY_DEV
        return UserDefaults.standard.string(forKey: "WindowLevel") == "floating"
        #else
        return false
        #endif
    }
}
