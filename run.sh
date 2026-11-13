#!/usr/bin/env bash
# Linux / WSL launcher for the WhatsApp Web API.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (Ubuntu: sudo apt install python3 python3-venv)" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[setup] creating virtual environment..."
  if ! python3 -m venv .venv; then
    echo "[setup] python3-venv is missing; bootstrapping pip without sudo..."
    rm -rf .venv
    python3 -m venv --without-pip .venv
    if ! command -v curl >/dev/null 2>&1; then
      echo "install curl (or run: sudo apt install python3-venv curl)" >&2
      exit 1
    fi
    tmp="$(mktemp)"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$tmp"
    .venv/bin/python "$tmp"
    rm -f "$tmp"
  fi
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

export PYTHONUTF8=1
HOST="${WA_API_HOST:-127.0.0.1}"
PORT="${WA_API_PORT:-8080}"

echo "[wa-api] starting on http://${HOST}:${PORT}"
echo "[wa-api] status: http://${HOST}:${PORT}/status   docs: http://${HOST}:${PORT}/docs"
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
