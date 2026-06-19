"""최소 JSON-RPC 2.0 클라이언트 (ACP 전송용, transport-agnostic).

줄 단위 JSON. 신규 의존성 없이 stdlib json+asyncio 만 사용.
``write_line`` 으로 송신, ``handle_line`` 으로 수신 1라인을 디스패치한다.
응답은 id 로 상관(correlation)하고, agent→client 역요청/알림은 콜백으로 위임.
"""
import asyncio
import json
from typing import Awaitable, Callable


class AcpError(Exception):
    def __init__(self, error: dict):
        self.code = int(error.get("code", 0))
        self.message = str(error.get("message", ""))
        self.data = error.get("data")
        super().__init__(f"ACP error {self.code}: {self.message}")


class AcpConnection:
    def __init__(
        self,
        write_line: Callable[[str], Awaitable[None]],
        *,
        on_request: Callable[[str, dict], Awaitable[dict]] | None = None,
        on_notification: Callable[[str, dict], Awaitable[None]] | None = None,
    ):
        self._write_line = write_line
        self._on_request = on_request
        self._on_notification = on_notification
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write_line(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        except BaseException:
            self._pending.pop(rid, None)
            raise
        return await fut

    async def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return  # 비 JSON 라인 무시 (호스트로 예외 전파 금지)
        if not isinstance(msg, dict):
            return
        if "method" in msg and "id" in msg:
            await self._dispatch_request(msg)
        elif "method" in msg:
            if self._on_notification is not None:
                await self._on_notification(msg["method"], msg.get("params") or {})
        elif "id" in msg:
            self._resolve(msg)

    async def _dispatch_request(self, msg: dict) -> None:
        result: dict = {}
        if self._on_request is not None:
            result = await self._on_request(msg["method"], msg.get("params") or {})
        await self._write_line(json.dumps(
            {"jsonrpc": "2.0", "id": msg["id"], "result": result}))

    def _resolve(self, msg: dict) -> None:
        fut = self._pending.pop(msg.get("id"), None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            fut.set_exception(AcpError(msg["error"] or {}))
        else:
            fut.set_result(msg.get("result") or {})
