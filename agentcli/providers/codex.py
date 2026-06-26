"""Codex CLI 프로바이더 — agentic CLI (세션·스트리밍 지원).

Codex CLI (v0.118+)는 세션과 JSONL 이벤트 스트림을 제공:
  - `codex exec --json` → stdout에 이벤트: thread.started / turn.started /
    item.completed / turn.completed
  - `thread.started.thread_id` = session UUID
  - `turn.completed.usage = {input_tokens, cached_input_tokens, output_tokens}`
  - 재개: `codex exec resume --json <thread_id> "<prompt>"`

세션이 히스토리를 소유하므로 라이브러리는 session_id만 관리,
프롬프트 재직렬화는 하지 않는다.
"""

import asyncio
import glob as _glob
import json
import logging
import pathlib
import shutil
import subprocess
import time
from typing import AsyncIterator

from .base import (LLMProvider, StreamState, build_session_prompt,
                   estimate_payload_prompt_tokens, health_from_response,
                   run_health_command, run_subprocess_async)
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_TIMEOUT,
                     Message, LLMResponse, ProviderHealth, TokenUsage,
                     StreamChunk, classify_error)
from ..utils import build_env

logger = logging.getLogger(__name__)

# 저장된 thread(세션)가 삭제/만료되면 `codex exec resume` 가 이 메시지와 함께
# 실패한다 — 이때만 새 세션으로 1회 자동 복구한다 (claude STALE_SESSION_MARKER 대응).
# 실제 stderr: "thread/resume failed: no rollout found for thread id <uuid>".
# 버전별 표기 흔들림(대소문자 등)에 견디도록 짧은 anchor + 소문자 비교.
CODEX_STALE_MARKER = "no rollout found"


def _is_codex_stale(text: str | None) -> bool:
    return CODEX_STALE_MARKER in (text or "").lower()


_CODEX_INITIAL_GREETINGS = {
    "ready. what would you like me to work on?",
}

