$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Users\choda\AppData\Local\Programs\Python\Python312\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Python not found at: $python" -ForegroundColor Red
    Write-Host "Install Python 3.12 or update run_app.ps1 with your python.exe path."
    Read-Host "Press Enter to exit"
    exit 1
}

Set-Location $projectDir

Write-Host "Using Python:" $python -ForegroundColor Cyan
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "Dependency install failed." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit $LASTEXITCODE
}

Write-Host "Starting SS.lv Hybrid Car Watcher..." -ForegroundColor Green
& $python app.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "Application exited with code $LASTEXITCODE" -ForegroundColor Red
    Read-Host "Press Enter to close"
}
