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
import os
import time
import stat as stat_module
import threading
from collections import OrderedDict
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from .types import (AccountUsage, Message, LLMResponse, ProviderCapabilities, ProviderHealth,
                    TokenUsage, StreamChunk, make_error_chunk,
                    standardize_error_chunk)
from .store.base import ConversationStore
from .store.locking import reserved_turn, reserve_conversation, session_generation_matches
from .async_utils import complete_in_thread
from .context_session import ContextSession
from .planning import CallPlan
from .coordination import ConversationLease, async_lease
from .deadlines import request_budget, remaining_timeout, bounded_stream
from .auth import AuthProfile, AuthProfileError
from .auth_context import (AUTH_SCOPE_KEY, auth_scoped, current_auth_profile,
                           require_auth_scope, require_auth_method, validate_auth_options,
                           validate_auth_options_async,
                           service_auth_response, SERVICE_AUTH_ACTION)
from .providers.registry import ProviderRegistry, create_default_registry

logger = logging.getLogger(__name__)

SESSION_KEY_FMT = "session_id:{provider}"
SYSTEM_PROMPT_HASH_KEY_FMT = "system_prompt_hash:{provider}"
DRIFT_KEY = "instructions_hashes"
WORKSPACE_KEY = "workspace_root"


def _workspace_root(cwd: str | None) -> str:
    return os.path.normcase(str(Path(cwd or os.getcwd()).expanduser().resolve()))


def _require_conversation_scope(conv, owner: str, cwd: str | None):
    require_auth_scope(conv)
    if conv.owner != owner:
        raise PermissionError("Conversation does not belong to the supplied owner")
    bound = conv.metadata.get(WORKSPACE_KEY)
    if bound and bound != _workspace_root(cwd):
        raise PermissionError("Conversation workspace mismatch; use a different alias")


DRIFT_INSTRUCTION_FILES = (
    "AGENTS.md", "CLAUDE.md", "GUIDE.md", "AGENTS.override.md")

# mtime 기반 해시 캐시 — 파일이 변경되지 않으면 재해시하지 않음.
# {파일 절대경로: (mtime, hash16)}
_DRIFT_HASH_CACHE: OrderedDict[str, tuple[tuple[int, int], str]] = OrderedDict()
_DRIFT_CACHE_LOCK = threading.Lock()
_DRIFT_CACHE_LIMIT = 1024