CODEX_MODELS = [
    {"id": "", "name": "기본", "aliases": ["default"]},
    {
        "id": "gpt-5.3-codex",
        "name": "GPT-5.3 Codex",
    },
    {
        "id": "gpt-5.2-codex",
        "name": "GPT-5.2 Codex",
    },
    {
        "id": "gpt-5.1-codex-max",
        "name": "GPT-5.1 Codex Max",
        "aliases": ["codex-max"],
    },
    {
        "id": "gpt-5.1-codex",
        "name": "GPT-5.1 Codex",
    },
    {
        "id": "gpt-5.1-codex-mini",
        "name": "GPT-5.1 Codex mini",
    },
    {"id": "gpt-5-codex", "name": "GPT-5 Codex"},
    {"id": "gpt-5.5", "name": "GPT-5.5"},
    {"id": "gpt-5.5-pro", "name": "GPT-5.5 pro"},
    {"id": "gpt-5.4", "name": "GPT-5.4"},
    {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"},
    {"id": "gpt-5.4-nano", "name": "GPT-5.4 nano"},
    {"id": "gpt-5.2", "name": "GPT-5.2"},
    {"id": "gpt-5.2-pro", "name": "GPT-5.2 pro"},
    {"id": "gpt-5.1", "name": "GPT-5.1"},
    {"id": "gpt-5", "name": "GPT-5"},
    {"id": "o4-mini", "name": "o4-mini"},
    {"id": "o3", "name": "o3"},
    {"id": "gpt-4.1", "name": "GPT-4.1"},
]


def _toml_inline(value) -> str:
    """Python 값을 TOML inline 값으로 직렬화 (codex `-c key=value` 용).

    JSON 문자열 인용은 TOML basic string 과 호환되므로 str/숫자는 json.dumps,
    list/dict 는 TOML inline array/table 로 재귀 직렬화한다.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{k} = {_toml_inline(v)}" for k, v in value.items()) + "}"
    return json.dumps(str(value))


class CodexProvider(LLMProvider):
    provider_id = "codex"
    supports_sessions = True
    supports_streaming = True
    stores_history = False  # 히스토리는 Codex CLI 세션이 소유

    def __init__(self,
                 sandbox_mode: str = "danger-full-access",
                 approval_policy: str | None = None,
                 full_auto: bool = True,
                 skip_git_repo_check: bool = True):
        """
        Args:
            sandbox_mode: `read-only` | `workspace-write` | `danger-full-access`.
                **WARNING**: 기본값은 전체 권한. 임베딩 시 `workspace-write` 권장.
                재개(resume) 시에는 원 세션 설정이 유지되어 이 옵션은 무시된다.
            approval_policy: `-a` 옵션. None이면 생략. resume 시 무시.
            full_auto: `--full-auto` 플래그 사용 여부 (both exec & resume).
            skip_git_repo_check: `--skip-git-repo-check` — git 리포 외부에서도 실행 허용.
        """
        self._sandbox_mode = sandbox_mode
        self._approval_policy = approval_policy
        self._full_auto = full_auto
        self._skip_git = skip_git_repo_check

    def is_available(self) -> bool:
        return shutil.which("codex") is not None

    def _find_binary(self) -> str | None:
        return shutil.which("codex")

    def list_models(self) -> list[dict]:
        return list(CODEX_MODELS)

    def session_alive(self, session_id: str, *,
                      cwd: str | None = None) -> bool | None:
        """Codex thread(rollout) 파일 존재 여부로 liveness 판정 (호출 없이).

        Codex 는 세션을 ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>
        .jsonl`` 로 저장한다. thread_id 가 파일명에 들어가므로 그 sid 로
        recursive glob 한다. 없으면 ``resume`` 이 ``no rollout found`` 로 실패하고
        새 세션으로 자동 복구된다. (cwd 무관 — codex 세션 경로는 날짜 기반)
        """
        if not session_id:
            return None
        root = pathlib.Path.home() / ".codex" / "sessions"
        if not root.exists():
            return False
        # session_id 의 glob 메타문자(*,?,[)를 escape — 안 하면 '*' 가 아무 파일
        # 이나 매칭해 죽은 세션을 살아있다고 오판할 수 있다.
        return any(root.glob(f"**/rollout-*{_glob.escape(session_id)}.jsonl"))

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        bin_path = shutil.which("codex")
        if not bin_path:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="binary_missing",
                available=False, auth_ok=False,
                error_type=ERROR_BINARY_MISSING,
                message="codex CLI not found")

        version_proc = run_health_command([bin_path, "--version"], timeout=timeout)
        version = (version_proc.stdout or version_proc.stderr).strip()
        auth_proc = run_health_command(
            [bin_path, "login", "status"], timeout=timeout, cwd=cwd,
            env=build_env())
        auth_msg = ((auth_proc.stdout or "") + (auth_proc.stderr or "")).strip()
        if auth_proc.returncode == 124:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="timeout",
                available=True, binary=bin_path, version=version,
                auth_ok=None, error_type=ERROR_TIMEOUT,
                message=auth_msg or f"codex login status timed out after {timeout}s",
                raw_stdout=auth_proc.stdout, raw_stderr=auth_proc.stderr,
                exit_code=auth_proc.returncode)
        if auth_proc.returncode != 0:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="auth_required",
                available=True, binary=bin_path, version=version,
                auth_ok=False, error_type=ERROR_AUTH,
                message=auth_msg or "Codex authentication required",
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
            message=auth_msg or "Codex CLI authenticated")

    def _build_cmd(self, prompt: str, model: str, cwd: str | None,
                   session_id: str, *,
                   sandbox_mode: str | None = None,
                   approval_policy: str | None = None,
                   mcp_config: dict | None = None) -> list[str] | None:
        """세션 상태에 따라 `codex exec` 또는 `codex exec resume`를 조립.

        바이너리 없으면 None 반환 (3-provider 정규화 계약: 호출자가 즉시
        binary_missing 으로 실패 처리). claude/copilot 와 동일 패턴.

        sandbox_mode/approval_policy 는 호출 시점 오버라이드 (None 이면 생성자
        기본값). resume 시에는 원 세션 설정이 유지되어 무시된다 — acting run 은
        ``new_session=True`` 로 신규 세션을 강제해야 적용된다 (#154).

        mcp_config 는 외부 MCP 서버 정의 dict ``{name: {codex mcp_servers 필드}}``.
        codex 는 claude 와 달리 ``~/.codex/config.toml`` 의 ``[mcp_servers.<name>]``
        를 쓰므로, 각 서버를 ``-c 'mcp_servers.<name>=<TOML inline table>'`` 로
        per-call 주입한다. HTTP 는 ``{url, bearer_token_env_var?}``, stdio 는
        ``{command, args?, env?}`` (#154 follow-up). 토큰은 inline 헤더가 아니라
        ``bearer_token_env_var`` (환경변수명) 로 전달한다.
        """
        bin_path = self._find_binary()
        if not bin_path:
            return None
        sbox = sandbox_mode or self._sandbox_mode
        apol = (approval_policy if approval_policy is not None
                else self._approval_policy)
        mcp_args: list[str] = []
        for name, cfg in (mcp_config or {}).items():
            mcp_args += ["-c", f"mcp_servers.{name}={_toml_inline(cfg)}"]
        cmd = [bin_path, "exec"]
        if session_id:
            cmd += ["resume", "--json"]
            if self._full_auto:
                cmd.append("--full-auto")
            if self._skip_git:
                cmd.append("--skip-git-repo-check")
            if model:
                cmd += ["-m", model]
            cmd += mcp_args
            # `--` 로 옵션 파싱 종료 — session_id/prompt 가 `-` 로 시작해도
            # 플래그로 해석되지 않는다 (untrusted 입력 주입 방지).
            cmd += ["--", session_id, prompt]
            return cmd
        # 신규 세션
        cmd.append("--json")
        if self._full_auto:
            cmd.append("--full-auto")
        if self._skip_git:
            cmd.append("--skip-git-repo-check")
        if sbox:
            cmd += ["-s", sbox]
        if apol:
            cmd += ["-a", apol]
        if cwd:
            cmd += ["-C", cwd]
        if model:
            cmd += ["-m", model]
        cmd += mcp_args
        # `--` 로 옵션 파싱 종료 — prompt 가 `-` 로 시작해도 플래그로
        # 해석되지 않는다 (untrusted 입력 주입 방지).
        cmd += ["--", prompt]
        return cmd

    # ---------- 동기 ----------

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "",
               cwd: str | None = None,
               sandbox_mode: str | None = None,
               approval_policy: str | None = None,
               mcp_config: dict | None = None) -> LLMResponse:
        # 세션이 히스토리 소유 — system 지시와 최신 user 요청만 전달
        prompt = build_session_prompt(messages)
        cmd = self._build_cmd(prompt, model, cwd, session_id,
                              sandbox_mode=sandbox_mode,
                              approval_policy=approval_policy,
                              mcp_config=mcp_config)
        if cmd is None:
            logger.error("codex CLI를 찾을 수 없습니다")
            return LLMResponse(
                content="", provider=self.provider_id, model=model,
                session_id=session_id,
                error="codex CLI not found",
                error_type=ERROR_BINARY_MISSING,
                exit_code=127)

        start = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, env=build_env(), cwd=cwd)
            latency = int((time.time() - start) * 1000)

            if result.returncode != 0:
                if session_id and _is_codex_stale(result.stderr):
                    logger.warning(
                        "Codex 세션 %s 만료 — 새 세션으로 재시도", session_id[:8])
                    return self.invoke(
                        messages, model=model, timeout=timeout,
                        session_id="", cwd=cwd, sandbox_mode=sandbox_mode,
                        approval_policy=approval_policy, mcp_config=mcp_config)
                msg = (result.stderr or "").strip()[:300] or f"exit={result.returncode}"
                logger.error("Codex 실패 (code=%d): %s",
                             result.returncode, msg)
                return LLMResponse(
                    content="", provider=self.provider_id, model=model,
                    raw_stderr=result.stderr,
                    session_id=session_id,
                    error=msg, error_type=classify_error(msg),
                    exit_code=result.returncode)

            parsed = _parse_jsonl_events(result.stdout)
            parsed["usage"].payload_prompt_tokens = estimate_payload_prompt_tokens(prompt)
            parsed["usage"].prompt_tokens_reliable = False
            parsed["usage"].prompt_tokens_source = "codex_cli_reported"
            if _needs_initial_greeting_retry(parsed, session_id):
                first_thread_id = parsed["thread_id"]
                retry_result = subprocess.run(
                    self._build_cmd(prompt, model, cwd, first_thread_id,
                                    sandbox_mode=sandbox_mode,
                                    approval_policy=approval_policy,
                                    mcp_config=mcp_config),
                    capture_output=True, text=True, timeout=timeout,
                    stdin=subprocess.DEVNULL, env=build_env(), cwd=cwd)
                latency = int((time.time() - start) * 1000)
                if retry_result.returncode != 0:
                    msg = ((retry_result.stderr or "").strip()[:300]
                           or f"exit={retry_result.returncode}")
                    logger.error("Codex greeting retry 실패 (code=%d): %s",
                                 retry_result.returncode, msg)
                    return LLMResponse(
                        content="", provider=self.provider_id, model=model,
                        raw_stderr=retry_result.stderr,
                        session_id=parsed["thread_id"],
                        error=msg, error_type=classify_error(msg),
                        exit_code=retry_result.returncode)
                parsed = _parse_jsonl_events(retry_result.stdout)
                parsed["usage"].payload_prompt_tokens = estimate_payload_prompt_tokens(prompt)
                parsed["usage"].prompt_tokens_reliable = False
                parsed["usage"].prompt_tokens_source = "codex_cli_reported"
                if not parsed["thread_id"]:
                    parsed["thread_id"] = first_thread_id
                result = retry_result
            err = parsed.get("error", "")
            return LLMResponse(
                content=parsed["text"] if not err else "",
                provider=self.provider_id, model=model,
                tokens=parsed["usage"], latency_ms=latency,
                raw_stderr=result.stderr,
                session_id=parsed["thread_id"] or session_id,
                error=err,
                error_type=classify_error(err) if err else "",
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.error("Codex 타임아웃 (%d초)", timeout)
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error=f"timeout after {timeout}s",
                                error_type="timeout",
                                exit_code=124)
        except FileNotFoundError:
            logger.error("codex CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="codex CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

    # ---------- 비동기 ----------

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           sandbox_mode: str | None = None,
                           approval_policy: str | None = None,
                           mcp_config: dict | None = None) -> LLMResponse:
        prompt = build_session_prompt(messages)
        cmd = self._build_cmd(prompt, model, cwd, session_id,
                              sandbox_mode=sandbox_mode,
                              approval_policy=approval_policy,
                              mcp_config=mcp_config)
        if cmd is None:
            logger.error("codex CLI를 찾을 수 없습니다")
            return LLMResponse(
                content="", provider=self.provider_id, model=model,
                session_id=session_id,
                error="codex CLI not found",
                error_type=ERROR_BINARY_MISSING,
                exit_code=127)

        start = time.time()
        try:
            stdout_b, stderr_b, rc, timed_out = await run_subprocess_async(
                cmd, timeout=timeout, cwd=cwd,
                env=build_env(), use_stdin_devnull=True)
        except FileNotFoundError:
            logger.error("codex CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="codex CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)
        if timed_out:
            logger.error("Codex 타임아웃 (%d초)", timeout)
            return LLMResponse(content="", provider=self.provider_id,
                                model=model, session_id=session_id,
                                error=f"timeout after {timeout}s",
                                error_type="timeout",
                                exit_code=124)
        latency = int((time.time() - start) * 1000)
        stderr_txt = stderr_b.decode("utf-8", errors="replace")

        if rc != 0:
            if session_id and _is_codex_stale(stderr_txt):
                logger.warning(
                    "Codex 세션 %s 만료 — 새 세션으로 재시도", session_id[:8])
                return await self.invoke_async(
                    messages, model=model, timeout=timeout,
                    session_id="", cwd=cwd, sandbox_mode=sandbox_mode,
                    approval_policy=approval_policy, mcp_config=mcp_config)
            msg = stderr_txt.strip()[:300] or f"exit={rc}"
            logger.error("Codex 실패 (code=%d): %s", rc, msg)
            return LLMResponse(
                content="", provider=self.provider_id, model=model,
                raw_stderr=stderr_txt, session_id=session_id,
                error=msg, error_type=classify_error(msg),
                exit_code=rc)

        stdout_txt = stdout_b.decode("utf-8", errors="replace")
        parsed = _parse_jsonl_events(stdout_txt)
        err = parsed.get("error", "")
        return LLMResponse(
            content=parsed["text"] if not err else "",
            provider=self.provider_id, model=model,
            tokens=parsed["usage"], latency_ms=latency,
            raw_stderr=stderr_txt,
            session_id=parsed["thread_id"] or session_id,
            error=err,
            error_type=classify_error(err) if err else "",
            exit_code=rc,
        )

    # ---------- 스트리밍 ----------

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           idle_timeout: int | None = None,
                           wall_timeout: int | None = None,
                           sandbox_mode: str | None = None,
                           approval_policy: str | None = None,
                           mcp_config: dict | None = None) -> AsyncIterator[StreamChunk]:
        """Codex exec --json JSONL 이벤트 스트리밍.

        공통 readline/timeout/cleanup 골격은 ``LLMProvider._run_stream_template``
        에 위임. Codex 의 JSONL event 해석만 ``_dispatch_stream_event`` 에서.

        정규화 매핑:
          thread.started                              → event (+ session_id)
          item.completed(agent_message)               → text
          item.completed(reasoning)                   → thinking
          item.completed(command_execution/tool_call) → tool_use
          turn.completed                              → (usage 저장)
          error / turn.failed                         → error
        """
        prompt = build_session_prompt(messages)
        # 만료된 thread 로 resume 하면 출력 없이 즉시 실패하므로, 첫 청크가
        # stale-session 에러일 때만 새 세션으로 1회 재시도한다 (claude 동일 패턴).
        attempt_sid = session_id
        for _attempt in range(2):
            cmd = self._build_cmd(prompt, model, cwd, attempt_sid,
                                  sandbox_mode=sandbox_mode,
                                  approval_policy=approval_policy,
                                  mcp_config=mcp_config)
            if cmd is None:
                yield StreamChunk(type="error", content="codex CLI not found")
                return
            state = StreamState(
                final_session_id=attempt_sid,
                final_usage=TokenUsage(
                    payload_prompt_tokens=estimate_payload_prompt_tokens(prompt),
                    prompt_tokens_reliable=False,
                    prompt_tokens_source="codex_cli_reported"))
            retry_stale = False
            emitted = False
            async for chunk in self._run_stream_template(
                    cmd, state, model=model, cwd=cwd, timeout=timeout,
                    idle_timeout=idle_timeout, wall_timeout=wall_timeout,
                    env=build_env()):
                if (attempt_sid and not emitted
                        and chunk.type == "error"
                        and _is_codex_stale(chunk.content)):
                    retry_stale = True
                    break
                emitted = True
                yield chunk
            if not retry_stale:
                return
            logger.warning(
                "Codex 세션 %s 만료 — 새 세션으로 스트림 재시도", attempt_sid[:8])
            attempt_sid = ""

    async def _dispatch_stream_event(self, evt: dict,
                                     state: StreamState) -> AsyncIterator[StreamChunk]:
        """Codex JSONL event 정규화."""
        etype = evt.get("type", "")
        if etype == "thread.started":
            if evt.get("thread_id"):
                state.final_session_id = evt["thread_id"]
            yield StreamChunk(type="event", data=evt,
                              session_id=state.final_session_id)
        elif etype == "item.completed":
            item = evt.get("item") or {}
            itype = item.get("type", "")
            if itype == "agent_message":
                text = item.get("text", "")
                if _is_codex_initial_greeting(text) and not state.text_parts:
                    return  # 초기 인사 무시
                if text:
                    state.text_parts.append(text)
                    yield StreamChunk(type="text", content=text, data=item)
            elif itype == "reasoning":
                yield StreamChunk(type="thinking",
                                  content=item.get("text", ""), data=item)
            elif itype in ("command_execution", "tool_call"):
                yield StreamChunk(type="tool_use", data=item)
            else:
                yield StreamChunk(type="event", data=evt)
        elif etype == "turn.completed":
            u = evt.get("usage") or {}
            pt = int(u.get("input_tokens") or 0)
            ct = int(u.get("output_tokens") or 0)
            cached = int(u.get("cached_input_tokens") or 0)
            prev = state.final_usage
            state.final_usage = TokenUsage(
                prompt_tokens=pt, completion_tokens=ct,
                total_tokens=pt + ct, cached_tokens=cached,
                payload_prompt_tokens=(prev.payload_prompt_tokens if prev else 0),
                prompt_tokens_reliable=False,
                prompt_tokens_source="codex_cli_reported")
        elif etype == "error":
            yield StreamChunk(type="error",
                              content=evt.get("message", ""), data=evt)
        elif etype == "turn.failed":
            err = evt.get("error") or {}
            yield StreamChunk(type="error",
                              content=err.get("message", ""), data=evt)
        else:
            yield StreamChunk(type="event", data=evt)


# ---------- JSONL 이벤트 파싱 유틸 ----------

def _parse_jsonl_events(stdout: str) -> dict:
    """codex exec --json stdout 전체를 파싱하여 text/thread_id/usage/error 추출.

    stdout이 JSONL이지만 가끔 `Reading additional input from stdin...` 같은
    비 JSON 메타 라인이 섞일 수 있으니 JSON 파싱 실패는 무시.

    error 추출:
      - {"type":"error","message":"..."}                 — 즉시 에러
      - {"type":"turn.failed","error":{"message":"..."}} — 턴 실패 (한도 등)
    """
    text_parts: list[str] = []
    thread_id = ""
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    error_msg = ""
    ignored_initial_greeting = False

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            # 유효한 JSON이지만 객체가 아닌 라인은 무시 — 호스트로 예외 전파 금지.
            continue
        etype = evt.get("type", "")
        if etype == "thread.started":
            tid = evt.get("thread_id")
            if tid:
                thread_id = tid
        elif etype == "item.completed":
            item = evt.get("item") or {}
            if item.get("type") == "agent_message":
                t = item.get("text", "")
                if t:
                    if _is_codex_initial_greeting(t) and not text_parts:
                        ignored_initial_greeting = True
                        continue
                    text_parts.append(t)
        elif etype == "turn.completed":
            u = evt.get("usage") or {}
            prompt_tokens += int(u.get("input_tokens") or 0)
            completion_tokens += int(u.get("output_tokens") or 0)
            cached_tokens += int(u.get("cached_input_tokens") or 0)
        elif etype == "error":
            msg = evt.get("message", "")
            if msg:
                error_msg = msg
        elif etype == "turn.failed":
            err = evt.get("error") or {}
            msg = err.get("message", "")
            if msg:
                error_msg = msg

    usage = TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cached_tokens=cached_tokens,
        prompt_tokens_reliable=False,
        prompt_tokens_source="codex_cli_reported",
    )
    return {
        "text": "".join(text_parts).strip(),
        "thread_id": thread_id,
        "usage": usage,
        "error": error_msg,
        "ignored_initial_greeting": ignored_initial_greeting,
    }


def _is_codex_initial_greeting(text: str) -> bool:
    return text.strip().lower() in _CODEX_INITIAL_GREETINGS


def _needs_initial_greeting_retry(parsed: dict, session_id: str) -> bool:
    return (
        not session_id
        and bool(parsed.get("ignored_initial_greeting"))
        and not parsed.get("text")
        and bool(parsed.get("thread_id"))
        and not parsed.get("error")
    )
