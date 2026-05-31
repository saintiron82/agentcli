"""LLM 프로바이더 추상 인터페이스."""

import asyncio
import subprocess
from abc import ABC, abstractmethod
from typing import AsyncIterator
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_TIMEOUT, Message,
                     LLMResponse, ProviderHealth, StreamChunk)


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
