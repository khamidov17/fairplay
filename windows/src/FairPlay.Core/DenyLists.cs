namespace FairPlay.Core;

/// <summary>
/// Which processes are blocked during an exam and which are always kept. Data + matching
/// logic live here (platform-neutral, unit-tested); the Guard layer feeds it real process
/// names. Mirrors SystemChecks.deniedBundlePrefixes / Allowlist on the Mac, adapted to
/// Windows executable names and tuned for China (local AI apps, IMEs, chat apps).
///
/// Matching is case-insensitive on the executable base name (no ".exe"). Keep lists narrow
/// and update them from the server at exam start (a server-driven list avoids shipping a new
/// build every time a new cheating app appears).
/// </summary>
public static class DenyLists
{
    /// <summary>Remote-control / screen-sharing tools.</summary>
    public static readonly string[] RemoteControl =
    [
        "teamviewer", "anydesk", "rustdesk", "parsec", "todesk", "sunlogin", "sunloginclient",
        "oray", "aweray", "awedesktop", "vnc", "tigervnc", "ultravnc", "realvnc", "vncviewer",
        "mstsc", "splashtop", "nomachine", "chrome_remote_desktop", "remote_assistance",
        "quickassist", "dwservice", "gotomypc", "logmein",
    ];

    /// <summary>Screen recorders / capture.</summary>
    public static readonly string[] ScreenCapture =
    [
        "obs64", "obs", "obs32", "bdcam", "camtasia", "snagit", "sharex", "flashback",
        "action", "fraps", "xsplit", "screenrec", "apowersoft",
    ];

    /// <summary>AI desktop apps and overlays (Western + Chinese).</summary>
    public static readonly string[] AiAssistants =
    [
        "chatgpt", "claude", "copilot", "cluely", "perplexity", "poe", "gemini",
        "doubao", "kimi", "deepseek", "tongyi", "yuanbao", "quark", "wenxiaobai",
        "monica", "sider", "glarity",
    ];

    /// <summary>Launchers / quick-answer tools.</summary>
    public static readonly string[] Launchers =
    [
        "raycast", "alfred", "flow", "flowlauncher", "powertoys", "listary", "wox", "utools",
    ];

    /// <summary>Chat apps whose message previews can leak answers (blocked unless allowed).</summary>
    public static readonly string[] Messaging =
    [
        "wechat", "weixin", "qq", "tim", "dingtalk", "feishu", "lark", "telegram",
        "whatsapp", "slack", "discord", "line", "kakaotalk",
    ];

    /// <summary>Virtual-machine guest tools / emulators (a running VM also fails VmCheck).</summary>
    public static readonly string[] VirtualMachines =
    [
        "vmware", "vmtoolsd", "vmwaretray", "virtualbox", "vboxservice", "vboxtray",
        "prl_cc", "prl_tools", "vmmem", "vmcompute", "bluestacks", "hd-player", "nox", "ldplayer",
    ];

    /// <summary>VPN clients — ALWAYS kept running (campus networks need them to reach the server).</summary>
    public static readonly string[] VpnKeep =
    [
        "vpnui", "vpnagent", "csc_ui", "cisco secure client", "anyconnect",
        "pangps", "pangpa", "globalprotect",
        "openvpn", "openvpnconnect", "wireguard", "wg", "forticlient", "fortitray",
    ];

    /// <summary>Windows system processes that must never be touched.</summary>
    public static readonly string[] SystemKeep =
    [
        "explorer", "dwm", "csrss", "winlogon", "services", "lsass", "svchost", "wininit",
        "fontdrvhost", "sihost", "taskhostw", "ctfmon", "smss", "system", "registry",
        "searchhost", "startmenuexperiencehost", "shellexperiencehost", "textinputhost",
        "runtimebroker", "applicationframehost", "dllhost", "conhost",
        "securityhealthservice", "securityhealthsystray",
    ];

    /// <summary>Does this executable base name match any entry (case-insensitive substring)?</summary>
    public static bool MatchesAny(string exeBaseName, IEnumerable<string> list)
    {
        string n = exeBaseName.ToLowerInvariant();
        foreach (string e in list)
            if (n.Contains(e, StringComparison.Ordinal)) return true;
        return false;
    }

    public static bool IsKept(string exeBaseName, IReadOnlyCollection<string>? extraKeep = null)
    {
        if (MatchesAny(exeBaseName, SystemKeep)) return true;
        if (MatchesAny(exeBaseName, VpnKeep)) return true;
        if (extraKeep is not null && MatchesAny(exeBaseName, extraKeep)) return true;
        return false;
    }

    /// <summary>True for an app that must be force-killed (remote control, AI, recorders, VMs, messaging).</summary>
    public static bool IsDenied(string exeBaseName)
    {
        if (IsKept(exeBaseName)) return false;
        return MatchesAny(exeBaseName, RemoteControl)
            || MatchesAny(exeBaseName, ScreenCapture)
            || MatchesAny(exeBaseName, AiAssistants)
            || MatchesAny(exeBaseName, Launchers)
            || MatchesAny(exeBaseName, Messaging)
            || MatchesAny(exeBaseName, VirtualMachines);
    }
}
