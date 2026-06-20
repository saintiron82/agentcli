import asyncio
import os
import pytest
from unittest.mock import patch
from agentcli.providers.kiro import KiroProvider
from agentcli.types import TokenUsage
from agentcli.providers.kiro import _map_session_update
from agentcli.providers._acp import AcpConnection
from tests._acp_helpers import ScriptedAgent


def test_provider_id_and_capabilities():
    p = KiroProvider()
    assert p.provider_id == "kiro"
    assert p.supports_sessions is True
    assert p.supports_streaming is True
    assert p.stores_history is False


def test_list_models_has_default_passthrough():
    models = KiroProvider().list_models()
    assert any(m["id"] == "" for m in models)  # 빈 id = 기본
    # resolve_model 은 알 수 없는 selector 를 그대로 통과 (비-strict).
    assert KiroProvider().resolve_model("kiro-some-model") == "kiro-some-model"


@patch("agentcli.providers.kiro.shutil.which", return_value=None)
def test_health_check_binary_missing(mock_which):
    h = KiroProvider().health_check()
    assert h.ok is False
    assert h.status == "binary_missing"
    assert h.error_type == "binary_missing"


def test_map_agent_message_chunk_to_text():
    u = TokenUsage()
    chunks = _map_session_update(
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "Hello"}}, u)
    assert len(chunks) == 1
    assert chunks[0].type == "text" and chunks[0].content == "Hello"


def test_map_thought_and_tool_variants():
    u = TokenUsage()
    assert _map_session_update(
        {"sessionUpdate": "agent_thought_chunk",
         "content": {"type": "text", "text": "thinking..."}}, u)[0].type == "thinking"
    assert _map_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "t1",
         "title": "read"}, u)[0].type == "tool_use"
    assert _map_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
         "status": "completed"}, u)[0].type == "tool_result"


def test_map_usage_update_accumulates_and_emits_nothing():
    u = TokenUsage()
    out = _map_session_update(
        {"sessionUpdate": "usage_update", "used": 1500, "size": 200000}, u)
    assert out == []
    assert u.prompt_tokens == 1500
    assert u.prompt_tokens_source == "kiro_cli_reported"
    assert u.prompt_tokens_reliable is False


@pytest.mark.asyncio
async def test_acp_turn_new_session_streams_text_and_usage():
    p = KiroProvider()
    updates = [
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "Hel"}},
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "lo"}},
        {"sessionUpdate": "usage_update", "used": 1200, "size": 200000},
    ]

    def conn_factory(on_req, on_notif):
        conn = AcpConnection(_placeholder_write, on_request=on_req,
                             on_notification=on_notif)
        agent = ScriptedAgent(conn, updates=updates)
        # Replace the connection's write_line with the agent's write_line
        conn._write_line = agent.write_line
        return conn

    # Placeholder; replaced above before any I/O occurs
    async def _placeholder_write(line: str) -> None:  # pragma: no cover
        pass

    chunks = []
    async for ch in p._acp_turn(
            prompt="hi", model="", session_id="", cwd=None,
            timeout=10, idle_timeout=None, wall_timeout=None,
            conn_factory=conn_factory):
        chunks.append(ch)

    types = [c.type for c in chunks]
    assert "text" in types and types[-1] == "done"
    text = "".join(c.content for c in chunks if c.type == "text")
    assert text == "Hello"
    done = chunks[-1]
    assert done.session_id == "kiro-sess-1"
    assert done.usage.prompt_tokens == 1200


@pytest.mark.asyncio
async def test_resume_calls_session_load_with_stored_id():
    p = KiroProvider()
    holder = {}

    async def write_line(line):
        await holder["agent"].write_line(line)

    def factory(on_req, on_notif):
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        holder["agent"] = ScriptedAgent(conn, updates=[
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "ok"}}], load_ok=True)
        holder["conn"] = conn
        return conn

    chunks = [c async for c in p._acp_turn(
        prompt="again", model="", session_id="prev-sid", cwd=None,
        timeout=10, idle_timeout=None, wall_timeout=None, conn_factory=factory)]
    assert chunks[-1].type == "done"
    assert chunks[-1].session_id == "prev-sid"  # load success keeps the stored id


@pytest.mark.asyncio
async def test_permission_trust_all_selects_allow_option():
    p = KiroProvider(trust_all=True)
    res = await p._handle_agent_request("session/request_permission", {
        "options": [
            {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
            {"optionId": "reject", "name": "Reject", "kind": "reject_once"}],
        "toolCall": {"title": "read"}}, cwd=None)
    assert res["outcome"]["outcome"] == "selected"
    assert res["outcome"]["optionId"] == "allow"


@pytest.mark.asyncio
async def test_permission_denied_when_not_trusted():
    p = KiroProvider(trust_all=False, trust_tools=["grep"])
    res = await p._handle_agent_request("session/request_permission", {
        "options": [{"optionId": "allow", "kind": "allow_once"},
                    {"optionId": "reject", "kind": "reject_once"}],
        "toolCall": {"title": "bash"}}, cwd=None)
    assert res["outcome"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_fs_read_within_cwd(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("data", encoding="utf-8")
    p = KiroProvider()
    res = await p._handle_agent_request(
        "fs/read_text_file", {"path": str(f)}, cwd=str(tmp_path))
    assert res["content"] == "data"


@pytest.mark.asyncio
async def test_fs_read_outside_cwd_denied(tmp_path):
    p = KiroProvider()
    res = await p._handle_agent_request(
        "fs/read_text_file", {"path": "/etc/hosts"}, cwd=str(tmp_path))
    assert res.get("content", "") == ""


@pytest.mark.asyncio
async def test_fs_read_dotdot_traversal_denied(tmp_path):
    p = KiroProvider()
    escape = str(tmp_path) + "/../../../etc/hosts"
    res = await p._handle_agent_request(
        "fs/read_text_file", {"path": escape}, cwd=str(tmp_path))
    assert res.get("content", "") == ""


@pytest.mark.asyncio
async def test_fs_read_symlink_escape_denied(tmp_path):
    target_outside = tmp_path.parent / "outside.txt"
    target_outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    os.symlink(target_outside, link)
    p = KiroProvider()
    res = await p._handle_agent_request(
        "fs/read_text_file", {"path": str(link)}, cwd=str(tmp_path))
    assert res.get("content", "") == ""


@pytest.mark.asyncio
async def test_stale_session_falls_back_to_new_once():
    p = KiroProvider()
    holder = {}

    async def write_line(line):
        await holder["agent"].write_line(line)

    def factory(on_req, on_notif):
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        holder["agent"] = ScriptedAgent(conn, updates=[
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "fresh"}}],
            load_ok=False, new_session_id="kiro-new")
        return conn

    chunks = [c async for c in p._acp_turn(
        prompt="again", model="", session_id="expired", cwd=None,
        timeout=10, idle_timeout=None, wall_timeout=None, conn_factory=factory)]
    assert chunks[-1].type == "done"
    assert chunks[-1].session_id == "kiro-new"  # recovered via session/new
