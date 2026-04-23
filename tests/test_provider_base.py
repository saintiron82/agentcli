import pytest
from agentcli.providers.base import LLMProvider
from agentcli.types import Message, LLMResponse, TokenUsage


def test_cannot_instantiate_abc():
    with pytest.raises(TypeError):
        LLMProvider()


def test_concrete_subclass():
    class FakeProvider(LLMProvider):
        provider_id = "fake"

        def invoke(self, messages, *, model="", timeout=120, session_id=""):
            return LLMResponse(content="ok", provider="fake", model=model,
                               tokens=TokenUsage())

        def list_models(self):
            return [{"id": "", "name": "default"}]

        def is_available(self):
            return True

    p = FakeProvider()
    assert p.provider_id == "fake"
    assert p.is_available()
    resp = p.invoke([Message(role="user", content="hi")])
    assert resp.content == "ok"


def test_missing_invoke_raises():
    with pytest.raises(TypeError):
        class Incomplete(LLMProvider):
            provider_id = "bad"
            def list_models(self): return []
            def is_available(self): return True
        Incomplete()
