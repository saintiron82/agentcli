"""Regression tests for issue #27 — Windows claude session-resume guard removal.

#4 made ``claude -p --resume <sid>`` hang for 5+ minutes on Windows, and the
original fix disabled sessions there wholesale
(``supports_sessions = platform.system() != "Windows"``). The real trigger was
an interactive **stdin wait**, and agentcli hands the CLI an immediate EOF on
every spawn path, so the guard was stale and is now removed.

The guard removal only stays correct while that stdin premise holds, and the
premise was not actually structural when this fix was first written:

- issue #30 moved prompts over 8,000 UTF-8 bytes off argv and onto
  ``stdin=PIPE``, a *different* stdin shape than the ``DEVNULL`` the analysis
  rested on (still safe — write-then-close ⇒ EOF — but unpinned), and
- ``run_subprocess_async`` used to leave ``stdin`` unset when given neither
  ``input_bytes`` nor ``use_stdin_devnull``, so the child **inherited the
  parent's stdin**. Measured against a live parent stdin, that reproduced #4
  exactly: ``timed_out=True, rc=124``. Safety was caller discipline, not an
  invariant. The helper now always closes stdin it does not write to.

These tests pin all three halves:

1. ``--resume`` is emitted on every platform when a session id is stored —
   asserted against a **simulated Windows** provider, not merely the host
   platform, so the check has teeth on a POSIX-only CI matrix.
2. No spawn path — sync or async — ever leaves stdin open for the CLI to read.
3. claude's own async call site keeps closing stdin while resuming.

Verified end-to-end on Windows 11 / Claude Code 2.1.220 before merge: plain
resume, >8KB-via-stdin resume, and MCP-on resume all keep session continuity
and finish in under 30s.

Reference: https://github.com/saintiron82/agentcli/issues/27
"""

import asyncio
import importlib
import sys
from unittest.mock import patch

import pytest

from agentcli.providers import base
from agentcli.providers.base import PROMPT_STDIN_THRESHOLD
from agentcli.providers.claude import ClaudeProvider

SID = "11111111-2222-3333-4444-555555555555"


def _provider_as_if_on(system: str) -> type:
    """``platform.system()`` 이 *system* 을 보고하는 환경에서의 ClaudeProvider.

    ``supports_sessions`` 는 클래스 정의 시점에 평가되므로 모듈을 reload 해야
    한다. 이 우회가 없으면 "현재 플랫폼에서의 값"만 단언하게 되는데, 제거된
    가드는 ``platform.system() != "Windows"`` 였으므로 POSIX 에서는 수정 전
    코드도 ``True`` 로 평가된다 — 즉 CI(ubuntu/macos)에서 가드를 되돌려도
    통과해버려 회귀 방어력이 0 이 된다.
    """
    import agentcli.providers.claude as claude_mod
    with patch("platform.system", return_value=system):
        reloaded = importlib.reload(claude_mod)
        cls = reloaded.ClaudeProvider
    importlib.reload(claude_mod)          # 전역 상태 원복
    return cls


@pytest.mark.parametrize("system", ["Windows", "Linux", "Darwin"])
def test_sessions_enabled_on_every_platform(system):
    """어느 플랫폼을 보고하든 세션이 켜져 있어야 한다 (#27).

    수정 전 코드에서는 system="Windows" 일 때 False 라 이 테스트가 실패한다 —
    POSIX 전용 CI 에서도 가드 회귀를 잡아내는 것이 요점이다.
    """
    assert _provider_as_if_on(system).supports_sessions is True


def test_resume_emitted_on_simulated_windows():
    """시뮬레이션된 Windows 에서도 ``--resume`` 이 실제로 붙는다."""
    cls = _provider_as_if_on("Windows")
    with patch.object(cls, "_find_binary", return_value="/usr/bin/claude"):
        cmd, used_sid = cls()._build_cmd(
            prompt="hello", model="", session_id=SID, output_format="json")
    assert cmd is not None
    assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == SID
    assert used_sid == SID


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_stored_session_id_resumes(_mock_find):
    """저장된 session_id 가 있으면 ``--resume`` 으로 재개한다 (전 플랫폼)."""
    p = ClaudeProvider()
    cmd, used_sid = p._build_cmd(
        prompt="hello", model="", session_id=SID, output_format="json")
    assert cmd is not None
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == SID
    assert "--session-id" not in cmd, "resume 시에는 새 식별자를 붙이지 않는다"
    assert used_sid == SID, "resume 후에도 같은 session_id 를 유지해 보고한다"


