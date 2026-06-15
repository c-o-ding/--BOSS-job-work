param(
    [int]$Port = 8010
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root ".boss_job_work_server.pid"

function Add-DescendantProcessIds {
    param(
        [int]$ParentId,
        [System.Collections.Generic.HashSet[int]]$Ids
    )
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        if ($Ids.Add([int]$child.ProcessId)) {
            Add-DescendantProcessIds -ParentId ([int]$child.ProcessId) -Ids $Ids
        }
    }
}

if (-not (Test-Path $PidFile)) {
    Write-Output "No PID file found. Nothing to stop."
    exit 0
}

$pidText = (Get-Content -Path $PidFile -Raw).Trim()
if (-not ($pidText -match '^\d+$')) {
    Remove-Item -Path $PidFile -Force
    throw "Invalid PID file content."
}

$proc = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
if (-not $proc) {
    Remove-Item -Path $PidFile -Force
    Write-Output "Recorded process is not running."
} else {
    $idsToStop = [System.Collections.Generic.HashSet[int]]::new()
    [void]$idsToStop.Add([int]$proc.Id)
    Add-DescendantProcessIds -ParentId ([int]$proc.Id) -Ids $idsToStop

    $portOwners = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($owner in $portOwners) {
        $ownerProc = Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue
        if ($ownerProc -and $ownerProc.CommandLine -like "*boss_app.py*") {
            [void]$idsToStop.Add([int]$owner)
        }
    }

    foreach ($id in ($idsToStop | Sort-Object -Descending)) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    Write-Output "boss-job-work server stopped. PID(s): $($idsToStop -join ', ')"
}

Remove-Item -Path $PidFile -Force

