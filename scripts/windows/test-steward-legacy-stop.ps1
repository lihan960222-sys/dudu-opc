# Only disposable hidden sleepers are started. No Dudu services, Python, Task Scheduler or visible UI.
param([string]$ChildRecord,[string]$GrandchildRecord)
$ErrorActionPreference='Stop'
$exe=Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell/v1.0/powershell.exe'
function Start-Sleeper([string]$argsText) {
    $start=[Diagnostics.ProcessStartInfo]::new($exe,$argsText)
    $start.UseShellExecute=$false; $start.CreateNoWindow=$true; $start.WindowStyle='Hidden'
    [Diagnostics.Process]::Start($start)
}
$head='-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden '
if ($ChildRecord) {
    $argsText = if ($GrandchildRecord) { '-ExecutionPolicy RemoteSigned -File "'+$PSCommandPath+'" -ChildRecord "'+$GrandchildRecord+'"' }
        else { '-Command "Start-Sleep -Seconds 25"' }
    $leaf=Start-Sleeper ($head + $argsText)
    try { [IO.File]::WriteAllText($ChildRecord, [string]$leaf.Id); $null=$leaf.WaitForExit(25000) }
    finally { $leaf.Dispose() }
    return
}
. (Join-Path $PSScriptRoot 'steward-lifecycle.ps1')
Import-StewardWorker
[Dudu.WorkerGroup]::Quiet()
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
public sealed class DuduFixtureJob : IDisposable {
    [DllImport("kernel32",SetLastError=true)] static extern IntPtr CreateJobObjectW(IntPtr a,IntPtr n);
    [DllImport("kernel32",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32",SetLastError=true)] static extern bool IsProcessInJob(IntPtr process,IntPtr job,out bool member);
    [DllImport("kernel32")] static extern bool CloseHandle(IntPtr handle);
    readonly IntPtr job=CreateJobObjectW(IntPtr.Zero,IntPtr.Zero);
    public int TryAttach(Process p) {
        bool member;
        if(!IsProcessInJob(p.Handle,job,out member)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return member || AssignProcessToJobObject(job,p.Handle) ? 0 : Marshal.GetLastWin32Error();
    }
    public void Dispose() { CloseHandle(job); }
}
'@
$privateRecord=Join-Path ([IO.Path]::GetTempPath()) ('dudu-legacy-child-'+[guid]::NewGuid().ToString('N')+'.pid')
$lateRecord=$privateRecord+'.late'
$oldJob=[DuduFixtureJob]::new()
$candidate=[DuduFixtureJob]::new()
$root=$leaf=$grandchild=$control=$null
$pinned=@{}
try {
    $root=Start-Sleeper ($head+'-ExecutionPolicy RemoteSigned -File "'+$PSCommandPath+'" -ChildRecord "'+$privateRecord+'" -GrandchildRecord "'+$lateRecord+'"')
    $control=Start-Sleeper ($head+'-Command "Start-Sleep -Seconds 25"')
    $deadline=[DateTime]::UtcNow.AddSeconds(10)
    do {
        if ([IO.File]::Exists($privateRecord)) { $childText=[IO.File]::ReadAllText($privateRecord) }
        if ([IO.File]::Exists($lateRecord)) { $grandText=[IO.File]::ReadAllText($lateRecord) }
        if ($childText -match '^\d+$' -and $grandText -match '^\d+$') { break }
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Synthetic child did not start.' }
        Start-Sleep -Milliseconds 40
    } while ($true)
    $leaf=[Diagnostics.Process]::GetProcessById([int]$childText)
    $grandchild=[Diagnostics.Process]::GetProcessById([int]$grandText)
    if ($oldJob.TryAttach($leaf) -ne 0 -or $candidate.TryAttach($root) -ne 0 -or $candidate.TryAttach($root) -ne 0) {
        throw 'Could not construct the nested-job fixture or repeat root membership.'
    }
    if ($candidate.TryAttach($leaf) -ne 5) { throw 'The incompatible sibling job did not return Windows error 5.' }
    Write-Output 'PASS repeated membership is harmless; attaching a child in a sibling Windows job returns ERROR_ACCESS_DENIED'
    $scope=[pscustomobject]@{Sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value}
    $rootRow=[pscustomobject]@{ProcessId=$root.Id;ParentProcessId=$PID;SessionId=$root.SessionId;CreationDate=$root.StartTime}
    $leafRow=[pscustomobject]@{ProcessId=$leaf.Id;ParentProcessId=$root.Id;SessionId=$leaf.SessionId;CreationDate=$leaf.StartTime}
    $sideRow=[pscustomobject]@{ProcessId=$control.Id;ParentProcessId=$PID;SessionId=$control.SessionId;CreationDate=$control.StartTime}
    $lateRow=[pscustomobject]@{ProcessId=$grandchild.Id;ParentProcessId=$leaf.Id;SessionId=$grandchild.SessionId;CreationDate=$grandchild.StartTime}
    $testRows=@($rootRow,$leafRow,$sideRow)
    $pinned[$root.Id]=Get-StewardPinnedProcess $rootRow $scope -CanStop
    $pinned[$leaf.Id]=Get-StewardPinnedProcess $leafRow $scope -CanStop
    if ($root.HasExited -or $leaf.HasExited -or $control.HasExited) { throw 'Stop preflight changed a synthetic process.' }
    Write-Output 'PASS stop rights and creation times are checked without changing either existing Windows job'
    $snapshotCount=@{n=0}
    Stop-StewardPinnedTree $rootRow $scope $pinned {
        $snapshotCount.n++
        if ($snapshotCount.n -eq 1) { $testRows } else { $testRows + @($lateRow) }
    }
    Stop-StewardPinnedTree $rootRow $scope $pinned { $testRows }
    if (-not $root.HasExited -or -not $leaf.HasExited -or -not $grandchild.HasExited -or $control.HasExited) { throw 'Pinned stop or harmless retry targeted the wrong tree.' }
    Write-Output 'PASS both verified synthetic workers exit across existing jobs; repeated stop preserves their unrelated sibling'
    if ($pinned[$leaf.Id].CouldHaveSpawned([datetime]::UtcNow.AddSeconds(1).ToFileTimeUtc())) { throw 'A recycled parent PID could admit an unrelated later child.' }
    Write-Output 'PASS a child first observed after its pinned parent exited is stopped; a post-exit birth is excluded'
} finally {
    foreach ($entry in $pinned.Values) { if ($entry) { $entry.Dispose() } }
    foreach ($process in @($grandchild,$leaf,$root,$control)) {
        if ($process) {
            try { if (-not $process.HasExited) { $process.Kill(); $null=$process.WaitForExit(5000) } }
            finally { $process.Dispose() }
        }
    }
    $candidate.Dispose(); $oldJob.Dispose()
    if ([IO.File]::Exists($privateRecord)) { [IO.File]::Delete($privateRecord) }
    if ([IO.File]::Exists($lateRecord)) { [IO.File]::Delete($lateRecord) }
}
