from datetime import datetime
from agentcli.store.sqlite import SQLiteStore
from agentcli.types import Message


def test_create_tables():
    store = SQLiteStore(":memory:")
    tables = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = [r[0] for r in tables]
    assert "conversations" in names
    assert "messages" in names


def test_create_and_get():
    store = SQLiteStore(":memory:")
    conv = store.create("bot1", "claude", "sonnet")
    assert conv.owner == "bot1"
    got = store.get(conv.id)
    assert got is not None
    assert got.provider == "claude"


def test_get_nonexistent():
    store = SQLiteStore(":memory:")
    assert store.get("nope") is None


def test_add_and_get_messages():
    store = SQLiteStore(":memory:")
    conv = store.create("bot1", "claude")
    for i in range(5):
        store.add_message(conv.id, Message(
            role="user", content=f"msg-{i}", timestamp=datetime.now(),
            metadata={"prompt_tokens": 10, "completion_tokens": 5}))

    msgs = store.get_messages(conv.id)
    assert len(msgs) == 5
    assert msgs[0].content == "msg-0"


def test_get_messages_limit():
    store = SQLiteStore(":memory:")
    conv = store.create("bot1", "claude")
    for i in range(5):
        store.add_message(conv.id, Message(
            role="user", content=f"msg-{i}", timestamp=datetime.now()))

    msgs = store.get_messages(conv.id, limit=3)
    assert len(msgs) == 3
    assert msgs[0].content == "msg-2"


def test_delete():
    store = SQLiteStore(":memory:")
    conv = store.create("bot1", "claude")
    store.add_message(conv.id, Message(role="user", content="hi",
                                       timestamp=datetime.now()))
    store.delete(conv.id)
    assert store.get(conv.id) is None
    assert store.get_messages(conv.id) == []


def test_list_by_owner():
    store = SQLiteStore(":memory:")
    store.create("bot1", "claude")
    store.create("bot1", "codex")
    store.create("bot2", "claude")

    assert len(store.list_by_owner("bot1")) == 2
    assert len(store.list_by_owner("bot1", limit=1)) == 1
    assert len(store.list_by_owner("bot2")) == 1


def test_token_stats():
    """토큰 통계는 usage_log를 참조한다 (messages 컬럼이 아님)."""
    store = SQLiteStore(":memory:")
    conv = store.create("bot1", "claude")
    store.record_usage(conv.id, prompt_tokens=100, completion_tokens=50,
                       total_tokens=150, provider="claude")
    store.record_usage(conv.id, prompt_tokens=80, completion_tokens=40,
                       total_tokens=120, provider="claude")

    stats = store.get_token_stats("bot1")
    assert stats["total_tokens"] == 270
    assert stats["total_calls"] == 2
    assert stats["by_provider"]["claude"] == 270


def test_set_metadata():
    store = SQLiteStore(":memory:")
    conv = store.create("bot1", "claude")
    store.set_metadata(conv.id, "session_id", "sess-42")
    got = store.get(conv.id)
    assert got.metadata["session_id"] == "sess-42"

    # 갱신
    store.set_metadata(conv.id, "session_id", "sess-99")
    got = store.get(conv.id)
    assert got.metadata["session_id"] == "sess-99"


def test_record_usage_not_in_messages():
    """record_usage는 usage_log에 기록하지 messages 테이블은 건드리지 않는다."""
    store = SQLiteStore(":memory:")
    conv = store.create("bot1", "claude")
    store.record_usage(conv.id, prompt_tokens=10, completion_tokens=5,
                       total_tokens=15, latency_ms=100, provider="claude")
    # messages 테이블은 비어 있어야 함
    assert store.get_messages(conv.id) == []
    # usage_log에는 기록됨
    rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM usage_log WHERE conversation_id=?",
        (conv.id,)).fetchone()
    assert rows["n"] == 1
