namespace FairPlay.Guard;

/// <summary>What the lock needs from the window showing the exam (implemented by the WPF shell).</summary>
public interface ILockSurface
{
    /// <summary>Native HWND of the exam window (for focus guard + capture protection).</summary>
    nint WindowHandle { get; }

    /// <summary>Go full-screen, borderless, topmost, covering every monitor.</summary>
    void EnterLockedMode();

    /// <summary>Return to the normal resizable window.</summary>
    void ExitLockedMode();

    /// <summary>Show the red blocking overlay. <paramref name="canClose"/> shows the "Close these apps now" button.</summary>
    void ShowBlocking(string reason, bool canClose);

    /// <summary>Hide the blocking overlay.</summary>
    void HideBlocking();
}
