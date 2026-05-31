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

from .base import (LLMProvider, build_session_prompt, health_from_response,
                   run_health_command, run_subprocess_async)
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, Message, LLMResponse,
                     ProviderHealth, TokenUsage, StreamChunk, classify_error)
from ..utils import build_env

logger = logging.getLogger(__name__)

COPILOT_MODELS = [
    {"id": "", "name": "기본", "aliases": ["default"]},
    {
        "id": "claude-sonnet-4.6",
        "name": "Claude Sonnet 4.6",
        "aliases": ["claude-sonnet", "sonnet"],
    },
    {"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5"},
    {"id": "claude-haiku-4.5", "name": "Claude Haiku 4.5"},
    {"id": "claude-opus-4.7", "name": "Claude Opus 4.7"},
    {"id": "claude-opus-4.6", "name": "Claude Opus 4.6"},
    {
        "id": "claude-opus-4.6-fast",
        "name": "Claude Opus 4.6 fast",
    },
    {"id": "claude-opus-4.5", "name": "Claude Opus 4.5"},
    {"id": "claude-sonnet-4", "name": "Claude Sonnet 4"},
    {"id": "gpt-5.5", "name": "GPT-5.5"},
    {"id": "gpt-5.4", "name": "GPT-5.4"},
    {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
    {"id": "gpt-5.2-codex", "name": "GPT-5.2 Codex"},
    {"id": "gpt-5.2", "name": "GPT-5.2"},
    {"id": "gpt-5.1", "name": "GPT-5.1"},
    {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"},
    {"id": "gpt-5-mini", "name": "GPT-5 mini"},
    {"id": "gpt-4.1", "name": "GPT-4.1"},
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

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        bin_path, use_gh = self._find_binary()
        if not bin_path:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="binary_missing",
                available=False, auth_ok=False,
                error_type=ERROR_BINARY_MISSING,
                message="Copilot CLI or gh CLI not found")

        version_cmd = [bin_path, "copilot", "version"] if use_gh else [bin_path, "version"]
        version_proc = run_health_command(version_cmd, timeout=timeout)
        version = (version_proc.stdout or version_proc.stderr).strip()

        if use_gh:
            auth_proc = run_health_command(
                [bin_path, "auth", "status"], timeout=timeout, cwd=cwd,
                env=build_env())
            auth_msg = ((auth_proc.stdout or "") + (auth_proc.stderr or "")).strip()
            if auth_proc.returncode != 0:
                return ProviderHealth(
                    provider=self.provider_id, ok=False, status="auth_required",
                    available=True, binary=bin_path, version=version,
                    auth_ok=False, error_type=ERROR_AUTH,
                    message=auth_msg or "GitHub authentication required",
                    raw_stdout=auth_proc.stdout, raw_stderr=auth_proc.stderr,
                    exit_code=auth_proc.returncode)
            if not probe:
                return ProviderHealth(
                    provider=self.provider_id, ok=True, status="ok",
                    available=True, binary=bin_path, version=version,
                    auth_ok=True, message=auth_msg or "GitHub authenticated")

        if probe:
            resp = self.invoke(
                [Message(role="user", content="Reply exactly OK.")],
                timeout=timeout, cwd=cwd)
            return health_from_response(
                self.provider_id, resp, binary=bin_path, version=version)
        return ProviderHealth(
            provider=self.provider_id, ok=True, status="ok", available=True,
            binary=bin_path, version=version, auth_ok=None,
            message="Copilot binary available; run with probe=True to verify auth/quota")

    def _build_cmd(self, prompt: str, model: str,
                   session_id: str,
                   output_format: str = "text",
                   alias: str = "",
                   resume_by_alias: bool = True) -> tuple[list[str] | None, bool]:
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

        if session_id:
            cmd += [f"--resume={session_id}"]
        elif alias:
            # 이름으로 기존 세션 재개를 시도하고, 없으면 같은 이름으로 새 세션을 만든다.
            cmd.append(f"--name={alias}")
            if resume_by_alias:
                cmd.append(f"--resume={alias}")

        return cmd, use_gh

    # ---------- 동기 ----------

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "",
               cwd: str | None = None,
               alias: str = "",
               resume_by_alias: bool = True) -> LLMResponse:
        prompt = build_session_prompt(messages)
        cmd, _ = self._build_cmd(prompt, model, session_id,
                                   output_format="json", alias=alias,
                                   resume_by_alias=resume_by_alias)
        if cmd is None:
            logger.error("Copilot CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                session_id=session_id or alias,
                                error="Copilot CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

        start = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, env=build_env(), cwd=cwd)
            latency = int((time.time() - start) * 1000)

            if result.returncode != 0:
                msg = (result.stderr or "").strip()[:300] or f"exit={result.returncode}"
                logger.error("Copilot 실패 (code=%d): %s",
                             result.returncode, msg)
                return LLMResponse(
                    content="", provider=self.provider_id, model=model,
                    raw_stderr=result.stderr,
                    session_id=session_id or alias,
                    error=msg, error_type=classify_error(msg),
                    exit_code=result.returncode)

            parsed = _parse_copilot_jsonl(result.stdout)
            err = parsed.get("error", "")
            return LLMResponse(
                content=parsed["text"] if not err else "",
                provider=self.provider_id, model=model,
                tokens=parsed["usage"], latency_ms=latency,
                raw_stderr=result.stderr,
                session_id=parsed["session_id"] or session_id or alias,
                error=err,
                error_type=classify_error(err) if err else "",
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.error("Copilot 타임아웃 (%d초)", timeout)
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                session_id=session_id or alias,
                                error=f"timeout after {timeout}s",
                                error_type="timeout",
                                exit_code=124)
        except FileNotFoundError:
            logger.error("Copilot CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="Copilot CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

    # ---------- 비동기 ----------

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           alias: str = "",
                           resume_by_alias: bool = True) -> LLMResponse:
        prompt = build_session_prompt(messages)
        cmd, _ = self._build_cmd(prompt, model, session_id,
                                   output_format="json", alias=alias,
                                   resume_by_alias=resume_by_alias)
        if cmd is None:
            logger.error("Copilot CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                session_id=session_id or alias,
                                error="Copilot CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

        start = time.time()
        try:
            stdout_b, stderr_b, rc, timed_out = await run_subprocess_async(
                cmd, timeout=timeout, cwd=cwd,
                env=build_env(), use_stdin_devnull=True)
        except FileNotFoundError:
            logger.error("Copilot CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                session_id=session_id or alias,
                                error="Copilot CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)
        if timed_out:
            logger.error("Copilot 타임아웃 (%d초)", timeout)
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                session_id=session_id or alias,
                                error=f"timeout after {timeout}s",
                                error_type="timeout",
                                exit_code=124)
        latency = int((time.time() - start) * 1000)
        stdout_txt = stdout_b.decode("utf-8", errors="replace")
        stderr_txt = stderr_b.decode("utf-8", errors="replace")

        if rc != 0:
            msg = stderr_txt.strip()[:300] or f"exit={rc}"
            logger.error("Copilot 실패 (code=%d): %s", rc, msg)
            return LLMResponse(
                content="", provider=self.provider_id, model=model,
                raw_stderr=stderr_txt,
                session_id=session_id or alias,
                error=msg, error_type=classify_error(msg),
                exit_code=rc)

        parsed = _parse_copilot_jsonl(stdout_txt)
        err = parsed.get("error", "")
        return LLMResponse(
            content=parsed["text"] if not err else "",
            provider=self.provider_id, model=model,
            tokens=parsed["usage"], latency_ms=latency,
            raw_stderr=stderr_txt,
            session_id=parsed["session_id"] or session_id or alias,
            error=err,
            error_type=classify_error(err) if err else "",
            exit_code=rc,
        )

    # ---------- 스트리밍 ----------

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           alias: str = "",
                           resume_by_alias: bool = True,
                           idle_timeout: int | None = None,
                           wall_timeout: int | None = None) -> AsyncIterator[StreamChunk]:
        """Copilot CLI --output-format json 스트리밍.

        정규화:
          assistant.message_delta → text (증분)
          assistant.message       → (무시, delta 합산으로 충분)
          result                  → session_id, usage 확정
          기타 session.*          → event
        """
        prompt = build_session_prompt(messages)
        cmd, _ = self._build_cmd(prompt, model, session_id,
                                   output_format="json", alias=alias,
                                   resume_by_alias=resume_by_alias)
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
            final_usage = TokenUsage(
                prompt_tokens_reliable=False,
                prompt_tokens_source="copilot_not_reported")
            final_sid = session_id or alias
            timed_out = False
            # `timeout`은 wall-clock deadline이 아니라 **마지막 청크 이후 idle 한도**.
            # 진행 청크가 들어오면 매번 last_activity 갱신.
            last_activity = time.time()
            idle_limit = idle_timeout if idle_timeout is not None else timeout
            wall_deadline = start + wall_timeout if wall_timeout else None

            assert proc.stdout
            while True:
                read_timeout = idle_limit
                if wall_deadline is not None:
                    remaining = wall_deadline - time.time()
                    if remaining <= 0:
                        proc.kill()
                        yield StreamChunk(
                            type="error",
                            content=f"wall timeout: {wall_timeout}s 초과",
                            data={"error_type": "timeout",
                                  "timeout_kind": "wall"})
                        timed_out = True
                        break
                    read_timeout = min(read_timeout, remaining)
                try:
                    line_b = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=read_timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    idle = int(time.time() - last_activity)
                    timeout_kind = (
                        "wall" if wall_deadline is not None
                        and time.time() >= wall_deadline else "idle")
                    content = (
                        f"wall timeout: {wall_timeout}s 초과"
                        if timeout_kind == "wall"
                        else f"idle timeout: {idle}s 동안 청크 없음")
                    yield StreamChunk(
                        type="error",
                        content=content,
                        data={"error_type": "timeout",
                              "timeout_kind": timeout_kind})
                    timed_out = True
                    break
                if not line_b:
                    break
                last_activity = time.time()
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
                            cached_tokens=final_usage.cached_tokens,
                            prompt_tokens_reliable=False,
                            prompt_tokens_source="copilot_not_reported")
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
                if proc and proc.returncode is None:
                    await proc.wait()
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
      - error: result.exitCode != 0 또는 error/assistant.error 이벤트의 message
    """
    text_parts: list[str] = []
    final_message_content = ""
    session_id = ""
    completion_tokens = 0
    error_msg = ""
    result_exit_code: int | None = None

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
            if evt.get("exitCode") is not None:
                result_exit_code = int(evt["exitCode"])
            # 명시적 error 메시지가 result에 담겨 올 수도
            if evt.get("error"):
                e = evt["error"]
                error_msg = e if isinstance(e, str) else str(e)
        elif etype in ("error", "assistant.error", "session.error"):
            data = evt.get("data") or {}
            msg = data.get("message") or evt.get("message") or str(data)
            if msg:
                error_msg = msg

    # delta로 모은 게 있으면 우선, 없으면 최종 message content 사용
    text = "".join(text_parts) if text_parts else final_message_content
    # exitCode != 0 이지만 별도 메시지가 없으면 일반 표기
    if not error_msg and result_exit_code not in (None, 0):
        error_msg = f"copilot exit={result_exit_code}"

    return {
        "text": text.strip(),
        "session_id": session_id,
        "usage": TokenUsage(
            prompt_tokens=0,            # Copilot 미공개
            completion_tokens=completion_tokens,
            total_tokens=completion_tokens,
            cached_tokens=0,
            prompt_tokens_reliable=False,
            prompt_tokens_source="copilot_not_reported"),
        "error": error_msg,
    }
