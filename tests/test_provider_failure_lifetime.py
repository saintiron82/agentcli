import asyncio

import pytest

from agentcli import LLMClient, MemoryStore, ProviderRegistry
from agentcli.coordination import ConversationLease
from agentcli.providers.base import LLMProvider
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.providers.kiro import KiroProvider
from agentcli.types import LLMResponse, Message, StreamChunk, TokenUsage


@pytest.mark.parametrize("cls", [ClaudeProvider, CopilotProvider, KiroProvider])
@pytest.mark.parametrize("via_client", [False, True])
def test_provider_close_waits_for_delegated_cleanup(monkeypatch, cls, via_client):
    provider = cls()
    store = MemoryStore()
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()
        events = []
        async def inner(*args, **kwargs):
            try:
                yield StreamChunk(type="text", content="partial")
            finally:
                started.set()
                await finish.wait()
                events.append("closed")
        if cls is KiroProvider:
            monkeypatch.setattr(provider, "_find_binary", lambda: "fake")
            monkeypatch.setattr(provider, "_subprocess_conn_factory", lambda *args: (None, {}))
            monkeypatch.setattr(provider, "_acp_turn", inner)
        else:
            monkeypatch.setattr(provider, "_build_cmd", lambda *args, **kwargs: (["fake"], "sid"))
            monkeypatch.setattr(provider, "_run_stream_template", inner)
        if via_client:
            registry = ProviderRegistry()
            registry.register(provider)
            stream = LLMClient(store, registry).chat_stream("hi", provider=provider.provider_id, alias="same")
        else:
            stream = provider.stream_async([Message(role="user", content="hi")])
        assert (await anext(stream)).content == "partial"
        closing = asyncio.create_task(stream.aclose())
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert not closing.done()
            if via_client:
                cid = store.list_by_owner("")[0].id
                assert ConversationLease(store, cid).try_acquire() is None
        finally:
            finish.set()
            await closing
        assert events == ["closed"]
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("error_type", ["timeout", "auth", "unknown"])
def test_kiro_partial_failure_is_not_persisted_as_success(monkeypatch, mode, error_type):
    provider = KiroProvider()
    usage = TokenUsage(total_tokens=5)
    async def failing(*args, **kwargs):
        yield StreamChunk(type="text", content="partial", session_id="fake-sid")
        yield StreamChunk(type="error", content="failure", usage=usage,
                          data={"error_type": error_type, "exit_code": 1, "suggested_action": "service action"})
    monkeypatch.setattr(provider, "stream_async", failing)
    registry = ProviderRegistry()
    registry.register(provider)
    client = LLMClient(MemoryStore(), registry)
    kwargs = dict(provider="kiro", owner="o", alias="same")
    response = client.chat("hi", **kwargs) if mode == "sync" else asyncio.run(client.chat_async("hi", **kwargs))
    assert response.content == "" and response.raw_content == "partial"
    assert response.error_type == error_type and response.exit_code == 1
    assert response.session_id == "fake-sid" and response.tokens.total_tokens == 5
    assert response.suggested_action == "service action"
    assert client.get_token_stats(owner="o")["total_calls"] == 0


def test_base_stream_preserves_original_failure_contract():
    class Custom(LLMProvider):
        provider_id = "custom"
        def invoke(self, messages, **kwargs):
            return LLMResponse("partial", "custom", "", session_id="sid", error="auth failure",
                               error_type="auth", exit_code=1, suggested_action="service action")
        def is_available(self): return True
        def list_models(self): return []
    async def run():
        return [chunk async for chunk in Custom().stream_async([Message(role="user", content="hi")])]
    chunks = asyncio.run(run())
    assert [chunk.type for chunk in chunks] == ["error"]
    assert chunks[0].data["error_type"] == "auth" and chunks[0].data["exit_code"] == 1
    assert chunks[0].data["suggested_action"] == "service action" and chunks[0].session_id == "sid"


@pytest.mark.parametrize("provider_kind", ["codex", "custom"])
def test_public_client_preserves_usage_on_failure(monkeypatch, provider_kind):
    from agentcli.providers.codex import CodexProvider
    from tests.test_codex_parity import events
    usage = TokenUsage(total_tokens=110, payload_prompt_tokens=9,
                       prompt_tokens_reliable=False, prompt_tokens_source="observed")
    class Custom(LLMProvider):
        provider_id = "custom"
        def invoke(self, messages, **kwargs):
            return LLMResponse("", "custom", "", tokens=usage, error="auth failure", error_type="auth")
        def is_available(self): return True
        def list_models(self): return []
    provider = Custom() if provider_kind == "custom" else CodexProvider()
    if provider_kind == "codex":
        monkeypatch.setattr(provider, "_build_cmd", lambda *args, **kwargs: ["fake"])
        calls = []
        async def native(cmd, state, **kwargs):
            calls.append(cmd)
            if len(calls) == 2:
                yield StreamChunk(type="error", content="401 unauthorized")
                return
            for event in events(True):
                async for chunk in provider._dispatch_stream_event(event, state):
                    yield chunk
            yield StreamChunk(type="done", usage=state.final_usage, session_id=state.final_session_id)
        monkeypatch.setattr(provider, "_run_stream_template", native)
    registry = ProviderRegistry()
    registry.register(provider)
    client = LLMClient(MemoryStore(), registry)
    async def run():
        return [chunk async for chunk in client.chat_stream("hi", provider=provider_kind, owner="o")]
    chunks = asyncio.run(run())
    error = next(chunk for chunk in chunks if chunk.type == "error")
    assert error.data["error_type"] == "auth" and error.usage.total_tokens == 110
    assert chunks[-1].type == "done" and chunks[-1].data["error"] is True
    assert chunks[-1].usage.total_tokens == 110
    assert client.get_token_stats(owner="o")["total_calls"] == 0
