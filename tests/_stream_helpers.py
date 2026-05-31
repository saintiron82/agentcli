"""Mock subprocess helpers for stream_async runtime circuit tests.

issue #11 인프라 — `_run_stream_template`, `run_subprocess_async`,
`_dispatch_stream_event` 의 runtime 회로를 결정적으로 테스트하기 위한
fake asyncio subprocess 헬퍼. issue #9 (wall_timeout=0) 와 issue #10
(GeneratorExit 좀비) 회귀 테스트도 같은 인프라를 공유.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock


class FakeReadline:
    """JSONL 라인 큐를 순서대로 돌려주는 readline mock. EOF 는 ``b""`` 로 신호."""

    def __init__(self, lines: list[bytes]):
        # 자동으로 끝에 EOF sentinel 추가
        self._lines = list(lines) + [b""]
        self._idx = 0

    async def readline(self) -> bytes:
        if self._idx >= len(self._lines):
            return b""
        line = self._lines[self._idx]
        self._idx += 1
        return line


class HangingReadline:
    """readline 호출 시 영원히 await — idle timeout 시뮬레이션용."""

    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""  # 도달 불가


def jsonl_bytes(events: list[dict]) -> list[bytes]:
    """dict 리스트를 JSONL bytes 리스트로 변환 (readline 큐 입력용)."""
    return [(json.dumps(e) + "\n").encode("utf-8") for e in events]


def make_fake_proc(
    stdout_lines: list[bytes] | None = None,
    *,
    returncode: int | None = 0,
    stderr_bytes: bytes = b"",
):
    """controllable stdout/stderr/returncode 를 가진 fake asyncio Process.

    ``proc.kill`` 은 MagicMock — 호출 추적 가능 (GeneratorExit 테스트용).
    ``proc.wait`` 는 returncode 를 돌려주는 AsyncMock.
    """
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.stdout = FakeReadline(stdout_lines or [])
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr_bytes)
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode if returncode is not None else 0)
    proc.kill = MagicMock()
    return proc


def patch_subprocess_exec(monkeypatch, fake_proc):
    """``asyncio.create_subprocess_exec`` 를 패치해 ``fake_proc`` 를 반환.

    호출 인자 (cmd, env, cwd, stdin/stdout/stderr) 는 ``fake_proc.call_args``
    같은 식으로 추적 안 됨 — 필요하면 별도 spy 사용.
    """
    async def fake_create(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)


async def collect(agen) -> list:
    """async generator 의 모든 chunk 수집."""
    return [c async for c in agen]


async def collect_until(agen, predicate) -> list:
    """``predicate(chunk)`` 가 True 되는 첫 chunk 까지 수집 (그 chunk 포함).

    issue #10 GeneratorExit 테스트용 — early break 시뮬레이션.
    """
    out = []
    async for c in agen:
        out.append(c)
        if predicate(c):
            break
    return out