def _compute_instructions_hashes(cwd: str | None, *, refresh: bool = False) -> dict:
    """cwd의 agent 지시 파일들 해시를 dict로 반환.

    (mtime_ns, size) 기반 캐시: 둘 다 이전과 같으면 재해시 스킵 → I/O 0.
    mtime float 하나만 보면 Windows 에서 변경이 은폐된다 — 파일 시각이
    ~15.6ms 시스템 틱 단위라 같은 틱 안의 연속 수정이 같은 mtime 을 받는다
    (#65 Windows CI 도입에서 실측). size 를 함께 보면 그 창이 "같은 틱 +
    같은 길이" 로 줄어든다.
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
        try:
            stat = f.stat()
            if not stat_module.S_ISREG(stat.st_mode):
                continue
            key = str(f.absolute())
            sig = (stat.st_mtime_ns, stat.st_size)
            with _DRIFT_CACHE_LOCK:
                cached = _DRIFT_HASH_CACHE.get(key)
                if cached is not None:
                    _DRIFT_HASH_CACHE.move_to_end(key)
            if not refresh and cached and cached[0] == sig:
                result[fname] = cached[1]
            else:
                digest = hashlib.sha256()
                with f.open("rb") as source:
                    for chunk in iter(lambda: source.read(65536), b""):
                        digest.update(chunk)
                h = digest.hexdigest()[:16]
                with _DRIFT_CACHE_LOCK:
                    _DRIFT_HASH_CACHE[key] = (sig, h)
                    _DRIFT_HASH_CACHE.move_to_end(key)
                    while len(_DRIFT_HASH_CACHE) > _DRIFT_CACHE_LIMIT:
                        _DRIFT_HASH_CACHE.popitem(last=False)
                result[fname] = h
        except OSError:
            with _DRIFT_CACHE_LOCK:
                _DRIFT_HASH_CACHE.pop(str(f.absolute()), None)
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

# These options change execution boundaries and must never be silently dropped.
_SECURITY_OPTIONS = frozenset({
    "permission_mode", "allowed_tools", "disallowed_tools", "sandbox_mode",
    "approval_policy", "mcp_config", "strict_mcp_config", "lean", "isolated",
    "env", "trust_all", "trust_tools", "allow_file_reads", "allow_file_writes",
    "allow_all_tools", "available_tools", "allow_all_paths", "add_dirs", "oauth_token",
})
_RESERVED_OPTIONS = frozenset({
    "messages", "model", "timeout", "session_id", "cwd", "alias", "resume_by_alias",
})


def _validate_provider_options(provider_obj, method_name: str, options: dict | None):
    if not options:
        return
    if _RESERVED_OPTIONS.intersection(options):
        raise ValueError("provider_options cannot override routing/execution arguments")
    unsupported = [name for name in _SECURITY_OPTIONS.intersection(options)
                   if options[name] is not None
                   and not _method_accepts_param(provider_obj, method_name, name)]
    if unsupported:
        raise ValueError(f"Unsupported security options: {sorted(unsupported)}")


def _fallback_preserves_policy(primary, candidate, options: dict | None) -> bool:
    """Do not guess equivalence between different native permission systems."""
    attributes = tuple("_" + key for key in _SECURITY_OPTIONS)
    explicit = bool(options and any(k in _SECURITY_OPTIONS and v is not None
                                    for k, v in options.items()))
    restricted = (
        getattr(primary, "_sandbox_mode", "danger-full-access") != "danger-full-access"
        or getattr(primary, "_permission_mode", "bypassPermissions") != "bypassPermissions"
        or getattr(primary, "_allowed_tools", None) is not None
        or bool(getattr(primary, "_disallowed_tools", None))
        or bool(getattr(primary, "_available_tools", None))
        or getattr(primary, "_allow_all_tools", True) is False
        or getattr(primary, "_allow_all_paths", True) is False
        or getattr(primary, "_trust_all", True) is False
        or getattr(primary, "_allow_file_reads", True) is False
        or getattr(primary, "_allow_file_writes", True) is False
        or getattr(primary, "_approval_policy", None) is not None
        or getattr(primary, "_oauth_token", None) is not None
        or getattr(primary, "_env", None) in ("isolated", "lean")
    )
    if not explicit and not restricted:
        return True
    return type(primary) is type(candidate) and all(
        getattr(primary, name, None) == getattr(candidate, name, None)
        for name in attributes)


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


def _codex_policy(provider_obj, options=None):
    if not provider_obj or provider_obj.provider_id != "codex":
        return None
    if not hasattr(provider_obj, "_sandbox_mode"):
        return None
    options = options or {}
    return [options.get("sandbox_mode") or provider_obj._sandbox_mode,
            options.get("approval_policy") if options.get("approval_policy") is not None
            else provider_obj._approval_policy]


def _supported_kwargs(provider_obj, method_name: str, kwargs: dict) -> dict:
    return {
        key: value
        for key, value in kwargs.items()
        if _method_accepts_param(provider_obj, method_name, key)
    }


def _merge_options(base: dict, provider_options: dict | None) -> dict:
    """provider_options(호출 시점 옵션)를 base kwargs 에 병합 — provider_options 우선.

    실제로 provider 의 메서드가 받는 키만 호출 직전 ``_supported_kwargs`` 가
    걸러낸다. 비보안 미지원 옵션은 무시하되 보안 제어는 검증 단계에서 거부하고
    fallback 정책도 별도로 확인한다."""
    if provider_options:
        base.update(provider_options)
    return base


def _reject_reasoning_conflict(effort, thinking, provider_options):
    """effort/thinking 를 1급 kwarg 와 provider_options 양쪽에 주면 모호 → 거부."""
    if not provider_options:
        return
    for name, val in (("effort", effort), ("thinking", thinking)):
        if val is not None and name in provider_options:
            raise ValueError(
                f"{name} given both as a keyword and in provider_options; "
                f"pass it once")


def _validate_reasoning_levels(effort, thinking):
    """effort/thinking 값이 유효한지 store/prepare 작업 전에 즉시 검증.

    provider.invoke 안쪽(agentcli.reasoning._resolve)에서도 같은 검사를
    하지만, 그 시점엔 이미 _prepare()가 conversation 레코드를 만든 뒤라
    잘못된 값이 예외와 함께 잔여 conversation 을 store 에 남긴다. 여기서
    먼저 걸러 chat/chat_async/chat_stream 이 어떤 store 작업도 하기 전에
    실패하도록 한다 (provider 쪽 검증은 defense in depth 로 유지)."""
    from .reasoning import EFFORT, THINKING
    for name, val, scale in (("effort", effort, EFFORT),
                             ("thinking", thinking, THINKING)):
        if val is not None and val not in scale:
            raise ValueError(
                f"unknown level {val!r}; valid: {', '.join(scale)}")


def _augment_system_prompt_with_schema(system_prompt: str,
                                       output_schema: dict) -> str:
    """output_schema 를 모델에게 선언하는 표준 블록을 system 에 덧붙인다 (#72).

    스키마 선언 없이 검증만 하면 재시도율이 불필요하게 오른다. system 경로로
    가야 claude 에서 캐시 가능한 안정 프리픽스에 앉는다(#51).
    """
    import json as _json
    block = ("반드시 아래 JSON Schema 를 만족하는 JSON 만 출력한다. "
             "마크다운 펜스와 설명 문장을 붙이지 않는다.\n"
             "=== OUTPUT JSON SCHEMA ===\n"
             + _json.dumps(output_schema, ensure_ascii=False))
    return f"{system_prompt}\n\n{block}" if system_prompt else block


def _accumulate_tokens(total: TokenUsage, add: TokenUsage) -> None:
    """스키마 재시도 전 호출의 usage 를 합산 — 시도 횟수만큼 쓴 토큰이 전부
    기록되어야 usage 회계가 참이다 (#72)."""
    total.accumulate(add)


def _validate_schema_retries(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("schema_retries must be a nonnegative integer")


def _corrected_fallback(original, fallback, current):
    return fallback if current is original else fallback + current[len(original):]


def _schema_correction_messages(messages: list[Message], raw: str,
                                errs: list[str]) -> list[Message]:
    """교정 재시도 메시지 — 이전 출력(절두)과 위반 목록을 되먹인다 (#72).

    컨텍스트 블록 방식이라 세션/무세션(new_session) 어느 모드에서든 동일하게
    동작한다. 세션 모드에서는 CLI 히스토리와 일부 중복되지만, 예측 가능성이
    모드별 분기보다 낫다.
    """
    return messages + [
        Message(role="assistant", content=raw[:2000]),
        Message(role="user", content=(
            "직전 응답이 출력 스키마를 위반했다:\n- "
            + "\n- ".join(errs[:10])
            + "\n위반을 고쳐 스키마를 만족하는 JSON 만 다시 출력하라. "
              "설명·펜스 금지.")),
    ]


def _native_alias(provider_obj, alias: str, namespace: str) -> str:
    # Copilot's named sessions are account-global, not keyed by app owner.
    if provider_obj.provider_id == "copilot" and alias and namespace:
        return "agentcli-" + hashlib.sha256(namespace.encode()).hexdigest()[:32]
    return alias


def _invoke_with_alias(provider_obj, messages, *, model, timeout,
                       session_id, cwd, alias, resume_by_alias=True,
                       effort=None, thinking=None,
                       provider_options=None, native_namespace=""):
    _validate_provider_options(provider_obj, "invoke", provider_options)
    try:
        validate_auth_options(provider_obj.provider_id, provider_options)
    except AuthProfileError as exc:
        return service_auth_response(LLMResponse("", provider_obj.provider_id, model,
                                                error=str(exc), error_type="auth"))
    timeout = remaining_timeout(timeout)
    if timeout <= 0:
        return _budget_expired(provider_obj.provider_id, model)
    kwargs = _supported_kwargs(provider_obj, "invoke", _merge_options({
        "model": model,
        "timeout": timeout,
        "session_id": session_id,
        "cwd": cwd,
        "alias": _native_alias(provider_obj, alias, native_namespace),
        "resume_by_alias": resume_by_alias,
        "effort": effort,
        "thinking": thinking,
    }, provider_options))
    result = provider_obj.invoke(messages, **kwargs)
    if remaining_timeout(1) <= 0:
        expired = _budget_expired(provider_obj.provider_id, model)
        expired.tokens, expired.raw_content = result.tokens, result.content
        return expired
    return service_auth_response(result)


def _budget_expired(provider, model):
    return LLMResponse(content="", provider=provider, model=model,
                       error="wall timeout expired", error_type="timeout", exit_code=124)


async def _persist_in_thread(function, *args):
    """Keep the conversation lease until a started store write has finished."""
    return await complete_in_thread(function, *args)


async def _invoke_async_with_alias(provider_obj, messages, *, model, timeout,
                                    session_id, cwd, alias,
                                    resume_by_alias=True,
                                    effort=None, thinking=None,
                                    provider_options=None, native_namespace=""):
    _validate_provider_options(provider_obj, "invoke_async", provider_options)
    try:
        await validate_auth_options_async(provider_obj.provider_id, provider_options)
    except AuthProfileError as exc:
        return service_auth_response(LLMResponse("", provider_obj.provider_id, model,
                                                error=str(exc), error_type="auth"))
    timeout = remaining_timeout(timeout)
    if timeout <= 0:
        return _budget_expired(provider_obj.provider_id, model)
    kwargs = _supported_kwargs(provider_obj, "invoke_async", _merge_options({
        "model": model,
        "timeout": timeout,
        "session_id": session_id,
        "cwd": cwd,
        "alias": _native_alias(provider_obj, alias, native_namespace),
        "resume_by_alias": resume_by_alias,
        "effort": effort,
        "thinking": thinking,
    }, provider_options))
    try:
        # No budget: retain provider-native timeout behavior.
        if remaining_timeout(float("inf")) == float("inf"):
            return service_auth_response(await provider_obj.invoke_async(messages, **kwargs))
        return service_auth_response(await asyncio.wait_for(
            provider_obj.invoke_async(messages, **kwargs), remaining_timeout(float("inf"))))
    except asyncio.TimeoutError:
        return _budget_expired(provider_obj.provider_id, model)


def _stream_with_alias(provider_obj, messages, *, model, timeout,
                        session_id, cwd, alias, idle_timeout=None,
                        wall_timeout=None, resume_by_alias=True,
                        effort=None, thinking=None,
                        provider_options=None, native_namespace="", deadline=None):
    _validate_provider_options(provider_obj, "stream_async", provider_options)
    validate_auth_options(provider_obj.provider_id, provider_options)
    kwargs = _supported_kwargs(provider_obj, "stream_async", _merge_options({
        "model": model,
        "timeout": timeout,
        "session_id": session_id,
        "cwd": cwd,
        "alias": _native_alias(provider_obj, alias, native_namespace),
        "idle_timeout": idle_timeout,
        "wall_timeout": wall_timeout,
        "resume_by_alias": resume_by_alias,
        "effort": effort,
        "thinking": thinking,
    }, provider_options))
    return bounded_stream(provider_obj.stream_async(messages, **kwargs), deadline)


class LLMClient:
    def __init__(self, store: ConversationStore,
                 registry: ProviderRegistry | None = None,
                 fallback_order: list[str] | None = None,
                 *, context_authorizer=None, session_lock_factory=None,
                 auth_profile: AuthProfile | None = None):
        self._store = store
        self._registry = registry or create_default_registry()
        self._context_authorizer = context_authorizer
        self._session_lock_factory = session_lock_factory
        if auth_profile is not None and not isinstance(auth_profile, AuthProfile):
            raise TypeError("auth_profile must be an AuthProfile")
        self._auth_profile = auth_profile
        if fallback_order:
            self._registry.set_fallback_order(fallback_order)

    def _store_lock(self):
        lock = getattr(self._store, "_lock", None)
        return lock if lock is not None else nullcontext()

    def _store_write(self):
        transaction = getattr(self._store, "transaction", None)
        return transaction() if transaction is not None else self._store_lock()

    @property
    def auth_profile(self) -> AuthProfile | None:
        """Immutable service profile; use public_dict() for non-secret identity."""
        return self._auth_profile

    def set_auth_profile(self, profile: AuthProfile):
        """Rotate this client's credentials; in-flight calls retain their snapshot.

        Account/root changes require a different profile/client and conversation.
        The host must ensure a rotation still belongs to the same account.
        """
        if not isinstance(profile, AuthProfile):
            raise TypeError("profile must be an AuthProfile")
        if self._auth_profile is None or profile.binding != self._auth_profile.binding:
            raise AuthProfileError("Rotation must preserve the service profile identity and root")
        if any(profile.method(pid) != self._auth_profile.method(pid)
               for pid in ("claude", "codex", "copilot")):
            raise AuthProfileError("Rotation cannot change authentication methods")
        profile._check_marker()
        self._auth_profile = profile

    def _validate_call_options(self, provider: str, method: str, options):
        if not provider:
            chain = self._registry.get_fallback_chain()
            provider = chain[0] if chain else ""
        selected = self._registry.get(provider)
        if selected is not None:
            validate_auth_options(provider, options)
            if current_auth_profile() is not None and getattr(selected, "_oauth_token", None) is not None:
                raise AuthProfileError("Provider credentials conflict with the service auth profile")
            _validate_provider_options(selected, method, options)

    def _sync_conversation_lock(self, conv_id: str):
        return ConversationLease(self._store, conv_id, self._session_lock_factory)

    def _check_resume_policy(self, conv_id, provider_obj, session_id, options):
        policy = _codex_policy(provider_obj, options)
        if not session_id or policy is None:
            return
        with self._store_lock():
            conv = self._store.get(conv_id)
            previous = conv.metadata.get("codex_execution_policy") if conv else None
        if ((previous is not None and previous != policy)
                or (previous is None and policy != ["danger-full-access", None])):
            raise PermissionError("Codex resume cannot change/verify this execution policy; use new_session=True")

    def _async_conversation_lock(self, conv_id: str):
        return async_lease(self._sync_conversation_lock(conv_id))

    def _locked_prepare(self, *args, method=None, options=None, check_policy=False):
        if method is not None:
            self._validate_call_options(args[1], method, options)
        with self._store_lock():
            plan = self._prepare(*args)
        if check_policy:
            self._check_resume_policy(plan.conv.id, plan.provider_obj, plan.session_id, options)
        return plan

    # ---------- 공용 준비/저장 로직 (sync/async 공유) ----------

    def _check_drift(self, conv, cwd: str | None) -> None:
        """cwd의 AGENTS.md/CLAUDE.md 해시를 계산하여 저장 + 드리프트 로깅."""
        if not cwd:
            return
        new_hashes = _compute_instructions_hashes(cwd)
        prev = conv.metadata.get(DRIFT_KEY)
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
                 new_session: bool = False) -> CallPlan:
        """프로바이더 결정 → conversation resolve → messages 조립.

        Conversation 해석 우선순위:
          1. conversation_id가 명시되면 그것으로 조회/생성
          2. alias가 명시되면 (owner, alias)로 조회/생성
          3. 둘 다 없으면 신규 conversation

        Returns a named CallPlan; lease holders refresh the complete plan.
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

        if conv is not None:
            _require_conversation_scope(conv, owner, cwd)
        # Validate all sources before creating or mutating the target handle.
        if inject_context:
            for ctx in inject_context:
                source = self._store.get(ctx["conversation_id"])
                if source is None:
                    raise LookupError("Injected context conversation is unavailable")
                if source is not None and source.owner != owner:
                    if (self._context_authorizer is None
                            or self._context_authorizer(owner, source) is not True):
                        raise PermissionError("Injected context is not authorized for this owner")

        created_conversation = False
        if conv is None:
            conv = self._store.create(owner, provider, model,
                                       conversation_id=conversation_id,
                                       alias=alias)
            created_conversation = alias_existing is None
            _require_conversation_scope(conv, owner, cwd)
            profile = current_auth_profile()
            if profile is not None:
                self._store.set_metadata(conv.id, AUTH_SCOPE_KEY, profile.binding)
        resolved_alias = conv.alias or alias
        reserve_conversation(self._store, conv)
        require_auth_method(conv, provider)
        alias_to_set = alias if alias and not conv.alias else ""

        is_session = bool(provider_obj and provider_obj.supports_sessions)
        instruction_hashes = _compute_instructions_hashes(
            cwd, refresh=reset_on_instruction_change)

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
            if (DRIFT_KEY in conv.metadata
                    and instruction_hashes != prev_instruction_hashes):
                force_new_session = True
            prev_system_hash = conv.metadata.get(
                SYSTEM_PROMPT_HASH_KEY_FMT.format(provider=provider), "")
            if (SYSTEM_PROMPT_HASH_KEY_FMT.format(provider=provider) in conv.metadata
                    and prev_system_hash != system_prompt_hash):
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
        return CallPlan(
            provider=provider, provider_obj=provider_obj, conv=conv,
            messages=messages, fallback_messages=fallback_messages,
            session_id=session_id, model=model, created_conversation=created_conversation,
            resolved_alias=resolved_alias, alias_to_set=alias_to_set,
            system_prompt_hash=system_prompt_hash, force_new_session=force_new_session,
        )

    def _discard_failed_prepare(self, conv_id: str,
                                created_conversation: bool) -> bool:
        """Remove a conversation created only for a failed call."""
        if not created_conversation:
            return False
        with self._store_write():
            current = self._store.get(conv_id)
            if current is None:
                return True
            if (current.metadata.get("has_successful_turn")
                    or current.metadata.get(WORKSPACE_KEY)
                    or _session_providers_from_metadata(current.metadata)):
                return False
            self._store.delete(conv_id)
        return True

    def _persist_success(self, conv, cwd: str | None, prompt: str,
                         response: LLMResponse, agent: str,
                         alias: str, alias_to_set: str = "",
                         system_prompt_hash: str = "", provider_options=None,
                         force_new_session: bool = False) -> None:
        with self._store_write():
            current = self._store.get(conv.id)
            if current is None:
                raise RuntimeError("Conversation disappeared before persistence")
            self._store.set_metadata(conv.id, "has_successful_turn", True)
            if not session_generation_matches(self._store, current, response.provider):
                self._persist(conv.id, prompt, response, agent, alias=alias,
                              persist_session=False)
                return
            profile = current_auth_profile()
            if profile is not None:
                self._store.set_metadata(conv.id, AUTH_SCOPE_KEY, profile.binding)
                self._store.set_metadata(conv.id, "service_auth_method:" + response.provider,
                                         profile.method(response.provider))
            if not conv.metadata.get(WORKSPACE_KEY):
                self._store.set_metadata(conv.id, WORKSPACE_KEY, _workspace_root(cwd))
            if alias_to_set:
                self._store.set_alias(conv.id, alias_to_set)
                conv.alias = alias_to_set
                alias = alias_to_set
            # 드리프트 체크는 성공 호출에만 저장한다. 실패 호출은 store 원자성을 유지.
            self._check_drift(conv, cwd)
            response_provider = (
                self._registry.get(response.provider) if response.provider else None)
            policy = _codex_policy(response_provider, provider_options)
            if policy is not None:
                self._store.set_metadata(conv.id, "codex_execution_policy", policy)
            system_key = SYSTEM_PROMPT_HASH_KEY_FMT.format(provider=response.provider)
            if (response_provider and response_provider.supports_sessions
                    and (system_prompt_hash or force_new_session or system_key not in conv.metadata)):
                self._store.set_metadata(
                    conv.id,
                    SYSTEM_PROMPT_HASH_KEY_FMT.format(provider=response.provider),
                    system_prompt_hash)
            self._persist(conv.id, prompt, response, agent, alias=alias)

    def _persist(self, conv_id: str, prompt: str,
                 response: LLMResponse, agent: str,
                 alias: str = "", persist_session: bool = True) -> None:
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
            if response.session_id and persist_session:
                self._store.set_metadata(
                    conv_id,
                    SESSION_KEY_FMT.format(provider=response.provider),
                    response.session_id)
        else:
            # CLI provider 가 비세션 모드로 동작해도 히스토리는 CLI 가 소유
            # — 대화 내용을 저장하지 않는다.
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

    @reserved_turn
    @auth_scoped
    @request_budget
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
             effort: str | None = None,
             thinking: str | None = None,
             provider_options: dict | None = None,
             output_schema: dict | None = None,
             validator=None,
             schema_retries: int = 1,
             ) -> LLMResponse:
        _validate_schema_retries(schema_retries)
        _reject_reasoning_conflict(effort, thinking, provider_options)
        _validate_reasoning_levels(effort, thinking)
        self._validate_call_options(provider, "invoke", provider_options)
        if output_schema is not None:
            from .schema import assert_supported_schema
            assert_supported_schema(output_schema)   # 재시도 비용 전에 터뜨림
            system_prompt = _augment_system_prompt_with_schema(
                system_prompt, output_schema)
        with self._store_lock():
            plan = self._prepare(
                prompt, provider, model, conversation_id, owner,
                system_prompt, context_turns, agent, inject_context,
                strict_model, reset_on_instruction_change, alias, cwd,
                new_session)
        conversation_id = plan.conv.id
        created_conversation = plan.created_conversation

        # 같은 conversation 동시 호출 직렬화 — 세션 분기/덮어쓰기 방지.
        with self._sync_conversation_lock(conversation_id):
            with self._store_lock():
                plan = self._prepare(
                    prompt, plan.provider, plan.model, conversation_id, owner,
                    system_prompt, context_turns, agent, inject_context,
                    strict_model, reset_on_instruction_change, alias, cwd, new_session)
                created_conversation = created_conversation or plan.created_conversation
            self._check_resume_policy(conversation_id, plan.provider_obj, plan.session_id, provider_options)

            def _dispatch(msgs: list[Message]) -> LLMResponse:
                return self._invoke_with_fallback(
                    plan.provider, msgs, _corrected_fallback(plan.messages, plan.fallback_messages, msgs), plan.model,
                    timeout, plan.session_id, cwd,
                    alias=plan.resolved_alias,
                    resume_by_alias=not plan.force_new_session,
                    allow_fallback=fallback, effort=effort,
                    thinking=thinking, provider_options=provider_options,
                    native_namespace=conversation_id)

            response = self._run_with_output_schema(
                _dispatch, plan.messages, output_schema, validator, schema_retries)
            response.conversation_id = conversation_id

            if not response.content:
                if self._discard_failed_prepare(conversation_id,
                                                created_conversation):
                    response.conversation_id = ""
                return response

            self._persist_success(plan.conv, cwd, prompt, response, agent,
                                  plan.resolved_alias, plan.alias_to_set,
                                  plan.system_prompt_hash, provider_options, plan.force_new_session)
            return response

    @staticmethod
    def _check_output_schema(resp: LLMResponse, output_schema, validator):
        """응답 1건을 파싱·검증 — ``(parsed, 위반목록)``. 통과면 위반이 빈 리스트."""
        from .schema import parse_json_output, validate as _validate
        obj, perr = parse_json_output(resp.content)
        if perr:
            return None, [perr]
        errs: list[str] = []
        if output_schema is not None:
            errs = _validate(obj, output_schema)
        if not errs and validator is not None:
            try:
                if validator(obj) is False:
                    errs = ["validator 가 False 를 반환"]
            except Exception as exc:  # noqa: BLE001 — 위반 사유로 수집
                errs = [f"validator 예외: {type(exc).__name__}: {exc}"]
        return obj, errs

    @staticmethod
    def _finalize_schema_failure(resp: LLMResponse, attempts: int,
                                 raw: str, errs: list[str],
                                 total: TokenUsage) -> LLMResponse:
        resp.raw_content = raw           # 관측용 원문 보존
        resp.content = ""                # 실패 계약: 저장 원자성 유지
        resp.parsed = None
        resp.error = (f"출력 스키마 위반 ({attempts}회 시도): "
                      + "; ".join(errs[:5]))[:500]
        resp.error_type = "schema"
        resp.tokens = total
        return resp

    def _run_with_output_schema(self, dispatch, messages,
                                output_schema, validator,
                                schema_retries: int) -> LLMResponse:
        """출력 계약 루프 (#72): 호출 → 파싱·검증 → 실패 시 교정 재시도.

        스키마 미사용 호출은 dispatch 1회로 기존 경로와 동일하다(무비용).
        전송 실패(content 없음)는 스키마 실패로 둔갑시키지 않고 그대로
        돌려준다 — error_type 이 회복 전략을 결정하기 때문이다.
        """
        if output_schema is None and validator is None:
            return dispatch(messages)
        total = TokenUsage()
        attempt_msgs = messages
        raw, errs, resp = "", [], None
        attempts = schema_retries + 1
        for attempt in range(attempts):
            resp = dispatch(attempt_msgs)
            _accumulate_tokens(total, resp.tokens)
            if not resp.content:
                resp.tokens = total
                return resp
            raw = resp.content
            obj, errs = self._check_output_schema(resp, output_schema,
                                                  validator)
            if not errs:
                resp.parsed = obj
                resp.tokens = total
                return resp
            if attempt < schema_retries:
                attempt_msgs = _schema_correction_messages(messages, raw, errs)
        return self._finalize_schema_failure(resp, attempts, raw, errs, total)

    async def _arun_with_output_schema(self, dispatch, messages,
                                       output_schema, validator,
                                       schema_retries: int) -> LLMResponse:
        """``_run_with_output_schema`` 의 async 쌍둥이 — dispatch 만 await."""
        if output_schema is None and validator is None:
            return await dispatch(messages)
        total = TokenUsage()
        attempt_msgs = messages
        raw, errs, resp = "", [], None
        attempts = schema_retries + 1
        for attempt in range(attempts):
            resp = await dispatch(attempt_msgs)
            _accumulate_tokens(total, resp.tokens)
            if not resp.content:
                resp.tokens = total
                return resp
            raw = resp.content
            obj, errs = self._check_output_schema(resp, output_schema,
                                                  validator)
            if not errs:
                resp.parsed = obj
                resp.tokens = total
                return resp
            if attempt < schema_retries:
                attempt_msgs = _schema_correction_messages(messages, raw, errs)
        return self._finalize_schema_failure(resp, attempts, raw, errs, total)

    def _invoke_with_fallback(self, provider_id: str,
                              messages: list[Message],
                              fallback_messages: list[Message],
                              model: str,
                              timeout: int, session_id: str,
                              cwd: str | None,
                              *, alias: str = "",
                              resume_by_alias: bool = True,
                              allow_fallback: bool = False,
                              effort: str | None = None,
                              thinking: str | None = None,
                              provider_options: dict | None = None,
                              native_namespace: str = "") -> LLMResponse:
        last_resp: LLMResponse | None = None

        # 1) primary 시도
        p = self._registry.get(provider_id)
        if p:
            resp = _invoke_with_alias(
                p, messages, model=model, timeout=timeout,
                session_id=session_id, cwd=cwd, alias=alias,
                resume_by_alias=resume_by_alias,
                effort=effort, thinking=thinking,
                provider_options=provider_options, native_namespace=native_namespace)
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
            if not _fallback_preserves_policy(p, np, provider_options):
                logger.warning("[LLM] fallback blocked: incompatible execution policy")
                if last_resp is not None:
                    last_resp.suggested_action += " Fallback blocked by execution policy."
                continue
            logger.warning("[LLM] %s(%s) 실패 → %s fallback",
                           provider_id, primary_type or "no-content", next_id)
            # Fallback은 세션 연속성 포기 — session_id 전달 안 함. alias 유지.
            resp = _invoke_with_alias(
                np, fallback_messages, model="", timeout=timeout,
                session_id="", cwd=cwd, alias=alias,
                resume_by_alias=resume_by_alias,
                effort=effort, thinking=thinking,
                provider_options=provider_options, native_namespace=native_namespace)
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
            logger.warning("[LLM] %s 실패(%s), exit_code=%s, error_chars=%d",
                           provider_id, resp.error_type or "?",
                           resp.exit_code, len(resp.error))

    # ---------- 비동기 chat ----------

    @reserved_turn
    @auth_scoped
    @request_budget
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
                         effort: str | None = None,
                         thinking: str | None = None,
                         provider_options: dict | None = None,
                         output_schema: dict | None = None,
                         validator=None,
                         schema_retries: int = 1,
                         ) -> LLMResponse:
        _validate_schema_retries(schema_retries)
        _reject_reasoning_conflict(effort, thinking, provider_options)
        _validate_reasoning_levels(effort, thinking)
        if output_schema is not None:
            from .schema import assert_supported_schema
            assert_supported_schema(output_schema)
            system_prompt = _augment_system_prompt_with_schema(
                system_prompt, output_schema)
        plan = await complete_in_thread(
            self._locked_prepare, prompt, provider, model, conversation_id, owner,
            system_prompt, context_turns, agent, inject_context,
            strict_model, reset_on_instruction_change, alias, cwd, new_session,
            method="invoke_async", options=provider_options)
        conversation_id = plan.conv.id
        created_conversation = plan.created_conversation

        # 같은 conversation 동시 호출 직렬화 — 세션 분기/덮어쓰기 방지.
        async with self._async_conversation_lock(conversation_id):
            plan = await complete_in_thread(
                self._locked_prepare, prompt, plan.provider, plan.model, conversation_id, owner,
                system_prompt, context_turns, agent, inject_context,
                strict_model, reset_on_instruction_change, alias, cwd, new_session,
                options=provider_options, check_policy=True)
            created_conversation = created_conversation or plan.created_conversation
            async def _dispatch(msgs: list[Message]) -> LLMResponse:
                return await self._invoke_async_with_fallback(
                    plan.provider, msgs, _corrected_fallback(plan.messages, plan.fallback_messages, msgs), plan.model,
                    timeout, plan.session_id, cwd,
                    alias=plan.resolved_alias,
                    resume_by_alias=not plan.force_new_session,
                    allow_fallback=fallback, effort=effort,
                    thinking=thinking, provider_options=provider_options,
                    native_namespace=conversation_id)

            response = await self._arun_with_output_schema(
                _dispatch, plan.messages, output_schema, validator, schema_retries)
            response.conversation_id = conversation_id

            if not response.content:
                if await complete_in_thread(self._discard_failed_prepare, conversation_id,
                                            created_conversation):
                    response.conversation_id = ""
                return response

            # 저장은 sync이므로 스레드로 이전 (SQLite 등 blocking 방지)
            await _persist_in_thread(self._persist_success, plan.conv, cwd, prompt,
                                    response, agent, plan.resolved_alias,
                                    plan.alias_to_set, plan.system_prompt_hash, provider_options, plan.force_new_session)
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
                                          effort: str | None = None,
                                          thinking: str | None = None,
                                          provider_options: dict | None = None,
                                          native_namespace: str = "") -> LLMResponse:
        last_resp: LLMResponse | None = None

        p = self._registry.get(provider_id)
        if p:
            resp = await _invoke_async_with_alias(
                p, messages, model=model, timeout=timeout,
                session_id=session_id, cwd=cwd, alias=alias,
                resume_by_alias=resume_by_alias,
                effort=effort, thinking=thinking,
                provider_options=provider_options, native_namespace=native_namespace)
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
            if not _fallback_preserves_policy(p, np, provider_options):
                logger.warning("[LLM] fallback blocked: incompatible execution policy")
                if last_resp is not None:
                    last_resp.suggested_action += " Fallback blocked by execution policy."
                continue
            logger.warning("[LLM] %s(%s) 실패 → %s fallback (async)",
                           provider_id, primary_type or "no-content", next_id)
            resp = await _invoke_async_with_alias(
                np, fallback_messages, model="", timeout=timeout,
                session_id="", cwd=cwd, alias=alias,
                resume_by_alias=resume_by_alias,
                effort=effort, thinking=thinking,
                provider_options=provider_options, native_namespace=native_namespace)
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

    def chat_stream(self, prompt: str, *,
                    output_schema: dict | None = None,
                    validator=None,
                    **kwargs) -> AsyncIterator[StreamChunk]:
        """스트리밍 진입점 — 호출 시점 검증 후 실제 제너레이터를 돌려준다.

        일반 함수인 이유: async generator 는 본문이 첫 iteration 까지 실행되지
        않아 잘못된 인자가 조용히 잠복한다. 사용법(``async for ... in
        client.chat_stream(...)``)은 동일하다. 전체 파라미터 목록은
        :meth:`_chat_stream_impl` 참고.

        output_schema/validator (#72) 는 스트리밍에서 지원하지 않는다 —
        검증·교정 재시도는 완결된 응답을 전제한다. chat/chat_async 를 쓴다.
        """
        if output_schema is not None or validator is not None:
            raise ValueError(
                "output_schema/validator 는 스트리밍 미지원 (#72) — "
                "검증·교정 재시도는 완결 응답 전제. chat/chat_async 를 쓴다.")
        return self._chat_stream_impl(prompt, **kwargs)

    @reserved_turn
    @auth_scoped
    async def _chat_stream_impl(self, prompt: str, *,
                                provider: str = "", model: str = "",
                                conversation_id: str = "", owner: str = "",
                                alias: str = "",
                                system_prompt: str = "",
                                context_turns: int = 3,
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
                                effort: str | None = None,
                                thinking: str | None = None,
                                provider_options: dict | None = None,
                                ) -> AsyncIterator[StreamChunk]:
        """스트리밍 호출. 청크를 yield하면서 응답을 누적하고 완료 시 저장.

        yield type:
          - "text" | "thinking" | "tool_use" | "tool_result" | "event" | "error"
          - "done" — 마지막 청크. content(누적), session_id, usage 포함.

        fallback=True는 첫 출력 전 실패에만 다른 provider를 시도한다.
        text/tool/event 등 어떤 출력이라도 나간 뒤 실패하면 provider를 바꾸지 않는다.
        """
        _reject_reasoning_conflict(effort, thinking, provider_options)
        _validate_reasoning_levels(effort, thinking)
        if wall_timeout is not None and wall_timeout < 0:
            raise ValueError("wall_timeout must be nonnegative")
        deadline = time.monotonic() + wall_timeout if wall_timeout is not None else None
        plan = await complete_in_thread(
            self._locked_prepare, prompt, provider, model, conversation_id, owner,
            system_prompt, context_turns, agent, inject_context,
            strict_model, reset_on_instruction_change, alias, cwd, new_session,
            method="stream_async", options=provider_options)
        conversation_id = plan.conv.id
        created_conversation = plan.created_conversation

        if plan.provider_obj is None:
            await complete_in_thread(self._discard_failed_prepare, conversation_id, created_conversation)
            yield make_error_chunk(f"unknown provider: {plan.provider}",
                                   provider=plan.provider)
            yield StreamChunk(type="done", content="",
                              data={"provider": plan.provider, "model": plan.model,
                                    "latency_ms": 0})
            return

        # 같은 conversation 동시 호출 직렬화 — 세션 분기/덮어쓰기 방지.
        # 스트림 소비가 끝날 때까지 같은 conversation 의 다른 호출은 대기한다.
        async with self._async_conversation_lock(conversation_id):
            plan = await complete_in_thread(
                self._locked_prepare, prompt, plan.provider, plan.model, conversation_id, owner,
                system_prompt, context_turns, agent, inject_context,
                strict_model, reset_on_instruction_change, alias, cwd, new_session,
                options=provider_options, check_policy=True)
            created_conversation = created_conversation or plan.created_conversation
            attempts = [
                (plan.provider, plan.provider_obj, plan.messages, plan.model, plan.session_id,
                 not plan.force_new_session)
            ]
            policy_blocked = False
            if fallback:
                for next_id in self._registry.get_fallback_chain():
                    if next_id == plan.provider:
                        continue
                    next_provider = self._registry.get(next_id)
                    if next_provider is None:
                        continue
                    if not _fallback_preserves_policy(plan.provider_obj, next_provider, provider_options):
                        policy_blocked = True
                        logger.warning("[LLM] stream fallback blocked: incompatible execution policy")
                        continue
                    attempts.append(
                        (next_id, next_provider, plan.fallback_messages, "", "",
                         not plan.force_new_session))

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
                current_stream = None

                try:
                    await validate_auth_options_async(current_id, provider_options)
                    current_stream = _stream_with_alias(
                            current_provider, current_messages,
                            model=current_model, timeout=timeout,
                            session_id=current_sid, cwd=cwd,
                            alias=plan.resolved_alias,
                            idle_timeout=idle_timeout,
                            wall_timeout=wall_timeout,
                            resume_by_alias=resume_by_alias,
                            effort=effort, thinking=thinking,
                            provider_options=provider_options,
                            native_namespace=conversation_id, deadline=deadline)
                    async for raw_chunk in current_stream:
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
                            if current_auth_profile() is not None and err_chunk.data.get("error_type") == "auth":
                                err_chunk.data["suggested_action"] = SERVICE_AUTH_ACTION
                            if policy_blocked:
                                err_chunk.data["fallback_blocked"] = True
                            last_error = err_chunk
                            if emitted_output:
                                yield err_chunk
                            continue

                        # 정규화 reasoning event(clamp/미지원 알림)는 요청 해석
                        # 결과를 알릴 뿐 실제 출력이 아니므로 fallback 판단에서
                        # emitted_output 으로 세지 않는다 — 그래도 caller 에게는
                        # 그대로 전달한다.
                        is_reasoning_event = (
                            raw_chunk.type == "event"
                            and "reasoning" in (raw_chunk.data or {}))
                        if not is_reasoning_event:
                            emitted_output = True
                        if raw_chunk.type == "text":
                            text_parts.append(raw_chunk.content)
                        yield raw_chunk
                except AuthProfileError as exc:
                    saw_error = True
                    last_error = make_error_chunk(str(exc), provider=current_id,
                                                  error_type="auth",
                                                  suggested_action=SERVICE_AUTH_ACTION)
                    if emitted_output:
                        yield last_error
                except asyncio.TimeoutError:
                    saw_error = True
                    last_error = make_error_chunk("wall timeout expired", provider=current_id,
                                                  error_type="timeout")
                    if emitted_output:
                        yield last_error
                except Exception as exc:
                    saw_error = True
                    err_chunk = make_error_chunk(str(exc), provider=current_id)
                    last_error = err_chunk
                    if emitted_output:
                        yield err_chunk
                finally:
                    if current_stream is not None:
                        await current_stream.aclose()

                full_content = "".join(text_parts)
                has_more_attempts = attempt_index < len(attempts) - 1

                if saw_error:
                    if emitted_output:
                        await complete_in_thread(self._discard_failed_prepare,
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
                    await complete_in_thread(self._discard_failed_prepare,
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
                    await _persist_in_thread(
                        self._persist_success, plan.conv, cwd, prompt, synthetic,
                        agent, plan.resolved_alias, plan.alias_to_set,
                        plan.system_prompt_hash, provider_options, plan.force_new_session)

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

                await complete_in_thread(self._discard_failed_prepare,
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

    @auth_scoped
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
        def check(p):
            try:
                validate_auth_options(p.provider_id)
                return p.health_check(timeout=timeout, cwd=cwd, probe=probe)
            except AuthProfileError as exc:
                return ProviderHealth(provider=p.provider_id, ok=False, status="auth_required",
                                      available=p.is_available(), auth_ok=False,
                                      error_type="auth", message=str(exc),
                                      suggested_action=SERVICE_AUTH_ACTION)
        if provider:
            p = self._registry.get(provider)
            if p is None:
                return ProviderHealth(
                    provider=provider, ok=False, status="unknown_provider",
                    message=f"unknown provider: {provider}",
                    suggested_action="Register the provider before calling health_check().")
            return check(p)

        result: dict[str, ProviderHealth] = {}
        for row in self._registry.list_providers():
            pid = row["id"]
            p = self._registry.get(pid)
            if p:
                result[pid] = check(p)
        return result

    @auth_scoped
    def get_account_usage(self, provider: str, *, timeout: float = 15,
                          allow_probe: bool = False,
                          oauth_token: str | None = None,
                          credentials_path: str | None = None) -> AccountUsage:
        """Read subscription utilization/reset times, separate from token stats.

        No session/store changes or token refresh. Claude setup-token inference
        probes require explicit ``allow_probe=True``. Unsupported providers/auth
        return a structured status rather than silently querying another account.
        """
        from .account_usage import validate_timeout
        validate_timeout(timeout)
        p = self._registry.get(provider)
        if p is None:
            return AccountUsage(provider=provider, status="unknown_provider",
                                message="Register the provider before querying account usage.")
        if current_auth_profile() is not None:
            if oauth_token is not None or credentials_path is not None:
                raise AuthProfileError("Service usage lookup cannot override the auth profile")
            if provider not in {"claude", "codex", "copilot"}:
                return AccountUsage(provider=provider, status="unsupported",
                                    message="Provider does not support service authentication")
            try:
                validate_auth_options(provider)
            except AuthProfileError as exc:
                return AccountUsage(provider=provider, status="auth_required",
                                    error_type="auth", message=str(exc),
                                    suggested_action=SERVICE_AUTH_ACTION)
        return p.get_account_usage(timeout=timeout, allow_probe=allow_probe,
                                   oauth_token=oauth_token,
                                   credentials_path=credentials_path)

    @auth_scoped
    async def get_account_usage_async(self, provider: str, *, timeout: float = 15,
                                      allow_probe: bool = False,
                                      oauth_token: str | None = None,
                                      credentials_path: str | None = None) -> AccountUsage:
        """Thread-backed async lookup. Cancellation cannot stop an in-flight HTTP request."""
        return await asyncio.to_thread(
            self.get_account_usage, provider, timeout=timeout,
            allow_probe=allow_probe, oauth_token=oauth_token,
            credentials_path=credentials_path)

    @auth_scoped
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
        _require_conversation_scope(conv, owner, cwd)
        sid = conv.metadata.get(SESSION_KEY_FMT.format(provider=provider), "")
        if not sid:
            return False
        return p.session_alive(sid, cwd=cwd)

    # ---------- capability 제어기 (어느 기능이 어느 provider 에서 되나) ----------

    def capabilities(self, provider: str) -> ProviderCapabilities:
        """provider 가 현재 환경에서 제공하는 기능 선언. 호출 전 질의용.

        provider 가 OS 별로 다른 값을 선언하면 그대로 반영된다. 현재 등록된
        네 provider 는 모두 전 플랫폼 동일 (claude 의 Windows 전용 stateless
        가드는 issue #27 에서 제거됐다).
        """
        p = self._registry.get(provider)
        if p is None:
            raise ValueError(f"unknown provider: {provider}")
        return p.capabilities()

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
            convs = self._store.list_by_owner(owner, limit=0)
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

    @auth_scoped
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

        require_auth_scope(conv)
        stored_hashes = dict(conv.metadata.get(DRIFT_KEY, {}) or {})
        if cwd and DRIFT_KEY in conv.metadata:
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

    @auth_scoped
    def clear_session_metadata(self, *, owner: str,
                               alias: str = "",
                               provider: str = "") -> list[dict]:
        """Clear agentcli-owned session handles only.

        This never deletes native Claude/Codex/Copilot session files. It blanks
        `session_id:<provider>` and matching `system_prompt_hash:<provider>` keys
        in the configured store so the next call starts a fresh CLI session.
        """
        with self._store_write():
            if alias:
                conv = self._store.find_by_alias(owner, alias)
                convs = [conv] if conv is not None else []
            else:
                convs = self._store.list_by_owner(owner, limit=0)

            rows: list[dict] = []
            for conv in convs:
                require_auth_scope(conv)
            for conv in convs:
                keys_to_clear: list[str] = []
                selected_providers = ({provider} if provider else
                                      {conv.provider, *self._registry.get_fallback_chain(),
                                       *_session_providers_from_metadata(conv.metadata)})
                for pid in selected_providers - {""}:
                    generation = "session_reset_generation:" + pid
                    self._store.set_metadata(conv.id, generation,
                                             conv.metadata.get(generation, 0) + 1)
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
