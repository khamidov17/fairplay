using FairPlay.Core;

namespace FairPlay.Guard;

/// <summary>
/// Session-wide low-level keyboard lock (WH_KEYBOARD_LL). Runs on its own thread with a
/// message loop, because the hook must answer within Windows' timeout or it is silently
/// dropped — no managed work, no allocation, on the hot path. Mirrors EventTap.swift.
///
/// Swallows escape shortcuts (decided by <see cref="KeyPolicy"/>), flags injected keystrokes,
/// and raises the invigilator emergency-exit combo. Cannot block Ctrl+Alt+Del or Win+L.
/// </summary>
public sealed class KeyboardHook : IDisposable
{
    private readonly bool _allowCopyPaste;
    private readonly Action<string> _onBlockedShortcut;
    private readonly Action _onEmergencyExit;
    private readonly Action<string> _onSyntheticInput;

    private Thread? _thread;
    private uint _threadId;
    private nint _hook;
    private Native.HookProc? _proc; // kept alive against GC
    private volatile bool _running;

    // Modifier state, tracked from the hook stream (bit per side folded into KeyPolicy.Mods).
    private bool _ctrl, _alt, _shift, _win;
    private int _syntheticCount;

    public KeyboardHook(bool allowCopyPaste, Action<string> onBlockedShortcut,
                        Action onEmergencyExit, Action<string> onSyntheticInput)
    {
        _allowCopyPaste = allowCopyPaste;
        _onBlockedShortcut = onBlockedShortcut;
        _onEmergencyExit = onEmergencyExit;
        _onSyntheticInput = onSyntheticInput;
    }

    /// <summary>Install the hook. Returns false if Windows refused it.</summary>
    public bool Start()
    {
        if (_running) return true;
        var ready = new ManualResetEventSlim(false);
        bool ok = false;
        _thread = new Thread(() =>
        {
            _threadId = Native.GetCurrentThreadId();
            _proc = HookCallback;
            _hook = Native.SetWindowsHookExW(Native.WH_KEYBOARD_LL, _proc, Native.GetModuleHandleW(0), 0);
            ok = _hook != 0;
            _running = ok;
            ready.Set();
            if (!ok) return;

            // Pump messages so the hook stays alive and responsive.
            while (Native.GetMessageW(out Native.MSG msg, 0, 0, 0))
            {
                if (msg.message == Native.WM_QUIT) break;
            }
        })
        { IsBackground = true, Name = "FairPlay.KeyboardHook", Priority = ThreadPriority.Highest };
        _thread.SetApartmentState(ApartmentState.STA);
        _thread.Start();
        ready.Wait(TimeSpan.FromSeconds(3));
        return ok;
    }

    public bool IsHealthy => _running && _hook != 0;
    public int SyntheticInputCount => _syntheticCount;

    private nint HookCallback(int nCode, nint wParam, nint lParam)
    {
        if (nCode != Native.HC_ACTION) return Native.CallNextHookEx(_hook, nCode, wParam, lParam);

        var data = System.Runtime.InteropServices.Marshal.PtrToStructure<Native.KBDLLHOOKSTRUCT>(lParam);
        int msg = (int)wParam;
        bool down = msg is Native.WM_KEYDOWN or Native.WM_SYSKEYDOWN;
        bool up = msg is Native.WM_KEYUP or Native.WM_SYSKEYUP;
        int vk = (int)data.vkCode;

        // Track modifiers.
        UpdateModifier(vk, down, up);

        if (down)
        {
            bool injected = (data.flags & Native.LLKHF_INJECTED) != 0;
            if (injected)
            {
                _syntheticCount++;
                // Don't block injected keys outright (accessibility tools inject too); flag it.
                try { _onSyntheticInput($"vk={vk}"); } catch { /* never throw on hot path */ }
            }

            KeyPolicy.Mods mods = CurrentMods();

            if (KeyPolicy.IsEmergencyExit(vk, mods))
            {
                try { _onEmergencyExit(); } catch { }
                return 1; // swallow the combo itself
            }

            if (KeyPolicy.ShouldSwallow(vk, mods, _allowCopyPaste))
            {
                try { _onBlockedShortcut(Describe(vk, mods)); } catch { }
                return 1; // block
            }
        }

        return Native.CallNextHookEx(_hook, nCode, wParam, lParam);
    }

    private void UpdateModifier(int vk, bool down, bool up)
    {
        const int VK_CONTROL = 0x11, VK_LCONTROL = 0xA2, VK_RCONTROL = 0xA3;
        const int VK_MENU = 0x12, VK_LMENU = 0xA4, VK_RMENU = 0xA5;
        const int VK_SHIFT = 0x10, VK_LSHIFT = 0xA0, VK_RSHIFT = 0xA1;
        if (vk is VK_CONTROL or VK_LCONTROL or VK_RCONTROL) { if (down) _ctrl = true; else if (up) _ctrl = false; }
        else if (vk is VK_MENU or VK_LMENU or VK_RMENU) { if (down) _alt = true; else if (up) _alt = false; }
        else if (vk is VK_SHIFT or VK_LSHIFT or VK_RSHIFT) { if (down) _shift = true; else if (up) _shift = false; }
        else if (vk is KeyPolicy.VK_LWIN or KeyPolicy.VK_RWIN) { if (down) _win = true; else if (up) _win = false; }
    }

    private KeyPolicy.Mods CurrentMods()
    {
        KeyPolicy.Mods m = KeyPolicy.Mods.None;
        if (_ctrl) m |= KeyPolicy.Mods.Ctrl;
        if (_alt) m |= KeyPolicy.Mods.Alt;
        if (_shift) m |= KeyPolicy.Mods.Shift;
        if (_win) m |= KeyPolicy.Mods.Win;
        return m;
    }

    private static string Describe(int vk, KeyPolicy.Mods m)
    {
        var parts = new List<string>();
        if (m.HasFlag(KeyPolicy.Mods.Ctrl)) parts.Add("Ctrl");
        if (m.HasFlag(KeyPolicy.Mods.Alt)) parts.Add("Alt");
        if (m.HasFlag(KeyPolicy.Mods.Shift)) parts.Add("Shift");
        if (m.HasFlag(KeyPolicy.Mods.Win)) parts.Add("Win");
        parts.Add($"0x{vk:X2}");
        return string.Join("+", parts);
    }

    public void Stop()
    {
        if (!_running) return;
        _running = false;
        if (_hook != 0) { Native.UnhookWindowsHookEx(_hook); _hook = 0; }
        if (_threadId != 0) Native.PostThreadMessageW(_threadId, Native.WM_QUIT, 0, 0);
        _thread?.Join(TimeSpan.FromSeconds(2));
        _thread = null;
        _proc = null;
    }

    public void Dispose() => Stop();
}
