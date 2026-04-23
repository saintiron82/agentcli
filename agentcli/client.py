"""LLMClient — 통합 멀티 AI 모델 클라이언트.

핵심 원칙:
  - 세션 provider (supports_sessions=True): CLI 세션이 대화 히스토리를 소유.
    라이브러리는 session_id만 Conversation.metadata에 저장하고, 호출 시
    prev_messages를 프롬프트에 재주입하지 않는다. messages 테이블도 사용하지
    않고, 토큰/지연시간만 usage_log에 기록한다 (중복 저장 제거).
  - 비세션 provider: 라이브러리가 content 원본을 저장하고 context_turns만큼
    프롬프트에 직렬화해 주입한다.
  - 실패 호출(content="")은 저장소에 어떤 흔적도 남기지 않는다 (원자성).
  - Fallback은 세션 연속성을 포기한다: 전환된 provider는 새 세션으로 시작.
  - 동기 `chat()` + 비동기 `chat_async()` + 스트리밍 `chat_stream()`을 제공.
"""

import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from .types import Message, LLMResponse, TokenUsage, StreamChunk
from .store.base import ConversationStore
from .providers.registry import ProviderRegistry, create_default_registry

logger = logging.getLogger(__name__)

SESSION_KEY_FMT = "session_id:{provider}"
DRIFT_KEY = "instructions_hashes"
DRIFT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "AGENTS.override.md")

# mtime 기반 해시 캐시 — 파일이 변경되지 않으면 재해시하지 않음.
# {파일 절대경로: (mtime, hash16)}
_DRIFT_HASH_CACHE: dict[str, tuple[float, str]] = {}


def _compute_instructions_hashes(cwd: str | None) -> dict:
    """cwd의 agent 지시 파일들 해시를 dict로 반환.

    mtime 기반 캐시: 파일 수정 시각이 이전과 같으면 재해시 스킵 → I/O 0.
    """
    if not cwd:
        return {}
    try:
        base = Path(cwd).expanduser()
    except (OSError, RuntimeError):
        return {}
    result = {}
    for fname in DRIFT_INSTRUCTION_FILES:
        f = base / fname
        if not f.exists() or not f.is_file():
            continue
        try:
            stat = f.stat()
            key = str(f)
            cached = _DRIFT_HASH_CACHE.get(key)
            if cached and cached[0] == stat.st_mtime:
                result[fname] = cached[1]
            else:
                data = f.read_bytes()
                h = hashlib.sha256(data).hexdigest()[:16]
                _DRIFT_HASH_CACHE[key] = (stat.st_mtime, h)
                result[fname] = h
        except OSError:
            pass
    return result


# (provider_class, method_name) → bool : 메서드가 alias 파라미터를 받는지.
# provider 클래스는 런타임에 바뀌지 않으므로 1회 계산 후 캐시.
_ALIAS_SUPPORT_CACHE: dict[tuple[type, str], bool] = {}


def _method_accepts_alias(provider_obj, method_name: str) -> bool:
    key = (type(provider_obj), method_name)
    cached = _ALIAS_SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    import inspect
    method = getattr(provider_obj, method_name)
    params = inspect.signature(method).parameters
    result = "alias" in params
    _ALIAS_SUPPORT_CACHE[key] = result
    return result


def _invoke_with_alias(provider_obj, messages, *, model, timeout,
                       session_id, cwd, alias):
    if _method_accepts_alias(provider_obj, "invoke"):
        return provider_obj.invoke(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd, alias=alias)
    return provider_obj.invoke(
        messages, model=model, timeout=timeout,
        session_id=session_id, cwd=cwd)


async def _invoke_async_with_alias(provider_obj, messages, *, model, timeout,
                                    session_id, cwd, alias):
    if _method_accepts_alias(provider_obj, "invoke_async"):
        return await provider_obj.invoke_async(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd, alias=alias)
    return await provider_obj.invoke_async(
        messages, model=model, timeout=timeout,
        session_id=session_id, cwd=cwd)


def _stream_with_alias(provider_obj, messages, *, model, timeout,
                        session_id, cwd, alias):
    if _method_accepts_alias(provider_obj, "stream_async"):
        return provider_obj.stream_async(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd, alias=alias)
    return provider_obj.stream_async(
        messages, model=model, timeout=timeout,
        session_id=session_id, cwd=cwd)


