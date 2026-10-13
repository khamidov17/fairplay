using System.Security.Cryptography;
using System.Text;

namespace FairPlay.Core;

/// <summary>
/// Protocol signing, byte-for-byte compatible with the server (server/app/app_client.py)
/// and the Mac app (mac/Sources/Proctora/AppSession.swift). Every message is HMAC-SHA256
/// with the shared app key, rendered as lowercase hex — the same as Python
/// hmac.hexdigest() and Swift CryptoKit HMAC.
///
/// Protocol v1 (HMAC with a build-embedded key) is what ships first so Windows talks to
/// the unchanged server. Protocol v2 (per-device TPM/Secure-Enclave keys) is added later
/// as <see cref="DeviceKey"/> and does not change these message shapes.
/// </summary>
public static class Signing
{
    /// <summary>Lowercase-hex HMAC-SHA256 of <paramref name="message"/> under <paramref name="key"/>.</summary>
    public static string Sign(string key, string message)
    {
        byte[] mac = HMACSHA256.HashData(Encoding.UTF8.GetBytes(key), Encoding.UTF8.GetBytes(message));
        return Convert.ToHexStringLower(mac);
    }

    /// <summary>Constant-time string compare (avoids leaking how much of a code/signature matched).</summary>
    public static bool ConstantTimeEquals(string a, string b)
    {
        byte[] x = Encoding.UTF8.GetBytes(a);
        byte[] y = Encoding.UTF8.GetBytes(b);
        return CryptographicOperations.FixedTimeEquals(x, y) && x.Length == y.Length;
        // FixedTimeEquals already returns false on length mismatch, but the explicit check
        // keeps intent obvious and guards future refactors.
    }

    // ---- message builders (must match app_client.py exactly) ----

    public static string StartMessage(string nonce, string deviceId) =>
        $"start|{nonce}|{deviceId}";

    public static string BeatMessage(string aid, string session, string deviceId, long ts, bool locked, string checks) =>
        $"beat|{aid}|{session}|{deviceId}|{ts}|{(locked ? 1 : 0)}|{checks}";

    public static string OfflineKeyMessage(string aid, string session, string deviceId, long ts) =>
        $"offkey|{aid}|{session}|{deviceId}|{ts}";

    public static string HandInMessage(string aid, string session, string status, string nonce) =>
        $"handin|{aid}|{session}|{status}|{nonce}";

    /// <summary>
    /// Offline invigilator exit code: first 4 bytes of HMAC(key,"offline-exit|&lt;macCode&gt;"),
    /// big-endian, mod 10^8, zero-padded to 8 digits. Same as OfflineExit.swift and
    /// app_client.offline_exit_code.
    /// </summary>
    public static string OfflineExitCode(byte[] key, string macCode)
    {
        byte[] mac = HMACSHA256.HashData(key, Encoding.UTF8.GetBytes($"offline-exit|{macCode}"));
        uint n = ((uint)mac[0] << 24) | ((uint)mac[1] << 16) | ((uint)mac[2] << 8) | mac[3];
        return (n % 100_000_000).ToString("D8");
    }

    // ---- token hygiene (matches AppSafe.token / isSafeToken) ----

    /// <summary>ASCII letters, digits, '-' and '_' only (no '|', so signed messages stay unambiguous).</summary>
    public static bool IsSafeToken(string? s, int min, int max)
    {
        if (s is null || s.Length < min || s.Length > max) return false;
        foreach (char c in s)
        {
            bool ok = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
                      || c == '-' || c == '_';
            if (!ok) return false;
        }
        return true;
    }

    /// <summary>Decode lowercase/uppercase hex to bytes; null if malformed. Used for the offline key.</summary>
    public static byte[]? FromHex(string hex)
    {
        if (string.IsNullOrEmpty(hex) || (hex.Length & 1) != 0) return null;
        try { return Convert.FromHexString(hex); }
        catch (FormatException) { return null; }
    }
}
