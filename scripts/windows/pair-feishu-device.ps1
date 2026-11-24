param([string]$DataDir = (Join-Path $env:LOCALAPPDATA "SuparagentSteward"), [switch]$RequireManage)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "steward-environment.ps1")
$Host.UI.RawUI.WindowTitle = "Dudu - Device Pairing"
$path = Join-Path $DataDir "feishu-device.dpapi"
$configPath = Join-Path $DataDir "feishu.json"
$needsManage = [bool]$RequireManage
if (Test-Path -LiteralPath $configPath) {
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $needsManage = $needsManage -or [bool]$config.proactive_suggestions
}
$endpoint = "http://127.0.0.1:8000/api/v1/auth"
$result = $null
$saved = $false
$tempFile = Join-Path $DataDir ("device-" + [guid]::NewGuid().ToString("N") + ".tmp")
try {
    if ($needsManage) { Write-Host "Choose a Manage pairing code in Account & privacy for proactive suggestions." }
    $inputCode = Read-Host "Paste the code shown next to the QR code and press Enter" -AsSecureString
    $body = @{code=[Net.NetworkCredential]::new("",$inputCode).Password.Trim();label="Dudu Feishu Gateway";cookie=$false} | ConvertTo-Json
    $result = Invoke-RestMethod -Method Post -Uri "$endpoint/pair" -ContentType application/json -Body $body -MaximumRedirection 0
    if (-not $result.token -or -not (Test-StewardPairScopes -Scopes $result.session.scopes -NeedsManage $needsManage)) {
        throw "Use an appropriate Chat or Manage pairing code. Full owner access is unnecessary."
    }
    $encrypted = ConvertFrom-SecureString (ConvertTo-SecureString $result.token -AsPlainText -Force)
    [IO.Directory]::CreateDirectory($DataDir) | Out-Null
    [IO.File]::WriteAllText($tempFile,$encrypted)
    if ([IO.File]::Exists($path)) { [IO.File]::Replace($tempFile, $path, [System.Management.Automation.Language.NullString]::Value) }
    else { [IO.File]::Move($tempFile, $path) }
    $saved = $true
    Write-Host "Paired successfully."
} catch {
    if ($result.token -and -not $saved) {
        try { Invoke-RestMethod -Method Post -Uri "$endpoint/logout" -Headers @{Authorization="Bearer $($result.token)"} -MaximumRedirection 0 | Out-Null }
        catch { Write-Host "The unused device token could not be revoked. Check the signed-in devices list." }
    }
    Write-Host "Pairing failed. Check the code, its access level and the local profile."
} finally {
    if ([IO.File]::Exists($tempFile)) { [IO.File]::Delete($tempFile) }
}
Read-Host "Press Enter to close" | Out-Null
