# WhatsApp Web API

A local REST API for your own WhatsApp account. It speaks the WhatsApp Web
multi-device protocol directly (via [neonize](https://github.com/krypton-byte/neonize) /
whatsmeow), so there is no browser or Selenium automation. Messages are archived
to SQLite as they arrive, and AI agents, scripts, or automation tools can read
and send them over plain HTTP + JSON.

> Unofficial. Automation can get an account limited or banned, so use your own
> account and keep volume human. This is not affiliated with WhatsApp/Meta.

## Quick start

Requires Python 3.10+ (the setup script creates its own virtual environment).

```powershell
cd wa-api
.\run.cmd            # or: .\run.ps1
```

1. Check `http://127.0.0.1:8080/status` (append `?key=<key>` if `WA_API_KEY`
   is set).
2. Open `http://127.0.0.1:8080/qr` (append `?key=<key>` if `WA_API_KEY` is
   set) and scan it with **WhatsApp → Settings → Linked devices → Link a
   device**. Prefer a pairing code? `POST /pair` with
   `{"phone": "+15551234567"}`.
3. Start calling the API. JSON schema: `http://127.0.0.1:8080/openapi.json`
   (Swagger UI at `/docs` is just a viewer for that schema).

The first connection downloads the recent chat history that WhatsApp shares
with a new linked device. Afterwards, new messages are stored as they arrive.

## Ubuntu / WSL

```bash
sudo apt update
sudo apt install -y python3 python3-venv curl ffmpeg   # ffmpeg is optional
cd ~/wa-api
chmod +x run.sh
./run.sh
```

`run.sh` creates a Linux virtual environment on first run. If `python3-venv`
is not installed it falls back to bootstrapping pip inside the venv, but
installing `python3-venv` is the cleaner option.

Notes for WSL:

- Copy the project into the Linux filesystem (for example `~/wa-api`) rather
  than running it from `/mnt/c/...`; SQLite and file permissions behave better
  and startup is faster.
- The Windows venv (`.venv`) is not reusable in Linux. `run.sh` creates its
  own; `.venv/` is gitignored.
- With WSL 2, a server bound to `127.0.0.1` is usually reachable from Windows
  at `http://localhost:8080`. If not, run
  `WA_API_HOST=0.0.0.0 ./run.sh` and use the WSL address from
  `hostname -I` (allow the port through Windows Firewall).
- The message archive and WhatsApp session live under `data/` in the project
  directory. If you later reinstall WSL, back that folder up to keep the link.

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
| --- | --- | --- |
| `WA_API_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose on the LAN. |
| `WA_API_PORT` | `8080` | Port. |
| `WA_API_KEY` | *(empty)* | If set, send `Authorization: Bearer <key>`, `X-API-Key: <key>`, or `?key=<key>`. |
| `WA_DATA_DIR` | `./data` | Session + message database directory. |
| `WA_SESSION_DB` | `data/session.sqlite3` | WhatsApp credentials database. |
| `WA_STORE_DB` | `data/messages.sqlite3` | Message archive. |

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/` | JSON index of the API. |
| GET | `/status` | Connection state, linked account, archive counts. |
| GET | `/qr` | QR image (`?format=text` for the raw payload). |
| POST | `/pair` | Get a phone pairing code. |
| GET | `/chats` | Chat list with previews, unread counts, message counts. `?search=`. |
| GET | `/chats/{chat}` | One chat + counts. |
| GET | `/chats/{chat}/count` | Total / sent / received / media counts. |
| GET | `/chats/{chat}/messages` | Read messages. `?limit=`, `?offset=`, `?all=true`, `?order=asc`, `?since=`, `?until=`, `?q=`, `?from_me=`, `?format=text`. |
| GET | `/chats/{chat}/messages/{id}` | Single message (with reactions). |
| GET | `/chats/{chat}/messages/{id}/media` | Download the stored media. `?download=1`. |
| POST | `/chats/{chat}/read` | Send read receipts, clear unread. |
| POST | `/chats/{chat}/typing` | Composing/paused presence. |
| POST | `/chats/{chat}/sync` | Ask the phone for older history (`{"count": 100}`). |
| GET | `/messages` | Read across all chats (same filters, plus `?chat=`, `?format=text`). |
| GET | `/messages/count` | Count across all chats. |
| GET | `/snapshot` | **Context dump for AI**: recent chats + latest messages in one call. `?chats=10&messages=20&unread=true`. |
| POST | `/send` | Send text: `{"to": "...", "text": "hi"}`. |
| POST | `/send/media` | Multipart: `to`, `file`, `caption`, `kind` (`auto`/`image`/`video`/`audio`/`voice`/`document`). |
| GET | `/contacts` | Contacts from WhatsApp + archive. `?search=`. |
| GET | `/groups` | Known groups. `?refresh=true` fetches live group list. |
| GET | `/groups/{chat}` | Live group info + participants. |
| GET | `/lookup/{number}` | Check whether a number is on WhatsApp. |
| GET | `/resolve?q=` | Resolve a JID, phone number, or chat name. |
| GET | `/events` | Server-Sent Events stream of `message`, `status`, `receipt`, `reaction`, `delete`, `edit`, `presence`, `qr`. |
| POST | `/logout` | Unlink the device (`{"confirm": true}`), then restart. |

`{chat}` accepts a JID (`15551234567@s.whatsapp.net`, `123456789@g.us`), an
international phone number (`+15551234567`), or a saved chat name.

### Reading

```powershell
# last 50 messages of a chat
curl.exe "http://127.0.0.1:8080/chats/+15551234567/messages?limit=50"

# every message ever archived for that chat
curl.exe "http://127.0.0.1:8080/chats/+15551234567/messages?all=true&order=asc"

# plain-text transcript, easy to feed to an LLM
curl.exe "http://127.0.0.1:8080/chats/+15551234567/messages?limit=100&format=text"

# unread count + totals
curl.exe "http://127.0.0.1:8080/chats/+15551234567/count"

# search all chats for a word
curl.exe "http://127.0.0.1:8080/messages?q=invoice&all=true"

# one-call agent context: 5 chats x 30 messages
curl.exe "http://127.0.0.1:8080/snapshot?chats=5&messages=30"
```

### Sending

```powershell
curl.exe -X POST http://127.0.0.1:8080/send ^
  -H "Content-Type: application/json" ^
  -d '{\"to\":\"+15551234567\",\"text\":\"hello from the API\"}'

# direct message with a WhatsApp registration check
curl.exe -X POST http://127.0.0.1:8080/send ^
  -H "Content-Type: application/json" ^
  -d '{\"to\":\"+15551234567\",\"text\":\"hi\",\"verify\":true}'

# media
curl.exe -X POST http://127.0.0.1:8080/send/media ^
  -F "to=+15551234567" -F "file=@C:\path\photo.jpg" -F "caption=look"
```

If `WA_API_KEY` is set, add `-H "Authorization: Bearer yourkey"` to every call
except `/`, `/health`, `/docs`, and `/openapi.json`. For browser-friendly
endpoints (`/qr`, `/status`) you can append `?key=yourkey` instead.

### AI / automation

Point any OpenAPI-aware tool at `http://127.0.0.1:8080/openapi.json`. A minimal
agent loop looks like:

```python
import requests

BASE = "http://127.0.0.1:8080"
HEADERS = {"Authorization": "Bearer yourkey"}  # omit if no key

context = requests.get(f"{BASE}/snapshot", params={"chats": 5, "messages": 30}, headers=HEADERS).json()
for chat in context["chats"]:
    print(chat["name"], chat["messages"][-1]["text"])

requests.post(
    f"{BASE}/send",
    json={"to": "+15551234567", "text": "Reply drafted by my agent"},
    headers=HEADERS,
).raise_for_status()
```

For event-driven automation, subscribe to `GET /events?types=message`. For
polling, `GET /messages?since=<unix>&limit=100` returns only newer messages.

A tiny standard-library client is included:

```powershell
python examples/agent_example.py context
python examples/agent_example.py transcript "+15551234567"
python examples/agent_example.py send +15551234567 "hello from my agent"
```

### Real-time stream

```powershell
curl.exe -N "http://127.0.0.1:8080/events?types=message,status"
```

Each event is JSON:

```
event: message
data: {"event":"message","data":{"id":"...","chat_jid":"...","text":"hi",...}}
```

## How history works

WhatsApp only gives a linked device a limited slice of history. This API stores
whatever arrives plus every new message. To page further back in a specific
chat, call `POST /chats/{chat}/sync` repeatedly: each call asks your phone for
up to `count` messages older than the oldest message currently stored. Results
arrive asynchronously and show up in later reads.

Media can only be downloaded for messages whose payload is still stored
locally. Media URLs on WhatsApp expire, so download what you need.

## Notes

- Session state lives in `data/session.sqlite3`. Delete the `data/` folder to
  start over (you will need to scan a new QR).
- The message archive is `data/messages.sqlite3`, safe to query with any
  SQLite tool while the server is running (WAL mode).
- FFmpeg is optional; it is only needed for some conversions (voice notes,
  video thumbnails, stickers).
- Keep the server bound to `127.0.0.1` unless you also set `WA_API_KEY`.
- Pairing code login (`POST /pair`) only works while the server is waiting for
  a QR code; phone must be online to answer history sync requests.
