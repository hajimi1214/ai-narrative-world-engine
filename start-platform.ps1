param([switch] $Development, [switch] $NoBrowser)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$apiPort = 8000
$webPort = 3000
$logRoot = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

function Test-PortListening([int] $port) {
  $client = [System.Net.Sockets.TcpClient]::new()
  try { return $client.ConnectAsync("127.0.0.1", $port).Wait(300) -and $client.Connected }
  catch { return $false }
  finally { $client.Dispose() }
}

function Test-ApiReady {
  try {
    $health = Invoke-RestMethod "http://127.0.0.1:$apiPort/health" -TimeoutSec 2
    if ($health.status -ne "ok" -or $health.service -ne "ai-narrative-world-engine") { return $false }
    $probe = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$apiPort/projects" -TimeoutSec 2
    return $probe.StatusCode -eq 200
  } catch { return $false }
}

function Test-WebReady {
  try {
    $probe = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$webPort/" -TimeoutSec 2
    return $probe.StatusCode -eq 200 -and $probe.Content.Contains("LONG-FORM NOVEL DESK")
  } catch { return $false }
}

function Quote-PowerShellLiteral([string] $value) { return "'" + $value.Replace("'", "''") + "'" }

function Start-PlatformProcess([string] $workingDirectory, [string] $command, [string] $logName) {
  $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
  Start-Process powershell -WorkingDirectory $workingDirectory -WindowStyle Hidden -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded
  ) -RedirectStandardOutput (Join-Path $logRoot "$logName.log") -RedirectStandardError (Join-Path $logRoot "$logName.error.log") | Out-Null
}

if (Test-PortListening $apiPort) {
  if (-not (Test-ApiReady)) { throw "Port $apiPort is occupied by an unready or different service. Close that service, then restart the platform. No process was stopped." }
} else {
  $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
  if (-not (Test-Path -LiteralPath $python)) { throw "Python environment missing. Install dependencies using the README first." }
  $quotedPython = Quote-PowerShellLiteral $python
  $databaseUrl = if ($env:DATABASE_URL) { $env:DATABASE_URL } else { "sqlite:///./narrative.db" }
  $configureDatabase = if ($env:DATABASE_URL) { "`$env:DATABASE_URL=$(Quote-PowerShellLiteral $databaseUrl); " } else { "" }
  $prepare = if ($databaseUrl.StartsWith("sqlite")) { "& $quotedPython apps/api/prepare_local_db.py" } else { "& $quotedPython -m alembic -c apps/api/alembic.ini upgrade head" }
  Start-PlatformProcess $projectRoot "$configureDatabase$prepare; if (`$LASTEXITCODE -ne 0) { throw 'Database preparation failed.' }; & $quotedPython -m uvicorn app.main:app --app-dir apps/api --host 127.0.0.1 --port $apiPort" "api"
}

if (Test-PortListening $webPort) {
  if (-not (Test-WebReady)) { throw "Port $webPort is occupied by an unready or different service. Close that service, then restart the platform. No process was stopped." }
} else {
  $webRoot = Join-Path $projectRoot "apps\web"
  $pnpm = (Get-Command pnpm -ErrorAction Stop).Source
  $mode = if ($Development -or -not (Test-Path -LiteralPath (Join-Path $webRoot ".next\BUILD_ID"))) { "dev" } else { "start" }
  Start-PlatformProcess $webRoot "`$env:NEXT_TELEMETRY_DISABLED='1'; & $(Quote-PowerShellLiteral $pnpm) $mode --hostname 127.0.0.1 --port $webPort" "web"
}

$deadline = [DateTime]::UtcNow.AddSeconds(90)
do {
  if ((Test-ApiReady) -and (Test-WebReady)) {
    Write-Host "Platform ready: http://127.0.0.1:$webPort" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$webPort" | Out-Null }
    exit 0
  }
  Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $deadline)
throw "Platform did not become ready. See logs/api.error.log and logs/web.error.log. Your novel data has not been replaced."
