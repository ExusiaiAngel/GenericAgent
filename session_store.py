"""Durable, transport-neutral conversation sidecar with FTS5 search."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Mapping


_SECRET_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,}|"
    r"\b(?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*\S{8,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConversationIdentity:
    platform: str
    account: str
    conversation: str
    actor: str = ""

    @classmethod
    def from_message(cls, message: Mapping) -> "ConversationIdentity":
        platform = str(message.get("platform") or "legacy").strip().lower()
        account = str(message.get("account_id") or message.get("account") or "default").strip()
        conversation = str(
            message.get("conversation_id") or message.get("chat_id") or ""
        ).strip()
        actor = str(message.get("actor_id") or message.get("user_id") or "").strip()
        if not actor and not bool(message.get("is_group", False)):
            actor = conversation
        if not conversation:
            raise ValueError("conversation identity requires conversation_id or chat_id")
        for label, value in (("platform", platform), ("account", account), ("conversation", conversation), ("actor", actor)):
            if len(value) > 256 or any(ch in value for ch in "\r\n\0"):
                raise ValueError(f"invalid {label} in conversation identity")
        return cls(platform, account, conversation, actor)

    @property
    def key(self) -> str:
        raw = json.dumps(
            [self.platform, self.account, self.conversation],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "account": self.account,
            "conversation": self.conversation,
            "actor": self.actor,
            "key": self.key,
        }


class SessionStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()
        try:
            os.chmod(self.path, 0o640)
        except OSError:
            pass

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    account TEXT NOT NULL,
                    conversation TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    conversation_key TEXT NOT NULL REFERENCES conversations(conversation_key),
                    generation INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(conversation_key, generation, request_id, role)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                    content, content='messages', content_rowid='id', tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO message_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO message_fts(message_fts, rowid, content)
                    VALUES('delete', old.id, old.content);
                END;
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def _safe_content(content: str, max_chars: int = 8000) -> str:
        text = str(content or "").strip()
        if _SECRET_RE.search(text):
            return "[sensitive content omitted]"
        return text[:max_chars]

    def ensure_conversation(self, identity: ConversationIdentity, generation: int = 0):
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations(
                    conversation_key,platform,account,conversation,actor,generation,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(conversation_key) DO UPDATE SET
                    actor=excluded.actor,
                    generation=MAX(conversations.generation, excluded.generation),
                    updated_at=excluded.updated_at
                """,
                (identity.key, identity.platform, identity.account, identity.conversation,
                 identity.actor, int(generation), now, now),
            )

    def set_generation(self, identity: ConversationIdentity, generation: int):
        self.ensure_conversation(identity, generation)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE conversations SET generation=?,updated_at=? WHERE conversation_key=?",
                (int(generation), time.time(), identity.key),
            )

    def generation(self, identity: ConversationIdentity) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT generation FROM conversations WHERE conversation_key=?",
                (identity.key,),
            ).fetchone()
        return int(row[0]) if row else 0

    def record_exchange(
        self, identity: ConversationIdentity, generation: int, request_id: str,
        user_text: str, assistant_text: str,
    ) -> None:
        self.ensure_conversation(identity, generation)
        now = time.time()
        rows = (
            (identity.key, int(generation), str(request_id), "user", self._safe_content(user_text), now),
            (identity.key, int(generation), str(request_id), "assistant", self._safe_content(assistant_text), now + 0.000001),
        )
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO messages
                (conversation_key,generation,request_id,role,content,created_at)
                VALUES(?,?,?,?,?,?)""",
                rows,
            )

    def recent(self, identity: ConversationIdentity, generation: int, limit: int = 20) -> list[dict]:
        limit = min(max(int(limit), 1), 100)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT role,content FROM messages
                WHERE conversation_key=? AND generation=?
                ORDER BY id DESC LIMIT ?""",
                (identity.key, int(generation), limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def search(self, identity: ConversationIdentity, query: str, limit: int = 5) -> list[dict]:
        limit = min(max(int(limit), 1), 20)
        query = str(query or "").strip()
        if len(query) < 3:
            return []
        expression = '"' + query.replace('"', '""') + '"'
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT m.role, m.content, m.generation, m.created_at
                FROM message_fts f JOIN messages m ON m.id=f.rowid
                WHERE message_fts MATCH ? AND m.conversation_key=?
                ORDER BY bm25(message_fts), m.id DESC LIMIT ?""",
                (expression, identity.key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, *, max_age_days: int = 90, max_messages: int = 50000) -> int:
        cutoff = time.time() - max(1, int(max_age_days)) * 86400
        with self._lock, self._connect() as conn:
            before = conn.total_changes
            conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            conn.execute(
                """DELETE FROM messages WHERE id IN (
                    SELECT id FROM messages ORDER BY id DESC LIMIT -1 OFFSET ?
                )""",
                (max(1, int(max_messages)),),
            )
            return conn.total_changes - before
