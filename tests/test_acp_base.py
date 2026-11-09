"""ACP driver behavior shared by every ACP provider."""
import json
import logging

import pytest

from agentcli.providers._acp import AcpConnection
from agentcli.providers.acp_base import AcpProvider
from agentcli.providers.kiro import KiroProvider
from agentcli.reasoning import LevelResolution, ReasoningResolution
from agentcli.types import Message


class MinimalAcp(AcpProvider):
    provider_id = "minimal"
    binary_name = "minimal-acp"
    usage_source = "minimal_reported"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.commands = []

    def _command(self, binary, model, reasoning):
        self.commands.append((binary, model, reasoning))
        return [binary, "acp"]

    def list_models(self):
        return []


def _clamped():
    return ReasoningResolution(effort=LevelResolution("medium", "high", clamped=True, supported=True))


class ReplayingAgent:
    """ACP agent that replays the stored conversation on session/load, as the
    ACP spec requires, before answering the new prompt."""

    def __init__(self, conn):
        self._conn = conn

    async def _send(self, obj):
        await self._conn.handle_line(json.dumps(obj))

    async def _update(self, sid, kind, text):
        await self._send({"jsonrpc": "2.0", "method": "session/update",
                          "params": {"sessionId": sid, "update": {
                              "sessionUpdate": kind,
                              "content": {"type": "text", "text": text}}}})

    async def write_line(self, line):
        msg = json.loads(line)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            await self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                "authMethods": []}})
        elif method == "session/load":
            sid = params["sessionId"]
            await self._update(sid, "user_message_chunk", "old question")
            await self._update(sid, "agent_thought_chunk", "old thought")
            await self._update(sid, "agent_message_chunk", "OLD ANSWER")
            await self._send({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "session/prompt":
            sid = params["sessionId"]
            await self._update(sid, "agent_thought_chunk", "new thought")
            await self._update(sid, "agent_message_chunk", "NEW")
            await self._send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def replaying_factory(on_request, on_notification):
    holder = {}

    async def write_line(line):
        await holder["agent"].write_line(line)

    conn = AcpConnection(write_line, on_request=on_request, on_notification=on_notification)
    holder["agent"] = ReplayingAgent(conn)
    return conn


@pytest.mark.asyncio
async def test_resume_ignores_history_replayed_by_session_load():
    chunks = [c async for c in KiroProvider()._acp_turn(
        prompt="next", model="", session_id="prev-sid", cwd=None, timeout=10,
        idle_timeout=None, wall_timeout=None, conn_factory=replaying_factory)]
    assert [(c.type, c.content) for c in chunks if c.type in ("text", "thinking", "event")] == [
        ("thinking", "new thought"), ("text", "NEW")]
    assert chunks[-1].type == "done"
    assert chunks[-1].content == "NEW"
    assert chunks[-1].session_id == "prev-sid"


class ScriptedAgent:
    """ACP agent whose session/load and session/new behavior is configurable."""

    def __init__(self, conn, *, load_fails=False, new_updates=()):
        self._conn = conn
        self._load_fails = load_fails
        self._new_updates = new_updates

    async def _send(self, obj):
        await self._conn.handle_line(json.dumps(obj))

    async def _update(self, sid, update):
        await self._send({"jsonrpc": "2.0", "method": "session/update",
                          "params": {"sessionId": sid, "update": update}})

    async def _text(self, sid, kind, text):
        await self._update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

    async def write_line(self, line):
        msg = json.loads(line)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            await self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
        elif method == "session/load":
            sid = params["sessionId"]
            await self._text(sid, "user_message_chunk", "old question")
            await self._text(sid, "agent_message_chunk", "OLD ANSWER")
            await self._update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1"})
            await self._update(sid, {"sessionUpdate": "usage_update", "used": 999, "size": 1000})
            await self._update(sid, {"sessionUpdate": "plan", "entries": [
                {"content": "old step", "priority": "medium", "status": "completed"}]})
            if self._load_fails:
                await self._send({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32002, "message": "session not found"}})
            else:
                await self._send({"jsonrpc": "2.0", "id": rid, "result": None})
        elif method == "session/new":
            for update in self._new_updates:
                await self._update("new-sid", update)
            await self._send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "new-sid"}})
        elif method == "session/prompt":
            sid = params["sessionId"]
            await self._text(sid, "agent_message_chunk", "NEW")
            await self._send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


