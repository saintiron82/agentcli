"""상주(warm) claude 워커 — 프로세스를 살려둔 채 stdin 으로 질의를 이어 먹인다.

issue #48. ``claude -p`` 는 호출마다 하네스를 새로 부팅해, 최소 프롬프트조차
호출당 6~11s 가 든다(Windows 11 / Claude Code 2.1.220, lean 실측).
``--input-format stream-json`` 으로 프로세스를 한 번만 띄워두면 부팅은 1회뿐이고
이후 질의는 턴 처리 시간만 부담한다. 같은 머신 실측:

    턴1  wall 6.40s   duration_ms 1663   → 부팅 ≈ 4.7s
    턴2  wall 2.69s   duration_ms 2687
    턴3  wall 1.61s   duration_ms 1611
    턴4  wall 1.65s   duration_ms 1652
    /clear           API 왕복 없음 (즉시)

즉 부팅 후에는 벽시계 ≈ CLI 가 보고한 턴 처리 시간이고, 하네스 오버헤드가
사실상 0 이다. cold ``-p`` 경로가 같은 머신에서 호출당 6.3~11.1s 였다.

## 이 모듈이 아는 실측 사실

- ``--output-format stream-json`` 은 ``--verbose`` 가 없으면 CLI 가 거부한다
  ("When using --print, --output-format=stream-json requires --verbose").
- 턴 비용은 ``result.duration_ms`` 로 재야 한다 — **이 필드가 턴별**이고 벽시계와
  거의 정확히 일치한다(실측: 2687ms vs 2.69s, 1611ms vs 1.61s). 첫 턴에서는
  부팅을 제외한 값이 나오므로 ``부팅 ≈ 첫 턴 벽시계 − duration_ms`` 다.
  ``duration_api_ms`` 는 세션 **누적값**이라 델타를 내야 하고, 그마저 첫 턴 값이
  부팅 중 API 호출을 흡수해 2턴째 델타가 벽시계를 넘기는 경우가 관측됐다
  (2701 vs duration_ms 1663). 턴 계측에는 쓰지 않는다.
- ``/clear`` 는 재부팅 없이 컨텍스트를 지우지만 **새 session_id 를 발급**한다.
  응답 본문이 비어 있어 성공 여부를 내용으로 판정할 수 없으므로, 이 모듈은
  **session_id 가 바뀌었는지**로 판정한다 (바뀌지 않으면 세션을 폐기한다 —
  여러 질의를 오가는 세션가 질의 사이에 컨텍스트를 흘리면 교차 질의 유출이다).
- ``--append-system-prompt`` 로 넣은 고정 컨텍스트는 ``/clear`` 를 살아남는다.
  따라서 "시스템 + 고정 컨텍스트는 유지, 직전 Q&A 는 미상속"이 한 프로세스
  안에서 성립한다.
- tools 를 켜두면 ``/clear`` 이후 모델이 툴로 헤매 턴당 지연이 3~9배로 뛴다.
  그래서 상주 세션는 lean(``--safe-mode --tools ""``) 을 기본값으로 쓴다.

## 범위 — 이 모듈이 하는 일과 하지 않는 일

이 모듈이 지원하는 것은 **상주 세션 하나의 제어**뿐이다: 기동 · 턴 주고받기 ·
``/clear`` 격리 · 종료 · 관측 사실(:class:`WarmHandle`) 노출.

그것을 어떻게 쓸지는 서비스의 일이다. 아래는 의도적으로 넣지 않는다.

- 세션 풀링 · 동시성 배분 · 오버플로 정책 (상주 세션 하나는 직렬이다 — 병렬이
  필요하면 서비스가 여러 개를 띄우고 배분한다)
- liveness 감시 · 자동 재기동 · 감독자
- 재기동 후 잔여 프로세스 탐색 · 종료 · 이어받기
- 그 무엇을 위한 전역 파일이나 DB (agentcli 는 저장소를 스스로 고르지 않는다)

라이브러리가 프로세스 수명을 통제하면 배포·감독 계층(오케스트레이터·서비스
매니저)과 이중으로 겹치고, 전역 상태를 갖는 순간 서로 다른 소비자가 상대의
프로세스를 건드릴 수 있다.

## stdin 계약 주의 (issue #27 과의 관계)

#27 의 안전 논거는 "어느 spawn 경로도 CLI 가 읽을 stdin 을 열어두지 않는다"
였고, ``run_subprocess_async`` 기본값까지 그렇게 바꿔 구조적 불변식으로 만들었다.
**상주 모드는 그 불변식을 의도적으로 깨는 유일한 경로**다 — stdin 을 열어둔 채
JSON 메시지를 계속 쓰는 것이 이 기능의 본질이기 때문이다. 그래서 상주 세션는
공용 헬퍼를 쓰지 않고 여기서 직접 spawn 하며, issue #4 류 hang 은 이 경로에서
독립적으로 확인해야 한다(턴별 ``turn_timeout`` 이 상한 역할을 한다).
"""

import asyncio
import json
import platform
import shutil
import time
from dataclasses import dataclass, field

