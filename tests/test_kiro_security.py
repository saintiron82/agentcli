import asyncio
import json

import pytest

from agentcli.providers.kiro import KiroProvider
from agentcli.providers._acp import AcpConnection


def test_trust_false_cannot_write_without_explicit_filesystem_permission(tmp_path):
    provider = KiroProvider(trust_all=False, trust_tools=["write"])
    target = tmp_path / "file.txt"
    with pytest.raises(PermissionError):
        asyncio.run(provider._handle_agent_request("fs/write_text_file", {"path": str(target), "content": "fake"}, str(tmp_path)))
    assert not target.exists()
    explicit = KiroProvider(trust_all=False, allow_file_writes=True)
    explicit._fs_write({"path": str(target), "content": "fake"}, str(tmp_path))
    assert target.read_text(encoding="utf-8") == "fake"


def test_file_read_policy_and_size_limit(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("abcd", encoding="utf-8")
    with pytest.raises(PermissionError):
        KiroProvider(allow_file_reads=False)._fs_read({"path": str(target)}, str(tmp_path))
    with pytest.raises(ValueError, match="max_file_bytes"):
        KiroProvider(max_file_bytes=3)._fs_read({"path": str(target)}, str(tmp_path))
    with pytest.raises(ValueError, match="max_file_bytes"):
        KiroProvider(max_file_bytes=3)._fs_write({"path": str(target), "content": "long"}, str(tmp_path))
    assert target.read_text(encoding="utf-8") == "abcd"


def test_permission_denial_returns_rpc_error_without_breaking_transport():
    replies = []
    async def write(line):
        replies.append(json.loads(line))
    async def handler(method, params):
        raise PermissionError("FAKE_PRIVATE_PATH")
    conn = AcpConnection(write, on_request=handler)
    asyncio.run(conn.handle_line('{"id":1,"method":"fs/write_text_file","params":{}}'))
    assert replies[0]["error"]["code"] == -32001
    assert "FAKE_PRIVATE_PATH" not in json.dumps(replies)


def test_restricted_filesystem_capabilities_are_advertised():
    provider = KiroProvider(trust_all=False)
    assert provider._allow_file_reads is True
    assert provider._allow_file_writes is False


def test_file_read_respects_requested_line_range(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = KiroProvider()._fs_read({"path": str(target), "line": 2, "limit": 1}, str(tmp_path))
    assert result == {"content": "two\n"}