@patch("agentcli.providers.claude.ClaudeProvider._find_binary",
       return_value="/usr/bin/claude")
def test_stored_session_id_resumes_in_stream_mode(_mock_find):
    """stream-json 출력 모드도 같은 resume 계약을 따른다."""
    p = ClaudeProvider()
    cmd, used_sid = p._build_cmd(
        prompt="hello", model="", session_id=SID, output_format="stream-json")
    assert cmd is not None
    assert "--resume" in cmd
    assert used_sid == SID


def _run_invoke(prompt: str):
    """invoke 를 스텁 위에서 1회 돌리고 run_subprocess_sync 호출 kwargs 를 돌려준다."""
    from agentcli.types import Message
    payload = ('{"type":"result","subtype":"success","result":"ok",'
               '"session_id":"' + SID + '"}')
    with patch("agentcli.providers.claude.ClaudeProvider._find_binary",
               return_value="/usr/bin/claude"), \
         patch("agentcli.providers.claude.run_subprocess_sync",
               return_value=(payload.encode(), b"", 0, False)) as mock_run:
        ClaudeProvider().invoke([Message(role="user", content=prompt)],
                                session_id=SID)
    return mock_run.call_args


def test_small_prompt_keeps_stdin_closed():
    """임계치 이하 프롬프트: argv 전달 + ``input_bytes=None`` → stdin=DEVNULL."""
    call = _run_invoke("hi")
    assert call.kwargs["input_bytes"] is None, (
        "작은 프롬프트는 stdin 을 열지 않아야 한다 — 열어두면 #4 의 인터랙티브 "
        "대기 조건이 되살아난다")
    assert "--resume" in call.args[0]


def test_large_prompt_writes_stdin_then_closes():
    """임계치 초과 프롬프트: stdin=PIPE 이지만 write 후 즉시 close → 여전히 EOF."""
    big = "x" * (PROMPT_STDIN_THRESHOLD + 1000)
    call = _run_invoke(big)
    assert call.kwargs["input_bytes"] is not None, (
        f"{PROMPT_STDIN_THRESHOLD}B 초과 프롬프트는 stdin 으로 가야 한다 (#30)")
    assert big.encode("utf-8") in call.kwargs["input_bytes"]
    assert "--resume" in call.args[0], (
        "대형 프롬프트 경로에서도 resume 이 유지되어야 한다 — 이 조합이 #27 "
        "가드 제거의 유일한 미검증 구멍이었다")


_READ_STDIN = (
    "import sys; d = sys.stdin.read(); "
    "sys.stdout.write('read:%d' % len(d))")


def test_stdin_pipe_path_actually_reaches_eof():
    """stdin=PIPE 경로가 실제로 EOF 에 도달하는지 진짜 자식으로 확인.

    write 만 하고 닫지 않으면 자식의 ``stdin.read()`` 가 영원히 안 끝나 #4 의
    hang 이 되살아난다. 여기서는 stdin 을 끝까지 읽는 자식을 띄워, 타임아웃이
    아니라 정상 종료로 끝나는지를 본다 (모킹 없이).
    """
    payload = b"y" * (PROMPT_STDIN_THRESHOLD + 1000)
    stdout, _stderr, rc, timed_out = base.run_subprocess_sync(
        [sys.executable, "-c", _READ_STDIN], timeout=30, input_bytes=payload)
    assert not timed_out, "stdin 이 닫히지 않아 자식이 EOF 를 못 봤다 (#4 재발)"
    assert rc == 0
    assert stdout.decode() == f"read:{len(payload)}"


