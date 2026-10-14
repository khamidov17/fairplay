// gstack /review of the Mac app, 2026-10-14: regression tests for what it found.
// Vectors were computed by the server code (server/app/app_client.py) with the public dev
// key "fairplay-dev-app-key-not-secret", which test builds use (no FairPlayBuild.plist).
import Testing
import Foundation
import AppKit
@testable import Proctora

@Suite struct SignedMessageTests {
    let aid = "a_1", sess = "sess-12345678", nonce = "n0nce123"
    let dev = "hw-0123456789abcdef0123456789abcdef"

    @Test func messagesMatchTheServer() {
        // python: hmac.new(dev_key, <message>, sha256).hexdigest()
        #expect(AppSession.sign(AppSession.startMessage(nonce: nonce, deviceId: dev))
                == "d5e3e7e7eda4b1fa4e107f431d9d5e8ba489b095b68e098febc46cc611a24f5d")
        #expect(AppSession.sign(AppSession.beatMessage(aid: aid, session: sess, deviceId: dev, ts: 1_700_000_000_000,
                                                       locked: true, checks: "vm=0,disp=1"))
                == "e2fef4043796c9909a42b6443dece3ca721ba6ba0bdf9f9e81c3430c1af95ad9")
        #expect(AppSession.sign(AppSession.offkeyMessage(aid: aid, session: sess, deviceId: dev, ts: 1_700_000_000_000))
                == "2d66abb1baa130b4c2df844162d9345304eb7b809207cef41c6bc01c1b9df5bb")
    }

    @Test func handInReplyNeedsTheServersSignature() {
        let proof = "68c1bb21f7a714817f28975e648063d50a77dd6bc3563fe6442afad16ff769a1"   // server vector
        #expect(AppSession.handInReplyIsGenuine(proofs: [proof], attemptId: aid, session: sess, status: "submitted", nonce: nonce))
        #expect(!AppSession.handInReplyIsGenuine(proofs: [proof], attemptId: aid, session: sess, status: "submitted", nonce: "other123"))
        #expect(!AppSession.handInReplyIsGenuine(proofs: [proof], attemptId: aid, session: sess, status: "gone", nonce: nonce))
        #expect(!AppSession.handInReplyIsGenuine(proofs: [], attemptId: aid, session: sess, status: "submitted", nonce: nonce))
        #expect(AppSession.unlockAllowed(status: "submitted") && AppSession.unlockAllowed(status: "moved"))
        #expect(!AppSession.unlockAllowed(status: "in_progress"))
    }

    @Test func exitReplyMapping() {
        let proof = "db2bcf33a9b349b0056518a0a48411bac429213ac544f93ee8ae81c399f7bfca"   // server vector
        func r(_ s: Int?, _ j: [String: Any]?) -> AppSession.ExitResult {
            AppSession.exitResult(status: s, json: j, attemptId: aid, session: sess, nonce: nonce)
        }
        #expect(r(200, ["ok": true, "proofs": [proof]]) == .ok)
        #expect(r(200, ["ok": true, "proofs": []]) == .serverError(200))       // unsigned "ok": not trusted
        #expect(r(200, ["ok": false]) == .wrong)
        #expect(r(403, nil) == .wrong)
        #expect(r(429, nil) == .tooMany)
        #expect(r(nil, nil) == .unreachable)
        #expect(r(502, nil) == .serverError(502))
    }

    @Test func hexIsStrict() {
        #expect(AppSession.data(hex: "+f") == nil)
        #expect(AppSession.data(hex: "-0") == nil)
        #expect(AppSession.data(hex: "0aFf") == Data([0x0a, 0xff]))
        #expect(AppSession.data(hex: "") == Data())
    }

    @Test func offlineKeyRetryPolicy() {
        #expect(AppSession.offlineKeyRetryable(nil))
        #expect(AppSession.offlineKeyRetryable(503) && AppSession.offlineKeyRetryable(429))
        #expect(!AppSession.offlineKeyRetryable(409) && !AppSession.offlineKeyRetryable(403))
    }
}

@Suite struct LockedSessionTests {
    /// "<uid>:student:<exp>:0:kid" base64url + "." + signature (server security.make_token).
    func token(_ uid: Int) -> String {
        let payload = Data("\(uid):student:9999999999:0:abcd".utf8).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        return payload + ".sig"
    }