# 상주 프로세스를 stream-json 입출력 모드로 띄우는 데 필요한 플래그.
# --verbose 는 선택이 아니라 CLI 가 강제한다(위 모듈 docstring 참고).
_BASE_FLAGS = (
    "--print",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--include-partial-messages",
    "--verbose",
)
# lean: 커스터마이즈(CLAUDE.md/skills/hooks/MCP)와 빌트인 툴을 끊는다.
_LEAN_FLAGS = ("--safe-mode", "--tools", "")

CLEAR_COMMAND = "/clear"


class WarmSessionError(RuntimeError):
    """상주 세션가 더 이상 신뢰할 수 없는 상태 — 호출자는 폐기 후 재기동한다."""


@dataclass(frozen=True)
class WarmHandle:
    """이 워커에 대해 라이브러리가 **관측한 사실**. 순수 데이터다.

    프로세스 수명 통제(재기동 후 잔여 워커 탐색·종료·이어받기)는 배포·감독
    영역이므로 이 라이브러리의 책임이 아니다. agentcli 는 저장소를 스스로 고르지
    않고(전역 파일·DB 없음), 다른 프로세스를 조회하거나 죽이지도 않는다. 대신
    소비자가 자기 저장소에 그대로 넣을 수 있는 이 핸들을 노출한다.

    소비자가 재기동 후 할 수 있는 것:

    - ``pid`` 로 잔여 프로세스를 찾는다. **PID 재사용을 반드시 자기 쪽에서
      배제하라** — ``spawned_at`` 은 이 라이브러리의 시계로 찍은 값이지 OS 가
      보고하는 프로세스 생성시각이 아니다. 정확한 판별이 필요하면 소비자가
      OS 에 직접 물어야 한다.
    - ``session_id`` 로 **대화를 이어받는다**. 살아 있는 워커의 stdin/stdout 에
      다시 붙는 것은 OS 수준에서 불가능하다 — 파이프는 그것을 띄운 부모가
      소유하고 부모와 함께 죽는다. 대신 잔여 프로세스를 소비자가 정리한 뒤
      ``resume_session_id=<sid>`` 로 새 워커를 띄우면 대화가 이어진다
      (상주 모드에서 ``--resume`` 동작·sid 유지 실측 확인).

    ``session_id`` 는 ``/clear`` 때마다 바뀐다 — 저장했다면 갱신해야 한다.
    """
    pid: int | None
    session_id: str
    argv: tuple[str, ...]
    cwd: str | None
    spawned_at: float | None      # 라이브러리 시계 (OS 생성시각 아님)


