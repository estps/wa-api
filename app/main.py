from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import tempfile
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import segno
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from neonize.exc import NeonizeError
from pydantic import BaseModel, Field

from .config import API_KEY, DATA_DIR, STORE_DB
from .store import Store
from .whatsapp import WhatsAppService, jid_str, parse_jid

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logging.getLogger("neonize").setLevel(logging.WARNING)
log = logging.getLogger("wa-api")

store = Store(STORE_DB)
wa = WhatsAppService(store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    wa.attach_loop(asyncio.get_running_loop())
    wa.start()
    try:
        yield
    finally:
        wa.stop()


app = FastAPI(
    title="WhatsApp Web API",
    version="1.0.0",
    description=(
        "Local REST API over WhatsApp Web (multi-device). "
        "Scan the QR at /qr once, then read chats, messages, media, and send messages. "
        "Unofficial - use only with your own account."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def custom_openapi():
    """Declare the optional API key so OpenAPI tool callers know how to authenticate."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    components["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": "Value of WA_API_KEY",
        },
        "apiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Alternative to the bearer token",
        },
    }
    if API_KEY:
        schema["security"] = [{"bearerAuth": []}, {"apiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi

PUBLIC_PATHS = {
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
}


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    if not API_KEY or request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)
    supplied = (
        request.headers.get("x-api-key")
        or request.query_params.get("key")
        or ""
    )
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if supplied != API_KEY:
        return JSONResponse(
            {"detail": "unauthorized: use Authorization: Bearer <key> or X-API-Key"},
            status_code=401,
        )
    return await call_next(request)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(KeyError)
async def key_error_handler(request: Request, exc: KeyError):
    return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)


@app.exception_handler(NeonizeError)
async def neonize_error_handler(request: Request, exc: NeonizeError):
    return JSONResponse({"detail": f"WhatsApp error: {exc}"}, status_code=502)


# --------------------------------------------------------------------- models


class SendRequest(BaseModel):
    to: str = Field(..., description="Phone number, JID, or saved chat name")
    text: str = Field(..., min_length=1)
    reply_to: str | None = None
    link_preview: bool = False
    verify: bool = Field(False, description="Check the number with WhatsApp before sending")


class ReadRequest(BaseModel):
    up_to: str | None = Field(None, description="Only mark messages at or before this message ID")
    limit: int = 50


class TypingRequest(BaseModel):
    state: str = "composing"
    media: str = "text"


class SyncRequest(BaseModel):
    count: int = Field(50, ge=1, le=500)


class PairRequest(BaseModel):
    phone: str


class LogoutRequest(BaseModel):
    confirm: bool = False


# --------------------------------------------------------------------- helpers


def resolve_chat(chat: str) -> str:
    """Accept a JID, an international phone number, or a saved chat name."""
    try:
        return jid_str(parse_jid(chat))
    except ValueError:
        pass
    matches = store.find_chats(chat)
    if len(matches) == 1:
        return matches[0]["jid"]
    if not matches:
        raise HTTPException(404, f"no chat matches {chat!r}")
    raise HTTPException(
        400,
        {
            "detail": f"{chat!r} is ambiguous",
            "candidates": [
                {"jid": m["jid"], "name": m.get("name"), "is_group": m["is_group"]}
                for m in matches
            ],
        },
    )


def chat_or_basic(chat: str) -> dict[str, Any]:
    found = store.get_chat(chat)
    if found:
        return found
    return {
        "jid": chat,
        "name": None,
        "is_group": chat.endswith("@g.us"),
        "unread": 0,
        "archived": False,
        "pinned": False,
        "muted_until": 0,
        "last_message_ts": None,
        "last_message_preview": None,
        "last_message_from_me": False,
        "message_count": 0,
    }


def page_limit(limit: int, fetch_all: bool, maximum: int = 500) -> int:
    if fetch_all:
        return 1_000_000
    if limit <= 0:
        raise HTTPException(400, "limit must be positive (or use all=true)")
    return min(limit, maximum)


def render_transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in sorted(messages, key=lambda m: (m.get("ts") or 0, m.get("id") or "")):
        stamp = datetime.fromtimestamp(int(message.get("ts") or 0), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        if message.get("from_me"):
            who = "Me"
        else:
            who = message.get("sender_name") or message.get("sender_jid") or "Them"
        body = message.get("text") or f"[{message.get('type') or 'message'}]"
        if message.get("deleted"):
            body = "[deleted]"
        lines.append(f"[{stamp}] {who}: {body}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- routes


@app.get("/", include_in_schema=False)
def api_index():
    return {
        "name": "WhatsApp Web API",
        "description": (
            "Local REST API over your own WhatsApp account. "
            "JSON in, JSON out; OpenAPI schema available for tool calling."
        ),
        "auth_required": bool(API_KEY),
        "auth": "Authorization: Bearer <WA_API_KEY> (or X-API-Key)",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": {
            "status": "GET /status",
            "qr": "GET /qr",
            "pair_phone": "POST /pair",
            "chats": "GET /chats",
            "messages": "GET /chats/{chat}/messages (limit/offset/all/since/until/q/format=text)",
            "count": "GET /chats/{chat}/count",
            "snapshot": "GET /snapshot?chats=10&messages=20",
            "send": "POST /send",
            "send_media": "POST /send/media",
            "events": "GET /events (SSE)",
            "resolve": "GET /resolve?q=",
        },
    }


@app.get("/health", tags=["system"])
def health():
    return {"ok": True}


@app.get("/status", tags=["system"])
def status():
    return wa.status()


@app.get("/qr", tags=["system"])
def qr(format: str = "png"):
    if not wa.qr:
        if wa.logged_in:
            raise HTTPException(404, "already linked to WhatsApp; no QR code needed")
        raise HTTPException(404, "no QR code yet - the WhatsApp connection is still starting")
    if format in {"text", "json"}:
        return {"qr": wa.qr, "updated_at": wa.qr_updated_at, "status": wa.status()}
    buffer = io.BytesIO()
    segno.make_qr(wa.qr).save(buffer, kind="png", scale=8, border=2)
    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/pair", tags=["system"])
def pair_phone(body: PairRequest):
    code = wa.pair_phone(body.phone)
    return {"pair_code": code, "phone": body.phone}


@app.post("/logout", tags=["system"])
def logout(body: LogoutRequest):
    if not body.confirm:
        raise HTTPException(400, "send {\"confirm\": true} to unlink this device")
    wa.logout()
    return {"ok": True, "detail": "device unlinked - restart the server to pair again"}


@app.get("/chats", tags=["chats"])
def list_chats(
    search: str = "",
    limit: int = 100,
    offset: int = 0,
):
    return {
        "chats": store.list_chats(search=search, limit=min(max(limit, 1), 1000), offset=offset),
        "total": store.count_chats(search=search),
    }


@app.get("/chats/{chat}", tags=["chats"])
def get_chat(chat: str):
    jid = resolve_chat(chat)
    data = chat_or_basic(jid)
    data["counts"] = store.count_messages(chat_jid=jid)
    return data


@app.get("/chats/{chat}/count", tags=["chats"])
def count_chat(chat: str):
    jid = resolve_chat(chat)
    counts = store.count_messages(chat_jid=jid)
    row = store.get_chat(jid)
    return {
        "chat": jid,
        "name": row.get("name") if row else None,
        "unread": row.get("unread") if row else 0,
        **counts,
    }


@app.get("/chats/{chat}/messages", tags=["messages"])
def list_chat_messages(
    chat: str,
    limit: int = 50,
    offset: int = 0,
    order: str = "desc",
    since: int | None = None,
    until: int | None = None,
    q: str | None = None,
    from_me: bool | None = None,
    sender: str | None = None,
    all: bool = Query(False, description="Return every matching message (ignores limit)"),
    format: str = Query("json", pattern="^(json|text)$", description="json or a plain-text transcript"),
):
    jid = resolve_chat(chat)
    if all:
        offset = 0
    limit = page_limit(limit, all)
    messages = store.list_messages(
        chat_jid=jid,
        limit=limit,
        offset=offset,
        order=order,
        since=since,
        until=until,
        text=q,
        from_me=from_me,
        sender=sender,
    )
    if format == "text":
        return PlainTextResponse(render_transcript(messages) + "\n")
    counts = store.count_messages(
        chat_jid=jid, since=since, until=until, text=q, from_me=from_me, sender=sender
    )
    return {
        "chat": chat_or_basic(jid),
        "count": counts,
        "returned": len(messages),
        "messages": messages,
    }


@app.get("/messages", tags=["messages"])
def list_all_messages(
    chat: str | None = None,
    limit: int = 50,
    offset: int = 0,
    order: str = "desc",
    since: int | None = None,
    until: int | None = None,
    q: str | None = None,
    from_me: bool | None = None,
    sender: str | None = None,
    all: bool = Query(False, description="Return every matching message (ignores limit)"),
    format: str = Query("json", pattern="^(json|text)$", description="json or a plain-text transcript"),
):
    jid = resolve_chat(chat) if chat else None
    if all:
        offset = 0
    limit = page_limit(limit, all)
    messages = store.list_messages(
        chat_jid=jid,
        limit=limit,
        offset=offset,
        order=order,
        since=since,
        until=until,
        text=q,
        from_me=from_me,
        sender=sender,
    )
    if format == "text":
        return PlainTextResponse(render_transcript(messages) + "\n")
    counts = store.count_messages(
        chat_jid=jid, since=since, until=until, text=q, from_me=from_me, sender=sender
    )
    return {"count": counts, "returned": len(messages), "messages": messages}


@app.get("/messages/count", tags=["messages"])
def count_all_messages(
    chat: str | None = None,
    since: int | None = None,
    until: int | None = None,
    q: str | None = None,
    from_me: bool | None = None,
    sender: str | None = None,
):
    jid = resolve_chat(chat) if chat else None
    return store.count_messages(
        chat_jid=jid, since=since, until=until, text=q, from_me=from_me, sender=sender
    )


@app.get("/snapshot", tags=["agent"])
def snapshot(
    chats: int = Query(10, ge=1, le=50, description="How many recent chats to include"),
    messages: int = Query(20, ge=0, le=200, description="Messages per chat"),
    unread: bool = Query(False, description="Only chats with unread messages"),
    search: str = "",
):
    """One-call context dump made for AI agents: recent chats with their latest messages."""
    chat_rows = store.list_chats(search=search, limit=1000)
    if unread:
        chat_rows = [row for row in chat_rows if row.get("unread")]
    chat_rows = chat_rows[:chats]
    result = []
    for row in chat_rows:
        history = store.list_messages(chat_jid=row["jid"], limit=messages, order="desc")
        item = dict(row)
        item["messages"] = list(reversed(history))
        item["counts"] = store.count_messages(chat_jid=row["jid"])
        result.append(item)
    status_info = wa.status()
    return {
        "generated_at": int(time.time()),
        "account": status_info.get("me"),
        "status": status_info.get("state"),
        "chats": result,
    }


@app.get("/chats/{chat}/messages/{message_id}", tags=["messages"])
def get_message(chat: str, message_id: str):
    jid = resolve_chat(chat)
    row = store.get_message(jid, message_id)
    if row is None:
        raise HTTPException(404, "message not found")
    data = store.message_dict(row, include_reactions=True)
    data["chat_name"] = (store.get_chat(jid) or {}).get("name")
    data["has_stored_payload"] = store.get_raw(jid, message_id) is not None
    return data


@app.get("/chats/{chat}/messages/{message_id}/media", tags=["messages"])
def get_media(chat: str, message_id: str, download: bool = False):
    jid = resolve_chat(chat)
    data, mime, name = wa.download_media(jid, message_id)
    safe_name = urllib.parse.quote(name or f"{message_id}.bin")
    disposition = "attachment" if download else "inline"
    return Response(
        content=data,
        media_type=mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "Content-Length": str(len(data)),
        },
    )


@app.post("/chats/{chat}/read", tags=["messages"])
def mark_read(chat: str, body: ReadRequest):
    jid = resolve_chat(chat)
    return wa.mark_chat_read(jid, up_to=body.up_to, limit=body.limit)


@app.post("/chats/{chat}/typing", tags=["messages"])
def typing(chat: str, body: TypingRequest):
    wa.send_presence(resolve_chat(chat), state=body.state, media=body.media)
    return {"ok": True}


@app.post("/chats/{chat}/sync", tags=["chats"])
def sync_history(chat: str, body: SyncRequest):
    jid = resolve_chat(chat)
    return wa.request_history(jid, count=body.count)


@app.post("/send", tags=["send"])
def send(body: SendRequest):
    jid = resolve_chat(body.to)
    return wa.send_text(
        jid,
        body.text,
        reply_to=body.reply_to,
        link_preview=body.link_preview,
        verify=body.verify,
    )


@app.post("/send/media", tags=["send"])
async def send_media(
    to: str = Form(...),
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    kind: str = Form("auto"),
    reply_to: str | None = Form(None),
    voice_note: bool = Form(False),
    verify: bool = Form(False),
):
    jid = resolve_chat(to)
    suffix = Path(file.filename or "").suffix
    temp_dir = DATA_DIR / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=temp_dir)
    try:
        payload = await file.read()
        if not payload:
            raise HTTPException(400, "uploaded file is empty")
        handle.write(payload)
        handle.close()
        return wa.send_media(
            jid,
            handle.name,
            kind=kind,
            caption=caption,
            filename=file.filename,
            mime=file.content_type,
            reply_to=reply_to,
            voice_note=voice_note,
            verify=verify,
        )
    finally:
        try:
            handle.close()
        except Exception:
            pass
        try:
            Path(handle.name).unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/contacts", tags=["contacts"])
def contacts(search: str = "", limit: int = 500):
    return {"contacts": wa.contacts(search=search, limit=min(max(limit, 1), 2000))}


@app.get("/groups", tags=["chats"])
def groups(refresh: bool = False):
    if refresh:
        return {"groups": wa.refresh_groups()}
    chats = [c for c in store.list_chats(limit=10_000) if c["is_group"]]
    return {"groups": chats, "total": len(chats)}


@app.get("/groups/{chat}", tags=["chats"])
def group_info(chat: str):
    jid = resolve_chat(chat)
    return wa.group_info(jid)


@app.get("/lookup/{number}", tags=["contacts"])
def lookup(number: str):
    return {"results": wa.is_on_whatsapp(number)}


@app.get("/resolve", tags=["contacts"])
def resolve(q: str):
    try:
        jid = jid_str(parse_jid(q))
        chat = store.get_chat(jid)
        return {
            "input": q,
            "jid": jid,
            "name": chat.get("name") if chat else None,
            "is_group": jid.endswith("@g.us"),
            "message_count": chat.get("message_count", 0) if chat else 0,
        }
    except ValueError:
        pass
    matches = store.find_chats(q)
    return {
        "input": q,
        "matches": [
            {"jid": m["jid"], "name": m.get("name"), "is_group": m["is_group"]}
            for m in matches
        ],
    }


@app.get("/events", tags=["system"])
async def events(request: Request, types: str = ""):
    allowed = {t.strip() for t in types.split(",") if t.strip()}
    queue = wa.subscribe()

    async def stream():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if allowed and item["event"] not in allowed:
                    continue
                payload = json.dumps(item, ensure_ascii=False, default=str)
                yield f"event: {item['event']}\ndata: {payload}\n\n"
        finally:
            wa.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def run() -> None:
    import uvicorn

    from .config import HOST, PORT

    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    run()
