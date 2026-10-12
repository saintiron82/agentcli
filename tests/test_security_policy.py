import asyncio
from unittest.mock import patch

import pytest

from agentcli import AgentProfile, LLMClient, MemoryStore, ProviderRegistry
from agentcli.providers.base import LLMProvider
from agentcli.providers.copilot import CopilotProvider
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
