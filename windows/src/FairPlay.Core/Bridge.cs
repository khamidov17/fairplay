using System.Text.Json;

namespace FairPlay.Core;

/// <summary>
/// The native bridge wire contract (CONTRACT.md "Native bridge"), shared with the Mac app
/// and web/src/lib/bridge.ts. On Windows the page posts JSON through
/// window.chrome.webview.postMessage({action,...}) and receives app messages as a
/// CustomEvent('proctora-native', {detail:{type,...}}). The action/type names and field
/// names are identical to the Mac bridge, so the React app needs only a transport adapter.
/// </summary>
public static class Bridge
{
    /// <summary>Action names the page may send.</summary>
    public static class Actions
    {
        public const string Hello = "hello";
        public const string Attest = "attest";
        public const string Session = "session";
        public const string Lock = "lock";
        public const string Unlock = "unlock";
        public const string OpenExternal = "openExternal";
        public const string Log = "log"; // dev trace only, never drives anything
    }

    /// <summary>Message types the app sends back.</summary>
    public static class Types
    {
        public const string State = "state";
        public const string Locked = "locked";
        public const string LockFailed = "lockFailed";
        public const string Unlocked = "unlocked";
        public const string UnlockPending = "unlockPending";
        public const string Event = "event";
        public const string Attestation = "attestation";
        public const string AttestationFailed = "attestationFailed";
        public const string ExternalFailed = "externalFailed";
    }

    /// <summary>A parsed inbound message from the page. Unknown/extra fields are ignored.</summary>
    public sealed class InMessage
    {
        public required string Action { get; init; }
        public string? AttemptId { get; init; }
        public string? Token { get; init; }
        public string? Session { get; init; }
        public string? Nonce { get; init; }
        public bool Offline { get; init; }
        // openExternal extras
        public string? Url { get; init; }
        public IReadOnlyList<string> AllowedDomains { get; init; } = [];
        public string? FinishUrlContains { get; init; }
        public string? DeadlineAt { get; init; }
        public string? ServerTime { get; init; }
        public string? Title { get; init; }
        // dev log
        public string? Kind { get; init; }
        public string? Msg { get; init; }
    }

    /// <summary>Parse the JSON the page posted. Returns null when it has no "action" string.</summary>
    public static InMessage? Parse(string json)
    {
        JsonElement root;
        try { root = JsonDocument.Parse(json).RootElement; }
        catch (JsonException) { return null; }
        if (root.ValueKind != JsonValueKind.Object) return null;
        if (!root.TryGetProperty("action", out JsonElement a) || a.ValueKind != JsonValueKind.String)
            return null;

        string? Str(string name) =>
            root.TryGetProperty(name, out JsonElement e) && e.ValueKind == JsonValueKind.String
                ? e.GetString() : null;

        bool Flag(string name) =>
            root.TryGetProperty(name, out JsonElement e) && e.ValueKind == JsonValueKind.True;

        List<string> Domains()
        {
            var list = new List<string>();
            if (root.TryGetProperty("allowed_domains", out JsonElement e) && e.ValueKind == JsonValueKind.Array)
                foreach (JsonElement item in e.EnumerateArray())
                    if (item.ValueKind == JsonValueKind.String && item.GetString() is { } s)
                        list.Add(s);
            return list;
        }

        return new InMessage
        {
            Action = a.GetString()!,
            AttemptId = Str("attempt_id"),
            Token = Str("token"),
            Session = Str("session"),
            Nonce = Str("nonce"),
            Offline = Flag("offline"),
            Url = Str("url"),
            AllowedDomains = Domains(),
            FinishUrlContains = Str("finish_url_contains"),
            DeadlineAt = Str("deadline_at"),
            ServerTime = Str("server_time"),
            Title = Str("title"),
            Kind = Str("kind"),
            Msg = Str("msg"),
        };
    }

    /// <summary>
    /// Build the JS that delivers one app→page message, matching the Mac app's
    /// dispatchEvent('proctora-native', {detail}). The payload is JSON-encoded so it is safe
    /// to inline. Execute via CoreWebView2.ExecuteScriptAsync.
    /// </summary>
    public static string DispatchScript(object detail)
    {
        string json = JsonSerializer.Serialize(detail);
        return $"window.dispatchEvent(new CustomEvent('proctora-native', {{ detail: {json} }}));";
    }
}
