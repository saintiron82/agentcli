"""상주(warm) 모드 — claude 프로세스를 살려둔 채 stdin 으로 질의를 이어 먹인다.

issue #48. ``claude -p`` 는 호출마다 하네스를 새로 부팅해, 최소 프롬프트조차
호출당 6~11s 가 든다(Windows 11 / Claude Code 2.1.220, lean 실측).
``--input-format stream-json`` 으로 프로세스를 한 번만 띄워두면 부팅은 1회뿐이고
이후 질의는 턴 처리 시간만 부담한다. 같은 머신 실측:

    턴1  wall 6.40s   duration_ms 1663   → 부팅 ≈ 4.7s
    턴2  wall 2.69s   duration_ms 2687
    턴3  wall 1.61s   duration_ms 1611
    턴4  wall 1.65s   duration_ms 1652

부팅 후에는 벽시계 ≈ CLI 가 보고한 턴 처리 시간이라 하네스 오버헤드가 사실상
0 이다. TTFT 는 0.8~1.7s 로 관측됐다.

## 범위

이 모듈이 제공하는 것은 **상주 모드로 여는 것과 그 session_id 를 넘겨주는 것**
이다. 열기 · 주고받기 · 닫기, 그리고 ``session_id`` 노출이 전부다::

    s = await open_warm(append_system_prompt=RULES)
    s.session_id                      # ← 서비스에 넘기는 것
    async for chunk in s.stream("질문"):
        ...
    await s.close()

그것을 어떻게 운용할지는 서비스의 일이라 여기 넣지 않는다 — 풀링 · 동시성 배분 ·
오버플로 · liveness 감시 · 자동 재기동 · 재기동 후 잔여 프로세스 탐색이나 종료 ·
그것을 위한 전역 파일이나 DB. agentcli 는 저장소를 스스로 고르지 않고, 다른
프로세스를 조회하거나 죽이지도 않는다.

``/clear`` 도 정책이 아니라 그냥 보내는 메시지다::

    await s.send("/clear")            # 직전 Q&A 를 버린다
    s.session_id                      # ← 새 값으로 바뀐다

## 알아두어야 할 실측 사실

- ``--output-format stream-json`` 은 ``--verbose`` 가 없으면 CLI 가 거부한다
  ("When using --print, --output-format=stream-json requires --verbose").
- ``/clear`` 는 재부팅 없이 컨텍스트를 지우지만 **새 session_id 를 발급**한다.
  응답 본문이 비어 있어 내용으로는 성공을 알 수 없으므로, 확인이 필요하면
  ``session_id`` 가 바뀌었는지로 판단한다. 한 세션을 여러 질의에 돌려 쓰는데
  ``/clear`` 가 먹지 않으면 이전 질의의 컨텍스트가 다음 질의로 샌다.
- ``--append-system-prompt`` 로 넣은 고정 컨텍스트는 ``/clear`` 를 살아남는다.
  "시스템·고정 컨텍스트는 유지, 직전 Q&A 는 미상속"이 한 프로세스에서 성립한다.
  단 기동 시 고정이라 사는 동안 바꿀 수 없다.
- ``--resume`` 이 상주 모드에서도 동작하고 session_id 가 유지된다. 다른
  프로세스가 남긴 세션을 이어받을 수 있다.
- tools 를 켜두면 ``/clear`` 이후 모델이 툴로 헤매 턴당 지연이 3~9배로 뛴다.
  그래서 lean(``--safe-mode --tools ""``) 이 기본값이다.
- 살아 있는 상주 프로세스의 stdin/stdout 에 **다른 프로세스가 다시 붙는 것은
  불가능**하다. 파이프는 그것을 띄운 부모가 소유하고 부모와 함께 죽는다.
  이어갈 수 있는 것은 프로세스가 아니라 세션이다(``resume_session_id``).

## stdin 계약 주의 (issue #27 과의 관계)

#27 의 안전 논거는 "어느 spawn 경로도 CLI 가 읽을 stdin 을 열어두지 않는다"
였고, ``run_subprocess_async`` 기본값까지 그렇게 바꿔 구조적 불변식으로 만들었다.
**상주 모드는 그 불변식을 의도적으로 깨는 유일한 경로**다 — stdin 을 열어둔 채
JSON 메시지를 계속 쓰는 것이 이 기능의 본질이기 때문이다. 그래서 여기서 직접
spawn 하고, issue #4 류 무한 대기의 상한으로 턴별 타임아웃을 둔다.
"""

