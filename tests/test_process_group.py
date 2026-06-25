"""프로세스 그룹 teardown 회귀 테스트 (좀비/고아 손자 방지).

확인된 원인: CLI(claude 등)가 띄운 MCP 서버·hook **손자** 프로세스는 직속
자식만 kill 하면 고아로 남아 누적된다. ``run_subprocess_sync`` /
``run_subprocess_async`` 는 새 세션(프로세스 그룹)으로 띄운 뒤 타임아웃 시
그룹 전체를 killpg 하여 손자까지 reap 해야 한다.

재현 모델: ``sh -c 'sleep <tag> & exit 0'`` — 직속 sh 는 즉시 종료하지만
백그라운드 손자 ``sleep`` 이 stdout 파이프를 물고 살아남는다(=고아).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

import pytest

from agentcli.providers.base import run_subprocess_async, run_subprocess_sync

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="process-group teardown is POSIX-only")


def _orphan_alive(tag: str) -> bool:
    r = subprocess.run(["pgrep", "-f", f"sleep {tag}"],
                       capture_output=True, text=True)
    return bool([p for p in r.stdout.split() if p])


def _cleanup(tag: str) -> None:
    subprocess.run(["pkill", "-f", f"sleep {tag}"], capture_output=True)


def test_run_subprocess_sync_reaps_grandchild():
    """타임아웃 시 그룹 kill → 손자 sleep 이 남지 않아야 한다."""
    tag = "9182731"
    try:
        _out, _err, _rc, timed = run_subprocess_sync(
            ["sh", "-c", f"sleep {tag} & exit 0"], timeout=1)
        assert timed is True
        time.sleep(0.4)
        assert not _orphan_alive(tag), "그룹 kill 후 고아 sleep 손자가 남으면 안 됨"
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
        time.sleep(0.4)
        assert not _orphan_alive(tag)
    finally:
        _cleanup(tag)
