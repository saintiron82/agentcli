"""issue #48 상주(warm) 모드 — 실제 claude CLI 없이 도는 hermetic 테스트.

지원 범위는 "상주 모드로 열고 session_id 를 넘겨준다" 이다. 실측으로 확인한
계약과 그 범위 경계를 고정한다.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from agentcli.providers import warm
from agentcli.providers.warm import (WarmSession, WarmSessionError,
                                     build_warm_cmd, open_warm)


# ---- 가짜 claude 프로세스 ----

class _FakeStdin:
    def __init__(self, sink):
        self._sink = sink
        self.closed = False

    def write(self, data):
        self._sink.append(json.loads(data.decode("utf-8")))

    async def drain(self):
        return None

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class _FakeStream:
    """미리 정해둔 응답을 턴 단위로 흘려주는 stdout/stderr 대역."""

    def __init__(self, turns):
        self._turns = list(turns)
        self._buf: list[bytes] = []

    def feed_next_turn(self):
        if self._turns:
            for evt in self._turns.pop(0):
                self._buf.append(json.dumps(evt).encode("utf-8") + b"\n")

    async def readline(self):
        if not self._buf:
            return b""          # EOF
        return self._buf.pop(0)


class _FakeProc:
    def __init__(self, turns):
        self.returncode = None
        self.pid = 4242
        self.written: list[dict] = []
        self.stdout = _FakeStream(turns)
        self.stderr = _FakeStream([])
        self.stdin = _FakeStdin(self.written)
        original = self.stdin.write

        def write_and_arm(data):
            original(data)
            self.stdout.feed_next_turn()     # 쓰기에 대한 응답을 준비

        self.stdin.write = write_and_arm

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def _result(sid, **kw):
    evt = {"type": "result", "subtype": "success", "session_id": sid,
           "duration_ms": 1500}
    evt.update(kw)
    return evt


def _delta(text):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": text}}}


def _session_with(turns, **kw):
    proc = _FakeProc(turns)

    async def fake_exec(*cmd, **kwargs):
        return proc

    s = WarmSession(cmd=["/usr/bin/claude", "--print"], **kw)
    return s, proc, fake_exec


# ---- 기동 argv ----

def test_stream_json_requires_verbose():
    """``--output-format stream-json`` 은 ``--verbose`` 없이는 CLI 가 거부한다.

    실측: "When using --print, --output-format=stream-json requires --verbose".
    빠지면 프로세스가 즉시 죽으므로 argv 에서 고정한다.
    """
    cmd = build_warm_cmd(binary="claude")
    assert "--verbose" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert "--print" in cmd and "--include-partial-messages" in cmd


def test_lean_is_the_default():
    """tools 를 켜두면 ``/clear`` 이후 턴당 지연이 3~9배로 뛴다 — lean 이 기본."""
    cmd = build_warm_cmd(binary="claude")
    assert "--safe-mode" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--safe-mode" not in build_warm_cmd(binary="claude", lean=False)


def test_fixed_context_and_resume_are_wired():
    """고정 컨텍스트는 ``--append-system-prompt``(``/clear`` 생존) 자리로 간다."""
    cmd = build_warm_cmd(binary="claude", append_system_prompt="RULES",
                         resume_session_id="sid-1", model="claude-sonnet-4-6")
    assert cmd[cmd.index("--append-system-prompt") + 1] == "RULES"
    assert cmd[cmd.index("--resume") + 1] == "sid-1"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_open_warm_requires_a_binary():
    with patch.object(warm, "_find_claude", return_value=None):
        with pytest.raises(WarmSessionError, match="not found"):
            asyncio.run(open_warm())


# ---- 주고받기 ----

def test_send_returns_text_and_writes_stream_json_user_message():
    s, proc, fake_exec = _session_with([
        [{"type": "system", "subtype": "init", "session_id": "s1"},
         _delta("ZEBRA-"), _delta("4417"), _result("s1")],
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            return await s.send("What is the password?")

    assert asyncio.run(run()) == "ZEBRA-4417"
    assert proc.written == [
        {"type": "user", "message": {"role": "user",
                                     "content": "What is the password?"}}]


def test_stream_yields_project_standard_chunks():
    """상주 경로가 별도 청크 계약을 만들지 않는다 — 기존 정규화를 재사용한다."""
    from agentcli.types import STREAM_CHUNK_TYPES

    s, _proc, fake_exec = _session_with([
        [_delta("hi "), _delta("there"), _result("s1")],
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            return [c async for c in s.stream("q")]

    chunks = asyncio.run(run())
    assert all(c.type in STREAM_CHUNK_TYPES for c in chunks)
    assert "".join(c.content for c in chunks if c.type == "text") == "hi there"
    done = chunks[-1]
    assert done.type == "done" and done.session_id == "s1"


def test_stream_reports_failure_as_error_chunk():
    """스트림 도중 세션이 죽으면 예외가 아니라 error 청크로 알린다."""
    s, _proc, fake_exec = _session_with([])       # 응답 없음 → 즉시 EOF

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            return [c async for c in s.stream("q")]

    chunks = asyncio.run(run())
    assert chunks[-1].type == "error" and "EOF" in chunks[-1].content


def test_dead_process_raises_instead_of_hanging():
    s, _proc, fake_exec = _session_with([])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await s.send("q")

    with pytest.raises(WarmSessionError, match="EOF"):
        asyncio.run(run())


def test_turn_timeout_is_bounded():
    """상주 모드는 stdin 을 열어두는 유일한 경로 — #4 류 무한 대기의 상한이 필요하다."""
    proc = _FakeProc([])

    async def never(*a, **k):
        await asyncio.sleep(3600)

    proc.stdout.readline = never

    async def fake_exec(*cmd, **kwargs):
        return proc

    s = WarmSession(cmd=["/usr/bin/claude"], turn_timeout=0.3)

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await s.send("q")

    with pytest.raises(WarmSessionError, match="타임아웃"):
        asyncio.run(run())


