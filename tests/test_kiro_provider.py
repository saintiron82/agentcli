import asyncio
import os
import pytest
from unittest.mock import patch
from agentcli.providers.kiro import KiroProvider
from agentcli.types import TokenUsage, StreamChunk as SC, Message
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


# ---------------------------------------------------------------------------
# Task 8: stream_async / invoke_async / invoke (public surface)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_async_yields_text_then_done():
    p = KiroProvider()
    async def fake_turn(self_or_ignored=None, **kwargs):
        yield SC(type="text", content="Hi")
        yield SC(type="done", content="", session_id="s9",
                 usage=TokenUsage(prompt_tokens=5), data={"latency_ms": 1})
    with patch.object(KiroProvider, "_acp_turn", new=fake_turn), \
         patch.object(KiroProvider, "_find_binary", return_value="/usr/bin/kiro-cli"):
        out = [c async for c in p.stream_async([Message(role="user", content="hi")])]
    assert [c.type for c in out] == ["text", "done"]
    assert out[-1].session_id == "s9"


@pytest.mark.asyncio
async def test_stream_async_binary_missing():
    p = KiroProvider()
    with patch.object(KiroProvider, "_find_binary", return_value=None):
        out = [c async for c in p.stream_async([Message(role="user", content="hi")])]
    assert out[0].type == "error"
    assert out[0].data.get("error_type") == "binary_missing"


@pytest.mark.asyncio
async def test_invoke_async_folds_chunks_into_response():
    p = KiroProvider()
    async def fake_turn(self_or_ignored=None, **kwargs):
        yield SC(type="text", content="A")
        yield SC(type="text", content="B")
        yield SC(type="done", content="", session_id="s1",
                 usage=TokenUsage(prompt_tokens=7), data={"latency_ms": 2})
    with patch.object(KiroProvider, "_acp_turn", new=fake_turn), \
         patch.object(KiroProvider, "_find_binary", return_value="/usr/bin/kiro-cli"):
        resp = await p.invoke_async([Message(role="user", content="hi")])
    assert resp.content == "AB"
    assert resp.session_id == "s1"
    assert resp.tokens.prompt_tokens == 7
    assert resp.provider == "kiro"


# ---------------------------------------------------------------------------
# Task 8 carry-forward B: wall_timeout in _acp_turn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acp_turn_wall_timeout_yields_error():
    """A wall_timeout of nearly-zero expires before a scripted agent responds."""
    p = KiroProvider()
    barrier: asyncio.Event = asyncio.Event()

    async def write_line(line: str) -> None:
        # Never reply — simulates an unresponsive agent.
        pass

    def factory(on_req, on_notif):
        from agentcli.providers._acp import AcpConnection
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        return conn

    chunks = [c async for c in p._acp_turn(
        prompt="ping", model="", session_id="", cwd=None,
        timeout=30, idle_timeout=None, wall_timeout=0.05,
        conn_factory=factory)]
    assert any(
        c.type == "error" and c.data.get("timeout_kind") == "wall"
        for c in chunks
    ), f"Expected wall timeout error chunk, got: {chunks}"


# ---------------------------------------------------------------------------
# Finding 1: error_type normalization — classify_error instead of "unknown"
# ---------------------------------------------------------------------------

class UsageLimitAgent:
    """Fake agent that returns a JSON-RPC error on session/prompt
    with a usage-limit message so we can verify error_type classification."""
    def __init__(self, conn):
        self._conn = conn

    async def write_line(self, line: str) -> None:
        import json as _json
        msg = _json.loads(line)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            await self._conn.handle_line(_json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": 1,
                           "agentCapabilities": {}, "authMethods": []}}))
        elif method == "session/new":
            await self._conn.handle_line(_json.dumps({
                "jsonrpc": "2.0", "id": rid, "result": {"sessionId": "ulim-sess-1"}}))
        elif method == "session/prompt":
            # Return a JSON-RPC error whose message contains a usage-limit string.
            await self._conn.handle_line(_json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32000,
                          "message": "You've hit your usage limit"}}))
        # Other methods: ignore (no reply needed for this test path).


@pytest.mark.asyncio
async def test_acp_turn_error_type_classified_as_usage_limit():
    """AcpError whose message contains 'usage limit' must classify as
    usage_limit, not be hardcoded to 'unknown'."""
    p = KiroProvider()
    holder = {}

    async def write_line(line: str) -> None:
        await holder["agent"].write_line(line)

    def factory(on_req, on_notif):
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        holder["agent"] = UsageLimitAgent(conn)
        return conn

    chunks = [c async for c in p._acp_turn(
        prompt="ping", model="", session_id="", cwd=None,
        timeout=10, idle_timeout=None, wall_timeout=None,
        conn_factory=factory)]

    error_chunks = [c for c in chunks if c.type == "error"]
    assert error_chunks, f"Expected at least one error chunk, got: {chunks}"
    err = error_chunks[0]
    from agentcli.types import classify_error, ERROR_USAGE_LIMIT
    # Verify that classify_error agrees with what the chunk carries.
    assert classify_error(err.content) == ERROR_USAGE_LIMIT, (
        f"classify_error({err.content!r}) should be {ERROR_USAGE_LIMIT!r}")
    assert err.data.get("error_type") == ERROR_USAGE_LIMIT, (
        f"error chunk data['error_type'] should be {ERROR_USAGE_LIMIT!r}, "
        f"got {err.data.get('error_type')!r}")


