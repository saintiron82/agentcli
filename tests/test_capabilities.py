"""provider capability 제어기 테스트 — 어느 기능이 어느 provider 에서/어느 OS
에서 되는지 선언·질의.

OS 의존 항목(claude 세션은 Windows 에서 False, 프로세스그룹 teardown POSIX 전용)
도 함께 검증한다.
"""
import pytest

from agentcli import LLMClient, MemoryStore, ProviderCapabilities
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider


def _client():
    return LLMClient(MemoryStore())


def test_claude_capabilities_flags_and_options():
    caps = ClaudeProvider().capabilities()
    assert caps.provider == "claude"
    assert caps.token_streaming is True
    assert caps.session_recovery is True
    assert caps.session_liveness is True
    # 옵션은 시그니처에서 자동 유래 — lean/debug/partial_messages 포함
    for opt in ("lean", "debug", "debug_log_path", "partial_messages",
                "mcp_config", "permission_mode"):
        assert opt in caps.options, f"{opt} 누락"


def test_codex_capabilities_differ_from_claude():
    caps = CodexProvider().capabilities()
    assert caps.token_streaming is False            # 블록 단위
    assert caps.session_recovery is True
    assert "sandbox_mode" in caps.options
    assert "approval_policy" in caps.options
    # claude 전용 옵션은 codex 에 없다 ("claude된 codex 안됨")
    assert "lean" not in caps.options
    assert "partial_messages" not in caps.options


def test_client_supports_query():
    c = _client()
    assert c.supports("claude", "lean") is True
    assert c.supports("codex", "lean") is False
    assert c.supports("claude", "token_streaming") is True
    assert c.supports("codex", "token_streaming") is False
    assert c.supports("claude", "sessions") is True


def test_client_unsupported_options_guides():
    c = _client()
    # codex 는 sandbox_mode 만 받고 lean/debug 는 안 받는다
    # codex 는 debug(스트리밍 계측)·sandbox_mode 를 받지만 lean 은 claude 전용.
    assert c.unsupported_options(
        "codex", {"lean": 1, "debug": 1, "sandbox_mode": "x"}) == ["lean"]
    # claude 는 셋 다 받음(단 sandbox_mode 는 claude 옵션 아님) → sandbox_mode 만 미지원
    assert c.unsupported_options(
        "claude", {"lean": 1, "debug": 1, "sandbox_mode": 1}) == ["sandbox_mode"]
    assert c.unsupported_options("claude", None) == []


def test_capability_debug_flag_cross_provider():
    """debug 계측은 claude/codex/copilot(stream-json·JSONL) 지원, kiro(ACP) 미지원."""
    c = _client()
    assert c.capabilities("claude").debug is True
    assert c.capabilities("codex").debug is True
    assert c.capabilities("copilot").debug is True
    assert c.capabilities("kiro").debug is False
    assert c.supports("codex", "debug") is True       # 옵션으로도 노출
    m = c.capability_matrix()
    assert m["codex"]["debug"] is True
    assert m["kiro"]["debug"] is False


def test_capability_matrix_lists_all_providers():
    m = _client().capability_matrix()
    assert {"claude", "codex", "copilot", "kiro"} <= set(m)
    assert m["claude"]["token_streaming"] is True
    assert m["copilot"]["session_liveness"] is False


def test_capabilities_unknown_provider_raises():
    with pytest.raises(ValueError):
        _client().capabilities("nope")


def test_provider_capabilities_helpers():
    caps = ProviderCapabilities(
        provider="x", sessions=True, streaming=True, token_streaming=False,
        session_recovery=False, session_liveness=False,
        options=frozenset({"foo"}))
    assert caps.supports("sessions") is True
    assert caps.supports("foo") is True          # 옵션 이름
    assert caps.supports("token_streaming") is False
    assert caps.supports("nonexistent") is False
    assert caps.to_dict()["options"] == ["foo"]


# ===== OS 의존 capability =====

def test_claude_capabilities_windows_no_sessions(monkeypatch):
    """Windows(stateless) 시뮬: claude 세션 capability 가 False 로 반영."""
    p = ClaudeProvider()
    monkeypatch.setattr(p, "supports_sessions", False)
    caps = p.capabilities()
    assert caps.sessions is False
    # 토큰 스트리밍/복구 같은 비-세션 capability 는 그대로
    assert caps.token_streaming is True


def test_client_capabilities_windows_note(monkeypatch):
    """OS 의존 — Windows claude 면 notes 에 단서를 단다."""
    c = _client()
    p = c._registry.get("claude")
    monkeypatch.setattr(p, "supports_sessions", False)
    caps = c.capabilities("claude")
    assert caps.sessions is False
    assert "Windows" in caps.notes


def test_capability_matrix_reflects_os(monkeypatch):
    c = _client()
    p = c._registry.get("claude")
    monkeypatch.setattr(p, "supports_sessions", False)
    assert c.capability_matrix()["claude"]["sessions"] is False
