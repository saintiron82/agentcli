"""세션 liveness 확인 API (provider.session_alive + LLMClient.session_alive).

claude/codex 는 세션 파일 존재로 판정, copilot 등은 None(미지원). 파일 기반
판정은 POSIX HOME 환경에서 검증.
"""
import os

import pytest

from agentcli.client import LLMClient
from agentcli.providers.base import LLMProvider
from agentcli.providers.claude import ClaudeProvider
from agentcli.providers.codex import CodexProvider
from agentcli.providers.copilot import CopilotProvider
from agentcli.providers.registry import ProviderRegistry
from agentcli.store.memory import MemoryStore
from agentcli.types import LLMResponse, Message, TokenUsage

_posix = pytest.mark.skipif(os.name != "posix", reason="HOME-based file check is POSIX")


@_posix
def test_claude_session_alive_file_check(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = ClaudeProvider()
    if not p.supports_sessions:
        pytest.skip("claude stateless on this platform")
    cwd = "/work/proj"
    sid = "abc-123"
    d = tmp_path / ".claude" / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True)
    assert p.session_alive(sid, cwd=cwd) is False          # 파일 없음 → 죽음
    (d / f"{sid}.jsonl").write_text("{}", encoding="utf-8")
    assert p.session_alive(sid, cwd=cwd) is True           # 파일 있음 → 살아있음
    assert p.session_alive("", cwd=cwd) is None             # sid 없음 → 판단불가


@_posix
def test_codex_session_alive_glob(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = CodexProvider()
    sid = "019eabcd-1234-7000-aaaa-bbbbbbbbbbbb"
    sess = tmp_path / ".codex" / "sessions" / "2026" / "06" / "26"
    sess.mkdir(parents=True)
    assert p.session_alive(sid) is False
    (sess / f"rollout-2026-06-26T00-00-00-{sid}.jsonl").write_text("{}", encoding="utf-8")
    assert p.session_alive(sid) is True
    assert p.session_alive("") is None


def test_copilot_session_alive_unknown():
    # 불투명 provider — 기본 None(판단 불가) 유지.
    assert CopilotProvider().session_alive("anything") is None


class _FakeProvider(LLMProvider):
    provider_id = "fake"
    supports_sessions = True
    stores_history = False

    def __init__(self):
        self.asked = None

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None):
        return LLMResponse(content="ok", provider="fake", model=model,
                           session_id="SID-1", tokens=TokenUsage())

    def list_models(self):
        return [{"id": "", "name": "fake"}]

    def is_available(self):
        return True

    def session_alive(self, session_id, *, cwd=None):
        self.asked = (session_id, cwd)
        return True


def _client_with(prov):
    reg = ProviderRegistry()
    reg.register(prov)
    reg.set_fallback_order([prov.provider_id])
    return LLMClient(MemoryStore(), registry=reg)


def test_client_session_alive_no_session_returns_false():
    client = _client_with(_FakeProvider())
    assert client.session_alive("fake", owner="o", alias="missing") is False


def test_client_session_alive_resolves_and_delegates():
    prov = _FakeProvider()
    client = _client_with(prov)
    # 호출 한 번 → conv + 세션 메타데이터 생성
    client.chat("hi", provider="fake", owner="o", alias="a")
    result = client.session_alive("fake", owner="o", alias="a", cwd="/x")
    assert result is True
    assert prov.asked == ("SID-1", "/x"), "저장된 sid 와 cwd 를 provider 에 전달"


def test_client_session_alive_unknown_provider_raises():
    client = _client_with(_FakeProvider())
    with pytest.raises(ValueError):
        client.session_alive("nope", owner="o", alias="a")
