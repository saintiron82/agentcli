"""인메모리 대화 저장소 (테스트/경량용)."""

import uuid
from datetime import datetime, timedelta
from threading import RLock
from .base import ConversationStore
from ..types import Conversation, Message


_BUCKET_KEYS = (
    "total_tokens", "total_prompt", "total_completion", "total_cached",
    "total_latency_ms", "total_calls",
)


def _zero_bucket() -> dict:
    return {k: 0 for k in _BUCKET_KEYS}


def _accumulate(bucket: dict, row: dict) -> None:
    bucket["total_tokens"] += row.get("total_tokens", 0)
    bucket["total_prompt"] += row.get("prompt_tokens", 0)
    bucket["total_completion"] += row.get("completion_tokens", 0)
    bucket["total_cached"] += row.get("cached_tokens", 0)
    bucket["total_latency_ms"] += row.get("latency_ms", 0)
    bucket["total_calls"] += 1


def _group_key(row: dict, axis: str) -> str:
    if axis == "day":
        ts = row.get("timestamp")
        if isinstance(ts, datetime):
            return ts.date().isoformat()
        # 문자열이면 앞 10자 (YYYY-MM-DD)
        return str(ts)[:10]
    if axis == "provider":
        return row.get("provider") or "unknown"
    if axis == "model":
        return row.get("model") or "unknown"
    if axis == "alias":
        return row.get("alias") or "(no alias)"
    if axis == "agent":
        return row.get("agent") or "(no agent)"
    return "unknown"


