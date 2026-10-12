import asyncio

import pytest

from agentcli import LLMClient, MemoryStore, SQLiteStore, ProviderRegistry
from agentcli.types import Message
from agentcli.client import _native_alias
from tests.conftest import MockProvider


@pytest.fixture(params=[MemoryStore, SQLiteStore])
def store(request):
    result = request.param()
    yield result
    if hasattr(result, "close"):
        result.close()


def make_client(store, **kwargs):
    provider = MockProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    return LLMClient(store, registry=registry, **kwargs), provider


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_cross_owner_resume_is_denied_without_mutation(store, mode):
    client, provider = make_client(store)
    first = client.chat("private fake", provider="mock", owner="A")
    before = store.get_messages(first.conversation_id)
    kwargs = {"provider": "mock", "owner": "B", "conversation_id": first.conversation_id}
    with pytest.raises(PermissionError):
        if mode == "sync":
            client.chat("attack", **kwargs)
        elif mode == "async":
            asyncio.run(client.chat_async("attack", **kwargs))
        else:
            async def consume():
                return [c async for c in client.chat_stream("attack", **kwargs)]
            asyncio.run(consume())
    assert store.get_messages(first.conversation_id) == before
    assert store.list_by_owner("B") == []
    assert provider.last_messages[-1].content == "private fake"


def test_cross_owner_context_requires_host_authorization(store):
    client, _ = make_client(store)
    source = store.create("A", "mock")
    store.add_message(source.id, Message("user", "private fake"))
    with pytest.raises(PermissionError):
        client.chat("test", provider="mock", owner="B", inject_context=[{"conversation_id": source.id}])
    assert store.list_by_owner("B") == []
    shared, provider = make_client(store, context_authorizer=lambda owner, conv: owner == "B" and conv.id == source.id)
    shared.chat("test", provider="mock", owner="B", inject_context=[{"conversation_id": source.id}])
    assert any(m.content == "private fake" for m in provider.last_messages)


def test_workspace_reuse_denied_and_separate_alias_still_works(store, tmp_path):
    client, _ = make_client(store)
    first = client.chat("A", provider="mock", owner="o", alias="review", cwd=str(tmp_path / "A"))
    with pytest.raises(PermissionError, match="workspace"):
        client.chat("B", provider="mock", owner="o", alias="review", cwd=str(tmp_path / "B"))
    second = client.chat("B", provider="mock", owner="o", alias="review-B", cwd=str(tmp_path / "B"))
    assert first.conversation_id != second.conversation_id
    resumed = client.chat("A2", provider="mock", owner="o", alias="review", cwd=str(tmp_path / "A" / ".." / "A"))
    assert first.conversation_id == resumed.conversation_id


def test_liveness_lookup_also_checks_owner(store):
    client, _ = make_client(store)
    first = client.chat("A", provider="mock", owner="A")
    with pytest.raises(PermissionError):
        client.session_alive("mock", owner="B", conversation_id=first.conversation_id)


def test_store_create_rejects_cross_owner_identifier(store):
    first = store.create("A", "mock", conversation_id="fixed")
    with pytest.raises(PermissionError):
        store.create("B", "mock", conversation_id="fixed", alias="unsafe")
    assert store.get(first.id).owner == "A"
    assert store.get(first.id).alias == ""


def test_copilot_named_sessions_are_scoped_to_app_conversation():
    from agentcli.providers.copilot import CopilotProvider
    provider = CopilotProvider()
    assert _native_alias(provider, "review", "owner-A-conversation") != _native_alias(provider, "review", "owner-B-conversation")
    assert _native_alias(provider, "review", "owner-A-conversation") == _native_alias(provider, "review", "owner-A-conversation")
