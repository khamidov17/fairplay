namespace FairPlay.Core;

/// <summary>
/// One allowed-site entry for website-mode exams: [*.]host[/path-prefix].
/// Direct port of ExternalExamController.SiteRule (mac/Sources/Proctora/ExternalExamController.swift)
/// and the server's allowed_domains grammar. The Mac, Windows and server must agree, so the
/// test vectors in shared/test-vectors/site-rules.json are run against all three.
///
/// Rules (audit 2026-10-12 #10):
///   "canvas.dku.edu"                     exact host only (NOT x.canvas.dku.edu)
///   "*.dku.edu"                          the host and all subdomains
///   "docs.google.com/forms/d/e/1FAIpQL/" host + path prefix
/// </summary>
public sealed class SiteRule : IEquatable<SiteRule>
{
    public bool Wildcard { get; }
    public string Host { get; }
    public string PathPrefix { get; }

    private SiteRule(bool wildcard, string host, string pathPrefix)
    {
        Wildcard = wildcard;
        Host = host;
        PathPrefix = pathPrefix;
    }

    /// <summary>Parse one raw entry. Returns null for an unusable entry (empty host, leftover '*').</summary>
    public static SiteRule? Parse(string raw)
    {
        string s = raw.Trim();
        int scheme = s.IndexOf("://", StringComparison.Ordinal);
        if (scheme >= 0) s = s[(scheme + 3)..];

        int cut = s.IndexOfAny(['?', '#']);
        if (cut >= 0) s = s[..cut];

        string host = s;
        string path = "";
        int slash = s.IndexOf('/');
        if (slash >= 0)
        {
            host = s[..slash];
            path = s[slash..];
        }

        bool wild = false;
        if (host.StartsWith("*.", StringComparison.Ordinal))
        {
            wild = true;
            host = host[2..];
        }

        int colon = host.IndexOf(':');
        if (colon >= 0) host = host[..colon];

        host = host.ToLowerInvariant().Trim('.');
        if (host.Length == 0 || host.Contains('*')) return null;

        return new SiteRule(wild, host, path == "/" ? "" : path);
    }

    public bool Matches(string host, string path)
    {
        string h = host.ToLowerInvariant();
        bool hostOk = h == Host || (Wildcard && h.EndsWith("." + Host, StringComparison.Ordinal));
        if (!hostOk) return false;
        if (PathPrefix.Length == 0) return true;
        string p = string.IsNullOrEmpty(path) ? "/" : path;
        return p.StartsWith(PathPrefix, StringComparison.Ordinal);
    }

    /// <summary>Does any rule in the list allow this absolute URL? https always; http only in dev.</summary>
    public static bool Allows(IReadOnlyList<SiteRule> rules, Uri url, bool allowHttp)
    {
        string scheme = url.Scheme.ToLowerInvariant();
        if (scheme != "https" && !(allowHttp && scheme == "http")) return false;
        foreach (SiteRule r in rules)
            if (r.Matches(url.Host, url.AbsolutePath)) return true;
        return false;
    }

    public bool Equals(SiteRule? other) =>
        other is not null && Wildcard == other.Wildcard && Host == other.Host && PathPrefix == other.PathPrefix;

    public override bool Equals(object? obj) => Equals(obj as SiteRule);
    public override int GetHashCode() => HashCode.Combine(Wildcard, Host, PathPrefix);
    public override string ToString() => (Wildcard ? "*." : "") + Host + PathPrefix;
}
