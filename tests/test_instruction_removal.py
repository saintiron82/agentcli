import asyncio
import importlib
import pytest
from agentcli import MemoryStore
from tests.test_turn_state import client_for


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("source", ["file", "system"])
def test_removing_instructions_resets_native_session(tmp_path, mode, source):
    client, provider = client_for(MemoryStore())
    path = tmp_path / "AGENTS.md"
    if source == "file": path.write_text("old instructions", encoding="utf-8")
    async def call(system):
        options = dict(provider="gate", owner="o", alias="a", cwd=str(tmp_path),
                       system_prompt=system, reset_on_instruction_change=True)
        if mode == "sync": return client.chat("fast", **options)
        if mode == "async": return await client.chat_async("fast", **options)
        return [c async for c in client.chat_stream("fast", **options)]
    async def run():
        await call("old system" if source == "system" else "")
        if source == "file": path.unlink()
        await call("")
        assert provider.inputs[-1] == ""
        conv = client._store.find_by_alias("o", "a")
        assert conv.metadata["instructions_hashes"] == {}
        assert conv.metadata["system_prompt_hash:gate"] == ""
        await call("")
        assert provider.inputs[-1] == "new-sid"  # Empty baseline is still a baseline.
    asyncio.run(run())


def test_instruction_hash_cache_is_bounded_and_deleted_files_removed(tmp_path, monkeypatch):
    module = importlib.import_module("agentcli.client")
    module._DRIFT_HASH_CACHE.clear()
    monkeypatch.setattr(module, "_DRIFT_CACHE_LIMIT", 3)
    for index in range(5):
        root = tmp_path / str(index)
        root.mkdir()
        path = root / "AGENTS.md"
        path.write_text(str(index))
        assert module._compute_instructions_hashes(str(root))
    assert len(module._DRIFT_HASH_CACHE) == 3
    path.unlink()
    assert module._compute_instructions_hashes(str(root)) == {}
    assert len(module._DRIFT_HASH_CACHE) == 2
    module._DRIFT_HASH_CACHE.clear()
