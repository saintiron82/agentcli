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
import os
import subprocess
import time

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
