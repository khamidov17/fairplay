using System.Runtime.InteropServices;

namespace FairPlay.Guard;

/// <summary>
/// Hand-written Win32 P/Invoke. Kept in one file so the native surface is easy to audit.
/// Only what the lockdown needs; no third-party interop package.
/// </summary>
internal static partial class Native
{
    // ---- windows / foreground ----
    internal const int GWL_EXSTYLE = -20;
    internal const uint WS_EX_TOOLWINDOW = 0x00000080;

    [LibraryImport("user32.dll", SetLastError = true)]
    internal static partial nint GetForegroundWindow();

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool SetForegroundWindow(nint hWnd);

    [LibraryImport("user32.dll")]
    internal static partial uint GetWindowThreadProcessId(nint hWnd, out uint processId);

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool ShowWindow(nint hWnd, int nCmdShow);

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool SetWindowPos(nint hWnd, nint hWndInsertAfter, int x, int y, int cx, int cy, uint flags);

    internal static readonly nint HWND_TOPMOST = new(-1);
    internal const uint SWP_NOMOVE = 0x0002;
    internal const uint SWP_NOSIZE = 0x0001;
    internal const uint SWP_SHOWWINDOW = 0x0040;

    // ---- capture protection ----
    internal const uint WDA_NONE = 0;
    internal const uint WDA_EXCLUDEFROMCAPTURE = 0x00000011;

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool SetWindowDisplayAffinity(nint hWnd, uint affinity);

    // ---- low-level hooks ----
    internal const int WH_KEYBOARD_LL = 13;
    internal const int WH_MOUSE_LL = 14;
    internal const int HC_ACTION = 0;
    internal const int WM_KEYDOWN = 0x0100;
    internal const int WM_KEYUP = 0x0101;
    internal const int WM_SYSKEYDOWN = 0x0104;
    internal const int WM_SYSKEYUP = 0x0105;
    internal const uint LLKHF_INJECTED = 0x00000010;
    internal const uint LLMHF_INJECTED = 0x00000001;

    internal delegate nint HookProc(int nCode, nint wParam, nint lParam);

    [StructLayout(LayoutKind.Sequential)]
    internal struct KBDLLHOOKSTRUCT
    {
        public uint vkCode;
        public uint scanCode;
        public uint flags;
        public uint time;
        public nint dwExtraInfo;
    }

    [LibraryImport("user32.dll", SetLastError = true)]
    internal static partial nint SetWindowsHookExW(int idHook, HookProc lpfn, nint hMod, uint dwThreadId);

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool UnhookWindowsHookEx(nint hhk);

    [LibraryImport("user32.dll")]
    internal static partial nint CallNextHookEx(nint hhk, int nCode, nint wParam, nint lParam);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    internal static partial nint GetModuleHandleW(nint zero);

    // ---- message loop (for the hook thread) ----
    [StructLayout(LayoutKind.Sequential)]
    internal struct MSG
    {
        public nint hwnd; public uint message; public nint wParam; public nint lParam;
        public uint time; public int ptX; public int ptY;
    }

    [LibraryImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool GetMessageW(out MSG msg, nint hWnd, uint min, uint max);

    [LibraryImport("user32.dll")]
    internal static partial nint PostThreadMessageW(uint threadId, uint msg, nint wParam, nint lParam);

    internal const uint WM_QUIT = 0x0012;

    [LibraryImport("kernel32.dll")]
    internal static partial uint GetCurrentThreadId();

    // ---- displays ----
    [LibraryImport("user32.dll")]
    internal static partial int GetSystemMetrics(int index);
    internal const int SM_CMONITORS = 80;
    internal const int SM_REMOTESESSION = 0x1000;

    // ---- input / IME ----
    [LibraryImport("user32.dll")]
    internal static partial nint GetKeyboardLayout(uint idThread);

    [LibraryImport("imm32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool ImmDisableIME(uint idThread);

    // ---- sessions (WTS) ----
    [LibraryImport("kernel32.dll")]
    internal static partial uint WTSGetActiveConsoleSessionId();

    [LibraryImport("kernel32.dll")]
    internal static partial uint ProcessIdToSessionId(uint processId, out uint sessionId);

    // ---- clipboard ----
    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool OpenClipboard(nint hWndNewOwner);

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool EmptyClipboard();

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool CloseClipboard();

    // ---- CPUID / hypervisor is read via a tiny helper (see VmCheck) ----
}
