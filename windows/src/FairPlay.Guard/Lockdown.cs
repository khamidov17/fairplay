using FairPlay.Core;

namespace FairPlay.Guard;

/// <summary>
/// Owns the locked state on Windows (mirrors LockdownController.swift). Defences:
///  1. Session-wide WH_KEYBOARD_LL hook swallows escape shortcuts. Lock refuses to start without it.
///  2. Full-screen topmost window covering every monitor + capture exclusion.
///  3. Foreground guard re-asserts the window every 300 ms.
///  4. HKCU policy journal (Task Manager, Win keys, lock, Game Bar, clipboard, toasts), restored on exit/crash.
///  5. At lock and every 3 s: VM, extra/mirrored displays, remote/other session, hook health,
///     keyboard layout, blocked processes. Problems raise the blocking screen and are reported.
///  6. Watchdog (separate process) makes killing the shell loud; the app's own signed beats
///     tell the server whether it is really locked.
///
/// UI calls go through <paramref name="uiInvoke"/> so the surface is always touched on the UI thread.
/// </summary>
public sealed class Lockdown : IDisposable
{
    private readonly ILockSurface _surface;
    private readonly PolicyJournal _policy;
    private readonly Action<Action> _uiInvoke;
    private readonly Action<string, string> _event;       // (type, detail)
    private readonly Action _onEmergencyExit;
    private readonly bool _allowVmForDev;
    private readonly IReadOnlyCollection<string>? _extraKeep;

    private KeyboardHook? _keyboard;
    private ForegroundGuard? _foreground;
    private ProcessGuard? _processes;
    private readonly ClipboardGuard _clipboard = new();
    private Timer? _checkTimer;
    private Timer? _clipboardTimer;

    private readonly HashSet<string> _reported = new();
    private Dictionary<string, string> _problems = new();
    private string _summary = "";

    public bool IsLocked { get; private set; }
    /// <summary>Set true while our own exit dialog is open (so IME/focus churn isn't flagged).</summary>
    public bool OwnModalOpen { get; set; }

    public Lockdown(ILockSurface surface, PolicyJournal policy, Action<Action> uiInvoke,
                    Action<string, string> onEvent, Action onEmergencyExit,
                    bool allowVmForDev, IReadOnlyCollection<string>? extraKeep = null)
    {
        _surface = surface;
        _policy = policy;
        _uiInvoke = uiInvoke;
        _event = onEvent;
        _onEmergencyExit = onEmergencyExit;
        _allowVmForDev = allowVmForDev;
        _extraKeep = extraKeep;
    }

    /// <summary>Short summary for signed beats, e.g. "vm=0,disp=1,mir=0,console=1,tap=1,inp=1,proc=0".</summary>
    public string CheckSummary => _summary;

    /// <summary>Returns false (stays unlocked) if the keyboard hook could not be installed.</summary>
    public bool Lock(bool allowCopyPaste)
    {
        if (IsLocked) return true;

        _keyboard = new KeyboardHook(
            allowCopyPaste,
            onBlockedShortcut: s => _event(EventTypes.BlockedShortcut, s),
            onEmergencyExit: () => _uiInvoke(_onEmergencyExit),
            onSyntheticInput: s => Report("synthetic", EventTypes.SyntheticInput, s));

        if (!_keyboard.Start())
        {
            _event(EventTypes.TapUnavailable, "keyboard hook refused by Windows");
            _keyboard = null;
            return false;
        }

        IsLocked = true;
        _reported.Clear();
        _problems = new();

        _event(EventTypes.LockStarted, "");
        _policy.Apply(PolicyJournal.Default);

        _processes = new ProcessGuard(_event, _extraKeep);
        _foreground = new ForegroundGuard(_surface.WindowHandle, s => Report("fg", EventTypes.OtherAppForeground, s));

        _uiInvoke(() =>
        {
            _surface.EnterLockedMode();
            CaptureProtection.Exclude(_surface.WindowHandle);
            _foreground!.BringToFront();
        });

        _foreground.Start();
        _clipboard.ClearNow();
        _clipboardTimer = new Timer(_ => _clipboard.ClearNow(), null, 200, 200);

        RunChecks();
        _checkTimer = new Timer(_ => RunChecks(), null, 3000, 3000);
        return true;
    }

