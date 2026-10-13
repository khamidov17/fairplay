using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;

namespace FairPlay.Core;

/// <summary>
/// Every request to the exam server (port of Net.swift). Adds the student bearer token and
/// the X-FairPlay-Session header, and enforces TLS pinning when the build has pins: a pin is
/// the base64 SHA-256 of a certificate (DER) somewhere in the server's chain (audit #11, M-04).
/// Requests to other hosts (website-mode exams) get normal validation only.
/// </summary>
public sealed class ServerClient : IDisposable
{
    private readonly AppConfig _config;
    private readonly HttpClient _http;

    public ServerClient(AppConfig config, HttpMessageHandler? handler = null)
    {
        _config = config;
        _http = new HttpClient(handler ?? MakePinningHandler(config), disposeHandler: handler is null)
        {
            Timeout = TimeSpan.FromSeconds(15),
        };
    }

    private static HttpClientHandler MakePinningHandler(AppConfig config)
    {
        var h = new HttpClientHandler();
        if (config.Pins.Count == 0) return h;

        string serverHost = config.ServerUri.Host.ToLowerInvariant();
        var pins = new HashSet<string>(config.Pins);

        h.ServerCertificateCustomValidationCallback = (request, cert, chain, errors) =>
        {
            // Pin only the exam server; website-mode hosts get default validation.
            string? host = request.RequestUri?.Host.ToLowerInvariant();
            if (host != serverHost)
                return errors == System.Net.Security.SslPolicyErrors.None;

            // Chain must still validate normally, AND one cert must match a pin.
            if (errors != System.Net.Security.SslPolicyErrors.None) return false;
            if (chain is null) return false;
            foreach (X509ChainElement el in chain.ChainElements)
            {
                byte[] der = el.Certificate.RawData;
                string hash = Convert.ToBase64String(SHA256.HashData(der));
                if (pins.Contains(hash)) return true;
            }
            return false; // caller logs tls_pin_mismatch
        };
        return h;
    }

    public sealed record Response(int? Status, JsonElement? Json)
    {
        public bool Ok => Status is >= 200 and < 300;
    }

    /// <summary>
    /// JSON request. <paramref name="path"/> may contain a "?query". Returns (status or null
    /// when offline, parsed JSON object or null). Never throws for network errors.
    /// </summary>
    public async Task<Response> RequestAsync(HttpMethod method, string path, string token,
        string? appSession, object? body, CancellationToken ct = default)
    {
        Uri url = BuildUri(path);
        using var req = new HttpRequestMessage(method, url);
        req.Headers.Accept.ParseAdd("application/json");
        req.Headers.Authorization = new("Bearer", token);
        if (!string.IsNullOrEmpty(appSession))
            req.Headers.Add("X-FairPlay-Session", appSession);
        if (body is not null)
            req.Content = JsonContent.Create(body, options: JsonOpts);

        try
        {
            using HttpResponseMessage res = await _http.SendAsync(req, ct).ConfigureAwait(false);
            int status = (int)res.StatusCode;
            JsonElement? json = null;
            try
            {
                await using Stream s = await res.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
                using JsonDocument doc = await JsonDocument.ParseAsync(s, cancellationToken: ct).ConfigureAwait(false);
                json = doc.RootElement.Clone();
            }
            catch (JsonException) { /* empty/non-JSON body */ }
            return new Response(status, json);
        }
        catch (HttpRequestException) { return new Response(null, null); }
        catch (TaskCanceledException) { return new Response(null, null); }
    }

    private Uri BuildUri(string path)
    {
        int q = path.IndexOf('?');
        string p = q >= 0 ? path[..q] : path;
        string query = q >= 0 ? path[q..] : "";
        var b = new UriBuilder(new Uri(_config.ServerUri, p));
        if (query.Length > 0) b.Query = query.TrimStart('?');
        return b.Uri;
    }

    public static readonly JsonSerializerOptions JsonOpts = new()
    {
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
    };

    public void Dispose() => _http.Dispose();
}
