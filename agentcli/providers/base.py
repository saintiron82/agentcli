"""LLM 프로바이더 추상 인터페이스."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_TIMEOUT, Message,
                     LLMResponse, ProviderHealth, StreamChunk, TokenUsage)
from ..utils import serialize_messages

# CLI 가 띄운 손자(MCP 서버·hook·node 헬퍼)까지 정리하려면 프로세스 그룹
# 단위로 죽여야 한다. POSIX 에서만 setsid/killpg 가능 — Windows 는 직속 kill.
_POSIX = os.name == "posix"


def _new_session_kwargs() -> dict:
    """spawn 시 새 세션(프로세스 그룹) 분리 kwargs (POSIX 만)."""
    return {"start_new_session": True} if _POSIX else {}


def _kill_process_group(proc) -> None:
    """spawn 된 프로세스의 **그룹 전체**를 SIGKILL — 직속 자식만 죽이면 CLI 가
    띄운 MCP 서버·hook 손자가 좀비로 남아 누적된다 (확인된 좀비 원인).

    ``start_new_session=True`` 로 띄웠으므로 자식 PID == PGID. ``getpgid`` 는
    자식이 먼저 종료하면 race 로 실패하므로 ``proc.pid`` 를 PGID 로 직접 쓴다.
    그룹 kill 이 불가/실패하면 직속 kill 로 폴백.
    """
    pid = getattr(proc, "pid", None)
    if _POSIX and pid:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — 더 이상 할 수 있는 게 없음
        pass


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

    히스토리 3-모드 계약:
      - CLI 네이티브 세션 모드: client 가 ``[system?, user]`` 만 전달 →
        system + 최신 user 요청만 프롬프트가 된다 (CLI 세션이 히스토리 소유).
      - 호스트 주입 모드 (``inject_context``): client 가 중간 메시지를
        명시적으로 담아 보내면 "Context" 블록으로 직렬화되어 전달된다.
      - 미사용 모드: client 가 최신 user 만 전달.

    즉 이 함수는 받은 메시지를 충실히 직렬화할 뿐, 무엇을 담을지는 client
    의 모드 결정이 담당한다.
    """
    if not messages:
        return ""

    system_parts = [
        m.content.strip()
        for m in messages
        if m.role == "system" and m.content.strip()
    ]
    latest_user_msg = None
    for msg in reversed(messages):
        if msg.role == "user":
            latest_user_msg = msg
            break
    if latest_user_msg is None:
        latest_user_msg = messages[-1]
    latest_user = latest_user_msg.content

    context_msgs = [
        m for m in messages
        if m.role != "system" and m is not latest_user_msg
    ]

    parts: list[str] = []
    if system_parts:
        parts.append("System instructions:\n" + "\n\n".join(system_parts))
    if context_msgs:
        parts.append("Context (injected by host application):\n"
                     + serialize_messages(context_msgs))
    if not parts:
        return latest_user
    parts.append(f"User request:\n{latest_user}")
    return "\n\n".join(parts)


def estimate_payload_prompt_tokens(prompt: str) -> int:
    """Return a cheap estimate for the prompt string agentcli passed to the CLI."""
    text = prompt.strip()
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


# ---- optional debug instrumentation (opt-in; zero cost when disabled) ----

def redact_argv(cmd: list[str]) -> list[str]:
    """Return argv with the `-p <prompt>` payload replaced by a length marker.

    Debug 로그/trace 에 50k 짜리 프롬프트 본문이나 민감 내용을 그대로 흘리지
    않도록, ``-p`` 다음 인자만 ``<prompt:N chars>`` 로 치환한다.
    """
    out: list[str] = []
    redact_next = False
    for arg in cmd:
        if redact_next:
            out.append(f"<prompt:{len(arg)} chars>")
            redact_next = False
            continue
        out.append(arg)
        if arg == "-p":
            redact_next = True
    return out


def write_debug_trace(path: str, record: dict) -> None:
    """Append one JSON-Lines trace record to ``path`` (best-effort, zero-dep).

    실패는 경고만 남기고 호출을 막지 않는다 — 디버그 보조 기능이 본 호출을
    깨뜨리면 안 된다.
    """
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "debug trace 기록 실패 (%s): %s", path, exc)


async def _drain_stderr(stream, sink: list[str], logger, provider_id: str) -> None:
    """Concurrently drain a subprocess stderr pipe into ``sink``.

    ``--debug`` 는 stderr 를 대량 출력하는데, 이를 빼주지 않으면 OS 파이프
    버퍼가 차서 subprocess 가 stderr write 에서 블록 → 행이 걸린다. 디버그
    스트리밍에서 이 task 로 stderr 를 계속 읽어 데드락을 막고, 동시에 각 줄을
    DEBUG 로그로 흘린다.
    """
    try:
        while True:
            line_b = await stream.readline()
            if not line_b:
                break
            text = line_b.decode("utf-8", errors="replace").rstrip("\n")
            sink.append(text)
            logger.debug("[debug] %s stderr: %s", provider_id, text)
    except Exception:  # noqa: BLE001 — drain 실패는 본 스트림에 영향 주지 않음
        pass


