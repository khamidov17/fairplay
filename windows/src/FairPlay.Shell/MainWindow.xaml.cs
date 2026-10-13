using System.IO;
using System.Windows;
using System.Windows.Interop;
using FairPlay.Core;
using FairPlay.Guard;
using Microsoft.Web.WebView2.Core;

namespace FairPlay.Shell;

/// <summary>
/// The one window: the React exam UI in a WebView2, plus the native bridge (CONTRACT.md
/// "Native bridge"). Normal window until the exam asks to lock; then borderless, topmost,
/// covering every screen. Mirrors MainWindowController.swift.
/// </summary>
public partial class MainWindow : Window, ILockSurface
{
    private readonly AppConfig _config;
    private readonly ServerClient _client;
    private readonly AppSession _session;
    private readonly PolicyJournal _policy;
    private readonly Lockdown _lockdown;
    private readonly OfflineExit _offlineExit;
    private readonly string _folder;

    private Rect _normalBounds;
    private bool _webReady;
    private System.Threading.Timer? _unlockRetry;

    public MainWindow()
    {
        InitializeComponent();

        _folder = LockMarker.DefaultFolder;
        _config = LoadConfig();
        _client = new ServerClient(_config);
        _session = new AppSession(_config, _client, Path.Combine(_folder, "device-id"))
        {
            IsLockedProvider = () => _lockdown?.IsLocked ?? false,
            ChecksProvider = () => _lockdown?.CheckSummary ?? "",
        };
        _policy = new PolicyJournal(Path.Combine(_folder, "policy-journal.json"));
        _offlineExit = new OfflineExit(
            keyProvider: () => _session.OfflineKey,
            devFixedCode: _config.IsDevBuild ? "0000" : null);

        _lockdown = new Lockdown(
            surface: this,
            policy: _policy,
            uiInvoke: a => Dispatcher.Invoke(a),
            onEvent: OnGuardEvent,
            onEmergencyExit: PromptEmergencyExit,
            allowVmForDev: _config.IsDevBuild);

        _session.StatusReceived += OnServerStatus;

        // A marker from a previous run means we died while locked: restore policies and flag it.
        if (LockMarker.Exists(_folder))
        {
            _policy.Restore();
            LockMarker.Remove(_folder);
            // Reported to the server once the student is back in the exam (page forwards it);
            // the server also sees the beat gap.
        }

        Loaded += async (_, _) => await InitWebAsync();
        Closing += (_, e) => { if (_lockdown.IsLocked) e.Cancel = true; }; // can't quit while locked
    }

    private static AppConfig LoadConfig()
    {
        string path = Path.Combine(AppContext.BaseDirectory, "fairplay-build.json");
        string? json = File.Exists(path) ? File.ReadAllText(path) : null;
        return AppConfig.Load(buildJson: json);
    }

    // ---- WebView2 setup ----

    private async Task InitWebAsync()
    {
        string userData = Path.Combine(Path.GetTempPath(), "FairPlayWeb-" + Guid.NewGuid().ToString("N"));
        var env = await CoreWebView2Environment.CreateAsync(userDataFolder: userData);
        await Web.EnsureCoreWebView2Async(env);

        CoreWebView2Settings s = Web.CoreWebView2.Settings;
        s.AreDevToolsEnabled = false;
        s.AreDefaultContextMenusEnabled = false;
        s.AreBrowserAcceleratorKeysEnabled = false;   // Ctrl+R reload, F12, Ctrl+P, etc.
        s.IsStatusBarEnabled = false;
        s.AreDefaultScriptDialogsEnabled = true;       // keep alert/confirm the page may use
        s.IsPasswordAutosaveEnabled = false;
        s.IsGeneralAutofillEnabled = false;
        s.IsSwipeNavigationEnabled = false;
        s.IsZoomControlEnabled = false;

        // Page guards (same as the Mac's injected script).
        await Web.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
            "(function(){document.addEventListener('contextmenu',e=>e.preventDefault(),true);" +
            "document.addEventListener('dragstart',e=>e.preventDefault(),true);" +
            "window.open=function(){return null;};})();");

