[CmdletBinding()]
param(
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "SuparagentSteward"),
    [ValidateRange(1,65535)][int]$Port = 8000,
    [ValidateSet("Web","Feishu")][string]$Component = "Web"
)
$ErrorActionPreference = "Stop"
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
$backend = Join-Path $repo "backend"
$python = Join-Path $backend ".venv/Scripts/python.exe"
if (-not [IO.File]::Exists($python)) { throw "Install the backend first: cd backend, then uv sync --locked." }
. (Join-Path $PSScriptRoot "steward-environment.ps1")
Initialize-StewardEnvironment -DataDir $DataDir
$env:SUPARAGENT_PORT = [string]$Port
$env:SUPARAGENT_UI_DIR = Join-Path $repo "frontend/out"
Push-Location $backend
try {
    if ($Component -eq "Feishu") {
        if (-not $env:FEISHU_CONFIG) { $env:FEISHU_CONFIG = Join-Path $DataDir "feishu.json" }
        if (-not [IO.File]::Exists($env:FEISHU_CONFIG)) { throw "Missing Feishu configuration: $env:FEISHU_CONFIG" }
        & $python -m app.feishu
    } else {
        if (-not [IO.File]::Exists((Join-Path $env:SUPARAGENT_UI_DIR "index.html"))) {
            throw "Build the local pages first: node scripts/build-ui.js"
        }
        & $python -m app.server migrate
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $python -m app.server --host 127.0.0.1 --port $Port
    }
    exit $LASTEXITCODE
} finally { Pop-Location }
