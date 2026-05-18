import pytest
from agentcli.providers.base import LLMProvider, build_session_prompt
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


def test_resolve_model_aliases():
    class FakeProvider(LLMProvider):
        provider_id = "fake"

        def invoke(self, messages, *, model="", timeout=120, session_id=""):
            return LLMResponse(content="ok", provider="fake", model=model,
                               tokens=TokenUsage())

        def list_models(self):
            return [
                {"id": "", "name": "default"},
                {"id": "real-model", "name": "Real Model",
                 "aliases": ["real"]},
            ]

        def is_available(self):
            return True

    p = FakeProvider()
    assert p.resolve_model("real", strict=True) == "real-model"
    assert p.resolve_model("Real Model", strict=True) == "real-model"
    assert p.resolve_model("default", strict=True) == ""
    assert p.resolve_model("custom-model", strict=False) == "custom-model"
    with pytest.raises(ValueError):
        p.resolve_model("custom-model", strict=True)


def test_build_session_prompt_keeps_system_and_latest_user_only():
    prompt = build_session_prompt([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="new question"),
    ])
    assert "Follow GUIDE v2" in prompt
    assert "new question" in prompt
    assert "old question" not in prompt
    assert "old answer" not in prompt


def test_build_session_prompt_without_system_is_latest_user():
    prompt = build_session_prompt([
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="new question"),
    ])
    assert prompt == "new question"


def test_default_health_check_reports_binary_missing():
    class FakeProvider(LLMProvider):
        provider_id = "fake"

        def invoke(self, messages, *, model="", timeout=120, session_id=""):
            return LLMResponse(content="ok", provider="fake", model=model,
                               tokens=TokenUsage())

        def list_models(self):
            return []

        def is_available(self):
            return False

    health = FakeProvider().health_check()
    assert health.ok is False
    assert health.status == "binary_missing"
    assert health.suggested_action


def test_missing_invoke_raises():
    with pytest.raises(TypeError):
        class Incomplete(LLMProvider):
            provider_id = "bad"
            def list_models(self): return []
            def is_available(self): return True
        Incomplete()