import asyncio
import json
import platform
import shutil
from typing import AsyncIterator

from ..types import StreamChunk

# 상주 프로세스를 stream-json 입출력 모드로 띄우는 데 필요한 플래그.
# --verbose 는 선택이 아니라 CLI 가 강제한다(모듈 docstring 참고).
_BASE_FLAGS = (
    "--print",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--include-partial-messages",
    "--verbose",
)
# lean: 커스터마이즈(CLAUDE.md/skills/hooks/MCP)와 빌트인 툴을 끊는다.
_LEAN_FLAGS = ("--safe-mode", "--tools", "")


class WarmSessionError(RuntimeError):
    """상주 세션이 더 이상 신뢰할 수 없는 상태 — 호출자는 닫고 다시 연다."""


def _find_claude() -> str | None:
    if platform.system() == "Windows":
        return shutil.which("claude.cmd") or shutil.which("claude")
    return shutil.which("claude")


def build_warm_cmd(*, binary: str,
                   lean: bool = True,
                   permission_mode: str = "bypassPermissions",
                   model: str = "",
                   append_system_prompt: str = "",
                   resume_session_id: str = "",
                   extra_args: tuple[str, ...] = ()) -> list[str]:
    """상주 프로세스 기동 argv."""
    cmd = [binary, *_BASE_FLAGS, "--permission-mode", permission_mode]
    if lean:
        cmd += list(_LEAN_FLAGS)
    if model:
        cmd += ["--model", model]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += list(extra_args)
    return cmd


