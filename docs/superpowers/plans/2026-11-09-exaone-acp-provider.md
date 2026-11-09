# EXAONE ACP Provider (unofficial) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let agentcli drive the separate, unofficial `exaone` CLI (exaone-cli, ACP over stdio) as an opt-in `exaone` provider with sessions, streaming, effort control and service auth profiles.

**Architecture:** Move the ACP turn driver out of `providers/kiro.py` into a reusable `providers/acp_base.py` (`AcpProvider`), fixing one latent bug on the way (history replayed by `session/load` was emitted as new output). `KiroProvider` and the new `ExaoneProvider` become thin subclasses that supply the binary, command line and health check. `AuthProfile` gains `exaone_api_key` and a private `EXAONE_HOME`.

**Tech Stack:** Python ≥ 3.11, stdlib only (zero runtime deps), pytest + pytest-asyncio.

Spec: `docs/superpowers/specs/2026-11-09-exaone-acp-provider-design.md`.
Depends on: exaone-cli stage 1 (`D:\BProject\exaone-cli`) for the live check only; every offline test uses a fake `exaone` launcher.

Conventions for every task:
- Worktree `D:\BProject\agentcli\agentcli-exaone`, branch `feat/exaone-provider`. Run commands from there.
- `pytest` means the agentcli dev environment (`pip install -e ".[dev]"` in this worktree if not done).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Do not bump the version. Follow the CLAUDE.md terminology rule.
- Korean and English docs change together.

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `agentcli/providers/acp_base.py` | create | `AcpProvider`: ACP turn driver, subprocess transport, permission/fs callbacks, `_map_session_update` |
| `agentcli/providers/kiro.py` | rewrite (thin) | `KiroProvider`: binary `kiro-cli`, `--agent`, Kiro health check, `KIRO_MODELS`; re-exports `_map_session_update` |
| `agentcli/providers/exaone.py` | create | `ExaoneProvider`: binary `exaone`, `--model`/`--reasoning-effort`, effort/thinking, health, unofficial note |
| `agentcli/reasoning.py` | modify | `exaone` rows in `_EFFORT_MAP` / `_THINKING_MAP` |
| `agentcli/auth.py` | modify | `exaone_api_key`, `exaone` directory, method, validate, environment, `EXAONE_` prefix guard |
| `agentcli/client.py` | modify | rotation method check includes `exaone` |
| `tests/test_acp_base.py` | create | replay regression test |
| `tests/test_exaone_reasoning.py`, `tests/test_exaone_auth.py`, `tests/test_exaone_provider.py` | create | new behavior |
| `README.md`, `README.ko.md`, `docs/service-auth.md`, `docs/service-auth.ko.md`, `CHANGELOG.md` | modify | docs |

---

### Task 1: Regression test — `session/load` replay must not leak into the new turn

**Files:**
- Create: `tests/test_acp_base.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run to verify it fails for the right reason**

Run: `pytest tests/test_acp_base.py -q`
Expected: FAIL. The list contains `("event", "")` for the replayed user chunk plus `("thinking", "old thought")`, `("text", "OLD ANSWER")` before the new chunks, and `done.content == "OLD ANSWERNEW"`.

- [ ] **Step 3: Commit the failing test**

```
git add tests/test_acp_base.py
git commit -m "test(acp): pin that session/load replay is not new output"
```

---

### Task 2: Extract `AcpProvider` and fix the replay leak

**Files:**
- Create: `agentcli/providers/acp_base.py`
- Rewrite: `agentcli/providers/kiro.py`

Background the engineer needs:
- Kiro tests patch `agentcli.providers.kiro.shutil.which`, `...kiro.time.monotonic`, `...kiro.time.time`, `...kiro.asyncio.wait_for`. These are attributes of the shared stdlib modules, so they still take effect after the code moves, as long as `kiro.py` keeps importing `shutil`, `time` and `asyncio`.
- Kiro tests patch `agentcli.providers.kiro.run_health_command` by name, so `health_check` stays in `kiro.py` and calls the name imported there.
- Kiro tests import `_map_session_update` from `agentcli.providers.kiro` and call it with two arguments; it must keep that signature (new third parameter has a default).
- Kiro tests patch `KiroProvider._acp_turn` with fakes that accept exactly `prompt, model, session_id, cwd, timeout, idle_timeout, wall_timeout, conn_factory`. Do not add keyword arguments to the `_acp_turn` call.
- Kiro tests call `p._subprocess_conn_factory("", str(tmp_path))` with two positional arguments.

- [ ] **Step 1: Create `agentcli/providers/acp_base.py`**

```python
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
from contextlib import aclosing
from pathlib import Path
from typing import AsyncIterator

from ..auth_context import provider_environment
from ..filesystem import safe_workspace_path
from ..launchers import prepare_subprocess_command
from ..processes import attach_process_job
from ..types import (ERROR_BINARY_MISSING, LLMResponse, Message, StreamChunk, TokenUsage,
                     classify_error)
from ..utils import build_env
from ._acp import AcpConnection, AcpError
from .base import (LLMProvider, _drain_stderr, _kill_process_group, _new_session_kwargs,
                   build_session_prompt)
from .base import estimate_payload_prompt_tokens as _estimate

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = 1
_DONE = object()  # queue end sentinel


