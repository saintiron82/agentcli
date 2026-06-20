"""Kiro CLI 프로바이더 — ACP(JSON-RPC 2.0 over stdio) 기반 세션·스트리밍.

`kiro-cli acp` 를 호출당 1회 one-shot turn 으로 감싼다:
  initialize → session/new|load → session/prompt → session/update 소비 → stopReason.
세션이 히스토리를 소유하므로 라이브러리는 session_id 만 관리한다.

verified against kiro-cli: (Task 0 spike 에서 고정)
"""
import asyncio
import contextlib
import logging
import shutil
import time
from pathlib import Path
from typing import AsyncIterator

from .base import LLMProvider, build_session_prompt, run_health_command, estimate_payload_prompt_tokens as _estimate
from ._acp import AcpConnection, AcpError
from ..types import (ERROR_BINARY_MISSING, ProviderHealth, Message, LLMResponse,
                     StreamChunk, TokenUsage)
from ..utils import build_env

logger = logging.getLogger(__name__)

# ACP protocol constants
_PROTOCOL_VERSION = 1
_DONE = object()  # 큐 종료 센티넬

# 모델 selector 는 resolve_model(비-strict) 로 그대로 통과시킨다.
# 알려진 id 는 Task 0 spike / `kiro-cli chat --list-models` 로 확장 가능.
KIRO_MODELS = [
    {"id": "", "name": "기본", "aliases": ["default"]},
]


