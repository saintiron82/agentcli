"""Behavior coverage for agentcli.store.sqlite.SQLiteStore.

These tests exercise branches not covered by tests/test_sqlite_store.py:
on-disk (WAL) persistence and re-open, alias set/find/reassignment, metadata
set on missing rows, record_usage alias backfill, and every get_token_stats
filter + group_by axis (including the residual/unmatched ``None`` axis).

All tests run a real SQLiteStore against a tmp_path db file (or :memory:);
no mocks — a store is cheap to instantiate for real.
"""

from datetime import datetime, timedelta

from agentcli.store.sqlite import SQLiteStore
from agentcli.types import Message


# ===== on-disk db: WAL pragma (28-31) + persistence across re-open =====

def test_file_db_enables_wal_and_busy_timeout(tmp_path):
    db = str(tmp_path / "store.db")
    store = SQLiteStore(db)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    timeout = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000


def test_persistence_across_reopen(tmp_path):
    db = str(tmp_path / "persist.db")
    store = SQLiteStore(db)
    conv = store.create("owner-x", "claude", "sonnet", alias="worker")
    store.set_metadata(conv.id, "session_id:claude", "sid-7")
    store.record_usage(conv.id, prompt_tokens=10, completion_tokens=5,
                       total_tokens=15, provider="claude")
    del store

    reopened = SQLiteStore(db)
    got = reopened.get(conv.id)
    assert got is not None
    assert got.alias == "worker"
    assert got.metadata["session_id:claude"] == "sid-7"
    # alias lookup survives re-open
    assert reopened.find_by_alias("owner-x", "worker").id == conv.id
    # usage_log survives re-open
    stats = reopened.get_token_stats("owner-x")
    assert stats["total_tokens"] == 15
    assert stats["total_calls"] == 1


# ===== _row_to_conv alias fallback (114-115) =====

def test_row_to_conv_handles_missing_alias_column():
    """If a row lacks the alias key, _row_to_conv falls back to ""."""
    store = SQLiteStore(":memory:")
    conv = store.create("o", "claude")
    row = store._conn.execute(
        "SELECT id, owner, provider, model, created_at, updated_at, metadata "
        "FROM conversations WHERE id=?", (conv.id,)).fetchone()
    # This Row has no "alias" key, exercising the except/fallback path.
    result = store._row_to_conv(row)
    assert result.alias == ""
    assert result.id == conv.id


# ===== create() dedup branches (131, 136-139) =====

def test_create_returns_existing_conversation_for_same_alias():
    store = SQLiteStore(":memory:")
    first = store.create("team", "claude", alias="bull")
    again = store.create("team", "codex", alias="bull")  # same owner+alias
    # alias already taken -> the original conversation is returned, untouched
    assert again.id == first.id
    assert again.provider == "claude"


def test_create_with_existing_id_backfills_alias():
    store = SQLiteStore(":memory:")
    created = store.create("team", "claude", conversation_id="fixed-id")
    assert created.alias == ""
    # same id, now supplying an alias -> set_alias path (136-139)
    again = store.create("team", "claude", conversation_id="fixed-id",
                         alias="late-alias")
    assert again.id == "fixed-id"
    assert again.alias == "late-alias"
    assert store.find_by_alias("team", "late-alias").id == "fixed-id"


def test_create_with_existing_id_keeps_alias_when_already_set():
    store = SQLiteStore(":memory:")
    store.create("team", "claude", conversation_id="cid", alias="keep")
    # existing alias is non-empty -> backfill branch is skipped
    again = store.create("team", "claude", conversation_id="cid",
                         alias="ignored")
    assert again.id == "cid"
    assert again.alias == "keep"


# ===== find_by_alias empty alias (161) =====

def test_find_by_alias_empty_returns_none():
    store = SQLiteStore(":memory:")
    store.create("team", "claude", alias="something")
    assert store.find_by_alias("team", "") is None


def test_find_by_alias_unmatched_returns_none():
    store = SQLiteStore(":memory:")
    store.create("team", "claude", alias="present")
    assert store.find_by_alias("team", "absent") is None