        Web.CoreWebView2.WebMessageReceived += OnWebMessage;
        Web.CoreWebView2.NavigationStarting += OnNavigationStarting;
        Web.CoreWebView2.NewWindowRequested += OnNewWindowRequested;
        Web.CoreWebView2.PermissionRequested += OnPermissionRequested;
        Web.CoreWebView2.ProcessFailed += (_, _) => Dispatcher.Invoke(ReloadStart);

        _webReady = true;

        if (!_config.ServerIsAcceptable)
        {
            Web.CoreWebView2.NavigateToString(
                "<!doctype html><meta charset=utf-8><body style='font:16px Segoe UI;padding:40px'>" +
                "<h1>This copy of FairPlay is not set up</h1>" +
                "<p>It has no secure (https) exam server. Ask your school for the correct FairPlay app.</p>");
            return;
        }
        ReloadStart();
    }

    private void ReloadStart() => Web.CoreWebView2?.Navigate(_config.StartUri.ToString());

    private bool IsServerUrl(Uri u) =>
        string.Equals(u.Host, _config.ServerUri.Host, StringComparison.OrdinalIgnoreCase)
        && u.Port == _config.ServerUri.Port
        && string.Equals(u.Scheme, _config.ServerUri.Scheme, StringComparison.OrdinalIgnoreCase);

    private static bool IsTeacherPath(string path)
    {
        string p = path.ToLowerInvariant();
        string[] teacher = ["/teacher", "/docs", "/redoc", "/openapi.json"];
        return teacher.Any(t => p == t || p.StartsWith(t + "/", StringComparison.Ordinal));
    }

    private void OnNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
    {
        if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out Uri? u)) { e.Cancel = true; return; }
        if (u.Scheme is "about" or "data") return;

        // Audit M-15: the student app never opens teacher pages / API docs.
        if (IsServerUrl(u) && IsTeacherPath(u.AbsolutePath))
        {
            OnGuardEvent(EventTypes.BlockedNavigation, "teacher page " + u.AbsolutePath);
            e.Cancel = true;
            Web.CoreWebView2.Navigate(_config.StartUri.ToString());
            return;
        }
        if (IsServerUrl(u)) return;
        if (_externalRules.Count > 0 && SiteRule.Allows(_externalRules, u, _config.IsDevBuild)) return;

        OnGuardEvent(EventTypes.BlockedNavigation, u.Host);
        e.Cancel = true;
    }

    private void OnNewWindowRequested(object? sender, CoreWebView2NewWindowRequestedEventArgs e)
    {
        e.Handled = true; // never open a second window
        if (Uri.TryCreate(e.Uri, UriKind.Absolute, out Uri? u)
            && (IsServerUrl(u) || (_externalRules.Count > 0 && SiteRule.Allows(_externalRules, u, _config.IsDevBuild))))
            Web.CoreWebView2.Navigate(e.Uri); // same-target links open in place
    }

    private void OnPermissionRequested(object? sender, CoreWebView2PermissionRequestedEventArgs e)
    {
        // Deny camera/mic/geolocation by default (speaking-question support comes later).
        e.State = CoreWebView2PermissionState.Deny;
    }

    // ---- bridge ----

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string json;
        try { json = e.WebMessageAsJson; } catch { return; }
        Bridge.InMessage? m = Bridge.Parse(json);
        if (m is null) return;

        // Only the exam server's own page may drive the lock.
        if (!Uri.TryCreate(e.Source, UriKind.Absolute, out Uri? origin) || !IsServerUrl(origin))
        {
            if (m.Action == Bridge.Actions.Log) return; // ignore stray logs from other origins
            return;
        }

        switch (m.Action)
        {
            case Bridge.Actions.Hello:
                SendState();
                break;
            case Bridge.Actions.Attest:
                Send(_session.Attestation(m.Nonce ?? ""));
                break;
            case Bridge.Actions.Session:
                _session.SetExam(m.AttemptId, m.Token, m.Session);
                break;
            case Bridge.Actions.Lock:
                HandleLock(m);
                break;
            case Bridge.Actions.OpenExternal:
                HandleOpenExternal(m);
                break;
            case Bridge.Actions.Unlock:
                _ = RequestUnlockAsync(startRetry: true);
                break;
        }
    }

    private void HandleLock(Bridge.InMessage m)
    {
        bool offlineStart = m.Offline && m.AttemptId is null && m.Session is null;
        if (!offlineStart && !_session.SetExam(m.AttemptId, m.Token, m.Session))
        {
            Send(new { type = Bridge.Types.LockFailed, reason = "bad_session" });
            return;
        }
        if (_lockdown.Lock(allowCopyPaste: false))
        {
            WriteMarker();
            _ = _session.BeatAsync();
            Send(new { type = Bridge.Types.Locked });
        }
        else
        {
            Send(new { type = Bridge.Types.LockFailed, reason = "hook" });
        }
    }

    private List<SiteRule> _externalRules = [];

    private void HandleOpenExternal(Bridge.InMessage m)
    {
        if (!_session.SetExam(m.AttemptId, m.Token, m.Session))
        {
            Send(new { type = Bridge.Types.ExternalFailed, reason = "bad_session" });
            return;
        }
        if (!_lockdown.IsLocked && !_lockdown.Lock(allowCopyPaste: false))
        {
            Send(new { type = Bridge.Types.LockFailed, reason = "hook" });
            return;
        }
        WriteMarker();
        if (m.Url is null || !Uri.TryCreate(m.Url, UriKind.Absolute, out Uri? url))
        {
            Send(new { type = Bridge.Types.ExternalFailed, reason = "bad_request" });
            return;
        }
        _externalRules = (m.AllowedDomains ?? []).Select(SiteRule.Parse).Where(r => r is not null).Cast<SiteRule>().ToList();
        if (!SiteRule.Allows(_externalRules, url, _config.IsDevBuild))
        {
            // Always allow the start page's own host+folder (mirrors the Mac).
            string folder = url.AbsolutePath.Contains('/')
                ? url.AbsolutePath[..(url.AbsolutePath.LastIndexOf('/') + 1)] : "";
            SiteRule? r = SiteRule.Parse(url.Host + folder);
            if (r is not null) _externalRules.Add(r);
        }
        Send(new { type = Bridge.Types.Locked });
        Web.CoreWebView2.Navigate(url.ToString());
        // NOTE: the external-exam top strip (countdown + Finish) is a follow-up; navigation
        // filtering, beats and events already work through the shared session.
    }

    private async Task RequestUnlockAsync(bool startRetry)
    {
        if (!_lockdown.IsLocked) { Send(new { type = Bridge.Types.Unlocked }); return; }
        AppSession.HandIn r = await _session.ConfirmHandInAsync();
        if (!_lockdown.IsLocked) return;
        if (r == AppSession.HandIn.Confirmed)
        {
            StopUnlockRetry();
            DoUnlock();
            return;
        }
        Send(new { type = Bridge.Types.UnlockPending, reason = r == AppSession.HandIn.Unreachable ? "offline" : "not_submitted" });
        if (startRetry && _unlockRetry is null)
            _unlockRetry = new System.Threading.Timer(async _ =>
            {
                if (!_lockdown.IsLocked) { StopUnlockRetry(); return; }
                if (await _session.ConfirmHandInAsync() == AppSession.HandIn.Confirmed)
                    Dispatcher.Invoke(() => { StopUnlockRetry(); DoUnlock(); });
            }, null, 3000, 3000);
    }

    private void StopUnlockRetry() { _unlockRetry?.Dispose(); _unlockRetry = null; }

    private void DoUnlock()
    {
        _session.ClearExam();
        _lockdown.Unlock();
        LockMarker.Remove(_folder);
        _ = _session.BeatAsync();
        _externalRules = [];
        Send(new { type = Bridge.Types.Unlocked });
    }

    private void OnServerStatus(string status)
    {
        if (status == "submitted" && _lockdown.IsLocked)
            Dispatcher.Invoke(() => _ = RequestUnlockAsync(startRetry: false));
    }

    private void OnGuardEvent(string type, string detail)
    {
        string time = DateTimeOffset.UtcNow.ToString("o");
        Dispatcher.Invoke(() => Send(new { type = Bridge.Types.Event, @event = new { type, detail, client_time = time } }));
    }

    private void SendState() => Send(new
    {
        type = Bridge.Types.State,
        locked = _lockdown.IsLocked,
        trusted = true, // Windows needs no accessibility grant for the hook
        version = AppConfig.Version,
    });

    private void Send(object detail)
    {
        if (!_webReady || Web.CoreWebView2 is null) return;
        try { _ = Web.CoreWebView2.ExecuteScriptAsync(Bridge.DispatchScript(detail)); }
        catch { }
    }

    private void WriteMarker()
    {
        AppSession.Exam? ex = _session.Current;
        if (ex is null) return;
        new LockMarker(_config.ServerUrl, ex.AttemptId, ex.Token, ex.Session).Write(_folder);
    }

    // ---- ILockSurface ----

    public nint WindowHandle => new WindowInteropHelper(this).Handle;

    public void EnterLockedMode()
    {
        _normalBounds = new Rect(Left, Top, Width, Height);
        WindowStyle = WindowStyle.None;
        ResizeMode = ResizeMode.NoResize;
        ShowInTaskbar = false;   // keep it out of the taskbar / Alt-Tab list while locked
        Topmost = true;
        // Cover the whole virtual desktop (all monitors).
        Left = SystemParameters.VirtualScreenLeft;
        Top = SystemParameters.VirtualScreenTop;
        Width = SystemParameters.VirtualScreenWidth;
        Height = SystemParameters.VirtualScreenHeight;
        Activate();
    }

    public void ExitLockedMode()
    {
        Topmost = false;
        ShowInTaskbar = true;
        WindowStyle = WindowStyle.SingleBorderWindow;
        ResizeMode = ResizeMode.CanResize;
        Left = _normalBounds.Left; Top = _normalBounds.Top;
        Width = _normalBounds.Width; Height = _normalBounds.Height;
    }

    public void ShowBlocking(string reason, bool canClose)
    {
        BlockingText.Text = reason;
        CloseAppsButton.Visibility = canClose ? Visibility.Visible : Visibility.Collapsed;
        BlockingOverlay.Visibility = Visibility.Visible;
    }

    public void HideBlocking() => BlockingOverlay.Visibility = Visibility.Collapsed;

    private void CloseAppsButton_Click(object sender, RoutedEventArgs e) => _lockdown.CloseBlockedNow();

    // ---- invigilator exit ----

    private void PromptEmergencyExit()
    {
        if (!_lockdown.IsLocked) return;
        _lockdown.OwnModalOpen = true;
        try
        {
            var dlg = new ExitDialog(_config.IsDevBuild) { Owner = this };
            if (dlg.ShowDialog() == true)
                _ = HandleExitCodeAsync(dlg.EnteredCode, dlg.UsedOffline);
        }
        finally { _lockdown.OwnModalOpen = false; }
    }

    private async Task HandleExitCodeAsync(string code, bool offline)
    {
        if (offline)
        {
            OfflineExit.Result r = _offlineExit.Check(code);
            if (r == OfflineExit.Result.Ok)
            {
                OnGuardEvent(EventTypes.OfflineExit, "offline invigilator code accepted");
                DoUnlock();
            }
            else if (r == OfflineExit.Result.Wrong || r == OfflineExit.Result.TooMany)
                OnGuardEvent(EventTypes.OfflineExitRejected, r.ToString());
            return;
        }

        (AppSession.ExitKind kind, _) = await _session.CheckExitCodeAsync(code);
        switch (kind)
        {
            case AppSession.ExitKind.Ok:
                DoUnlock();
                break;
            case AppSession.ExitKind.Unreachable:
            case AppSession.ExitKind.NoSession:
            case AppSession.ExitKind.ServerError:
                // Offer the offline path.
                Dispatcher.Invoke(() =>
                {
                    var dlg = new ExitDialog(_config.IsDevBuild, forceOffline: true,
                        pcCode: _offlineExit.ChallengeForDisplay) { Owner = this };
                    if (dlg.ShowDialog() == true)
                        _ = HandleExitCodeAsync(dlg.EnteredCode, offline: true);
                });
                break;
            // Wrong / TooMany: ExitDialog shows a retry message when reopened by the student.
        }
    }
}
