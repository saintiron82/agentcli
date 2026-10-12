"""Blocking operations must not freeze unrelated loop work or outlive leases."""
import asyncio
import sqlite3
import threading
import time
import pytest
from agentcli import LLMClient, MemoryStore, SQLiteStore
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import LLMResponse
from tests.test_turn_state import GateProvider


@pytest.mark.parametrize("stream", [False, True])
def test_sqlite_write_contention_does_not_block_event_loop(tmp_path, stream):
    store = SQLiteStore(str(tmp_path / "busy.db"))
    provider = GateProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    client = LLMClient(store, registry)
    locked = threading.Event()
    release = threading.Event()
    def hold_lock():
        connection = sqlite3.connect(str(tmp_path / "busy.db"))
        connection.execute("BEGIN IMMEDIATE")
        locked.set()
        release.wait(3)
        connection.rollback()
        connection.close()
    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert locked.wait(2)
    async def run():
        async def call():
            if stream:
                return [c async for c in client.chat_stream("fast", provider="gate", owner="o", alias="a")]
            return await client.chat_async("fast", provider="gate", owner="o", alias="a")
        task = asyncio.create_task(call())
        await asyncio.sleep(0.03)
        assert not task.done()
        assert not release.is_set()
        release.set()  # Cannot run until SQLite releases if the loop is blocked.
        await asyncio.wait_for(task, 2)
    try:
        asyncio.run(run())
    finally:
        release.set()
        worker.join(3)
        store.close()


def test_cancelled_thread_provider_keeps_session_lease_until_worker_finishes():
    class ThreadProvider(LLMProvider):
        provider_id = "thread"
        supports_sessions = True
        def is_available(self): return True
        def list_models(self): return []
        def invoke(self, messages, **kwargs):
            entered.set()
            if messages[-1].content == "slow":
                release.wait(3)
            calls.append(messages[-1].content)
            return LLMResponse("ok", "thread", "", session_id="fake")
    entered, release = threading.Event(), threading.Event()
    calls = []
    registry = ProviderRegistry()
    registry.register(ThreadProvider())
    client = LLMClient(MemoryStore(), registry)
    async def run():
        first = asyncio.create_task(client.chat_async("slow", provider="thread", owner="o", alias="a"))
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        second = asyncio.create_task(client.chat_async("fast", provider="thread", owner="o", alias="a"))
        await asyncio.sleep(0.03)
        assert not second.done()
        assert calls == []
        release.set()
        with pytest.raises(asyncio.CancelledError): await first
        await second
        assert calls == ["slow", "fast"]
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_cancelled_prepare_releases_memory_pin_after_worker_completes():
    class SlowStore(MemoryStore):
        def create(self, *args, **kwargs):
            entered.set()
            release.wait(3)
            return super().create(*args, **kwargs)
    entered, release = threading.Event(), threading.Event()
    store = SlowStore()
    registry = ProviderRegistry()
    registry.register(GateProvider())
    client = LLMClient(store, registry)
    async def run():
        task = asyncio.create_task(client.chat_async("fast", provider="gate"))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert store._pins == {}
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_loop_shutdown_does_not_release_running_worker_lease():
    entered, release, checking, finished = (threading.Event() for _ in range(4))
    class Provider(LLMProvider):
        provider_id = "thread"
        supports_sessions = True
        def is_available(self): return True
        def list_models(self): return []
        def invoke(self, messages, **kwargs):
            if messages[-1].content == "slow":
                entered.set()
                release.wait(3)
            return LLMResponse("ok", "thread", "", session_id="fake")
    registry = ProviderRegistry()
    registry.register(Provider())
    store = MemoryStore()
    client = LLMClient(store, registry)
    errors = []
    async def background():
        asyncio.create_task(client.chat_async("slow", provider="thread", owner="o", alias="a"))
        assert await asyncio.to_thread(entered.wait, 2)
        # Returning starts asyncio.run's cancellation of ALL outstanding Tasks.
    def shutdown():
        try: asyncio.run(background())
        except BaseException as exc: errors.append(exc)
    def next_turn():
        assert entered.wait(2)
        checking.set()
        client.chat("fast", provider="thread", owner="o", alias="a")
        finished.set()
    first = threading.Thread(target=shutdown)
    second = threading.Thread(target=next_turn)
    first.start(); second.start()
    try:
        assert checking.wait(2)
        time.sleep(0.05)
        assert not finished.is_set()
        assert store._pins
    finally:
        release.set()
        first.join(3); second.join(3)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert finished.is_set() and not store._pins
