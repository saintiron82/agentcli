"""ACP 테스트용 in-memory duplex 트랜스포트 + 스크립트형 가짜 agent."""
import json


class FakeTransport:
    """client.write_line → 여기에 쌓이고, feed()로 agent→client 라인을 주입."""
    def __init__(self):
        self.client_to_agent: list[str] = []   # client가 보낸 라인(JSON 문자열)
        self._conn = None

    def bind(self, conn):
        self._conn = conn

    async def write_line(self, line: str) -> None:
        self.client_to_agent.append(line)

    async def feed(self, obj: dict) -> None:
        """agent→client 메시지 1개를 conn에 전달."""
        await self._conn.handle_line(json.dumps(obj))

    def last_sent(self) -> dict:
        return json.loads(self.client_to_agent[-1])

    def sent_methods(self) -> list[str]:
        return [json.loads(l).get("method") for l in self.client_to_agent]


class ScriptedAgent:
    """client 가 보낸 요청에 대해 미리 정한 응답/알림을 큐에 흘리는 가짜 agent.

    client.write_line 을 가로채 method 별로 핸들러를 호출하고, 그 결과
    메시지들을 conn.handle_line 으로 되먹인다. session/prompt 수신 시
    session/update 알림들을 보낸 뒤 prompt result(stopReason)로 마무리.
    """
    def __init__(self, conn, *, updates=None, stop_reason="end_turn",
                 new_session_id="kiro-sess-1", load_ok=True):
        self._conn = conn
        self._updates = updates or []
        self._stop = stop_reason
        self._sid = new_session_id
        self._load_ok = load_ok

    async def write_line(self, line: str) -> None:
        msg = json.loads(line)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": 1,
                           "agentCapabilities": {"loadSession": True},
                           "authMethods": []}}))
        elif method == "session/new":
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid, "result": {"sessionId": self._sid}}))
        elif method == "session/load":
            if self._load_ok:
                await self._conn.handle_line(json.dumps(
                    {"jsonrpc": "2.0", "id": rid, "result": {}}))
            else:
                await self._conn.handle_line(json.dumps(
                    {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32000, "message": "session not found"}}))
        elif method == "session/prompt":
            sid = params.get("sessionId", self._sid)
            for upd in self._updates:
                await self._conn.handle_line(json.dumps({
                    "jsonrpc": "2.0", "method": "session/update",
                    "params": {"sessionId": sid, "update": upd}}))
            await self._conn.handle_line(json.dumps({
                "jsonrpc": "2.0", "id": rid, "result": {"stopReason": self._stop}}))
