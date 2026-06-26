"""멀티 AI 모델 시스템 — 공통 데이터 타입."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # prompt 토큰 중 캐시 히트로 처리된 수 (Codex cached_input_tokens, Anthropic cache_read).
    # 일반적으로 단가가 1/10 수준이므로 비용 계산 시 분리 필요.
    cached_tokens: int = 0
    # agentcli가 provider CLI에 직접 넘긴 prompt 문자열의 가벼운 추정치.
    # provider CLI가 내부 agent 컨텍스트를 더하거나 일부 usage만 공개할 수 있어
    # prompt_tokens와 별도로 보관한다.
    payload_prompt_tokens: int = 0
    prompt_tokens_reliable: bool = True
    prompt_tokens_source: str = "provider_reported"


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)
    agent: str = ""  # 메시지 작성자 에이전트 ID


@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    tokens: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    raw_stderr: str = ""
    conversation_id: str = ""
    session_id: str = ""  # provider 측 세션 ID (supports_sessions=True 시 발급/재사용)
    # 실패 사유 (content가 비어 있을 때 채워짐).
    #   error: 사용자 가독성 메시지
    #   error_type: "usage_limit" | "auth" | "network" | "timeout" | "unknown"
    error: str = ""
    error_type: str = ""
    exit_code: int | None = None
    recoverable: bool = False
    suggested_action: str = ""

    def __post_init__(self) -> None:
        if self.error and not self.error_type:
            self.error_type = classify_error(self.error)
        if self.error_type:
            self.recoverable = is_recoverable_error(self.error_type)
            if not self.suggested_action:
                self.suggested_action = suggested_action_for_error(
                    self.error_type, self.provider)


# 에러 타입 상수
ERROR_USAGE_LIMIT = "usage_limit"
ERROR_AUTH = "auth"
ERROR_NETWORK = "network"
ERROR_TIMEOUT = "timeout"
ERROR_BINARY_MISSING = "binary_missing"
ERROR_STREAM_UNSUPPORTED = "stream_unsupported"
ERROR_UNKNOWN = "unknown"


def classify_error(message: str) -> str:
    """에러 메시지를 휴리스틱 분류. primary/fallback 실패 패턴 구분용."""
    if not message:
        return ""
    low = message.lower()
    if any(k in low for k in (
        "command not found", "no such file or directory",
        "cli not found", "binary not found")):
        return ERROR_BINARY_MISSING
    if any(k in low for k in (
        "usage limit", "rate limit", "quota", "credits", "upgrade to pro",
        "too many requests", "429")):
        return ERROR_USAGE_LIMIT
    if any(k in low for k in (
        "unauthorized", "401", "invalid api key", "not authenticated",
        "authentication", "forbidden", "403", "not logged in",
        "login required", "please login", "please log in", "sign in",
        "not signed in")):
        return ERROR_AUTH
    if any(k in low for k in (
        "timeout", "timed out", "deadline")):
        return ERROR_TIMEOUT
    if any(k in low for k in (
        "network", "connection", "dns", "unreachable", "refused")):
        return ERROR_NETWORK
    return ERROR_UNKNOWN


def is_recoverable_error(error_type: str) -> bool:
    """Whether retry/fallback can plausibly recover without user setup."""
    return error_type in {
        ERROR_USAGE_LIMIT,
        ERROR_NETWORK,
        ERROR_TIMEOUT,
        ERROR_STREAM_UNSUPPORTED,
    }


def suggested_action_for_error(error_type: str, provider: str = "") -> str:
    provider = provider or "provider"
    if error_type == ERROR_BINARY_MISSING:
        return f"Install the {provider} CLI and ensure it is on PATH."
    if error_type == ERROR_AUTH:
        if provider == "claude":
            return "Run `claude auth login` and retry."
        if provider == "codex":
            return "Run `codex login` or configure OPENAI_API_KEY, then retry."
        if provider == "copilot":
            return "Run `copilot login` or configure a supported GitHub token."
        return "Authenticate the provider CLI and retry."
    if error_type == ERROR_USAGE_LIMIT:
        return "Wait for quota reset or switch to another provider/model."
    if error_type == ERROR_NETWORK:
        return "Check network connectivity and retry."
    if error_type == ERROR_TIMEOUT:
        return "Increase wall_timeout/idle_timeout or simplify the task."
    if error_type == ERROR_STREAM_UNSUPPORTED:
        return "Use chat_async(..., fallback=True), or retry streaming with another provider."
    return "Inspect raw_stderr/logs and retry after fixing the provider issue."


_PUBLIC_REDACTIONS = (
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    (re.compile(r"\bpypi-[A-Za-z0-9_-]+"), "pypi-[redacted]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]+"), "gh[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]+"), "sk-[redacted]"),
)


def _redact_public_text(value: str) -> str:
    """Redact common local identifiers/tokens before showing health in UI."""
    if not value:
        return ""
    text = str(value)
    try:
        home = str(Path.home())
        if home and home in text:
            text = text.replace(home, "~")
    except RuntimeError:
        pass
    for pattern, replacement in _PUBLIC_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class ProviderHealth:
    provider: str
    ok: bool
    status: str
    available: bool = False
    binary: str = ""
    version: str = ""
    auth_ok: bool | None = None
    error_type: str = ""
    message: str = ""
    suggested_action: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if self.message and not self.error_type and not self.ok:
            self.error_type = classify_error(self.message)
        if self.error_type and not self.suggested_action:
            self.suggested_action = suggested_action_for_error(
                self.error_type, self.provider)

    def public_dict(self) -> dict:
        """Return a UI/log-safe representation without raw CLI payloads."""
        return {
            "provider": self.provider,
            "ok": self.ok,
            "status": self.status,
            "available": self.available,
            "binary": Path(self.binary).name if self.binary else "",
            "version": _redact_public_text(self.version),
            "auth_ok": self.auth_ok,
            "error_type": self.error_type,
            "message": _redact_public_text(self.message),
            "suggested_action": _redact_public_text(self.suggested_action),
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class ProviderCapabilities:
    """한 provider 가 (현재 OS 에서) 실제로 제공하는 기능 선언.

    "이 기능이 이 provider 에서 되나?" 를 호출 전에 확실히 알기 위한 제어기.
    provider 마다, 그리고 OS 마다 다르다 (예: claude 세션은 Windows 에서 False).
    ``options`` 는 그 provider 의 호출이 받는 ``provider_options`` 키 집합 —
    여기 없는 키는 ``_supported_kwargs`` 가 조용히 버린다.
    """
    provider: str
    sessions: bool            # 세션 resume 지원 (claude 는 Windows 에서 False)
    streaming: bool           # 증분 스트리밍
    token_streaming: bool     # 토큰 단위 델타 (False = 메시지 블록 단위)
    session_recovery: bool    # 죽은 세션 자동 재개
    session_liveness: bool    # session_alive 가 bool 반환 (None=미지원 아님)
    options: frozenset        # 받는 per-call provider_options 키
    notes: str = ""           # OS 등 단서
    debug: bool = False        # debug 계측(청크 타임라인/trace) 지원

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "sessions": self.sessions,
            "streaming": self.streaming,
            "token_streaming": self.token_streaming,
            "session_recovery": self.session_recovery,
            "session_liveness": self.session_liveness,
            "debug": self.debug,
            "options": sorted(self.options),
            "notes": self.notes,
        }

    def supports(self, feature: str) -> bool:
        """기능 플래그 이름 또는 옵션 키 이름으로 지원 여부 질의."""
        if feature in self.options:
            return True
        return bool(getattr(self, feature, False)) if hasattr(self, feature) else False


@dataclass
class Conversation:
    id: str
    owner: str
    provider: str
    model: str = ""
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)
    # alias: 사람 읽는 이름. (owner, alias) 조합이 유일하게 한 conversation을 가리킴.
    # 예: owner="team", alias="bull-analyst" → 팀의 bull 분석가 에이전트 세션.
    alias: str = ""


# ===== 스트리밍 =====

# 정규화된 스트림 청크 타입.
# - "text": 증분 텍스트 조각 (content에 담김)
# - "tool_use": 에이전트의 도구 호출 이벤트 (data에 원본)
# - "tool_result": 도구 결과 (data에 원본)
# - "thinking": 사고 과정 (content에 담김, 일부 provider만)
# - "event": 기타 정규화되지 않은 이벤트 (data에 원본 그대로)
# - "error": 스트림 중 발생한 에러
# - "done": 스트림 종료 — session_id/usage 포함
STREAM_CHUNK_TYPES = (
    "text", "tool_use", "tool_result", "thinking", "event", "error", "done")


@dataclass
class StreamChunk:
    type: str
    content: str = ""
    data: dict = field(default_factory=dict)
    session_id: str = ""
    usage: TokenUsage | None = None


def make_error_chunk(content: str = "", *,
                     provider: str = "",
                     error_type: str = "",
                     exit_code: int | None = None,
                     recoverable: bool | None = None,
                     suggested_action: str = "",
                     data: dict | None = None,
                     session_id: str = "") -> StreamChunk:
    """Create a standardized streaming error chunk.

    Error chunks always carry provider, error_type, recoverable,
    suggested_action, and exit_code keys in data.
    """
    payload = dict(data or {})
    provider = provider or str(payload.get("provider") or "")
    raw_exit = payload.get("exit_code", payload.get("returncode", exit_code))
    try:
        normalized_exit = int(raw_exit) if raw_exit is not None else None
    except (TypeError, ValueError):
        normalized_exit = None
    message = content or str(payload.get("message") or payload.get("error") or "")
    error_type = (
        error_type
        or str(payload.get("error_type") or "")
        or classify_error(message)
        or ERROR_UNKNOWN
    )
    if recoverable is None:
        raw_recoverable = payload.get("recoverable")
        recoverable = (
            bool(raw_recoverable)
            if raw_recoverable is not None else is_recoverable_error(error_type)
        )
    action = (
        suggested_action
        or str(payload.get("suggested_action") or "")
        or suggested_action_for_error(error_type, provider)
    )
    payload.update({
        "provider": provider,
        "error_type": error_type,
        "recoverable": recoverable,
        "suggested_action": action,
        "exit_code": normalized_exit,
    })
    return StreamChunk(type="error", content=message, data=payload,
                       session_id=session_id)


def standardize_error_chunk(chunk: StreamChunk, *,
                            provider: str = "") -> StreamChunk:
    """Fill required fields on provider-emitted error chunks."""
    if chunk.type != "error":
        return chunk
    return make_error_chunk(
        chunk.content,
        provider=provider,
        data=chunk.data,
        session_id=chunk.session_id,
    )
