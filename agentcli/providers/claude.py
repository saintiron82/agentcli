"""Claude Code CLI 프로바이더."""

import asyncio
import json
import logging
import platform
import shutil
import subprocess
import time
import uuid
from typing import AsyncIterator

from .base import LLMProvider
from ..types import Message, LLMResponse, TokenUsage, StreamChunk

logger = logging.getLogger(__name__)

CLAUDE_MODELS = [
    {"id": "", "name": "기본 (자동)"},
    {"id": "sonnet", "name": "Sonnet"},
    {"id": "opus", "name": "Opus"},
    {"id": "haiku", "name": "Haiku"},
]


class ClaudeProvider(LLMProvider):
    provider_id = "claude"
    supports_sessions = True
    supports_streaming = True

    def __init__(self,
                 permission_mode: str = "bypassPermissions",
                 allowed_tools: list[str] | None = None,
                 disallowed_tools: list[str] | None = None):
        """
        Args:
            permission_mode: `default`, `acceptEdits`, `plan`, `bypassPermissions` 중 하나.
                **WARNING**: 기본값 `bypassPermissions`는 에이전트에 전체 권한을 부여한다.
                신뢰할 수 없는 컨텍스트에서 임베딩할 때는 `default`로 변경할 것.
            allowed_tools: 허용 도구 목록 (예: ["Read", "Grep", "Bash"]).
                None이면 제한 없음.
            disallowed_tools: 금지 도구 목록.
        """
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools

    def _find_binary(self) -> str | None:
        executable = "claude.cmd" if platform.system() == "Windows" else "claude"
        return shutil.which(executable) or shutil.which("claude")

    def is_available(self) -> bool:
        return self._find_binary() is not None

    def list_models(self) -> list[dict]:
        return list(CLAUDE_MODELS)

    def _build_cmd(self, prompt: str, model: str,
                   session_id: str,
                   output_format: str = "json") -> tuple[list[str] | None, str]:
        """CLI 명령어와 사용한 session_id 반환. (None, "") 이면 바이너리 없음."""
        bin_path = self._find_binary()
        if not bin_path:
            return None, ""

        cmd = [bin_path, "-p", prompt,
               "--output-format", output_format,
               "--permission-mode", self._permission_mode]
        if output_format == "stream-json":
            # stream-json은 반드시 --verbose 필요 (Claude Code 제약)
            cmd.append("--verbose")
        if model:
            cmd += ["--model", model]
        if self._allowed_tools:
            cmd += ["--allowedTools", ",".join(self._allowed_tools)]
        if self._disallowed_tools:
            cmd += ["--disallowedTools", ",".join(self._disallowed_tools)]

        if session_id:
            cmd += ["--resume", session_id]
            used_session_id = session_id
        else:
            used_session_id = str(uuid.uuid4())
            cmd += ["--session-id", used_session_id]
        return cmd, used_session_id

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "",
               cwd: str | None = None) -> LLMResponse:
        empty = LLMResponse(content="", provider=self.provider_id, model=model)
        prompt = messages[-1].content if messages else ""
        cmd, used_sid = self._build_cmd(prompt, model, session_id, "json")
        if cmd is None:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return empty

        start = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, cwd=cwd)
            latency = int((time.time() - start) * 1000)

            if result.returncode != 0:
                logger.error("Claude 실패 (code=%d): %s",
                             result.returncode, result.stderr[:300])
                return empty

            content, tokens = _parse_claude_json(result.stdout)
            return LLMResponse(
                content=content,
                provider=self.provider_id, model=model,
                tokens=tokens, latency_ms=latency,
                raw_stderr=result.stderr, session_id=used_sid,
            )
        except subprocess.TimeoutExpired:
            logger.error("Claude 타임아웃 (%d초)", timeout)
            return empty
        except FileNotFoundError:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return empty

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> LLMResponse:
        empty = LLMResponse(content="", provider=self.provider_id, model=model)
        prompt = messages[-1].content if messages else ""
        cmd, used_sid = self._build_cmd(prompt, model, session_id, "json")
        if cmd is None:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return empty

        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.error("Claude 타임아웃 (%d초)", timeout)
                return empty
            latency = int((time.time() - start) * 1000)

            if proc.returncode != 0:
                stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace")
                logger.error("Claude 실패 (code=%d): %s",
                             proc.returncode, stderr_txt[:300])
                return empty

            content, tokens = _parse_claude_json(
                (stdout_b or b"").decode("utf-8", errors="replace"))
            return LLMResponse(
                content=content, provider=self.provider_id, model=model,
                tokens=tokens, latency_ms=latency,
                raw_stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
                session_id=used_sid,
            )
        except FileNotFoundError:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return empty

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> AsyncIterator[StreamChunk]:
        """Claude Code `--output-format stream-json` 기반 스트리밍.

        이벤트 예:
          {"type":"system","subtype":"init","session_id":"..."}
          {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
          {"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}
          {"type":"user","message":{"content":[{"type":"tool_result",...}]}}
          {"type":"result","subtype":"success","result":"...","usage":{...},"session_id":"..."}
        """
        prompt = messages[-1].content if messages else ""
        cmd, used_sid = self._build_cmd(prompt, model, session_id, "stream-json")
        if cmd is None:
            yield StreamChunk(type="error", content="Claude CLI not found")
            return

        start = time.time()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd)

            total_content_parts: list[str] = []
            final_usage = TokenUsage()
            final_session_id = used_sid
            timed_out = False

            deadline = start + timeout
            assert proc.stdout
            while True:
                if time.time() > deadline:
                    proc.kill()
                    yield StreamChunk(type="error", content="timeout")
                    timed_out = True
                    break
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    yield StreamChunk(type="event", data={"raw": line})
                    continue

                etype = evt.get("type", "")
                if etype == "system" and evt.get("session_id"):
                    final_session_id = evt["session_id"]
                    yield StreamChunk(type="event", data=evt,
                                      session_id=final_session_id)
                elif etype == "assistant":
                    msg = evt.get("message") or {}
                    for block in msg.get("content") or []:
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text", "")
                            if text:
                                total_content_parts.append(text)
                                yield StreamChunk(type="text", content=text,
                                                  data=block)
                        elif btype == "thinking":
                            yield StreamChunk(type="thinking",
                                              content=block.get("thinking", ""),
                                              data=block)
                        elif btype == "tool_use":
                            yield StreamChunk(type="tool_use", data=block)
                        else:
                            yield StreamChunk(type="event", data=block)
                elif etype == "user":
                    msg = evt.get("message") or {}
                    for block in msg.get("content") or []:
                        if block.get("type") == "tool_result":
                            yield StreamChunk(type="tool_result", data=block)
                        else:
                            yield StreamChunk(type="event", data=block)
                elif etype == "result":
                    usage = evt.get("usage") or {}
                    pt = int(usage.get("input_tokens") or 0)
                    ct = int(usage.get("output_tokens") or 0)
                    final_usage = TokenUsage(
                        prompt_tokens=pt, completion_tokens=ct,
                        total_tokens=pt + ct)
                    if evt.get("session_id"):
                        final_session_id = evt["session_id"]
                else:
                    yield StreamChunk(type="event", data=evt)

            if timed_out:
                return

            rc = await proc.wait()
            if rc != 0 and not total_content_parts:
                err = b""
                if proc.stderr:
                    err = await proc.stderr.read()
                yield StreamChunk(
                    type="error",
                    content=err.decode("utf-8", errors="replace")[:500],
                    data={"returncode": rc})
                return

            yield StreamChunk(
                type="done",
                content="".join(total_content_parts),
                session_id=final_session_id, usage=final_usage,
                data={"provider": self.provider_id, "model": model,
                      "latency_ms": int((time.time() - start) * 1000)})
        except FileNotFoundError:
            yield StreamChunk(type="error", content="Claude CLI not found")
        except Exception as e:
            logger.exception("Claude stream 예외")
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            yield StreamChunk(type="error", content=str(e))


def _parse_claude_json(stdout: str) -> tuple[str, TokenUsage]:
    """Claude CLI --output-format json stdout 파싱."""
    stdout = stdout.strip()
    if not stdout:
        return "", TokenUsage()
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, TokenUsage()

    content = (data.get("result")
               or data.get("content")
               or data.get("text")
               or "")
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = prompt_tokens + completion_tokens
    return str(content).strip(), TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
    )
