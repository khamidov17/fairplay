using System.Text.Json;

namespace FairPlay.Core;

/// <summary>
/// Build + runtime settings, mirroring Config.swift / BuildConfig.swift.
///
/// Release builds read NOTHING a student can change: server, app key and TLS pins are baked
/// into the signed bundle (an embedded fairplay-build.json written by the CI build) and env
/// overrides are ignored. Dev builds accept FAIRPLAY_SERVER / FAIRPLAY_APP_KEY env vars and
/// the public dev key, pair with FAIRPLAY_DEV=1 on the server.
/// </summary>
public sealed class AppConfig
{
    public const string Version = "0.5";

    /// <summary>Public dev key; servers accept it only with FAIRPLAY_DEV=1.</summary>
    public const string DevAppKey = "fairplay-dev-app-key-not-secret";

    public required string ServerUrl { get; init; }
    public required string AppKey { get; init; }
    public required IReadOnlyList<string> Pins { get; init; }
    public required bool IsDevBuild { get; init; }
    /// <summary>Optional dev-build auto-unlock (minutes); always 0 in release.</summary>
    public int SafetyUnlockMinutes { get; init; }

    public Uri ServerUri => new(ServerUrl.TrimEnd('/', ' '));
    public Uri StartUri => new(ServerUri, "student/login");

    /// <summary>Release builds only talk to an https server (audit #11).</summary>
    public bool ServerIsAcceptable =>
        IsDevBuild || ServerUri.Scheme.Equals("https", StringComparison.OrdinalIgnoreCase);

    public string BuildTag => IsDevBuild ? "dev" : "release";

    // ---- loading ----

    /// <summary>
    /// Load from an embedded/side-by-side fairplay-build.json if present (release), else fall
    /// back to dev defaults with env overrides. <paramref name="buildJson"/> lets tests inject.
    /// </summary>
    public static AppConfig Load(string? buildJson = null, bool? devOverride = null,
                                 Func<string, string?>? env = null)
    {
        env ??= Environment.GetEnvironmentVariable;

        BuildFile? built = null;
        if (!string.IsNullOrEmpty(buildJson))
        {
            try { built = JsonSerializer.Deserialize<BuildFile>(buildJson); }
            catch (JsonException) { built = null; }
        }

        bool dev = devOverride ?? built is null;

        string server = Pick(built?.ServerUrl, dev ? env("FAIRPLAY_SERVER") : null, "http://127.0.0.1:8000");
        string key = Pick(built?.AppKey, dev ? env("FAIRPLAY_APP_KEY") : null, DevAppKey);
        IReadOnlyList<string> pins = built?.Pins ?? [];
        int safety = dev ? ParseInt(env("FAIRPLAY_SAFETY_MINUTES"), 180) : 0;

        return new AppConfig
        {
            ServerUrl = server,
            AppKey = key,
            Pins = pins,
            IsDevBuild = dev,
            SafetyUnlockMinutes = safety,
        };
    }

    private static string Pick(string? built, string? envVal, string fallback)
    {
        if (!string.IsNullOrEmpty(built)) return built;
        if (!string.IsNullOrEmpty(envVal)) return envVal;
        return fallback;
    }

    private static int ParseInt(string? s, int fallback) =>
        int.TryParse(s, out int v) ? v : fallback;

    private sealed class BuildFile
    {
        public string? ServerUrl { get; set; }
        public string? AppKey { get; set; }
        public List<string>? Pins { get; set; }
    }
}
