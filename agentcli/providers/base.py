"""LLM 프로바이더 추상 인터페이스."""

import asyncio
import json
import logging
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_TIMEOUT, Message,
                     LLMResponse, ProviderHealth, StreamChunk, TokenUsage)


@dataclass
class StreamState:
    """Mutable state threaded through the stream_async template method.

    Provider hooks read/write this between events. 공통 키:
      - ``text_parts``: yield 된 text chunk 모음 (최종 done content 조립용)
      - ``final_session_id``: 가장 최근 관찰된 session id (provider 가 갱신)
      - ``final_usage``: 마지막 turn 의 token usage
      - ``extra``: provider 별 추가 상태 (임의 dict)
    """
    text_parts: list[str] = field(default_factory=list)
    final_session_id: str = ""
    final_usage: TokenUsage | None = None
    extra: dict = field(default_factory=dict)


def build_session_prompt(messages: list[Message]) -> str:
    """Build the prompt for CLI session providers.

    Session providers own prior turns, so only durable system instructions and
    the latest user request are injected. Earlier user/assistant messages are
    intentionally excluded.
    """
    if not messages:
        return ""

    system_parts = [
        m.content.strip()
        for m in messages
        if m.role == "system" and m.content.strip()
    ]
    latest_user = ""
    for msg in reversed(messages):
        if msg.role == "user":
            latest_user = msg.content
            break
    if not latest_user:
        latest_user = messages[-1].content

    if not system_parts:
        return latest_user
    system_text = "\n\n".join(system_parts)
    return f"System instructions:\n{system_text}\n\nUser request:\n{latest_user}"


def estimate_payload_prompt_tokens(prompt: str) -> int:
    """Return a cheap estimate for the prompt string agentcli passed to the CLI."""
    text = prompt.strip()
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


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
        증분 청크를 yield 해야 한다. 일반 패턴: provider 가 cmd + 초기
        ``StreamState`` 를 준비한 뒤 ``self._run_stream_template(...)`` 을
        async-for 로 위임하고, ``_dispatch_stream_event`` hook 으로 자기
        JSON 스키마만 해석한다.
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

    # ---- streaming 골격 helper (provider stream_async 가 위임 호출) ----

    async def _run_stream_template(
            self, cmd: list[str], state: "StreamState", *,
            model: str = "",
            cwd: str | None = None,
            timeout: int = 120,
            idle_timeout: int | None = None,
            wall_timeout: int | None = None,
            env: dict | None = None,
            ) -> AsyncIterator[StreamChunk]:
        """3-provider 공통 스트리밍 골격.

        provider stream_async 가 cmd + 초기 state 를 준비한 뒤 이 helper 를
        async-for 로 위임 호출한다. 공통 처리: subprocess 생성 →
        readline + idle/wall timeout → JSON 파싱 → ``_dispatch_stream_event``
        hook → done/error chunk + cleanup.

        ``timeout`` 은 wall-clock deadline 이 아니라 **마지막 청크 이후 idle
        한도** — thinking/tool_use 같은 진행 청크가 들어오면 매번 last_activity
        갱신. ``wall_timeout`` 이 명시되면 절대 deadline 도 함께 적용.
        """
        logger = logging.getLogger(self.__class__.__module__)
        start = time.time()
        proc = None
        timed_out = False
        last_activity = start
        idle_limit = idle_timeout if idle_timeout is not None else timeout
        wall_deadline = start + wall_timeout if wall_timeout else None

        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "stdin": asyncio.subprocess.DEVNULL,
        }
        if env is not None:
            kwargs["env"] = env
        if cwd is not None:
            kwargs["cwd"] = cwd

        try:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
            assert proc.stdout
            while True:
                read_timeout = idle_limit
                if wall_deadline is not None:
                    remaining = wall_deadline - time.time()
                    if remaining <= 0:
                        proc.kill()
                        yield StreamChunk(
                            type="error",
                            content=f"wall timeout: {wall_timeout}s 초과",
                            data={"error_type": "timeout",
                                  "timeout_kind": "wall"})
                        timed_out = True
                        break
                    read_timeout = min(read_timeout, remaining)
                try:
                    line_b = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=read_timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    idle = int(time.time() - last_activity)
                    timeout_kind = (
                        "wall" if wall_deadline is not None
                        and time.time() >= wall_deadline else "idle")
                    logger.error("%s %s timeout (%ds since last chunk)",
                                 self.provider_id, timeout_kind, idle)
                    content = (
                        f"wall timeout: {wall_timeout}s 초과"
                        if timeout_kind == "wall"
                        else f"idle timeout: {idle}s 동안 청크 없음")
                    yield StreamChunk(
                        type="error", content=content,
                        data={"error_type": "timeout",
                              "timeout_kind": timeout_kind})
                    timed_out = True
                    break
                if not line_b:
                    break
                last_activity = time.time()
                line = line_b.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    yield StreamChunk(type="event", data={"raw": line})
                    continue

                async for chunk in self._dispatch_stream_event(evt, state):
                    yield chunk

            if timed_out:
                if proc and proc.returncode is None:
                    await proc.wait()
                return

            rc = await proc.wait()
            if rc != 0 and not state.text_parts:
                err_b = b""
                if proc.stderr:
                    err_b = await proc.stderr.read()
                yield StreamChunk(
                    type="error",
                    content=err_b.decode("utf-8", errors="replace")[:500],
                    data={"returncode": rc})
                return

            yield StreamChunk(
                type="done",
                content="".join(state.text_parts),
                session_id=state.final_session_id,
                usage=state.final_usage,
                data={"provider": self.provider_id, "model": model,
                      "latency_ms": int((time.time() - start) * 1000)})
        except FileNotFoundError:
            yield StreamChunk(
                type="error",
                content=f"{self.provider_id} CLI not found")
        except Exception as exc:  # noqa: BLE001 — 공통 cleanup
            logger.exception("%s stream 예외", self.provider_id)
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            yield StreamChunk(type="error", content=str(exc))

    async def _dispatch_stream_event(
            self, evt: dict, state: "StreamState"
            ) -> AsyncIterator[StreamChunk]:
        """JSON event 한 줄을 정규화된 StreamChunk 로 변환.

        ``_run_stream_template`` 의 핵심 hook. ``state.text_parts`` 에 누적 +
        필요한 모든 chunk yield. session id 갱신은 ``state.final_session_id``,
        최종 usage 는 ``state.final_usage`` 에 저장. Provider 가 override
        하지 않으면 raw event chunk 를 그대로 흘려보낸다.
        """
        yield StreamChunk(type="event", data=evt)

    @abstractmethod
    def list_models(self) -> list[dict]: ...

    def resolve_model(self, model: str = "", *, strict: bool = False) -> str:
        """모델 selector를 provider가 받는 실제 model id로 해석.

        `list_models()` 항목의 `id`, `name`, `aliases`를 모두 selector로 받는다.
        strict=False는 하위 호환을 위해 알 수 없는 문자열을 그대로 통과시킨다.
        """
        selector = (model or "").strip()
        if not selector:
            return ""

        selector_l = selector.lower()
        known: list[str] = []
        for row in self.list_models():
            model_id = str(row.get("id", "")).strip()
            name = str(row.get("name", "")).strip()
            aliases = [str(a).strip() for a in row.get("aliases", [])]
            candidates = [model_id, name, *aliases]
            known.extend(c for c in candidates if c)
            if any(c and c.lower() == selector_l for c in candidates):
                return model_id

        if strict:
            supported = ", ".join(sorted(set(known))) or "(none)"
            raise ValueError(
                f"unsupported model for {self.provider_id}: {selector}. "
                f"supported selectors: {supported}")
        return selector

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        """Return a cheap provider health diagnosis.

        Providers should override this when the CLI exposes auth/version
        commands. The default can only report binary availability via
        `is_available()`.
        """
        available = self.is_available()
        if available:
            return ProviderHealth(
                provider=self.provider_id, ok=True, status="ok",
                available=True, auth_ok=None,
                message="provider reports available")
        return ProviderHealth(
            provider=self.provider_id, ok=False, status="binary_missing",
            available=False, auth_ok=False,
            error_type=ERROR_BINARY_MISSING,
            message=f"{self.provider_id} CLI not found")

    @abstractmethod
    def is_available(self) -> bool: ...


