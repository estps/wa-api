from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    is_group INTEGER NOT NULL DEFAULT 0,
    unread INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    muted_until INTEGER NOT NULL DEFAULT 0,
    last_message_ts INTEGER,
    last_message_preview TEXT,
    last_message_from_me INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    sender_jid TEXT,
    sender_name TEXT,
    from_me INTEGER NOT NULL DEFAULT 0,
    ts INTEGER NOT NULL,
    type TEXT,
    text TEXT,
    has_media INTEGER NOT NULL DEFAULT 0,
    media_mime TEXT,
    media_name TEXT,
    quoted_id TEXT,
    edited INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    status TEXT,
    is_view_once INTEGER NOT NULL DEFAULT 0,
    raw BLOB,
    PRIMARY KEY (chat_jid, id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_text ON messages(text);

CREATE TABLE IF NOT EXISTS contacts (
    jid TEXT PRIMARY KEY,
    name TEXT,
    push_name TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS reactions (
    chat_jid TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_jid TEXT NOT NULL,
    emoji TEXT,
    ts INTEGER NOT NULL,
    PRIMARY KEY (chat_jid, message_id, sender_jid)
);
CREATE INDEX IF NOT EXISTS idx_reactions_msg ON reactions(chat_jid, message_id);
"""


def iso(ts: int | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def preview_text(rec: dict[str, Any]) -> str:
    text = (rec.get("text") or "").strip().replace("\n", " ")
    if text:
        return text[:160]
    kind = rec.get("type") or ""
    labels = {
        "image": "[image]",
        "video": "[video]",
        "video_note": "[video note]",
        "audio": "[audio]",
        "voice": "[voice note]",
        "document": "[document]",
        "sticker": "[sticker]",
        "location": "[location]",
        "contact": "[contact]",
        "poll": "[poll]",
        "reaction": "[reaction]",
        "protocol": "[system]",
    }
    return labels.get(kind, f"[{kind}]" if kind else "")


class Store:
    """A small SQLite message archive fed by WhatsApp events."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ chats

    def upsert_chat(
        self,
        jid: str,
        *,
        name: str | None = None,
        is_group: bool = False,
        unread: int | None = None,
        archived: bool | None = None,
        pinned: bool | None = None,
        muted_until: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO chats (jid, name, is_group, unread, archived, pinned, muted_until)
                VALUES (?, ?, ?, COALESCE(?, 0), COALESCE(?, 0), COALESCE(?, 0), COALESCE(?, 0))
                ON CONFLICT(jid) DO UPDATE SET
                    name = COALESCE(excluded.name, chats.name),
                    is_group = CASE WHEN chats.is_group = 1 THEN 1 ELSE excluded.is_group END,
                    unread = COALESCE(?, chats.unread),
                    archived = COALESCE(?, chats.archived),
                    pinned = COALESCE(?, chats.pinned),
                    muted_until = COALESCE(?, chats.muted_until)
                """,
                (
                    jid,
                    name,
                    int(is_group),
                    unread,
                    int(bool(archived)) if archived is not None else None,
                    int(bool(pinned)) if pinned is not None else None,
                    muted_until,
                    unread,
                    int(bool(archived)) if archived is not None else None,
                    int(bool(pinned)) if pinned is not None else None,
                    muted_until,
                ),
            )

    def set_chat_name(self, jid: str, name: str) -> None:
        if not name:
            return
        with self.connect() as conn:
            conn.execute("UPDATE chats SET name = ? WHERE jid = ?", (name, jid))

    def clear_unread(self, chat_jid: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE chats SET unread = 0 WHERE jid = ?", (chat_jid,))

    def _touch_preview(self, conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
        ts = int(rec.get("ts") or 0)
        conn.execute(
            """
            INSERT INTO chats (jid, is_group, last_message_ts, last_message_preview, last_message_from_me)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(jid) DO UPDATE SET
                is_group = CASE WHEN chats.is_group = 1 THEN 1 ELSE excluded.is_group END,
                last_message_ts = CASE
                    WHEN excluded.last_message_ts >= COALESCE(chats.last_message_ts, 0)
                    THEN excluded.last_message_ts ELSE chats.last_message_ts END,
                last_message_preview = CASE
                    WHEN excluded.last_message_ts >= COALESCE(chats.last_message_ts, 0)
                    THEN excluded.last_message_preview ELSE chats.last_message_preview END,
                last_message_from_me = CASE
                    WHEN excluded.last_message_ts >= COALESCE(chats.last_message_ts, 0)
                    THEN excluded.last_message_from_me ELSE chats.last_message_from_me END
            """,
            (
                rec["chat_jid"],
                1 if rec.get("is_group") else 0,
                ts,
                preview_text(rec),
                1 if rec.get("from_me") else 0,
            ),
        )

    # --------------------------------------------------------------- messages

    def add_message(self, rec: dict[str, Any], *, count_unread: bool = True) -> bool:
        ts = int(rec.get("ts") or time.time())
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, chat_jid, sender_jid, sender_name, from_me, ts, type, text,
                     has_media, media_mime, media_name, quoted_id, edited, deleted,
                     status, is_view_once, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["id"],
                    rec["chat_jid"],
                    rec.get("sender_jid"),
                    rec.get("sender_name"),
                    1 if rec.get("from_me") else 0,
                    ts,
                    rec.get("type"),
                    rec.get("text"),
                    1 if rec.get("has_media") else 0,
                    rec.get("media_mime"),
                    rec.get("media_name"),
                    rec.get("quoted_id"),
                    1 if rec.get("edited") else 0,
                    1 if rec.get("deleted") else 0,
                    rec.get("status"),
                    1 if rec.get("is_view_once") else 0,
                    rec.get("raw"),
                ),
            )
            inserted = cur.rowcount > 0
            if inserted:
                self._touch_preview(conn, rec)
                if count_unread and not rec.get("from_me"):
                    conn.execute("UPDATE chats SET unread = unread + 1 WHERE jid = ?", (rec["chat_jid"],))
            return inserted

    def add_history(self, records: Iterable[dict[str, Any]]) -> int:
        records = list(records)
        if not records:
            return 0
        inserted = 0
        newest: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            for rec in records:
                ts = int(rec.get("ts") or 0)
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO messages
                        (id, chat_jid, sender_jid, sender_name, from_me, ts, type, text,
                         has_media, media_mime, media_name, quoted_id, edited, deleted,
                         status, is_view_once, raw)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec["id"],
                        rec["chat_jid"],
                        rec.get("sender_jid"),
                        rec.get("sender_name"),
                        1 if rec.get("from_me") else 0,
                        ts,
                        rec.get("type"),
                        rec.get("text"),
                        1 if rec.get("has_media") else 0,
                        rec.get("media_mime"),
                        rec.get("media_name"),
                        rec.get("quoted_id"),
                        1 if rec.get("edited") else 0,
                        1 if rec.get("deleted") else 0,
                        rec.get("status"),
                        1 if rec.get("is_view_once") else 0,
                        rec.get("raw"),
                    ),
                )
                inserted += max(cur.rowcount, 0)
                prev = newest.get(rec["chat_jid"])
                if prev is None or ts >= int(prev.get("ts") or 0):
                    newest[rec["chat_jid"]] = rec
            for rec in newest.values():
                self._touch_preview(conn, rec)
        return inserted

    def get_message(self, chat_jid: str, message_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM messages WHERE chat_jid = ? AND id = ?", (chat_jid, message_id)
            ).fetchone()

    def get_raw(self, chat_jid: str, message_id: str) -> bytes | None:
        row = self.get_message(chat_jid, message_id)
        return bytes(row["raw"]) if row is not None and row["raw"] is not None else None

    def update_text(self, chat_jid: str, message_id: str, text: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE messages SET text = ?, edited = 1 WHERE chat_jid = ? AND id = ?",
                (text, chat_jid, message_id),
            )

    def mark_deleted(self, chat_jid: str, message_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE messages SET deleted = 1 WHERE chat_jid = ? AND id = ?",
                (chat_jid, message_id),
            )

    def update_status(self, chat_jid: str, message_ids: list[str], status: str) -> None:
        ids = [i for i in message_ids if i]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE messages SET status = ? WHERE chat_jid = ? AND id IN ({placeholders})",
                (status, chat_jid, *ids),
            )

    def oldest_message(self, chat_jid: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT id, from_me, ts FROM messages WHERE chat_jid = ? ORDER BY ts ASC, id ASC LIMIT 1",
                (chat_jid,),
            ).fetchone()

    def last_incoming(self, chat_jid: str, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, sender_jid, ts FROM messages
                WHERE chat_jid = ? AND from_me = 0 AND deleted = 0
                ORDER BY ts DESC, id DESC LIMIT ?
                """,
                (chat_jid, limit),
            ).fetchall()

    # ------------------------------------------------------------ reactions

    def set_reaction(self, chat_jid: str, message_id: str, sender_jid: str, emoji: str) -> None:
        with self.connect() as conn:
            if not emoji:
                conn.execute(
                    "DELETE FROM reactions WHERE chat_jid = ? AND message_id = ? AND sender_jid = ?",
                    (chat_jid, message_id, sender_jid),
                )
                return
            conn.execute(
                """
                INSERT INTO reactions (chat_jid, message_id, sender_jid, emoji, ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_jid, message_id, sender_jid) DO UPDATE SET
                    emoji = excluded.emoji, ts = excluded.ts
                """,
                (chat_jid, message_id, sender_jid, emoji, int(time.time())),
            )

    def reactions_for(self, chat_jid: str, message_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT sender_jid, emoji, ts FROM reactions WHERE chat_jid = ? AND message_id = ? ORDER BY ts",
                (chat_jid, message_id),
            ).fetchall()
        return [
            {"sender": r["sender_jid"], "emoji": r["emoji"], "timestamp": r["ts"]}
            for r in rows
        ]

    # --------------------------------------------------------------- queries

    def list_chats(self, search: str = "", limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        like = f"%{search.strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*, COALESCE(m.cnt, 0) AS message_count
                FROM chats c
                LEFT JOIN (
                    SELECT chat_jid, COUNT(*) AS cnt FROM messages GROUP BY chat_jid
                ) m ON m.chat_jid = c.jid
                WHERE (? = '' OR c.name LIKE ? OR c.jid LIKE ?)
                ORDER BY COALESCE(c.last_message_ts, 0) DESC, c.jid ASC
                LIMIT ? OFFSET ?
                """,
                (search.strip(), like, like, limit, offset),
            ).fetchall()
        return [self.chat_dict(r) for r in rows]

    def count_chats(self, search: str = "") -> int:
        like = f"%{search.strip()}%"
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM chats WHERE (? = '' OR name LIKE ? OR jid LIKE ?)",
                (search.strip(), like, like),
            ).fetchone()
        return int(row[0])

    def get_chat(self, chat_jid: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, COALESCE(m.cnt, 0) AS message_count
                FROM chats c
                LEFT JOIN (
                    SELECT chat_jid, COUNT(*) AS cnt FROM messages GROUP BY chat_jid
                ) m ON m.chat_jid = c.jid
                WHERE c.jid = ?
                """,
                (chat_jid,),
            ).fetchone()
        return self.chat_dict(row) if row else None

    def find_chats(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        like = f"%{query.strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.*, COALESCE(m.cnt, 0) AS message_count
                FROM chats c
                LEFT JOIN (
                    SELECT chat_jid, COUNT(*) AS cnt FROM messages GROUP BY chat_jid
                ) m ON m.chat_jid = c.jid
                WHERE c.name LIKE ? OR c.jid LIKE ?
                ORDER BY COALESCE(c.last_message_ts, 0) DESC
                LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()
        return [self.chat_dict(r) for r in rows]

    def chat_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["is_group"] = bool(d.get("is_group"))
        d["archived"] = bool(d.get("archived"))
        d["pinned"] = bool(d.get("pinned"))
        d["last_message_from_me"] = bool(d.get("last_message_from_me"))
        return d

    def _where(
        self,
        *,
        chat_jid: str | None = None,
        since: int | None = None,
        until: int | None = None,
        text: str | None = None,
        from_me: bool | None = None,
        sender: str | None = None,
        alias: str = "",
    ) -> tuple[str, list[Any]]:
        p = alias
        clauses: list[str] = []
        params: list[Any] = []
        if chat_jid:
            clauses.append(f"{p}chat_jid = ?")
            params.append(chat_jid)
        if since is not None:
            clauses.append(f"{p}ts >= ?")
            params.append(int(since))
        if until is not None:
            clauses.append(f"{p}ts <= ?")
            params.append(int(until))
        if text:
            clauses.append(f"({p}text LIKE ? OR {p}sender_name LIKE ? OR {p}type LIKE ?)")
            like = f"%{text}%"
            params.extend([like, like, like])
        if from_me is not None:
            clauses.append(f"{p}from_me = ?")
            params.append(1 if from_me else 0)
        if sender:
            clauses.append(f"{p}sender_jid = ?")
            params.append(sender)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def list_messages(
        self,
        *,
        chat_jid: str | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "desc",
        since: int | None = None,
        until: int | None = None,
        text: str | None = None,
        from_me: bool | None = None,
        sender: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._where(
            chat_jid=chat_jid,
            since=since,
            until=until,
            text=text,
            from_me=from_me,
            sender=sender,
            alias="m.",
        )
        direction = "ASC" if order.lower() == "asc" else "DESC"
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*, c.name AS chat_name
                FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
                {where}
                ORDER BY m.ts {direction}, m.id {direction}
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [self.message_dict(r) for r in rows]

    def count_messages(
        self,
        *,
        chat_jid: str | None = None,
        since: int | None = None,
        until: int | None = None,
        text: str | None = None,
        from_me: bool | None = None,
        sender: str | None = None,
    ) -> dict[str, int]:
        where, params = self._where(
            chat_jid=chat_jid, since=since, until=until, text=text, from_me=from_me, sender=sender
        )
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(from_me), 0) AS from_me,
                       COALESCE(SUM(CASE WHEN from_me = 0 THEN 1 ELSE 0 END), 0) AS incoming,
                       COALESCE(SUM(has_media), 0) AS with_media
                FROM messages {where}
                """,
                params,
            ).fetchone()
        return {
            "total": int(row["total"]),
            "from_me": int(row["from_me"]),
            "incoming": int(row["incoming"]),
            "with_media": int(row["with_media"]),
        }

    def message_dict(self, row: sqlite3.Row, *, include_reactions: bool = False) -> dict[str, Any]:
        d = dict(row)
        d.pop("raw", None)
        d["from_me"] = bool(d.get("from_me"))
        d["has_media"] = bool(d.get("has_media"))
        d["edited"] = bool(d.get("edited"))
        d["deleted"] = bool(d.get("deleted"))
        d["is_view_once"] = bool(d.get("is_view_once"))
        d["timestamp"] = d.get("ts")
        d["datetime"] = iso(d.get("ts"))
        d["media"] = (
            {"mime": d.get("media_mime"), "name": d.get("media_name")}
            if d.get("has_media")
            else None
        )
        if include_reactions:
            d["reactions"] = self.reactions_for(d["chat_jid"], d["id"])
        return d

    # -------------------------------------------------------------- contacts

    def upsert_contact(self, jid: str, name: str | None = None, push_name: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO contacts (jid, name, push_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(jid) DO UPDATE SET
                    name = COALESCE(excluded.name, contacts.name),
                    push_name = COALESCE(excluded.push_name, contacts.push_name),
                    updated_at = excluded.updated_at
                """,
                (jid, name, push_name, int(time.time())),
            )

    def list_contacts(self, search: str = "", limit: int = 500) -> list[dict[str, Any]]:
        like = f"%{search.strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM contacts
                WHERE (? = '' OR name LIKE ? OR push_name LIKE ? OR jid LIKE ?)
                ORDER BY COALESCE(name, push_name, jid)
                LIMIT ?
                """,
                (search.strip(), like, like, like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            chats = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        return {"chats": int(chats), "messages": int(messages), "contacts": int(contacts)}
