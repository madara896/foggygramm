$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
  Write-Host "Creating virtual environment..." -ForegroundColor Cyan
  python -m venv .venv
  & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
  & ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
}

Write-Host "Starting FoggyGramm on http://127.0.0.1:8765" -ForegroundColor Green
& ".\.venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8765
