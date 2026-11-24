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
    $legacyArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -DataDir "{1}" -Port {2} -Component {3}' -f $launcher,$DataDir,$Port,$component
    $arguments = $legacyArguments.Replace('-WindowStyle Hidden -File', '-WindowStyle Hidden -ExecutionPolicy RemoteSigned -File')
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        $present = @($existing.Actions)
        if ($AtBoot -or $present.Count -ne 1 -or $present[0].Execute -ne 'powershell.exe' -or
            $existing.Principal.LogonType -ne 'Interactive' -or $present[0].Arguments -notin @($arguments, $legacyArguments)) {
            throw "A different task named '$name' already exists. Inspect its action and trigger."
        }
        if ($present[0].Arguments -eq $legacyArguments) {
            Set-ScheduledTask -TaskName $name -Action $action | Out-Null
        }
        continue
    }
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
