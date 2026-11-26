# The default checks do not launch processes, contact services, or show windows. The optional
# integration case only runs its own hidden, time-limited sleepers in two temporary Windows jobs.
param([switch]$ExerciseSyntheticJob)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'steward-lifecycle.ps1')
function Assert([bool]$Condition, [string]$Label) {
    if (-not $Condition) { throw "FAILED: $Label" }
    Write-Output "PASS $Label"
}
$launcher = Join-Path $PSScriptRoot 'start-steward.ps1'
$directory = 'C:\Fixture With Spaces\Dudu'
$ownerSid = 'S-1-5-21-111-222-333-1001'
$spec = New-StewardScope $launcher $directory 8123 $ownerSid
$same = New-StewardScope $launcher $directory.ToUpperInvariant() 8123 $ownerSid
$otherPort = New-StewardScope $launcher $directory 8124 $ownerSid
$otherUser = New-StewardScope $launcher $directory 8123 'S-1-5-21-111-222-333-1002'
$otherData = New-StewardScope $launcher (Join-Path $directory 'other') 8123 $ownerSid
Assert ($spec.Key -eq $same.Key -and $spec.Key -ne $otherPort.Key -and $spec.Key -ne $otherUser.Key -and $spec.Key -ne $otherData.Key) 'scope is stable and bound to install, owner, port and data'
$argsNow = Get-StewardTaskArguments $spec Web
$task = [pscustomobject]@{
    TaskName = 'Suparagent Steward - Web'; TaskPath = '\'
    Principal = [pscustomobject]@{UserId=$ownerSid}
    Actions = @([pscustomobject]@{Execute='powershell.exe'; Arguments=$argsNow; WorkingDirectory=''})
}
Assert (Test-StewardTaskIdentity $task $spec Web) 'trusted current task matches its full action and owner'
$task.Actions[0].Arguments = $argsNow.Replace('-ExecutionPolicy RemoteSigned ', '')
Assert (Test-StewardTaskIdentity $task $spec Web) 'known legacy action matches'
$task.Actions[0].Arguments += ' -Command "anything"'
Assert (-not (Test-StewardTaskIdentity $task $spec Web)) 'extra command invalidates old task'
$task.Actions[0].Arguments = $argsNow
$task.Principal.UserId = $otherUser.Sid
Assert (-not (Test-StewardTaskIdentity $task $spec Web)) 'another owner cannot be stopped'
$task.Principal.UserId = $ownerSid
$task.Actions[0].Execute = 'C:\Fixture\powershell.exe'
Assert (-not (Test-StewardTaskIdentity $task $spec Web)) 'same executable name outside Windows is rejected'
Assert ((Get-StewardEdgeProfile '{"profile":{"last_used":"Profile 4"}}') -eq 'Profile 4') 'keep current Edge profile'
Assert (-not (Get-StewardEdgeProfile '{"profile":{"last_used":"..\\other"}}')) 'ignore unsafe Edge profile name'
Assert (-not (Get-StewardEdgeProfile '{}')) 'let Edge choose its normal profile when no last-used profile is known'
$register = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'register-steward.ps1'))
Assert ($register -notmatch 'New-ScheduledTaskTrigger|Get-Credential|-RestartCount|-StartWhenAvailable') 'install creates no automatic triggers or retries'
function Test-Registration {
    $captured = [Collections.Generic.List[object]]::new()
    $fixture = New-StewardScope $launcher $directory 8123 ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    function Get-ScheduledTask([string]$TaskPath,[string]$TaskName,$ErrorAction) {
        if ($TaskName -ne 'Suparagent Steward - Feishu') { return }
        [pscustomobject]@{TaskName=$TaskName; TaskPath=$TaskPath; Principal=[pscustomobject]@{UserId=$fixture.Sid}
            Triggers=@('old logon trigger'); Actions=@([pscustomobject]@{Execute='powershell.exe';WorkingDirectory='';Arguments=(Get-StewardTaskArguments $fixture Feishu)})}
    }
    function New-ScheduledTaskAction($Execute,$Argument) { [pscustomobject]@{Execute=$Execute;Arguments=$Argument} }
    function New-ScheduledTaskPrincipal($UserId,$LogonType,$RunLevel) { [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel} }
    function New-ScheduledTaskSettingsSet {
        [CmdletBinding()]param($MultipleInstances,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,$ExecutionTimeLimit)
        [pscustomobject]@{MultipleInstances=$MultipleInstances}
    }
    function New-ScheduledTask {
        [CmdletBinding()]param($Action,$Principal,$Settings)
        [pscustomobject]@{Action=$Action;Principal=$Principal;Settings=$Settings}
    }
    function Register-ScheduledTask($TaskPath,$TaskName,$InputObject,[switch]$Force) {
        $captured.Add([pscustomobject]@{Name=$TaskName;Definition=$InputObject;Force=$Force})
    }
    & (Join-Path $PSScriptRoot 'register-steward.ps1') -Port 8123 -DataDir $directory -AtBoot -WarningAction SilentlyContinue -Confirm:$false
    Assert ($captured.Count -eq 2 -and $captured[1].Name -eq 'Suparagent Steward - Feishu') 'reinstall also converts an existing Feishu task when IncludeFeishu was omitted'
    Assert ($captured[0].Definition.Principal.RunLevel -eq 'Limited' -and $captured[0].Definition.Principal.LogonType -eq 'Interactive') 'legacy AtBoot request creates only limited on-demand workers'
}
Test-Registration
$entry = [IO.File]::ReadAllText((Join-Path $PSScriptRoot '../../start-steward.cmd'))
Assert ($entry -notmatch '(?im)^.*\bpause\b') 'failed desktop start cannot leave a console waiting for input'

