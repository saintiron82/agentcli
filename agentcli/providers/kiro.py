"""Kiro CLI 프로바이더 — ACP(JSON-RPC 2.0 over stdio) 기반 세션·스트리밍.

`kiro-cli acp` 를 호출당 1회 one-shot turn 으로 감싼다:
  initialize → session/new|load → session/prompt → session/update 소비 → stopReason.
세션이 히스토리를 소유하므로 라이브러리는 session_id 만 관리한다.

verified against kiro-cli: (Task 0 spike 에서 고정)
"""
import logging
import shutil

from .base import LLMProvider, run_health_command
from ..types import (ERROR_BINARY_MISSING, ProviderHealth, Message, LLMResponse,
                     StreamChunk, TokenUsage)
from ..utils import build_env

logger = logging.getLogger(__name__)

# 모델 selector 는 resolve_model(비-strict) 로 그대로 통과시킨다.
# 알려진 id 는 Task 0 spike / `kiro-cli chat --list-models` 로 확장 가능.
KIRO_MODELS = [
    {"id": "", "name": "기본", "aliases": ["default"]},
]


class KiroProvider(LLMProvider):
    provider_id = "kiro"
    supports_sessions = True
    supports_streaming = True
    stores_history = False  # 히스토리는 Kiro ACP 세션이 소유

    def __init__(self, *, trust_all: bool = True,
                 trust_tools: list[str] | None = None,
                 model: str = "", agent: str = ""):
        """
        Args:
            trust_all: session/request_permission 을 전부 자동 승인 (dev 기본).
                multi-tenant/untrusted 임베딩 시 False + trust_tools 로 좁힐 것.
            trust_tools: trust_all=False 일 때 자동 승인할 도구 이름 목록.
            model: 기본 모델 selector (호출 인자가 우선).
            agent: kiro agent 이름 (선택).
        """
        self._trust_all = trust_all
        self._trust_tools = set(trust_tools or [])
        self._model = model
        self._agent = agent

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "", cwd: str | None = None) -> LLMResponse:
        """Invoke Kiro CLI. (Task 8 implementation)"""
        raise NotImplementedError("invoke is implemented in Task 8")

    def is_available(self) -> bool:
        return shutil.which("kiro-cli") is not None

    def _find_binary(self) -> str | None:
        return shutil.which("kiro-cli")

    def list_models(self) -> list[dict]:
        return list(KIRO_MODELS)

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        bin_path = shutil.which("kiro-cli")
        if not bin_path:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="binary_missing",
                available=False, auth_ok=False,
                error_type=ERROR_BINARY_MISSING,
                message="kiro-cli not found")
        version_proc = run_health_command([bin_path, "--version"], timeout=timeout)
        version = (version_proc.stdout or version_proc.stderr).strip()
        return ProviderHealth(
            provider=self.provider_id, ok=True, status="ok", available=True,
            binary=bin_path, version=version, auth_ok=None,
            message="kiro-cli available")


def _map_session_update(update: dict, usage: TokenUsage) -> list[StreamChunk]:
    """ACP session/update.update 1개를 정규화 청크로 변환 + usage 누적.

    필드명은 ACP 표준 기준 (Task 0 spike 로 확정). 모르는 변형은 event 로.
    """
    kind = update.get("sessionUpdate", "")
    if kind == "agent_message_chunk":
        text = (update.get("content") or {}).get("text", "")
        return [StreamChunk(type="text", content=text, data=update)] if text else []
    if kind == "agent_thought_chunk":
        text = (update.get("content") or {}).get("text", "")
        return [StreamChunk(type="thinking", content=text, data=update)] if text else []
    if kind == "tool_call":
        return [StreamChunk(type="tool_use", data=update)]
    if kind == "tool_call_update":
        return [StreamChunk(type="tool_result", data=update)]
    if kind == "usage_update":
        used = int(update.get("used") or 0)
        usage.prompt_tokens = used
        usage.total_tokens = used + usage.completion_tokens
        usage.prompt_tokens_reliable = False
        usage.prompt_tokens_source = "kiro_cli_reported"
        return []
    return [StreamChunk(type="event", data=update)]
