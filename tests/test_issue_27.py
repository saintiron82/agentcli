"""Regression tests for issue #27 — Windows claude session-resume guard removal.

#4 made ``claude -p --resume <sid>`` hang for 5+ minutes on Windows, and the
original fix disabled sessions there wholesale
(``supports_sessions = platform.system() != "Windows"``). The real trigger was
an interactive **stdin wait**, and agentcli hands the CLI an immediate EOF on
every spawn path, so the guard was stale and is now removed.

The guard removal only stays correct while that stdin premise holds. It has
already been dented once: issue #30 moved prompts over 8,000 UTF-8 bytes off
argv and onto ``stdin=PIPE``, which is a *different* stdin shape than the
``DEVNULL`` the #27 analysis rested on. It is still safe (write-then-close ⇒
EOF), but nothing was pinning that down. These tests pin both halves:

1. ``--resume`` is emitted on every platform when a session id is stored.
2. Neither spawn path ever leaves stdin open for the CLI to read from — the
   small-prompt path closes it, the large-prompt path writes and closes it.

Verified end-to-end on Windows 11 / Claude Code 2.1.220 before merge: plain
resume, >8KB-via-stdin resume, and MCP-on resume all keep session continuity
and finish in under 30s.

Reference: https://github.com/saintiron82/agentcli/issues/27
"""

import sys
from unittest.mock import patch

from agentcli.providers import base
from agentcli.providers.base import PROMPT_STDIN_THRESHOLD
from agentcli.providers.claude import ClaudeProvider

SID = "11111111-2222-3333-4444-555555555555"


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
