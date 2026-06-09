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

from .base import (LLMProvider, StreamState, build_session_prompt,
                   estimate_payload_prompt_tokens, health_from_response,
                   run_health_command, run_subprocess_async)
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_TIMEOUT,
                     Message, LLMResponse, ProviderHealth, TokenUsage,
                     StreamChunk, classify_error)

logger = logging.getLogger(__name__)

# 저장된 session_id 의 네이티브 세션 파일이 삭제/만료되면 CLI 가 이 메시지와
# 함께 즉시 실패한다 — 이때만 새 세션으로 1회 자동 복구한다.
STALE_SESSION_MARKER = "No conversation found with session ID"

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
    # macOS/Linux: `claude -p --resume <sid>` 가 네이티브 세션을 재개하며
    # resume 후에도 동일 session_id 가 유지된다 (Claude Code 2.1.x 검증).
    # Windows: `-p` + `--resume` 조합이 인터랙티브 입력 대기로 폴백되어
    # 5분+ hang (issue #4) — Windows 에서만 stateless 로 동작하고
    # `--session-id <new-uuid>` 는 usage audit 식별자로만 쓴다.
    supports_sessions = platform.system() != "Windows"
    supports_streaming = True
    # 어느 모드든 히스토리는 Claude CLI 가 소유 — 라이브러리는 대화 내용을
    # 저장하지 않는다 (Windows stateless 모드 포함).
    stores_history = False

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

        # macOS/Linux: 저장된 session_id 가 있으면 `--resume` 으로 재개.
        # Windows (supports_sessions=False): issue #4 데드락 회피를 위해
        # resume 하지 않고 항상 새 식별자만 부여한다.
        if session_id and self.supports_sessions:
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
                if (used_sid == session_id and session_id
                        and STALE_SESSION_MARKER in (result.stderr or "")):
                    logger.warning(
                        "Claude 세션 %s 만료 — 새 세션으로 재시도",
                        session_id[:8])
                    return self.invoke(messages, model=model,
                                       timeout=timeout, session_id="",
                                       cwd=cwd)
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
            if (used_sid == session_id and session_id
                    and STALE_SESSION_MARKER in stderr_txt):
                logger.warning(
                    "Claude 세션 %s 만료 — 새 세션으로 재시도", session_id[:8])
                return await self.invoke_async(
                    messages, model=model, timeout=timeout,
                    session_id="", cwd=cwd)
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

        공통 readline/timeout/cleanup 골격은 ``LLMProvider._run_stream_template``
        에 위임. Claude 의 JSON event 해석만 ``_dispatch_stream_event`` 에서.

        이벤트 예:
          {"type":"system","subtype":"init","session_id":"..."}
          {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
          {"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}
          {"type":"user","message":{"content":[{"type":"tool_result",...}]}}
          {"type":"result","subtype":"success","result":"...","usage":{...},"session_id":"..."}
        """
        prompt = build_session_prompt(messages)
        # 만료된 session_id 로 resume 하면 출력 없이 즉시 실패하므로, 첫 청크가
        # stale-session 에러일 때만 새 세션으로 1회 재시도한다. 어떤 출력이든
        # caller 에 전달된 뒤에는 재시도하지 않는다.
        attempt_sid = session_id
        for _attempt in range(2):
            cmd, used_sid = self._build_cmd(
                prompt, model, attempt_sid, "stream-json")
            if cmd is None:
                yield StreamChunk(type="error", content="Claude CLI not found")
                return
            state = StreamState(
                final_session_id=used_sid,
                final_usage=TokenUsage(
                    payload_prompt_tokens=estimate_payload_prompt_tokens(prompt),
                    prompt_tokens_reliable=False,
                    prompt_tokens_source="claude_cli_reported"))
            retry_stale = False
            emitted = False
            async for chunk in self._run_stream_template(
                    cmd, state, model=model, cwd=cwd, timeout=timeout,
                    idle_timeout=idle_timeout, wall_timeout=wall_timeout):
                if (attempt_sid and not emitted
                        and chunk.type == "error"
                        and STALE_SESSION_MARKER in (chunk.content or "")):
                    retry_stale = True
                    break
                emitted = True
                yield chunk
            if not retry_stale:
                return
            logger.warning(
                "Claude 세션 %s 만료 — 새 세션으로 스트림 재시도",
                attempt_sid[:8])
            attempt_sid = ""

    async def _dispatch_stream_event(self, evt: dict,
                                     state: StreamState) -> AsyncIterator[StreamChunk]:
        """Claude Code event 정규화 — text / thinking / tool_use / tool_result / event."""
        etype = evt.get("type", "")
        if etype == "system" and evt.get("session_id"):
            state.final_session_id = evt["session_id"]
            yield StreamChunk(type="event", data=evt,
                              session_id=state.final_session_id)
        elif etype == "assistant":
            msg = evt.get("message") or {}
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        state.text_parts.append(text)
                        yield StreamChunk(type="text", content=text, data=block)
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
            prev = state.final_usage
            state.final_usage = TokenUsage(
                prompt_tokens=pt, completion_tokens=ct,
                total_tokens=pt + ct,
                payload_prompt_tokens=(prev.payload_prompt_tokens if prev else 0),
                prompt_tokens_reliable=False,
                prompt_tokens_source="claude_cli_reported")
            if evt.get("session_id"):
                state.final_session_id = evt["session_id"]
        else:
            yield StreamChunk(type="event", data=evt)


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
    if not isinstance(data, dict):
        # 유효한 JSON이지만 객체가 아니면 (배열/스칼라) raw 텍스트와 동일 취급.
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
