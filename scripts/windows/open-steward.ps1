param([ValidateRange(1,65535)][int]$Port = 8000,
      [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'SuparagentSteward'),
      [ValidateSet('Web','Feishu')][string]$Component = 'Web')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'steward-lifecycle.ps1')
$scope = New-StewardScope (Join-Path $PSScriptRoot 'start-steward.ps1') $DataDir $Port ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
if ($Component -eq 'Feishu') { & $scope.Launcher -Component Feishu -Port $Port -DataDir $DataDir; exit $LASTEXITCODE }
$url = "http://127.0.0.1:$Port"
$webLease = Join-Path $scope.DataDir 'suparagent.db.backend-lease'

function Test-StewardReady {
    try {
        if (-not (Test-StewardHeld $webLease)) { return $false }
        $page = Invoke-WebRequest -UseBasicParsing -Uri "$url/" -MaximumRedirection 0 -TimeoutSec 2
        return ($page.Content -match '(?i)<title>[^<]*(?:Suparagent|Dudu)[^<]*</title>' -and
            (Invoke-RestMethod -Uri "$url/api/v1/health" -MaximumRedirection 0 -TimeoutSec 2).status -eq 'ok')
    } catch { return $false }
}

function Start-StewardComponent([string]$Name) {
    $start = [Diagnostics.ProcessStartInfo]::new($scope.Shell,(Get-StewardTaskArguments $scope $Name))
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    [Diagnostics.Process]::Start($start)
}

function Wait-StewardComponent([string]$Name,[scriptblock]$Ready,$process = $null) {
    if (-not $process) {
        if (& $Ready) { return }
        $process = Start-StewardComponent $Name
    }
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds(75)
        while (-not (& $Ready)) {
            if ([DateTime]::UtcNow -ge $deadline -or ($process.HasExited -and $process.ExitCode -ne 0)) {
                if ($Name -eq 'Feishu') { throw '飞书机器人未能连接，请检查飞书配置和设备登录状态。' }
                throw "工作台未能在本机端口 $Port 启动。请检查端口是否被占用，以及本机运行目录的权限。"
            }
            Start-Sleep -Milliseconds 350
        }
    } finally { $process.Dispose() }
}

function Open-OrdinaryStewardPage {
    Start-Process $url
    Connect-StewardFeishu
}

function Connect-StewardFeishu {
    try {
        if ($feishuStartError) { throw $feishuStartError }
        if ($feishuProcess) { Wait-StewardComponent Feishu { Test-StewardHeld $feishuLease } $feishuProcess }
    } catch {
        Add-Type -AssemblyName System.Windows.Forms
        $null = [Windows.Forms.MessageBox]::Show("飞书未启动。工作台仍可使用。" + [Environment]::NewLine + $_.Exception.Message,'嘟嘟')
    }
}

function Get-StewardEdge {
    $edgePaths = @(${env:ProgramFiles(x86)},$env:ProgramFiles) | Where-Object { $_ } | ForEach-Object { Join-Path $_ 'Microsoft/Edge/Application/msedge.exe' }
    $edgePaths | Where-Object { [IO.File]::Exists($_) } | Select-Object -First 1
}