class LLMProvider(ABC):
    provider_id: str = ""
    supports_sessions: bool = False
    supports_streaming: bool = False
    # False 면 supports_sessions=False 여도 client 가 대화 내용을 messages
    # 테이블에 저장하거나 이전 턴을 프롬프트에 재주입하지 않는다. CLI 가 자체
    # 히스토리를 소유하는 3-provider 는 False. 라이브러리가 컨텍스트를 직접
    # 관리해야 하는 custom 비세션 provider 만 True (기본값).
    stores_history: bool = True

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
            debug: bool = False,
            debug_log_path: str | None = None,
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
        # debug 누적기 — finally 에서 trace 를 쓰려면 try 밖에 선언.
        debug_chunks: list[dict] = []
        debug_stderr: list[str] = []
        stderr_task = None
        idle_limit = idle_timeout if idle_timeout is not None else timeout
        # issue #9: ``wall_timeout=0`` 도 의미 있는 입력 (즉시 만료) 이므로
        # ``if wall_timeout`` (falsy check) 대신 ``is not None`` 명시적 검사.
        wall_deadline = (start + wall_timeout
                         if wall_timeout is not None else None)

        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "stdin": asyncio.subprocess.DEVNULL,
            # 새 세션으로 분리 → 아래 cleanup 이 손자까지 그룹 단위로 죽인다.
            **_new_session_kwargs(),
        }
        if env is not None:
            kwargs["env"] = env
        if cwd is not None:
            kwargs["cwd"] = cwd

        # issue #10: ``except Exception`` 은 ``GeneratorExit`` 을 못 잡아
        # caller 가 ``async for ... break`` / ``aclose()`` 로 일찍 종료할 때
        # subprocess cleanup 이 실행되지 않아 좀비 프로세스가 남는다.
        # ``try/finally`` 로 어떤 종료 경로에서도 그룹 kill 보장.
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
            assert proc.stdout
            if debug:
                logger.info("[debug] %s spawn: %s",
                            self.provider_id, redact_argv(cmd))
                # stderr 동시 드레인 — --debug 의 대량 stderr 로 인한 파이프
                # 데드락 방지 (위 _drain_stderr docstring 참고).
                if proc.stderr is not None:
                    stderr_task = asyncio.create_task(
                        _drain_stderr(proc.stderr, debug_stderr,
                                      logger, self.provider_id))
            while True:
                read_timeout = idle_limit
                if wall_deadline is not None:
                    remaining = wall_deadline - time.time()
                    if remaining <= 0:
                        _kill_process_group(proc)
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
                    _kill_process_group(proc)
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
                if not isinstance(evt, dict):
                    # 객체가 아닌 JSON 라인 — dispatcher 의 evt.get 이
                    # AttributeError 로 스트림을 죽이지 않도록 raw event 처리.
                    yield StreamChunk(type="event", data={"raw": line})
                    continue

                async for chunk in self._dispatch_stream_event(evt, state):
                    if debug:
                        t = time.time() - start
                        entry = {"t": round(t, 3), "type": chunk.type}
                        # Claude --debug 정보는 stderr 가 아니라 stdout event 로
                        # 흘러나오므로, 청크 type 만으론 툴 루프를 못 읽는다.
                        # event 의 내부 type/subtype 과 tool 이름을 함께 기록해
                        # 타임라인이 "tool_use(Bash) → tool_result → ..." 처럼
                        # 진단 가능하게 만든다.
                        data = chunk.data
                        if isinstance(data, dict):
                            inner = data.get("type")
                            if inner and inner != chunk.type:
                                entry["evt"] = inner
                            for k in ("subtype", "name"):
                                if data.get(k):
                                    entry[k] = data[k]
                        debug_chunks.append(entry)
                        logger.info(
                            "[debug] %s +%.2fs %s%s", self.provider_id, t,
                            chunk.type,
                            f" {entry.get('name') or entry.get('evt') or entry.get('subtype') or ''}".rstrip())
                    yield chunk

            if timed_out:
                if proc and proc.returncode is None:
                    await proc.wait()
                return

            rc = await proc.wait()
            if debug and stderr_task is not None:
                try:
                    await stderr_task
                except Exception:  # noqa: BLE001
                    pass
            if rc != 0 and not state.text_parts:
                if debug:
                    # stderr 는 드레인 task 가 이미 소진 → 누적분 사용.
                    err_text = "\n".join(debug_stderr)
                else:
                    err_b = b""
                    if proc.stderr:
                        err_b = await proc.stderr.read()
                    err_text = err_b.decode("utf-8", errors="replace")
                yield StreamChunk(
                    type="error",
                    content=err_text[:500],
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
        except Exception as exc:  # noqa: BLE001 — error chunk 만 yield, cleanup 은 finally
            logger.exception("%s stream 예외", self.provider_id)
            yield StreamChunk(type="error", content=str(exc))
        finally:
            # issue #10: GeneratorExit 포함 모든 종료 경로에서 proc 정리.
            # 직속만이 아니라 그룹 전체를 죽여 MCP/hook 손자까지 reap.
            if proc is not None and proc.returncode is None:
                try:
                    _kill_process_group(proc)
                    await proc.wait()
                except Exception:
                    # cleanup 실패는 silent — 더 이상 할 수 있는 게 없음
                    pass
            # debug: GeneratorExit/timeout/error/done 어떤 경로든 trace 마감.
            if debug:
                if stderr_task is not None and not stderr_task.done():
                    stderr_task.cancel()
                    try:
                        await stderr_task
                    except Exception:  # noqa: BLE001
                        pass
                elapsed_ms = int((time.time() - start) * 1000)
                logger.info(
                    "[debug] %s stream 종료: chunks=%d elapsed=%dms",
                    self.provider_id, len(debug_chunks), elapsed_ms)
                if debug_log_path:
                    write_debug_trace(debug_log_path, {
                        "provider": self.provider_id, "phase": "stream",
                        "model": model, "argv": redact_argv(cmd),
                        "elapsed_ms": elapsed_ms,
                        "chunk_count": len(debug_chunks),
                        "chunks": debug_chunks,
                        "session_id": state.final_session_id,
                        "stderr": "\n".join(debug_stderr)[-20000:],
                    })

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
        # 새 세션 분리 → 타임아웃/취소 시 손자까지 그룹 단위로 reap.
        **_new_session_kwargs(),
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
        # 타임아웃이면 직속 자식이 이미 종료했어도(손자가 파이프를 물고 행)
        # 그룹 전체를 무조건 kill — returncode 가드로 건너뛰면 손자가 좀비로
        # 남는다 (test_process_group 회귀).
        _kill_process_group(proc)
        return b"", f"timeout after {timeout}s".encode(), 124, True
    finally:
        # 호출 task 취소(CancelledError) 포함 모든 종료 경로에서 proc 정리.
        # 직속만이 아니라 그룹 전체 kill — CLI 가 띄운 MCP/hook 손자 좀비 방지.
        if proc.returncode is None:
            try:
                _kill_process_group(proc)
                await proc.wait()
            except Exception:
                pass
    return stdout_b or b"", stderr_b or b"", proc.returncode or 0, False


def run_subprocess_sync(
    cmd: list[str], *, timeout: int,
    cwd: str | None = None,
    env: dict | None = None,
) -> tuple[bytes, bytes, int, bool]:
    """Synchronous subprocess with process-group teardown.

    ``subprocess.run`` 의 타임아웃 정리는 **직속 자식만** kill 하므로, CLI 가
    띄운 MCP 서버·hook 손자가 좀비로 남아 누적된다 (확인된 좀비 원인). 또한
    손자가 stdout 파이프를 물고 있으면 정리 단계의 재-communicate 가 매달릴
    수 있다. 이를 막기 위해 ``Popen(start_new_session=True)`` 로 새 그룹에
    띄우고, 타임아웃/정리 시 ``killpg`` 로 **그룹 전체**를 죽인다. stdin 은
    DEVNULL (일회성 비대화형 호출이므로 stdin 대기 방지).

    Returns: ``(stdout, stderr, returncode, timed_out)`` — ``run_subprocess_async``
    와 동일 계약. timeout 이면 ``(b"", b"timeout...", 124, True)``.

    Raises: ``FileNotFoundError`` — 호출자가 binary_missing 으로 정규화.
    """
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        **_new_session_kwargs(),
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env

    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout)
        return stdout_b or b"", stderr_b or b"", proc.returncode or 0, False
    except subprocess.TimeoutExpired:
        # 그룹 전체 SIGKILL → 손자까지 죽어 파이프 해제 → 재-communicate 가 즉시
        # EOF (직속만 kill 하던 subprocess.run 의 정리-행을 회피).
        _kill_process_group(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        return b"", f"timeout after {timeout}s".encode(), 124, True
    finally:
        if proc.returncode is None:
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                pass


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
