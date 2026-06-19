import asyncio
import pytest
from agentcli.providers._acp import AcpConnection, AcpError
from tests._acp_helpers import FakeTransport


@pytest.mark.asyncio
async def test_request_correlates_response_by_id():
    t = FakeTransport()
    conn = AcpConnection(t.write_line)
    t.bind(conn)

    async def respond_later():
        # client가 보낸 요청의 id로 응답을 돌려준다.
        await asyncio.sleep(0)
        rid = t.last_sent()["id"]
        await t.feed({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})

    asyncio.create_task(respond_later())
    result = await conn.request("initialize", {"protocolVersion": 1})
    assert result == {"ok": True}
    sent = t.last_sent()
    assert sent["jsonrpc"] == "2.0" and sent["method"] == "initialize"
    assert sent["params"] == {"protocolVersion": 1}
    assert isinstance(sent["id"], int)


@pytest.mark.asyncio
async def test_request_raises_on_error_response():
    t = FakeTransport()
    conn = AcpConnection(t.write_line)
    t.bind(conn)

    async def respond_err():
        await asyncio.sleep(0)
        rid = t.last_sent()["id"]
        await t.feed({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32000, "message": "boom"}})

    asyncio.create_task(respond_err())
    with pytest.raises(AcpError) as ei:
        await conn.request("session/new", {})
    assert ei.value.code == -32000
    assert "boom" in ei.value.message


@pytest.mark.asyncio
async def test_handle_line_ignores_non_json_and_non_object():
    sent = []
    async def write_line(l): sent.append(l)
    conn = AcpConnection(write_line)
    await conn.handle_line("not json at all")
    await conn.handle_line("[1, 2, 3]")   # valid JSON, not an object
    assert sent == []   # nothing emitted, no exception raised
