using System.Text.Json;

namespace FairPlay.Core;

/// <summary>
/// The app's own link to the server for the attempt it is locked for (port of
/// AppSession.swift; CONTRACT.md "App session"). Signs start challenges, sends a signed beat
/// every 5 s carrying the REAL lock state and check summary, fetches the offline-exit key,
/// and checks with the server before unlocking. Platform-neutral: the Shell injects the lock
/// state, the check summary, and where to persist the device id.
/// </summary>
public sealed class AppSession : IDisposable
{
    public sealed record Exam(string AttemptId, string Token, string Session);

    private readonly AppConfig _config;
    private readonly ServerClient _client;
    private readonly object _gate = new();

    private Timer? _beatTimer;
    private long _lastTs;
    private int _offlineKeyTries;

    public string DeviceId { get; }
    public Exam? Current { get; private set; }
    /// <summary>The attempt's offline-exit key (32 bytes), or null until fetched. Memory only.</summary>
    public byte[]? OfflineKey { get; private set; }

    /// <summary>Returns the live lock state for beats. Set by the Shell.</summary>
    public Func<bool> IsLockedProvider { get; set; } = () => false;
    /// <summary>Returns the short check summary, e.g. "vm=0,disp=1,tap=1". Set by the Shell.</summary>
    public Func<string> ChecksProvider { get; set; } = () => "";
    /// <summary>Raised on each beat with the attempt status ("submitted", ...).</summary>
    public event Action<string>? StatusReceived;

    public AppSession(AppConfig config, ServerClient client, string deviceIdPath)
    {
        _config = config;
        _client = client;
        DeviceId = LoadOrCreateDeviceId(deviceIdPath);
    }

    // ---- identity + signing ----

