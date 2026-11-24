[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "SuparagentSteward"),
    [ValidateRange(1,65535)][int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$cmd = Get-Command tailscale.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$cli = if ($cmd) { $cmd.Source } else { Join-Path $env:ProgramFiles "Tailscale/tailscale.exe" }
if (-not [IO.File]::Exists($cli)) { throw "Install Tailscale on the home computer first." }
$local = "http://127.0.0.1:$Port"
$owner = Invoke-RestMethod -Uri "$local/api/v1/auth/status" -TimeoutSec 5
if (-not $owner.owner_configured) { throw "Set the owner's password on the local management page first." }
$node = (& $cli status --json | Out-String) | ConvertFrom-Json
if ($node.BackendState -ne "Running") { throw "Sign this computer into your tailnet, then run this command again." }
$dnsName = ([string]$node.Self.DNSName).TrimEnd(".").ToLowerInvariant()
if ($dnsName -notmatch '^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net$') {
    throw "Tailscale did not return a private DNS name. Check MagicDNS."
}
$serveState = & $cli serve status --json
if ($LASTEXITCODE -ne 0) { throw "Could not inspect existing Tailscale services." }
$serve = ($serveState | Out-String) | ConvertFrom-Json
$address = "${dnsName}:443"
if ($serve.AllowFunnel.$address) { throw "The requested port is already shared publicly. Choose a private port before continuing." }
$rootHandler = $serve.Web.$address.Handlers.'/'
if ($rootHandler -and ([string]$rootHandler.Proxy).TrimEnd("/") -ne $local) {
    throw "Another application already uses the private HTTPS root."
}
if (-not $rootHandler) {
    if ($serve.TCP.'443' -and -not $serve.TCP.'443'.HTTPS) { throw "Port 443 is already used by a different private service." }
    & $cli serve --bg --https=443 $local
    if ($LASTEXITCODE -ne 0) { throw "Tailscale Serve did not enable private HTTPS." }
}
$settingsPath = Join-Path $DataDir "private-access.json"
$tmp = Join-Path $DataDir ("private-access-" + [Guid]::NewGuid().ToString("N") + ".tmp")
try {
    [IO.File]::WriteAllText($tmp, (@{dns_name=$dnsName} | ConvertTo-Json -Compress))
    if ([IO.File]::Exists($settingsPath)) { [IO.File]::Replace($tmp, $settingsPath, [System.Management.Automation.Language.NullString]::Value) }
    else { [IO.File]::Move($tmp, $settingsPath) }
} finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
Write-Output "Private management address: https://$dnsName"
Write-Output "The Web component reads this setting at startup. Sign in with the existing owner account."