# ===== get_messages agent filter (191-192) =====

def test_get_messages_filtered_by_agent():
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude")
    store.add_message(conv.id, Message(
        role="assistant", content="from-bull", timestamp=datetime.now(),
        agent="bull"))
    store.add_message(conv.id, Message(
        role="assistant", content="from-bear", timestamp=datetime.now(),
        agent="bear"))
    only_bull = store.get_messages(conv.id, agent="bull")
    assert [m.content for m in only_bull] == ["from-bull"]
    assert only_bull[0].agent == "bull"
    # No agent filter -> both messages.
    assert len(store.get_messages(conv.id)) == 2


# ===== set_metadata on missing conversation (230) =====

def test_set_metadata_on_missing_conversation_is_noop():
    store = SQLiteStore(":memory:")
    # Should return early without raising.
    store.set_metadata("does-not-exist", "key", "value")
    assert store.get("does-not-exist") is None


# ===== set_alias branches (240-253) =====

def test_set_alias_on_missing_conversation_is_noop():
    store = SQLiteStore(":memory:")
    store.set_alias("ghost", "anything")  # early return at the missing-row check
    assert store.get("ghost") is None


def test_set_alias_reassigns_and_strips_previous_holder():
    store = SQLiteStore(":memory:")
    a = store.create("team", "claude", alias="shared")
    b = store.create("team", "codex")
    # Move "shared" to b -> a must lose it (uniqueness 박탈).
    store.set_alias(b.id, "shared")
    assert store.get(b.id).alias == "shared"
    assert store.get(a.id).alias == ""
    assert store.find_by_alias("team", "shared").id == b.id


def test_set_alias_clear_to_empty():
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude", alias="named")
    store.set_alias(conv.id, "")
    assert store.get(conv.id).alias == ""
    assert store.find_by_alias("team", "named") is None


# ===== record_usage alias backfill from conversation (264-269) =====

def test_record_usage_backfills_alias_from_conversation():
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude", alias="from-conv")
    # No alias passed -> store should read it off the conversation row.
    store.record_usage(conv.id, total_tokens=20, provider="claude")
    stats = store.get_token_stats("team", group_by="alias")
    assert "from-conv" in stats["groups"]
    assert stats["groups"]["from-conv"]["total_tokens"] == 20


# ===== get_token_stats filters: alias/provider/model/agent (302-312) =====

def _seed_multiaxis(store):
    conv = store.create("team", "claude")
    store.record_usage(conv.id, total_tokens=100, prompt_tokens=60,
                       completion_tokens=40, provider="claude",
                       model="sonnet", agent="bull", alias="a1")
    store.record_usage(conv.id, total_tokens=10, prompt_tokens=6,
                       completion_tokens=4, provider="codex",
                       model="gpt5", agent="bear", alias="a2")
    return conv


def test_token_stats_filter_by_alias():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", alias="a1")
    assert stats["total_tokens"] == 100
    assert stats["total_calls"] == 1


def test_token_stats_filter_by_provider():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", provider="codex")
    assert stats["total_tokens"] == 10
    assert stats["total_calls"] == 1


def test_token_stats_filter_by_model():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", model="sonnet")
    assert stats["total_tokens"] == 100
    assert stats["total_calls"] == 1


def test_token_stats_filter_by_agent():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", agent="bear")
    assert stats["total_tokens"] == 10
    assert stats["total_calls"] == 1


def test_token_stats_combined_filters_no_owner_join():
    """Filtering without owner exercises the no-JOIN path plus AND filters."""
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats(provider="claude", model="sonnet",
                                  agent="bull", alias="a1")
    assert stats["total_tokens"] == 100
    assert stats["total_calls"] == 1


# ===== unreliable prompt-token accounting (341, 360-361) =====

