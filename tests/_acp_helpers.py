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
