"""Turn lifetime and reset races: fake providers only."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pytest
from agentcli import LLMClient, MemoryStore, SQLiteStore
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import LLMResponse, StreamChunk


class GateProvider(LLMProvider):
    provider_id = "gate"
    supports_sessions = True
    supports_streaming = True
    stores_history = False

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.inputs = []

    def is_available(self): return True
    def list_models(self): return []
    def invoke(self, messages, **options):
        self.inputs.append(options.get("session_id", ""))
        return LLMResponse("ok", "gate", "", session_id=options.get("session_id") or "new-sid")

    async def invoke_async(self, messages, **options):
        if messages[-1].content == "slow":
            self.started.set()
            await self.release.wait()
        return self.invoke(messages, **options)

    async def stream_async(self, messages, **options):
        response = await self.invoke_async(messages, **options)
        yield StreamChunk(type="text", content=response.content)
        yield StreamChunk(type="done", content=response.content, session_id=response.session_id)


def client_for(store):
    provider = GateProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    registry.set_fallback_order(["gate"])
    return LLMClient(store, registry), provider


@pytest.mark.parametrize("stream", [False, True])
def test_active_turn_is_not_evicted(stream):
    async def run():
        store = MemoryStore(max_conversations=1)
        client, provider = client_for(store)
        async def slow():
            if stream:
                return [c async for c in client.chat_stream("slow", owner="o", alias="a")]
            return await client.chat_async("slow", owner="o", alias="a")
        task = asyncio.create_task(slow())
        await provider.started.wait()
        original = store.find_by_alias("o", "a")
        await client.chat_async("fast", owner="o", alias="b")
        assert store.get(original.id) is not None
        provider.release.set()
        await task
        assert store.get(original.id).metadata["session_id:gate"] == "new-sid"
        await client.chat_async("fast", owner="o", alias="a")
        assert provider.inputs[-1] == "new-sid"
        assert not store._pins
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("sqlite", [False, True])
def test_clear_during_turn_is_not_undone(tmp_path, stream, sqlite):
    async def run():
        store = SQLiteStore(str(tmp_path / "state.db")) if sqlite else MemoryStore()
        client, provider = client_for(store)
        await client.chat_async("seed", owner="o", alias="a")
        async def slow():
            if stream:
                return [c async for c in client.chat_stream("slow", owner="o", alias="a")]
            return await client.chat_async("slow", owner="o", alias="a")
        task = asyncio.create_task(slow())
        await provider.started.wait()
        client.clear_session_metadata(owner="o", alias="a")
        provider.release.set()
        await task
        assert store.find_by_alias("o", "a").metadata["session_id:gate"] == ""
        await client.chat_async("next", owner="o", alias="a")
        assert provider.inputs[-1] == ""
        assert client.get_token_stats(days=0)["total_calls"] == 3
        if sqlite: store.close()
    asyncio.run(run())


def test_existing_handle_lookup_does_not_evict_it():
    store = MemoryStore(max_conversations=1)
    original = store.create("o", "gate", alias="a")
    assert store.create("o", "gate", alias="a").id == original.id


def test_pin_protects_ttl():
    store = MemoryStore(ttl_hours=1)
    conv = store.create("o", "gate")
    store.pin(conv.id)
    conv.updated_at = datetime.now() - timedelta(hours=2)
    assert store.get(conv.id) is not None
    store.unpin(conv.id)
    assert store.get(conv.id) is None


@pytest.mark.parametrize("sqlite", [False, True])
def test_clear_all_is_not_truncated(tmp_path, sqlite):
    store = SQLiteStore(str(tmp_path / "all.db")) if sqlite else MemoryStore(max_conversations=1300)
    client, _ = client_for(store)
    for index in range(1200):
        conv = store.create("o", "gate", alias=str(index))
        store.set_metadata(conv.id, "session_id:gate", "fake")
    assert len(client.clear_session_metadata(owner="o")) == 1200
    assert all(not c.metadata["session_id:gate"] for c in store.list_by_owner("o", limit=0))
    if sqlite: store.close()


def test_stats_and_create_are_intrinsically_synchronized():
    store = MemoryStore(max_conversations=1000)
    def write():
        for index in range(500):
            conv = store.create("o", "gate", alias=str(index))
            store.record_usage(conv.id, total_tokens=1)
    def read():
        for _ in range(200): store.get_token_stats(days=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write), pool.submit(read)]
        for future in futures: future.result(timeout=10)
    assert store.get_token_stats(days=0)["total_calls"] == 500


@pytest.mark.parametrize("sqlite", [False, True])
def test_late_creator_failure_does_not_delete_other_success(tmp_path, monkeypatch, sqlite):
    store = SQLiteStore(str(tmp_path / "race.db")) if sqlite else MemoryStore()
    client, provider = client_for(store)
    entered, release = threading.Event(), threading.Event()
    prepare, invoke = client._locked_prepare, provider.invoke
    first = [True]
    def delayed(*args, **kwargs):
        plan = prepare(*args, **kwargs)
        if args[0] == "fail" and first[0]:
            first[0] = False
            entered.set()
            assert release.wait(3)
        return plan
    def failed(messages, **options):
        if messages[-1].content == "fail": return LLMResponse("", "gate", "", error="fake")
        return invoke(messages, **options)
    monkeypatch.setattr(client, "_locked_prepare", delayed)
    monkeypatch.setattr(provider, "invoke", failed)
    async def run():
        failing = asyncio.create_task(client.chat_async("fail", provider="gate", owner="o", alias="a"))
        assert await asyncio.to_thread(entered.wait, 2)
        success = await client.chat_async("fast", provider="gate", owner="o", alias="a")
        release.set()
        result = await failing
        assert not result.content
        assert result.conversation_id == success.conversation_id
        assert store.get(success.conversation_id).metadata["session_id:gate"] == "new-sid"
        assert store.get_token_stats(days=0)["total_calls"] == 1
    try: asyncio.run(run())
    finally:
        release.set()
        if sqlite: store.close()
