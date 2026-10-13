using FairPlay.Core;

// Minimal dependency-free test runner. Each Check prints PASS/FAIL; the process exits
// non-zero if anything fails. On CI this is replaced/augmented by xUnit.
// Reference vectors are generated from the real server code (server/app/app_client.py).

int failed = 0;
void Check(string name, bool ok)
{
    Console.WriteLine($"[{(ok ? "PASS" : "FAIL")}] {name}");
    if (!ok) failed++;
}

const string K = "fairplay-dev-app-key-not-secret";

// ---- Signing: byte-for-byte vs Python hmac.hexdigest() ----
Check("sign start",
    Signing.Sign(K, Signing.StartMessage("abc12345", "dev-device-01"))
    == "2bc5022f3b5508331cb77ab76ee63ea56d3609e758e25f0fa554248cb1485916");

Check("sign beat",
    Signing.Sign(K, Signing.BeatMessage("a1", "sess12345", "dev-device-01", 1700000000000, true, "vm=0,disp=1"))
    == "513583f7356f7958e901070516942935c8f5f6d5f2d33793740f031b6a4a73dd");

Check("sign offkey",
    Signing.Sign(K, Signing.OfflineKeyMessage("a1", "sess12345", "dev-device-01", 1700000000000))
    == "5298c407a1db0b18fb95f68fc08e4da866d86233272595d4f28a372ce14c43de");

Check("sign handin",
    Signing.Sign(K, Signing.HandInMessage("a1", "sess12345", "submitted", "nonce0001"))
    == "1fd0854dcc5cda10c44223c6a7b19ffd6961b6d5103a0f23819642409b246ca2");

// ---- Offline exit code ----
byte[] key32 = Convert.FromHexString("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f");
Check("offline code (32-byte key)", Signing.OfflineExitCode(key32, "K7QM3XPA") == "06710479");
Check("offline code (dev key)",
    Signing.OfflineExitCode(System.Text.Encoding.UTF8.GetBytes(K), "K7QM3XPA") == "37496243");

// ---- Safe token ----
Check("token ok", Signing.IsSafeToken("abc_123-XYZ", 3, 32));
Check("token rejects pipe", !Signing.IsSafeToken("a|b", 1, 32));
Check("token rejects short", !Signing.IsSafeToken("ab", 3, 32));
Check("token rejects unicode", !Signing.IsSafeToken("abcé", 1, 32));

// ---- FromHex ----
Check("fromhex ok", Signing.FromHex("00ff10")!.SequenceEqual(new byte[] { 0, 255, 16 }));
Check("fromhex odd length", Signing.FromHex("abc") is null);

// ---- SiteRule grammar (audit #10) ----
var exact = SiteRule.Parse("canvas.dku.edu")!;
Check("exact host matches", exact.Matches("canvas.dku.edu", "/x"));
Check("exact host no subdomain", !exact.Matches("x.canvas.dku.edu", "/"));

var wild = SiteRule.Parse("*.dku.edu")!;
Check("wildcard matches sub", wild.Matches("x.dku.edu", "/"));
// Matches the Mac/server grammar: a "*.host" rule ALSO allows the apex host itself
// (the h==host branch fires regardless of the wildcard).
Check("wildcard matches apex too", wild.Matches("dku.edu", "/"));
Check("wildcard rejects unrelated", !wild.Matches("evil.com", "/"));

var pathRule = SiteRule.Parse("docs.google.com/forms/d/e/1FAIpQL/")!;
Check("path prefix matches", pathRule.Matches("docs.google.com", "/forms/d/e/1FAIpQL/abc"));
Check("path prefix rejects other path", !pathRule.Matches("docs.google.com", "/spreadsheets/"));

Check("parse strips scheme", SiteRule.Parse("https://canvas.dku.edu/foo")!.Host == "canvas.dku.edu");
Check("parse rejects empty", SiteRule.Parse("https://") is null);

var rules = new[] { exact, wild };
Check("Allows https", SiteRule.Allows(rules, new Uri("https://x.dku.edu/q"), allowHttp: false));
Check("Allows rejects http in release", !SiteRule.Allows(rules, new Uri("http://x.dku.edu/q"), allowHttp: false));
Check("Allows http in dev", SiteRule.Allows(rules, new Uri("http://x.dku.edu/q"), allowHttp: true));

// ---- OfflineExit flow ----
var oe = new OfflineExit(keyProvider: () => key32);
string macCode = oe.Challenge;
string good = Signing.OfflineExitCode(key32, macCode);
Check("offline wrong code", oe.Check("00000000") == OfflineExit.Result.Wrong);
Check("offline right code", oe.Check(good) == OfflineExit.Result.Ok);

var oeLock = new OfflineExit(keyProvider: () => key32);
for (int i = 0; i < 4; i++) oeLock.Check("00000000");
Check("offline lockout after 5", oeLock.Check("00000000") == OfflineExit.Result.TooMany);

