"""issue #48 상주(warm) 워커 단위 테스트 — 실제 claude CLI 없이 도는 hermetic 테스트.

실측으로 확인한 계약을 고정한다. 각 단언 옆의 근거는 모듈 docstring 참고.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from agentcli.providers import warm
from agentcli.providers.warm import (CLEAR_COMMAND, TurnResult, WarmSession,
                                     WarmSessionError, build_warm_cmd)


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
        self._written: list[dict] = []
        self.stdout = _FakeStream(turns)
        self.stderr = _FakeStream([])
        self.stdin = _FakeStdin(self._written)

    @property
    def written(self):
        return self._written

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


def _result(sid, *, duration_ms=1500, duration_api_ms=9999, is_error=False):
    return {"type": "result", "subtype": "success", "session_id": sid,
            "duration_ms": duration_ms, "duration_api_ms": duration_api_ms,
            "is_error": is_error}


def _delta(text):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": text}}}


def _session_with(turns, **kw):
    """가짜 프로세스를 물린 WarmSession 와 그 프로세스를 함께 돌려준다."""
    proc = _FakeProc(turns)

    async def fake_exec(*cmd, **kwargs):
        return proc

    w = WarmSession(binary="/usr/bin/claude", **kw)
    original_write = proc.stdin.write

    def write_and_arm(data):
        original_write(data)
        proc.stdout.feed_next_turn()      # 쓰기에 대한 응답을 준비

    proc.stdin.write = write_and_arm
    return w, proc, fake_exec


# ---- 기동 argv ----

def test_stream_json_requires_verbose():
    """``--output-format stream-json`` 은 ``--verbose`` 없이는 CLI 가 거부한다.

    실측: "When using --print, --output-format=stream-json requires --verbose".
    플래그를 빼면 프로세스가 즉시 죽으므로 argv 에서 고정한다.
    """
    cmd = build_warm_cmd(binary="claude")
    assert "--verbose" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert "--print" in cmd


def test_lean_is_the_default():
    """tools 를 켜두면 ``/clear`` 이후 턴당 지연이 3~9배로 뛴다 — lean 이 기본."""
    cmd = build_warm_cmd(binary="claude")
    assert "--safe-mode" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--safe-mode" not in build_warm_cmd(binary="claude", lean=False)


def test_append_system_prompt_and_resume_are_wired():
    """고정 컨텍스트는 ``--append-system-prompt`` 자리(=``/clear`` 생존)에 간다."""
    cmd = build_warm_cmd(binary="claude", append_system_prompt="RULES",
                         resume_session_id="sid-1", model="claude-sonnet-4-6")
    assert cmd[cmd.index("--append-system-prompt") + 1] == "RULES"
    assert cmd[cmd.index("--resume") + 1] == "sid-1"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


# ---- 턴 ----

def test_ask_collects_deltas_and_result():
    w, proc, fake_exec = _session_with([
        [{"type": "system", "subtype": "init", "session_id": "s1"},
         _delta("ZEBRA-"), _delta("4417"),
         _result("s1", duration_ms=1234)],
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            return await w.ask("What is the password?")

    r: TurnResult = asyncio.run(run())
    assert r.text == "ZEBRA-4417"
    assert r.session_id == "s1"
    assert r.is_error is False
    # stdin 으로 나간 것은 stream-json user 메시지 한 줄.
    assert proc.written == [
        {"type": "user", "message": {"role": "user",
                                     "content": "What is the password?"}}]


def test_turn_cost_uses_duration_ms_not_cumulative_api_ms():
    """턴 비용은 ``duration_ms``(턴별) — ``duration_api_ms`` 는 누적이라 못 쓴다.

    실측에서 첫 턴 ``duration_api_ms`` 가 부팅 중 API 호출을 흡수해 2턴째 델타가
    벽시계를 넘겼다(2701 vs duration_ms 1663).
    """
    w, _proc, fake_exec = _session_with([
        [_delta("a"), _result("s1", duration_ms=1663, duration_api_ms=2701)],
        [_delta("b"), _result("s1", duration_ms=2687, duration_api_ms=5383)],
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            return await w.ask("q1"), await w.ask("q2")

    r1, r2 = asyncio.run(run())
    assert r1.turn_ms == 1663
    assert r2.turn_ms == 2687, "누적 api_ms 델타(2682)가 아니라 턴별 duration_ms"


def test_boot_cost_is_first_turn_wall_minus_duration_ms():
    w, _proc, fake_exec = _session_with([
        [_delta("ok"), _result("s1", duration_ms=0)],
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await w.ask("q")

    asyncio.run(run())
    assert w.boot_seconds is not None and w.boot_seconds >= 0
    assert w.turns == 1


def test_dead_process_raises_instead_of_hanging():
    """stdout EOF = 프로세스 사망. 조용히 매달리지 말고 즉시 에러."""
    w, _proc, fake_exec = _session_with([])      # 응답 없음 → 즉시 EOF

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await w.ask("q")

    with pytest.raises(WarmSessionError, match="EOF"):
        asyncio.run(run())


def test_turn_timeout_is_bounded():
    """상주 경로는 stdin 을 열어두므로 #4 류 무한 대기의 상한이 필요하다."""
    proc = _FakeProc([])

    async def never(*a, **k):          # 응답이 영원히 안 오는 stdout
        await asyncio.sleep(3600)

    proc.stdout.readline = never

    async def fake_exec(*cmd, **kwargs):
        return proc

    w = WarmSession(binary="/usr/bin/claude", boot_timeout=0.3)

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await w.ask("q")

    with pytest.raises(WarmSessionError, match="타임아웃"):
        asyncio.run(run())


