function New-StewardScope([string]$Launcher, [string]$DataDir, [int]$Port, [string]$Sid) {
    $Launcher = [IO.Path]::GetFullPath($Launcher)
    $DataDir = [IO.Path]::GetFullPath($DataDir).TrimEnd('\')
    if ($DataDir -match '^[a-z]:$') { $DataDir += '\' }
    $value = ($Launcher.ToUpperInvariant(),$DataDir.ToUpperInvariant(),$Port,$Sid) -join [char]0
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $key = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($value))).Replace('-','') }
    finally { $hash.Dispose() }
    [pscustomobject]@{Launcher=$Launcher; DataDir=$DataDir; Port=$Port; Sid=$Sid; Key="Local\Dudu-$key"
        Shell=(Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe')}
}

function Get-StewardTaskArguments($Scope, [string]$Component) {
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "{0}" -DataDir "{1}" -Port {2} -Component {3}' -f $Scope.Launcher,$Scope.DataDir,$Scope.Port,$Component
}

function Test-StewardTaskIdentity($Task, $Scope, [string]$Component) {
    if (-not $Task -or $Task.TaskPath -ne '\' -or $Task.TaskName -ne "Suparagent Steward - $Component") { return $false }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1 -or $actions[0].WorkingDirectory -or $actions[0].Execute -notin @('powershell.exe',$Scope.Shell)) { return $false }
    $argsNow = Get-StewardTaskArguments $Scope $Component
    if ($actions[0].Arguments -notin @($argsNow,$argsNow.Replace('-ExecutionPolicy RemoteSigned ',''))) { return $false }
    $sid = $Task.Principal.UserId
    if ($sid -notmatch '^S-1-') {
        try { $sid = ([Security.Principal.NTAccount]::new($sid)).Translate([Security.Principal.SecurityIdentifier]).Value } catch { return $false }
    }
    return $sid -eq $Scope.Sid
}

function Get-StewardEdgeProfile([string]$LocalState) {
    try { $name = ($LocalState | ConvertFrom-Json -ErrorAction Stop).profile.last_used } catch { return }
    if ($name -is [string] -and $name -cmatch '^(Default|Profile [0-9]+)$') { return $name }
}

function Get-StewardFeishuLock($Scope) {
    $configPath = if ($env:FEISHU_CONFIG) { $env:FEISHU_CONFIG } else { Join-Path $Scope.DataDir 'feishu.json' }
    if (-not [IO.File]::Exists($configPath)) { return }
    $config = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json
    if (-not $config.steward_thread -or -not $config.state_dir) { return }
    [IO.Path]::GetFullPath([IO.Path]::Combine([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($configPath)), $config.state_dir,'gateway.lock'))
}

function Test-StewardHeld([string]$Path) {
    if (-not $Path -or -not [IO.File]::Exists($Path)) { return $false }
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path,'Open','ReadWrite','ReadWrite')
        $stream.Lock(0,1)
        $stream.Unlock(0,1)
        return $false
    } catch [IO.IOException] { return $true }
    finally { if ($stream) { $stream.Dispose() } }
}

function Test-StewardLegacyCommand([string[]]$Tokens, $Scope, [string]$Component) {
    if ($Tokens.Count -lt 2 -or $Tokens[0] -notin @('powershell.exe',$Scope.Shell)) { return $false }
    $tail = @('-File',$Scope.Launcher,'-DataDir',$Scope.DataDir,'-Port',[string]$Scope.Port,'-Component',$Component)
    $registered = @('-NoProfile','-NonInteractive','-WindowStyle','Hidden')
    $policy = @('-ExecutionPolicy','RemoteSigned')
    $direct = @('-NoProfile') + $policy + @('-File',$Scope.Launcher)
    if ($Component -eq 'Feishu') { $direct += @('-Component','Feishu') }
    $direct += @('-Port',[string]$Scope.Port,'-DataDir',$Scope.DataDir)
    $separator = [char]31
    $actual = $Tokens[1..($Tokens.Count-1)] -join $separator
    return $actual -in @(($direct -join $separator),(($registered + $tail) -join $separator),(($registered + $policy + $tail) -join $separator))
}

function Import-StewardWorker {
    if (-not ('Dudu.WorkerGroup' -as [type])) {
        Add-Type -TypeDefinition ([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'StewardWorker.cs')))
    }
}

function Get-StewardTreeRows($Rows, $Root) {
    $accepted = @{ [int]$Root.ProcessId = $Root }
    do {
        $added = $false
        foreach ($row in $Rows) {
            $parent = $accepted[[int]$row.ParentProcessId]
            if ($null -ne $parent -and -not $accepted.ContainsKey([int]$row.ProcessId) -and
                $row.CreationDate -ge $parent.CreationDate -and $row.SessionId -eq $Root.SessionId) {
                $accepted[[int]$row.ProcessId] = $row
                $added = $true
            }
        }
    } while ($added)
    return $accepted.Values
}

function Get-StewardPinnedProcess($Row, $Scope, [switch]$CanStop) {
    [Dudu.PinnedWorker]::Open([int]$Row.ProcessId,$Row.CreationDate.ToFileTimeUtc(),$Scope.Sid,$CanStop.IsPresent)
}

