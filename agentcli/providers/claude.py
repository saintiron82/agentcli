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

from .base import (LLMProvider, build_session_prompt,
                   estimate_payload_prompt_tokens, health_from_response,
                   run_health_command, run_subprocess_async)
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_TIMEOUT,
                     Message, LLMResponse, ProviderHealth, TokenUsage,
                     StreamChunk, classify_error)

logger = logging.getLogger(__name__)

CLAUDE_MODELS = [
    {"id": "", "name": "기본", "aliases": ["default"]},
    {"id": "best", "name": "Best available"},
    {"id": "claude-opus-4-7", "name": "Claude Opus 4.7"},
    {"id": "claude-opus-4-7[1m]", "name": "Claude Opus 4.7 1M context"},
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
    {"id": "claude-sonnet-4-6[1m]", "name": "Claude Sonnet 4.6 1M context"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
    {
        "id": "sonnet",
        "name": "Sonnet",
    },
    {"id": "sonnet[1m]", "name": "Sonnet 1M context"},
    {
        "id": "opus",
        "name": "Opus",
    },
    {"id": "opus[1m]", "name": "Opus 1M context"},
    {"id": "opusplan", "name": "Opus plan mode"},
    {
        "id": "haiku",
        "name": "Haiku",
    },
]


class ClaudeProvider(LLMProvider):
    provider_id = "claude"
    # `claude -p`는 single-shot/stateless이며 `--resume`은 인터랙티브 모드 전용이다.
    # 두 모드를 같이 호출하면 Windows에서 5분+ hang (issue #4). 라이브러리는
    # claude를 stateless로 선언하여 상위 client가 session_id를 저장·재생하지
    # 않도록 한다. `--session-id <new-uuid>`는 새 세션 식별자 부여 용도로 계속
    # 사용된다 (resume과 무관).
    supports_sessions = False
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

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        bin_path = self._find_binary()
        if not bin_path:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="binary_missing",
                available=False, auth_ok=False,
                error_type=ERROR_BINARY_MISSING,
                message="Claude CLI not found")

        version_proc = run_health_command([bin_path, "--version"], timeout=timeout)
        version = (version_proc.stdout or version_proc.stderr).strip()
        auth_proc = run_health_command(
            [bin_path, "auth", "status"], timeout=timeout, cwd=cwd)
        auth_msg = ((auth_proc.stdout or "") + (auth_proc.stderr or "")).strip()
        if auth_proc.returncode == 124:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="timeout",
                available=True, binary=bin_path, version=version,
                auth_ok=None, error_type=ERROR_TIMEOUT,
                message=auth_msg or f"claude auth status timed out after {timeout}s",
                raw_stdout=auth_proc.stdout, raw_stderr=auth_proc.stderr,
                exit_code=auth_proc.returncode)
        if auth_proc.returncode != 0:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="auth_required",
                available=True, binary=bin_path, version=version,
                auth_ok=False, error_type=ERROR_AUTH,
                message=auth_msg or "Claude authentication required",
                raw_stdout=auth_proc.stdout, raw_stderr=auth_proc.stderr,
                exit_code=auth_proc.returncode)
        if probe:
            resp = self.invoke(
                [Message(role="user", content="Reply exactly OK.")],
                timeout=timeout, cwd=cwd)
            return health_from_response(
                self.provider_id, resp, binary=bin_path, version=version)
        return ProviderHealth(
            provider=self.provider_id, ok=True, status="ok", available=True,
            binary=bin_path, version=version, auth_ok=True,
            message=auth_msg or "Claude CLI authenticated")

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

        # issue #4: `-p` mode는 stateless라 `--resume`을 붙이면 인터랙티브
        # 입력 대기로 폴백되어 Windows에서 데드락. session_id가 들어와도
        # resume 시도 없이 새 식별자만 부여한다.
        used_session_id = str(uuid.uuid4())
        cmd += ["--session-id", used_session_id]
        return cmd, used_session_id

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "",
               cwd: str | None = None) -> LLMResponse:
        prompt = build_session_prompt(messages)
        cmd, used_sid = self._build_cmd(prompt, model, session_id, "json")
        if cmd is None:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="Claude CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

        start = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, cwd=cwd)
            latency = int((time.time() - start) * 1000)

            if result.returncode != 0:
                err_msg = (result.stderr or "").strip()[:300]
                msg = err_msg or f"exit={result.returncode}"
                logger.error("Claude 실패 (code=%d): %s",
                             result.returncode, msg)
                return LLMResponse(
                    content="", provider=self.provider_id, model=model,
                    raw_stderr=result.stderr, session_id=used_sid,
                    error=msg,
                    error_type=classify_error(msg),
                    exit_code=result.returncode,
                )

            content, tokens, err = _parse_claude_json(result.stdout)
            tokens.payload_prompt_tokens = estimate_payload_prompt_tokens(prompt)
            tokens.prompt_tokens_reliable = False
            tokens.prompt_tokens_source = "claude_cli_reported"
            return LLMResponse(
                content=content if not err else "",
                provider=self.provider_id, model=model,
                tokens=tokens, latency_ms=latency,
                raw_stderr=result.stderr, session_id=used_sid,
                error=err,
                error_type=classify_error(err) if err else "",
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.error("Claude 타임아웃 (%d초)", timeout)
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                session_id=used_sid,
                                error=f"timeout after {timeout}s",
                                error_type="timeout",
                                exit_code=124)
        except FileNotFoundError:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="Claude CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> LLMResponse:
        prompt = build_session_prompt(messages)
        cmd, used_sid = self._build_cmd(prompt, model, session_id, "json")
        if cmd is None:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="Claude CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

        start = time.time()
        try:
            stdout_b, stderr_b, rc, timed_out = await run_subprocess_async(
                cmd, timeout=timeout, cwd=cwd)
        except FileNotFoundError:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="Claude CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)
        if timed_out:
            logger.error("Claude 타임아웃 (%d초)", timeout)
            return LLMResponse(content="", provider=self.provider_id,
                                model=model, session_id=used_sid,
                                error=f"timeout after {timeout}s",
                                error_type="timeout",
                                exit_code=124)
        latency = int((time.time() - start) * 1000)

        if rc != 0:
            stderr_txt = stderr_b.decode("utf-8", errors="replace")
            logger.error("Claude 실패 (code=%d): %s", rc, stderr_txt[:300])
            msg = stderr_txt.strip()[:300] or f"exit={rc}"
            return LLMResponse(
                content="", provider=self.provider_id, model=model,
                raw_stderr=stderr_txt, session_id=used_sid,
                error=msg, error_type=classify_error(msg),
                exit_code=rc)

        stderr_txt = stderr_b.decode("utf-8", errors="replace")
        content, tokens, err = _parse_claude_json(
            stdout_b.decode("utf-8", errors="replace"))
        return LLMResponse(
            content=content if not err else "",
            provider=self.provider_id, model=model,
            tokens=tokens, latency_ms=latency,
            raw_stderr=stderr_txt, session_id=used_sid,
            error=err,
            error_type=classify_error(err) if err else "",
            exit_code=rc,
        )

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           idle_timeout: int | None = None,
                           wall_timeout: int | None = None) -> AsyncIterator[StreamChunk]:
        """Claude Code `--output-format stream-json` 기반 스트리밍.

        이벤트 예:
          {"type":"system","subtype":"init","session_id":"..."}
          {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
          {"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}
          {"type":"user","message":{"content":[{"type":"tool_result",...}]}}
          {"type":"result","subtype":"success","result":"...","usage":{...},"session_id":"..."}
        """
        prompt = build_session_prompt(messages)
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
            final_usage = TokenUsage(
                payload_prompt_tokens=estimate_payload_prompt_tokens(prompt),
                prompt_tokens_reliable=False,
                prompt_tokens_source="claude_cli_reported")
            final_session_id = used_sid
            timed_out = False

            # `timeout`은 wall-clock deadline이 아니라 **마지막 청크 이후 idle 한도**.
            # Claude가 thinking/tool_use 같은 진행 청크를 보내면 카운트가 리셋되므로
            # "장시간 사고 중"인 정상 동작은 잘못 끊지 않는다.
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
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=read_timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    idle = int(time.time() - last_activity)
                    timeout_kind = (
                        "wall" if wall_deadline is not None
                        and time.time() >= wall_deadline else "idle")
                    logger.error("Claude %s timeout (%ds since last chunk)",
                                 timeout_kind, idle)
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
                if not line_bytes:
                    break
                last_activity = time.time()
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
                        total_tokens=pt + ct,
                        payload_prompt_tokens=estimate_payload_prompt_tokens(prompt),
                        prompt_tokens_reliable=False,
                        prompt_tokens_source="claude_cli_reported")
                    if evt.get("session_id"):
                        final_session_id = evt["session_id"]
                else:
                    yield StreamChunk(type="event", data=evt)

            if timed_out:
                if proc and proc.returncode is None:
                    await proc.wait()
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


def _parse_claude_json(stdout: str) -> tuple[str, TokenUsage, str]:
    """Claude CLI --output-format json stdout 파싱.

    Returns: (content, tokens, error_message)
      Claude API의 한도/오류는 보통 `is_error: true` + `subtype: "error_*"` 또는
      content 자체에 에러 메시지로 응답.
    """
    stdout = stdout.strip()
    if not stdout:
        return "", TokenUsage(), ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, TokenUsage(), ""

    content = (data.get("result")
               or data.get("content")
               or data.get("text")
               or "")
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = prompt_tokens + completion_tokens

    # 에러 감지: is_error / subtype != success / type=error
    error_msg = ""
    if data.get("is_error"):
        error_msg = str(content) or data.get("subtype", "claude error")
    elif data.get("subtype") and str(data.get("subtype")).startswith("error"):
        error_msg = str(content) or str(data.get("subtype"))
    elif data.get("type") == "error":
        error_msg = str(content) or "claude error"

    return str(content).strip(), TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
    ), error_msg
