"""LLMClient — 통합 멀티 AI 모델 클라이언트.

핵심 원칙:
  - 세션 provider (supports_sessions=True): CLI 세션이 대화 히스토리를 소유.
    라이브러리는 session_id만 Conversation.metadata에 저장하고, 호출 시
    prev_messages를 프롬프트에 재주입하지 않는다. messages 테이블도 사용하지
    않고, 토큰/지연시간만 usage_log에 기록한다 (중복 저장 제거).
  - 비세션 provider: 라이브러리가 content 원본을 저장하고 context_turns만큼
    프롬프트에 직렬화해 주입한다.
  - 실패 호출(content="")은 저장소에 어떤 흔적도 남기지 않는다 (원자성).
  - Fallback은 명시적으로 켠 호출에서만 동작한다. 전환된 provider는 새 세션으로 시작.
  - 동기 `chat()` + 비동기 `chat_async()` + 스트리밍 `chat_stream()`을 제공.
"""

import asyncio
import hashlib
import logging
import threading
import uuid
import weakref
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from .types import (Message, LLMResponse, ProviderCapabilities, ProviderHealth,
                    TokenUsage, StreamChunk, make_error_chunk,
                    standardize_error_chunk)
from .store.base import ConversationStore
from .providers.registry import ProviderRegistry, create_default_registry

logger = logging.getLogger(__name__)

SESSION_KEY_FMT = "session_id:{provider}"
SYSTEM_PROMPT_HASH_KEY_FMT = "system_prompt_hash:{provider}"
DRIFT_KEY = "instructions_hashes"
DRIFT_INSTRUCTION_FILES = (
    "AGENTS.md", "CLAUDE.md", "GUIDE.md", "AGENTS.override.md")

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


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _session_providers_from_metadata(metadata: dict) -> list[str]:
    providers = [
        k.split(":", 1)[1]
        for k, value in metadata.items()
        if k.startswith("session_id:") and value
    ]
    return sorted(set(providers))


# (provider_class, method_name, param_name) → bool : 메서드가 파라미터를 받는지.
# provider 클래스는 런타임에 바뀌지 않으므로 1회 계산 후 캐시.
_PARAM_SUPPORT_CACHE: dict[tuple[type, str, str], bool] = {}


def _method_accepts_param(provider_obj, method_name: str, param_name: str) -> bool:
    key = (type(provider_obj), method_name, param_name)
    cached = _PARAM_SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    import inspect
    method = getattr(provider_obj, method_name)
    params = inspect.signature(method).parameters
    result = param_name in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    _PARAM_SUPPORT_CACHE[key] = result
    return result


def _supported_kwargs(provider_obj, method_name: str, kwargs: dict) -> dict:
    return {
        key: value
        for key, value in kwargs.items()
        if _method_accepts_param(provider_obj, method_name, key)
    }


def _merge_options(base: dict, provider_options: dict | None) -> dict:
    """provider_options(호출 시점 옵션)를 base kwargs 에 병합 — provider_options 우선.

    실제로 provider 의 메서드가 받는 키만 호출 직전 ``_supported_kwargs`` 가
    걸러내므로, 한 provider 전용 옵션(예: claude mcp_config)을 다른 provider
    로 fallback 해도 안전하게 무시된다 (#154)."""
    if provider_options:
        base.update(provider_options)
    return base


def _invoke_with_alias(provider_obj, messages, *, model, timeout,
                       session_id, cwd, alias, resume_by_alias=True,
                       provider_options=None):
    kwargs = _supported_kwargs(provider_obj, "invoke", _merge_options({
        "model": model,
        "timeout": timeout,
        "session_id": session_id,
        "cwd": cwd,
        "alias": alias,
        "resume_by_alias": resume_by_alias,
    }, provider_options))
    return provider_obj.invoke(messages, **kwargs)


async def _invoke_async_with_alias(provider_obj, messages, *, model, timeout,
                                    session_id, cwd, alias,
                                    resume_by_alias=True,
                                    provider_options=None):
    kwargs = _supported_kwargs(provider_obj, "invoke_async", _merge_options({
        "model": model,
        "timeout": timeout,
        "session_id": session_id,
        "cwd": cwd,
        "alias": alias,
        "resume_by_alias": resume_by_alias,
    }, provider_options))
    return await provider_obj.invoke_async(messages, **kwargs)


def _stream_with_alias(provider_obj, messages, *, model, timeout,
                        session_id, cwd, alias, idle_timeout=None,
                        wall_timeout=None, resume_by_alias=True,
                        provider_options=None):
    kwargs = _supported_kwargs(provider_obj, "stream_async", _merge_options({
        "model": model,
        "timeout": timeout,
        "session_id": session_id,
        "cwd": cwd,
        "alias": alias,
        "idle_timeout": idle_timeout,
        "wall_timeout": wall_timeout,
        "resume_by_alias": resume_by_alias,
    }, provider_options))
    return provider_obj.stream_async(messages, **kwargs)


