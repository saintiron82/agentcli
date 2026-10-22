import os
from agentcli import LLMClient, MemoryStore
from agentcli.providers.registry import ProviderRegistry
from tests.test_session_routing import FakeSessionProvider


def test_explicit_reset_detects_same_size_same_mtime_changes(tmp_path):
    provider = FakeSessionProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    client = LLMClient(MemoryStore(), registry)
    guide = tmp_path / "GUIDE.md"
    guide.write_text("guide v1", encoding="utf-8")
    args = dict(provider="sessprov", owner="bot", alias="guide", cwd=str(tmp_path))
    first = client.chat("hi", **args)
    stamp = guide.stat()
    guide.write_text("guide v2", encoding="utf-8")
    os.utime(guide, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    second = client.chat("again", reset_on_instruction_change=True, **args)
    assert provider.last_session_id == ""
    assert second.session_id != first.session_id
