"""Shared ACP (Agent Client Protocol, JSON-RPC 2.0 over stdio) provider driver.

Each call is a one-shot turn:
  initialize → session/new | session/load → session/prompt → session/update* → stopReason.
The agent CLI owns history; the library keeps only the session ID. Subclasses
supply the binary name, the command line and the health check.

ACP field names follow the ACP spec (https://agentclientprotocol.com).
"""
import asyncio
import contextlib
import logging
import shutil
import time
from abc import abstractmethod
from contextlib import aclosing
from pathlib import Path
from typing import AsyncIterator

from ..auth_context import provider_environment
from ..filesystem import safe_workspace_path
from ..launchers import prepare_subprocess_command
from ..processes import attach_process_job
from ..reasoning import ReasoningResolution, needs_event, to_dict
from ..types import (ERROR_BINARY_MISSING, LLMResponse, Message, StreamChunk, TokenUsage,
                     classify_error)
from ..utils import build_env
from ._acp import AcpConnection, AcpError
from .base import (LLMProvider, _drain_stderr, _kill_process_group, _new_session_kwargs,
                   build_session_prompt)
from .base import estimate_payload_prompt_tokens as _estimate

# ACP protocol constants
_PROTOCOL_VERSION = 1
_DONE = object()  # 큐 종료 센티넬

# session/update kinds that carry conversation content. The ACP spec makes an
# agent replay the stored conversation as these during session/load; received
# before session/prompt they are history, not output of this turn. usage_update
# is dropped there too so replayed usage does not overwrite this turn's usage.
_PRE_PROMPT_DROPPED = frozenset({
    "user_message_chunk", "agent_message_chunk", "agent_thought_chunk",
    "tool_call", "tool_call_update", "usage_update",
})