class LLMClient:
    def __init__(self, store: ConversationStore,
                 registry: ProviderRegistry | None = None,
                 fallback_order: list[str] | None = None):
        self._store = store
        self._registry = registry or create_default_registry()
        if fallback_order:
            self._registry.set_fallback_order(fallback_order)
        # 같은 conversation 에 대한 동시 호출 직렬화 (in-process).
        # 직렬화 없이는 두 호출이 같은 session_id 를 읽고 각자 CLI 세션을
        # 분기시킨 뒤 마지막 쓰기가 다른 쪽을 덮어쓴다.
        self._conv_locks_guard = threading.Lock()
        self._sync_conv_locks: dict[str, threading.Lock] = {}
        # asyncio.Lock 은 event loop 에 묶이므로 루프별로 분리. 루프가
        # 소멸하면 WeakKeyDictionary 가 잠금 사전을 함께 회수한다.
        self._async_conv_locks: "weakref.WeakKeyDictionary" = (
            weakref.WeakKeyDictionary())

    def _store_lock(self):
        lock = getattr(self._store, "_lock", None)
        return lock if lock is not None else nullcontext()

    def _sync_conversation_lock(self, conv_id: str) -> threading.Lock:
        with self._conv_locks_guard:
            lock = self._sync_conv_locks.get(conv_id)
            if lock is None:
                lock = threading.Lock()
                self._sync_conv_locks[conv_id] = lock
            return lock

    def _async_conversation_lock(self, conv_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._conv_locks_guard:
            per_loop = self._async_conv_locks.get(loop)
            if per_loop is None:
                per_loop = {}
                self._async_conv_locks[loop] = per_loop
            lock = per_loop.get(conv_id)
            if lock is None:
                lock = asyncio.Lock()
                per_loop[conv_id] = lock
            return lock

    def _refresh_session_id(self, conv_id: str, provider: str,
                            provider_obj, session_id: str,
                            force_new_session: bool) -> str:
        """conversation 잠금 획득 후 최신 session_id 재조회.

        _prepare 와 잠금 획득 사이에 동시 호출이 새 세션을 만들었을 수 있다.
        같은 모드(sync↔sync, async↔async) 내 동시 호출만 직렬화된다.
        """
        if force_new_session or not (
                provider_obj and provider_obj.supports_sessions):
            return session_id
        with self._store_lock():
            conv = self._store.get(conv_id)
        if conv is None:
            return session_id
        # 저장값을 그대로 반환 — 동시 clear_session_metadata("") 도 존중한다.
        return conv.metadata.get(
            SESSION_KEY_FMT.format(provider=provider), "")

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
                 strict_model: bool = False,
                 reset_on_instruction_change: bool = False,
                 alias: str = "",
                 cwd: str | None = None,
                 new_session: bool = False):
        """프로바이더 결정 → conversation resolve → messages 조립.

        Conversation 해석 우선순위:
          1. conversation_id가 명시되면 그것으로 조회/생성
          2. alias가 명시되면 (owner, alias)로 조회/생성
          3. 둘 다 없으면 신규 conversation

        Returns:
            (provider_id, provider_obj, conv, messages, fallback_messages,
             session_id, model, created_conversation, resolved_alias,
             alias_to_set, system_prompt_hash, force_new_session)
        """
        if not provider:
            chain = self._registry.get_fallback_chain()
            provider = chain[0] if chain else ""

        provider_obj = self._registry.get(provider)
        if provider_obj is not None:
            model = provider_obj.resolve_model(model, strict=strict_model)

        alias_existing = None
        if alias:
            alias_existing = self._store.find_by_alias(owner, alias)
        if (conversation_id and alias_existing is not None
                and alias_existing.id != conversation_id):
            raise ValueError(
                f"alias conflict for owner={owner!r}, alias={alias!r}: "
                f"points to {alias_existing.id!r}, not {conversation_id!r}")

        conv = None
        if conversation_id:
            conv = self._store.get(conversation_id)
        elif alias_existing is not None:
            conv = alias_existing

        created_conversation = False
        if conv is None:
            conv = self._store.create(owner, provider, model,
                                       conversation_id=conversation_id,
                                       alias=alias)
            created_conversation = alias_existing is None
        resolved_alias = conv.alias or alias
        alias_to_set = alias if alias and not conv.alias else ""

        is_session = bool(provider_obj and provider_obj.supports_sessions)
        instruction_hashes = _compute_instructions_hashes(cwd)

        session_id = ""
        prev_messages: list[Message] = []
        injected_messages: list[Message] = []
        # CLI provider (stores_history=False) 는 비세션 모드여도 라이브러리가
        # 히스토리를 갖고 있지 않으므로 재주입할 것이 없다.
        wants_history = (provider_obj is None
                         or getattr(provider_obj, "stores_history", True))
        if is_session:
            session_id = conv.metadata.get(
                SESSION_KEY_FMT.format(provider=provider), "")
        elif context_turns > 0 and wants_history and not new_session:
            prev_messages = self._store.get_messages(
                conv.id, limit=context_turns * 2)
        # 호스트 주입 모드: inject_context 는 명시적 의도이므로 세션 provider
        # 에도 적용된다 (build_session_prompt 가 Context 블록으로 직렬화).
        if inject_context:
            for ctx in inject_context:
                ctx_msgs = self._store.get_messages(
                    ctx["conversation_id"],
                    limit=ctx.get("limit", 10),
                    agent=ctx.get("agent", ""),
                )
                injected_messages.extend(ctx_msgs)

        system_prompt_hash = _hash_text(system_prompt) if system_prompt.strip() else ""
        force_new_session = False
        if is_session and reset_on_instruction_change and session_id:
            prev_instruction_hashes = conv.metadata.get(DRIFT_KEY, {}) or {}
            if (instruction_hashes and prev_instruction_hashes
                    and instruction_hashes != prev_instruction_hashes):
                force_new_session = True
            prev_system_hash = conv.metadata.get(
                SYSTEM_PROMPT_HASH_KEY_FMT.format(provider=provider), "")
            if system_prompt_hash and prev_system_hash and prev_system_hash != system_prompt_hash:
                force_new_session = True
            if force_new_session:
                session_id = ""

        # 미사용 모드: 이 호출만 의도적으로 새 세션에서 시작.
        if new_session and is_session:
            session_id = ""
            force_new_session = True

        include_system = bool(system_prompt)
        if is_session and system_prompt_hash:
            seen_hash = conv.metadata.get(
                SYSTEM_PROMPT_HASH_KEY_FMT.format(provider=provider), "")
            include_system = not session_id or seen_hash != system_prompt_hash

        def build_messages(*, include_system_prompt: bool) -> list[Message]:
            built: list[Message] = []
            if include_system_prompt and system_prompt:
                built.append(Message(role="system", content=system_prompt))
            if injected_messages:
                built.extend(injected_messages)
            if prev_messages:
                built.extend(prev_messages)
            built.append(Message(role="user", content=prompt, agent=agent))
            return built

        messages = build_messages(include_system_prompt=include_system)
        # Fallback providers start fresh, so they must still receive the current
        # system instructions even when the primary session has already seen them.
        fallback_messages = build_messages(include_system_prompt=bool(system_prompt))
        return (
            provider, provider_obj, conv, messages, fallback_messages,
            session_id, model, created_conversation, resolved_alias,
            alias_to_set, system_prompt_hash, force_new_session,
        )

    def _discard_failed_prepare(self, conv_id: str,
                                created_conversation: bool) -> bool:
        """Remove a conversation created only for a failed call."""
        if not created_conversation:
            return False
        with self._store_lock():
            self._store.delete(conv_id)
        return True

    def _persist_success(self, conv, cwd: str | None, prompt: str,
                         response: LLMResponse, agent: str,
                         alias: str, alias_to_set: str = "",
                         system_prompt_hash: str = "") -> None:
        with self._store_lock():
            if alias_to_set:
                self._store.set_alias(conv.id, alias_to_set)
                conv.alias = alias_to_set
                alias = alias_to_set
            # 드리프트 체크는 성공 호출에만 저장한다. 실패 호출은 store 원자성을 유지.
            self._check_drift(conv, cwd)
            response_provider = (
                self._registry.get(response.provider) if response.provider else None)
            if (system_prompt_hash and response_provider
                    and response_provider.supports_sessions):
                self._store.set_metadata(
                    conv.id,
                    SYSTEM_PROMPT_HASH_KEY_FMT.format(provider=response.provider),
                    system_prompt_hash)
            self._persist(conv.id, prompt, response, agent, alias=alias)

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
            payload_prompt_tokens=response.tokens.payload_prompt_tokens,
            prompt_tokens_reliable=response.tokens.prompt_tokens_reliable,
            prompt_tokens_source=response.tokens.prompt_tokens_source,
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
            # CLI provider 가 비세션 모드로 동작해도 (예: Windows 의 claude)
            # 히스토리는 CLI 가 소유 — 대화 내용을 저장하지 않는다.
            if p is not None and not getattr(p, "stores_history", True):
                return
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
             strict_model: bool = False,
             reset_on_instruction_change: bool = False,
             fallback: bool = False,
             wall_timeout: int | None = None,
             new_session: bool = False,
             provider_options: dict | None = None,
             ) -> LLMResponse:
        with self._store_lock():
            (provider, provider_obj, conv, messages, fallback_messages,
             session_id, model,
             created_conversation, resolved_alias, alias_to_set,
             system_prompt_hash, force_new_session) = self._prepare(
                prompt, provider, model, conversation_id, owner,
                system_prompt, context_turns, agent, inject_context,
                strict_model, reset_on_instruction_change, alias, cwd,
                new_session)
        conversation_id = conv.id

        # 같은 conversation 동시 호출 직렬화 — 세션 분기/덮어쓰기 방지.
        with self._sync_conversation_lock(conversation_id):
            session_id = self._refresh_session_id(
                conversation_id, provider, provider_obj,
                session_id, force_new_session)
            response = self._invoke_with_fallback(
                provider, messages, fallback_messages, model,
                wall_timeout or timeout, session_id, cwd,
                alias=resolved_alias, resume_by_alias=not force_new_session,
                allow_fallback=fallback, provider_options=provider_options)
            response.conversation_id = conversation_id

            if not response.content:
                if self._discard_failed_prepare(conversation_id,
                                                created_conversation):
                    response.conversation_id = ""
                return response

            self._persist_success(conv, cwd, prompt, response, agent,
                                  resolved_alias, alias_to_set,
                                  system_prompt_hash)
            return response

    def _invoke_with_fallback(self, provider_id: str,
                              messages: list[Message],
                              fallback_messages: list[Message],
                              model: str,
                              timeout: int, session_id: str,
                              cwd: str | None,
                              *, alias: str = "",
                              resume_by_alias: bool = True,
                              allow_fallback: bool = False,
                              provider_options: dict | None = None) -> LLMResponse:
        last_resp: LLMResponse | None = None

        # 1) primary 시도
        p = self._registry.get(provider_id)
        if p:
            resp = _invoke_with_alias(
                p, messages, model=model, timeout=timeout,
                session_id=session_id, cwd=cwd, alias=alias,
                resume_by_alias=resume_by_alias,
                provider_options=provider_options)
            if resp.content:
                return resp
            last_resp = resp
            self._log_provider_failure(provider_id, resp)
        else:
            return LLMResponse(content="", provider=provider_id, model=model,
                               tokens=TokenUsage(),
                               error=f"unknown provider: {provider_id}",
                               error_type="unknown")

        if not allow_fallback:
            if last_resp is not None:
                return last_resp
            return LLMResponse(content="", provider=provider_id, model=model,
                               tokens=TokenUsage(),
                               error="no provider available",
                               error_type="unknown")

        # 2) 명시 fallback 호출에서만 체인 전체에서 primary 제외 provider를 시도
        chain = self._registry.get_fallback_chain()
        primary_type = (last_resp.error_type if last_resp else "") or ""
        for next_id in chain:
            if next_id == provider_id:
                continue
            np = self._registry.get(next_id)
            if not np:
                continue
            logger.warning("[LLM] %s(%s) 실패 → %s fallback",
                           provider_id, primary_type or "no-content", next_id)
            # Fallback은 세션 연속성 포기 — session_id 전달 안 함. alias 유지.
            resp = _invoke_with_alias(
                np, fallback_messages, model="", timeout=timeout,
                session_id="", cwd=cwd, alias=alias,
                resume_by_alias=resume_by_alias,
                provider_options=provider_options)
            if resp.content:
                return resp
            last_resp = resp
            self._log_provider_failure(next_id, resp)

        # 모두 실패: 가장 마지막 실패 사유 보존하여 반환
        if last_resp is not None:
            return last_resp
        return LLMResponse(content="", provider=provider_id, model=model,
                           tokens=TokenUsage(),
                           error="no provider available",
                           error_type="unknown")

    @staticmethod
    def _log_provider_failure(provider_id: str, resp: LLMResponse) -> None:
        if resp.error:
            logger.warning("[LLM] %s 실패(%s): %s",
                           provider_id, resp.error_type or "?",
                           resp.error[:200])

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
                         strict_model: bool = False,
                         reset_on_instruction_change: bool = False,
                         fallback: bool = False,
                         wall_timeout: int | None = None,
                         new_session: bool = False,
                         provider_options: dict | None = None,
                         ) -> LLMResponse:
        with self._store_lock():
            (provider, provider_obj, conv, messages, fallback_messages,
             session_id, model,
             created_conversation, resolved_alias, alias_to_set,
             system_prompt_hash, force_new_session) = self._prepare(
                prompt, provider, model, conversation_id, owner,
                system_prompt, context_turns, agent, inject_context,
                strict_model, reset_on_instruction_change, alias, cwd,
                new_session)
        conversation_id = conv.id

        # 같은 conversation 동시 호출 직렬화 — 세션 분기/덮어쓰기 방지.
        async with self._async_conversation_lock(conversation_id):
            session_id = self._refresh_session_id(
                conversation_id, provider, provider_obj,
                session_id, force_new_session)
            response = await self._invoke_async_with_fallback(
                provider, messages, fallback_messages, model,
                wall_timeout or timeout, session_id, cwd,
                alias=resolved_alias, resume_by_alias=not force_new_session,
                allow_fallback=fallback, provider_options=provider_options)
            response.conversation_id = conversation_id

            if not response.content:
                if self._discard_failed_prepare(conversation_id,
                                                created_conversation):
                    response.conversation_id = ""
                return response

            # 저장은 sync이므로 스레드로 이전 (SQLite 등 blocking 방지)
            await asyncio.to_thread(self._persist_success, conv, cwd, prompt,
                                    response, agent, resolved_alias,
                                    alias_to_set, system_prompt_hash)
            return response

    async def _invoke_async_with_fallback(self, provider_id: str,
                                          messages: list[Message],
                                          fallback_messages: list[Message],
                                          model: str,
                                          timeout: int, session_id: str,
                                          cwd: str | None,
                                          *, alias: str = "",
                                          resume_by_alias: bool = True,
                                          allow_fallback: bool = False,
                              provider_options: dict | None = None) -> LLMResponse:
        last_resp: LLMResponse | None = None

        p = self._registry.get(provider_id)
        if p:
            resp = await _invoke_async_with_alias(
                p, messages, model=model, timeout=timeout,
                session_id=session_id, cwd=cwd, alias=alias,
                resume_by_alias=resume_by_alias,
                provider_options=provider_options)
            if resp.content:
                return resp
            last_resp = resp
            self._log_provider_failure(provider_id, resp)
        else:
            return LLMResponse(content="", provider=provider_id, model=model,
                               tokens=TokenUsage(),
                               error=f"unknown provider: {provider_id}",
                               error_type="unknown")

        if not allow_fallback:
            if last_resp is not None:
                return last_resp
            return LLMResponse(content="", provider=provider_id, model=model,
                               tokens=TokenUsage(),
                               error="no provider available",
                               error_type="unknown")

        chain = self._registry.get_fallback_chain()
        primary_type = (last_resp.error_type if last_resp else "") or ""
        for next_id in chain:
            if next_id == provider_id:
                continue
            np = self._registry.get(next_id)
            if not np:
                continue
            logger.warning("[LLM] %s(%s) 실패 → %s fallback (async)",
                           provider_id, primary_type or "no-content", next_id)
            resp = await _invoke_async_with_alias(
                np, fallback_messages, model="", timeout=timeout,
                session_id="", cwd=cwd, alias=alias,
                resume_by_alias=resume_by_alias,
                provider_options=provider_options)
            if resp.content:
                return resp
            last_resp = resp
            self._log_provider_failure(next_id, resp)

        if last_resp is not None:
            return last_resp
        return LLMResponse(content="", provider=provider_id, model=model,
                           tokens=TokenUsage(),
                           error="no provider available",
                           error_type="unknown")

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
                          strict_model: bool = False,
                          reset_on_instruction_change: bool = False,
                          fallback: bool = False,
                          idle_timeout: int | None = None,
                          wall_timeout: int | None = None,
                          new_session: bool = False,
                          provider_options: dict | None = None,
                          ) -> AsyncIterator[StreamChunk]:
        """스트리밍 호출. 청크를 yield하면서 응답을 누적하고 완료 시 저장.

        yield type:
          - "text" | "thinking" | "tool_use" | "tool_result" | "event" | "error"
          - "done" — 마지막 청크. content(누적), session_id, usage 포함.

        fallback=True는 첫 출력 전 실패에만 다른 provider를 시도한다.
        text/tool/event 등 어떤 출력이라도 나간 뒤 실패하면 provider를 바꾸지 않는다.
        """
        with self._store_lock():
            (provider, provider_obj, conv, messages, fallback_messages,
             session_id, model, created_conversation, resolved_alias, alias_to_set,
             system_prompt_hash, force_new_session) = self._prepare(
                prompt, provider, model, conversation_id, owner,
                system_prompt, context_turns, agent, inject_context,
                strict_model, reset_on_instruction_change, alias, cwd,
                new_session)
        conversation_id = conv.id

        if provider_obj is None:
            self._discard_failed_prepare(conversation_id, created_conversation)
            yield make_error_chunk(f"unknown provider: {provider}",
                                   provider=provider)
            yield StreamChunk(type="done", content="",
                              data={"provider": provider, "model": model,
                                    "latency_ms": 0})
            return

        # 같은 conversation 동시 호출 직렬화 — 세션 분기/덮어쓰기 방지.
        # 스트림 소비가 끝날 때까지 같은 conversation 의 다른 호출은 대기한다.
        async with self._async_conversation_lock(conversation_id):
            session_id = self._refresh_session_id(
                conversation_id, provider, provider_obj,
                session_id, force_new_session)
            attempts = [
                (provider, provider_obj, messages, model, session_id,
                 not force_new_session)
            ]
            if fallback:
                for next_id in self._registry.get_fallback_chain():
                    if next_id == provider:
                        continue
                    next_provider = self._registry.get(next_id)
                    if next_provider is None:
                        continue
                    attempts.append(
                        (next_id, next_provider, fallback_messages, "", "",
                         not force_new_session))

            last_error: StreamChunk | None = None
            for attempt_index, (current_id, current_provider, current_messages,
                                current_model, current_sid,
                                resume_by_alias) in enumerate(attempts):
                text_parts: list[str] = []
                final_sid = current_sid
                final_usage = TokenUsage()
                latency_ms = 0
                saw_error = False
                emitted_output = False

                try:
                    async for raw_chunk in _stream_with_alias(
                            current_provider, current_messages,
                            model=current_model, timeout=timeout,
                            session_id=current_sid, cwd=cwd,
                            alias=resolved_alias,
                            idle_timeout=idle_timeout,
                            wall_timeout=wall_timeout,
                            resume_by_alias=resume_by_alias,
                            provider_options=provider_options):
                        if raw_chunk.session_id:
                            final_sid = raw_chunk.session_id

                        if raw_chunk.type == "done":
                            if raw_chunk.content and not text_parts:
                                text_parts.append(raw_chunk.content)
                            if raw_chunk.usage is not None:
                                final_usage = raw_chunk.usage
                            latency_ms = int(
                                raw_chunk.data.get("latency_ms") or 0)
                            break

                        if raw_chunk.type == "error":
                            saw_error = True
                            err_chunk = standardize_error_chunk(
                                raw_chunk, provider=current_id)
                            last_error = err_chunk
                            if emitted_output:
                                yield err_chunk
                            continue

                        emitted_output = True
                        if raw_chunk.type == "text":
                            text_parts.append(raw_chunk.content)
                        yield raw_chunk
                except Exception as exc:
                    saw_error = True
                    err_chunk = make_error_chunk(str(exc), provider=current_id)
                    last_error = err_chunk
                    if emitted_output:
                        yield err_chunk

                full_content = "".join(text_parts)
                has_more_attempts = attempt_index < len(attempts) - 1

                if saw_error:
                    if emitted_output:
                        self._discard_failed_prepare(
                            conversation_id, created_conversation)
                        yield StreamChunk(
                            type="done", content=full_content,
                            session_id=final_sid, usage=final_usage,
                            data={"provider": current_id,
                                  "model": current_model,
                                  "latency_ms": latency_ms, "error": True})
                        return
                    if fallback and has_more_attempts:
                        logger.warning(
                            "[LLM] %s stream failed before output → fallback",
                            current_id)
                        continue
                    self._discard_failed_prepare(
                        conversation_id, created_conversation)
                    if last_error is not None:
                        yield last_error
                    yield StreamChunk(
                        type="done", content="", session_id=final_sid,
                        usage=final_usage,
                        data={"provider": current_id, "model": current_model,
                              "latency_ms": latency_ms, "error": True})
                    return

                if full_content:
                    synthetic = LLMResponse(
                        content=full_content, provider=current_id,
                        model=current_model, tokens=final_usage,
                        latency_ms=latency_ms, session_id=final_sid,
                        conversation_id=conversation_id)
                    await asyncio.to_thread(
                        self._persist_success, conv, cwd, prompt, synthetic,
                        agent, resolved_alias, alias_to_set,
                        system_prompt_hash)

                    yield StreamChunk(
                        type="done", content=full_content,
                        session_id=final_sid, usage=final_usage,
                        data={"provider": current_id, "model": current_model,
                              "latency_ms": latency_ms,
                              "conversation_id": conversation_id})
                    return

                empty_error = make_error_chunk(
                    "empty stream response", provider=current_id)
                last_error = empty_error
                if fallback and has_more_attempts and not emitted_output:
                    logger.warning(
                        "[LLM] %s stream ended before output → fallback",
                        current_id)
                    continue

                self._discard_failed_prepare(
                    conversation_id, created_conversation)
                yield empty_error
                yield StreamChunk(
                    type="done", content="", session_id=final_sid,
                    usage=final_usage,
                    data={"provider": current_id, "model": current_model,
                          "latency_ms": latency_ms, "error": True})
                return

    # ---------- 메타 API ----------

    def list_providers(self) -> list[dict]:
        return self._registry.list_providers()

    def list_models(self, provider: str = "") -> list[dict]:
        return self._registry.list_models(provider)

    def resolve_model(self, provider: str, model: str = "",
                      *, strict: bool = True) -> str:
        """provider의 모델 selector를 실제 CLI model id로 변환."""
        p = self._registry.get(provider)
        if p is None:
            raise ValueError(f"unknown provider: {provider}")
        return p.resolve_model(model, strict=strict)

    def select_model(self, provider: str, model: str = "",
                     *, strict: bool = True) -> str:
        """`resolve_model()`의 의미가 드러나는 별칭."""
        return self.resolve_model(provider, model, strict=strict)

    def health_check(self, provider: str = "", *,
                     timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False
                     ) -> ProviderHealth | dict[str, ProviderHealth]:
        """Diagnose CLI binary/auth/quota readiness.

        `probe=False` keeps checks cheap and uses provider-specific auth/status
        commands where available. `probe=True` performs a minimal model call to
        catch quota/usage-limit failures before production work starts.
        """
        if provider:
            p = self._registry.get(provider)
            if p is None:
                return ProviderHealth(
                    provider=provider, ok=False, status="unknown_provider",
                    message=f"unknown provider: {provider}",
                    suggested_action="Register the provider before calling health_check().")
            return p.health_check(timeout=timeout, cwd=cwd, probe=probe)

        result: dict[str, ProviderHealth] = {}
        for row in self._registry.list_providers():
            pid = row["id"]
            p = self._registry.get(pid)
            if p:
                result[pid] = p.health_check(
                    timeout=timeout, cwd=cwd, probe=probe)
        return result

    def session_alive(self, provider: str, *, owner: str = "",
                      alias: str = "", conversation_id: str = "",
                      cwd: str | None = None) -> bool | None:
        """저장된 세션이 (전체 LLM 호출 없이) 재개 가능한지 저렴하게 확인.

        반환: ``True`` = 재개 가능 / ``False`` = 세션 없음 또는 죽음(다음 호출이
        새 세션을 자동 발급) / ``None`` = provider 가 판단 불가(예: copilot).

        ``cwd`` 는 claude 처럼 세션 경로가 cwd 로 해시되는 provider 에서 호출 때와
        동일해야 정확하다.
        """
        p = self._registry.get(provider)
        if p is None:
            raise ValueError(f"unknown provider: {provider}")
        with self._store_lock():
            conv = None
            if conversation_id:
                conv = self._store.get(conversation_id)
            elif alias:
                conv = self._store.find_by_alias(owner, alias)
        if conv is None:
            return False  # 추적 중인 세션 없음
        sid = conv.metadata.get(SESSION_KEY_FMT.format(provider=provider), "")
        if not sid:
            return False
        return p.session_alive(sid, cwd=cwd)

    # ---------- capability 제어기 (어느 기능이 어느 provider 에서 되나) ----------

    def capabilities(self, provider: str) -> ProviderCapabilities:
        """provider 가 현재 OS 에서 제공하는 기능 선언. 호출 전 질의용.

        OS 의존: 예컨대 claude 세션은 Windows 에서 ``sessions=False`` (issue #4).
        """
        p = self._registry.get(provider)
        if p is None:
            raise ValueError(f"unknown provider: {provider}")
        notes = ""
        if p.provider_id == "claude" and not p.supports_sessions:
            notes = "Windows: stateless — `-p`+`--resume` 데드락 회피 (issue #4)"
        return p.capabilities(capability_notes=notes)

    def supports(self, provider: str, feature: str) -> bool:
        """기능/옵션 지원 여부 (예: supports('codex','lean') → False)."""
        return self.capabilities(provider).supports(feature)

    def capability_matrix(self) -> dict[str, dict]:
        """등록된 모든 provider 의 capability 표 (claude vs codex vs ... 비교)."""
        matrix: dict[str, dict] = {}
        for row in self._registry.list_providers():
            pid = row["id"]
            try:
                matrix[pid] = self.capabilities(pid).to_dict()
            except ValueError:
                continue
        return matrix

    def pin_context(self, context: str, *, provider: str = "", owner: str = "",
                    alias: str = "", cwd: str | None = None, model: str = "",
                    separator: str = "\n\n---\n",
                    **chat_kwargs) -> "ContextSession":
        """큰 컨텍스트(예: 5만 자 전사록)를 1회 주입하고 그 위에서 여러 질의.

        반환된 ``ContextSession`` 은 ``refine()``(이어가기)·``fork()``(독립 변형)
        를 제공한다. 같은 ``alias`` 로 다시 ``pin_context`` 하면 — 시간이 지났거나
        프로세스를 재시작했어도 — 세션이 살아있으면 전사록 재전송 없이 이어가고,
        죽었으면 자동으로 전사록을 재시드한다. 자세한 동작은 ``ContextSession``.

        라이브러리는 세션 id 만 저장하므로(설계 불변식), 전사록 본문은 호스트가
        들고 이 핸들을 (재)구성한다.
        """
        return ContextSession(
            self, context, provider=provider, owner=owner, alias=alias,
            cwd=cwd, model=model, separator=separator, **chat_kwargs)

    def unsupported_options(self, provider: str,
                            options: dict | None) -> list[str]:
        """provider 가 받지 않는 provider_options 키 목록.

        "claude 는 되는데 codex 는 안 됨" 을 호출 전에 확실히 알려준다 — 빈
        리스트면 전부 지원. (실제 호출 시엔 ``_supported_kwargs`` 가 이 키들을
        조용히 버린다.)
        """
        caps = self.capabilities(provider)
        return sorted(k for k in (options or {}) if k not in caps.options)

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
                "session_providers": _session_providers_from_metadata(
                    c.metadata),
                "updated_at": c.updated_at.isoformat(),
            })
        return rows

    def get_alias_status(self, owner: str, alias: str,
                         cwd: str | None = None) -> dict:
        """Return UI-ready session/instruction status for one alias."""
        current_hashes = _compute_instructions_hashes(cwd)
        conv = self._store.find_by_alias(owner, alias)
        if conv is None:
            return {
                "owner": owner,
                "alias": alias,
                "exists": False,
                "status": "missing",
                "fresh": False,
                "stale": False,
                "conversation_id": "",
                "cwd_hashes": current_hashes,
                "stored_hashes": {},
                "session_providers": [],
                "updated_at": "",
            }

        stored_hashes = dict(conv.metadata.get(DRIFT_KEY, {}) or {})
        if current_hashes and stored_hashes:
            status = "fresh" if current_hashes == stored_hashes else "stale"
        else:
            status = "unknown"
        return {
            "owner": conv.owner,
            "alias": conv.alias,
            "exists": True,
            "status": status,
            "fresh": status == "fresh",
            "stale": status == "stale",
            "conversation_id": conv.id,
            "cwd_hashes": current_hashes,
            "stored_hashes": stored_hashes,
            "session_providers": _session_providers_from_metadata(
                conv.metadata),
            "updated_at": conv.updated_at.isoformat(),
        }

    def clear_session_metadata(self, *, owner: str,
                               alias: str = "",
                               provider: str = "") -> list[dict]:
        """Clear agentcli-owned session handles only.

        This never deletes native Claude/Codex/Copilot session files. It blanks
        `session_id:<provider>` and matching `system_prompt_hash:<provider>` keys
        in the configured store so the next call starts a fresh CLI session.
        """
        with self._store_lock():
            if alias:
                conv = self._store.find_by_alias(owner, alias)
                convs = [conv] if conv is not None else []
            else:
                convs = self._store.list_by_owner(owner, limit=1000)

            rows: list[dict] = []
            for conv in convs:
                keys_to_clear: list[str] = []
                for key, value in conv.metadata.items():
                    if not value:
                        continue
                    if not (key.startswith("session_id:")
                            or key.startswith("system_prompt_hash:")):
                        continue
                    key_provider = key.split(":", 1)[1]
                    if provider and key_provider != provider:
                        continue
                    keys_to_clear.append(key)

                for key in keys_to_clear:
                    self._store.set_metadata(conv.id, key, "")

                if keys_to_clear:
                    rows.append({
                        "owner": conv.owner,
                        "alias": conv.alias,
                        "conversation_id": conv.id,
                        "cleared_keys": keys_to_clear,
                    })
            return rows


