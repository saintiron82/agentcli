"""GitHub Copilot CLI 프로바이더 — agentic CLI (Claude Code와 동등).

JSONL 이벤트 스키마 (--output-format json 관찰 결과, v2026-04):
  - session.mcp_server_status_changed / session.mcp_servers_loaded
  - session.skills_loaded / session.tools_updated
  - user.message          {data: {content, transformedContent, interactionId}}
  - assistant.turn_start  {data: {turnId, interactionId}}
  - assistant.message_delta {data: {messageId, deltaContent}}  ← streaming
  - assistant.message     {data: {messageId, content, outputTokens, ...}}
  - assistant.turn_end    {data: {turnId}}
  - result                {sessionId, exitCode, usage:{premiumRequests, ...}}

session_id = result.sessionId (이 값이 Copilot이 발급한 진짜 UUID)
"""

import asyncio
import json
import logging
import platform
import shutil
import subprocess
import time
from typing import AsyncIterator

from .base import LLMProvider
from ..types import Message, LLMResponse, TokenUsage, StreamChunk
from ..utils import build_env

logger = logging.getLogger(__name__)

COPILOT_MODELS = [
    {"id": "", "name": "기본 (자동)"},
    {"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6"},
    {"id": "claude-sonnet", "name": "Claude Sonnet"},
    {"id": "gpt-4o", "name": "GPT-4o"},
    {"id": "gpt-5", "name": "GPT-5"},
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
]


