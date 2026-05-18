"""비동기·스트리밍 API 회귀 테스트."""

import asyncio
import pytest
from agentcli.client import LLMClient
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.store.memory import MemoryStore
from agentcli.types import Message, LLMResponse, StreamChunk, TokenUsage


# ===== 1. 기본 invoke_async (base fallback = to_thread) =====

class SyncOnlyProvider(LLMProvider):
    provider_id = "sync"
    supports_sessions = False

    def __init__(self):
        self.sync_calls = 0

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        self.sync_calls += 1
        return LLMResponse(
            content="sync-ok", provider="sync", model=model,
            tokens=TokenUsage(total_tokens=10))

    def list_models(self): return []
    def is_available(self): return True


def test_base_invoke_async_wraps_sync():
    """base 기본 구현: invoke_async는 동기 invoke를 to_thread로 감싼다."""
    p = SyncOnlyProvider()
    resp = asyncio.run(p.invoke_async([Message(role="user", content="x")]))
    assert resp.content == "sync-ok"
    assert p.sync_calls == 1


def test_base_stream_async_wraps_invoke():
    """base 기본 stream: invoke_async 완료 후 text + done 한 번씩 yield."""
    p = SyncOnlyProvider()

    async def collect():
        chunks = []
        async for c in p.stream_async([Message(role="user", content="x")]):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(collect())
    types = [c.type for c in chunks]
    assert "text" in types
    assert types[-1] == "done"
    done = chunks[-1]
    assert done.content == "sync-ok"


# ===== 2. LLMClient.chat_async =====

class AsyncProvider(LLMProvider):
    provider_id = "asyncp"
    supports_sessions = False

    def __init__(self):
        self.last_messages = []
        self.last_cwd = None

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        # sync fallback을 위해 남겨둠
        return asyncio.run(self.invoke_async(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd))

    async def invoke_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None):
        self.last_messages = list(messages)
        self.last_cwd = cwd
        await asyncio.sleep(0.01)  # async 티 내기
        return LLMResponse(
            content="async-ok", provider="asyncp", model=model,
            tokens=TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
            latency_ms=10)

    def list_models(self): return []
    def is_available(self): return True


def test_chat_async_basic():
    p = AsyncProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["asyncp"])
    client = LLMClient(store=store, registry=reg)

    resp = asyncio.run(client.chat_async(
        "hello", provider="asyncp", owner="bot1"))
    assert resp.content == "async-ok"
    assert resp.conversation_id
    # 비세션: messages 저장됨
    msgs = store.get_messages(resp.conversation_id)
    assert len(msgs) == 2


def test_chat_async_cwd_propagation():
    p = AsyncProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["asyncp"])
    client = LLMClient(store=MemoryStore(), registry=reg)

    asyncio.run(client.chat_async(
        "hi", provider="asyncp", owner="b", cwd="/repo/x"))
    assert p.last_cwd == "/repo/x"


def test_chat_async_parallel_speedup():
    """두 호출을 병렬로 돌리면 직렬보다 빠르다."""
    import time
    p = AsyncProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["asyncp"])
    client = LLMClient(store=MemoryStore(), registry=reg)

    async def parallel():
        return await asyncio.gather(
            client.chat_async("a", provider="asyncp", owner="b",
                              conversation_id="conv-a"),
            client.chat_async("b", provider="asyncp", owner="b",
                              conversation_id="conv-b"),
            client.chat_async("c", provider="asyncp", owner="b",
                              conversation_id="conv-c"),
        )

    t0 = time.perf_counter()
    results = asyncio.run(parallel())
    dt = time.perf_counter() - t0
    assert all(r.content == "async-ok" for r in results)
    # 각 호출이 0.01s sleep. 병렬이면 ~0.01s, 직렬이면 ~0.03s.
    # 여유 있게 0.025 이하면 병렬 성공.
    assert dt < 0.025


# ===== 3. LLMClient.chat_stream =====

class StreamingProvider(LLMProvider):
    provider_id = "streamp"
    supports_sessions = True
    supports_streaming = True

    async def invoke_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None):
        # 스트리밍 대신 한방 응답 (fallback 검증용)
        return LLMResponse(
            content="one-shot", provider=self.provider_id, model=model,
            tokens=TokenUsage(total_tokens=5),
            session_id=session_id or "stream-sid-1")

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        return asyncio.run(self.invoke_async(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd))

    async def stream_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None):
        used_sid = session_id or "stream-sid-1"
        yield StreamChunk(type="event", data={"init": True},
                          session_id=used_sid)
        for word in ["Hello", " ", "world", "!"]:
            yield StreamChunk(type="text", content=word)
        yield StreamChunk(type="done", content="Hello world!",
                          session_id=used_sid,
                          usage=TokenUsage(prompt_tokens=2, completion_tokens=4,
                                           total_tokens=6),
                          data={"provider": self.provider_id, "model": model,
                                "latency_ms": 5})

    def list_models(self): return []
    def is_available(self): return True


