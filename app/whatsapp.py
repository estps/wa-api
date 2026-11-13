from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import segno
from neonize.client import NewClient
from neonize.events import (
    ChatMarkAsReadEv,
    ChatPresenceEv,
    ConnectedEv,
    DisconnectedEv,
    HistorySyncEv,
    LoggedOutEv,
    MessageDeleteForMeEv,
    MessageEv,
    PairStatusEv,
    QREv,
    ReceiptEv,
)
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, ProtocolMessage
from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import HistorySync
from neonize.utils.enum import (
    ChatPresence,
    ChatPresenceMedia,
    ClientName,
    ClientType,
    ReceiptType,
)
from neonize.utils.jid import build_jid
from neonize.utils.message import extract_text, get_message_type

from .config import SESSION_DB, STORE_DB
from .store import Store

log = logging.getLogger("wa-api")

RECEIPT_STATUS = {
    1: "delivered",
    2: "sent",
    3: "retry",
    4: "read",
    5: "read",
    6: "played",
    7: "played",
    8: "error",
    9: "inactive",
}


def jid_str(jid) -> str:
    """Canonical chat/sender string without device/agent suffixes."""
    if jid is None:
        return ""
    if getattr(jid, "User", ""):
        return f"{jid.User}@{jid.Server}"
    return jid.Server or ""


def normalize_ts(value: Any) -> int:
    """WhatsApp timestamps are seconds, but a few paths expose milliseconds."""
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if ts > 100_000_000_000:  # ~year 5138 in seconds, definitely ms
        ts //= 1000
    return ts


def _has(message: Message, field: str) -> bool:
    try:
        return message.HasField(field)
    except (ValueError, AttributeError):
        return False


def unwrap_message(message: Message) -> Message:
    for _ in range(6):
        if _has(message, "ephemeralMessage") and _has(message.ephemeralMessage, "message"):
            message = message.ephemeralMessage.message
        elif _has(message, "viewOnceMessage") and _has(message.viewOnceMessage, "message"):
            message = message.viewOnceMessage.message
        elif _has(message, "viewOnceMessageV2") and _has(message.viewOnceMessageV2, "message"):
            message = message.viewOnceMessageV2.message
        elif _has(message, "viewOnceMessageV2Extension") and _has(
            message.viewOnceMessageV2Extension, "message"
        ):
            message = message.viewOnceMessageV2Extension.message
        elif _has(message, "documentWithCaptionMessage") and _has(
            message.documentWithCaptionMessage, "message"
        ):
            message = message.documentWithCaptionMessage.message
        elif _has(message, "editedMessage") and _has(message.editedMessage, "message"):
            message = message.editedMessage.message
        else:
            break
    return message


def _context_info(message: Message):
    for field in (
        "extendedTextMessage",
        "imageMessage",
        "videoMessage",
        "audioMessage",
        "documentMessage",
        "stickerMessage",
        "contactMessage",
        "locationMessage",
        "ptvMessage",
    ):
        try:
            sub = getattr(message, field)
        except AttributeError:
            continue
        if sub.ListFields() and _has(sub, "contextInfo"):
            return sub.contextInfo
    return None


