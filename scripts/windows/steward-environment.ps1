function Test-StewardPairScopes {
    param([string[]]$Scopes, [bool]$NeedsManage)
    return ('chat' -in $Scopes -and 'admin' -notin $Scopes -and
        @($Scopes | Where-Object { $_ -notin @('chat','files','manage') }).Count -eq 0 -and
        (-not $NeedsManage -or 'manage' -in $Scopes))
}

function Get-StewardCodexProxy {
    param([Parameter(Mandatory)][Net.IWebProxy]$Proxy)
    $api = $Proxy.GetProxy([uri]'https://api.openai.com')
    $chat = $Proxy.GetProxy([uri]'https://chatgpt.com')
    if (-not $api -or $api -ne $chat -or $api.Scheme -ne 'http' -or
        $api.Host -notin @('localhost','127.0.0.1','[::1]') -or $api.UserInfo -or
        $api.AbsolutePath -ne '/' -or $api.Query -or $api.Fragment) { return }
    return ('http://{0}:{1}' -f $api.Host,$api.Port)
}

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
    if (-not $env:FEISHU_CONFIG) { $env:FEISHU_CONFIG = Join-Path $DataDir "feishu.json" }
    $env:DECISION_LOG_DIR = Join-Path $DataDir "decisions"
    $env:ATTACHMENTS_DIR = Join-Path $DataDir "attachments"
    $env:LOCAL_WORKSPACE_ROOT = Join-Path $DataDir "workspace"
    $env:LOCAL_EXECUTION_ENABLED = "true"
    $pilotFile = Join-Path $DataDir "employee-project-runtime.json"
    if ([IO.File]::Exists($pilotFile)) {
        try {
            $pilot = Get-Content -LiteralPath $pilotFile -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
            $ids = foreach ($id in @($pilot.employees)) {
                if ($null -eq $id) { continue }
                if (($id -isnot [int] -and $id -isnot [long]) -or $id -le 0) {
                    throw 'Employee IDs must be positive integers.'
                }
                "local-owner/$id"
            }
            if (-not $env:SUPARAGENT_EMPLOYEE_NATIVE_IDS) {
                $env:SUPARAGENT_EMPLOYEE_NATIVE_IDS = (@($ids | Select-Object -Unique) -join ",")
            }
            if (-not $env:SUPARAGENT_WEEKLY_KNOWLEDGE_ENABLED -and
                ($pilot.weekly_knowledge -is [bool]) -and $pilot.weekly_knowledge) {
                $env:SUPARAGENT_WEEKLY_KNOWLEDGE_ENABLED = "1"
            }
        } catch {
            Write-Warning "Invalid employee-project-runtime.json. Check the employee IDs and weekly_knowledge setting."
        }
    }
    $networkSettings = Join-Path $DataDir "codex-network.json"
    if (-not $env:SUPARAGENT_CODEX_HTTPS_PROXY -and [IO.File]::Exists($networkSettings)) {
        $useProxy = (Get-Content -LiteralPath $networkSettings -Raw -Encoding UTF8 | ConvertFrom-Json).use_windows_proxy
        if ($useProxy -is [bool] -and $useProxy) {
            try {
                $proxyAddress = Get-StewardCodexProxy -Proxy ([Net.WebRequest]::GetSystemWebProxy())
                if ($proxyAddress) { $env:SUPARAGENT_CODEX_HTTPS_PROXY = $proxyAddress }
            } catch { Write-Warning "Could not read the Windows proxy. Codex will use its normal network settings." }
        }
    }
    if (-not $env:LOCAL_CODEX_CLI_PATH -and -not (Get-Command codex -CommandType Application -ErrorAction SilentlyContinue)) {
        $package = Get-AppxPackage -Name OpenAI.Codex -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($package.InstallLocation) {
            $bundledCli = Join-Path $package.InstallLocation "app/resources/codex.exe"
            if ([IO.File]::Exists($bundledCli)) { $env:LOCAL_CODEX_CLI_PATH = $bundledCli }
        }
    }
    $env:STEWARD_WHISPER_MODEL_DIR = Join-Path $DataDir "models/faster-whisper-tiny"
    $env:STEWARD_STRICT_OWNER = "local-owner"
    $env:SUPARAGENT_HOST = "127.0.0.1"
    $env:TRUSTED_HOSTS = "127.0.0.1,localhost,[::1]"
    $env:LOOPBACK_TRUST = "off"
    $privateSettings = Join-Path $DataDir "private-access.json"
    if ([IO.File]::Exists($privateSettings)) {
        $dnsName = (Get-Content -LiteralPath $privateSettings -Raw -Encoding UTF8 | ConvertFrom-Json).dns_name
        if ($dnsName -isnot [string] -or $dnsName -notmatch '^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net$') {
            throw "Invalid private hostname in private-access.json."
        }
        $env:SUPARAGENT_PUBLIC_URL = "https://$dnsName"
        $env:FRONTEND_ORIGINS = "http://localhost:3000,$env:SUPARAGENT_PUBLIC_URL"
    }
    $env:PYTHONUTF8 = "1"
}
