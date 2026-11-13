"""Minimal automation client for the WhatsApp Web API.

Examples:
    python examples/agent_example.py context
    python examples/agent_example.py chats
    python examples/agent_example.py send 15551234567 "hello from my agent"

Set WA_API_BASE (default http://127.0.0.1:8080) and WA_API_KEY if configured.
Uses only the Python standard library.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.getenv("WA_API_BASE", "http://127.0.0.1:8080").rstrip("/")
KEY = os.getenv("WA_API_KEY", "").strip()


def call(method: str, path: str, payload: dict | None = None, *, raw: bool = False):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if KEY:
        request.add_header("Authorization", f"Bearer {KEY}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return body.decode() if raw else json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


def status() -> dict:
    return call("GET", "/status")


def chats(search: str = "", limit: int = 50) -> list[dict]:
    query = urllib.parse.urlencode({"search": search, "limit": limit})
    return call("GET", f"/chats?{query}")["chats"]


def context(chats_limit: int = 5, messages: int = 30) -> dict:
    """Compact account + chat + message context, ready to hand to an LLM."""
    query = urllib.parse.urlencode({"chats": chats_limit, "messages": messages})
    return call("GET", f"/snapshot?{query}")


def transcript(chat: str, limit: int = 100) -> str:
    query = urllib.parse.urlencode({"limit": limit, "format": "text", "order": "asc"})
    return call("GET", f"/chats/{urllib.parse.quote(chat, safe='')}/messages?{query}", raw=True)


def send_text(to: str, text: str) -> dict:
    return call("POST", "/send", {"to": to, "text": text})


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    if command == "status":
        print(json.dumps(status(), indent=2))
    elif command == "chats":
        print(json.dumps(chats(argv[2] if len(argv) > 2 else ""), indent=2))
    elif command == "context":
        print(json.dumps(context(), indent=2, ensure_ascii=False))
    elif command == "transcript":
        if len(argv) < 3:
            return _usage("transcript needs a chat (JID, phone number, or name)")
        print(transcript(argv[2]))
    elif command == "send":
        if len(argv) < 4:
            return _usage("send needs a recipient and text")
        print(json.dumps(send_text(argv[2], " ".join(argv[3:])), indent=2, ensure_ascii=False))
    else:
        return _usage(f"unknown command {command!r}")
    return 0


def _usage(error: str | None = None) -> int:
    if error:
        print(f"error: {error}\n", file=sys.stderr)
    print(__doc__)
    return 2 if error else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