def test_turns_are_serialized():
    """상주 세션 하나는 직렬 — 겹쳐 부르면 이전 턴의 result 까지 기다린다."""
    s, proc, fake_exec = _session_with([
        [_delta("1"), _result("s1")],
        [_delta("2"), _result("s1")],
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            return await asyncio.gather(s.send("a"), s.send("b"))

    r1, r2 = asyncio.run(run())
    assert {r1, r2} == {"1", "2"}
    assert [m["message"]["content"] for m in proc.written] == ["a", "b"]


# ---- session_id: 라이브러리가 넘겨주는 유일한 것 ----

def test_session_id_is_exposed_and_follows_clear():
    """``/clear`` 는 정책이 아니라 그냥 보내는 메시지 — sid 가 바뀌는 걸로 확인한다."""
    s, proc, fake_exec = _session_with([
        [_delta("hi"), _result("s1")],
        [_result("s2", duration_ms=0)],       # /clear 턴: 본문 없음, 새 sid
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await s.send("seed")
            before = s.session_id
            await s.send("/clear")
            return before, s.session_id

    before, after = asyncio.run(run())
    assert (before, after) == ("s1", "s2"), (
        "sid 가 안 바뀌면 이전 질의 컨텍스트가 다음 질의로 샐 수 있다")
    assert proc.written[-1]["message"]["content"] == "/clear"


def test_close_is_idempotent():
    s, proc, fake_exec = _session_with([[_delta("hi"), _result("s1")]])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await s.send("q")
            assert s.alive()
            await s.close()
            await s.close()

    asyncio.run(run())
    assert not s.alive()
    assert proc.stdin.closed


def test_async_context_manager_closes():
    s, proc, fake_exec = _session_with([[_delta("hi"), _result("s1")]])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            async with s:
                await s.send("q")

    asyncio.run(run())
    assert not s.alive()


# ---- 범위 경계 ----

def test_library_does_not_manage_processes_or_pools():
    """지원 범위는 상주 모드로 열고 session_id 를 넘겨주는 것까지다.

    운용(풀링·동시성 배분·감독·재기동 후 잔여 프로세스 정리)은 서비스의 일이고,
    라이브러리가 전역 파일·DB 를 갖거나 남의 프로세스를 조회·종료해서도 안 된다.
    편의를 이유로 이 표면이 다시 생기면 여기서 걸린다.
    """
    banned = {
        # 프로세스 직접 제어
        "terminate", "kill_residual", "sweep", "process_matches",
        "registry", "WarmRegistry", "adopt", "reattach",
        # 풀링·동시성·감독
        "WarmPool", "Pool", "acquire", "release", "supervise",
        "Supervisor", "restart_dead", "scale",
    }
    assert banned.isdisjoint(dir(warm))
    assert banned.isdisjoint(dir(WarmSession))
