param([Parameter(Mandatory)][string]$Scratch)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "steward-environment.ps1")
Initialize-StewardEnvironment -DataDir $Scratch
$key = $env:TOKEN_ENCRYPTION_KEYS
if ([Convert]::FromBase64String($key.Replace("-","+").Replace("_","/")).Length -ne 32) { throw "Invalid Fernet key" }
if ((Get-Content -Raw (Join-Path $Scratch "key.dpapi")).Contains($key)) { throw "Key written in clear" }
Initialize-StewardEnvironment -DataDir $Scratch
if ($key -ne $env:TOKEN_ENCRYPTION_KEYS) { throw "Key changed across launches" }
if ($env:SUPARAGENT_HOST -ne "127.0.0.1" -or $env:DATABASE_BACKEND -ne "sqlite" -or $env:LOOPBACK_TRUST -ne "off") { throw "Unsafe local profile" }
if ($env:SQLITE_PATH -ne (Join-Path $Scratch "suparagent.db")) { throw "Wrong database" }
Write-Output "PASS local profile, DPAPI key reuse, loopback and database"
