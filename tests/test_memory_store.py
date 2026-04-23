from datetime import datetime
from agentcli.store.memory import MemoryStore
from agentcli.types import Message


def test_create_and_get():
    store = MemoryStore()
    conv = store.create("bot1", "claude", "sonnet")
    assert conv.owner == "bot1"
    assert conv.provider == "claude"
    assert conv.model == "sonnet"
    assert conv.id

    got = store.get(conv.id)
    assert got is not None
    assert got.id == conv.id


def test_get_nonexistent():
    store = MemoryStore()
    assert store.get("nonexistent") is None


def test_add_and_get_messages():
    store = MemoryStore()
    conv = store.create("bot1", "claude")
    for i in range(5):
        store.add_message(conv.id, Message(
            role="user", content=f"msg-{i}", timestamp=datetime.now()))

    msgs = store.get_messages(conv.id)
    assert len(msgs) == 5
    assert msgs[0].content == "msg-0"


def test_get_messages_with_limit():
    store = MemoryStore()
    conv = store.create("bot1", "claude")
    for i in range(5):
        store.add_message(conv.id, Message(
            role="user", content=f"msg-{i}", timestamp=datetime.now()))

    msgs = store.get_messages(conv.id, limit=3)
    assert len(msgs) == 3
    assert msgs[0].content == "msg-2"


def test_delete():
    store = MemoryStore()
    conv = store.create("bot1", "claude")
    store.delete(conv.id)
    assert store.get(conv.id) is None


def test_list_by_owner():
    store = MemoryStore()
    store.create("bot1", "claude")
    store.create("bot1", "codex")
    store.create("bot2", "claude")

    bot1_convs = store.list_by_owner("bot1")
    assert len(bot1_convs) == 2

    bot1_limited = store.list_by_owner("bot1", limit=1)
    assert len(bot1_limited) == 1


def test_set_metadata():
    store = MemoryStore()
    conv = store.create("bot1", "claude")
    store.set_metadata(conv.id, "session_id", "sess-7")
    got = store.get(conv.id)
    assert got.metadata["session_id"] == "sess-7"


def test_record_usage_separate_from_messages():
    store = MemoryStore()
    conv = store.create("bot1", "claude")
    store.record_usage(conv.id, prompt_tokens=100, completion_tokens=50,
                       total_tokens=150, provider="claude")
    # messages는 비어야 함
    assert store.get_messages(conv.id) == []
    stats = store.get_token_stats("bot1")
    assert stats["total_tokens"] == 150
    assert stats["total_calls"] == 1


def test_max_conversations_evicts_oldest():
    import time
    store = MemoryStore(max_conversations=3)
    ids = []
    for i in range(3):
        conv = store.create(f"bot{i}", "claude")
        ids.append(conv.id)
        time.sleep(0.001)  # updated_at 분리
    # 4번째 create → 가장 오래된 것 evict
    store.create("bot3", "claude")
    assert store.get(ids[0]) is None
    assert store.get(ids[1]) is not None
    assert len(store.list_by_owner("bot1")) == 1


def test_ttl_expiration():
    from datetime import datetime, timedelta
    store = MemoryStore(ttl_hours=1)
    conv = store.create("bot1", "claude")
    # updated_at을 과거로 되돌림
    conv.updated_at = datetime.now() - timedelta(hours=2)
    assert store.get(conv.id) is None
