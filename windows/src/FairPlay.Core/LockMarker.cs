using System.Text.Json;

namespace FairPlay.Core;

/// <summary>
/// The on-disk marker that exists exactly while the app is locked (mirrors Watchdog.swift's
/// marker). Holds just enough for the watchdog to report a kill and relaunch: server URL and
/// the attempt's id/token/session. Written to %LOCALAPPDATA%\FairPlay. Deleted on clean unlock;
/// a marker found at launch means the last run died while locked.
/// </summary>
public sealed record LockMarker(string ServerUrl, string AttemptId, string Token, string Session)
{
    public static string DefaultFolder =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FairPlay");

    public static string PathFor(string folder) => Path.Combine(folder, "locked.marker");

    public void Write(string folder)
    {
        Directory.CreateDirectory(folder);
        File.WriteAllText(PathFor(folder), JsonSerializer.Serialize(this));
    }

    public static LockMarker? Read(string folder)
    {
        try
        {
            string p = PathFor(folder);
            if (!File.Exists(p)) return null;
            return JsonSerializer.Deserialize<LockMarker>(File.ReadAllText(p));
        }
        catch { return null; }
    }

    public static bool Exists(string folder) => File.Exists(PathFor(folder));

    public static void Remove(string folder)
    {
        try { File.Delete(PathFor(folder)); } catch (IOException) { }
    }
}
