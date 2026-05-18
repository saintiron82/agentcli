from agentcli.client import LLMClient
from tests.conftest import MockProvider
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import LLMResponse, TokenUsage


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
    resp = client.chat("hello", provider="fail", owner="bot1",
                       fallback=True)
    assert resp.content == "fallback ok"


def test_chat_does_not_fallback_unless_requested(memory_store):
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
    assert resp.content == ""
    assert ok_provider.last_messages == []


def test_provider_with_minimal_signature_still_works(memory_store):
    class MinimalProvider(LLMProvider):
        provider_id = "minimal"

        def __init__(self):
            self.kwargs_seen = None

        def invoke(self, messages, *, model="", timeout=120):
            self.kwargs_seen = {"model": model, "timeout": timeout}
            return LLMResponse(
                content="ok", provider=self.provider_id, model=model,
                tokens=TokenUsage(total_tokens=1))

        def list_models(self):
            return []

        def is_available(self):
            return True

    p = MinimalProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["minimal"])
    client = LLMClient(store=memory_store, registry=reg)

    resp = client.chat(
        "hello", provider="minimal", model="m1", owner="u",
        alias="worker", cwd="/tmp")
    assert resp.content == "ok"
    assert p.kwargs_seen == {"model": "m1", "timeout": 120}


def test_alias_conversation_id_conflict_is_rejected(memory_store, mock_registry):
    client = LLMClient(store=memory_store, registry=mock_registry)
    first = client.chat(
        "one", provider="mock", owner="team",
        conversation_id="conv-one", alias="collector")
    assert first.conversation_id == "conv-one"

    try:
        client.chat(
            "two", provider="mock", owner="team",
            conversation_id="conv-two", alias="collector")
    except ValueError as exc:
        assert "alias conflict" in str(exc)
    else:
        raise AssertionError("expected alias conflict")

    assert memory_store.get("conv-two") is None
    assert memory_store.find_by_alias("team", "collector").id == "conv-one"


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
    resp = client.chat("hello", provider="f1", owner="bot1",
                       fallback=True)
    assert resp.content == ""


def test_list_models(memory_store, mock_registry):
    client = LLMClient(store=memory_store, registry=mock_registry)
    models = client.list_models("mock")
    assert len(models) >= 1


def test_select_model_resolves_alias(memory_store):
    class ModelProvider(MockProvider):
        provider_id = "modelp"

        def __init__(self):
            super().__init__()
            self.last_model = ""

        def invoke(self, messages, *, model="", timeout=120,
                   session_id="", cwd=None):
            self.last_model = model
            return LLMResponse(
                content="ok", provider=self.provider_id, model=model,
                tokens=TokenUsage(total_tokens=1))

        def list_models(self):
            return [
                {"id": "", "name": "default"},
                {"id": "real-model", "name": "Real Model",
                 "aliases": ["real"]},
            ]

    p = ModelProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["modelp"])
    client = LLMClient(store=memory_store, registry=reg)

    assert client.select_model("modelp", "real") == "real-model"
    resp = client.chat("hello", provider="modelp", model="real",
                       owner="u", strict_model=True)
    assert resp.model == "real-model"
    assert p.last_model == "real-model"


def test_strict_model_rejects_unknown_before_call(memory_store):
    class ModelProvider(MockProvider):
        provider_id = "modelp"

        def __init__(self):
            super().__init__()
            self.call_count = 0

        def invoke(self, messages, *, model="", timeout=120,
                   session_id="", cwd=None):
            self.call_count += 1
            return super().invoke(messages, model=model, timeout=timeout,
                                  session_id=session_id, cwd=cwd)

        def list_models(self):
            return [{"id": "known", "name": "Known"}]

    p = ModelProvider()
    reg = ProviderRegistry()
    reg.register(p)
    reg.set_fallback_order(["modelp"])
    client = LLMClient(store=memory_store, registry=reg)

    try:
        client.chat("hello", provider="modelp", model="unknown",
                    owner="u", strict_model=True)
    except ValueError as exc:
        assert "unsupported model" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert p.call_count == 0


def test_health_check_unknown_provider(memory_store, mock_registry):
    client = LLMClient(store=memory_store, registry=mock_registry)
    health = client.health_check("missing")
    assert health.ok is False
    assert health.status == "unknown_provider"