async def run_subprocess_async(
    cmd: list[str], *, timeout: int,
    cwd: str | None = None,
    env: dict | None = None,
    use_stdin_devnull: bool = False,
) -> tuple[bytes, bytes, int, bool]:
    """Run an async subprocess with a single-shot communicate() + timeout.

    3-provider invoke_async 공통 패턴 (proc 생성 → communicate(timeout) →
    timeout 시 kill+wait) 을 한 곳에서 처리한다.

    Returns:
        ``(stdout, stderr, returncode, timed_out)``.
        ``timed_out=True`` 면 ``stdout=b""``, ``stderr`` 는 encoded timeout
        메시지, ``returncode=124``. 이 경우 호출자는 그대로 timeout LLMResponse
        를 작성하면 된다.

    Raises:
        ``FileNotFoundError`` — 호출자가 잡아서 binary_missing 으로 정규화.

    Args:
        cmd: subprocess argv 리스트.
        timeout: 초 단위 wall timeout (``asyncio.wait_for`` 에 직접 전달).
        cwd: subprocess cwd.
        env: subprocess env. ``None`` 이면 부모 환경 상속.
        use_stdin_devnull: True 면 stdin 을 ``/dev/null`` 로 닫는다 (codex/copilot
            처럼 stdin 입력 대기를 막아야 하는 CLI 용).
    """
    kwargs: dict = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if use_stdin_devnull:
        kwargs["stdin"] = asyncio.subprocess.DEVNULL
    if env is not None:
        kwargs["env"] = env
    if cwd is not None:
        kwargs["cwd"] = cwd

    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return b"", f"timeout after {timeout}s".encode(), 124, True
    return stdout_b or b"", stderr_b or b"", proc.returncode or 0, False


def run_health_command(cmd: list[str], *, timeout: int = 10,
                       cwd: str | None = None,
                       env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a health command and normalize timeout into a CompletedProcess."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=cwd, env=env)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, 124,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"timeout after {timeout}s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            cmd, 127, stdout="", stderr="command not found")


def health_from_response(provider: str, resp: LLMResponse,
                         *, binary: str = "", version: str = "") -> ProviderHealth:
    if resp.content:
        return ProviderHealth(
            provider=provider, ok=True, status="ok", available=True,
            binary=binary, version=version, auth_ok=True,
            message="provider probe succeeded",
            exit_code=resp.exit_code)
    status = resp.error_type or "unknown"
    if resp.error_type == ERROR_TIMEOUT:
        status = "timeout"
    return ProviderHealth(
        provider=provider, ok=False, status=status, available=bool(binary),
        binary=binary, version=version,
        auth_ok=False if resp.error_type == ERROR_AUTH else True,
        error_type=resp.error_type,
        message=resp.error or "provider probe failed",
        raw_stderr=resp.raw_stderr,
        exit_code=resp.exit_code)