# `source` defaults to Kiro's value so the original two-argument call
# `_map_session_update(update, usage)` (imported from providers.kiro) keeps working.
def _map_session_update(update: dict, usage: TokenUsage,
                        source: str = "kiro_cli_reported") -> list[StreamChunk]:
    """ACP session/update.update 1개를 정규화 청크로 변환 + usage 누적.

    필드명은 ACP 표준 기준. 모르는 변형은 event 로.
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
        usage.prompt_tokens_source = source
        return []
    return [StreamChunk(type="event", data=update)]


class AcpProvider(LLMProvider):
    """Base for providers that drive an `<binary> acp` style agent over stdio.

    Subclass contract:
      Required: class attributes `provider_id`, `binary_name`, `usage_source`;
        methods `_command(binary, model, reasoning)`, `health_check`, `list_models`.
      Optional: `_legacy_environment()` (child env when no auth profile is bound)
        and `_map_update(update, usage)` (extra per-update mapping).
      A subclass that adds call options (for example `effort` / `thinking`) must
      override `invoke`, `invoke_async` and `stream_async` explicitly, because
      capabilities are derived from those signatures, and must forward the
      resolved reasoning to `_invoke` / `_stream`. `_stream` emits the reasoning
      event itself, so those overrides need no other behavior.
    """

    supports_sessions = True
    supports_streaming = True
    stores_history = False  # 히스토리는 agent CLI 의 ACP 세션이 소유
    binary_name: str = ""
    usage_source: str = ""

    def __init__(self, *, trust_all: bool = True,
                 trust_tools: list[str] | None = None,
                 model: str = "",
                 allow_file_reads: bool = True,
                 allow_file_writes: bool | None = None,
                 max_file_bytes: int = 1024 * 1024):
        self._trust_all = trust_all
        self._trust_tools = set(trust_tools or [])
        self._allow_file_reads = allow_file_reads
        self._allow_file_writes = trust_all if allow_file_writes is None else allow_file_writes
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive integer")
        self._max_file_bytes = max_file_bytes
        self._model = model

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def _command(self, binary: str, model: str,
                 reasoning: "ReasoningResolution | None") -> list[str]:
        """argv for one turn."""

    def _legacy_environment(self) -> dict:
        """Child env when no auth profile is bound."""
        return build_env()

    def _child_env(self) -> dict:
        """Environment for the agent subprocess (and subclass health checks)."""
        return provider_environment(self.provider_id, self._legacy_environment)

    @property
    def _logger(self) -> logging.Logger:
        return logging.getLogger(f"agentcli.providers.{self.provider_id}")

    def _map_update(self, update: dict, usage: TokenUsage) -> list[StreamChunk]:
        return _map_session_update(update, usage, self.usage_source)

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
        return await self._fold(self.stream_async(
            messages, model=model, timeout=timeout,
            session_id=session_id, cwd=cwd),
            model=model, session_id=session_id, reasoning=None)

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "", cwd: str | None = None,
                           idle_timeout: int | None = None,
                           wall_timeout: float | None = None) -> AsyncIterator[StreamChunk]:
        """Public streaming interface. Yields normalized StreamChunk objects."""
        async with aclosing(self._stream(
                messages, model=model, timeout=timeout, session_id=session_id, cwd=cwd,
                idle_timeout=idle_timeout, wall_timeout=wall_timeout, reasoning=None)) as stream:
            async for chunk in stream:
                yield chunk

    def is_available(self) -> bool:
        return self._find_binary() is not None

    def _find_binary(self) -> str | None:
        return shutil.which(self.binary_name)

    # ------------------------------------------------------------------
    # Shared implementation
    # ------------------------------------------------------------------

    async def _invoke(self, messages, *, model, timeout, session_id, cwd,
                      reasoning) -> LLMResponse:
        """Fold _stream (with an explicit reasoning resolution) into an LLMResponse."""
        return await self._fold(self._stream(
            messages, model=model, timeout=timeout, session_id=session_id, cwd=cwd,
            idle_timeout=None, wall_timeout=None, reasoning=reasoning),
            model=model, session_id=session_id, reasoning=reasoning)

    async def _fold(self, chunks: AsyncIterator[StreamChunk], *, model: str,
                    session_id: str, reasoning) -> LLMResponse:
        """Consume a chunk stream and fold it into an LLMResponse."""
        parts: list[str] = []
        usage = TokenUsage()
        sid = session_id
        err = ""
        err_type = ""
        exit_code = None
        suggested_action = ""
        latency = 0
        async with aclosing(chunks) as stream:
            async for ch in stream:
                if ch.session_id:
                    sid = ch.session_id
                if ch.usage is not None:
                    usage = ch.usage
                if ch.type == "text":
                    parts.append(ch.content)
                elif ch.type == "error":
                    err = ch.content or f"{self.provider_id} stream error"
                    err_type = ch.data.get("error_type") or classify_error(err)
                    exit_code = ch.data.get("exit_code")
                    suggested_action = ch.data.get("suggested_action", "")
                elif ch.type == "done":
                    latency = int(ch.data.get("latency_ms") or 0)
        content = "".join(parts)
        if err:
            response = LLMResponse(
                content="", provider=self.provider_id,
                model=model or self._model, session_id=sid,
                error=err, error_type=err_type, raw_content=content,
                tokens=usage, latency_ms=latency, suggested_action=suggested_action,
                exit_code=exit_code if exit_code is not None else (127 if "not found" in err else None))
        else:
            response = LLMResponse(
                content=content, provider=self.provider_id,
                model=model or self._model, tokens=usage,
                latency_ms=latency, session_id=sid)
        response.reasoning = reasoning
        return response

    async def _stream(self, messages, *, model, timeout, session_id, cwd,
                      idle_timeout, wall_timeout, reasoning) -> AsyncIterator[StreamChunk]:
        # The one place the reasoning event is emitted, so _invoke and
        # stream_async callers see identical chunks.
        if reasoning is not None and needs_event(reasoning):
            yield StreamChunk(type="event", data={"reasoning": to_dict(reasoning),
                                                  "provider": self.provider_id})
        if not self._find_binary():
            yield StreamChunk(type="error", content=f"{self.binary_name} not found",
                              data={"error_type": ERROR_BINARY_MISSING,
                                    "exit_code": 127})
            return
        prompt = build_session_prompt(messages)
        factory, state = self._subprocess_conn_factory(model or self._model, cwd, reasoning)
        try:
            async with aclosing(self._acp_turn(
                    prompt=prompt, model=model or self._model,
                    session_id=session_id, cwd=cwd, timeout=timeout,
                    idle_timeout=idle_timeout, wall_timeout=wall_timeout,
                    conn_factory=factory)) as stream:
                async for chunk in stream:
                    yield chunk
        finally:
            box = state.get("proc_box") or {}
            rdr = box.get("reader")
            proc = box.get("proc")
            if proc is not None:
                with contextlib.suppress(Exception):
                    _kill_process_group(proc)
                    await asyncio.wait_for(proc.wait(), timeout=5)
            for task in (rdr, box.get("stderr")):
                if task:
                    if not task.done():
                        task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task

    def _subprocess_conn_factory(self, model: str, cwd: str | None,
                                 reasoning: "ReasoningResolution | None" = None):
        """Return (factory, state) for spawning the real agent subprocess.

        factory(on_request, on_notification) -> AcpConnection
        state["proc_box"] holds {"proc": ..., "reader": ...} after first write.
        """
        state: dict = {}

        def factory(on_request, on_notification):
            bin_path = self._find_binary()
            cmd = self._command(bin_path, model, reasoning)
            proc_box: dict = {}

            async def ensure_proc():
                if "proc" in proc_box:
                    return proc_box["proc"]
                proc = await asyncio.create_subprocess_exec(
                    *prepare_subprocess_command(cmd),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd, env=self._child_env(), limit=8 * 1024 * 1024,
                    **_new_session_kwargs())
                attach_process_job(proc)
                proc_box["proc"] = proc

                async def reader():
                    assert proc.stdout
                    try:
                        while True:
                            line = await proc.stdout.readline()
                            if not line:
                                break
                            await conn.handle_line(line.decode("utf-8", errors="replace"))
                    finally:
                        conn.close()

                proc_box["reader"] = asyncio.create_task(reader())
                if proc.stderr is not None:
                    proc_box["stderr"] = asyncio.create_task(
                        _drain_stderr(proc.stderr, [], self._logger, self.provider_id, debug=False))
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

    async def _acp_turn(self, *, prompt: str, model: str, session_id: str,
                        cwd: str | None, timeout: int,
                        idle_timeout: float | None, wall_timeout: float | None,
                        conn_factory) -> AsyncIterator[StreamChunk]:
        """한 turn 을 구동하며 정규화 청크를 yield.

        conn_factory(on_request, on_notification) -> AcpConnection 으로 transport 주입.
        프로덕션 경로는 _subprocess_conn_factory 가 transport 를 주입하고, 테스트는 가짜 팩토리를 주입한다.
        """
        queue: asyncio.Queue = asyncio.Queue()
        usage = TokenUsage(payload_prompt_tokens=_estimate(prompt),
                           prompt_tokens_reliable=False,
                           prompt_tokens_source=self.usage_source)
        # "prompting" flips to True right before session/prompt is sent. Until
        # then, content updates (_PRE_PROMPT_DROPPED) are replayed history from
        # session/load and are dropped; every other kind (available_commands_update,
        # current_mode_update, ...) is forwarded as before.
        state = {"session_id": session_id, "prompting": False}

        async def on_notification(method: str, params: dict) -> None:
            if method == "session/update":
                update = params.get("update") or {}
                if not state["prompting"] and update.get("sessionUpdate") in _PRE_PROMPT_DROPPED:
                    return
                for ch in self._map_update(update, usage):
                    await queue.put(ch)

        async def on_request(method: str, params: dict) -> dict:
            return await self._handle_agent_request(method, params, cwd)

        conn = conn_factory(on_request, on_notification)

        async def drive() -> None:
            try:
                await conn.request("initialize", {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": self._allow_file_reads,
                               "writeTextFile": self._allow_file_writes},
                        "terminal": False},
                    "clientInfo": {"name": "agentcli", "version": "0"}})
                if session_id:
                    try:
                        await conn.request(
                            "session/load",
                            {"sessionId": session_id, "cwd": cwd or ".",
                             "mcpServers": []})
                    except AcpError:
                        # 잔여(stale) 세션 → 새 세션으로 1회 복구.
                        state["session_id"] = (await conn.request(
                            "session/new",
                            {"cwd": cwd or ".", "mcpServers": []}))["sessionId"]
                else:
                    state["session_id"] = (await conn.request(
                        "session/new",
                        {"cwd": cwd or ".", "mcpServers": []}))["sessionId"]
                state["prompting"] = True
                res = await conn.request("session/prompt", {
                    "sessionId": state["session_id"],
                    "prompt": [{"type": "text", "text": prompt}]})
                await queue.put((_DONE, res.get("stopReason", "")))
            except Exception as exc:  # noqa: BLE001  (AcpError included)
                await queue.put((_DONE, exc))

        start = time.monotonic()
        wall_deadline = start + wall_timeout if wall_timeout is not None else None
        driver = asyncio.create_task(drive())
        idle = idle_timeout if idle_timeout is not None else timeout
        text_parts: list[str] = []
        try:
            while True:
                # Compute effective timeout: min(idle, remaining wall time).
                if wall_deadline is not None:
                    remaining = wall_deadline - time.monotonic()
                    if remaining <= 0:
                        yield StreamChunk(type="error",
                                          content="wall timeout expired",
                                          data={"error_type": "timeout",
                                                "timeout_kind": "wall"})
                        return
                    wall_limited = remaining <= idle
                    effective = min(idle, remaining)
                else:
                    wall_limited = False
                    effective = idle
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=effective)
                except asyncio.TimeoutError:
                    # Determine whether wall or idle expired.
                    if wall_limited or (wall_deadline is not None and time.monotonic() >= wall_deadline):
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
                    if item.type == "text":
                        text_parts.append(item.content)
                    yield item
                    continue
                # (_DONE, payload) 센티넬
                _, payload = item
                if isinstance(payload, AcpError):
                    yield StreamChunk(type="error", content=payload.message,
                                      data={"error_type": classify_error(payload.message)})
                    return
                if isinstance(payload, Exception):
                    yield StreamChunk(type="error", content=str(payload),
                                      data={"error_type": classify_error(str(payload))})
                    return
                yield StreamChunk(
                    type="done", content="".join(text_parts),
                    session_id=state["session_id"], usage=usage,
                    data={"provider": self.provider_id, "model": model,
                          "latency_ms": int((time.monotonic() - start) * 1000),
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
        # terminal/* 등 미지원 역요청은 빈 result.
        return {}

    def _decide_permission(self, params: dict) -> dict:
        options = params.get("options") or []
        tool_title = (params.get("toolCall") or {}).get("title", "")
        allowed = self._trust_all or (tool_title in self._trust_tools)
        if allowed:
            # Prefer an explicitly "allow"-named option first.
            for opt in options:
                if str(opt.get("kind", "")).startswith("allow") or \
                   opt.get("optionId") == "allow":
                    return {"outcome": {"outcome": "selected",
                                        "optionId": opt.get("optionId")}}
            # Fall back to the first non-reject option if no allow-named one exists.
            for opt in options:
                if not str(opt.get("kind", "")).startswith("reject") and \
                   opt.get("optionId") != "reject":
                    return {"outcome": {"outcome": "selected",
                                        "optionId": opt.get("optionId")}}
        return {"outcome": {"outcome": "cancelled"}}

    def _within_cwd(self, path: str, cwd: str | None) -> Path | None:
        if not cwd:
            return None
        try:
            root = Path(cwd).absolute()
            target = Path(path)
            return safe_workspace_path(root, target if target.is_absolute() else root / target)
        except (ValueError, OSError):
            return None

    def _fs_read(self, params: dict, cwd: str | None) -> dict:
        if not self._allow_file_reads:
            raise PermissionError("File reads are disabled")
        target = self._within_cwd(params.get("path", ""), cwd)
        if target is None or not target.is_file():
            self._logger.warning("%s fs/read 거부", self.provider_id)
            return {"content": ""}
        try:
            with target.open("rb") as source:
                data = source.read(self._max_file_bytes + 1)
            if len(data) > self._max_file_bytes:
                raise ValueError("File exceeds max_file_bytes")
            text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            line, limit = params.get("line", 1), params.get("limit")
            if (isinstance(line, bool) or not isinstance(line, int) or line < 1
                    or (limit is not None and (isinstance(limit, bool)
                        or not isinstance(limit, int) or limit < 1))):
                raise ValueError("Invalid file line/limit")
            if line != 1 or limit is not None:
                lines = text.splitlines(keepends=True)
                text = "".join(lines[line - 1:line - 1 + limit if limit is not None else None])
            return {"content": text}
        except OSError:
            return {"content": ""}

    def _fs_write(self, params: dict, cwd: str | None) -> dict:
        if not self._allow_file_writes:
            raise PermissionError("File writes are disabled")
        content = params.get("content", "")
        if not isinstance(content, str) or len(content.encode("utf-8")) > self._max_file_bytes:
            raise ValueError("File content exceeds max_file_bytes or is not text")
        target = self._within_cwd(params.get("path", ""), cwd)
        if target is None:
            self._logger.warning("%s fs/write 거부", self.provider_id)
            raise PermissionError("File write outside the workspace is denied")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {}
