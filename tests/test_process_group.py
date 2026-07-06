"""프로세스 그룹 teardown 회귀 테스트 (좀비 손자 방지).

확인된 원인: CLI(claude 등)가 띄운 MCP 서버·hook **손자** 프로세스는 직속
자식만 kill 하면 좀비로 남아 누적된다. ``run_subprocess_sync`` /
``run_subprocess_async`` / 스트리밍 ``_run_stream_template`` 은 새 세션(프로세스
그룹)으로 띄운 뒤 타임아웃 시 그룹 전체를 killpg 하여 손자까지 reap 해야 한다.

재현 모델: ``sh -c 'sleep <tag> & ...'`` — 직속 sh 의 백그라운드 손자
``sleep`` 이 stdout 파이프를 물고 살아남는다(=좀비 잔여 프로세스).
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentcli.providers.base import (StreamState, run_subprocess_async,
                                     run_subprocess_sync)
from agentcli.providers.claude import ClaudeProvider

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="process-group teardown is POSIX-only")


def test_run_subprocess_sync_happy_path_returns_streams_and_rc():
    out, err, rc, timed = run_subprocess_sync(
        ["sh", "-c", "printf out; printf err >&2; exit 3"], timeout=5)
    assert out == b"out"
    assert err == b"err"
    assert rc == 3
    assert timed is False


def test_run_subprocess_sync_closes_stdin():
    # stdin 이 DEVNULL 이면 cat 은 즉시 EOF → 행 없이 종료.
    out, _err, _rc, timed = run_subprocess_sync(
        ["sh", "-c", "cat; printf done"], timeout=5)
    assert out == b"done"
    assert timed is False


def test_run_subprocess_sync_missing_binary_raises():
    with pytest.raises(FileNotFoundError):
        run_subprocess_sync(["/no/such/binary_xyz123"], timeout=2)


def test_run_subprocess_async_happy_path():
    async def run():
        return await run_subprocess_async(
            ["sh", "-c", "printf hi; exit 0"], timeout=5)
    out, _err, rc, timed = asyncio.run(run())
    assert out == b"hi" and rc == 0 and timed is False


def test_run_subprocess_sync_preserves_partial_stderr_on_timeout():
    """타임아웃이어도 kill 전에 자식이 쓴 stderr(예: claude ``--debug`` 로그)는
    합성 'timeout after Ns' 문자열로 덮어쓰지 말고 그대로 보존해야 한다 —
    그래야 느리거나 멈춘 호출을 진단할 수 있다 (issue #34)."""
    script = (
        "import sys, time\n"
        "sys.stderr.write('MARKER_STDERR_SYNC_9182751')\n"
        "sys.stderr.flush()\n"
        "time.sleep(5)\n"
    )
    out, err, rc, timed = run_subprocess_sync(
        [sys.executable, "-c", script], timeout=1)
    assert timed is True
    assert rc == 124
    assert b"MARKER_STDERR_SYNC_9182751" in err


def test_run_subprocess_async_preserves_partial_stderr_on_timeout():
    """async 경로도 동일하게 타임아웃 시 부분 stderr 를 보존해야 한다."""
    script = (
        "import sys, time\n"
        "sys.stderr.write('MARKER_STDERR_ASYNC_9182752')\n"
        "sys.stderr.flush()\n"
        "time.sleep(5)\n"
    )

    async def run():
        return await run_subprocess_async(
            [sys.executable, "-c", script], timeout=1)

    out, err, rc, timed = asyncio.run(run())
    assert timed is True
    assert rc == 124
    assert b"MARKER_STDERR_ASYNC_9182752" in err


def test_run_subprocess_sync_delivers_input_bytes():
    """input_bytes 는 stdin 으로 write-then-close 전달돼야 한다 (issue #30)."""
    script = "import sys; sys.stdout.write(sys.stdin.read())"
    out, _err, rc, timed = run_subprocess_sync(
        [sys.executable, "-c", script], timeout=5,
        input_bytes=b"hello from stdin (issue #30)")
    assert out == b"hello from stdin (issue #30)"
    assert rc == 0
    assert timed is False


def test_run_subprocess_sync_input_bytes_none_keeps_stdin_devnull():
    """input_bytes 미지정이면 기존과 동일하게 stdin 이 DEVNULL 이라 즉시 EOF."""
    out, _err, _rc, timed = run_subprocess_sync(
        ["sh", "-c", "cat; printf done"], timeout=5)
    assert out == b"done"
    assert timed is False


def test_run_subprocess_async_delivers_input_bytes():
    """async 경로도 동일하게 input_bytes 를 stdin 으로 전달해야 한다."""
    script = "import sys; sys.stdout.write(sys.stdin.read())"

    async def run():
        return await run_subprocess_async(
            [sys.executable, "-c", script], timeout=5,
            input_bytes=b"hello async stdin (issue #30)")

    out, _err, rc, timed = asyncio.run(run())
    assert out == b"hello async stdin (issue #30)"
    assert rc == 0
    assert timed is False


def test_run_subprocess_sync_input_bytes_timeout_preserves_partial_stderr():
    """input_bytes 를 쓰는 큰 프롬프트 경로에서도 issue #34 의 timeout 부분
    stderr 보존 계약이 유지돼야 한다 (write-then-close 후 자식이 멈춰도 kill 전
    까지 쓴 stderr 는 합성 문자열로 덮이지 않는다)."""
    script = (
        "import sys, time\n"
        "sys.stdin.read()\n"
        "sys.stderr.write('MARKER_STDIN_SYNC_9182761')\n"
        "sys.stderr.flush()\n"
        "time.sleep(5)\n"
    )
    out, err, rc, timed = run_subprocess_sync(
        [sys.executable, "-c", script], timeout=1,
        input_bytes=b"large prompt payload")
    assert timed is True
    assert rc == 124
    assert b"MARKER_STDIN_SYNC_9182761" in err


def test_run_subprocess_async_input_bytes_timeout_preserves_partial_stderr():
    """async 경로도 동일 계약: input_bytes + timeout → 부분 stderr 보존."""
    script = (
        "import sys, time\n"
        "sys.stdin.read()\n"
        "sys.stderr.write('MARKER_STDIN_ASYNC_9182762')\n"
        "sys.stderr.flush()\n"
        "time.sleep(5)\n"
    )

    async def run():
        return await run_subprocess_async(
            [sys.executable, "-c", script], timeout=1,
            input_bytes=b"large prompt payload")

    out, err, rc, timed = asyncio.run(run())
    assert timed is True
    assert rc == 124
    assert b"MARKER_STDIN_ASYNC_9182762" in err


def _zombie_alive(tag: str) -> bool:
    r = subprocess.run(["pgrep", "-f", f"sleep {tag}"],
                       capture_output=True, text=True)
    return bool([p for p in r.stdout.split() if p])


def _cleanup(tag: str) -> None:
    subprocess.run(["pkill", "-f", f"sleep {tag}"], capture_output=True)


def _wait_gone(tag: str, timeout: float = 5.0) -> bool:
    """좀비 손자가 사라질 때까지 폴링 — 고정 sleep 대신 조건 대기로 부하 무관 견고.

    SIGKILL 후 OS 가 손자를 reap 하는 데 부하 시 시간이 더 걸릴 수 있어, 고정
    0.4s 대기는 간헐 실패를 낳았다. 사라지면 즉시 True 반환.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _zombie_alive(tag):
            return True
        time.sleep(0.05)
    return not _zombie_alive(tag)


def test_run_subprocess_sync_reaps_grandchild():
    """타임아웃 시 그룹 kill → 손자 sleep 이 남지 않아야 한다."""
    tag = "9182731"
    try:
        _out, _err, _rc, timed = run_subprocess_sync(
            ["sh", "-c", f"sleep {tag} & exit 0"], timeout=1)
        assert timed is True
        assert _wait_gone(tag), "그룹 kill 후 좀비 sleep 손자가 남으면 안 됨"
    finally:
        _cleanup(tag)


def test_run_subprocess_async_reaps_grandchild():
    """async 경로도 동일하게 손자까지 reap."""
    tag = "9182732"

    async def run():
        return await run_subprocess_async(
            ["sh", "-c", f"sleep {tag} & exit 0"], timeout=1)

    try:
        _out, _err, _rc, timed = asyncio.run(run())
        assert timed is True
        assert _wait_gone(tag)
    finally:
        _cleanup(tag)


def test_run_subprocess_sync_reaps_detached_grandchild_on_clean_exit():
    """정상완료(타임아웃 아님)여도 떨어져나간 손자를 reap (Fix A).

    손자 sleep 이 stdout 을 /dev/null 로 돌려 파이프를 안 물면 sh 가 즉시
    종료 → communicate 정상 반환(타임아웃 아님). 그래도 finally 의 그룹 kill 이
    살아있는 손자를 reap 해야 한다."""
    tag = "9182741"
    try:
        _out, _err, rc, timed = run_subprocess_sync(
            ["sh", "-c", f"sleep {tag} >/dev/null 2>&1 & exit 0"], timeout=5)
        assert timed is False, "손자가 파이프를 안 물어 정상완료여야 함"
        assert rc == 0
        assert _wait_gone(tag), "정상완료여도 떨어져나간 손자가 reap 돼야 함"
    finally:
        _cleanup(tag)


def test_run_subprocess_async_reaps_detached_grandchild_on_clean_exit():
    tag = "9182742"

    async def run():
        return await run_subprocess_async(
            ["sh", "-c", f"sleep {tag} >/dev/null 2>&1 & exit 0"], timeout=5)

    try:
        _out, _err, rc, timed = asyncio.run(run())
        assert timed is False
        assert rc == 0
        assert _wait_gone(tag)
    finally:
        _cleanup(tag)


def test_run_stream_template_reaps_grandchild():
    """스트리밍 경로(_run_stream_template)도 idle 타임아웃 시 그룹 전체 killpg.

    직속 sh 가 foreground ``sleep`` 으로 살아있고 stdout 출력이 없어 idle
    timeout 이 걸린다 → ``_kill_process_group`` 이 그룹을 SIGKILL → 백그라운드
    손자 ``sleep <tag>`` 까지 reap. (mock proc 테스트는 .pid 가 없어 killpg
    분기를 타지 못하므로, 실제 손자로 스트리밍 분기를 검증한다.)
    """
    tag = "9182733"
    prov = ClaudeProvider()

    async def run():
        chunks = []
        async for c in prov._run_stream_template(
                ["sh", "-c", f"sleep {tag} & sleep 10"],
                StreamState(), timeout=1):
            chunks.append(c)
        return chunks

    try:
        chunks = asyncio.run(run())
        assert any(c.type == "error" for c in chunks), "idle timeout error chunk 기대"
        assert _wait_gone(tag), "스트리밍 그룹 kill 후 좀비 손자가 남으면 안 됨"
    finally:
        _cleanup(tag)


# ===== post-kill communicate retry: logging + single-attempt guarantee =====
#
# When the kill-then-retry `communicate()` itself fails (a pathological
# grandchild still holding the pipe), the fallback to empty bytes used to be
# silent. It should now log a warning (never including output payloads). For
# the sync runner specifically, that failure must not trigger a *second*
# bounded 10s wait from the `finally` block's own `communicate(timeout=10)` --
# that would reintroduce the ~20s worst case commit 13dd1c8 removed.

def test_run_subprocess_sync_double_communicate_failure_logs_and_single_attempt(
        monkeypatch, caplog):
    """Force TimeoutExpired on both the initial and the post-kill retry
    communicate() calls. Must fall back to empty bytes, log a warning, and
    call communicate() at most twice total (not a third time from finally)."""
    fake_proc = MagicMock()
    fake_proc.returncode = None
    fake_proc.pid = 999001

    def _always_times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["fake"], timeout=kwargs.get("timeout"))

    fake_proc.communicate.side_effect = _always_times_out

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: fake_proc)
    monkeypatch.setattr(
        "agentcli.providers.base._kill_process_group", lambda proc: None)

    with caplog.at_level(logging.WARNING, logger="agentcli.providers.base"):
        out, err, rc, timed_out = run_subprocess_sync(["fake"], timeout=1)

    assert out == b""
    assert err == b""
    assert timed_out is True
    assert rc == 124
    # initial communicate(timeout=1) + one post-kill retry communicate(timeout=10)
    # -- the finally block must NOT fire a third (that would double the 10s wait).
    assert fake_proc.communicate.call_count == 2
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("post-kill communicate retry failed" in m for m in warnings)


def test_run_subprocess_async_communicate_retry_failure_logs_empty_fallback(
        monkeypatch, caplog):
    """Force the bounded post-kill retry (`asyncio.wait_for(task, timeout=10)`)
    to raise -- the pathological case where a grandchild still holds the pipe
    after the group kill. Must fall back to empty bytes and log a warning."""

    async def _hangs_forever(*args, **kwargs):
        await asyncio.sleep(1000)

    fake_proc = MagicMock()
    fake_proc.returncode = None
    fake_proc.pid = 999002
    fake_proc.communicate = _hangs_forever
    fake_proc.wait = AsyncMock(return_value=0)

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    async def _fake_wait_for(_aw, timeout):
        # Simulates the bounded post-kill retry itself timing out/failing.
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(
        "agentcli.providers.base._kill_process_group", lambda proc: None)

    async def run():
        return await run_subprocess_async(["fake"], timeout=0.01)

    with caplog.at_level(logging.WARNING, logger="agentcli.providers.base"):
        out, err, rc, timed_out = asyncio.run(run())

    assert out == b""
    assert err == b""
    assert timed_out is True
    assert rc == 124
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("post-kill communicate retry failed" in m for m in warnings)