class KiroProvider(LLMProvider):
    provider_id = "kiro"
    supports_sessions = True
    supports_streaming = True
    stores_history = False  # 히스토리는 Kiro ACP 세션이 소유

    def __init__(self, *, trust_all: bool = True,
                 trust_tools: list[str] | None = None,
                 model: str = "", agent: str = ""):
        """
        Args:
            trust_all: session/request_permission 을 전부 자동 승인 (dev 기본).
                multi-tenant/untrusted 임베딩 시 False + trust_tools 로 좁힐 것.
            trust_tools: trust_all=False 일 때 자동 승인할 도구 이름 목록.
            model: 기본 모델 selector (호출 인자가 우선).
            agent: kiro agent 이름 (선택).
        """
        self._trust_all = trust_all
        self._trust_tools = set(trust_tools or [])
        self._model = model
        self._agent = agent

    # ------------------------------------------------------------------
    # Public surface: invoke / invoke_async / stream_async
    # ------------------------------------------------------------------

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "", cwd: str | None = None) -> LLMResponse:
        """Synchronous invoke — must NOT be called from inside a running event loop.
        Use invoke_async / stream_async directly in async contexts.
        """
        return asyncio.run(self.invoke_async(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd))

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> LLMResponse:
        """Async invoke — consumes stream_async and folds chunks."""
        parts: list[str] = []
        usage = TokenUsage()
        sid = session_id
        err = ""
        err_type = ""
        latency = 0
        async for ch in self.stream_async(
                messages, model=model, timeout=timeout,
                session_id=session_id, cwd=cwd):
            if ch.session_id:
                sid = ch.session_id
            if ch.type == "text":
                parts.append(ch.content)
            elif ch.type == "error":
                err = ch.content or "kiro stream error"
                err_type = (ch.data or {}).get("error_type", "") or "unknown"
            elif ch.type == "done":
                if ch.usage is not None:
                    usage = ch.usage
                latency = int((ch.data or {}).get("latency_ms") or 0)
        content = "".join(parts)
        if not content and err:
            return LLMResponse(
                content="", provider=self.provider_id,
                model=model or self._model, session_id=sid,
                error=err, error_type=err_type,
                exit_code=127 if "not found" in err else None)
        return LLMResponse(
            content=content, provider=self.provider_id,
            model=model or self._model, tokens=usage,
            latency_ms=latency, session_id=sid)

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "", cwd: str | None = None,
                           idle_timeout: int | None = None,
                           wall_timeout: float | None = None) -> AsyncIterator[StreamChunk]:
        """Public streaming interface. Yields normalized StreamChunk objects."""
        if not self._find_binary():
            yield StreamChunk(type="error", content="kiro-cli not found",
                              data={"error_type": ERROR_BINARY_MISSING,
                                    "exit_code": 127})
            return
        prompt = build_session_prompt(messages)
        factory, state = self._subprocess_conn_factory(model or self._model, cwd)
        try:
            async for chunk in self._acp_turn(
                    prompt=prompt, model=model or self._model,
                    session_id=session_id, cwd=cwd, timeout=timeout,
                    idle_timeout=idle_timeout, wall_timeout=wall_timeout,
                    conn_factory=factory):
                yield chunk
        finally:
            box = state.get("proc_box") or {}
            rdr = box.get("reader")
            proc = box.get("proc")
            if rdr and not rdr.done():
                rdr.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await rdr
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    await proc.wait()

    def _subprocess_conn_factory(self, model: str, cwd: str | None):
        """Return (factory, state) for spawning a real kiro-cli acp subprocess.

        factory(on_request, on_notification) -> AcpConnection
        state["proc_box"] holds {"proc": ..., "reader": ...} after first write.
        """
        state: dict = {}

        def factory(on_request, on_notification):
            bin_path = self._find_binary()
            cmd = [bin_path, "acp"]
            if self._agent:
                cmd += ["--agent", self._agent]
            proc_box: dict = {}

            async def ensure_proc():
                if "proc" in proc_box:
                    return proc_box["proc"]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd, env=build_env())
                proc_box["proc"] = proc

                async def reader():
                    assert proc.stdout
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        await conn.handle_line(line.decode("utf-8", errors="replace"))

                proc_box["reader"] = asyncio.create_task(reader())
                return proc

            async def write_line(line: str) -> None:
                proc = await ensure_proc()
                assert proc.stdin
                proc.stdin.write((line + "\n").encode("utf-8"))
                await proc.stdin.drain()

            conn = AcpConnection(write_line, on_request=on_request,
                                 on_notification=on_notification)
            state["proc_box"] = proc_box
            return conn

        return factory, state

    def is_available(self) -> bool:
        return shutil.which("kiro-cli") is not None

    def _find_binary(self) -> str | None:
        return shutil.which("kiro-cli")

    def list_models(self) -> list[dict]:
        return list(KIRO_MODELS)

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        bin_path = shutil.which("kiro-cli")
        if not bin_path:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="binary_missing",
                available=False, auth_ok=False,
                error_type=ERROR_BINARY_MISSING,
                message="kiro-cli not found")
        version_proc = run_health_command([bin_path, "--version"], timeout=timeout)
        version = (version_proc.stdout or version_proc.stderr).strip()
        return ProviderHealth(
            provider=self.provider_id, ok=True, status="ok", available=True,
            binary=bin_path, version=version, auth_ok=None,
            message="kiro-cli available")

    async def _acp_turn(self, *, prompt: str, model: str, session_id: str,
                        cwd: str | None, timeout: int,
                        idle_timeout: int | None, wall_timeout: int | None,
                        conn_factory) -> AsyncIterator[StreamChunk]:
        """한 turn 을 구동하며 정규화 청크를 yield.

        conn_factory(on_request, on_notification) -> AcpConnection 으로 transport 주입.
        실제 subprocess 연결은 Task 8에서 구현; 테스트에서는 ScriptedAgent-기반 팩토리를 주입.
        """
        queue: asyncio.Queue = asyncio.Queue()
        usage = TokenUsage(payload_prompt_tokens=_estimate(prompt),
                           prompt_tokens_reliable=False,
                           prompt_tokens_source="kiro_cli_reported")
        state = {"session_id": session_id}

        async def on_notification(method: str, params: dict) -> None:
            if method == "session/update":
                for ch in _map_session_update(params.get("update") or {}, usage):
                    await queue.put(ch)

        async def on_request(method: str, params: dict) -> dict:
            return await self._handle_agent_request(method, params, cwd)

        conn = conn_factory(on_request, on_notification)

        async def drive() -> None:
            try:
                await conn.request("initialize", {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": False},
                    "clientInfo": {"name": "agentcli", "version": "0"}})
                if session_id:
                    try:
                        await conn.request("session/load",
                                           {"sessionId": session_id, "cwd": cwd or "."})
                    except AcpError:
                        # 잔여(stale) 세션 → 새 세션으로 1회 복구 (Task 6 에서 테스트).
                        state["session_id"] = (await conn.request(
                            "session/new", {"cwd": cwd or "."}))["sessionId"]
                else:
                    state["session_id"] = (await conn.request(
                        "session/new", {"cwd": cwd or "."}))["sessionId"]
                res = await conn.request("session/prompt", {
                    "sessionId": state["session_id"],
                    "prompt": [{"type": "text", "text": prompt}]})
                await queue.put((_DONE, res.get("stopReason", "")))
            except AcpError as exc:
                await queue.put((_DONE, exc))
            except Exception as exc:  # noqa: BLE001
                await queue.put((_DONE, exc))

        start = time.time()
        wall_deadline = start + wall_timeout if wall_timeout is not None else None
        driver = asyncio.create_task(drive())
        idle = idle_timeout if idle_timeout is not None else timeout
        try:
            while True:
                # Compute effective timeout: min(idle, remaining wall time).
                if wall_deadline is not None:
                    remaining = wall_deadline - time.time()
                    if remaining <= 0:
                        yield StreamChunk(type="error",
                                          content="wall timeout expired",
                                          data={"error_type": "timeout",
                                                "timeout_kind": "wall"})
                        return
                    effective = min(idle, remaining)
                else:
                    effective = idle
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=effective)
                except asyncio.TimeoutError:
                    # Determine whether wall or idle expired.
                    if wall_deadline is not None and time.time() >= wall_deadline:
                        yield StreamChunk(type="error",
                                          content="wall timeout expired",
                                          data={"error_type": "timeout",
                                                "timeout_kind": "wall"})
                    else:
                        yield StreamChunk(type="error",
                                          content=f"idle timeout: {idle}s",
                                          data={"error_type": "timeout",
                                                "timeout_kind": "idle"})
                    return
                if isinstance(item, StreamChunk):
                    yield item
                    continue
                # (_DONE, payload) 센티넬
                _, payload = item
                if isinstance(payload, AcpError):
                    yield StreamChunk(type="error", content=payload.message,
                                      data={"error_type": "unknown"})
                    return
                if isinstance(payload, Exception):
                    yield StreamChunk(type="error", content=str(payload),
                                      data={"error_type": "unknown"})
                    return
                yield StreamChunk(
                    type="done", content="",
                    session_id=state["session_id"], usage=usage,
                    data={"provider": self.provider_id, "model": model,
                          "latency_ms": int((time.time() - start) * 1000),
                          "stop_reason": payload})
                return
        finally:
            # Fix A: await driver cancellation to prevent "never retrieved" warnings.
            if not driver.done():
                driver.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await driver

    async def _handle_agent_request(self, method: str, params: dict,
                                    cwd: str | None) -> dict:
        """agent→client 역콜백 핸들러."""
        if method == "session/request_permission":
            return self._decide_permission(params)
        if method == "fs/read_text_file":
            return self._fs_read(params, cwd)
        if method == "fs/write_text_file":
            return self._fs_write(params, cwd)
        # terminal/* 등 미지원 역요청은 빈 result (Task 0 spike 로 필요성 확인).
        return {}

    def _decide_permission(self, params: dict) -> dict:
        options = params.get("options") or []
        tool_title = (params.get("toolCall") or {}).get("title", "")
        allowed = self._trust_all or (tool_title in self._trust_tools)
        if allowed:
            for opt in options:
                if str(opt.get("kind", "")).startswith("allow") or \
                   opt.get("optionId") == "allow":
                    return {"outcome": {"outcome": "selected",
                                        "optionId": opt.get("optionId")}}
        return {"outcome": {"outcome": "cancelled"}}

    def _within_cwd(self, path: str, cwd: str | None) -> Path | None:
        if not cwd:
            return None
        try:
            root = Path(cwd).resolve()
            target = Path(path).resolve()
            target.relative_to(root)  # cwd 밖이면 ValueError
            return target
        except (ValueError, OSError):
            return None

    def _fs_read(self, params: dict, cwd: str | None) -> dict:
        target = self._within_cwd(params.get("path", ""), cwd)
        if target is None or not target.is_file():
            logger.warning("kiro fs/read 거부: %s", params.get("path"))
            return {"content": ""}
        try:
            return {"content": target.read_text(encoding="utf-8", errors="replace")}
        except OSError:
            return {"content": ""}

    def _fs_write(self, params: dict, cwd: str | None) -> dict:
        target = self._within_cwd(params.get("path", ""), cwd)
        if target is None:
            logger.warning("kiro fs/write 거부: %s", params.get("path"))
            return {}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(params.get("content", ""), encoding="utf-8")
        except OSError:
            pass
        return {}


def _map_session_update(update: dict, usage: TokenUsage) -> list[StreamChunk]:
    """ACP session/update.update 1개를 정규화 청크로 변환 + usage 누적.

    필드명은 ACP 표준 기준 (Task 0 spike 로 확정). 모르는 변형은 event 로.
    """
    kind = update.get("sessionUpdate", "")
    if kind == "agent_message_chunk":
        text = (update.get("content") or {}).get("text", "")
        return [StreamChunk(type="text", content=text, data=update)] if text else []
    if kind == "agent_thought_chunk":
        text = (update.get("content") or {}).get("text", "")
        return [StreamChunk(type="thinking", content=text, data=update)] if text else []
    if kind == "tool_call":
        return [StreamChunk(type="tool_use", data=update)]
    if kind == "tool_call_update":
        return [StreamChunk(type="tool_result", data=update)]
    if kind == "usage_update":
        used = int(update.get("used") or 0)
        usage.prompt_tokens = used
        usage.total_tokens = used + usage.completion_tokens
        usage.prompt_tokens_reliable = False
        usage.prompt_tokens_source = "kiro_cli_reported"
        return []
    return [StreamChunk(type="event", data=update)]
