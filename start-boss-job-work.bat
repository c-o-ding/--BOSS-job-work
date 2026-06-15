@echo off
setlocal

chcp 65001 >nul

set "ROOT=%~dp0"
set "PORT=8010"
set "URL=http://127.0.0.1:%PORT%"

cd /d "%ROOT%"

echo [boss-job-work] Starting local dashboard on %URL%

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port=%PORT%; $root='%ROOT%'; $url='%URL%'; if (Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue) { Write-Host ('Server already running: ' + $url); exit 0 }; & (Join-Path $root 'start-boss-job-work.ps1') -Port $port"

if errorlevel 1 (
  echo.
  echo [boss-job-work] Failed to start the server.
  echo [boss-job-work] If port %PORT% is occupied, close the old process or run stop-boss-job-work.ps1.
  echo.
  pause
  exit /b 1
)

echo [boss-job-work] Waiting for API health check...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$url='%URL%/api/status'; for ($i = 0; $i -lt 90; $i++) { try { $r = Invoke-RestMethod $url -TimeoutSec 3; if ($r) { exit 0 } } catch { }; Start-Sleep -Seconds 1 }; exit 1"

if errorlevel 1 (
  echo [boss-job-work] API health check timed out. Opening dashboard anyway.
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$log=Join-Path '%ROOT%' 'logs\server.err.log'; if (Test-Path $log) { Write-Host '--- recent server.err.log ---'; Get-Content -Tail 30 $log }"
) else (
  echo [boss-job-work] API is ready.
)

start "" "%URL%"

echo.
choice /C YN /N /M "Start BOSS browser and monitor now? [Y/N] "
if errorlevel 2 goto done

echo [boss-job-work] Starting BOSS browser and monitor...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { Invoke-RestMethod -Method Post '%URL%/api/system/start' -TimeoutSec 60 | ConvertTo-Json -Compress; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }"

if errorlevel 1 (
  echo [boss-job-work] Failed to start BOSS browser or monitor. Open the dashboard and click Start Browser manually.
) else (
  echo [boss-job-work] BOSS browser and monitor start request sent.
)

:done
echo.
echo [boss-job-work] Dashboard: %URL%
echo [boss-job-work] Keep this window only if you want to see startup messages.
pause

