"""ACP driver behavior shared by every ACP provider."""
import json

import pytest

from agentcli.providers._acp import AcpConnection
from agentcli.providers.kiro import KiroProvider


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