var oeNoKey = new OfflineExit(keyProvider: () => null);
Check("offline unavailable without key", oeNoKey.Check("12345678") == OfflineExit.Result.Unavailable);
Check("offline available with dev code", new OfflineExit(() => null, devFixedCode: "0000").IsAvailable);
Check("offline dev code accepted", new OfflineExit(() => null, devFixedCode: "0000").Check("0000") == OfflineExit.Result.Ok);

// ---- Bridge parse ----
var m = Bridge.Parse("""{"action":"lock","attempt_id":"a1","token":"t","session":"s1"}""");
Check("bridge parse action", m?.Action == "lock" && m.AttemptId == "a1");
Check("bridge parse offline flag", Bridge.Parse("""{"action":"lock","offline":true}""")?.Offline == true);
var ext = Bridge.Parse("""{"action":"openExternal","url":"https://x","allowed_domains":["a.com","*.b.com"]}""");
Check("bridge parse domains", ext?.AllowedDomains.Count == 2 && ext.AllowedDomains[0] == "a.com");
Check("bridge rejects no action", Bridge.Parse("""{"foo":1}""") is null);
Check("bridge rejects junk", Bridge.Parse("not json") is null);
Check("dispatch script shape",
    Bridge.DispatchScript(new { type = "locked" }).Contains("proctora-native")
    && Bridge.DispatchScript(new { type = "locked" }).Contains("\"locked\""));

// ---- AppConfig ----
var dev = AppConfig.Load(env: _ => null);
Check("dev config defaults", dev.IsDevBuild && dev.AppKey == AppConfig.DevAppKey && dev.ServerIsAcceptable);
var rel = AppConfig.Load(buildJson: """{"ServerUrl":"https://exam.edu","AppKey":"a-long-private-key-0123456789","Pins":["p"]}""");
Check("release config from json", !rel.IsDevBuild && rel.ServerUri.Host == "exam.edu" && rel.SafetyUnlockMinutes == 0);
var relHttp = AppConfig.Load(buildJson: """{"ServerUrl":"http://exam.edu","AppKey":"k"}""");
Check("release rejects http", !relHttp.ServerIsAcceptable);

// ---- KeyPolicy ----
Check("block Alt+Tab", KeyPolicy.ShouldSwallow(KeyPolicy.VK_TAB, KeyPolicy.Mods.Alt, false));
Check("block Win alone", KeyPolicy.ShouldSwallow(KeyPolicy.VK_LWIN, KeyPolicy.Mods.None, false));
Check("block Win+D", KeyPolicy.ShouldSwallow(KeyPolicy.VK_D, KeyPolicy.Mods.Win, false));
Check("block PrintScreen", KeyPolicy.ShouldSwallow(KeyPolicy.VK_SNAPSHOT, KeyPolicy.Mods.None, false));
Check("block Ctrl+Esc", KeyPolicy.ShouldSwallow(KeyPolicy.VK_ESCAPE, KeyPolicy.Mods.Ctrl, false));
Check("block Ctrl+Shift+Esc", KeyPolicy.ShouldSwallow(KeyPolicy.VK_ESCAPE, KeyPolicy.Mods.Ctrl | KeyPolicy.Mods.Shift, false));
Check("block Ctrl+C when paste off", KeyPolicy.ShouldSwallow(KeyPolicy.VK_C, KeyPolicy.Mods.Ctrl, false));
Check("allow Ctrl+C when paste on", !KeyPolicy.ShouldSwallow(KeyPolicy.VK_C, KeyPolicy.Mods.Ctrl, true));
Check("allow plain letter", !KeyPolicy.ShouldSwallow(KeyPolicy.VK_A, KeyPolicy.Mods.None, false));
Check("allow Ctrl+A (not clipboard-copy)", !KeyPolicy.ShouldSwallow(KeyPolicy.VK_A, KeyPolicy.Mods.Ctrl, false));
Check("emergency exit Ctrl+Alt+E", KeyPolicy.IsEmergencyExit(KeyPolicy.VK_E, KeyPolicy.Mods.Ctrl | KeyPolicy.Mods.Alt));
Check("not emergency without alt", !KeyPolicy.IsEmergencyExit(KeyPolicy.VK_E, KeyPolicy.Mods.Ctrl));

// ---- DenyLists ----
Check("deny teamviewer", DenyLists.IsDenied("TeamViewer"));
Check("deny chatgpt", DenyLists.IsDenied("ChatGPT"));
Check("deny doubao (China AI)", DenyLists.IsDenied("Doubao"));
Check("deny wechat", DenyLists.IsDenied("WeChat"));
Check("keep cisco vpn", DenyLists.IsKept("vpnui"));
Check("keep explorer", DenyLists.IsKept("explorer"));
Check("vpn not denied", !DenyLists.IsDenied("vpnui"));
Check("explorer not denied", !DenyLists.IsDenied("explorer"));
Check("random app not denied", !DenyLists.IsDenied("notepad"));

Console.WriteLine();
Console.WriteLine(failed == 0 ? "ALL TESTS PASSED" : $"{failed} TEST(S) FAILED");
return failed == 0 ? 0 : 1;
