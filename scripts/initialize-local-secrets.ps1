$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$settingsPath = Join-Path $projectRoot ".env"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
  throw "Python environment is missing. Install project dependencies before creating the local credential key."
}

$lines = [System.Collections.Generic.List[string]]::new()
if (Test-Path -LiteralPath $settingsPath) {
  foreach ($line in [System.IO.File]::ReadAllLines($settingsPath)) {
    $lines.Add($line)
  }
}

$masterKeyLine = -1
for ($index = 0; $index -lt $lines.Count; $index++) {
  if ($lines[$index] -match '^\s*AI_CREDENTIAL_MASTER_KEY\s*=(.*)$') {
    $masterKeyLine = $index
    $configuredValue = $Matches[1].Trim().Trim('"').Trim("'")
    if ($configuredValue -and $configuredValue -ne "生成后持久保存的Fernet密钥") {
      Write-Host "本地密钥库主密钥已经配置，文件未修改。"
      exit 0
    }
  }
}

$masterKey = $env:AI_CREDENTIAL_MASTER_KEY
if (-not $masterKey) {
  $masterKey = & $python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode('ascii'))"
  if ($LASTEXITCODE -ne 0 -or -not $masterKey) {
    throw "Unable to create the local credential key."
  }
}

$setting = "AI_CREDENTIAL_MASTER_KEY=$masterKey"
if ($masterKeyLine -ge 0) {
  $lines[$masterKeyLine] = $setting
} else {
  $lines.Add($setting)
}

$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines($settingsPath, $lines, $utf8WithoutBom)
$setting = $null
$masterKey = $null
$lines = $null
Write-Host "本地密钥库主密钥已保存到 .env。脚本不会显示密钥值。请在保存项目 API 密钥前完成此步骤。"
