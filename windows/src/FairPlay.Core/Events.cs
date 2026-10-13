using System.Text.Json.Serialization;

namespace FairPlay.Core;

/// <summary>
/// One integrity event, shaped exactly as the exam page expects
/// (web/src/api/types IntegrityEvent, forwarded to POST .../events).
/// </summary>
public sealed record IntegrityEvent(
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("detail")] string Detail,
    [property: JsonPropertyName("client_time")] string ClientTime);

/// <summary>
/// Event-type names. The ones the server already knows (server/app/app_client.py
/// CLIENT_EVENT_TYPES) reach the dashboard unchanged; Windows-only names are stored by the
/// server as "client_&lt;name&gt;" until we add them there, which is harmless.
/// </summary>
public static class EventTypes
{
    // Shared with the Mac app (already in the server's allow-list).
    public const string LockStarted = "lock_started";
    public const string LockEnded = "lock_ended";
    public const string TapUnavailable = "tap_unavailable";
    public const string SafetyUnlock = "safety_unlock";
    public const string OtherAppForeground = "other_app_foreground";
    public const string OtherAppLaunched = "other_app_launched";
    public const string ExtraDisplay = "extra_display";
    public const string MirroredDisplay = "mirrored_display";
    public const string BlockedNavigation = "blocked_navigation";
    public const string ExternalFinished = "external_finished";
    public const string ProcessBlocked = "process_blocked";
    public const string VmDetected = "vm_detected";
    public const string RemoteSession = "remote_session";
    public const string TapLost = "tap_lost";
    public const string InputSourceBlocked = "input_source_blocked";
    public const string SyntheticInput = "synthetic_input";
    public const string TlsPinMismatch = "tls_pin_mismatch";
    public const string PreviousRunKilled = "previous_run_killed";
    public const string OfflineExit = "offline_exit";
    public const string OfflineExitRejected = "offline_exit_rejected";
    public const string WatchdogAppKilled = "watchdog_app_killed";
    public const string BlockedShortcut = "blocked_shortcut";
    public const string SpaceChanged = "space_changed";

    // Windows-specific (server stores as client_* until added there).
    public const string SessionLocked = "win_session_locked";          // Win+L / Ctrl-Alt-Del lock
    public const string SessionUnlocked = "win_session_unlocked";
    public const string OtherUserSession = "win_other_user_session";   // fast user switching
    public const string PolicyRestored = "win_policy_restored";        // watchdog restored HKCU policies
    public const string ClipboardCleared = "win_clipboard_cleared";
    public const string VirtualDesktopSwitch = "win_virtual_desktop_switch";
}