Import-StewardWorker
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Add-Type -AssemblyName System.Windows.Forms,System.Drawing
    $sources = @((Join-Path $PSScriptRoot 'StewardWindow.cs'),(Join-Path $PSScriptRoot 'StewardExitDialog.cs'))
    Add-Type -Path $sources -ReferencedAssemblies System,System.Core,System.Drawing,System.Windows.Forms
    Assert ([Dudu.ExitDialog]::KeepLabel -ceq '仅关闭网页' -and [Dudu.ExitDialog]::StopLabel -ceq '同时停止服务') 'Windows PowerShell compiles the real native chooser and retains both Chinese labels'
} else { Add-Type -Path (Join-Path $PSScriptRoot 'StewardWindow.cs') }
$id = '0123456789abcdef0123456789abcdef'
Assert ([Dudu.ManagedWindow]::MarkedTitle("Dudu [$id] - Microsoft Edge",$id,$false)) 'Edge suffix permits initial exact window binding'
Assert (-not [Dudu.ManagedWindow]::MarkedTitle("Other Dudu [$id]",$id,$false)) 'unrelated titles cannot bind a window'
Assert (-not [Dudu.ManagedWindow]::MarkedTitle("Dudu close [$id]",$id,$false)) 'a request to close cannot bind another window'
Assert ([Dudu.ManagedWindow]::MarkedTitle("Dudu close [$id]",$id,$true)) 'bound window can request the native chooser'
$calls = [Collections.Generic.List[string]]::new()
$stop = [Action]{ $calls.Add('stop') }
$close = [Action]{ $calls.Add('close') }
[Dudu.DesktopExit]::Apply('Keep',$true,$false,$stop,$close)
Assert (($calls -join ',') -eq 'close') 'keep decision only closes the managed window'
$calls.Clear()
[Dudu.DesktopExit]::Apply('Stop',$true,$false,$stop,$close)
Assert (($calls -join ',') -eq 'stop,close') 'stop decision signals service before closing the managed window'
$calls.Clear()
[Dudu.DesktopExit]::Apply('Stop',$false,$true,$stop,$close)
[Dudu.DesktopExit]::Apply('Dismiss',$true,$false,$stop,$close)
Assert ($calls.Count -eq 0) 'dismissal and Windows session ending leave services alone'
[Dudu.DesktopExit]::Apply('Stop',$false,$false,$stop,$close)
Assert (($calls -join ',') -eq 'stop') 'native X does not close a second window'
$legacy = @($spec.Shell,'-NoProfile','-ExecutionPolicy','RemoteSigned','-File',$spec.Launcher,'-Component','Feishu','-Port','8123','-DataDir',$directory)
Assert (Test-StewardLegacyCommand $legacy $spec Feishu) 'exact previous gateway startup is recognized'
$altered = $legacy.Clone()
$altered[-1] = 'C:\OtherData'
Assert (-not (Test-StewardLegacyCommand $altered $spec Feishu)) 'similar legacy command for another data directory is rejected'
$when = [datetime]::UtcNow
$root = [pscustomobject]@{ProcessId=40;ParentProcessId=999;SessionId=15;CreationDate=$when}
$rows = @( $root,
    [pscustomobject]@{ProcessId=41;ParentProcessId=40;SessionId=15;CreationDate=$when.AddSeconds(1)},
    [pscustomobject]@{ProcessId=42;ParentProcessId=41;SessionId=15;CreationDate=$when.AddSeconds(2)},
    [pscustomobject]@{ProcessId=43;ParentProcessId=41;SessionId=15;CreationDate=$when.AddSeconds(-1)},
    [pscustomobject]@{ProcessId=44;ParentProcessId=40;SessionId=14;CreationDate=$when.AddSeconds(2)},
    [pscustomobject]@{ProcessId=45;ParentProcessId=99;SessionId=15;CreationDate=$when.AddSeconds(3)})
