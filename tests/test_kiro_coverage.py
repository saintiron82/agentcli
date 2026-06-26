"""Coverage-focused behavior tests for agentcli/providers/kiro.py.

Targets the previously-uncovered branches of the Kiro ACP flow:
  - sync invoke() entrypoint
  - the real _subprocess_conn_factory path (ensure_proc/reader/write_line)
    driven by a fake `kiro-cli acp` script speaking JSON-RPC over stdio,
    plus the stream_async finally-block cleanup (reader cancel + proc kill)
  - is_available / _find_binary / health_check success path
  - on_request wrapper inside a live turn (permission + fs round-trips)
  - drive() generic-Exception path and the (_DONE, Exception) error chunk
  - wall-timeout where remaining <= 0 at the top of the loop
  - _handle_agent_request unknown method, _within_cwd(cwd=None)
  - _fs_read / _fs_write OSError paths
  - _map_session_update unknown-kind -> event

All ACP mocking follows tests/_acp_helpers.py conventions (ScriptedAgent /
AcpConnection). The real-subprocess tests use an on-disk fake binary so the
production _subprocess_conn_factory closures actually execute.
"""
import asyncio
import json
import os
import stat
import sys
import textwrap
from unittest.mock import patch

import pytest

from agentcli.providers.kiro import KiroProvider, _map_session_update
from agentcli.providers._acp import AcpConnection, AcpError
from agentcli.types import TokenUsage, StreamChunk as SC, Message
from tests._acp_helpers import ScriptedAgent


# ---------------------------------------------------------------------------
# A fake `kiro-cli acp` binary: a tiny JSON-RPC-over-stdio agent.
# This exercises the *real* _subprocess_conn_factory closures (ensure_proc,
# reader task, write_line) and the stream_async finally-block cleanup.
# ---------------------------------------------------------------------------

_FAKE_ACP_SCRIPT = textwrap.dedent(
    '''
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"protocolVersion": 1,
                             "agentCapabilities": {"loadSession": True},
                             "authMethods": []}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"sessionId": "fake-sess-1"}})
        elif method == "session/prompt":
            sid = params.get("sessionId", "fake-sess-1")
            # Stream two text chunks then a usage update.
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid, "update": {
                      "sessionUpdate": "agent_message_chunk",
                      "content": {"type": "text", "text": "sub"}}}})
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid, "update": {
                      "sessionUpdate": "agent_message_chunk",
                      "content": {"type": "text", "text": "proc"}}}})
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid, "update": {
                      "sessionUpdate": "usage_update", "used": 42}}})
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"stopReason": "end_turn"}})
        # ignore anything else
    '''
)


