"""Claude Code CLI 프로바이더."""

import asyncio
import json
import logging
import os
import pathlib
import platform
import re
import shutil
import time
import uuid
from typing import AsyncIterator

from .base import (LLMProvider, StreamState, build_session_prompt,
                   estimate_payload_prompt_tokens, health_from_response,
                   redact_argv, run_health_command, run_subprocess_async,
                   run_subprocess_sync, write_debug_trace)
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


def _emit_invoke_debug(cmd: list[str], rc, latency_ms: int, stderr: str,
                       sid: str, path: str | None, phase: str) -> None:
    """비스트리밍 invoke 경로의 debug 로깅 + 선택적 trace 기록."""
    logger.info("[debug] claude %s rc=%s latency=%dms argv=%s",
                phase, rc, latency_ms, redact_argv(cmd))
    tail = (stderr or "").strip()
    if tail:
        logger.info("[debug] claude stderr tail:\n%s", tail[-2000:])
    if path:
        write_debug_trace(path, {
            "provider": "claude", "phase": phase, "returncode": rc,
            "latency_ms": latency_ms, "argv": redact_argv(cmd),
            "session_id": sid, "stderr": (stderr or "")[-20000:],
        })


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
                 disallowed_tools: list[str] | None = None,
                 lean: bool = False,
                 debug: bool = False,
                 debug_log_path: str | None = None,
                 partial_messages: bool = False):
        """
        Args:
            permission_mode: `default`, `acceptEdits`, `plan`, `bypassPermissions` 중 하나.
                **WARNING**: 기본값 `bypassPermissions`는 에이전트에 전체 권한을 부여한다.
                신뢰할 수 없는 컨텍스트에서 임베딩할 때는 `default`로 변경할 것.
            allowed_tools: 허용 도구 목록 (예: ["Read", "Grep", "Bash"]).
                None이면 제한 없음.
            disallowed_tools: 금지 도구 목록.
            lean: 단일 completion(요약/생성 등 툴이 필요 없는 1회 호출) 전용 경량
                모드. True 면 호출마다 `--safe-mode`(CLAUDE.md/skills/plugins/hooks/
                MCP/custom agents 등 커스터마이즈 비활성화) + `--tools`(빌트인 툴
                allowlist; allowed_tools 미지정 시 `""` 로 전부 비활성화) 를 붙여
                하네스 부팅 비용과 주입 컨텍스트를 최소화한다. lean 에서는 mcp_config/
                disallowed_tools 가 무시된다(safe-mode 가 MCP 를 끄고 `--tools` 가
                allowlist 이므로). 기본 False — 기존 동작 불변.
            debug: 진단용 계측 모드. True 면 호출마다 claude CLI 에 ``--debug`` 를
                붙여 MCP 연결·툴 호출·API 왕복 내부 로그를 stderr 로 끌어내고,
                agentcli 가 그것 + 타이밍을 Python logging(logger 이름
                ``agentcli.providers.claude``) 으로 남긴다. 스트리밍에서는 각
                청크를 ``[+{elapsed}s] type`` 타임라인으로 로깅해 툴 루프/행을
                눈으로 확인할 수 있다. 기본 False — 기존 동작 불변.
            debug_log_path: 지정 시 debug 모드의 구조화 trace(JSON Lines:
                redact 된 argv·청크 타임라인·stderr·총 소요)를 이 파일에 append.
                20분 행을 재현한 뒤 이 파일 하나로 원인을 파악한다.
            partial_messages: 스트리밍 전용. True 면 stream-json 에
                ``--include-partial-messages`` 를 붙여 Claude 가 토큰 단위 델타
                (``content_block_delta``/``text_delta``·``thinking_delta``)를
                내보내게 한다. ``stream_async`` 가 이를 증분 ``text``/``thinking``
                청크로 흘려, 단일 긴 생성도 토큰이 실시간으로 나온다(기본은 메시지
                블록 단위). invoke(비스트리밍)에는 영향 없음. 기본 False.
        """
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools
        self._lean = lean
        self._debug = debug
        self._debug_log_path = debug_log_path
        self._partial_messages = partial_messages

    def _find_binary(self) -> str | None:
        executable = "claude.cmd" if platform.system() == "Windows" else "claude"
        return shutil.which(executable) or shutil.which("claude")

    def is_available(self) -> bool:
        return self._find_binary() is not None

    def list_models(self) -> list[dict]:
        return list(CLAUDE_MODELS)

    def session_alive(self, session_id: str, *,
                      cwd: str | None = None) -> bool | None:
        """Claude 네이티브 세션 파일 존재 여부로 liveness 판정 (호출 없이).

        Claude Code 는 세션을 ``~/.claude/projects/<encode(cwd)>/<sid>.jsonl``
        에 저장한다(2.1.x 검증). 인코딩 규칙은 **영숫자 외 모든 문자를 '-' 로**
        치환 — 예: ``/Users/x/.claude`` → ``-Users-x--claude``(`/`·`.` 둘 다 '-').
        파일이 없으면 다음 ``--resume`` 이 실패하고 새 세션으로 자동 복구된다.
        cwd 는 호출 때와 동일해야 정확하다(경로가 cwd 로 해시되므로) — None 이면
        현재 프로세스 cwd. symlink/trailing slash 는 ``realpath`` 로 정규화.
        """
        if not session_id or not self.supports_sessions:
            return False if session_id else None
        base = os.path.realpath(cwd if cwd is not None else os.getcwd())
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", base)
        path = (pathlib.Path.home() / ".claude" / "projects"
                / encoded / f"{session_id}.jsonl")
        return path.exists()

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
                   output_format: str = "json", *,
                   permission_mode: str | None = None,
                   allowed_tools: list[str] | None = None,
                   disallowed_tools: list[str] | None = None,
                   mcp_config: dict | str | None = None,
                   strict_mcp_config: bool = False,
                   lean: bool | None = None,
                   debug: bool | None = None,
                   partial_messages: bool | None = None) -> tuple[list[str] | None, str]:
        """CLI 명령어와 사용한 session_id 반환. (None, "") 이면 바이너리 없음.

        permission_mode/allowed_tools/disallowed_tools/mcp_config/lean 은 호출
        시점 오버라이드 (None 이면 생성자 기본값). mcp_config 는 외부 MCP 서버
        정의 — dict 면 ``{"mcpServers": ...}`` 로 감싸 JSON 직렬화, str 이면 그대로
        (파일 경로 또는 사전 직렬화 JSON) 전달한다 (#154). lean=True 면 단일
        completion 용으로 ``--safe-mode`` + ``--tools`` allowlist 만 붙이고
        MCP/disallowed_tools 블록은 건너뛴다.
        """
        bin_path = self._find_binary()
        if not bin_path:
            return None, ""

        pmode = permission_mode or self._permission_mode
        atools = allowed_tools if allowed_tools is not None else self._allowed_tools
        dtools = (disallowed_tools if disallowed_tools is not None
                  else self._disallowed_tools)
        use_lean = self._lean if lean is None else lean
        use_debug = self._debug if debug is None else debug
        use_partial = (self._partial_messages if partial_messages is None
                       else partial_messages)

        cmd = [bin_path, "-p", prompt,
               "--output-format", output_format,
               "--permission-mode", pmode]
        if output_format == "stream-json":
            # stream-json은 반드시 --verbose 필요 (Claude Code 제약)
            cmd.append("--verbose")
            if use_partial:
                # 토큰 단위 델타(content_block_delta/text_delta) 방출.
                cmd.append("--include-partial-messages")
        if use_debug:
            # claude 내부(MCP 연결/툴 호출/API 왕복) 로그를 stderr 로 끌어낸다.
            cmd.append("--debug")
        if model:
            cmd += ["--model", model]
        if use_lean:
            # 단일 completion 경량 모드: 커스터마이즈(CLAUDE.md/skills/plugins/
            # hooks/MCP/custom agents 등) 와 빌트인 툴을 끊어 호출당 하네스 부팅
            # 비용·주입 컨텍스트를 최소화한다. --tools 는 빌트인 allowlist 로,
            # allowed_tools 가 명시되면 그 툴만 남기고 아니면 "" 로 전부 끈다.
            # safe-mode 가 MCP 를 끄므로 mcp_config/disallowed_tools 는 무시.
            cmd.append("--safe-mode")
            cmd += ["--tools", ",".join(atools) if atools else ""]
        else:
            if atools:
                cmd += ["--allowedTools", ",".join(atools)]
            if dtools:
                cmd += ["--disallowedTools", ",".join(dtools)]
            if mcp_config:
                if isinstance(mcp_config, str):
                    cmd += ["--mcp-config", mcp_config]
                else:
                    payload = (mcp_config if "mcpServers" in mcp_config
                               else {"mcpServers": mcp_config})
                    cmd += ["--mcp-config", json.dumps(payload)]
                if strict_mcp_config:
                    cmd.append("--strict-mcp-config")

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
               cwd: str | None = None,
               mcp_config: dict | str | None = None,
               strict_mcp_config: bool = False,
               permission_mode: str | None = None,
               allowed_tools: list[str] | None = None,
               disallowed_tools: list[str] | None = None,
               lean: bool | None = None,
               debug: bool | None = None,
               debug_log_path: str | None = None) -> LLMResponse:
        prompt = build_session_prompt(messages)
        use_debug = self._debug if debug is None else debug
        dbg_path = self._debug_log_path if debug_log_path is None else debug_log_path
        cmd, used_sid = self._build_cmd(
            prompt, model, session_id, "json",
            permission_mode=permission_mode, allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools, mcp_config=mcp_config,
            strict_mcp_config=strict_mcp_config, lean=lean, debug=use_debug)
        if cmd is None:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="Claude CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

        start = time.time()
        try:
            # run_subprocess_sync: 새 프로세스 그룹 + 타임아웃/정리 시 그룹 전체
            # killpg → CLI 가 띄운 MCP/hook 손자 좀비 방지 (subprocess.run 의
            # 직속-only kill 한계 회피).
            stdout_b, stderr_b, rc, timed_out = run_subprocess_sync(
                cmd, timeout=timeout, cwd=cwd)
        except FileNotFoundError:
            logger.error("Claude CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="Claude CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)
        if timed_out:
            logger.error("Claude 타임아웃 (%d초)", timeout)
            if use_debug:
                _emit_invoke_debug(cmd, 124,
                                   int((time.time() - start) * 1000),
                                   f"timeout after {timeout}s", used_sid,
                                   dbg_path, "invoke")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                session_id=used_sid,
                                error=f"timeout after {timeout}s",
                                error_type="timeout",
                                exit_code=124)
        latency = int((time.time() - start) * 1000)
        stderr_txt = stderr_b.decode("utf-8", errors="replace")
        stdout_txt = stdout_b.decode("utf-8", errors="replace")
        if use_debug:
            _emit_invoke_debug(cmd, rc, latency, stderr_txt,
                               used_sid, dbg_path, "invoke")

        if rc != 0:
            if (used_sid == session_id and session_id
                    and STALE_SESSION_MARKER in stderr_txt):
                logger.warning(
                    "Claude 세션 %s 만료 — 새 세션으로 재시도",
                    session_id[:8])
                return self.invoke(
                    messages, model=model, timeout=timeout,
                    session_id="", cwd=cwd,
                    permission_mode=permission_mode,
                    allowed_tools=allowed_tools,
                    disallowed_tools=disallowed_tools,
                    mcp_config=mcp_config,
                    strict_mcp_config=strict_mcp_config,
                    lean=lean, debug=debug,
                    debug_log_path=debug_log_path)
            err_msg = stderr_txt.strip()[:300]
            msg = err_msg or f"exit={rc}"
            logger.error("Claude 실패 (code=%d): %s", rc, msg)
            return LLMResponse(
                content="", provider=self.provider_id, model=model,
                raw_stderr=stderr_txt, session_id=used_sid,
                error=msg,
                error_type=classify_error(msg),
                exit_code=rc,
            )

        content, tokens, err = _parse_claude_json(stdout_txt)
        tokens.payload_prompt_tokens = estimate_payload_prompt_tokens(prompt)
        tokens.prompt_tokens_reliable = False
        tokens.prompt_tokens_source = "claude_cli_reported"
        return LLMResponse(
            content=content if not err else "",
            provider=self.provider_id, model=model,
            tokens=tokens, latency_ms=latency,
            raw_stderr=stderr_txt, session_id=used_sid,
            error=err,
            error_type=classify_error(err) if err else "",
            exit_code=rc,
        )

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           mcp_config: dict | str | None = None,
                           strict_mcp_config: bool = False,
                           permission_mode: str | None = None,
                           allowed_tools: list[str] | None = None,
                           disallowed_tools: list[str] | None = None,
                           lean: bool | None = None,
                           debug: bool | None = None,
                           debug_log_path: str | None = None) -> LLMResponse:
        prompt = build_session_prompt(messages)
        use_debug = self._debug if debug is None else debug
        dbg_path = self._debug_log_path if debug_log_path is None else debug_log_path
        cmd, used_sid = self._build_cmd(
            prompt, model, session_id, "json",
            permission_mode=permission_mode, allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools, mcp_config=mcp_config,
            strict_mcp_config=strict_mcp_config, lean=lean, debug=use_debug)
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
            if use_debug:
                _emit_invoke_debug(cmd, 124,
                                   int((time.time() - start) * 1000),
                                   stderr_b.decode("utf-8", errors="replace"),
                                   used_sid, dbg_path, "invoke_async")
            return LLMResponse(content="", provider=self.provider_id,
                                model=model, session_id=used_sid,
                                error=f"timeout after {timeout}s",
                                error_type="timeout",
                                exit_code=124)
        latency = int((time.time() - start) * 1000)
        if use_debug:
            _emit_invoke_debug(cmd, rc, latency,
                               stderr_b.decode("utf-8", errors="replace"),
                               used_sid, dbg_path, "invoke_async")

        if rc != 0:
            stderr_txt = stderr_b.decode("utf-8", errors="replace")
            if (used_sid == session_id and session_id
                    and STALE_SESSION_MARKER in stderr_txt):
                logger.warning(
                    "Claude 세션 %s 만료 — 새 세션으로 재시도", session_id[:8])
                return await self.invoke_async(
                    messages, model=model, timeout=timeout,
                    session_id="", cwd=cwd,
                    permission_mode=permission_mode,
                    allowed_tools=allowed_tools,
                    disallowed_tools=disallowed_tools,
                    mcp_config=mcp_config,
                    strict_mcp_config=strict_mcp_config,
                    lean=lean, debug=debug,
                    debug_log_path=debug_log_path)
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
                           wall_timeout: int | None = None,
                           mcp_config: dict | str | None = None,
                           strict_mcp_config: bool = False,
                           permission_mode: str | None = None,
                           allowed_tools: list[str] | None = None,
                           disallowed_tools: list[str] | None = None,
                           lean: bool | None = None,
                           debug: bool | None = None,
                           debug_log_path: str | None = None,
                           partial_messages: bool | None = None) -> AsyncIterator[StreamChunk]:
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
        use_debug = self._debug if debug is None else debug
        dbg_path = self._debug_log_path if debug_log_path is None else debug_log_path
        use_partial = (self._partial_messages if partial_messages is None
                       else partial_messages)
        # 만료된 session_id 로 resume 하면 출력 없이 즉시 실패하므로, 첫 청크가
        # stale-session 에러일 때만 새 세션으로 1회 재시도한다. 어떤 출력이든
        # caller 에 전달된 뒤에는 재시도하지 않는다.
        attempt_sid = session_id
        for _attempt in range(2):
            cmd, used_sid = self._build_cmd(
                prompt, model, attempt_sid, "stream-json",
                permission_mode=permission_mode, allowed_tools=allowed_tools,
                disallowed_tools=disallowed_tools, mcp_config=mcp_config,
                strict_mcp_config=strict_mcp_config, lean=lean, debug=use_debug,
                partial_messages=use_partial)
            if cmd is None:
                yield StreamChunk(type="error", content="Claude CLI not found")
                return
            state = StreamState(
                final_session_id=used_sid,
                final_usage=TokenUsage(
                    payload_prompt_tokens=estimate_payload_prompt_tokens(prompt),
                    prompt_tokens_reliable=False,
                    prompt_tokens_source="claude_cli_reported"))
            # partial 모드: assistant 전체 블록 text/thinking 를 건너뛰고 델타로만
            # 누적·방출한다 (이중 집계 방지). _dispatch_stream_event 가 참조.
            state.extra["partial"] = use_partial
            retry_stale = False
            emitted = False
            async for chunk in self._run_stream_template(
                    cmd, state, model=model, cwd=cwd, timeout=timeout,
                    idle_timeout=idle_timeout, wall_timeout=wall_timeout,
                    debug=use_debug, debug_log_path=dbg_path):
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
        """Claude Code event 정규화 — text / thinking / tool_use / tool_result / event.

        ``--include-partial-messages`` 사용 시 Claude 는 토큰 델타를
        ``{"type":"stream_event","event":{"type":"content_block_delta",
        "delta":{"type":"text_delta","text":...}}}`` 로 내보낸다. 이때는 델타로
        증분 방출하고, 뒤따르는 전체 ``assistant`` 블록의 text/thinking 는
        건너뛴다(``state.extra["partial"]`` 가드) — 같은 내용을 두 번 세지 않기
        위함. tool_use 는 (델타가 부분 JSON 이라) 전체 assistant 블록을 쓴다.
        """
        etype = evt.get("type", "")
        partial = bool(state.extra.get("partial"))
        if etype == "system" and evt.get("session_id"):
            state.final_session_id = evt["session_id"]
            yield StreamChunk(type="event", data=evt,
                              session_id=state.final_session_id)
        elif etype == "stream_event":
            ev = evt.get("event") or {}
            evtype = ev.get("type")
            if evtype == "message_start":
                # 새 메시지: delta-seen 플래그 리셋 → 뒤따르는 전체 assistant
                # 블록 skip 여부를 이 메시지의 델타 수신 여부로만 판단한다.
                state.extra["saw_text_delta"] = False
                state.extra["saw_thinking_delta"] = False
            elif evtype == "content_block_delta":
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        state.extra["saw_text_delta"] = True
                        state.text_parts.append(text)
                        yield StreamChunk(type="text", content=text, data=delta)
                elif dtype == "thinking_delta":
                    thinking = delta.get("thinking", "")
                    if thinking:
                        state.extra["saw_thinking_delta"] = True
                        yield StreamChunk(type="thinking",
                                          content=thinking, data=delta)
                # input_json_delta(툴 인자 부분 JSON) 등은 무시 — 전체 tool_use
                # 블록을 assistant 이벤트에서 받는다.
            # content_block_start/stop, message_delta/stop 는 내부 프로토콜
            # 프레이밍 — 스트림 청크로 흘리지 않는다.
        elif etype == "assistant":
            msg = evt.get("message") or {}
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    # partial: 델타로 이미 스트리밍됐을 때만 skip(중복 방지).
                    # 델타가 한 번도 안 왔으면 전체 블록을 fallback 으로 방출 —
                    # 안 그러면 텍스트가 조용히 유실된다(merge-gate 회귀).
                    if partial and state.extra.get("saw_text_delta"):
                        continue
                    text = block.get("text", "")
                    if text:
                        state.text_parts.append(text)
                        yield StreamChunk(type="text", content=text, data=block)
                elif btype == "thinking":
                    if partial and state.extra.get("saw_thinking_delta"):
                        continue
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
