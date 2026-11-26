[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "SuparagentSteward"),
    [ValidateRange(1,65535)][int]$Port = 8000,
    [ValidateSet("Web","Feishu")][string]$Component = "Web"
)
$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSEdition -eq "Desktop") {
    $env:PSModulePath = (Join-Path $PSHOME "Modules") + ";" + $env:PSModulePath
}
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
$backend = Join-Path $repo "backend"
$python = Join-Path $backend ".venv/Scripts/python.exe"
if (-not [IO.File]::Exists($python)) { throw "Install the backend first: cd backend, then uv sync --locked." }
. (Join-Path $PSScriptRoot "steward-environment.ps1")
. (Join-Path $PSScriptRoot 'steward-lifecycle.ps1')
Import-StewardWorker
$scope = New-StewardScope $PSCommandPath $DataDir $Port ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
$created = $false
$stop = [Threading.EventWaitHandle]::new($false,'ManualReset',"$($scope.Key)-$Component-stop",[ref]$created)
if (-not $created) { $stop.Dispose(); exit 0 }
$group = $null
try {
    $group = [Dudu.WorkerGroup]::new($true)
    Initialize-StewardEnvironment -DataDir $DataDir
    $env:SUPARAGENT_PORT = [string]$Port
    $env:SUPARAGENT_UI_DIR = Join-Path $repo "frontend/out"
    if ($Component -eq "Feishu") {
        if (-not $env:FEISHU_CONFIG) { $env:FEISHU_CONFIG = Join-Path $DataDir "feishu.json" }
        if (-not [IO.File]::Exists($env:FEISHU_CONFIG)) { throw "Missing Feishu configuration: $env:FEISHU_CONFIG" }
        $gatewayConfig = Get-Content -LiteralPath $env:FEISHU_CONFIG -Raw -Encoding UTF8 | ConvertFrom-Json
        $appId = $gatewayConfig.steward.app_id
        if ($appId -match '^cli_[a-zA-Z0-9]+$' -and $gatewayConfig.steward.secret_env -eq 'FEISHU_STEWARD_SECRET' -and -not $env:FEISHU_STEWARD_SECRET) {
            $secretFile = Join-Path $DataDir "$appId.dpapi"
            if ([IO.File]::Exists($secretFile)) {
                $secret = ConvertTo-SecureString ([IO.File]::ReadAllText($secretFile))
                $env:FEISHU_STEWARD_SECRET = [Net.NetworkCredential]::new("",$secret).Password
            }
        }
        $deviceFile = Join-Path $DataDir "feishu-device.dpapi"
        if ($gatewayConfig.backend_token_env -eq 'FEISHU_BACKEND_TOKEN' -and -not $env:FEISHU_BACKEND_TOKEN -and [IO.File]::Exists($deviceFile)) {
            $device = ConvertTo-SecureString ([IO.File]::ReadAllText($deviceFile))
            $env:FEISHU_BACKEND_TOKEN = [Net.NetworkCredential]::new("",$device).Password
        }
        $exitCode = $group.Run($python,'-m app.feishu',$backend,$stop)
    } else {
        if (-not [IO.File]::Exists((Join-Path $env:SUPARAGENT_UI_DIR "index.html"))) {
            throw "Build the local pages first: node scripts/build-ui.js"
        }
        $exitCode = $group.Run($python,'-m app.server migrate',$backend,$stop)
        if ($exitCode -eq 0) {
            $exitCode = $group.Run($python,"-m app.server --host 127.0.0.1 --port $Port",$backend,$stop)
        }
    }
    exit $exitCode
} finally {
    if ($group) { $group.Dispose() }
    $stop.Dispose()
}