@pytest.fixture
def fake_kiro_bin(tmp_path):
    """Write an executable fake kiro-cli that ignores the `acp` argv and
    speaks the minimal ACP handshake on stdio. Returns its path."""
    script = tmp_path / "fake_acp.py"
    script.write_text(_FAKE_ACP_SCRIPT, encoding="utf-8")
    launcher = tmp_path / "kiro-cli"
    launcher.write_text(
        "#!/bin/sh\nexec {py} {script} \"$@\"\n".format(
            py=sys.executable, script=str(script)),
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(launcher)


# ---------------------------------------------------------------------------
# 1. Real subprocess path: _subprocess_conn_factory closures + stream_async
#    cleanup (lines 136-142, 153-190).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_async_drives_real_subprocess(fake_kiro_bin):
    """Drive a real kiro-cli acp subprocess end-to-end: this exercises
    ensure_proc, the reader task, and write_line, then the finally-block
    cancels the reader and reaps the process."""
    p = KiroProvider()
    with patch.object(KiroProvider, "_find_binary", return_value=fake_kiro_bin):
        chunks = [c async for c in p.stream_async(
            [Message(role="user", content="hi")], timeout=10)]
    types = [c.type for c in chunks]
    assert types[-1] == "done"
    text = "".join(c.content for c in chunks if c.type == "text")
    assert text == "subproc"
    done = chunks[-1]
    assert done.session_id == "fake-sess-1"
    assert done.usage.prompt_tokens == 42


@pytest.mark.asyncio
async def test_stream_async_subprocess_with_agent_flag(fake_kiro_bin):
    """When agent is set, cmd includes --agent; subprocess still completes,
    proving the agent-flag branch of the factory is wired."""
    p = KiroProvider(agent="my-agent")
    with patch.object(KiroProvider, "_find_binary", return_value=fake_kiro_bin):
        chunks = [c async for c in p.stream_async(
            [Message(role="user", content="hi")], timeout=10)]
    assert chunks[-1].type == "done"
    assert "".join(c.content for c in chunks if c.type == "text") == "subproc"


@pytest.mark.asyncio
async def test_subprocess_reader_breaks_on_clean_eof(tmp_path):
    """The reader task's `if not line: break` (EOF) must execute when the
    subprocess closes stdout cleanly. We use a fake binary that answers
    `initialize` and then exits, closing its stdout, and await the reader
    task directly to confirm it terminates on EOF rather than being cancelled."""
    script = tmp_path / "eof_acp.py"
    script.write_text(textwrap.dedent('''
        import json, sys
        line = sys.stdin.readline()
        msg = json.loads(line)
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg.get("id"),
            "result": {"protocolVersion": 1, "agentCapabilities": {},
                       "authMethods": []}}) + "\\n")
        sys.stdout.flush()
        # Exit immediately -> stdout closes -> reader sees EOF.
    '''), encoding="utf-8")
    launcher = tmp_path / "kiro-cli"
    launcher.write_text(
        "#!/bin/sh\nexec {py} {s} \"$@\"\n".format(py=sys.executable, s=str(script)),
        encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    p = KiroProvider()
    with patch.object(KiroProvider, "_find_binary", return_value=str(launcher)):
        factory, state = p._subprocess_conn_factory("", str(tmp_path))
        conn = factory(lambda m, p: _noop_request(m, p), lambda m, p: _noop_notify(m, p))
        # Issue initialize: spawns the proc + reader, gets a response, then the
        # child exits and the reader hits EOF.
        result = await conn.request("initialize", {"protocolVersion": 1})
        assert result.get("protocolVersion") == 1
        reader = state["proc_box"]["reader"]
        # Await the reader; it must finish on its own via the EOF break.
        await asyncio.wait_for(reader, timeout=2.0)
        assert reader.done() and reader.exception() is None
        proc = state["proc_box"]["proc"]
        await asyncio.wait_for(proc.wait(), timeout=2.0)


async def _noop_request(method, params):  # pragma: no cover - not exercised
    return {}


async def _noop_notify(method, params):  # pragma: no cover - not exercised
    return None


def test_invoke_sync_runs_event_loop(fake_kiro_bin):
    """invoke() (sync, line 70) wraps invoke_async via asyncio.run."""
    p = KiroProvider()
    with patch.object(KiroProvider, "_find_binary", return_value=fake_kiro_bin):
        resp = p.invoke([Message(role="user", content="hi")], timeout=10)
    assert resp.content == "subproc"
    assert resp.provider == "kiro"
    assert resp.session_id == "fake-sess-1"
    assert resp.tokens.prompt_tokens == 42


@pytest.mark.asyncio
async def test_stream_async_cleanup_kills_live_process(tmp_path):
    """A subprocess that streams forever and never finishes the turn forces
    the stream_async finally block to kill the still-running process and
    cancel the reader (lines 136-142)."""
    # Fake agent that answers the handshake but then loops emitting updates
    # forever, so the consumer must tear it down via idle timeout.
    script = tmp_path / "loop_acp.py"
    script.write_text(textwrap.dedent('''
        import json, sys
        def send(obj):
            sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
        while True:
            raw = sys.stdin.readline()
            if not raw:
                break
            raw = raw.strip()
            if not raw:
                continue
            msg = json.loads(raw); method = msg.get("method"); rid = msg.get("id")
            if method == "initialize":
                send({"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":1,
                      "agentCapabilities":{},"authMethods":[]}})
            elif method == "session/new":
                send({"jsonrpc":"2.0","id":rid,"result":{"sessionId":"loop-1"}})
            elif method == "session/prompt":
                # stream one chunk, then never send the prompt result
                send({"jsonrpc":"2.0","method":"session/update","params":{
                      "sessionId":"loop-1","update":{
                      "sessionUpdate":"agent_message_chunk",
                      "content":{"type":"text","text":"x"}}}})
                # block on stdin forever (no further output)
    '''), encoding="utf-8")
    launcher = tmp_path / "kiro-cli"
    launcher.write_text(
        "#!/bin/sh\nexec {py} {s} \"$@\"\n".format(py=sys.executable, s=str(script)),
        encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    p = KiroProvider()
    with patch.object(KiroProvider, "_find_binary", return_value=str(launcher)):
        # idle_timeout 을 넉넉히 — 부하 시 python 서브프로세스 startup+handshake
        # 가 좁은 창을 넘겨 첫 text 청크 전에 idle timeout 이 터지는 flake 방지.
        chunks = [c async for c in p.stream_async(
            [Message(role="user", content="hi")],
            timeout=6, idle_timeout=6.0)]
    # We got the streamed text then an idle-timeout error: the turn never
    # finished, so the finally block had to kill the live process + reader.
    assert any(c.type == "text" for c in chunks)
    assert any(c.type == "error" and c.data.get("timeout_kind") == "idle"
               for c in chunks)


# ---------------------------------------------------------------------------
# 2. is_available / _find_binary / health_check success (195, 198, 213-215).
# ---------------------------------------------------------------------------

def test_is_available_true_when_binary_present():
    with patch("agentcli.providers.kiro.shutil.which", return_value="/usr/bin/kiro-cli"):
        assert KiroProvider().is_available() is True


def test_is_available_false_when_missing():
    with patch("agentcli.providers.kiro.shutil.which", return_value=None):
        assert KiroProvider().is_available() is False


def test_find_binary_returns_which_result():
    with patch("agentcli.providers.kiro.shutil.which", return_value="/opt/kiro-cli"):
        assert KiroProvider()._find_binary() == "/opt/kiro-cli"


def test_health_check_ok_when_binary_present():
    """health_check success path (213-215): binary present, version probed."""
    class _Proc:
        stdout = "kiro-cli 9.9.9\n"
        stderr = ""
    with patch("agentcli.providers.kiro.shutil.which", return_value="/usr/bin/kiro-cli"), \
         patch("agentcli.providers.kiro.run_health_command", return_value=_Proc()):
        h = KiroProvider().health_check()
    assert h.ok is True
    assert h.status == "ok"
    assert h.available is True
    assert h.binary == "/usr/bin/kiro-cli"
    assert h.version == "kiro-cli 9.9.9"
    assert h.auth_ok is None


def test_health_check_version_falls_back_to_stderr():
    class _Proc:
        stdout = ""
        stderr = "kiro-cli 1.2.3 (stderr)\n"
    with patch("agentcli.providers.kiro.shutil.which", return_value="/usr/bin/kiro-cli"), \
         patch("agentcli.providers.kiro.run_health_command", return_value=_Proc()):
        h = KiroProvider().health_check()
    assert h.version == "kiro-cli 1.2.3 (stderr)"


# ---------------------------------------------------------------------------
# 3. on_request wrapper inside a live turn (line 241): agent issues a
#    permission request + an fs/read during the turn, handled via the
#    _handle_agent_request callback wired in _acp_turn.
# ---------------------------------------------------------------------------

class RequestingAgent:
    """Fake agent that, during session/prompt, calls back to the client with
    a permission request and an fs/read before streaming text and finishing.
    Exercises the on_request wrapper (line 241) inside _acp_turn."""

    def __init__(self, conn, *, read_path):
        self._conn = conn
        self._read_path = read_path
        self.permission_reply = None
        self.read_reply = None

    async def write_line(self, line):
        msg = json.loads(line)
        method, rid = msg.get("method"), msg.get("id")
        if method is not None and rid is None:
            # This is a *response* from the client to one of our requests.
            if "result" in msg and self.permission_reply is None:
                pass
            return
        if rid is not None and method is None:
            # Client responded to a server-initiated request.
            if self.permission_reply is None:
                self.permission_reply = msg.get("result")
            else:
                self.read_reply = msg.get("result")
            return
        if method == "initialize":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": 1,
                           "agentCapabilities": {}, "authMethods": []}}))
        elif method == "session/new":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid, "result": {"sessionId": "req-sess"}}))
        elif method == "session/prompt":
            # 1) ask for permission
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": 9001,
                "method": "session/request_permission",
                "params": {"options": [
                    {"optionId": "allow", "kind": "allow_once"},
                    {"optionId": "reject", "kind": "reject_once"}],
                    "toolCall": {"title": "read"}}}))
            # 2) ask to read a file
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": 9002,
                "method": "fs/read_text_file",
                "params": {"path": self._read_path}}))
            # 3) stream + finish
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "method": "session/update",
                "params": {"sessionId": "req-sess", "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "done"}}}}))
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}}))


