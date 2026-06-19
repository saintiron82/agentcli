"""Fallback 상호 스위칭 + 실패 사유 포착 테스트.

원칙:
  - `fallback=True`인 호출만 체인의 다른 provider로 전환
  - 체인 순서와 무관하게 primary 제외 모든 provider 시도
  - 모두 실패 시 가장 마지막 실패 사유를 LLMResponse.error에 보존
"""

import asyncio
from unittest.mock import patch, MagicMock
from agentcli.client import LLMClient
from agentcli.providers.base import LLMProvider
from agentcli.providers.codex import CodexProvider, _parse_jsonl_events
from agentcli.providers.claude import _parse_claude_json
from agentcli.providers.copilot import _parse_copilot_jsonl
from agentcli.providers.registry import ProviderRegistry
from agentcli.store.memory import MemoryStore
from agentcli.types import (LLMResponse, TokenUsage, Message,
                              classify_error, ERROR_USAGE_LIMIT,
                              ERROR_TIMEOUT, ERROR_AUTH)


# ===== classify_error =====

def test_classify_usage_limit():
    assert classify_error("You've hit your usage limit. Upgrade...") == ERROR_USAGE_LIMIT
    assert classify_error("rate limit exceeded") == ERROR_USAGE_LIMIT
    assert classify_error("HTTP 429 too many requests") == ERROR_USAGE_LIMIT
    assert classify_error("quota exhausted") == ERROR_USAGE_LIMIT


def test_classify_auth():
    assert classify_error("HTTP 401 Unauthorized") == ERROR_AUTH
    assert classify_error("invalid API key provided") == ERROR_AUTH


def test_classify_timeout():
    assert classify_error("request timed out") == ERROR_TIMEOUT


def test_classify_unknown():
    assert classify_error("something weird happened") == "unknown"
    assert classify_error("") == ""


# ===== Codex parser: turn.failed/error 추출 =====

def test_codex_parser_extracts_turn_failed():
    stdout = '\n'.join([
        '{"type":"thread.started","thread_id":"tid-1"}',
        '{"type":"turn.started"}',
        '{"type":"error","message":"You\'ve hit your usage limit. Try again at 12:29 PM."}',
        '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. Try again at 12:29 PM."}}',
    ])
    r = _parse_jsonl_events(stdout)
    assert r["text"] == ""
    assert r["thread_id"] == "tid-1"
    assert "usage limit" in r["error"]


def test_codex_parser_no_error_when_success():
    stdout = '\n'.join([
        '{"type":"thread.started","thread_id":"tid-2"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
    ])
    r = _parse_jsonl_events(stdout)
    assert r["text"] == "ok"
    assert r["error"] == ""


# ===== Claude parser: is_error 감지 =====

def test_claude_parser_is_error():
    stdout = '{"is_error":true,"result":"You hit your usage limit","subtype":"error_max_turns"}'
    text, tokens, err = _parse_claude_json(stdout)
    assert "usage limit" in err.lower()


def test_claude_parser_subtype_error():
    stdout = '{"subtype":"error_during_execution","result":"something failed"}'
    _, _, err = _parse_claude_json(stdout)
    assert err == "something failed"


def test_claude_parser_no_error():
    stdout = '{"result":"hello","subtype":"success","usage":{"input_tokens":5,"output_tokens":2}}'
    text, tokens, err = _parse_claude_json(stdout)
    assert text == "hello"
    assert err == ""


# ===== Copilot parser: error/exitCode 감지 =====

def test_copilot_parser_extracts_error_event():
    stdout = '\n'.join([
        '{"type":"error","data":{"message":"unauthorized 401"}}',
        '{"type":"result","sessionId":"s1","exitCode":1}',
    ])
    r = _parse_copilot_jsonl(stdout)
    assert "unauthorized" in r["error"].lower()


def test_copilot_parser_exit_code_only():
    stdout = '{"type":"result","sessionId":"s1","exitCode":2}'
    r = _parse_copilot_jsonl(stdout)
    assert "exit=2" in r["error"]


def test_copilot_parser_success():
    stdout = '\n'.join([
        '{"type":"assistant.message","data":{"content":"hi","outputTokens":1}}',
        '{"type":"result","sessionId":"s1","exitCode":0}',
    ])
    r = _parse_copilot_jsonl(stdout)
    assert r["text"] == "hi"
    assert r["error"] == ""


# ===== Provider 모킹: error 노출 =====

@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_codex_invoke_returns_error_on_usage_limit(mock_env, mock_run, mock_find):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=(
            '{"type":"thread.started","thread_id":"tid"}\n'
            '{"type":"error","message":"You\'ve hit your usage limit"}\n'
            '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit"}}\n'
        ),
        stderr="")
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="x")])
    assert resp.content == ""
    assert "usage limit" in resp.error.lower()
    assert resp.error_type == ERROR_USAGE_LIMIT


@patch("agentcli.providers.codex.CodexProvider._find_binary", return_value="/usr/bin/codex")
@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_codex_invoke_error_suppresses_partial_text(mock_env, mock_run, mock_find):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=(
            '{"type":"thread.started","thread_id":"tid"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}\n'
            '{"type":"turn.failed","error":{"message":"HTTP 401 unauthorized"}}\n'
        ),
        stderr="")
    p = CodexProvider()
    resp = p.invoke([Message(role="user", content="x")])
    assert resp.content == ""
    assert resp.error_type == ERROR_AUTH


