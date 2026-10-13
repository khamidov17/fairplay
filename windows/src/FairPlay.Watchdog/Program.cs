using System.Diagnostics;
using FairPlay.Core;
using FairPlay.Guard;

// FairPlay.Watchdog.exe --watchdog <parentPid> [--shell <path-to-shell.exe>]
//
// Watches the parent shell while the lock marker exists. If the shell dies with the marker
// still present, it restores the HKCU policy journal, reports the kill to the server, and
// relaunches the shell. Exits cleanly once the marker is gone (normal unlock).

if (args.Length < 2 || args[0] != "--watchdog" || !int.TryParse(args[1], out int parentPid))
    return 2;

string? shellPath = null;
for (int i = 2; i + 1 < args.Length; i++)
    if (args[i] == "--shell") shellPath = args[i + 1];

string folder = LockMarker.DefaultFolder;
string journalPath = Path.Combine(folder, "policy-journal.json");
var policy = new PolicyJournal(journalPath);

while (true)
{
    Thread.Sleep(1000);

    if (!LockMarker.Exists(folder))
        return 0; // lock ended normally; shell restored its own policies

    bool parentAlive;
    try { parentAlive = !Process.GetProcessById(parentPid).HasExited; }
    catch (ArgumentException) { parentAlive = false; } // no such process

    if (parentAlive) continue;

    // Shell died while locked.
    LockMarker? marker = LockMarker.Read(folder);
    policy.Restore();                        // undo Task Manager / Win-key / etc. tweaks
    if (marker is not null)
        await ReportKillAsync(marker);
    LockMarker.Remove(folder);
    Relaunch(shellPath);
    return 0;
}

static async Task ReportKillAsync(LockMarker m)
{
    if (string.IsNullOrEmpty(m.AttemptId) || string.IsNullOrEmpty(m.Token)) return;
    try
    {
        // Minimal config just to reuse ServerClient's request plumbing (no pinning here; this
        // is a best-effort notification, and the server also sees the beat gap regardless).
        var cfg = AppConfig.Load(buildJson:
            $$"""{"ServerUrl":{{System.Text.Json.JsonSerializer.Serialize(m.ServerUrl)}},"AppKey":"x"}""");
        using var client = new ServerClient(cfg);
        var body = new
        {
            events = new[]
            {
                new
                {
                    type = EventTypes.WatchdogAppKilled,
                    detail = "FairPlay was force-quit while locked",
                    client_time = DateTimeOffset.UtcNow.ToString("o"),
                },
            },
        };
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await client.RequestAsync(HttpMethod.Post,
            $"api/student/attempts/{m.AttemptId}/events", m.Token,
            string.IsNullOrEmpty(m.Session) ? null : m.Session, body, cts.Token);
    }
    catch { /* best effort */ }
}

static void Relaunch(string? shellPath)
{
    try
    {
        shellPath ??= Path.Combine(AppContext.BaseDirectory, "FairPlay.Shell.exe");
        if (File.Exists(shellPath))
            Process.Start(new ProcessStartInfo(shellPath) { UseShellExecute = true });
    }
    catch { /* best effort */ }
}
