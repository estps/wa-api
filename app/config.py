from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("WA_DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_DB = Path(os.getenv("WA_SESSION_DB", str(DATA_DIR / "session.sqlite3")))
STORE_DB = Path(os.getenv("WA_STORE_DB", str(DATA_DIR / "messages.sqlite3")))

# Optional shared secret. When set, clients must send X-API-Key or ?key=...
API_KEY = os.getenv("WA_API_KEY", "").strip()

HOST = os.getenv("WA_API_HOST", "127.0.0.1")
PORT = int(os.getenv("WA_API_PORT", "8080"))
