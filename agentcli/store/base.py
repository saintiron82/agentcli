"""대화 저장소 추상 인터페이스."""

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
                     agent: str = "") -> list[Message]: ...

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
                     latency_ms: int = 0,
                     provider: str = "", model: str = "",
                     agent: str = "", alias: str = "") -> None:
        """AI 호출의 토큰·지연시간을 기록.

        cached_tokens: prompt 중 캐시 히트 수 (비용 계산용).
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
