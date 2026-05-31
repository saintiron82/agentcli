"""3-provider ``_dispatch_stream_event`` 단위 테스트.

provider 가 각 CLI 의 JSON event 를 정규화된 StreamChunk 로 매핑하는 핵심 hook.
subprocess 없이 evt dict + StreamState 만 mock 으로 주입 → chunk 결정적 검증.

triad-review 합의 (PR #8) follow-up issue #11 의 최우선 항목.
"""
from __future__ import annotations

import asyncio

import pytest

from agentcli.providers.base import StreamState
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.types import TokenUsage


def _collect_chunks(provider, evt: dict, state: StreamState) -> list:
    """``_dispatch_stream_event`` 가 yield 한 chunk 모두 모음."""

    async def run():
        return [c async for c in provider._dispatch_stream_event(evt, state)]

    return asyncio.run(run())


# ============================================================
# ClaudeProvider._dispatch_stream_event
# ============================================================


class TestClaudeDispatch:
    def setup_method(self):
        self.provider = ClaudeProvider()
        self.state = StreamState(final_session_id="initial")

    def test_system_event_updates_session_id(self):
        evt = {"type": "system", "session_id": "sys-xyz"}
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert self.state.final_session_id == "sys-xyz"
        assert len(chunks) == 1
        assert chunks[0].type == "event"
        assert chunks[0].session_id == "sys-xyz"

    def test_assistant_text_block_becomes_text_chunk(self):
        evt = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "world"},
                ]
            },
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        text_chunks = [c for c in chunks if c.type == "text"]
        assert [c.content for c in text_chunks] == ["Hello ", "world"]
        assert self.state.text_parts == ["Hello ", "world"]

    def test_assistant_thinking_block_becomes_thinking_chunk(self):
        evt = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "internal reasoning"},
                ]
            },
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "thinking"
        assert chunks[0].content == "internal reasoning"
        # thinking 은 text_parts 에 누적되지 않는다 (정규화 계약)
        assert self.state.text_parts == []

    def test_assistant_tool_use_block_becomes_tool_use_chunk(self):
        evt = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Bash"},
                ]
            },
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "tool_use"
        assert chunks[0].data["id"] == "tu_1"

    def test_user_tool_result_block_becomes_tool_result_chunk(self):
        evt = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "ok"},
                ]
            },
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "tool_result"

    def test_result_event_updates_session_and_usage(self):
        self.state.final_usage = TokenUsage(payload_prompt_tokens=10)
        evt = {
            "type": "result",
            "usage": {"input_tokens": 15, "output_tokens": 5},
            "session_id": "result-sid",
        }
        _collect_chunks(self.provider, evt, self.state)
        assert self.state.final_session_id == "result-sid"
        assert self.state.final_usage.prompt_tokens == 15
        assert self.state.final_usage.completion_tokens == 5
        assert self.state.final_usage.total_tokens == 20
        # payload_prompt_tokens (seed) 는 보존되어야 함
        assert self.state.final_usage.payload_prompt_tokens == 10

    def test_unknown_event_type_falls_through_to_event_chunk(self):
        evt = {"type": "unknown-future-type", "data": 1}
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "event"


# ============================================================
# CodexProvider._dispatch_stream_event
# ============================================================


class TestCodexDispatch:
    def setup_method(self):
        self.provider = CodexProvider()
        self.state = StreamState(final_session_id="initial")

    def test_thread_started_updates_session_id(self):
        evt = {"type": "thread.started", "thread_id": "thread-abc"}
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert self.state.final_session_id == "thread-abc"
        assert len(chunks) == 1
        assert chunks[0].type == "event"
        assert chunks[0].session_id == "thread-abc"

    def test_agent_message_becomes_text_chunk(self):
        evt = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "hi from codex"},
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert [c.content for c in chunks if c.type == "text"] == ["hi from codex"]
        assert self.state.text_parts == ["hi from codex"]

    def test_initial_greeting_is_filtered_when_no_text_yet(self):
        evt = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Ready. What would you like me to work on?",
            },
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        # initial greeting 은 text_parts 가 비어있을 때 무시
        assert chunks == []
        assert self.state.text_parts == []

    def test_reasoning_item_becomes_thinking_chunk(self):
        evt = {
            "type": "item.completed",
            "item": {"type": "reasoning", "text": "let me think"},
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "thinking"
        assert chunks[0].content == "let me think"

    @pytest.mark.parametrize("itype", ["command_execution", "tool_call"])
    def test_tool_items_become_tool_use_chunk(self, itype):
        evt = {
            "type": "item.completed",
            "item": {"type": itype, "command": "ls"},
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "tool_use"

    def test_turn_completed_accumulates_usage_with_cached(self):
        self.state.final_usage = TokenUsage(payload_prompt_tokens=8)
        evt = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "cached_input_tokens": 5,
            },
        }
        _collect_chunks(self.provider, evt, self.state)
        assert self.state.final_usage.prompt_tokens == 20
        assert self.state.final_usage.completion_tokens == 10
        assert self.state.final_usage.total_tokens == 30
        assert self.state.final_usage.cached_tokens == 5
        # payload seed 보존
        assert self.state.final_usage.payload_prompt_tokens == 8

    def test_error_event_becomes_error_chunk(self):
        evt = {"type": "error", "message": "rate limit"}
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "error"
        assert chunks[0].content == "rate limit"

    def test_turn_failed_becomes_error_chunk(self):
        evt = {"type": "turn.failed", "error": {"message": "model overloaded"}}
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "error"
        assert chunks[0].content == "model overloaded"