function Add-StewardPinnedDescendants($Rows, $Root, $Scope, $Pinned) {
    do {
        $added = $false
        foreach ($row in $Rows) {
            $id = [int]$row.ProcessId
            if ($Pinned.ContainsKey($id) -or $row.SessionId -ne $Root.SessionId) { continue }
            $parent = $Pinned[[int]$row.ParentProcessId]
            # A retained handle supplies both ends of the real parent's lifespan,
            # even if that parent is absent from the latest process snapshot.
            if (-not $parent -or -not $parent.CouldHaveSpawned($row.CreationDate.ToFileTimeUtc())) { continue }
            $pin = Get-StewardPinnedProcess $row $Scope -CanStop
            if ($pin) { $Pinned[$id] = $pin; $added = $true }
        }
    } while ($added)
}

function Stop-StewardPinnedTree($Root, $Scope, $Pinned, [scriptblock]$Snapshot = {
    Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,CreationDate,SessionId
}) {
    for ($pass=0; $pass -lt 8; $pass++) {
        $latest = @(& $Snapshot)
        Add-StewardPinnedDescendants $latest $Root $Scope $Pinned
        # Open and check every observed handle before the first termination. Stop
        # older parents first, then catch any children that appeared during this pass.
        $live = @($Pinned.Values | Where-Object { -not $_.Exited } | Sort-Object Birth)
        if (-not $live.Count) { return }
        foreach ($pin in $live) { $pin.Stop() }
    }
    throw 'The verified process tree is still changing; check the remaining service processes.'
}

function Test-StewardPythonChild($Child,$Root,[string]$Python,[string[]]$Expected) {
    if (-not $Child -or $Child.ParentProcessId -ne $Root.ProcessId -or $Child.SessionId -ne $Root.SessionId -or
        $Child.CreationDate -lt $Root.CreationDate -or $Child.ExecutablePath -ne $Python) { return $false }
    $argv = [Dudu.WorkerGroup]::Arguments($Child.CommandLine)
    return $argv.Count -eq $Expected.Count+1 -and $argv[0] -eq $Python -and
        ($argv[1..($argv.Count-1)] -join [char]31) -eq ($Expected -join [char]31)
}

function Stop-StewardLegacyGroup($Scope, [string]$Component, [string]$Lease, [switch]$InspectOnly) {
    $repo = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetDirectoryName($Scope.Launcher)) '../..'))
    $python = [IO.Path]::GetFullPath((Join-Path $repo 'backend/.venv/Scripts/python.exe'))
    $expected = if ($Component -eq 'Feishu') { @('-m','app.feishu') } else { @('-m','app.server','--host','127.0.0.1','--port',[string]$Scope.Port) }
    $owners = [Dudu.LeaseOwners]::ForFile($Lease)
    $rows = @(Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,CreationDate,SessionId,Name)
    $candidates = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -Property ProcessId,ParentProcessId,CreationDate,SessionId,ExecutablePath,CommandLine)
    foreach ($root in $candidates) {
        if ($root.ExecutablePath -ne $Scope.Shell -or -not (Test-StewardLegacyCommand ([Dudu.WorkerGroup]::Arguments($root.CommandLine)) $Scope $Component)) { continue }
        $direct = @(Get-CimInstance Win32_Process -Filter ("ParentProcessId={0} AND Name='python.exe'" -f $root.ProcessId) |
            Where-Object { Test-StewardPythonChild $_ $root $python $expected })
        if ($direct.Count -ne 1) { continue }
        $child = $direct[0]
        # The old PowerShell launcher may also own a console host or other Windows helper.
        # Only the genuine Python service and its descendants may be stopped.
        $tree = @(Get-StewardTreeRows $rows $child)
        $pinned = @{}
        $launcher = $null
        try {
            $launcher = Get-StewardPinnedProcess $root $Scope
            if (-not $launcher) { continue }
            $service = Get-StewardPinnedProcess $child $Scope -CanStop
            if (-not $service) { continue }
            $pinned[$service.Id] = $service
            if (-not $launcher.CouldHaveSpawned($service.Birth)) { continue }
            Add-StewardPinnedDescendants $tree $child $Scope $pinned
            $leaseConfirmed = $false
            foreach ($pin in $pinned.Values) {
                if ($owners.ContainsKey($pin.Id) -and $owners[$pin.Id] -eq $pin.Birth) { $leaseConfirmed=$true; break }
            }
            if (-not $leaseConfirmed) { continue }
            if ($InspectOnly) { return $true }
            Stop-StewardPinnedTree $child $Scope $pinned
            return
        } finally {
            foreach ($proc in $pinned.Values) { $proc.Dispose() }
            if ($launcher) { $launcher.Dispose() }
        }
    }
    throw "Could not prove that the running $Component service belongs to this Dudu installation."
}

function Stop-StewardComponent($Scope, [string]$Component, [string]$Lease) {
    $signal = $null
    if ([Threading.EventWaitHandle]::TryOpenExisting("$($Scope.Key)-$Component-stop",[ref]$signal)) {
        try { $null=$signal.Set() } finally { $signal.Dispose() }
    } elseif (Test-StewardHeld $Lease) {
        Stop-StewardLegacyGroup $Scope $Component $Lease
    } else { return }
    $deadline = [DateTime]::UtcNow.AddSeconds(12)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = $null
        $present = [Threading.EventWaitHandle]::TryOpenExisting("$($Scope.Key)-$Component-stop",[ref]$remaining)
        if ($remaining) { $remaining.Dispose() }
        if (-not $present -and -not (Test-StewardHeld $Lease)) { return }
        Start-Sleep -Milliseconds 150
    }
    throw "The $Component service has not released its worker or its data lock."
}
