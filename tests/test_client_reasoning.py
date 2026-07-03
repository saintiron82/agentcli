import pytest
from agentcli import LLMClient, MemoryStore

def test_double_specify_effort_raises():
    c = LLMClient(store=MemoryStore())
    with pytest.raises(ValueError):
        # effort as first-class kwarg AND inside provider_options -> ambiguous
        c.chat("hi", provider="claude", effort="high",
               provider_options={"effort": "low"})

def test_effort_threads_into_supported_kwargs():
    # _supported_kwargs must keep 'effort' for a provider that accepts it
    from agentcli.client import _supported_kwargs
    from agentcli.providers.claude import ClaudeProvider
    kw = _supported_kwargs(ClaudeProvider(), "invoke",
                           {"model": "", "effort": "high", "thinking": None})
    assert kw.get("effort") == "high"