def _map_session_update(update: dict, usage: TokenUsage,
                        source: str = "kiro_cli_reported") -> list[StreamChunk]:
    """Convert one ACP session/update.update into normalized chunks and fold usage.

    Unknown variants become `event` chunks.
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
    """Base for providers that drive an `<binary> acp` style agent over stdio."""

    supports_sessions = True
    supports_streaming = True
    stores_history = False  # the agent CLI owns history
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

    def _command(self, binary: str, model: str, reasoning) -> list[str]:
        """argv for one turn. `reasoning` is a ReasoningResolution or None."""
        raise NotImplementedError

    def _legacy_environment(self) -> dict:
        """Child env when no auth profile is bound."""
        return build_env()

    def _map_update(self, update: dict, usage: TokenUsage) -> list[StreamChunk]:
        return _map_session_update(update, usage, self.usage_source)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "", cwd: str | None = None) -> LLMResponse:
        """Synchronous invoke — must NOT be called from inside a running event loop."""
        return asyncio.run(self.invoke_async(
            messages, model=model, timeout=timeout, session_id=session_id, cwd=cwd))

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> LLMResponse:
        return await self._invoke(messages, model=model, timeout=timeout,
                                  session_id=session_id, cwd=cwd, reasoning=None)

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "", cwd: str | None = None,
                           idle_timeout: int | None = None,
                           wall_timeout: float | None = None) -> AsyncIterator[StreamChunk]:
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
        """Consume _stream and fold chunks into an LLMResponse."""
        parts: list[str] = []
        usage = TokenUsage()
        sid = session_id
        err = ""
        err_type = ""
        exit_code = None
        suggested_action = ""
        latency = 0
        async with aclosing(self._stream(
                messages, model=model, timeout=timeout, session_id=session_id, cwd=cwd,
                idle_timeout=None, wall_timeout=None, reasoning=reasoning)) as stream:
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
        if not self._find_binary():
            yield StreamChunk(type="error", content=f"{self.binary_name} not found",
                              data={"error_type": ERROR_BINARY_MISSING, "exit_code": 127})
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

    def _subprocess_conn_factory(self, model: str, cwd: str | None, reasoning=None):
        """Return (factory, state) that spawns the real agent subprocess.

        factory(on_request, on_notification) -> AcpConnection
        state["proc_box"] holds {"proc": ..., "reader": ...} after the first write.
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
                    cwd=cwd, env=provider_environment(self.provider_id, self._legacy_environment),
                    limit=8 * 1024 * 1024,
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
                        _drain_stderr(proc.stderr, [], logger, self.provider_id, debug=False))
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
        """Drive one turn and yield normalized chunks.

        conn_factory(on_request, on_notification) -> AcpConnection injects the
        transport (subprocess in production, fakes in tests).
        """
        queue: asyncio.Queue = asyncio.Queue()
        usage = TokenUsage(payload_prompt_tokens=_estimate(prompt),
                           prompt_tokens_reliable=False,
                           prompt_tokens_source=self.usage_source)
        # "prompting" flips to True right before session/prompt is sent. The
        # ACP spec makes agents replay the whole conversation as session/update
        # during session/load; those replayed updates are history, not output
        # of this turn, so they are dropped.
        state = {"session_id": session_id, "prompting": False}

        async def on_notification(method: str, params: dict) -> None:
            if method == "session/update" and state["prompting"]:
                for ch in self._map_update(params.get("update") or {}, usage):
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
                            {"sessionId": session_id, "cwd": cwd or ".", "mcpServers": []})
                    except AcpError:
                        # Stale session → one recovery to a new session.
                        state["session_id"] = (await conn.request(
                            "session/new", {"cwd": cwd or ".", "mcpServers": []}))["sessionId"]
                else:
                    state["session_id"] = (await conn.request(
                        "session/new", {"cwd": cwd or ".", "mcpServers": []}))["sessionId"]
                state["prompting"] = True
                res = await conn.request("session/prompt", {
                    "sessionId": state["session_id"],
                    "prompt": [{"type": "text", "text": prompt}]})
                await queue.put((_DONE, res.get("stopReason", "")))
            except AcpError as exc:
                await queue.put((_DONE, exc))
            except Exception as exc:  # noqa: BLE001
                await queue.put((_DONE, exc))

        start = time.monotonic()
        wall_deadline = start + wall_timeout if wall_timeout is not None else None
        driver = asyncio.create_task(drive())
        idle = idle_timeout if idle_timeout is not None else timeout
        text_parts: list[str] = []
        try:
            while True:
                if wall_deadline is not None:
                    remaining = wall_deadline - time.monotonic()
                    if remaining <= 0:
                        yield StreamChunk(type="error", content="wall timeout expired",
                                          data={"error_type": "timeout", "timeout_kind": "wall"})
                        return
                    wall_limited = remaining <= idle
                    effective = min(idle, remaining)
                else:
                    wall_limited = False
                    effective = idle
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=effective)
                except asyncio.TimeoutError:
                    if wall_limited or (wall_deadline is not None and time.monotonic() >= wall_deadline):
                        yield StreamChunk(type="error", content="wall timeout expired",
                                          data={"error_type": "timeout", "timeout_kind": "wall"})
                    else:
                        yield StreamChunk(type="error", content=f"idle timeout: {idle}s",
                                          data={"error_type": "timeout", "timeout_kind": "idle"})
                    return
                if isinstance(item, StreamChunk):
                    if item.type == "text":
                        text_parts.append(item.content)
                    yield item
                    continue
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
            if not driver.done():
                driver.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await driver

    async def _handle_agent_request(self, method: str, params: dict,
                                    cwd: str | None) -> dict:
        """agent→client reverse requests."""
        if method == "session/request_permission":
            return self._decide_permission(params)
        if method == "fs/read_text_file":
            return self._fs_read(params, cwd)
        if method == "fs/write_text_file":
            return self._fs_write(params, cwd)
        return {}  # terminal/* and other unsupported reverse requests

    def _decide_permission(self, params: dict) -> dict:
        options = params.get("options") or []
        tool_title = (params.get("toolCall") or {}).get("title", "")
        allowed = self._trust_all or (tool_title in self._trust_tools)
        if allowed:
            for opt in options:
                if str(opt.get("kind", "")).startswith("allow") or opt.get("optionId") == "allow":
                    return {"outcome": {"outcome": "selected", "optionId": opt.get("optionId")}}
            for opt in options:
                if not str(opt.get("kind", "")).startswith("reject") and opt.get("optionId") != "reject":
                    return {"outcome": {"outcome": "selected", "optionId": opt.get("optionId")}}
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
            logger.warning("%s fs/read 거부", self.provider_id)
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
            logger.warning("%s fs/write 거부", self.provider_id)
            raise PermissionError("File write outside the workspace is denied")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {}
