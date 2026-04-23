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

from .base import LLMProvider
from ..types import Message, LLMResponse, TokenUsage, StreamChunk
from ..utils import build_env

logger = logging.getLogger(__name__)

CODEX_MODELS = [
    {"id": "", "name": "기본 (자동)"},
    {"id": "o4-mini", "name": "o4-mini"},
    {"id": "o3", "name": "o3"},
    {"id": "gpt-4.1", "name": "GPT-4.1"},
    {"id": "gpt-5-codex", "name": "GPT-5 Codex"},
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

    def list_models(self) -> list[dict]:
        return list(CODEX_MODELS)

    def _build_cmd(self, prompt: str, model: str, cwd: str | None,
                   session_id: str) -> list[str]:
        """세션 상태에 따라 `codex exec` 또는 `codex exec resume`를 조립."""
        cmd = ["codex", "exec"]
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
        empty = LLMResponse(content="", provider=self.provider_id, model=model)
        # 세션이 히스토리 소유 — 마지막 user 메시지만 전달
        prompt = messages[-1].content if messages else ""
        cmd = self._build_cmd(prompt, model, cwd, session_id)

        start = time.time()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL, env=build_env(), cwd=cwd)
            latency = int((time.time() - start) * 1000)

            if result.returncode != 0:
                logger.error("Codex 실패 (code=%d): %s",
                             result.returncode, result.stderr[:300])
                return empty

            parsed = _parse_jsonl_events(result.stdout)
            return LLMResponse(
                content=parsed["text"], provider=self.provider_id, model=model,
                tokens=parsed["usage"], latency_ms=latency,
                raw_stderr=result.stderr,
                session_id=parsed["thread_id"] or session_id,
            )
        except subprocess.TimeoutExpired:
            logger.error("Codex 타임아웃 (%d초)", timeout)
            return empty
        except FileNotFoundError:
            logger.error("codex CLI를 찾을 수 없습니다")
            return empty

    # ---------- 비동기 ----------

    async def invoke_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> LLMResponse:
        empty = LLMResponse(content="", provider=self.provider_id, model=model)
        prompt = messages[-1].content if messages else ""
        cmd = self._build_cmd(prompt, model, cwd, session_id)

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
                return empty
            latency = int((time.time() - start) * 1000)

            if proc.returncode != 0:
                stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace")
                logger.error("Codex 실패 (code=%d): %s",
                             proc.returncode, stderr_txt[:300])
                return empty

            stdout_txt = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace")
            parsed = _parse_jsonl_events(stdout_txt)
            return LLMResponse(
                content=parsed["text"], provider=self.provider_id, model=model,
                tokens=parsed["usage"], latency_ms=latency,
                raw_stderr=stderr_txt,
                session_id=parsed["thread_id"] or session_id,
            )
        except FileNotFoundError:
            logger.error("codex CLI를 찾을 수 없습니다")
            return empty

    # ---------- 스트리밍 ----------

    async def stream_async(self, messages: list[Message], *,
                           model: str = "", timeout: int = 120,
                           session_id: str = "",
                           cwd: str | None = None) -> AsyncIterator[StreamChunk]:
        """Codex exec --json JSONL 이벤트 스트리밍.

        정규화 매핑:
          thread.started       → event (+ session_id)
          turn.started         → event
          item.completed(agent_message)   → text
          item.completed(reasoning)       → thinking
          item.completed(command_execution/tool_call) → tool_use
          turn.completed       → (usage 저장, 마지막 done 청크에서 방출)
        """
        prompt = messages[-1].content if messages else ""
        cmd = self._build_cmd(prompt, model, cwd, session_id)

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
            final_sid = session_id
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
                        cached_tokens=cached)
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
            yield StreamChunk(type="error", content="codex CLI not found")
        except Exception as e:
            logger.exception("Codex stream 예외")
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            yield StreamChunk(type="error", content=str(e))


# ---------- JSONL 이벤트 파싱 유틸 ----------

def _parse_jsonl_events(stdout: str) -> dict:
    """codex exec --json stdout 전체를 파싱하여 text/thread_id/usage 추출.

    stdout이 JSONL이지만 가끔 `Reading additional input from stdin...` 같은
    비 JSON 메타 라인이 섞일 수 있으니 JSON 파싱 실패는 무시.
    """
    text_parts: list[str] = []
    thread_id = ""
    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0

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
                    text_parts.append(t)
        elif etype == "turn.completed":
            u = evt.get("usage") or {}
            prompt_tokens += int(u.get("input_tokens") or 0)
            completion_tokens += int(u.get("output_tokens") or 0)
            cached_tokens += int(u.get("cached_input_tokens") or 0)

    usage = TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cached_tokens=cached_tokens,
    )
    return {
        "text": "".join(text_parts).strip(),
        "thread_id": thread_id,
        "usage": usage,
    }