$tree = @(Get-StewardTreeRows $rows $root | ForEach-Object ProcessId | Sort-Object)
Assert (($tree -join ',') -eq '40,41,42') 'old gateway children are matched despite dead outer parent and unrelated/recycled PIDs'
$venvFixture='C:\Dudu\backend\.venv\Scripts\python.exe'
$targetRow=[pscustomobject]@{ProcessId=41;ParentProcessId=40;SessionId=15;CreationDate=$when.AddSeconds(1);ExecutablePath=$venvFixture;CommandLine=('"'+$venvFixture+'" -m app.feishu')}
$hostRow=[pscustomobject]@{ProcessId=49;ParentProcessId=40;SessionId=15;CreationDate=$when.AddSeconds(1);ExecutablePath='C:\Windows\System32\conhost.exe';CommandLine='conhost 0x4'}
$matches=@($targetRow,$hostRow | Where-Object { Test-StewardPythonChild $_ $root $venvFixture @('-m','app.feishu') })
$serviceTree=@(Get-StewardTreeRows ($rows+@($hostRow)) $targetRow | ForEach-Object ProcessId | Sort-Object)
Assert ($matches.Count -eq 1 -and $matches[0].ProcessId -eq 41 -and ($serviceTree -join ',') -eq '41,42') 'real legacy console host and other launcher siblings stay outside the verified service tree'
$tempLease=Join-Path ([IO.Path]::GetTempPath()) ('dudu-lease-test-'+[Guid]::NewGuid().ToString('N')+'.lock')
$stream=[IO.FileStream]::new($tempLease,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite,256,[IO.FileOptions]::DeleteOnClose)
try {
    $stream.WriteByte(1)
    $stream.Lock(0,1)
    $owners=[Dudu.LeaseOwners]::ForFile($tempLease)
    $self=[Diagnostics.Process]::GetCurrentProcess()
    try {
        Assert ($owners.ContainsKey($self.Id) -and $owners[$self.Id] -eq $self.StartTime.ToFileTimeUtc()) 'Windows confirms the synthetic lease holder by exact PID and process creation time'
    } finally { $self.Dispose() }
} finally { $stream.Dispose() }

if ($ExerciseSyntheticJob) {
    $systemShell = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell/v1.0/powershell.exe'
    $command = Join-Path ([Environment]::SystemDirectory) 'cmd.exe'
    if (-not [IO.File]::Exists($command) -or -not [IO.File]::Exists($systemShell)) { throw 'System test executables are unavailable.' }
    $stdioGroup=[Dudu.WorkerGroup]::new($true)
    $stdioSignal=[Threading.EventWaitHandle]::new($false,'ManualReset')
    try {
        $stdioCheck='-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command "if ([Console]::OpenStandardOutput() -eq [IO.Stream]::Null -or [Console]::OpenStandardError() -eq [IO.Stream]::Null) { exit 42 }"'
        Assert ($stdioGroup.Run($systemShell,$stdioCheck,[Environment]::SystemDirectory,$stdioSignal) -eq 0) 'hidden service children retain valid output and error streams for console logging libraries'
    } finally { $stdioSignal.Dispose(); $stdioGroup.Dispose() }
    $fixtures = @()
    try {
        foreach ($name in @('target','control')) {
            $fixture = [pscustomobject]@{ Group=[Dudu.WorkerGroup]::new($true); Signal=[Threading.EventWaitHandle]::new($false,'ManualReset'); Shell=[PowerShell]::Create(); Pending=$null }
            $fixtures += $fixture
            $arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command "Start-Sleep -Seconds 25"'
            $exe = $systemShell
            if ($name -eq 'target') {
                $arguments = '/D /S /C ""' + $systemShell + '" ' + $arguments + '"'
                $exe = $command
            }
            $null = $fixture.Shell.AddScript('param($g,$e,$a,$cwd,$s) $g.Run($e,$a,$cwd,$s)').AddArgument($fixture.Group).AddArgument($exe).AddArgument($arguments).AddArgument([Environment]::SystemDirectory).AddArgument($fixture.Signal)
            $fixture.Pending = $fixture.Shell.BeginInvoke()
        }
        $until = [DateTime]::UtcNow.AddSeconds(10)
        while (($fixtures[0].Group.Active -lt 2 -or $fixtures[1].Group.Active -lt 1) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 50 }
        Assert ($fixtures[0].Group.Active -ge 2 -and $fixtures[1].Group.Active -ge 1) 'hidden synthetic parent and child started in their own job beside a control job'
        [Dudu.DesktopExit]::Apply('Keep',$false,$false,{ $null=$fixtures[0].Signal.Set() },{})
        Assert ($fixtures[0].Group.Active -ge 2 -and $fixtures[1].Group.Active -ge 1) 'real keep handler leaves both synthetic jobs running'
        [Dudu.DesktopExit]::Apply('Stop',$false,$false,{ $null=$fixtures[0].Signal.Set() },{})
        $null = $fixtures[0].Shell.EndInvoke($fixtures[0].Pending)
        Assert ($fixtures[0].Group.Active -eq 0 -and $fixtures[1].Group.Active -ge 1) 'confirmed stop removes the whole synthetic tree and keeps the independent process'
    } finally {
        foreach ($fixture in $fixtures) {
            $null = $fixture.Signal.Set()
            try { if ($fixture.Pending -and -not $fixture.Pending.IsCompleted) { $null=$fixture.Shell.EndInvoke($fixture.Pending) } }
            finally { $fixture.Shell.Dispose(); $fixture.Group.Dispose(); $fixture.Signal.Dispose() }
        }
    }
}
