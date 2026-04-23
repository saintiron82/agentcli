"""agentcli — agentic CLI 멀티 프로바이더 embedding 라이브러리.

Claude Code, Codex, GitHub Copilot CLI 같은 agentic CLI 에이전트를
다른 프로젝트에서 내장 호출하기 위한 통합 레이어.

핵심 원칙:
  - CLI 에이전트의 세션이 히스토리 SSoT (라이브러리는 session_id만 관리)
  - sync/async/streaming 3 가지 호출 모드
  - cwd 명시 제어 (에이전트 세션 파일 위치 관리)
  - 권한/도구 세밀 제어 (embedding 환경 맞춤)
  - 토큰·지연 통계
  - Fallback 체인 (세션 연속성 없이)
"""

from .types import (
    Message, Conversation, LLMResponse, TokenUsage, StreamChunk,
    STREAM_CHUNK_TYPES,
)
from .client import LLMClient
from .providers.base import LLMProvider
from .providers.registry import ProviderRegistry, create_default_registry
from .store.base import ConversationStore
from .store.memory import MemoryStore
from .store.sqlite import SQLiteStore
from .profile import AgentProfile, AgentRegistry, set_default_client

__all__ = [
    "LLMClient",
    "LLMResponse", "Message", "Conversation", "TokenUsage",
    "StreamChunk", "STREAM_CHUNK_TYPES",
    "LLMProvider", "ProviderRegistry", "create_default_registry",
    "ConversationStore", "MemoryStore", "SQLiteStore",
    "AgentProfile", "AgentRegistry", "set_default_client",
]
