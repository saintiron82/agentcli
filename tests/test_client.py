from agentcli.client import LLMClient
from tests.conftest import MockProvider
from agentcli.providers.registry import ProviderRegistry


def test_init(memory_store, mock_registry):
    client = LLMClient(store=memory_store, registry=mock_registry)
    providers = client.list_providers()
    assert len(providers) >= 1


def test_chat_new_conversation(memory_store, mock_registry):
    client = LLMClient(store=memory_store, registry=mock_registry)
    resp = client.chat("hello", provider="mock", owner="bot1")
    assert resp.content == "mock response"
    assert resp.conversation_id != ""

    # 저장소에 대화가 생성됨
    convs = memory_store.list_by_owner("bot1")
    assert len(convs) == 1
    msgs = memory_store.get_messages(convs[0].id)
    assert len(msgs) == 2  # user + assistant


def test_chat_resume_conversation(memory_store, mock_registry, mock_provider):
    client = LLMClient(store=memory_store, registry=mock_registry)
    resp1 = client.chat("first", provider="mock", owner="bot1")
    conv_id = resp1.conversation_id

    resp2 = client.chat("second", provider="mock", owner="bot1",
                        conversation_id=conv_id)
    assert resp2.conversation_id == conv_id

    # 두 번째 호출 시 이전 메시지가 포함됨
    assert len(mock_provider.last_messages) > 1


def test_chat_context_turns(memory_store, mock_registry, mock_provider):
    client = LLMClient(store=memory_store, registry=mock_registry)
    resp = client.chat("msg1", provider="mock", owner="bot1")
    conv_id = resp.conversation_id
    client.chat("msg2", provider="mock", owner="bot1", conversation_id=conv_id)
    client.chat("msg3", provider="mock", owner="bot1", conversation_id=conv_id)

    # context_turns=1: 직전 1턴만 (user+assistant 2개 메시지) + 현재 user = 3
    client.chat("msg4", provider="mock", owner="bot1",
                conversation_id=conv_id, context_turns=1)
    assert len(mock_provider.last_messages) <= 3


def test_chat_system_prompt(memory_store, mock_registry, mock_provider):
    client = LLMClient(store=memory_store, registry=mock_registry)
    client.chat("hello", provider="mock", owner="bot1",
                system_prompt="You are a trader")
    assert mock_provider.last_messages[0].role == "system"
    assert "trader" in mock_provider.last_messages[0].content


def test_chat_fallback(memory_store):
    fail_provider = MockProvider(fail=True)
    fail_provider.provider_id = "fail"
    ok_provider = MockProvider(response="fallback ok")
    ok_provider.provider_id = "ok"

    reg = ProviderRegistry()
    reg.register(fail_provider)
    reg.register(ok_provider)
    reg.set_fallback_order(["fail", "ok"])

    client = LLMClient(store=memory_store, registry=reg)
    resp = client.chat("hello", provider="fail", owner="bot1")
    assert resp.content == "fallback ok"


def test_chat_all_fail(memory_store):
    fail1 = MockProvider(fail=True)
    fail1.provider_id = "f1"
    fail2 = MockProvider(fail=True)
    fail2.provider_id = "f2"

    reg = ProviderRegistry()
    reg.register(fail1)
    reg.register(fail2)
    reg.set_fallback_order(["f1", "f2"])

    client = LLMClient(store=memory_store, registry=reg)
    resp = client.chat("hello", provider="f1", owner="bot1")
    assert resp.content == ""


def test_list_models(memory_store, mock_registry):
    client = LLMClient(store=memory_store, registry=mock_registry)
    models = client.list_models("mock")
    assert len(models) >= 1
