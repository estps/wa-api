$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[setup] creating virtual environment..."
    python -m venv .venv
    .\.venv\Scripts\python -m pip install --upgrade pip
    .\.venv\Scripts\python -m pip install -r requirements.txt
}

$env:PYTHONUTF8 = "1"
$bindHost = if ($env:WA_API_HOST) { $env:WA_API_HOST } else { "127.0.0.1" }
$bindPort = if ($env:WA_API_PORT) { $env:WA_API_PORT } else { "8080" }
Write-Host "[wa-api] starting on http://${bindHost}:${bindPort}"
Write-Host "[wa-api] status: http://${bindHost}:${bindPort}/status   docs: http://${bindHost}:${bindPort}/docs"
.\.venv\Scripts\python -m uvicorn app.main:app --host $bindHost --port $bindPort