    private static string LoadOrCreateDeviceId(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                string id = File.ReadAllText(path).Trim();
                if (Signing.IsSafeToken(id, 8, 64)) return id;
            }
        }
        catch (IOException) { }
        string fresh = Guid.NewGuid().ToString("N");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, fresh);
        }
        catch (IOException) { }
        return fresh;
    }

    private string Sign(string message) => Signing.Sign(_config.AppKey, message);

    /// <summary>Bridge reply to {"action":"attest","nonce":...}.</summary>
    public object Attestation(string nonce)
    {
        if (!Signing.IsSafeToken(nonce, 8, 128))
            return new { type = Bridge.Types.AttestationFailed, nonce, reason = "bad_nonce" };
        return new
        {
            type = Bridge.Types.Attestation,
            nonce,
            device_id = DeviceId,
            signature = Sign(Signing.StartMessage(nonce, DeviceId)),
            version = AppConfig.Version,
            build = _config.BuildTag,
        };
    }

    // ---- exam session + beats ----

    /// <summary>
    /// Set the attempt the app locks for. Returns false (changing nothing) when malformed
    /// (audit M-06). Starts the beat loop when the session changes.
    /// </summary>
    public bool SetExam(string? attemptId, string? token, string? session)
    {
        if (attemptId is null || token is null || session is null
            || !Signing.IsSafeToken(attemptId, 3, 32)
            || !Signing.IsSafeToken(session, 8, 64)
            || token.Length == 0 || token.Length >= 4000 || token.Contains('\n'))
            return false;

        bool changed;
        lock (_gate)
        {
            changed = Current is null || Current.AttemptId != attemptId
                      || Current.Session != session || Current.Token != token;
            Current = new Exam(attemptId, token, session);
        }
        if (changed)
        {
            OfflineKey = null;
            _offlineKeyTries = 0;
            StartBeats();
            _ = FetchOfflineKeyAsync();
        }
        return true;
    }

    public void ClearExam()
    {
        lock (_gate)
        {
            _beatTimer?.Dispose();
            _beatTimer = null;
            Current = null;
            OfflineKey = null;
        }
    }

    private void StartBeats()
    {
        lock (_gate)
        {
            _beatTimer?.Dispose();
            _beatTimer = new Timer(_ => _ = BeatAsync(), null, TimeSpan.Zero, TimeSpan.FromSeconds(5));
        }
    }

    private long NextTs()
    {
        long ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        if (ts <= _lastTs) ts = _lastTs + 1;
        _lastTs = ts;
        return ts;
    }

    /// <summary>One signed beat. Also call right after lock/unlock so the teacher sees it at once.</summary>
    public async Task BeatAsync()
    {
        Exam? exam = Current;
        if (exam is null) return;
        long ts = NextTs();
        bool locked = IsLockedProvider();
        string checks = Truncate(ChecksProvider(), 200);
        string sig = Sign(Signing.BeatMessage(exam.AttemptId, exam.Session, DeviceId, ts, locked, checks));
        var body = new { session = exam.Session, device_id = DeviceId, ts, locked, checks, signature = sig };
        ServerClient.Response r = await _client.RequestAsync(HttpMethod.Post,
            $"api/student/attempts/{exam.AttemptId}/app-beat", exam.Token, exam.Session, body);
        if (r.Ok && r.Json is { } j && j.TryGetProperty("status", out JsonElement st)
            && st.ValueKind == JsonValueKind.String)
            StatusReceived?.Invoke(st.GetString()!);
    }

    // ---- offline-exit key ----

    private async Task FetchOfflineKeyAsync()
    {
        Exam? exam = Current;
        if (exam is null || OfflineKey is not null) return;
        _offlineKeyTries++;
        long ts = NextTs();
        string sig = Sign(Signing.OfflineKeyMessage(exam.AttemptId, exam.Session, DeviceId, ts));
        var body = new { session = exam.Session, device_id = DeviceId, ts, signature = sig };
        ServerClient.Response r = await _client.RequestAsync(HttpMethod.Post,
            $"api/student/attempts/{exam.AttemptId}/offline-key", exam.Token, exam.Session, body);

        Exam? now = Current;
        if (now is null || now.AttemptId != exam.AttemptId || now.Session != exam.Session) return;

        if (r.Ok && r.Json is { } j && j.TryGetProperty("key", out JsonElement k)
            && k.ValueKind == JsonValueKind.String
            && Signing.FromHex(k.GetString()!) is { Length: 32 } key)
        {
            OfflineKey = key;
        }
        else if (r.Status is null && _offlineKeyTries < 30)
        {
            await Task.Delay(TimeSpan.FromSeconds(10));
            await FetchOfflineKeyAsync();
        }
    }

    // ---- unlock checks ----

    public enum HandIn { Confirmed, NotYet, Unreachable }

    /// <summary>
    /// May the lock end? Only on a SIGNED server answer (GET .../handin?nonce=, HMAC over
    /// handin|aid|session|status|nonce), so a man-in-the-middle without the key can't fake
    /// "submitted" (audit M-04). Confirmed for submitted/gone/moved.
    /// </summary>
    public async Task<HandIn> ConfirmHandInAsync()
    {
        Exam? exam = Current;
        if (exam is null) return HandIn.NotYet;
        string nonce = Guid.NewGuid().ToString("N");
        ServerClient.Response r = await _client.RequestAsync(HttpMethod.Get,
            $"api/student/attempts/{exam.AttemptId}/handin?nonce={nonce}", exam.Token, exam.Session, null);
        if (!r.Ok || r.Json is not { } j
            || !j.TryGetProperty("status", out JsonElement stEl) || stEl.ValueKind != JsonValueKind.String
            || !j.TryGetProperty("proofs", out JsonElement pr) || pr.ValueKind != JsonValueKind.Array)
            return r.Status is null ? HandIn.Unreachable : HandIn.NotYet;

        string status = stEl.GetString()!;
        string expected = Sign(Signing.HandInMessage(exam.AttemptId, exam.Session, status, nonce));
        bool genuine = pr.EnumerateArray().Any(p =>
            p.ValueKind == JsonValueKind.String && Signing.ConstantTimeEquals(p.GetString()!, expected));
        if (!genuine) return HandIn.NotYet;
        return status is "submitted" or "gone" or "moved" ? HandIn.Confirmed : HandIn.NotYet;
    }

    public enum ExitKind { Ok, Wrong, TooMany, Unreachable, NoSession, ServerError }

    /// <summary>Invigilator exit code, checked and rate-limited by the server.</summary>
    public async Task<(ExitKind Kind, int Status)> CheckExitCodeAsync(string code)
    {
        Exam? exam = Current;
        if (exam is null) return (ExitKind.NoSession, 0);
        ServerClient.Response r = await _client.RequestAsync(HttpMethod.Post,
            $"api/student/attempts/{exam.AttemptId}/exit-code", exam.Token, exam.Session,
            new { code = Truncate(code, 16) });
        return r.Status switch
        {
            200 => (r.Json is { } j && j.TryGetProperty("ok", out JsonElement ok)
                    && ok.ValueKind == JsonValueKind.True ? ExitKind.Ok : ExitKind.Wrong, 200),
            403 => (ExitKind.Wrong, 403),
            429 => (ExitKind.TooMany, 429),
            null => (ExitKind.Unreachable, 0),
            _ => (ExitKind.ServerError, r.Status.Value),
        };
    }

    private static string Truncate(string s, int n) => s.Length <= n ? s : s[..n];

    public void Dispose()
    {
        _beatTimer?.Dispose();
    }
}
