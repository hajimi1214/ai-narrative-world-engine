$ErrorActionPreference = "Stop"

$repoRoot = (Get-Location).Path
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("narrative-phase06-smoke-" + [guid]::NewGuid().ToString("N"))
$reportPath = Join-Path $repoRoot "logs/usability-qa/phase-06-startup-smoke-2026-11-24.json"
$ports = @(8000, 3000)
$ownerProcessIds = @()
$report = [ordered]@{
  date = "2026-11-24"
  status = "failed"
  model_provider = "disabled"
  isolated_sqlite = $true
  api_health = $null
  api_projects = $null
  web_home = $null
  service_process_count = 0
  cleanup = "not_started"
  error = $null
}

try {
  $occupiedPorts = @(
    Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty LocalPort -Unique
  )
  if ($occupiedPorts.Count -gt 0) {
    throw "STARTUP_SMOKE_PORTS_ALREADY_IN_USE"
  }

  New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $tempRoot "apps") | Out-Null

  New-Item -ItemType Junction -Path (Join-Path $tempRoot ".venv") -Target (Join-Path $repoRoot ".venv") | Out-Null
  New-Item -ItemType Junction -Path (Join-Path $tempRoot "apps/api") -Target (Join-Path $repoRoot "apps/api") | Out-Null
  New-Item -ItemType Junction -Path (Join-Path $tempRoot "apps/web") -Target (Join-Path $repoRoot "apps/web") | Out-Null
  New-Item -ItemType Junction -Path (Join-Path $tempRoot "scripts") -Target (Join-Path $repoRoot "scripts") | Out-Null
  Copy-Item -LiteralPath (Join-Path $repoRoot "start-platform.ps1") -Destination $tempRoot

  $isolatedDatabase = Join-Path $tempRoot "startup-smoke.sqlite"
  $databaseUrl = [System.IO.Path]::GetFullPath($isolatedDatabase).Replace(
    [System.IO.Path]::DirectorySeparatorChar,
    [char]"/"
  )
  $env:DATABASE_URL = "sqlite:///" + $databaseUrl
  $env:AI_PROVIDER = "disabled"
  $env:AI_API_KEY = ""
  $env:AI_EMBEDDING_PROVIDER = "disabled"
  $env:AI_EMBEDDING_API_KEY = ""
  $env:AI_CREDENTIAL_MASTER_KEY = ""

  & (Join-Path $tempRoot "start-platform.ps1") -NoBrowser

  $health = Invoke-RestMethod "http://127.0.0.1:8000/health" -TimeoutSec 5
  $projectsResponse = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8000/projects" -TimeoutSec 5
  $webResponse = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:3000/" -TimeoutSec 10
  if ($health.status -ne "ok" -or $health.service -ne "ai-narrative-world-engine") {
    throw "STARTUP_SMOKE_API_HEALTH_INVALID"
  }
  if ($projectsResponse.StatusCode -ne 200 -or $webResponse.StatusCode -ne 200) {
    throw "STARTUP_SMOKE_READINESS_INVALID"
  }
  if (-not $webResponse.Content.Contains("LONG-FORM NOVEL DESK")) {
    throw "STARTUP_SMOKE_WEB_MARKER_MISSING"
  }

  $listeners = @(
    Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction Stop
  )
  $ownerProcessIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
  $report.api_health = $health.status
  $report.api_projects = $projectsResponse.StatusCode
  $report.web_home = $webResponse.StatusCode
  $report.service_process_count = $ownerProcessIds.Count
  $report.status = "passed"
} catch {
  $report.error = $_.Exception.Message
} finally {
  foreach ($ownerProcessId in $ownerProcessIds) {
    Stop-Process -Id $ownerProcessId -Force -ErrorAction SilentlyContinue
  }
  foreach ($port in $ports) {
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
      $stillListening = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
      if ($stillListening.Count -eq 0) { break }
      Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
  }

  $tempFullPath = [System.IO.Path]::GetFullPath($tempRoot)
  $tempPrefix = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
  ) + [System.IO.Path]::DirectorySeparatorChar
  if ($tempFullPath.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -LiteralPath $tempFullPath -Recurse -Force -ErrorAction SilentlyContinue
    $report.cleanup = "temporary_workspace_removed"
  } else {
    $report.cleanup = "temporary_workspace_retained_path_check_failed"
  }

  $reportDirectory = Split-Path -Parent $reportPath
  New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
  $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding utf8
}

if ($report.status -ne "passed") {
  throw "STARTUP_SMOKE_FAILED:$($report.error)"
}
