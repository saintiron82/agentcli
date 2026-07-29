"""issue #44 pin: copilot 은 CLI 자체에 stdin 프롬프트 모드가 없다.

claude/codex 는 stream_async 가 8000 UTF-8 바이트 초과 프롬프트를 stdin 으로
라우팅하도록 고쳐졌다(issue #44) — copilot 의 `-p`/`--prompt` 는 인자 전용이라
라이브러리 차원에서 이 우회를 적용할 수 없다(issue #44 문서화된 스코프).
이 파일은 그 사실을 회귀 감지용으로 고정한다: 향후 누군가 실수로 copilot 에도
stdin 라우팅을 얹으려 하면(혹은 다른 리팩토링이 우연히 그렇게 만들면) 이
테스트가 실패해야 한다 — copilot 은 어떤 크기의 프롬프트든 항상 argv 에
인라인해야 한다.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from agentcli.providers.base import PROMPT_STDIN_THRESHOLD
from agentcli.providers.copilot import CopilotProvider
from agentcli.types import Message, StreamChunk


def _big_prompt() -> str:
    return "x" * (PROMPT_STDIN_THRESHOLD + 1)


@patch("agentcli.providers.copilot.CopilotProvider._find_binary",
       return_value=("/usr/bin/copilot", False))
def test_build_cmd_always_inlines_prompt_regardless_of_size(mock_find):
    """copilot ``_build_cmd`` 는 (claude/codex 와 달리) prompt_via_stdin 파라미터
    자체가 없다 — 어떤 길이의 프롬프트도 항상 ``-p`` 뒤 위치 인자로 들어간다."""
    p = CopilotProvider()
    big = _big_prompt()
    cmd, _use_gh = p._build_cmd(big, "", "", output_format="json")
    assert cmd is not None
    assert cmd[cmd.index("-p") + 1] == big, (
        "copilot 은 CLI 에 stdin 프롬프트 모드가 없으므로 프롬프트가 항상 "
        "argv 에 그대로 인라인되어야 한다")


def test_stream_async_large_prompt_still_in_argv(monkeypatch):
    """stream_async 도 대형 프롬프트를 그대로 argv 로 넘겨야 한다 — claude/codex
    처럼 ``input_bytes`` 로 stdin 라우팅하면 안 된다(copilot 은 그 훅을 아예
    쓰지 않는다)."""
    captured: dict = {}

    async def fake_template(self, cmd, state, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        yield StreamChunk(type="done", session_id="s")

    monkeypatch.setattr(CopilotProvider, "_find_binary",
                        lambda self: ("/usr/bin/copilot", False))
    monkeypatch.setattr("agentcli.providers.copilot.build_env", lambda: {})
    monkeypatch.setattr(
        "agentcli.providers.base.LLMProvider._run_stream_template",
        fake_template)
    prov = CopilotProvider()
    big = _big_prompt()

    async def run():
        return [c async for c in prov.stream_async(
            [Message(role="user", content=big)])]

    asyncio.run(run())
    assert big in captured["cmd"], (
        "copilot 은 stdin 프롬프트 모드가 없으므로 큰 프롬프트도 argv 에 "
        "그대로 남아있어야 한다")
    # copilot stream_async 는 input_bytes 를 전혀 계산하지 않는다 — kwargs 에
    # 아예 나타나지 않아야 한다(claude/codex 는 항상 넘긴다, None 이라도).
    assert "input_bytes" not in captured, (
        "copilot 이 input_bytes 를 계산/전달하기 시작했다면 그건 이 스코프 "
        "밖의 회귀다 — CLI 에 stdin 프롬프트 모드가 없다는 전제가 깨진 것")
