import asyncio
import pytest
from agentcli import LLMClient, MemoryStore
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import LLMResponse, StreamChunk, TokenUsage

def test_double_specify_effort_raises():
    c = LLMClient(store=MemoryStore())
    with pytest.raises(ValueError):
        # effort as first-class kwarg AND inside provider_options -> ambiguous
        c.chat("hi", provider="claude", effort="high",
               provider_options={"effort": "low"})

def test_effort_threads_into_supported_kwargs():
    # _supported_kwargs must keep 'effort' for a provider that accepts it
    from agentcli.client import _supported_kwargs
    from agentcli.providers.claude import ClaudeProvider
    kw = _supported_kwargs(ClaudeProvider(), "invoke",
                           {"model": "", "effort": "high", "thinking": None})
    assert kw.get("effort") == "high"


# ===== chat_stream fallback must survive a leading reasoning event =====
#
# All three providers yield a library-synthesized reasoning `event` chunk
# (clamp/unsupported notice, data={"reasoning": ..., "provider": ...}) as the
# FIRST chunk of stream_async when effort/thinking needed reporting — before
# any binary-missing/error check. chat_stream's fallback logic must not treat
# that event as "output was emitted", otherwise a failing primary with a
# reasoning event never falls back even though no real output happened.

class _ReasoningEventThenFail(LLMProvider):
    """Primary provider: yields the reasoning event, then fails with no
    other output — the exact shape all three real providers produce when a
    clamp/unsupported reasoning control coincides with e.g. a missing
    binary."""

    provider_id = "primary"
    supports_streaming = True

    def __init__(self):
        self.calls = 0

    def invoke(self, messages, **kw):
        return LLMResponse(content="", provider=self.provider_id, model="")

    async def stream_async(self, messages, **kw):
        self.calls += 1
        yield StreamChunk(type="event",
                          data={"reasoning": {"effort": {"requested": "concise",
                                                          "applied": "off",
                                                          "clamped": True}},
                                "provider": self.provider_id})
        yield StreamChunk(type="error", content="binary not found")
        yield StreamChunk(type="done", content="")

    def list_models(self): return []
    def is_available(self): return True


class _FallbackOk(LLMProvider):
    provider_id = "fallback"
    supports_streaming = True

    def __init__(self):
        self.calls = 0

    def invoke(self, messages, **kw):
        return LLMResponse(content="fallback ok", provider=self.provider_id,
                           model="")

    async def stream_async(self, messages, **kw):
        self.calls += 1
        yield StreamChunk(type="text", content="fallback ok")
        yield StreamChunk(type="done", content="fallback ok",
                          session_id="fb-sid",
                          usage=TokenUsage(total_tokens=4),
                          data={"provider": self.provider_id, "latency_ms": 3})

    def list_models(self): return []
    def is_available(self): return True


def test_chat_stream_falls_back_after_leading_reasoning_event():
    """A reasoning event as the sole preceding chunk must not suppress
    fallback — this is the regression guard for the client.py fix where a
    reasoning `event` chunk was counted as `emitted_output`, which made
    `saw_error and emitted_output` short-circuit fallback. Revert that fix
    and this test fails: fallback.calls stays 0 and the stream ends in an
    error `done` from the primary instead of a successful fallback `done`.
    """
    primary = _ReasoningEventThenFail()
    fallback = _FallbackOk()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(primary)
    reg.register(fallback)
    reg.set_fallback_order(["primary", "fallback"])
    client = LLMClient(store=store, registry=reg)

    async def collect():
        return [
            c async for c in client.chat_stream(
                "hi", provider="primary", owner="b",
                thinking="concise", fallback=True)
        ]

    chunks = asyncio.run(collect())

    assert primary.calls == 1
    assert fallback.calls == 1

    # The reasoning event chunk from the primary must still reach the caller.
    events = [c for c in chunks if c.type == "event"]
    assert events and "reasoning" in events[0].data
    assert events[0].data.get("provider") == "primary"

    # No error chunk should reach the caller once fallback succeeds.
    assert not [c for c in chunks if c.type == "error"]

    assert [c.content for c in chunks if c.type == "text"] == ["fallback ok"]
    done = [c for c in chunks if c.type == "done"][0]
    assert done.data["provider"] == "fallback"
    assert done.content == "fallback ok"
