from datetime import datetime, timedelta
import pytest
from agentcli import MemoryStore, SQLiteStore


@pytest.mark.parametrize("axis", [None, "provider", "model", "alias", "agent", "day", "unknown-axis"])
@pytest.mark.parametrize("filters", [{}, {"owner": "a"}, {"provider": "codex"}, {"alias": "worker"},
                                     {"agent": "review"}, {"model": "m"}, {"owner": "missing"}])
def test_sql_stats_preserve_memory_contract(axis, filters):
    stores = MemoryStore(), SQLiteStore()
    for store in stores:
        for index in range(6):
            conv = store.create("a" if index % 2 else "b", "claude",
                                alias="worker" if index % 2 else "other")
            store.record_usage(conv.id, prompt_tokens=10, completion_tokens=2, total_tokens=12,
                               cached_tokens=3, payload_prompt_tokens=4, latency_ms=7,
                               prompt_tokens_reliable=index % 2 == 0,
                               provider="codex" if index % 2 else "", model="m" if index % 3 else "",
                               agent="review" if index % 2 else "")
    assert stores[0].get_token_stats(days=0, group_by=axis, **filters) == stores[1].get_token_stats(
        days=0, group_by=axis, **filters)
    stores[1].close()


def test_sql_stats_apply_timestamp_filter():
    store = SQLiteStore()
    conv = store.create("o", "p")
    for _ in range(2): store.record_usage(conv.id, total_tokens=10)
    store._conn.execute("UPDATE usage_log SET timestamp=? WHERE id=1",
                        ((datetime.now() - timedelta(days=30)).isoformat(),))
    store._conn.commit()
    assert store.get_token_stats(days=7)["total_calls"] == 1
    assert store.get_token_stats(days=0)["total_calls"] == 2
    store.close()