class MemoryStore(ConversationStore):
    def __init__(self, max_conversations: int = 200, ttl_hours: int = 24):
        self._lock = RLock()
        self._conversations: dict[str, Conversation] = {}
        self._messages: dict[str, list[Message]] = {}
        self._usage: dict[str, list[dict]] = {}
        # (owner, alias) → conversation_id 인덱스
        self._alias_index: dict[tuple[str, str], str] = {}
        self._max = max(1, int(max_conversations))
        self._ttl = timedelta(hours=ttl_hours) if ttl_hours > 0 else None

    def create(self, owner: str, provider: str, model: str = "",
               *, conversation_id: str = "",
               alias: str = "") -> Conversation:
        self._evict_if_needed()

        # alias로 이미 존재하는 conversation이 있으면 반환
        if alias:
            existing = self.find_by_alias(owner, alias)
            if existing is not None:
                return existing

        conv_id = conversation_id or str(uuid.uuid4())
        existing = self._conversations.get(conv_id)
        if existing and not self._is_expired(existing):
            # 기존 conversation에 alias가 비어 있으면 지금 부여
            if alias and not existing.alias:
                self._set_alias_internal(existing, alias)
            return existing

        now = datetime.now()
        conv = Conversation(id=conv_id, owner=owner, provider=provider,
                            model=model, created_at=now, updated_at=now,
                            alias=alias)
        self._conversations[conv_id] = conv
        self._messages[conv_id] = []
        self._usage[conv_id] = []
        if alias:
            self._alias_index[(owner, alias)] = conv_id
        return conv

    def get(self, conversation_id: str) -> Conversation | None:
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return None
        if self._is_expired(conv):
            self.delete(conversation_id)
            return None
        return conv

    def find_by_alias(self, owner: str, alias: str) -> Conversation | None:
        if not alias:
            return None
        cid = self._alias_index.get((owner, alias))
        if cid is None:
            return None
        return self.get(cid)

    def add_message(self, conversation_id: str, message: Message) -> None:
        if conversation_id in self._messages:
            self._messages[conversation_id].append(message)
            conv = self._conversations.get(conversation_id)
            if conv:
                conv.updated_at = datetime.now()

    def get_messages(self, conversation_id: str, limit: int = 0,
                     agent: str = "") -> list[Message]:
        msgs = self._messages.get(conversation_id, [])
        if agent:
            msgs = [m for m in msgs if m.agent == agent]
        if limit > 0:
            return msgs[-limit:]
        return list(msgs)

    def delete(self, conversation_id: str) -> None:
        conv = self._conversations.get(conversation_id)
        if conv and conv.alias:
            self._alias_index.pop((conv.owner, conv.alias), None)
        self._conversations.pop(conversation_id, None)
        self._messages.pop(conversation_id, None)
        self._usage.pop(conversation_id, None)

    def list_by_owner(self, owner: str, limit: int = 20) -> list[Conversation]:
        result = [c for c in self._conversations.values()
                  if c.owner == owner and not self._is_expired(c)]
        result.sort(key=lambda c: c.updated_at, reverse=True)
        return result[:limit]

    def set_metadata(self, conversation_id: str, key: str, value) -> None:
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return
        conv.metadata[key] = value
        conv.updated_at = datetime.now()

    def set_alias(self, conversation_id: str, alias: str) -> None:
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return
        self._set_alias_internal(conv, alias)

    def _set_alias_internal(self, conv: Conversation, alias: str) -> None:
        # 기존 alias 인덱스 제거
        if conv.alias:
            self._alias_index.pop((conv.owner, conv.alias), None)
        # 새 alias가 다른 conversation을 가리키고 있었다면 그 링크를 빼앗는다
        if alias:
            prev_cid = self._alias_index.get((conv.owner, alias))
            if prev_cid and prev_cid != conv.id:
                prev_conv = self._conversations.get(prev_cid)
                if prev_conv:
                    prev_conv.alias = ""
            self._alias_index[(conv.owner, alias)] = conv.id
        conv.alias = alias
        conv.updated_at = datetime.now()

    def record_usage(self, conversation_id: str, *,
                     prompt_tokens: int = 0, completion_tokens: int = 0,
                     total_tokens: int = 0, cached_tokens: int = 0,
                     latency_ms: int = 0,
                     provider: str = "", model: str = "",
                     agent: str = "", alias: str = "") -> None:
        if conversation_id not in self._usage:
            return
        # alias 미지정 시 conversation에서 자동 추출 (집계 축으로 쓰기 위함)
        if not alias:
            conv = self._conversations.get(conversation_id)
            if conv:
                alias = conv.alias
        self._usage[conversation_id].append({
            "timestamp": datetime.now(),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "latency_ms": latency_ms,
            "provider": provider,
            "model": model,
            "agent": agent,
            "alias": alias,
        })
        conv = self._conversations.get(conversation_id)
        if conv:
            conv.updated_at = datetime.now()

    def get_token_stats(self, owner: str = "", days: int = 7,
                        *, alias: str = "", provider: str = "",
                        model: str = "", agent: str = "",
                        group_by: str | None = None) -> dict:
        cutoff = datetime.now() - timedelta(days=days) if days > 0 else None
        rows = self._collect_usage_rows(
            cutoff=cutoff, owner=owner, alias=alias,
            provider=provider, model=model, agent=agent)

        total = _zero_bucket()
        groups: dict[str, dict] = {}
        for r in rows:
            _accumulate(total, r)
            if group_by:
                key = _group_key(r, group_by)
                if key not in groups:
                    groups[key] = _zero_bucket()
                _accumulate(groups[key], r)

        out = dict(total)
        # 하위 호환: by_provider 키 제공 (group_by와 별도로 항상 계산)
        by_provider: dict[str, int] = {}
        for r in rows:
            p = r["provider"] or "unknown"
            by_provider[p] = by_provider.get(p, 0) + r["total_tokens"]
        out["by_provider"] = by_provider

        if group_by:
            out["group_by"] = group_by
            out["groups"] = groups
        return out

    def _collect_usage_rows(self, *, cutoff, owner, alias, provider,
                             model, agent) -> list[dict]:
        rows: list[dict] = []
        for conv_id, conv in self._conversations.items():
            if owner and conv.owner != owner:
                continue
            for row in self._usage.get(conv_id, []):
                if cutoff and row["timestamp"] < cutoff:
                    continue
                if alias and row.get("alias", "") != alias:
                    continue
                if provider and row.get("provider", "") != provider:
                    continue
                if model and row.get("model", "") != model:
                    continue
                if agent and row.get("agent", "") != agent:
                    continue
                rows.append(row)
        return rows

    def _is_expired(self, conv: Conversation) -> bool:
        if self._ttl is None:
            return False
        return datetime.now() - conv.updated_at > self._ttl

    def _evict_if_needed(self) -> None:
        if self._ttl is not None:
            expired = [cid for cid, c in self._conversations.items()
                       if self._is_expired(c)]
            for cid in expired:
                self.delete(cid)
        while len(self._conversations) >= self._max:
            oldest_id = min(self._conversations.items(),
                            key=lambda kv: kv[1].updated_at)[0]
            self.delete(oldest_id)
