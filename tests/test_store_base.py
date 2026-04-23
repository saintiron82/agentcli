import pytest
from agentcli.store.base import ConversationStore


def test_cannot_instantiate_abc():
    with pytest.raises(TypeError):
        ConversationStore()


def test_concrete_subclass():
    class FakeStore(ConversationStore):
        def create(self, owner, provider, model="", *, conversation_id="",
                   alias=""): pass
        def get(self, conversation_id): pass
        def find_by_alias(self, owner, alias): pass
        def add_message(self, conversation_id, message): pass
        def get_messages(self, conversation_id, limit=0, agent=""): return []
        def delete(self, conversation_id): pass
        def list_by_owner(self, owner, limit=20): return []
        def set_metadata(self, conversation_id, key, value): pass
        def set_alias(self, conversation_id, alias): pass
        def record_usage(self, conversation_id, *, prompt_tokens=0,
                         completion_tokens=0, total_tokens=0, cached_tokens=0,
                         latency_ms=0, provider="", model="",
                         agent="", alias=""): pass
        def get_token_stats(self, owner="", days=7, *, alias="",
                            provider="", model="", agent="",
                            group_by=None): return {}

    store = FakeStore()
    assert store.get_messages("x") == []
