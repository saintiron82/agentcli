import asyncio
import threading
import time

import pytest

from agentcli import MemoryStore
from agentcli.coordination import ConversationLease, async_lease


def test_cancel_all_acquisition_success_before_owner_resumes_is_reclaimed(monkeypatch):
    store = MemoryStore()
    entered, finish, completed = (threading.Event() for _ in range(3))
    events = []
    class External:
        def acquire(self):
            entered.set()
            assert finish.wait(3)
            events.append("acquired")
            return True
        def release(self):
            events.append("released")
    lease = ConversationLease(store, "same", lambda *args: External())
    original = lease.try_acquire
    def acquire(*args):
        try:
            return original(*args)
        finally:
            completed.set()
    monkeypatch.setattr(lease, "try_acquire", acquire)
    async def waiter():
        async with async_lease(lease):
            pytest.fail("Cancelled owner entered body")
    async def run():
        owner = asyncio.create_task(waiter())
        assert await asyncio.to_thread(entered.wait, 2)
        # Model loop shutdown cancelling all outstanding Tasks. Allow worker
        # success before the owner gets a chance to process cancellation.
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
        finish.set()
        assert completed.wait(2)
        with pytest.raises(asyncio.CancelledError):
            await owner
    try:
        asyncio.run(run())
        assert events == ["acquired", "released"]
        assert not lease.acquired
        following = ConversationLease(store, "same")
        assert following.try_acquire() is following
        following.release()
    finally:
        finish.set()
        lease.release()


def test_remote_release_keeps_loop_responsive_and_local_lock_held():
    store = MemoryStore()
    entered, finish = threading.Event(), threading.Event()
    class External:
        def acquire(self):
            return True
        def release(self):
            entered.set()
            assert finish.wait(3)
    lease = ConversationLease(store, "same", lambda *args: External())
    async def turn():
        async with async_lease(lease):
            pass
    async def run():
        task = asyncio.create_task(turn())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0)
            assert not task.done()
            assert ConversationLease(store, "same").try_acquire() is None
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done() and lease.acquired
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not lease.acquired
    asyncio.run(run())


def test_shutdown_collects_blocked_acquisition():
    store = MemoryStore()
    entered, finish = threading.Event(), threading.Event()
    events = []
    class External:
        def acquire(self):
            entered.set()
            assert finish.wait(3)
            return True
        def release(self):
            events.append("released")
    lease = ConversationLease(store, "same", lambda *args: External())
    async def waiter():
        async with async_lease(lease):
            pytest.fail("shutdown waiter entered")
    async def main():
        asyncio.create_task(waiter())
        assert await asyncio.to_thread(entered.wait, 2)
    errors = []
    def shutdown():
        try:
            asyncio.run(main())
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=shutdown)
    thread.start()
    try:
        assert entered.wait(2)
        time.sleep(0.02)
        assert thread.is_alive()
    finally:
        finish.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert events == ["released"] and not lease.acquired