def scripted_factory(**agent_kwargs):
    def factory(on_request, on_notification):
        holder = {}

        async def write_line(line):
            await holder["agent"].write_line(line)

        conn = AcpConnection(write_line, on_request=on_request, on_notification=on_notification)
        holder["agent"] = ScriptedAgent(conn, **agent_kwargs)
        return conn
    return factory


async def _turn(provider, session_id, factory):
    return [c async for c in provider._acp_turn(
        prompt="next", model="", session_id=session_id, cwd=None, timeout=10,
        idle_timeout=None, wall_timeout=None, conn_factory=factory)]


@pytest.mark.asyncio
async def test_non_content_update_before_prompt_is_still_forwarded():
    update = {"sessionUpdate": "available_commands_update", "availableCommands": []}
    chunks = await _turn(KiroProvider(), "", scripted_factory(new_updates=[update]))
    assert [(c.type, c.data) for c in chunks if c.type == "event"] == [("event", update)]
    assert chunks[-1].type == "done" and chunks[-1].content == "NEW"


@pytest.mark.asyncio
async def test_stale_load_fallback_leaks_no_replay():
    chunks = await _turn(KiroProvider(), "stale-sid", scripted_factory(load_fails=True))
    assert [(c.type, c.content) for c in chunks if c.type != "done"] == [("text", "NEW")]
    done = chunks[-1]
    assert done.type == "done" and done.content == "NEW"
    assert done.session_id == "new-sid"
    assert done.usage.prompt_tokens == 0  # replayed usage_update was not folded


@pytest.mark.asyncio
async def test_plan_replayed_during_load_is_dropped():
    chunks = await _turn(KiroProvider(), "prev-sid", scripted_factory())
    assert [(c.type, c.content) for c in chunks if c.type != "done"] == [("text", "NEW")]
    assert not any(c.type == "event" and c.data.get("sessionUpdate") == "plan" for c in chunks)
    assert chunks[-1].session_id == "prev-sid"


@pytest.mark.asyncio
async def test_stream_emits_reasoning_event_first():
    provider = MinimalAcp()
    provider._find_binary = lambda: None
    chunks = [c async for c in provider._stream(
        [Message(role="user", content="hi")], model="", timeout=10, session_id="", cwd=None,
        idle_timeout=None, wall_timeout=None, reasoning=_clamped())]
    assert chunks[0].type == "event"
    assert chunks[0].data["provider"] == "minimal"
    assert chunks[0].data["reasoning"]["effort"] == {
        "requested": "medium", "applied": "high", "clamped": True, "supported": True}
    assert [c.type for c in chunks[1:]] == ["error"]


@pytest.mark.asyncio
async def test_stream_without_reasoning_emits_no_event():
    provider = MinimalAcp()
    provider._find_binary = lambda: None
    chunks = [c async for c in provider._stream(
        [Message(role="user", content="hi")], model="", timeout=10, session_id="", cwd=None,
        idle_timeout=None, wall_timeout=None, reasoning=None)]
    assert [c.type for c in chunks] == ["error"]


def test_command_hook_receives_binary_model_and_reasoning():
    provider = MinimalAcp()
    provider._find_binary = lambda: "/fake/minimal-acp"
    reasoning = _clamped()
    factory, state = provider._subprocess_conn_factory("m1", None, reasoning)
    factory(None, None)
    assert provider.commands == [("/fake/minimal-acp", "m1", reasoning)]
    assert "proc" not in state["proc_box"]  # nothing spawned before the first write


def test_command_hook_is_abstract():
    class NoCommand(AcpProvider):
        provider_id = "nocommand"

        def list_models(self):
            return []

    with pytest.raises(TypeError):
        NoCommand()


def test_fs_deny_warning_uses_provider_logger(caplog, tmp_path):
    with caplog.at_level(logging.WARNING):
        assert KiroProvider()._fs_read({"path": "../outside.txt"}, str(tmp_path)) == {"content": ""}
    assert [r.name for r in caplog.records if "fs/read" in r.getMessage()] == ["agentcli.providers.kiro"]
