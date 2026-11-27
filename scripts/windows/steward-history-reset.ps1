# The Web launcher remains alive while both data writers are stopped. Only a
# fixed local module can perform cleanup. It independently validates the request.
function Get-StewardHistoryRequestId($Scope) {
    try {
        $path = $Scope.DataDir
        foreach ($part in 'employee-settings','history-reset','request.json') {
            $path = Join-Path $path $part
            if ([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint) { return }
        }
        $stream = [IO.File]::Open($path,'Open','Read','Read')
        try {
            if ($stream.Length -gt 100) { return }
            $reader = [IO.StreamReader]::new($stream)
            try { $id = ($reader.ReadToEnd() | ConvertFrom-Json -ErrorAction Stop).id }
            finally { $reader.Dispose() }
        } finally { $stream.Dispose() }
        if ($id -is [string] -and $id -cmatch '\A[0-9a-f]{32}\z') { return $id }
    } catch { return }
}

function Invoke-StewardHistoryWorker($Python, $Backend, [ValidateSet('check','apply','fail')]$Action,
        [ValidatePattern('^[0-9a-f]{32}$')][string]$RequestId) {
    $worker = [Dudu.WorkerGroup]::new($true)
    $neverStop = [Threading.ManualResetEvent]::new($false)
    try {
        return $worker.Run($Python, "-m app.history_reset_worker $Action $RequestId", $Backend, $neverStop)
    } finally { $worker.Dispose(); $neverStop.Dispose() }
}

function Resume-StewardHistoryGateway($Scope, $State) {
    if ($State.Barrier) { $State.Barrier.Dispose(); $State.Barrier = $null }
    if (!$State.Restore) { return }
    if (Test-StewardHeld $State.Lease) { $State.Restore = $false; return }
    $task = Get-ScheduledTask -TaskPath '\' -TaskName 'Suparagent Steward - Feishu' -ErrorAction SilentlyContinue
    if ((Test-StewardTaskIdentity $task $Scope Feishu) -and $task.Settings.AllowDemandStart -and
        $task.Principal.RunLevel -eq 'Limited' -and $task.State -ne 'Disabled') {
        $deadline = [datetime]::UtcNow.AddSeconds(12)
        while ($task.State -in 'Running','Queued' -and [datetime]::UtcNow -lt $deadline) {
            if (Test-StewardHeld $State.Lease) { $State.Restore = $false; return }
            Start-Sleep -Milliseconds 180
            $task = Get-ScheduledTask -TaskPath '\' -TaskName $task.TaskName
        }
        Start-ScheduledTask -InputObject $task
    } else {
        $start = [Diagnostics.ProcessStartInfo]::new($Scope.Shell,(Get-StewardTaskArguments $Scope Feishu))
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.WindowStyle = 'Hidden'
        $child = [Diagnostics.Process]::Start($start)
        $child.Dispose()
    }
    $deadline = [datetime]::UtcNow.AddSeconds(30)
    while (!(Test-StewardHeld $State.Lease)) {
        if ([datetime]::UtcNow -ge $deadline) { throw 'Feishu did not reacquire its gateway lease.' }
        Start-Sleep -Milliseconds 200
    }
    $State.Restore = $false
}

function Start-StewardHistoryWindow($Scope, $State, $Python, $Backend) {
    $State.RequestId = Get-StewardHistoryRequestId $Scope
    if (!$State.RequestId) { return $false }
    try {
        # The validator retires a stale/busy request itself. A second 'fail'
        # here could otherwise retire a new confirmation made after that.
        if ((Invoke-StewardHistoryWorker $Python $Backend check $State.RequestId) -ne 0) { return $false }
        $State.Lease = Get-StewardFeishuLock $Scope
        if (!$State.Lease) { throw 'Cannot identify the Feishu gateway.' }
        if (Test-StewardHeld $State.Lease) {
            $State.Restore = $true
            Stop-StewardComponent $Scope Feishu $State.Lease
        }
        # Prevent an overlapping launcher from reopening the gateway.
        $created = $false
        $State.Barrier = [Threading.EventWaitHandle]::new($false,'ManualReset',"$($Scope.Key)-Feishu-stop",[ref]$created)
        if (!$created -or (Test-StewardHeld $State.Lease)) { throw 'Gateway is not offline.' }
        if ((Invoke-StewardHistoryWorker $Python $Backend check $State.RequestId) -ne 0) {
            Resume-StewardHistoryGateway $Scope $State
            return $false
        }
        return $true
    } catch {
        try { $null = Invoke-StewardHistoryWorker $Python $Backend fail $State.RequestId }
        catch { [Console]::Error.WriteLine("Could not retire the history reset request: {0}", $_.Exception.Message) }
        try { Resume-StewardHistoryGateway $Scope $State }
        catch { [Console]::Error.WriteLine("Could not restore the Feishu gateway: {0}", $_.Exception.Message) }
        return $false
    }
}

function Invoke-StewardWebLoop($Scope, $Python, $Backend, $Stop) {
    $marker = Join-Path $Scope.DataDir 'employee-settings/history-reset/request.json'
    $state = @{ Restore=$false; Barrier=$null; Lease=$null; RequestId=$null }
    $prepare = [Func[bool]] { Start-StewardHistoryWindow $Scope $state $Python $Backend }
    $started = [Action] {
        if ($state.Restore) {
            $deadline = [datetime]::UtcNow.AddSeconds(45)
            while ([datetime]::UtcNow -lt $deadline) {
                try {
                    if ((Invoke-RestMethod -Uri "http://127.0.0.1:$($Scope.Port)/api/v1/health" -TimeoutSec 2 -MaximumRedirection 0).status -eq 'ok') { break }
                } catch {}
                Start-Sleep -Milliseconds 250
            }
        }
        try { Resume-StewardHistoryGateway $Scope $state }
        catch { [Console]::Error.WriteLine("Could not restore the Feishu gateway: {0}", $_.Exception.Message) }
    }
    try {
        do {
            $web = [Dudu.WorkerGroup]::new($true)
            try {
                $exitCode = $web.RunManaged($Python, "-m app.server --host 127.0.0.1 --port $($Scope.Port)",
                    $Backend, $Stop, $marker, $prepare, $started)
                if (!$web.MaintenanceRequested) { return $exitCode }
            } finally { $web.Dispose() }

            # The Web stop handle remains owned by this supervisor. Both
            # public launchers are excluded until the cleaner finishes.
            $lease = Join-Path $Scope.DataDir 'suparagent.db.backend-lease'
            try {
                if ((Test-StewardHeld $lease) -or (Test-StewardHeld $state.Lease)) { throw 'Services are not offline.' }
                if ((Invoke-StewardHistoryWorker $Python $Backend apply $state.RequestId) -ne 0) { throw 'Cleanup failed.' }
            } catch {
                try { $null = Invoke-StewardHistoryWorker $Python $Backend fail $state.RequestId }
                catch { [Console]::Error.WriteLine("Could not retire the history reset request: {0}", $_.Exception.Message) }
            }
        } while (!$Stop.WaitOne(0))
        return 0
    } finally { Resume-StewardHistoryGateway $Scope $state }
}