class ContextSession:
    """큰 컨텍스트를 1회 주입하고 그 위에서 여러 질의를 던지는 핸들.

    ``LLMClient.pin_context(...)`` 가 생성. 두 가지 방식:

    - ``refine(prompt)`` — **이어가기**: 같은 세션을 resume 하여 이전 답을 본다.
      세션이 살아있으면 전사록을 재전송하지 않고 지시만 보낸다(효율). 시간이
      지났거나/프로세스 재시작/세션 만료로 세션이 죽었으면 ``session_alive`` 로
      판정해 **자동으로 전사록을 재시드**한다 — "잠시 있다가 다시 요청" 대응.
    - ``fork(prompt)`` — **독립 변형**: 매번 전사록을 새 세션에 재시드하여 변형
      끼리 섞이지 않는다(예: 같은 전사록으로 격식 회의록 / 액션아이템 / 캐주얼
      요약을 따로). 효율은 provider 의 프롬프트 캐싱에 의존.

    라이브러리는 세션 id 만 저장하므로(설계 불변식), 전사록 본문은 호스트(이
    객체)가 들고 있다. 나중에 같은 ``alias`` 로 다시 ``pin_context`` 하면 살아
    있으면 resume, 죽었으면 재시드한다.

    각 메서드는 sync/async/stream 3종: ``refine``/``refine_async``/
    ``refine_stream``, ``fork``/``fork_async``/``fork_stream``. 순차 사용 전제.
    """

    def __init__(self, client: "LLMClient", context: str, *,
                 provider: str = "", owner: str = "", alias: str = "",
                 cwd: str | None = None, model: str = "",
                 separator: str = "\n\n---\n", **chat_kwargs):
        if not context:
            raise ValueError("context must be a non-empty string")
        self._client = client
        self._context = context
        self._provider = provider
        self._owner = owner
        self._alias = alias or f"ctx-{uuid.uuid4().hex[:8]}"
        self._cwd = cwd
        self._model = model
        self._sep = separator
        self._chat_kwargs = chat_kwargs
        self._seeded = False
        self._fork_n = 0

    @property
    def alias(self) -> str:
        return self._alias

    def is_alive(self) -> bool | None:
        """pin 된 세션이 현재 재개 가능한지 (LLM 호출 없이)."""
        return self._client.session_alive(
            self._provider, owner=self._owner, alias=self._alias, cwd=self._cwd)

    def _seed_prompt(self, prompt: str) -> str:
        return f"{self._context}{self._sep}{prompt}"

    def _opts(self, alias: str, kw: dict) -> dict:
        opts = dict(provider=self._provider, owner=self._owner, alias=alias,
                    cwd=self._cwd, model=self._model)
        opts.update(self._chat_kwargs)
        opts.update(kw)
        return opts

    def _refine(self, prompt: str) -> tuple[str, dict]:
        """세션 생존 여부로 전사록 재시드 결정. ``(prompt, extra_kwargs)`` 반환.

        살아있음(True) → 지시만(전사록 이미 세션에 있음), resume. 죽음/없음(False)
        → 전사록 재시드 + ``new_session=True``(죽은 sid 에 의존하지 않고 새 세션을
        명시적으로 시작). 판단불가(None, 예: copilot) → 이 프로세스에서 시드한 적
        있으면 신뢰(resume), 아니면 안전하게 재시드.
        """
        alive = self.is_alive()
        need_seed = (alive is False) or (alive is None and not self._seeded)
        self._seeded = True
        if need_seed:
            return self._seed_prompt(prompt), {"new_session": True}
        return prompt, {}

    # ---- refine: 이어가기 (resume; 죽었으면 새 세션에 재시드) ----
    def refine(self, prompt: str, **kw):
        p, extra = self._refine(prompt)
        return self._client.chat(p, **self._opts(self._alias, {**extra, **kw}))

    async def refine_async(self, prompt: str, **kw):
        p, extra = self._refine(prompt)
        return await self._client.chat_async(
            p, **self._opts(self._alias, {**extra, **kw}))

    def refine_stream(self, prompt: str, **kw):
        p, extra = self._refine(prompt)
        return self._client.chat_stream(
            p, **self._opts(self._alias, {**extra, **kw}))

    # ---- fork: 독립 변형 (매번 새 세션에 전사록 재시드) ----
    def _fork_target(self, prompt: str, label: str) -> tuple[str, str]:
        self._fork_n += 1
        return (self._seed_prompt(prompt),
                f"{self._alias}#fork:{label or self._fork_n}")

    def fork(self, prompt: str, *, label: str = "", **kw):
        p, a = self._fork_target(prompt, label)
        return self._client.chat(p, new_session=True, **self._opts(a, kw))

    async def fork_async(self, prompt: str, *, label: str = "", **kw):
        p, a = self._fork_target(prompt, label)
        return await self._client.chat_async(
            p, new_session=True, **self._opts(a, kw))

    def fork_stream(self, prompt: str, *, label: str = "", **kw):
        p, a = self._fork_target(prompt, label)
        return self._client.chat_stream(p, new_session=True, **self._opts(a, kw))

    async def fork_many(self, prompts, *, concurrency: int = 4,
                        labels=None, **kw):
        """여러 독립 변형을 **병렬** 실행 (동시 개수 상한). 결과는 입력 순서대로.

        출력도 큰 복수 결과를 동시에 뽑을 때 — 각 변형은 전사록을 자기 세션에
        재시드(서로 안 섞임)하고, ``concurrency`` 로 동시 서브프로세스 수를
        제한한다(각 ``claude -p`` 가 무겁기 때문). wall-time 은 (상한 내에서)
        가장 느린 변형으로 수렴한다.

        labels 가 없으면 인덱스를 라벨로 써 alias 가 결정적·고유해진다(공유
        카운터 race 회피). 큰 출력을 파일로 흘리려면 대신 ``fork_stream`` 을
        항목별로 쓰거나, lean 을 끄고 에이전트에게 직접 파일을 쓰게 한다.
        """
        prompts = list(prompts)
        if labels is None:
            labels = [str(i) for i in range(len(prompts))]
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(prompt, label):
            async with sem:
                return await self.fork_async(prompt, label=label, **kw)

        return await asyncio.gather(
            *(_one(p, lbl) for p, lbl in zip(prompts, labels)))
