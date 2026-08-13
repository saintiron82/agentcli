"""issue #54 — warm 세션에서 응답이 길면 LimitOverrunError 로 턴이 실패한다.

근본 원인: ``WarmSession._start`` 가 ``asyncio.create_subprocess_exec`` 를
``limit=`` 없이 불러 stdout StreamReader 가 기본 64KiB 한도를 갖고, 상주
프로토콜의 이벤트는 **한 줄에 하나**라 assistant 이벤트 한 줄이 64KiB 를
넘는 순간 ``readline()`` 이 ``ValueError``("Separator is found, but chunk is
longer than limit") 를 던진다. 요청 크기가 아니라 응답 한 줄의 길이가
임계값을 넘느냐의 문제 — 이슈의 정정 코멘트("행 수를 줄여도 실패")와 일치.

여기 테스트는 가짜 스트림이 아니라 **실제 spawn 경로**(진짜 파이프 + 진짜
StreamReader)를 지나야 의미가 있다 — 한도는 이벤트 루프의 transport 계층에
있기 때문이다.
"""

import asyncio
import sys
from unittest.mock import patch

import pytest

from agentcli.providers import warm
from agentcli.providers.warm import WarmSession, WarmSessionError, open_warm

# stream-json 프로토콜 흉내: user 메시지 한 줄을 받으면 지정 크기의 텍스트를
# 담은 assistant 이벤트 한 줄 + result 한 줄을 내보내고 끝난다.
_LONG_LINE_CHILD = """
import json, sys
sys.stdin.readline()
text = "x" * {size}
sys.stdout.write(json.dumps({{"type": "assistant", "session_id": "s-54",
    "message": {{"role": "assistant",
                 "content": [{{"type": "text", "text": text}}]}}}}) + "\\n")
sys.stdout.write(json.dumps({{"type": "result", "subtype": "success",
    "session_id": "s-54"}}) + "\\n")
sys.stdout.flush()
"""


def _long_line_session(size: int, **kw) -> WarmSession:
    code = _LONG_LINE_CHILD.format(size=size)
    return WarmSession(cmd=[sys.executable, "-c", code], **kw)


def test_single_event_over_64kib_survives():
    """assistant 이벤트 한 줄이 64KiB 를 넘어도 턴이 완주한다 (#54 재현)."""
    s = _long_line_session(100_000)

    async def run():
        async with s:
            return await s.send("표를 항목 단위 JSON 으로 다시 써줘")

    assert asyncio.run(run()) == "x" * 100_000


def test_oversized_event_raises_warm_session_error():
    """한도를 정말 넘으면 원시 ValueError 대신 WarmSessionError.

    WarmSessionError 는 "닫고 다시 연다" 계약(클래스 docstring)이라 호출자가
    복구 경로를 안다 — asyncio 내부 문구("Separator is found ...")를 그대로
    올려보내면 호출자는 뭘 해야 하는지 알 수 없다.
    """
    s = _long_line_session(300_000, stream_limit=64 * 1024)

    async def run():
        async with s:
            await s.send("q")

    with pytest.raises(WarmSessionError, match="stream_limit"):
        asyncio.run(run())


def test_stream_limit_reaches_the_subprocess_pipe():
    """stream_limit 이 spawn 의 ``limit=`` 로 전달된다 — 기본값도 64KiB 초과."""
    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("spawn 중단 — kwargs 만 확인")

    async def run(sess):
        with patch("asyncio.create_subprocess_exec", fake_exec):
            await sess.send("q")

    with pytest.raises(RuntimeError):
        asyncio.run(run(WarmSession(cmd=["claude"])))
    assert captured["limit"] == warm._DEFAULT_STREAM_LIMIT
    assert warm._DEFAULT_STREAM_LIMIT > 64 * 1024

    captured.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(run(WarmSession(cmd=["claude"], stream_limit=1234_567)))
    assert captured["limit"] == 1234_567


def test_open_warm_wires_stream_limit():
    """open_warm 경유로도 같은 인자가 세션까지 온다."""
    async def run():
        return await open_warm(binary="claude", stream_limit=2 * 1024 * 1024)

    s = asyncio.run(run())
    assert s._stream_limit == 2 * 1024 * 1024