def test_token_stats_counts_unreliable_prompt_calls():
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude")
    store.record_usage(conv.id, total_tokens=10, prompt_tokens=5,
                       prompt_tokens_reliable=False, provider="claude")
    store.record_usage(conv.id, total_tokens=10, prompt_tokens=5,
                       prompt_tokens_reliable=True, provider="claude")
    stats = store.get_token_stats("team", group_by="provider")
    assert stats["prompt_tokens_unreliable_calls"] == 1
    # group totals also track the unreliable count
    assert stats["groups"]["claude"]["prompt_tokens_unreliable_calls"] == 1


# ===== group_by axes (344-367) + _sqlite_group_key (375-386) =====

def test_token_stats_group_by_provider():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", group_by="provider")
    assert stats["group_by"] == "provider"
    assert stats["groups"]["claude"]["total_tokens"] == 100
    assert stats["groups"]["codex"]["total_tokens"] == 10


def test_token_stats_group_by_model():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", group_by="model")
    assert stats["groups"]["sonnet"]["total_tokens"] == 100
    assert stats["groups"]["gpt5"]["total_tokens"] == 10


def test_token_stats_group_by_alias():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", group_by="alias")
    assert stats["groups"]["a1"]["total_tokens"] == 100
    assert stats["groups"]["a2"]["total_tokens"] == 10


def test_token_stats_group_by_agent():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", group_by="agent")
    assert stats["groups"]["bull"]["total_tokens"] == 100
    assert stats["groups"]["bear"]["total_tokens"] == 10


def test_token_stats_group_by_day():
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude")
    store.record_usage(conv.id, total_tokens=42, provider="claude")
    stats = store.get_token_stats("team", group_by="day")
    today = datetime.now().isoformat()[:10]
    assert today in stats["groups"]
    assert stats["groups"][today]["total_tokens"] == 42


def test_token_stats_group_by_unknown_axis_uses_residual_key():
    """An unrecognized axis bucket falls through to the 'unknown' key."""
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude")
    store.record_usage(conv.id, total_tokens=5, provider="claude")
    stats = store.get_token_stats("team", group_by="nonexistent-axis")
    assert stats["group_by"] == "nonexistent-axis"
    assert "unknown" in stats["groups"]
    assert stats["groups"]["unknown"]["total_tokens"] == 5


def test_token_stats_group_by_none_omits_groups():
    store = SQLiteStore(":memory:")
    _seed_multiaxis(store)
    stats = store.get_token_stats("team", group_by=None)
    assert "groups" not in stats
    assert "group_by" not in stats
    assert stats["total_tokens"] == 110


def test_token_stats_group_keys_fall_back_for_blank_fields():
    """Blank provider/model/alias/agent map to their residual labels."""
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude")
    # No provider/model/alias/agent supplied -> all blank.
    store.record_usage(conv.id, total_tokens=3)

    assert "unknown" in store.get_token_stats(
        "team", group_by="provider")["groups"]
    assert "unknown" in store.get_token_stats(
        "team", group_by="model")["groups"]
    assert "(no alias)" in store.get_token_stats(
        "team", group_by="alias")["groups"]
    assert "(no agent)" in store.get_token_stats(
        "team", group_by="agent")["groups"]


def test_token_stats_empty_store_returns_zeroed_totals():
    store = SQLiteStore(":memory:")
    stats = store.get_token_stats("team")
    assert stats["total_tokens"] == 0
    assert stats["total_calls"] == 0
    assert stats["by_provider"] == {}


def test_token_stats_days_window_excludes_old_rows():
    """days>0 filters by timestamp; a backdated usage row is excluded."""
    store = SQLiteStore(":memory:")
    conv = store.create("team", "claude")
    store.record_usage(conv.id, total_tokens=7, provider="claude")
    # Backdate the row well past the window.
    old = (datetime.now() - timedelta(days=30)).isoformat()
    store._conn.execute("UPDATE usage_log SET timestamp=?", (old,))
    store._conn.commit()
    stats = store.get_token_stats("team", days=7)
    assert stats["total_calls"] == 0
    # days=0 means "all rows" -> the old row reappears.
    assert store.get_token_stats("team", days=0)["total_calls"] == 1