class LLMClient:
    def __init__(self, store: ConversationStore,
                 registry: ProviderRegistry | None = None,
                 fallback_order: list[str] | None = None):
        self._store = store
        self._registry = registry or create_default_registry()
        if fallback_order:
            self._registry.set_fallback_order(fallback_order)

    # ---------- 공용 준비/저장 로직 (sync/async 공유) ----------

    def _check_drift(self, conv, cwd: str | None) -> None:
        """cwd의 AGENTS.md/CLAUDE.md 해시를 계산하여 저장 + 드리프트 로깅."""
        if not cwd:
            return
        new_hashes = _compute_instructions_hashes(cwd)
        if not new_hashes:
            return
        prev = conv.metadata.get(DRIFT_KEY, {}) or {}
        if prev and prev != new_hashes:
            diffs = {}
            for k in set(prev) | set(new_hashes):
                p, n = prev.get(k), new_hashes.get(k)
                if p != n:
                    diffs[k] = f"{(p or '—')[:8]}→{(n or '—')[:8]}"
            logger.warning(
                "[드리프트] conv=%s alias=%s cwd=%s: %s",
                conv.id[:16], conv.alias or "—", cwd, diffs)
        if prev != new_hashes:
            self._store.set_metadata(conv.id, DRIFT_KEY, new_hashes)

    def _prepare(self, prompt: str, provider: str, model: str,
                 conversation_id: str, owner: str,
                 system_prompt: str, context_turns: int,
                 agent: str,
                 inject_context: list[dict] | None,
                 alias: str = "",
                 cwd: str | None = None):
        """프로바이더 결정 → conversation resolve → messages 조립.

        Conversation 해석 우선순위:
          1. conversation_id가 명시되면 그것으로 조회/생성
          2. alias가 명시되면 (owner, alias)로 조회/생성
          3. 둘 다 없으면 신규 conversation

        Returns: (provider_id, provider_obj, conv, messages, session_id)
        """
        if not provider:
            chain = self._registry.get_fallback_chain()
            provider = chain[0] if chain else ""

        conv = None
        if conversation_id:
            conv = self._store.get(conversation_id)
        elif alias and owner:
            conv = self._store.find_by_alias(owner, alias)
        if conv is None:
            conv = self._store.create(owner, provider, model,
                                       conversation_id=conversation_id,
                                       alias=alias)
        elif alias and not conv.alias:
            # 기존 conversation에 alias 부여 (처음 호출 시 지정한 경우)
            self._store.set_alias(conv.id, alias)
            conv.alias = alias

        # 드리프트 체크: cwd의 지시 파일 해시 비교 + 저장
        self._check_drift(conv, cwd)

        provider_obj = self._registry.get(provider)
        is_session = bool(provider_obj and provider_obj.supports_sessions)

        session_id = ""
        prev_messages: list[Message] = []
        injected_messages: list[Message] = []
        if is_session:
            session_id = conv.metadata.get(
                SESSION_KEY_FMT.format(provider=provider), "")
        else:
            if context_turns > 0:
                prev_messages = self._store.get_messages(
                    conv.id, limit=context_turns * 2)
            if inject_context:
                for ctx in inject_context:
                    ctx_msgs = self._store.get_messages(
                        ctx["conversation_id"],
                        limit=ctx.get("limit", 10),
                        agent=ctx.get("agent", ""),
                    )
                    injected_messages.extend(ctx_msgs)

        messages: list[Message] = []
        if system_prompt:
            messages.append(Message(role="system", content=system_prompt))
        if injected_messages:
            messages.extend(injected_messages)
        if prev_messages:
            messages.extend(prev_messages)
        messages.append(Message(role="user", content=prompt, agent=agent))
        return provider, provider_obj, conv, messages, session_id

    def _persist(self, conv_id: str, prompt: str,
                 response: LLMResponse, agent: str,
                 alias: str = "") -> None:
        """성공한 호출에 대해 usage 기록 + (비세션) 메시지 저장.

        세션 provider는 `add_message` 호출 없이 session_id만 metadata에 저장.
        cached_tokens와 alias를 usage_log에 함께 기록하여 에이전트별/캐시 관측.
        """
        self._store.record_usage(
            conv_id,
            prompt_tokens=response.tokens.prompt_tokens,
            completion_tokens=response.tokens.completion_tokens,
            total_tokens=response.tokens.total_tokens,
            cached_tokens=response.tokens.cached_tokens,
            latency_ms=response.latency_ms,
            provider=response.provider,
            model=response.model,
            agent=agent,
            alias=alias,
        )
        p = self._registry.get(response.provider) if response.provider else None
        actual_is_session = bool(p and p.supports_sessions)

        if actual_is_session:
            if response.session_id:
                self._store.set_metadata(
                    conv_id,
                    SESSION_KEY_FMT.format(provider=response.provider),
                    response.session_id)
        else:
            now = datetime.now()
            self._store.add_message(conv_id, Message(
                role="user", content=prompt, timestamp=now, agent=agent))
            self._store.add_message(conv_id, Message(
                role="assistant", content=response.content,
                timestamp=now, agent=agent,
                metadata={
                    "provider": response.provider,
                    "model": response.model,
                    "prompt_tokens": response.tokens.prompt_tokens,
                    "completion_tokens": response.tokens.completion_tokens,
                    "total_tokens": response.tokens.total_tokens,
                    "latency_ms": response.latency_ms,
                }))

    # ---------- 동기 chat ----------

    def chat(self, prompt: str, *,
             provider: str = "", model: str = "",
             conversation_id: str = "", owner: str = "",
             alias: str = "",
             system_prompt: str = "", context_turns: int = 3,
             timeout: int = 120,
             agent: str = "",
             cwd: str | None = None,
             inject_context: list[dict] | None = None,
             ) -> LLMResponse:
        provider, _, conv, messages, session_id = self._prepare(
            prompt, provider, model, conversation_id, owner,
            system_prompt, context_turns, agent, inject_context, alias, cwd)
        conversation_id = conv.id
        resolved_alias = conv.alias

        response = self._invoke_with_fallback(
            provider, messages, model, timeout, session_id, cwd,
            alias=resolved_alias)
        response.conversation_id = conversation_id

        if not response.content:
            return response

        self._persist(conversation_id, prompt, response, agent,
                      alias=resolved_alias)
        return response

    def _invoke_with_fallback(self, provider_id: str,
                              messages: list[Message], model: str,
                              timeout: int, session_id: str,
                              cwd: str | None,
                              *, alias: str = "") -> LLMResponse:
        p = self._registry.get(provider_id)
        if p:
            resp = _invoke_with_alias(
                p, messages, model=model, timeout=timeout,
                session_id=session_id, cwd=cwd, alias=alias)
            if resp.content:
                return resp

        current = provider_id
        while True:
            next_id = self._registry.get_next_fallback(current)
            if not next_id:
                break
            logger.warning("[LLM] %s 실패 → %s fallback", current, next_id)
            p = self._registry.get(next_id)
            if p:
                # Fallback은 세션 연속성 포기 — session_id 전달 안 함. alias는 유지.
                resp = _invoke_with_alias(
                    p, messages, model="", timeout=timeout,
                    session_id="", cwd=cwd, alias=alias)
                if resp.content:
                    return resp
            current = next_id

        return LLMResponse(content="", provider=provider_id, model=model,
                           tokens=TokenUsage())

    # ---------- 비동기 chat ----------

    async def chat_async(self, prompt: str, *,
                         provider: str = "", model: str = "",
                         conversation_id: str = "", owner: str = "",
                         alias: str = "",
                         system_prompt: str = "", context_turns: int = 3,
                         timeout: int = 120,
                         agent: str = "",
                         cwd: str | None = None,
                         inject_context: list[dict] | None = None,
                         ) -> LLMResponse:
        provider, _, conv, messages, session_id = self._prepare(
            prompt, provider, model, conversation_id, owner,
            system_prompt, context_turns, agent, inject_context, alias, cwd)
        conversation_id = conv.id
        resolved_alias = conv.alias

        response = await self._invoke_async_with_fallback(
            provider, messages, model, timeout, session_id, cwd,
            alias=resolved_alias)
        response.conversation_id = conversation_id

        if not response.content:
            return response

        # 저장은 sync이므로 스레드로 이전 (SQLite 등 blocking 방지)
        await asyncio.to_thread(self._persist, conversation_id, prompt,
                                response, agent, resolved_alias)
        return response

    async def _invoke_async_with_fallback(self, provider_id: str,
                                          messages: list[Message], model: str,
                                          timeout: int, session_id: str,
                                          cwd: str | None,
                                          *, alias: str = "") -> LLMResponse:
        p = self._registry.get(provider_id)
        if p:
            resp = await _invoke_async_with_alias(
                p, messages, model=model, timeout=timeout,
                session_id=session_id, cwd=cwd, alias=alias)
            if resp.content:
                return resp

        current = provider_id
        while True:
            next_id = self._registry.get_next_fallback(current)
            if not next_id:
                break
            logger.warning("[LLM] %s 실패 → %s fallback (async)", current, next_id)
            p = self._registry.get(next_id)
            if p:
                resp = await _invoke_async_with_alias(
                    p, messages, model="", timeout=timeout,
                    session_id="", cwd=cwd, alias=alias)
                if resp.content:
                    return resp
            current = next_id

        return LLMResponse(content="", provider=provider_id, model=model,
                           tokens=TokenUsage())

    # ---------- 스트리밍 chat ----------

    async def chat_stream(self, prompt: str, *,
                          provider: str = "", model: str = "",
                          conversation_id: str = "", owner: str = "",
                          alias: str = "",
                          system_prompt: str = "", context_turns: int = 3,
                          timeout: int = 120,
                          agent: str = "",
                          cwd: str | None = None,
                          inject_context: list[dict] | None = None,
                          ) -> AsyncIterator[StreamChunk]:
        """스트리밍 호출. 청크를 yield하면서 응답을 누적하고 완료 시 저장.

        yield type:
          - "text" | "thinking" | "tool_use" | "tool_result" | "event" | "error"
          - "done" — 마지막 청크. content(누적), session_id, usage 포함.

        Fallback은 스트리밍 경로에서는 미지원 (원 provider 실패 시 "error" 후 종료).
        Fallback이 필요하면 `chat_async()`를 사용.
        """
        provider, provider_obj, conv, messages, session_id = self._prepare(
            prompt, provider, model, conversation_id, owner,
            system_prompt, context_turns, agent, inject_context, alias, cwd)
        conversation_id = conv.id
        resolved_alias = conv.alias

        if provider_obj is None:
            yield StreamChunk(type="error",
                              content=f"unknown provider: {provider}")
            return

        text_parts: list[str] = []
        final_sid = session_id
        final_usage = TokenUsage()
        latency_ms = 0

        async for chunk in _stream_with_alias(
                provider_obj, messages, model=model, timeout=timeout,
                session_id=session_id, cwd=cwd, alias=resolved_alias):
            if chunk.type == "text":
                text_parts.append(chunk.content)
            if chunk.session_id:
                final_sid = chunk.session_id
            if chunk.type == "done":
                if chunk.usage is not None:
                    final_usage = chunk.usage
                latency_ms = int(chunk.data.get("latency_ms") or 0)
                # done은 마지막에 다시 합쳐서 보냄 (content 누적 기준)
                break
            yield chunk

        full_content = "".join(text_parts)

        # 실패: 저장 안 함
        if not full_content:
            yield StreamChunk(type="done", content="",
                              session_id=final_sid, usage=final_usage,
                              data={"provider": provider, "model": model,
                                    "latency_ms": latency_ms})
            return

        # 성공: 일반 경로와 동일하게 저장
        synthetic = LLMResponse(
            content=full_content, provider=provider, model=model,
            tokens=final_usage, latency_ms=latency_ms,
            session_id=final_sid, conversation_id=conversation_id)
        await asyncio.to_thread(self._persist, conversation_id, prompt,
                                synthetic, agent, resolved_alias)

        yield StreamChunk(type="done", content=full_content,
                          session_id=final_sid, usage=final_usage,
                          data={"provider": provider, "model": model,
                                "latency_ms": latency_ms,
                                "conversation_id": conversation_id})

    # ---------- 메타 API ----------

    def list_providers(self) -> list[dict]:
        return self._registry.list_providers()

    def list_models(self, provider: str = "") -> list[dict]:
        return self._registry.list_models(provider)

    def get_token_stats(self, owner: str = "", days: int = 7,
                        *, alias: str = "", provider: str = "",
                        model: str = "", agent: str = "",
                        group_by: str | None = None) -> dict:
        """토큰·비용 통계. 필터 + 집계 축 지원.

        group_by: 'provider' | 'model' | 'alias' | 'agent' | 'day' | None.
        """
        if hasattr(self._store, "get_token_stats"):
            return self._store.get_token_stats(
                owner, days,
                alias=alias, provider=provider, model=model, agent=agent,
                group_by=group_by)
        return {"total_tokens": 0, "total_calls": 0}

    def list_drifts(self, *, owner: str = "",
                    alias: str = "") -> list[dict]:
        """프로젝트 간 지시문 드리프트 가시화.

        같은 alias로 여러 conversation이 존재하거나, 한 conversation이
        서로 다른 hash 이력을 갖는 경우를 나열.

        Returns: [{"alias","conversation_id","owner","cwd_hashes": {...},
                   "session_providers": [...]}]
        """
        rows: list[dict] = []
        # 범위 결정: owner가 있으면 그 owner의 conv들, 아니면 전체 순회 불가능이므로
        # owner 없을 때는 각 provider 세션 목록을 쓸 수 없음. 현재 API로는
        # list_by_owner를 owner별로 돌려야 함.
        if owner:
            convs = self._store.list_by_owner(owner, limit=1000)
        else:
            # store에 전체 순회 없음 — 알려진 캐시 conversation만 반환
            convs = []
        for c in convs:
            if alias and c.alias != alias:
                continue
            hashes = c.metadata.get(DRIFT_KEY, {})
            if not hashes:
                continue
            rows.append({
                "alias": c.alias,
                "conversation_id": c.id,
                "owner": c.owner,
                "cwd_hashes": dict(hashes),
                "session_providers": [
                    k.split(":", 1)[1] for k in c.metadata
                    if k.startswith("session_id:")],
                "updated_at": c.updated_at.isoformat(),
            })
        return rows
