// Audit 2026-10-12 T-07: the Mac app's pure logic, checked against the server's values.
// Uses Swift Testing (ships with the Command Line Tools), so no full Xcode is needed.
import Testing
import Foundation
import CoreGraphics
import Carbon.HIToolbox
@testable import Proctora

@Suite struct SiteRuleTests {
    typealias Rule = ExternalExamController.SiteRule

    @Test func exactHostDoesNotOpenSubdomains() {
        let r = Rule("canvas.dku.edu")!
        #expect(r.matches(host: "canvas.dku.edu", path: "/x"))
        #expect(!r.matches(host: "x.canvas.dku.edu", path: "/"))
        #expect(!r.matches(host: "evilcanvas.dku.edu", path: "/"))
    }

    @Test func wildcardAndPathPrefix() {
        #expect(Rule("*.dku.edu")!.matches(host: "canvas.dku.edu", path: "/"))
        #expect(!Rule("*.dku.edu")!.matches(host: "dku.edu.evil.com", path: "/"))
        let forms = Rule("docs.google.com/forms/d/e/ABC/")!
        #expect(forms.matches(host: "docs.google.com", path: "/forms/d/e/ABC/viewform"))
        #expect(!forms.matches(host: "docs.google.com", path: "/document/d/other"))
    }

    @Test func badEntriesAreRefused() {
        #expect(Rule("") == nil)
        #expect(Rule("*.*.com") == nil)
        #expect(Rule("https://Canvas.DKU.edu:443/")?.host == "canvas.dku.edu")
    }
}

@Suite struct TokenTests {
    @Test func safeTokens() {
        #expect(AppSession.isSafeToken("a_1-B", min: 3, max: 32))
        #expect(!AppSession.isSafeToken("a|b|c", min: 3, max: 32))     // no field injection
        #expect(!AppSession.isSafeToken("ab", min: 3, max: 32))
        #expect(!AppSession.isSafeToken("ä_123", min: 3, max: 32))
    }

    @Test func offlineExitVectorMatchesServer() {
        // server tests/test_app_session.py::test_offline_key_vector
        let key = Data((0..<32).map { UInt8($0) })
        #expect(OfflineExit.expectedCode(for: "K7QM3XPA", key: key) == "06710479")
        // dev-key fallback vector (test_offline_exit_code_matches_known_vector)
        #expect(OfflineExit.expectedCode(for: "K7QM3XPA", key: Data("fairplay-dev-app-key-not-secret".utf8))
                == "37496243")
    }

    // `swift test` builds without FAIRPLAY_DEV, exactly like a release build:
    // the dev code 0000 must never end locked mode there.
    @Test func devCodeNeverWorksInReleaseBuilds() {
        #expect(!Config.isDevBuild)
        #expect(!OfflineExit.devCodeMatches("0000"))
        OfflineExit.reset()
        #expect(OfflineExit.check("0000") != .ok)
        #expect(OfflineExit.check("00-00") != .ok)
        OfflineExit.reset()
    }

    @Test func hexDecoding() {
        #expect(AppSession.data(hex: "00ff10") == Data([0, 255, 16]))
        #expect(AppSession.data(hex: "0g") == nil)
        #expect(AppSession.data(hex: "abc") == nil)
    }

    @Test func constantTimeEqual() {
        #expect(AppSession.constantTimeEqual("abc", "abc"))
        #expect(!AppSession.constantTimeEqual("abc", "abd"))
        #expect(!AppSession.constantTimeEqual("abc", "ab"))
    }
}

// Serialized: these tests change EventTap's shared settings.
@Suite(.serialized) struct KeyFilterTests {
    init() {
        EventTap.userHotkeys = []
        EventTap.voiceOverOn = false
    }

    private func key(_ code: Int, _ flags: CGEventFlags = []) -> CGEvent {
        let e = CGEvent(keyboardEventSource: nil, virtualKey: CGKeyCode(code), keyDown: true)!
        e.flags = flags
        return e
    }

    @Test func typingPasses() {
        #expect(!EventTap.shouldSwallow(key(kVK_ANSI_A), type: .keyDown))
        #expect(!EventTap.shouldSwallow(key(kVK_ANSI_A, .maskShift), type: .keyDown))
        #expect(!EventTap.shouldSwallow(key(kVK_Space, .maskControl), type: .keyDown))   // input source
        #expect(!EventTap.shouldSwallow(key(kVK_ANSI_A, .maskControl), type: .keyDown))  // Ctrl-A: line start
    }

    @Test func escapesAreSwallowed() {
        #expect(EventTap.shouldSwallow(key(kVK_Tab, .maskCommand), type: .keyDown))       // Cmd-Tab
        #expect(EventTap.shouldSwallow(key(kVK_Space, .maskAlternate), type: .keyDown))   // Opt-Space launchers
        #expect(EventTap.shouldSwallow(key(kVK_F9), type: .keyDown))                       // any F-key
        #expect(EventTap.shouldSwallow(key(kVK_F2, .maskControl), type: .keyDown))         // Ctrl-F2 menu bar
        #expect(EventTap.shouldSwallow(key(kVK_UpArrow, .maskControl), type: .keyDown))    // Mission Control
        #expect(EventTap.shouldSwallow(key(kVK_ANSI_E, .maskSecondaryFn), type: .keyDown)) // Fn-E emoji
    }

    @Test func userShortcutsFromSystemSettingsAreSwallowed() {
        let ctrlShift = CGEventFlags.maskControl.rawValue | CGEventFlags.maskShift.rawValue
        EventTap.userHotkeys = [EventTap.Hotkey(keyCode: kVK_ANSI_K, modifiers: ctrlShift)]
        #expect(EventTap.shouldSwallow(key(kVK_ANSI_K, [.maskControl, .maskShift]), type: .keyDown))
        #expect(!EventTap.shouldSwallow(key(kVK_ANSI_K, .maskShift), type: .keyDown))
    }

    @Test func voiceOverChordsPassOnlyWithVoiceOver() {
        let chord = key(kVK_ANSI_A, [.maskControl, .maskAlternate])
        #expect(EventTap.shouldSwallow(chord, type: .keyDown))
        EventTap.voiceOverOn = true
        #expect(!EventTap.shouldSwallow(chord, type: .keyDown))
    }
}

@Suite struct TeacherPathTests {
    @Test func teacherPagesAreRecognised() {
        #expect(MainWindowController.isTeacherPath("/teacher"))
        #expect(MainWindowController.isTeacherPath("/Teacher/exams/1"))
        #expect(MainWindowController.isTeacherPath("/openapi.json"))
        #expect(!MainWindowController.isTeacherPath("/student/teacher-notes"))
        #expect(!MainWindowController.isTeacherPath("/teachers"))
    }
}
