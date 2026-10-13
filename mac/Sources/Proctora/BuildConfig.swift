// Dev defaults only. Audit M-14: build.sh no longer rewrites this file; each build writes
// its server, app key, pins and trace path into Contents/Resources/FairPlayBuild.plist,
// which Config reads first (and the code signature seals). Never put real keys here.
enum BuildConfig {
    /// Exam server. Release builds must use https (build.sh refuses http).
    static let serverURL = "http://127.0.0.1:8000"
    /// HMAC key shared with the server (FAIRPLAY_APP_KEY). This value is the public dev
    /// key, which servers accept only with FAIRPLAY_DEV=1.
    static let appKey = "fairplay-dev-app-key-not-secret"
    /// Base64 SHA-256 of certificates (DER) in the server's chain; empty = normal TLS checks only.
    static let pins: [String] = []
    /// Dev builds: readable trace file (DebugLog.swift). Empty = off (always in release).
    static let debugLogPath = ""
}
