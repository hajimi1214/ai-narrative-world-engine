$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$apiPort = 8000
$webPort = 3000
$databaseUrl = if ($env:DATABASE_URL) { $env:DATABASE_URL } else { "sqlite:///./narrative.db" }
$logRoot = Join-Path $projectRoot "logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

function Test-PortListening([int] $port) {
  return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

if (Test-PortListening $apiPort) {
  $apiProbe = try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$apiPort/projects" -TimeoutSec 3 } catch { $null }
  if (-not $apiProbe -or $apiProbe.StatusCode -ne 200) {
    $staleApi = Get-NetTCPConnection -LocalPort $apiPort -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $staleApi) { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
  }
}

if (-not (Test-PortListening $apiPort)) {
  $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
  if (-not (Test-Path $python)) { $python = "python" }
  $prepareCommand = if ($databaseUrl.StartsWith("sqlite")) {
    "& '$python' apps/api/prepare_local_db.py"
  } else {
    "& '$python' -m alembic -c apps/api/alembic.ini upgrade head"
  }
  Start-Process powershell -WorkingDirectory $projectRoot -ArgumentList @(
    "-ExecutionPolicy", "Bypass", "-Command",
    "`$env:DATABASE_URL='$databaseUrl'; $prepareCommand; if (`$LASTEXITCODE -ne 0) { throw 'Database preparation failed.' }; & '$python' -m uvicorn app.main:app --app-dir apps/api --host 127.0.0.1 --port $apiPort"
  ) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot "api.log") -RedirectStandardError (Join-Path $logRoot "api.error.log") | Out-Null
} else {
  Write-Host "API already listening on http://127.0.0.1:$apiPort" -ForegroundColor DarkGreen
}

if (-not (Test-PortListening $webPort)) {
  Start-Process powershell -WorkingDirectory (Join-Path $projectRoot "apps\web") -ArgumentList @(
    "-ExecutionPolicy", "Bypass", "-Command",
    "pnpm dev --hostname 127.0.0.1 --port $webPort"
  ) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot "web.log") -RedirectStandardError (Join-Path $logRoot "web.error.log") | Out-Null
} else {
  Write-Host "Web already listening on http://127.0.0.1:$webPort" -ForegroundColor DarkGreen
}

Start-Sleep -Seconds 5
$web = try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$webPort/" -TimeoutSec 5 } catch { $null }
$api = try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$apiPort/health" -TimeoutSec 5 } catch { $null }
$projects = try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$apiPort/projects" -TimeoutSec 5 } catch { $null }
if ($web.StatusCode -eq 200 -and $api.StatusCode -eq 200 -and $projects.StatusCode -eq 200) {
  Write-Host "Platform ready: http://127.0.0.1:$webPort" -ForegroundColor Green
  Start-Process "http://127.0.0.1:$webPort" | Out-Null
} else {
  Write-Warning "Platform did not become ready. Check logs/api.error.log and logs/web.error.log."
  if ($api) { Write-Host "API status: $($api.StatusCode)" }
  if ($web) { Write-Host "Web status: $($web.StatusCode)" }
}
