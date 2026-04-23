"""토큰 사용량 4축 집계 + cached_tokens 추적 회귀 테스트."""

import asyncio
from agentcli import (
    LLMClient, MemoryStore, SQLiteStore,
    LLMResponse, TokenUsage,
)
from agentcli.providers.base import LLMProvider
from agentcli.providers.registry import ProviderRegistry


# ===== Provider: 다양한 model/provider를 시뮬레이트 =====

class TaggedProvider(LLMProvider):
    """호출 때마다 다른 model/cached_tokens를 반환하도록 설정 가능."""
    def __init__(self, provider_id: str, supports_sessions=True):
        self.provider_id = provider_id
        self.supports_sessions = supports_sessions
        self.next_cached = 0
        self.next_model = ""

    def invoke(self, messages, *, model="", timeout=120, session_id="",
               cwd=None, alias=""):
        return LLMResponse(
            content="ok", provider=self.provider_id,
            model=model or self.next_model,
            tokens=TokenUsage(prompt_tokens=100, completion_tokens=50,
                              total_tokens=150,
                              cached_tokens=self.next_cached),
            latency_ms=10,
            session_id=session_id or "sid-x")

    async def invoke_async(self, messages, *, model="", timeout=120,
                           session_id="", cwd=None, alias=""):
        return self.invoke(messages, model=model, timeout=timeout,
                            session_id=session_id, cwd=cwd, alias=alias)

    def list_models(self): return []
    def is_available(self): return True


def _build_client(providers: list[TaggedProvider], store=None) -> LLMClient:
    reg = ProviderRegistry()
    for p in providers:
        reg.register(p)
    reg.set_fallback_order([p.provider_id for p in providers])
    return LLMClient(store=store or MemoryStore(), registry=reg)


# ===== cached_tokens 파이프라인 =====

def test_cached_tokens_stored_in_usage():
    p = TaggedProvider("claude")
    p.next_cached = 30
    store = MemoryStore()
    client = _build_client([p], store=store)

    client.chat("hi", provider="claude", owner="u",
                conversation_id="c1", alias="bull")

    stats = client.get_token_stats("u")
    assert stats["total_cached"] == 30
    assert stats["total_prompt"] == 100
    assert stats["total_completion"] == 50
    assert stats["total_tokens"] == 150


def test_cached_tokens_sqlite_stored():
    """SQLiteStore도 cached_tokens 컬럼에 저장."""
    p = TaggedProvider("claude")
    p.next_cached = 20
    store = SQLiteStore(":memory:")
    client = _build_client([p], store=store)

    client.chat("hi", provider="claude", owner="u",
                conversation_id="c1", alias="x")
    stats = client.get_token_stats("u")
    assert stats["total_cached"] == 20


# ===== 필터 =====

def test_filter_by_provider():
    claude = TaggedProvider("claude")
    codex = TaggedProvider("codex")
    store = MemoryStore()
    client = _build_client([claude, codex], store=store)

    client.chat("a", provider="claude", owner="u", conversation_id="c-a")
    client.chat("b", provider="codex", owner="u", conversation_id="c-b")
    client.chat("c", provider="codex", owner="u", conversation_id="c-c")

    # 전체: 3 calls
    total = client.get_token_stats("u")
    assert total["total_calls"] == 3

    # codex만: 2 calls
    codex_only = client.get_token_stats("u", provider="codex")
    assert codex_only["total_calls"] == 2


def test_filter_by_alias():
    p = TaggedProvider("claude")
    client = _build_client([p])

    client.chat("a", provider="claude", owner="team", alias="bull")
    client.chat("b", provider="claude", owner="team", alias="bear")
    client.chat("c", provider="claude", owner="team", alias="bull")

    bull = client.get_token_stats("team", alias="bull")
    assert bull["total_calls"] == 2


def test_filter_by_model():
    p = TaggedProvider("claude")
    client = _build_client([p])

    client.chat("a", provider="claude", model="sonnet", owner="u",
                conversation_id="c1")
    client.chat("b", provider="claude", model="opus", owner="u",
                conversation_id="c2")

    sonnet = client.get_token_stats("u", model="sonnet")
    assert sonnet["total_calls"] == 1


# ===== group_by =====

def test_group_by_provider():
    claude = TaggedProvider("claude")
    codex = TaggedProvider("codex")
    client = _build_client([claude, codex])

    client.chat("a", provider="claude", owner="u", conversation_id="c1")
    client.chat("b", provider="codex", owner="u", conversation_id="c2")
    client.chat("c", provider="codex", owner="u", conversation_id="c3")

    stats = client.get_token_stats("u", group_by="provider")
    assert stats["group_by"] == "provider"
    assert "claude" in stats["groups"]
    assert "codex" in stats["groups"]
    assert stats["groups"]["codex"]["total_calls"] == 2
    assert stats["groups"]["claude"]["total_calls"] == 1


