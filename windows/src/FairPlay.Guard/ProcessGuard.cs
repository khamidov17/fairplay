using System.Diagnostics;
using FairPlay.Core;

namespace FairPlay.Guard;

/// <summary>
/// Closes apps that must not run during an exam and reports those that remain. Mirrors the
/// quit/deny logic in LockdownController.swift. Windowed apps (with a main window) are asked
/// to close, then killed; denied background apps (remote control, AI) are killed outright.
/// VPN and system processes are always kept.
/// </summary>
public sealed class ProcessGuard
{
    private readonly Action<string, string> _event; // (type, detail)
    private readonly IReadOnlyCollection<string>? _extraKeep;
    private readonly Dictionary<int, DateTime> _asked = new();

    public ProcessGuard(Action<string, string> onEvent, IReadOnlyCollection<string>? extraKeep = null)
    {
        _event = onEvent;
        _extraKeep = extraKeep;
    }

    public sealed record Finding(IReadOnlyList<string> StillRunning);

    /// <summary>
    /// One sweep. Returns the names of apps that still need closing (shown on the blocking
    /// screen). <paramref name="force"/> kills immediately (the "Close these apps now" button).
    /// </summary>
    public Finding Sweep(bool force)
    {
        var still = new List<string>();
        int me = Environment.ProcessId;
        DateTime now = DateTime.UtcNow;

        foreach (Process p in SafeGetProcesses())
        {
            using (p)
            {
                if (p.Id == me) continue;
                string name = SafeName(p);
                if (name.Length == 0) continue;
                if (DenyLists.IsKept(name, _extraKeep)) continue;

                bool denied = DenyLists.IsDenied(name);
                bool hasWindow = HasVisibleWindow(p);

                if (!denied && !hasWindow) continue; // background, not denied → leave it

                if (denied || force)
                {
                    _event(EventTypes.ProcessBlocked, name);
                    if (!TryKill(p)) still.Add(name);
                    continue;
                }

                // Windowed, not denied: ask to close, then kill after a grace period.
                if (_asked.TryGetValue(p.Id, out DateTime askedAt))
                {
                    if ((now - askedAt).TotalSeconds > 2 && TryKill(p)) continue;
                    still.Add(name);
                }
                else
                {
                    _asked[p.Id] = now;
                    _event(EventTypes.ProcessBlocked, $"close at lock: {name}");
                    if (!TryCloseGracefully(p)) { if (!TryKill(p)) still.Add(name); }
                    else still.Add(name);
                }
            }
        }

        return new Finding(still.Distinct().OrderBy(x => x).ToList());
    }

    private static IEnumerable<Process> SafeGetProcesses()
    {
        try { return Process.GetProcesses(); }
        catch { return []; }
    }

    private static string SafeName(Process p)
    {
        try { return p.ProcessName; } catch { return ""; }
    }

    private static bool HasVisibleWindow(Process p)
    {
        try { return p.MainWindowHandle != 0; } catch { return false; }
    }

    private static bool TryCloseGracefully(Process p)
    {
        try { return p.CloseMainWindow(); } catch { return false; }
    }

    private static bool TryKill(Process p)
    {
        try
        {
            if (p.HasExited) return true;
            p.Kill();
            return true;
        }
        catch { return false; }
    }
}