function Open-StewardWindow([Threading.EventWaitHandle]$Activate) {
    $edge = Get-StewardEdge
    if (-not $edge) { Open-OrdinaryStewardPage; return }
    try {
        Add-Type -AssemblyName System.Windows.Forms,System.Drawing
        $source = @((Join-Path $PSScriptRoot 'StewardWindow.cs'),(Join-Path $PSScriptRoot 'StewardExitDialog.cs'))
        Add-Type -Path $source -ReferencedAssemblies System,System.Core,System.Drawing,System.Windows.Forms
    } catch { Open-OrdinaryStewardPage; return }
    $id = [guid]::NewGuid().ToString('N')
    $window = [Dudu.ManagedWindow]::new($edge,$id,$scope.Key)
    [Dudu.ExitDialog]::Watch()
    try {
        $argsEdge = "--app=$url/#dudu-managed=$id"
        $localState = Join-Path $env:LOCALAPPDATA 'Microsoft/Edge/User Data/Local State'
        if ([IO.File]::Exists($localState)) {
            try { $profile = Get-StewardEdgeProfile ([IO.File]::ReadAllText($localState)) } catch { $profile = $null }
            if ($profile) { $argsEdge += ' --profile-directory="' + $profile + '"' }
        }
        try {
            # Existing Edge may accept the request and the returned process may immediately exit.
            # The newly created HWND is matched separately from this short-lived launcher.
            $browser = [Diagnostics.Process]::Start($edge,$argsEdge)
            $browser.Dispose()
        } catch { Open-OrdinaryStewardPage; return }
        $deadline = [DateTime]::UtcNow.AddSeconds(65)
        while (-not $window.Bind()) {
            if ([DateTime]::UtcNow -ge $deadline -or [Dudu.ExitDialog]::Ending) { return }
            Start-Sleep -Milliseconds 200
        }
        Connect-StewardFeishu
        $prompted = $false
        while (-not [Dudu.ExitDialog]::Ending) {
            if ($Activate.WaitOne(200)) { $window.Activate() }
            $alive = $window.Alive
            $requested = $alive -and $window.Requested
            if ($alive -and (-not $requested -or $prompted)) {
                if (-not $requested) { $prompted = $false }
                continue
            }
            if (-not $alive) {
                Start-Sleep -Milliseconds 550
                if ([Dudu.ExitDialog]::Ending) { return }
            }
            $prompted = $true
            $choice = [Dudu.ExitDialog]::Choose()
            try {
                [Dudu.DesktopExit]::Apply($choice,$requested,[Dudu.ExitDialog]::Ending,{
                    $problems = @()
                    foreach ($part in @('Feishu','Web')) {
                        try {
                            $lease = if ($part -eq 'Feishu') { Get-StewardFeishuLock $scope } else { $webLease }
                            Stop-StewardComponent $scope $part $lease
                        } catch { $problems += $_.Exception.Message }
                    }
                    if ($problems) { throw ($problems -join [Environment]::NewLine) }
                },{ $window.Close() })
            } catch {
                [Dudu.ExitDialog]::Report("部分服务还在运行。请检查未能确认归属或退出的服务。" + [Environment]::NewLine + $_.Exception.GetBaseException().Message)
                if (-not $alive) { return }
                continue
            }
            if (-not $alive -or $choice -ne [Dudu.ExitChoice]::Dismiss) { return }
        }
    } finally { $window.Dispose(); [Dudu.ExitDialog]::Unwatch() }
}

$windowLock = [Threading.Mutex]::new($false,"$($scope.Key)-desktop")
$activate = [Threading.EventWaitHandle]::new($false,'AutoReset',"$($scope.Key)-activate")
$held=$false
try {
    try { $held = $windowLock.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) {
        $null=$activate.Set()
        $edge = Get-StewardEdge
        if ($edge) {
            try {
                Add-Type -Path (Join-Path $PSScriptRoot 'StewardWindow.cs')
                [Dudu.ManagedWindow]::Reactivate($scope.Key,$edge)
            } catch { Write-Verbose 'The existing Dudu window is still opening.' }
        }
        return
    }
    Import-StewardWorker
    [Dudu.WorkerGroup]::Quiet()
    Wait-StewardComponent Web { Test-StewardReady }
    $feishuProcess = $null
    $feishuStartError = $null
    try {
        $feishuLease = Get-StewardFeishuLock $scope
        if ($feishuLease -and -not (Test-StewardHeld $feishuLease)) {
            $feishuProcess = Start-StewardComponent Feishu
        }
    } catch { $feishuStartError = $_ }
    Open-StewardWindow $activate
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    $null = [Windows.Forms.MessageBox]::Show($_.Exception.Message,'嘟嘟')
} finally {
    if ($held) { $windowLock.ReleaseMutex() }
    $activate.Dispose()
    $windowLock.Dispose()
}
