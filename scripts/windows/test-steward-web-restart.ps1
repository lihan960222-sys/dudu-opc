# Exercise a real Dudu web worker twice on a disposable hosted Windows runner.
# The database and port are created here. No scheduled task or browser is involved.
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'This smoke test requires a disposable GitHub-hosted Windows runner.'
}
. (Join-Path $PSScriptRoot 'steward-lifecycle.ps1')
$testData = Join-Path $env:RUNNER_TEMP ('dudu-web-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($testData)
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
try { $listener.Start(); $testPort = $listener.LocalEndpoint.Port } finally { $listener.Stop() }
$launcher = Join-Path $PSScriptRoot 'start-steward.ps1'
$scope = New-StewardScope $launcher $testData $testPort ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
$lease = Join-Path $testData 'suparagent.db.backend-lease'
$url = "http://127.0.0.1:$testPort/api/v1/health"
$env:FEISHU_CONFIG = Join-Path $testData 'unconfigured-feishu.json'

function Test-Ready {
    try { return (Invoke-RestMethod -Uri $url -TimeoutSec 2 -MaximumRedirection 0).status -eq 'ok' }
    catch { return $false }
}

foreach ($round in 1,2) {
    $start = [Diagnostics.ProcessStartInfo]::new($scope.Shell, (Get-StewardTaskArguments $scope Web))
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardError = $true
    $worker = [Diagnostics.Process]::Start($start)
    $errors = $worker.StandardError.ReadToEndAsync()
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds(90)
        while (!(Test-Ready)) {
            if ($worker.HasExited) { throw "Web worker exited before ready: $($errors.GetAwaiter().GetResult())" }
            if ([DateTime]::UtcNow -ge $deadline) { throw 'Web worker did not start within 90 seconds.' }
            Start-Sleep -Milliseconds 250
        }
        if (!(Test-StewardHeld $lease) -or ![IO.File]::Exists((Join-Path $testData 'suparagent.db'))) {
            throw 'The running worker does not own the new test database.'
        }
        # Only exercise the managed stop signal created by this fresh worker.
        $ownEvent = [Threading.EventWaitHandle]::OpenExisting("$($scope.Key)-Web-stop")
        $ownEvent.Dispose()
        Stop-StewardComponent $scope Web $lease
        if (!$worker.WaitForExit(10000) -or $worker.ExitCode -ne 0 -or (Test-Ready) -or (Test-StewardHeld $lease)) {
            throw 'The worker, database lease or HTTP endpoint survived the requested stop.'
        }
        Write-Output "PASS cycle $round - started on demand, served health, released the database and closed the port."
    } finally {
        if (!$worker.HasExited) { $worker.Kill(); [void]$worker.WaitForExit(5000) }
        $worker.Dispose()
    }
}
