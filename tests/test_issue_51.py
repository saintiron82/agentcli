"""Regression tests for issue #51 — claude cache-token visibility + real
system-prompt flags.

Part 1 (usage): Anthropic bills cache reads at ~1/10 of the input rate and
cache creation at 1.25x, and the claude CLI already reports both
(``usage.cache_read_input_tokens`` / ``usage.cache_creation_input_tokens``).
``_parse_claude_json`` used to drop them — callers had zero visibility into
whether isolating static context earns a caching discount. codex has surfaced
its equivalent (``cached_input_tokens``) since day one, so this closes a
normalization gap.

Anthropic's ``input_tokens`` EXCLUDES cache reads/creation (unlike OpenAI's,
which includes cached). agentcli's normalized contract is
"``cached_tokens`` ⊆ ``prompt_tokens``" (see ``TokenUsage``), so claude's
``prompt_tokens`` must become ``input + cache_read + cache_creation`` to keep
the invariant — otherwise a 14k-token cached context reports as
``prompt_tokens=4``.

Part 2 (flags): tested in this file as well — system messages must reach the
CLI as a real ``--append-system-prompt`` block instead of being flattened into
the single ``-p`` string, or the static block can never sit on a stable,
cacheable prefix.

Reference: https://github.com/saintiron82/agentcli/issues/51
"""

import json

from unittest.mock import patch

from agentcli.providers.base import StreamState
from agentcli.providers.claude import ClaudeProvider, _parse_claude_json
from agentcli.types import Message, TokenUsage

SID = "aaaa1111-2222-3333-4444-555566667777"


# ---- Part 1: cache token fields --------------------------------------------

def test_parse_claude_json_surfaces_cache_tokens():
    """cache_read → cached_tokens, cache_creation → cache_creation_tokens,
    prompt_tokens 는 셋의 합(실제 입력 컨텍스트 전체)."""
    payload = json.dumps({
        "type": "result", "subtype": "success", "result": "ok",
        "session_id": SID,
        "usage": {"input_tokens": 12, "output_tokens": 34,
                  "cache_read_input_tokens": 4444,
                  "cache_creation_input_tokens": 555}})
    content, tokens, err = _parse_claude_json(payload)
    assert err == ""
    assert tokens.cached_tokens == 4444
    assert tokens.cache_creation_tokens == 555
    assert tokens.prompt_tokens == 12 + 4444 + 555
    assert tokens.completion_tokens == 34
    assert tokens.total_tokens == tokens.prompt_tokens + 34


def test_parse_claude_json_without_cache_fields_unchanged():
    """캐시 필드가 없으면 기존과 동일 (하위호환)."""
    payload = json.dumps({
        "type": "result", "subtype": "success", "result": "ok",
        "usage": {"input_tokens": 7, "output_tokens": 3}})
    _content, tokens, _err = _parse_claude_json(payload)
    assert tokens.prompt_tokens == 7
    assert tokens.cached_tokens == 0
    assert tokens.cache_creation_tokens == 0
    assert tokens.total_tokens == 10


def test_token_usage_has_cache_creation_field_default_zero():
    """새 필드는 기본 0 — codex/copilot 경로는 건드릴 필요가 없다."""
    assert TokenUsage().cache_creation_tokens == 0