def test_chat_stream_yields_text_chunks():
    p = StreamingProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["streamp"])
    client = LLMClient(store=store, registry=reg)

    async def collect():
        out = []
        async for c in client.chat_stream(
                "hi", provider="streamp", owner="b",
                conversation_id="bot:s1"):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    text_chunks = [c for c in chunks if c.type == "text"]
    done_chunks = [c for c in chunks if c.type == "done"]
    assert len(text_chunks) == 4  # Hello / " " / world / !
    assert len(done_chunks) == 1
    assert done_chunks[0].content == "Hello world!"
    assert done_chunks[0].session_id == "stream-sid-1"

    # 세션 provider → messages 저장 안 됨, session_id만 metadata에
    assert store.get_messages("bot:s1") == []
    conv = store.get("bot:s1")
    assert conv.metadata.get("session_id:streamp") == "stream-sid-1"

    # usage도 기록됨
    stats = store.get_token_stats("b")
    assert stats["total_calls"] == 1
    assert stats["total_tokens"] == 6


def test_chat_stream_passes_idle_and_wall_timeout():
    class TimeoutAwareProvider(StreamingProvider):
        provider_id = "timeoutp"

        def __init__(self):
            self.last_idle_timeout = None
            self.last_wall_timeout = None

        async def stream_async(self, messages, *, model="", timeout=120,
                               session_id="", cwd=None, idle_timeout=None,
                               wall_timeout=None):
            self.last_idle_timeout = idle_timeout
            self.last_wall_timeout = wall_timeout
            yield StreamChunk(type="text", content="ok")
            yield StreamChunk(type="done", content="ok",
                              session_id=session_id or "sid")

    p = TimeoutAwareProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["timeoutp"])
    client = LLMClient(store=MemoryStore(), registry=reg)

    async def collect():
        return [
            c async for c in client.chat_stream(
                "hi", provider="timeoutp", owner="b",
                idle_timeout=7, wall_timeout=30)
        ]

    asyncio.run(collect())
    assert p.last_idle_timeout == 7
    assert p.last_wall_timeout == 30


def test_chat_stream_failed_call_does_not_pollute():
    """스트리밍 중 text가 하나도 없으면 실패로 취급 → 저장 안 됨."""
    class FailStreamProvider(LLMProvider):
        provider_id = "fs"
        supports_sessions = True
        supports_streaming = True

        def invoke(self, messages, **kw):
            return LLMResponse(content="", provider="fs", model="")

        async def invoke_async(self, messages, **kw):
            return LLMResponse(content="", provider="fs", model="")

        async def stream_async(self, messages, **kw):
            yield StreamChunk(type="error", content="boom")
            yield StreamChunk(type="done", content="")

        def list_models(self): return []
        def is_available(self): return True

    p = FailStreamProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["fs"])
    client = LLMClient(store=store, registry=reg)

    async def collect():
        out = []
        async for c in client.chat_stream(
                "hi", provider="fs", owner="b",
                conversation_id="bot:dead"):
            out.append(c)
        return out

    chunks = asyncio.run(collect())
    # error 청크는 표준 필드를 채워 전달되어야 함
    errors = [c for c in chunks if c.type == "error"]
    assert errors
    assert errors[0].data["provider"] == "fs"
    assert errors[0].data["error_type"] == "unknown"
    assert errors[0].data["recoverable"] is False
    assert "suggested_action" in errors[0].data
    assert "exit_code" in errors[0].data
    assert store.get_messages("bot:dead") == []
    stats = store.get_token_stats("b")
    assert stats["total_calls"] == 0


