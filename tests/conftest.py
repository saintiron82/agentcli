import pytest
from agentcli.store.memory import MemoryStore
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import Message, LLMResponse, TokenUsage


class MockProvider(LLMProvider):
    provider_id = "mock"
    supports_sessions = False

    def __init__(self, response: str = "mock response", fail: bool = False):
        self._response = response
        self._fail = fail
        self.last_messages: list[Message] = []

    def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
        self.last_messages = list(messages)
        self.last_session_id = session_id
        self.last_cwd = cwd
        if self._fail:
            return LLMResponse(content="", provider=self.provider_id, model=model)
        return LLMResponse(
            content=self._response, provider=self.provider_id, model=model,
            tokens=TokenUsage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
            latency_ms=100,
            session_id=session_id or "mock-session")

    def list_models(self):
        return [{"id": "", "name": "mock"}]

    def is_available(self):
        return not self._fail


@pytest.fixture
def memory_store():
    return MemoryStore()


@pytest.fixture
def mock_provider():
    return MockProvider()


@pytest.fixture
def mock_registry(mock_provider):
    reg = ProviderRegistry()
    reg.register(mock_provider)
    reg.set_fallback_order(["mock"])
    return reg