# ============================================================
# CopilotProvider._dispatch_stream_event
# ============================================================


class TestCopilotDispatch:
    def setup_method(self):
        self.provider = CopilotProvider()
        self.state = StreamState(final_session_id="initial")

    def test_message_delta_yields_text_and_accumulates(self):
        evt1 = {
            "type": "assistant.message_delta",
            "data": {"deltaContent": "Hel"},
        }
        evt2 = {
            "type": "assistant.message_delta",
            "data": {"deltaContent": "lo"},
        }
        c1 = _collect_chunks(self.provider, evt1, self.state)
        c2 = _collect_chunks(self.provider, evt2, self.state)
        assert [c.content for c in c1] == ["Hel"]
        assert [c.content for c in c2] == ["lo"]
        assert self.state.text_parts == ["Hel", "lo"]

    def test_assistant_message_used_when_no_delta_arrived(self):
        # delta 가 도착하지 않은 상태에서 final message 가 와도 content 보충
        evt = {
            "type": "assistant.message",
            "data": {"content": "final fallback", "outputTokens": 4},
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert [c.content for c in chunks if c.type == "text"] == ["final fallback"]
        assert self.state.text_parts == ["final fallback"]

    def test_assistant_message_ignored_when_delta_already_present(self):
        # delta 가 먼저 누적됐다면 final message 의 content 는 무시
        self.state.text_parts.append("delta-accumulated")
        evt = {
            "type": "assistant.message",
            "data": {"content": "should-not-add", "outputTokens": 7},
        }
        chunks = _collect_chunks(self.provider, evt, self.state)
        text_chunks = [c for c in chunks if c.type == "text"]
        assert text_chunks == []
        # text_parts 도 변함 없음
        assert self.state.text_parts == ["delta-accumulated"]
        # 그러나 outputTokens 는 누적
        assert self.state.final_usage is not None
        assert self.state.final_usage.completion_tokens == 7

    def test_assistant_message_accumulates_output_tokens_across_calls(self):
        self.state.final_usage = TokenUsage(
            prompt_tokens=0, completion_tokens=3, total_tokens=3,
            cached_tokens=0)
        evt = {
            "type": "assistant.message",
            "data": {"content": "x", "outputTokens": 4},
        }
        # 이미 delta 누적이 있다고 가정해 content 보충 분기는 안 타게
        self.state.text_parts.append("prev")
        _collect_chunks(self.provider, evt, self.state)
        assert self.state.final_usage.completion_tokens == 3 + 4
        assert self.state.final_usage.total_tokens == 3 + 4

    def test_result_event_updates_session_id(self):
        evt = {"type": "result", "sessionId": "session-from-result"}
        _collect_chunks(self.provider, evt, self.state)
        assert self.state.final_session_id == "session-from-result"

    @pytest.mark.parametrize("etype", ["assistant.tool_use", "tool.invocation"])
    def test_tool_events_become_tool_use_chunk(self, etype):
        evt = {"type": etype, "data": {"name": "shell"}}
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "tool_use"

    def test_unknown_event_falls_through_to_event_chunk(self):
        evt = {"type": "session.something_new"}
        chunks = _collect_chunks(self.provider, evt, self.state)
        assert len(chunks) == 1
        assert chunks[0].type == "event"