```

Before saving, diff each moved method against the current `kiro.py` (lines 79–481). Bodies must be identical except: names `kiro-cli`/`kiro` replaced by `self.binary_name`/`self.provider_id`, the `"kiro_cli_reported"` literal in `_acp_turn` replaced by `self.usage_source`, `_map_session_update` call replaced by `self._map_update`, the `prompting` flag, and the invoke body moved into `_invoke` (plus `response.reasoning = reasoning`). If the current file differs from what is shown here (someone changed kiro.py), keep the current body and apply only those listed differences.

- [ ] **Step 2: Rewrite `agentcli/providers/kiro.py`**

```python
"""Kiro CLI provider — `kiro-cli acp` over ACP (JSON-RPC 2.0 on stdio).

The turn driver lives in acp_base.AcpProvider. Field names follow the ACP spec;
not verified against a real kiro-cli.
"""
import asyncio  # noqa: F401  (tests patch agentcli.providers.kiro.asyncio.wait_for)
import shutil
import time  # noqa: F401  (tests patch agentcli.providers.kiro.time.*)

from ..auth import AuthProfileError
from ..auth_context import current_auth_profile
from ..types import ERROR_BINARY_MISSING, ProviderHealth
from .acp_base import AcpProvider, _map_session_update  # noqa: F401  (re-export)
from .base import run_health_command

# Model selector passes through non-strictly; extend when kiro-cli exposes IDs.
KIRO_MODELS = [
    {"id": "", "name": "기본", "aliases": ["default"]},
]


class KiroProvider(AcpProvider):
    provider_id = "kiro"
    binary_name = "kiro-cli"
    usage_source = "kiro_cli_reported"

    def __init__(self, *, trust_all: bool = True,
                 trust_tools: list[str] | None = None,
                 model: str = "", agent: str = "",
                 allow_file_reads: bool = True,
                 allow_file_writes: bool | None = None,
                 max_file_bytes: int = 1024 * 1024):
        """
        Args:
            trust_all: auto-approve every session/request_permission (dev default).
                **WARNING**: the default True approves every tool call. For
                multi-tenant or untrusted embedding use False + trust_tools.
            trust_tools: tool names auto-approved when trust_all=False.
            model: default model selector (the call argument wins).
            agent: kiro agent name (optional).
        """
        super().__init__(trust_all=trust_all, trust_tools=trust_tools, model=model,
                         allow_file_reads=allow_file_reads,
                         allow_file_writes=allow_file_writes,
                         max_file_bytes=max_file_bytes)
        self._agent = agent

    def _command(self, binary: str, model: str, reasoning) -> list[str]:
        cmd = [binary, "acp"]
        if self._agent:
            cmd += ["--agent", self._agent]
        return cmd

    def list_models(self) -> list[dict]:
        return list(KIRO_MODELS)

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        if current_auth_profile() is not None:
            raise AuthProfileError("Kiro service auth profiles are not supported")
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
```

Keep the existing Korean docstring of `__init__` if the reviewer prefers; behavior is what matters. Compare with the old `health_check` (old lines 235–252): it must be byte-for-byte the same logic.

- [ ] **Step 3: Run the replay test and the whole Kiro/ACP suite**

Run: `pytest tests/test_acp_base.py tests/test_kiro_provider.py tests/test_kiro_coverage.py tests/test_kiro_security.py tests/test_acp_connection.py tests/test_capabilities.py tests/test_registry.py -q`
Expected: all pass, including the replay test from Task 1. No Kiro test file was edited.

- [ ] **Step 4: Run the full suite**

Run: `pytest -q`
Expected: same pass/skip counts as before the change plus the 1 new test. If anything else fails, it imported a moved private name from `kiro`; re-export that name from `kiro.py` rather than editing the test.

- [ ] **Step 5: Commit**

```
git add agentcli/providers/acp_base.py agentcli/providers/kiro.py
git commit -m "refactor(acp): extract AcpProvider base; drop session/load replay from turn output"
```

---

### Task 3: Effort and thinking maps for `exaone`

**Files:**
- Modify: `agentcli/reasoning.py` (`_EFFORT_MAP`, `_THINKING_MAP`)
- Create: `tests/test_exaone_reasoning.py`

- [ ] **Step 1: Write the failing tests**

```python
from agentcli.reasoning import effort_levels, resolve_effort, resolve_thinking, thinking_levels


def test_exaone_effort_mapping_rounds_up():
    got = {lvl: (resolve_effort("exaone", lvl).applied, resolve_effort("exaone", lvl).clamped)
           for lvl in ("minimal", "low", "medium", "high", "xhigh", "max")}
    assert got == {"minimal": ("low", True), "low": ("low", False),
                   "medium": ("high", True), "high": ("high", False),
                   "xhigh": ("max", True), "max": ("max", False)}
    assert effort_levels("exaone") == frozenset({"low", "high", "max"})


def test_exaone_thinking_is_reported_unsupported_not_raised():
    res = resolve_thinking("exaone", "detailed")
    assert res.supported is False and res.applied == ""
    assert thinking_levels("exaone") == frozenset()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_exaone_reasoning.py -q`
Expected: FAIL — `resolve_effort("exaone", ...)` returns `supported=False`, `applied=""`.

- [ ] **Step 3: Add the rows in `agentcli/reasoning.py`**

In `_EFFORT_MAP`, after the `"codex"` entry:
```python
    # EXAONE (unofficial, via exaone-cli) has low/high/max. medium rounds up:
    # equidistant from low and high, and under-reasoning is the worse failure.
    "exaone":  {"minimal": ("low", True), "low": ("low", False),
                "medium": ("high", True), "high": ("high", False),
                "xhigh": ("max", True), "max": ("max", False)},
```
In `_THINKING_MAP`, after the `"codex"` entry:
```python
    # EXAONE always reasons and always streams it; no visibility toggle.
    "exaone":  {},
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_exaone_reasoning.py tests/test_reasoning.py tests/test_client_reasoning.py -q`
Expected: all pass (the import-time full-scale assert in `reasoning.py` also passes).

- [ ] **Step 5: Commit**

```
git add agentcli/reasoning.py tests/test_exaone_reasoning.py
git commit -m "feat(reasoning): effort map for the unofficial exaone provider"
```

---

### Task 4: `AuthProfile` support for `exaone`

**Files:**
- Modify: `agentcli/auth.py`
- Modify: `agentcli/client.py` (rotation check, around line 461)
- Create: `tests/test_exaone_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

import pytest

