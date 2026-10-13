using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace FairPlay.Guard;

/// <summary>
/// Environment checks run at lock and every few seconds (mirrors SystemChecks.swift):
/// virtual machine, extra/mirrored displays, remote session, other logged-in users, and the
/// keyboard layout / IME. Each returns facts; <see cref="Lockdown"/> decides what to block.
/// </summary>
public static class SystemChecks
{
    // ---- displays ----

    public readonly record struct Displays(int Count, bool Remote);

    public static Displays GetDisplays()
    {
        int count = Native.GetSystemMetrics(Native.SM_CMONITORS);
        if (count <= 0) count = 1; // metric unavailable: don't false-flag
        bool remote = Native.GetSystemMetrics(Native.SM_REMOTESESSION) != 0;
        return new Displays(count, remote);
    }

    // ---- virtual machine ----

    /// <summary>
    /// Best-effort VM detection. The CPUID hypervisor bit alone is NOT proof on Windows
    /// (Hyper-V / VBS set it on bare metal), so we rely on SMBIOS/registry vendor strings and
    /// known guest-tool services. A dev bypass (env or build flag) is handled by the caller.
    /// </summary>
    public static (bool IsVm, string Detail) DetectVirtualMachine()
    {
        try
        {
            string sys = ReadReg(@"HARDWARE\DESCRIPTION\System\BIOS", "SystemManufacturer")
                         + " " + ReadReg(@"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName")
                         + " " + ReadReg(@"HARDWARE\DESCRIPTION\System", "SystemBiosVersion");
            string low = sys.ToLowerInvariant();
            string[] vendors = ["vmware", "virtualbox", "vbox", "qemu", "kvm", "parallels",
                                "xen", "innotek", "bochs", "hyper-v", "virtual machine"];
            foreach (string v in vendors)
                if (low.Contains(v)) return (true, v);
        }
        catch { /* registry read failed; fall through */ }
        return (false, "");
    }

    private static string ReadReg(string subkey, string name)
    {
        try
        {
            using RegistryKey? k = Registry.LocalMachine.OpenSubKey(subkey);
            object? v = k?.GetValue(name);
            if (v is string s) return s;
            if (v is string[] arr) return string.Join(" ", arr);
            return v?.ToString() ?? "";
        }
        catch { return ""; }
    }

    // ---- sessions ----

    /// <summary>True when this process runs in the physical console session (not RDP / not another user).</summary>
    public static bool IsOnConsoleSession()
    {
        try
        {
            uint console = Native.WTSGetActiveConsoleSessionId();
            if (console == 0xFFFFFFFF) return true; // no session attached; don't false-flag
            Native.ProcessIdToSessionId((uint)Environment.ProcessId, out uint mine);
            return mine == console;
        }
        catch { return true; }
    }

    // ---- input method / keyboard layout ----

    // Language IDs (primary) considered "safe" built-in input. The real guard forces a
    // Microsoft IME; a third-party IME (Sogou, Baidu) shows up with a non-Microsoft layout
    // handle. For v1 we detect foreground-thread layout and flag unknown ones.
    public static (bool Ok, string LayoutId) CheckKeyboardLayout()
    {
        try
        {
            nint fg = Native.GetForegroundWindow();
            uint tid = Native.GetWindowThreadProcessId(fg, out _);
            nint hkl = Native.GetKeyboardLayout(tid);
            // Low word = language id; high word = device/IME handle. A high word that is not a
            // standard language id usually means a third-party IME.
            long v = hkl.ToInt64();
            int langId = (int)(v & 0xFFFF);
            int device = (int)((v >> 16) & 0xFFFF);
            // Heuristic: device handle equal to langId (or 0) is a plain layout; a large
            // non-matching device handle is an IME we treat as "needs review".
            bool plain = device == 0 || device == langId || (device & 0xF000) != 0xE000;
            return (plain, $"0x{v:X8}");
        }
        catch { return (true, ""); }
    }

    /// <summary>Disable the IME for the current UI thread (forces direct keyboard input).</summary>
    public static bool DisableImeForThread()
    {
        try { return Native.ImmDisableIME(0); } catch { return false; }
    }
}
