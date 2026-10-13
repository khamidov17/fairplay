namespace FairPlay.Guard;

/// <summary>Clears the clipboard at lock and whenever it changes (polled). Mirrors the Mac pasteboard guard.</summary>
public sealed class ClipboardGuard
{
    public void ClearNow()
    {
        try
        {
            if (Native.OpenClipboard(0))
            {
                Native.EmptyClipboard();
                Native.CloseClipboard();
            }
        }
        catch { /* best effort */ }
    }
}

/// <summary>Excludes the exam window from screen capture (Win10 2004+). Not effective against an HDMI grabber or a phone camera.</summary>
public static class CaptureProtection
{
    public static bool Exclude(nint hWnd)
    {
        try { return Native.SetWindowDisplayAffinity(hWnd, Native.WDA_EXCLUDEFROMCAPTURE); }
        catch { return false; }
    }

    public static bool Release(nint hWnd)
    {
        try { return Native.SetWindowDisplayAffinity(hWnd, Native.WDA_NONE); }
        catch { return false; }
    }
}

/// <summary>
/// Keeps the exam window frontmost and topmost. A timer re-asserts focus if another window
/// comes forward (mirrors the Mac focus guard). Never fights a window in our own process.
/// </summary>
public sealed class ForegroundGuard : IDisposable
{
    private readonly nint _hWnd;
    private readonly Action<string> _onOtherForeground;
    private Timer? _timer;
    private int _myPid = Environment.ProcessId;

    public ForegroundGuard(nint hWnd, Action<string> onOtherForeground)
    {
        _hWnd = hWnd;
        _onOtherForeground = onOtherForeground;
    }

    public void Start()
    {
        _timer?.Dispose();
        _timer = new Timer(_ => Tick(), null, TimeSpan.FromMilliseconds(300), TimeSpan.FromMilliseconds(300));
    }

    private void Tick()
    {
        try
        {
            nint fg = Native.GetForegroundWindow();
            if (fg == _hWnd || fg == 0) return;
            Native.GetWindowThreadProcessId(fg, out uint pid);
            if ((int)pid == _myPid) return;
            try { _onOtherForeground($"pid={pid}"); } catch { }
            Native.SetForegroundWindow(_hWnd);
            Native.SetWindowPos(_hWnd, Native.HWND_TOPMOST, 0, 0, 0, 0,
                Native.SWP_NOMOVE | Native.SWP_NOSIZE | Native.SWP_SHOWWINDOW);
        }
        catch { }
    }

    public void BringToFront()
    {
        try
        {
            Native.ShowWindow(_hWnd, 3 /* SW_MAXIMIZE */);
            Native.SetForegroundWindow(_hWnd);
            Native.SetWindowPos(_hWnd, Native.HWND_TOPMOST, 0, 0, 0, 0,
                Native.SWP_NOMOVE | Native.SWP_NOSIZE | Native.SWP_SHOWWINDOW);
        }
        catch { }
    }

    public void Dispose() { _timer?.Dispose(); _timer = null; }
}
