param([ValidateRange(1,65535)][int]$Port = 8000,
      [string]$DataDir = (Join-Path $env:LOCALAPPDATA "SuparagentSteward"),
      [ValidateSet("Web","Feishu")][string]$Component = "Web")
$ErrorActionPreference = "Stop"
$service = Join-Path $PSScriptRoot "start-steward.ps1"
if ($Component -eq "Feishu") { & $service -Component Feishu -Port $Port -DataDir $DataDir; exit $LASTEXITCODE }
$url = "http://127.0.0.1:$Port"
function Test-StewardReady {
    try {
        $page = Invoke-WebRequest -UseBasicParsing -Uri "$url/" -TimeoutSec 2
        return ($page.Content -match '(?i)<title>[^<]*(?:Suparagent|Dudu)[^<]*</title>' -and
            (Invoke-RestMethod -Uri "$url/api/v1/health" -TimeoutSec 2).status -eq "ok")
    } catch { return $false }
}
function Test-FeishuRunning([string]$LockFile) {
    if (-not [IO.File]::Exists($LockFile)) { return $false }
    $stream = $null
    try {
        $stream = [IO.File]::Open($LockFile, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
        $stream.Lock(0,1)
        $stream.Unlock(0,1)
        return $false
    } catch [IO.IOException] { return $true }
    finally { if ($stream) { $stream.Dispose() } }
}
$lock = New-Object Threading.Mutex($false, "Local\SuparagentLauncher-$Port")
$held = $false
try {
    try { $held = $lock.WaitOne(90000) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { throw "Another launcher is still waiting for the management page." }
    if (-not (Test-StewardReady)) {
        $arguments = "-NoProfile -ExecutionPolicy RemoteSigned -File `"$service`" -Port $Port -DataDir `"$DataDir`""
        $process = Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds(75)
        while (-not (Test-StewardReady)) {
            if ($process.HasExited) { throw "Suparagent could not start. Check whether port $Port is already in use." }
            if ([DateTime]::UtcNow -gt $deadline) { throw "Suparagent did not become ready on port $Port." }
            Start-Sleep -Milliseconds 400
        }
    }
    $configFile = if ($env:FEISHU_CONFIG) { $env:FEISHU_CONFIG } else { Join-Path $DataDir 'feishu.json' }
    if ([IO.File]::Exists($configFile)) {
        try {
            $config = [IO.File]::ReadAllText($configFile) | ConvertFrom-Json
            $channel = [IO.Path]::Combine([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($configFile)), [string]$config.state_dir)
            if ($config.steward_thread -and -not (Test-FeishuRunning (Join-Path $channel 'gateway.lock'))) {
                $gatewayArgs = "-NoProfile -ExecutionPolicy RemoteSigned -File `"$service`" -Component Feishu -Port $Port -DataDir `"$DataDir`""
                Start-Process powershell.exe -ArgumentList $gatewayArgs -WindowStyle Hidden
            }
        } catch { Write-Warning "Could not start the configured Feishu gateway: $($_.Exception.Message)" }
    }
} finally {
    if ($held) { $lock.ReleaseMutex() }
    $lock.Dispose()
}
Start-Process $url