@pytest.mark.asyncio
async def test_invoke_async_error_type_classified_as_usage_limit():
    """invoke_async must propagate a classified error_type (not 'unknown')
    and set recoverable=True for usage_limit errors."""
    p = KiroProvider()

    async def fake_acp_turn(*args, **kwargs):
        # Yield an error chunk with a usage-limit message and no pre-set error_type
        # (simulating the pre-fix state where 'unknown' was hardcoded).
        # After the fix, _acp_turn itself sets error_type via classify_error.
        # Here we test the invoke_async layer: pass an error chunk *without*
        # error_type so __post_init__ classifies from the content.
        from agentcli.types import StreamChunk as _SC
        yield _SC(type="error", content="You've hit your usage limit", data={})

    with patch.object(KiroProvider, "_acp_turn", new=fake_acp_turn), \
         patch.object(KiroProvider, "_find_binary", return_value="/usr/bin/kiro-cli"):
        resp = await p.invoke_async([Message(role="user", content="hi")])

    from agentcli.types import ERROR_USAGE_LIMIT
    assert resp.error_type == ERROR_USAGE_LIMIT, (
        f"Expected error_type={ERROR_USAGE_LIMIT!r}, got {resp.error_type!r}")
    assert resp.recoverable is True, "usage_limit errors must be recoverable"


# ---------------------------------------------------------------------------
# Fix 1: done chunk carries accumulated text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acp_turn_done_chunk_carries_accumulated_text():
    """done chunk content must equal the joined text from all text chunks."""
    p = KiroProvider()
    updates = [
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "Hel"}},
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "lo"}},
    ]

    async def _placeholder_write(line: str) -> None:  # pragma: no cover
        pass

    def conn_factory(on_req, on_notif):
        conn = AcpConnection(_placeholder_write, on_request=on_req,
                             on_notification=on_notif)
        agent = ScriptedAgent(conn, updates=updates)
        conn._write_line = agent.write_line
        return conn

    chunks = []
    async for ch in p._acp_turn(
            prompt="hi", model="", session_id="", cwd=None,
            timeout=10, idle_timeout=None, wall_timeout=None,
            conn_factory=conn_factory):
        chunks.append(ch)

    done = chunks[-1]
    assert done.type == "done"
    assert done.content == "Hello", (
        f"done.content should be 'Hello', got {done.content!r}")


# ---------------------------------------------------------------------------
# Fix 4: _decide_permission must honor trust_all even without "allow"-named option
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permission_trust_all_selects_non_allow_named_option():
    """trust_all=True must select the first non-reject option even if no option
    has kind starting with 'allow' or optionId == 'allow'."""
    p = KiroProvider(trust_all=True)
    res = await p._handle_agent_request("session/request_permission", {
        "options": [
            {"optionId": "proceed", "kind": "approve"},
            {"optionId": "no", "kind": "reject_once"}],
        "toolCall": {"title": "bash"}}, cwd=None)
    assert res["outcome"]["outcome"] == "selected", (
        f"Expected 'selected', got {res['outcome']['outcome']!r}")
    assert res["outcome"]["optionId"] == "proceed", (
        f"Expected optionId 'proceed', got {res['outcome']['optionId']!r}")


# ---------------------------------------------------------------------------
# Fix 5: fs/write tests and idle timeout test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_write_within_cwd(tmp_path):
    """fs/write_text_file within cwd creates the file with correct content."""
    p = KiroProvider()
    out_path = str(tmp_path / "out.txt")
    res = await p._handle_agent_request(
        "fs/write_text_file", {"path": out_path, "content": "hi"},
        cwd=str(tmp_path))
    assert res == {}
    written = tmp_path / "out.txt"
    assert written.exists(), "File should have been created"
    assert written.read_text(encoding="utf-8") == "hi"


@pytest.mark.asyncio
async def test_fs_write_outside_cwd_denied(tmp_path):
    """fs/write_text_file outside cwd must be denied — no file created."""
    p = KiroProvider()
    evil_path = str(tmp_path.parent / "evil.txt")
    await p._handle_agent_request(
        "fs/write_text_file", {"path": evil_path, "content": "bad"},
        cwd=str(tmp_path))
    import pathlib
    assert not pathlib.Path(evil_path).exists(), (
        "File outside cwd must NOT be created")


@pytest.mark.asyncio
async def test_acp_turn_idle_timeout_yields_error():
    """idle_timeout that expires before any reply must yield an error chunk
    with error_type='timeout' and timeout_kind='idle'."""
    p = KiroProvider()

    async def write_line(line: str) -> None:
        # Never replies — simulates a permanently silent agent.
        pass

    def factory(on_req, on_notif):
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        return conn

    chunks = [c async for c in p._acp_turn(
        prompt="ping", model="", session_id="", cwd=None,
        timeout=0.05, idle_timeout=0.05, wall_timeout=None,
        conn_factory=factory)]

    error_chunks = [c for c in chunks if c.type == "error"]
    assert error_chunks, f"Expected at least one error chunk, got: {chunks}"
    err = error_chunks[0]
    assert err.data.get("error_type") == "timeout", (
        f"error_type should be 'timeout', got {err.data.get('error_type')!r}")
    assert err.data.get("timeout_kind") == "idle", (
        f"timeout_kind should be 'idle', got {err.data.get('timeout_kind')!r}")
