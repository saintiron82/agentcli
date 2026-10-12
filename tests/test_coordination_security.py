import asyncio
import multiprocessing
import threading
from agentcli.client import LLMClient
from agentcli.coordination import ConversationLease, async_lease
from agentcli.providers.registry import ProviderRegistry
from agentcli.store.memory import MemoryStore
from agentcli.store.sqlite import SQLiteStore
from tests.test_concurrency import SlowSessionProvider


def clients(store1, store2):
    provider = SlowSessionProvider(delay=0.04)
    registry = ProviderRegistry()
    registry.register(provider)
    return provider, LLMClient(store1, registry), LLMClient(store2, registry)


def test_two_clients_and_sync_async_share_lease():
    store = MemoryStore()
    provider, a, b = clients(store, store)
    errors = []
    def sync():
        try:
            a.chat("sync", provider="slowsess", owner="o", alias="same")
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=sync)
    thread.start()
    asyncio.run(b.chat_async("async", provider="slowsess", owner="o", alias="same"))
    thread.join(3)
    assert not errors and not thread.is_alive()
    assert sorted(provider.received_sids) == ["", "sid-1"]


def test_two_sqlite_instances_share_session(tmp_path):
    path = str(tmp_path / "state.db")
    s1, s2 = SQLiteStore(path), SQLiteStore(path)
    provider, a, b = clients(s1, s2)
    async def run():
        return await asyncio.gather(
            a.chat_async("a", provider="slowsess", alias="same"),
            b.chat_async("b", provider="slowsess", alias="same"))
    result = asyncio.run(run())
    assert result[0].session_id == result[1].session_id
    assert sorted(provider.received_sids) == ["", "sid-1"]
    s1.close()
    s2.close()


def test_many_waiters_do_not_starve_persistence():
    store = MemoryStore()
    provider, a, b = clients(store, store)
    provider.delay = 0
    async def run():
        return await asyncio.wait_for(asyncio.gather(*[
            (a if n % 2 else b).chat_async("hi", provider="slowsess", alias="same")
            for n in range(64)]), 8)
    result = asyncio.run(run())
    assert len({r.session_id for r in result}) == 1


def test_cancelled_waiter_does_not_leak_lease():
    store = MemoryStore()
    held = ConversationLease(store, "same")
    held.acquire()
    async def run():
        async def waiter():
            async with async_lease(ConversationLease(store, "same")):
                raise AssertionError("Cancelled waiter entered")
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.03)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        held.release()
        async with async_lease(ConversationLease(store, "same")):
            pass
    asyncio.run(run())


def _process_lock(path, queue):
    store = SQLiteStore(path)
    lease = ConversationLease(store, "same")
    got = lease.try_acquire()
    queue.put(got is not None)
    if got:
        got.release()
    store.close()


def test_file_lease_blocks_separate_process(tmp_path):
    path = str(tmp_path / "state.db")
    store = SQLiteStore(path)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with ConversationLease(store, "same"):
        proc = ctx.Process(target=_process_lock, args=(path, queue))
        proc.start()
        assert queue.get(timeout=10) is False
        proc.join(10)
        assert proc.exitcode == 0
    store.close()


def test_host_lease_factory_is_entered_and_released():
    events = []
    class External:
        def acquire(self):
            events.append("acquire")
        def release(self):
            events.append("release")
    store = MemoryStore()
    with ConversationLease(store, "same", lambda s, cid: External()):
        events.append("body")
    assert events == ["acquire", "body", "release"]
