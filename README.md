# WhatsApp Web API

A local REST API for your **own** WhatsApp account. Read chats, search and
export messages, send text or media, and consume live events from scripts, AI
agents, or automation tools.

It speaks the WhatsApp Web multi-device protocol directly through
[neonize](https://github.com/krypton-byte/neonize) / whatsmeow — no browser, no
Selenium, no web scraping. Messages are archived to SQLite as they arrive.

> **Unofficial.** This is not affiliated with WhatsApp or Meta. Linked-device
> automation can get an account limited or banned. Use your own account and
> keep message volume human.

---

## Features

- **Read** chats, unread counts, message totals, full history, search results,
  and plain-text transcripts.
- **Send** text, replies, images, video, audio/voice notes, and documents.
- **Direct messages** by phone number, JID, or saved chat name; optional
  WhatsApp registration check before sending.
- **Live events** over Server-Sent Events (`message`, `status`, `receipt`,
  `reaction`, `delete`, `edit`, `presence`, `qr`).
- **Media download** for messages whose payload is still stored locally.
- **Groups & contacts** lookup, read receipts, typing presence, history sync.
- **AI-ready:** OpenAPI schema with bearer auth, a one-call `/snapshot` context
  endpoint, and `format=text` transcripts.
- **Runs on Windows and Ubuntu/WSL** with the same code and SQLite archive.

## How it works

```
WhatsApp phone  ⇄  WhatsApp multi-device servers  ⇄  neonize / whatsmeow
                                                           │ events
                                                           ▼
                                             FastAPI (app/main.py)
                                                           │
                                              SQLite archive (app/store.py)
                                                           │
                                              HTTP/JSON + SSE
                                                           ▼
                                               agents / scripts / tools
```

The server links as another WhatsApp Web device. On first link, WhatsApp sends
the recent history it normally gives a new device; everything after that is
archived as events arrive. WhatsApp does not provide a complete server-side
archive, so older messages are fetched from your phone on demand with
`POST /chats/{chat}/sync`.

## Requirements

- Python **3.10+**
- Internet access for the initial link and WhatsApp connection
- Optional: **FFmpeg** (voice notes, some video/sticker conversions)

## Quick start

### Windows

```powershell
git clone https://github.com/estps/wa-api.git
cd wa-api
.\run.cmd        # or: .\run.ps1
```

### Ubuntu / WSL

```bash
sudo apt update
sudo apt install -y python3 python3-venv curl ffmpeg   # ffmpeg is optional
git clone https://github.com/estps/wa-api.git ~/wa-api
cd ~/wa-api
chmod +x run.sh
./run.sh
```

`run.sh` creates a Linux virtual environment on first run. If `python3-venv` is
missing, it bootstraps pip inside the venv without sudo; installing
`python3-venv` is still the cleaner option.

**WSL notes**

- Keep the project in the Linux filesystem (`~/wa-api`), not `/mnt/c/...`, for
  correct SQLite locking and faster startup.
- The Windows `.venv` is not reusable in Linux; `run.sh` creates its own.
- WSL 2 usually forwards `127.0.0.1` to Windows, so `http://localhost:8080`
  works. If it does not, run `WA_API_HOST=0.0.0.0 ./run.sh` and use the address
  from `hostname -I` (allow the port through Windows Firewall).
- Back up `data/` before reinstalling WSL if you want to keep the linked
  session and archive.

### Link your phone (one time)

1. Check the server:

   ```bash
   curl http://127.0.0.1:8080/status
   ```

2. Open `http://127.0.0.1:8080/qr` and scan it with
   **WhatsApp → Settings → Linked devices → Link a device**.
   Append `?key=<key>` if `WA_API_KEY` is set. The QR is also printed in the
   terminal.

   Prefer a pairing code?

   ```bash
   curl -X POST http://127.0.0.1:8080/pair \
     -H "Content-Type: application/json" \
     -d '{"phone": "+15551234567"}'
   ```

3. Wait for `logged_in: true` in `/status`, then start reading and sending.

## Configuration

All settings are environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `WA_API_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose the API. |
| `WA_API_PORT` | `8080` | HTTP port. |
| `WA_API_KEY` | *(empty)* | When set, require `Authorization: Bearer <key>`, `X-API-Key: <key>`, or `?key=<key>`. |
| `WA_DATA_DIR` | `./data` | Directory for the session and message archive. |
| `WA_SESSION_DB` | `data/session.sqlite3` | WhatsApp credentials database. |
| `WA_STORE_DB` | `data/messages.sqlite3` | Message archive database. |

When an API key is set, `/`, `/health`, `/docs`, `/redoc`, and
`/openapi.json` remain public; everything else requires the key.

## API overview

Base URL: `http://127.0.0.1:8080`

`{chat}` accepts a JID (`15551234567@s.whatsapp.net`, `123456789@g.us`), an
international phone number (`+15551234567`), or a saved chat name.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/` | JSON index of the API. |
| GET | `/status` | Connection state, linked account, archive counts. |
| GET | `/qr` | QR image; `?format=text` for the raw payload. |
| POST | `/pair` | Request a phone pairing code. |
| GET | `/chats` | Chat list with unread and message counts. `?search=`. |
| GET | `/chats/{chat}` | One chat plus counts. |
| GET | `/chats/{chat}/count` | Total / sent / received / media counts. |
| GET | `/chats/{chat}/messages` | Read messages (see filters below). |
| GET | `/chats/{chat}/messages/{id}` | One message with reactions. |
| GET | `/chats/{chat}/messages/{id}/media` | Download stored media; `?download=1`. |
| POST | `/chats/{chat}/read` | Send read receipts and clear unread. |
| POST | `/chats/{chat}/typing` | Composing/paused presence. |
| POST | `/chats/{chat}/sync` | Ask the phone for older history. |
| GET | `/messages` | Read/search across all chats. |
| GET | `/messages/count` | Count messages across all chats. |
| GET | `/snapshot` | Recent chats + latest messages in one call (AI context). |
| POST | `/send` | Send text or a reply. |
| POST | `/send/media` | Send an image, video, audio, voice note, or document. |
| GET | `/contacts` | Contacts from WhatsApp and the archive. `?search=`. |
| GET | `/groups` | Known groups; `?refresh=true` fetches live group data. |
| GET | `/groups/{chat}` | Group topic, owner, and participants. |
| GET | `/lookup/{number}` | Check whether a number is on WhatsApp. |
| GET | `/resolve?q=` | Resolve a JID, number, or chat name. |
| GET | `/events` | Server-Sent Events stream. |
| POST | `/logout` | Unlink the device (`{"confirm": true}`), then restart. |

### Message filters

`GET /chats/{chat}/messages` and `GET /messages` accept:

| Parameter | Meaning |
| --- | --- |
| `limit` | Page size (default 50, max 500 unless `all=true`). |
| `offset` | Skip N messages. |
| `all=true` | Return every matching message. |
| `order` | `desc` (default, newest first) or `asc`. |
| `since`, `until` | Unix timestamps (seconds), inclusive. |
| `q` | Search text, sender name, or message type. |
| `from_me` | `true` for sent, `false` for received. |
| `sender` | Filter by sender JID. |
| `format=text` | Plain-text transcript instead of JSON. |

## Examples

Set these once:

```bash
BASE=http://127.0.0.1:8080
KEY=""   # set to your WA_API_KEY if you configured one
AUTH=()
[ -n "$KEY" ] && AUTH=(-H "Authorization: Bearer $KEY")
```

### Read chats and counts

```bash
curl -s "${AUTH[@]}" "$BASE/chats"
curl -s "${AUTH[@]}" "$BASE/chats/+15551234567/count"
curl -s "${AUTH[@]}" "$BASE/chats/+15551234567/messages?limit=50"
curl -s "${AUTH[@]}" "$BASE/chats/+15551234567/messages?all=true&order=asc"
curl -s "${AUTH[@]}" "$BASE/chats/+15551234567/messages?format=text&limit=100"
curl -s "${AUTH[@]}" "$BASE/messages?q=invoice&all=true"
```

### Send

```bash
curl -s -X POST "${AUTH[@]}" "$BASE/send" \
  -H "Content-Type: application/json" \
  -d '{"to":"+15551234567","text":"hello from the API"}'

# Verify the number is on WhatsApp first
curl -s -X POST "${AUTH[@]}" "$BASE/send" \
  -H "Content-Type: application/json" \
  -d '{"to":"+15551234567","text":"hi","verify":true}'

# Media upload
curl -s -X POST "${AUTH[@]}" "$BASE/send/media" \
  -F "to=+15551234567" \
  -F "file=@photo.jpg" \
  -F "caption=look at this"
```

`POST /send` also accepts `reply_to` (a message ID) and `link_preview`.
`/send/media` accepts `kind` (`auto`, `image`, `video`, `audio`, `voice`,
`document`) and `filename`/`mime` overrides via the upload.

### AI / agent context

```bash
# 5 most recent chats, 30 messages each
curl -s "${AUTH[@]}" "$BASE/snapshot?chats=5&messages=30"
```

```json
{
  "generated_at": 1794500000,
  "account": {"jid": "15551234567@s.whatsapp.net", "push_name": "Me"},
  "status": "connected",
  "chats": [
    {
      "jid": "15551234567@s.whatsapp.net",
      "name": "Alice",
      "unread": 2,
      "counts": {"total": 183, "incoming": 91, "from_me": 92, "with_media": 4},
      "messages": [
        {"id": "ABCD", "from_me": false, "sender_name": "Alice", "timestamp": 1794499990, "type": "text", "text": "hey"}
      ]
    }
  ]
}
```

- OpenAPI schema for tool calling: `GET /openapi.json`
- Human-friendly schema viewer: `GET /docs`
- Python standard-library client: `examples/agent_example.py`

```bash
python examples/agent_example.py context
python examples/agent_example.py transcript "+15551234567"
python examples/agent_example.py send +15551234567 "hello from my agent"
```

### Live events

```bash
curl -N "${AUTH[@]}" "$BASE/events?types=message,status"
```

```
event: message
data: {"event":"message","data":{"id":"...","chat_jid":"...","text":"hi",...}}
```

For polling instead, use `GET /messages?since=<unix>&limit=100`.

## Data and history

| File | Contents |
| --- | --- |
| `data/session.sqlite3` | WhatsApp linked-device credentials. |
| `data/messages.sqlite3` | Archived chats, messages, contacts, reactions. |
| `data/messages.sqlite3-wal` | SQLite WAL (safe to ignore). |

- The archive uses SQLite WAL mode and can be read by external tools while the
  server is running.
- Deleting `data/` resets everything and requires scanning a new QR.
- History is limited to what WhatsApp shares with a linked device. Use
  `POST /chats/{chat}/sync` repeatedly to page further back; requests are
  answered asynchronously by your phone.
- Media URLs expire. Download media you need while the message payload is still
  in the archive (`/chats/{chat}/messages/{id}/media`).

## Security

- The default bind address is `127.0.0.1`. Set `WA_API_KEY` before exposing the
  server on a LAN or through a tunnel.
- The API key is accepted as a bearer token, `X-API-Key` header, or `?key=`
  query parameter (useful for `/qr` in a browser).
- CORS is open for local automation tools. Put a reverse proxy with TLS in
  front if the API is reachable beyond your machine.
- This API can send messages as you. Treat the key like a password.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `401 unauthorized` | Set `WA_API_KEY` and send `Authorization: Bearer <key>` (or `?key=`). |
| `state: waiting_for_qr`, no QR | Open `/qr` again; QR codes rotate. Check the terminal. |
| `409 not linked yet` | Scan the QR or use `POST /pair`. |
| No chats / old messages missing | Wait for initial history sync, then `POST /chats/{chat}/sync`. |
| `python3-venv is missing` | `sudo apt install python3-venv`, or let `run.sh` bootstrap pip. |
| WSL server not reachable from Windows | Use `WA_API_HOST=0.0.0.0`, find `hostname -I`, allow the port in Windows Firewall. |
| Voice note or video send fails | Install FFmpeg (`sudo apt install ffmpeg`). |
| Port already in use | `WA_API_PORT=8081 ./run.sh` (or set it in Windows). |

## Project layout

```
wa-api/
├─ app/
│  ├─ main.py          FastAPI routes, auth, OpenAPI, SSE
│  ├─ whatsapp.py      neonize client, event handling, sends/media
│  ├─ store.py         SQLite archive and queries
│  └─ config.py        environment and paths
├─ examples/
│  └─ agent_example.py small stdlib automation client
├─ run.cmd / run.ps1   Windows launchers
├─ run.sh              Ubuntu / WSL launcher
├─ requirements.txt
└─ README.md
```

## Disclaimer

This project is for personal use with your own WhatsApp account. It relies on
an unofficial implementation of the WhatsApp Web protocol. Meta may change the
protocol or restrict accounts that automate it. There is no warranty; use at
your own risk.
