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
import json
import logging
import shutil
import subprocess
import time
from typing import AsyncIterator

from .base import (LLMProvider, build_session_prompt,
                   estimate_payload_prompt_tokens, health_from_response,
                   run_health_command)
from ..types import (ERROR_AUTH, ERROR_BINARY_MISSING, ERROR_TIMEOUT,
                     Message, LLMResponse, ProviderHealth, TokenUsage,
                     StreamChunk, classify_error)
from ..utils import build_env

logger = logging.getLogger(__name__)

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


class CodexProvider(LLMProvider):
    provider_id = "codex"
    supports_sessions = True
    supports_streaming = True

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
                   session_id: str) -> list[str] | None:
        """세션 상태에 따라 `codex exec` 또는 `codex exec resume`를 조립.

        바이너리 없으면 None 반환 (3-provider 정규화 계약: 호출자가 즉시
        binary_missing 으로 실패 처리). claude/copilot 와 동일 패턴.
        """
        bin_path = self._find_binary()
        if not bin_path:
            return None
        cmd = [bin_path, "exec"]
        if session_id:
            cmd += ["resume", "--json"]
            if self._full_auto:
                cmd.append("--full-auto")
            if self._skip_git:
                cmd.append("--skip-git-repo-check")
            if model:
                cmd += ["-m", model]
            cmd += [session_id, prompt]
            return cmd
        # 신규 세션
        cmd.append("--json")
        if self._full_auto:
            cmd.append("--full-auto")
        if self._skip_git:
            cmd.append("--skip-git-repo-check")
        if self._sandbox_mode:
            cmd += ["-s", self._sandbox_mode]
        if self._approval_policy:
            cmd += ["-a", self._approval_policy]
        if cwd:
            cmd += ["-C", cwd]
        if model:
            cmd += ["-m", model]
        cmd.append(prompt)
        return cmd

    # ---------- 동기 ----------

    def invoke(self, messages: list[Message], *,
               model: str = "", timeout: int = 120,
               session_id: str = "",
               cwd: str | None = None) -> LLMResponse:
        # 세션이 히스토리 소유 — system 지시와 최신 user 요청만 전달
        prompt = build_session_prompt(messages)
        cmd = self._build_cmd(prompt, model, cwd, session_id)
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
                    self._build_cmd(prompt, model, cwd, first_thread_id),
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
                           cwd: str | None = None) -> LLMResponse:
        prompt = build_session_prompt(messages)
        cmd = self._build_cmd(prompt, model, cwd, session_id)
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
                logger.error("Codex 타임아웃 (%d초)", timeout)
                return LLMResponse(content="", provider=self.provider_id,
                                    model=model, session_id=session_id,
                                    error=f"timeout after {timeout}s",
                                    error_type="timeout",
                                    exit_code=124)
            latency = int((time.time() - start) * 1000)

            if proc.returncode != 0:
                stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace")
                msg = stderr_txt.strip()[:300] or f"exit={proc.returncode}"
                logger.error("Codex 실패 (code=%d): %s",
                             proc.returncode, msg)
                return LLMResponse(
                    content="", provider=self.provider_id, model=model,
                    raw_stderr=stderr_txt, session_id=session_id,
                    error=msg, error_type=classify_error(msg),
                    exit_code=proc.returncode)

            stdout_txt = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace")
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
                exit_code=proc.returncode,
            )
        except FileNotFoundError:
            logger.error("codex CLI를 찾을 수 없습니다")
            return LLMResponse(content="", provider=self.provider_id, model=model,
                                error="codex CLI not found",
                                error_type=ERROR_BINARY_MISSING,
                                exit_code=127)

    # ---------- 스트리밍 ----------

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None,
                           idle_timeout: int | None = None,
                           wall_timeout: int | None = None) -> AsyncIterator[StreamChunk]:
        """Codex exec --json JSONL 이벤트 스트리밍.

        정규화 매핑:
          thread.started       → event (+ session_id)
          turn.started         → event
          item.completed(agent_message)   → text
          item.completed(reasoning)       → thinking
          item.completed(command_execution/tool_call) → tool_use
          turn.completed       → (usage 저장, 마지막 done 청크에서 방출)
        """
        prompt = build_session_prompt(messages)
        cmd = self._build_cmd(prompt, model, cwd, session_id)
        if cmd is None:
            yield StreamChunk(type="error", content="codex CLI not found")
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
                payload_prompt_tokens=estimate_payload_prompt_tokens(prompt),
                prompt_tokens_reliable=False,
                prompt_tokens_source="codex_cli_reported")
            final_sid = session_id
            timed_out = False
            # `timeout`은 wall-clock deadline이 아니라 **마지막 청크 이후 idle 한도**.
            # 진행 중 청크가 들어오면 매번 last_activity 갱신 → 사고/도구 호출 시간은
            # timeout에서 차감되지 않는다.
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
                if etype == "thread.started":
                    if evt.get("thread_id"):
                        final_sid = evt["thread_id"]
                    yield StreamChunk(type="event", data=evt,
                                       session_id=final_sid)
                elif etype == "item.completed":
                    item = evt.get("item") or {}
                    itype = item.get("type", "")
                    if itype == "agent_message":
                        text = item.get("text", "")
                        if _is_codex_initial_greeting(text) and not text_parts:
                            continue
                        if text:
                            text_parts.append(text)
                            yield StreamChunk(type="text", content=text,
                                               data=item)
                    elif itype == "reasoning":
                        yield StreamChunk(type="thinking",
                                           content=item.get("text", ""),
                                           data=item)
                    elif itype in ("command_execution", "tool_call"):
                        yield StreamChunk(type="tool_use", data=item)
                    else:
                        yield StreamChunk(type="event", data=evt)
                elif etype == "turn.completed":
                    u = evt.get("usage") or {}
                    pt = int(u.get("input_tokens") or 0)
                    ct = int(u.get("output_tokens") or 0)
                    cached = int(u.get("cached_input_tokens") or 0)
                    final_usage = TokenUsage(
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=pt + ct,
                        cached_tokens=cached,
                        payload_prompt_tokens=estimate_payload_prompt_tokens(prompt),
                        prompt_tokens_reliable=False,
                        prompt_tokens_source="codex_cli_reported")
                elif etype == "error":
                    msg = evt.get("message", "")
                    yield StreamChunk(type="error", content=msg, data=evt)
                elif etype == "turn.failed":
                    err = evt.get("error") or {}
                    msg = err.get("message", "")
                    yield StreamChunk(type="error", content=msg, data=evt)
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
            yield StreamChunk(type="error", content="codex CLI not found")
        except Exception as e:
            logger.exception("Codex stream 예외")
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            yield StreamChunk(type="error", content=str(e))


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
