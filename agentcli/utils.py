"""유틸리티 — 토큰 파싱, 메시지 직렬화, 환경변수."""

import os
import re
import subprocess
from .types import Message

# GITHUB_TOKEN 캐시 (프로세스 수명 동안 1회 발급).
# 캐시 무효화가 필요하면 clear_gh_token_cache() 호출.
_GH_TOKEN_CACHE: str | None = None
_GH_TOKEN_RESOLVED: bool = False


def parse_tokens(stderr: str) -> int:
    """stderr에서 토큰 사용량 추출 (Codex/Copilot legacy fallback)."""
    if not stderr:
        return 0
    match = re.search(r'tokens?\s*used\s*\n?\s*([\d,]+)', stderr, re.IGNORECASE)
    if match:
        return int(match.group(1).replace(",", ""))
    match = re.search(r'total\s*tokens?\s*:?\s*([\d,]+)', stderr, re.IGNORECASE)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def serialize_messages(messages: list[Message]) -> str:
    """메시지 목록을 텍스트로 직렬화 (비세션 provider 롤링 컨텍스트용).

    agent 필드가 있으면 `[role:agent] content` 형식으로, 없으면 `[role] content`.
    """
    if not messages:
        return ""
    lines = []
    for msg in messages:
        tag = f"{msg.role}:{msg.agent}" if msg.agent else msg.role
        lines.append(f"[{tag}] {msg.content}")
    return "\n".join(lines)


def build_env() -> dict:
    """subprocess 환경변수 구성. GitHub 토큰 주입 (캐시 사용)."""
    global _GH_TOKEN_CACHE, _GH_TOKEN_RESOLVED
    env = os.environ.copy()
    if "GITHUB_TOKEN" in env:
        return env
    if not _GH_TOKEN_RESOLVED:
        try:
            token = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()
            _GH_TOKEN_CACHE = token or None
        except Exception:
            _GH_TOKEN_CACHE = None
        _GH_TOKEN_RESOLVED = True
    if _GH_TOKEN_CACHE:
        env["GITHUB_TOKEN"] = _GH_TOKEN_CACHE
    return env


def clear_gh_token_cache() -> None:
    """gh 토큰 캐시를 초기화 (테스트 및 수동 갱신용)."""
    global _GH_TOKEN_CACHE, _GH_TOKEN_RESOLVED
    _GH_TOKEN_CACHE = None
    _GH_TOKEN_RESOLVED = False