def test_stdin_devnull_path_reaches_eof_immediately():
    """input_bytes=None 경로도 자식이 stdin 에서 즉시 EOF 를 본다 (DEVNULL)."""
    stdout, _stderr, rc, timed_out = base.run_subprocess_sync(
        [sys.executable, "-c", _READ_STDIN], timeout=30, input_bytes=None)
    assert not timed_out
    assert rc == 0
    assert stdout.decode() == "read:0"


# ---- async 경로: 회귀가 실제로 가능한 쪽 ----

@pytest.mark.parametrize("kwargs,expected", [
    ({}, "DEVNULL"),                          # 기본값 — 예전엔 부모 stdin 상속
    ({"use_stdin_devnull": True}, "DEVNULL"),
    ({"input_bytes": b"payload"}, "PIPE"),
])
def test_async_spawn_never_inherits_stdin(kwargs, expected):
    """``run_subprocess_async`` 는 어떤 인자 조합에서도 stdin 을 상속하지 않는다.

    예전 기본값은 ``stdin`` 키를 아예 설정하지 않아 부모 stdin 을 물려줬고,
    부모 stdin 이 살아 있으면 자식이 거기서 대기했다(#4 의 hang 조건).
    """
    captured = {}

    async def fake_exec(*cmd, **kw):
        captured.update(kw)
        raise FileNotFoundError("stop here — kwargs 만 확인한다")

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            with pytest.raises(FileNotFoundError):
                await base.run_subprocess_async(["x"], timeout=5, **kwargs)

    asyncio.run(run())
    want = getattr(asyncio.subprocess, expected)
    assert captured.get("stdin") == want, (
        f"stdin={captured.get('stdin')!r} — 상속(미설정)은 #4 를 되살린다")


def test_async_default_child_reaches_eof():
    """기본 인자로도 stdin 을 읽는 실제 자식이 EOF 를 보고 정상 종료한다."""
    async def run():
        return await base.run_subprocess_async(
            [sys.executable, "-c", _READ_STDIN], timeout=30)

    stdout, _stderr, rc, timed_out = asyncio.run(run())
    assert not timed_out, "stdin 이 열린 채라 자식이 EOF 를 못 봤다 (#4 재발)"
    assert rc == 0
    assert stdout.decode() == "read:0"


def test_claude_async_call_site_closes_stdin_while_resuming():
    """claude 의 ``invoke_async`` 도 resume 하면서 stdin 을 닫는다.

    #27 의 안전 논거는 claude 의 *모든* spawn 경로에 걸린다. sync 만 고정하면
    async 호출부가 플래그를 빠뜨리는 회귀를 놓친다.
    """
    from agentcli.types import Message
    payload = ('{"type":"result","subtype":"success","result":"ok",'
               '"session_id":"' + SID + '"}')

    async def fake_run(cmd, **kw):
        fake_run.cmd, fake_run.kw = cmd, kw
        return payload.encode(), b"", 0, False

    async def run():
        with patch("agentcli.providers.claude.ClaudeProvider._find_binary",
                   return_value="/usr/bin/claude"), \
             patch("agentcli.providers.claude.run_subprocess_async", fake_run):
            await ClaudeProvider().invoke_async(
                [Message(role="user", content="hi")], session_id=SID)

    asyncio.run(run())
    assert "--resume" in fake_run.cmd
    assert fake_run.kw.get("use_stdin_devnull") is True
    assert fake_run.kw.get("input_bytes") is None

    # 대형 프롬프트 async 경로: stdin=PIPE(write-then-close) + resume 유지.
    big = "x" * (PROMPT_STDIN_THRESHOLD + 1000)

    async def run_big():
        with patch("agentcli.providers.claude.ClaudeProvider._find_binary",
                   return_value="/usr/bin/claude"), \
             patch("agentcli.providers.claude.run_subprocess_async", fake_run):
            await ClaudeProvider().invoke_async(
                [Message(role="user", content=big)], session_id=SID)

    asyncio.run(run_big())
    assert "--resume" in fake_run.cmd
    assert fake_run.kw.get("use_stdin_devnull") is False
    assert big.encode("utf-8") in fake_run.kw.get("input_bytes")
