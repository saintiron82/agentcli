"""세션 라우팅/사용량 저장소 추상 인터페이스.

`ConversationStore` is not a chat-history requirement for session providers.
Claude/Codex/Copilot keep their own turn history in their native CLI sessions;
agentcli stores only routing metadata (`session_id:<provider>`), aliases,
instruction hashes, and usage rows for those providers.

The message methods exist for future/non-session providers that need library
managed context. Session-capable providers must not persist prompt/response
content through this interface.
"""

from abc import ABC, abstractmethod
from ..types import Conversation, Message


class ConversationStore(ABC):
    @abstractmethod
    def create(self, owner: str, provider: str, model: str = "",
               *, conversation_id: str = "",
               alias: str = "") -> Conversation:
        """Conversation 생성.

        conversation_id가 지정되면 해당 ID로 생성. 이미 존재하면 그대로 반환.
        alias가 지정되면 (owner, alias) 유일성 보장.
        """

    @abstractmethod
    def get(self, conversation_id: str) -> Conversation | None: ...

    @abstractmethod
    def find_by_alias(self, owner: str, alias: str) -> Conversation | None:
        """(owner, alias) 쌍으로 conversation 조회. 없으면 None."""

    @abstractmethod
    def add_message(self, conversation_id: str, message: Message) -> None: ...

    @abstractmethod
    def get_messages(self, conversation_id: str, limit: int = 0,
                     agent: str = "") -> list[Message]:
        """Return library-managed messages.

        For session providers this should normally be empty because the CLI
        session owns history. Non-session providers may use it for context.
        """

    @abstractmethod
    def delete(self, conversation_id: str) -> None: ...

    @abstractmethod
    def list_by_owner(self, owner: str, limit: int = 20) -> list[Conversation]: ...

    @abstractmethod
    def set_metadata(self, conversation_id: str, key: str, value) -> None:
        """Conversation.metadata의 특정 키를 갱신."""

    @abstractmethod
    def set_alias(self, conversation_id: str, alias: str) -> None:
        """Conversation의 alias 변경. (owner, 새 alias) 충돌 시 덮어쓰기."""

    @abstractmethod
    def record_usage(self, conversation_id: str, *,
                     prompt_tokens: int = 0, completion_tokens: int = 0,
                     total_tokens: int = 0, cached_tokens: int = 0,
                     payload_prompt_tokens: int = 0,
                     prompt_tokens_reliable: bool = True,
                     prompt_tokens_source: str = "",
                     latency_ms: int = 0,
                     provider: str = "", model: str = "",
                     agent: str = "", alias: str = "") -> None:
        """AI 호출의 토큰·지연시간을 기록.

        cached_tokens: prompt 중 캐시 히트 수 (비용 계산용).
        payload_prompt_tokens: agentcli가 CLI에 넘긴 prompt 문자열의 추정 토큰 수.
        prompt_tokens_reliable: prompt_tokens가 cross-provider 비교에 안전한지 여부.
        prompt_tokens_source: prompt_tokens 출처.
        alias: 호출 시점의 Conversation alias (에이전트 축 집계용).
        agent: 메시지 작성자 에이전트 (멀티에이전트 한 세션 내 구분).
        """

    @abstractmethod
    def get_token_stats(self, owner: str = "", days: int = 7,
                        *, alias: str = "", provider: str = "",
                        model: str = "", agent: str = "",
                        group_by: str | None = None) -> dict:
        """토큰·비용 통계 조회.

        필터: owner, alias, provider, model, agent (모두 AND).
        group_by: 'provider' | 'model' | 'alias' | 'agent' | 'day' | None.
        """