from agentcli import AuthProfile, AuthProfileError


def prof(tmp_path, name="svc", **kw):
    return AuthProfile(name, str(tmp_path / name), **kw).prepare()


def test_exaone_environment_uses_profile_key_and_private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAONE_API_KEY", "HOST_KEY")
    monkeypatch.setenv("EXAONE_BASE_URL", "https://host-proxy.example/v1")
    monkeypatch.setenv("EXAONE_HOME", "/host/home")
    p = prof(tmp_path, exaone_api_key=" PROFILE_KEY ")
    env = p.environment("exaone")
    assert env["EXAONE_API_KEY"] == "PROFILE_KEY"
    assert env["EXAONE_HOME"] == str(Path(p.root) / "exaone")
    assert "EXAONE_BASE_URL" not in env
    assert (Path(p.root) / "exaone").is_dir()
    assert p.method("exaone") == "api_key"


def test_exaone_key_is_required(tmp_path):
    with pytest.raises(AuthProfileError):
        prof(tmp_path).environment("exaone")


@pytest.mark.parametrize("name", ["EXAONE_API_KEY", "EXAONE_BASE_URL", "EXAONE_HOME", "exaone_api_key"])
def test_extra_env_cannot_set_exaone_variables(tmp_path, name):
    with pytest.raises(AuthProfileError):
        AuthProfile("svc", str(tmp_path / "x"), extra_env={name: "v"})


def test_exaone_can_be_disabled(tmp_path):
    p = prof(tmp_path, exaone_api_key="K", disabled_providers={"exaone"})
    with pytest.raises(AuthProfileError):
        p.environment("exaone")


def test_exaone_key_never_in_repr_or_public_dict(tmp_path):
    p = prof(tmp_path, exaone_api_key="SECRET_K")
    assert "SECRET_K" not in repr(p)
    assert "SECRET_K" not in str(p.public_dict())


@pytest.mark.parametrize("bad", ["", "  ", "a\nb"])
def test_invalid_exaone_key_rejected(tmp_path, bad):
    with pytest.raises(AuthProfileError):
        AuthProfile("svc", str(tmp_path / "y"), exaone_api_key=bad)


def test_profile_prepared_before_exaone_existed_needs_prepare_again(tmp_path):
    p = prof(tmp_path, exaone_api_key="K")
    (Path(p.root) / "exaone").rmdir()
    with pytest.raises(AuthProfileError):
        p.environment("exaone")
    p.prepare()
    assert p.environment("exaone")["EXAONE_API_KEY"] == "K"


def test_rotation_keeps_exaone_method(tmp_path):
    from dataclasses import replace

    from agentcli import LLMClient, MemoryStore
    from agentcli.providers.registry import ProviderRegistry
    p = prof(tmp_path, exaone_api_key="K1")
    client = LLMClient(MemoryStore(), ProviderRegistry(), auth_profile=p)
    client.set_auth_profile(replace(p, exaone_api_key="K2"))  # same method: allowed
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_exaone_auth.py -q`
Expected: FAIL — `TypeError: AuthProfile.__init__() got an unexpected keyword argument 'exaone_api_key'`.

- [ ] **Step 3: Modify `agentcli/auth.py`**

1. `_AUTH_PREFIXES` gains `"EXAONE_"`:
```python
_AUTH_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "AGENTCLI_CLAUDE_", "COPILOT_", "CODEX_", "EXAONE_")
```
2. New field, last in the dataclass (after `extra_env`, so positional construction is unchanged):
```python
    exaone_api_key: str | None = field(default=None, repr=False)
```
3. In `__post_init__`, the disabled-provider set and the credential loop:
```python
        if not disabled.issubset({"claude", "codex", "copilot", "exaone"}):
```
```python
        for name in ("claude_oauth_token", "claude_api_key", "codex_api_key", "github_token",
                     "exaone_api_key"):
```
4. `directory()` and the `prepare()` loop accept `"exaone"`:
```python
        if provider not in ("claude", "codex", "copilot", "exaone", "gh", "home"):
```
```python
        for provider in ("claude", "codex", "copilot", "exaone", "gh", "home"):
```
5. `method()` — before the final `raise`:
```python
        if provider == "exaone":
            return "api_key"
```
6. `validate()` — add a branch after the copilot branch, before `return {}`:
```python
        elif provider == "exaone" and self.exaone_api_key is None:
            raise AuthProfileError("Service EXAONE API key is required; the host EXAONE_API_KEY is not used")
```
7. `_environment()` — add after the copilot branch:
```python
        elif provider == "exaone":
            env["EXAONE_HOME"] = str(self.directory("exaone"))
            env["EXAONE_API_KEY"] = self.exaone_api_key
```
(`validate()` already ran `_check_private(self.directory("exaone"))`; `EXAONE_BASE_URL` is not in `_BASE_ENV`, so it is never copied from the host.)

- [ ] **Step 4: Modify `agentcli/client.py` rotation check**

In `set_auth_profile`, change the provider tuple:
```python
               for pid in ("claude", "codex", "copilot", "exaone")):
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_exaone_auth.py tests/test_service_auth.py -q`
Expected: all pass. If an existing service-auth test lists the exact set of prepared directories, update its expectation to include `exaone` — that is an intended change; note it in the commit message.

- [ ] **Step 6: Commit**

```
git add agentcli/auth.py agentcli/client.py tests/test_exaone_auth.py
git commit -m "feat(auth): exaone_api_key and private EXAONE_HOME in service profiles"
```

---

### Task 5: `ExaoneProvider`

**Files:**
- Create: `agentcli/providers/exaone.py`
- Create: `tests/test_exaone_provider.py`

- [ ] **Step 1: Write the failing tests**

```python
"""ExaoneProvider against a fake `exaone acp` launcher (no network)."""
import asyncio
import json

import pytest

from agentcli import AuthProfile, LLMClient, MemoryStore
from agentcli.providers.exaone import ExaoneProvider
from agentcli.providers.registry import ProviderRegistry, create_default_registry
from agentcli.types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_NETWORK, ERROR_TIMEOUT,
                            ERROR_USAGE_LIMIT, Message)
from tests._stream_helpers import write_fake_cli_launcher

