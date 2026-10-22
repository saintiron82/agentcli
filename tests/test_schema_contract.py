import asyncio
import pytest
from agentcli import LLMClient, MemoryStore
from agentcli.schema import assert_supported_schema, validate
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.types import LLMResponse, TokenUsage


@pytest.mark.parametrize("schema", [{"required": None}, {"enum": None}, {"required": "ok"},
                                     {"required": [1]}, {"required": ["x", "x"]},
                                     {"enum": []}, {"properties": []}, {"type": None}])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_invalid_schema_rejected_before_creating_or_invoking(schema, asynchronous):
    client = LLMClient(MemoryStore())
    with pytest.raises(ValueError):
        if asynchronous: asyncio.run(client.chat_async("x", output_schema=schema))
        else: client.chat("x", output_schema=schema)
    assert not client._store._conversations


@pytest.mark.parametrize("value", [-1, 1.2, True, "1"])
def test_invalid_retry_count_rejected_before_store(value):
    client = LLMClient(MemoryStore())
    with pytest.raises(ValueError): client.chat("x", schema_retries=value)
    with pytest.raises(ValueError): asyncio.run(client.chat_async("x", schema_retries=value))
    assert not client._store._conversations


@pytest.mark.parametrize("value,enum", [(True, [1]), ([True], [[1]]), ({"x": True}, [{"x": 1}])])
def test_enum_uses_json_boolean_semantics(value, enum):
    assert validate(value, {"enum": enum})
    assert validate(1.0, {"enum": [1]}) == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_fallback_receives_schema_correction_and_preserves_usage_metadata(asynchronous):
    class Fake(LLMProvider):
        supports_sessions = False
        def __init__(self, name): self.provider_id = name; self.calls = []
        def is_available(self): return True
        def list_models(self): return []
        def invoke(self, messages, **options):
            self.calls.append(messages)
            if self.provider_id == "primary":
                return LLMResponse("", self.provider_id, "", error_type="network", error="fake")
            text = '{"ok":true}' if len(self.calls) > 1 and "직전 응답" in messages[-1].content else '{"wrong":1}'
            return LLMResponse(text, self.provider_id, "", tokens=TokenUsage(
                total_tokens=10, payload_prompt_tokens=7, prompt_tokens_reliable=False,
                prompt_tokens_source="fake_estimate"))
    registry = ProviderRegistry()
    primary, fallback = Fake("primary"), Fake("fallback")
    registry.register(primary); registry.register(fallback)
    registry.set_fallback_order(["primary", "fallback"])
    client = LLMClient(MemoryStore(), registry)
    options = dict(provider="primary", fallback=True, output_schema={"type": "object", "required": ["ok"]})
    response = (asyncio.run(client.chat_async("task", **options)) if asynchronous
                else client.chat("task", **options))
    assert response.parsed == {"ok": True}
    assert fallback.calls[0] != fallback.calls[1]
    assert response.tokens.total_tokens == 20
    assert response.tokens.payload_prompt_tokens == 14
    assert not response.tokens.prompt_tokens_reliable
    assert response.tokens.prompt_tokens_source == "fake_estimate"