def test_group_by_alias():
    p = TaggedProvider("claude")
    client = _build_client([p])

    client.chat("a", provider="claude", owner="team", alias="bull")
    client.chat("b", provider="claude", owner="team", alias="bear")
    client.chat("c", provider="claude", owner="team", alias="trader")
    client.chat("d", provider="claude", owner="team", alias="bull")

    stats = client.get_token_stats("team", group_by="alias")
    assert stats["groups"]["bull"]["total_calls"] == 2
    assert stats["groups"]["bear"]["total_calls"] == 1
    assert stats["groups"]["trader"]["total_calls"] == 1


def test_group_by_model():
    p = TaggedProvider("claude")
    client = _build_client([p])

    client.chat("a", provider="claude", model="sonnet", owner="u",
                conversation_id="c1")
    client.chat("b", provider="claude", model="sonnet", owner="u",
                conversation_id="c2")
    client.chat("c", provider="claude", model="opus", owner="u",
                conversation_id="c3")

    stats = client.get_token_stats("u", group_by="model")
    assert stats["groups"]["sonnet"]["total_calls"] == 2
    assert stats["groups"]["opus"]["total_calls"] == 1


def test_group_by_day():
    p = TaggedProvider("claude")
    client = _build_client([p])

    client.chat("a", provider="claude", owner="u", conversation_id="c1")
    client.chat("b", provider="claude", owner="u", conversation_id="c2")

    stats = client.get_token_stats("u", group_by="day")
    # 오늘 하루만 있을 것
    assert len(stats["groups"]) == 1
    for day, bucket in stats["groups"].items():
        assert bucket["total_calls"] == 2


def test_group_by_agent():
    p = TaggedProvider("claude")
    client = _build_client([p])

    client.chat("bull 분석", provider="claude", owner="team",
                conversation_id="shared", agent="bull")
    client.chat("bear 분석", provider="claude", owner="team",
                conversation_id="shared", agent="bear")
    client.chat("trader 결정", provider="claude", owner="team",
                conversation_id="shared", agent="trader")

    stats = client.get_token_stats("team", group_by="agent")
    assert len(stats["groups"]) == 3
    for a in ("bull", "bear", "trader"):
        assert stats["groups"][a]["total_calls"] == 1


# ===== 혼합: 필터 + group_by =====

def test_filter_plus_group_by():
    claude = TaggedProvider("claude")
    codex = TaggedProvider("codex")
    client = _build_client([claude, codex])

    client.chat("a", provider="claude", owner="u", alias="bull",
                model="sonnet", conversation_id="c1")
    client.chat("b", provider="codex", owner="u", alias="bull",
                model="o3", conversation_id="c2")
    client.chat("c", provider="codex", owner="u", alias="bear",
                model="o3", conversation_id="c3")

    # codex만 필터 + alias로 그룹
    stats = client.get_token_stats("u", provider="codex", group_by="alias")
    assert stats["total_calls"] == 2
    assert stats["groups"]["bull"]["total_calls"] == 1
    assert stats["groups"]["bear"]["total_calls"] == 1


# ===== 하위 호환 =====

def test_backward_compat_by_provider_key():
    """기존 by_provider 키는 계속 존재 (deprecated지만 호환)."""
    p = TaggedProvider("claude")
    client = _build_client([p])
    client.chat("a", provider="claude", owner="u", conversation_id="c1")

    stats = client.get_token_stats("u")
    assert "by_provider" in stats
    assert "claude" in stats["by_provider"]


def test_sqlite_backward_compat_no_args():
    """기존 호출 `get_token_stats("owner", days)` 문법이 깨지지 않음."""
    p = TaggedProvider("claude")
    store = SQLiteStore(":memory:")
    client = _build_client([p], store=store)
    client.chat("a", provider="claude", owner="u", conversation_id="c1")

    # 기존 인터페이스
    stats = store.get_token_stats("u", 7)
    assert stats["total_calls"] == 1


# ===== latency 집계 =====

def test_latency_aggregated():
    p = TaggedProvider("claude")
    client = _build_client([p])
    for i in range(3):
        client.chat(f"q{i}", provider="claude", owner="u",
                    conversation_id=f"c{i}")
    stats = client.get_token_stats("u")
    assert stats["total_latency_ms"] >= 30  # 각 호출 10ms
