import asyncio
from unittest.mock import patch

import pytest

from agentcli import AgentProfile, LLMClient, MemoryStore, ProviderRegistry
from agentcli.providers.base import LLMProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.client import _fallback_preserves_policy
from agentcli.types import LLMResponse, StreamChunk


class Recorder(LLMProvider):
    provider_id = "record"
    def __init__(self):
        self.calls = []
    def invoke(self, messages, *, model="", **kwargs):
        self.calls.append(kwargs)
        return LLMResponse("ok", self.provider_id, model)
    async def invoke_async(self, messages, **kwargs):
        return self.invoke(messages, **kwargs)
    async def stream_async(self, messages, **kwargs):
        self.calls.append(kwargs)
        yield StreamChunk(type="text", content="ok")
        yield StreamChunk(type="done", content="ok")
    def list_models(self):
        return []
    def is_available(self):
        return True


def client_for(provider):
    registry = ProviderRegistry()
    registry.register(provider)
    return LLMClient(MemoryStore(), registry=registry)


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_profile_permissions_reach_every_call_path(mode):
    provider = Recorder()
    client = client_for(provider)
    profile = AgentProfile("review", provider="record", allowed_tools=["Read"],
                           disallowed_tools=["Write"], mcp_config={"demo": {"command": "fake"}})
    if mode == "sync":
        profile.chat("test", client=client)
    elif mode == "async":
        asyncio.run(profile.chat_async("test", client=client))
    else:
        async def consume():
            return [c async for c in profile.chat_stream("test", client=client)]
        asyncio.run(consume())
    assert provider.calls[-1]["allowed_tools"] == ["Read"]
    assert provider.calls[-1]["disallowed_tools"] == ["Write"]
    assert provider.calls[-1]["mcp_config"] == profile.mcp_config


def test_profile_options_merge_without_mutating_inputs():
    profile = AgentProfile("p", allowed_tools=["Read"], disallowed_tools=["Write"])
    options = {"allowed_tools": ["Grep"], "debug": True}
    kwargs = profile._build_kwargs(owner="o", cwd_override=None, overrides={"provider_options": options})
    assert kwargs["provider_options"] == {"allowed_tools": ["Grep"], "disallowed_tools": ["Write"], "debug": True}
    assert options == {"allowed_tools": ["Grep"], "debug": True}
    assert "provider_options" not in AgentProfile("empty")._build_kwargs(owner="", cwd_override=None, overrides={})


def test_copilot_per_call_allowlist_disables_allow_all():
    provider = CopilotProvider()
    with patch.object(provider, "_find_binary", return_value=("fake", False)):
        cmd, _ = provider._build_cmd("test", "", "", allowed_tools=["Read"], disallowed_tools=["Write"])
        unrestricted, _ = provider._build_cmd("test", "", "")
    assert "--allow-all-tools" not in cmd
    assert "--allow-tool" in cmd and "Read" in cmd
    assert "--deny-tool" in cmd and "Write" in cmd
    assert "--allow-all-tools" in unrestricted


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_unsupported_security_options_fail_before_execution(mode):
    class Unsupported(Recorder):
        def invoke(self, messages, *, model="", timeout=120, session_id="", cwd=None):
            raise AssertionError("must not execute")
        async def invoke_async(self, messages, *, model="", timeout=120, session_id="", cwd=None):
            raise AssertionError("must not execute")
        async def stream_async(self, messages, *, model="", timeout=120, session_id="", cwd=None):
            raise AssertionError("must not execute")
            yield
    client = client_for(Unsupported())
    options = {"provider_options": {"sandbox_mode": "read-only"}, "provider": "record"}
    if mode == "sync":
        with pytest.raises(ValueError, match="Unsupported security"):
            client.chat("test", **options)
    elif mode == "async":
        with pytest.raises(ValueError, match="Unsupported security"):
            asyncio.run(client.chat_async("test", **options))
    else:
        async def consume():
            return [c async for c in client.chat_stream("test", **options)]
        with pytest.raises(ValueError, match="Unsupported security"):
            asyncio.run(consume())
    assert client._store.list_by_owner("") == []


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_restrictive_constructor_and_per_call_policy_do_not_fallback(mode):
    class Restricted(Recorder):
        provider_id = "restricted"
        _sandbox_mode = "read-only"
        def invoke(self, messages, *, model="", **kwargs):
            return LLMResponse("", self.provider_id, model, error="fake authentication error")
        async def stream_async(self, messages, **kwargs):
            yield StreamChunk(type="error", content="fake authentication error")
    primary, candidate = Restricted(), Recorder()
    registry = ProviderRegistry()
    registry.register(primary)
    registry.register(candidate)
    registry.set_fallback_order(["restricted", "record"])
    client = LLMClient(MemoryStore(), registry=registry)
    kwargs = {"provider": "restricted", "fallback": True}
    if mode == "sync":
        result = client.chat("test", **kwargs)
        assert "Fallback blocked" in result.suggested_action
    elif mode == "async":
        result = asyncio.run(client.chat_async("test", **kwargs))
        assert "Fallback blocked" in result.suggested_action
    else:
        async def consume():
            return [c async for c in client.chat_stream("test", **kwargs)]
        chunks = asyncio.run(consume())
        assert any(c.data.get("fallback_blocked") for c in chunks if c.type == "error")
    assert candidate.calls == []


def test_native_policies_are_compared_without_launching_clis():
    assert not _fallback_preserves_policy(CodexProvider(sandbox_mode="read-only"), ClaudeProvider(), None)
    assert not _fallback_preserves_policy(CodexProvider(), ClaudeProvider(), {"sandbox_mode": "read-only"})
    assert _fallback_preserves_policy(CodexProvider(sandbox_mode="read-only"), CodexProvider(sandbox_mode="read-only"), None)
    assert not _fallback_preserves_policy(CodexProvider(sandbox_mode="read-only"), CodexProvider(), None)
    assert _fallback_preserves_policy(Recorder(), Recorder(), None)


def test_codex_resume_requires_explicit_fresh_session_for_policy_change():
    class FakeCodex(CodexProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0
        def invoke(self, messages, *, model="", session_id="", **kwargs):
            self.calls += 1
            return LLMResponse("ok", "codex", model, session_id=session_id or "fake-native")
    provider = FakeCodex()
    client = client_for(provider)
    client.chat("one", provider="codex", alias="review")
    with pytest.raises(PermissionError, match="new_session"):
        client.chat("two", provider="codex", alias="review", provider_options={"sandbox_mode": "read-only"})
    assert provider.calls == 1
    client.chat("two", provider="codex", alias="review", new_session=True,
                provider_options={"sandbox_mode": "read-only"})
    client.chat("three", provider="codex", alias="review", provider_options={"sandbox_mode": "read-only"})
    assert provider.calls == 3
