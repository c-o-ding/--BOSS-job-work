param(
    [int]$Port = 8010
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile = Join-Path $Root ".boss_job_work_server.pid"
$LogDir = Join-Path $Root "logs"
$OutLog = Join-Path $LogDir "server.out.log"
$ErrLog = Join-Path $LogDir "server.err.log"

if (-not (Test-Path $Python)) {
    throw "Virtual environment not found. Expected: $Python"
}

$existing = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "Port $Port is already in use."
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$proc = Start-Process `
    -FilePath $Python `
    -ArgumentList @("boss_app.py", "--host", "127.0.0.1", "--port", "$Port") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru

$proc.Id | Set-Content -Path $PidFile -Encoding ASCII
Write-Output "boss-job-work server started: http://127.0.0.1:$Port"
Write-Output "PID: $($proc.Id)"
Write-Output "Logs: $OutLog ; $ErrLog"

