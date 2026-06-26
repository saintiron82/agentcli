"""세션 liveness 확인 API (provider.session_alive + LLMClient.session_alive).

claude/codex 는 세션 파일 존재로 판정, copilot 등은 None(미지원). 파일 기반
판정은 POSIX HOME 환경에서 검증.
"""
import os
import re

import pytest


def _claude_enc(cwd: str) -> str:
    """ClaudeProvider.session_alive 와 동일한 cwd→디렉토리 인코딩(영숫자 외 '-')."""
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(cwd))

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
    d = tmp_path / ".claude" / "projects" / _claude_enc(cwd)
    d.mkdir(parents=True)
    assert p.session_alive(sid, cwd=cwd) is False          # 파일 없음 → 죽음
    (d / f"{sid}.jsonl").write_text("{}", encoding="utf-8")
    assert p.session_alive(sid, cwd=cwd) is True           # 파일 있음 → 살아있음
    assert p.session_alive("", cwd=cwd) is None             # sid 없음 → 판단불가


@_posix
def test_claude_session_alive_dotted_cwd(tmp_path, monkeypatch):
    """점(.) 포함 cwd 도 올바로 인코딩 — Claude 는 '.'도 '-'로 바꾼다.
    (단순 '/'→'-' 만 하던 버그 회귀 가드)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    p = ClaudeProvider()
    if not p.supports_sessions:
        pytest.skip("claude stateless on this platform")
    cwd = "/work/my.proj/.config"
    sid = "s1"
    enc = _claude_enc(cwd)
    assert enc != cwd.replace("/", "-"), "점이 '-'로 인코딩되어야 함"
    d = tmp_path / ".claude" / "projects" / enc
    d.mkdir(parents=True)
    (d / f"{sid}.jsonl").write_text("{}", encoding="utf-8")
    assert p.session_alive(sid, cwd=cwd) is True


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


@_posix
def test_codex_session_alive_escapes_glob_metachars(tmp_path, monkeypatch):
    """sid 의 glob 메타문자('*')가 escape 되어 죽은 세션을 살아있다고 오판 안 함."""
    monkeypatch.setenv("HOME", str(tmp_path))
    p = CodexProvider()
    sess = tmp_path / ".codex" / "sessions" / "2026" / "06" / "26"
    sess.mkdir(parents=True)
    (sess / "rollout-2026-06-26T00-00-00-realsid.jsonl").write_text("{}", encoding="utf-8")
    assert p.session_alive("*") is False       # '*' escape → 아무것도 매칭 안 함
    assert p.session_alive("realsid") is True   # 정상 sid 는 매칭


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


@_posix
def test_claude_session_alive_cwd_none_uses_getcwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    p = ClaudeProvider()
    if not p.supports_sessions:
        pytest.skip("claude stateless on this platform")
    enc = _claude_enc(os.getcwd())        # impl 과 동일 인코딩 (realpath + 영숫자외 '-')
    d = tmp_path / ".claude" / "projects" / enc
    d.mkdir(parents=True, exist_ok=True)
    (d / "sid-x.jsonl").write_text("{}", encoding="utf-8")
    assert p.session_alive("sid-x") is True   # cwd 미지정 → os.getcwd()


def test_claude_session_alive_stateless_returns_false(monkeypatch):
    """Windows(stateless) 시뮬: 저장 sid 가 있어도 재개 불가 → False."""
    p = ClaudeProvider()
    monkeypatch.setattr(p, "supports_sessions", False)
    assert p.session_alive("abc", cwd="/x") is False
    assert p.session_alive("") is None        # sid 없음 → 판단불가


def test_client_session_alive_via_conversation_id():
    prov = _FakeProvider()
    client = _client_with(prov)
    client.chat("hi", provider="fake", owner="o", alias="a")
    conv = client._store.find_by_alias("o", "a")
    assert client.session_alive("fake", conversation_id=conv.id) is True


def test_client_session_alive_none_passthrough():
    class _NoneProvider(_FakeProvider):
        def session_alive(self, session_id, *, cwd=None):
            return None

    prov = _NoneProvider()
    client = _client_with(prov)
    client.chat("hi", provider="fake", owner="o", alias="a")
    assert client.session_alive("fake", owner="o", alias="a") is None
