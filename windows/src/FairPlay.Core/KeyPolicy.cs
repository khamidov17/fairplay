namespace FairPlay.Core;

/// <summary>
/// Pure decision for the low-level keyboard hook: given a virtual-key code and the current
/// modifier state, should the key be swallowed (blocked)? Kept platform-neutral so it is
/// unit-tested without Windows. The Guard layer tracks modifier state from the hook stream
/// and calls <see cref="ShouldSwallow"/>.
///
/// Windows reserves Ctrl+Alt+Del and Win+L — those cannot be swallowed by any user-mode
/// hook; the Guard logs them via session notifications instead.
/// </summary>
public static class KeyPolicy
{
    // Virtual-key codes we care about (winuser.h).
    public const int VK_TAB = 0x09;
    public const int VK_ESCAPE = 0x1B;
    public const int VK_SPACE = 0x20;
    public const int VK_LWIN = 0x5B;
    public const int VK_RWIN = 0x5C;
    public const int VK_APPS = 0x5D;       // context-menu key
    public const int VK_SNAPSHOT = 0x2C;   // PrintScreen
    public const int VK_F1 = 0x70;
    public const int VK_F24 = 0x87;
    public const int VK_C = 0x43;
    public const int VK_V = 0x56;
    public const int VK_X = 0x58;
    public const int VK_A = 0x41;
    public const int VK_R = 0x52;
    public const int VK_E = 0x45;
    public const int VK_D = 0x44;
    public const int VK_N = 0x4E;
    public const int VK_W = 0x57;
    public const int VK_T = 0x54;
    public const int VK_F = 0x46;
    public const int VK_G = 0x47;
    public const int VK_S = 0x53;

    [Flags]
    public enum Mods { None = 0, Ctrl = 1, Alt = 2, Shift = 4, Win = 8 }

    /// <summary>
    /// The invigilator emergency-exit combo (Ctrl+Alt+E, to match the Mac's Ctrl-Opt-Cmd-E
    /// idea on a keyboard without a Cmd key). Not swallowed; the Guard raises the exit prompt.
    /// </summary>
    public static bool IsEmergencyExit(int vk, Mods m) =>
        vk == VK_E && m.HasFlag(Mods.Ctrl) && m.HasFlag(Mods.Alt);

    /// <summary>
    /// True when this key (with these modifiers) should be blocked while locked.
    /// <paramref name="allowCopyPaste"/> lets the exam permit in-app Ctrl+C/V/X/A.
    /// </summary>
    public static bool ShouldSwallow(int vk, Mods m, bool allowCopyPaste)
    {
        // Windows key alone or in any combo: Start menu, Win+D/E/R/V/G/Shift+S, lock, search.
        if (vk is VK_LWIN or VK_RWIN) return true;
        if (m.HasFlag(Mods.Win)) return true;

        // Context-menu key.
        if (vk == VK_APPS) return true;

        // PrintScreen (any modifier) — screenshots.
        if (vk == VK_SNAPSHOT) return true;

        // Alt+Tab / Alt+Esc / Alt+F4 / Alt+Space (window switching & system menu).
        if (m.HasFlag(Mods.Alt) && (vk == VK_TAB || vk == VK_ESCAPE || vk == VK_SPACE
                                    || (vk >= VK_F1 && vk <= VK_F24)))
            return true;

        // Ctrl+Esc = Start menu; Ctrl+Shift+Esc = Task Manager.
        if (m.HasFlag(Mods.Ctrl) && vk == VK_ESCAPE) return true;

        // Function keys on their own are allowed (brightness/volume); with Alt handled above.

        // Clipboard: block unless the exam allows in-app copy/paste.
        if (!allowCopyPaste && m.HasFlag(Mods.Ctrl) && !m.HasFlag(Mods.Alt)
            && vk is VK_C or VK_V or VK_X)
            return true;

        return false;
    }
}
