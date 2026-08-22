$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$apiPort = 8000
$webPort = 3000

function Test-PortListening([int] $port) {
  return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

if (-not (Test-PortListening $apiPort)) {
  $python = Join-Path $projectRoot ".venv\Scripts\python.exe"
  if (-not (Test-Path $python)) { $python = "python" }
  Start-Process powershell -WorkingDirectory $projectRoot -ArgumentList @(
    "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
    "& '$python' -m uvicorn app.main:app --app-dir apps/api --host 127.0.0.1 --port $apiPort"
  ) | Out-Null
} else {
  Write-Host "API already listening on http://127.0.0.1:$apiPort" -ForegroundColor DarkGreen
}

if (-not (Test-PortListening $webPort)) {
  Start-Process powershell -WorkingDirectory (Join-Path $projectRoot "apps\web") -ArgumentList @(
    "-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
    "pnpm start --hostname 127.0.0.1 --port $webPort"
  ) | Out-Null
} else {
  Write-Host "Web already listening on http://127.0.0.1:$webPort" -ForegroundColor DarkGreen
}

Start-Sleep -Seconds 2
$web = try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$webPort/" -TimeoutSec 5 } catch { $null }
$api = try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$apiPort/health" -TimeoutSec 5 } catch { $null }
if ($web.StatusCode -eq 200 -and $api.StatusCode -eq 200) {
  Write-Host "Platform ready: http://127.0.0.1:$webPort" -ForegroundColor Green
} else {
  Write-Warning "Platform did not become ready. Check the API/Web terminal windows for startup errors."
  if ($api) { Write-Host "API status: $($api.StatusCode)" }
  if ($web) { Write-Host "Web status: $($web.StatusCode)" }
}