@pytest.mark.asyncio
async def test_acp_turn_handles_agent_requests_during_turn(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("file-body", encoding="utf-8")
    p = KiroProvider(trust_all=True)
    holder = {}

    async def write_line(line):
        await holder["agent"].write_line(line)

    def factory(on_req, on_notif):
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        holder["agent"] = RequestingAgent(conn, read_path=str(f))
        return conn

    chunks = [c async for c in p._acp_turn(
        prompt="hi", model="", session_id="", cwd=str(tmp_path),
        timeout=10, idle_timeout=None, wall_timeout=None, conn_factory=factory)]

    assert chunks[-1].type == "done"
    assert "".join(c.content for c in chunks if c.type == "text") == "done"
    agent = holder["agent"]
    # The client auto-approved the permission and served the file content.
    assert agent.permission_reply["outcome"]["outcome"] == "selected"
    assert agent.permission_reply["outcome"]["optionId"] == "allow"
    assert agent.read_reply["content"] == "file-body"


# ---------------------------------------------------------------------------
# 4. drive() generic Exception path (line 275) + (_DONE, Exception) error
#    chunk (323-325). A non-AcpError raised inside drive() must surface as an
#    error chunk classified from the message.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_acp_turn_generic_exception_yields_error_chunk():
    """If conn.request raises a plain Exception (not AcpError), drive()
    catches it (275) and the loop yields an error chunk (323-325)."""
    p = KiroProvider()

    class _BoomConn:
        def __init__(self):
            pass

        async def request(self, method, params):
            raise RuntimeError("transport exploded")

    def factory(on_req, on_notif):
        return _BoomConn()

    chunks = [c async for c in p._acp_turn(
        prompt="hi", model="", session_id="", cwd=None,
        timeout=10, idle_timeout=None, wall_timeout=None, conn_factory=factory)]

    assert len(chunks) == 1
    assert chunks[0].type == "error"
    assert chunks[0].content == "transport exploded"
    assert "error_type" in chunks[0].data


# ---------------------------------------------------------------------------
# 5. wall-timeout where remaining <= 0 at the TOP of the loop (288-292).
#    Distinct from the existing test which trips the wait_for path. Here the
#    queue already has an item but the wall deadline is already in the past
#    when the loop head is reached on a subsequent iteration.
# ---------------------------------------------------------------------------

class OneChunkThenSilentAgent:
    """Answers the handshake, streams exactly one text chunk during
    session/prompt, and then never sends the prompt result. This keeps the
    turn alive so the consumer loop iterates again after the first chunk."""

    def __init__(self, conn):
        self._conn = conn

    async def write_line(self, line):
        msg = json.loads(line)
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": 1,
                           "agentCapabilities": {}, "authMethods": []}}))
        elif method == "session/new":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid, "result": {"sessionId": "wall-sess"}}))
        elif method == "session/prompt":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "method": "session/update",
                "params": {"sessionId": "wall-sess", "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "one"}}}}))
            # Deliberately no prompt result -> turn stays open.


@pytest.mark.asyncio
async def test_acp_turn_wall_deadline_already_passed_at_loop_top():
    """Stream one chunk, then have the wall deadline already be in the past so
    the NEXT loop iteration takes the top-of-loop `remaining <= 0` branch
    (288-292) rather than the wait_for-timeout branch. We flip the clock past
    the deadline only after the first text chunk has been yielded."""
    p = KiroProvider()
    holder = {}

    async def write_line(line):
        await holder["agent"].write_line(line)

    def factory(on_req, on_notif):
        conn = AcpConnection(write_line, on_request=on_req, on_notification=on_notif)
        holder["agent"] = OneChunkThenSilentAgent(conn)
        return conn

    real_time = __import__("time").time
    base = real_time()
    flip = {"jumped": False}

    def fake_time():
        return base + 1000.0 if flip["jumped"] else base

    async def gen():
        agen = p._acp_turn(
            prompt="hi", model="", session_id="", cwd=None,
            timeout=10, idle_timeout=10, wall_timeout=5.0, conn_factory=factory)
        out = []
        async for ch in agen:
            out.append(ch)
            if ch.type == "text":
                # After the first text chunk is yielded, advance the clock past
                # the wall deadline so the next loop-top check trips 288-292.
                flip["jumped"] = True
        return out

    with patch("agentcli.providers.kiro.time.time", side_effect=fake_time):
        chunks = await gen()

    assert any(c.type == "text" for c in chunks)
    err = [c for c in chunks if c.type == "error"]
    assert err, f"expected wall-timeout error, got {[c.type for c in chunks]}"
    assert err[-1].content == "wall timeout expired"
    assert err[-1].data.get("timeout_kind") == "wall"


# ---------------------------------------------------------------------------
# 6. _handle_agent_request unknown method (line 350).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_agent_request_unknown_method_returns_empty():
    p = KiroProvider()
    res = await p._handle_agent_request("terminal/create", {"foo": "bar"}, cwd=None)
    assert res == {}


# ---------------------------------------------------------------------------
# 7. _within_cwd with cwd=None (line 373) via fs/read.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_read_with_no_cwd_denied():
    """cwd=None makes _within_cwd return None immediately (373) -> empty read."""
    p = KiroProvider()
    res = await p._handle_agent_request(
        "fs/read_text_file", {"path": "/etc/hosts"}, cwd=None)
    assert res.get("content", "") == ""


@pytest.mark.asyncio
async def test_fs_write_with_no_cwd_denied():
    p = KiroProvider()
    res = await p._handle_agent_request(
        "fs/write_text_file", {"path": "/tmp/whatever.txt", "content": "x"}, cwd=None)
    assert res == {}


# ---------------------------------------------------------------------------
# 8. _fs_read OSError path (389-390) and _fs_write OSError path (400-401).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fs_read_oserror_returns_empty_content(tmp_path):
    """A readable, in-cwd file whose read_text raises OSError -> empty content."""
    f = tmp_path / "a.txt"
    f.write_text("data", encoding="utf-8")
    p = KiroProvider()
    with patch("pathlib.Path.read_text", side_effect=OSError("boom")):
        res = await p._handle_agent_request(
            "fs/read_text_file", {"path": str(f)}, cwd=str(tmp_path))
    assert res == {"content": ""}


@pytest.mark.asyncio
async def test_fs_write_oserror_swallowed(tmp_path):
    """write_text raising OSError is swallowed; result is still {} (400-401)."""
    p = KiroProvider()
    out = str(tmp_path / "sub" / "out.txt")
    with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
        res = await p._handle_agent_request(
            "fs/write_text_file", {"path": out, "content": "hi"},
            cwd=str(tmp_path))
    assert res == {}


# ---------------------------------------------------------------------------
# 9. _map_session_update unknown kind -> event chunk (line 428).
# ---------------------------------------------------------------------------

def test_map_unknown_kind_falls_back_to_event():
    u = TokenUsage()
    out = _map_session_update({"sessionUpdate": "mystery_kind", "x": 1}, u)
    assert len(out) == 1
    assert out[0].type == "event"
    assert out[0].data == {"sessionUpdate": "mystery_kind", "x": 1}


def test_map_empty_text_chunks_emit_nothing():
    """agent_message_chunk / agent_thought_chunk with empty text emit nothing."""
    u = TokenUsage()
    assert _map_session_update(
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": ""}}, u) == []
    assert _map_session_update(
        {"sessionUpdate": "agent_thought_chunk",
         "content": {"type": "text", "text": ""}}, u) == []
