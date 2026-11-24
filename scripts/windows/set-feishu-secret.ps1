param([ValidatePattern('^cli_[a-zA-Z0-9]+$')][string]$AppId)
$ErrorActionPreference = "Stop"
if (-not $AppId) {
    $profilePath = Join-Path $env:LOCALAPPDATA "SuparagentSteward/feishu.json"
    if (Test-Path -LiteralPath $profilePath) {
        $AppId = (Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json).steward.app_id
    }
    if (-not $AppId) { $AppId = Read-Host "Feishu App ID (cli_...)" }
}
if ($AppId -notmatch '^cli_[a-zA-Z0-9]+$') { throw "A valid Feishu App ID is required." }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Security
$form = New-Object Windows.Forms.Form
$form.Text = "Dudu - Connect Feishu"
$form.Width = 480
$form.Height = 175
$form.StartPosition = "CenterScreen"
$form.TopMost = $true
$form.Add_Shown({ $form.Activate(); $inputBox.Focus() | Out-Null })
$label = New-Object Windows.Forms.Label
$label.Text = "App Secret for $AppId"
$label.SetBounds(16,12,430,24)
$inputBox = New-Object Windows.Forms.TextBox
$inputBox.UseSystemPasswordChar = $true
$inputBox.SetBounds(16,42,430,24)
$save = New-Object Windows.Forms.Button
$save.Text = "Save"
$save.SetBounds(365,78,80,30)
$form.Controls.AddRange(@($label,$inputBox,$save))
$form.AcceptButton = $save
$save.Add_Click({
    if (-not $inputBox.Text.Trim()) { $label.Text = "Paste the App Secret first."; return }
    try {
        $step = "Protect input"
        $bytes = [Text.Encoding]::Unicode.GetBytes($inputBox.Text.Trim())
        try {
            $blob = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        } finally { [Array]::Clear($bytes, 0, $bytes.Length) }
        # Windows PowerShell reads current-user DPAPI ciphertext as a hex string.
        $encrypted = [BitConverter]::ToString($blob).Replace("-", "").ToLowerInvariant()
        $step = "Open settings folder"
        $directory = [IO.Path]::Combine($env:LOCALAPPDATA, "SuparagentSteward")
        $null = [IO.Directory]::CreateDirectory($directory)
        $path = [IO.Path]::Combine($directory, "$AppId.dpapi")
        $step = "Write encrypted file"
        [IO.File]::WriteAllText($path,$encrypted)
        $step = "Verify encrypted file"
        if ([IO.File]::ReadAllText($path) -cne $encrypted) { throw "Incomplete write" }
        $inputBox.Clear()
        [Windows.Forms.MessageBox]::Show($form,"Saved on this computer.","Dudu") | Out-Null
        $form.Close()
    } catch {
        $reason = $_.Exception.GetBaseException().GetType().Name
        [Windows.Forms.MessageBox]::Show($form,"$step failed ($reason).","Dudu setup") | Out-Null
    }
})
try { $null = $form.ShowDialog() } finally { $inputBox.Clear(); $form.Dispose() }
