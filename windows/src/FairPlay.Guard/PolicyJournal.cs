using System.Text.Json;
using Microsoft.Win32;

namespace FairPlay.Guard;

/// <summary>
/// Applies per-user (HKCU) policy tweaks that soften Windows during an exam — Task Manager,
/// Win-key combos, lock/switch-user, Game Bar, clipboard history, toasts — and JOURNALS every
/// change so the exact prior state is restored on exit, on crash, and at next launch.
///
/// No admin rights needed (HKCU only). The journal is a file in %LOCALAPPDATA%\FairPlay so the
/// watchdog can restore it even if the shell was killed. A disabled-Task-Manager left behind
/// after a crash is the classic lockdown-app support nightmare; the journal prevents it.
/// </summary>
public sealed class PolicyJournal
{
    public readonly record struct Policy(string SubKey, string Name, int Value);

    /// <summary>The default exam policy set. Server/profile can override later.</summary>
    public static readonly Policy[] Default =
    [
        new(@"Software\Microsoft\Windows\CurrentVersion\Policies\System", "DisableTaskMgr", 1),
        new(@"Software\Microsoft\Windows\CurrentVersion\Policies\System", "DisableLockWorkstation", 1),
        new(@"Software\Microsoft\Windows\CurrentVersion\Policies\System", "DisableChangePassword", 1),
        new(@"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "NoWinKeys", 1),
        new(@"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer", "NoLogoff", 1),
        // Game Bar / DVR capture.
        new(@"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", 0),
        new(@"System\GameConfigStore", "GameDVR_Enabled", 0),
        // Clipboard history + cloud clipboard.
        new(@"Software\Microsoft\Clipboard", "EnableClipboardHistory", 0),
        new(@"Software\Microsoft\Clipboard", "CloudClipboardAutomaticUpload", 0),
        // Toast notifications.
        new(@"Software\Microsoft\Windows\CurrentVersion\PushNotifications", "ToastEnabled", 0),
    ];

    private readonly string _journalPath;

    public PolicyJournal(string journalPath) => _journalPath = journalPath;

    private sealed class Entry
    {
        public string SubKey { get; set; } = "";
        public string Name { get; set; } = "";
        public bool Existed { get; set; }
        public int PriorValue { get; set; }
    }

    /// <summary>Apply the policies, journaling prior state first. Idempotent-ish: re-applying rewrites the journal.</summary>
    public void Apply(IEnumerable<Policy> policies)
    {
        var journal = new List<Entry>();
        foreach (Policy p in policies)
        {
            try
            {
                using RegistryKey key = Registry.CurrentUser.CreateSubKey(p.SubKey, writable: true);
                object? existing = key.GetValue(p.Name);
                journal.Add(new Entry
                {
                    SubKey = p.SubKey,
                    Name = p.Name,
                    Existed = existing is not null,
                    PriorValue = existing is int i ? i : 0,
                });
                key.SetValue(p.Name, p.Value, RegistryValueKind.DWord);
            }
            catch (Exception) { /* a single policy failing must not abort the lock */ }
        }
        WriteJournal(journal);
    }

    private void WriteJournal(List<Entry> journal)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_journalPath)!);
            File.WriteAllText(_journalPath, JsonSerializer.Serialize(journal));
        }
        catch (IOException) { }
    }

    /// <summary>
    /// Restore every journaled value to its prior state and delete the journal. Safe to call
    /// when there is no journal (returns false). Called by the shell on unlock and by the
    /// watchdog if the shell died.
    /// </summary>
    public bool Restore()
    {
        List<Entry>? journal = ReadJournal();
        if (journal is null) return false;
        foreach (Entry e in journal)
        {
            try
            {
                using RegistryKey? key = Registry.CurrentUser.OpenSubKey(e.SubKey, writable: true);
                if (key is null) continue;
                if (e.Existed) key.SetValue(e.Name, e.PriorValue, RegistryValueKind.DWord);
                else key.DeleteValue(e.Name, throwOnMissingValue: false);
            }
            catch (Exception) { }
        }
        try { File.Delete(_journalPath); } catch (IOException) { }
        return true;
    }

    private List<Entry>? ReadJournal()
    {
        try
        {
            if (!File.Exists(_journalPath)) return null;
            return JsonSerializer.Deserialize<List<Entry>>(File.ReadAllText(_journalPath));
        }
        catch { return null; }
    }

    /// <summary>True if a journal is on disk (i.e. policies may be in the applied state).</summary>
    public bool HasPendingJournal() => File.Exists(_journalPath);
}
