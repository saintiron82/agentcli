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


def test_build_session_prompt_session_mode_is_system_plus_latest_user():
    """CLI 네이티브 세션 모드: client 는 [system?, user] 만 전달한다."""
    prompt = build_session_prompt([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="new question"),
    ])
    assert "Follow GUIDE v2" in prompt
    assert "new question" in prompt
    assert "Context" not in prompt


def test_build_session_prompt_single_user_is_passthrough():
    """미사용 모드: 최신 user 만 있으면 가공 없이 그대로."""
    prompt = build_session_prompt([
        Message(role="user", content="new question"),
    ])
    assert prompt == "new question"


def test_build_session_prompt_serializes_injected_context():
    """호스트 주입 모드: 중간 메시지는 Context 블록으로 직렬화된다.

    무엇을 담을지는 client 의 모드 결정 (세션 경로는 [system?, user] 만
    전달) — provider 는 받은 것을 충실히 직렬화한다.
    """
    prompt = build_session_prompt([
        Message(role="system", content="Follow GUIDE v2"),
        Message(role="user", content="bull says market is up", agent="bull"),
        Message(role="assistant", content="noted", agent="bull"),
        Message(role="user", content="new question"),
    ])
    assert "Follow GUIDE v2" in prompt
    assert "Context (injected by host application):" in prompt
    assert "[user:bull] bull says market is up" in prompt
    assert "[assistant:bull] noted" in prompt
    assert "User request:\nnew question" in prompt
    # 순서: system → context → 최신 user 요청
    assert prompt.index("System instructions:") \
        < prompt.index("Context (injected") \
        < prompt.index("User request:")


def test_build_session_prompt_context_without_system():
    prompt = build_session_prompt([
        Message(role="user", content="prior note"),
        Message(role="user", content="ask now"),
    ])
    assert "Context (injected by host application):" in prompt
    assert "[user] prior note" in prompt
    assert "User request:\nask now" in prompt


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