# ---- 격리 (/clear) ----

def test_clear_succeeds_when_session_id_changes():
    """``/clear`` 성공 판정은 session_id 변화 — 본문으로는 알 수 없다."""
    w, proc, fake_exec = _session_with([
        [_delta("hi"), _result("s1")],
        [_result("s2", duration_ms=0)],        # /clear 턴: 본문 없음, 새 sid
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await w.ask("seed")
            return await w.clear()

    assert asyncio.run(run()) is True
    assert w.session_id == "s2"
    assert proc.written[-1]["message"]["content"] == CLEAR_COMMAND


def test_clear_fails_when_session_id_unchanged():
    """sid 가 그대로면 컨텍스트가 남았을 수 있다 — 재사용 금지 신호(False)."""
    w, _proc, fake_exec = _session_with([
        [_delta("hi"), _result("s1")],
        [_result("s1", duration_ms=0)],        # sid 그대로
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await w.ask("seed")
            return await w.clear()

    assert asyncio.run(run()) is False, (
        "sid 미변화를 성공으로 보면 이전 질의 컨텍스트가 다음 질의로 샌다")


def test_clear_reports_failure_instead_of_raising():
    """워커가 죽어 있어도 clear() 는 False 를 돌려 호출자가 폐기하게 한다."""
    w, _proc, fake_exec = _session_with([[_delta("hi"), _result("s1")]])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await w.ask("seed")
            return await w.clear()       # 다음 턴 응답 없음 → EOF

    assert asyncio.run(run()) is False


# ---- 직렬화 ----

def test_turns_are_serialized():
    """상주 1개 = 직렬. 겹쳐 부르면 이전 턴의 result 까지 기다린다."""
    w, proc, fake_exec = _session_with([
        [_delta("1"), _result("s1")],
        [_delta("2"), _result("s1")],
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            return await asyncio.gather(w.ask("a"), w.ask("b"))

    r1, r2 = asyncio.run(run())
    assert {r1.text, r2.text} == {"1", "2"}
    assert [m["message"]["content"] for m in proc.written] == ["a", "b"]


def test_missing_binary_raises_clearly():
    with patch.object(warm, "_find_claude", return_value=None):
        w = WarmSession()

        async def run():
            await w.start()

        with pytest.raises(WarmSessionError, match="not found"):
            asyncio.run(run())


# ---- 소비자에게 넘기는 관측 사실 (프로세스 제어는 라이브러리 밖) ----

def test_handle_exposes_facts_for_consumer_owned_persistence():
    """라이브러리는 핸들을 노출만 하고 저장하지 않는다.

    재기동 후 잔여 워커 탐색·종료는 배포·감독 영역이라 소비자 몫이다. 라이브러리가
    전역 파일·DB 를 갖거나 남의 프로세스를 조회·종료하면 안 된다.
    """
    w, proc, fake_exec = _session_with([[_delta("hi"), _result("s1")]],
                                      cwd="/tmp/work")

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await w.ask("q")
            return w.handle

    h = asyncio.run(run())
    assert h.session_id == "s1"
    assert h.cwd == "/tmp/work"
    assert h.argv[0] == "/usr/bin/claude" and "--verbose" in h.argv
    assert h.spawned_at is not None
    assert h.pid == proc.pid


def test_handle_session_id_follows_clear():
    """``/clear`` 로 sid 가 바뀌면 핸들도 새 값을 낸다 — 소비자는 갱신 저장해야 한다."""
    w, _proc, fake_exec = _session_with([
        [_delta("hi"), _result("s1")],
        [_result("s2", duration_ms=0)],
    ])

    async def run():
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await w.ask("seed")
            before = w.handle.session_id
            await w.clear()
            return before, w.handle.session_id

    before, after = asyncio.run(run())
    assert (before, after) == ("s1", "s2")


def test_library_exposes_no_process_control_surface():
    """라이브러리가 프로세스를 조회·종료하는 공개 수단을 갖지 않는다.

    "직접 제어는 하지 않는다"는 경계를 회귀로 고정한다. 자기가 띄운 워커를 닫는
    ``close()`` 는 자기 자원 정리라 예외다.
    """
    banned = {
        # 프로세스 직접 제어
        "terminate", "kill_residual", "sweep", "process_matches",
        "registry", "WarmRegistry", "adopt", "reattach",
        # 풀링·동시성·감독 — 어떻게 쓸지는 서비스의 일
        "WarmPool", "Pool", "acquire", "release", "supervise",
        "Supervisor", "restart_dead", "scale",
    }
    assert banned.isdisjoint(dir(warm)), (
        "상주 세션 하나의 제어만 지원한다 — 풀링·감독·잔여 프로세스 정리는 "
        "소비자(서비스) 책임이다")