    @Test func tokenUserIdIsRead() {
        #expect(AppSession.tokenUserId(token(42)) == "42")
        #expect(AppSession.tokenUserId("garbage") == nil)
    }

    @Test func lockedSessionCantSwitchAttemptOrAccount() {
        let cur = AppSession.Exam(attemptId: "a_1", token: token(7), session: "sess-12345678")
        #expect(AppSession.mayReplace(current: cur, attemptId: "a_1", token: token(7)))        // token refresh
        #expect(!AppSession.mayReplace(current: cur, attemptId: "a_2", token: token(7)))       // other attempt
        #expect(!AppSession.mayReplace(current: cur, attemptId: "a_1", token: token(8)))       // other student
    }

    @Test func malformedExamSessionIsRefused() {
        let s = AppSession.shared
        #expect(!s.setExam(attemptId: "a|b", token: "t", session: "sess-12345678"))
        #expect(!s.setExam(attemptId: "a_1", token: "t\nx", session: "sess-12345678"))
        #expect(!s.setExam(attemptId: "a_1", token: "t", session: "short"))
        #expect(!s.setExam(attemptId: "a_1", token: "", session: "sess-12345678"))
    }

    @Test func offlineCodeLocksOutAfterFiveWrong() {
        OfflineExit.reset(); defer { OfflineExit.reset() }
        let key = Data((0..<32).map { UInt8($0) })
        let right = OfflineExit.expectedCode(for: OfflineExit.challenge, key: key)
        let wrong = right == "11111111" ? "22222222" : "11111111"
        let t0 = Date()
        for _ in 0..<4 { #expect(OfflineExit.check(wrong, key: key, now: t0) == .wrong) }
        #expect(OfflineExit.check(wrong, key: key, now: t0) == .tooMany)
        #expect(OfflineExit.check(right, key: key, now: t0.addingTimeInterval(60)) == .tooMany)
        #expect(OfflineExit.check(right, key: key, now: t0.addingTimeInterval(OfflineExit.waitAfterWrong + 1)) == .ok)
        #expect(OfflineExit.check(right, key: nil, now: t0.addingTimeInterval(OfflineExit.waitAfterWrong + 1)) == .unavailable)
    }
}

@Suite struct AppPolicyTests {
    @Test func vpnNamesNeedADotBoundary() {
        #expect(Allowlist.keepsId("com.cisco.anyconnect.gui"))
        #expect(Allowlist.keepsId("com.fortinet.FortiClient"))
        #expect(!Allowlist.keepsId("com.fortinetnotes"))
        #expect(!Allowlist.keepsId("com.wireguardx.app"))
    }

    @Test func teacherAllowedAppsAreParsedSafely() {
        let got = Allowlist.parseTeacherAllowed(["com.apple.calculator", "excel", "com.microsoft.Excel",
                                                 "com.apple.Terminal", "com.google.Chrome", "bad id", 5])
        #expect(got == ["com.apple.calculator", "com.microsoft.excel"])     // never terminals or browsers
        #expect(Allowlist.parseTeacherAllowed(nil).isEmpty)
    }

    @Test func orphanRules() {
        let me: uid_t = 501, since = Date().addingTimeInterval(-600)
        let recent = Date(), old = Date().addingTimeInterval(-3600)
        #expect(SystemChecks.classify(name: "sleep", uid: me, ppid: 1, started: recent, myUid: me, since: since) == .end)
        #expect(SystemChecks.classify(name: "sleep", uid: me, ppid: 1, started: old, myUid: me, since: since) == .report)
        #expect(SystemChecks.classify(name: "python3", uid: me, ppid: 1, started: recent, myUid: me, since: since) == .report)
        #expect(SystemChecks.classify(name: "tmux", uid: me, ppid: 77, started: old, myUid: me, since: since) == .end)
        #expect(SystemChecks.classify(name: "sleep", uid: 0, ppid: 1, started: recent, myUid: me, since: since) == .none)
        #expect(SystemChecks.classify(name: "zsh", uid: me, ppid: 400, started: recent, myUid: me, since: since) == .none)
    }

    @Test func applesParsecdIsNotDenied() {
        #expect(!SystemChecks.deniedProcessNames.contains { "parsecd".contains($0) })
    }

