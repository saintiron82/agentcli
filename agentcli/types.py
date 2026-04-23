"""멀티 AI 모델 시스템 — 공통 데이터 타입."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # prompt 토큰 중 캐시 히트로 처리된 수 (Codex cached_input_tokens, Anthropic cache_read).
    # 일반적으로 단가가 1/10 수준이므로 비용 계산 시 분리 필요.
    cached_tokens: int = 0


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
