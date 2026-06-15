@echo off
setlocal

chcp 65001 >nul

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo [boss-job-work] Stopping local dashboard server...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%stop-boss-job-work.ps1"

echo.
pause

