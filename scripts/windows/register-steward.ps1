[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "SuparagentSteward"),
    [ValidateRange(1,65535)][int]$Port = 8000,
    [switch]$IncludeFeishu,
    [switch]$AtBoot
)
$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "start-steward.ps1"
$DataDir = [IO.Path]::GetFullPath($DataDir)
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$components = @("Web")
if ($IncludeFeishu) { $components += "Feishu" }
foreach ($component in $components) {
    $name = "Suparagent Steward - $component"
    if (-not $PSCmdlet.ShouldProcess($name, "Register task for $user")) { continue }
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) { throw "A task named '$name' already exists. Inspect it before replacing it." }
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -DataDir "{1}" -Port {2} -Component {3}' -f $launcher,$DataDir,$Port,$component
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 15 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    if ($AtBoot) {
        if (-not $credential) { $credential = Get-Credential -UserName $user -Message "Windows account for running before logon" }
        if (-not $credential -or $credential.UserName -ne $user) { throw "Use the Windows account that created the profile." }
        $trigger = New-ScheduledTaskTrigger -AtStartup
        Register-ScheduledTask -TaskName $name -Action $action -Settings $settings -Trigger $trigger -User $user -Password $credential.GetNetworkCredential().Password | Out-Null
    } else {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        Register-ScheduledTask -TaskName $name -Action $action -Settings $settings -Trigger $trigger -User $user | Out-Null
    }
}
