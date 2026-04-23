from agentcli.providers.registry import ProviderRegistry, create_default_registry
from agentcli.providers.base import LLMProvider
from agentcli.types import Message, LLMResponse, TokenUsage


class FakeProvider(LLMProvider):
    provider_id = "fake"
    def invoke(self, messages, model="", timeout=120):
        return LLMResponse(content="ok", provider="fake", model=model, tokens=TokenUsage())
    def list_models(self): return [{"id": "", "name": "default"}]
    def is_available(self): return True


def test_register_and_get():
    reg = ProviderRegistry()
    reg.register(FakeProvider())
    assert reg.get("fake") is not None
    assert reg.get("unknown") is None


def test_list_providers():
    reg = ProviderRegistry()
    reg.register(FakeProvider())
    providers = reg.list_providers()
    assert len(providers) == 1
    assert providers[0]["id"] == "fake"
    assert providers[0]["available"] is True


def test_list_models():
    reg = ProviderRegistry()
    reg.register(FakeProvider())
    models = reg.list_models("fake")
    assert len(models) >= 1


def test_list_all_models():
    reg = ProviderRegistry()
    reg.register(FakeProvider())
    models = reg.list_models()
    assert all("provider" in m for m in models)


def test_fallback():
    reg = ProviderRegistry()
    reg.set_fallback_order(["codex", "claude", "copilot"])
    assert reg.get_fallback_chain() == ["codex", "claude", "copilot"]
    assert reg.get_next_fallback("codex") == "claude"
    assert reg.get_next_fallback("claude") == "copilot"
    assert reg.get_next_fallback("copilot") is None
    assert reg.get_next_fallback("unknown") is None


def test_default_registry():
    reg = create_default_registry()
    providers = reg.list_providers()
    ids = [p["id"] for p in providers]
    assert "claude" in ids
    assert "codex" in ids
    assert "copilot" in ids
    # 세션 provider 우선, Codex는 --full-auto 부담으로 후순위.
    assert reg.get_fallback_chain() == ["claude", "copilot", "codex"]