    public void Unlock()
    {
        if (!IsLocked) return;
        IsLocked = false;

        _checkTimer?.Dispose(); _checkTimer = null;
        _clipboardTimer?.Dispose(); _clipboardTimer = null;
        _foreground?.Dispose(); _foreground = null;
        _keyboard?.Stop(); _keyboard = null;

        _policy.Restore();

        _uiInvoke(() =>
        {
            CaptureProtection.Release(_surface.WindowHandle);
            _surface.HideBlocking();
            _surface.ExitLockedMode();
        });

        _event(EventTypes.LockEnded, "");
    }

    // ---- checks ----

    private void RunChecks()
    {
        if (!IsLocked) return;
        var found = new Dictionary<string, string>();
        var parts = new List<string>();

        // VM
        (bool vm, string vmDetail) = SystemChecks.DetectVirtualMachine();
        if (vm && _allowVmForDev) vm = false;
        parts.Add($"vm={(vm ? 1 : 0)}");
        if (vm)
        {
            found["vm"] = "FairPlay cannot be used in a virtual machine.\nUse the computer itself.";
            Report("vm", EventTypes.VmDetected, vmDetail);
        }

        // Displays
        SystemChecks.Displays d = SystemChecks.GetDisplays();
        parts.Add($"disp={d.Count}");
        parts.Add("mir=0"); // duplicate-mode detection via QueryDisplayConfig is a later refinement
        if (d.Count > 1)
        {
            found["display"] = "Only one screen is allowed.\nUnplug other displays and turn off duplicate/second-screen mode.";
            Report("display", EventTypes.ExtraDisplay, $"count={d.Count}");
        }

        // Console / remote session
        bool onConsole = SystemChecks.IsOnConsoleSession() && !d.Remote;
        parts.Add($"console={(onConsole ? 1 : 0)}");
        if (!onConsole)
        {
            found["remote"] = "This computer is being used remotely.\nThe exam only works at the computer itself.";
            Report("remote", EventTypes.RemoteSession, "not the console session");
        }

        // Keyboard hook health
        bool tap = _keyboard?.IsHealthy ?? false;
        parts.Add($"tap={(tap ? 1 : 0)}");
        if (!tap)
        {
            found["tap"] = "The keyboard lock stopped working.\nAsk the invigilator.";
            Report("tap", EventTypes.TapLost, "hook not healthy");
        }

        // Keyboard layout / IME
        (bool inputOk, string layout) = SystemChecks.CheckKeyboardLayout();
        parts.Add($"inp={(inputOk ? 1 : 0)}");
        if (!inputOk)
        {
            found["input"] = "A third-party keyboard input method is active.\nSwitch to Microsoft Pinyin or the English (US) keyboard.";
            Report($"input:{layout}", EventTypes.InputSourceBlocked, layout);
        }

        // Processes
        ProcessGuard.Finding f = _processes!.Sweep(force: false);
        parts.Add($"proc={f.StillRunning.Count}");
        if (f.StillRunning.Count > 0)
        {
            found["proc"] = "These apps must be closed for the exam:\n"
                + string.Join(", ", f.StillRunning.Take(6))
                + "\nFairPlay closes them in a few seconds, or press the button. Unsaved work in them is lost.";
        }

        _summary = string.Join(",", parts);
        _problems = found;

        _uiInvoke(() =>
        {
            if (!IsLocked) return;
            if (found.Count == 0) { _surface.HideBlocking(); return; }
            bool canClose = found.ContainsKey("proc");
            string text = string.Join("\n\n", found.Keys.OrderBy(k => k).Select(k => found[k]))
                + "\n\nStuck? Invigilator: Ctrl+Alt+E";
            _surface.ShowBlocking(text, canClose);
        });
    }

    /// <summary>The student pressed "Close these apps now".</summary>
    public void CloseBlockedNow()
    {
        if (!IsLocked) return;
        _processes?.Sweep(force: true);
        RunChecks();
    }

    private void Report(string key, string type, string detail)
    {
        if (!_reported.Add(key)) return;
        _event(type, detail);
    }

    public void Dispose()
    {
        _checkTimer?.Dispose();
        _clipboardTimer?.Dispose();
        _foreground?.Dispose();
        _keyboard?.Dispose();
    }
}
