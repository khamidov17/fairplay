# FairPlay for Windows

The Windows student app: a thin C# (.NET 10 / WPF) shell that hosts the existing React exam
UI in WebView2 and holds the lockdown. Same server, same protocol, and the same native-bridge
contract as the macOS app (`mac/`), so the web UI is unchanged apart from a transport adapter.

Status (2026-10-13): **early build.** The platform-neutral core is written and fully tested;
the Windows lockdown and shell are written and compile on CI; nothing has run on real hardware
yet. See `claude/windows-architecture-2026-10-12.md` in the project for the full design.

## Layout

```
windows/
  FairPlay.sln
  src/FairPlay.Core      net10.0  — protocol signing, site rules, offline codes, config,
                                    events, bridge wire format, app session, server client.
                                    Platform-neutral, 61 unit tests (run anywhere).
  src/FairPlay.Guard     net10.0-windows — the lock: keyboard hook, foreground guard, process
                                    control, VM/display/session checks, IME + clipboard guards,
                                    capture protection, HKCU policy journal, orchestrator.
  src/FairPlay.Shell     net10.0-windows (WPF) — WebView2 host, native bridge, blocking screen,
                                    invigilator-exit dialog.
  src/FairPlay.Watchdog  net10.0-windows — restores policies + relaunches if the shell is killed.
  tests/FairPlay.Tests   the Core test runner (no NuGet; also wired into CI).
  shared/test-vectors    protocol vectors shared with the server and the Mac app.
  tools/build.ps1        dev / release build (mirrors mac/build.sh).
  tools/pin.ps1          compute TLS pins for a release build.
  installer/             (later) WiX MSI + Managed-mode service.
```

## Build & test

```powershell
# Dev build (local server http://127.0.0.1:8000, public dev key):
pwsh tools/build.ps1

# Release build (embeds server + key + pins, then optionally signs):
pwsh tools/build.ps1 -Release -Server https://exam.example.edu -AppKey <private-key> -Pins <b64,...> -SignThumbprint <sha1>
```

Core tests alone: `dotnet run --project tests/FairPlay.Tests -c Release`.

CI (`.github/workflows/windows.yml`) builds the whole solution and runs the tests on
`windows-latest` — this is the only place `Guard`/`Shell`/`Watchdog` are verified, since they
can't build on Linux.

## Two deployment modes

- **Standard** — the student installs it, no admin rights. Hooks + policy journal + topmost
  window. Good enough for most rooms.
- **Managed** — university IT deploys an MSI with a LocalSystem service (`installer/`, later)
  that restores state even if the user kills the shell and can block denied processes. This is
  the B2B story.

## What is NOT done yet

- No real-hardware run; no installer/signing; no Managed-mode service.
- External-website exam: navigation filtering + beats work; the top strip (countdown + Finish)
  is a follow-up.
- Protocol v2 (per-device TPM keys) — designed, not built; v1 uses the shared app key like Mac.
- Duplicate-display (mirrored) detection via QueryDisplayConfig is a refinement.

## Cheating routes specific to Windows (tracked in the architecture doc)

Can't block Ctrl+Alt+Del / Win+L (logged instead); third-party IMEs (Sogou/Baidu) forced to a
Microsoft IME; AI overlays & remote tools on the deny list; capture excluded from screenshots
(not HDMI grabbers). A student with admin rights can still patch the binary — the server makes
it loud, not impossible; TPM device keys (v2) raise the bar.