def test_invoke_propagates_cache_tokens():
    """invoke() 응답의 LLMResponse.tokens 까지 캐시 필드가 도달한다."""
    payload = json.dumps({
        "type": "result", "subtype": "success", "result": "ok",
        "session_id": SID,
        "usage": {"input_tokens": 2, "output_tokens": 5,
                  "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 100}})
    with patch.object(ClaudeProvider, "_find_binary",
                      return_value="/usr/bin/claude"), \
         patch("agentcli.providers.claude.run_subprocess_sync",
               return_value=(payload.encode(), b"", 0, False)):
        resp = ClaudeProvider().invoke([Message(role="user", content="hi")])
    assert resp.tokens.cached_tokens == 1000
    assert resp.tokens.cache_creation_tokens == 100
    assert resp.tokens.prompt_tokens == 2 + 1000 + 100


def _drain(agen):
    """async generator 를 동기적으로 소진한다 (이벤트 루프 1회)."""
    import asyncio

    async def run():
        return [c async for c in agen]
    return asyncio.run(run())


def test_stream_result_event_carries_cache_tokens():
    """stream-json 의 result 이벤트도 같은 매핑으로 final_usage 를 채운다."""
    p = ClaudeProvider()
    state = StreamState()
    evt = {"type": "result", "subtype": "success", "result": "ok",
           "session_id": SID,
           "usage": {"input_tokens": 3, "output_tokens": 8,
                     "cache_read_input_tokens": 2000,
                     "cache_creation_input_tokens": 250}}
    _drain(p._dispatch_stream_event(evt, state))
    assert state.final_usage is not None
    assert state.final_usage.cached_tokens == 2000
    assert state.final_usage.cache_creation_tokens == 250
    assert state.final_usage.prompt_tokens == 3 + 2000 + 250
    assert state.final_usage.total_tokens == 3 + 2000 + 250 + 8


# ---- Part 2: real --append-system-prompt(-file) wiring ----------------------

_OK_PAYLOAD = json.dumps({
    "type": "result", "subtype": "success", "result": "ok",
    "session_id": SID, "usage": {"input_tokens": 1, "output_tokens": 1}})


def _capture_invoke(messages, fake_run=None):
    """invoke 1회를 스텁 위에서 돌리고 run_subprocess_sync 의 call_args 반환."""
    if fake_run is None:
        with patch.object(ClaudeProvider, "_find_binary",
                          return_value="/usr/bin/claude"), \
             patch("agentcli.providers.claude.run_subprocess_sync",
                   return_value=(_OK_PAYLOAD.encode(), b"", 0, False)) as m:
            ClaudeProvider().invoke(messages)
        return m.call_args
    with patch.object(ClaudeProvider, "_find_binary",
                      return_value="/usr/bin/claude"), \
         patch("agentcli.providers.claude.run_subprocess_sync",
               side_effect=fake_run) as m:
        ClaudeProvider().invoke(messages)
    return m.call_args


def test_system_message_becomes_real_flag():
    """system 메시지는 ``--append-system-prompt`` 로 가고, ``-p`` 프롬프트에서는
    빠진다 — 정적 블록이 안정된 (캐시 가능한) prefix 로 격리되는 조건 (#51)."""
    call = _capture_invoke([Message(role="system", content="You are terse."),
                            Message(role="user", content="hi")])
    cmd = call.args[0]
    assert "--append-system-prompt" in cmd
    assert cmd[cmd.index("--append-system-prompt") + 1] == "You are terse."
    prompt_arg = cmd[cmd.index("-p") + 1]
    assert prompt_arg == "hi"
    assert "System instructions:" not in prompt_arg


def test_no_system_message_adds_no_flag():
    """system 메시지가 없으면 argv 는 기존과 동일 (하위호환)."""
    call = _capture_invoke([Message(role="user", content="hi")])
    assert "--append-system-prompt" not in call.args[0]
    assert "--append-system-prompt-file" not in call.args[0]


def test_multiple_system_messages_joined():
    """복수 system 메시지는 build_session_prompt 와 같은 규칙(빈 줄)으로 합친다."""
    call = _capture_invoke([Message(role="system", content="A."),
                            Message(role="system", content="B."),
                            Message(role="user", content="hi")])
    cmd = call.args[0]
    assert cmd[cmd.index("--append-system-prompt") + 1] == "A.\n\nB."


def test_large_system_prompt_routes_through_file():
    """임계치 초과 system 블록은 argv 대신 파일로 — Windows 32,767자 argv 한계
    안전. spawn 시점에 파일이 존재하고, 호출이 끝나면 정리된다."""
    import os

    from agentcli.providers.base import PROMPT_STDIN_THRESHOLD

    big = "R" * (PROMPT_STDIN_THRESHOLD + 500)
    seen = {}

    def fake_run(cmd, **kw):
        idx = cmd.index("--append-system-prompt-file")
        seen["path"] = cmd[idx + 1]
        with open(seen["path"], encoding="utf-8") as f:
            seen["content"] = f.read()
        return (_OK_PAYLOAD.encode(), b"", 0, False)

    call = _capture_invoke([Message(role="system", content=big),
                            Message(role="user", content="hi")], fake_run)
    assert "--append-system-prompt" not in call.args[0]
    assert seen["content"] == big
    assert not os.path.exists(seen["path"]), (
        "임시 system-prompt 파일은 invoke 종료 후 남으면 안 된다")


def test_stream_async_wires_system_flag_too():
    """스트리밍 경로도 같은 배선을 탄다 (chat_stream 의 대형 seed 시나리오)."""
    captured = {}

    async def fake_template(self, cmd, state, **kw):
        captured["cmd"] = cmd
        return
        yield  # pragma: no cover — async generator 로 만들기 위한 표식

    with patch.object(ClaudeProvider, "_find_binary",
                      return_value="/usr/bin/claude"), \
         patch.object(ClaudeProvider, "_run_stream_template", fake_template):
        _drain(ClaudeProvider().stream_async(
            [Message(role="system", content="Rules."),
             Message(role="user", content="go")]))
    cmd = captured["cmd"]
    assert "--append-system-prompt" in cmd
    assert cmd[cmd.index("--append-system-prompt") + 1] == "Rules."
    assert cmd[cmd.index("-p") + 1] == "go"


def test_system_only_messages_fall_back_to_flattening():
    """user 턴이 없는 퇴화 입력은 기존 평탄화 유지 — 빈 ``-p`` 를 만들지 않는다."""
    call = _capture_invoke([Message(role="system", content="only system")])
    cmd = call.args[0]
    assert "--append-system-prompt" not in cmd
    assert "only system" in cmd[cmd.index("-p") + 1]


def test_redact_argv_hides_system_prompt_payload():
    """debug trace 에 system 블록 본문이 새지 않는다."""
    from agentcli.providers.base import redact_argv

    redacted = redact_argv(["claude", "-p", "hi",
                            "--append-system-prompt", "SECRET RULES"])
    assert "SECRET RULES" not in redacted
    idx = redacted.index("--append-system-prompt")
    assert redacted[idx + 1] == f"<system-prompt:{len('SECRET RULES')} chars>"
