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
    # The reasoning event is emitted by AcpProvider._stream; these overrides only
    # resolve reasoning and forward it.
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
            env = self._child_env()
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