def describe_message(message: Message) -> dict[str, Any]:
    """Reduce an E2E protobuf message to the fields the API needs."""
    m = unwrap_message(message)
    text = ""
    try:
        text = extract_text(m) or ""
    except Exception:
        pass

    quoted_id = None
    ctx = _context_info(m)
    if ctx is not None and getattr(ctx, "stanzaId", ""):
        quoted_id = ctx.stanzaId

    kind = "unknown"
    mime = None
    name = None
    has_media = False

    def set_text(value: str) -> None:
        nonlocal text
        if value:
            text = value

    if m.HasField("conversation"):
        kind = "text"
        set_text(m.conversation)
    elif _has(m, "extendedTextMessage"):
        kind = "text"
        set_text(m.extendedTextMessage.text)
    elif _has(m, "imageMessage"):
        kind, mime, has_media = "image", m.imageMessage.mimetype, True
        set_text(m.imageMessage.caption)
    elif _has(m, "videoMessage"):
        vm = m.videoMessage
        kind = "video_note" if vm.gifPlayback and not vm.caption else "video"
        mime, has_media = vm.mimetype, True
        set_text(vm.caption)
    elif _has(m, "ptvMessage"):
        kind, mime, has_media = "video_note", m.ptvMessage.mimetype, True
    elif _has(m, "audioMessage"):
        am = m.audioMessage
        kind = "voice" if am.PTT else "audio"
        mime, has_media = am.mimetype, True
    elif _has(m, "documentMessage"):
        dm = m.documentMessage
        kind, mime, has_media = "document", dm.mimetype, True
        name = dm.fileName or None
        set_text(dm.caption or dm.title)
    elif _has(m, "stickerMessage"):
        kind, mime, has_media = "sticker", m.stickerMessage.mimetype, True
    elif _has(m, "contactMessage"):
        kind = "contact"
        set_text(m.contactMessage.displayName)
    elif _has(m, "contactsArrayMessage"):
        kind = "contact"
        set_text(m.contactsArrayMessage.displayName)
    elif _has(m, "locationMessage"):
        kind = "location"
        set_text(m.locationMessage.name or m.locationMessage.address)
    elif _has(m, "liveLocationMessage"):
        kind = "location"
        set_text(m.liveLocationMessage.comment)
    elif _has(m, "reactionMessage"):
        kind, text = "reaction", m.reactionMessage.text
    elif _has(m, "pollCreationMessage"):
        kind = "poll"
        set_text(m.pollCreationMessage.name)
    elif _has(m, "pollCreationMessageV2"):
        kind = "poll"
        set_text(m.pollCreationMessageV2.name)
    elif _has(m, "pollCreationMessageV3"):
        kind = "poll"
        set_text(m.pollCreationMessageV3.name)
    elif _has(m, "protocolMessage"):
        kind = "protocol"
    elif _has(m, "buttonsResponseMessage"):
        kind = "text"
        set_text(m.buttonsResponseMessage.selectedDisplayText)
    elif _has(m, "listResponseMessage"):
        kind = "text"
        set_text(m.listResponseMessage.title)
    elif _has(m, "templateButtonReplyMessage"):
        kind = "text"
        set_text(m.templateButtonReplyMessage.selectedDisplayText)
    else:
        try:
            mt = get_message_type(m)
            kind = type(mt).__name__.removesuffix("Message")
            kind = re.sub(r"(?<!^)(?=[A-Z])", "_", kind).lower()
        except Exception:
            kind = "unknown"

    return {
        "type": kind,
        "text": text or "",
        "has_media": has_media,
        "media_mime": mime,
        "media_name": name,
        "quoted_id": quoted_id,
    }