def test_chat_stream_pre_output_fallback():
    class FailBeforeOutput(LLMProvider):
        provider_id = "primary"
        supports_streaming = True

        def __init__(self):
            self.calls = 0

        def invoke(self, messages, **kw):
            return LLMResponse(content="", provider=self.provider_id, model="")

        async def stream_async(self, messages, **kw):
            self.calls += 1
            yield StreamChunk(type="error", content="rate limit")
            yield StreamChunk(type="done", content="")

        def list_models(self): return []
        def is_available(self): return True

    class FallbackStream(LLMProvider):
        provider_id = "fallback"
        supports_streaming = True

        def __init__(self):
            self.calls = 0

        def invoke(self, messages, **kw):
            return LLMResponse(content="fallback ok",
                               provider=self.provider_id, model="")

        async def stream_async(self, messages, **kw):
            self.calls += 1
            yield StreamChunk(type="text", content="fallback ok")
            yield StreamChunk(type="done", content="fallback ok",
                              session_id="fb-sid",
                              usage=TokenUsage(total_tokens=4),
                              data={"provider": self.provider_id,
                                    "latency_ms": 3})

        def list_models(self): return []
        def is_available(self): return True

    primary = FailBeforeOutput()
    fallback = FallbackStream()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(primary)
    reg.register(fallback)
    reg.set_fallback_order(["primary", "fallback"])
    client = LLMClient(store=store, registry=reg)

    async def collect():
        return [
            c async for c in client.chat_stream(
                "hi", provider="primary", owner="b", alias="s",
                fallback=True)
        ]

    chunks = asyncio.run(collect())
    assert primary.calls == 1
    assert fallback.calls == 1
    assert [c.content for c in chunks if c.type == "text"] == ["fallback ok"]
    assert not [c for c in chunks if c.type == "error"]
    done = [c for c in chunks if c.type == "done"][0]
    assert done.data["provider"] == "fallback"
    assert done.content == "fallback ok"


def test_chat_stream_does_not_fallback_after_output():
    class PartialThenFail(LLMProvider):
        provider_id = "primary"
        supports_streaming = True

        def __init__(self):
            self.calls = 0

        def invoke(self, messages, **kw):
            return LLMResponse(content="", provider=self.provider_id, model="")

        async def stream_async(self, messages, **kw):
            self.calls += 1
            yield StreamChunk(type="text", content="partial")
            yield StreamChunk(type="error", content="rate limit")
            yield StreamChunk(type="done", content="")

        def list_models(self): return []
        def is_available(self): return True

    class ShouldNotRun(LLMProvider):
        provider_id = "fallback"
        supports_streaming = True

        def __init__(self):
            self.calls = 0

        def invoke(self, messages, **kw):
            return LLMResponse(content="bad", provider=self.provider_id,
                               model="")

        async def stream_async(self, messages, **kw):
            self.calls += 1
            yield StreamChunk(type="text", content="bad")
            yield StreamChunk(type="done", content="bad")

        def list_models(self): return []
        def is_available(self): return True

    primary = PartialThenFail()
    fallback = ShouldNotRun()
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
                conversation_id="stream-partial", fallback=True)
        ]

    chunks = asyncio.run(collect())
    assert primary.calls == 1
    assert fallback.calls == 0
    assert [c.content for c in chunks if c.type == "text"] == ["partial"]
    errors = [c for c in chunks if c.type == "error"]
    assert errors[0].data["provider"] == "primary"
    assert errors[0].data["error_type"] == "usage_limit"
    assert store.get_messages("stream-partial") == []
    assert store.get_token_stats("b")["total_calls"] == 0


# ===== 4. chat_async도 세션 provider 규칙 준수 =====

def test_chat_async_session_provider_does_not_store_content():
    """chat_async도 chat과 동일하게 세션 provider content 미저장."""
    class AsyncSessionProvider(LLMProvider):
        provider_id = "asp"
        supports_sessions = True

        def invoke(self, messages, **kw):
            return LLMResponse(content="ok", provider="asp", model="",
                               session_id=kw.get("session_id") or "s1",
                               tokens=TokenUsage(total_tokens=3))

        async def invoke_async(self, messages, *, model="", timeout=120,
                               session_id="", cwd=None):
            return LLMResponse(content="ok", provider="asp", model=model,
                               session_id=session_id or "s1",
                               tokens=TokenUsage(total_tokens=3))

        def list_models(self): return []
        def is_available(self): return True

    p = AsyncSessionProvider()
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["asp"])
    client = LLMClient(store=store, registry=reg)

    resp = asyncio.run(client.chat_async(
        "hi", provider="asp", owner="b", conversation_id="bot:asp-1"))
    assert resp.content == "ok"
    assert store.get_messages("bot:asp-1") == []
    conv = store.get("bot:asp-1")
    assert conv.metadata.get("session_id:asp") == "s1"
