import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from agentcli import SQLiteStore
from tests.test_turn_state import client_for


def test_nested_write_unit_commits_once_and_rolls_back():
    store = SQLiteStore()
    conv = store.create("o", "gate")
    statements = []
    store._conn.set_trace_callback(statements.append)
    with store.transaction():
        store.set_metadata(conv.id, "a", 1)
        with store.transaction(): store.set_metadata(conv.id, "b", 2)
    assert statements.count("COMMIT") == 1
    with pytest.raises(ValueError):
        with store.transaction():
            store.set_metadata(conv.id, "a", 9)
            raise ValueError("rollback")
    assert store.get(conv.id).metadata == {"a": 1, "b": 2}
    store.close()


def test_other_sqlite_connection_cannot_clear_between_generation_check_and_sid_write(tmp_path, monkeypatch):
    path = str(tmp_path / "shared.db")
    first, second = SQLiteStore(path), SQLiteStore(path)
    client, _ = client_for(first)
    other, _ = client_for(second)
    client.chat("seed", provider="gate", owner="o", alias="a")
    entered, release, cleared = threading.Event(), threading.Event(), threading.Event()
    original = first.record_usage
    def blocked_record(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(first, "record_usage", blocked_record)
    def clear():
        other.clear_session_metadata(owner="o", alias="a")
        cleared.set()
    async def run(pool):
        turn = asyncio.create_task(client.chat_async("fast", provider="gate", owner="o", alias="a"))
        assert await asyncio.to_thread(entered.wait, 2)
        reset = pool.submit(clear)
        await asyncio.sleep(0.03)
        assert not cleared.is_set()
        release.set()
        await turn
        await asyncio.to_thread(reset.result, 2)
        assert not first.find_by_alias("o", "a").metadata["session_id:gate"]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool: asyncio.run(run(pool))
    finally:
        release.set()
        first.close(); second.close()