class CopilotProvider(LLMProvider):
    provider_id = "copilot"
    supports_sessions = True
    supports_streaming = True

    def __init__(self,
                 allow_all_tools: bool = True,
                 allowed_tools: list[str] | None = None,
                 disallowed_tools: list[str] | None = None,
                 available_tools: list[str] | None = None,
                 allow_all_paths: bool = False,
                 add_dirs: list[str] | None = None,
                 effort: str | None = None):
        self._allow_all_tools = allow_all_tools
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools
        self._available_tools = available_tools
        self._allow_all_paths = allow_all_paths
        self._add_dirs = add_dirs or []
        self._effort = effort

    def _find_binary(self) -> tuple[str | None, bool]:
        bin_path = shutil.which("copilot")
        if bin_path:
            return bin_path, False
        gh_name = "gh.exe" if platform.system() == "Windows" else "gh"
        gh_path = shutil.which(gh_name)
        if gh_path:
            return gh_path, True
        return None, False

    def is_available(self) -> bool:
        path, _ = self._find_binary()
        return path is not None

    def list_models(self) -> list[dict]:
        return list(COPILOT_MODELS)

    def _build_cmd(self, prompt: str, model: str,
                   session_id: str,
                   output_format: str = "text",
                   alias: str = "") -> tuple[list[str] | None, bool]:
        """(cmd, use_gh) 반환. session_id 발급은 CLI가 담당, 우리는 stdout에서 파싱."""
        bin_path, use_gh = self._find_binary()
        if not bin_path:
            return None, False

        cmd = [bin_path]
        if use_gh:
            cmd.append("copilot")
        cmd += ["-p", prompt, "--no-color"]

        if output_format == "json":
            cmd += ["--output-format", "json"]
        else:
            cmd += ["-s"]  # silent: 에이전트 응답만

        if model and not use_gh:
            cmd += ["--model", model]
        if self._allow_all_tools:
            cmd.append("--allow-all-tools")
        if self._allowed_tools:
            for t in self._allowed_tools:
                cmd += ["--allow-tool", t]
        if self._disallowed_tools:
            for t in self._disallowed_tools:
                cmd += ["--deny-tool", t]
        if self._available_tools:
            cmd += ["--available-tools", ",".join(self._available_tools)]
        if self._allow_all_paths:
            cmd.append("--allow-all-paths")
        for d in self._add_dirs:
            cmd += ["--add-dir", d]
        if self._effort:
            cmd += ["--effort", self._effort]

        # alias → Copilot --name (CLI에서 사용자가 이름으로 재개 가능)
        if alias:
            cmd.append(f"--name={alias}")

        if session_id:
            cmd += [f"--resume={session_id}"]
        elif alias:
            # session_id가 없지만 alias가 있으면 이름 기반 재개 시도.
            # 첫 호출이면 Copilot이 새 세션을 만들고, 우리는 result.sessionId를 파싱.
            cmd += [f"--resume={alias}"]
        return cmd, use_gh

    # ---------- 동기 ----------

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "",
               cwd: str | None = None,
               alias: str = "") -> LLMResponse:
        empty = LLMResponse(content="", provider=self.provider_id, model=model)
        prompt = messages[-1].content if messages else ""
        cmd, _ = self._build_cmd(prompt, model, session_id,
                                   output_format="json", alias=alias)
        if cmd is None:
            logger.error("Copilot CLI를 찾을 수 없습니다")
            return empty

        start = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, env=build_env(), cwd=cwd)
            latency = int((time.time() - start) * 1000)

            if result.returncode != 0:
                logger.error("Copilot 실패 (code=%d): %s",
                             result.returncode, result.stderr[:300])
                return empty

            parsed = _parse_copilot_jsonl(result.stdout)
            return LLMResponse(
                content=parsed["text"],
                provider=self.provider_id, model=model,
                tokens=parsed["usage"], latency_ms=latency,
                raw_stderr=result.stderr,
                session_id=parsed["session_id"] or session_id or alias,
            )
        except subprocess.TimeoutExpired:
            logger.error("Copilot 타임아웃 (%d초)", timeout)
            return empty
        except FileNotFoundError:
            logger.error("Copilot CLI를 찾을 수 없습니다")
            return empty

    # ---------- 비동기 ----------

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           alias: str = "") -> LLMResponse:
        empty = LLMResponse(content="", provider=self.provider_id, model=model)
        prompt = messages[-1].content if messages else ""
        cmd, _ = self._build_cmd(prompt, model, session_id,
                                   output_format="json", alias=alias)
        if cmd is None:
            logger.error("Copilot CLI를 찾을 수 없습니다")
            return empty

        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=build_env(), cwd=cwd)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.error("Copilot 타임아웃 (%d초)", timeout)
                return empty
            latency = int((time.time() - start) * 1000)

            if proc.returncode != 0:
                stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace")
                logger.error("Copilot 실패 (code=%d): %s",
                             proc.returncode, stderr_txt[:300])
                return empty

            stdout_txt = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace")
            parsed = _parse_copilot_jsonl(stdout_txt)
            return LLMResponse(
                content=parsed["text"],
                provider=self.provider_id, model=model,
                tokens=parsed["usage"], latency_ms=latency,
                raw_stderr=stderr_txt,
                session_id=parsed["session_id"] or session_id or alias,
            )
        except FileNotFoundError:
            logger.error("Copilot CLI를 찾을 수 없습니다")
            return empty

    # ---------- 스트리밍 ----------

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           alias: str = "") -> AsyncIterator[StreamChunk]:
        """Copilot CLI --output-format json 스트리밍.

        정규화:
          assistant.message_delta → text (증분)
          assistant.message       → (무시, delta 합산으로 충분)
          result                  → session_id, usage 확정
          기타 session.*          → event
        """
        prompt = messages[-1].content if messages else ""
        cmd, _ = self._build_cmd(prompt, model, session_id,
                                   output_format="json", alias=alias)
        if cmd is None:
            yield StreamChunk(type="error", content="Copilot CLI not found")
            return

        start = time.time()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=build_env(), cwd=cwd)

            text_parts: list[str] = []
            final_usage = TokenUsage()
            final_sid = session_id or alias
            timed_out = False
            deadline = start + timeout

            assert proc.stdout
            while True:
                if time.time() > deadline:
                    proc.kill()
                    yield StreamChunk(type="error", content="timeout")
                    timed_out = True
                    break
                line_b = await proc.stdout.readline()
                if not line_b:
                    break
                s = line_b.decode("utf-8", errors="replace").strip()
                if not s:
                    continue
                try:
                    evt = json.loads(s)
                except json.JSONDecodeError:
                    yield StreamChunk(type="event", data={"raw": s})
                    continue

                etype = evt.get("type", "")
                if etype == "assistant.message_delta":
                    delta = ((evt.get("data") or {}).get("deltaContent") or "")
                    if delta:
                        text_parts.append(delta)
                        yield StreamChunk(type="text", content=delta, data=evt)
                elif etype == "assistant.message":
                    # 최종 메시지 — delta 누적이 부족하면 여기 content 사용
                    data = evt.get("data") or {}
                    if not text_parts and data.get("content"):
                        text_parts.append(data["content"])
                        yield StreamChunk(type="text",
                                           content=data["content"], data=evt)
                    if data.get("outputTokens"):
                        final_usage = TokenUsage(
                            prompt_tokens=final_usage.prompt_tokens,
                            completion_tokens=final_usage.completion_tokens
                                + int(data["outputTokens"]),
                            total_tokens=final_usage.total_tokens
                                + int(data["outputTokens"]),
                            cached_tokens=final_usage.cached_tokens)
                elif etype == "result":
                    sid = evt.get("sessionId")
                    if sid:
                        final_sid = sid
                elif etype and (etype.startswith("assistant.tool_")
                                or etype.startswith("tool.")):
                    yield StreamChunk(type="tool_use", data=evt)
                else:
                    yield StreamChunk(type="event", data=evt)

            if timed_out:
                return

            rc = await proc.wait()
            if rc != 0 and not text_parts:
                err_b = b""
                if proc.stderr:
                    err_b = await proc.stderr.read()
                yield StreamChunk(
                    type="error",
                    content=err_b.decode("utf-8", errors="replace")[:500],
                    data={"returncode": rc})
                return

            yield StreamChunk(
                type="done",
                content="".join(text_parts),
                session_id=final_sid, usage=final_usage,
                data={"provider": self.provider_id, "model": model,
                      "latency_ms": int((time.time() - start) * 1000)})
        except FileNotFoundError:
            yield StreamChunk(type="error", content="Copilot CLI not found")
        except Exception as e:
            logger.exception("Copilot stream 예외")
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            yield StreamChunk(type="error", content=str(e))