@dataclass
class TurnResult:
    """한 턴의 결과.

    ``turn_ms`` 는 CLI 가 보고한 **이 턴의** 처리 시간(``result.duration_ms``)
    으로, 부팅을 포함하지 않는다. 첫 턴에서 ``wall_s`` 와 크게 벌어지면 그
    차이가 곧 부팅 비용이다. 세션 누적 ``duration_api_ms`` 는 계측에 쓰지 않고
    ``raw_result`` 에만 남긴다 (모듈 docstring 참고).
    """
    text: str = ""
    session_id: str = ""
    wall_s: float = 0.0
    ttft_s: float | None = None
    turn_ms: int | None = None
    is_error: bool = False
    raw_result: dict = field(default_factory=dict)


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
    """상주 프로세스 기동 argv.

    ``append_system_prompt`` 는 ``/clear`` 를 살아남는 유일한 컨텍스트 자리다
    (실측 확인) — 질의별로 격리하면서 유지하고 싶은 고정 컨텍스트를 여기 넣는다.
    단 기동 시 고정이라 워커가 살아 있는 동안에는 바꿀 수 없다.

    ``resume_session_id`` 를 주면 기존 세션에 붙는다 — 상주 프로세스도
    ``--resume`` 이 동작하고 session_id 가 유지되는 것을 실측으로 확인했다.
    """
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
    """상주 claude 프로세스 하나. **직렬** — 한 번에 한 턴만 처리한다.

    다음 stdin 쓰기는 이전 턴의 ``result`` 이벤트까지 기다린다. 병렬이 필요하면
    **서비스가** 이 객체를 여러 개 띄우고 배분한다 — 풀·오버플로·감독은 이
    라이브러리의 범위가 아니다(모듈 docstring 의 "범위" 참고).
    """

    def __init__(self, *, lean: bool = True,
                 permission_mode: str = "bypassPermissions",
                 model: str = "",
                 append_system_prompt: str = "",
                 resume_session_id: str = "",
                 cwd: str | None = None,
                 env: dict | None = None,
                 binary: str | None = None,
                 boot_timeout: float = 180.0,
                 turn_timeout: float = 300.0):
        self._binary = binary or _find_claude()
        self._cmd = (build_warm_cmd(
            binary=self._binary, lean=lean, permission_mode=permission_mode,
            model=model, append_system_prompt=append_system_prompt,
            resume_session_id=resume_session_id) if self._binary else [])
        self._cwd = cwd
        self._env = env
        self._boot_timeout = boot_timeout
        self._turn_timeout = turn_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._session_id = ""
        self._last_turn_ms: int | None = None
        self._turns = 0
        self._stderr_tail: list[str] = []
        self._boot_s: float | None = None
        self._spawned_at: float | None = None

    # ---- 수명 ----

    @property
    def cmd(self) -> list[str]:
        return list(self._cmd)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def turns(self) -> int:
        return self._turns

    @property
    def boot_seconds(self) -> float | None:
        """첫 턴에서 관측된 부팅 비용(첫 턴 벽시계 − 그 턴 ``duration_ms``)."""
        return self._boot_s

    @property
    def handle(self) -> WarmHandle:
        """소비자가 자기 저장소에 넣을 수 있는 관측 사실 (:class:`WarmHandle`).

        이 라이브러리는 이 값을 어디에도 저장하지 않는다 — 영속화·재기동 후
        정리 정책은 전적으로 소비자 몫이다.
        """
        return WarmHandle(
            pid=self._proc.pid if self._proc is not None else None,
            session_id=self._session_id,
            argv=tuple(self._cmd),
            cwd=self._cwd,
            spawned_at=self._spawned_at)

    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self._proc is not None:
            return
        if not self._binary:
            raise WarmSessionError("claude CLI not found on PATH")
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
        self._spawned_at = time.time()
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

    # ---- 턴 ----

    async def ask_stream(self, prompt: str, *, timeout: float | None = None):
        """한 턴을 스트리밍한다. ``(raw_event: dict)`` 를 순서대로 yield 한다.

        정규화는 호출자(provider)가 기존 ``_dispatch_stream_event`` 로 한다 —
        상주 경로가 별도 파서를 갖지 않아야 청크 계약이 갈라지지 않는다.
        마지막에 ``result`` 이벤트까지 yield 한 뒤 종료한다.
        """
        if not self.alive():
            await self.start()
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None

        async with self._lock:
            first = self._turns == 0
            limit = timeout or (self._boot_timeout if first else self._turn_timeout)
            payload = json.dumps(
                {"type": "user", "message": {"role": "user", "content": prompt}},
                ensure_ascii=False)
            t0 = time.monotonic()
            try:
                proc.stdin.write(payload.encode("utf-8") + b"\n")
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise WarmSessionError(f"상주 세션 stdin 쓰기 실패: {exc}") from exc

            deadline = t0 + limit
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WarmSessionError(
                        f"상주 세션 턴 타임아웃 ({limit}s) — stderr: "
                        f"{''.join(self._stderr_tail[-5:])[:400]}")
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise WarmSessionError(f"상주 세션 턴 타임아웃 ({limit}s)")
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
                    self._record_result(evt, time.monotonic() - t0, first)
                    return

    def _record_result(self, evt: dict, wall_s: float, first_turn: bool) -> None:
        self._turns += 1
        turn_ms = evt.get("duration_ms")
        if isinstance(turn_ms, int):
            self._last_turn_ms = turn_ms
            if first_turn:
                # duration_ms 는 부팅을 제외한 턴 처리 시간 — 차이가 곧 부팅.
                self._boot_s = max(0.0, wall_s - turn_ms / 1000)

    async def ask(self, prompt: str, *, timeout: float | None = None) -> TurnResult:
        """한 턴을 끝까지 돌리고 텍스트로 합쳐 돌려준다."""
        parts: list[str] = []
        ttft: float | None = None
        t0 = time.monotonic()
        result_evt: dict = {}
        async for evt in self.ask_stream(prompt, timeout=timeout):
            etype = evt.get("type")
            if etype == "stream_event":
                inner = evt.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        if ttft is None:
                            ttft = time.monotonic() - t0
                        parts.append(delta.get("text", ""))
            elif etype == "assistant" and not parts:
                for blk in (evt.get("message") or {}).get("content") or []:
                    if blk.get("type") == "text":
                        if ttft is None:
                            ttft = time.monotonic() - t0
                        parts.append(blk.get("text", ""))
            elif etype == "result":
                result_evt = evt
        turn_ms = result_evt.get("duration_ms")
        return TurnResult(
            text="".join(parts).strip(),
            session_id=self._session_id,
            wall_s=time.monotonic() - t0,
            ttft_s=ttft,
            turn_ms=turn_ms if isinstance(turn_ms, int) else None,
            is_error=bool(result_evt.get("is_error")),
            raw_result=result_evt)

    # ---- 격리 ----

    async def clear(self, *, timeout: float = 60.0) -> bool:
        """``/clear`` 로 직전 Q&A 를 버린다. 고정 system prompt 는 유지된다.

        성공 판정은 **session_id 변화**로 한다. ``/clear`` 턴은 본문이 비어 있어
        내용으로는 성공을 알 수 없다. 반환값이
        False 면 호출자는 이 워커를 재사용하지 말고 폐기해야 한다 — 이전 질의의
        컨텍스트가 남아 다음 질의로 새는 것을 막는 유일한 가드다.
        """
        before = self._session_id
        try:
            await self.ask(CLEAR_COMMAND, timeout=timeout)
        except WarmSessionError:
            return False
        after = self._session_id
        return bool(after) and after != before
