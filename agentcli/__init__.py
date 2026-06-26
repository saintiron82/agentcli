"""agentcli — agentic CLI 멀티 프로바이더 embedding 라이브러리.

Claude Code, Codex, GitHub Copilot CLI 같은 agentic CLI 에이전트를
다른 프로젝트에서 내장 호출하기 위한 통합 레이어.

핵심 원칙:
  - CLI 에이전트의 세션이 히스토리 SSoT (라이브러리는 session_id만 관리)
  - sync/async/streaming 3 가지 호출 모드
  - cwd 명시 제어 (에이전트 세션 파일 위치 관리)
  - 명시 provider/model 선택
  - 토큰·지연 통계
  - 명시 `fallback=True` 호출에서만 provider 전환
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .types import (
    Message, Conversation, LLMResponse, TokenUsage, StreamChunk,
    ProviderHealth, ProviderCapabilities, STREAM_CHUNK_TYPES,
    make_error_chunk, standardize_error_chunk,
)
from .client import LLMClient, ContextSession
from .providers.base import LLMProvider
from .providers.registry import ProviderRegistry, create_default_registry
from .store.base import ConversationStore
from .store.memory import MemoryStore
from .store.sqlite import SQLiteStore, SQLiteSessionStore
from .profile import AgentProfile, AgentRegistry, set_default_client

try:
    __version__ = _pkg_version("agentcli-py")
except PackageNotFoundError:
    try:
        # Pre-rename dist name (installed before the agentcli-py rename).
        __version__ = _pkg_version("agentcli")
    except PackageNotFoundError:
        __version__ = "0.6.4"

__all__ = [
    "__version__",
    "LLMClient", "ContextSession",
    "LLMResponse", "Message", "Conversation", "TokenUsage", "ProviderHealth",
    "ProviderCapabilities",
    "StreamChunk", "STREAM_CHUNK_TYPES",
    "make_error_chunk", "standardize_error_chunk",
    "LLMProvider", "ProviderRegistry", "create_default_registry",
    "ConversationStore", "MemoryStore", "SQLiteStore", "SQLiteSessionStore",
    "AgentProfile", "AgentRegistry", "set_default_client",
]