NEW_ID = "11111111-1111-1111-1111-111111111111"
KNOWN_ID = "22222222-2222-2222-2222-222222222222"
STALE_ID = "33333333-3333-3333-3333-333333333333"

_FAKE = '''
import json, os, sys
LOG = __LOG__
MODE = __MODE__
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "key": os.environ.get("EXAONE_API_KEY"),
                         "home": os.environ.get("EXAONE_HOME"),
                         "base": os.environ.get("EXAONE_BASE_URL"),
                         "gh": os.environ.get("GITHUB_TOKEN")}) + "\\n")

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def update(sid, kind, text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
          "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}}}})

while True:
    raw = sys.stdin.readline()
    if not raw:
        break
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1,
              "agentCapabilities": {"loadSession": True}, "authMethods": []}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "__NEW__"}})
    elif method == "session/load":
        if params.get("sessionId") != "__KNOWN__":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "session not found"}})
        else:
            update(params["sessionId"], "user_message_chunk", "old q")
            update(params["sessionId"], "agent_message_chunk", "OLD")
            send({"jsonrpc": "2.0", "id": rid, "result": None})
    elif method == "session/prompt":
        sid = params["sessionId"]
        if MODE != "ok":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": MODE,
                  "data": {"kind": "x"}}})
            continue
        update(sid, "agent_thought_chunk", "thinking...")
        update(sid, "agent_message_chunk", "Hello")
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
              "update": {"sessionUpdate": "usage_update", "used": 12,
                         "_meta": {"completion_tokens": 5, "reasoning_tokens": 2}}}})
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
'''


@pytest.fixture
def fake_exaone(tmp_path):
    def make(mode="ok"):
        log = tmp_path / "calls.jsonl"
        script = (_FAKE.replace("__LOG__", repr(str(log))).replace("__MODE__", repr(mode))
                  .replace("__NEW__", NEW_ID).replace("__KNOWN__", KNOWN_ID))
        return write_fake_cli_launcher(tmp_path, script, name="exaone"), log
    return make