class WarmSession:
    """열린 상주 claude 프로세스 하나. :func:`open_warm` 이 돌려준다.

    **직렬**이다 — 다음 질의는 이전 턴의 ``result`` 이벤트까지 기다린다. 병렬이
    필요하면 서비스가 여러 개를 연다(모듈 docstring 의 "범위" 참고).
    """

    def __init__(self, *, cmd: list[str],
                 cwd: str | None = None,
                 env: dict | None = None,
                 turn_timeout: float = 300.0):
        self._cmd = cmd
        self._cwd = cwd
        self._env = env
        self._turn_timeout = turn_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._session_id = ""
        self._stderr_tail: list[str] = []
        self._turn_open = False      # 진행 중인 턴이 result 를 못 본 상태
        self._dirty = False          # 스트림 위치를 신뢰할 수 없음
        self._closed = False

    # ---- 노출하는 것 ----

    @property
    def session_id(self) -> str:
        """현재 세션 id. ``/clear`` 를 보내면 새 값으로 바뀐다.

        이 값이 서비스에 넘기는 것이다 — 저장해 두었다가 다른 프로세스에서
        ``resume_session_id`` 로 이어받을 수 있다. 라이브러리는 이 값을 어디에도
        저장하지 않는다.
        """
        return self._session_id

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def cmd(self) -> list[str]:
        return list(self._cmd)

    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # ---- 수명 ----

    async def _start(self) -> None:
        kwargs: dict = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if self._cwd is not None:
            kwargs["cwd"] = self._cwd
        if self._env is not None:
            kwargs["env"] = self._env
        self._proc = await asyncio.create_subprocess_exec(*self._cmd, **kwargs)
        asyncio.ensure_future(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """stderr 를 계속 비운다 — 안 비우면 파이프 버퍼가 차서 프로세스가 멈춘다."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                self._stderr_tail.append(line.decode("utf-8", "replace"))
                del self._stderr_tail[:-50]
        except (asyncio.CancelledError, ValueError):
            return

    async def close(self) -> None:
        """stdin 을 닫아 프로세스를 끝낸다. 안 끝나면 kill."""
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except (BrokenPipeError, RuntimeError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def __aenter__(self) -> "WarmSession":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---- 주고받기 ----

    async def stream_events(self, prompt: str, *,
                            timeout: float | None = None) -> AsyncIterator[dict]:
        """한 턴의 **원본** claude 이벤트를 순서대로 yield 한다.

        마지막에 ``result`` 이벤트까지 내보내고 끝난다. 정규화된 청크가 필요하면
        :meth:`stream` 을 쓴다.
        """
        if self._closed:
            raise WarmSessionError(
                "닫힌 상주 세션 — 다시 쓰려면 open_warm() 으로 새로 연다")
        if self._dirty:
            raise WarmSessionError(
                "이전 턴이 끝나지 않은 채 버려져 스트림 위치를 신뢰할 수 없다 "
                "— 닫고 새로 연다")

        async with self._lock:
            # spawn 은 반드시 락 안에서. 밖에 두면 첫 턴 동시 호출 때 둘 다
            # `_proc is None` 을 보고 각자 프로세스를 띄우고, 그중 하나는
            # `self._proc` 에 남지 않아 close() 로도 닫을 수 없는 잔여
            # 프로세스가 된다 (실측: spawn 2회).
            if self._proc is None:
                await self._start()
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                raise WarmSessionError("상주 세션 파이프가 없다")

            payload = json.dumps(
                {"type": "user", "message": {"role": "user", "content": prompt}},
                ensure_ascii=False)
            try:
                proc.stdin.write(payload.encode("utf-8") + b"\n")
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise WarmSessionError(f"상주 세션 stdin 쓰기 실패: {exc}") from exc

            limit = timeout or self._turn_timeout
            loop = asyncio.get_running_loop()
            deadline = loop.time() + limit
            self._turn_open = True
            try:
                async for evt in self._read_turn(proc, deadline, limit):
                    yield evt
            finally:
                # 소비자가 턴을 끝까지 안 읽고 빠져나가면(스트리밍 취소 등) 남은
                # 이벤트가 stdout 에 그대로 남아 **다음 턴이 그것을 자기 응답으로
                # 읽는다** (실측: 턴2 가 턴1 의 잔여 델타를 받고 session_id 도
                # 낡은 값에 고착). 여기서 result 까지 배수해 스트림 위치를 맞춘다.
                if self._turn_open:
                    await self._drain_open_turn(proc, deadline, limit)

    async def _read_turn(self, proc, deadline: float,
                         limit: float) -> AsyncIterator[dict]:
        """``result`` 까지 한 턴의 이벤트를 읽어 흘린다."""
        loop = asyncio.get_running_loop()
        while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise WarmSessionError(self._timeout_msg(limit))
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise WarmSessionError(self._timeout_msg(limit)) from None
                if not line:
                    raise WarmSessionError(
                        "상주 세션 stdout 이 EOF — 프로세스가 죽었다. stderr: "
                        f"{''.join(self._stderr_tail[-5:])[:400]}")
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    evt = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if evt.get("session_id"):
                    self._session_id = evt["session_id"]
                yield evt
                if evt.get("type") == "result":
                    self._turn_open = False
                    return

    async def _drain_open_turn(self, proc, deadline: float,
                               limit: float) -> None:
        """버려진 턴의 남은 이벤트를 ``result`` 까지 읽어 버린다 (best-effort).

        배수에 실패하면 스트림 위치를 더는 신뢰할 수 없으므로 세션을 오염으로
        표시한다 — 이후 턴은 조용히 잘못된 응답을 주는 대신 명시적으로 실패한다.
        """
        try:
            async for _ in self._read_turn(proc, deadline, limit):
                pass
        except (WarmSessionError, asyncio.CancelledError, OSError):
            self._dirty = True
        finally:
            if self._turn_open:
                self._dirty = True
            self._turn_open = False

    def _timeout_msg(self, limit: float) -> str:
        return (f"상주 세션 턴 타임아웃 ({limit}s) — stderr: "
                f"{''.join(self._stderr_tail[-5:])[:400]}")

    async def stream(self, prompt: str, *,
                     timeout: float | None = None) -> AsyncIterator[StreamChunk]:
        """한 턴을 프로젝트 표준 :class:`StreamChunk` 로 스트리밍한다.

        청크 타입은 기존 스트리밍 경로와 같은 계약(text/thinking/tool_use/
        tool_result/event/error/done)을 따른다 — claude provider 의 이벤트
        정규화를 그대로 재사용하므로 상주 경로가 별도 계약을 만들지 않는다.
        """
        from .base import StreamState
        from .claude import ClaudeProvider

        provider = ClaudeProvider()
        state = StreamState()
        # argv 한 근원에서 파생 — 하드코딩하면 build_warm_cmd 가 바뀔 때
        # 델타가 이중 계산되거나 누락된다.
        state.extra["partial"] = "--include-partial-messages" in self._cmd
        try:
            async for evt in self.stream_events(prompt, timeout=timeout):
                async for chunk in provider._dispatch_stream_event(evt, state):
                    yield chunk
        except WarmSessionError as exc:
            yield StreamChunk(type="error", content=str(exc),
                              data={"provider": "claude", "warm": True})
            return
        yield StreamChunk(type="done", content="".join(state.text_parts),
                          session_id=self._session_id, usage=state.final_usage)

    async def send(self, prompt: str, *, timeout: float | None = None) -> str:
        """한 턴을 끝까지 돌리고 텍스트만 돌려주는 비스트리밍 편의."""
        parts: list[str] = []
        async for evt in self.stream_events(prompt, timeout=timeout):
            etype = evt.get("type")
            if etype == "stream_event":
                inner = evt.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        parts.append(delta.get("text", ""))
            elif etype == "assistant" and not parts:
                for blk in (evt.get("message") or {}).get("content") or []:
                    if blk.get("type") == "text":
                        parts.append(blk.get("text", ""))
        return "".join(parts).strip()


async def open_warm(*, lean: bool = True,
                    permission_mode: str = "bypassPermissions",
                    model: str = "",
                    append_system_prompt: str = "",
                    resume_session_id: str = "",
                    cwd: str | None = None,
                    env: dict | None = None,
                    binary: str | None = None,
                    turn_timeout: float = 300.0) -> WarmSession:
    """상주 모드로 claude 를 열고 :class:`WarmSession` 을 돌려준다.

    프로세스는 첫 질의에서 뜬다 — 부팅 비용은 그 턴에 붙고, 이후 질의는 턴 처리
    시간만 부담한다.

    Args:
        lean: ``--safe-mode --tools ""``. tools 를 켜면 ``/clear`` 이후 턴당
            지연이 3~9배로 뛰므로 기본값 True 를 권한다.
        append_system_prompt: ``/clear`` 를 살아남는 고정 컨텍스트. 기동 시
            고정이라 세션이 사는 동안 바꿀 수 없다.
        resume_session_id: 기존 세션을 이어받는다. 다른 프로세스가 남긴
            session_id 도 된다 — 단 그 프로세스가 아직 살아 있다면 정리는
            서비스 몫이다(라이브러리는 남의 프로세스를 건드리지 않는다).
        turn_timeout: 턴당 상한. 상주 모드는 stdin 을 열어두는 유일한 경로라
            issue #4 류 무한 대기의 상한이 필요하다.

    Raises:
        WarmSessionError: claude CLI 를 찾지 못하면.
    """
    resolved = binary or _find_claude()
    if not resolved:
        raise WarmSessionError("claude CLI not found on PATH")
    cmd = build_warm_cmd(
        binary=resolved, lean=lean, permission_mode=permission_mode,
        model=model, append_system_prompt=append_system_prompt,
        resume_session_id=resume_session_id)
    return WarmSession(cmd=cmd, cwd=cwd, env=env, turn_timeout=turn_timeout)
