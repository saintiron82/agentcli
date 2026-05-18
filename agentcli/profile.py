"""AgentProfile — 에이전트 설정을 단일 진실원천으로 관리.

목적:
  - 여러 프로젝트·CLI 간 base instructions 드리프트 방지
  - 원본 디렉토리 1개 → 각 프로젝트 cwd에 AGENTS.md/CLAUDE.md 자동 자재화
  - 라이브러리 호출 시점에 자동 배포 + 해시 기반 드리프트 관찰

디렉토리 규약:
  <profile_root>/
    <name>/
      profile.json          # 메타데이터 (선택)
      AGENTS.md             # 지시문 (cross-agent 표준, 필수)
      skills/               # 선택 — agent skills (복사 대상)
      mcp.json              # 선택 — MCP 서버 설정

materialize 규약:
  - AGENTS.md 와 CLAUDE.md 모두 cwd에 씀 (Claude Code는 CLAUDE.md만 읽음)
  - 첫 줄에 `<!-- managed-by: agentcli AgentProfile ... -->` 마커
  - 마커가 없는 기존 파일은 **덮어쓰지 않음** (사용자 편집 보호)
    → 대신 AGENTS.override.md에 씀 (Codex 표준 준수)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, TYPE_CHECKING

if TYPE_CHECKING:
    from .client import LLMClient
    from .types import LLMResponse, StreamChunk

logger = logging.getLogger(__name__)

MANAGED_MARKER = "<!-- managed-by: agentcli AgentProfile -->"
# 하위 호환: libs.llm 시절 생성된 파일도 "managed"로 인식하여 덮어쓰기 허용.
_LEGACY_MARKERS = ("<!-- managed-by: libs.llm AgentProfile -->",)
MARKER_FILES = ("AGENTS.md", "CLAUDE.md")


@dataclass
class AgentProfile:
    """에이전트 프로필 — 지시문과 구성 설정을 함께 들고 있음."""
    name: str
    instructions: str = ""             # 인라인 지시문 (우선)
    instructions_file: Path | None = None  # 파일 경로 (인라인이 비었을 때)
    model: str = ""
    provider: str = "claude"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    default_cwd: Path | None = None
    skills_dir: Path | None = None     # 이 프로필이 가진 skill 번들 디렉토리
    mcp_config: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    # 내부 트래킹
    source_dir: Path | None = None     # 원본 디렉토리 (from_dir 사용 시)

    # ------- 팩토리 -------

    @classmethod
    def from_dir(cls, path: str | Path, name: str | None = None) -> "AgentProfile":
        """디렉토리에서 프로필 로드.

        디렉토리 구조:
          <path>/
            profile.json   (선택)
            AGENTS.md      (지시문 — 없으면 CLAUDE.md 대체 검색)
            skills/        (선택)
            mcp.json       (선택)
        """
        path = Path(path).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"프로필 디렉토리 없음: {path}")

        meta = {}
        meta_file = path / "profile.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))

        # 지시문 파일 찾기
        inst_file = None
        for cand in ("AGENTS.md", "CLAUDE.md", "AGENT.md", "agent.md"):
            f = path / cand
            if f.exists():
                inst_file = f
                break

        skills_dir = path / "skills"
        if not skills_dir.exists():
            skills_dir = None

        mcp_config = {}
        mcp_file = path / "mcp.json"
        if mcp_file.exists():
            try:
                mcp_config = json.loads(mcp_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("mcp.json 파싱 실패: %s", mcp_file)

        profile_name = name or meta.get("name") or path.name

        return cls(
            name=profile_name,
            instructions=meta.get("instructions", ""),
            instructions_file=inst_file,
            model=meta.get("model", ""),
            provider=meta.get("provider", "claude"),
            allowed_tools=list(meta.get("allowed_tools", [])),
            disallowed_tools=list(meta.get("disallowed_tools", [])),
            default_cwd=Path(meta["default_cwd"]).expanduser()
                if meta.get("default_cwd") else None,
            skills_dir=skills_dir,
            mcp_config=mcp_config,
            metadata={k: v for k, v in meta.items()
                     if k not in {"name", "instructions", "model", "provider",
                                  "allowed_tools", "disallowed_tools",
                                  "default_cwd"}},
            source_dir=path,
        )

    # ------- 조회 -------

    def resolve_instructions(self) -> str:
        """실제 지시문 텍스트 반환 (inline > file).

        파일 기반일 때 mtime 캐시로 중복 read 회피.
        """
        if self.instructions:
            return self.instructions
        if self.instructions_file and self.instructions_file.exists():
            try:
                stat = self.instructions_file.stat()
                cache = self.__dict__.get("_inst_cache")
                if cache and cache[0] == stat.st_mtime:
                    return cache[1]
                text = self.instructions_file.read_text(encoding="utf-8")
                self.__dict__["_inst_cache"] = (stat.st_mtime, text)
                return text
            except OSError:
                return ""
        return ""

    def hash(self) -> str:
        """프로필의 stable 해시 (지시문 + 핵심 설정)."""
        h = hashlib.sha256()
        h.update(self.resolve_instructions().encode("utf-8"))
        h.update(self.model.encode())
        h.update(self.provider.encode())
        h.update(",".join(sorted(self.allowed_tools)).encode())
        h.update(",".join(sorted(self.disallowed_tools)).encode())
        return h.hexdigest()[:16]

    # ------- 자재화 -------

    def materialize(self, cwd: str | Path,
                    *, files: tuple[str, ...] = MARKER_FILES,
                    include_skills: bool = True) -> dict:
        """cwd에 지시문 파일을 자재화 (AGENTS.md, CLAUDE.md 등).

        규칙:
          - 타겟 파일이 존재하지 않거나 managed marker를 포함하면 덮어쓴다.
          - 사용자 편집 파일(마커 없음)은 건드리지 않고,
            대신 `AGENTS.override.md`에 씀 (Codex 관례).
          - skills_dir이 있으면 `.agents/skills/`로 복사 (include_skills=True).

        Returns:
          {"files_written": [...], "skipped": [...], "hash": "<16hex>",
           "skills_copied": [...]}
        """
        cwd = Path(cwd).expanduser().resolve()
        cwd.mkdir(parents=True, exist_ok=True)
        inst_text = self.resolve_instructions()
        if not inst_text.strip():
            logger.warning("profile '%s': 지시문이 비어 있음", self.name)

        content = self._wrap_with_marker(inst_text)
        written: list[str] = []
        skipped: list[str] = []

        for fname in files:
            target = cwd / fname
            if self._can_write(target):
                target.write_text(content, encoding="utf-8")
                written.append(str(target))
            else:
                # 사용자 파일 보호: override 파일에 쓴다
                override = cwd / "AGENTS.override.md"
                if self._can_write(override):
                    override.write_text(content, encoding="utf-8")
                    written.append(str(override))
                skipped.append(str(target))
                break  # 다른 마커 파일들도 같은 이유로 스킵 가능 → 한 번만 override

        skills_copied: list[str] = []
        if include_skills and self.skills_dir and self.skills_dir.exists():
            target_skills = cwd / ".agents" / "skills"
            target_skills.mkdir(parents=True, exist_ok=True)
            for skill_path in self.skills_dir.iterdir():
                if skill_path.is_dir():
                    dst = target_skills / skill_path.name
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(skill_path, dst)
                    skills_copied.append(str(dst))

        return {
            "files_written": written,
            "skipped": skipped,
            "skills_copied": skills_copied,
            "hash": self.hash(),
            "name": self.name,
            "cwd": str(cwd),
            "materialized_at": datetime.now().isoformat(),
        }

    def _wrap_with_marker(self, text: str) -> str:
        src = str(self.source_dir) if self.source_dir else "<inline>"
        header = (
            f"{MANAGED_MARKER}\n"
            f"<!-- profile: {self.name} | source: {src} | "
            f"hash: {self.hash()} -->\n"
            f"<!-- do-not-edit manually; regenerate via AgentProfile.materialize -->\n\n"
        )
        return header + text.rstrip() + "\n"

    @staticmethod
    def _can_write(target: Path) -> bool:
        """덮어써도 안전한가? (존재하지 않거나 marker 포함)."""
        if not target.exists():
            return True
        try:
            first_chunk = target.read_text(encoding="utf-8", errors="replace")[:1024]
        except OSError:
            return False
        if MANAGED_MARKER in first_chunk:
            return True
        return any(m in first_chunk for m in _LEGACY_MARKERS)

    # ------- 호출 편의 -------

    def _resolve_cwd(self, cwd: str | Path | None) -> str | None:
        """호출 시 cwd 결정. 인자 > default_cwd."""
        if cwd:
            return str(Path(cwd).expanduser())
        if self.default_cwd:
            return str(self.default_cwd)
        return None

    def _build_kwargs(self, *, owner: str, cwd_override, overrides: dict) -> dict:
        """LLMClient 호출용 kwargs 생성 (profile 기본값 + overrides)."""
        resolved_cwd = self._resolve_cwd(cwd_override)
        kwargs = {
            "provider": self.provider,
            "model": self.model,
            "alias": self.name,
            "owner": owner or self.metadata.get("owner", "") or "",
            "cwd": resolved_cwd,
            "system_prompt": self.resolve_instructions(),
        }
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return kwargs

    async def chat_async(self, prompt: str, *,
                         client: LLMClient | None = None,
                         owner: str = "",
                         cwd: str | Path | None = None,
                         materialize: bool = False,
                         **overrides) -> "LLMResponse":
        """이 프로필 설정으로 비동기 호출.

        Args:
            client: 주입할 LLMClient. None이면 기본 싱글톤을 쓴다.
            owner: conversation owner (미지정 시 profile 메타에서 시도).
            cwd: 호출별 cwd 오버라이드 (없으면 default_cwd).
            materialize: True면 호출 전에 cwd에 AGENTS.md 자재화.
            **overrides: LLMClient.chat_async의 다른 kwargs.
        """
        if client is None:
            client = _get_default_client()
        kwargs = self._build_kwargs(owner=owner, cwd_override=cwd,
                                     overrides=overrides)
        if materialize and kwargs.get("cwd"):
            self.materialize(kwargs["cwd"])
        return await client.chat_async(prompt, **kwargs)

    def chat(self, prompt: str, *,
             client: LLMClient | None = None,
             owner: str = "",
             cwd: str | Path | None = None,
             materialize: bool = False,
             **overrides) -> "LLMResponse":
        """동기 버전 chat."""
        if client is None:
            client = _get_default_client()
        kwargs = self._build_kwargs(owner=owner, cwd_override=cwd,
                                     overrides=overrides)
        if materialize and kwargs.get("cwd"):
            self.materialize(kwargs["cwd"])
        return client.chat(prompt, **kwargs)

    async def chat_stream(self, prompt: str, *,
                          client: LLMClient | None = None,
                          owner: str = "",
                          cwd: str | Path | None = None,
                          materialize: bool = False,
                          **overrides) -> AsyncIterator["StreamChunk"]:
        """스트리밍 호출."""
        if client is None:
            client = _get_default_client()
        kwargs = self._build_kwargs(owner=owner, cwd_override=cwd,
                                     overrides=overrides)
        if materialize and kwargs.get("cwd"):
            self.materialize(kwargs["cwd"])
        async for chunk in client.chat_stream(prompt, **kwargs):
            yield chunk


# ------- 기본 LLMClient 싱글톤 (편의용) -------

_default_client: "LLMClient | None" = None


def _get_default_client() -> "LLMClient":
    """지연 로드되는 기본 LLMClient. MemoryStore 기반."""
    global _default_client
    if _default_client is None:
        from .client import LLMClient
        from .store.memory import MemoryStore
        _default_client = LLMClient(store=MemoryStore(max_conversations=200,
                                                      ttl_hours=24))
    return _default_client


def set_default_client(client: "LLMClient") -> None:
    """기본 클라이언트를 외부에서 주입 (예: SQLite 기반)."""
    global _default_client
    _default_client = client


# ------- Registry -------

class AgentRegistry:
    """여러 AgentProfile을 이름으로 관리."""

    def __init__(self):
        self._profiles: dict[str, AgentProfile] = {}

    def register(self, profile: AgentProfile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> AgentProfile | None:
        return self._profiles.get(name)

    def list(self) -> list[AgentProfile]:
        return sorted(self._profiles.values(), key=lambda p: p.name)

    def names(self) -> list[str]:
        return sorted(self._profiles)

    def __len__(self) -> int:
        return len(self._profiles)

    def __contains__(self, name: str) -> bool:
        return name in self._profiles

    @classmethod
    def from_dir(cls, root: str | Path,
                 on_error: str = "warn") -> "AgentRegistry":
        """루트 디렉토리의 모든 하위 디렉토리를 프로필로 로드.

        Args:
            root: 프로필들을 담은 상위 디렉토리
            on_error: 'warn' | 'raise' | 'ignore' — 로드 실패 시 동작
        """
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"레지스트리 루트 없음: {root}")

        reg = cls()
        for sub in sorted(root.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            try:
                reg.register(AgentProfile.from_dir(sub))
            except Exception as e:
                if on_error == "raise":
                    raise
                if on_error == "warn":
                    logger.warning("프로필 로드 실패: %s — %s", sub, e)
        return reg

    def materialize_all(self, cwd: str | Path,
                        *, names: list[str] | None = None) -> list[dict]:
        """지정한 프로필(또는 전체)을 한 cwd에 일괄 자재화.

        주의: 동일 cwd에 여러 프로필을 자재화하면 AGENTS.md가 마지막 것으로 덮임.
        보통은 프로필별로 `default_cwd`가 다를 때 사용.
        """
        target_names = names or self.names()
        results = []
        for n in target_names:
            prof = self.get(n)
            if prof is None:
                continue
            results.append(prof.materialize(cwd))
        return results
