[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'SuparagentSteward'),
    [ValidateRange(1,65535)][int]$Port = 8000,
    [switch]$IncludeFeishu,
    [switch]$AtBoot
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'steward-lifecycle.ps1')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$scope = New-StewardScope (Join-Path $PSScriptRoot 'start-steward.ps1') $DataDir $Port $identity.User.Value
if ($AtBoot) { Write-Warning 'Dudu now starts from its desktop shortcut. The legacy AtBoot flag is ignored.' }
foreach ($component in @('Web','Feishu')) {
    $name = "Suparagent Steward - $component"
    $existing = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
    if (-not $existing -and $component -eq 'Feishu' -and -not $IncludeFeishu) { continue }
    if ($existing -and -not (Test-StewardTaskIdentity $existing $scope $component)) {
        throw "The existing '$name' belongs to a different account or installation."
    }
    if (-not $PSCmdlet.ShouldProcess($name, 'Install an on-demand worker without automatic startup')) { continue }
    $action = New-ScheduledTaskAction -Execute $scope.Shell -Argument (Get-StewardTaskArguments $scope $component)
    $principal = New-ScheduledTaskPrincipal -UserId $identity.User.Value -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    $definition = New-ScheduledTask -Action $action -Principal $principal -Settings $settings
    $null = Register-ScheduledTask -TaskPath '\' -TaskName $name -InputObject $definition -Force
}