    @Test func overlayWindowsAtOurLevel() {
        let level = 2_000
        let list: [[String: Any]] = [
            [kCGWindowOwnerPID as String: NSNumber(value: 10), kCGWindowLayer as String: NSNumber(value: level),
             kCGWindowOwnerName as String: "Sneaky", kCGWindowAlpha as String: NSNumber(value: 1.0),
             kCGWindowBounds as String: ["X": 0, "Y": 0, "Width": 300, "Height": 200]],
            [kCGWindowOwnerPID as String: NSNumber(value: 11), kCGWindowLayer as String: NSNumber(value: 25),
             kCGWindowOwnerName as String: "MenuBarThing", kCGWindowBounds as String: ["Width": 30, "Height": 22]],
            [kCGWindowOwnerPID as String: NSNumber(value: 12), kCGWindowLayer as String: NSNumber(value: level),
             kCGWindowOwnerName as String: "Invisible", kCGWindowAlpha as String: NSNumber(value: 0.0),
             kCGWindowBounds as String: ["Width": 300, "Height": 200]],
            [kCGWindowOwnerPID as String: NSNumber(value: 99), kCGWindowLayer as String: NSNumber(value: level),
             kCGWindowOwnerName as String: "FairPlay", kCGWindowBounds as String: ["Width": 1440, "Height": 900]],
        ]
        #expect(SystemChecks.overlays(in: list, myPid: 99, myLevel: level) == [SystemChecks.Overlay(pid: 10, owner: "Sneaky")])
    }

    @Test func repeatedEventsAreThrottled() {
        var last: [String: Date] = [:], per: [String: Int] = [:]
        let t = Date()
        #expect(EventLog.shouldSend(type: "blocked_shortcut", detail: "cmd-tab", at: t, lastSent: &last, perType: &per))
        #expect(!EventLog.shouldSend(type: "blocked_shortcut", detail: "cmd-tab", at: t.addingTimeInterval(2), lastSent: &last, perType: &per))
        #expect(EventLog.shouldSend(type: "blocked_shortcut", detail: "cmd-tab", at: t.addingTimeInterval(11), lastSent: &last, perType: &per))
        #expect(EventLog.shouldSend(type: "lock_started", detail: "", at: t, lastSent: &last, perType: &per))
        #expect(EventLog.shouldSend(type: "lock_started", detail: "", at: t, lastSent: &last, perType: &per))  // never throttled
    }
}

@Suite struct NavigationTests {
    @Test func serverOriginMatching() {
        let srv = URL(string: "https://Exam.DKU.edu")!
        #expect(MainWindowController.isServerURL(URL(string: "https://exam.dku.edu:443/student"), server: srv))
        #expect(!MainWindowController.isServerURL(URL(string: "http://exam.dku.edu/student"), server: srv))
        #expect(!MainWindowController.isServerURL(URL(string: "https://exam.dku.edu:8443/"), server: srv))
        #expect(!MainWindowController.isServerURL(URL(string: "https://exam.dku.edu.evil.com/"), server: srv))
    }

    @Test func lockedToTheAttemptsPages() {
        #expect(MainWindowController.allowedWhileLocked(path: "/student/exam/a_1", attemptId: "a_1"))
        #expect(MainWindowController.allowedWhileLocked(path: "/student/done/a_1", attemptId: "a_1"))
        #expect(MainWindowController.allowedWhileLocked(path: "/student/external/a_1", attemptId: "a_1"))
        #expect(MainWindowController.allowedWhileLocked(path: "/student/exam/offline:e_1", attemptId: "a_1"))
        #expect(!MainWindowController.allowedWhileLocked(path: "/student", attemptId: "a_1"))
        #expect(!MainWindowController.allowedWhileLocked(path: "/student/grades/e_1", attemptId: "a_1"))
        #expect(!MainWindowController.allowedWhileLocked(path: "/student/exam/a_2", attemptId: "a_1"))
        #expect(!MainWindowController.allowedWhileLocked(path: "/student/exam/a_10", attemptId: "a_1"))
        #expect(MainWindowController.allowedWhileLocked(path: "/student/exam/a_9", attemptId: nil))   // offline start
        #expect(!MainWindowController.allowedWhileLocked(path: "/student/grades", attemptId: nil))
    }
}
