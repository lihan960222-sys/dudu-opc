# Isolated Windows lifecycle smoke. All marker files, locks and Python children
# belong to this fixture; it has no connection to the installed Dudu services.
param([Parameter(Mandatory=$true)][string]$Python, [string]$TempRoot = [IO.Path]::GetTempPath())
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'steward-lifecycle.ps1')
. (Join-Path $PSScriptRoot 'steward-history-reset.ps1')
Import-StewardWorker
$Python = (Get-Item -LiteralPath $Python).FullName
$guid = [guid]::NewGuid().ToString('N')
$temp = (Get-Item -LiteralPath $TempRoot).FullName.TrimEnd('\')
$folder = [IO.Path]::GetFullPath((Join-Path $temp "dudu-reset-$guid"))
$scope = [pscustomobject]@{Key="Local\Dudu-test-$guid"; DataDir=$folder; Port=65001}
$never = [Threading.ManualResetEvent]::new($false)
try {
    $app = [IO.Directory]::CreateDirectory((Join-Path $folder 'app')).FullName
    $markerDir = [IO.Directory]::CreateDirectory((Join-Path $folder 'employee-settings/history-reset')).FullName
    $marker = Join-Path $markerDir 'request.json'
    $request = @{id=$guid} | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $app '__init__.py'),'')
    [IO.File]::WriteAllText((Join-Path $app 'server.py'), @'
from pathlib import Path
import time
with open('starts', 'a') as stream:
    stream.write('S')
if Path('starts').read_text() == 'S':
    time.sleep(8)
'@)
    [IO.File]::WriteAllText((Join-Path $app 'history_reset_worker.py'), @'
from pathlib import Path
import json, sys
command = sys.argv[1]
with open('actions', 'a') as stream:
    stream.write(command+'\n')
req = Path('employee-settings/history-reset/request.json')
assert len(sys.argv) == 3 and sys.argv[2] == json.loads(req.read_text())['id']
if command == 'check':
    if Path('busy').exists():
        req.unlink()
        sys.exit(1)
    sys.exit(0)
if command == 'apply' and Path('starts').read_text() != 'S':
    sys.exit(1)
req.unlink(missing_ok=True)
'@)
    function Get-StewardFeishuLock($Scope) { Join-Path $Scope.DataDir 'unused.lock' }
    function Stop-StewardComponent { throw 'Offline fixture gateway must never be stopped' }
    [IO.File]::WriteAllText($marker,$request)
    if ((Invoke-StewardWebLoop $scope $Python $folder $never) -ne 0 -or
        [IO.File]::ReadAllText((Join-Path $folder 'starts')) -ne 'SS' -or
        ([IO.File]::ReadAllLines((Join-Path $folder 'actions')) -join ',') -ne 'check,check,apply') {
        throw 'Reset did not verify, stop, apply once, and return to service.'
    }
    [IO.File]::WriteAllText($marker,$request)
    [IO.File]::WriteAllText((Join-Path $folder 'busy'),'busy')
    $state = @{ Restore=$false; Barrier=$null; Lease=$null }
    if ((Start-StewardHistoryWindow $scope $state $Python $folder) -or $state.Barrier -or
        ([IO.File]::ReadAllLines((Join-Path $folder 'actions')) -join ',') -ne 'check,check,apply,check') {
        throw 'Busy work must refuse reset before Web or Feishu stops.'
    }
    $gateway = @{ online=$true; stops=0; starts=0 }
    function Test-StewardHeld($Path) { return $Path.EndsWith('unused.lock') -and $gateway.online }
    function Stop-StewardComponent { $gateway.online=$false; $gateway.stops++ }
    function Get-ScheduledTask {
        [pscustomobject]@{Settings=@{AllowDemandStart=$true}; Principal=@{RunLevel='Limited'}; State='Ready'}
    }
    function Test-StewardTaskIdentity { return $true }
    function Start-ScheduledTask { $gateway.starts++; $gateway.online=$true }
    function Invoke-RestMethod { return @{status='ok'} }
    [IO.File]::WriteAllText((Join-Path $folder 'starts'),'')
    [IO.File]::Delete((Join-Path $folder 'busy'))
    [IO.File]::WriteAllText($marker,$request)
    if ((Invoke-StewardWebLoop $scope $Python $folder $never) -ne 0 -or !$gateway.online -or
        $gateway.stops -ne 1 -or $gateway.starts -ne 1) { throw 'Did not restore the active gateway exactly once.' }
    # Replace the marker after the gateway stops. The old attempt must carry
    # its original ID into the second check, then restore the gateway.
    $replacement = [guid]::NewGuid().ToString('N')
    function Stop-StewardComponent {
        $gateway.online=$false
        [IO.File]::WriteAllText($marker,(@{id=$replacement} | ConvertTo-Json -Compress))
    }
    [IO.File]::WriteAllText($marker,$request)
    if ((Start-StewardHistoryWindow $scope $state $Python $folder) -or !$gateway.online -or
        $state.RequestId -ne $guid -or (Get-StewardHistoryRequestId $scope) -ne $replacement) {
        throw 'A superseded request was allowed to act on the new confirmation.'
    }
    'PASS idle reset, two checks, web restart, offline gateway, active gateway restoration, busy and superseded requests'
} finally {
    $never.Dispose()
    $target = (Resolve-Path -LiteralPath $folder -ErrorAction SilentlyContinue).Path
    if ($target -and [IO.Path]::GetDirectoryName($target) -eq $temp -and [IO.Path]::GetFileName($target) -eq "dudu-reset-$guid") {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
