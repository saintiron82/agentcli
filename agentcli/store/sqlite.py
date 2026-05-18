"""SQLite-backed session metadata and usage store.

Despite the generic `ConversationStore` interface, this is not intended to be a
chat transcript database for session CLI providers. Claude/Codex/Copilot keep
turn history in their own session files; SQLite persists only the app-side
handles needed to resume them (`conversation_id`, `alias`,
`session_id:<provider>`), instruction hashes, and token/latency usage logs.

The `messages` table remains for non-session providers and backward-compatible
store semantics. Session-capable providers should leave it empty.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from threading import RLock
from .base import ConversationStore
from ..types import Conversation, Message


class SQLiteStore(ConversationStore):
    def __init__(self, db_path: str = ":memory:"):
        self._lock = RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=WAL;")
            except sqlite3.OperationalError:
                pass
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT DEFAULT '',
                alias TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_conv_owner ON conversations(owner);
            -- (owner, alias) 유일성 — alias가 비어있지 않을 때만 유일
            CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_owner_alias
                ON conversations(owner, alias) WHERE alias != '';

            -- Only non-session providers should write prompt/response content.
            -- Session providers store their native CLI session id in
            -- conversations.metadata instead and leave this table empty.
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                latency_ms INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);

            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                agent TEXT DEFAULT '',
                alias TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_usage_conv ON usage_log(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_usage_alias ON usage_log(alias);
            CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage_log(provider);
        """)
        self._conn.commit()
        # Migrations — 구 스키마 호환
        for alter in (
            "ALTER TABLE messages ADD COLUMN agent TEXT DEFAULT ''",
            "ALTER TABLE conversations ADD COLUMN alias TEXT DEFAULT ''",
            "ALTER TABLE usage_log ADD COLUMN cached_tokens INTEGER DEFAULT 0",
            "ALTER TABLE usage_log ADD COLUMN alias TEXT DEFAULT ''",
        ):
            try:
                self._conn.execute(alter)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    def _row_to_conv(self, row) -> Conversation:
        alias = ""
        try:
            alias = row["alias"] if "alias" in row.keys() else ""
        except (IndexError, KeyError):
            alias = ""
        return Conversation(
            id=row["id"], owner=row["owner"], provider=row["provider"],
            model=row["model"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]),
            alias=alias or "")

    def create(self, owner: str, provider: str, model: str = "",
               *, conversation_id: str = "",
               alias: str = "") -> Conversation:
        # alias 중복 검사 — 이미 있으면 그 conversation 반환
        if alias:
            existing = self.find_by_alias(owner, alias)
            if existing is not None:
                return existing

        conv_id = conversation_id or str(uuid.uuid4())
        existing = self.get(conv_id)
        if existing is not None:
            if alias and not existing.alias:
                self.set_alias(conv_id, alias)
                existing = self.get(conv_id)
            return existing

        now = datetime.now()
        self._conn.execute(
            """INSERT INTO conversations (id, owner, provider, model, alias,
               created_at, updated_at) VALUES (?,?,?,?,?,?,?)""",
            (conv_id, owner, provider, model, alias or "",
             now.isoformat(), now.isoformat()))
        self._conn.commit()
        return Conversation(id=conv_id, owner=owner, provider=provider,
                            model=model, created_at=now, updated_at=now,
                            alias=alias or "")

    def get(self, conversation_id: str) -> Conversation | None:
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            return None
        return self._row_to_conv(row)

    def find_by_alias(self, owner: str, alias: str) -> Conversation | None:
        if not alias:
            return None
        row = self._conn.execute(
            "SELECT * FROM conversations WHERE owner=? AND alias=?",
            (owner, alias)).fetchone()
        if not row:
            return None
        return self._row_to_conv(row)

    def add_message(self, conversation_id: str, message: Message) -> None:
        meta = message.metadata
        self._conn.execute(
            """INSERT INTO messages (conversation_id, role, content, timestamp,
               prompt_tokens, completion_tokens, total_tokens, provider, model,
               latency_ms, metadata, agent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (conversation_id, message.role, message.content,
             message.timestamp.isoformat(),
             meta.get("prompt_tokens", 0), meta.get("completion_tokens", 0),
             meta.get("total_tokens", 0), meta.get("provider", ""),
             meta.get("model", ""), meta.get("latency_ms", 0),
             json.dumps(meta, ensure_ascii=False), message.agent))
        self._conn.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (datetime.now().isoformat(), conversation_id))
        self._conn.commit()

    def get_messages(self, conversation_id: str, limit: int = 0,
                     agent: str = "") -> list[Message]:
        where = "WHERE conversation_id=?"
        params: list = [conversation_id]
        if agent:
            where += " AND agent=?"
            params.append(agent)
        if limit > 0:
            rows = self._conn.execute(
                f"""SELECT * FROM (
                    SELECT * FROM messages {where}
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC""",
                params + [limit]).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT * FROM messages {where} ORDER BY id ASC",
                params).fetchall()
        return [Message(
            role=r["role"], content=r["content"],
            timestamp=datetime.fromisoformat(r["timestamp"]),
            metadata=json.loads(r["metadata"]),
            agent=r["agent"] if "agent" in r.keys() else "") for r in rows]

    def delete(self, conversation_id: str) -> None:
        self._conn.execute("DELETE FROM usage_log WHERE conversation_id=?",
                           (conversation_id,))
        self._conn.execute("DELETE FROM messages WHERE conversation_id=?",
                           (conversation_id,))
        self._conn.execute("DELETE FROM conversations WHERE id=?",
                           (conversation_id,))
        self._conn.commit()

    def list_by_owner(self, owner: str, limit: int = 20) -> list[Conversation]:
        rows = self._conn.execute(
            "SELECT * FROM conversations WHERE owner=? ORDER BY updated_at DESC LIMIT ?",
            (owner, limit)).fetchall()
        return [self._row_to_conv(r) for r in rows]

    def set_metadata(self, conversation_id: str, key: str, value) -> None:
        row = self._conn.execute(
            "SELECT metadata FROM conversations WHERE id=?",
            (conversation_id,)).fetchone()
        if not row:
            return
        meta = json.loads(row["metadata"] or "{}")
        meta[key] = value
        self._conn.execute(
            "UPDATE conversations SET metadata=?, updated_at=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False),
             datetime.now().isoformat(), conversation_id))
        self._conn.commit()

    def set_alias(self, conversation_id: str, alias: str) -> None:
        row = self._conn.execute(
            "SELECT owner FROM conversations WHERE id=?",
            (conversation_id,)).fetchone()
        if not row:
            return
        # 다른 conversation이 이 alias를 먼저 쓰고 있다면 박탈
        if alias:
            self._conn.execute(
                "UPDATE conversations SET alias='' WHERE owner=? AND alias=? AND id!=?",
                (row["owner"], alias, conversation_id))
        self._conn.execute(
            "UPDATE conversations SET alias=?, updated_at=? WHERE id=?",
            (alias or "", datetime.now().isoformat(), conversation_id))
        self._conn.commit()

    def record_usage(self, conversation_id: str, *,
                     prompt_tokens: int = 0, completion_tokens: int = 0,
                     total_tokens: int = 0, cached_tokens: int = 0,
                     latency_ms: int = 0,
                     provider: str = "", model: str = "",
                     agent: str = "", alias: str = "") -> None:
        if not alias:
            row = self._conn.execute(
                "SELECT alias FROM conversations WHERE id=?",
                (conversation_id,)).fetchone()
            if row:
                alias = row["alias"] or ""
        self._conn.execute(
            """INSERT INTO usage_log (conversation_id, timestamp,
               prompt_tokens, completion_tokens, total_tokens, cached_tokens,
               latency_ms, provider, model, agent, alias)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (conversation_id, datetime.now().isoformat(),
             prompt_tokens, completion_tokens, total_tokens, cached_tokens,
             latency_ms, provider, model, agent, alias))
        self._conn.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (datetime.now().isoformat(), conversation_id))
        self._conn.commit()

    def get_token_stats(self, owner: str = "", days: int = 7,
                        *, alias: str = "", provider: str = "",
                        model: str = "", agent: str = "",
                        group_by: str | None = None) -> dict:
        """토큰 사용 통계 조회 (usage_log 기반, 다축 집계 지원)."""
        where = []
        params: list = []
        join = ""
        if days > 0:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            where.append("u.timestamp >= ?")
            params.append(cutoff)
        if owner:
            join = "JOIN conversations c ON u.conversation_id = c.id"
            where.append("c.owner = ?")
            params.append(owner)
        if alias:
            where.append("u.alias = ?")
            params.append(alias)
        if provider:
            where.append("u.provider = ?")
            params.append(provider)
        if model:
            where.append("u.model = ?")
            params.append(model)
        if agent:
            where.append("u.agent = ?")
            params.append(agent)

        sql = f"""
            SELECT u.prompt_tokens, u.completion_tokens, u.total_tokens,
                   u.cached_tokens, u.latency_ms, u.provider, u.model,
                   u.agent, u.alias, u.timestamp
            FROM usage_log u {join}
            WHERE {' AND '.join(where) if where else '1=1'}
        """
        rows = self._conn.execute(sql, params).fetchall()

        total = {k: 0 for k in (
            "total_tokens", "total_prompt", "total_completion",
            "total_cached", "total_latency_ms", "total_calls")}
        by_provider: dict[str, int] = {}
        groups: dict[str, dict] = {}

        for r in rows:
            total["total_tokens"] += r["total_tokens"]
            total["total_prompt"] += r["prompt_tokens"]
            total["total_completion"] += r["completion_tokens"]
            total["total_cached"] += r["cached_tokens"] or 0
            total["total_latency_ms"] += r["latency_ms"] or 0
            total["total_calls"] += 1
            p = r["provider"] or "unknown"
            by_provider[p] = by_provider.get(p, 0) + r["total_tokens"]
            if group_by:
                key = _sqlite_group_key(r, group_by)
                if key not in groups:
                    groups[key] = {k: 0 for k in (
                        "total_tokens", "total_prompt", "total_completion",
                        "total_cached", "total_latency_ms", "total_calls")}
                g = groups[key]
                g["total_tokens"] += r["total_tokens"]
                g["total_prompt"] += r["prompt_tokens"]
                g["total_completion"] += r["completion_tokens"]
                g["total_cached"] += r["cached_tokens"] or 0
                g["total_latency_ms"] += r["latency_ms"] or 0
                g["total_calls"] += 1

        out = dict(total)
        out["by_provider"] = by_provider
        if group_by:
            out["group_by"] = group_by
            out["groups"] = groups
        return out


SQLiteSessionStore = SQLiteStore


def _sqlite_group_key(row, axis: str) -> str:
    if axis == "day":
        ts = row["timestamp"] or ""
        return ts[:10]
    if axis == "provider":
        return row["provider"] or "unknown"
    if axis == "model":
        return row["model"] or "unknown"
    if axis == "alias":
        return row["alias"] or "(no alias)"
    if axis == "agent":
        return row["agent"] or "(no agent)"
    return "unknown"
