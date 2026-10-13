<#
    Build FairPlay for Windows.   pwsh tools/build.ps1 [-Release]

    Dev build (default): local server, public dev key, dev overrides (env ServerURL, safety
    unlock), http allowed. Pair with FAIRPLAY_DEV=1 on the server.

    Release build:
      pwsh tools/build.ps1 -Release `
        -Server https://exam.example.edu `
        -AppKey <same value as the server's FAIRPLAY_APP_KEY> `
        -Pins <base64 sha256 of a CA cert, comma-separated; see pin.ps1>

    Release writes src/FairPlay.Shell/fairplay-build.json (server, key, pins), which the shell
    reads first and the code signature seals. Never commit a real key.
#>
[CmdletBinding()]
param(
    [switch]$Release,
    [string]$Server,
    [string]$AppKey,
    [string]$Pins,
    [string]$SignThumbprint   # Authenticode cert for signtool (optional)
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$shell = Join-Path $root "src/FairPlay.Shell"
$buildJson = Join-Path $shell "fairplay-build.json"
$DevKey = "fairplay-dev-app-key-not-secret"

if ($Release) {
    if (-not $Server) { throw "Release needs -Server https://your-exam-server" }
    if ($Server -notlike "https://*") { throw "Release builds need an https:// server" }
    if (-not $AppKey -or $AppKey -eq $DevKey -or $AppKey.Length -lt 32) {
        throw "Release needs -AppKey (private, 32+ chars, same as the server)"
    }
    if (-not $Pins) { throw "Release needs -Pins (run: pwsh tools/pin.ps1 https://your-exam-server)" }

    $pinArray = @($Pins -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $cfg = [ordered]@{ ServerUrl = $Server; AppKey = $AppKey; Pins = $pinArray }
    $cfg | ConvertTo-Json | Set-Content -Path $buildJson -Encoding utf8
    Write-Host "==> Wrote $buildJson (release config)"
    $config = "Release"
} else {
    if (Test-Path $buildJson) { Remove-Item $buildJson }  # dev: no embedded config, env overrides
    $config = "Release"   # always build optimized; "dev" vs "release" is the embedded config, not the .NET config
    Write-Host "==> Dev build (local server, public dev key, env overrides allowed)"
}

Write-Host "==> Restoring + building"
dotnet build (Join-Path $root "FairPlay.sln") -c $config
if ($LASTEXITCODE -ne 0) { throw "build failed" }

Write-Host "==> Running tests"
dotnet run --project (Join-Path $root "tests/FairPlay.Tests") -c $config
if ($LASTEXITCODE -ne 0) { throw "tests failed" }

if ($Release -and $SignThumbprint) {
    Write-Host "==> Signing binaries"
    $out = Join-Path $shell "bin/$config/net10.0-windows"
    foreach ($exe in @("FairPlay.Shell.exe", "FairPlay.Watchdog.exe")) {
        $p = Join-Path $out $exe
        if (Test-Path $p) {
            & signtool sign /sha1 $SignThumbprint /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $p
        }
    }
}

Write-Host "==> Done. Shell: src/FairPlay.Shell/bin/$config/net10.0-windows/FairPlay.Shell.exe"