# ===== LLMClient 상호 스위칭 =====

class FakeProvider(LLMProvider):
    def __init__(self, provider_id: str, *,
                 supports_sessions=True,
                 fail_with_error: str = "",
                 succeed_text: str = "ok"):
        self.provider_id = provider_id
        self.supports_sessions = supports_sessions
        self._fail = fail_with_error
        self._succeed = succeed_text
        self.call_count = 0

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None, alias=""):
        self.call_count += 1
        if self._fail:
            return LLMResponse(
                content="", provider=self.provider_id, model=model,
                error=self._fail, error_type=classify_error(self._fail))
        return LLMResponse(
            content=self._succeed, provider=self.provider_id, model=model,
            tokens=TokenUsage(total_tokens=10),
            session_id=session_id or "sid")

    async def invoke_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None, alias=""):
        return self.invoke(messages, model=model, timeout=timeout,
                           session_id=session_id, cwd=cwd, alias=alias)

    def list_models(self): return []
    def is_available(self): return True


def _build_client(providers, fallback_chain):
    reg = ProviderRegistry()
    for p in providers:
        reg.register(p)
    reg.set_fallback_order(fallback_chain)
    return LLMClient(store=MemoryStore(), registry=reg)


def test_fallback_switches_when_primary_hits_usage_limit():
    """primary(codex)가 한도 초과 → 명시 fallback 호출만 provider 전환."""
    codex = FakeProvider("codex",
                          fail_with_error="You've hit your usage limit")
    claude = FakeProvider("claude", succeed_text="from-claude")
    copilot = FakeProvider("copilot", succeed_text="from-copilot")
    client = _build_client([claude, copilot, codex],
                            ["claude", "copilot", "codex"])

    resp = client.chat("hi", provider="codex", owner="u",
                       conversation_id="c1", fallback=True)
    # codex 시도 → 한도 → claude로 fallback → 성공
    assert resp.content == "from-claude"
    assert resp.provider == "claude"
    assert codex.call_count == 1
    assert claude.call_count == 1
    assert copilot.call_count == 0  # 첫 fallback에서 성공했으므로 안 시도


def test_fallback_when_primary_is_last_in_chain():
    """체인에서 primary가 마지막이어도 다른 provider로 시도 (구버전 버그 수정 검증)."""
    codex = FakeProvider("codex",
                          fail_with_error="usage limit")
    claude = FakeProvider("claude", succeed_text="from-claude")
    # 체인 [claude, codex] — codex가 마지막
    client = _build_client([claude, codex], ["claude", "codex"])

    resp = client.chat("hi", provider="codex", owner="u",
                       conversation_id="c2", fallback=True)
    # codex(primary) 실패 → claude로 fallback
    assert resp.content == "from-claude"
    assert resp.provider == "claude"


def test_fallback_skips_primary_in_chain():
    """fallback은 primary를 다시 시도하지 않는다."""
    codex = FakeProvider("codex", fail_with_error="usage limit")
    claude = FakeProvider("claude", succeed_text="ok")
    client = _build_client([claude, codex], ["codex", "claude"])

    resp = client.chat("hi", provider="codex", owner="u",
                       conversation_id="c3", fallback=True)
    # codex 1회 (primary) + claude 1회 (fallback). codex는 다시 안 시도
    assert codex.call_count == 1
    assert claude.call_count == 1
    assert resp.content == "ok"


def test_all_providers_fail_returns_last_error():
    """모든 provider 실패 시 마지막 실패 사유 보존."""
    codex = FakeProvider("codex", fail_with_error="usage limit")
    claude = FakeProvider("claude", fail_with_error="HTTP 401 unauthorized")
    client = _build_client([codex, claude], ["codex", "claude"])

    resp = client.chat("hi", provider="codex", owner="u",
                       conversation_id="c4", fallback=True)
    assert resp.content == ""
    assert resp.error  # 마지막 실패 사유가 들어 있음
    # 마지막 시도가 claude였으니 그 에러 타입이 들어야 함
    assert resp.error_type == ERROR_AUTH


def test_chat_async_also_switches():
    codex = FakeProvider("codex", fail_with_error="usage limit")
    claude = FakeProvider("claude", succeed_text="async-ok")
    client = _build_client([claude, codex], ["claude", "codex"])

    resp = asyncio.run(client.chat_async(
        "hi", provider="codex", owner="u", conversation_id="c5",
        fallback=True))
    assert resp.content == "async-ok"
    assert resp.provider == "claude"


def test_failure_doesnt_pollute_store():
    """모든 provider 실패한 호출은 저장소에 기록 남기지 않음."""
    codex = FakeProvider("codex", fail_with_error="usage limit")
    claude = FakeProvider("claude", fail_with_error="auth")
    store = MemoryStore()
    reg = ProviderRegistry()
    reg.register(codex)
    reg.register(claude)
    reg.set_fallback_order(["codex", "claude"])
    client = LLMClient(store=store, registry=reg)

    resp = client.chat("hi", provider="codex", owner="u",
                       conversation_id="c-dead", alias="d",
                       fallback=True)
    assert resp.content == ""
    # 신규 실패 호출은 conversation 자체도 되돌리고 usage도 기록하지 않는다.
    assert resp.conversation_id == ""
    assert store.get("c-dead") is None
    assert store.get_messages("c-dead") == []
    stats = store.get_token_stats("u")
    assert stats["total_calls"] == 0