@pytest.fixture(autouse=True)
def host_env(monkeypatch):
    monkeypatch.setenv("EXAONE_API_KEY", "HOST_KEY")
    monkeypatch.delenv("EXAONE_BASE_URL", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def calls(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def provider(binary, **kw):
    p = ExaoneProvider(**kw)
    p._find_binary = lambda: binary
    return p


def stream(p, **kw):
    async def collect():
        return [c async for c in p.stream_async([Message(role="user", content="hi")], **kw)]
    return asyncio.run(collect())


def test_new_session_streams_thinking_text_usage_and_reports_clamp(fake_exaone, tmp_path):
    binary, log = fake_exaone()
    chunks = stream(provider(binary, effort="medium"), cwd=str(tmp_path))
    assert chunks[0].type == "event"
    assert chunks[0].data["reasoning"]["effort"] == {
        "requested": "medium", "applied": "high", "clamped": True, "supported": True}
    assert [(c.type, c.content) for c in chunks if c.type in ("thinking", "text")] == [
        ("thinking", "thinking..."), ("text", "Hello")]
    done = chunks[-1]
    assert done.type == "done" and done.content == "Hello" and done.session_id == NEW_ID
    assert (done.usage.prompt_tokens, done.usage.completion_tokens, done.usage.total_tokens) == (12, 5, 17)
    assert done.usage.prompt_tokens_source == "exaone_cli_reported"
    call = calls(log)[0]
    assert call["argv"] == ["acp", "--model", "k-exaone_v2.5", "--reasoning-effort", "high"]
    assert call["key"] == "HOST_KEY" and call["gh"] is None


def test_resume_drops_replayed_history(fake_exaone, tmp_path):
    binary, _ = fake_exaone()
    chunks = stream(provider(binary), session_id=KNOWN_ID, cwd=str(tmp_path))
    assert [c.content for c in chunks if c.type == "text"] == ["Hello"]
    assert chunks[-1].session_id == KNOWN_ID


def test_stale_session_recovers_with_new_session(fake_exaone, tmp_path):
    binary, _ = fake_exaone()
    chunks = stream(provider(binary), session_id=STALE_ID, cwd=str(tmp_path))
    assert chunks[-1].type == "done" and chunks[-1].session_id == NEW_ID


def test_invoke_folds_and_reports_reasoning(fake_exaone, tmp_path):
    binary, log = fake_exaone()
    r = provider(binary).invoke([Message(role="user", content="hi")], effort="max",
                                thinking="detailed", cwd=str(tmp_path))
    assert (r.content, r.error, r.session_id) == ("Hello", "", NEW_ID)
    assert r.reasoning.effort.applied == "max"
    assert r.reasoning.thinking.supported is False
    assert calls(log)[-1]["argv"][-2:] == ["--reasoning-effort", "max"]


def test_no_effort_means_no_flag_and_model_override(fake_exaone, tmp_path):
    binary, log = fake_exaone()
    p = provider(binary)
    p.invoke([Message(role="user", content="hi")], cwd=str(tmp_path))
    p.invoke([Message(role="user", content="hi")], model="other-model", cwd=str(tmp_path))
    assert calls(log)[0]["argv"] == ["acp", "--model", "k-exaone_v2.5"]
    assert calls(log)[1]["argv"] == ["acp", "--model", "other-model"]


@pytest.mark.parametrize("message,expected", [
    ("authentication required: EXAONE_API_KEY is not set", ERROR_AUTH),
    ("authentication failed (HTTP 401)", ERROR_AUTH),
    ("rate limit exceeded (HTTP 429)", ERROR_USAGE_LIMIT),
    ("timeout: no data for 300s", ERROR_TIMEOUT),
    ("network error: ConnectionRefusedError", ERROR_NETWORK),
])
def test_exaone_cli_error_prefixes_classify(fake_exaone, tmp_path, message, expected):
    binary, _ = fake_exaone(mode=message)
    r = provider(binary).invoke([Message(role="user", content="hi")], cwd=str(tmp_path))
    assert r.error == message and r.error_type == expected


def test_binary_missing():
    p = ExaoneProvider()
    p._find_binary = lambda: None
    r = p.invoke([Message(role="user", content="hi")])
    assert (r.error, r.error_type) == ("exaone not found", ERROR_BINARY_MISSING)


def test_not_in_default_registry():
    reg = create_default_registry()
    assert reg.get("exaone") is None
    assert "exaone" not in reg.get_fallback_chain()


def test_capabilities_are_honest():
    caps = ExaoneProvider().capabilities()
    assert caps.sessions and caps.streaming
    assert not caps.token_streaming and not caps.session_liveness and not caps.debug
    assert caps.options == frozenset({"effort", "thinking"})
    assert caps.effort_levels == frozenset({"low", "high", "max"})
    assert caps.notes.startswith("unofficial")


def test_defaults_grant_no_tools_or_files():
    p = ExaoneProvider()
    assert p._trust_all is False and p._allow_file_reads is False and p._allow_file_writes is False


def test_service_profile_reaches_child(fake_exaone, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAONE_BASE_URL", "https://host-proxy.example/v1")
    binary, log = fake_exaone()
    auth = AuthProfile("svc", str(tmp_path / "svc"), exaone_api_key="PROFILE_KEY").prepare()
    reg = ProviderRegistry()
    reg.register(provider(binary))
    ws = tmp_path / "ws"
    ws.mkdir()
    r = LLMClient(MemoryStore(), reg, auth_profile=auth).chat("hi", provider="exaone", cwd=str(ws))
    assert r.error == "" and r.content == "Hello"
    call = calls(log)[-1]
    assert call["key"] == "PROFILE_KEY"
    assert call["home"] == str((tmp_path / "svc").resolve() / "exaone")
    assert call["base"] is None


def test_health_without_key_is_auth_required(fake_exaone, monkeypatch):
    monkeypatch.delenv("EXAONE_API_KEY")
    binary, _ = fake_exaone()
    h = provider(binary).health_check()
    assert (h.ok, h.status, h.auth_ok, h.error_type) == (False, "auth_required", False, ERROR_AUTH)


def test_health_with_key_is_ok(fake_exaone):
    binary, _ = fake_exaone()
    h = provider(binary).health_check()
    assert (h.ok, h.status, h.auth_ok) == (True, "ok", True)


def test_health_binary_missing():
    p = ExaoneProvider()
    p._find_binary = lambda: None
    h = p.health_check()
    assert (h.ok, h.status, h.error_type) == (False, "binary_missing", ERROR_BINARY_MISSING)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_exaone_provider.py -q`
Expected: `ModuleNotFoundError: No module named 'agentcli.providers.exaone'`.

- [ ] **Step 3: Implement `agentcli/providers/exaone.py`**

```python
"""Unofficial EXAONE provider — drives the separate `exaone` CLI (exaone-cli) over ACP.

Not an LG AI Research product. exaone-cli owns the HTTP API, the API key and the
conversation history; this provider only runs `exaone acp` turns. Opt-in: it is
not part of create_default_registry().
"""
import asyncio
import os
from contextlib import aclosing
from typing import AsyncIterator

from ..auth import AuthProfileError
from ..auth_context import provider_environment
from ..reasoning import needs_event
from ..reasoning import to_dict as reasoning_to_dict
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, LLMResponse, Message, ProviderHealth,
                     StreamChunk, TokenUsage)
from .acp_base import AcpProvider
from .base import health_from_response, run_health_command, validate_reasoning_defaults

EXAONE_MODELS = [
    {"id": "k-exaone_v2.5", "name": "K-EXAONE 2.5", "aliases": ["default"]},
]
UNOFFICIAL_NOTE = "unofficial: wraps exaone-cli (not an LG AI Research product)"


class ExaoneProvider(AcpProvider):
    provider_id = "exaone"
    binary_name = "exaone"
    usage_source = "exaone_cli_reported"

    def __init__(self, *, model: str = "k-exaone_v2.5", effort: str | None = None,
                 trust_all: bool = False, trust_tools: list[str] | None = None,
                 allow_file_reads: bool = False, allow_file_writes: bool = False,
                 max_file_bytes: int = 1024 * 1024):
        """
        Args:
            model: default model ID passed as `--model` (the call argument wins).
            effort: default normalized effort (`minimal`..`max`), mapped to
                exaone-cli `--reasoning-effort low|high|max`.
            trust_all / trust_tools / allow_file_*: ACP tool and file callbacks.
                exaone-cli stage 1 requests none, so everything is off by default.
        """
        super().__init__(trust_all=trust_all, trust_tools=trust_tools, model=model,
                         allow_file_reads=allow_file_reads,
                         allow_file_writes=allow_file_writes,
                         max_file_bytes=max_file_bytes)
        self._effort, self._thinking = validate_reasoning_defaults(self.provider_id, effort, None)

    # -- hooks --------------------------------------------------------------
    def _command(self, binary: str, model: str, reasoning) -> list[str]:
        cmd = [binary, "acp"]
        if model:
            cmd += ["--model", model]
        effort = reasoning.effort if reasoning is not None else None
        if effort is not None and effort.applied:
            cmd += ["--reasoning-effort", effort.applied]
        return cmd

    def _legacy_environment(self) -> dict:
        # Host env as is; unlike build_env() this never runs `gh auth token`,
        # so no GitHub token is handed to exaone-cli.
        return dict(os.environ)

    def _map_update(self, update: dict, usage: TokenUsage) -> list[StreamChunk]:
        chunks = super()._map_update(update, usage)
        meta = update.get("_meta")
        if update.get("sessionUpdate") == "usage_update" and isinstance(meta, dict):
            completion = meta.get("completion_tokens")
            if isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0:
                usage.completion_tokens = completion
                usage.total_tokens = usage.prompt_tokens + completion
        return chunks

    # -- public surface (adds effort / thinking) -------------------------------
    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "", cwd: str | None = None,
               effort: str | None = None, thinking: str | None = None) -> LLMResponse:
        """Synchronous invoke — must NOT be called from inside a running event loop."""
        return asyncio.run(self.invoke_async(
            messages, model=model, timeout=timeout, session_id=session_id, cwd=cwd,
            effort=effort, thinking=thinking))

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "", cwd: str | None = None,
                           effort: str | None = None,
                           thinking: str | None = None) -> LLMResponse:
        _, _, reasoning = self._resolve_reasoning(effort, thinking)
        return await self._invoke(messages, model=model, timeout=timeout,
                                  session_id=session_id, cwd=cwd, reasoning=reasoning)

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "", cwd: str | None = None,
                           idle_timeout: int | None = None,
                           wall_timeout: float | None = None,
                           effort: str | None = None,
                           thinking: str | None = None) -> AsyncIterator[StreamChunk]:
        _, _, reasoning = self._resolve_reasoning(effort, thinking)
        if reasoning is not None and needs_event(reasoning):
            yield StreamChunk(type="event", data={"reasoning": reasoning_to_dict(reasoning),
                                                  "provider": self.provider_id})
        async with aclosing(self._stream(
                messages, model=model, timeout=timeout, session_id=session_id, cwd=cwd,
                idle_timeout=idle_timeout, wall_timeout=wall_timeout,
                reasoning=reasoning)) as stream:
            async for chunk in stream:
                yield chunk

    def list_models(self) -> list[dict]:
        return list(EXAONE_MODELS)

    def capabilities(self, *, capability_notes: str = ""):
        return super().capabilities(capability_notes=capability_notes or UNOFFICIAL_NOTE)

    def health_check(self, *, timeout: int = 10, cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        binary = self._find_binary()
        if not binary:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="binary_missing",
                available=False, auth_ok=False, error_type=ERROR_BINARY_MISSING,
                message="exaone not found")
        try:
            env = provider_environment(self.provider_id, self._legacy_environment)
        except AuthProfileError as exc:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="auth_required",
                available=True, binary=binary, auth_ok=False, error_type=ERROR_AUTH,
                message=str(exc))
        version_proc = run_health_command([binary, "--version"], timeout=timeout, env=env)
        version = (version_proc.stdout or version_proc.stderr).strip()
        if not (env.get("EXAONE_API_KEY") or "").strip():
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="auth_required",
                available=True, binary=binary, version=version, auth_ok=False,
                error_type=ERROR_AUTH, message="EXAONE_API_KEY not set")
        if probe:
            resp = self.invoke([Message(role="user", content="Reply exactly OK.")],
                               timeout=timeout, cwd=cwd)
            return health_from_response(self.provider_id, resp, binary=binary, version=version)
        return ProviderHealth(
            provider=self.provider_id, ok=True, status="ok", available=True,
            binary=binary, version=version, auth_ok=True,
            message="EXAONE_API_KEY configured; use probe=True to verify it")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_exaone_provider.py -q`
Expected: all pass. If `test_capabilities_are_honest` shows extra options, compare with `LLMProvider._COMMON_CALL_ARGS`; only `effort` and `thinking` are provider-specific here.

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```
git add agentcli/providers/exaone.py tests/test_exaone_provider.py
git commit -m "feat(providers): unofficial opt-in ExaoneProvider over exaone-cli ACP"
```

---

### Task 6: Docs (Korean/English pair) and CHANGELOG

**Files:**
- Modify: `README.md`, `README.ko.md`, `docs/service-auth.md`, `docs/service-auth.ko.md`, `CHANGELOG.md`

- [ ] **Step 1: README capability table — add an `exaone (unofficial)` column**

In `README.md`, the table that starts `| Capability | claude | codex | copilot | kiro |` becomes (append one cell per row):

```markdown
| Capability | claude | codex | copilot | kiro | exaone (unofficial) |
|---|---|---|---|---|---|
| `sessions` (resume) | ✅ | ✅ | ✅ | ✅ | ✅ |
| `streaming` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `token_streaming` | ✅ (`partial_messages`) | ❌ (block) | ✅ (native delta) | ❌ | ❌ |
| `session_recovery` (auto-reopen) | ✅ | ✅ | ✅ | ❌ | ❌ |
| `session_liveness` (`session_alive`) | ✅ | ✅ | ❌ | ❌ | ❌ |
| `debug` (chunk-timeline trace) | ✅ | ✅ | ✅ | ❌ | ❌ |
| `effort` (reasoning intensity) | `low`–`max` | `minimal`–`high` (`xhigh`/`max`→`high`) | `minimal`–`max` | — | `low`/`high`/`max` (`minimal`→`low`, `medium`→`high`, `xhigh`→`max`) |
| `thinking` (visibility) | — | `off`/`concise`/`detailed` | boolean (`concise`=`detailed`) | — | — (always streamed) |
| claude-only options | `env` (tier), `lean`, `isolated`, `partial_messages` | — | — | — | — |
```

`README.ko.md` table (around line 597), same change with the Korean row labels kept as they are:

```markdown
| Capability | claude | codex | copilot | kiro | exaone (비공식) |
|---|---|---|---|---|---|
| `sessions` (resume) | ✅ | ✅ | ✅ | ✅ | ✅ |
| `streaming` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `token_streaming` | ✅ (`partial_messages`) | ❌ (블록) | ✅ (네이티브 delta) | ❌ | ❌ |
| `session_recovery` (자동 재개) | ✅ | ✅ | ✅ | ❌ | ❌ |
| `session_liveness` (`session_alive`) | ✅ | ✅ | ❌ | ❌ | ❌ |
| `debug` (청크 타임라인 trace) | ✅ | ✅ | ✅ | ❌ | ❌ |
| `effort` (입력 다이얼) | `low`–`max` | `minimal`–`high` (`xhigh`/`max`→`high`) | `minimal`–`max` | — | `low`/`high`/`max` (`minimal`→`low`, `medium`→`high`, `xhigh`→`max`) |
| `thinking` (출력 가시성) | — | `off`/`concise`/`detailed` | boolean (`concise`=`detailed`) | — | — (항상 스트리밍) |
| claude 전용 옵션 | `env`(티어), `lean`, `isolated`, `partial_messages` | — | — | — | — |
```

- [ ] **Step 2: README — "Unofficial providers" subsection**

In `README.md`, directly after the paragraph that starts ``` `KiroProvider` drives `kiro-cli acp` ``` (around line 510), insert:

````markdown
#### Unofficial providers

`ExaoneProvider` (`provider_id="exaone"`) is **unofficial and opt-in**. K-EXAONE
has no vendor CLI, so it drives [exaone-cli](https://github.com/saintiron/exaone-cli),
a separate unofficial CLI (not an LG AI Research product) that calls the EXAONE
API and owns the session history. agentcli talks to `exaone acp` exactly like
`kiro-cli acp`. It is not in `create_default_registry()` or the fallback order:

```python
from agentcli.providers.exaone import ExaoneProvider
registry.register(ExaoneProvider())            # model k-exaone_v2.5 by default
client.chat("...", provider="exaone", effort="high")
```

Authentication is the `EXAONE_API_KEY` environment variable, or
`AuthProfile(exaone_api_key=...)` in service mode. Limits: conversation only (no
tools or file access yet), no per-call key, no quota lookup, no debug trace.
Tool, file and permission callbacks are off by default.
````

In `README.ko.md`, directly after the paragraph that starts ``` `KiroProvider`는 `kiro-cli acp` ``` (around line 629), insert:

````markdown
#### 비공식 provider

`ExaoneProvider`(`provider_id="exaone"`)는 **비공식이며 직접 등록해야 합니다**.
K-EXAONE에는 공식 CLI가 없어서, EXAONE API를 호출하고 세션 히스토리를 직접 관리하는
별도의 비공식 CLI [exaone-cli](https://github.com/saintiron/exaone-cli)(LG AI연구원
제품 아님)를 구동합니다. agentcli는 `kiro-cli acp`와 같은 방식으로 `exaone acp`와
통신합니다. `create_default_registry()`와 fallback 순서에는 들어가지 않습니다.

```python
from agentcli.providers.exaone import ExaoneProvider
registry.register(ExaoneProvider())            # 기본 모델 k-exaone_v2.5
client.chat("...", provider="exaone", effort="high")
```

인증은 `EXAONE_API_KEY` 환경 변수, 서비스 모드에서는
`AuthProfile(exaone_api_key=...)`입니다. 제한: 대화만 지원(도구·파일 접근 없음),
호출별 키 없음, 사용량 조회 없음, debug trace 없음. 도구·파일·권한 콜백은 기본으로 꺼져 있습니다.
````

The exaone-cli repository URL is a placeholder until the user publishes it. Before committing, ask the user for the real URL; if there is none yet, write the text without a link (`exaone-cli (separate repository)` / `exaone-cli(별도 저장소)`).

- [ ] **Step 3: service-auth docs**

`docs/service-auth.md`, Provider support table: insert before the Kiro row:
```markdown
| EXAONE (unofficial) | explicit `exaone_api_key` | `EXAONE_HOME=<root>/exaone`; host `EXAONE_BASE_URL` is not inherited |
```
and after the table's paragraph add:
```markdown
EXAONE has no login; only an explicit API key is accepted, never the host
`EXAONE_API_KEY`. Profiles prepared before this version must call `prepare()`
again to create the `exaone` directory.
```

`docs/service-auth.ko.md`, Provider 지원 표의 Kiro 행 앞에:
```markdown
| EXAONE (비공식) | 명시적 `exaone_api_key` | `EXAONE_HOME=<root>/exaone`; 호스트 `EXAONE_BASE_URL`은 상속하지 않음 |
```
표 아래 문단 뒤에:
```markdown
EXAONE에는 로그인이 없어 명시적인 API 키만 받고 호스트의 `EXAONE_API_KEY`는 쓰지
않습니다. 이 버전 이전에 준비한 프로필은 `prepare()`를 다시 호출해 `exaone`
디렉터리를 만들어야 합니다.
```

- [ ] **Step 4: CHANGELOG**

Under `## unreleased`, add to `### Added`:
```markdown
- **Unofficial, opt-in `ExaoneProvider`.** Drives the separate exaone-cli
  (`exaone acp`, ACP over stdio) for K-EXAONE 2.5. Sessions, streaming
  (`thinking` + `text`), usage, `effort` mapped to `low`/`high`/`max`
  (`medium`→`high`, `xhigh`→`max`, reported as clamped). Not registered by
  default. Service profiles gain `exaone_api_key` and a private `EXAONE_HOME`.
```
and to `### Fixed`:
```markdown
- **ACP resume no longer re-emits history.** Per the ACP spec an agent replays
  the conversation as `session/update` during `session/load`; the driver now
  ignores updates received before `session/prompt`, so a resumed Kiro (or
  EXAONE) turn returns only the new answer. The ACP driver moved to
  `providers/acp_base.py`; `KiroProvider`'s API is unchanged.
```

