function Initialize-StewardEnvironment {
    param([Parameter(Mandatory)][string]$DataDir)
    $DataDir = [IO.Path]::GetFullPath($DataDir)
    [IO.Directory]::CreateDirectory($DataDir) | Out-Null
    $keyPath = Join-Path $DataDir "key.dpapi"
    if (-not (Test-Path -LiteralPath $keyPath)) {
        if (Test-Path -LiteralPath (Join-Path $DataDir "suparagent.db")) {
            throw "Missing key.dpapi for an existing database. Restore the original key under its Windows account."
        }
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $key = [Convert]::ToBase64String($bytes).Replace("+","-").Replace("/","_")
        $protected = ConvertFrom-SecureString (ConvertTo-SecureString $key -AsPlainText -Force)
        $temp = Join-Path $DataDir ([Guid]::NewGuid().ToString() + ".tmp")
        try {
            [IO.File]::WriteAllText($temp, $protected)
            try { [IO.File]::Move($temp, $keyPath) }
            catch [IO.IOException] { if (-not [IO.File]::Exists($keyPath)) { throw } }
        } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
    }
    $secret = ConvertTo-SecureString ([IO.File]::ReadAllText($keyPath))
    $env:TOKEN_ENCRYPTION_KEYS = [Net.NetworkCredential]::new("", $secret).Password
    $env:DATABASE_BACKEND = "sqlite"
    $env:SQLITE_PATH = Join-Path $DataDir "suparagent.db"
    $env:DECISION_LOG_DIR = Join-Path $DataDir "decisions"
    $env:ATTACHMENTS_DIR = Join-Path $DataDir "attachments"
    $env:LOCAL_WORKSPACE_ROOT = Join-Path $DataDir "workspace"
    $env:LOCAL_EXECUTION_ENABLED = "true"
    $env:STEWARD_STRICT_OWNER = "local-owner"
    $env:SUPARAGENT_HOST = "127.0.0.1"
    $env:TRUSTED_HOSTS = "127.0.0.1,localhost,[::1]"
    $env:LOOPBACK_TRUST = "off"
    $env:PYTHONUTF8 = "1"
}
