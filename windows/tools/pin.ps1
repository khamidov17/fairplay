<#
    Print the base64 SHA-256 pins for a server's certificate chain (feed into build.ps1 -Pins).
    Pin the CA intermediate and root (plus a backup) so routine leaf renewals keep working.

        pwsh tools/pin.ps1 https://exam.example.edu
#>
param([Parameter(Mandatory)][string]$Url)

$ErrorActionPreference = "Stop"
$uri = [Uri]$Url
$client = [System.Net.Http.HttpClientHandler]::new()
$chainCerts = @()
$client.ServerCertificateCustomValidationCallback = {
    param($req, $cert, $chain, $errors)
    foreach ($el in $chain.ChainElements) { $script:chainCerts += $el.Certificate }
    return $true
}
$http = [System.Net.Http.HttpClient]::new($client)
try { $http.GetAsync($uri).Wait() } catch { }

if ($chainCerts.Count -eq 0) { throw "could not read the certificate chain for $Url" }
Write-Host "Pins for $($uri.Host) (newest/leaf first):"
foreach ($c in $chainCerts) {
    $der = $c.GetRawCertData()
    $sha = [System.Security.Cryptography.SHA256]::HashData($der)
    $b64 = [Convert]::ToBase64String($sha)
    Write-Host ("  {0}   {1}" -f $b64, $c.Subject)
}
Write-Host "`nUse the intermediate and root (not the leaf) with build.ps1 -Pins."