- [ ] **Step 5: Check the pairs and commit**

Check: every edit made to `README.md` / `docs/service-auth.md` has its counterpart in the `.ko.md` file.

```
git add README.md README.ko.md docs/service-auth.md docs/service-auth.ko.md CHANGELOG.md
git commit -m "docs: unofficial exaone provider (en/ko) and ACP replay fix"
```

---

### Task 7: Live check through agentcli (after exaone-cli stage 1; needs approval)

Do not run without the user's explicit approval; it spends tokens with their key.
The key must already be in the environment of the terminal that started the
session. Never ask for it in the chat or put it in a command.

- [ ] **Step 1:** Install exaone-cli into the same environment: `pip install -e D:\BProject\exaone-cli`. Confirm `exaone --version`.

- [ ] **Step 2:** Run (in a scratch directory outside both repos):

```python
from agentcli import LLMClient, MemoryStore
from agentcli.providers.exaone import ExaoneProvider
from agentcli.providers.registry import ProviderRegistry

reg = ProviderRegistry()
reg.register(ExaoneProvider())
client = LLMClient(MemoryStore(), reg)
first = client.chat("내 이름은 테스트야. 기억해줘.", provider="exaone", alias="live", effort="low")
second = client.chat("내 이름이 뭐라고 했지?", provider="exaone", alias="live")
print(first.error or "ok", second.error or second.content[:200])
print(first.tokens, first.reasoning)
```

Expected: both calls without error; the second answer mentions "테스트" (resume works and the replayed history did not leak into `second.content`).

- [ ] **Step 3:** Record the result (date, exaone-cli commit, outcome) in `CHANGELOG.md` under the Added entry, then commit:

```
git add CHANGELOG.md
git commit -m "docs: record live exaone check"
```
