"""LLM 프로바이더 추상 인터페이스."""

import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator
from ..types import Message, LLMResponse, StreamChunk


class LLMProvider(ABC):
    provider_id: str = ""
    supports_sessions: bool = False
    supports_streaming: bool = False

    @abstractmethod
    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "", cwd: str | None = None) -> LLMResponse:
        """프로바이더를 동기 호출.

        supports_sessions=True 프로바이더:
          - session_id가 비어 있으면 새 세션 발급(CLI `--session-id`)
          - session_id가 있으면 재개(CLI `--resume`)
          - LLMResponse.session_id에 실제 사용한 값 반환
          - messages는 `[system?, user(prompt)]` 최소 형태 — 히스토리는 세션이 보유

        supports_sessions=False 프로바이더:
          - session_id 무시
          - messages 전체를 프롬프트에 직렬화

        cwd: 서브프로세스 작업 디렉토리. Claude Code는 `~/.claude/projects/<cwd-hash>/`
             에 세션 파일을 쌓으므로 임베딩 프로젝트가 반드시 제어해야 한다.
        """

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> LLMResponse:
        """비동기 호출. 기본 구현은 동기 invoke를 스레드풀에서 실행.

        진짜 async 서브프로세스가 필요한 provider는 이 메서드를 오버라이드.
        """
        return await asyncio.to_thread(
            self.invoke, messages,
            model=model, timeout=timeout,
            session_id=session_id, cwd=cwd)

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> AsyncIterator[StreamChunk]:
        """스트리밍 호출. 기본 구현은 invoke_async 완료 후 한 번에 방출 (비스트리밍 fallback).

        supports_streaming=True 프로바이더는 이 메서드를 오버라이드하여
        증분 청크를 yield 해야 한다.
        """
        resp = await self.invoke_async(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd)
        if resp.content:
            yield StreamChunk(type="text", content=resp.content)
        yield StreamChunk(
            type="done", content=resp.content,
            session_id=resp.session_id, usage=resp.tokens,
            data={"provider": resp.provider, "model": resp.model,
                  "latency_ms": resp.latency_ms})

    @abstractmethod
    def list_models(self) -> list[dict]: ...

    @abstractmethod
    def is_available(self) -> bool: ...
