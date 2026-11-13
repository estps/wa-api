@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [setup] creating virtual environment...
  python -m venv .venv || exit /b 1
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || exit /b 1
)
set PYTHONUTF8=1
if "%WA_API_HOST%"=="" set WA_API_HOST=127.0.0.1
if "%WA_API_PORT%"=="" set WA_API_PORT=8080
echo [wa-api] starting on http://%WA_API_HOST%:%WA_API_PORT%
echo [wa-api] status: http://%WA_API_HOST%:%WA_API_PORT%/status   docs: http://%WA_API_HOST%:%WA_API_PORT%/docs
.venv\Scripts\python -m uvicorn app.main:app --host %WA_API_HOST% --port %WA_API_PORT%
