namespace FairPlay.Core;

/// <summary>
/// Invigilator exit that works WITHOUT the exam server (crash, Wi-Fi down, server error,
/// exam not yet on the server) — so a stuck lock never needs a reboot. Port of
/// OfflineExit.swift.
///
/// 1. The locked PC shows a random "PC code", e.g. K7QM-3XPA (new for every lock).
/// 2. In the teacher's live room the invigilator picks the student, types the PC code and
///    gets an 8-digit exit code.
/// 3. exit code = <see cref="Signing.OfflineExitCode"/> under this attempt's offline key,
///    which the app fetched from the server right after locking (memory only, never in the
///    binary). No key and release build ⇒ offline exit is unavailable.
///
/// 5 wrong codes ⇒ 5-minute wait. Instances are not thread-safe; call from the UI thread.
/// </summary>
public sealed class OfflineExit
{
    private const string Alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
    public const int MaxWrong = 5;
    public static readonly TimeSpan WaitAfterWrong = TimeSpan.FromMinutes(5);

    private readonly Func<byte[]?> _keyProvider;
    private readonly string? _devFixedCode; // dev builds only; null in release
    private readonly Func<DateTime> _now;

    private string? _current;
    private int _wrong;
    private DateTime _lockedUntil = DateTime.MinValue;

    /// <param name="keyProvider">Returns the attempt's offline key, or null when none is available.</param>
    /// <param name="devFixedCode">A fixed accept-anything code for dev builds (e.g. "0000"); null in release.</param>
    /// <param name="now">Clock, injectable for tests.</param>
    public OfflineExit(Func<byte[]?> keyProvider, string? devFixedCode = null, Func<DateTime>? now = null)
    {
        _keyProvider = keyProvider;
        _devFixedCode = string.IsNullOrEmpty(devFixedCode) ? null : devFixedCode;
        _now = now ?? (() => DateTime.UtcNow);
    }

    /// <summary>"K7QM3XPA" (no dash). Stable until used or the lock ends.</summary>
    public string Challenge
    {
        get
        {
            if (_current is not null) return _current;
            Span<char> c = stackalloc char[8];
            for (int i = 0; i < 8; i++)
                c[i] = Alphabet[System.Security.Cryptography.RandomNumberGenerator.GetInt32(Alphabet.Length)];
            _current = new string(c);
            return _current;
        }
    }

    /// <summary>"K7QM-3XPA" for display.</summary>
    public string ChallengeForDisplay
    {
        get { string c = Challenge; return c[..4] + "-" + c[4..]; }
    }

    public bool IsAvailable => _keyProvider() is not null || _devFixedCode is not null;

    /// <summary>New challenge next time (after a successful exit or a new lock).</summary>
    public void Reset()
    {
        _current = null;
        _wrong = 0;
        _lockedUntil = DateTime.MinValue;
    }

    public enum Result { Ok, Wrong, TooMany, Unavailable }

    public Result Check(string typed)
    {
        if (_now() < _lockedUntil) return Result.TooMany;

        string digits = new(typed.Where(ch => !char.IsWhiteSpace(ch) && ch != '-').ToArray());

        if (_devFixedCode is not null && Signing.ConstantTimeEquals(digits, _devFixedCode))
            return Result.Ok;

        byte[]? key = _keyProvider();
        if (key is null) return Result.Unavailable;

        if (Signing.ConstantTimeEquals(digits, Signing.OfflineExitCode(key, Challenge)))
            return Result.Ok;

        if (++_wrong >= MaxWrong)
        {
            _wrong = 0;
            _lockedUntil = _now().Add(WaitAfterWrong);
            return Result.TooMany;
        }
        return Result.Wrong;
    }
}
