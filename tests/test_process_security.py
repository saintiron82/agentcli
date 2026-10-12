import asyncio
import sys
import time
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.base import StreamState, _drain_stderr, run_subprocess_async, run_subprocess_sync
from agentcli.providers._acp import AcpConnection, AcpError


def test_non_debug_stream_drains_flood_without_newlines():
    script = "import sys; sys.stderr.write('x'*2000000); sys.stderr.flush(); print('{}')"
    async def run():
        return [c async for c in ClaudeProvider()._run_stream_template(
            [sys.executable, "-c", script], StreamState(), model="", timeout=4)]
    assert asyncio.run(run())[-1].type == "done"


def test_stderr_retention_is_bounded():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 2000000)
        reader.feed_eof()
        sink = []
        import logging
        await _drain_stderr(reader, sink, logging.getLogger(__name__), "fake", debug=False)
        assert len("".join(sink)) == 65536
    asyncio.run(run())


def test_eof_does_not_allow_unbounded_process_wait():
    script = "import os,time; os.close(1); time.sleep(30)"
    async def run():
        return [c async for c in ClaudeProvider()._run_stream_template(
            [sys.executable, "-c", script], StreamState(), model="", timeout=0.5)]
    start = time.monotonic()
    chunks = asyncio.run(run())
    assert chunks[-1].type == "error"
    assert chunks[-1].data["error_type"] == "timeout"
    assert time.monotonic() - start < 8


def test_owned_descendant_pipes_are_closed_after_timeout():
    script = ("import subprocess,sys,time; "
              "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              "print('ready',flush=True); time.sleep(30)")
    cmd = [sys.executable, "-c", script]
    start = time.monotonic()
    out, _, rc, expired = run_subprocess_sync(cmd, timeout=0.5)
    assert rc == 124 and expired and b"ready" in out
    assert time.monotonic() - start < 8
    async def run():
        return await run_subprocess_async(cmd, timeout=0.5)
    start = time.monotonic()
    out, _, rc, expired = asyncio.run(run())
    assert rc == 124 and expired and b"ready" in out
    assert time.monotonic() - start < 8


def test_acp_eof_resolves_pending_requests():
    async def run():
        async def write(line):
            pass
        conn = AcpConnection(write)
        task = asyncio.create_task(conn.request("initialize", {}))
        await asyncio.sleep(0)
        conn.close()
        try:
            await asyncio.wait_for(task, 1)
        except AcpError as exc:
            assert exc.message == "ACP transport closed"
        else:
            raise AssertionError("EOF did not fail the pending request")
    asyncio.run(run())