class WhatsAppService:
    def __init__(self, store: Store):
        self.store = store
        self.client = NewClient(str(SESSION_DB))
        self.state = "starting"
        self.qr: str | None = None
        self.qr_updated_at: float | None = None
        self.last_error: str | None = None
        self.last_event_at: float | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._group_fetching: set[str] = set()
        self._register_handlers()

    # --------------------------------------------------------------- startup

    def _register_handlers(self) -> None:
        client = self.client
        client.qr(self._on_qr)
        client.event(ConnectedEv)(self._on_connected)
        client.event(DisconnectedEv)(self._on_disconnected)
        client.event(LoggedOutEv)(self._on_logged_out)
        client.event(MessageEv)(self._on_message)
        client.event(HistorySyncEv)(self._on_history)
        client.event(ReceiptEv)(self._on_receipt)
        client.event(ChatMarkAsReadEv)(self._on_chat_mark_read)
        client.event(MessageDeleteForMeEv)(self._on_delete_for_me)
        client.event(QREv)(self._on_qr_event)
        client.event(PairStatusEv)(self._on_pair_status)
        client.event(ChatPresenceEv)(self._on_chat_presence)

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="whatsapp", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stopping:
            try:
                self.state = "connecting"
                self._publish("status", self.status())
                log.info("connecting to WhatsApp...")
                self.client.connect()
            except BaseException as exc:  # noqa: BLE001 - the thread must survive
                self.last_error = str(exc)
                log.exception("WhatsApp connection error")
            if self._stopping:
                break
            self.state = "reconnecting"
            self._publish("status", self.status())
            time.sleep(5)

    def stop(self) -> None:
        self._stopping = True
        try:
            self.client.stop()
        except Exception:
            log.debug("client.stop failed", exc_info=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # -------------------------------------------------------------- statuses

    @property
    def connected(self) -> bool:
        try:
            return bool(self.client.is_connected)
        except Exception:
            return False

    @property
    def logged_in(self) -> bool:
        try:
            return bool(self.client.is_logged_in)
        except Exception:
            return False

    def me(self) -> dict[str, Any] | None:
        device = getattr(self.client, "me", None)
        if device is None or not getattr(device, "JID", None) or not device.JID.User:
            return None
        return {
            "jid": jid_str(device.JID),
            "lid": jid_str(device.LID) if getattr(device, "LID", None) else None,
            "push_name": device.PushName,
            "business_name": device.BussinessName,
            "platform": device.Platform,
        }

    def status(self) -> dict[str, Any]:
        if self.logged_in and self.connected and state in {"connecting", "reconnecting", "starting"}:
            state = "connected"
        else:
            state = self.state
        return {
            "state": state,
            "connected": self.connected,
            "logged_in": self.logged_in,
            "me": self.me(),
            "qr": {
                "available": bool(self.qr),
                "updated_at": self.qr_updated_at,
            },
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
            "store": self.store.stats(),
            "server_time": int(time.time()),
        }

    def _publish(self, event: str, data: Any) -> None:
        self.last_event_at = time.time()
        if self._loop is None or self._loop.is_closed():
            return
        payload = {"event": event, "data": data, "timestamp": self.last_event_at}

        def put_now() -> None:
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

        try:
            self._loop.call_soon_threadsafe(put_now)
        except RuntimeError:
            pass

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    # ----------------------------------------------------------------- events

    def _on_qr(self, client, data: bytes) -> None:
        try:
            code = data.decode("utf-8", "ignore")
        except Exception:
            code = str(data)
        self.qr = code
        self.qr_updated_at = time.time()
        self.state = "waiting_for_qr"
        log.info("QR code ready - open http://%s:%s/qr", os.getenv("WA_API_HOST", "127.0.0.1"), os.getenv("WA_API_PORT", "8080"))
        try:
            segno.make_qr(code).terminal(compact=True, border=1)
        except Exception:
            pass
        self._publish("qr", self.status())

    def _on_qr_event(self, client, event) -> None:
        codes = list(event.Codes)
        if codes:
            self._on_qr(client, codes[-1].encode())

    def _on_connected(self, client, event) -> None:
        self.state = "connected"
        self.qr = None
        self.last_error = None
        log.info("connected as %s", (self.me() or {}).get("jid"))
        self._publish("status", self.status())
        self._publish("connected", self.status())

    def _on_disconnected(self, client, event) -> None:
        self.state = "disconnected"
        self._publish("status", self.status())
        self._publish("disconnected", {"status": self.status()})

    def _on_logged_out(self, client, event) -> None:
        self.state = "logged_out"
        self.qr = None
        log.warning("WhatsApp reported that this device was logged out")
        self._publish("status", self.status())

    def _on_pair_status(self, client, event) -> None:
        if getattr(event, "Status", 0) == 2:  # SUCCESS
            self._publish("status", self.status())
        elif getattr(event, "Error", ""):
            self.last_error = event.Error
            self._publish("status", self.status())

    def _on_chat_presence(self, client, event) -> None:
        self._publish(
            "presence",
            {
                "chat": jid_str(event.MessageSource.Chat),
                "sender": jid_str(event.MessageSource.Sender),
                "state": int(event.State),
            },
        )

    def _on_message(self, client, event) -> None:
        try:
            self._handle_message(event)
        except Exception:
            log.exception("failed to handle message event")

    def _handle_message(self, event) -> None:
        info = event.Info
        source = info.MessageSource
        chat = jid_str(source.Chat)
        if not chat:
            return
        sender = jid_str(source.Sender) or chat
        from_me = bool(source.IsFromMe)
        ts = normalize_ts(info.Timestamp) or int(time.time())
        pushname = info.Pushname or None
        is_group = bool(source.IsGroup) or chat.endswith("@g.us")

        self.store.upsert_chat(
            chat,
            name=self._chat_name(chat, sender, pushname, from_me, is_group),
            is_group=is_group,
        )
        if pushname:
            self.store.upsert_contact(sender, push_name=pushname)

        message: Message = event.Message

        if _has(message, "protocolMessage"):
            self._handle_protocol(chat, message.protocolMessage, ts)
            return

        if _has(message, "reactionMessage"):
            reaction = message.reactionMessage
            target = reaction.key.ID
            self.store.set_reaction(chat, target, sender, reaction.text)
            self._publish(
                "reaction",
                {"chat": chat, "message_id": target, "sender": sender, "emoji": reaction.text, "timestamp": ts},
            )
            return

        if event.IsEdit and info.Edit:
            edited = describe_message(message)
            self.store.update_text(chat, info.Edit, edited["text"])
            self.store.upsert_chat(chat)
            self._publish(
                "edit",
                {"chat": chat, "message_id": info.Edit, "text": edited["text"], "timestamp": ts},
            )
            return

        desc = describe_message(message)
        rec = {
            "id": info.ID,
            "chat_jid": chat,
            "sender_jid": sender,
            "sender_name": pushname or ("me" if from_me else None),
            "from_me": from_me,
            "ts": ts,
            "type": desc["type"],
            "text": desc["text"],
            "has_media": desc["has_media"],
            "media_mime": desc["media_mime"],
            "media_name": desc["media_name"],
            "quoted_id": desc["quoted_id"],
            "status": "sent" if from_me else "received",
            "is_view_once": bool(event.IsViewOnce or event.IsViewOnceV2 or event.IsViewOnceV2Extension),
            "raw": unwrap_message(event.Message).SerializeToString(),
            "is_group": is_group,
        }
        self.store.add_message(rec)
        self._publish("message", self.message_payload(chat, info.ID))

    def _handle_protocol(self, chat: str, protocol: ProtocolMessage, ts: int) -> None:
        if protocol.type == ProtocolMessage.REVOKE:
            message_id = protocol.key.ID
            if message_id:
                self.store.mark_deleted(chat, message_id)
                self._publish(
                    "delete", {"chat": chat, "message_id": message_id, "timestamp": ts}
                )
        elif _has(protocol, "editedMessage"):
            edited = describe_message(protocol.editedMessage)
            if protocol.key.ID:
                self.store.update_text(chat, protocol.key.ID, edited["text"])
                self._publish(
                    "edit",
                    {"chat": chat, "message_id": protocol.key.ID, "text": edited["text"], "timestamp": ts},
                )

    def _chat_name(
        self, chat: str, sender: str, pushname: str | None, from_me: bool, is_group: bool
    ) -> str | None:
        existing = self.store.get_chat(chat)
        if existing and existing.get("name"):
            return None
        if is_group:
            self._fetch_group_name_async(chat)
            return None
        if not from_me and pushname:
            return pushname
        return None

    def _fetch_group_name_async(self, chat: str) -> None:
        if chat in self._group_fetching:
            return
        self._group_fetching.add(chat)

        def work() -> None:
            try:
                info = self.client.get_group_info(parse_jid(chat))
                name = info.GroupName.Name
                if name:
                    self.store.upsert_chat(chat, name=name, is_group=True)
                    self._publish(
                        "chat", {"jid": chat, "name": name, "is_group": True}
                    )
            except Exception:
                log.debug("could not fetch group name for %s", chat, exc_info=True)
            finally:
                self._group_fetching.discard(chat)

        threading.Thread(target=work, name="group-info", daemon=True).start()

    def _on_history(self, client, event) -> None:
        try:
            data: HistorySync = event.Data
            total = 0
            for conv in data.conversations:
                chat = conv.ID
                if not chat:
                    continue
                name = conv.name or conv.displayName or None
                is_group = chat.endswith("@g.us")
                recs: list[dict[str, Any]] = []
                for item in conv.messages:
                    wi = item.message
                    if wi is None or not wi.key.ID or not wi.key.remoteJID:
                        continue
                    msg = wi.message
                    if msg is None or not msg.ListFields():
                        continue
                    desc = describe_message(msg)
                    if desc["type"] == "protocol" and not msg.ListFields():
                        continue
                    sender = wi.key.participant or wi.key.remoteJID
                    if wi.key.fromMe:
                        sender = jid_str(self.client.me.JID if self.client.me else None) or sender
                    recs.append(
                        {
                            "id": wi.key.ID,
                            "chat_jid": chat,
                            "sender_jid": sender,
                            "sender_name": wi.pushName or None,
                            "from_me": bool(wi.key.fromMe),
                            "ts": normalize_ts(wi.messageTimestamp),
                            "type": desc["type"],
                            "text": desc["text"],
                            "has_media": desc["has_media"],
                            "media_mime": desc["media_mime"],
                            "media_name": desc["media_name"],
                            "quoted_id": desc["quoted_id"],
                            "status": "sent" if wi.key.fromMe else "received",
                            "raw": unwrap_message(msg).SerializeToString(),
                            "is_group": is_group,
                        }
                    )
                    if wi.pushName:
                        self.store.upsert_contact(sender, push_name=wi.pushName)
                total += self.store.add_history(recs)
                self.store.upsert_chat(
                    chat,
                    name=name,
                    is_group=is_group,
                    unread=conv.unreadCount,
                    archived=bool(conv.archived),
                    pinned=bool(conv.pinned),
                    muted_until=conv.muteEndTime or None,
                )
            for push in data.pushnames:
                if push.ID and push.pushname:
                    self.store.upsert_contact(push.ID, push_name=push.pushname)
            log.info(
                "history sync type=%s conversations=%s new_messages=%s",
                data.syncType,
                len(data.conversations),
                total,
            )
            self._publish("history", {"new_messages": total, "conversations": len(data.conversations)})
            self._publish("status", self.status())
        except Exception:
            log.exception("failed to ingest history sync")

    def _on_receipt(self, client, event) -> None:
        try:
            chat = jid_str(event.MessageSource.Chat)
            status = RECEIPT_STATUS.get(int(event.Type))
            if chat and status:
                self.store.update_status(chat, list(event.MessageIDs), status)
                self._publish(
                    "receipt",
                    {"chat": chat, "message_ids": list(event.MessageIDs), "status": status},
                )
        except Exception:
            log.debug("receipt handling failed", exc_info=True)

    def _on_chat_mark_read(self, client, event) -> None:
        chat = jid_str(event.JID)
        if chat:
            self.store.clear_unread(chat)
            self._publish("read", {"chat": chat})

    def _on_delete_for_me(self, client, event) -> None:
        chat = jid_str(event.ChatJID)
        if chat and event.MessageID:
            self.store.mark_deleted(chat, event.MessageID)
            self._publish(
                "delete", {"chat": chat, "message_id": event.MessageID, "for_me": True}
            )

    def message_payload(self, chat: str, message_id: str) -> dict[str, Any] | None:
        row = self.store.get_message(chat, message_id)
        if row is None:
            return None
        d = self.store.message_dict(row, include_reactions=True)
        chat_row = self.store.get_chat(chat)
        d["chat_name"] = (chat_row or {}).get("name")
        return d

    # ------------------------------------------------------------------ reads

    def resolve_jid(self, value: str):
        return parse_jid(value)

    def is_on_whatsapp(self, number: str) -> list[dict[str, Any]]:
        responses = self.client.is_on_whatsapp(*normalize_phones(number))
        result = []
        for item in responses:
            result.append(
                {
                    "query": item.Query,
                    "jid": jid_str(item.JID),
                    "exists": bool(item.IsIn),
                    "verified_name": item.VerifiedName.Details.VerifiedName
                    if item.VerifiedName and item.VerifiedName.Details
                    else None,
                }
            )
        return result

    # ----------------------------------------------------------------- writes

    def send_text(
        self,
        to: str,
        text: str,
        *,
        reply_to: str | None = None,
        link_preview: bool = False,
        verify: bool = False,
    ) -> dict[str, Any]:
        self.require_login()
        jid = self.resolve_send_jid(to, verify=verify)
        chat = jid_str(jid)
        quoted = None
        if reply_to:
            raw = self.store.get_raw(chat, reply_to)
            if not raw:
                raise ValueError(f"reply_to message {reply_to!r} was not found in chat {chat}")
            quoted = Message.FromString(raw)
        if quoted is not None:
            response = self.client.reply_message(text, quoted, to=jid, link_preview=link_preview)
        else:
            response = self.client.send_message(jid, text, link_preview=link_preview)
        ts = normalize_ts(response.Timestamp) or int(time.time())
        raw = response.Message.SerializeToString() if response.Message.ListFields() else None
        rec = {
            "id": response.ID,
            "chat_jid": chat,
            "sender_jid": (self.me() or {}).get("jid"),
            "sender_name": (self.me() or {}).get("push_name") or "me",
            "from_me": True,
            "ts": ts,
            "type": "text",
            "text": text,
            "status": "sent",
            "quoted_id": reply_to,
            "raw": raw,
            "is_group": chat.endswith("@g.us"),
        }
        self.store.add_message(rec, count_unread=False)
        payload = self.message_payload(chat, response.ID) or rec
        self._publish("message", payload)
        return {
            "sent": True,
            "id": response.ID,
            "timestamp": ts,
            "to": chat,
            "message": payload,
        }

    def send_media(
        self,
        to: str,
        path: str,
        *,
        kind: str = "auto",
        caption: str | None = None,
        filename: str | None = None,
        mime: str | None = None,
        reply_to: str | None = None,
        voice_note: bool = False,
        verify: bool = False,
    ) -> dict[str, Any]:
        self.require_login()
        jid = self.resolve_send_jid(to, verify=verify)
        chat = jid_str(jid)
        quoted = None
        if reply_to:
            raw = self.store.get_raw(chat, reply_to)
            if not raw:
                raise ValueError(f"reply_to message {reply_to!r} was not found in chat {chat}")
            quoted = Message.FromString(raw)

        if kind == "auto":
            guessed = mime or mimetypes.guess_type(filename or path)[0] or ""
            if guessed.startswith("image/"):
                kind = "image"
            elif guessed.startswith("video/"):
                kind = "video"
            elif guessed.startswith("audio/"):
                kind = "voice" if voice_note else "audio"
            else:
                kind = "document"
        kind = kind.lower()
        if kind == "image":
            response = self.client.send_image(jid, path, caption=caption, quoted=quoted)
        elif kind in {"video", "gif"}:
            response = self.client.send_video(
                jid, path, caption=caption, quoted=quoted, gifplayback=kind == "gif"
            )
        elif kind in {"audio", "voice"}:
            response = self.client.send_audio(
                jid, path, ptt=kind == "voice" or voice_note, quoted=quoted
            )
        else:
            response = self.client.send_document(
                jid,
                path,
                caption=caption,
                filename=filename or Path(path).name,
                mimetype=mime,
                quoted=quoted,
            )
        ts = normalize_ts(response.Timestamp) or int(time.time())
        raw = response.Message.SerializeToString() if response.Message.ListFields() else None
        rec = {
            "id": response.ID,
            "chat_jid": chat,
            "sender_jid": (self.me() or {}).get("jid"),
            "sender_name": (self.me() or {}).get("push_name") or "me",
            "from_me": True,
            "ts": ts,
            "type": kind,
            "text": caption or "",
            "has_media": True,
            "media_mime": mime,
            "media_name": filename,
            "status": "sent",
            "quoted_id": reply_to,
            "raw": raw,
            "is_group": chat.endswith("@g.us"),
        }
        self.store.add_message(rec, count_unread=False)
        payload = self.message_payload(chat, response.ID) or rec
        self._publish("message", payload)
        return {
            "sent": True,
            "id": response.ID,
            "timestamp": ts,
            "to": chat,
            "kind": kind,
            "message": payload,
        }

    def resolve_send_jid(self, to: str, *, verify: bool = False):
        jid = parse_jid(to)
        if verify and jid.Server == "s.whatsapp.net":
            results = self.client.is_on_whatsapp(jid.User)
            if results and results[0].IsIn:
                return results[0].JID
            raise ValueError(f"{to!r} is not registered on WhatsApp")
        return jid

    def send_presence(self, chat: str, state: str = "composing", media: str = "text") -> None:
        self.require_login()
        jid = parse_jid(chat)
        states = {
            "composing": ChatPresence.CHAT_PRESENCE_COMPOSING,
            "paused": ChatPresence.CHAT_PRESENCE_PAUSED,
        }
        medias = {
            "text": ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
            "audio": ChatPresenceMedia.CHAT_PRESENCE_MEDIA_AUDIO,
        }
        if state not in states:
            raise ValueError("state must be 'composing' or 'paused'")
        if media not in medias:
            raise ValueError("media must be 'text' or 'audio'")
        self.client.send_chat_presence(jid, states[state], medias[media])

    def mark_chat_read(self, chat: str, up_to: str | None = None, limit: int = 50) -> dict[str, Any]:
        self.require_login()
        jid = parse_jid(chat)
        rows = self.store.last_incoming(chat, limit=limit)
        if up_to:
            rows = [r for r in rows if r["id"] == up_to or r["ts"] <= self._message_ts(chat, up_to)]
        if not rows:
            self.store.clear_unread(chat)
            return {"marked": 0, "chat": chat}
        sender = parse_jid(rows[0]["sender_jid"] or chat)
        ids = [r["id"] for r in rows]
        self.client.mark_read(
            *ids,
            chat=jid,
            sender=sender,
            receipt=ReceiptType.READ,
            timestamp=int(rows[0]["ts"]),
        )
        self.store.clear_unread(chat)
        self._publish("read", {"chat": chat, "message_ids": ids})
        return {"marked": len(ids), "chat": chat, "message_ids": ids}

    def _message_ts(self, chat: str, message_id: str) -> int:
        row = self.store.get_message(chat, message_id)
        return int(row["ts"]) if row else 0

    def request_history(self, chat: str, count: int = 50) -> dict[str, Any]:
        self.require_login()
        jid = parse_jid(chat)
        oldest = self.store.oldest_message(chat)
        if oldest is None:
            raise ValueError(
                "no local messages yet - wait for the initial history sync or send a message first"
            )
        response = self.client.request_history_sync(
            jid,
            oldest["id"],
            bool(oldest["from_me"]),
            int(oldest["ts"]),
            count=count,
        )
        return {
            "requested": True,
            "chat": chat,
            "anchor": oldest["id"],
            "count": count,
            "response_id": response.ID,
        }

    def download_media(self, chat: str, message_id: str) -> tuple[bytes, str | None, str | None]:
        self.require_login()
        row = self.store.get_message(chat, message_id)
        if row is None:
            raise KeyError(message_id)
        raw = self.store.get_raw(chat, message_id)
        if not raw:
            raise ValueError("this message has no stored payload (it may predate the API)")
        message = Message.FromString(raw)
        data = self.client.download_any(message)
        if not data:
            raise ValueError("WhatsApp did not return any media for this message")
        return data, row["media_mime"], row["media_name"]

    def logout(self) -> None:
        self.client.logout()
        self.state = "logged_out"
        self.qr = None
        self._stopping = True
        try:
            self.client.stop()
        except Exception:
            pass
        self._publish("status", self.status())

    def pair_phone(self, phone: str) -> str:
        self.require_login(allow_waiting=True)
        digits = normalize_phones(phone)[0]
        return self.client.PairPhone(
            digits,
            False,
            client_name=ClientName.WINDOWS,
            client_type=ClientType.CHROME,
        )

    def require_login(self, *, allow_waiting: bool = False) -> None:
        if self.logged_in:
            return
        if allow_waiting and self.state == "waiting_for_qr":
            return
        raise RuntimeError(
            "WhatsApp is not linked yet. Open /qr and scan it with your phone first."
        )

    # --------------------------------------------------------------- groups

    def group_info(self, chat: str) -> dict[str, Any]:
        self.require_login()
        info = self.client.get_group_info(parse_jid(chat))
        return self._group_dict(info)

    def refresh_groups(self) -> list[dict[str, Any]]:
        self.require_login()
        groups = []
        for info in self.client.get_joined_groups():
            item = self._group_dict(info)
            groups.append(item)
            self.store.upsert_chat(
                item["jid"], name=item["name"], is_group=True
            )
        return groups

    def _group_dict(self, info) -> dict[str, Any]:
        participants = []
        for participant in info.Participants:
            participants.append(
                {
                    "jid": jid_str(participant.JID),
                    "lid": jid_str(participant.LID) if getattr(participant, "LID", None) else None,
                    "phone": jid_str(participant.PhoneNumber)
                    if getattr(participant, "PhoneNumber", None)
                    else None,
                    "name": participant.DisplayName,
                    "is_admin": participant.IsAdmin,
                    "is_super_admin": participant.IsSuperAdmin,
                }
            )
        return {
            "jid": jid_str(info.JID),
            "name": info.GroupName.Name,
            "topic": info.GroupTopic.Topic,
            "owner": jid_str(info.OwnerJID),
            "created": normalize_ts(info.GroupCreated),
            "locked": info.GroupLocked.isLocked,
            "announce": info.GroupAnnounce.IsAnnounce,
            "participants": participants,
        }

    def contacts(self, search: str = "", limit: int = 500) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        try:
            for contact in self.client.contact.get_all_contacts():
                jid = jid_str(contact.JID)
                info = contact.Info
                name = info.FullName or info.FirstName or None
                merged[jid] = {
                    "jid": jid,
                    "name": name,
                    "push_name": info.PushName or None,
                    "business_name": info.BusinessName or None,
                    "found": bool(info.Found),
                }
                if jid:
                    self.store.upsert_contact(jid, name=name, push_name=info.PushName or None)
        except Exception:
            log.debug("live contact lookup failed; using local store", exc_info=True)
        for row in self.store.list_contacts(search, limit):
            jid = row["jid"]
            current = merged.get(jid, {"jid": jid})
            current.setdefault("name", row["name"])
            current.setdefault("push_name", row["push_name"])
            merged[jid] = current
        items = list(merged.values())
        if search:
            needle = search.lower()
            items = [
                c
                for c in items
                if needle in (c.get("name") or "").lower()
                or needle in (c.get("push_name") or "").lower()
                or needle in c["jid"]
            ]
        items.sort(key=lambda c: (c.get("name") or c.get("push_name") or c["jid"]).lower())
        return items[:limit]


def parse_jid(value: str):
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty chat identifier")
    if "@" in raw:
        left, server = raw.split("@", 1)
        user = left.split(":", 1)[0]
        if not user or not server:
            raise ValueError(f"invalid JID: {value!r}")
        return build_jid(user, server)
    # Only treat the input as a phone number if it contains no letters/names.
    if not re.fullmatch(r"\+?[\d\s().-]+", raw):
        raise ValueError(f"not a phone number or JID: {value!r}")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        raise ValueError(f"invalid phone number or JID: {value!r}")
    return build_jid(digits)


def normalize_phones(value: str) -> list[str]:
    parts = [part for part in re.split(r"[,;]+", (value or "").strip()) if part.strip()]
    numbers = []
    for part in parts:
        digits = re.sub(r"\D", "", part)
        if digits.startswith("00"):
            digits = digits[2:]
        if digits:
            numbers.append(digits)
    if not numbers:
        raise ValueError("no phone number supplied")
    return numbers
