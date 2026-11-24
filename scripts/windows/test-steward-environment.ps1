param([Parameter(Mandatory)][string]$Scratch)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "steward-environment.ps1")
Initialize-StewardEnvironment -DataDir $Scratch
$key = $env:TOKEN_ENCRYPTION_KEYS
if ([Convert]::FromBase64String($key.Replace("-","+").Replace("_","/")).Length -ne 32) { throw "Invalid Fernet key" }
if ((Get-Content -Raw (Join-Path $Scratch "key.dpapi")).Contains($key)) { throw "Key written in clear" }
Initialize-StewardEnvironment -DataDir $Scratch
if ($key -ne $env:TOKEN_ENCRYPTION_KEYS) { throw "Key changed across launches" }
if ($env:SUPARAGENT_HOST -ne "127.0.0.1" -or $env:DATABASE_BACKEND -ne "sqlite" -or $env:LOOPBACK_TRUST -ne "off") { throw "Unsafe local profile" }
if ($env:SQLITE_PATH -ne (Join-Path $Scratch "suparagent.db")) { throw "Wrong database" }
Write-Output "PASS local profile, DPAPI key reuse, loopback and database"
$privateConfig = Join-Path $Scratch "private-access.json"
if (Test-Path -LiteralPath $privateConfig) { throw "Use an empty test profile" }
try {
    [IO.File]::WriteAllText($privateConfig, '{"dns_name":"home.example.ts.net"}')
    Initialize-StewardEnvironment -DataDir $Scratch
    if ($env:SUPARAGENT_PUBLIC_URL -ne "https://home.example.ts.net" -or $env:LOOPBACK_TRUST -ne "off") {
        throw "Private origin or required owner sign-in is missing"
    }
    [IO.File]::WriteAllText($privateConfig, '{"dns_name":"invalid.example.com"}')
    $rejected = $false
    try { Initialize-StewardEnvironment -DataDir $Scratch } catch { $rejected = $true }
    if (-not $rejected) { throw "Non-tailnet origin accepted" }
} finally { [IO.File]::Delete($privateConfig) }
Write-Output "PASS authenticated private origin"
if (-not (Test-StewardPairScopes -Scopes @('chat','files','manage') -NeedsManage $true)) { throw "Valid management pairing rejected" }
if (Test-StewardPairScopes -Scopes @('chat') -NeedsManage $true) { throw "Proactive mode accepted chat-only pairing" }
if (-not (Test-StewardPairScopes -Scopes @('chat') -NeedsManage $false)) { throw "Chat pairing rejected" }
if (Test-StewardPairScopes -Scopes @('admin','chat','files') -NeedsManage $false) { throw "Gateway accepted a full owner token" }
Write-Output "PASS gateway pairing scope"
$loopbackProxy = [Net.WebProxy]::new("http://127.0.0.1:4567")
if ((Get-StewardCodexProxy -Proxy $loopbackProxy) -ne 'http://127.0.0.1:4567') { throw "Loopback proxy not recognized" }
foreach ($address in @("http://user:password@localhost:4567", "https://localhost:4567", "http://example.org:4567", "http://localhost:4567/path")) {
    if (Get-StewardCodexProxy -Proxy ([Net.WebProxy]::new($address))) { throw "Unexpected proxy accepted" }
}
$splitProxy = [Net.WebProxy]::new("http://localhost:4567", $false, [string[]]@('chatgpt\.com'))
if (Get-StewardCodexProxy -Proxy $splitProxy) { throw "A proxy bypass for a model endpoint was ignored" }
Write-Output "PASS local Codex proxy and endpoint bypass"
