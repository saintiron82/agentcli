"""Kiro CLI 프로바이더 — `kiro-cli acp` (ACP, JSON-RPC 2.0 over stdio).

turn 드라이버는 acp_base.AcpProvider 에 있다. ACP 필드명은 ACP 표준 스펙
(https://agentclientprotocol.com) 기준 — 실 kiro-cli 대상 미검증.
"""
import asyncio  # noqa: F401  (tests patch agentcli.providers.kiro.asyncio.wait_for)
import shutil
import time  # noqa: F401  (tests patch agentcli.providers.kiro.time.*)

from ..auth import AuthProfileError
from ..auth_context import current_auth_profile
from ..types import ERROR_BINARY_MISSING, ProviderHealth
from .acp_base import AcpProvider, _map_session_update  # noqa: F401  (re-export)
from .base import run_health_command

# 모델 selector 는 resolve_model(비-strict) 로 그대로 통과시킨다.
# 알려진 id 는 kiro-cli 가 노출하면 확장 가능.
KIRO_MODELS = [
    {"id": "", "name": "기본", "aliases": ["default"]},
]


class KiroProvider(AcpProvider):
    provider_id = "kiro"
    binary_name = "kiro-cli"
    usage_source = "kiro_cli_reported"

    def __init__(self, *, trust_all: bool = True,
                 trust_tools: list[str] | None = None,
                 model: str = "", agent: str = "",
                 allow_file_reads: bool = True,
                 allow_file_writes: bool | None = None,
                 max_file_bytes: int = 1024 * 1024):
        """
        Args:
            trust_all: session/request_permission 을 전부 자동 승인 (dev 기본).
                **WARNING**: 기본값 True 는 모든 도구 호출을 자동 승인한다.
                multi-tenant/untrusted 임베딩 시 False + trust_tools 로 명시 허용 도구만
                좁혀야 한다.
            trust_tools: trust_all=False 일 때 자동 승인할 도구 이름 목록.
            model: 기본 모델 selector (호출 인자가 우선).
            agent: kiro agent 이름 (선택).
        """
        super().__init__(trust_all=trust_all, trust_tools=trust_tools, model=model,
                         allow_file_reads=allow_file_reads,
                         allow_file_writes=allow_file_writes,
                         max_file_bytes=max_file_bytes)
        self._agent = agent

    def _command(self, binary: str, model: str, reasoning) -> list[str]:
        cmd = [binary, "acp"]
        if self._agent:
            cmd += ["--agent", self._agent]
        return cmd

    def list_models(self) -> list[dict]:
        return list(KIRO_MODELS)

    def health_check(self, *, timeout: int = 10,
                     cwd: str | None = None,
                     probe: bool = False) -> ProviderHealth:
        if current_auth_profile() is not None:
            raise AuthProfileError("Kiro service auth profiles are not supported")
        bin_path = shutil.which("kiro-cli")
        if not bin_path:
            return ProviderHealth(
                provider=self.provider_id, ok=False, status="binary_missing",
                available=False, auth_ok=False,
                error_type=ERROR_BINARY_MISSING,
                message="kiro-cli not found")
        version_proc = run_health_command([bin_path, "--version"], timeout=timeout)
        version = (version_proc.stdout or version_proc.stderr).strip()
        return ProviderHealth(
            provider=self.provider_id, ok=True, status="ok", available=True,
            binary=bin_path, version=version, auth_ok=None,
            message="kiro-cli available")
