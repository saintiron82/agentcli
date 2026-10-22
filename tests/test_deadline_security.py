import asyncio
import time
import pytest
from agentcli.client import LLMClient
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.store.memory import MemoryStore
from agentcli.types import LLMResponse, StreamChunk


class BudgetProvider(LLMProvider):
    def __init__(self, name, delay=0.02, content="invalid"):
        self.provider_id, self.delay, self.content = name, delay, content
        self.timeouts = []
        self.cancelled = False

    def invoke(self, messages, *, timeout=120, **kwargs):
        self.timeouts.append(timeout)
        time.sleep(min(self.delay, timeout))
        return LLMResponse(content=self.content, provider=self.provider_id, model="")

    async def invoke_async(self, messages, *, timeout=120, **kwargs):
        self.timeouts.append(timeout)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return LLMResponse(content=self.content, provider=self.provider_id, model="")

    async def stream_async(self, messages, **kwargs):
        self.timeouts.append(kwargs["wall_timeout"])
        await asyncio.sleep(self.delay)
        if self.content:
            yield StreamChunk(type="text", content=self.content)
            yield StreamChunk(type="done", content=self.content)
        else:
            yield StreamChunk(type="error", content="failed")

    def list_models(self):
        return []

    def is_available(self):
        return True


def client(*providers):
    registry = ProviderRegistry()
    for p in providers:
        registry.register(p)
    registry.set_fallback_order([p.provider_id for p in providers])
    return LLMClient(MemoryStore(), registry)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_zero_budget_does_not_invoke_provider(asynchronous):
    p = BudgetProvider("fake")
    c = client(p)
    call = c.chat_async if asynchronous else c.chat
    result = call("hi", provider="fake", wall_timeout=0)
    if asynchronous:
        result = asyncio.run(result)
    assert result.error_type == "timeout" and not p.timeouts


@pytest.mark.parametrize("asynchronous", [False, True])
def test_fallback_gets_remaining_budget(asynchronous):
    a, b = BudgetProvider("a", content=""), BudgetProvider("b", delay=0)
    c = client(a, b)
    call = c.chat_async if asynchronous else c.chat
    result = call("hi", provider="a", fallback=True, timeout=120, wall_timeout=0.1)
    if asynchronous:
        result = asyncio.run(result)
    assert result.content == "invalid"
    assert 0 < b.timeouts[0] < a.timeouts[0] <= 0.1


def test_schema_retries_share_one_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agentcli.deadlines.time.monotonic", lambda: clock[0])
    p = BudgetProvider("fake", delay=0.04)
    def invoke(messages, *, timeout=120, **kwargs):
        p.timeouts.append(timeout)
        clock[0] += min(p.delay, timeout)
        return LLMResponse(content="invalid", provider="fake", model="")
    monkeypatch.setattr(p, "invoke", invoke)
    c = client(p)
    result = c.chat("hi", provider="fake", wall_timeout=0.07,
                    output_schema={"type": "object"}, schema_retries=4)
    assert result.error_type == "timeout"
    assert len(p.timeouts) == 2
    assert p.timeouts[1] < p.timeouts[0]


def test_async_noncompliant_provider_is_cancelled():
    p = BudgetProvider("fake", delay=30)
    result = asyncio.run(client(p).chat_async("hi", provider="fake", wall_timeout=0.03))
    assert result.error_type == "timeout" and p.cancelled


def test_streaming_fallback_does_not_restart_budget():
    a, b = BudgetProvider("a", content=""), BudgetProvider("b", delay=30)
    async def run():
        return [c async for c in client(a, b).chat_stream(
            "hi", provider="a", fallback=True, wall_timeout=0.08)]
    start = time.monotonic()
    chunks = asyncio.run(run())
    assert time.monotonic() - start < 1
    assert any(c.type == "error" and c.data.get("error_type") == "timeout" for c in chunks)


def test_default_timeout_remains_per_attempt():
    a, b = BudgetProvider("a", delay=0, content=""), BudgetProvider("b", delay=0)
    client(a, b).chat("hi", provider="a", fallback=True, timeout=12)
    assert a.timeouts == b.timeouts == [12]