# ---------- JSONL 파싱 유틸 ----------

def _parse_copilot_jsonl(stdout: str) -> dict:
    """Copilot CLI --output-format json stdout 파싱.

    추출:
      - text: assistant.message.content (또는 delta 누적)
      - session_id: result.sessionId
      - usage.completion_tokens: assistant.message.outputTokens 합계
        (Copilot은 input_tokens를 공개하지 않음)
    """
    text_parts: list[str] = []
    final_message_content = ""
    session_id = ""
    completion_tokens = 0

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type", "")
        if etype == "assistant.message_delta":
            delta = ((evt.get("data") or {}).get("deltaContent") or "")
            if delta:
                text_parts.append(delta)
        elif etype == "assistant.message":
            data = evt.get("data") or {}
            if data.get("content"):
                final_message_content = data["content"]
            if data.get("outputTokens"):
                completion_tokens += int(data["outputTokens"])
        elif etype == "result":
            sid = evt.get("sessionId")
            if sid:
                session_id = sid

    # delta로 모은 게 있으면 우선, 없으면 최종 message content 사용
    text = "".join(text_parts) if text_parts else final_message_content

    return {
        "text": text.strip(),
        "session_id": session_id,
        "usage": TokenUsage(
            prompt_tokens=0,            # Copilot 미공개
            completion_tokens=completion_tokens,
            total_tokens=completion_tokens,
            cached_tokens=0),
    }
